"""Keep the Chuhaijiang page on the exact retired FastMoss presentation contract."""
import unittest

from scripts.web_app import (
    CHAT_PROVIDER_DEFAULT_DOMAINS,
    CHAT_PROVIDER_UI,
    chat_presentation_provider,
    render_chat_official_workflow_modal,
    render_chat_quick_actions,
)


class TestChuhaijiangUiContract(unittest.TestCase):
    def test_chuhaijiang_reuses_fastmoss_presentation_and_backend_domain(self):
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


if __name__ == "__main__":
    unittest.main()
