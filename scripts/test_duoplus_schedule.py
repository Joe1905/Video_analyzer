"""Scheduler tests with virtual time, temporary SQLite and no network or phones."""
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import duoplus
import duoplus_schedule as schedule
import duoplus_workflow as workflow


def stamp(text, zone="America/Los_Angeles"):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp()


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.store = schedule.Store(self.data)
        self.now = stamp("2026-10-14T08:59:00")
        self.worker = schedule.Scheduler(self.data, clock=lambda: self.now)
        self.body = {"start_date": "2026-10-14", "end_date": "2026-10-16",
                     "start_time": "09:00", "end_time": "18:00", "repeat": True,
                     "interval_seconds": 1800, "payload": {
                         "workflow": workflow.template(), "card_key": "builtin-search",
                         "name": "单主题搜索", "image_id": "phone-1", "mode": "search",
                         "topics": "AI translator", "budget_seconds": 300, "ai_max_steps": 16,
                         "auto_power_off": True, "auto_power_on": True}}
        self.network = patch("requests.Session.post", side_effect=AssertionError("No network in offline tests"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.config = patch.object(duoplus, "load_config", return_value={"api_key": "offline", "image_id": "phone-1"})
        self.config.start()
        self.addCleanup(self.config.stop)

    def tearDown(self):
        workflow.JOBS.clear()
        workflow.EVENTS.clear()
        schedule._WORKERS.clear()
        if duoplus.LOCK.locked():
            duoplus.LOCK.release()

    def enabled(self, body=None):
        plan = self.store.save(body or self.body, self.now)
        return self.store.enable(plan["id"], True, self.now)

    def complete_claim(self, plan, status="complete"):
        job = self.store.claim(plan, self.now, stamp("2026-10-14T18:00"))
        job.update(status=status, finished_at=self.now + 120, logs=[{"kind": "complete", "message": "offline"}])
        self.store.put_run(job)
        self.now += 120
        self.worker.tick()
        return job

    def test_default_zone_and_save_does_not_enable_or_call_phone(self):
        plan = self.store.save(self.body, self.now)
        self.assertFalse(plan["enabled"])
        self.assertEqual(plan["timezone"], "America/Los_Angeles")
        self.assertEqual(plan["clock_source"], "fallback")
        with patch.object(workflow, "start") as start:
            self.worker.tick()
            start.assert_not_called()
        self.assertEqual(schedule.Store(self.data).schedule(plan["id"])["payload"]["topics"], "AI translator")

    def test_date_window_and_daylight_saving(self):
        plan = self.store.save(self.body, self.now)
        self.assertEqual(schedule.window(plan, self.now)[0], stamp("2026-10-14T09:00"))
        self.assertEqual(schedule.window(plan, stamp("2026-10-14T18:00"))[0], stamp("2026-10-15T09:00"))
        self.assertIsNone(schedule.window(plan, stamp("2026-10-16T18:00")))
        plan.update(start_date="2026-10-31", end_date="2026-11-02")
        before = schedule.window(plan, stamp("2026-10-31T08:00"))[0]
        after = schedule.window(plan, stamp("2026-11-01T08:00"))[0]
        self.assertEqual(after - before, 25 * 3600, "LA fallback must include the DST transition")

    def test_device_zone_and_clock_offset(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO phone_clocks VALUES(?,?,?,?)", ("phone-1", "Asia/Tokyo", 120, self.now))
        plan = self.store.save(self.body, self.now)
        self.assertEqual(plan["timezone"], "Asia/Tokyo")
        self.assertEqual(plan["clock_offset"], 120)
        self.assertEqual(plan["clock_source"], "device")
        value = stamp("2026-10-14T08:58:00", "Asia/Tokyo")
        self.assertEqual(schedule.window(plan, value)[0], value)

    def test_invalid_plans_and_repeat_power_options(self):
        for changes in ({"end_date": "2026-10-13"}, {"end_time": "08:00"}, {"repeat": "true"},
                        {"interval_seconds": 0}, {"start_date": "invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save({**self.body, **changes}, self.now)
        invalid = copy.deepcopy(self.body)
        invalid["payload"]["auto_power_on"] = False
        with self.assertRaisesRegex(ValueError, "自动开机"):
            self.store.save(invalid, self.now)

    def test_once_and_repeat_interval_from_completion(self):
        self.now = stamp("2026-10-14T09:00")
        plan = self.enabled()
        job = self.complete_claim(plan)
        updated = self.store.schedule(plan["id"])
        self.assertEqual(updated["next_run_at"], job["finished_at"] + 1800)
        self.assertEqual(self.store.history("builtin-search")[0]["status"], "complete")
        body = copy.deepcopy(self.body)
        body.update(repeat=False)
        body["payload"]["card_key"] = "once"
        plan = self.enabled(body)
        self.complete_claim(plan)
        self.assertFalse(self.store.schedule(plan["id"])["enabled"])
        self.assertEqual(self.store.schedule(plan["id"])["status"], "complete")

    def test_atomic_claim_and_repeated_tick_never_duplicate_dispatch(self):
        self.now = stamp("2026-10-14T09:00")
        plan = self.enabled()
        with patch.object(workflow, "start") as start:
            self.worker.tick()
            self.worker.tick()
            self.assertEqual(start.call_count, 1)
        other = schedule.Store(self.data)
        self.assertIsNone(other.claim(plan, self.now, self.now + 300))
        self.assertEqual(self.store.history(plan["card_key"])[0]["status"], "queued")

    def test_busy_phone_is_skipped_and_not_consumed_for_once(self):
        self.now = stamp("2026-10-14T09:00")
        body = {**self.body, "repeat": False}
        plan = self.enabled(body)
        duoplus.LOCK.acquire()
        with patch.object(workflow, "start") as start:
            self.worker.tick()
            start.assert_not_called()
        self.assertEqual(self.store.history(plan["card_key"])[0]["status"], "skipped")
        self.assertTrue(self.store.schedule(plan["id"])["enabled"])
        self.assertEqual(self.store.schedule(plan["id"])["next_run_at"], self.now + 1800)

    def test_failure_creates_separate_log_and_no_retry(self):
        self.now = stamp("2026-10-14T09:00")
        plan = self.enabled()
        with patch.object(workflow, "start", side_effect=ValueError("offline error")) as start:
            self.worker.tick()
            self.worker.tick()
            self.assertEqual(start.call_count, 1)
        record = self.store.history(plan["card_key"])[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(self.store.run(record["id"])["logs"][0]["message"], "offline error")

    def test_pause_no_future_dispatch_and_window_end_cancels_current(self):
        self.now = stamp("2026-10-14T09:00")
        plan = self.enabled()
        job = self.store.claim(plan, self.now, stamp("2026-10-14T18:00"))
        self.store.enable(plan["id"], False, self.now)
        workflow.JOBS[job["id"]] = {"status": "running"}
        workflow.EVENTS[job["id"]] = threading.Event()
        self.worker.tick()
        self.assertFalse(workflow.EVENTS[job["id"]].is_set(), "Pause must not cancel the current attempt")
        self.now = stamp("2026-10-14T18:00")
        self.worker.tick()
        self.assertTrue(workflow.EVENTS[job["id"]].is_set())
        self.assertIn("结束时间", workflow.JOBS[job["id"]]["cancel_reason"])

    def test_recovery_marks_interrupted_and_skips_missed_intervals(self):
        self.now = stamp("2026-10-14T09:00")
        plan = self.enabled()
        job = self.store.claim(plan, self.now, self.now + 300)
        self.now = stamp("2026-10-15T12:00")
        self.store.recover(self.now)
        self.assertEqual(self.store.run(job["id"])["status"], "interrupted")
        self.assertEqual(self.store.schedule(plan["id"])["next_run_at"], self.now + 1800)
        self.assertIsNone(self.store.schedule(plan["id"])["active_run_id"])

    def test_device_clock_success_and_fallback_never_power_on(self):
        with patch.object(duoplus, "list_phones", return_value=[{"id": "phone-1", "status": 1}]), \
             patch.object(duoplus, "request", return_value={"data": {"success": True, "content": "Europe/London\n" + str(int(self.now))}}) as request, \
             patch.object(schedule.time, "time", return_value=self.now):
            info = schedule.read_device_clock(self.data, "phone-1")
            self.assertEqual(info["timezone"], "Europe/London")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[1], "/api/v1/cloudPhone/command")
        with patch.object(duoplus, "list_phones", return_value=[{"id": "phone-2", "status": 2}]), \
             patch.object(duoplus, "request") as request:
            self.assertEqual(schedule.read_device_clock(self.data, "phone-2")["timezone"], "America/Los_Angeles")
            request.assert_not_called()
        self.assertFalse(duoplus.LOCK.locked())

    def test_auto_power_on_submits_once_and_obeys_cancellation(self):
        job = {"id": "power-test", "image_id": "phone-1", "auto_power_on": True,
               "budget_seconds": 300, "logs": [], "started_monotonic": 0}
        runner = workflow.Runner(job, {}, [], [], threading.Event(), self.data)
        with patch.object(duoplus, "list_phones", side_effect=[
            [{"id": "phone-1", "status": 2}], [{"id": "phone-1", "status": 10}],
            [{"id": "phone-1", "status": 1}]]), patch.object(runner, "wait"), \
             patch.object(duoplus, "request", return_value={"data": {"success": ["phone-1"]}}) as request, \
             patch.object(duoplus, "get_phone", return_value={"id": "phone-1"}):
            runner.prepare_phone()
            self.assertEqual(request.call_count, 1)
        runner.event.set()
        with patch.object(duoplus, "request") as request, self.assertRaises(ValueError):
            runner.prepare_phone()
        request.assert_not_called()

    def test_manual_failure_history_survives_job_memory_eviction(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline"}), \
             patch.object(duoplus, "get_phone", side_effect=ValueError("offline phone failure")), \
             patch.object(workflow.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            result = workflow.start({"image_id": "phone-1"}, {**self.body["payload"], "auto_power_on": False,
                                                               "auto_power_off": False}, self.data)
        workflow.JOBS.clear()
        handler = SimpleNamespace(command="GET")
        responses = []
        workflow.handle(handler, urlparse("/api/duoplus/workflow/job?id=" + result["id"]),
                        self.data, lambda _, status, data: responses.append((status, data)))
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["status"], "failed")
        self.assertIn("offline phone failure", responses[0][1]["error"])
        self.assertEqual(self.store.history("builtin-search")[0]["status"], "failed")

    def test_scheduler_start_is_idempotent_and_recovery_is_offline(self):
        with patch.object(schedule.threading, "Thread") as thread:
            self.assertIs(schedule.start_scheduler(self.data), schedule.start_scheduler(self.data))
            self.assertEqual(thread.call_count, 1)


if __name__ == "__main__":
    unittest.main()
