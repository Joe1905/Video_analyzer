#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import viral_elements
import viral_feishu_sync


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
        self.assertEqual(17.0, review["heat_score"])
        review["elements"][0]["approved"] = True
        review = viral_elements.validate_review(review)
        with tempfile.TemporaryDirectory() as tmp:
            store = viral_elements.ViralElementStore(Path(tmp))
            store.save_review("demo.mp4", review)
            library = store.list_library()
        self.assertEqual(1, len(library))
        self.assertEqual("demo.mp4", library[0]["filename"])
        self.assertEqual("待审核", review["review_status"])

    def test_pdf_composer_rules_are_applied_and_duration_is_optional(self):
        captured = {}

        def fake_model(prompt, max_tokens=6000):
            captured["prompt"] = prompt
            return {"versions": [{
                "id": key, "strategy": "策略", "hook": "Hook", "duration": "10秒",
                "cta": "Buy now", "video_prompt": "Create a product video.",
                "source_elements": ["source.mp4:hook"],
                "shots": [
                    {"time_range": "00:00-00:05", "visual": "A", "voiceover": "A", "on_screen_text": "A", "selling_point": "B"},
                    {"time_range": "00:05-00:10", "visual": "B", "voiceover": "B", "on_screen_text": "B", "selling_point": "B"},
                ],
            } for key in ("V1", "V2", "V3")]}

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

    def test_script_approval_is_human_controlled_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = viral_elements.ViralElementStore(Path(tmp))
            saved = store.save_scripts("demo.mp4", {}, {"versions": [{"id": "V1"}]})
            self.assertEqual("待审核", saved["scripts"]["workflow"]["status"])
            approved = store.update_script_approval(saved["id"], {
                "status": "已通过", "reviewer": "Alice", "approval_comment": "采用V2", "final_version": "V2"
            })
            self.assertEqual("V2", approved["scripts"]["workflow"]["final_version"])
            self.assertEqual("Alice", store.latest_scripts("demo.mp4")["scripts"]["workflow"]["reviewer"])

    def test_script_validator_rejects_broken_timeline(self):
        payload = {"versions": [{
            "id": key, "strategy": "S", "hook": "H", "duration": "10秒", "cta": "C",
            "video_prompt": "Prompt", "source_elements": [],
            "shots": [{"time_range": "00:02-00:10"}],
        } for key in ("V1", "V2", "V3")]}
        errors = viral_elements.validate_generated_scripts(payload, {})
        self.assertTrue(any("时间轴不连续" in item for item in errors))

    def test_pdf_feishu_field_mapping_covers_elements_and_scripts(self):
        review = viral_elements.validate_review({
            "filename": "demo.mp4", "source_channel": "B", "review_status": "已通过",
            "reviewer": "刘鹏飞", "review_comment": "通过",
            "source_metrics": {"views": 100, "likes": 10, "favorites": 2, "comments": 1},
            "elements": [{"key": key, "value": label, "approved": True}
                         for _, key, label in viral_elements.ELEMENT_DEFS],
        })
        fields = viral_feishu_sync.review_fields(review, "刘鹏飞")
        self.assertEqual("已通过", fields["元素审核状态"])
        self.assertEqual("视频类型", fields["视频类型"])
        self.assertTrue(fields["豆包Work.模型输出B"])
        self.assertEqual("demo.mp4", fields["本地文件ID"])
        saved = {"id": 7, "filename": "demo.mp4", "brief": {
            "product": "产品", "selling_points": "卖点", "audience": "人群",
            "supplemental_requirements": "补充",
        }, "scripts": {"versions": [{"id": key, "video_prompt": f"{key} prompt",
            "strategy": "策略", "source_elements": ["demo.mp4:hook"]} for key in ("V1", "V2", "V3")],
            "workflow": {"status": "待审核"}}, "created_at": "now"}
        script = viral_feishu_sync.script_fields(saved, review, "刘鹏飞")
        self.assertEqual("V2 prompt", script["V2视频提示词"])
        self.assertEqual("demo.mp4:hook", script["采用元素"])

    def test_native_feishu_people_video_and_relation_values(self):
        review = viral_elements.validate_review({
            "filename": "演示 视频.mp4", "reviewer": "刘鹏飞",
            "source_url": "https://example.com/video", "elements": [],
        })
        fields = viral_feishu_sync.review_fields(
            review, "刘鹏飞", "ou_owner", "http://127.0.0.1:4003/video"
        )
        self.assertEqual([{"id": "ou_owner"}], fields["负责人"])
        self.assertEqual([{"id": "ou_owner"}], fields["审核人"])
        self.assertEqual("http://127.0.0.1:4003/video/%E6%BC%94%E7%A4%BA%20%E8%A7%86%E9%A2%91.mp4", fields["对标视频"])
        saved = {"id": 1, "scripts": {"versions": [], "workflow": {}}}
        script = viral_feishu_sync.script_fields(saved, review, "刘鹏飞", "ou_owner", ["rec1"])
        self.assertEqual(["rec1"], script["采用元素"])

    def test_feishu_record_id_is_persisted_for_update(self):
        class Client:
            def __init__(self): self.created = 0; self.updated = 0
            def create_bitable_record(self, payload): self.created += 1; return {"recordId": "rec1"}
            def update_bitable_record(self, payload): self.updated += 1; return {"recordId": "rec1"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "VIRAL_FEISHU_ELEMENTS_URL": "https://example.feishu.cn/base/app?table=tbl1",
            "VIRAL_FEISHU_SCRIPTS_URL": "https://example.feishu.cn/base/app?table=tbl2",
        }):
            store = viral_elements.ViralElementStore(Path(tmp)); client = Client()
            sync = viral_feishu_sync.ViralFeishuSync(store, client)
            review = viral_elements.validate_review({"filename": "demo.mp4", "elements": []})
            first = sync.sync_review(review); second = sync.sync_review(review)
        self.assertEqual("rec1", first["record_id"])
        self.assertEqual("updated", second["action"])
        self.assertEqual((1, 1), (client.created, client.updated))


if __name__ == "__main__":
    unittest.main()
