"""Lock the third tab to the 2026-08-09 FastMoss presentation contract."""
import unittest
from unittest.mock import patch

try:
    from scripts import web_app
    from scripts.web_app import (
        CHAT_PROVIDER_DEFAULT_DOMAINS,
        CHAT_PROVIDER_UI,
        chat_presentation_provider,
        render_app_nav,
        render_chat_official_workflow_modal,
        render_chat_quick_actions,
        serve_chat_template,
    )
except ModuleNotFoundError:
    import web_app
    from web_app import (
        CHAT_PROVIDER_DEFAULT_DOMAINS,
        CHAT_PROVIDER_UI,
        chat_presentation_provider,
        render_app_nav,
        render_chat_official_workflow_modal,
        render_chat_quick_actions,
        serve_chat_template,
    )


class TestChuhaijiangUiContract(unittest.TestCase):
    def test_d534532_fastmoss_presentation_is_reused_with_chuhaijiang_backend_domain(self):
        presentation = chat_presentation_provider("chuhaijiang")
        self.assertEqual(presentation, "fastmoss")
        self.assertEqual(CHAT_PROVIDER_DEFAULT_DOMAINS["chuhaijiang"], {"chuhaijiang"})
        self.assertEqual(
            render_chat_quick_actions(presentation, CHAT_PROVIDER_UI[presentation], True),
            render_chat_quick_actions("fastmoss", CHAT_PROVIDER_UI["fastmoss"], True),
        )
        self.assertEqual(
            render_chat_official_workflow_modal(presentation),
            render_chat_official_workflow_modal("fastmoss"),
        )

    def test_d534532_navigation_is_provider_scoped_and_only_rebrands_third_tab(self):
        nav = render_app_nav("/chuhaijiang", legacy_chuhaijiang=True)
        self.assertIn('href="/chuhaijiang"', nav)
        self.assertIn('>chuhaijiang<', nav)
        self.assertNotIn('href="/fastmoss"', nav)
        self.assertNotIn("ui-nav__identity", nav)
        # These entries were present on yesterday's FastMoss page and must not
        # be restored globally after the shared navigation was intentionally hidden.
        for href in ("/shop", "/metrics", "/extract"):
            self.assertIn(f'href="{href}"', nav)

    def test_both_chuhaijiang_urls_render_the_same_python_chat_template(self):
        rendered = {}

        def capture(_handler, _status, html, _content_type):
            rendered.setdefault("pages", []).append(html)

        with patch.object(web_app, "text_response", side_effect=capture):
            serve_chat_template(None, "chuhaijiang", "/chuhaijiang")
            serve_chat_template(None, "chuhaijiang", "/chuhaijiang/")

        self.assertEqual(rendered["pages"][0], rendered["pages"][1])
        page = rendered["pages"][0]
        self.assertIn('const CHAT_PROVIDER="chuhaijiang"', page)
        self.assertIn("ui-chuhaijiang-d534532", page)
        self.assertIn("chuhaijiang-d534532-nav-style", page)
        self.assertNotIn("ui-global-user-modal", page)
        # The shared template always uses the provider-scoped Python APIs.
        self.assertIn("fetch('/api/chat/ask'", page)
        self.assertNotIn("/chuhaijiang/api/ask", page)

    def test_old_proxy_path_is_not_reachable_from_chuhaijiang(self):
        app_source = web_app.__file__
        with open(app_source, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('if parsed.path in {"/chuhaijiang", "/chuhaijiang/"}:', source)
        self.assertIn('if parsed.path.startswith("/chuhaijiang/"):\n            return json_response(self, HTTPStatus.NOT_FOUND', source)
        self.assertIn('"chuhaijiang": "chuhaijiang"', source)


if __name__ == "__main__":
    unittest.main()
