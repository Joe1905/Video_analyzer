"""HTTP route for local video uploads."""

from __future__ import annotations

import cgi
from http import HTTPStatus
from typing import Any, Callable, Mapping

from core.http import json_response
from routes.router import Router
from services.upload import UploadService


MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


def register_upload_routes(
    router: Router,
    service: UploadService,
    *,
    field_storage_factory: Callable[..., Any] = cgi.FieldStorage,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
    normalize_video_source: Callable[[Any, str], str],
    default_source: str,
) -> None:
    """Register the multipart local-video upload endpoint."""

    def upload(handler: Any, _params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > max_upload_bytes:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "Invalid upload size"})
            return

        form = field_storage_factory(
            fp=handler.rfile,
            headers=handler.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )
        try:
            raw_file_items = form["video"]
        except KeyError:
            raw_file_items = []
        if not isinstance(raw_file_items, list):
            raw_file_items = [raw_file_items]
        file_items = [item for item in raw_file_items if getattr(item, "filename", None)]
        if not file_items:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "Missing video file"})
            return
        source = normalize_video_source(form.getfirst("source_tag") or form.getfirst("source"), default_source)
        payload = service.upload(file_items, source=source)
        status = HTTPStatus.OK if payload["files"] else HTTPStatus.BAD_REQUEST
        json_response(handler, status, payload)

    router.post("/api/upload", upload)
