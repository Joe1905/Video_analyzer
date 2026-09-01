"""HTTP route for queued video analysis."""

from __future__ import annotations

from http import HTTPStatus
import json
import os
from typing import Any, Callable, Mapping

from core.http import json_response
from routes.router import Router
from services.analyze import AnalyzeService


def register_analyze_routes(
    router: Router,
    service: AnalyzeService,
    *,
    getenv: Callable[[str, str], str] = os.getenv,
) -> None:
    """Register the queued video-analysis endpoint."""

    def analyze(handler: Any, _params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            request = service.prepare_request(
                filename=str(payload.get("filename", "")),
                postprocess=bool(payload.get("postprocess", False)),
                reset_output=bool(payload.get("reset_output", False)),
                analysis_mode=str(payload.get("analysis_mode") or getenv("ANALYSIS_MODE", "analyzer")),
                analysis_prompt=str(payload.get("analysis_prompt") or "").strip(),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(handler, HTTPStatus.ACCEPTED, service.enqueue(request))

    router.post("/api/analyze", analyze)
