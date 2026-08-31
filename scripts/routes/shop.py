"""Registration for the cached TikTok Shop page."""

from __future__ import annotations

from http import HTTPStatus
import json
import os
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from core.http import json_response, text_response, write_sse_event
from routes.router import Router
from services.shop import ShopService


def register_shop_page(
    router: Router,
    *,
    html_snapshot: str,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the Shop page using its import-time HTML snapshot."""

    def shop_page(handler: Any, params: Mapping[str, str]) -> None:
        text_response(
            handler,
            HTTPStatus.OK,
            inject_nav(html_snapshot, "/shop"),
            "text/html; charset=utf-8",
        )

    router.get("/shop", shop_page)


def register_shop_api_routes(
    router: Router,
    service: ShopService,
    *,
    getenv: Callable[[str, str], str | None] = os.getenv,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Register the TikTok Shop job HTTP and SSE endpoints."""

    def shop_job(handler: Any, params: Mapping[str, str]) -> None:
        job_id = parse_qs(urlparse(handler.path).query).get("id", [""])[0]
        payload = service.payload_for(job_id)
        if payload is None:
            return json_response(handler, HTTPStatus.NOT_FOUND, {"error": "TikTok Shop job not found"})
        return json_response(handler, HTTPStatus.OK, payload)

    def shop_events(handler: Any, params: Mapping[str, str]) -> None:
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
                    write_sse_event(handler, {"status": "missing", "error": "TikTok Shop job not found"})
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

    def shop_extract(handler: Any, params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            url = str(payload.get("url", "")).strip()
            source_type = str(payload.get("source_type") or "product")
            region = str(payload.get("region") or getenv("SOCIAVAULT_REGION", "US")).strip().upper()
            max_pages = int(payload.get("max_pages") or getenv("SOCIAVAULT_MAX_PAGES", "1"))
            review_pages = int(payload.get("review_pages") or getenv("SOCIAVAULT_REVIEW_PAGES", "1"))
            prompt = str(payload.get("prompt") or "").strip()
            analyze = bool(payload.get("analyze", True))
            related_videos = bool(payload.get("related_videos", False))
            if source_type not in {"product", "details", "reviews", "shop", "search"}:
                raise ValueError("source_type must be product, details, reviews, shop, or search")
            if not url or len(url) > 2048:
                raise ValueError("A TikTok Shop URL is required")
            if max_pages < 1 or max_pages > 20:
                raise ValueError("max_pages must be between 1 and 20")
            if review_pages < 0 or review_pages > 20:
                raise ValueError("review_pages must be between 0 and 20")
            if len(prompt) > 6000:
                raise ValueError("prompt is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        return json_response(
            handler,
            HTTPStatus.ACCEPTED,
            service.create_and_start(
                url=url,
                source_type=source_type,
                region=region,
                max_pages=max_pages,
                review_pages=review_pages,
                analyze=analyze,
                related_videos=related_videos,
                prompt=prompt,
            ),
        )

    router.get("/api/shop-job", shop_job)
    router.get("/api/shop-events", shop_events)
    router.post("/api/shop-extract", shop_extract)
