"""Focused no-credential checks; run in the 4003 Compose runtime."""
import hashlib
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, Mock

import requests

import buffer_publish as publish
import buffer_media_server as media


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [patch.object(publish, "DATA", root / "data"), patch.object(publish, "VIDEOS", root / "videos")]
        for item in self.patches:
            item.start()
        publish.save_config({"buffer_key": "unit-test-key-must-never-leak", "media_token": "unit-test-media-token"})
        publish.VIDEOS.mkdir()
        self.asset_id = uuid.uuid4().hex
        video = publish.VIDEOS / (self.asset_id + ".mp4")
        video.write_bytes(b"test")
        video.with_suffix(".json").write_text(json.dumps({"size": 4, "sha256": hashlib.sha256(b"test").hexdigest()}))
        self.channel = {"id": "channel-test", "name": "test", "isDisconnected": False, "isLocked": False, "isQueuePaused": False, "allowedActions": ["scheduleUpdates"]}
        self.payload = {"request_id": str(uuid.uuid4()), "asset_id": self.asset_id, "channel_id": "channel-test", "text": "test", "mode": "draft", "ai_generated": True}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def create(self):
        with patch.object(publish, "channels", return_value=[self.channel]):
            return publish.create_job(self.payload)

    def test_repeated_request_returns_same_job(self):
        one = self.create()
        two = self.create()
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(len(publish.jobs()), 1)

    def test_input_uses_buffer_draft_flag_and_ai_disclosure(self):
        job = self.create()
        job["media_url"] = "https://ruitech.hk/buffer-media/test.mp4"
        value = publish.post_input(job)
        self.assertTrue(value["saveToDraft"])
        self.assertTrue(value["metadata"]["tiktok"]["isAiGenerated"])
        self.assertEqual(value["schedulingType"], "automatic")

    def test_schedule_converts_la_to_utc(self):
        with patch.object(publish.time, "time", return_value=0):
            self.assertEqual(publish.due_at("2026-12-01T10:00", "America/Los_Angeles"), "2026-12-01T18:00:00Z")
            self.assertEqual(publish.due_at("2026-07-01T10:00", "America/Los_Angeles"), "2026-07-01T17:00:00Z")

    def test_dst_ambiguous_and_nonexistent_times_rejected(self):
        for value in ("2027-03-14T02:30", "2027-11-07T01:30"):
            with self.assertRaises(publish.PublishError):
                publish.due_at(value, "America/Los_Angeles")

    def test_schedule_requires_future(self):
        with self.assertRaises(publish.PublishError):
            publish.due_at("2001-01-01T10:00", "America/Los_Angeles")

    def test_preview_can_convert_past_dates_without_changing_schedule_validation(self):
        self.assertEqual(publish.due_at("2001-01-01T10:00", "America/Los_Angeles", require_future=False), "2001-01-01T18:00:00Z")
        for value in ("2027-03-14T02:30", "2027-11-07T01:30"):
            with self.assertRaises(publish.PublishError):
                publish.due_at(value, "America/Los_Angeles", require_future=False)

    def test_channel_pause_blocks_scheduling(self):
        self.channel["isQueuePaused"] = True
        self.payload["mode"] = "now"
        with self.assertRaises(publish.PublishError):
            self.create()

    def test_cart_field_cannot_silently_publish_plain_video(self):
        self.payload["product_id"] = "123"
        with self.assertRaises(publish.PublishError):
            self.create()

    def test_mutation_timeout_is_uncertain_and_not_replayed(self):
        job = self.create()
        with patch.object(publish, "upload_media", return_value="https://ruitech.hk/buffer-media/test.mp4"), patch.object(publish, "graphql", side_effect=publish.Uncertain("timeout")) as api:
            publish.submit(job)
            self.assertEqual(publish.get_job(job["id"])["state"], "uncertain")
            publish.worker_tick()
            self.assertEqual(api.call_count, 1)

    def test_typed_mutation_error_is_failure(self):
        job = self.create()
        with patch.object(publish, "upload_media", return_value="https://ruitech.hk/buffer-media/test.mp4"), patch.object(publish, "graphql", return_value={"createPost": {"message": "Channel disconnected"}}):
            publish.submit(job)
        self.assertEqual(publish.get_job(job["id"])["state"], "failed")

    def test_scheduled_receipt_is_not_success(self):
        job = self.create()
        publish.apply_post(job, {"id": "post-test", "status": "scheduled", "dueAt": "2027-01-01T18:00:00Z"})
        self.assertEqual(publish.get_job(job["id"])["state"], "scheduled")
        publish.apply_post(job, {"id": "post-test", "status": "sent", "externalLink": "https://www.tiktok.com/@test/video/1"})
        self.assertEqual(publish.get_job(job["id"])["state"], "sent")

    def test_cancel_queued_makes_no_buffer_call(self):
        job = self.create()
        with patch.object(publish, "graphql") as api:
            self.assertEqual(publish.cancel(job["id"])["state"], "cancelled")
            api.assert_not_called()

    def test_cancel_cannot_delete_published_post(self):
        job = self.create()
        publish.apply_post(job, {"id": "post-test", "status": "scheduled"})
        with patch.object(publish, "graphql", return_value={"post": {"id": "post-test", "status": "sent", "allowedActions": ["deletePost"]}}) as api:
            with self.assertRaises(publish.PublishError):
                publish.cancel(job["id"])
            self.assertEqual(api.call_count, 1)
            self.assertEqual(publish.get_job(job["id"])["state"], "sent")

    def scheduled(self):
        job = self.create()
        post = {"id": "post-test", "text": job["text"], "status": "scheduled", "dueAt": "2027-12-01T18:00:00Z", "updatedAt": "version-1", "allowedActions": ["updatePost", "updatePostSchedule"],
                "assets": [{"type": "video", "source": "https://ruitech.hk/buffer-media/current.mp4", "video": {"title": "Current title", "thumbnailOffset": 0}}],
                "metadata": {"isAiGenerated": True}}
        publish.apply_post(job, post)
        return job, post

    def test_edit_queued_stays_local(self):
        job = self.create()
        with patch.object(publish, "graphql") as api:
            result = publish.edit_job({"id": job["id"], "version": job["updated"], "text": "new text"})
            self.assertEqual(result["text"], "new text")
            self.assertEqual(result["state"], "queued")
            api.assert_not_called()

    def test_edit_text_keeps_media_and_schedule(self):
        job, post = self.scheduled()
        receipt = {**post, "text": "new text", "updatedAt": "version-2"}
        with patch.object(publish, "graphql", side_effect=[{"post": post}, {"editPost": {"post": receipt}}]) as api:
            result = publish.edit_job({"id": job["id"], "version": "version-1", "text": "new text"})
            self.assertEqual(result["post_id"], "post-test")
            self.assertEqual(result["text"], "new text")
            value = api.call_args.args[1]["input"]
            self.assertEqual(value["text"], "new text")
            self.assertEqual(value["assets"][0]["video"], {"url": post["assets"][0]["source"], "metadata": {"title": "Current title", "thumbnailOffset": 0}})
            self.assertTrue(value["metadata"]["tiktok"]["isAiGenerated"])
            self.assertNotIn("mode", value)
            self.assertNotIn("dueAt", value)
            self.assertIn("editPost", api.call_args.args[0])
            self.assertNotIn("createPost", api.call_args.args[0])

    def test_edit_schedule_preserves_text_and_uses_la_time(self):
        job, post = self.scheduled()
        receipt = {**post, "dueAt": "2027-12-02T18:30:00Z", "updatedAt": "version-2"}
        with patch.object(publish, "graphql", side_effect=[{"post": post}, {"editPost": {"post": receipt}}]) as api:
            result = publish.edit_job({"id": job["id"], "version": "version-1", "text": job["text"], "local_time": "2027-12-02T10:30"})
            self.assertEqual(result["due_at"], receipt["dueAt"])
            value = api.call_args.args[1]["input"]
            self.assertEqual(value["dueAt"], receipt["dueAt"])
            self.assertEqual(value["mode"], "customScheduled")
            self.assertEqual(value["text"], post["text"])
            self.assertEqual(value["assets"][0]["video"]["url"], post["assets"][0]["source"])

    def test_tiktok_edit_requires_full_media_payload_before_merge(self):
        job, post = self.scheduled()
        def api(query, variables=None, **_kwargs):
            if "editPost" not in query:
                return {"post": post}
            value = variables["input"]
            if not value.get("assets"):
                return {"editPost": {"message": "Invalid post: Post must have either text or media., TikTok posts require at least one image or video."}}
            return {"editPost": {"post": {**post, "dueAt": value["dueAt"], "updatedAt": "version-2"}}}
        with patch.object(publish, "graphql", side_effect=api):
            result = publish.edit_job({"id": job["id"], "version": "version-1", "text": post["text"], "local_time": "2027-12-02T10:30"})
        self.assertEqual(result["state"], "scheduled")
        self.assertEqual(result["due_at"], "2027-12-02T18:30:00Z")

    def test_missing_video_stops_before_edit_mutation(self):
        job, post = self.scheduled()
        with patch.object(publish, "graphql", return_value={"post": {**post, "assets": []}}) as api:
            with self.assertRaises(publish.PublishError):
                publish.edit_job({"id": job["id"], "version": "version-1", "text": "new text"})
            self.assertEqual(api.call_count, 1)

    def test_edit_receipt_cannot_silently_drop_video(self):
        job, post = self.scheduled()
        with patch.object(publish, "graphql", side_effect=[{"post": post}, {"editPost": {"post": {**post, "text": "new text", "assets": []}}}]):
            with self.assertRaises(publish.Uncertain):
                publish.edit_job({"id": job["id"], "version": "version-1", "text": "new text"})
        self.assertEqual(publish.get_job(job["id"])["state"], "uncertain")

    def test_edit_stale_version_does_not_mutate(self):
        job, post = self.scheduled()
        with patch.object(publish, "graphql", return_value={"post": post}) as api:
            with self.assertRaises(publish.PublishError):
                publish.edit_job({"id": job["id"], "version": "old-version", "text": "new text"})
            self.assertEqual(api.call_count, 1)

    def test_edit_published_and_missing_permission_rejected(self):
        for overrides in ({"status": "sent"}, {"allowedActions": []}):
            job, post = self.scheduled()
            with patch.object(publish, "graphql", return_value={"post": {**post, **overrides}}) as api:
                with self.assertRaises(publish.PublishError):
                    publish.edit_job({"id": job["id"], "version": "version-1", "text": "new text"})
                self.assertEqual(api.call_count, 1)

    def test_edit_rejection_keeps_original_pending_task(self):
        job, post = self.scheduled()
        with patch.object(publish, "graphql", side_effect=[{"post": post}, {"editPost": {"message": "Cannot edit"}}]):
            with self.assertRaises(publish.PublishError):
                publish.edit_job({"id": job["id"], "version": "version-1", "text": "new text"})
        stored = publish.get_job(job["id"])
        self.assertEqual(stored["state"], "scheduled")
        self.assertEqual(stored["text"], "test")

    def test_edit_timeout_needs_matching_readback(self):
        job, post = self.scheduled()
        with patch.object(publish, "graphql", side_effect=[{"post": post}, publish.Uncertain("timeout")]):
            with self.assertRaises(publish.Uncertain):
                publish.edit_job({"id": job["id"], "version": "version-1", "text": "new text"})
        stored = publish.get_job(job["id"])
        self.assertEqual(stored["state"], "uncertain")
        with patch.object(publish, "graphql", return_value={"post": post}):
            with self.assertRaises(publish.Uncertain):
                publish.reconcile(stored)
        with patch.object(publish, "graphql", return_value={"post": {**post, "text": "new text", "updatedAt": "version-2"}}):
            publish.reconcile(stored)
        self.assertEqual(publish.get_job(job["id"])["state"], "scheduled")
        self.assertNotIn("pending_edit", publish.get_job(job["id"]))

    def test_unchanged_edit_performs_no_mutation(self):
        job, post = self.scheduled()
        with patch.object(publish, "graphql", return_value={"post": post}) as api:
            publish.edit_job({"id": job["id"], "version": "version-1", "text": post["text"]})
            self.assertEqual(api.call_count, 1)

    def test_restart_does_not_replay_ambiguous_submission(self):
        job = self.create()
        publish.save(job, "submitting")
        with patch.object(publish, "_started", False), patch.object(publish.threading, "Thread"):
            publish.start_worker()
        self.assertEqual(publish.get_job(job["id"])["state"], "uncertain")

    def test_worker_and_recovery_are_not_limited_by_ui_history(self):
        with publish.db() as conn:
            for index in range(105):
                item = {"id": str(index), "state": "submitting", "logs": []}
                conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (str(index), str(index), item["state"], json.dumps(item), time.time()))
        self.assertEqual(len(publish.jobs()), 100)
        self.assertEqual(len(publish.jobs({"submitting"})), 105)
        with patch.object(publish, "_started", False), patch.object(publish.threading, "Thread"):
            publish.start_worker()
        self.assertEqual(len(publish.jobs({"uncertain"})), 105)

    def test_graphql_http_200_errors_are_not_success(self):
        response = Mock(status_code=200)
        response.json.return_value = {"errors": [{"message": "write may have happened"}]}
        with patch.object(publish.requests, "Session") as session:
            session.return_value.post.return_value = response
            with self.assertRaises(publish.Uncertain):
                publish.graphql("mutation { test }", mutation=True)

    def test_graphql_network_error_does_not_leak_key(self):
        with patch.object(publish.requests, "Session") as session:
            session.return_value.post.side_effect = requests.Timeout("secret header")
            with self.assertRaises(publish.Uncertain) as error:
                publish.graphql("mutation { test }", mutation=True)
        self.assertNotIn("secret header", str(error.exception))
        self.assertNotIn("unit-test-key", publish.safe_error("unit-test-key-must-never-leak"))

    def test_rate_budget_stops_mutations_before_sending(self):
        with publish.db() as conn:
            conn.executemany("INSERT INTO api_calls VALUES (?)", [(time.time(),)] * 200)
        with patch.object(publish.requests, "Session") as session:
            with self.assertRaises(publish.PublishError):
                publish.graphql("mutation { test }", mutation=True)
            session.assert_not_called()

    def test_origin_and_peer_are_both_required(self):
        handler = Mock(client_address=("192.168.1.50", 12345), headers={"Host": "192.168.1.254:4003", "Origin": "http://evil.example"})
        self.assertFalse(publish.allowed(handler, True))
        handler.headers["Origin"] = "http://192.168.1.254:4003"
        self.assertTrue(publish.allowed(handler, True))
        handler.client_address = ("8.8.8.8", 12345)
        self.assertFalse(publish.allowed(handler, True))


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.token = cls.root / "token"
        cls.token.write_text("test-token")
        cls.patches = [patch.object(media, "ROOT", cls.root), patch.object(media, "TOKEN_FILE", cls.token)]
        for item in cls.patches:
            item.start()
        cls.server = media.ThreadingHTTPServer(("127.0.0.1", 0), media.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.body = b"\x00\x00\x00\x20ftypisom" + b"a" * 100
        cls.headers = {"Authorization": "Bearer test-token", "Content-Type": "video/mp4"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for item in reversed(cls.patches):
            item.stop()
        cls.temp.cleanup()

    def target(self):
        return self.base + "/buffer-media/" + uuid.uuid4().hex + ".mp4"

    def test_unauthenticated_upload_rejected(self):
        self.assertEqual(requests.put(self.target(), data=self.body, timeout=5).status_code, 401)

    def test_put_head_range_and_idempotency(self):
        target = self.target()
        response = requests.put(target, data=self.body, headers=self.headers, timeout=5)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["sha256"], hashlib.sha256(self.body).hexdigest())
        self.assertEqual(requests.head(target, timeout=5).headers["Content-Type"], "video/mp4")
        self.assertEqual(requests.get(target, headers={"Range": "bytes=0-31"}, timeout=5).content, self.body[:32])
        self.assertEqual(requests.get(target, headers={"Range": "bytes=-10"}, timeout=5).content, self.body[-10:])
        self.assertEqual(requests.get(target, headers={"Range": "bytes=9999-"}, timeout=5).status_code, 416)
        self.assertEqual(requests.put(target, data=self.body, headers=self.headers, timeout=5).status_code, 200)
        self.assertEqual(requests.put(target, data=self.body+b"b", headers=self.headers, timeout=5).status_code, 409)

    def test_path_and_non_mp4_rejected(self):
        self.assertEqual(requests.put(self.base+"/buffer-media/invalid.mp4", data=self.body, headers=self.headers, timeout=5).status_code, 400)
        self.assertEqual(requests.put(self.target(), data=b"not mp4", headers=self.headers, timeout=5).status_code, 400)
        self.assertEqual(requests.get(self.base+"/buffer-media/token", timeout=5).status_code, 404)


if __name__ == "__main__":
    unittest.main()
