"""Analyzer result payload assembly without HTTP dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class VideoResultService:
    """Read the existing analyzer artifacts into the legacy result schema."""

    def __init__(
        self,
        root: Path,
        output_dir_for_filename: Callable[[str], Path],
        read_json_file: Callable[[Path], Any],
    ) -> None:
        self._root = root
        self._output_dir_for_filename = output_dir_for_filename
        self._read_json_file = read_json_file

    def payload_for(self, filename: str) -> dict[str, Any]:
        output_dir = self._output_dir_for_filename(filename)
        analysis = self._read_json_file(output_dir / "analysis.json")
        social_context = self._read_json_file(output_dir / "social_context.json")
        return {
            "filename": filename,
            "status": "saved",
            "output_dir": str(output_dir.relative_to(self._root)),
            "analysis_mode": analysis.get("processing_mode") if isinstance(analysis, dict) else None,
            "analysis": analysis,
            "analysis_zh": self._read_json_file(output_dir / "analysis_zh.json"),
            "direct_analysis": self._read_json_file(output_dir / "direct_analysis.json"),
            "direct_analysis_zh": self._read_json_file(output_dir / "direct_analysis_zh.json"),
            "audit_result": self._read_json_file(output_dir / "audit_result.json"),
            "audit_result_zh": self._read_json_file(output_dir / "audit_result_zh.json"),
            "direct_audit_result": self._read_json_file(output_dir / "direct_audit_result.json"),
            "direct_audit_result_zh": self._read_json_file(output_dir / "direct_audit_result_zh.json"),
            "feedback_result": self._read_json_file(output_dir / "feedback_result.json"),
            "feedback_result_zh": self._read_json_file(output_dir / "feedback_result_zh.json"),
            "direct_feedback_result": self._read_json_file(output_dir / "direct_feedback_result.json"),
            "direct_feedback_result_zh": self._read_json_file(output_dir / "direct_feedback_result_zh.json"),
            "social_context": social_context,
            "social_insights": self._read_json_file(output_dir / "social_insights.json"),
            "log": [],
        }
