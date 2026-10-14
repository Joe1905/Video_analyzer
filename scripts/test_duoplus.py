"""Focused offline checks for the DuoPlus bridge (no cloud phone actions)."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import duoplus


class DuoPlusTest(unittest.TestCase):
    def call(self, directory, path, body=None, method="POST", origin="http://127.0.0.1:4003"):
        raw = json.dumps(body or {}).encode()
        handler = SimpleNamespace(command=method, client_address=("127.0.0.1", 1234),
                                  headers={"Host": "127.0.0.1:4003", "Origin": origin,
                                           "Content-Type": "application/json", "Content-Length": str(len(raw))},
                                  rfile=io.BytesIO(raw))
        responses = []
        handled = duoplus.handle(handler, urlparse(path), Path(directory),
                                 lambda _, status, data: responses.append((status, data)))
        self.assertTrue(handled)
        return responses[0]

    def test_config_keeps_key_private_and_preserves_blank(self):
        with tempfile.TemporaryDirectory() as directory:
            status, public = self.call(directory, "/api/duoplus/config", {"api_key": "test-secret", "image_id": "phone-1"})
            self.assertEqual(status, 200)
            self.assertNotIn("test-secret", json.dumps(public))
            self.call(directory, "/api/duoplus/config", {"api_key": ""})
            self.assertEqual(duoplus.load_config(Path(directory))["api_key"], "test-secret")
            self.assertNotIn("api_key", self.call(directory, "/api/duoplus/config", method="GET")[1])

    def test_foreign_origin_cannot_control(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.call(directory, "/api/duoplus/run", origin="https://example.com")[0], 403)

    def test_invalid_scripts_rejected_before_cloud_request(self):
        for steps in ([], [{"action": "EXECUTE_ADB"}], [{"action": "CLICK_COORDINATE", "params": {"x": 1001, "y": 1}}],
                      [{"action": "WAIT_TIME", "params": {"wait_time": -1}}], [{"action": "PAGE_BACK"}] * 21):
            with self.assertRaises(ValueError):
                duoplus.validate_steps(steps)

    def test_phone_id_and_http_support_are_checked(self):
        with patch.object(duoplus, "list_phones", return_value=[{"id": "other", "status": 1, "http_status": 1}]):
            with self.assertRaises(ValueError):
                duoplus.get_phone({}, "phone-1")
        with patch.object(duoplus, "list_phones", return_value=[{"id": "phone-1", "status": 1, "http_status": 0}]):
            with self.assertRaises(ValueError):
                duoplus.get_phone({}, "phone-1")

    def test_submission_checks_inner_result_and_ids(self):
        with patch.object(duoplus, "request", return_value={"state": "SUCCEEDED", "result_json": '{"success":true}'}) as request:
            duoplus.submit({}, {}, "PAGE_BACK")
            body = request.call_args.args[2]
            self.assertEqual(body["task_id"], body["payload"]["task_id"])
        with patch.object(duoplus, "request", return_value={"state": "SUCCEEDED", "result_json": '{"success":false}'}):
            with self.assertRaises(ValueError):
                duoplus.submit({}, {})

    def test_failure_stops_sequence_without_retry(self):
        with patch.object(duoplus, "request", return_value={"state": "FAILED",
                          "result_json": '{"success":false,"message":"Unsupported action test-secret"}'}):
            with self.assertRaises(duoplus.ActionFailure) as failure:
                duoplus.submit({"api_key": "test-secret"}, {}, "PAGE_BACK")
            self.assertEqual(failure.exception.details["gateway_state"], "FAILED")
            self.assertIn("Unsupported action", str(failure.exception))
            self.assertNotIn("test-secret", str(failure.exception))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(duoplus, "get_phone", return_value={}), patch.object(duoplus, "submit", side_effect=[
                {"success": True}, {"success": True}, {"success": True}, ValueError("failed")]) as submit:
                steps = [{"action": "PAGE_BACK"}] * 3
                status, result = self.call(directory, "/api/duoplus/run", {"image_id": "phone-1", "steps": steps})
                self.assertEqual(status, 502)
                self.assertEqual(len(result["logs"]), 1)
                self.assertEqual(submit.call_count, 4)

    def test_action_log_does_not_duplicate_screenshot(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(duoplus, "get_phone", return_value={}), patch.object(duoplus, "submit", side_effect=[
                {"success": True}, {"success": True, "screenshot": "large-image", "current_app": "tiktok"},
                {"success": True, "screenshot": "latest-image"}]):
                status, result = self.call(directory, "/api/duoplus/run", {"image_id": "phone-1", "steps": [{"action": "PAGE_BACK"}]})
                self.assertEqual(status, 200)
                self.assertNotIn("screenshot", result["logs"][0]["result"])
                self.assertEqual(result["screen"]["screenshot"], "latest-image")


if __name__ == "__main__":
    unittest.main()
