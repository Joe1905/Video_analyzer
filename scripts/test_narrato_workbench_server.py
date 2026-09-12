"""Tests for NarratoAI Modern Workbench Tornado server & router."""
import json
import tornado.web
import tornado.routing
import tornado.testing
from app.services import narrato_workbench_server as wb


class WorkbenchServerTestCase(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        # Create Tornado application with workbench rules
        rules = wb.get_workbench_rules()
        app = tornado.web.Application()
        for rule in reversed(rules):
            app.wildcard_router.rules.insert(0, rule)
        return app

    def test_root_serves_approved_html_without_legacy_header(self):
        response = self.fetch("/")
        self.assertEqual(response.code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("NarratoAI · 智能剪辑工作台", body)
        self.assertIn("物品识别剪辑", body)
        self.assertIn("全自动视频制作", body)
        self.assertIn("theater-modal", body)
        self.assertIn("product-modal", body)
        self.assertIn("task-drawer", body)
        # Verify legacy elements are completely absent
        self.assertNotIn("Narrato:blue[AI]:sunglasses:", body)
        self.assertNotIn("一站式 AI 影视解说+自动化剪辑工具", body)

    def test_api_state(self):
        response = self.fetch("/api/state")
        self.assertEqual(response.code, 200)
        data = json.loads(response.body.decode("utf-8"))
        self.assertIn("current_task", data)
        self.assertIn("object_tasks", data)
        self.assertIn("materials", data)
        self.assertIn("voices", data)
        self.assertIn("auto_tasks", data)

    def test_api_voices(self):
        response = self.fetch("/api/voices")
        self.assertEqual(response.code, 200)
        data = json.loads(response.body.decode("utf-8"))
        self.assertIn("voices", data)
        self.assertTrue(len(data["voices"]) > 0)
        first_voice = data["voices"][0]
        self.assertIn("voice_id", first_voice)
        self.assertIn("name", first_voice)

    def test_patch_streamlit_server(self):
        from streamlit.web.server.server import Server
        wb.patch_streamlit_server()
        self.assertTrue(getattr(Server, "_workbench_patched", False))


if __name__ == "__main__":
    import unittest
    unittest.main()
