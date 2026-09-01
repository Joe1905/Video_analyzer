"""Analyze request orchestration without HTTP dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Callable


@dataclass(frozen=True)
class AnalyzeRequest:
    filename: str
    postprocess: bool
    reset_output: bool
    analysis_mode: str
    analysis_prompt: str


class AnalyzeService:
    def __init__(
        self,
        videos_dir: Path,
        output_dir_for_filename: Callable[[str], Path],
        safe_filename: Callable[[str], str],
        queue_enqueue: Callable[[str, str], None],
    ) -> None:
        self._videos_dir = videos_dir
        self._output_dir_for_filename = output_dir_for_filename
        self._safe_filename = safe_filename
        self._queue_enqueue = queue_enqueue

    def prepare_request(
        self,
        *,
        filename: str,
        postprocess: bool,
        reset_output: bool,
        analysis_mode: str,
        analysis_prompt: str,
    ) -> AnalyzeRequest:
        filename = self._safe_filename(filename)
        if analysis_mode not in {"analyzer", "direct_video"}:
            raise ValueError("analysis_mode must be analyzer or direct_video")
        if len(analysis_prompt) > 12000:
            raise ValueError("analysis_prompt is too long")
        if not (self._videos_dir / filename).is_file():
            raise ValueError(f"Video file not found: {filename}")
        return AnalyzeRequest(
            filename=filename,
            postprocess=postprocess,
            reset_output=reset_output,
            analysis_mode=analysis_mode,
            analysis_prompt=analysis_prompt,
        )

    @staticmethod
    def _reset_analysis_output(output_dir: Path, analysis_mode: str) -> None:
        if analysis_mode == "direct_video":
            output_dir.mkdir(parents=True, exist_ok=True)
            for output_name in ("direct_analysis.json", "direct_analysis_zh.json"):
                output_path = output_dir / output_name
                if output_path.is_file():
                    output_path.unlink()
        elif output_dir.is_dir():
            shutil.rmtree(output_dir)

    def enqueue(self, request: AnalyzeRequest) -> dict[str, Any]:
        output_dir = self._output_dir_for_filename(request.filename)
        if request.reset_output:
            self._reset_analysis_output(output_dir, request.analysis_mode)

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "analysis_mode.txt").write_text(request.analysis_mode, encoding="utf-8")
        if request.analysis_prompt:
            (output_dir / "analysis_prompt.txt").write_text(request.analysis_prompt, encoding="utf-8")

        queued = ["analyze"]
        self._queue_enqueue(request.filename, "analyze")
        if request.postprocess:
            self._queue_enqueue(request.filename, "report")
            queued.append("report")
        return {"status": "queued", "filename": request.filename, "queued": queued}
