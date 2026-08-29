"""Registration for the cached Taobao page."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Callable, Mapping

from core.http import text_response
from routes.router import Router


def register_taobao_page(
    router: Router,
    *,
    html_snapshot: str,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the Taobao page using its import-time HTML snapshot."""

    def taobao_page(handler: Any, params: Mapping[str, str]) -> None:
        text_response(
            handler,
            HTTPStatus.OK,
            inject_nav(html_snapshot, "/taobao"),
            "text/html; charset=utf-8",
        )

    router.get("/taobao", taobao_page)
