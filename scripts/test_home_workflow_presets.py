"""Boundary checks for request-scoped homepage workflow presets."""

from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import web_app
from scripts.chat_preset_forms import HOME_PRESET_FORMS, preset_forms_for_provider


class HomeWorkflowPresetTests(unittest.TestCase):
    def test_registered_presets_have_matching_forms_and_scoped_tools(self) -> None:
        self.assertEqual(set(HOME_PRESET_FORMS), set(web_app.HOME_WORKFLOW_PRESETS))
        self.assertEqual(preset_forms_for_provider("home"), HOME_PRESET_FORMS)
        for preset_id, preset in web_app.HOME_WORKFLOW_PRESETS.items():
            with self.subTest(preset_id=preset_id):
                self.assertTrue(preset["tools"])
                self.assertTrue(all("__" in tool_id for tool_id in preset["tools"]))
                self.assertEqual(
                    web_app.official_preset_catalog_for_provider("home")[preset_id],
                    preset,
                )

    def test_video_workflow_mixes_mcp_and_local_plugins(self) -> None:
        tools = web_app.HOME_WORKFLOW_PRESETS["home/video-analysis"]["tools"]
        self.assertIn("sociavault__tiktok_video_info", tools)
        self.assertIn("function__video_download", tools)
        self.assertIn("function__video_analyze", tools)

    def test_route_and_execution_fail_closed(self) -> None:
        route = web_app.home_workflow_preset_route("home/web-verification")
        self.assertEqual(route["route_source"], "home_preset")
        self.assertEqual(
            set(route["tools"]),
            set(web_app.HOME_WORKFLOW_PRESETS["home/web-verification"]["tools"]),
        )
        invalid = web_app.home_workflow_preset_route("home/not-registered")
        self.assertEqual(invalid["route_source"], "invalid_preset")
        self.assertEqual(invalid["tools"], [])
        blocked = web_app.execute_prefixed_tool(
            "system__web_search", {"query": "must not run"}, allowed_tool_ids=set()
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("outside the active preset boundary", blocked["error"])


if __name__ == "__main__":
    unittest.main()
