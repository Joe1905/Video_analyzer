"""Registration for the video extraction page."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping

from core.http import text_response
from routes.router import Router


def register_extract_page(
    router: Router,
    *,
    template_path: Path,
    analysis_mode: Callable[[], str],
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the extraction page with request-fresh template and mode values."""

    def extract_page(handler: Any, params: Mapping[str, str]) -> None:
        template = template_path.read_text(encoding="utf-8")
        html = template.replace("__DEFAULT_ANALYSIS_MODE__", analysis_mode())
        text_response(
            handler,
            HTTPStatus.OK,
            inject_nav(html, "/extract"),
            "text/html; charset=utf-8",
        )

    router.get("/extract", extract_page)
