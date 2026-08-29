"""Registration for the LAN chat page route."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from core.http import text_response
from routes.router import Router


def register_lan_chat_page(
    router: Router,
    *,
    scripts_dir: Path,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the LAN chat page while reading its template per request."""

    def lan_chat_page(handler: Any, params: Mapping[str, str]) -> None:
        html = (scripts_dir / "static" / "lan_chat.html").read_text(encoding="utf-8")
        text_response(handler, 200, inject_nav(html, "/lan-chat"), "text/html; charset=utf-8")

    router.get("/lan-chat", lan_chat_page)
