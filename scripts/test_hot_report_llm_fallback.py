#!/usr/bin/env python3
"""Run with: docker compose -p short-video-analyzer run --rm analyzer python scripts/test_hot_report_llm_fallback.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import hot_video_report as report


def main() -> int:
    previous_key = os.environ.get("DEEPSEEK_API_KEY")
    original_call = report.call_deepseek
    os.environ["DEEPSEEK_API_KEY"] = "fixture"
    report.call_deepseek = lambda **kwargs: {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
    try:
        videos = [{"report_rank": 1, "title": "fixture", "metrics": {}, "analysis": {"summary": "fixture"}, "insight": {"one_sentence": "ok"}}]
        result, markdown = report._generate_daily_summary("2026-08-01", videos)
        assert set(report.REQUIRED_DAILY_REPORT_KEYS).issubset(result)
        assert result["generation"]["mode"] == "local_fallback"
        assert "本地降级" in markdown
    finally:
        report.call_deepseek = original_call
        if previous_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous_key
    print("hot report LLM fallback: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
