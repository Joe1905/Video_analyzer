#!/usr/bin/env python3
"""Run with: docker compose -p short-video-analyzer run --rm analyzer python scripts/test_hot_report_contract.py"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import deepseek_postprocess
import hot_video_report as report


def main() -> int:
    payload = {key: [] for key in report.REQUIRED_DAILY_REPORT_KEYS}
    payload["summary"] = "fixture"
    assert report._validate_daily_report_shape(payload) == payload
    assert "# fixture" in report._markdown_from_report(payload)
    try:
        report._validate_daily_report_shape({"summary": "missing"})
    except ValueError:
        pass
    else:
        raise AssertionError("missing required report keys were accepted")

    captured = {}
    original_post = deepseek_postprocess.requests.post
    original_record = deepseek_postprocess.record_api_call
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "{}"}}]}
    def fake_post(*args, **kwargs):
        captured.update(kwargs["json"])
        return Response()
    deepseek_postprocess.requests.post = fake_post
    deepseek_postprocess.record_api_call = lambda *args, **kwargs: None
    try:
        deepseek_postprocess.call_deepseek("fixture", "fixture", "https://example.test/v1", "fixture", 100, reasoning_effort="disabled")
    finally:
        deepseek_postprocess.requests.post = original_post
        deepseek_postprocess.record_api_call = original_record
    assert captured.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in captured
    print("hot report contract and DeepSeek parameters: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
