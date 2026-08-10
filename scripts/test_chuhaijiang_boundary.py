"""Focused contract tests for the isolated Chuhaijiang provider."""
import unittest
from pathlib import Path

from chuhaijiang_official_skill import (
    OFFICIAL_TOOL_NAMES,
    OFFICIAL_SKILL_VERSION,
    is_high_risk_tool,
    load_official_skill_prompt,
    verify_official_skill,
)


class TestChuhaijiangBoundary(unittest.TestCase):
    def test_complete_official_skill_and_19_tool_catalog(self):
        verify_official_skill()
        self.assertEqual(OFFICIAL_SKILL_VERSION, "1.2.6")
        self.assertEqual(len(OFFICIAL_TOOL_NAMES), 19)
        self.assertIn("social_seller", OFFICIAL_TOOL_NAMES)
        self.assertIn("references/social-media.md", load_official_skill_prompt())

    def test_high_risk_gate_is_conservative(self):
        self.assertTrue(is_high_risk_tool("ai_generate", {}))
        self.assertTrue(is_high_risk_tool("social_publish", {}))
        self.assertTrue(is_high_risk_tool("canvas", {"action": "generate"}))
        self.assertFalse(is_high_risk_tool("canvas", {"action": "load"}))
        self.assertFalse(is_high_risk_tool("account_info", {}))

    def test_legacy_workflow_presentation_has_no_preset_id(self):
        web_app = Path(__file__).with_name("web_app.py").read_text(encoding="utf-8")
        chat_html = Path(__file__).with_name("static").joinpath("chat.html").read_text(encoding="utf-8")
        self.assertIn('or provider == "chuhaijiang"', web_app)
        self.assertIn('data-chuhaijiang-prompt=', web_app)
        self.assertIn('data-prompt="{prompt}"', web_app)
        self.assertIn('data-chuhaijiang-prompt', chat_html)


if __name__ == "__main__":
    unittest.main()
