"""Test FastMoss official presets registration, tool whitelists, and boundary isolation."""
import unittest
from scripts.web_app import (
    FASTMOSS_OFFICIAL_PRESETS,
    FASTMOSS_LABEL_TO_PRESET_ID,
    fastmoss_official_skill_route,
    fastmoss_official_skill_tool_ids,
    render_chat_official_workflow_modal,
    render_chat_quick_actions,
    CHAT_PROVIDER_UI,
    select_official_fastmoss_skill_prompt,
)


class TestFastMossPresetsBoundary(unittest.TestCase):
    def test_presets_structure(self):
        self.assertEqual(len(FASTMOSS_OFFICIAL_PRESETS), 7)
        self.assertEqual(len(FASTMOSS_LABEL_TO_PRESET_ID), 7)
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
        preset_id = "comprehensive/product-research"
        route = fastmoss_official_skill_route(official_preset_id=preset_id)
        self.assertEqual(route["route_source"], "official_preset")
        self.assertEqual(route["official_preset_id"], preset_id)
        self.assertEqual(route["official_skill_file"], "references/tools-product.md")
        self.assertEqual(route["tools"], sorted(FASTMOSS_OFFICIAL_PRESETS[preset_id]["tools"]))

    def test_route_resolution_by_user_text_prefix(self):
        user_text = "请使用 FastMoss 官方 Skill「达人带货筛选」开始分析。"
        route = fastmoss_official_skill_route(user_text=user_text)
        self.assertEqual(route["route_source"], "official_preset")
        self.assertEqual(route["official_preset_id"], "comprehensive/creator-discovery")
        self.assertEqual(route["official_skill_file"], "references/tools-creator.md")

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

        # Whitelisted for creator-discovery
        creator_tools = FASTMOSS_OFFICIAL_PRESETS["comprehensive/creator-discovery"]["tools"]
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
        self.assertIn("官方策略 <span>7</span>", modal_ui["tabs"])
        self.assertIn("<button", modal_ui["tabs"])
        self.assertIn("comprehensive/product-research", modal_ui["panels"])
        self.assertIn("comprehensive/creator-discovery", modal_ui["panels"])

    def test_selected_prompt_keeps_base_and_preset_reference(self):
        prompt = (
            "FastMoss provenance"
            "\n\n## 官方文件：SKILL.md\n\nbase"
            "\n\n## 官方文件：references/tool-call.md\n\ncall rules"
            "\n\n## 官方文件：references/tools-product.md\n\nproduct tools"
            "\n\n## 官方文件：references/tools-creator.md\n\ncreator tools"
        )
        selected = select_official_fastmoss_skill_prompt(
            prompt, "references/tools-product.md"
        )
        self.assertIn("SKILL.md", selected)
        self.assertIn("references/tool-call.md", selected)
        self.assertIn("references/tools-product.md", selected)
        self.assertNotIn("references/tools-creator.md", selected)

    def test_quick_actions_3_plus_1_format(self):
        html = render_chat_quick_actions("fastmoss", CHAT_PROVIDER_UI["fastmoss"], official_enabled=True)
        self.assertIn("商品选品分析", html)
        self.assertIn("达人带货筛选", html)
        self.assertIn("爆款视频拆解", html)
        self.assertIn('data-official-preset-id="comprehensive/product-research"', html)
        self.assertIn('data-official-preset-id="comprehensive/creator-discovery"', html)
        self.assertIn('data-official-preset-id="comprehensive/content-dissect"', html)
        self.assertIn("official-workflow-launch", html)
        self.assertIn("查看全部", html)


if __name__ == "__main__":
    unittest.main()
