"""Focused checks: python scripts/test_vision_provider.py (inside Compose)."""
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, Mock

import requests
import vision_provider as vp
import frame_vision_analyze as frames


def reply(status=200, payload=None):
    response = Mock(status_code=status, ok=status < 400)
    response.json.return_value = payload or {"choices": [{"message": {"content": "white"}}]}
    return response


class VisionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.env = patch.dict(os.environ, {"VISION_API_KEY": "private-qwen", "VISION_MODEL": "qwen-test",
            "DIRECT_VIDEO_MODEL": "qwen-test", "DEEPSEEK_API_KEY": "private-deepseek"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        state_patch = patch.object(vp, "STATE_PATH", self.path)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def test_first_check_cache_expiration_and_recovery(self):
        with patch.object(vp.time, "time", return_value=100), patch.object(vp, "_probe", return_value=(False, "Arrearage", "欠费")) as probe:
            state = vp.get_status()
            self.assertEqual(state["next_check_at"], 100 + 86400)
            self.assertEqual(state["frame_provider"], "deepseek")
            self.assertFalse(state["direct_video_enabled"])
            self.assertEqual(vp.get_status(), state)
            probe.assert_called_once()
        # New database connection uses persisted state, with no background wake-up.
        with patch.object(vp.time, "time", return_value=86499), patch.object(vp, "_probe") as probe:
            self.assertEqual(vp.get_status(), state)
            probe.assert_not_called()
        with patch.object(vp.time, "time", return_value=86500), patch.object(vp, "_probe", return_value=(True, "", "")) as probe:
            self.assertTrue(vp.get_status()["direct_video_enabled"])
            self.assertEqual(vp.frame_config()["provider"], "qwen")
            probe.assert_called_once()

    def test_concurrent_requests_only_probe_once(self):
        with patch.object(vp, "_probe", return_value=(True, "", "")) as probe:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: vp.get_status(), range(16)))
            probe.assert_called_once()
            self.assertTrue(all(r["direct_video_enabled"] for r in results))

    def test_probe_uses_image_and_records_arrearage_without_secrets(self):
        result = reply(400, {"error": {"code": "Arrearage", "message": "private-qwen"}})
        with patch.object(vp.requests, "post", return_value=result) as post:
            state = vp.get_status()
        self.assertEqual(state["code"], "Arrearage")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["content"][0]["type"], "image_url")
        self.assertNotIn("private-qwen", json.dumps(state))
        self.assertNotIn(b"private-qwen", self.path.read_bytes())
        self.assertEqual(vp.frame_config()["api_key"], "private-deepseek")
        with self.assertRaisesRegex(RuntimeError, "视频直连分析已禁用"):
            vp.require_direct_video()

    def test_actual_failure_updates_cached_healthy_state(self):
        with patch.object(vp, "_probe", return_value=(True, "", "")):
            vp.get_status()
        vp.observe_response(reply(400, {"error": {"code": "Arrearage"}}))
        with patch.object(vp, "_probe") as probe:
            self.assertFalse(vp.get_status()["direct_video_enabled"])
            probe.assert_not_called()

    def test_bad_video_does_not_disable_account(self):
        with patch.object(vp, "_probe", return_value=(True, "", "")):
            vp.get_status()
        self.assertIsNone(vp.observe_response(reply(400, {"error": {"code": "InvalidParameter"}})))
        self.assertTrue(vp.get_status()["direct_video_enabled"])

    def test_probe_network_failure_and_missing_backup(self):
        with patch.object(vp.requests, "post", side_effect=requests.Timeout):
            self.assertEqual(vp.get_status()["code"], "ConnectionFailed")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            self.assertFalse(vp.get_status()["frame_analysis_enabled"])
            with self.assertRaisesRegex(RuntimeError, "缺少 DeepSeek"):
                vp.frame_config()

    def test_deepseek_frame_request_and_failure_isolation(self):
        vp.mark_unavailable("Arrearage", "欠费")
        config = vp.frame_config()
        image = Path(self.tmp.name) / "frame.jpg"
        image.write_bytes(b"test-jpeg")
        with patch.object(frames.requests, "post", return_value=reply()) as post:
            self.assertEqual(frames.generate(config, "Describe", image), {"response": "white"})
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], vp.DEEPSEEK_VISION_MODEL)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["messages"][0]["content"][1]["type"], "image_url")
        before = vp.get_status()
        with patch.object(frames.requests, "post", return_value=reply(503)):
            with self.assertRaises(SystemExit):
                frames.generate(config, "Describe", image)
        self.assertEqual(vp.get_status(), before)

    def test_qwen_frame_failure_aborts_instead_of_saving_fake_success(self):
        with patch.object(vp, "_probe", return_value=(True, "", "")):
            config = vp.frame_config()
        with patch.object(frames.requests, "post", return_value=reply(403)):
            with self.assertRaises(SystemExit):
                frames.generate(config, "Describe")
        self.assertEqual(vp.frame_config()["provider"], "deepseek")

    def test_direct_video_guard_runs_before_video_upload(self):
        import direct_video_analyze as direct
        vp.mark_unavailable("Arrearage", "欠费")
        with patch.object(direct.requests, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "禁用"):
                direct.call_vision_api("key", "url", "model", "large-video", 2, {}, "", 20)
            post.assert_not_called()

    def test_api_refuses_direct_before_deleting_or_enqueuing(self):
        import web_app as web
        vp.mark_unavailable("Arrearage", "欠费")
        videos = Path(self.tmp.name)
        (videos / "fixture.mp4").write_bytes(b"fixture")
        handler = object.__new__(web.Handler)
        for method, payload in [("handle_analyze", {"filename": "fixture.mp4", "analysis_mode": "direct_video", "reset_output": True}),
                                ("handle_postprocess", {"filename": "fixture.mp4", "analysis_source": "direct"})]:
            body = json.dumps(payload).encode()
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = io.BytesIO(body)
            with patch.object(web, "VIDEOS_DIR", videos), patch.object(web, "output_dir_for_filename", return_value=videos / "absent"), \
                 patch.object(web, "json_response", side_effect=lambda h, code, data: (code, data)), \
                 patch.object(web.video_queue, "enqueue") as enqueue, patch.object(web.shutil, "rmtree") as delete:
                code, data = getattr(handler, method)()
                self.assertEqual(code, 409)
                self.assertEqual(data["code"], "DIRECT_VIDEO_DISABLED")
                enqueue.assert_not_called()
                delete.assert_not_called()
        self.assertFalse((videos / "absent").exists())


if __name__ == "__main__":
    unittest.main()
