"""Daily report HTTP and SSE routes."""

from __future__ import annotations

import hmac
from http import HTTPStatus
import json
import os
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from core.http import json_response, write_sse_event
from routes.router import Router
from services.report import ReportDisabledError, ReportService


def register_report_routes(
    router: Router,
    service: ReportService,
    *,
    sleep: Callable[[float], None] = time.sleep,
    getenv: Callable[[str, str], str] = os.getenv,
) -> None:
    """Register the daily report JSON APIs and dedicated event stream."""

    def query(handler: Any) -> dict[str, list[str]]:
        return parse_qs(urlparse(handler.path).query)

    def report_today(handler: Any, params: Mapping[str, str]) -> None:
        include_raw = query(handler).get("raw", ["0"])[0] in {"1", "true", "yes"}
        return json_response(handler, HTTPStatus.OK, service.today(include_raw=include_raw))

    def report(handler: Any, params: Mapping[str, str]) -> None:
        values = query(handler)
        include_raw = values.get("raw", ["0"])[0] in {"1", "true", "yes"}
        report_date = values.get("date", [""])[0] or None
        return json_response(handler, HTTPStatus.OK, service.dated_report(report_date, include_raw=include_raw))

    def history(handler: Any, params: Mapping[str, str]) -> None:
        try:
            limit = int(query(handler).get("limit", ["30"])[0])
        except ValueError:
            limit = 30
        return json_response(handler, HTTPStatus.OK, service.history(limit))

    def settings(handler: Any, params: Mapping[str, str]) -> None:
        return json_response(handler, HTTPStatus.OK, service.settings())

    def report_feishu(handler: Any, params: Mapping[str, str]) -> None:
        values = query(handler)
        expected = getenv("REPORT_BOT_TOKEN", "").strip()
        if expected:
            authorization = handler.headers.get("Authorization", "").strip()
            supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            if not supplied:
                supplied = values.get("token", [""])[0].strip()
            if not supplied or not hmac.compare_digest(supplied, expected):
                return json_response(handler, HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
        report_date = values.get("date", [""])[0].strip() or None
        if report_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "date must be YYYY-MM-DD"})
        try:
            limit = int(values.get("limit", ["10"])[0])
        except ValueError:
            limit = 10
        host = handler.headers.get("Host", "").strip()
        detail_origin = ""
        if host:
            proto = "https" if handler.headers.get("X-Forwarded-Proto", "").lower() == "https" else "http"
            detail_origin = f"{proto}://{host}"
        return json_response(
            handler,
            HTTPStatus.OK,
            service.feishu_payload(report_date, limit=limit, detail_origin=detail_origin),
        )

    def events(handler: Any, params: Mapping[str, str]) -> None:
        report_date = query(handler).get("date", [""])[0] or None
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()

        last_marker: tuple[Any, ...] | None = None
        while True:
            payload = service.progress(report_date)
            marker = (
                payload.get("status"),
                payload.get("stage"),
                payload.get("progress"),
                payload.get("message"),
                payload.get("updated_at"),
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

    def report_run(handler: Any, params: Mapping[str, str]) -> None:
        try:
            return json_response(handler, HTTPStatus.ACCEPTED, service.run())
        except ReportDisabledError as exc:
            return json_response(handler, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except Exception as exc:
            return json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def report_delete(handler: Any, params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            return json_response(handler, HTTPStatus.OK, service.delete(str(payload.get("date") or payload.get("report_date") or "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def report_settings(handler: Any, params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            return json_response(handler, HTTPStatus.OK, service.save(payload))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def report_translate(handler: Any, params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            return json_response(
                handler,
                HTTPStatus.OK,
                service.translate(
                    str(payload.get("date") or payload.get("report_date") or ""),
                    str(payload.get("platform") or ""),
                    str(payload.get("video_id") or ""),
                    bool(payload.get("force", False)),
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def backfill_covers(handler: Any, params: Mapping[str, str]) -> None:
        return json_response(handler, HTTPStatus.OK, service.backfill())

    router.get("/api/report/today", report_today)
    router.get("/api/report", report)
    router.get("/api/report/history", history)
    router.get("/api/report/settings", settings)
    router.get("/api/report/events", events)
    router.get("/api/report/feishu", report_feishu)
    router.post("/api/report/run", report_run)
    router.post("/api/report/delete", report_delete)
    router.post("/api/report/settings", report_settings)
    router.post("/api/report/translate", report_translate)
    router.post("/api/report/backfill-covers", backfill_covers)
