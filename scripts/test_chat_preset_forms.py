#!/usr/bin/env python3
"""Contracts for the data-driven official preset composer forms."""

import json
import re
import unittest
from unittest.mock import patch

try:
    from scripts import web_app
    from scripts.chat_preset_forms import CHUHAIJIANG_PRESET_FORMS, SELLERSPRITE_PRESET_FORMS
except ModuleNotFoundError:
    import web_app
    from chat_preset_forms import CHUHAIJIANG_PRESET_FORMS, SELLERSPRITE_PRESET_FORMS


class TestChatPresetForms(unittest.TestCase):
    @staticmethod
    def render(provider: str, path: str) -> str:
        captured = []
        with patch.object(web_app, "text_response", side_effect=lambda *_args: captured.append(_args[2])):
            web_app.serve_chat_template(None, provider, path)
        return captured[0]

    def test_every_sellersprite_official_preset_has_one_form(self):
        self.assertEqual(set(SELLERSPRITE_PRESET_FORMS), set(web_app.SELLERSPRITE_OFFICIAL_PRESETS))
        self.assertEqual(len(SELLERSPRITE_PRESET_FORMS), 27)

    def test_all_eight_chuhaijiang_scenes_have_multi_field_forms(self):
        self.assertEqual(len(CHUHAIJIANG_PRESET_FORMS), 8)
        for form_id, definition in CHUHAIJIANG_PRESET_FORMS.items():
            with self.subTest(form_id=form_id):
                self.assertTrue(form_id.startswith("chuhaijiang/"))
                self.assertGreaterEqual(len(definition["fields"]), 4)
                self.assertTrue(any(field["required"] for field in definition["fields"]))

    def test_each_field_has_a_stable_name_label_and_placeholder(self):
        all_forms = {**SELLERSPRITE_PRESET_FORMS, **CHUHAIJIANG_PRESET_FORMS}
        for form_id, definition in all_forms.items():
            with self.subTest(form_id=form_id):
                self.assertTrue(definition["label"])
                self.assertTrue(definition["prompt"])
                names = [field["name"] for field in definition["fields"]]
                self.assertEqual(len(names), len(set(names)))
                self.assertEqual(names[-1], "additional_notes")
                self.assertEqual(definition["fields"][-1]["label"], "补充说明")
                self.assertFalse(definition["fields"][-1]["required"])
                self.assertTrue(definition["fields"][-1]["full"])
                for field in definition["fields"]:
                    self.assertTrue(field["name"])
                    self.assertTrue(field["label"])
                    self.assertIn("placeholder", field)
                    self.assertTrue(field["empty_meaning"])

    def test_semantic_fields_keep_friendly_labels_and_canonical_execution_values(self):
        seller_market = SELLERSPRITE_PRESET_FORMS["comprehensive/product-research"]["fields"][0]
        chuhaijiang_market = CHUHAIJIANG_PRESET_FORMS["chuhaijiang/product-selection"]["fields"][0]
        self.assertEqual(seller_market["parameter"], "marketplace")
        self.assertIn({"value": "US", "label": "美国"}, seller_market["options"])
        self.assertEqual(chuhaijiang_market["parameter"], "country")
        self.assertIn({"value": "US", "label": "美国"}, chuhaijiang_market["options"])

    def test_rendered_pages_inject_only_their_own_form_definitions(self):
        amazon = self.render("amazon", "/amazon")
        chuhaijiang = self.render("chuhaijiang", "/chuhaijiang")
        home = self.render("home", "/")
        pattern = r"const CHAT_PRESET_FORMS=(\{.*?\});"
        amazon_forms = json.loads(re.search(pattern, amazon).group(1))
        chuhaijiang_forms = json.loads(re.search(pattern, chuhaijiang).group(1))
        home_forms = json.loads(re.search(pattern, home).group(1))
        self.assertEqual(set(amazon_forms), set(SELLERSPRITE_PRESET_FORMS))
        self.assertEqual(set(chuhaijiang_forms), set(CHUHAIJIANG_PRESET_FORMS))
        self.assertEqual(home_forms, {})

    def test_server_builds_safe_structured_preset_summary(self):
        metadata = web_app.chat_official_preset_metadata(
            "amazon",
            "comprehensive/product-research",
            """请使用卖家精灵官方 Skill「智能选品助手」完成选品研究。

用户意图：
- 亚马逊站点：美国
- 关键词 / 类目：AI智能翻译机
- 价格区间：用户未指定，表示无额外限制。
- 最低月销量：用户未指定，表示无额外限制。
- 最低评分：4.2
- 配送方式：不限
- 补充说明：用户没有额外补充，以其他表单项表达的意图为准。

执行语义：
- marketplace 使用标准值 \"US\"。""",
        )
        self.assertEqual(metadata["label"], "智能选品助手")
        self.assertEqual(
            metadata["fields"],
            [
                {"label": "亚马逊站点", "value": "美国"},
                {"label": "关键词 / 类目", "value": "AI智能翻译机"},
                {"label": "价格区间", "value": "未填写"},
                {"label": "最低月销量", "value": "未填写"},
                {"label": "最低评分", "value": "4.2"},
                {"label": "配送方式", "value": "不限"},
                {"label": "补充说明", "value": "未填写"},
            ],
        )
        self.assertIsNone(
            web_app.chat_official_preset_metadata(
                "amazon", "unknown-preset", "用户意图：\n- 关键词 / 类目：伪造"
            )
        )

    def test_form_submission_keeps_sellersprite_boundary_and_chuhaijiang_normal_chat(self):
        amazon = self.render("amazon", "/amazon")
        chuhaijiang = self.render("chuhaijiang", "/chuhaijiang")
        self.assertIn('data-official-preset-id="comprehensive/product-research"', amazon)
        self.assertIn('data-preset-form-id="comprehensive/product-research"', amazon)
        self.assertNotIn("data-official-preset-id=", chuhaijiang)
        self.assertIn('data-preset-form-id="chuhaijiang/product-selection"', chuhaijiang)
        template = (web_app.SCRIPTS_DIR / "static" / "chat.html").read_text(encoding="utf-8")
        self.assertIn("askPayload.officialPresetId=S.officialPresetId", template)
        self.assertIn("function buildPresetPrompt()", template)
        self.assertIn("请填写${control.dataset.presetLabel}", template)
        self.assertIn('"用户意图："', template)
        self.assertIn('"执行语义："', template)
        self.assertIn("未指定项不得自行臆造具体值", template)
        self.assertIn("control.dataset.presetEmptyMeaning", template)
        self.assertNotIn('input.placeholder="补充说明（可选）"', template)
        styles = (web_app.SCRIPTS_DIR / "static" / "assets" / "ui-system.css").read_text(encoding="utf-8")
        self.assertIn(".input-bar.has-preset-form #input", styles)
        self.assertIn(".input-bar.has-preset-form .expand-input-btn", styles)
        self.assertIn('id="presetFormCancel"', template)
        self.assertIn('id="presetFormSubmit"', template)
        self.assertNotIn('id="officialPresetChip"', template)
        self.assertIn(".input-bar.has-preset-form > .attach-button", styles)
        self.assertIn(".input-bar.has-preset-form > .official-workflow-composer-button", styles)
        self.assertIn(".input-bar.has-preset-form > .send-button", styles)
        self.assertIn("S.presetInputDraft=draft", template)
        enter_form = template.split("function enterPresetForm", 1)[1].split("function buildPresetPrompt", 1)[0]
        self.assertNotIn('input.value=""', enter_form)
        self.assertIn("function presetMessageFields(message)", template)
        self.assertIn('bubble.replaceChildren()', template)
        self.assertIn(".preset-message-fields", styles)


if __name__ == "__main__":
    unittest.main()
