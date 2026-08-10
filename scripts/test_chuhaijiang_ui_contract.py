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
        self.assertEqual(CHAT_PROVIDER_DEFAULT_DOMAINS["chuhaijiang"], {"chuhaijiang"})
        self.assertEqual(MCP_CHAT_CONFIGS["chuhaijiang"]["default_port"], 4104)
        self.assertIn("出海匠", page)
        for legacy_marker in (
            'data-chuhaijiang-independent="1"', 'SellerSprite MCP',
            'const BASE_PATH=',
        ):
            self.assertNotIn(legacy_marker, page)

    def test_source_canonicalizes_slashes_and_cuts_off_v1_proxy_routes(self):
        source = Path(web_app.__file__).read_text(encoding="utf-8")
        self.assertIn('if parsed.path == "/amazon/":', source)
        self.assertIn('if parsed.path == "/chuhaijiang/":', source)
        self.assertIn('if parsed.path in {"/fastmoss", "/fastmoss/"}:', source)
        self.assertIn('self.send_header("Location", "/chuhaijiang")', source)
        self.assertNotIn("build_chuhaijiang_independent_template", source)
        self.assertNotIn("serve_chuhaijiang_independent_template", source)
        self.assertNotIn('proxy_mcp_chat(self, "sellersprite")', source)


if __name__ == "__main__":
    unittest.main()
