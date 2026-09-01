"""Analyzer video-catalog listing without HTTP dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class VideoFilesService:
    """Assemble the existing analyzer-visible video list."""

    def __init__(
        self,
        videos_dir: Path,
        suffixes: set[str],
        media_validator: Callable[[Path], bool],
        analyzer_visible_source: Callable[[str], bool],
        queue_status: Callable[[str], str],
        queue_status_meta: Callable[[str], dict[str, str]],
        queue_title: Callable[[str], str],
        output_dir_for_filename: Callable[[str], Path],
        read_json_file: Callable[[Path], Any],
        social_summary: Callable[[Any], dict[str, str]],
    ) -> None:
        self._videos_dir = videos_dir
        self._suffixes = suffixes
        self._media_validator = media_validator
        self._analyzer_visible_source = analyzer_visible_source
        self._queue_status = queue_status
        self._queue_status_meta = queue_status_meta
        self._queue_title = queue_title
        self._output_dir_for_filename = output_dir_for_filename
        self._read_json_file = read_json_file
        self._social_summary = social_summary

    def list_files(self) -> list[dict[str, Any]]:
        files = []
        for path in sorted(
            self._videos_dir.glob("*"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        ):
            if path.is_file():
                if path.suffix.lower() not in self._suffixes:
                    continue
                if not self._media_validator(path):
                    continue
                name = path.name
                if not self._analyzer_visible_source(name):
                    continue
                meta = self._queue_status_meta(name)
                social_meta = self._social_summary(
                    self._read_json_file(
                        self._output_dir_for_filename(name) / "social_context.json"
                    )
                )
                files.append(
                    {
                        "name": name,
                        "size": path.stat().st_size,
                        "mtime": path.stat().st_mtime,
                        "status": self._queue_status(name),
                        "status_label": meta["label"],
                        "status_color": meta["color"],
                        "status_bg": meta["bg"],
                        "title": self._queue_title(name),
                        **social_meta,
                    }
                )
        return files
