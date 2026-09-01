"""HTTP route for synchronous analysis-artifact translation."""

from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any, Mapping

from core.http import json_response
from routes.router import Router
from services.translate import TranslateService, TranslationCommandError


def register_translate_routes(router: Router, service: TranslateService) -> None:
    """Register the analysis-artifact translation endpoint."""

    def translate(handler: Any, _params: Mapping[str, str]) -> None:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            request = service.prepare_request(
                filename=str(payload.get("filename", "")),
                tab=str(payload.get("tab") or "").strip(),
                source_mode=str(payload.get("analysis_source") or payload.get("source") or "standard").strip(),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        try:
            result = service.translate(request)
        except TranslationCommandError as exc:
            json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        json_response(handler, HTTPStatus.OK, result)

    router.post("/api/translate", translate)
