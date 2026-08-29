"""Regression coverage for the extracted low-level HTTP helpers."""

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from core.http import binary_response, file_response, json_response, text_response, write_sse_event


class RecordingWriter(BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.flush_count = 0

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return super().write(data)

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class DisconnectingWriter(RecordingWriter):
    def __init__(self, error: type[OSError]) -> None:
        super().__init__()
        self.error = error

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        raise self.error()


class FakeHandler:
    def __init__(
        self,
        *,
        command: str = "GET",
        headers: dict[str, str] | None = None,
        writer: RecordingWriter | None = None,
    ) -> None:
        self.command = command
        self.headers = headers or {}
        self.responses: list[object] = []
        self.sent_headers: list[tuple[str, str]] = []
        self.end_headers_count = 0
        self.wfile = writer or RecordingWriter()

    def send_response(self, status: object) -> None:
        self.responses.append(status)

    def send_header(self, key: str, value: str) -> None:
        self.sent_headers.append((key, value))

    def end_headers(self) -> None:
        self.end_headers_count += 1

    def header(self, name: str) -> str | None:
        return dict(self.sent_headers).get(name)


class CoreHttpTests(unittest.TestCase):
    def test_json_response_uses_utf8_indent_and_extra_headers(self) -> None:
        handler = FakeHandler()

        json_response(handler, 201, {"标题": "测试", "items": [1, 2]}, {"X-Test": "yes"})

        expected = b'{\n  "\xe6\xa0\x87\xe9\xa2\x98": "\xe6\xb5\x8b\xe8\xaf\x95",\n  "items": [\n    1,\n    2\n  ]\n}'
        self.assertEqual(handler.responses, [201])
        self.assertEqual(handler.sent_headers, [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(expected))),
            ("X-Test", "yes"),
        ])
        self.assertEqual(handler.wfile.getvalue(), expected)

    def test_text_response_disables_caching(self) -> None:
        handler = FakeHandler()

        text_response(handler, 200, "中文", "text/plain; charset=utf-8")

        self.assertEqual(handler.header("Content-Length"), "6")
        self.assertEqual(handler.header("Cache-Control"), "no-cache, no-store, must-revalidate")
        self.assertEqual(handler.wfile.getvalue(), "中文".encode("utf-8"))

    def test_json_and_text_responses_write_body_for_head_requests(self) -> None:
        json_handler = FakeHandler(command="HEAD")
        text_handler = FakeHandler(command="HEAD")

        json_response(json_handler, 200, {"ok": True})
        text_response(text_handler, 200, "body", "text/plain")

        self.assertEqual(json_handler.wfile.getvalue(), b'{\n  "ok": true\n}')
        self.assertEqual(text_handler.wfile.getvalue(), b"body")

    def test_json_and_text_responses_swallow_disconnects_after_headers(self) -> None:
        cases = (
            (
                "json-broken-pipe",
                FakeHandler(writer=DisconnectingWriter(BrokenPipeError)),
                lambda handler: json_response(handler, 200, {"ok": True}),
            ),
            (
                "text-connection-reset",
                FakeHandler(writer=DisconnectingWriter(ConnectionResetError)),
                lambda handler: text_response(handler, 200, "body", "text/plain"),
            ),
        )
        for label, handler, send in cases:
            with self.subTest(label=label):
                send(handler)
                self.assertEqual(handler.responses, [200])
                self.assertEqual(handler.end_headers_count, 1)

    def test_json_serialization_errors_still_propagate_before_headers(self) -> None:
        handler = FakeHandler()

        with self.assertRaises(TypeError):
            json_response(handler, 200, {"invalid": object()})

        self.assertEqual(handler.responses, [])
        self.assertEqual(handler.end_headers_count, 0)

    def test_binary_response_honors_head_and_sanitizes_filename(self) -> None:
        handler = FakeHandler(command="HEAD")

        binary_response(handler, 200, b"abc", "image/png", 'a"b.png', "public, max-age=60")

        self.assertEqual(handler.header("Content-Disposition"), 'attachment; filename="ab.png"')
        self.assertEqual(handler.header("Cache-Control"), "public, max-age=60")
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_binary_response_swallows_broken_pipe_after_headers(self) -> None:
        handler = FakeHandler(writer=DisconnectingWriter(BrokenPipeError))

        binary_response(handler, 200, b"abc", "application/octet-stream")

        self.assertEqual(handler.responses, [200])
        self.assertEqual(handler.end_headers_count, 1)
        self.assertEqual(handler.header("Content-Length"), "3")

    def test_file_response_range_download_and_streams_in_megabyte_chunks(self) -> None:
        content = (b"0123456789abcdef" * (2 * 1024 * 1024 // 16 + 1))[:2 * 1024 * 1024 + 17]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(content)
            handler = FakeHandler(headers={"Range": "bytes=1048570-2097153"})

            file_response(handler, path, "application/octet-stream", "中文 文件.bin", len(content))

        expected = content[1048570:2097154]
        self.assertEqual(handler.responses, [206])
        self.assertEqual(handler.header("Content-Range"), f"bytes 1048570-2097153/{len(content)}")
        self.assertEqual(handler.header("Content-Length"), str(len(expected)))
        self.assertEqual(
            handler.header("Content-Disposition"),
            "attachment; filename=download; filename*=UTF-8''%E4%B8%AD%E6%96%87%20%E6%96%87%E4%BB%B6.bin",
        )
        self.assertEqual(handler.wfile.getvalue(), expected)
        self.assertTrue(all(len(chunk) <= 1024 * 1024 for chunk in handler.wfile.writes))
        self.assertEqual([len(chunk) for chunk in handler.wfile.writes], [1024 * 1024, len(expected) - 1024 * 1024])

    def test_file_response_invalid_range_returns_416(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"abc")
            handler = FakeHandler(headers={"Range": "bytes=9-10"})

            file_response(handler, path, "application/octet-stream", "payload.bin", 3)

        self.assertEqual(handler.responses, [416])
        self.assertEqual(handler.header("Content-Range"), "bytes */3")
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_file_response_rejects_invalid_and_empty_ranges(self) -> None:
        cases = (("items=0-1", 3), ("bytes=0-1,2-2", 3), ("bytes=-0", 3), ("bytes=0-0", 0))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            for range_header, size in cases:
                with self.subTest(range_header=range_header, size=size):
                    path.write_bytes(b"abc"[:size])
                    handler = FakeHandler(headers={"Range": range_header})

                    file_response(handler, path, "application/octet-stream", "payload.bin", size)

                    self.assertEqual(handler.responses, [416])
                    self.assertEqual(handler.header("Content-Range"), f"bytes */{size}")

    def test_file_response_head_does_not_open_or_write_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.bin"
            handler = FakeHandler(command="HEAD")

            file_response(handler, path, "application/octet-stream", "missing.bin", 5, download=False)

        self.assertEqual(handler.responses, [200])
        self.assertEqual(handler.header("Content-Length"), "5")
        self.assertIsNone(handler.header("Content-Disposition"))
        self.assertIsNone(handler.header("Cache-Control"))
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_file_response_swallows_connection_reset_after_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"abc")
            handler = FakeHandler(writer=DisconnectingWriter(ConnectionResetError))

            file_response(handler, path, "application/octet-stream", "payload.bin", 3)

        self.assertEqual(handler.responses, [200])
        self.assertEqual(handler.end_headers_count, 1)
        self.assertEqual(handler.header("Content-Length"), "3")

    def test_sse_event_writes_data_frame_and_flushes(self) -> None:
        handler = FakeHandler()

        write_sse_event(handler, {"消息": "好", "count": 1})

        self.assertEqual(handler.wfile.getvalue(), 'data: {"消息":"好","count":1}\n\n'.encode("utf-8"))
        self.assertEqual(handler.wfile.flush_count, 1)

    def test_sse_event_propagates_broken_pipe(self) -> None:
        handler = FakeHandler(writer=DisconnectingWriter(BrokenPipeError))

        with self.assertRaises(BrokenPipeError):
            write_sse_event(handler, {"ok": True})


if __name__ == "__main__":
    unittest.main()
