"""Registration for the cached metrics page and Metrics job APIs."""

from __future__ import annotations

from http import HTTPStatus
import json
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from core.http import json_response, text_response, write_sse_event
from routes.router import Router
from services.metrics import MetricsService, TIKTOK_ENDPOINTS


def register_metrics_page(
    router: Router,
    *,
    html_snapshot: str,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the metrics page using its import-time HTML snapshot."""

    def metrics_page(handler: Any, params: Mapping[str, str]) -> None:
        text_response(
            handler,
            HTTPStatus.OK,
            inject_nav(html_snapshot, "/metrics"),
            "text/html; charset=utf-8",
        )

    router.get("/metrics", metrics_page)


def register_metrics_api_routes(
    router: Router,
    service: MetricsService,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Register the Metrics job HTTP and SSE endpoints."""

    def metrics_job(handler: Any, params: Mapping[str, str]) -> None:
        job_id = parse_qs(urlparse(handler.path).query).get("id", [""])[0]
        payload = service.payload_for(job_id)
        if payload is None:
            return json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Video metrics job not found"})
        return json_response(handler, HTTPStatus.OK, payload)

    def metrics_events(handler: Any, params: Mapping[str, str]) -> None:
        job_id = parse_qs(urlparse(handler.path).query).get("id", [""])[0]
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()

        last_marker: tuple[Any, ...] | None = None
        while True:
            payload = service.payload_for(job_id)
            if payload is None:
                try:
                    write_sse_event(handler, {"status": "missing", "error": "Video metrics job not found"})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                handler.close_connection = True
                return

            marker = (
                payload.get("status"),
                payload.get("updated_at"),
                len(payload.get("log") or []),
                payload.get("error"),
            )
            try:
                if marker != last_marker:
                    write_sse_event(handler, payload)
                    last_marker = marker
                if payload.get("status") not in {"queued", "running"}:
                    handler.close_connection = True
                    return
                sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                handler.close_connection = True
                return

    def video_metrics(handler: Any, params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            target = str(payload.get("target", "")).strip()
            endpoint = str(payload.get("endpoint", "video-info")).strip()
            if endpoint not in TIKTOK_ENDPOINTS:
                raise ValueError(f"Unknown endpoint: {endpoint}")
            if not target and endpoint not in ("trending", "music-popular"):
                raise ValueError("target is required for this endpoint")
            if len(target) > 2048:
                raise ValueError("target is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        return json_response(
            handler,
            HTTPStatus.ACCEPTED,
            service.create_and_start(target=target, endpoint=endpoint),
        )

    router.get("/api/video-metrics-job", metrics_job)
    router.get("/api/video-metrics-events", metrics_events)
    router.post("/api/video-metrics", video_metrics)
