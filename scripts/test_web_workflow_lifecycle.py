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
from services.metrics import MetricsJob, MetricsService
from services.amazon import AmazonJob, AmazonService, parse_json_from_process_output
from services.analyze import AnalyzeService
from services.downloads import DownloadJob, DownloadService
from services.postprocess import PostprocessService
from services.translate import TranslateService
from services.upload import UploadService
from jobs.registry import JobRegistry
from routes.analyze import register_analyze_routes
from routes.postprocess import register_postprocess_routes
from routes.router import Router
from routes.translate import register_translate_routes
from routes.upload import MAX_UPLOAD_BYTES as UPLOAD_MAX_UPLOAD_BYTES, register_upload_routes


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def make_download_service(
    web_app: Any,
    registry: JobRegistry,
    *,
    read_json_file: Any | None = None,
    write_json_file: Any | None = None,
    run_factory: Any | None = None,
    thread_factory: Any | None = None,
    get_cached: Any | None = None,
    store_response: Any | None = None,
    register_video: Any | None = None,
    register_from_payload: Any | None = None,
    make_web_manual_visible: Any | None = None,
    start_social_context_job: Any | None = None,
    analyzer_media_is_valid: Any | None = None,
    ensure_analyzer_media_or_delete: Any | None = None,
    ensure_us_proxy: Any | None = None,
    requests_get: Any | None = None,
) -> DownloadService:
    generated_ids = iter(range(1, 10_000))
    return DownloadService(
        registry=registry,
        root=web_app.ROOT,
        videos_dir=web_app.VIDEOS_DIR,
        output_dir=web_app.OUTPUT_DIR,
        scripts_dir=web_app.SCRIPTS_DIR,
        read_json_file=read_json_file or web_app.read_json,
        write_json_file=write_json_file or web_app.atomic_write_json,
        run_factory=run_factory or (lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="")),
        thread_factory=thread_factory or threading.Thread,
        job_id_factory=lambda: f"download-lifecycle-fixture-{next(generated_ids)}",
        fallback_video_id_factory=lambda: "download-fallback-fixture",
        environ=os.environ,
        get_cached=get_cached or web_app.get_cached,
        store_response=store_response or web_app.store_response,
        video_cache_request=web_app.video_cache_request,
        video_cache_metadata=web_app.video_cache_metadata,
        with_download_cache_meta=web_app.with_download_cache_meta,
        register_video=register_video or web_app.register_video,
        register_from_payload=register_from_payload or web_app.register_from_payload,
        platform_for_url=web_app.platform_for_url,
        video_source_hidden=web_app.video_source_hidden,
        make_web_manual_visible=make_web_manual_visible or web_app.make_web_manual_visible,
        start_social_context_job=start_social_context_job or web_app.start_social_context_job,
        safe_filename=web_app.safe_filename,
        analyzer_media_is_valid=analyzer_media_is_valid or web_app.analyzer_media_is_valid,
        ensure_analyzer_media_or_delete=ensure_analyzer_media_or_delete or web_app.ensure_analyzer_media_or_delete,
        ensure_us_proxy=ensure_us_proxy or web_app.ensure_us_proxy,
        requests_get=requests_get or (lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected requests.get"))),
        cache_log_label=web_app.cache_log_label,
        normalize_video_source=web_app.normalize_video_source,
        default_source=web_app.SOURCE_API_UPLOAD,
        video_media_ttl_seconds=web_app.APP_CONFIG.video_media_ttl_seconds,
        default_sociavault_api_base=web_app.DEFAULT_SOCIA_VAULT_API_BASE,
    )


def request(
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    if body is None and payload is not None:
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    headers = {"Content-Type": content_type} if content_type else {}
    if extra_headers:
        headers.update(extra_headers)
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


def multipart_videos(
    files: list[tuple[str, bytes]], *, fields: dict[str, str] | None = None
) -> tuple[bytes, str]:
    boundary = "----v2-workflow-test-boundary"
    parts: list[bytes] = []
    for name, value in (fields or {}).items():
        parts.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ))
    for filename, content in files:
        parts.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="video"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: video/mp4\r\n\r\n",
            content,
            b"\r\n",
        ))
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def multipart_video(filename: str, content: bytes) -> tuple[bytes, str]:
    return multipart_videos([(filename, content)])


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


def assert_upload_http_contract(web_app: Any, port: int) -> None:
    calls: list[tuple[str, Any]] = []

    def ensure_media(path: Path) -> None:
        calls.append(("ensure", path.name))
        if path.name == "invalid.mp4":
            path.unlink()
            raise RuntimeError("invalid analyzer video: invalid.mp4")

    def register_upload(**kwargs: Any) -> dict[str, Any]:
        calls.append(("register", dict(kwargs)))
        return {}

    def make_visible(source: str, platform: str, video_id: str) -> None:
        calls.append(("visible", (source, platform, video_id)))

    def start_social(filename: str, *, generate_insights: bool) -> bool:
        calls.append(("social", (filename, generate_insights)))
        if filename == "social-error.mp4":
            raise RuntimeError("fixture social start failure")
        return True

    service = UploadService(
        videos_dir=web_app.VIDEOS_DIR,
        safe_filename=web_app.safe_filename,
        ensure_analyzer_media_or_delete=ensure_media,
        register_video=register_upload,
        video_source_hidden=web_app.video_source_hidden,
        make_web_manual_visible=make_visible,
        start_social_context_job=start_social,
    )
    router = Router()
    register_upload_routes(
        router,
        service,
        normalize_video_source=web_app.normalize_video_source,
        default_source=web_app.SOURCE_API_UPLOAD,
    )
    with patch.object(web_app, "WEB_ROUTER", router):
        status, _headers, empty_upload = json_request(
            port, "POST", "/api/upload", body=b"", content_type="multipart/form-data; boundary=fixture"
        )
        assert status == 400 and empty_upload == {"error": "Invalid upload size"}

        status, _headers, too_large = json_request(
            port,
            "POST",
            "/api/upload",
            body=b"",
            content_type="multipart/form-data; boundary=fixture",
            extra_headers={"Content-Length": str(UPLOAD_MAX_UPLOAD_BYTES + 1)},
        )
        assert status == 400 and too_large == {"error": "Invalid upload size"}
        assert calls == []

        missing_body, missing_type = multipart_videos([], fields={"source": "manual"})
        status, _headers, missing_video = json_request(
            port, "POST", "/api/upload", body=missing_body, content_type=missing_type
        )
        assert status == 400 and missing_video == {"error": "Missing video file"}

        empty_name_body, empty_name_type = multipart_videos([("", b"")])
        status, _headers, empty_name_video = json_request(
            port, "POST", "/api/upload", body=empty_name_body, content_type=empty_name_type
        )
        assert status == 400 and empty_name_video == {"error": "Missing video file"}
        assert calls == []

        for original_name, fields, source, filename, hidden in (
            ("source-only.mp4", {"source": "manual"}, web_app.SOURCE_WEB_MANUAL, "source-only.mp4", False),
            ("fallback!name.mp4", {"source": "manual", "source_tag": ""}, web_app.SOURCE_WEB_MANUAL, "fallbackname.mp4", False),
        ):
            calls.clear()
            source_body, source_type = multipart_videos([(original_name, b"source")], fields=fields)
            status, _headers, source_upload = json_request(
                port, "POST", "/api/upload", body=source_body, content_type=source_type
            )
            assert status == 200 and source_upload == {"files": [{"filename": filename, "size": 6}], "errors": []}
            assert (web_app.VIDEOS_DIR / filename).read_bytes() == b"source"
            assert calls == [
                ("ensure", filename),
                ("register", {
                    "video_id": filename, "platform": "local", "filename": filename, "title": filename,
                    "source": source, "hidden_from_analyzer": hidden,
                }),
                ("visible", (source, "local", filename)),
                ("social", (filename, False)),
            ]

        calls.clear()
        partial_body, partial_type = multipart_videos(
            [("manual.mp4", b"manual"), ("invalid.mp4", b"invalid"), ("???", b"unsafe")],
            fields={"source": "api", "source_tag": "manual"},
        )
        status, _headers, partial = json_request(
            port, "POST", "/api/upload", body=partial_body, content_type=partial_type
        )
        assert status == 200 and partial == {
            "files": [{"filename": "manual.mp4", "size": 6}],
            "errors": [
                {"filename": "invalid.mp4", "error": "invalid analyzer video: invalid.mp4"},
                {"filename": "???", "error": "Invalid filename"},
            ],
        }
        assert (web_app.VIDEOS_DIR / "manual.mp4").read_bytes() == b"manual"
        assert not (web_app.VIDEOS_DIR / "invalid.mp4").exists()
        assert calls == [
            ("ensure", "manual.mp4"),
            ("register", {
                "video_id": "manual.mp4", "platform": "local", "filename": "manual.mp4",
                "title": "manual.mp4", "source": web_app.SOURCE_WEB_MANUAL,
                "hidden_from_analyzer": False,
            }),
            ("visible", (web_app.SOURCE_WEB_MANUAL, "local", "manual.mp4")),
            ("social", ("manual.mp4", False)),
            ("ensure", "invalid.mp4"),
        ]

        calls.clear()
        failed_body, failed_type = multipart_videos(
            [("invalid.mp4", b"invalid"), ("???", b"unsafe")]
        )
        status, _headers, all_failed = json_request(
            port, "POST", "/api/upload", body=failed_body, content_type=failed_type
        )
        assert status == 400 and all_failed == {
            "files": [],
            "errors": [
                {"filename": "invalid.mp4", "error": "invalid analyzer video: invalid.mp4"},
                {"filename": "???", "error": "Invalid filename"},
            ],
        }
        assert calls == [("ensure", "invalid.mp4")]

        calls.clear()
        social_body, social_type = multipart_video("social-error.mp4", b"social")
        status, _headers, social_failure = json_request(
            port, "POST", "/api/upload", body=social_body, content_type=social_type
        )
        assert status == 200 and social_failure == {
            "files": [{"filename": "social-error.mp4", "size": 6}],
            "errors": [{"filename": "social-error.mp4", "error": "fixture social start failure"}],
        }
        assert calls == [
            ("ensure", "social-error.mp4"),
            ("register", {
                "video_id": "social-error.mp4", "platform": "local", "filename": "social-error.mp4",
                "title": "social-error.mp4", "source": web_app.SOURCE_API_UPLOAD,
                "hidden_from_analyzer": True,
            }),
            ("visible", (web_app.SOURCE_API_UPLOAD, "local", "social-error.mp4")),
            ("social", ("social-error.mp4", False)),
        ]

        calls.clear()
        blocked_body, blocked_type = multipart_video("blocked.mp4", b"blocked")
        blocked_router = Router()

        def blocked_field_storage(**_kwargs: Any) -> Any:
            raise AssertionError("UI_TEST gate must run before multipart parsing")

        register_upload_routes(
            blocked_router,
            service,
            field_storage_factory=blocked_field_storage,
            normalize_video_source=web_app.normalize_video_source,
            default_source=web_app.SOURCE_API_UPLOAD,
        )
        with patch.object(web_app, "WEB_ROUTER", blocked_router), patch.object(
            web_app, "ui_test_mode_allows_live_write", return_value=False
        ), patch.object(
            service, "upload", side_effect=AssertionError("UI_TEST gate must run before upload service")
        ):
            status, _headers, blocked = json_request(
                port, "POST", "/api/upload", body=blocked_body, content_type=blocked_type
            )
        assert status == 409 and blocked == {
            "error": "UI 测试模式已拦截写操作，未触发真实业务。",
            "simulated": True,
            "status": "blocked",
            "path": "/api/upload",
        }
        assert calls == []


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


class RecordingAnalyzeQueue:
    """Analyze-only queue fake that records production calls without artifacts."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on

    def enqueue(self, filename: str, job_type: str) -> None:
        self.calls.append((filename, job_type))
        if job_type == self.fail_on:
            raise RuntimeError(f"fixture {job_type} enqueue failure")


class RecordingPostprocessQueue:
    """Postprocess queue fake that records queue-time artifact state only."""

    def __init__(self, output_dir: Path, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.snapshots: list[tuple[str, str, dict[str, str]]] = []
        self.output_dir = output_dir
        self.fail_on = fail_on

    def enqueue(self, filename: str, job_type: str) -> None:
        self.calls.append((filename, job_type))
        self.snapshots.append((
            filename,
            job_type,
            {
                path.name: path.read_text(encoding="utf-8")
                for path in self.output_dir.iterdir()
                if path.is_file()
            },
        ))
        if job_type == self.fail_on:
            raise RuntimeError(f"fixture {job_type} enqueue failure")


def assert_analyze_http_contract(web_app: Any, port: int) -> None:
    """Freeze the legacy Handler Analyze request and side-effect contract."""

    filename = "analyze-contract.mp4"
    cleaned_filename = "analyzecontract.mp4"
    video_path = web_app.VIDEOS_DIR / filename
    output_dir = web_app.OUTPUT_DIR / "registry-style-analyze-output"
    web_app.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fixture analyze video")
    (web_app.VIDEOS_DIR / cleaned_filename).write_bytes(b"fixture cleaned analyze video")

    output_requests: list[str] = []

    def registry_style_output_dir(requested_filename: str) -> Path:
        output_requests.append(requested_filename)
        assert requested_filename in {filename, cleaned_filename}
        return output_dir

    def remove_output_dir() -> None:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_requests.clear()

    queue = RecordingAnalyzeQueue()
    service = AnalyzeService(
        videos_dir=web_app.VIDEOS_DIR,
        output_dir_for_filename=registry_style_output_dir,
        safe_filename=web_app.safe_filename,
        queue_enqueue=queue.enqueue,
    )
    router = Router()
    register_analyze_routes(router, service)
    with patch.object(web_app, "WEB_ROUTER", router):
        for payload, error in (
            ({}, "Missing filename"),
            ({"filename": ""}, "Missing filename"),
            ({"filename": filename, "analysis_mode": "unsupported"}, "analysis_mode must be analyzer or direct_video"),
            ({"filename": filename, "analysis_prompt": "x" * 12001}, "analysis_prompt is too long"),
            ({"filename": "missing-analyze-contract.mp4"}, "Video file not found: missing-analyze-contract.mp4"),
        ):
            status, _headers, response = json_request(port, "POST", "/api/analyze", payload)
            assert status == 400 and response == {"error": error}
            assert queue.calls == [] and output_requests == [] and not output_dir.exists()

        status, _headers, malformed = json_request(
            port, "POST", "/api/analyze", body=b"{", content_type="application/json"
        )
        assert status == 400 and malformed.get("error")
        assert queue.calls == [] and output_requests == [] and not output_dir.exists()

        with patch.dict(os.environ, {"ANALYSIS_MODE": "direct_video"}):
            status, _headers, queued_default = json_request(port, "POST", "/api/analyze", {"filename": filename})
        assert status == 202 and queued_default == {
            "status": "queued", "filename": filename, "queued": ["analyze"]
        }
        assert output_requests == [filename] and queue.calls == [(filename, "analyze")]
        assert (output_dir / "analysis_mode.txt").read_text(encoding="utf-8") == "direct_video"

        queue.calls.clear()
        output_requests.clear()
        status, _headers, queued_explicit = json_request(
            port,
            "POST",
            "/api/analyze",
            {"filename": filename, "analysis_mode": "analyzer", "analysis_prompt": "  trimmed prompt  ", "postprocess": "false"},
        )
        assert status == 202 and queued_explicit["queued"] == ["analyze", "report"]
        assert output_requests == [filename]
        assert queue.calls == [(filename, "analyze"), (filename, "report")]
        assert (output_dir / "analysis_mode.txt").read_text(encoding="utf-8") == "analyzer"
        prompt_path = output_dir / "analysis_prompt.txt"
        assert prompt_path.read_text(encoding="utf-8") == "trimmed prompt"

        queue.calls.clear()
        output_requests.clear()
        prompt_path.write_text("existing prompt", encoding="utf-8")
        status, _headers, empty_prompt = json_request(
            port, "POST", "/api/analyze", {"filename": filename, "analysis_prompt": "   "}
        )
        assert status == 202 and empty_prompt["queued"] == ["analyze"]
        assert output_requests == [filename] and queue.calls == [(filename, "analyze")]
        assert prompt_path.read_text(encoding="utf-8") == "existing prompt"

        remove_output_dir()
        queue.calls.clear()
        status, _headers, cleaned = json_request(port, "POST", "/api/analyze", {"filename": "analyze!contract.mp4"})
        assert status == 202 and cleaned == {
            "status": "queued", "filename": cleaned_filename, "queued": ["analyze"]
        }
        assert output_requests == [cleaned_filename] and queue.calls == [(cleaned_filename, "analyze")]
        assert (output_dir / "analysis_mode.txt").read_text(encoding="utf-8") == "analyzer"

        remove_output_dir()
        output_dir.mkdir(parents=True)
        (output_dir / "obsolete.json").write_text("obsolete", encoding="utf-8")
        (output_dir / "nested").mkdir()
        (output_dir / "nested" / "obsolete.txt").write_text("obsolete", encoding="utf-8")
        queue.calls.clear()
        status, _headers, reset_analyzer = json_request(
            port, "POST", "/api/analyze", {"filename": filename, "analysis_mode": "analyzer", "reset_output": True}
        )
        assert status == 202 and reset_analyzer["queued"] == ["analyze"]
        assert queue.calls == [(filename, "analyze")]
        assert not (output_dir / "obsolete.json").exists() and not (output_dir / "nested").exists()
        assert (output_dir / "analysis_mode.txt").read_text(encoding="utf-8") == "analyzer"

        (output_dir / "direct_analysis.json").write_text("direct", encoding="utf-8")
        (output_dir / "direct_analysis_zh.json").write_text("direct zh", encoding="utf-8")
        (output_dir / "analysis.json").write_text("keep analysis", encoding="utf-8")
        (output_dir / "keep.txt").write_text("keep", encoding="utf-8")
        queue.calls.clear()
        status, _headers, reset_direct = json_request(
            port, "POST", "/api/analyze", {"filename": filename, "analysis_mode": "direct_video", "reset_output": True}
        )
        assert status == 202 and reset_direct["queued"] == ["analyze"]
        assert queue.calls == [(filename, "analyze")]
        assert not (output_dir / "direct_analysis.json").exists()
        assert not (output_dir / "direct_analysis_zh.json").exists()
        assert (output_dir / "analysis.json").read_text(encoding="utf-8") == "keep analysis"
        assert (output_dir / "keep.txt").read_text(encoding="utf-8") == "keep"

    remove_output_dir()
    failing_queue = RecordingAnalyzeQueue(fail_on="report")
    failing_service = AnalyzeService(
        videos_dir=web_app.VIDEOS_DIR,
        output_dir_for_filename=registry_style_output_dir,
        safe_filename=web_app.safe_filename,
        queue_enqueue=failing_queue.enqueue,
    )
    failing_router = Router()
    register_analyze_routes(failing_router, failing_service)
    with patch.object(web_app, "WEB_ROUTER", failing_router):
        try:
            json_request(port, "POST", "/api/analyze", {"filename": filename, "postprocess": True})
        except http.client.RemoteDisconnected:
            pass
        else:
            raise AssertionError("second enqueue failure must close the legacy Handler response")
    assert output_requests == [filename]
    assert failing_queue.calls == [(filename, "analyze"), (filename, "report")]
    assert (output_dir / "analysis_mode.txt").read_text(encoding="utf-8") == "analyzer"

    remove_output_dir()
    blocked_router = Router()
    blocked_router.post(
        "/api/analyze",
        lambda _handler, _params: (_ for _ in ()).throw(
            AssertionError("UI_TEST gate must run before the Analyze route and body parsing")
        ),
    )
    with patch.object(web_app, "ui_test_mode_allows_live_write", return_value=False), patch.object(
        web_app, "WEB_ROUTER", blocked_router
    ):
        status, _headers, blocked = json_request(
            port, "POST", "/api/analyze", body=b"{", content_type="application/json"
        )
    assert status == 409 and blocked == {
        "error": "UI 测试模式已拦截写操作，未触发真实业务。",
        "simulated": True,
        "status": "blocked",
        "path": "/api/analyze",
    }
    assert not output_dir.exists()


def assert_postprocess_http_contract(web_app: Any, port: int) -> None:
    """Freeze the legacy Postprocess JSON, artifact, and queue-time contract."""

    filename = "postprocess-contract.mp4"
    output_dir = web_app.OUTPUT_DIR / "registry-style-postprocess-output"
    output_requests: list[str] = []

    def registry_style_output_dir(requested_filename: str) -> Path:
        output_requests.append(requested_filename)
        assert requested_filename == filename
        return output_dir

    def reset_output() -> None:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_requests.clear()

    def write_artifacts(*names: str) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            (output_dir / name).write_text(f"fixture {name}", encoding="utf-8")

    def router_for(queue: RecordingPostprocessQueue) -> Router:
        service = PostprocessService(
            output_dir_for_filename=registry_style_output_dir,
            safe_filename=web_app.safe_filename,
            queue_enqueue=queue.enqueue,
        )
        router = Router()
        register_postprocess_routes(router, service)
        return router

    reset_output()
    queue = RecordingPostprocessQueue(output_dir)
    with patch.object(web_app, "WEB_ROUTER", router_for(queue)):
        for payload, error in (
            ({}, "Missing filename"),
            ({"filename": filename, "analysis_source": "invalid"}, "analysis_source must be standard or direct"),
            ({"filename": filename, "analysis_source": " ", "source": "direct"}, "analysis_source must be standard or direct"),
        ):
            status, _headers, invalid = json_request(port, "POST", "/api/postprocess", payload)
            assert status == 400 and invalid == {"error": error}
            assert queue.calls == [] and output_requests == [] and not output_dir.exists()

        status, _headers, empty_body = json_request(
            port,
            "POST",
            "/api/postprocess",
            body=b"",
            content_type="application/json",
            extra_headers={"Content-Length": "0"},
        )
        assert status == 400 and empty_body == {"error": "Missing filename"}
        assert queue.calls == [] and output_requests == [] and not output_dir.exists()

        status, _headers, malformed = json_request(
            port, "POST", "/api/postprocess", body=b"{", content_type="application/json"
        )
        assert status == 400 and malformed.get("error")
        assert queue.calls == [] and output_requests == [] and not output_dir.exists()

        status, _headers, standard_missing = json_request(
            port,
            "POST",
            "/api/postprocess",
            {"filename": filename, "analysis_source": "standard", "source": "direct"},
        )
        assert status == 400 and standard_missing == {"error": f"analysis.json not found for {filename}"}
        assert queue.calls == [] and output_requests == [filename] and not output_dir.exists()

        reset_output()
        queue.calls.clear()
        queue.snapshots.clear()
        write_artifacts(
            "analysis.json", "audit_result.json", "audit_result_zh.json",
            "direct_analysis.json", "direct_audit_result.json", "direct_audit_result_zh.json",
        )
        status, _headers, standard = json_request(
            port,
            "POST",
            "/api/postprocess",
            {
                "filename": f"nested/{filename}",
                "analysis_source": "standard",
                "source": "direct",
                "analysis_prompt": "  standard prompt  ",
            },
        )
        assert status == 202 and standard == {"status": "queued", "filename": filename}
        assert queue.calls == [(filename, "report")]
        assert output_requests == [filename]
        queued_standard = queue.snapshots[-1][2]
        assert queued_standard["report_source.txt"] == "standard"
        assert queued_standard["analysis_prompt.txt"] == "standard prompt"
        assert "audit_result.json" not in queued_standard and "audit_result_zh.json" not in queued_standard
        assert (output_dir / "direct_audit_result.json").is_file()

        write_artifacts("audit_result.json", "audit_result_zh.json")
        (output_dir / "analysis_prompt.txt").write_text("existing prompt", encoding="utf-8")
        queue.calls.clear()
        queue.snapshots.clear()
        status, _headers, empty_prompt = json_request(
            port, "POST", "/api/postprocess", {"filename": filename, "analysis_prompt": "   "}
        )
        assert status == 202 and empty_prompt == {"status": "queued", "filename": filename}
        assert queue.calls == [(filename, "report")]
        assert (output_dir / "analysis_prompt.txt").read_text(encoding="utf-8") == "existing prompt"
        assert "audit_result.json" not in queue.snapshots[-1][2]

        write_artifacts("direct_audit_result.json", "direct_audit_result_zh.json", "audit_result.json")
        queue.calls.clear()
        queue.snapshots.clear()
        status, _headers, direct = json_request(
            port,
            "POST",
            "/api/postprocess",
            {"filename": filename, "analysis_source": " direct ", "analysis_prompt": "  direct prompt  "},
        )
        assert status == 202 and direct == {"status": "queued", "filename": filename}
        assert queue.calls == [(filename, "report")]
        queued_direct = queue.snapshots[-1][2]
        assert queued_direct["report_source.txt"] == "direct"
        assert queued_direct["analysis_prompt.txt"] == "direct prompt"
        assert "direct_audit_result.json" not in queued_direct and "direct_audit_result_zh.json" not in queued_direct
        assert (output_dir / "audit_result.json").is_file()

        reset_output()
        queue.calls.clear()
        queue.snapshots.clear()
        status, _headers, direct_missing = json_request(
            port,
            "POST",
            "/api/postprocess",
            {"filename": filename, "source": "direct", "analysis_prompt": "must not persist"},
        )
        assert status == 202 and direct_missing == {
            "status": "queued", "filename": filename, "queued": ["analyze", "report"],
        }
        assert queue.calls == [(filename, "analyze"), (filename, "report")]
        for _queued_filename, _job_type, snapshot in queue.snapshots:
            assert snapshot == {"analysis_mode.txt": "direct_video", "report_source.txt": "direct"}
        assert not (output_dir / "analysis_prompt.txt").exists()

        for failed_job, expected_calls in (
            ("analyze", [(filename, "analyze")]),
            ("report", [(filename, "analyze"), (filename, "report")]),
        ):
            reset_output()
            failing_queue = RecordingPostprocessQueue(output_dir, fail_on=failed_job)
            with patch.object(web_app, "WEB_ROUTER", router_for(failing_queue)):
                try:
                    json_request(port, "POST", "/api/postprocess", {"filename": filename, "source": "direct"})
                except http.client.RemoteDisconnected:
                    pass
                else:
                    raise AssertionError(f"{failed_job} enqueue failure must keep the legacy connection failure")
            assert failing_queue.calls == expected_calls
            assert (output_dir / "analysis_mode.txt").read_text(encoding="utf-8") == "direct_video"
            assert (output_dir / "report_source.txt").read_text(encoding="utf-8") == "direct"

        reset_output()
        write_artifacts("analysis.json", "audit_result.json", "audit_result_zh.json")
        failing_queue = RecordingPostprocessQueue(output_dir, fail_on="report")
        with patch.object(web_app, "WEB_ROUTER", router_for(failing_queue)):
            try:
                json_request(
                    port,
                    "POST",
                    "/api/postprocess",
                    {"filename": filename, "analysis_prompt": "  failure prompt  "},
                )
            except http.client.RemoteDisconnected:
                pass
            else:
                raise AssertionError("report enqueue failure must keep the legacy connection failure")
        assert failing_queue.calls == [(filename, "report")]
        failed_snapshot = failing_queue.snapshots[-1][2]
        assert failed_snapshot["report_source.txt"] == "standard"
        assert failed_snapshot["analysis_prompt.txt"] == "failure prompt"
        assert "audit_result.json" not in failed_snapshot
        assert "audit_result_zh.json" not in failed_snapshot

        reset_output()
        blocked_router = Router()
        blocked_router.post(
            "/api/postprocess",
            lambda _handler, _params: (_ for _ in ()).throw(
                AssertionError("UI_TEST gate must run before Postprocess route and body parsing")
            ),
        )
        with patch.object(web_app, "ui_test_mode_allows_live_write", return_value=False), patch.object(
            web_app, "WEB_ROUTER", blocked_router
        ):
            status, _headers, blocked = json_request(
                port, "POST", "/api/postprocess", body=b"{", content_type="application/json"
            )
        assert status == 409 and blocked == {
            "error": "UI 测试模式已拦截写操作，未触发真实业务。",
            "simulated": True,
            "status": "blocked",
            "path": "/api/postprocess",
        }
        assert not output_dir.exists()


def assert_translate_http_contract(web_app: Any, port: int) -> None:
    """Freeze the Translate route input, file, and process contract."""

    filename = "translate-contract.mp4"
    requested_filename = f"nested/{filename}"
    output_dir = web_app.OUTPUT_DIR / filename
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    run_calls: list[tuple[list[str], dict[str, Any]]] = []

    def recording_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        run_calls.append((list(command), dict(kwargs)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def request_payload(**values: Any) -> dict[str, Any]:
        return {"filename": requested_filename, **values}

    def route_for(run_factory: Any) -> Router:
        service = TranslateService(
            root=web_app.ROOT,
            scripts_dir=web_app.SCRIPTS_DIR,
            output_dir_for_filename=lambda requested: web_app.OUTPUT_DIR / requested,
            safe_filename=web_app.safe_filename,
            run_factory=run_factory,
            environ=os.environ,
        )
        router = Router()
        register_translate_routes(router, service)
        return router

    with patch.object(web_app, "WEB_ROUTER", route_for(recording_run)):
        for payload, error in (
            ({"filename": "", "tab": "content"}, "Missing filename"),
            (request_payload(tab="unknown"), "tab must be content, direct, audit, or feedback"),
            (request_payload(tab="content", analysis_source="other"), "analysis_source must be standard or direct"),
        ):
            status, _headers, response = json_request(port, "POST", "/api/translate", payload)
            assert status == 400 and response == {"error": error}
            assert run_calls == [] and list(output_dir.iterdir()) == []
        status, _headers, malformed = json_request(
            port, "POST", "/api/translate", body=b"{", content_type="application/json"
        )
        assert status == 400 and malformed.get("error") and run_calls == [] and list(output_dir.iterdir()) == []

        def assert_run(source_name: str, output_name: str) -> None:
            command, kwargs = run_calls[-1]
            assert command == [
                "python", str(web_app.SCRIPTS_DIR / "translate_analysis.py"),
                str(output_dir / source_name), "--output", str(output_dir / output_name),
            ]
            assert kwargs["cwd"] == web_app.ROOT
            assert kwargs["check"] is True and kwargs["capture_output"] is True and kwargs["text"] is True
            assert kwargs["env"] == dict(os.environ) and kwargs["env"] is not os.environ

        for values, source_name, output_name in (
            ({"tab": "content"}, "analysis.json", "analysis_zh.json"),
            ({"tab": "direct"}, "direct_analysis.json", "direct_analysis_zh.json"),
            ({"tab": "audit"}, "audit_result.json", "audit_result_zh.json"),
            ({"tab": "feedback"}, "feedback_result.json", "feedback_result_zh.json"),
            ({"tab": "audit", "analysis_source": " direct ", "source": "standard"}, "direct_audit_result.json", "direct_audit_result_zh.json"),
            ({"tab": "feedback", "source": "direct"}, "direct_feedback_result.json", "direct_feedback_result_zh.json"),
        ):
            source_path = output_dir / source_name
            output_path = output_dir / output_name
            source_path.write_text("fixture source", encoding="utf-8")
            output_path.unlink(missing_ok=True)
            status, _headers, translated = json_request(port, "POST", "/api/translate", request_payload(**values))
            assert status == 200 and translated == {"status": "translated", "filename": filename, "tab": values["tab"]}
            assert_run(source_name, output_name)
            assert not output_path.exists()

        missing_source = output_dir / "analysis.json"
        missing_source.unlink(missing_ok=True)
        before_missing = len(run_calls)
        status, _headers, missing = json_request(port, "POST", "/api/translate", request_payload(tab="content"))
        assert status == 400 and missing == {"error": f"analysis.json not found for {filename}"}
        assert len(run_calls) == before_missing

    source_path = output_dir / "analysis.json"
    source_path.write_text("fixture source", encoding="utf-8")

    class BlankCalledProcessError(subprocess.CalledProcessError):
        def __str__(self) -> str:
            return " "

    for failure, expected_error in (
        (subprocess.CalledProcessError(7, ["fixture"], output="stdout ignored", stderr=" stderr selected \n"), "stderr selected"),
        (subprocess.CalledProcessError(7, ["fixture"], output=" stdout selected \n", stderr=""), "stdout selected"),
        (subprocess.CalledProcessError(7, ["fixture"], output="", stderr=""), "Command '['fixture']' returned non-zero exit status 7."),
        (BlankCalledProcessError(7, ["fixture"], output="", stderr=""), "Translation failed"),
    ):
        def failing_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
            run_calls.append((list(command), dict(kwargs)))
            raise failure

        with patch.object(web_app, "WEB_ROUTER", route_for(failing_run)):
            status, _headers, failed = json_request(port, "POST", "/api/translate", request_payload(tab="content"))
        assert status == 500 and failed == {"error": expected_error}

    output_path = output_dir / "analysis_zh.json"
    output_path.unlink(missing_ok=True)

    def unavailable_run(command: list[str], **kwargs: Any) -> None:
        run_calls.append((list(command), dict(kwargs)))
        raise OSError("fixture translate runner unavailable")

    before_oserror = len(run_calls)
    with patch.object(web_app, "WEB_ROUTER", route_for(unavailable_run)):
        try:
            json_request(port, "POST", "/api/translate", request_payload(tab="content"))
        except http.client.RemoteDisconnected:
            pass
        else:
            raise AssertionError("non-CalledProcessError must keep the legacy connection failure")
    assert len(run_calls) == before_oserror + 1
    assert source_path.read_text(encoding="utf-8") == "fixture source" and not output_path.exists()

    blocked_router = Router()
    blocked_router.post(
        "/api/translate",
        lambda _handler, _params: (_ for _ in ()).throw(
            AssertionError("UI_TEST gate must run before Translate route and body parsing")
        ),
    )
    with patch.object(web_app, "ui_test_mode_allows_live_write", return_value=False), patch.object(
        web_app, "WEB_ROUTER", blocked_router
    ):
        status, _headers, blocked = json_request(
            port, "POST", "/api/translate", body=b"{", content_type="application/json"
        )
    assert status == 409 and blocked == {
        "error": "UI 测试模式已拦截写操作，未触发真实业务。",
        "simulated": True,
        "status": "blocked",
        "path": "/api/translate",
    }


def assert_real_download_worker_registry_updates(web_app: Any) -> None:
    registry = JobRegistry()
    service = make_download_service(web_app, registry)
    success_id = "worker-success"
    success_filename = "worker-success.mp4"
    registry.register(success_id, DownloadJob(id=success_id, url="https://www.tiktok.com/@fixture/video/worker"))
    web_app.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    (web_app.VIDEOS_DIR / success_filename).write_bytes(b"fixture")

    def cached_success(_job_id: str, _url: str, _source: str, result_path: Path) -> bool:
        result_path.write_text(json.dumps({"filename": success_filename, "meta": {"source": "worker"}}), encoding="utf-8")
        return True

    with patch.object(service, "try_cached_download_result", side_effect=cached_success):
        service.run_job(success_id)
    success = registry.snapshot(success_id)
    assert success is not None and success.status == "complete"
    assert success.filename == success_filename
    assert success.result == {"filename": success_filename, "meta": {"source": "worker"}}
    assert success.result is not None
    success.result["meta"]["source"] = "mutated"
    assert registry.snapshot(success_id).result == {"filename": success_filename, "meta": {"source": "worker"}}

    failure_id = "worker-failure"
    registry.register(failure_id, DownloadJob(id=failure_id, url="https://www.tiktok.com/@fixture/video/failure"))

    def cached_failure(job_id: str, _url: str, _source: str, _result_path: Path) -> bool:
        service.append_log(job_id, "fixture useful failure")
        raise RuntimeError("fixture raw failure")

    with patch.object(service, "try_cached_download_result", side_effect=cached_failure):
        service.run_job(failure_id)
    failure = registry.snapshot(failure_id)
    assert failure is not None and failure.status == "failed"
    assert failure.error == "fixture useful failure" and failure.log[-1] == "fixture raw failure"

    for job_id, result, expected_error in (
        ("worker-missing-filename", {}, "Downloader did not return a video filename"),
        ("worker-missing-file", {"filename": "worker-absent.mp4"}, "Downloaded file not found: worker-absent.mp4"),
    ):
        registry.register(job_id, DownloadJob(id=job_id, url=f"https://www.tiktok.com/@fixture/video/{job_id}"))

        def cached_incomplete(_job_id: str, _url: str, _source: str, result_path: Path, *, payload=result) -> bool:
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            return True

        with patch.object(service, "try_cached_download_result", side_effect=cached_incomplete):
            service.run_job(job_id)
        failed = registry.snapshot(job_id)
        assert failed is not None and failed.status == "failed"
        assert failed.error == expected_error and failed.log[-1] == expected_error


def assert_download_command_cache_and_fallback_contract(web_app: Any) -> None:
    registry = JobRegistry()
    service = make_download_service(web_app, registry)
    try:
        command_id = "download-command-success"
        registry.register(command_id, DownloadJob(id=command_id, url="https://www.tiktok.com/@fixture/video/command"))
        command = ["python", "fixture-download.py", "--url", "fixture"]
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def successful_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="first stdout\nsecond stdout\n")

        with patch.dict(os.environ, {"DOWNLOAD_COMMAND_TIMEOUT": "37"}), patch.object(
            service, "_run_factory", side_effect=successful_run
        ):
            service.run_command(command_id, command)
        assert calls == [(
            command,
            {
                "cwd": web_app.ROOT,
                "stdout": web_app.subprocess.PIPE,
                "stderr": web_app.subprocess.STDOUT,
                "text": True,
                "timeout": 37,
            },
        )]
        command_job = registry.snapshot(command_id)
        assert command_job is not None
        assert command_job.log == ["$ python fixture-download.py --url fixture", "first stdout", "second stdout"]

        failed_command_id = "download-command-failure"
        registry.register(failed_command_id, DownloadJob(id=failed_command_id, url="https://www.tiktok.com/@fixture/video/failure"))
        with patch.object(service, "_run_factory", return_value=SimpleNamespace(returncode=7, stdout="failure stdout\n")):
            try:
                service.run_command(failed_command_id, command)
            except RuntimeError as exc:
                assert str(exc) == "Command failed with exit code 7: python fixture-download.py --url fixture"
            else:
                raise AssertionError("non-zero download command must fail")
        failed_command = registry.snapshot(failed_command_id)
        assert failed_command is not None
        assert failed_command.log == ["$ python fixture-download.py --url fixture", "failure stdout"]

        timeout_command_id = "download-command-timeout"
        registry.register(timeout_command_id, DownloadJob(id=timeout_command_id, url="https://www.tiktok.com/@fixture/video/timeout"))
        timeout_error = subprocess.TimeoutExpired(command, 37, output="timeout first\ntimeout second\n")
        with patch.dict(os.environ, {"DOWNLOAD_COMMAND_TIMEOUT": "37"}), patch.object(
            service, "_run_factory", side_effect=timeout_error
        ):
            try:
                service.run_command(timeout_command_id, command)
            except RuntimeError as exc:
                assert str(exc) == "Command timed out after 37s: python fixture-download.py --url fixture"
            else:
                raise AssertionError("timed-out download command must fail")
        timed_out_command = registry.snapshot(timeout_command_id)
        assert timed_out_command is not None
        assert timed_out_command.log == [
            "$ python fixture-download.py --url fixture",
            "timeout first",
            "timeout second",
        ]

        url = "https://www.tiktok.com/@fixture/video/cache"
        cached_filename = "download-cache.mp4"
        cached_path = web_app.VIDEOS_DIR / cached_filename
        web_app.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(b"fixture")
        cache_id = "download-cache-hit"
        registry.register(cache_id, DownloadJob(id=cache_id, url=url, source=web_app.SOURCE_WEB_MANUAL))
        registered: list[dict[str, Any]] = []
        visible: list[tuple[str, str, str]] = []
        cache_payload = {"filename": cached_filename, "id": "cache-video", "title": "fixture"}
        result_path = web_app.OUTPUT_DIR / "download_jobs" / f"{cache_id}.json"
        with patch.object(service, "_get_cached", return_value=cache_payload) as get_cached, patch.object(
            service, "_analyzer_media_is_valid", return_value=True
        ), patch.object(service, "_register_video", side_effect=lambda **kwargs: registered.append(kwargs)), patch.object(
            service, "_make_web_manual_visible", side_effect=lambda *args: visible.append(args)
        ):
            assert service.try_cached_download_result(cache_id, url, web_app.SOURCE_WEB_MANUAL, result_path) is True
        get_cached.assert_called_once_with("short_video_download", "download", web_app.video_cache_request(url))
        cached_result = web_app.read_json(result_path)
        assert cached_result["filename"] == cached_filename
        assert cached_result["_cache"] == {
            "hit": True,
            "provider": "short_video_download",
            "endpoint": "download",
            "label": "缓存命中",
        }
        assert registered and registered[0]["source"] == web_app.SOURCE_WEB_MANUAL
        assert visible == [(web_app.SOURCE_WEB_MANUAL, web_app.platform_for_url(url), "cache-video")]

        for filename, valid_media, expected_log in (
            ("cached-audio.mp3", True, "删除缓存命中的无效音频文件，重新下载：cached-audio.mp3"),
            ("cached-invalid.mp4", False, "删除缓存命中的无效视频文件，重新下载：cached-invalid.mp4"),
        ):
            path = web_app.VIDEOS_DIR / filename
            path.write_bytes(b"invalid")
            job_id = f"download-{filename}"
            registry.register(job_id, DownloadJob(id=job_id, url=url))
            with patch.object(service, "_get_cached", return_value={"filename": filename}), patch.object(
                service, "_analyzer_media_is_valid", return_value=valid_media
            ):
                assert service.try_cached_download_result(job_id, url, web_app.SOURCE_API_UPLOAD, web_app.OUTPUT_DIR / "download_jobs" / f"{job_id}.json") is False
            assert not path.exists()
            cached_job = registry.snapshot(job_id)
            assert cached_job is not None and expected_log in cached_job.log

        fallback_id = "download-sociavault-fallback"
        fallback_url = "https://www.douyin.com/video/fallback"
        fallback_filename = "fallback.mp4"
        registry.register(fallback_id, DownloadJob(id=fallback_id, url=fallback_url, source=web_app.SOURCE_WEB_MANUAL))
        fallback_result_path = web_app.OUTPUT_DIR / "download_jobs" / f"{fallback_id}.json"
        fallback_registered: list[dict[str, Any]] = []
        fallback_visible: list[tuple[str, str, str]] = []
        social: list[tuple[str, bool]] = []
        fallback_order: list[str] = []

        def cache_result_miss(*_args: Any) -> bool:
            fallback_order.append("download-cache")
            return False

        def video_info_cache_miss(*_args: Any) -> bool:
            fallback_order.append("video-info-cache")
            return False

        def crawler_failure(*_args: Any, **_kwargs: Any) -> None:
            fallback_order.append("original-downloader")
            raise RuntimeError("fixture crawler failure")

        def socia_fallback(job_id: str, actual_url: str, source: str, result_path: Path) -> bool:
            fallback_order.append("sociavault-video-info")
            assert (job_id, actual_url, source, result_path) == (
                fallback_id, fallback_url, web_app.SOURCE_WEB_MANUAL, fallback_result_path,
            )
            (web_app.VIDEOS_DIR / fallback_filename).write_bytes(b"fixture")
            result_path.write_text(json.dumps({"filename": fallback_filename, "id": "fallback-video"}), encoding="utf-8")
            return True

        with patch.object(service, "try_cached_download_result", side_effect=cache_result_miss), patch.object(
            service, "try_cached_video_info_download", side_effect=video_info_cache_miss
        ), patch.object(service, "run_command", side_effect=crawler_failure), patch.object(
            service, "try_sociavault_video_info_download", side_effect=socia_fallback
        ), patch.object(service, "_register_video", side_effect=lambda **kwargs: fallback_registered.append(kwargs)), patch.object(
            service, "_make_web_manual_visible", side_effect=lambda *args: fallback_visible.append(args)
        ), patch.object(service, "_start_social_context_job", side_effect=lambda filename, *, generate_insights: social.append((filename, generate_insights))):
            service.run_job(fallback_id)
        fallback = registry.snapshot(fallback_id)
        assert fallback is not None and fallback.status == "complete"
        assert fallback.filename == fallback_filename and fallback.result == {"filename": fallback_filename, "id": "fallback-video"}
        assert any("原下载器失败，最后降级调用 SociaVault video-info：fixture crawler failure" == line for line in fallback.log)
        assert fallback_registered and fallback_registered[0]["source"] == web_app.SOURCE_WEB_MANUAL
        assert fallback_visible == [(web_app.SOURCE_WEB_MANUAL, web_app.platform_for_url(fallback_url), "fallback-video")]
        assert social == [(fallback_filename, True)]
        assert fallback_order == [
            "download-cache",
            "video-info-cache",
            "original-downloader",
            "sociavault-video-info",
        ]

        for suffix, expected_log in (
            (".mp3", "删除无效音频文件并降级到 SociaVault video-info"),
            (".mp4", "删除无效视频文件并降级到 SociaVault video-info"),
        ):
            job_id = f"download-original-{suffix[1:]}"
            crawler_filename = f"crawler-output{suffix}"
            recovered_filename = f"recovered-{suffix[1:]}.mp4"
            registry.register(job_id, DownloadJob(id=job_id, url=url))
            fallback_calls: list[tuple[str, str, str, Path]] = []

            def crawler_output(_job_id: str, _command: list[str]) -> None:
                (web_app.VIDEOS_DIR / crawler_filename).write_bytes(b"crawler")
                (web_app.OUTPUT_DIR / "download_jobs" / f"{job_id}.json").write_text(
                    json.dumps({"filename": crawler_filename}), encoding="utf-8"
                )

            def recover_from_sociavault(actual_job_id: str, actual_url: str, source: str, result_path: Path) -> bool:
                fallback_calls.append((actual_job_id, actual_url, source, result_path))
                (web_app.VIDEOS_DIR / recovered_filename).write_bytes(b"recovered")
                result_path.write_text(json.dumps({"filename": recovered_filename}), encoding="utf-8")
                return True

            def reject_invalid_media(path: Path) -> None:
                path.unlink(missing_ok=True)
                raise RuntimeError("fixture invalid media")

            invalid_media = reject_invalid_media if suffix == ".mp4" else None
            with patch.object(service, "try_cached_download_result", return_value=False), patch.object(
                service, "try_cached_video_info_download", return_value=False
            ), patch.object(service, "run_command", side_effect=crawler_output), patch.object(
                service, "try_sociavault_video_info_download", side_effect=recover_from_sociavault
            ), patch.object(service, "_ensure_analyzer_media_or_delete", side_effect=invalid_media), patch.object(
                service, "_start_social_context_job", return_value=False
            ):
                service.run_job(job_id)
            recovered = registry.snapshot(job_id)
            assert recovered is not None and recovered.status == "complete"
            assert recovered.filename == recovered_filename
            assert not (web_app.VIDEOS_DIR / crawler_filename).exists()
            assert any(expected_log in line for line in recovered.log)
            assert fallback_calls == [
                (job_id, url, web_app.SOURCE_API_UPLOAD, web_app.OUTPUT_DIR / "download_jobs" / f"{job_id}.json")
            ]

        dynamic_job_id = "download-dynamic-boundaries"
        registry.register(dynamic_job_id, DownloadJob(id=dynamic_job_id, url=url))
        proxy_attempts: list[dict[str, str] | None] = []

        class DirectResponse:
            headers = {"Content-Length": "524288"}

            def __enter__(self) -> "DirectResponse":
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def iter_content(*, chunk_size: int) -> list[bytes]:
                assert chunk_size == 1024 * 1024
                return [b"x" * 524288]

        def direct_get(_media_url: str, **kwargs: Any) -> DirectResponse:
            proxy_attempts.append(kwargs["proxies"])
            if kwargs["proxies"] is not None:
                raise RuntimeError("fixture proxy failure")
            return DirectResponse()

        with patch.object(service, "_requests_get", side_effect=direct_get), patch.dict(
            os.environ,
            {"TIKTOK_MAX_BYTES": "600000", "TIKTOK_PROXY_URL": "http://proxy.fixture:8080"},
        ), patch.object(service, "_ensure_us_proxy", return_value=None), patch.object(
            service, "_ensure_analyzer_media_or_delete", return_value=None
        ):
            direct_result = service._download_direct_media(
                dynamic_job_id,
                "https://cdn.fixture/video.mp4",
                url,
                {"id": "dynamic-video"},
            )
        assert proxy_attempts == [{"http": "http://proxy.fixture:8080", "https": "http://proxy.fixture:8080"}, None]
        assert direct_result["filename"] == "shortvideo_SociaVault_dynamic-video.mp4"
        assert direct_result["size"] == 524288

        max_bytes_error = RuntimeError("not raised")
        with patch.object(service, "_requests_get", side_effect=lambda *_args, **_kwargs: DirectResponse()), patch.dict(
            os.environ, {"TIKTOK_MAX_BYTES": "524287", "TIKTOK_PROXY_URL": ""}
        ), patch.object(service, "_ensure_us_proxy", return_value=None):
            try:
                service._download_direct_media(dynamic_job_id, "https://cdn.fixture/too-large.mp4", url, {"id": "too-large"})
            except RuntimeError as exc:
                max_bytes_error = exc
        assert str(max_bytes_error) == "direct: SociaVault media is too large: 524288 bytes"

        class StreamingTooLargeResponse:
            headers: dict[str, str] = {}

            def __enter__(self) -> "StreamingTooLargeResponse":
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def iter_content(*, chunk_size: int) -> list[bytes]:
                assert chunk_size == 1024 * 1024
                return [b"a" * 400000, b"b" * 200000]

        stream_target = web_app.VIDEOS_DIR / "shortvideo_SociaVault_stream-over-limit.mp4"
        stream_part = stream_target.with_suffix(".mp4.part")
        with patch.object(service, "_requests_get", side_effect=lambda *_args, **_kwargs: StreamingTooLargeResponse()), patch.dict(
            os.environ, {"TIKTOK_MAX_BYTES": "500000", "TIKTOK_PROXY_URL": ""}
        ), patch.object(service, "_ensure_us_proxy", return_value=None):
            try:
                service._download_direct_media(dynamic_job_id, "https://cdn.fixture/stream-over-limit.mp4", url, {"id": "stream-over-limit"})
            except RuntimeError as exc:
                assert str(exc) == "direct: SociaVault media exceeded max size: 600000 bytes"
            else:
                raise AssertionError("streaming over-limit media must fail")
        assert not stream_part.exists()
        assert not stream_target.exists()

        missing_key_id = "download-sociavault-missing-key"
        registry.register(missing_key_id, DownloadJob(id=missing_key_id, url=url))
        with patch.dict(os.environ, {"SOCIAVAULT_API_KEY": ""}), patch.object(
            service, "run_command", side_effect=AssertionError("missing key must skip command")
        ):
            assert service.try_sociavault_video_info_download(
                missing_key_id, url, web_app.SOURCE_API_UPLOAD, web_app.OUTPUT_DIR / "download_jobs" / f"{missing_key_id}.json"
            ) is False
        missing_key = registry.snapshot(missing_key_id)
        assert missing_key is not None and missing_key.log == ["未配置 SOCIAVAULT_API_KEY，跳过 SociaVault video-info。"]

        configured_id = "download-sociavault-configured"
        registry.register(configured_id, DownloadJob(id=configured_id, url=url))
        configured_result = web_app.OUTPUT_DIR / "download_jobs" / f"{configured_id}.json"
        captured_sociavault_commands: list[list[str]] = []

        def capture_sociavault_command(_job_id: str, command: list[str]) -> None:
            captured_sociavault_commands.append(command)
            output_path = Path(command[command.index("--output") + 1])
            output_path.write_text(json.dumps({"data": "fixture"}), encoding="utf-8")

        with patch.dict(os.environ, {"SOCIAVAULT_API_KEY": "fixture-key", "SOCIAVAULT_API_BASE": "https://api.fixture.test/"}), patch.object(
            service, "run_command", side_effect=capture_sociavault_command
        ), patch.object(service, "_try_video_info_payload_download", return_value=False):
            assert service._sociavault_video_info_request(url) == {
                "api_base": "https://api.fixture.test",
                "endpoint": "video-info",
                "params": {"url": url},
            }
            assert service.try_sociavault_video_info_download(
                configured_id, url, web_app.SOURCE_API_UPLOAD, configured_result
            ) is False
        assert captured_sociavault_commands == [[
            "python",
            str(web_app.SCRIPTS_DIR / "sociavault_tiktok.py"),
            "--endpoint",
            "video-info",
            "--url",
            url,
            "--output",
            str(configured_result.with_suffix(".sociavault-video-info.json")),
        ]]
    finally:
        pass


def assert_real_metrics_worker_registry_updates(web_app: Any) -> None:
    registry = web_app.JobRegistry()
    registered_payloads: list[tuple[dict[str, Any], str]] = []

    def record_video(payload: dict[str, Any], *, source_url: str) -> None:
        registered_payloads.append((payload, source_url))

    service = MetricsService(
        registry=registry,
        root=web_app.ROOT,
        output_dir=web_app.OUTPUT_DIR,
        scripts_dir=web_app.SCRIPTS_DIR,
        read_json_file=web_app.read_json,
        popen_factory=lambda *args, **kwargs: subprocess.Popen(*args, **kwargs),
        thread_factory=threading.Thread,
        job_id_factory=lambda: "unused",
        register_from_payload=record_video,
    )
    success_id = "metrics-worker-success"
    success_target = "https://www.tiktok.com/@fixture/video/metrics-worker"
    registry.register(success_id, MetricsJob(id=success_id, target=success_target, endpoint="video-info"))

    def command_success(job_id: str, command: list[str]) -> None:
        assert job_id == success_id
        assert command[command.index("--endpoint") + 1] == "video-info"
        assert command[command.index("--url") + 1] == success_target
        result_path = Path(command[command.index("--output") + 1])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({"metric": {"views": 7}}), encoding="utf-8")

    with patch.object(service, "run_command", side_effect=command_success):
        service.run_job(success_id)
    success = registry.snapshot(success_id)
    assert success is not None
    assert success.status == "complete"
    expected_output_dir = str((web_app.OUTPUT_DIR / "tiktok_api" / success_id).relative_to(web_app.ROOT))
    assert success.output_dir == expected_output_dir
    result_path = web_app.OUTPUT_DIR / "tiktok_api" / success_id / "result.json"
    assert web_app.read_json(result_path) == {"metric": {"views": 7}}
    payload = service.payload_for(success_id)
    assert payload is not None and payload["result"] == {"metric": {"views": 7}}
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
    registry.register(command_log_id, MetricsJob(id=command_log_id, target="@fixture", endpoint="profile"))
    command = ["python", "fixture-metrics.py"]
    with patch.object(
        subprocess,
        "Popen",
        return_value=FakeMetricsProcess(["fixture stdout  \n", "second stdout\r\n"], 0),
    ) as popen:
        service.run_command(command_log_id, command)
    popen.assert_called_once()
    command_log = registry.snapshot(command_log_id)
    assert command_log is not None
    assert command_log.log == ["$ python fixture-metrics.py", "fixture stdout", "second stdout"]

    command_failure_id = "metrics-command-failure"
    registry.register(command_failure_id, MetricsJob(id=command_failure_id, target="@fixture", endpoint="profile"))
    failure_command = ["python", "fixture-metrics-fail.py"]
    with patch.object(subprocess, "Popen", return_value=FakeMetricsProcess([], 9)):
        try:
            service.run_command(command_failure_id, failure_command)
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
        registry.register(job_id, MetricsJob(id=job_id, target=target, endpoint=endpoint))
        commands: list[list[str]] = []

        def capture_command(captured_job_id: str, command: list[str]) -> None:
            assert captured_job_id == job_id
            commands.append(command)
            result_path = Path(command[command.index("--output") + 1])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps({"metric": {"case": index}}), encoding="utf-8")

        with patch.object(service, "run_command", side_effect=capture_command):
            service.run_job(job_id)
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
    registry.register(failure_id, MetricsJob(id=failure_id, target="@fixture", endpoint="profile"))

    def command_failure(job_id: str, _command: list[str]) -> None:
        service.append_log(job_id, "fixture prior failure")
        raise RuntimeError("fixture raw metrics failure")

    with patch.object(service, "run_command", side_effect=command_failure):
        service.run_job(failure_id)
    failure = registry.snapshot(failure_id)
    assert failure is not None
    assert failure.status == "failed"
    assert failure.error == "fixture raw metrics failure"
    assert failure.log[-2:] == ["fixture prior failure", "fixture raw metrics failure"]


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


def assert_real_amazon_worker_registry_updates(web_app: Any) -> None:
    registry = web_app.JobRegistry()

    def make_service(**overrides: Any) -> AmazonService:
        dependencies = {
            "registry": registry,
            "root": web_app.ROOT,
            "output_dir": web_app.OUTPUT_DIR,
            "read_json_file": web_app.read_json,
            "write_json_file": web_app.atomic_write_json,
            "popen_factory": lambda *args, **kwargs: subprocess.Popen(*args, **kwargs),
            "thread_factory": threading.Thread,
            "job_id_factory": lambda: "amazon-worker-fixture",
            "environ": os.environ,
            "ensure_us_proxy": lambda *_args, **_kwargs: None,
            "get_cached_or_call": lambda _service, _operation, _request, fetch, **_kwargs: fetch(),
            "cache_log_label": lambda _payload: None,
        }
        dependencies.update(overrides)
        return AmazonService(**dependencies)

    class FakeAmazonProcess:
        def __init__(self, lines: list[str], returncode: int) -> None:
            self.stdout = iter(lines)
            self.returncode = returncode

        def wait(self) -> int:
            return self.returncode

    service = make_service()
    command_success_id = "amazon-command-success"
    command = ["docker", "fixture-amazon"]
    registry.register(
        command_success_id,
        AmazonJob(
            id=command_success_id,
            target="B000COMMAND",
            target_type="asin",
            url="https://www.amazon.com/dp/B000COMMAND",
            pages=1,
        ),
    )
    with patch.object(
        subprocess, "Popen", return_value=FakeAmazonProcess(["first stdout  \n", "second stdout\r\n"], 0)
    ) as popen:
        output, code = service.run_command(command_success_id, command)
    assert (output, code) == ("first stdout  \nsecond stdout\r\n", 0)
    assert popen.call_args.args == (command,)
    assert popen.call_args.kwargs["cwd"] == web_app.ROOT
    assert popen.call_args.kwargs["stdout"] is subprocess.PIPE
    assert popen.call_args.kwargs["stderr"] is subprocess.STDOUT
    assert popen.call_args.kwargs["text"] is True
    assert popen.call_args.kwargs["env"] == dict(os.environ)
    assert popen.call_args.kwargs["env"] is not os.environ
    assert registry.snapshot(command_success_id).log == [
        "$ docker fixture-amazon", "first stdout", "second stdout",
    ]

    command_failure_id = "amazon-command-failure"
    registry.register(
        command_failure_id,
        AmazonJob(
            id=command_failure_id,
            target="B000COMMANDFAIL",
            target_type="asin",
            url="https://www.amazon.com/dp/B000COMMANDFAIL",
            pages=1,
        ),
    )
    with patch.object(
        subprocess, "Popen", return_value=FakeAmazonProcess(["failure stdout  \n"], 7)
    ) as popen:
        output, code = service.run_command(command_failure_id, ["docker", "fixture-amazon-fail"])
    assert (output, code) == ("failure stdout  \n", 7)
    assert popen.call_args.args == (["docker", "fixture-amazon-fail"],)
    assert registry.snapshot(command_failure_id).log == [
        "$ docker fixture-amazon-fail", "failure stdout", "Command exited with code 7",
    ]

    for output, expected in (
        ('{"products":[{"asin":"B000PURE01"}]}', {"products": [{"asin": "B000PURE01"}]}),
        (
            'scraper booting\n{"small":true}\nresult={"products":[{"asin":"B000LARGEST","title":"fixture product"}]}\n',
            {"products": [{"asin": "B000LARGEST", "title": "fixture product"}]},
        ),
    ):
        assert parse_json_from_process_output(output) == expected
    for output, message in (("", "amazon-scraper returned no output"), ("scraper only logs", "amazon-scraper output did not contain JSON")):
        try:
            parse_json_from_process_output(output)
        except ValueError as exc:
            assert str(exc) == message
        else:
            raise AssertionError("invalid amazon-scraper output must fail")

    success_id = "amazon-worker-success"
    success_url = "https://www.amazon.com/dp/B000WORKER"
    registry.register(
        success_id,
        AmazonJob(id=success_id, target="B000WORKER", target_type="asin", url=success_url, pages=3),
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

    def cache_success(service_name: str, operation: str, request: dict[str, Any], fetch: Any, *, metadata_builder: Any) -> dict[str, Any]:
        cache_calls.append((service_name, operation, request))
        assert metadata_builder(command_payload) == {
            "entity_type": "amazon", "entity_id": "B000WORKER", "title": "fixture product", "source_url": success_url,
        }
        registry.update_fields(success_id, {"url": "https://www.amazon.com/dp/B000MUTATED", "pages": 5})
        return fetch()

    worker_service = make_service(ensure_us_proxy=ensure_proxy, get_cached_or_call=cache_success)
    worker_snapshot_ids: list[str] = []
    original_snapshot = registry.snapshot

    def recording_worker_snapshot(job_id: str) -> Any:
        worker_snapshot_ids.append(job_id)
        return original_snapshot(job_id)

    with patch.object(worker_service, "run_command", side_effect=command_success), patch.object(
        registry, "snapshot", side_effect=recording_worker_snapshot
    ):
        worker_service.run_job(success_id)
    assert worker_snapshot_ids == [success_id]
    success = registry.snapshot(success_id)
    assert success is not None and success.status == "complete"
    output_dir = web_app.OUTPUT_DIR / "amazon" / success_id
    assert success.output_dir == str(output_dir.relative_to(web_app.ROOT))
    assert proxy_calls == ["amazon"]
    assert cache_calls == [("amazon_scraper", "web", {"url": success_url, "pages": 3})]
    assert len(commands) == 1
    assert success.url == "https://www.amazon.com/dp/B000MUTATED" and success.pages == 5
    result_path = output_dir / "result.json"
    assert web_app.read_json(result_path) == command_payload
    payload = worker_service.payload_for(success_id)
    assert payload is not None and payload["result"] == command_payload
    payload["result"]["products"][0]["title"] = "mutated"
    assert web_app.read_json(result_path) == command_payload

    error_id = "amazon-worker-result-error"
    registry.register(
        error_id,
        AmazonJob(id=error_id, target="B000ERROR1", target_type="asin", url="https://www.amazon.com/dp/B000ERROR1", pages=1),
    )
    error_process_output = json.dumps({"status": "ERROR", "message": "fixture scraper error"}) + "\n"
    with patch.object(
        subprocess, "Popen", return_value=FakeAmazonProcess([error_process_output], 9)
    ):
        service.run_job(error_id)
    result_error = registry.snapshot(error_id)
    assert result_error is not None and result_error.status == "failed"
    assert result_error.error == "fixture scraper error"
    assert result_error.log[-1] == "Command exited with code 9"
    assert "fixture scraper error" not in result_error.log
    assert web_app.read_json(web_app.OUTPUT_DIR / "amazon" / error_id / "result.json") == {
        "status": "ERROR", "message": "fixture scraper error",
    }

    docker_missing_id = "amazon-worker-docker-missing"
    registry.register(
        docker_missing_id,
        AmazonJob(id=docker_missing_id, target="B000DOCKER", target_type="asin", url="https://www.amazon.com/dp/B000DOCKER", pages=1),
    )
    make_service(get_cached_or_call=lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError())).run_job(docker_missing_id)
    docker_missing = registry.snapshot(docker_missing_id)
    assert docker_missing is not None and docker_missing.status == "failed"
    assert docker_missing.error == "Docker CLI is not available in the web container"
    assert docker_missing.log[-1] == docker_missing.error

    failure_id = "amazon-worker-failure"
    registry.register(
        failure_id,
        AmazonJob(id=failure_id, target="B000FAILURE", target_type="asin", url="https://www.amazon.com/dp/B000FAILURE", pages=1),
    )
    make_service(get_cached_or_call=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture raw amazon failure"))).run_job(failure_id)
    failure = registry.snapshot(failure_id)
    assert failure is not None and failure.status == "failed"
    assert failure.error == "fixture raw amazon failure"
    assert failure.log[-1] == failure.error

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
        composed_download_service = web_app.download_service
        composed_metrics_registry = web_app.metrics_job_registry
        composed_amazon_registry = web_app.amazon_job_registry
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
            registry = composed_metrics_registry
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
            registry = composed_amazon_registry
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

        assert_download_command_cache_and_fallback_contract(web_app)

        with ExitStack() as patches:
            patches.enter_context(
                patch.object(web_app, "ui_test_mode_allows_live_write", side_effect=lambda path: path in allowed_writes)
            )
            patches.enter_context(patch.object(composed_download_service, "run_job", side_effect=complete_download))
            patches.enter_context(patch.object(web_app.shop_service, "run_job", side_effect=complete_shop))
            patches.enter_context(patch.object(web_app.metrics_service, "run_job", side_effect=complete_metrics))
            patches.enter_context(patch.object(web_app.amazon_service, "run_job", side_effect=complete_amazon))
            patches.enter_context(patch.object(web_app, "video_queue", fake_queue))
            patches.enter_context(patch.object(composed_download_service, "_ensure_analyzer_media_or_delete", side_effect=lambda _path: None))
            patches.enter_context(patch.object(composed_download_service, "_register_video", side_effect=lambda **_kwargs: None))
            patches.enter_context(patch.object(composed_download_service, "_make_web_manual_visible", side_effect=lambda *_args: None))
            patches.enter_context(patch.object(composed_download_service, "_start_social_context_job", side_effect=lambda *_args, **_kwargs: None))
            patches.enter_context(patch.object(composed_download_service, "_analyzer_media_is_valid", side_effect=lambda _path: True))
            patches.enter_context(patch.object(web_app, "analyzer_visible_source", side_effect=lambda _name: True))
            patches.enter_context(patch.object(web_app, "analyzer_media_is_valid", side_effect=lambda _path: True))

            assert_real_download_worker_registry_updates(web_app)
            assert_real_shop_worker_registry_updates(web_app)
            assert_real_metrics_worker_registry_updates(web_app)
            assert_real_amazon_worker_registry_updates(web_app)

            server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port

            assert_upload_http_contract(web_app, port)
            assert_analyze_http_contract(web_app, port)
            assert_postprocess_http_contract(web_app, port)
            assert_translate_http_contract(web_app, port)

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
            for payload, error in (
                ({"url": "https://example.invalid/video"}, "Only TikTok or Douyin URLs are supported"),
                ({"url": "https://www.tiktok.com/" + "x" * 2049}, "URL is too long"),
            ):
                status, _headers, invalid_download = json_request(port, "POST", "/api/download", payload)
                assert status == 400 and invalid_download == {"error": error}
            status, _headers, malformed_download = json_request(
                port,
                "POST",
                "/api/download",
                body=b"{not json",
                content_type="application/json",
            )
            assert status == 400 and "Expecting property name enclosed in double quotes" in malformed_download["error"]
            status, health_headers, health = json_request(port, "GET", "/healthz")
            assert status == 200
            assert health_headers.get("content-type") == "application/json; charset=utf-8"
            assert health == {"status": "ok", "ui_test_mode": True}

            with patch.object(web_app, "ui_test_mode_allows_live_write", return_value=False), patch.object(
                composed_download_service,
                "create_and_start",
                side_effect=AssertionError("UI_TEST gate must run before the Download service"),
            ):
                status, _headers, blocked_download = json_request(
                    port,
                    "POST",
                    "/api/download",
                    {"url": "https://www.tiktok.com/@fixture/video/blocked"},
                )
            assert status == 409
            assert blocked_download == {
                "error": "UI 测试模式已拦截写操作，未触发真实业务。",
                "simulated": True,
                "status": "blocked",
                "path": "/api/download",
            }

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
            status, _headers, alias_feedback = json_request(port, "GET", f"/api/video-feedback?download_id={download_id}")
            assert status == 200 and alias_feedback["state"] == "downloading"
            download_release.set()
            job = wait_for_job(port, f"/api/download-job?id={download_id}", "complete")
            assert job["result"] == {"filename": "fixture-download.mp4", "source": "fixture"}
            event = sse_payload(port, f"/api/download-events?id={download_id}")
            assert event["status"] == "complete" and event["result"] == job["result"]
            status, _headers, complete_feedback = json_request(port, "GET", f"/api/video-feedback?download_job_id={download_id}")
            assert status == 200
            assert complete_feedback["state"] == "uploaded"
            assert complete_feedback["download"] == job
            web_app.download_job_registry.update_fields(download_id, {"status": "failed", "error": "fixture feedback failure"})
            status, _headers, failed_feedback = json_request(port, "GET", f"/api/video-feedback?download_id={download_id}")
            assert status == 200
            assert failed_feedback["state"] == "failed"
            assert failed_feedback["failure_stage"] == "download"
            assert failed_feedback["failure_reason"] == "fixture feedback failure"
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

            with patch.object(web_app, "ui_test_mode_allows_live_write", return_value=False), patch.object(
                web_app.metrics_service,
                "create_and_start",
                side_effect=AssertionError("UI_TEST gate must run before the Metrics route"),
            ):
                status, _headers, blocked_metrics = json_request(
                    port,
                    "POST",
                    "/api/video-metrics",
                    {"target": "@blocked-fixture", "endpoint": "profile"},
                )
            assert status == 409
            assert blocked_metrics == {
                "error": "UI 测试模式已拦截写操作，未触发真实业务。",
                "simulated": True,
                "status": "blocked",
                "path": "/api/video-metrics",
            }

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

            with patch.object(web_app, "ui_test_mode_allows_live_write", return_value=False), patch.object(
                web_app.amazon_service,
                "create_and_start",
                side_effect=AssertionError("UI_TEST gate must run before the Amazon handler"),
            ):
                status, _headers, blocked_amazon = json_request(
                    port,
                    "POST",
                    "/api/amazon-scrape",
                    {"target": "B000BLOCK1", "target_type": "asin"},
                )
            assert status == 409
            assert blocked_amazon == {
                "error": "UI 测试模式已拦截写操作，未触发真实业务。",
                "simulated": True,
                "status": "blocked",
                "path": "/api/amazon-scrape",
            }

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
            with patch.dict(os.environ, {"AMAZON_MAX_PAGES": "99"}):
                run_amazon_success(
                    {"target": "B000ZERO01", "target_type": "asin", "pages": 0},
                    {
                        "target": "B000ZERO01",
                        "target_type": "asin",
                        "url": "https://www.amazon.com/dp/B000ZERO01",
                        "pages": 5,
                    },
                )
            with patch.dict(os.environ, {"AMAZON_MAX_PAGES": "-2"}):
                run_amazon_success(
                    {"target": "B000LOW001", "target_type": "asin", "pages": 0},
                    {
                        "target": "B000LOW001",
                        "target_type": "asin",
                        "url": "https://www.amazon.com/dp/B000LOW001",
                        "pages": 1,
                    },
                )
            with patch.dict(os.environ, {"AMAZON_MAX_PAGES": "3"}):
                status, _headers, string_zero_pages = json_request(
                    port,
                    "POST",
                    "/api/amazon-scrape",
                    {"target": "B000STR001", "target_type": "asin", "pages": "0"},
                )
                assert status == 400 and string_zero_pages == {"error": "pages must be between 1 and 5"}
                run_amazon_success(
                    {"target": "B000STR002", "target_type": "asin", "pages": "2"},
                    {
                        "target": "B000STR002",
                        "target_type": "asin",
                        "url": "https://www.amazon.com/dp/B000STR002",
                        "pages": 2,
                    },
                )
            with patch.dict(os.environ, {"AMAZON_MAX_PAGES": "not-a-number"}):
                status, _headers, invalid_max_pages = json_request(
                    port,
                    "POST",
                    "/api/amazon-scrape",
                    {"target": "B000ENV001", "target_type": "asin"},
                )
            assert status == 400 and invalid_max_pages == {
                "error": "invalid literal for int() with base 10: 'not-a-number'"
            }
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

            upload_sources: list[str] = []

            def fixture_upload(file_items: list[Any], *, source: str) -> dict[str, list[dict[str, Any]]]:
                upload_sources.append(source)
                assert len(file_items) == 1 and file_items[0].filename == "fixture.mp4"
                target = web_app.VIDEOS_DIR / "fixture.mp4"
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as target_file:
                    shutil.copyfileobj(file_items[0].file, target_file)
                return {"files": [{"filename": target.name, "size": target.stat().st_size}], "errors": []}

            upload_body, upload_type = multipart_video("fixture.mp4", b"not-a-real-video")
            with patch.object(web_app.upload_service, "upload", side_effect=fixture_upload):
                status, _headers, uploaded = json_request(
                    port, "POST", "/api/upload", body=upload_body, content_type=upload_type
                )
            assert status == 200 and uploaded["files"] == [{"filename": "fixture.mp4", "size": 16}]
            assert upload_sources == [web_app.SOURCE_API_UPLOAD]
            filename = "fixture.mp4"
            status, _headers, files = json_request(port, "GET", "/api/files")
            assert status == 200 and any(item["name"] == filename for item in files)

            analyze_router = Router()
            register_analyze_routes(
                analyze_router,
                AnalyzeService(
                    videos_dir=web_app.VIDEOS_DIR,
                    output_dir_for_filename=lambda requested: web_app.OUTPUT_DIR / requested,
                    safe_filename=web_app.safe_filename,
                    queue_enqueue=fake_queue.enqueue,
                ),
            )
            with patch.object(web_app, "WEB_ROUTER", analyze_router):
                status, _headers, analyzed = json_request(
                    port, "POST", "/api/analyze", {"filename": filename, "analysis_prompt": "fixture"}
                )
            assert status == 202 and analyzed["queued"] == ["analyze"]
            assert fake_queue.calls[-1] == (filename, "analyze")

            translate_router = Router()
            register_translate_routes(
                translate_router,
                TranslateService(
                    root=web_app.ROOT,
                    scripts_dir=web_app.SCRIPTS_DIR,
                    output_dir_for_filename=lambda requested: web_app.OUTPUT_DIR / requested,
                    safe_filename=web_app.safe_filename,
                    run_factory=fake_translate,
                    environ=os.environ,
                ),
            )
            with patch.object(web_app, "WEB_ROUTER", translate_router):
                status, _headers, translated = json_request(
                    port, "POST", "/api/translate", {"filename": filename, "tab": "content"}
                )
            assert status == 200 and translated == {"status": "translated", "filename": filename, "tab": "content"}

            postprocess_router = Router()
            register_postprocess_routes(
                postprocess_router,
                PostprocessService(
                    output_dir_for_filename=lambda requested: web_app.OUTPUT_DIR / requested,
                    safe_filename=web_app.safe_filename,
                    queue_enqueue=fake_queue.enqueue,
                ),
            )
            with patch.object(web_app, "WEB_ROUTER", postprocess_router):
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
