"""Contract for the isolated Chuhaijiang independent MCP chat page."""
import unittest
from unittest.mock import patch

try:
    from scripts import web_app
    from scripts.web_app import (
        CHAT_PROVIDER_DEFAULT_DOMAINS,
        MCP_CHAT_CONFIGS,
        build_chuhaijiang_independent_template,
        serve_chuhaijiang_independent_template,
    )
except ModuleNotFoundError:
    import web_app
    from web_app import (
        CHAT_PROVIDER_DEFAULT_DOMAINS,
        MCP_CHAT_CONFIGS,
        build_chuhaijiang_independent_template,
        serve_chuhaijiang_independent_template,
    )


class TestChuhaijiangUiContract(unittest.TestCase):
    def test_independent_fastmoss_slash_shell_is_reused_on_python_chuhaijiang_domain(self):
        page = build_chuhaijiang_independent_template()
        self.assertEqual(CHAT_PROVIDER_DEFAULT_DOMAINS["chuhaijiang"], {"chuhaijiang"})
        self.assertEqual(MCP_CHAT_CONFIGS["chuhaijiang"]["default_port"], 4104)
        for marker in ('<header>', 'class="chat-shell"', 'grid-template-columns:280px',
                       'class="empty-chat"', 'class="input-bar"'):
            self.assertIn(marker, page)
        for workbench_marker in ('ui-nav', 'chat-hero', 'quick-prompt',
                                 'official-workflow', 'ui-chuhaijiang-d534532'):
            self.assertNotIn(workbench_marker, page)
        self.assertIn('const BASE_PATH="/chuhaijiang";', page)
        self.assertIn('const PROVIDER_TYPE="chuhaijiang";', page)
        self.assertIn('const PROVIDER_LABEL="出海匠";', page)
        self.assertIn('label.textContent="chuhaijiang"', page)

    def test_independent_page_uses_only_python_chuhaijiang_chat_api(self):
        page = build_chuhaijiang_independent_template()
        self.assertIn('/api/chat/sessions?provider=chuhaijiang', page)
        self.assertIn('/api/chat/ask', page)
        self.assertIn('provider:"chuhaijiang"', page)
        self.assertIn('/api/chat/events', page)
        self.assertIn('/api/chat/sessions/${encodeURIComponent(id)}/delete', page)
        self.assertNotIn('/amazon/api/', page)
        self.assertNotIn('/fastmoss/api/', page)

    def test_both_chuhaijiang_urls_render_identical_independent_page(self):
        rendered = []

        def capture(_handler, _status, html, _content_type):
            rendered.append(html)

        with patch.object(web_app, "text_response", side_effect=capture):
            serve_chuhaijiang_independent_template(None)
            serve_chuhaijiang_independent_template(None)

        self.assertEqual(rendered[0], rendered[1])
        self.assertIn('data-chuhaijiang-independent="1"', rendered[0])

    def test_source_cuts_off_legacy_fastmoss_proxy_and_shared_template(self):
        source = open(web_app.__file__, encoding="utf-8").read()
        self.assertIn('return serve_chuhaijiang_independent_template(self)', source)
        self.assertIn('self.send_header("Location", "/chuhaijiang/")', source)
        self.assertNotIn('legacy_chuhaijiang=', source)


if __name__ == "__main__":
    unittest.main()
