"""Registration for the utility-tool page route."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from core.http import text_response
from routes.router import Router


def register_tool_page(
    router: Router,
    *,
    scripts_dir: Path,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the utility page while reading its template per request."""

    def tool_page(handler: Any, params: Mapping[str, str]) -> None:
        html = (scripts_dir / "static" / "tool.html").read_text(encoding="utf-8")
        text_response(handler, 200, inject_nav(html, "/tool"), "text/html; charset=utf-8")

    router.get("/tool", tool_page)
