"""Shared v2 chat-shell contract for the three AI providers."""
from pathlib import Path
import unittest
from unittest.mock import patch

try:
    from scripts import web_app
    from scripts.web_app import (
        CHAT_PROVIDER_DEFAULT_DOMAINS,
        MCP_CHAT_CONFIGS,
        serve_chat_template,
    )
except ModuleNotFoundError:
    import web_app
    from web_app import (
        CHAT_PROVIDER_DEFAULT_DOMAINS,
        MCP_CHAT_CONFIGS,
        serve_chat_template,
    )


class TestChuhaijiangUiContract(unittest.TestCase):
    @staticmethod
    def render(provider: str, path: str) -> str:
        captured = []
        with patch.object(web_app, "text_response", side_effect=lambda *_args: captured.append(_args[2])):
            serve_chat_template(None, provider, path)
        return captured[0]

    def test_three_providers_share_the_v2_chat_shell_and_assets(self):
        pages = {
            provider: self.render(provider, path)
            for provider, path in (("home", "/"), ("amazon", "/amazon"), ("chuhaijiang", "/chuhaijiang"))
        }
        core = (
            'class="chat-shell"', 'class="sidebar"', 'class="chat-main"',
            'class="input-bar"', 'id="ui-system-css"', 'id="ui-system-js"',
            'class="ui-nav"', 'const CHAT_PROVIDER=',
        )
        for page in pages.values():
            for marker in core:
                self.assertIn(marker, page)
        self.assertIn('const CHAT_PROVIDER="chuhaijiang"', pages["chuhaijiang"])
        self.assertIn('body data-provider="chuhaijiang"', pages["chuhaijiang"])

    def test_chuhaijiang_has_provider_copy_but_never_the_old_independent_shell(self):
        page = self.render("chuhaijiang", "/chuhaijiang")
        shared_styles = (web_app.SCRIPTS_DIR / "static" / "assets" / "ui-system.css").read_text(encoding="utf-8")
        self.assertEqual(CHAT_PROVIDER_DEFAULT_DOMAINS["chuhaijiang"], {"chuhaijiang"})
        self.assertEqual(MCP_CHAT_CONFIGS["chuhaijiang"]["default_port"], 4104)
        self.assertIn("出海匠", page)
        self.assertIn("ui-route-chuhaijiang", shared_styles)
        self.assertNotIn("ui-route-retired-provider", shared_styles)
        for legacy_marker in (
            'data-chuhaijiang-independent="1"', 'SellerSprite MCP',
            'const BASE_PATH=',
        ):
            self.assertNotIn(legacy_marker, page)

    def test_chuhaijiang_official_scene_dialog_opens_forms_with_request_scoped_preset_ids(self):
        page = self.render("chuhaijiang", "/chuhaijiang")
        self.assertIn('const OFFICIAL_WORKFLOW_ENABLED=true', page)
        self.assertIn('id="officialWorkflowComposerLaunch"', page)
        self.assertEqual(page.count('data-chuhaijiang-scene="'), 8)
        self.assertIn('data-official-preset-id="chuhaijiang/product-selection"', page)
        for label in (
            "选品与市场调研", "利润测算", "达人筛选与建联", "竞品、店铺与广告分析",
            "AI 内容生成", "AI 画布创作", "视频剪辑", "社媒运营",
        ):
            self.assertIn(label, page)
        template = (web_app.SCRIPTS_DIR / "static" / "chat.html").read_text(encoding="utf-8")
        self.assertIn("data-chuhaijiang-scene", template)
        self.assertIn("enterPresetForm(button.dataset.chuhaijiangScene,button.dataset.presetFormId,button.dataset.presetFormId)", template)
        self.assertIn('id="presetForm"', template)

    def test_all_three_providers_show_three_frequent_actions_and_a_more_entry(self):
        home = self.render("home", "/")
        chuhaijiang = self.render("chuhaijiang", "/chuhaijiang")
        amazon = web_app.render_chat_quick_actions(
            "amazon", web_app.CHAT_PROVIDER_UI["amazon"], True
        )
        for quick_actions in (home, chuhaijiang, amazon):
            self.assertEqual(quick_actions.count('class="quick-prompt'), 4)
            self.assertIn('id="officialWorkflowLaunch"', quick_actions)
            self.assertIn("<strong>更多</strong>", quick_actions)
        self.assertEqual(home.count('data-chat-scene="'), 0)
        self.assertEqual(chuhaijiang.count('data-chuhaijiang-scene="'), 8)
        self.assertIn("短视频洞察与运营协作", home)
        self.assertIn("亚马逊选品与竞品洞察", self.render("amazon", "/amazon"))
        self.assertIn("TikTok Shop 出海经营与内容运营", chuhaijiang)

    def test_home_uses_sociavault_identity_and_a_distinct_accent(self):
        home = self.render("home", "/")
        styles = (web_app.SCRIPTS_DIR / "static" / "assets" / "ui-system.css").read_text(encoding="utf-8")
        self.assertIn("SociaVault 数据洞察", home)
        self.assertEqual(web_app.CHAT_PROVIDER_UI["home"]["model"], "SociaVault · 就绪")
        self.assertIn('body[data-provider="home"]', styles)
        self.assertIn("--ui-brand: #7447d9", styles)
        self.assertIn('body[data-provider="amazon"]', styles)
        self.assertIn('body[data-provider="chuhaijiang"]', styles)

    def test_empty_session_sidebar_has_an_icon_and_guidance_copy(self):
        template = (web_app.SCRIPTS_DIR / "static" / "chat.html").read_text(encoding="utf-8")
        styles = (web_app.SCRIPTS_DIR / "static" / "assets" / "ui-system.css").read_text(encoding="utf-8")
        self.assertIn('class="session-empty-state"', template)
        self.assertIn("还没有对话", template)
        self.assertIn("从一次新的提问开始", template)
        self.assertIn(".session-empty-state svg", styles)
        self.assertIn(".session-list:has(.session-empty-state)", styles)

    def test_source_canonicalizes_active_provider_slashes(self):
        source = Path(web_app.__file__).read_text(encoding="utf-8")
        self.assertIn('if parsed.path == "/amazon/":', source)
        self.assertIn('if parsed.path == "/chuhaijiang/":', source)
        self.assertNotIn("build_chuhaijiang_independent_template", source)
        self.assertNotIn("serve_chuhaijiang_independent_template", source)
        self.assertNotIn('proxy_mcp_chat(self, "sellersprite")', source)


if __name__ == "__main__":
    unittest.main()
