#!/usr/bin/env python3
"""Observable job payload and SSE contracts before the job snapshot migration."""

from __future__ import annotations

import ast
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


class RecordingWriter(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class FakeHandler:
    def __init__(self, path: str = "") -> None:
        self.path = path
        self.headers: dict[str, str] = {}
        self.command = "GET"
        self.wfile = RecordingWriter()
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


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def sse_body(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


def dispatch_get(web_app: Any, path: str) -> FakeHandler:
    handler = FakeHandler(path)
    for name in (
        "stream_events",
        "stream_download_events",
        "stream_shop_events",
        "stream_metrics_events",
        "stream_amazon_events",
    ):
        setattr(handler, name, getattr(web_app.Handler, name).__get__(handler, FakeHandler))
    web_app.Handler.do_GET(handler)
    return handler


def assert_json_response(handler: FakeHandler, status: int, payload: dict[str, Any]) -> None:
    assert handler.responses == [status]
    assert handler.header("Content-Type") == "application/json; charset=utf-8"
    assert handler.header("Content-Length") == str(len(json_body(payload)))
    assert handler.wfile.getvalue() == json_body(payload)
    assert handler.ended is True


def assert_sse_response(handler: FakeHandler, payload: dict[str, Any]) -> None:
    assert handler.responses == [200]
    assert handler.header("Content-Type") == "text/event-stream; charset=utf-8"
    assert handler.header("Cache-Control") == "no-cache"
    assert handler.header("Connection") == "keep-alive"
    assert handler.wfile.getvalue() == sse_body(payload)
    assert handler.wfile.flush_count == 1
    assert handler.ended is True
    assert handler.close_connection is True


def make_jobs(web_app: Any) -> dict[str, Any]:
    download_result = {"filename": "fixture.mp4", "meta": {"source": "fixture"}}
    download = web_app.DownloadJob(
        id="download-fixture",
        url="https://www.tiktok.com/@fixture/video/123",
        status="complete",
        created_at=10.0,
        updated_at=11.0,
        log=[f"download-{index}" for index in range(82)],
        filename="fixture.mp4",
        result=download_result,
    )
    shop = web_app.ShopJob(
        id="shop-fixture",
        url="https://shop.tiktok.com/view/product/fixture",
        source_type="product",
        region="US",
        max_pages=2,
        review_pages=1,
        analyze=True,
        related_videos=True,
        prompt="private fixture prompt",
        status="complete",
        created_at=20.0,
        updated_at=21.0,
        log=[f"shop-{index}" for index in range(122)],
        output_dir="output/tiktok_shop/shop-fixture",
    )
    metrics = web_app.MetricsJob(
        id="metrics-fixture",
        target="@fixture",
        endpoint="video-info",
        status="complete",
        created_at=30.0,
        updated_at=31.0,
        log=[f"metrics-{index}" for index in range(122)],
        output_dir="output/tiktok_api/metrics-fixture",
    )
    amazon = web_app.AmazonJob(
        id="amazon-fixture",
        target="B000FIXTURE",
        target_type="asin",
        url="https://www.amazon.com/dp/B000FIXTURE",
        pages=2,
        status="complete",
        created_at=40.0,
        updated_at=41.0,
        log=[f"amazon-{index}" for index in range(122)],
        output_dir="output/amazon/amazon-fixture",
    )
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / shop.id / "shop_extract.json", {"items": [{"id": "extract"}]})
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / shop.id / "shop_analysis.json", {"summary": "analysis"})
    write_json(web_app.OUTPUT_DIR / "tiktok_api" / metrics.id / "result.json", {"metric": {"views": 7}})
    write_json(web_app.OUTPUT_DIR / "amazon" / amazon.id / "result.json", {"products": [{"asin": "B000FIXTURE"}]})
    return {"download": download, "shop": shop, "metrics": metrics, "amazon": amazon}


def assert_public_payloads(web_app: Any, jobs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payloads = {
        "download": web_app.public_download_job(jobs["download"]),
        "shop": web_app.public_shop_job(jobs["shop"]),
        "metrics": web_app.public_metrics_job(jobs["metrics"]),
        "amazon": web_app.public_amazon_job(jobs["amazon"]),
    }
    assert set(payloads["download"]) == {"id", "url", "status", "created_at", "updated_at", "filename", "error", "log", "result"}
    assert set(payloads["shop"]) == {"id", "url", "source_type", "region", "max_pages", "review_pages", "analyze", "related_videos", "status", "created_at", "updated_at", "output_dir", "error", "log", "extract", "analysis"}
    assert set(payloads["metrics"]) == {"id", "target", "endpoint", "status", "created_at", "updated_at", "output_dir", "error", "log", "result"}
    assert set(payloads["amazon"]) == {"id", "target", "target_type", "url", "pages", "status", "created_at", "updated_at", "output_dir", "error", "log", "result"}
    assert "source" not in payloads["download"]
    assert "prompt" not in payloads["shop"]
    assert payloads["download"] | {"log": None, "result": None} == {
        "id": "download-fixture",
        "url": "https://www.tiktok.com/@fixture/video/123",
        "status": "complete",
        "created_at": 10.0,
        "updated_at": 11.0,
        "filename": "fixture.mp4",
        "error": None,
        "log": None,
        "result": None,
    }
    assert payloads["shop"] | {"log": None, "extract": None, "analysis": None} == {
        "id": "shop-fixture",
        "url": "https://shop.tiktok.com/view/product/fixture",
        "source_type": "product",
        "region": "US",
        "max_pages": 2,
        "review_pages": 1,
        "analyze": True,
        "related_videos": True,
        "status": "complete",
        "created_at": 20.0,
        "updated_at": 21.0,
        "output_dir": "output/tiktok_shop/shop-fixture",
        "error": None,
        "log": None,
        "extract": None,
        "analysis": None,
    }
    assert payloads["metrics"] | {"log": None, "result": None} == {
        "id": "metrics-fixture",
        "target": "@fixture",
        "endpoint": "video-info",
        "status": "complete",
        "created_at": 30.0,
        "updated_at": 31.0,
        "output_dir": "output/tiktok_api/metrics-fixture",
        "error": None,
        "log": None,
        "result": None,
    }
    assert payloads["amazon"] | {"log": None, "result": None} == {
        "id": "amazon-fixture",
        "target": "B000FIXTURE",
        "target_type": "asin",
        "url": "https://www.amazon.com/dp/B000FIXTURE",
        "pages": 2,
        "status": "complete",
        "created_at": 40.0,
        "updated_at": 41.0,
        "output_dir": "output/amazon/amazon-fixture",
        "error": None,
        "log": None,
        "result": None,
    }
    assert payloads["download"]["log"] == [f"download-{index}" for index in range(2, 82)]
    for name in ("shop", "metrics", "amazon"):
        assert payloads[name]["log"] == [f"{name}-{index}" for index in range(2, 122)]
    for name, job in jobs.items():
        assert payloads[name]["log"] is not job.log
        original_log = list(payloads[name]["log"])
        job.log.append("after-snapshot")
        assert payloads[name]["log"] == original_log
    download_result = jobs["download"].result
    assert payloads["download"]["result"] == download_result
    assert payloads["download"]["result"] is not download_result
    assert payloads["download"]["result"]["meta"] is not download_result["meta"]
    assert json.loads(json_body(payloads["download"]))["result"] == download_result
    payloads["download"]["result"]["meta"]["source"] = "payload-mutated"
    assert download_result["meta"]["source"] == "fixture"
    download_result["meta"]["source"] = "job-mutated"
    assert payloads["download"]["result"]["meta"]["source"] == "payload-mutated"
    none_result = web_app.DownloadJob(id="download-none", url="https://example.invalid", result=None)
    assert web_app.public_download_job(none_result)["result"] is None
    assert payloads["shop"]["extract"] == {"items": [{"id": "extract"}]}
    assert payloads["shop"]["analysis"] == {"summary": "analysis"}
    assert payloads["metrics"]["result"] == {"metric": {"views": 7}}
    assert payloads["amazon"]["result"] == {"products": [{"asin": "B000FIXTURE"}]}

    original_shop_extract = payloads["shop"]["extract"]
    original_shop_analysis = payloads["shop"]["analysis"]
    original_metrics_result = payloads["metrics"]["result"]
    original_amazon_result = payloads["amazon"]["result"]
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_extract.json", {"items": [{"id": "extract-v2"}]})
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_analysis.json", {"summary": "analysis-v2"})
    write_json(web_app.OUTPUT_DIR / "tiktok_api" / jobs["metrics"].id / "result.json", {"metric": {"views": 8}})
    write_json(web_app.OUTPUT_DIR / "amazon" / jobs["amazon"].id / "result.json", {"products": [{"asin": "B000FIXTUREV2"}]})
    refreshed_shop = web_app.public_shop_job(jobs["shop"])
    refreshed_metrics = web_app.public_metrics_job(jobs["metrics"])
    refreshed_amazon = web_app.public_amazon_job(jobs["amazon"])
    assert refreshed_shop["extract"] == {"items": [{"id": "extract-v2"}]}
    assert refreshed_shop["analysis"] == {"summary": "analysis-v2"}
    assert refreshed_metrics["result"] == {"metric": {"views": 8}}
    assert refreshed_amazon["result"] == {"products": [{"asin": "B000FIXTUREV2"}]}
    assert original_shop_extract == {"items": [{"id": "extract"}]}
    assert original_shop_analysis == {"summary": "analysis"}
    assert original_metrics_result == {"metric": {"views": 7}}
    assert original_amazon_result == {"products": [{"asin": "B000FIXTURE"}]}
    assert refreshed_shop["extract"] is not original_shop_extract
    assert refreshed_shop["analysis"] is not original_shop_analysis
    assert refreshed_metrics["result"] is not original_metrics_result
    assert refreshed_amazon["result"] is not original_amazon_result
    original_shop_extract["items"][0]["id"] = "mutated"
    original_shop_analysis["summary"] = "mutated"
    original_metrics_result["metric"]["views"] = 99
    original_amazon_result["products"][0]["asin"] = "mutated"
    assert web_app.public_shop_job(jobs["shop"])["extract"] == {"items": [{"id": "extract-v2"}]}
    assert web_app.public_shop_job(jobs["shop"])["analysis"] == {"summary": "analysis-v2"}
    assert web_app.public_metrics_job(jobs["metrics"])["result"] == {"metric": {"views": 8}}
    assert web_app.public_amazon_job(jobs["amazon"])["result"] == {"products": [{"asin": "B000FIXTUREV2"}]}
    return payloads


def assert_no_download_legacy_store() -> None:
    tree = ast.parse((SCRIPTS_DIR / "web_app.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {"download_jobs", "download_jobs_lock"} & names
    private_registry_access = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "download_job_registry"
        and node.attr in {"_jobs", "_lock"}
    ]
    assert not private_registry_access


def assert_get_and_sse_contracts(web_app: Any, jobs: dict[str, Any]) -> None:
    download = jobs["download"]
    web_app.download_job_registry.register(download.id, download)
    expected = web_app.public_download_job(web_app.download_job_registry.snapshot(download.id))
    assert_json_response(dispatch_get(web_app, f"/api/download-job?id={download.id}"), 200, expected)
    assert_sse_response(dispatch_get(web_app, f"/api/download-events?id={download.id}"), expected)
    assert_json_response(dispatch_get(web_app, "/api/download-job?id=missing"), 404, {"error": "Download job not found"})
    assert_sse_response(
        dispatch_get(web_app, "/api/download-events?id=missing"),
        {"status": "missing", "error": "Download job not found"},
    )

    specs = (
        ("shop", web_app.shop_jobs_lock, web_app.shop_jobs, web_app.public_shop_job, "/api/shop-job", "/api/shop-events", "TikTok Shop job not found"),
        ("metrics", web_app.metrics_jobs_lock, web_app.metrics_jobs, web_app.public_metrics_job, "/api/video-metrics-job", "/api/video-metrics-events", "Video metrics job not found"),
        ("amazon", web_app.amazon_jobs_lock, web_app.amazon_jobs, web_app.public_amazon_job, "/api/amazon-job", "/api/amazon-events", "Amazon job not found"),
    )
    for name, lock, store, serializer, get_path, events_path, missing_message in specs:
        job = jobs[name]
        with lock:
            store.clear()
            store[job.id] = job
        expected = serializer(job)
        assert_json_response(dispatch_get(web_app, f"{get_path}?id={job.id}"), 200, expected)
        assert_sse_response(dispatch_get(web_app, f"{events_path}?id={job.id}"), expected)
        with lock:
            store.clear()
        assert_json_response(dispatch_get(web_app, f"{get_path}?id=missing"), 404, {"error": missing_message})
        assert_sse_response(
            dispatch_get(web_app, f"{events_path}?id=missing"),
            {"status": "missing", "error": missing_message},
        )


def assert_sse_marker(web_app: Any) -> None:
    job = web_app.DownloadJob(
        id="marker-fixture",
        url="https://www.tiktok.com/@fixture/video/marker",
        status="queued",
        created_at=50.0,
        updated_at=51.0,
        result={"version": 1},
    )
    original_registry = web_app.download_job_registry
    timestamps = iter((52.0, 53.0, 54.0, 55.0))
    registry = web_app.JobRegistry(clock=lambda: next(timestamps))
    registry.register(job.id, job)
    web_app.download_job_registry = registry
    handler = FakeHandler()
    original_sleep = web_app.time.sleep
    calls = 0

    def advance(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            registry.update_fields(job.id, {"result": {"version": 2}})
        elif calls == 2:
            registry.append_log(job.id, "marker log")
        elif calls == 3:
            registry.update_fields(job.id, {"error": "marker error"})
        else:
            registry.update_fields(job.id, {"status": "complete"})

    try:
        web_app.time.sleep = advance
        web_app.Handler.stream_download_events(handler, job.id)
    finally:
        web_app.time.sleep = original_sleep
        web_app.download_job_registry = original_registry
    frames = [json.loads(line[6:]) for line in handler.wfile.getvalue().decode("utf-8").splitlines() if line.startswith("data: ")]
    assert [frame["status"] for frame in frames] == ["queued", "queued", "queued", "queued", "complete"]
    assert frames[0]["result"] == {"version": 1}
    assert [frame["result"] for frame in frames[1:]] == [{"version": 2}] * 4
    assert [frame["updated_at"] for frame in frames] == [51.0, 52.0, 53.0, 54.0, 55.0]
    assert [len(frame["log"]) for frame in frames] == [0, 0, 1, 1, 1]
    assert [frame["error"] for frame in frames] == [None, None, None, "marker error", "marker error"]
    assert registry.status(job.id) == "complete"
    assert calls == 4
    assert handler.ended is True
    assert handler.close_connection is True


def run_contract() -> None:
    temporary = Path(tempfile.mkdtemp(prefix=".test-v2-job-snapshot-", dir=ROOT))
    original_environment = {key: os.environ.get(key) for key in ("UI_TEST_MODE", "APP_TEST_ROOT", "PROXY_POOL_ENABLED", "HOT_VIDEO_REPORT_ENABLED")}
    web_app: Any | None = None
    try:
        os.environ.update({
            "UI_TEST_MODE": "1",
            "APP_TEST_ROOT": str(temporary),
            "PROXY_POOL_ENABLED": "0",
            "HOT_VIDEO_REPORT_ENABLED": "0",
        })
        sys.modules.pop("web_app", None)
        web_app = importlib.import_module("web_app")
        assert_no_download_legacy_store()
        jobs = make_jobs(web_app)
        assert_public_payloads(web_app, jobs)
        assert_get_and_sse_contracts(web_app, jobs)
        assert_sse_marker(web_app)
    finally:
        if web_app is not None:
            for lock, store in (
                (web_app.shop_jobs_lock, web_app.shop_jobs),
                (web_app.metrics_jobs_lock, web_app.metrics_jobs),
                (web_app.amazon_jobs_lock, web_app.amazon_jobs),
            ):
                with lock:
                    store.clear()
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(temporary, ignore_errors=False)
        assert not temporary.exists()


def main() -> int:
    run_contract()
    print("job snapshot contract tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
