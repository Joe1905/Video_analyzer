"""Health-check route registration with explicit application configuration."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Mapping

from core.http import json_response
from routes.router import Router


def register_health_route(router: Router, *, ui_test_mode: bool) -> None:
    """Register the exact GET health endpoint for this application instance."""

    def healthz(handler: Any, params: Mapping[str, str]) -> None:
        json_response(
            handler,
            HTTPStatus.OK,
            {"status": "ok", "ui_test_mode": ui_test_mode},
        )

    router.get("/healthz", healthz)
