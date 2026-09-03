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


if __name__ == "__main__":
    unittest.main()
