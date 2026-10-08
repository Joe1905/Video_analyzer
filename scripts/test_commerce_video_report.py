"""Focused regression checks for collection evidence and extraction/report separation."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import deepseek_postprocess as post
from video_performance_context import external_video_id, load_performance_context, validate_report


class CommerceReportTests(unittest.TestCase):
    def test_explicit_thinking_payload_and_timeout(self):
        for effort in ("high", "disabled", None):
            with patch.object(post.requests, "post", return_value=Mock()) as request, \
                 patch.object(post, "record_api_call"):
                post.call_deepseek("test", "{}", "https://example.com", "test", 32768, effort)
            payload = request.call_args.kwargs["json"]
            if effort == "high":
                self.assertEqual(payload["thinking"], {"type": "enabled"})
                self.assertEqual(payload["reasoning_effort"], "high")
                self.assertEqual(request.call_args.kwargs["timeout"], 600)
            elif effort == "disabled":
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertNotIn("reasoning_effort", payload)
            else:
                self.assertNotIn("thinking", payload)

    def test_truncated_report_is_rejected(self):
        with self.assertRaises(ValueError):
            post.extract_content({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.db = self.root / "data/proxy_pool.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""CREATE TABLE collect_jobs (id TEXT,platform TEXT);
                INSERT INTO collect_jobs VALUES ('tk','tiktok'),('ig','instagram');
                CREATE TABLE collect_results (id INTEGER PRIMARY KEY,job_id TEXT,account_id INTEGER,
                video_id TEXT,published_at TEXT,collected_at TEXT,title TEXT,payload_json TEXT);""")
        self.vid = "7683856958457253150"

    def put(self, number, payload, platform="tk", vid=None):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO collect_results VALUES (?,?,?,?,?,?,?,?)",
                         (number, platform, 1, vid or self.vid, "2026-09-11",
                          f"2026-10-{number:02d}T00:00:00Z", "sample", json.dumps(payload)))

    def test_latest_valid_same_video_and_platform_zero_preserved(self):
        self.put(1, {"overview": {"play_count": "389"}})
        self.put(2, {"overview": {"play_count": "0", "completion_rate": "0%"},
                     "retention": {"0:00": "100%", "0:01": "50%"},
                     "account": {"secret": "DO_NOT_SEND"}})
        self.put(3, {"overview": {"play_count": "999"}}, platform="ig")
        self.put(4, {"overview": {"play_count": "888"}}, vid="7684226408121503007")
        self.put(5, {"error": "collection failed"})
        result = load_performance_context(self.root, self.vid)
        self.assertEqual(result["collection_id"], 2)
        self.assertEqual(result["overview"]["play_count"], "0")
        self.assertEqual(result["retention"]["0:01"], "50%")
        self.assertNotIn("DO_NOT_SEND", json.dumps(result))
        self.assertIn("历史采集快照", " ".join(result["limitations"]))

    def test_missing_evidence_not_zero(self):
        result = load_performance_context(self.root, self.vid)
        self.assertFalse(result["available"])
        self.assertNotIn("overview", result)
        self.assertIsNone(external_video_id("other_7683856958457253150.mp4"))
        self.assertEqual(external_video_id(f"shortvideo_SociaVault_{self.vid}.mp4"), self.vid)

    def test_prompt_contains_both_evidence_and_causal_limits(self):
        self.put(1, {"overview": {"play_count": "389"}, "retention": {"0:03": "24%"}})
        text = post.build_prompt({"timeline": [{"time_range": "2-3s", "visual": "red toy"}]},
                                 "关注开头", load_performance_context(self.root, self.vid))
        for value in ("red toy", "389", "24%", "2026-10-01", "关注开头", "无法判断转化", "百分点"):
            self.assertIn(value, text)

    def test_reject_extraction_disguised_as_report(self):
        with self.assertRaises(ValueError):
            validate_report({"summary": "ok", "timeline": [], "visual_evidence": []})
        with self.assertRaises(ValueError):
            validate_report([])

    def test_duration_uses_media_probe_not_sample_timestamps(self):
        media = self.root / "sample.mp4"
        media.touch()
        with patch.object(post.subprocess, "run", return_value=Mock(stdout="11.239\n")):
            duration = post.video_duration(media)
        self.assertEqual(duration, 11.239)
        self.assertIsNone(post.video_duration(self.root / "missing.mp4"))
        with patch.object(post.subprocess, "run", return_value=Mock(stdout="NaN")):
            self.assertIsNone(post.video_duration(media))
        prompt = post.build_prompt({"metadata": {"duration_seconds": duration}})
        self.assertIn('"duration_seconds": 11.239', prompt)
        self.assertIn("禁止用完播率乘视频时长", prompt)

    def test_report_queue_does_not_reuse_extraction_prompt(self):
        import web_app as app
        folder = self.root / "sample.mp4"
        folder.mkdir()
        (folder / "analysis.json").write_text('{}')
        (folder / "analysis_prompt.txt").write_text('EXTRACT_ONLY')
        (folder / "report_prompt.txt").write_text('REPORT_ONLY')
        with patch.object(app, "OUTPUT_DIR", self.root), patch.object(app, "video_queue"), \
             patch.object(app, "get_video_by_filename", return_value={"platform": "tiktok", "video_id": self.vid}), \
             patch.object(app.subprocess, "run") as run:
            app.execute_queue_job("sample.mp4", "report", {})
        command = run.call_args.args[0]
        self.assertIn('REPORT_ONLY', command)
        self.assertNotIn('EXTRACT_ONLY', command)
        self.assertIn(self.vid, command)

    def test_api_report_request_preserves_extraction_prompt(self):
        import web_app as app
        (self.root / "analysis.json").write_text('{}')
        (self.root / "analysis_prompt.txt").write_text('EXTRACT_ONLY')
        body = json.dumps({"filename": "sample.mp4", "report_prompt": "REPORT_ONLY"}).encode()
        handler = Mock(headers={"Content-Length": str(len(body))}, rfile=io.BytesIO(body))
        with patch.object(app, "output_dir_for_filename", return_value=self.root), \
             patch.object(app, "json_response") as reply, patch.object(app, "video_queue") as queue:
            app.Handler.handle_postprocess(handler)
        self.assertEqual((self.root / "analysis_prompt.txt").read_text(), 'EXTRACT_ONLY')
        self.assertEqual((self.root / "report_prompt.txt").read_text(), 'REPORT_ONLY')
        self.assertEqual(reply.call_args.args[1], 202)
        queue.enqueue.assert_called_once_with('sample.mp4', 'report')


if __name__ == "__main__":
    unittest.main()
