"""HTTP routes for the short-video download job workflow."""

from __future__ import annotations

from http import HTTPStatus
import json
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from core.http import json_response, write_sse_event
from routes.router import Router
from services.downloads import DownloadService


def register_download_routes(
    router: Router,
    service: DownloadService,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Register Download job HTTP and SSE endpoints on ``router``."""

    def download_job(handler: Any, _params: Mapping[str, str]) -> None:
        job_id = parse_qs(urlparse(handler.path).query).get("id", [""])[0]
        payload = service.payload_for(job_id)
        if payload is None:
            json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Download job not found"})
            return
        json_response(handler, HTTPStatus.OK, payload)

    def download_events(handler: Any, _params: Mapping[str, str]) -> None:
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
                    write_sse_event(handler, {"status": "missing", "error": "Download job not found"})
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

    def download(handler: Any, _params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        attempted_url = ""
        try:
            request = json.loads(body.decode("utf-8") or "{}")
            attempted_url = str(request.get("url", ""))
            source = request.get("source_tag") or request.get("source")
            payload = service.create_and_start(url=attempted_url, source=source)
        except (json.JSONDecodeError, ValueError) as exc:
            service.register_failed_attempt(url=attempted_url, error=str(exc))
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        json_response(handler, HTTPStatus.ACCEPTED, payload)

    router.get("/api/download-job", download_job)
    router.get("/api/download-events", download_events)
    router.post("/api/download", download)
