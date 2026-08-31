#!/usr/bin/env python3
"""Observable job payload and SSE contracts before the job snapshot migration."""

from __future__ import annotations

import ast
from copy import deepcopy
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch


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


def shop_artifacts(web_app: Any, job: Any) -> tuple[Any, Any]:
    output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / job.id
    return (
        web_app.read_json(output_dir / "shop_extract.json"),
        web_app.read_json(output_dir / "shop_analysis.json"),
    )


def public_shop_payload(web_app: Any, job: Any) -> dict[str, Any]:
    extract, analysis = shop_artifacts(web_app, job)
    return web_app.public_shop_job(job, extract=extract, analysis=analysis)


def public_amazon_payload(web_app: Any, job: Any) -> dict[str, Any]:
    result = web_app.read_json(web_app.OUTPUT_DIR / "amazon" / job.id / "result.json")
    return web_app.public_amazon_job(job, result=result)


def assert_public_payloads(web_app: Any, jobs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metrics_result_path = web_app.OUTPUT_DIR / "tiktok_api" / jobs["metrics"].id / "result.json"
    payloads = {
        "download": web_app.public_download_job(jobs["download"]),
        "shop": public_shop_payload(web_app, jobs["shop"]),
        "metrics": web_app.public_metrics_job(jobs["metrics"], result=web_app.read_json(metrics_result_path)),
        "amazon": public_amazon_payload(web_app, jobs["amazon"]),
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
    refreshed_shop = public_shop_payload(web_app, jobs["shop"])
    refreshed_metrics = web_app.public_metrics_job(jobs["metrics"], result=web_app.read_json(metrics_result_path))
    refreshed_amazon = public_amazon_payload(web_app, jobs["amazon"])
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
    assert public_shop_payload(web_app, jobs["shop"])["extract"] == {"items": [{"id": "extract-v2"}]}
    assert public_shop_payload(web_app, jobs["shop"])["analysis"] == {"summary": "analysis-v2"}
    refreshed_shop["extract"]["items"][0]["id"] = "payload-extract-mutated"
    assert web_app.read_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_extract.json") == {
        "items": [{"id": "extract-v2"}]
    }
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_extract.json", {"items": [{"id": "extract-v3"}]})
    assert refreshed_shop["extract"] == {"items": [{"id": "payload-extract-mutated"}]}
    assert refreshed_shop["analysis"] == {"summary": "analysis-v2"}
    after_extract_refresh = public_shop_payload(web_app, jobs["shop"])
    assert after_extract_refresh["extract"] == {"items": [{"id": "extract-v3"}]}
    assert after_extract_refresh["analysis"] == {"summary": "analysis-v2"}
    after_extract_refresh["extract"]["items"][0]["id"] = "second-payload-extract-mutated"
    assert web_app.read_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_extract.json") == {
        "items": [{"id": "extract-v3"}]
    }
    refreshed_shop["analysis"]["summary"] = "payload-analysis-mutated"
    assert web_app.read_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_analysis.json") == {
        "summary": "analysis-v2"
    }
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_analysis.json", {"summary": "analysis-v3"})
    assert refreshed_shop["extract"] == {"items": [{"id": "payload-extract-mutated"}]}
    assert refreshed_shop["analysis"] == {"summary": "payload-analysis-mutated"}
    after_analysis_refresh = public_shop_payload(web_app, jobs["shop"])
    assert after_analysis_refresh["extract"] == {"items": [{"id": "extract-v3"}]}
    assert after_analysis_refresh["analysis"] == {"summary": "analysis-v3"}
    after_analysis_refresh["analysis"]["summary"] = "second-payload-analysis-mutated"
    assert web_app.read_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_analysis.json") == {
        "summary": "analysis-v3"
    }
    assert web_app.public_metrics_job(jobs["metrics"], result=web_app.read_json(metrics_result_path))["result"] == {
        "metric": {"views": 8}
    }
    assert public_amazon_payload(web_app, jobs["amazon"])["result"] == {"products": [{"asin": "B000FIXTUREV2"}]}
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


def assert_no_metrics_legacy_store() -> None:
    tree = ast.parse((SCRIPTS_DIR / "web_app.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {"metrics_jobs", "metrics_jobs_lock"} & names
    private_registry_access = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "metrics_job_registry"
        and node.attr in {"_jobs", "_lock"}
    ]
    assert not private_registry_access


def assert_no_shop_legacy_store() -> None:
    tree = ast.parse((SCRIPTS_DIR / "web_app.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {"shop_jobs", "shop_jobs_lock"} & names
    private_registry_access = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "shop_job_registry"
        and node.attr in {"_jobs", "_lock"}
    ]
    assert not private_registry_access


def assert_no_amazon_legacy_store() -> None:
    tree = ast.parse((SCRIPTS_DIR / "web_app.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {"amazon_jobs", "amazon_jobs_lock"} & names
    private_registry_access = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "amazon_job_registry"
        and node.attr in {"_jobs", "_lock"}
    ]
    assert not private_registry_access
    generic_streams = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "stream_events"
    ]
    assert not generic_streams
    top_level_registries = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "JobRegistry"
    ]
    all_registry_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "JobRegistry"
    ]
    assert len(all_registry_calls) == 4
    assert len(top_level_registries) == 4
    assert {
        target.id
        for node in top_level_registries
        for target in node.targets
        if isinstance(target, ast.Name)
    } == {
        "download_job_registry",
        "shop_job_registry",
        "metrics_job_registry",
        "amazon_job_registry",
    }


def assert_post_order_contracts(web_app: Any) -> None:
    def handler_for(payload: dict[str, Any]) -> FakeHandler:
        body = json.dumps(payload).encode("utf-8")
        handler = FakeHandler()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        return handler

    def deferred_thread(order: list[str]) -> type[Any]:
        class DeferredThread:
            def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self) -> None:
                order.append("thread.start")

        return DeferredThread

    download_url = "https://www.tiktok.com/@fixture/video/123"
    original_download_registry = web_app.download_job_registry
    download_registry = web_app.JobRegistry()
    web_app.download_job_registry = download_registry
    try:
        order: list[str] = []
        original_register = download_registry.register
        original_snapshot = download_registry.snapshot
        original_serializer = web_app.public_download_job

        def recording_register(job_id: str, job: Any) -> None:
            order.append("register")
            assert job.url == download_url and job.source == web_app.SOURCE_WEB_MANUAL
            return original_register(job_id, job)

        def recording_snapshot(job_id: str) -> Any:
            order.append("snapshot")
            return original_snapshot(job_id)

        def recording_serializer(job: Any) -> dict[str, Any]:
            order.append("serializer")
            return original_serializer(job)

        handler = handler_for({"url": download_url, "source": "web"})
        with patch.object(web_app.threading, "Thread", deferred_thread(order)), patch.object(
            download_registry, "register", side_effect=recording_register
        ), patch.object(download_registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "public_download_job", side_effect=recording_serializer
        ):
            web_app.Handler.handle_download(handler)
        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.responses == [202]
        assert response["url"] == download_url and response["status"] == "queued"
        assert order == ["register", "thread.start", "snapshot", "serializer"]
    finally:
        web_app.download_job_registry = original_download_registry

    shop_url = "https://shop.tiktok.com/view/product/fixture"
    original_shop_registry = web_app.shop_job_registry
    shop_registry = web_app.JobRegistry()
    web_app.shop_job_registry = shop_registry
    try:
        order = []
        original_register = shop_registry.register
        original_snapshot = shop_registry.snapshot
        original_read_json = web_app.read_json
        original_serializer = web_app.public_shop_job

        def recording_register(job_id: str, job: Any) -> None:
            order.append("register")
            assert (job.url, job.source_type, job.region, job.max_pages, job.review_pages) == (
                shop_url, "search", "JP", 2, 1,
            )
            assert job.analyze is False and job.related_videos is True and job.prompt == "private post prompt"
            return original_register(job_id, job)

        def recording_snapshot(job_id: str) -> Any:
            order.append("snapshot")
            return original_snapshot(job_id)

        def recording_read_json(path: Path) -> Any:
            order.append(path.name)
            return {"fixture": path.name}

        def recording_serializer(job: Any, *, extract: Any, analysis: Any) -> dict[str, Any]:
            order.append("serializer")
            return original_serializer(job, extract=extract, analysis=analysis)

        handler = handler_for({
            "url": shop_url,
            "source_type": "search",
            "region": "jp",
            "max_pages": 2,
            "review_pages": 1,
            "analyze": False,
            "related_videos": True,
            "prompt": "private post prompt",
        })
        with patch.object(web_app.threading, "Thread", deferred_thread(order)), patch.object(
            shop_registry, "register", side_effect=recording_register
        ), patch.object(shop_registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "read_json", side_effect=recording_read_json
        ), patch.object(web_app, "public_shop_job", side_effect=recording_serializer):
            web_app.Handler.handle_shop_extract(handler)
        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.responses == [202]
        assert response["url"] == shop_url and response["status"] == "queued"
        assert response["source_type"] == "search" and response["region"] == "JP"
        assert "prompt" not in response
        assert order == ["register", "thread.start", "snapshot", "shop_extract.json", "shop_analysis.json", "serializer"]
    finally:
        web_app.shop_job_registry = original_shop_registry

    metrics_target = "https://www.tiktok.com/@fixture/video/456"
    original_metrics_registry = web_app.metrics_job_registry
    metrics_registry = web_app.JobRegistry()
    web_app.metrics_job_registry = metrics_registry
    try:
        order = []
        original_register = metrics_registry.register
        original_snapshot = metrics_registry.snapshot
        original_serializer = web_app.public_metrics_job

        def recording_register(job_id: str, job: Any) -> None:
            order.append("register")
            assert (job.target, job.endpoint) == (metrics_target, "video-info")
            return original_register(job_id, job)

        def recording_snapshot(job_id: str) -> Any:
            order.append("snapshot")
            return original_snapshot(job_id)

        def recording_read_json(path: Path) -> Any:
            order.append(path.name)
            return {"metric": "fixture"}

        def recording_serializer(job: Any, result: Any) -> dict[str, Any]:
            order.append("serializer")
            return original_serializer(job, result)

        handler = handler_for({"target": metrics_target, "endpoint": "video-info"})
        with patch.object(web_app.threading, "Thread", deferred_thread(order)), patch.object(
            metrics_registry, "register", side_effect=recording_register
        ), patch.object(metrics_registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "read_json", side_effect=recording_read_json
        ), patch.object(web_app, "public_metrics_job", side_effect=recording_serializer):
            web_app.Handler.handle_video_metrics(handler)
        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert handler.responses == [202]
        assert response["target"] == metrics_target and response["endpoint"] == "video-info"
        assert response["status"] == "queued"
        assert order == ["register", "thread.start", "snapshot", "result.json", "serializer"]
    finally:
        web_app.metrics_job_registry = original_metrics_registry


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

    metrics = jobs["metrics"]
    original_metrics_registry = web_app.metrics_job_registry
    metrics_registry = web_app.JobRegistry()
    web_app.metrics_job_registry = metrics_registry
    try:
        metrics_registry.register(metrics.id, metrics)
        result_path = web_app.OUTPUT_DIR / "tiktok_api" / metrics.id / "result.json"
        expected = web_app.public_metrics_job(metrics_registry.snapshot(metrics.id), result=web_app.read_json(result_path))
        assert_json_response(dispatch_get(web_app, f"/api/video-metrics-job?id={metrics.id}"), 200, expected)
        assert_sse_response(dispatch_get(web_app, f"/api/video-metrics-events?id={metrics.id}"), expected)
        assert_json_response(
            dispatch_get(web_app, "/api/video-metrics-job?id=missing"),
            404,
            {"error": "Video metrics job not found"},
        )
        assert_sse_response(
            dispatch_get(web_app, "/api/video-metrics-events?id=missing"),
            {"status": "missing", "error": "Video metrics job not found"},
        )

        call_order: list[str] = []
        original_snapshot = metrics_registry.snapshot
        original_read_json = web_app.read_json

        def recording_snapshot(job_id: str) -> Any:
            call_order.append("snapshot")
            return original_snapshot(job_id)

        def recording_read_json(path: Path) -> Any:
            call_order.append("read")
            return original_read_json(path)

        with patch.object(metrics_registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "read_json", side_effect=recording_read_json
        ):
            assert_json_response(dispatch_get(web_app, f"/api/video-metrics-job?id={metrics.id}"), 200, expected)
        assert call_order == ["snapshot", "read"]
    finally:
        web_app.metrics_job_registry = original_metrics_registry

    shop = jobs["shop"]
    original_shop_registry = web_app.shop_job_registry
    shop_registry = web_app.JobRegistry()
    web_app.shop_job_registry = shop_registry
    try:
        shop_registry.register(shop.id, shop)
        snapshot = shop_registry.snapshot(shop.id)
        assert snapshot is not None
        expected = public_shop_payload(web_app, snapshot)
        assert "prompt" not in expected
        assert "private fixture prompt" not in json_body(expected).decode("utf-8")
        assert_json_response(dispatch_get(web_app, f"/api/shop-job?id={shop.id}"), 200, expected)
        assert_sse_response(dispatch_get(web_app, f"/api/shop-events?id={shop.id}"), expected)
        assert "private fixture prompt" not in dispatch_get(web_app, f"/api/shop-events?id={shop.id}").wfile.getvalue().decode("utf-8")
        assert_json_response(
            dispatch_get(web_app, "/api/shop-job?id=missing"), 404, {"error": "TikTok Shop job not found"}
        )
        assert_sse_response(
            dispatch_get(web_app, "/api/shop-events?id=missing"),
            {"status": "missing", "error": "TikTok Shop job not found"},
        )

        call_order: list[str] = []
        original_snapshot = shop_registry.snapshot
        original_read_json = web_app.read_json

        def recording_snapshot(job_id: str) -> Any:
            call_order.append("snapshot")
            return original_snapshot(job_id)

        def recording_read_json(path: Path) -> Any:
            call_order.append(path.name)
            return original_read_json(path)

        with patch.object(shop_registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "read_json", side_effect=recording_read_json
        ):
            assert_json_response(dispatch_get(web_app, f"/api/shop-job?id={shop.id}"), 200, expected)
        assert call_order == ["snapshot", "shop_extract.json", "shop_analysis.json"]
    finally:
        web_app.shop_job_registry = original_shop_registry

    amazon = jobs["amazon"]
    original_amazon_registry = web_app.amazon_job_registry
    amazon_registry = web_app.JobRegistry()
    web_app.amazon_job_registry = amazon_registry
    try:
        amazon_registry.register(amazon.id, amazon)
        snapshot = amazon_registry.snapshot(amazon.id)
        assert snapshot is not None
        expected = public_amazon_payload(web_app, snapshot)
        assert_json_response(dispatch_get(web_app, f"/api/amazon-job?id={amazon.id}"), 200, expected)
        assert_sse_response(dispatch_get(web_app, f"/api/amazon-events?id={amazon.id}"), expected)
        assert_json_response(
            dispatch_get(web_app, "/api/amazon-job?id=missing"), 404, {"error": "Amazon job not found"}
        )
        assert_sse_response(
            dispatch_get(web_app, "/api/amazon-events?id=missing"),
            {"status": "missing", "error": "Amazon job not found"},
        )

        call_order: list[str] = []
        original_snapshot = amazon_registry.snapshot
        original_read_json = web_app.read_json

        def recording_snapshot(job_id: str) -> Any:
            call_order.append("snapshot")
            return original_snapshot(job_id)

        def recording_read_json(path: Path) -> Any:
            call_order.append(path.name)
            return original_read_json(path)

        with patch.object(amazon_registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "read_json", side_effect=recording_read_json
        ):
            assert_json_response(dispatch_get(web_app, f"/api/amazon-job?id={amazon.id}"), 200, expected)
        assert call_order == ["snapshot", "result.json"]

        class DeferredThread:
            def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self) -> None:
                post_order.append("thread.start")
                return None

        post_body = json.dumps({"target": "B000POST01", "target_type": "asin", "pages": 3}).encode("utf-8")
        post_handler = FakeHandler()
        post_handler.headers = {"Content-Length": str(len(post_body))}
        post_handler.rfile = io.BytesIO(post_body)
        post_order: list[str] = []

        def recording_post_snapshot(job_id: str) -> Any:
            post_order.append("snapshot")
            return original_snapshot(job_id)

        def recording_post_register(job_id: str, job: Any) -> None:
            post_order.append("register")
            assert (job.target, job.target_type, job.url, job.pages) == (
                "B000POST01", "asin", "https://www.amazon.com/dp/B000POST01", 3,
            )
            return original_register(job_id, job)

        def recording_post_read_json(path: Path) -> Any:
            post_order.append(path.name)
            return original_read_json(path)

        def recording_post_serializer(job: Any, *, result: Any) -> dict[str, Any]:
            post_order.append("serializer")
            return original_serializer(job, result=result)

        original_register = amazon_registry.register
        original_serializer = web_app.public_amazon_job
        with patch.object(web_app.threading, "Thread", DeferredThread), patch.object(
            amazon_registry, "register", side_effect=recording_post_register
        ), patch.object(
            amazon_registry, "snapshot", side_effect=recording_post_snapshot
        ), patch.object(web_app, "read_json", side_effect=recording_post_read_json), patch.object(
            web_app, "public_amazon_job", side_effect=recording_post_serializer
        ):
            web_app.Handler.handle_amazon_scrape(post_handler)
        response = json.loads(post_handler.wfile.getvalue().decode("utf-8"))
        assert post_handler.responses == [202]
        assert (response["target"], response["target_type"], response["url"], response["pages"], response["status"]) == (
            "B000POST01", "asin", "https://www.amazon.com/dp/B000POST01", 3, "queued",
        )
        assert post_order == ["register", "thread.start", "snapshot", "result.json", "serializer"]
    finally:
        web_app.amazon_job_registry = original_amazon_registry


def assert_shop_artifact_failure_contract(web_app: Any) -> None:
    original_registry = web_app.shop_job_registry
    registry = web_app.JobRegistry()
    web_app.shop_job_registry = registry
    try:
        def shop_job(job_id: str) -> Any:
            return web_app.ShopJob(
                id=job_id,
                url="https://shop.tiktok.com/view/product/artifact-fixture",
                source_type="product",
                region="US",
                max_pages=1,
                review_pages=1,
                analyze=True,
                status="complete",
                created_at=90.0,
                updated_at=91.0,
                output_dir=f"output/tiktok_shop/{job_id}",
            )

        def paths_for(job_id: str) -> tuple[Path, Path]:
            output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / job_id
            return output_dir / "shop_extract.json", output_dir / "shop_analysis.json"

        def handler_for_get(job_id: str) -> FakeHandler:
            handler = FakeHandler(f"/api/shop-job?id={job_id}")
            handler.stream_shop_events = web_app.Handler.stream_shop_events.__get__(handler, FakeHandler)
            return handler

        for missing_name in ("shop_extract.json", "shop_analysis.json"):
            job_id = f"shop-missing-{missing_name.removesuffix('.json')}"
            registry.register(job_id, shop_job(job_id))
            extract_path, analysis_path = paths_for(job_id)
            write_json(extract_path, {"items": ["extract"]})
            write_json(analysis_path, {"summary": "analysis"})
            (extract_path if missing_name == "shop_extract.json" else analysis_path).unlink()
            snapshot = registry.snapshot(job_id)
            assert snapshot is not None
            expected = public_shop_payload(web_app, snapshot)
            assert expected["extract" if missing_name == "shop_extract.json" else "analysis"] is None
            get_handler = handler_for_get(job_id)
            web_app.Handler.do_GET(get_handler)
            assert_json_response(get_handler, 200, expected)
            sse_handler = FakeHandler()
            web_app.Handler.stream_shop_events(sse_handler, job_id)
            assert_sse_response(sse_handler, expected)

        for invalid_name in ("shop_extract.json", "shop_analysis.json"):
            job_id = f"shop-invalid-{invalid_name.removesuffix('.json')}"
            registry.register(job_id, shop_job(job_id))
            extract_path, analysis_path = paths_for(job_id)
            write_json(extract_path, {"items": ["extract"]})
            write_json(analysis_path, {"summary": "analysis"})
            (extract_path if invalid_name == "shop_extract.json" else analysis_path).write_text("{not json", encoding="utf-8")

            get_handler = handler_for_get(job_id)
            try:
                web_app.Handler.do_GET(get_handler)
            except json.JSONDecodeError:
                pass
            else:
                raise AssertionError(f"GET did not raise for invalid {invalid_name}")
            assert get_handler.responses == []
            assert get_handler.wfile.getvalue() == b""
            assert get_handler.ended is False
            assert get_handler.close_connection is False

            sse_handler = FakeHandler()
            try:
                web_app.Handler.stream_shop_events(sse_handler, job_id)
            except json.JSONDecodeError:
                pass
            else:
                raise AssertionError(f"SSE did not raise for invalid {invalid_name}")
            assert sse_handler.responses == [200]
            assert sse_handler.header("Content-Type") == "text/event-stream; charset=utf-8"
            assert sse_handler.header("Cache-Control") == "no-cache"
            assert sse_handler.header("Connection") == "keep-alive"
            assert sse_handler.wfile.getvalue() == b""
            assert sse_handler.wfile.flush_count == 0
            assert sse_handler.ended is True
            assert sse_handler.close_connection is False
    finally:
        web_app.shop_job_registry = original_registry


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


def assert_metrics_sse_marker(web_app: Any) -> None:
    job = web_app.MetricsJob(
        id="metrics-marker-fixture",
        target="@fixture",
        endpoint="profile",
        status="queued",
        created_at=60.0,
        updated_at=61.0,
        output_dir="output/tiktok_api/metrics-marker-fixture",
    )
    result_path = web_app.OUTPUT_DIR / "tiktok_api" / job.id / "result.json"
    write_json(result_path, {"metric": {"views": 1}})
    original_registry = web_app.metrics_job_registry
    timestamps = iter((62.0, 63.0, 64.0))
    registry = web_app.JobRegistry(clock=lambda: next(timestamps))
    registry.register(job.id, job)
    web_app.metrics_job_registry = registry
    handler = FakeHandler()
    original_sleep = web_app.time.sleep
    original_snapshot = registry.snapshot
    original_read_json = web_app.read_json
    read_order: list[str] = []
    calls = 0

    def advance(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            write_json(result_path, {"metric": {"views": 2}})
        elif calls == 2:
            registry.append_log(job.id, "metrics marker log")
        elif calls == 3:
            registry.update_fields(job.id, {"error": "metrics marker error"})
        else:
            registry.update_fields(job.id, {"status": "complete"})

    def recording_snapshot(job_id: str) -> Any:
        read_order.append("snapshot")
        return original_snapshot(job_id)

    def recording_read_json(path: Path) -> Any:
        read_order.append("read")
        return original_read_json(path)

    try:
        web_app.time.sleep = advance
        with patch.object(
            registry,
            "snapshot",
            side_effect=recording_snapshot,
        ), patch.object(
            web_app,
            "read_json",
            side_effect=recording_read_json,
        ):
            web_app.Handler.stream_metrics_events(handler, job.id)
    finally:
        web_app.time.sleep = original_sleep
        web_app.metrics_job_registry = original_registry
    frames = [json.loads(line[6:]) for line in handler.wfile.getvalue().decode("utf-8").splitlines() if line.startswith("data: ")]
    assert [frame["status"] for frame in frames] == ["queued", "queued", "queued", "complete"]
    assert [frame["result"] for frame in frames] == [
        {"metric": {"views": 1}},
        {"metric": {"views": 2}},
        {"metric": {"views": 2}},
        {"metric": {"views": 2}},
    ]
    assert [frame["updated_at"] for frame in frames] == [61.0, 62.0, 63.0, 64.0]
    assert [len(frame["log"]) for frame in frames] == [0, 1, 1, 1]
    assert [frame["error"] for frame in frames] == [None, None, "metrics marker error", "metrics marker error"]
    assert registry.status(job.id) == "complete"
    assert calls == 4
    assert read_order == ["snapshot", "read"] * 5
    assert handler.ended is True
    assert handler.close_connection is True


def assert_shop_sse_marker(web_app: Any) -> None:
    job = web_app.ShopJob(
        id="shop-marker-fixture",
        url="https://shop.tiktok.com/view/product/marker",
        source_type="product",
        region="US",
        max_pages=1,
        review_pages=0,
        analyze=True,
        related_videos=False,
        prompt="private marker prompt",
        status="queued",
        created_at=70.0,
        updated_at=71.0,
        output_dir="output/tiktok_shop/shop-marker-fixture",
    )
    output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / job.id
    extract_path = output_dir / "shop_extract.json"
    analysis_path = output_dir / "shop_analysis.json"
    write_json(extract_path, {"items": [{"id": "extract-v1"}]})
    write_json(analysis_path, {"summary": {"id": "analysis-v1"}})
    original_registry = web_app.shop_job_registry
    timestamps = iter((72.0, 73.0))
    registry = web_app.JobRegistry(clock=lambda: next(timestamps))
    registry.register(job.id, job)
    web_app.shop_job_registry = registry
    handler = FakeHandler()
    original_sleep = web_app.time.sleep
    original_snapshot = registry.snapshot
    original_read_json = web_app.read_json
    read_order: list[str] = []
    calls = 0

    def advance(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            write_json(extract_path, {"items": [{"id": "extract-v2"}]})
        elif calls == 2:
            write_json(analysis_path, {"summary": {"id": "analysis-v2"}})
        elif calls == 3:
            registry.append_log(job.id, "shop marker log")
        else:
            registry.update_fields(job.id, {"status": "complete"})

    def recording_snapshot(job_id: str) -> Any:
        read_order.append("snapshot")
        return original_snapshot(job_id)

    def recording_read_json(path: Path) -> Any:
        read_order.append(path.name)
        return original_read_json(path)

    try:
        web_app.time.sleep = advance
        with patch.object(registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "read_json", side_effect=recording_read_json
        ):
            web_app.Handler.stream_shop_events(handler, job.id)
    finally:
        web_app.time.sleep = original_sleep
        web_app.shop_job_registry = original_registry
    frames = [json.loads(line[6:]) for line in handler.wfile.getvalue().decode("utf-8").splitlines() if line.startswith("data: ")]
    assert [frame["status"] for frame in frames] == ["queued", "queued", "complete"]
    assert [frame["extract"] for frame in frames] == [
        {"items": [{"id": "extract-v1"}]},
        {"items": [{"id": "extract-v2"}]},
        {"items": [{"id": "extract-v2"}]},
    ]
    assert [frame["analysis"] for frame in frames] == [
        {"summary": {"id": "analysis-v1"}},
        {"summary": {"id": "analysis-v2"}},
        {"summary": {"id": "analysis-v2"}},
    ]
    assert [len(frame["log"]) for frame in frames] == [0, 1, 1]
    assert [frame["updated_at"] for frame in frames] == [71.0, 72.0, 73.0]
    assert all("prompt" not in frame for frame in frames)
    assert all("private marker prompt" not in json.dumps(frame, ensure_ascii=False) for frame in frames)
    assert registry.status(job.id) == "complete"
    assert calls == 4
    assert read_order == ["snapshot", "shop_extract.json", "shop_analysis.json"] * 5
    assert handler.ended is True
    assert handler.close_connection is True


def assert_amazon_sse_marker(web_app: Any) -> None:
    job = web_app.AmazonJob(
        id="amazon-marker-fixture",
        target="B000MARKER",
        target_type="asin",
        url="https://www.amazon.com/dp/B000MARKER",
        pages=2,
        status="queued",
        created_at=80.0,
        updated_at=81.0,
        output_dir="output/amazon/amazon-marker-fixture",
    )
    result_path = web_app.OUTPUT_DIR / "amazon" / job.id / "result.json"
    write_json(result_path, {"products": [{"asin": "B000MARKER", "title": "v1"}]})
    original_registry = web_app.amazon_job_registry
    registry = web_app.JobRegistry()
    registry.register(job.id, job)
    web_app.amazon_job_registry = registry
    handler = FakeHandler()
    original_sleep = web_app.time.sleep
    original_read_json = web_app.read_json
    read_order: list[str] = []
    calls = 0

    artifact_only = deepcopy(job)
    error_only = deepcopy(job)
    error_only.error = "amazon marker error"
    log_changed = deepcopy(error_only)
    log_changed.log.append("amazon marker log")
    log_changed.updated_at = 82.0
    completed = deepcopy(log_changed)
    completed.status = "complete"
    completed.updated_at = 83.0
    snapshots = (job, artifact_only, error_only, log_changed, completed)
    snapshot_index = 0

    def advance(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            write_json(result_path, {"products": [{"asin": "B000MARKER", "title": "v2"}]})

    def recording_snapshot(job_id: str) -> Any:
        nonlocal snapshot_index
        read_order.append("snapshot")
        assert job_id == job.id
        snapshot = snapshots[snapshot_index]
        snapshot_index += 1
        return deepcopy(snapshot)

    def recording_read_json(path: Path) -> Any:
        read_order.append(path.name)
        return original_read_json(path)

    try:
        web_app.time.sleep = advance
        with patch.object(registry, "snapshot", side_effect=recording_snapshot), patch.object(
            web_app, "read_json", side_effect=recording_read_json
        ):
            web_app.Handler.stream_amazon_events(handler, job.id)
    finally:
        web_app.time.sleep = original_sleep
        web_app.amazon_job_registry = original_registry
    frames = [json.loads(line[6:]) for line in handler.wfile.getvalue().decode("utf-8").splitlines() if line.startswith("data: ")]
    assert [frame["status"] for frame in frames] == ["queued", "queued", "queued", "complete"]
    assert [frame["result"] for frame in frames] == [
        {"products": [{"asin": "B000MARKER", "title": "v1"}]},
        {"products": [{"asin": "B000MARKER", "title": "v2"}]},
        {"products": [{"asin": "B000MARKER", "title": "v2"}]},
        {"products": [{"asin": "B000MARKER", "title": "v2"}]},
    ]
    assert [len(frame["log"]) for frame in frames] == [0, 0, 1, 1]
    assert [frame["error"] for frame in frames] == [None, "amazon marker error", "amazon marker error", "amazon marker error"]
    assert [frame["updated_at"] for frame in frames] == [81.0, 81.0, 82.0, 83.0]
    assert calls == 4
    assert read_order == ["snapshot", "result.json"] * 5
    assert handler.ended is True
    assert handler.close_connection is True

    class BrokenPipeWriter:
        def write(self, _data: bytes) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            return None

    missing_handler = FakeHandler()
    missing_handler.wfile = BrokenPipeWriter()
    web_app.amazon_job_registry = type("MissingRegistry", (), {"snapshot": lambda _self, _job_id: None})()
    try:
        web_app.Handler.stream_amazon_events(missing_handler, "missing-amazon-job")
    finally:
        web_app.amazon_job_registry = original_registry
    assert missing_handler.responses == [200]
    assert missing_handler.ended is True
    assert missing_handler.close_connection is True


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
        assert_no_metrics_legacy_store()
        assert_no_shop_legacy_store()
        assert_no_amazon_legacy_store()
        jobs = make_jobs(web_app)
        assert_public_payloads(web_app, jobs)
        assert_post_order_contracts(web_app)
        assert_get_and_sse_contracts(web_app, jobs)
        assert_shop_artifact_failure_contract(web_app)
        assert_sse_marker(web_app)
        assert_metrics_sse_marker(web_app)
        assert_shop_sse_marker(web_app)
        assert_amazon_sse_marker(web_app)
    finally:
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
