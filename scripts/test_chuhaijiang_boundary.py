"""Focused contract tests for the isolated Chuhaijiang provider."""
import ast
import io
import json
import unittest
from pathlib import Path

import web_app
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
        focused = load_official_skill_prompt(["references/product-selection.md"])
        self.assertIn("## 官方文件：SKILL.md", focused)
        self.assertIn("## 官方文件：references/product-selection.md", focused)
        self.assertNotIn("## 官方文件：references/social-media.md", focused)

    def test_product_selection_preset_is_fail_closed_and_execution_scoped(self):
        prompt = "请按出海匠官方 Skill 的「选品与市场调研」流程处理以下信息。"
        route = web_app.chuhaijiang_official_skill_route(prompt)
        self.assertEqual(route["route_source"], "official_preset")
        self.assertEqual(route["official_preset_id"], "chuhaijiang/product-selection")
        self.assertEqual(route["official_skill_files"], ["references/product-selection.md"])
        self.assertIn("chuhaijiang__search", route["tools"])
        self.assertNotIn("chuhaijiang__social_publish", route["tools"])
        blocked = web_app.execute_prefixed_tool(
            "chuhaijiang__social_publish", {}, allowed_tool_ids=set(route["tools"])
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("outside the active preset boundary", blocked["error"])
        invalid = web_app.chuhaijiang_official_skill_route("", "unknown-preset")
        self.assertEqual(invalid["route_source"], "invalid_preset")
        self.assertEqual(invalid["tools"], [])

    def test_structured_mcp_business_error_is_not_reported_as_success(self):
        raw = {
            "ok": True,
            "data": {
                "content": [{}],
                "structuredContent": {"result": json.dumps({"error": "Unknown marketplace: US"})},
                "isError": False,
            },
        }
        normalized = web_app.normalize_prefixed_tool_result("chuhaijiang__amazon", raw)
        self.assertFalse(normalized["ok"])
        self.assertEqual(normalized["data_state"], "error")
        self.assertIn("Unknown marketplace", normalized["error"])

    def test_high_risk_gate_is_conservative(self):
        self.assertTrue(is_high_risk_tool("ai_generate", {}))
        self.assertTrue(is_high_risk_tool("social_publish", {}))
        self.assertTrue(is_high_risk_tool("canvas", {"action": "generate"}))
        self.assertFalse(is_high_risk_tool("canvas", {"action": "load"}))
        self.assertFalse(is_high_risk_tool("account_info", {}))

    def test_shared_presentation_keeps_only_active_provider_domains(self):
        source = Path(__file__).with_name("web_app.py").read_text(encoding="utf-8")
        chat_html = Path(__file__).with_name("static").joinpath("chat.html").read_text(encoding="utf-8")
        self.assertIn('"chuhaijiang": {"chuhaijiang"}', source)
        self.assertIn('return serve_chat_template(self, "chuhaijiang", parsed.path)', source)
        self.assertNotIn('build_chuhaijiang_independent_template', source)
        self.assertNotIn('if provider not in {"home", "amazon", "chuhaijiang"}:', source)
        self.assertEqual(web_app.CHAT_PROVIDERS, {"home", "amazon", "chuhaijiang"})
        self.assertEqual(
            set(web_app.CHAT_TOOL_DOMAINS),
            {"system", "function", "sociavault", "sellersprite", "chuhaijiang"},
        )
        self.assertNotIn('data-chuhaijiang-prompt', chat_html)

    def test_external_provider_input_is_fail_closed_without_changing_internal_default(self):
        """HTTP provider input must not turn a retired/unknown domain into Home."""
        self.assertEqual(web_app.normalize_chat_provider(None), "home")

        for value in ("fast" + "moss", "unregistered-provider"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web_app.parse_external_chat_provider(value)

        class FakeHandler:
            def __init__(self):
                self.status = None
                self.headers = {}
                self.wfile = io.BytesIO()

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers[name.lower()] = value

            def end_headers(self):
                return None

        handler = FakeHandler()
        result = web_app.Handler.require_external_chat_provider(
            handler, "unregistered-provider"
        )
        self.assertIsNone(result)
        self.assertEqual(handler.status, 400)
        self.assertEqual(
            json.loads(handler.wfile.getvalue().decode("utf-8")),
            {"error": "Unknown chat provider"},
        )

        source = Path(__file__).with_name("web_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        expected_handler_calls = {
            "do_GET": 6,
            "do_DELETE": 1,
            "handle_chat_ask": 1,
            "handle_chat_export_pdf": 1,
            "handle_chat_rename_session": 1,
        }
        handler = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Handler"
        )
        actual_handler_calls = {
            node.name: sum(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and call.func.attr == "require_external_chat_provider"
                for call in ast.walk(node)
            )
            for node in handler.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(
            {name: actual_handler_calls.get(name, 0) for name in expected_handler_calls},
            expected_handler_calls,
        )


if __name__ == "__main__":
    unittest.main()
