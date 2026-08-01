"""Test FastMoss official presets registration, tool whitelists, and boundary isolation."""
import os
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from scripts.chat_session import ChatStore, Message, load_sessions_from_disk, save_sessions_to_disk
from scripts.web_app import (
    FASTMOSS_OFFICIAL_PRESETS,
    FASTMOSS_LABEL_TO_PRESET_ID,
    fastmoss_official_skill_route,
    fastmoss_official_skill_tool_ids,
    render_chat_official_workflow_modal,
    render_chat_quick_actions,
    CHAT_PROVIDER_UI,
    fallback_chat_session_title,
    select_official_fastmoss_skill_prompt,
)
from scripts.fastmoss_lightweight_skill import load_lightweight_fastmoss_skill_prompt


class TestFastMossPresetsBoundary(unittest.TestCase):
    def setUp(self):
        self._previous_v2_mode = os.environ.get("FASTMOSS_PRODUCT_SCOUT_V2_MODE")
        os.environ["FASTMOSS_PRODUCT_SCOUT_V2_MODE"] = "off"

    def tearDown(self):
        if self._previous_v2_mode is None:
            os.environ.pop("FASTMOSS_PRODUCT_SCOUT_V2_MODE", None)
        else:
            os.environ["FASTMOSS_PRODUCT_SCOUT_V2_MODE"] = self._previous_v2_mode

    def test_presets_structure(self):
        self.assertEqual(len(FASTMOSS_OFFICIAL_PRESETS), 5)
        self.assertEqual(len(FASTMOSS_LABEL_TO_PRESET_ID), 5)
        for preset_id, info in FASTMOSS_OFFICIAL_PRESETS.items():
            self.assertIn("label", info)
            self.assertIn("skill_file", info)
            self.assertIn("description", info)
            self.assertIn("tools", info)
            self.assertTrue(isinstance(info["tools"], frozenset))
            self.assertGreater(len(info["tools"]), 0)
            for tool_id in info["tools"]:
                self.assertTrue(tool_id.startswith("fastmoss__"), f"Invalid tool prefix: {tool_id}")

    def test_route_resolution_by_preset_id(self):
        preset_id = "fm-product-scout"
        previous = os.environ.get("FASTMOSS_SKILL_SOURCE")
        os.environ["FASTMOSS_SKILL_SOURCE"] = "local"
        try:
            route = fastmoss_official_skill_route(official_preset_id=preset_id)
        finally:
            if previous is None:
                os.environ.pop("FASTMOSS_SKILL_SOURCE", None)
            else:
                os.environ["FASTMOSS_SKILL_SOURCE"] = previous
        self.assertEqual(route["route_source"], "lightweight_skill")
        self.assertTrue(route["lightweight_fastmoss_skill"])
        self.assertEqual(route["max_rounds"], 12)
        self.assertEqual(route["official_preset_id"], preset_id)
        self.assertEqual(route["official_skill_file"], "references/fm-product-scout.md")
        self.assertEqual(route["tools"], sorted(FASTMOSS_OFFICIAL_PRESETS[preset_id]["tools"]))

    def test_product_scout_local_skill_is_reviewable_and_can_roll_back(self):
        prompt = load_lightweight_fastmoss_skill_prompt("fm-product-scout")
        self.assertIn("选品决策 Skill", prompt)
        self.assertIn("FastMoss MCP", prompt)
        self.assertIn("不在与当前任务无关的场景推广 MossCreator、Oumomo", prompt)

        previous = os.environ.get("FASTMOSS_SKILL_SOURCE")
        os.environ["FASTMOSS_SKILL_SOURCE"] = "official"
        try:
            route = fastmoss_official_skill_route(official_preset_id="fm-product-scout")
        finally:
            if previous is None:
                os.environ.pop("FASTMOSS_SKILL_SOURCE", None)
            else:
                os.environ["FASTMOSS_SKILL_SOURCE"] = previous
        self.assertEqual(route["route_source"], "official_preset")
        self.assertNotIn("lightweight_fastmoss_skill", route)

    def test_product_scout_v2_mode_is_scoped_to_this_preset(self):
        previous = os.environ.get("FASTMOSS_PRODUCT_SCOUT_V2_MODE")
        os.environ["FASTMOSS_PRODUCT_SCOUT_V2_MODE"] = "enforce"
        try:
            product_scout = fastmoss_official_skill_route(official_preset_id="fm-product-scout")
            other_preset = fastmoss_official_skill_route(official_preset_id="fm-creator-outreach")
        finally:
            if previous is None:
                os.environ.pop("FASTMOSS_PRODUCT_SCOUT_V2_MODE", None)
            else:
                os.environ["FASTMOSS_PRODUCT_SCOUT_V2_MODE"] = previous
        self.assertEqual(product_scout["product_scout_v2_mode"], "enforce")
        self.assertTrue(product_scout["product_scout_v2"])
        self.assertEqual(product_scout["route_source"], "product_scout_v2_enforce")
        self.assertNotIn("product_scout_v2", other_preset)

    def test_route_resolution_by_user_text_prefix(self):
        user_text = "请使用 FastMoss 官方 Skill「达人建联」开始分析。"
        route = fastmoss_official_skill_route(user_text=user_text)
        self.assertEqual(route["route_source"], "official_preset")
        self.assertEqual(route["official_preset_id"], "fm-creator-outreach")
        self.assertEqual(route["official_skill_file"], "references/fm-creator-outreach.md")

    def test_tool_ids_whitelist_isolation(self):
        enabled_tools = {
            "fastmoss__search_category_by_words",
            "fastmoss__market_category_analysis",
            "fastmoss__creator_search",
            "sellersprite__product_research",
            "system__weather",
        }
        # Unrestricted
        all_fastmoss = fastmoss_official_skill_tool_ids(enabled_tools)
        self.assertEqual(all_fastmoss, {
            "fastmoss__search_category_by_words",
            "fastmoss__market_category_analysis",
            "fastmoss__creator_search",
        })

        # Whitelisted for the official creator-outreach Skill.
        creator_tools = FASTMOSS_OFFICIAL_PRESETS["fm-creator-outreach"]["tools"]
        isolated = fastmoss_official_skill_tool_ids(enabled_tools, allowed_tools=creator_tools)
        self.assertEqual(isolated, {
            "fastmoss__search_category_by_words",
            "fastmoss__creator_search",
        })
        self.assertNotIn("fastmoss__market_category_analysis", isolated)

    def test_modal_rendering(self):
        modal_ui = render_chat_official_workflow_modal("fastmoss")
        self.assertEqual(modal_ui["kicker"], "FASTMOSS · OFFICIAL SKILLS")
        self.assertEqual(modal_ui["title"], "FastMoss 官方策略库")
        self.assertEqual(modal_ui["tabs_class"], " official-workflow-tabs--single")
        self.assertIn("进入对应的选品", modal_ui["intro"])
        self.assertIn("官方 Skills <span>5</span>", modal_ui["tabs"])
        self.assertIn("<button", modal_ui["tabs"])
        self.assertIn("fm-product-scout", modal_ui["panels"])
        self.assertIn("fm-creator-outreach", modal_ui["panels"])

    def test_selected_prompt_keeps_base_and_preset_reference(self):
        prompt = (
            "FastMoss provenance"
            "\n\n## 官方文件：SKILL.md\n\nbase"
            "\n\n## 官方文件：references/PRINCIPLES.md\n\nshared principles"
            "\n\n## 官方文件：references/GLOSSARY.md\n\nglossary"
            "\n\n## 官方文件：references/fm-product-scout.md\n\nproduct workflow"
            "\n\n## 官方文件：references/fm-creator-outreach.md\n\ncreator workflow"
        )
        selected = select_official_fastmoss_skill_prompt(
            prompt, "references/fm-product-scout.md"
        )
        self.assertIn("SKILL.md", selected)
        self.assertIn("references/PRINCIPLES.md", selected)
        self.assertIn("references/GLOSSARY.md", selected)
        self.assertIn("references/fm-product-scout.md", selected)
        self.assertNotIn(
            "\n\n## 官方文件：references/fm-creator-outreach.md\n\n", selected
        )

    def test_quick_actions_3_plus_1_format(self):
        html = render_chat_quick_actions("fastmoss", CHAT_PROVIDER_UI["fastmoss"], official_enabled=True)
        self.assertIn("选品决策", html)
        self.assertIn("达人建联", html)
        self.assertIn("视频策略", html)
        self.assertIn('data-official-preset-id="fm-product-scout"', html)
        self.assertIn('data-official-preset-id="fm-creator-outreach"', html)
        self.assertIn('data-official-preset-id="fm-video-brief"', html)
        self.assertIn("official-workflow-launch", html)
        self.assertIn("查看全部", html)

    def test_official_preset_is_persisted_with_user_message(self):
        with TemporaryDirectory() as temp_dir:
            sessions_file = Path(temp_dir) / "sessions.json"
            store = ChatStore(sessions_file)
            session = store.get_or_create("fastmoss:demo")
            store.add_message(session, Message(
                id="user-1", role="user", content="解压玩具",
                official_preset={
                    "id": "fm-product-scout",
                    "label": "选品决策",
                },
            ))
            save_sessions_to_disk(store)
            restored = ChatStore(sessions_file)
            load_sessions_from_disk(restored)
            self.assertEqual(
                restored.get_session("fastmoss:demo").messages[0].official_preset,
                {"id": "fm-product-scout", "label": "选品决策"},
            )

    def test_short_question_title_fallback_keeps_analysis_intent(self):
        self.assertEqual(fallback_chat_session_title("解压玩具"), "解压玩具分析")
        self.assertEqual(
            fallback_chat_session_title("目标：Fidget Toys 市场机会"),
            "Fidget Toys",
        )


if __name__ == "__main__":
    unittest.main()
