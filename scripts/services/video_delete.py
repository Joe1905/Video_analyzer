"""Domain behavior for deleting a video and its fixed output directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class VideoDeleteService:
    """Delete pre-validated video artifacts without cross-domain side effects."""

    def __init__(
        self,
        videos_dir: Path,
        output_dir: Path,
        rmtree: Callable[[Path], None],
    ) -> None:
        self._videos_dir = videos_dir
        self._output_dir = output_dir
        self._rmtree = rmtree

    def delete(self, filename: str) -> dict[str, Any]:
        video_path = self._videos_dir / filename
        output_path = self._output_dir / filename
        deleted_video = False
        deleted_output = False
        if video_path.is_file():
            video_path.unlink()
            deleted_video = True
        if output_path.is_dir():
            self._rmtree(output_path)
            deleted_output = True
        return {
            "filename": filename,
            "deleted_video": deleted_video,
            "deleted_output": deleted_output,
        }
