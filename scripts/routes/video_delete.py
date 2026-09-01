"""HTTP registration for the video-delete API."""

from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any, Callable, Mapping

from core.http import json_response
from routes.router import Router
from services.video_delete import VideoDeleteService


def register_video_delete_routes(
    router: Router,
    service: VideoDeleteService,
    *,
    safe_filename: Callable[[str], str],
) -> None:
    def delete(handler: Any, _params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        json_response(handler, HTTPStatus.OK, service.delete(filename))

    router.post("/api/delete", delete)
