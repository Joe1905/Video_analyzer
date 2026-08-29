"""Registration for the standalone test harness page."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.http import text_response
from routes.router import Router


def register_harness_page(router: Router, *, scripts_dir: Path) -> None:
    """Register the harness page while reading its template per request."""

    def harness_page(handler: Any, params: Mapping[str, str]) -> None:
        html = (scripts_dir / "static" / "harness.html").read_text(encoding="utf-8")
        text_response(handler, 200, html, "text/html; charset=utf-8")

    router.get("/harness", harness_page)
