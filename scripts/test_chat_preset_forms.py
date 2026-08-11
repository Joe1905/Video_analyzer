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
                for field in definition["fields"]:
                    self.assertTrue(field["name"])
                    self.assertTrue(field["label"])
                    self.assertIn("placeholder", field)

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


if __name__ == "__main__":
    unittest.main()
