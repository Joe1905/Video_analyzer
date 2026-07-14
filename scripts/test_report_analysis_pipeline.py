#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", types.SimpleNamespace(post=None))
sys.modules.setdefault("api_cache", types.SimpleNamespace(record_api_call=lambda *args, **kwargs: None))

from standardize_analysis import numeric_timestamp, timeline_rows
from translate_analysis import (
    looks_truncated_translation,
    translate_analysis_payload,
    validate_analysis_translation,
)


class TimelineStandardizationTests(unittest.TestCase):
    def test_structured_and_legacy_timestamps_are_preserved(self) -> None:
        rows = timeline_rows(
            [
                {"response": "Frame 0", "frame_number": 0, "timestamp_seconds": 7.43},
                {"response": "Frame 1 (15.50s): next scene"},
                {"response": "第2帧（25.25秒）：结束"},
            ]
        )
        self.assertEqual([row["timestamp_seconds"] for row in rows], [7.43, 15.5, 25.25])
        self.assertEqual([row["frame_number"] for row in rows], [0, 1, 2])
        self.assertEqual(numeric_timestamp({}, "画面3（37.17秒）"), 37.17)


class TranslationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "schema_version": "1.1",
            "metadata": {"duration_seconds": 31.5, "analysis_prompt": "English prompt"},
            "summary": "A complete summary.",
            "transcript": {
                "text": "Spoken words.",
                "segments": [{"start": 0, "words": [{"word": "Spoken"}]}],
            },
            "timeline": [
                {
                    "index": 0,
                    "frame_number": 0,
                    "timestamp_seconds": 0.0,
                    "time_range": "0.000s",
                    "visual": "A complete frame description.",
                }
            ],
            "visual_evidence": [
                {
                    "index": 0,
                    "frame_number": 0,
                    "timestamp_seconds": 0.0,
                    "description": "A complete frame description.",
                }
            ],
        }

    def test_translation_changes_only_display_text(self) -> None:
        def fake_translate(_key, _url, _model, text, _chars, _tokens):
            return f"译文：{text}。"

        with patch("translate_analysis._translate_display_text", side_effect=fake_translate):
            translated = translate_analysis_payload("key", "url", "model", self.source)
        self.assertEqual(translated["timeline"][0]["timestamp_seconds"], 0.0)
        self.assertEqual(translated["timeline"][0]["frame_number"], 0)
        self.assertEqual(translated["transcript"]["segments"], self.source["transcript"]["segments"])
        self.assertEqual(
            translated["visual_evidence"][0]["description"],
            translated["timeline"][0]["visual"],
        )

    def test_validation_rejects_changed_timestamps(self) -> None:
        translated = copy.deepcopy(self.source)
        translated["timeline"][0]["timestamp_seconds"] = 9.0
        with self.assertRaisesRegex(ValueError, "structural field"):
            validate_analysis_translation(self.source, translated)

    def test_compact_translation_of_nonterminal_source_is_not_truncated(self) -> None:
        source = ("Detailed visual continuity and composition notes for the following moment " * 8).strip()
        translated = ("这是完整的画面连续性与构图说明，描述接下来的时刻。" * 7).rstrip("。")
        self.assertGreater(len(translated), len(source) * 0.18)
        self.assertLess(len(translated), len(source) * 0.35)
        self.assertFalse(looks_truncated_translation(source, translated))

    def test_extremely_short_translation_is_rejected(self) -> None:
        source = "A complete source sentence with substantial detail. " * 8
        self.assertTrue(looks_truncated_translation(source, "过短译文"))


if __name__ == "__main__":
    unittest.main()
