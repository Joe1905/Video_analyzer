"""DeepSeek postprocess request orchestration without HTTP dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class PostprocessRequest:
    filename: str
    analysis_prompt: str
    analysis_source: str
    output_dir: Path
    audit_names: tuple[str, str]
    needs_direct_analysis: bool


class PostprocessService:
    def __init__(
        self,
        output_dir_for_filename: Callable[[str], Path],
        safe_filename: Callable[[str], str],
        queue_enqueue: Callable[[str, str], None],
    ) -> None:
        self._output_dir_for_filename = output_dir_for_filename
        self._safe_filename = safe_filename
        self._queue_enqueue = queue_enqueue

    def prepare_request(
        self,
        *,
        filename: str,
        analysis_prompt: str,
        analysis_source: str,
    ) -> PostprocessRequest:
        filename = self._safe_filename(filename)
        if analysis_source not in {"standard", "direct"}:
            raise ValueError("analysis_source must be standard or direct")

        output_dir = self._output_dir_for_filename(filename)
        analysis_name = "direct_analysis.json" if analysis_source == "direct" else "analysis.json"
        audit_names = (
            ("direct_audit_result.json", "direct_audit_result_zh.json")
            if analysis_source == "direct"
            else ("audit_result.json", "audit_result_zh.json")
        )
        needs_direct_analysis = not (output_dir / analysis_name).is_file()
        if needs_direct_analysis and analysis_source != "direct":
            raise ValueError(f"{analysis_name} not found for {filename}")
        return PostprocessRequest(
            filename=filename,
            analysis_prompt=analysis_prompt,
            analysis_source=analysis_source,
            output_dir=output_dir,
            audit_names=audit_names,
            needs_direct_analysis=needs_direct_analysis,
        )

    def enqueue(self, request: PostprocessRequest) -> dict[str, Any]:
        if request.needs_direct_analysis:
            request.output_dir.mkdir(parents=True, exist_ok=True)
            (request.output_dir / "analysis_mode.txt").write_text("direct_video", encoding="utf-8")
            (request.output_dir / "report_source.txt").write_text("direct", encoding="utf-8")
            self._queue_enqueue(request.filename, "analyze")
            self._queue_enqueue(request.filename, "report")
            return {
                "status": "queued",
                "filename": request.filename,
                "queued": ["analyze", "report"],
            }

        request.output_dir.mkdir(parents=True, exist_ok=True)
        (request.output_dir / "report_source.txt").write_text(request.analysis_source, encoding="utf-8")
        if request.analysis_prompt:
            (request.output_dir / "analysis_prompt.txt").write_text(request.analysis_prompt, encoding="utf-8")
        for report_name in request.audit_names:
            report_path = request.output_dir / report_name
            if report_path.is_file():
                report_path.unlink()
        self._queue_enqueue(request.filename, "report")
        return {"status": "queued", "filename": request.filename}
