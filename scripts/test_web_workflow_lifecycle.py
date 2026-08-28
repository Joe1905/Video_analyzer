#!/usr/bin/env python3
"""Deterministic HTTP lifecycle checks for V2 workflow endpoints.

This test deliberately starts ``web_app.Handler`` in-process with an isolated
``APP_TEST_ROOT``.  Every external worker is replaced with a small local fake;
it must never require credentials, network access, or deployed 4004 data.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def request(
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    if body is None and payload is not None:
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    headers = {"Content-Type": content_type} if content_type else {}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def json_request(
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    **request_kwargs: Any,
) -> tuple[int, dict[str, str], Any]:
    status, headers, response_body = request(port, method, path, payload, **request_kwargs)
    return status, headers, json.loads(response_body.decode("utf-8"))


def multipart_video(filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----v2-workflow-test-boundary"
    body = b"".join(
        (
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="video"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: video/mp4\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return body, f"multipart/form-data; boundary={boundary}"


def sse_payload(port: int, path: str) -> dict[str, Any]:
    status, headers, body = request(port, "GET", path)
    assert status == 200
    assert headers.get("content-type", "").startswith("text/event-stream")
    events = [line[6:] for line in body.decode("utf-8").splitlines() if line.startswith("data: ")]
    assert events, body
    return json.loads(events[-1])


def wait_for_job(port: int, path: str, terminal_status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, _headers, payload = json_request(port, "GET", path)
        assert status == 200
        if payload.get("status") == terminal_status:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {terminal_status}: {path}")


class FakeVideoQueue:
    def __init__(self, output_dir_for_filename: Any) -> None:
        self.calls: list[tuple[str, str]] = []
        self.statuses: dict[str, str] = {}
        self._output_dir_for_filename = output_dir_for_filename

    def enqueue(self, filename: str, job_type: str) -> None:
        self.calls.append((filename, job_type))
        self.statuses[filename] = "queued_analyze" if job_type == "analyze" else "queued_report"
        output_dir = self._output_dir_for_filename(filename)
        output_dir.mkdir(parents=True, exist_ok=True)
        if job_type == "analyze":
            (output_dir / "analysis.json").write_text(
                json.dumps({"summary": "fixture analysis"}), encoding="utf-8"
            )
        elif job_type == "report":
            (output_dir / "audit_result.json").write_text(
                json.dumps({"summary": "fixture audit"}), encoding="utf-8"
            )

    def get_status(self, filename: str) -> str:
        return self.statuses.get(filename, "idle")

    def get_status_meta(self, filename: str) -> dict[str, str]:
        del filename
        return {"label": "测试队列", "color": "#000", "bg": "#fff"}

    @staticmethod
    def get_title(filename: str) -> str:
        return filename

    @staticmethod
    def get_progress() -> dict[str, Any]:
        return {}


def run_lifecycle() -> None:
    # Production result payloads expose output paths relative to the repository
    # root, so keep the isolated fixture under that same root as well.
    temp_root = Path(tempfile.mkdtemp(prefix=".test-v2-workflow-", dir=ROOT))
    server = None
    thread = None
    try:
        os.environ["UI_TEST_MODE"] = "1"
        os.environ["APP_TEST_ROOT"] = str(temp_root)
        os.environ["PROXY_POOL_ENABLED"] = "0"
        os.environ["HOT_VIDEO_REPORT_ENABLED"] = "0"
        web_app = importlib.import_module("web_app")
        fake_queue = FakeVideoQueue(web_app.output_dir_for_filename)
        download_release = threading.Event()
        shop_release = threading.Event()
        metrics_release = threading.Event()
        allowed_writes = {
            "/api/download",
            "/api/shop-extract",
            "/api/video-metrics",
            "/api/upload",
            "/api/analyze",
            "/api/translate",
            "/api/postprocess",
            "/api/delete",
        }

        def complete_download(job_id: str) -> None:
            assert download_release.wait(timeout=5)
            with web_app.download_jobs_lock:
                job = web_app.download_jobs[job_id]
                job.status = "complete"
                job.filename = "fixture-download.mp4"
                job.result = {"filename": job.filename, "source": "fixture"}
                job.log.append("fixture download complete")
                job.updated_at = time.time()

        def complete_shop(job_id: str) -> None:
            assert shop_release.wait(timeout=5)
            with web_app.shop_jobs_lock:
                job = web_app.shop_jobs[job_id]
                output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / job.id
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "shop_extract.json").write_text(
                    json.dumps({"items": [{"id": "fixture-shop"}]}), encoding="utf-8"
                )
                job.status = "complete"
                job.output_dir = "fixture/shop"
                job.log.append("fixture shop complete")
                job.updated_at = time.time()

        def fail_metrics(job_id: str) -> None:
            assert metrics_release.wait(timeout=5)
            with web_app.metrics_jobs_lock:
                job = web_app.metrics_jobs[job_id]
                job.status = "failed"
                job.error = "fixture metrics failure"
                job.log.append(job.error)
                job.updated_at = time.time()

        def fake_translate(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps({"summary": "fixture translation"}), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with ExitStack() as patches:
            patches.enter_context(
                patch.object(web_app, "ui_test_mode_allows_live_write", side_effect=lambda path: path in allowed_writes)
            )
            patches.enter_context(patch.object(web_app, "run_download_job", side_effect=complete_download))
            patches.enter_context(patch.object(web_app, "run_shop_job", side_effect=complete_shop))
            patches.enter_context(patch.object(web_app, "run_metrics_job", side_effect=fail_metrics))
            patches.enter_context(patch.object(web_app, "video_queue", fake_queue))
            patches.enter_context(patch.object(web_app, "subprocess", SimpleNamespace(run=fake_translate)))
            patches.enter_context(patch.object(web_app, "ensure_analyzer_media_or_delete", side_effect=lambda _path: None))
            patches.enter_context(patch.object(web_app, "register_video", side_effect=lambda **_kwargs: None))
            patches.enter_context(patch.object(web_app, "make_web_manual_visible", side_effect=lambda *_args: None))
            patches.enter_context(patch.object(web_app, "start_social_context_job", side_effect=lambda *_args, **_kwargs: None))
            patches.enter_context(patch.object(web_app, "analyzer_visible_source", side_effect=lambda _name: True))
            patches.enter_context(patch.object(web_app, "analyzer_media_is_valid", side_effect=lambda _path: True))

            server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port

            status, _headers, download = json_request(
                port, "POST", "/api/download", {"url": "https://www.tiktok.com/@fixture/video/123"}
            )
            assert status == 202 and download["status"] == "queued"
            download_id = download["id"]
            download_release.set()
            job = wait_for_job(port, f"/api/download-job?id={download_id}", "complete")
            assert job["result"]["source"] == "fixture"
            assert sse_payload(port, f"/api/download-events?id={download_id}")["status"] == "complete"

            status, _headers, shop = json_request(
                port,
                "POST",
                "/api/shop-extract",
                {"url": "https://shop.tiktok.com/view/product/fixture", "analyze": False},
            )
            assert status == 202 and shop["status"] == "queued"
            shop_id = shop["id"]
            shop_release.set()
            job = wait_for_job(port, f"/api/shop-job?id={shop_id}", "complete")
            assert job["extract"]["items"][0]["id"] == "fixture-shop"
            assert sse_payload(port, f"/api/shop-events?id={shop_id}")["status"] == "complete"

            status, _headers, metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "https://www.tiktok.com/@fixture/video/123", "endpoint": "video-info"},
            )
            assert status == 202 and metrics["status"] == "queued"
            metrics_id = metrics["id"]
            metrics_release.set()
            job = wait_for_job(port, f"/api/video-metrics-job?id={metrics_id}", "failed")
            assert job["error"] == "fixture metrics failure"
            event = sse_payload(port, f"/api/video-metrics-events?id={metrics_id}")
            assert event["status"] == "failed" and event["error"] == "fixture metrics failure"

            upload_body, upload_type = multipart_video("fixture.mp4", b"not-a-real-video")
            status, _headers, uploaded = json_request(
                port, "POST", "/api/upload", body=upload_body, content_type=upload_type
            )
            assert status == 200 and uploaded["files"] == [{"filename": "fixture.mp4", "size": 16}]
            filename = "fixture.mp4"
            status, _headers, files = json_request(port, "GET", "/api/files")
            assert status == 200 and any(item["name"] == filename for item in files)

            status, _headers, analyzed = json_request(
                port, "POST", "/api/analyze", {"filename": filename, "analysis_prompt": "fixture"}
            )
            assert status == 202 and analyzed["queued"] == ["analyze"]
            assert fake_queue.calls[-1] == (filename, "analyze")

            status, _headers, translated = json_request(
                port, "POST", "/api/translate", {"filename": filename, "tab": "content"}
            )
            assert status == 200 and translated == {"status": "translated", "filename": filename, "tab": "content"}

            status, _headers, postprocessed = json_request(
                port, "POST", "/api/postprocess", {"filename": filename, "analysis_prompt": "fixture"}
            )
            assert status == 202 and postprocessed["status"] == "queued"
            assert fake_queue.calls[-1] == (filename, "report")

            status, _headers, result = json_request(port, "GET", f"/api/result?filename={filename}")
            assert status == 200
            assert result["analysis"]["summary"] == "fixture analysis"
            assert result["analysis_zh"]["summary"] == "fixture translation"
            assert result["audit_result"]["summary"] == "fixture audit"

            status, _headers, deleted = json_request(port, "POST", "/api/delete", {"filename": filename})
            assert status == 200 and deleted["deleted_video"] and deleted["deleted_output"]
            assert not (web_app.VIDEOS_DIR / filename).exists()
            assert not web_app.output_dir_for_filename(filename).exists()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        shutil.rmtree(temp_root, ignore_errors=False)
        assert not temp_root.exists()


def main() -> int:
    run_lifecycle()
    print("web workflow lifecycle tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
