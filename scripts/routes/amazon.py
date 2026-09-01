"""HTTP routes for the Amazon scraper job workflow."""

from __future__ import annotations

from http import HTTPStatus
import json
import os
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from core.http import json_response, write_sse_event
from routes.router import Router
from services.amazon import AmazonService, amazon_url_for_target


def register_amazon_routes(
    router: Router,
    service: AmazonService,
    *,
    getenv: Callable[[str, str], str | None] = os.getenv,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Register the Amazon scraper job APIs on ``router``."""

    def amazon_job(handler: Any, _params: Mapping[str, str]) -> None:
        job_id = parse_qs(urlparse(handler.path).query).get("id", [""])[0]
        payload = service.payload_for(job_id)
        if payload is None:
            json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Amazon job not found"})
            return
        json_response(handler, HTTPStatus.OK, payload)

    def amazon_events(handler: Any, _params: Mapping[str, str]) -> None:
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
                    write_sse_event(handler, {"status": "missing", "error": "Amazon job not found"})
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

    def amazon_scrape(handler: Any, _params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            request: Mapping[str, Any] = json.loads(body.decode("utf-8") or "{}")
            target = str(request.get("target", "")).strip()
            target_type = str(request.get("target_type") or "url").strip()
            max_pages = int(getenv("AMAZON_MAX_PAGES", "1") or "1")
            max_pages = max(1, min(max_pages, 5))
            pages = int(request.get("pages") or max_pages)
            if pages < 1 or pages > 5:
                raise ValueError("pages must be between 1 and 5")
            url = amazon_url_for_target(target, target_type)
        except (json.JSONDecodeError, ValueError) as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        payload = service.create_and_start(
            target=target,
            target_type=target_type,
            url=url,
            pages=pages,
        )
        json_response(handler, HTTPStatus.ACCEPTED, payload)

    router.get("/api/amazon-job", amazon_job)
    router.get("/api/amazon-events", amazon_events)
    router.post("/api/amazon-scrape", amazon_scrape)
