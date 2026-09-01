"""HTTP route for queued DeepSeek postprocessing."""

from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any, Mapping

from core.http import json_response
from routes.router import Router
from services.postprocess import PostprocessService


def register_postprocess_routes(router: Router, service: PostprocessService) -> None:
    """Register the queued DeepSeek postprocess endpoint."""

    def postprocess(handler: Any, _params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            request = service.prepare_request(
                filename=str(payload.get("filename", "")),
                analysis_prompt=str(payload.get("analysis_prompt") or "").strip(),
                analysis_source=str(payload.get("analysis_source") or payload.get("source") or "standard").strip(),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(handler, HTTPStatus.ACCEPTED, service.enqueue(request))

    router.post("/api/postprocess", postprocess)
