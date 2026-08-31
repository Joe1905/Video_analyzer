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
from uuid import UUID


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


def assert_real_download_worker_registry_updates(web_app: Any, runner: Any) -> None:
    original_registry = web_app.download_job_registry
    registry = web_app.JobRegistry()
    web_app.download_job_registry = registry
    try:
        success_id = "worker-success"
        success_filename = "worker-success.mp4"
        registry.register(
            success_id,
            web_app.DownloadJob(id=success_id, url="https://www.tiktok.com/@fixture/video/worker"),
        )
        web_app.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        (web_app.VIDEOS_DIR / success_filename).write_bytes(b"fixture")

        def cached_success(_job_id: str, _url: str, _source: str, result_path: Path) -> bool:
            result_path.write_text(
                json.dumps({"filename": success_filename, "meta": {"source": "worker"}}),
                encoding="utf-8",
            )
            return True

        with patch.object(web_app, "try_cached_download_result", side_effect=cached_success):
            runner(success_id)
        success = registry.snapshot(success_id)
        assert success is not None
        assert success.status == "complete"
        assert success.filename == success_filename
        assert success.result == {"filename": success_filename, "meta": {"source": "worker"}}
        assert success.result is not None
        success.result["meta"]["source"] = "mutated"
        assert registry.snapshot(success_id).result == {
            "filename": success_filename,
            "meta": {"source": "worker"},
        }

        failure_id = "worker-failure"
        registry.register(
            failure_id,
            web_app.DownloadJob(id=failure_id, url="https://www.tiktok.com/@fixture/video/failure"),
        )

        def cached_failure(job_id: str, _url: str, _source: str, _result_path: Path) -> bool:
            web_app.append_download_log(job_id, "fixture useful failure")
            raise RuntimeError("fixture raw failure")

        with patch.object(web_app, "try_cached_download_result", side_effect=cached_failure):
            runner(failure_id)
        failure = registry.snapshot(failure_id)
        assert failure is not None
        assert failure.status == "failed"
        assert failure.error == "fixture useful failure"
        assert failure.log[-1] == "fixture raw failure"
    finally:
        web_app.download_job_registry = original_registry


def assert_real_metrics_worker_registry_updates(web_app: Any, runner: Any) -> None:
    original_registry = web_app.metrics_job_registry
    registry = web_app.JobRegistry()
    web_app.metrics_job_registry = registry
    try:
        success_id = "metrics-worker-success"
        success_target = "https://www.tiktok.com/@fixture/video/metrics-worker"
        registry.register(success_id, web_app.MetricsJob(id=success_id, target=success_target, endpoint="video-info"))
        registered_payloads: list[tuple[dict[str, Any], str]] = []

        def command_success(job_id: str, command: list[str]) -> None:
            assert job_id == success_id
            assert command[command.index("--endpoint") + 1] == "video-info"
            assert command[command.index("--url") + 1] == success_target
            result_path = Path(command[command.index("--output") + 1])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps({"metric": {"views": 7}}), encoding="utf-8")

        def record_video(payload: dict[str, Any], *, source_url: str) -> None:
            registered_payloads.append((payload, source_url))

        with patch.object(web_app, "run_metrics_command", side_effect=command_success), patch.object(
            web_app, "register_from_payload", side_effect=record_video
        ):
            runner(success_id)
        success = registry.snapshot(success_id)
        assert success is not None
        assert success.status == "complete"
        expected_output_dir = str((web_app.OUTPUT_DIR / "tiktok_api" / success_id).relative_to(web_app.ROOT))
        assert success.output_dir == expected_output_dir
        result_path = web_app.OUTPUT_DIR / "tiktok_api" / success_id / "result.json"
        assert web_app.read_json(result_path) == {"metric": {"views": 7}}
        payload = web_app.public_metrics_job(success, result=web_app.read_json(result_path))
        assert payload["result"] == {"metric": {"views": 7}}
        assert registered_payloads == [({"metric": {"views": 7}}, success_target)]
        payload["result"]["metric"]["views"] = 99
        assert web_app.read_json(result_path) == {"metric": {"views": 7}}

        class FakeMetricsProcess:
            def __init__(self, lines: list[str], returncode: int) -> None:
                self.stdout = iter(lines)
                self.returncode = returncode

            def wait(self) -> int:
                return self.returncode

        command_log_id = "metrics-command-log"
        registry.register(command_log_id, web_app.MetricsJob(id=command_log_id, target="@fixture", endpoint="profile"))
        command = ["python", "fixture-metrics.py"]
        with patch.object(
            web_app.subprocess,
            "Popen",
            return_value=FakeMetricsProcess(["fixture stdout  \n", "second stdout\r\n"], 0),
            create=True,
        ) as popen:
            web_app.run_metrics_command(command_log_id, command)
        popen.assert_called_once()
        command_log = registry.snapshot(command_log_id)
        assert command_log is not None
        assert command_log.log == ["$ python fixture-metrics.py", "fixture stdout", "second stdout"]

        command_failure_id = "metrics-command-failure"
        registry.register(command_failure_id, web_app.MetricsJob(id=command_failure_id, target="@fixture", endpoint="profile"))
        failure_command = ["python", "fixture-metrics-fail.py"]
        with patch.object(
            web_app.subprocess,
            "Popen",
            return_value=FakeMetricsProcess([], 9),
            create=True,
        ):
            try:
                web_app.run_metrics_command(command_failure_id, failure_command)
            except RuntimeError as exc:
                assert str(exc) == "Command failed with exit code 9: python fixture-metrics-fail.py"
            else:
                raise AssertionError("non-zero metrics command must fail")
        command_failure = registry.snapshot(command_failure_id)
        assert command_failure is not None
        assert command_failure.log == ["$ python fixture-metrics-fail.py"]

        command_cases = (
            ("#fixture-tag", "profile", "--hashtag", "fixture-tag"),
            ("@fixture-handle", "profile", "--handle", "fixture-handle"),
            ("music-fixture", "music-info", "--sound-id", "music-fixture"),
            ("music-videos-fixture", "music-videos", "--sound-id", "music-videos-fixture"),
            ("fixture query", "search-keyword", "--query", "fixture query"),
            ("fixture-fallback", "profile", "--handle", "fixture-fallback"),
            ("", "music-popular", None, None),
        )
        target_flags = {"--url", "--hashtag", "--handle", "--sound-id", "--query"}
        for index, (target, endpoint, expected_flag, expected_value) in enumerate(command_cases):
            job_id = f"metrics-command-{index}"
            registry.register(job_id, web_app.MetricsJob(id=job_id, target=target, endpoint=endpoint))
            commands: list[list[str]] = []

            def capture_command(captured_job_id: str, command: list[str]) -> None:
                assert captured_job_id == job_id
                commands.append(command)
                result_path = Path(command[command.index("--output") + 1])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(json.dumps({"metric": {"case": index}}), encoding="utf-8")

            with patch.object(web_app, "run_metrics_command", side_effect=capture_command):
                runner(job_id)
            assert registry.status(job_id) == "complete"
            assert len(commands) == 1
            command = commands[0]
            assert command[command.index("--endpoint") + 1] == endpoint
            if expected_flag is None:
                assert not target_flags & set(command)
            else:
                assert command[command.index(expected_flag) + 1] == expected_value

        failure_id = "metrics-worker-failure"
        registry.register(failure_id, web_app.MetricsJob(id=failure_id, target="@fixture", endpoint="profile"))

        def command_failure(job_id: str, _command: list[str]) -> None:
            web_app.append_metrics_log(job_id, "fixture prior failure")
            raise RuntimeError("fixture raw metrics failure")

        with patch.object(web_app, "run_metrics_command", side_effect=command_failure):
            runner(failure_id)
        failure = registry.snapshot(failure_id)
        assert failure is not None
        assert failure.status == "failed"
        assert failure.error == "fixture raw metrics failure"
        assert failure.log[-2:] == ["fixture prior failure", "fixture raw metrics failure"]
    finally:
        web_app.metrics_job_registry = original_registry


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
        real_download_worker = web_app.run_download_job
        real_metrics_worker = web_app.run_metrics_job
        fake_queue = FakeVideoQueue(web_app.output_dir_for_filename)
        download_release = threading.Event()
        download_started = threading.Event()
        shop_release = threading.Event()
        metrics_success_release = threading.Event()
        metrics_success_started = threading.Event()
        metrics_failure_release = threading.Event()
        metrics_failure_started = threading.Event()
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
            web_app.download_job_registry.update_fields(job_id, {"status": "running"})
            download_started.set()
            assert download_release.wait(timeout=5)
            (web_app.VIDEOS_DIR / "fixture-download.mp4").write_bytes(b"fixture")
            web_app.download_job_registry.update_fields(
                job_id,
                {
                    "status": "complete",
                    "filename": "fixture-download.mp4",
                    "result": {"filename": "fixture-download.mp4", "source": "fixture"},
                },
                final_log="fixture download complete",
            )

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

        def complete_metrics(job_id: str) -> None:
            registry = web_app.metrics_job_registry
            current = registry.snapshot(job_id)
            assert current is not None
            registry.update_fields(job_id, {"status": "running"})
            if current.target == "@fixture-failure":
                metrics_failure_started.set()
                assert metrics_failure_release.wait(timeout=5)
                registry.update_fields(
                    job_id,
                    {"status": "failed", "error": "fixture metrics failure"},
                    final_log="fixture metrics failure",
                )
                return
            metrics_success_started.set()
            assert metrics_success_release.wait(timeout=5)
            result_path = web_app.OUTPUT_DIR / "tiktok_api" / job_id / "result.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps({"metric": {"views": 7}}), encoding="utf-8")
            registry.update_fields(
                job_id,
                {
                    "status": "complete",
                    "output_dir": str(result_path.parent.relative_to(web_app.ROOT)),
                },
                final_log="fixture metrics complete",
            )

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
            patches.enter_context(patch.object(web_app, "run_metrics_job", side_effect=complete_metrics))
            patches.enter_context(patch.object(web_app, "video_queue", fake_queue))
            patches.enter_context(patch.object(web_app, "subprocess", SimpleNamespace(run=fake_translate)))
            patches.enter_context(patch.object(web_app, "ensure_analyzer_media_or_delete", side_effect=lambda _path: None))
            patches.enter_context(patch.object(web_app, "register_video", side_effect=lambda **_kwargs: None))
            patches.enter_context(patch.object(web_app, "make_web_manual_visible", side_effect=lambda *_args: None))
            patches.enter_context(patch.object(web_app, "start_social_context_job", side_effect=lambda *_args, **_kwargs: None))
            patches.enter_context(patch.object(web_app, "analyzer_visible_source", side_effect=lambda _name: True))
            patches.enter_context(patch.object(web_app, "analyzer_media_is_valid", side_effect=lambda _path: True))

            assert_real_download_worker_registry_updates(web_app, real_download_worker)
            assert_real_metrics_worker_registry_updates(web_app, real_metrics_worker)

            server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port

            invalid_id = "00000000-0000-0000-0000-000000000001"
            with patch.object(web_app.uuid, "uuid4", return_value=UUID(invalid_id)):
                status, headers, invalid_download = json_request(
                    port, "POST", "/api/download", {"url": "ftp://fixture.invalid/video"}
                )
            assert status == 400
            assert headers.get("content-type") == "application/json; charset=utf-8"
            assert invalid_download == {"error": "Only http/https short-video URLs are supported"}
            status, _headers, invalid_job = json_request(port, "GET", f"/api/download-job?id={invalid_id}")
            assert status == 200
            assert invalid_job["id"] == invalid_id
            assert invalid_job["status"] == "failed"
            assert invalid_job["error"] == invalid_download["error"]
            assert invalid_job["log"] == [invalid_download["error"]]
            status, health_headers, health = json_request(port, "GET", "/healthz")
            assert status == 200
            assert health_headers.get("content-type") == "application/json; charset=utf-8"
            assert health == {"status": "ok", "ui_test_mode": True}

            status, _headers, download = json_request(
                port, "POST", "/api/download", {"url": "https://www.tiktok.com/@fixture/video/123"}
            )
            assert status == 202 and download["status"] in {"queued", "running"}
            download_id = download["id"]
            assert download_started.wait(timeout=5)
            status, _headers, running_download = json_request(port, "GET", f"/api/download-job?id={download_id}")
            assert status == 200 and running_download["status"] == "running"
            status, _headers, running_feedback = json_request(port, "GET", f"/api/video-feedback?download_job_id={download_id}")
            assert status == 200 and running_feedback["download"]["status"] == "running"
            download_release.set()
            job = wait_for_job(port, f"/api/download-job?id={download_id}", "complete")
            assert job["result"] == {"filename": "fixture-download.mp4", "source": "fixture"}
            event = sse_payload(port, f"/api/download-events?id={download_id}")
            assert event["status"] == "complete" and event["result"] == job["result"]
            status, _headers, complete_feedback = json_request(port, "GET", f"/api/video-feedback?download_job_id={download_id}")
            assert status == 200
            assert complete_feedback["state"] == "uploaded"
            assert complete_feedback["download"] == job
            status, _headers, missing_feedback = json_request(port, "GET", "/api/video-feedback?download_job_id=missing")
            assert status == 404 and missing_feedback == {
                "ok": False,
                "state": "failed",
                "error": "Download job not found",
                "download_job_id": "missing",
            }

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

            status, _headers, invalid_metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "@fixture", "endpoint": "not-an-endpoint"},
            )
            assert status == 400 and invalid_metrics == {"error": "Unknown endpoint: not-an-endpoint"}
            status, _headers, missing_metrics_target = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"endpoint": "profile"},
            )
            assert status == 400 and missing_metrics_target == {"error": "target is required for this endpoint"}
            status, _headers, long_metrics_target = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "x" * 2049, "endpoint": "profile"},
            )
            assert status == 400 and long_metrics_target == {"error": "target is too long"}

            status, _headers, metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "https://www.tiktok.com/@fixture/video/123", "endpoint": "video-info"},
            )
            assert status == 202 and metrics["status"] in {"queued", "running"}
            metrics_id = metrics["id"]
            assert metrics_success_started.wait(timeout=5)
            status, _headers, running_metrics = json_request(port, "GET", f"/api/video-metrics-job?id={metrics_id}")
            assert status == 200 and running_metrics["status"] == "running"
            metrics_success_release.set()
            job = wait_for_job(port, f"/api/video-metrics-job?id={metrics_id}", "complete")
            expected_metrics_output_dir = str(
                (web_app.OUTPUT_DIR / "tiktok_api" / metrics_id).relative_to(web_app.ROOT)
            )
            assert job["output_dir"] == expected_metrics_output_dir
            assert job["result"] == {"metric": {"views": 7}}
            event = sse_payload(port, f"/api/video-metrics-events?id={metrics_id}")
            assert event["status"] == "complete" and event["result"] == job["result"]

            status, _headers, failed_metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "@fixture-failure", "endpoint": "profile"},
            )
            assert status == 202 and failed_metrics["status"] in {"queued", "running"}
            failed_metrics_id = failed_metrics["id"]
            assert metrics_failure_started.wait(timeout=5)
            status, _headers, running_failed_metrics = json_request(
                port, "GET", f"/api/video-metrics-job?id={failed_metrics_id}"
            )
            assert status == 200 and running_failed_metrics["status"] == "running"
            metrics_failure_release.set()
            failed_job = wait_for_job(port, f"/api/video-metrics-job?id={failed_metrics_id}", "failed")
            assert failed_job["error"] == "fixture metrics failure"
            assert failed_job["log"][-1] == "fixture metrics failure"
            failed_event = sse_payload(port, f"/api/video-metrics-events?id={failed_metrics_id}")
            assert failed_event["status"] == "failed" and failed_event["error"] == "fixture metrics failure"

            status, _headers, trending_metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "", "endpoint": "trending"},
            )
            assert status == 202 and trending_metrics["status"] in {"queued", "running"}
            wait_for_job(port, f"/api/video-metrics-job?id={trending_metrics['id']}", "complete")

            status, _headers, music_popular_metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "", "endpoint": "music-popular"},
            )
            assert status == 202 and music_popular_metrics["status"] in {"queued", "running"}
            wait_for_job(port, f"/api/video-metrics-job?id={music_popular_metrics['id']}", "complete")

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
