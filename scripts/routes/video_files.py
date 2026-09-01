"""HTTP route for the analyzer video catalog."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Mapping

from core.http import json_response
from routes.router import Router
from services.video_files import VideoFilesService


def register_video_files_routes(router: Router, service: VideoFilesService) -> None:
    """Register the analyzer video-catalog endpoint."""

    def video_files(handler: Any, _params: Mapping[str, str]) -> None:
        json_response(handler, HTTPStatus.OK, service.list_files())

    router.get("/api/files", video_files)
