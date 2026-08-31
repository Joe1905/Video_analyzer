#!/usr/bin/env python3
"""In-process contract checks for the V2 HTTP response boundary.

These tests intentionally exercise the real response helpers and Handler methods
without binding a port or relying on external services.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from http import HTTPStatus
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import web_app  # noqa: E402


class RecordingWriter(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class FakeHandler:
    def __init__(self, *, command: str = "GET", headers: dict[str, str] | None = None, writer: RecordingWriter | None = None) -> None:
        self.command = command
        self.headers = headers or {}
        self.wfile = writer or RecordingWriter()
        self.responses: list[int] = []
        self.response_headers: list[tuple[str, str]] = []
        self.ended = False
        self.close_connection = False

    def send_response(self, status: int) -> None:
        self.responses.append(int(status))

    def send_header(self, key: str, value: str) -> None:
        self.response_headers.append((key, value))

    def end_headers(self) -> None:
        self.ended = True

    def header(self, key: str) -> str | None:
        return dict(self.response_headers).get(key)


def assert_response(handler: FakeHandler, status: int) -> None:
    assert handler.responses == [status], handler.responses
    assert handler.ended


def test_json_text_and_binary_contracts() -> None:
    json_handler = FakeHandler()
    web_app.json_response(json_handler, HTTPStatus.CREATED, {"message": "中文", "ok": True})
    assert_response(json_handler, HTTPStatus.CREATED)
    assert json_handler.header("Content-Type") == "application/json; charset=utf-8"
    assert int(json_handler.header("Content-Length") or -1) == len(json_handler.wfile.getvalue())
    assert json.loads(json_handler.wfile.getvalue().decode("utf-8")) == {"message": "中文", "ok": True}

    text_handler = FakeHandler()
    web_app.text_response(text_handler, HTTPStatus.OK, "hello 中文", "text/plain; charset=utf-8")
    assert_response(text_handler, HTTPStatus.OK)
    assert text_handler.header("Content-Type") == "text/plain; charset=utf-8"
    assert text_handler.header("Cache-Control") == "no-cache, no-store, must-revalidate"
    assert text_handler.wfile.getvalue() == "hello 中文".encode("utf-8")

    binary_handler = FakeHandler()
    web_app.binary_response(
        binary_handler,
        HTTPStatus.OK,
        b"binary-body",
        "application/octet-stream",
        filename='report".bin',
        cache_control="no-store",
    )
    assert_response(binary_handler, HTTPStatus.OK)
    assert binary_handler.header("Content-Disposition") == 'attachment; filename="report.bin"'
    assert binary_handler.header("Cache-Control") == "no-store"
    assert binary_handler.wfile.getvalue() == b"binary-body"

    binary_head = FakeHandler(command="HEAD")
    web_app.binary_response(binary_head, HTTPStatus.OK, b"binary-body", "application/octet-stream")
    assert_response(binary_head, HTTPStatus.OK)
    assert binary_head.header("Content-Length") == str(len(b"binary-body"))
    assert binary_head.wfile.getvalue() == b""


def test_file_response_head_range_and_disposition_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="http-contract-") as temporary:
        path = Path(temporary) / "payload.bin"
        payload = b"0123456789"
        path.write_bytes(payload)

        full = FakeHandler()
        web_app.file_response(full, path, "application/octet-stream", "报告 01.bin", len(payload))
        assert_response(full, HTTPStatus.OK)
        assert full.header("Accept-Ranges") == "bytes"
        assert full.header("Content-Length") == "10"
        assert full.header("Content-Disposition") == "attachment; filename=download; filename*=UTF-8''%E6%8A%A5%E5%91%8A%2001.bin"
        assert full.header("Cache-Control") == "no-store"
        assert full.wfile.getvalue() == payload

        head = FakeHandler(command="HEAD")
        web_app.file_response(head, path, "application/octet-stream", "报告 01.bin", len(payload))
        assert_response(head, HTTPStatus.OK)
        assert head.header("Content-Length") == "10"
        assert head.wfile.getvalue() == b""

        partial = FakeHandler(headers={"Range": "bytes=2-5"})
        web_app.file_response(partial, path, "application/octet-stream", "payload.bin", len(payload))
        assert_response(partial, HTTPStatus.PARTIAL_CONTENT)
        assert partial.header("Content-Length") == "4"
        assert partial.header("Content-Range") == "bytes 2-5/10"
        assert partial.wfile.getvalue() == b"2345"

        suffix = FakeHandler(headers={"Range": "bytes=-3"})
        web_app.file_response(suffix, path, "application/octet-stream", "payload.bin", len(payload))
        assert_response(suffix, HTTPStatus.PARTIAL_CONTENT)
        assert suffix.header("Content-Range") == "bytes 7-9/10"
        assert suffix.wfile.getvalue() == b"789"

        invalid = FakeHandler(headers={"Range": "bytes=10-"})
        web_app.file_response(invalid, path, "application/octet-stream", "payload.bin", len(payload))
        assert_response(invalid, HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        assert invalid.header("Content-Range") == "bytes */10"
        assert invalid.wfile.getvalue() == b""


def test_video_handler_range_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="video-contract-") as temporary:
        path = Path(temporary) / "clip.mp4"
        path.write_bytes(b"abcdefghij")
        partial = FakeHandler(headers={"Range": "bytes=3-6"})
        web_app.Handler.serve_video(partial, path)
        assert_response(partial, HTTPStatus.PARTIAL_CONTENT)
        assert partial.header("Content-Type") == "video/mp4"
        assert partial.header("Accept-Ranges") == "bytes"
        assert partial.header("Content-Range") == "bytes 3-6/10"
        assert partial.wfile.getvalue() == b"defg"

        invalid = FakeHandler(headers={"Range": "bytes=99-"})
        web_app.Handler.serve_video(invalid, path)
        assert_response(invalid, HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        assert invalid.header("Content-Range") == "bytes */10"


def test_sse_frame_flush_and_disconnect_contract() -> None:
    event_handler = FakeHandler()
    web_app.write_sse_event(event_handler, {"status": "done", "label": "中文"})
    assert event_handler.wfile.getvalue() == b'data: {"status":"done","label":' + "\"中文\"".encode("utf-8") + b"}\n\n"
    assert event_handler.wfile.flush_count == 1


def main() -> int:
    test_json_text_and_binary_contracts()
    test_file_response_head_range_and_disposition_contract()
    test_video_handler_range_contract()
    test_sse_frame_flush_and_disconnect_contract()
    print("http response contract tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
