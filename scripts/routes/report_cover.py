"""HTTP registration for report-cover artifacts."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import unquote

from core.http import binary_response, json_response
from routes.router import Router
from services.report_cover import ReportCoverNotFoundError, ReportCoverService


def register_report_cover_routes(
    router: Router,
    service: ReportCoverService,
    *,
    safe_filename: Callable[[str], str],
) -> None:
    """Register the existing GET-only report-cover prefix."""

    def report_cover(handler: Any, params: Mapping[str, str]) -> None:
        try:
            filename = safe_filename(unquote(params["suffix"]))
        except ValueError as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        try:
            body, content_type = service.load(filename)
        except ReportCoverNotFoundError:
            json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Cover not found"})
            return
        binary_response(handler, HTTPStatus.OK, body, content_type)

    router.get_prefix("/report-cover/", report_cover)
