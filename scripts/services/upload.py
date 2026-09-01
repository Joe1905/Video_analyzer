"""Local video upload orchestration without HTTP dependencies."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any, Callable


class UploadService:
    def __init__(
        self,
        videos_dir: Path,
        safe_filename: Callable[[str], str],
        ensure_analyzer_media_or_delete: Callable[[Path], None],
        register_video: Callable[..., Any],
        video_source_hidden: Callable[[str], bool],
        make_web_manual_visible: Callable[[str, str, str], None],
        start_social_context_job: Callable[..., bool],
    ) -> None:
        self._videos_dir = videos_dir
        self._safe_filename = safe_filename
        self._ensure_analyzer_media_or_delete = ensure_analyzer_media_or_delete
        self._register_video = register_video
        self._video_source_hidden = video_source_hidden
        self._make_web_manual_visible = make_web_manual_visible
        self._start_social_context_job = start_social_context_job

    def upload(self, file_items: list[Any], *, source: str) -> dict[str, list[dict[str, Any]]]:
        self._videos_dir.mkdir(parents=True, exist_ok=True)
        files = []
        errors = []
        for file_item in file_items:
            original_name = str(getattr(file_item, "filename", ""))
            try:
                filename = self._safe_filename(original_name)
                target = self._videos_dir / filename
                with target.open("wb") as file:
                    shutil.copyfileobj(file_item.file, file)
                self._ensure_analyzer_media_or_delete(target)
                self._register_video(
                    video_id=filename,
                    platform="local",
                    filename=filename,
                    title=filename,
                    source=source,
                    hidden_from_analyzer=self._video_source_hidden(source),
                )
                self._make_web_manual_visible(source, "local", filename)
                files.append({"filename": filename, "size": target.stat().st_size})
                self._start_social_context_job(filename, generate_insights=False)
            except Exception as exc:
                errors.append({"filename": original_name, "error": str(exc)})
        return {"files": files, "errors": errors}
