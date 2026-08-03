#!/usr/bin/env python3
"""Run with: docker compose -p short-video-analyzer run --rm analyzer python scripts/test_video_analyze_timeout.py"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import tools


def main() -> int:
    original_run = tools.subprocess.run
    original_output_dir = tools._video_output_dir
    with tempfile.TemporaryDirectory() as directory:
        output_dir = Path(directory) / "video-output"

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(
                args[0],
                kwargs["timeout"],
                output="phase=whisper\ntranscribing audio",
                stderr="phase=qwen\nwaiting for response",
            )

        tools.subprocess.run = fake_run
        tools._video_output_dir = lambda filename: output_dir
        try:
            try:
                tools._run_video_analyze("fixture.mp4", timeout_seconds=123)
            except RuntimeError as exc:
                assert "timed out after 123 seconds" in str(exc)
            else:
                raise AssertionError("timeout should be reported as a video analysis failure")
            diagnostic = (output_dir / "analysis_timeout.log").read_text(encoding="utf-8")
            assert "timeout_seconds=123" in diagnostic
            assert "phase=whisper" in diagnostic
            assert "phase=qwen" in diagnostic
        finally:
            tools.subprocess.run = original_run
            tools._video_output_dir = original_output_dir
    print("video analysis timeout diagnostics: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
