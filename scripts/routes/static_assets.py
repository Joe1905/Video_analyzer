"""Registration for the GET-only static asset prefix."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote

from core.http import binary_response, json_response
from routes.router import Router


CACHE_CONTROL = "no-cache, no-store, must-revalidate"


def register_static_asset_route(
    router: Router,
    *,
    asset_root: Path,
    guess_type: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Register the existing GET-only static asset file boundary."""

    def serve_static_asset(handler: Any, params: Mapping[str, str]) -> None:
        static_root = asset_root.resolve()
        asset_path = (static_root / unquote(params["suffix"])).resolve()
        if asset_path != static_root and static_root not in asset_path.parents:
            return json_response(
                handler, HTTPStatus.BAD_REQUEST, {"error": "Invalid asset path"}
            )
        if not asset_path.is_file():
            return json_response(
                handler, HTTPStatus.NOT_FOUND, {"error": "Asset not found"}
            )
        content_type = guess_type(asset_path.name)[0] or "application/octet-stream"
        return binary_response(
            handler,
            HTTPStatus.OK,
            asset_path.read_bytes(),
            content_type,
            cache_control=CACHE_CONTROL,
        )

    router.get_prefix("/assets/", serve_static_asset)
