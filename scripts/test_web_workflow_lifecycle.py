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
import subprocess
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

from services.shop import ShopJob, ShopService


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
        with patch.object(web_app, "subprocess", subprocess), patch.object(
            subprocess,
            "Popen",
            return_value=FakeMetricsProcess(["fixture stdout  \n", "second stdout\r\n"], 0),
        ) as popen:
            web_app.run_metrics_command(command_log_id, command)
        popen.assert_called_once()
        command_log = registry.snapshot(command_log_id)
        assert command_log is not None
        assert command_log.log == ["$ python fixture-metrics.py", "fixture stdout", "second stdout"]

        command_failure_id = "metrics-command-failure"
        registry.register(command_failure_id, web_app.MetricsJob(id=command_failure_id, target="@fixture", endpoint="profile"))
        failure_command = ["python", "fixture-metrics-fail.py"]
        with patch.object(web_app, "subprocess", subprocess), patch.object(
            subprocess,
            "Popen",
            return_value=FakeMetricsProcess([], 9),
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
            expected_command = [
                "python",
                str(web_app.SCRIPTS_DIR / "sociavault_tiktok.py"),
                "--endpoint",
                endpoint,
                "--output",
                str(web_app.OUTPUT_DIR / "tiktok_api" / job_id / "result.json"),
            ]
            if expected_flag is None:
                assert not target_flags & set(command)
            else:
                expected_command.extend([expected_flag, expected_value])
            assert command == expected_command

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


def assert_real_shop_worker_registry_updates(web_app: Any) -> None:
    registry = web_app.JobRegistry()
    service = ShopService(
        registry, web_app.ROOT, web_app.OUTPUT_DIR, web_app.SCRIPTS_DIR,
        web_app.read_json, subprocess.Popen, threading.Thread, lambda: "unused",
    )
    success_id = "shop-worker-success"
    success_url = "https://shop.tiktok.com/view/product/worker"
    success_prompt = "private worker prompt"
    registry.register(success_id, ShopJob(success_id, success_url, "product", "US", 20, 20, True, True, success_prompt))
    output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / success_id
    extract_path, analysis_path = output_dir / "shop_extract.json", output_dir / "shop_analysis.json"
    commands: list[list[str]] = []

    def command_success(job_id: str, command: list[str]) -> None:
        assert job_id == success_id
        commands.append(command)
        if len(commands) == 1:
            assert command == [
                "python",
                str(web_app.SCRIPTS_DIR / "sociavault_tiktok_shop.py"),
                success_url,
                "--source-type",
                "product",
                "--region",
                "US",
                "--max-pages",
                "20",
                "--review-pages",
                "20",
                "--output",
                str(extract_path),
                "--related-videos",
            ]
            extract_path.parent.mkdir(parents=True, exist_ok=True)
            extract_path.write_text(json.dumps({"items": [{"id": "worker-extract"}]}), encoding="utf-8")
        elif len(commands) == 2:
            assert command == [
                "python",
                str(web_app.SCRIPTS_DIR / "deepseek_shop_analyze.py"),
                str(extract_path),
                "--output",
                str(analysis_path),
                "--prompt",
                success_prompt,
            ]
            analysis_path.write_text(json.dumps({"summary": "worker-analysis"}), encoding="utf-8")
        else:
            raise AssertionError(f"unexpected Shop worker command: {command}")

    with patch.object(service, "run_command", side_effect=command_success):
        service.run_job(success_id)
    success = registry.snapshot(success_id)
    assert success is not None and success.status == "complete"
    assert success.output_dir == str(output_dir.relative_to(web_app.ROOT))
    assert len(commands) == 2
    payload = service.payload_for(success_id)
    assert payload is not None and payload["extract"] == {"items": [{"id": "worker-extract"}]}
    assert payload["analysis"] == {"summary": "worker-analysis"} and "prompt" not in payload

    failure_id = "shop-worker-failure"
    registry.register(failure_id, ShopJob(failure_id, success_url, "product", "US", 1, 1, False, False))

    def command_failure(job_id: str, _command: list[str]) -> None:
        service.append_log(job_id, "fixture prior shop failure")
        raise RuntimeError("fixture raw shop failure")

    with patch.object(service, "run_command", side_effect=command_failure):
        service.run_job(failure_id)
    failure = registry.snapshot(failure_id)
    assert failure is not None and failure.status == "failed"
    assert failure.error == "fixture raw shop failure"
    assert failure.log[-2:] == ["fixture prior shop failure", "fixture raw shop failure"]


def assert_real_amazon_worker_registry_updates(web_app: Any, runner: Any) -> None:
    original_registry = web_app.amazon_job_registry
    registry = web_app.JobRegistry()
    web_app.amazon_job_registry = registry
    try:
        class FakeAmazonProcess:
            def __init__(self, lines: list[str], returncode: int) -> None:
                self.stdout = iter(lines)
                self.returncode = returncode

            def wait(self) -> int:
                return self.returncode

        command_success_id = "amazon-command-success"
        command = ["docker", "fixture-amazon"]
        registry.register(
            command_success_id,
            web_app.AmazonJob(
                id=command_success_id,
                target="B000COMMAND",
                target_type="asin",
                url="https://www.amazon.com/dp/B000COMMAND",
                pages=1,
            ),
        )
        with patch.object(web_app, "subprocess", subprocess), patch.object(
            subprocess, "Popen", return_value=FakeAmazonProcess(["first stdout  \n", "second stdout\r\n"], 0)
        ) as popen:
            output, code = web_app.run_amazon_command(command_success_id, command)
        assert (output, code) == ("first stdout  \nsecond stdout\r\n", 0)
        assert popen.call_args.args == (command,)
        assert popen.call_args.kwargs["cwd"] == web_app.ROOT
        assert popen.call_args.kwargs["stdout"] is subprocess.PIPE
        assert popen.call_args.kwargs["stderr"] is subprocess.STDOUT
        assert popen.call_args.kwargs["text"] is True
        assert registry.snapshot(command_success_id).log == [
            "$ docker fixture-amazon", "first stdout", "second stdout",
        ]

        command_failure_id = "amazon-command-failure"
        registry.register(
            command_failure_id,
            web_app.AmazonJob(
                id=command_failure_id,
                target="B000COMMANDFAIL",
                target_type="asin",
                url="https://www.amazon.com/dp/B000COMMANDFAIL",
                pages=1,
            ),
        )
        with patch.object(web_app, "subprocess", subprocess), patch.object(
            subprocess, "Popen", return_value=FakeAmazonProcess(["failure stdout  \n"], 7)
        ) as popen:
            output, code = web_app.run_amazon_command(command_failure_id, ["docker", "fixture-amazon-fail"])
        assert (output, code) == ("failure stdout  \n", 7)
        assert popen.call_args.args == (["docker", "fixture-amazon-fail"],)
        assert registry.snapshot(command_failure_id).log == [
            "$ docker fixture-amazon-fail", "failure stdout", "Command exited with code 7",
        ]

        success_id = "amazon-worker-success"
        success_url = "https://www.amazon.com/dp/B000WORKER"
        registry.register(
            success_id,
            web_app.AmazonJob(
                id=success_id,
                target="B000WORKER",
                target_type="asin",
                url=success_url,
                pages=3,
            ),
        )
        command_payload = {"products": [{"asin": "B000WORKER", "title": "fixture product"}]}
        proxy_calls: list[str] = []
        cache_calls: list[tuple[str, str, dict[str, Any]]] = []
        commands: list[list[str]] = []

        def ensure_proxy(name: str, *, log: Any) -> None:
            proxy_calls.append(name)
            log("fixture amazon proxy ready")

        def command_success(job_id: str, command: list[str]) -> tuple[str, int]:
            assert job_id == success_id
            commands.append(command)
            assert command == [
                "docker", "run", "--rm", "--network", "host",
                "-e", "AMAZON_PROXY", "-e", "AMAZON_PROXIES",
                "amazon-scraper", "node", "assets/amazon_handler.js",
                success_url, "--pages", "3",
            ]
            return json.dumps(command_payload), 0

        def cache_success(service: str, operation: str, request: dict[str, Any], fetch: Any, *, metadata_builder: Any) -> dict[str, Any]:
            cache_calls.append((service, operation, request))
            assert metadata_builder(command_payload) == {
                "entity_type": "amazon",
                "entity_id": "B000WORKER",
                "title": "fixture product",
                "source_url": success_url,
            }
            registry.update_fields(
                success_id,
                {"url": "https://www.amazon.com/dp/B000MUTATED", "pages": 5},
            )
            return fetch()

        worker_snapshot_ids: list[str] = []
        original_snapshot = registry.snapshot

        def recording_worker_snapshot(job_id: str) -> Any:
            worker_snapshot_ids.append(job_id)
            return original_snapshot(job_id)

        with patch.object(web_app, "ensure_us_proxy", side_effect=ensure_proxy), patch.object(
            web_app, "run_amazon_command", side_effect=command_success
        ), patch.object(web_app, "get_cached_or_call", side_effect=cache_success), patch.object(
            registry, "snapshot", side_effect=recording_worker_snapshot
        ):
            runner(success_id)
        assert worker_snapshot_ids == [success_id]
        success = registry.snapshot(success_id)
        assert success is not None
        assert success.status == "complete"
        output_dir = web_app.OUTPUT_DIR / "amazon" / success_id
        assert success.output_dir == str(output_dir.relative_to(web_app.ROOT))
        assert proxy_calls == ["amazon"]
        assert cache_calls == [("amazon_scraper", "web", {"url": success_url, "pages": 3})]
        assert len(commands) == 1
        assert success.url == "https://www.amazon.com/dp/B000MUTATED"
        assert success.pages == 5
        result_path = output_dir / "result.json"
        assert web_app.read_json(result_path) == command_payload
        payload = web_app.public_amazon_job(success, result=web_app.read_json(result_path))
        assert payload["result"] == command_payload
        payload["result"]["products"][0]["title"] = "mutated"
        assert web_app.read_json(result_path) == command_payload

        error_id = "amazon-worker-result-error"
        registry.register(
            error_id,
            web_app.AmazonJob(
                id=error_id,
                target="B000ERROR1",
                target_type="asin",
                url="https://www.amazon.com/dp/B000ERROR1",
                pages=1,
            ),
        )
        with patch.object(
            web_app, "get_cached_or_call", return_value={"status": "ERROR", "message": "fixture scraper error"}
        ):
            runner(error_id)
        result_error = registry.snapshot(error_id)
        assert result_error is not None
        assert result_error.status == "failed"
        assert result_error.error == "fixture scraper error"
        assert result_error.log == []
        assert web_app.read_json(web_app.OUTPUT_DIR / "amazon" / error_id / "result.json") == {
            "status": "ERROR", "message": "fixture scraper error",
        }

        docker_missing_id = "amazon-worker-docker-missing"
        registry.register(
            docker_missing_id,
            web_app.AmazonJob(
                id=docker_missing_id,
                target="B000DOCKER",
                target_type="asin",
                url="https://www.amazon.com/dp/B000DOCKER",
                pages=1,
            ),
        )
        with patch.object(web_app, "get_cached_or_call", side_effect=FileNotFoundError):
            runner(docker_missing_id)
        docker_missing = registry.snapshot(docker_missing_id)
        assert docker_missing is not None
        assert docker_missing.status == "failed"
        assert docker_missing.error == "Docker CLI is not available in the web container"
        assert docker_missing.log[-1] == docker_missing.error

        failure_id = "amazon-worker-failure"
        registry.register(
            failure_id,
            web_app.AmazonJob(
                id=failure_id,
                target="B000FAILURE",
                target_type="asin",
                url="https://www.amazon.com/dp/B000FAILURE",
                pages=1,
            ),
        )
        with patch.object(web_app, "get_cached_or_call", side_effect=RuntimeError("fixture raw amazon failure")):
            runner(failure_id)
        failure = registry.snapshot(failure_id)
        assert failure is not None
        assert failure.status == "failed"
        assert failure.error == "fixture raw amazon failure"
        assert failure.log[-1] == failure.error
    finally:
        web_app.amazon_job_registry = original_registry


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
        real_amazon_worker = web_app.run_amazon_job
        fake_queue = FakeVideoQueue(web_app.output_dir_for_filename)
        download_release = threading.Event()
        download_started = threading.Event()
        shop_success_release = threading.Event()
        shop_success_started = threading.Event()
        shop_failure_release = threading.Event()
        shop_failure_started = threading.Event()
        metrics_success_release = threading.Event()
        metrics_success_started = threading.Event()
        metrics_failure_release = threading.Event()
        metrics_failure_started = threading.Event()
        amazon_success_release = threading.Event()
        amazon_success_started = threading.Event()
        amazon_failure_release = threading.Event()
        amazon_failure_started = threading.Event()
        allowed_writes = {
            "/api/download",
            "/api/shop-extract",
            "/api/video-metrics",
            "/api/amazon-scrape",
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
            registry = web_app.shop_job_registry
            current = registry.snapshot(job_id)
            assert current is not None
            registry.update_fields(job_id, {"status": "running"})
            if current.url.endswith("/fixture-failure"):
                shop_failure_started.set()
                assert shop_failure_release.wait(timeout=5)
                registry.update_fields(
                    job_id,
                    {"status": "failed", "error": "fixture shop failure"},
                    final_log="fixture shop failure",
                )
                return
            shop_success_started.set()
            assert shop_success_release.wait(timeout=5)
            output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / job_id
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "shop_extract.json").write_text(
                json.dumps({"items": [{"id": "fixture-shop"}]}), encoding="utf-8"
            )
            if current.analyze:
                (output_dir / "shop_analysis.json").write_text(
                    json.dumps({"summary": "fixture shop analysis"}), encoding="utf-8"
                )
            registry.update_fields(
                job_id,
                {
                    "status": "complete",
                    "output_dir": str(output_dir.relative_to(web_app.ROOT)),
                },
                final_log="fixture shop complete",
            )

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

        def complete_amazon(job_id: str) -> None:
            registry = web_app.amazon_job_registry
            current = registry.snapshot(job_id)
            assert current is not None
            registry.update_fields(job_id, {"status": "running"})
            if current.target == "B000FAIL01":
                amazon_failure_started.set()
                assert amazon_failure_release.wait(timeout=5)
                registry.update_fields(
                    job_id,
                    {"status": "failed", "error": "fixture amazon failure"},
                    final_log="fixture amazon failure",
                )
                return
            amazon_success_started.set()
            assert amazon_success_release.wait(timeout=5)
            result_path = web_app.OUTPUT_DIR / "amazon" / job_id / "result.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps({"products": [{"asin": current.target.upper(), "title": "fixture amazon"}]}),
                encoding="utf-8",
            )
            registry.update_fields(
                job_id,
                {
                    "status": "complete",
                    "output_dir": str(result_path.parent.relative_to(web_app.ROOT)),
                },
                final_log="fixture amazon complete",
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
            patches.enter_context(patch.object(web_app.shop_service, "run_job", side_effect=complete_shop))
            patches.enter_context(patch.object(web_app, "run_metrics_job", side_effect=complete_metrics))
            patches.enter_context(patch.object(web_app, "run_amazon_job", side_effect=complete_amazon))
            patches.enter_context(patch.object(web_app, "video_queue", fake_queue))
            patches.enter_context(patch.object(web_app, "subprocess", SimpleNamespace(run=fake_translate)))
            patches.enter_context(patch.object(web_app, "ensure_analyzer_media_or_delete", side_effect=lambda _path: None))
            patches.enter_context(patch.object(web_app, "register_video", side_effect=lambda **_kwargs: None))
            patches.enter_context(patch.object(web_app, "make_web_manual_visible", side_effect=lambda *_args: None))
            patches.enter_context(patch.object(web_app, "start_social_context_job", side_effect=lambda *_args, **_kwargs: None))
            patches.enter_context(patch.object(web_app, "analyzer_visible_source", side_effect=lambda _name: True))
            patches.enter_context(patch.object(web_app, "analyzer_media_is_valid", side_effect=lambda _path: True))

            assert_real_download_worker_registry_updates(web_app, real_download_worker)
            assert_real_shop_worker_registry_updates(web_app)
            assert_real_metrics_worker_registry_updates(web_app, real_metrics_worker)
            assert_real_amazon_worker_registry_updates(web_app, real_amazon_worker)

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

            invalid_shop_requests = (
                ({}, "A TikTok Shop URL is required"),
                ({"url": "x" * 2049}, "A TikTok Shop URL is required"),
                ({"url": "https://shop.tiktok.com/view/product/fixture", "source_type": "invalid"}, "source_type must be product, details, reviews, shop, or search"),
                ({"url": "https://shop.tiktok.com/view/product/fixture", "max_pages": -1}, "max_pages must be between 1 and 20"),
                ({"url": "https://shop.tiktok.com/view/product/fixture", "max_pages": 21}, "max_pages must be between 1 and 20"),
                ({"url": "https://shop.tiktok.com/view/product/fixture", "review_pages": -1}, "review_pages must be between 0 and 20"),
                ({"url": "https://shop.tiktok.com/view/product/fixture", "review_pages": 21}, "review_pages must be between 0 and 20"),
                ({"url": "https://shop.tiktok.com/view/product/fixture", "prompt": "p" * 6001}, "prompt is too long"),
            )
            for payload, error in invalid_shop_requests:
                status, _headers, invalid_shop = json_request(port, "POST", "/api/shop-extract", payload)
                assert status == 400 and invalid_shop == {"error": error}

            shop_prompt = "unique HTTP shop prompt"
            expected_shop_fields = {
                "source_type": "search",
                "region": "US",
                "max_pages": 20,
                "review_pages": 20,
                "analyze": False,
                "related_videos": True,
            }
            status, _headers, shop = json_request(
                port,
                "POST",
                "/api/shop-extract",
                {
                    "url": "https://shop.tiktok.com/view/product/fixture",
                    "source_type": "search",
                    "region": "us",
                    "max_pages": 20,
                    "review_pages": 20,
                    "analyze": False,
                    "related_videos": True,
                    "prompt": shop_prompt,
                },
            )
            assert status == 202 and shop["status"] in {"queued", "running"}
            assert {name: shop[name] for name in expected_shop_fields} == expected_shop_fields
            assert "prompt" not in shop and shop_prompt not in json.dumps(shop, ensure_ascii=False)
            shop_id = shop["id"]
            assert shop_success_started.wait(timeout=5)
            status, _headers, running_shop = json_request(port, "GET", f"/api/shop-job?id={shop_id}")
            assert status == 200 and running_shop["status"] == "running"
            assert {name: running_shop[name] for name in expected_shop_fields} == expected_shop_fields
            assert "prompt" not in running_shop and shop_prompt not in json.dumps(running_shop, ensure_ascii=False)
            shop_success_release.set()
            job = wait_for_job(port, f"/api/shop-job?id={shop_id}", "complete")
            assert job["extract"]["items"][0]["id"] == "fixture-shop"
            assert job["analysis"] is None
            assert {name: job[name] for name in expected_shop_fields} == expected_shop_fields
            assert "prompt" not in job and shop_prompt not in json.dumps(job, ensure_ascii=False)
            event = sse_payload(port, f"/api/shop-events?id={shop_id}")
            assert event["status"] == "complete" and event["analysis"] is None
            assert {name: event[name] for name in expected_shop_fields} == expected_shop_fields
            assert "prompt" not in event and shop_prompt not in json.dumps(event, ensure_ascii=False)

            status, _headers, failed_shop = json_request(
                port,
                "POST",
                "/api/shop-extract",
                {
                    "url": "https://shop.tiktok.com/view/product/fixture-failure",
                    "analyze": False,
                    "prompt": shop_prompt,
                },
            )
            assert status == 202 and failed_shop["status"] in {"queued", "running"}
            assert "prompt" not in failed_shop and shop_prompt not in json.dumps(failed_shop, ensure_ascii=False)
            failed_shop_id = failed_shop["id"]
            assert shop_failure_started.wait(timeout=5)
            status, _headers, running_failed_shop = json_request(port, "GET", f"/api/shop-job?id={failed_shop_id}")
            assert status == 200 and running_failed_shop["status"] == "running"
            assert "prompt" not in running_failed_shop and shop_prompt not in json.dumps(running_failed_shop, ensure_ascii=False)
            shop_failure_release.set()
            failed_job = wait_for_job(port, f"/api/shop-job?id={failed_shop_id}", "failed")
            assert failed_job["error"] == "fixture shop failure"
            assert failed_job["log"][-1] == "fixture shop failure"
            assert "prompt" not in failed_job and shop_prompt not in json.dumps(failed_job, ensure_ascii=False)
            failed_event = sse_payload(port, f"/api/shop-events?id={failed_shop_id}")
            assert failed_event["status"] == "failed" and failed_event["error"] == "fixture shop failure"
            assert "prompt" not in failed_event and shop_prompt not in json.dumps(failed_event, ensure_ascii=False)

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

            metrics_success_started.clear()
            metrics_success_release.clear()
            status, _headers, trending_metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "", "endpoint": "trending"},
            )
            assert status == 202 and trending_metrics["status"] in {"queued", "running"}
            assert metrics_success_started.wait(timeout=5)
            metrics_success_release.set()
            wait_for_job(port, f"/api/video-metrics-job?id={trending_metrics['id']}", "complete")

            metrics_success_started.clear()
            metrics_success_release.clear()
            status, _headers, music_popular_metrics = json_request(
                port,
                "POST",
                "/api/video-metrics",
                {"target": "", "endpoint": "music-popular"},
            )
            assert status == 202 and music_popular_metrics["status"] in {"queued", "running"}
            assert music_popular_metrics["target"] == ""
            assert music_popular_metrics["endpoint"] == "music-popular"
            assert metrics_success_started.wait(timeout=5)
            metrics_success_release.set()
            music_popular_job = wait_for_job(
                port,
                f"/api/video-metrics-job?id={music_popular_metrics['id']}",
                "complete",
            )
            assert music_popular_job["target"] == ""
            assert music_popular_job["endpoint"] == "music-popular"

            invalid_amazon_requests = (
                ({}, "Amazon URL, ASIN, or keyword is required"),
                ({"target": "ftp://www.amazon.com/dp/B000AMZ001", "target_type": "url"}, "Only http/https Amazon URLs are supported"),
                ({"target": "https://example.com/dp/B000AMZ001", "target_type": "url"}, "Only amazon.com URLs are supported"),
                ({"target": "https://www.amazon.com/" + "x" * 2049, "target_type": "url"}, "URL is too long"),
                ({"target": "B000SHORT", "target_type": "asin"}, "ASIN must be 10 letters or digits"),
                ({"target": "fixture", "target_type": "invalid"}, "target_type must be url, asin, or keyword"),
                ({"target": "x" * 201, "target_type": "keyword"}, "Keyword is too long"),
                ({"target": "B000AMZ001", "target_type": "asin", "pages": 6}, "pages must be between 1 and 5"),
            )
            for payload, error in invalid_amazon_requests:
                status, _headers, invalid_amazon = json_request(port, "POST", "/api/amazon-scrape", payload)
                assert status == 400 and invalid_amazon == {"error": error}

            def run_amazon_success(payload: dict[str, Any], expected_fields: dict[str, Any]) -> dict[str, Any]:
                amazon_success_started.clear()
                amazon_success_release.clear()
                status, _headers, amazon = json_request(port, "POST", "/api/amazon-scrape", payload)
                assert status == 202 and amazon["status"] in {"queued", "running"}
                assert {name: amazon[name] for name in expected_fields} == expected_fields
                amazon_id = amazon["id"]
                assert amazon_success_started.wait(timeout=5)
                status, _headers, running_amazon = json_request(port, "GET", f"/api/amazon-job?id={amazon_id}")
                assert status == 200 and running_amazon["status"] == "running"
                assert {name: running_amazon[name] for name in expected_fields} == expected_fields
                amazon_success_release.set()
                complete_amazon = wait_for_job(port, f"/api/amazon-job?id={amazon_id}", "complete")
                assert {name: complete_amazon[name] for name in expected_fields} == expected_fields
                event = sse_payload(port, f"/api/amazon-events?id={amazon_id}")
                assert event["status"] == "complete"
                assert {name: event[name] for name in expected_fields} == expected_fields
                return complete_amazon

            asin_fields = {
                "target": "b000amz001",
                "target_type": "asin",
                "url": "https://www.amazon.com/dp/B000AMZ001",
                "pages": 5,
            }
            amazon = run_amazon_success(
                {"target": "b000amz001", "target_type": "asin", "pages": 5}, asin_fields
            )
            expected_amazon_output_dir = str(
                (web_app.OUTPUT_DIR / "amazon" / amazon["id"]).relative_to(web_app.ROOT)
            )
            assert amazon["output_dir"] == expected_amazon_output_dir
            assert amazon["result"] == {"products": [{"asin": "B000AMZ001", "title": "fixture amazon"}]}
            with patch.dict(os.environ, {"AMAZON_MAX_PAGES": "3"}):
                run_amazon_success(
                    {"target": "B000ZERO01", "target_type": "asin", "pages": 0},
                    {
                        "target": "B000ZERO01",
                        "target_type": "asin",
                        "url": "https://www.amazon.com/dp/B000ZERO01",
                        "pages": 3,
                    },
                )
            run_amazon_success(
                {"target": "https://www.amazon.com/dp/B000URL001", "target_type": "url", "pages": 1},
                {
                    "target": "https://www.amazon.com/dp/B000URL001",
                    "target_type": "url",
                    "url": "https://www.amazon.com/dp/B000URL001",
                    "pages": 1,
                },
            )
            run_amazon_success(
                {"target": "fixture keyboard", "target_type": "keyword", "pages": 2},
                {
                    "target": "fixture keyboard",
                    "target_type": "keyword",
                    "url": "https://www.amazon.com/s?k=fixture+keyboard",
                    "pages": 2,
                },
            )

            amazon_failure_started.clear()
            amazon_failure_release.clear()
            status, _headers, failed_amazon = json_request(
                port,
                "POST",
                "/api/amazon-scrape",
                {"target": "B000FAIL01", "target_type": "asin", "pages": 4},
            )
            assert status == 202 and failed_amazon["status"] in {"queued", "running"}
            failed_amazon_id = failed_amazon["id"]
            assert amazon_failure_started.wait(timeout=5)
            status, _headers, running_failed_amazon = json_request(port, "GET", f"/api/amazon-job?id={failed_amazon_id}")
            assert status == 200 and running_failed_amazon["status"] == "running"
            amazon_failure_release.set()
            failed_amazon_job = wait_for_job(port, f"/api/amazon-job?id={failed_amazon_id}", "failed")
            assert failed_amazon_job["error"] == "fixture amazon failure"
            assert failed_amazon_job["log"][-1] == "fixture amazon failure"
            assert {name: failed_amazon_job[name] for name in {"target", "target_type", "url", "pages"}} == {
                "target": "B000FAIL01",
                "target_type": "asin",
                "url": "https://www.amazon.com/dp/B000FAIL01",
                "pages": 4,
            }
            failed_amazon_event = sse_payload(port, f"/api/amazon-events?id={failed_amazon_id}")
            assert failed_amazon_event["status"] == "failed" and failed_amazon_event["error"] == "fixture amazon failure"

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
