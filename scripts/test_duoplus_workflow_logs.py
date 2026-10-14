"""Durable log/evidence checks using temporary state; never contact a cloud phone."""
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import duoplus
import duoplus_workflow as workflow
from duoplus_schedule import Store


class WorkflowLogsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.store = Store(self.data)
        self.run_id = "a" * 32
        self.job = {"id": self.run_id, "card_key": "offline", "image_id": "phone-1",
                    "status": "running", "started_at": time.time(), "started_monotonic": time.monotonic(),
                    "budget_seconds": 500, "auto_power_off": True, "mode": "full", "logs": [], "screen": {}}
        self.store.put_run(self.job)
        self.network = patch("requests.Session.post", side_effect=AssertionError("No live network in log tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def tearDown(self):
        workflow.JOBS.clear()
        workflow.EVENTS.clear()
        if duoplus.LOCK.locked():
            duoplus.LOCK.release()

    def runner(self, store=None):
        directory = self.data.parent / "output" / "duoplus_workflows" / self.run_id
        directory.mkdir(parents=True)
        workflow.JOBS[self.run_id] = self.job
        return workflow.Runner(self.job, {}, [], [], threading.Event(), directory,
                               store=self.store if store is None else store)

    def test_cursor_survives_reopen_and_keeps_more_than_memory_tail(self):
        tail = []
        for index in range(505):
            tail.append(self.store.append_event(self.run_id, {"kind": "node", "index": index}))
        self.job.update(status="complete", logs=tail[-500:], log_sequence=505)
        self.store.put_run(self.job)
        reader = Store(self.data)
        cursor, events = 0, []
        while True:
            page = reader.events(self.run_id, cursor, 200)
            events.extend(page["events"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual([event["seq"] for event in events], list(range(1, 506)))
        self.assertTrue(page["terminal"])
        self.assertEqual(cursor, 505)
        self.assertNotIn("logs", page["run"])
        self.assertEqual(reader.events(self.run_id, cursor)["events"], [])

    def test_legacy_logs_and_restart_error_remain_readable(self):
        self.job["logs"] = [{"kind": "node"}, {"kind": "wait"}]
        self.store.put_run(self.job)
        self.assertEqual(self.store.events(self.run_id, 1, 1)["events"][0]["kind"], "wait")
        self.store.recover()
        page = self.store.events(self.run_id)
        self.assertEqual([event["kind"] for event in page["events"]], ["node", "wait", "error"])
        self.assertEqual(page["run"]["status"], "interrupted")
        self.assertTrue(page["terminal"])

    def test_failure_records_attempt_phase_and_always_cleans_up(self):
        runner = self.runner()
        runner.job["current_node"] = "failed-node"
        def request(config, path, payload, **kwargs):
            return {"data": {"success": True, "content": "Physical size: 1080x1920"}} if path.endswith("command") else {
                "data": {"success": ["phone-1"]}}
        duoplus.LOCK.acquire()
        with patch.object(duoplus, "get_phone", return_value={"id": "phone-1"}), \
                patch.object(duoplus, "request", side_effect=request) as platform, \
                patch.object(duoplus, "submit", side_effect=workflow.requests.Timeout()), \
                patch.object(runner, "graph_run", side_effect=lambda *args: runner.action("PAGE_BACK", {})):
            runner.run()
        page = Store(self.data).events(self.run_id)
        self.assertEqual(page["run"]["status"], "failed")
        self.assertEqual(page["run"]["failed_node"], "failed-node")
        self.assertEqual(page["run"]["failed_action"]["phase"], "sending")
        self.assertEqual(page["events"][0]["kind"], "action_start")
        self.assertEqual(page["events"][-1]["kind"], "finish")
        self.assertEqual(page["run"]["error_type"], "Timeout")
        self.assertEqual(platform.call_args_list[-1].args[1], "/api/v1/cloudPhone/powerOff")
        self.assertFalse(duoplus.LOCK.locked())

    def test_log_write_failure_stops_new_actions_but_not_shutdown(self):
        broken = SimpleNamespace(append_event=Mock(side_effect=OSError("offline disk failure")), put_run=Mock())
        runner = self.runner(store=broken)
        duoplus.LOCK.acquire()
        with patch.object(duoplus, "get_phone", return_value={"id": "phone-1"}), \
                patch.object(duoplus, "request", side_effect=[
                    {"data": {"success": True, "content": "Physical size: 1080x1920"}},
                    {"data": {"success": ["phone-1"]}}]) as platform, \
                patch.object(duoplus, "submit") as submit, \
                patch.object(runner, "graph_run", side_effect=lambda *args: runner.action("PAGE_BACK", {})):
            runner.run()
        submit.assert_not_called()
        self.assertEqual(runner.job["status"], "cancelled")
        self.assertTrue(runner.job["power_off_requested"])
        self.assertIn("OSError", runner.job["log_error"])
        self.assertEqual(platform.call_count, 2)
        self.assertFalse(duoplus.LOCK.locked())
        self.assertTrue((runner.directory / "result.json").is_file())

    def test_log_api_is_read_only_when_phone_locked_and_rejects_bad_cursor(self):
        self.store.append_event(self.run_id, {"kind": "node"})
        handler = SimpleNamespace(command="GET", client_address=("127.0.0.1", 1234),
                                  headers={"Host": "127.0.0.1:4003"})
        replies = []
        duoplus.LOCK.acquire()
        with patch.object(duoplus, "request") as cloud:
            workflow.handle(handler, urlparse("/api/duoplus/workflow/logs?id=" + self.run_id), self.data,
                            lambda _, status, body: replies.append((status, body)))
            self.assertEqual(replies[-1][0], 200)
            self.assertEqual(replies[-1][1]["next_cursor"], 1)
            for query in ("&after=-1", "&limit=201", "&after=NaN"):
                workflow.handle(handler, urlparse("/api/duoplus/workflow/logs?id=" + self.run_id + query), self.data,
                                lambda _, status, body: replies.append((status, body)))
                self.assertEqual(replies[-1][0], 400)
            cloud.assert_not_called()

    def test_evidence_api_rejects_traversal_and_returns_only_saved_image(self):
        runner = self.runner()
        image = b"\x89PNG\r\n\x1a\n"
        (runner.directory / "screen-001.png").write_bytes(image)
        headers, replies = {}, []
        handler = SimpleNamespace(command="GET", headers={}, wfile=io.BytesIO(),
                                  send_response=lambda status: replies.append(status),
                                  send_header=lambda key, value: headers.update({key: value}), end_headers=lambda: None)
        workflow.handle(handler, urlparse("/api/duoplus/workflow/evidence?id=" + self.run_id + "&file=screen-001.png"),
                        self.data, lambda _, status, body: replies.append(status))
        self.assertEqual(handler.wfile.getvalue(), image)
        self.assertEqual(headers["Content-Type"], "image/png")
        for filename in ("../result.json", "result.json", "screen-999.png"):
            workflow.handle(handler, urlparse("/api/duoplus/workflow/evidence?id=" + self.run_id + "&file=" + filename),
                            self.data, lambda _, status, body: replies.append(status))
            self.assertEqual(replies[-1], 400)


if __name__ == "__main__":
    unittest.main()
