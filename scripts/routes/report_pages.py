"""Registration for the two static daily-report page routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from core.http import text_response
from routes.router import Router


def register_report_pages(
    router: Router,
    *,
    scripts_dir: Path,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register report pages while keeping templates fresh on every request."""

    static_dir = scripts_dir / "static"

    def report_page(handler: Any, params: Mapping[str, str]) -> None:
        html = (static_dir / "report.html").read_text(encoding="utf-8")
        text_response(handler, 200, inject_nav(html, "/report"), "text/html; charset=utf-8")

    def report_player_page(handler: Any, params: Mapping[str, str]) -> None:
        html = (static_dir / "report_player.html").read_text(encoding="utf-8")
        text_response(handler, 200, inject_nav(html, "/report/player"), "text/html; charset=utf-8")

    router.get("/report", report_page)
    router.get("/report/player", report_player_page)
