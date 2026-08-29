"""Registration for the cached TikTok Shop page."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Callable, Mapping

from core.http import text_response
from routes.router import Router


def register_shop_page(
    router: Router,
    *,
    html_snapshot: str,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the Shop page using its import-time HTML snapshot."""

    def shop_page(handler: Any, params: Mapping[str, str]) -> None:
        text_response(
            handler,
            HTTPStatus.OK,
            inject_nav(html_snapshot, "/shop"),
            "text/html; charset=utf-8",
        )

    router.get("/shop", shop_page)
