"""Synchronous analysis-artifact translation without HTTP dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping


class TranslationCommandError(RuntimeError):
    """The existing HTTP-visible failure from the translation subprocess."""


@dataclass(frozen=True)
class TranslationRequest:
    filename: str
    tab: str
    source_path: Path
    output_path: Path


class TranslateService:
    def __init__(
        self,
        root: Path,
        scripts_dir: Path,
        output_dir_for_filename: Callable[[str], Path],
        safe_filename: Callable[[str], str],
        run_factory: Callable[..., Any],
        environ: Mapping[str, str],
    ) -> None:
        self._root = root
        self._scripts_dir = scripts_dir
        self._output_dir_for_filename = output_dir_for_filename
        self._safe_filename = safe_filename
        self._run_factory = run_factory
        self._environ = environ

    def prepare_request(self, *, filename: str, tab: str, source_mode: str) -> TranslationRequest:
        filename = self._safe_filename(filename)
        if source_mode not in {"standard", "direct"}:
            raise ValueError("analysis_source must be standard or direct")
        if tab not in {"content", "direct", "audit", "feedback"}:
            raise ValueError("tab must be content, direct, audit, or feedback")

        files = {
            "content": ("analysis.json", "analysis_zh.json"),
            "direct": ("direct_analysis.json", "direct_analysis_zh.json"),
            "audit": ("audit_result.json", "audit_result_zh.json"),
            "feedback": ("feedback_result.json", "feedback_result_zh.json"),
        }
        if source_mode == "direct":
            if tab == "audit":
                files["audit"] = ("direct_audit_result.json", "direct_audit_result_zh.json")
            elif tab == "feedback":
                files["feedback"] = ("direct_feedback_result.json", "direct_feedback_result_zh.json")
        source_name, output_name = files[tab]
        output_dir = self._output_dir_for_filename(filename)
        source_path = output_dir / source_name
        if not source_path.is_file():
            raise ValueError(f"{source_name} not found for {filename}")
        return TranslationRequest(
            filename=filename,
            tab=tab,
            source_path=source_path,
            output_path=output_dir / output_name,
        )

    def translate(self, request: TranslationRequest) -> dict[str, str]:
        try:
            self._run_factory(
                [
                    "python",
                    str(self._scripts_dir / "translate_analysis.py"),
                    str(request.source_path),
                    "--output",
                    str(request.output_path),
                ],
                cwd=self._root,
                check=True,
                env=dict(self._environ),
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise TranslationCommandError(message or "Translation failed") from exc
        return {"status": "translated", "filename": request.filename, "tab": request.tab}
