"""Offline contract tests. Network calls and phone actions are always mocked."""
import base64
import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
from PIL import Image

import duoplus
import duoplus_workflow as workflow


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.no_network = patch("requests.Session.post", side_effect=AssertionError("Offline tests must not use the network"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.original = workflow.template()

    def tearDown(self):
        workflow.JOBS.clear()
        workflow.EVENTS.clear()
        if duoplus.LOCK.locked():
            duoplus.LOCK.release()

    def runner(self, mode="search", power_off=False):
        job = {"id": "offline-test", "status": "queued", "mode": mode,
               "image_id": "phone-1", "topics": ["AI translator"], "budget_seconds": 300,
               "ai_max_steps": 16, "auto_power_off": power_off, "logs": [], "screen": {},
               "started_monotonic": time.monotonic()}
        workflow.JOBS[job["id"]] = job
        runner = workflow.Runner(job, {"api_key": "test-key"}, workflow.compile_workflow(self.original),
                                 ["AI translator"], threading.Event(), Path(self.directory.name))
        runner.phone = {"id": "phone-1"}
        runner.screen = {"current_app": workflow.PACKAGE, "screenshot": "iVBORw0KGgo="}
        return runner

    def search_node(self):
        return next(node for node in workflow.flatten(workflow.compile_workflow(self.original))
                    if node["id"] == workflow.SEARCH_NODE_ID)

    def decision(self, action=None, params=None):
        return {"status": "action" if action else "done", "observation": "Visible screen evidence",
                **({"action": action, "params": params or {}} if action else {})}

    def test_actual_template_and_edge_order(self):
        preview = workflow.preview(self.original)
        self.assertEqual(preview["node_count"], 37)
        self.assertEqual(len(preview["ai_nodes"]), 5)
        self.assertTrue(preview["search_available"])
        self.original["nodes"].reverse()
        graph = workflow.compile_workflow(self.original)
        self.assertEqual(graph[0]["data"]["action"], "START")
        self.assertEqual(graph[1]["data"]["action"], "EXECUTE_ADB")

    def test_disconnected_and_cyclic_graphs_are_rejected(self):
        for edges in (self.original["edges"][:-1], self.original["edges"] + [
            {"source": "1788173890918", "target": "1"}]):
            candidate = copy.deepcopy(self.original)
            candidate["edges"] = edges
            with self.assertRaises(ValueError):
                workflow.compile_workflow(candidate)

    def test_unsupported_adb_rejected_before_execution(self):
        candidate = copy.deepcopy(self.original)
        node = next(n for n in candidate["nodes"] if n["type"] == "EXECUTE_ADB")
        node["data"]["adb_command"] = "rm -rf /sdcard"
        with self.assertRaises(ValueError):
            workflow.compile_workflow(candidate)

    def test_decision_rejects_unsafe_params(self):
        invalid = [self.decision("EXECUTE_ADB", {}), self.decision("CLICK_COORDINATE", {"x": 1001, "y": 0}),
                   self.decision("CLICK_COORDINATE", {"x": 500, "y": 500, "command": "shell"}),
                   self.decision("INPUT_CONTENT", {"content": "query", "clear_first": "false"}),
                   self.decision("SLIDE_PAGE", {"direction": "up", "start_x": 10}),
                   self.decision("WAIT_TIME", {"wait_time": float("nan")}),
                   self.decision("OPEN_APP", {"package_name": "other.app"}),
                   {"status": "done", "observation": ""}]
        for decision in invalid:
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                workflow.validate_decision(decision)

    def test_unsupported_loop_state_and_string_reversal_are_rejected(self):
        candidate = copy.deepcopy(self.original)
        node = next(n for n in candidate["nodes"] if n["type"] == "FOR_DATA")
        node["data"]["reversal"] = "false"
        with self.assertRaises(ValueError):
            workflow.compile_workflow(candidate)
        node["data"]["reversal"] = False
        node["data"]["children"]["nodes"][-1]["data"]["loop_location_save_path"] = "unexpected"
        with self.assertRaises(ValueError):
            workflow.compile_workflow(candidate)

    def test_search_completion_requires_input_submit_and_result_click(self):
        runner = self.runner()
        with patch.object(runner, "observe"), patch.object(workflow, "decide", return_value=(self.decision(), {})):
            with self.assertRaisesRegex(ValueError, "未输入"):
                runner.ai(self.search_node(), {"S-Content": "AI translator"})
        decisions = [self.decision("INPUT_CONTENT", {"content": "AI translator", "clear_first": True}),
                     self.decision("KEYBOARD_OPERATION", {"key": "enter"}),
                     self.decision("CLICK_COORDINATE", {"x": 300, "y": 400}), self.decision()]
        with patch.object(runner, "observe"), patch.object(runner, "action") as action, \
                patch.object(workflow, "decide", side_effect=[(item, {}) for item in decisions]) as decide:
            runner.ai(self.search_node(), {"S-Content": "AI translator"})
            self.assertEqual(action.call_count, 3)
            self.assertIn("AI translator", decide.call_args_list[0].args[0])

    def test_ai_cannot_change_search_topic(self):
        runner = self.runner()
        with patch.object(runner, "observe"), patch.object(runner, "action") as action, \
                patch.object(workflow, "decide", return_value=(self.decision("INPUT_CONTENT", {"content": "different topic"}), {})):
            with self.assertRaisesRegex(ValueError, "输入词"):
                runner.ai(self.search_node(), {"S-Content": "AI translator"})
            action.assert_not_called()

    def test_search_mode_stops_before_interaction_loops(self):
        runner = self.runner()
        with patch.object(runner, "wait"), patch.object(runner, "observe"), patch.object(runner, "action"), \
                patch.object(runner, "ai") as ai, patch.object(duoplus, "request", return_value={"data": {"success": True}}) as request:
            with self.assertRaises(workflow.SearchComplete):
                runner.graph_run(runner.graph, {})
            self.assertEqual(ai.call_count, 1)
            self.assertEqual(ai.call_args.args[1]["S-Content"], "AI translator")
            self.assertEqual(request.call_count, 1)  # Initial force-stop only.
            self.assertFalse(any(log["kind"] == "loop" for log in runner.job["logs"]))

    def test_full_mode_nested_loops_and_probabilities(self):
        runner = self.runner(mode="full")
        with patch.object(runner, "wait"), patch.object(runner, "observe"), patch.object(runner, "action"), \
                patch.object(runner, "ai") as ai, patch.object(duoplus, "request", return_value={"data": {"success": True}}) as request, \
                patch.object(workflow.random, "randint", return_value=1), patch.object(workflow.random, "random", return_value=0):
            runner.graph_run(runner.graph, {})
            self.assertEqual(ai.call_count, 5)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(sum(log["kind"] == "condition" for log in runner.job["logs"]), 4)

    def test_coordinate_conversion_uses_physical_template_size(self):
        runner = self.runner(mode="full")
        node = next(node for node in workflow.flatten(runner.graph) if node["data"]["action"] == "CLICK_COORDINATE")
        with patch.object(runner, "wait"), patch.object(runner, "action") as action, \
                patch.object(workflow.random, "uniform", return_value=0):
            runner.graph_run([node], {})
            self.assertEqual(action.call_args.args[1]["x"], 859)
            self.assertEqual(action.call_args.args[1]["y"], 344)

    def test_failed_action_is_never_retried(self):
        runner = self.runner()
        with patch.object(duoplus, "submit", side_effect=ValueError("uncertain")) as submit, patch.object(runner, "observe") as observe:
            with self.assertRaises(ValueError):
                runner.action("PAGE_BACK", {})
            self.assertEqual(submit.call_count, 1)
            observe.assert_not_called()

    def test_expired_or_cancelled_runner_sends_no_action(self):
        for cancel in (False, True):
            runner = self.runner()
            runner.deadline = time.monotonic() - 1
            if cancel:
                runner.event.set()
            with patch.object(duoplus, "submit") as submit, self.assertRaises(ValueError):
                runner.action("PAGE_BACK", {})
            submit.assert_not_called()

    def test_missing_screenshot_blocks_model_request(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline"}), self.assertRaisesRegex(ValueError, "截图"):
            workflow.decide("goal", {}, [], time.monotonic() + 10)

    def test_webp_screenshot_reaches_model_as_png(self):
        source = io.BytesIO()
        Image.new("RGB", (72, 128), "blue").save(source, format="WEBP")
        shot = base64.b64encode(source.getvalue()).decode("ascii")
        response = SimpleNamespace(ok=True, json=lambda: {"choices": [{"finish_reason": "stop",
                                   "message": {"content": json.dumps(self.decision())}}]})
        for value in (shot, "data:image/webp;base64," + shot):
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline"}), \
                    patch("requests.Session.post", return_value=response) as post:
                _, usage = workflow.decide("goal", {"screenshot": value}, [], time.monotonic() + 10)
                self.assertIn("model_elapsed_seconds", usage)
                self.assertNotIn("elapsed_seconds", usage)
                image_url = post.call_args.kwargs["json"]["messages"][1]["content"][1]["image_url"]["url"]
                self.assertTrue(image_url.startswith("data:image/png;base64,"))
                with Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as image:
                    self.assertEqual(image.size, (72, 128))
        runner = self.runner()
        with patch.object(workflow.time, "monotonic", return_value=runner.job["started_monotonic"] + 180):
            runner.log("ai", **usage)
        self.assertEqual(runner.job["logs"][-1]["elapsed_seconds"], 180)
        self.assertEqual(runner.job["logs"][-1]["model_elapsed_seconds"], usage["model_elapsed_seconds"])
        with patch.object(duoplus, "submit", return_value={"screenshot": shot}):
            runner.observe()
        self.assertTrue(runner.screen["screenshot"].startswith("data:image/png;base64,"))
        with Image.open(Path(self.directory.name) / "screen-001.png") as image:
            self.assertEqual(image.format, "PNG")

    def test_corrupt_webp_blocks_model_request(self):
        shot = base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPinvalid").decode("ascii")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline"}), \
                patch("requests.Session.post") as post, self.assertRaisesRegex(ValueError, "WebP"):
            workflow.decide("goal", {"screenshot": shot}, [], time.monotonic() + 10)
        post.assert_not_called()

    def test_worker_failure_releases_lock_and_writes_result(self):
        runner = self.runner(power_off=False)
        duoplus.LOCK.acquire()
        with patch.object(duoplus, "get_phone", side_effect=ValueError("not ready")), patch.object(duoplus, "request") as request:
            runner.run()
            request.assert_not_called()
        self.assertFalse(duoplus.LOCK.locked())
        self.assertEqual(runner.job["status"], "failed")
        self.assertEqual(json.loads((Path(self.directory.name) / "result.json").read_text())["status"], "failed")

    def test_cleanup_requests_shutdown_once_on_cancellation(self):
        runner = self.runner(power_off=True)
        runner.event.set()
        duoplus.LOCK.acquire()
        with patch.object(duoplus, "get_phone", side_effect=ValueError("cancelled")), \
                patch.object(duoplus, "request", return_value={"data": {"success": ["phone-1"]}}) as request:
            runner.run()
        self.assertEqual(runner.job["status"], "cancelled")
        self.assertTrue(runner.job["power_off_requested"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], "/api/v1/cloudPhone/powerOff")

    def test_preview_and_job_routes_never_call_cloud_even_when_busy(self):
        handler = SimpleNamespace(command="GET", client_address=("127.0.0.1", 1234),
                                  headers={"Host": "127.0.0.1:4003"}, rfile=io.BytesIO())
        responses = []
        duoplus.LOCK.acquire()
        with patch.object(duoplus, "request") as request:
            duoplus.handle(handler, urlparse("/api/duoplus/workflow"), Path(self.directory.name),
                           lambda _, status, data: responses.append((status, data)))
            self.assertEqual(responses[0][0], 200)
            self.assertEqual(responses[0][1]["preview"]["node_count"], 37)
            request.assert_not_called()

    def test_budget_default_and_limit_preserve_explicit_shorter_budgets(self):
        for supplied, expected in (({}, 500), ({"budget_seconds": 500}, 500), ({"budget_seconds": 300}, 300)):
            with self.subTest(supplied=supplied):
                _, _, job = workflow.prepare({"topics": "AI translator", **supplied}, "phone-1")
                self.assertEqual(job["budget_seconds"], expected)

    def test_invalid_run_options_rejected_before_lock_or_cloud(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline"}):
            for body in ({"topics": "one\ntwo"}, {"topics": "AI translator", "budget_seconds": 501},
                         {"topics": "AI translator", "ai_max_steps": 0},
                         {"topics": "AI translator", "workflow": {}},
                         {"topics": "AI translator", "auto_power_off": "true"}):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    workflow.start({"image_id": "phone-1"}, body, Path(self.directory.name))
                self.assertFalse(duoplus.LOCK.locked())


if __name__ == "__main__":
    unittest.main()
