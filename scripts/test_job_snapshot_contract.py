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

from jobs.registry import JobRegistry
from routes.amazon import register_amazon_routes
from routes.metrics import register_metrics_api_routes
from routes.router import Router
from routes.shop import register_shop_api_routes
from services import metrics as metrics_module
from services import shop as shop_module
from services.amazon import AmazonJob, AmazonService
from services.metrics import MetricsJob, MetricsService
from services.shop import ShopJob, ShopService


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
    for name in ("stream_download_events",):
        setattr(handler, name, getattr(web_app.Handler, name).__get__(handler, FakeHandler))
    web_app.Handler.do_GET(handler)
    return handler


def make_shop_service(
    web_app: Any,
    registry: JobRegistry,
    *,
    read_json_file: Any | None = None,
    thread_factory: Any | None = None,
    job_id_factory: Any | None = None,
) -> ShopService:
    return ShopService(
        registry=registry,
        root=web_app.ROOT,
        output_dir=web_app.OUTPUT_DIR,
        scripts_dir=web_app.SCRIPTS_DIR,
        read_json_file=read_json_file or web_app.read_json,
        popen_factory=lambda *_args, **_kwargs: None,
        thread_factory=thread_factory or (lambda **_kwargs: None),
        job_id_factory=job_id_factory or (lambda: "shop-post-fixture"),
    )


def make_metrics_service(
    web_app: Any,
    registry: JobRegistry,
    *,
    read_json_file: Any | None = None,
    popen_factory: Any | None = None,
    thread_factory: Any | None = None,
    job_id_factory: Any | None = None,
    register_from_payload: Any | None = None,
) -> MetricsService:
    return MetricsService(
        registry=registry,
        root=web_app.ROOT,
        output_dir=web_app.OUTPUT_DIR,
        scripts_dir=web_app.SCRIPTS_DIR,
        read_json_file=read_json_file or web_app.read_json,
        popen_factory=popen_factory or (lambda *_args, **_kwargs: None),
        thread_factory=thread_factory or (lambda **_kwargs: None),
        job_id_factory=job_id_factory or (lambda: "metrics-post-fixture"),
        register_from_payload=register_from_payload or (lambda *_args, **_kwargs: None),
    )


def make_amazon_service(
    web_app: Any,
    registry: JobRegistry,
    *,
    read_json_file: Any | None = None,
    write_json_file: Any | None = None,
    popen_factory: Any | None = None,
    thread_factory: Any | None = None,
    job_id_factory: Any | None = None,
    environ: Any | None = None,
    ensure_us_proxy: Any | None = None,
    get_cached_or_call: Any | None = None,
    cache_log_label: Any | None = None,
) -> AmazonService:
    return AmazonService(
        registry=registry,
        root=web_app.ROOT,
        output_dir=web_app.OUTPUT_DIR,
        read_json_file=read_json_file or web_app.read_json,
        write_json_file=write_json_file or web_app.atomic_write_json,
        popen_factory=popen_factory or (lambda *_args, **_kwargs: None),
        thread_factory=thread_factory or (lambda **_kwargs: None),
        job_id_factory=job_id_factory or (lambda: "amazon-post-fixture"),
        environ=environ if environ is not None else os.environ,
        ensure_us_proxy=ensure_us_proxy or (lambda *_args, **_kwargs: None),
        get_cached_or_call=get_cached_or_call or (lambda _provider, _scope, _request, fetch, **_kwargs: fetch()),
        cache_log_label=cache_log_label or (lambda _payload: None),
    )


def make_shop_router(service: ShopService, *, sleep: Any = lambda _seconds: None) -> Router:
    router = Router()
    register_shop_api_routes(router, service, sleep=sleep)
    return router


def make_metrics_router(service: MetricsService, *, sleep: Any = lambda _seconds: None) -> Router:
    router = Router()
    register_metrics_api_routes(router, service, sleep=sleep)
    return router


def make_amazon_router(
    service: AmazonService,
    *,
    getenv: Any = lambda _name, default: default,
    sleep: Any = lambda _seconds: None,
) -> Router:
    router = Router()
    register_amazon_routes(router, service, getenv=getenv, sleep=sleep)
    return router


def dispatch_shop(router: Router, method: str, handler: FakeHandler) -> FakeHandler:
    route = router.resolve(method, handler.path.partition("?")[0])
    route.handler(handler, route.params)
    return handler


def dispatch_metrics(router: Router, method: str, handler: FakeHandler) -> FakeHandler:
    route = router.resolve(method, handler.path.partition("?")[0])
    route.handler(handler, route.params)
    return handler


def dispatch_amazon(router: Router, method: str, handler: FakeHandler) -> FakeHandler:
    route = router.resolve(method, handler.path.partition("?")[0])
    route.handler(handler, route.params)
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


def assert_event_headers(handler: FakeHandler) -> None:
    assert handler.responses == [200]
    assert handler.header("Content-Type") == "text/event-stream; charset=utf-8"
    assert handler.header("Cache-Control") == "no-cache"
    assert handler.header("Connection") == "keep-alive"
    assert handler.ended is True


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
    shop = ShopJob(
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
    metrics = MetricsJob(
        id="metrics-fixture",
        target="@fixture",
        endpoint="video-info",
        status="complete",
        created_at=30.0,
        updated_at=31.0,
        log=[f"metrics-{index}" for index in range(122)],
        output_dir="output/tiktok_api/metrics-fixture",
    )
    amazon = AmazonJob(
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
    shop_registry = JobRegistry()
    shop_registry.register(shop.id, shop)
    metrics_registry = JobRegistry()
    metrics_registry.register(metrics.id, metrics)
    amazon_registry = JobRegistry()
    amazon_registry.register(amazon.id, amazon)
    return {
        "download": download,
        "shop": shop,
        "shop_registry": shop_registry,
        "shop_service": make_shop_service(web_app, shop_registry),
        "metrics": metrics,
        "metrics_registry": metrics_registry,
        "metrics_service": make_metrics_service(web_app, metrics_registry),
        "amazon": amazon,
        "amazon_registry": amazon_registry,
        "amazon_service": make_amazon_service(web_app, amazon_registry),
    }


def public_shop_payload(service: ShopService, job: ShopJob) -> dict[str, Any]:
    payload = service.payload_for(job.id)
    assert payload is not None
    return payload


def public_amazon_payload(service: AmazonService, job: AmazonJob) -> dict[str, Any]:
    payload = service.payload_for(job.id)
    assert payload is not None
    return payload


def assert_public_payloads(web_app: Any, jobs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metrics_service = jobs["metrics_service"]
    payloads = {
        "download": web_app.public_download_job(jobs["download"]),
        "shop": public_shop_payload(jobs["shop_service"], jobs["shop"]),
        "metrics": metrics_service.payload_for(jobs["metrics"].id),
        "amazon": public_amazon_payload(jobs["amazon_service"], jobs["amazon"]),
    }
    assert payloads["metrics"] is not None
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
    for name in ("download", "shop", "metrics", "amazon"):
        job = jobs[name]
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

    direct_metrics_result = {"metric": {"nested": ["fixture"]}}
    direct_metrics_registry = JobRegistry()
    direct_metrics_job = MetricsJob(id="metrics-direct-copy", target="@fixture", endpoint="profile")
    direct_metrics_registry.register(direct_metrics_job.id, direct_metrics_job)
    direct_metrics_service = make_metrics_service(
        web_app,
        direct_metrics_registry,
        read_json_file=lambda _path: direct_metrics_result,
    )
    direct_metrics_payload = direct_metrics_service.payload_for(direct_metrics_job.id)
    assert direct_metrics_payload is not None
    assert direct_metrics_payload["result"] == direct_metrics_result
    assert direct_metrics_payload["result"] is not direct_metrics_result
    assert direct_metrics_payload["result"]["metric"] is not direct_metrics_result["metric"]
    direct_metrics_payload["result"]["metric"]["nested"].append("payload-mutated")
    assert direct_metrics_result == {"metric": {"nested": ["fixture"]}}
    direct_metrics_result["metric"]["nested"].append("source-mutated")
    assert direct_metrics_payload["result"] == {"metric": {"nested": ["fixture", "payload-mutated"]}}

    original_shop_extract = payloads["shop"]["extract"]
    original_shop_analysis = payloads["shop"]["analysis"]
    original_metrics_result = payloads["metrics"]["result"]
    original_amazon_result = payloads["amazon"]["result"]
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_extract.json", {"items": [{"id": "extract-v2"}]})
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_analysis.json", {"summary": "analysis-v2"})
    write_json(web_app.OUTPUT_DIR / "tiktok_api" / jobs["metrics"].id / "result.json", {"metric": {"views": 8}})
    write_json(web_app.OUTPUT_DIR / "amazon" / jobs["amazon"].id / "result.json", {"products": [{"asin": "B000FIXTUREV2"}]})
    refreshed_shop = public_shop_payload(jobs["shop_service"], jobs["shop"])
    refreshed_metrics = metrics_service.payload_for(jobs["metrics"].id)
    refreshed_amazon = public_amazon_payload(jobs["amazon_service"], jobs["amazon"])
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
    assert public_shop_payload(jobs["shop_service"], jobs["shop"])["extract"] == {"items": [{"id": "extract-v2"}]}
    assert public_shop_payload(jobs["shop_service"], jobs["shop"])["analysis"] == {"summary": "analysis-v2"}
    refreshed_shop["extract"]["items"][0]["id"] = "payload-extract-mutated"
    assert web_app.read_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_extract.json") == {
        "items": [{"id": "extract-v2"}]
    }
    write_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_extract.json", {"items": [{"id": "extract-v3"}]})
    assert refreshed_shop["extract"] == {"items": [{"id": "payload-extract-mutated"}]}
    assert refreshed_shop["analysis"] == {"summary": "analysis-v2"}
    after_extract_refresh = public_shop_payload(jobs["shop_service"], jobs["shop"])
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
    after_analysis_refresh = public_shop_payload(jobs["shop_service"], jobs["shop"])
    assert after_analysis_refresh["extract"] == {"items": [{"id": "extract-v3"}]}
    assert after_analysis_refresh["analysis"] == {"summary": "analysis-v3"}
    after_analysis_refresh["analysis"]["summary"] = "second-payload-analysis-mutated"
    assert web_app.read_json(web_app.OUTPUT_DIR / "tiktok_shop" / jobs["shop"].id / "shop_analysis.json") == {
        "summary": "analysis-v3"
    }
    final_metrics_payload = metrics_service.payload_for(jobs["metrics"].id)
    assert final_metrics_payload is not None
    assert final_metrics_payload["result"] == {
        "metric": {"views": 8}
    }
    assert public_amazon_payload(jobs["amazon_service"], jobs["amazon"])["result"] == {"products": [{"asin": "B000FIXTUREV2"}]}
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
    # Phase 4.4A freezes the pre-extraction Download ownership before the
    # service/route migration deliberately flips these assertions.
    definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert {
        "DownloadJob",
        "append_download_log",
        "run_download_command",
        "run_download_job",
        "public_download_job",
    } <= definitions
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
    handler_methods = {
        node.name
        for node in handler.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"handle_download", "stream_download_events"} <= handler_methods


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
    handler = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Handler"
    )
    handler_methods = {
        node.name: node
        for node in handler.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    obsolete_definitions = {
        "MetricsJob", "append_metrics_log", "run_metrics_command", "run_metrics_job", "public_metrics_job",
    }
    assert not {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in obsolete_definitions
    }
    assert not {"stream_metrics_events", "handle_video_metrics"} & handler_methods.keys()

    metrics_tree = ast.parse((SCRIPTS_DIR / "services" / "metrics.py").read_text(encoding="utf-8"))
    route_tree = ast.parse((SCRIPTS_DIR / "routes" / "metrics.py").read_text(encoding="utf-8"))
    for module in (metrics_tree, route_tree):
        assert not [
            node for node in ast.walk(module)
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "web_app" or node.module.startswith("web_app."))
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name == "web_app" or alias.name.startswith("web_app.")
                    for alias in node.names
                )
            )
        ]
        assert not [
            node for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr == "getenv"
        ]
    assert not [
        node for node in ast.walk(metrics_tree)
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "routes" or node.module.startswith("routes."))
        )
        or (
            isinstance(node, ast.Import)
            and any(
                alias.name == "routes" or alias.name.startswith("routes.")
                for alias in node.names
            )
        )
    ]


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
    obsolete_definitions = {
        "ShopJob", "append_shop_log", "run_shop_command", "run_shop_job", "public_shop_job",
    }
    assert not {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in obsolete_definitions
    }
    handler = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "Handler")
    assert not {
        node.name for node in handler.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"handle_shop_extract", "stream_shop_events"}
    }
    assert not {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value in {
            "shop_extract.json", "shop_analysis.json",
            "/api/shop-extract", "/api/shop-job", "/api/shop-events",
        }
    }
    for source_path, forbidden in (
        (SCRIPTS_DIR / "routes" / "shop.py", {"web_app"}),
        (SCRIPTS_DIR / "services" / "shop.py", {"web_app", "routes"}),
    ):
        source_tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(source_tree)
            for alias in (node.names if isinstance(node, ast.Import) else [])
        } | {
            str(node.module or "").split(".", 1)[0]
            for node in ast.walk(source_tree) if isinstance(node, ast.ImportFrom)
        }
        assert not imported & forbidden


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
    top_level_definitions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert not {
        "AmazonJob",
        "append_amazon_log",
        "amazon_url_for_target",
        "parse_json_from_process_output",
        "run_amazon_command",
        "run_amazon_job",
        "public_amazon_job",
        "validate_amazon_url",
    } & top_level_definitions
    top_level_assignments = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    assert not {"ALLOWED_AMAZON_HOST_SUFFIXES", "ASIN_RE"} & top_level_assignments
    amazon_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "services.amazon"
    ]
    assert len(amazon_imports) == 1
    assert [(alias.name, alias.asname) for alias in amazon_imports[0].names] == [("AmazonService", None)]
    amazon_service_assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "amazon_service" for target in node.targets)
    )
    assert isinstance(amazon_service_assignment.value, ast.Call)
    assert isinstance(amazon_service_assignment.value.func, ast.Name)
    assert amazon_service_assignment.value.func.id == "AmazonService"
    amazon_service_keywords = {
        keyword.arg: keyword.value for keyword in amazon_service_assignment.value.keywords
    }
    assert set(amazon_service_keywords) == {
        "registry",
        "root",
        "output_dir",
        "read_json_file",
        "write_json_file",
        "popen_factory",
        "thread_factory",
        "job_id_factory",
        "environ",
        "ensure_us_proxy",
        "get_cached_or_call",
        "cache_log_label",
    }
    for keyword, expected_name in {
        "registry": "amazon_job_registry",
        "root": "ROOT",
        "output_dir": "OUTPUT_DIR",
        "read_json_file": "read_json",
        "write_json_file": "atomic_write_json",
        "thread_factory": "threading.Thread",
        "popen_factory": "subprocess.Popen",
        "environ": "os.environ",
        "ensure_us_proxy": "ensure_us_proxy",
        "get_cached_or_call": "get_cached_or_call",
    }.items():
        assert ast.unparse(amazon_service_keywords[keyword]) == expected_name
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
    handler_methods = {
        node.name
        for node in handler.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not {"stream_amazon_events", "handle_amazon_scrape"} & handler_methods
    get_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_GET")
    post_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
    get_constants = {
        node.value
        for node in ast.walk(get_method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not {"/api/amazon-job", "/api/amazon-events"} & get_constants
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == "/api/amazon-scrape"
        for node in ast.walk(post_method)
    )
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
    service_tree = ast.parse((SCRIPTS_DIR / "services" / "amazon.py").read_text(encoding="utf-8"))
    route_tree = ast.parse((SCRIPTS_DIR / "routes" / "amazon.py").read_text(encoding="utf-8"))
    service_definitions = {
        node.name for node in service_tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    assert {"AmazonJob", "AmazonService", "validate_amazon_url", "amazon_url_for_target", "parse_json_from_process_output"} <= service_definitions
    route_definitions = {
        node.name for node in route_tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    assert "register_amazon_routes" in route_definitions
    for source_tree, forbidden in ((service_tree, {"web_app", "routes"}), (route_tree, {"web_app"})):
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(source_tree)
            for alias in (node.names if isinstance(node, ast.Import) else [])
        } | {
            str(node.module or "").split(".", 1)[0]
            for node in ast.walk(source_tree) if isinstance(node, ast.ImportFrom)
        }
        assert not imported & forbidden


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

        for payload, expected_source in (
            ({"url": download_url, "source_tag": "manual"}, web_app.SOURCE_WEB_MANUAL),
            ({"url": download_url, "source": "api_url"}, web_app.SOURCE_API_UPLOAD),
            ({"url": download_url}, web_app.SOURCE_API_UPLOAD),
            ({"url": download_url, "source_tag": "web", "source": "api"}, web_app.SOURCE_WEB_MANUAL),
        ):
            alias_handler = handler_for(payload)
            with patch.object(web_app.threading, "Thread", deferred_thread([])):
                web_app.Handler.handle_download(alias_handler)
            alias_payload = json.loads(alias_handler.wfile.getvalue().decode("utf-8"))
            alias_job = download_registry.snapshot(alias_payload["id"])
            assert alias_job is not None and alias_job.source == expected_source
    finally:
        web_app.download_job_registry = original_download_registry

    shop_url = "https://shop.tiktok.com/view/product/fixture"
    order: list[str] = []
    shop_registry = JobRegistry()
    original_register = shop_registry.register
    original_snapshot = shop_registry.snapshot
    original_serializer = shop_module.snapshot_shop_job

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

    service = make_shop_service(
        web_app,
        shop_registry,
        read_json_file=recording_read_json,
        thread_factory=deferred_thread(order),
    )
    router = make_shop_router(service)
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
    handler.path = "/api/shop-extract"
    with patch.object(shop_registry, "register", side_effect=recording_register), patch.object(
        shop_registry, "snapshot", side_effect=recording_snapshot
    ), patch.object(shop_module, "snapshot_shop_job", side_effect=recording_serializer):
        dispatch_shop(router, "POST", handler)
    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.responses == [202]
    assert response["url"] == shop_url and response["status"] == "queued"
    assert response["source_type"] == "search" and response["region"] == "JP"
    assert "prompt" not in response
    assert order == ["register", "thread.start", "snapshot", "shop_extract.json", "shop_analysis.json", "serializer"]

    metrics_target = "https://www.tiktok.com/@fixture/video/456"
    metrics_registry = JobRegistry()
    order: list[str] = []
    original_register = metrics_registry.register
    original_snapshot = metrics_registry.snapshot
    original_serializer = metrics_module.snapshot_metrics_job

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
        return original_serializer(job, result=result)

    metrics_service = make_metrics_service(
        web_app,
        metrics_registry,
        read_json_file=recording_read_json,
        thread_factory=deferred_thread(order),
    )
    metrics_router = make_metrics_router(metrics_service)
    handler = handler_for({"target": metrics_target, "endpoint": "video-info"})
    handler.path = "/api/video-metrics"
    with patch.object(metrics_registry, "register", side_effect=recording_register), patch.object(
        metrics_registry, "snapshot", side_effect=recording_snapshot
    ), patch.object(metrics_module, "snapshot_metrics_job", side_effect=recording_serializer):
        dispatch_metrics(metrics_router, "POST", handler)
    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert handler.responses == [202]
    assert response["target"] == metrics_target and response["endpoint"] == "video-info"
    assert response["status"] == "queued"
    assert order == ["register", "thread.start", "snapshot", "result.json", "serializer"]


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
    metrics_service = jobs["metrics_service"]
    metrics_router = make_metrics_router(metrics_service)
    expected = metrics_service.payload_for(metrics.id)
    assert expected is not None
    assert_json_response(
        dispatch_metrics(metrics_router, "GET", FakeHandler(f"/api/video-metrics-job?id={metrics.id}")),
        200,
        expected,
    )
    assert_sse_response(
        dispatch_metrics(metrics_router, "GET", FakeHandler(f"/api/video-metrics-events?id={metrics.id}")),
        expected,
    )
    assert_json_response(
        dispatch_metrics(metrics_router, "GET", FakeHandler("/api/video-metrics-job?id=missing")),
        404,
        {"error": "Video metrics job not found"},
    )
    assert_sse_response(
        dispatch_metrics(metrics_router, "GET", FakeHandler("/api/video-metrics-events?id=missing")),
        {"status": "missing", "error": "Video metrics job not found"},
    )

    call_order: list[str] = []
    order_registry = jobs["metrics_registry"]
    original_snapshot = order_registry.snapshot

    def recording_snapshot(job_id: str) -> Any:
        call_order.append("snapshot")
        return original_snapshot(job_id)

    def recording_read_json(path: Path) -> Any:
        call_order.append("read")
        return web_app.read_json(path)

    order_service = make_metrics_service(web_app, order_registry, read_json_file=recording_read_json)
    order_router = make_metrics_router(order_service)
    with patch.object(order_registry, "snapshot", side_effect=recording_snapshot):
        assert_json_response(
            dispatch_metrics(order_router, "GET", FakeHandler(f"/api/video-metrics-job?id={metrics.id}")),
            200,
            expected,
        )
    assert call_order == ["snapshot", "read"]

    shop = jobs["shop"]
    shop_registry = jobs["shop_registry"]
    shop_service = jobs["shop_service"]
    shop_router = make_shop_router(shop_service)
    expected = public_shop_payload(shop_service, shop)
    assert "prompt" not in expected
    assert "private fixture prompt" not in json_body(expected).decode("utf-8")
    assert_json_response(
        dispatch_shop(shop_router, "GET", FakeHandler(f"/api/shop-job?id={shop.id}")), 200, expected
    )
    assert_sse_response(
        dispatch_shop(shop_router, "GET", FakeHandler(f"/api/shop-events?id={shop.id}")), expected
    )
    assert "private fixture prompt" not in dispatch_shop(
        shop_router, "GET", FakeHandler(f"/api/shop-events?id={shop.id}")
    ).wfile.getvalue().decode("utf-8")
    assert_json_response(
        dispatch_shop(shop_router, "GET", FakeHandler("/api/shop-job?id=missing")),
        404,
        {"error": "TikTok Shop job not found"},
    )
    assert_sse_response(
        dispatch_shop(shop_router, "GET", FakeHandler("/api/shop-events?id=missing")),
        {"status": "missing", "error": "TikTok Shop job not found"},
    )

    call_order: list[str] = []
    original_snapshot = shop_registry.snapshot

    def recording_snapshot(job_id: str) -> Any:
        call_order.append("snapshot")
        return original_snapshot(job_id)

    def recording_read_json(path: Path) -> Any:
        call_order.append(path.name)
        return web_app.read_json(path)

    ordered_router = make_shop_router(make_shop_service(web_app, shop_registry, read_json_file=recording_read_json))
    with patch.object(shop_registry, "snapshot", side_effect=recording_snapshot):
        assert_json_response(
            dispatch_shop(ordered_router, "GET", FakeHandler(f"/api/shop-job?id={shop.id}")), 200, expected
        )
    assert call_order == ["snapshot", "shop_extract.json", "shop_analysis.json"]

    amazon = jobs["amazon"]
    amazon_registry = jobs["amazon_registry"]
    amazon_service = jobs["amazon_service"]
    amazon_router = make_amazon_router(amazon_service)
    expected = public_amazon_payload(amazon_service, amazon)
    assert_json_response(
        dispatch_amazon(amazon_router, "GET", FakeHandler(f"/api/amazon-job?id={amazon.id}")), 200, expected
    )
    assert_sse_response(
        dispatch_amazon(amazon_router, "GET", FakeHandler(f"/api/amazon-events?id={amazon.id}")), expected
    )
    assert_json_response(
        dispatch_amazon(amazon_router, "GET", FakeHandler("/api/amazon-job?id=missing")),
        404,
        {"error": "Amazon job not found"},
    )
    assert_sse_response(
        dispatch_amazon(amazon_router, "GET", FakeHandler("/api/amazon-events?id=missing")),
        {"status": "missing", "error": "Amazon job not found"},
    )

    call_order: list[str] = []
    original_snapshot = amazon_registry.snapshot

    def recording_snapshot(job_id: str) -> Any:
        call_order.append("snapshot")
        return original_snapshot(job_id)

    def recording_read_json(path: Path) -> Any:
        call_order.append(path.name)
        return web_app.read_json(path)

    ordered_router = make_amazon_router(
        make_amazon_service(web_app, amazon_registry, read_json_file=recording_read_json)
    )
    with patch.object(amazon_registry, "snapshot", side_effect=recording_snapshot):
        assert_json_response(
            dispatch_amazon(ordered_router, "GET", FakeHandler(f"/api/amazon-job?id={amazon.id}")), 200, expected
        )
    assert call_order == ["snapshot", "result.json"]

    post_order: list[str] = []

    class DeferredThread:
        def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            post_order.append("thread.start")

    def recording_post_register(job_id: str, job: Any) -> None:
        post_order.append("register")
        assert (job.target, job.target_type, job.url, job.pages) == (
            "B000POST01", "asin", "https://www.amazon.com/dp/B000POST01", 3,
        )
        return original_register(job_id, job)

    def recording_post_snapshot(job_id: str) -> Any:
        post_order.append("snapshot")
        return original_snapshot(job_id)

    def recording_post_read_json(path: Path) -> Any:
        post_order.append(path.name)
        return web_app.read_json(path)

    post_service = make_amazon_service(
        web_app,
        amazon_registry,
        read_json_file=recording_post_read_json,
        thread_factory=DeferredThread,
        job_id_factory=lambda: "amazon-post-fixture",
    )
    post_router = make_amazon_router(post_service)
    post_body = json.dumps({"target": "B000POST01", "target_type": "asin", "pages": 3}).encode("utf-8")
    post_handler = FakeHandler("/api/amazon-scrape")
    post_handler.headers = {"Content-Length": str(len(post_body))}
    post_handler.rfile = io.BytesIO(post_body)
    original_register = amazon_registry.register
    with patch.object(amazon_registry, "register", side_effect=recording_post_register), patch.object(
        amazon_registry, "snapshot", side_effect=recording_post_snapshot
    ):
        dispatch_amazon(post_router, "POST", post_handler)
    response = json.loads(post_handler.wfile.getvalue().decode("utf-8"))
    assert post_handler.responses == [202]
    assert (response["target"], response["target_type"], response["url"], response["pages"], response["status"]) == (
        "B000POST01", "asin", "https://www.amazon.com/dp/B000POST01", 3, "queued",
    )
    assert post_order == ["register", "thread.start", "snapshot", "result.json"]


def assert_metrics_artifact_failures_and_broken_pipe(web_app: Any) -> None:
    registry = JobRegistry()
    service = make_metrics_service(web_app, registry)
    router = make_metrics_router(service)
    missing_artifact = MetricsJob(
        id="metrics-artifact-missing",
        target="@fixture",
        endpoint="profile",
        status="complete",
        created_at=35.0,
        updated_at=36.0,
        output_dir="output/tiktok_api/metrics-artifact-missing",
    )
    registry.register(missing_artifact.id, missing_artifact)
    expected_missing = service.payload_for(missing_artifact.id)
    assert expected_missing is not None
    assert_json_response(
        dispatch_metrics(router, "GET", FakeHandler(f"/api/video-metrics-job?id={missing_artifact.id}")),
        200,
        expected_missing,
    )
    assert_sse_response(
        dispatch_metrics(router, "GET", FakeHandler(f"/api/video-metrics-events?id={missing_artifact.id}")),
        expected_missing,
    )

    invalid_artifact = MetricsJob(
        id="metrics-artifact-invalid",
        target="@fixture-invalid",
        endpoint="profile",
        status="complete",
        created_at=37.0,
        updated_at=38.0,
        output_dir="output/tiktok_api/metrics-artifact-invalid",
    )
    registry.register(invalid_artifact.id, invalid_artifact)
    invalid_path = web_app.OUTPUT_DIR / "tiktok_api" / invalid_artifact.id / "result.json"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_text("{invalid", encoding="utf-8")

    invalid_get = FakeHandler(f"/api/video-metrics-job?id={invalid_artifact.id}")
    try:
        dispatch_metrics(router, "GET", invalid_get)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("invalid Metrics artifact must propagate from GET")
    assert invalid_get.responses == []
    assert invalid_get.response_headers == []
    assert invalid_get.wfile.getvalue() == b""
    assert invalid_get.ended is False

    invalid_sse = FakeHandler(f"/api/video-metrics-events?id={invalid_artifact.id}")
    try:
        dispatch_metrics(router, "GET", invalid_sse)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("invalid Metrics artifact must propagate from SSE")
    assert invalid_sse.responses == [200]
    assert invalid_sse.header("Content-Type") == "text/event-stream; charset=utf-8"
    assert invalid_sse.header("Cache-Control") == "no-cache"
    assert invalid_sse.header("Connection") == "keep-alive"
    assert invalid_sse.ended is True
    assert invalid_sse.wfile.getvalue() == b""
    assert invalid_sse.close_connection is False

    class BrokenPipeWriter:
        def __init__(self) -> None:
            self.write_attempts = 0

        def write(self, _data: bytes) -> int:
            self.write_attempts += 1
            raise BrokenPipeError

        def flush(self) -> None:
            return None

    missing_pipe = FakeHandler("/api/video-metrics-events?id=missing-metrics-job")
    missing_pipe.wfile = BrokenPipeWriter()
    dispatch_metrics(router, "GET", missing_pipe)
    assert_event_headers(missing_pipe)
    assert missing_pipe.wfile.write_attempts == 1
    assert missing_pipe.close_connection is True

    normal_pipe = FakeHandler(f"/api/video-metrics-events?id={missing_artifact.id}")
    normal_pipe.wfile = BrokenPipeWriter()
    dispatch_metrics(router, "GET", normal_pipe)
    assert_event_headers(normal_pipe)
    assert normal_pipe.wfile.write_attempts == 1
    assert normal_pipe.close_connection is True


def assert_amazon_artifact_failures_and_broken_pipe(web_app: Any) -> None:
    registry = JobRegistry()
    missing_artifact = AmazonJob(
        id="amazon-artifact-missing",
        target="B000MISSING",
        target_type="asin",
        url="https://www.amazon.com/dp/B000MISSING",
        pages=1,
        status="complete",
        created_at=45.0,
        updated_at=46.0,
        output_dir="output/amazon/amazon-artifact-missing",
    )
    invalid_artifact = AmazonJob(
        id="amazon-artifact-invalid",
        target="B000INVALID",
        target_type="asin",
        url="https://www.amazon.com/dp/B000INVALID",
        pages=1,
        status="complete",
        created_at=47.0,
        updated_at=48.0,
        output_dir="output/amazon/amazon-artifact-invalid",
    )
    registry.register(missing_artifact.id, missing_artifact)
    registry.register(invalid_artifact.id, invalid_artifact)
    invalid_path = web_app.OUTPUT_DIR / "amazon" / invalid_artifact.id / "result.json"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_text("{invalid", encoding="utf-8")
    service = make_amazon_service(web_app, registry)
    router = make_amazon_router(service)
    expected_missing = service.payload_for(missing_artifact.id)
    assert expected_missing is not None
    assert_json_response(
        dispatch_amazon(router, "GET", FakeHandler(f"/api/amazon-job?id={missing_artifact.id}")),
        200,
        expected_missing,
    )
    assert_sse_response(
        dispatch_amazon(router, "GET", FakeHandler(f"/api/amazon-events?id={missing_artifact.id}")),
        expected_missing,
    )

    invalid_get = FakeHandler(f"/api/amazon-job?id={invalid_artifact.id}")
    try:
        dispatch_amazon(router, "GET", invalid_get)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("invalid Amazon artifact must propagate from GET")
    assert invalid_get.responses == []
    assert invalid_get.response_headers == []
    assert invalid_get.wfile.getvalue() == b""
    assert invalid_get.ended is False

    invalid_sse = FakeHandler(f"/api/amazon-events?id={invalid_artifact.id}")
    try:
        dispatch_amazon(router, "GET", invalid_sse)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("invalid Amazon artifact must propagate from SSE")
    assert_event_headers(invalid_sse)
    assert invalid_sse.wfile.getvalue() == b""
    assert invalid_sse.close_connection is False

    class BrokenPipeWriter:
        def __init__(self) -> None:
            self.write_attempts = 0

        def write(self, _data: bytes) -> int:
            self.write_attempts += 1
            raise BrokenPipeError

        def flush(self) -> None:
            return None

    normal_pipe = FakeHandler(f"/api/amazon-events?id={missing_artifact.id}")
    normal_pipe.wfile = BrokenPipeWriter()
    dispatch_amazon(router, "GET", normal_pipe)
    assert_event_headers(normal_pipe)
    assert normal_pipe.wfile.write_attempts == 1
    assert normal_pipe.close_connection is True


def assert_shop_artifact_failure_contract(web_app: Any) -> None:
    registry = JobRegistry()
    service = make_shop_service(web_app, registry)
    router = make_shop_router(service)

    def shop_job(job_id: str) -> ShopJob:
        return ShopJob(
            id=job_id,
            url="https://shop.tiktok.com/view/product/artifact-fixture",
            source_type="product",
            region="US",
            max_pages=1,
            review_pages=1,
            analyze=True,
            related_videos=False,
            status="complete",
            created_at=90.0,
            updated_at=91.0,
            output_dir=f"output/tiktok_shop/{job_id}",
        )

    def paths_for(job_id: str) -> tuple[Path, Path]:
        output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / job_id
        return output_dir / "shop_extract.json", output_dir / "shop_analysis.json"

    for missing_name in ("shop_extract.json", "shop_analysis.json"):
        job_id = f"shop-missing-{missing_name.removesuffix('.json')}"
        job = shop_job(job_id)
        registry.register(job_id, job)
        extract_path, analysis_path = paths_for(job_id)
        write_json(extract_path, {"items": ["extract"]})
        write_json(analysis_path, {"summary": "analysis"})
        (extract_path if missing_name == "shop_extract.json" else analysis_path).unlink()
        expected = public_shop_payload(service, job)
        assert expected["extract" if missing_name == "shop_extract.json" else "analysis"] is None
        assert_json_response(
            dispatch_shop(router, "GET", FakeHandler(f"/api/shop-job?id={job_id}")), 200, expected
        )
        assert_sse_response(
            dispatch_shop(router, "GET", FakeHandler(f"/api/shop-events?id={job_id}")), expected
        )

    for invalid_name in ("shop_extract.json", "shop_analysis.json"):
        job_id = f"shop-invalid-{invalid_name.removesuffix('.json')}"
        registry.register(job_id, shop_job(job_id))
        extract_path, analysis_path = paths_for(job_id)
        write_json(extract_path, {"items": ["extract"]})
        write_json(analysis_path, {"summary": "analysis"})
        (extract_path if invalid_name == "shop_extract.json" else analysis_path).write_text("{not json", encoding="utf-8")

        get_handler = FakeHandler(f"/api/shop-job?id={job_id}")
        try:
            dispatch_shop(router, "GET", get_handler)
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError(f"GET did not raise for invalid {invalid_name}")
        assert get_handler.responses == []
        assert get_handler.wfile.getvalue() == b""
        assert get_handler.ended is False
        assert get_handler.close_connection is False

        sse_handler = FakeHandler(f"/api/shop-events?id={job_id}")
        try:
            dispatch_shop(router, "GET", sse_handler)
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


def assert_shop_route_defaults_and_broken_pipe(web_app: Any) -> None:
    registry = JobRegistry()
    started: list[str] = []

    class DeferredThread:
        def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            started.append(self.args[0])

    defaults = {
        "SOCIAVAULT_REGION": "ca",
        "SOCIAVAULT_MAX_PAGES": "3",
        "SOCIAVAULT_REVIEW_PAGES": "4",
    }
    service = make_shop_service(web_app, registry, thread_factory=DeferredThread, job_id_factory=lambda: "shop-defaults")
    router = Router()
    register_shop_api_routes(router, service, getenv=lambda name, default: defaults.get(name, default))
    body = json.dumps({
        "url": "https://shop.tiktok.com/view/product/defaults",
        "analyze": "false",
        "related_videos": "false",
    }).encode("utf-8")
    handler = FakeHandler("/api/shop-extract")
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    dispatch_shop(router, "POST", handler)
    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    job = registry.snapshot("shop-defaults")
    assert job is not None
    assert (job.region, job.max_pages, job.review_pages, job.analyze, job.related_videos) == ("CA", 3, 4, True, True)
    assert response["status"] == "queued"
    assert (response["region"], response["max_pages"], response["review_pages"], response["analyze"], response["related_videos"]) == (
        "CA", 3, 4, True, True,
    )
    assert started == ["shop-defaults"]

    class BrokenPipeWriter:
        def __init__(self) -> None:
            self.write_attempts = 0

        def write(self, _data: bytes) -> int:
            self.write_attempts += 1
            raise BrokenPipeError

        def flush(self) -> None:
            return None

    missing_handler = FakeHandler("/api/shop-events?id=missing-shop-job")
    missing_handler.wfile = BrokenPipeWriter()
    dispatch_shop(router, "GET", missing_handler)
    assert missing_handler.responses == [200]
    assert missing_handler.header("Content-Type") == "text/event-stream; charset=utf-8"
    assert missing_handler.header("Cache-Control") == "no-cache"
    assert missing_handler.header("Connection") == "keep-alive"
    assert missing_handler.ended is True
    assert missing_handler.close_connection is True
    assert missing_handler.wfile.write_attempts == 1

    output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / "shop-defaults"
    write_json(output_dir / "shop_extract.json", {"items": [{"id": "broken-pipe"}]})
    write_json(output_dir / "shop_analysis.json", {"summary": "broken-pipe"})
    payload_handler = FakeHandler("/api/shop-events?id=shop-defaults")
    payload_handler.wfile = BrokenPipeWriter()
    dispatch_shop(router, "GET", payload_handler)
    assert payload_handler.responses == [200]
    assert payload_handler.header("Content-Type") == "text/event-stream; charset=utf-8"
    assert payload_handler.header("Cache-Control") == "no-cache"
    assert payload_handler.header("Connection") == "keep-alive"
    assert payload_handler.ended is True
    assert payload_handler.close_connection is True
    assert payload_handler.wfile.write_attempts == 1


def assert_shop_composition_contract(web_app: Any) -> None:
    job_id = "shop-composition-fixture"
    job = ShopJob(
        id=job_id,
        url="https://shop.tiktok.com/view/product/composition",
        source_type="product",
        region="US",
        max_pages=1,
        review_pages=1,
        analyze=True,
        related_videos=False,
        prompt="private composition prompt",
        status="complete",
        output_dir=f"output/tiktok_shop/{job_id}",
    )
    output_dir = web_app.OUTPUT_DIR / "tiktok_shop" / job_id
    extract_path = output_dir / "shop_extract.json"
    analysis_path = output_dir / "shop_analysis.json"
    write_json(extract_path, {"items": [{"id": "composition"}]})
    write_json(analysis_path, {"summary": "composition"})
    web_app.shop_job_registry.register(job_id, job)
    payload = web_app.shop_service.payload_for(job_id)
    assert payload is not None
    assert payload["extract"] == {"items": [{"id": "composition"}]}
    assert "prompt" not in payload
    assert_json_response(dispatch_get(web_app, f"/api/shop-job?id={job_id}"), 200, payload)
    assert_sse_response(dispatch_get(web_app, f"/api/shop-events?id={job_id}"), payload)
    assert_json_response(
        dispatch_get(web_app, "/api/shop-job?id=missing-composition"),
        404,
        {"error": "TikTok Shop job not found"},
    )
    assert_sse_response(
        dispatch_get(web_app, "/api/shop-events?id=missing-composition"),
        {"status": "missing", "error": "TikTok Shop job not found"},
    )

    extract_path.write_text("{not json", encoding="utf-8")
    get_handler = FakeHandler(f"/api/shop-job?id={job_id}")
    try:
        web_app.Handler.do_GET(get_handler)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("composed Shop GET did not raise for invalid extract")
    assert get_handler.responses == []
    assert get_handler.wfile.getvalue() == b""
    assert get_handler.ended is False

    sse_handler = FakeHandler(f"/api/shop-events?id={job_id}")
    try:
        web_app.Handler.do_GET(sse_handler)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("composed Shop SSE did not raise for invalid extract")
    assert sse_handler.responses == [200]
    assert sse_handler.header("Content-Type") == "text/event-stream; charset=utf-8"
    assert sse_handler.header("Cache-Control") == "no-cache"
    assert sse_handler.header("Connection") == "keep-alive"
    assert sse_handler.wfile.getvalue() == b""
    assert sse_handler.wfile.flush_count == 0
    assert sse_handler.ended is True
    assert sse_handler.close_connection is False


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
    handler = FakeHandler(f"/api/download-events?id={job.id}")
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


def assert_download_sse_broken_pipe(web_app: Any) -> None:
    class BrokenPipeWriter:
        def __init__(self) -> None:
            self.write_attempts = 0

        def write(self, _data: bytes) -> int:
            self.write_attempts += 1
            raise BrokenPipeError

        def flush(self) -> None:
            return None

    original_registry = web_app.download_job_registry
    registry = web_app.JobRegistry()
    web_app.download_job_registry = registry
    try:
        missing = FakeHandler("/api/download-events?id=missing-download-job")
        missing.wfile = BrokenPipeWriter()
        web_app.Handler.stream_download_events(missing, "missing-download-job")
        assert_event_headers(missing)
        assert missing.close_connection is True
        assert missing.wfile.write_attempts == 1

        job = web_app.DownloadJob(
            id="download-broken-pipe",
            url="https://www.tiktok.com/@fixture/video/broken-pipe",
            status="complete",
        )
        registry.register(job.id, job)
        payload = FakeHandler(f"/api/download-events?id={job.id}")
        payload.wfile = BrokenPipeWriter()
        web_app.Handler.stream_download_events(payload, job.id)
        assert_event_headers(payload)
        assert payload.close_connection is True
        assert payload.wfile.write_attempts == 1
    finally:
        web_app.download_job_registry = original_registry


def assert_metrics_sse_marker(web_app: Any) -> None:
    job = MetricsJob(
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
    timestamps = iter((62.0, 63.0, 64.0))
    registry = JobRegistry(clock=lambda: next(timestamps))
    registry.register(job.id, job)
    handler = FakeHandler(f"/api/video-metrics-events?id={job.id}")
    original_snapshot = registry.snapshot
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
        return web_app.read_json(path)

    service = make_metrics_service(web_app, registry, read_json_file=recording_read_json)
    router = make_metrics_router(service, sleep=advance)
    with patch.object(registry, "snapshot", side_effect=recording_snapshot):
        dispatch_metrics(router, "GET", handler)
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
    job = ShopJob(
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
    timestamps = iter((72.0, 73.0))
    registry = JobRegistry(clock=lambda: next(timestamps))
    registry.register(job.id, job)
    handler = FakeHandler()
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

    service = make_shop_service(web_app, registry, read_json_file=recording_read_json)
    router = make_shop_router(service, sleep=advance)
    with patch.object(registry, "snapshot", side_effect=recording_snapshot):
        handler.path = f"/api/shop-events?id={job.id}"
        dispatch_shop(router, "GET", handler)
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
    job = AmazonJob(
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
    registry = JobRegistry()
    registry.register(job.id, job)
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

    service = make_amazon_service(web_app, registry, read_json_file=recording_read_json)
    router = make_amazon_router(service, sleep=advance)
    handler = FakeHandler(f"/api/amazon-events?id={job.id}")
    with patch.object(registry, "snapshot", side_effect=recording_snapshot):
        dispatch_amazon(router, "GET", handler)
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
    missing_router = make_amazon_router(make_amazon_service(web_app, JobRegistry()))
    missing_handler.path = "/api/amazon-events?id=missing-amazon-job"
    dispatch_amazon(missing_router, "GET", missing_handler)
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
        assert_metrics_artifact_failures_and_broken_pipe(web_app)
        assert_amazon_artifact_failures_and_broken_pipe(web_app)
        assert_shop_artifact_failure_contract(web_app)
        assert_shop_route_defaults_and_broken_pipe(web_app)
        assert_shop_composition_contract(web_app)
        assert_sse_marker(web_app)
        assert_download_sse_broken_pipe(web_app)
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
