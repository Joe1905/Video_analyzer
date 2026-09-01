"""HTTP registration for public video streaming."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote

from core.http import json_response
from routes.router import Router


def register_video_stream_routes(
    router: Router,
    *,
    videos_dir: Path,
    safe_filename: Callable[[str], str],
    serve_video: Callable[[Any, Path], None],
) -> None:
    def stream(handler: Any, params: Mapping[str, str]) -> None:
        try:
            filename = safe_filename(unquote(params["suffix"]))
        except ValueError as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        serve_video(handler, videos_dir / filename)

    router.get_prefix("/video/", stream)
