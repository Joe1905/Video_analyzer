#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import viral_elements


class ViralElementsTests(unittest.TestCase):
    def test_analysis_is_normalized_to_exact_schema(self):
        fake = {"summary": "测试", "elements": [{
            "group": "structure", "key": "hook", "label": "wrong",
            "value": "结果前置", "confidence": 1.7, "evidence": "首帧展示结果",
            "time_range": "00:00-00:02",
        }]}
        with patch.object(viral_elements, "_model_json", return_value=fake):
            result = viral_elements.analyze_elements("demo.mp4", {"summary": "demo"})
        self.assertEqual(18, len(result["elements"]))
        hook = next(item for item in result["elements"] if item["key"] == "hook")
        self.assertEqual("Hook", hook["label"])
        self.assertEqual(1.0, hook["confidence"])
        self.assertEqual("结果前置", hook["value"])

    def test_review_round_trip_and_script_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = viral_elements.ViralElementStore(Path(tmp))
            review = viral_elements.validate_review({
                "filename": "demo.mp4",
                "elements": [{"key": "hook", "value": "提问", "approved": True}],
            })
            saved = store.save_review("demo.mp4", review)
            self.assertEqual(18, len(saved["elements"]))
            self.assertTrue(next(x for x in saved["elements"] if x["key"] == "hook")["approved"])
            store.save_scripts("demo.mp4", {"product": "A"}, {"versions": [{"id": "V1"}]})
            self.assertEqual("V1", store.latest_scripts("demo.mp4")["scripts"]["versions"][0]["id"])

    def test_script_generation_requires_all_brief_fields(self):
        with self.assertRaisesRegex(viral_elements.ViralElementError, "核心卖点"):
            viral_elements.generate_scripts(
                "demo.mp4", {"elements": [{"key": "hook", "approved": True}]},
                {"product": "A", "audience": "B", "duration": "30s"},
            )

    def test_heat_score_and_approved_library(self):
        source = {"metadata": {"views": 1000, "likes": 100, "comments": 10, "favorites": 20, "shares": 5}}
        with patch.object(viral_elements, "_model_json", return_value={"elements": []}):
            review = viral_elements.analyze_elements("demo.mp4", source)
        self.assertEqual(18.0, review["heat_score"])
        review["elements"][0]["approved"] = True
        review = viral_elements.validate_review(review)
        with tempfile.TemporaryDirectory() as tmp:
            store = viral_elements.ViralElementStore(Path(tmp))
            store.save_review("demo.mp4", review)
            library = store.list_library()
        self.assertEqual(1, len(library))
        self.assertEqual("demo.mp4", library[0]["filename"])
        self.assertEqual("已审核", review["review_status"])

    def test_pdf_composer_rules_are_applied_and_duration_is_optional(self):
        captured = {}

        def fake_model(prompt, max_tokens=6000):
            captured["prompt"] = prompt
            return {"versions": [{"id": key} for key in ("V1", "V2", "V3")]}

        with patch.object(viral_elements, "_model_json", side_effect=fake_model):
            result = viral_elements.generate_scripts(
                "demo.mp4",
                {"elements": [{"key": "hook", "approved": True, "value": "结果前置"}]},
                {"product": "A", "selling_points": "B", "audience": "C"},
                [{"key": "hook", "approved": True, "filename": "source.mp4", "value": "结果前置"}],
            )
        self.assertEqual("", result["brief"]["duration"])
        self.assertIn("需求匹配40", captured["prompt"])
        self.assertIn("同一来源原片每个脚本版本最多贡献2项", captured["prompt"])
        self.assertIn("不得覆盖人工填写的审批人", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
