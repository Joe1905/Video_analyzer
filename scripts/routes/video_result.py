"""HTTP route for the analyzer result payload."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from core.http import json_response
from routes.router import Router
from services.video_result import VideoResultService


def register_video_result_routes(
    router: Router,
    service: VideoResultService,
    *,
    safe_filename: Callable[[str], str],
) -> None:
    """Register the analyzer result endpoint."""

    def video_result(handler: Any, _params: Mapping[str, str]) -> None:
        raw_filename = parse_qs(urlparse(handler.path).query).get("filename", [""])[0]
        try:
            filename = safe_filename(raw_filename)
        except ValueError as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(handler, HTTPStatus.OK, service.payload_for(filename))

    router.get("/api/result", video_result)
