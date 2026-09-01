#!/usr/bin/env python3
"""Contract tests for the chat video-analysis subprocess executors."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import tools


@contextmanager
def no_video_analyze_lock():
    yield


class FinishedProcess:
    pid = 12345

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeouts: list[int | None] = []

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self.timeouts.append(timeout)
        return self.stdout, self.stderr


class VideoAnalyzeExecutorTests(unittest.TestCase):
    def test_timeout_normalization_and_output_directory_resolution(self) -> None:
        for raw, expected in (("2", 30), ("9999", 1800), ("bad", 600)):
            with self.subTest(raw=raw), patch.dict(os.environ, {"VIDEO_ANALYZE_TIMEOUT": raw}):
                self.assertEqual(tools._video_analyze_timeout_seconds(), expected)
        self.assertEqual(tools._video_analyze_timeout_seconds("45"), 45)
        with patch.dict(os.environ, {"VIDEO_ANALYZE_TIMEOUT": "600"}):
            self.assertEqual(tools._video_analyze_timeout_seconds(None), 600)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(tools, "ROOT", root), patch.object(tools, "get_video_by_filename", return_value={"extraction_dir": "registry-output"}):
                self.assertEqual(tools._video_output_dir("fixture.mp4"), root / "output" / "registry-output")
            for record in ({"extraction_dir": ""}, None):
                with self.subTest(record=record), patch.object(tools, "ROOT", root), patch.object(tools, "get_video_by_filename", return_value=record):
                    self.assertEqual(tools._video_output_dir("fixture.mp4"), root / "output" / "fixture.mp4")

    def test_standard_cache_and_success_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "video-output"
            output_dir.mkdir()
            analysis = output_dir / "analysis.json"
            popen = Mock(side_effect=AssertionError("cache must not spawn a subprocess"))
            lock = Mock(side_effect=AssertionError("cache must not acquire the analysis lock"))
            memory = Mock(side_effect=AssertionError("cache must not inspect memory"))
            mark = Mock(side_effect=AssertionError("cache must not mark extraction"))
            with patch.object(tools, "_video_output_dir", return_value=output_dir), patch.object(tools.subprocess, "Popen", popen), patch.object(tools, "_video_analyze_lock", lock), patch.object(tools, "_require_video_analyze_memory", memory), patch.object(tools, "mark_extracted", mark):
                analysis.write_text(json.dumps({"summary": "cached"}), encoding="utf-8")
                self.assertEqual(tools._run_video_analyze("fixture.mp4"), {"summary": "cached", "_cache": {"hit": True, "provider": "video_registry", "endpoint": "analysis"}})
                analysis.write_text(json.dumps(["non-dict cached payload"]), encoding="utf-8")
                self.assertEqual(tools._run_video_analyze("fixture.mp4"), ["non-dict cached payload"])
            popen.assert_not_called()
            lock.assert_not_called()
            memory.assert_not_called()
            mark.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "video-output"
            popen_calls: list[tuple[list[str], dict]] = []
            processes: list[FinishedProcess] = []

            def popen(command: list[str], **kwargs: object) -> FinishedProcess:
                popen_calls.append((command, kwargs))
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "analysis.json").write_text(json.dumps({"artifact": True}), encoding="utf-8")
                process = FinishedProcess("raw stdout", "raw stderr")
                processes.append(process)
                return process

            with patch.dict(os.environ, {"VIDEO_ANALYZE_TIMEOUT": "61", "VIDEO_ANALYZE_TEST_ENV": "kept"}), patch.object(tools, "_video_output_dir", return_value=output_dir), patch.object(tools, "_video_analyze_lock", no_video_analyze_lock), patch.object(tools, "_require_video_analyze_memory"), patch.object(tools.subprocess, "Popen", side_effect=popen), patch.object(tools, "mark_extracted") as mark:
                self.assertEqual(tools._run_video_analyze("fixture.mp4"), {"artifact": True})
            command, kwargs = popen_calls[0]
            self.assertEqual(command, ["bash", str(tools.SCRIPTS_DIR / "analyze_one.sh"), "fixture.mp4"])
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertIs(kwargs["stderr"], subprocess.PIPE)
            self.assertTrue(kwargs["text"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["cwd"], tools.ROOT)
            self.assertEqual(kwargs["env"]["ANALYSIS_OUTPUT_DIR"], str(output_dir))
            self.assertEqual(kwargs["env"]["VIDEO_ANALYZE_TEST_ENV"], "kept")
            self.assertEqual(processes[0].timeouts, [61])
            mark.assert_called_once_with("fixture.mp4", output_dir.name)

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "video-output"
            with patch.object(tools, "_video_output_dir", return_value=output_dir), patch.object(tools, "_video_analyze_lock", no_video_analyze_lock), patch.object(tools, "_require_video_analyze_memory"), patch.object(tools.subprocess, "Popen", return_value=FinishedProcess("only stdout")), patch.object(tools, "mark_extracted") as mark:
                self.assertEqual(tools._run_video_analyze("fixture.mp4", timeout_seconds=70), {"output": "only stdout"})
            mark.assert_called_once_with("fixture.mp4", output_dir.name)

    def test_standard_failures_timeout_and_lock_contract(self) -> None:
        for stdout, stderr, expected in (("stdout", "stderr", "stderr"), ("stdout", "", "stdout"), ("", "", "Exit code 7")):
            with self.subTest(stdout=stdout, stderr=stderr), tempfile.TemporaryDirectory() as directory:
                with patch.object(tools, "_video_output_dir", return_value=Path(directory) / "output"), patch.object(tools, "_video_analyze_lock", no_video_analyze_lock), patch.object(tools, "_require_video_analyze_memory"), patch.object(tools.subprocess, "Popen", return_value=FinishedProcess(stdout, stderr, 7)), patch.object(tools, "mark_extracted") as mark:
                    with self.assertRaisesRegex(RuntimeError, expected):
                        tools._run_video_analyze("fixture.mp4")
                mark.assert_not_called()

        popen = Mock(side_effect=AssertionError("Popen must not be reached"))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"VIDEO_ANALYZE_MIN_AVAILABLE_MB": "4096"}), patch.object(tools, "_video_output_dir", return_value=Path(directory) / "output"), patch.object(tools, "_video_analyze_lock", no_video_analyze_lock), patch.object(tools, "_available_memory_bytes", return_value=1), patch.object(tools.subprocess, "Popen", popen):
            with self.assertRaisesRegex(RuntimeError, "memory available"):
                tools._run_video_analyze("fixture.mp4")
        popen.assert_not_called()

        class Fcntl:
            LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4

            def __init__(self, blocked: bool = False) -> None:
                self.blocked, self.calls = blocked, []

            def flock(self, _fd: int, flags: int) -> None:
                self.calls.append(flags)
                if self.blocked and flags != self.LOCK_UN:
                    raise BlockingIOError()

        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "data" / "video_analyze.lock"
            fcntl = Fcntl()
            with patch.object(tools, "fcntl", fcntl), patch.object(tools, "VIDEO_ANALYZE_LOCK_PATH", lock_path):
                with self.assertRaisesRegex(RuntimeError, "release test"):
                    with tools._video_analyze_lock():
                        raise RuntimeError("release test")
            self.assertEqual(fcntl.calls, [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN])

            blocked = Fcntl(blocked=True)
            with patch.dict(os.environ, {"VIDEO_ANALYZE_LOCK_TIMEOUT_SECONDS": "0"}), patch.object(tools, "fcntl", blocked), patch.object(tools, "VIDEO_ANALYZE_LOCK_PATH", lock_path), patch.object(tools, "_video_output_dir", return_value=Path(directory) / "output"), patch.object(tools, "_require_video_analyze_memory"), patch.object(tools.subprocess, "Popen", popen), patch.object(tools.time, "monotonic", side_effect=[0, 0]):
                with self.assertRaisesRegex(RuntimeError, "resource lock wait timed out"):
                    tools._run_video_analyze("fixture.mp4")
            self.assertEqual(blocked.calls, [blocked.LOCK_EX | blocked.LOCK_NB])
        popen.assert_not_called()

        for label, stdout, stderr in (("bytes", b"bytes stdout", b"bytes stderr"), ("strings", "string stdout", "string stderr")):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory) / "video-output"

                class TimeoutProcess:
                    pid, returncode = 12345, None

                    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
                        raise subprocess.TimeoutExpired("analysis", timeout, output=stdout, stderr=stderr)

                process = TimeoutProcess()
                terminate = Mock(return_value=(stdout, stderr))
                with patch.object(tools, "_video_output_dir", return_value=output_dir), patch.object(tools, "_video_analyze_lock", no_video_analyze_lock), patch.object(tools, "_require_video_analyze_memory"), patch.object(tools.subprocess, "Popen", return_value=process), patch.object(tools, "_terminate_video_analyze_process_group", terminate):
                    with self.assertRaises(RuntimeError) as raised:
                        tools._run_video_analyze("fixture.mp4", timeout_seconds=123)
                self.assertIsInstance(raised.exception.__cause__, subprocess.TimeoutExpired)
                self.assertIn("process group terminated", str(raised.exception))
                self.assertIn("diagnostic log:", str(raised.exception))
                terminate.assert_called_once_with(process)
                diagnostic = (output_dir / "analysis_timeout.log").read_text(encoding="utf-8")
                self.assertIn("timeout_seconds=123", diagnostic)
                self.assertIn("bytes stdout" if isinstance(stdout, bytes) else stdout, diagnostic)
                self.assertIn("bytes stderr" if isinstance(stderr, bytes) else stderr, diagnostic)

    def test_process_group_timeout_termination_on_posix_and_non_posix(self) -> None:
        class TimeoutProcess:
            pid = 12345

            def __init__(self) -> None:
                self.calls: list[int | None] = []
                self.terminated = self.killed = 0

            def communicate(self, timeout: int | None = None) -> tuple[str, str]:
                self.calls.append(timeout)
                if timeout == 5:
                    raise subprocess.TimeoutExpired("analysis", timeout)
                return "final stdout", "final stderr"

            def terminate(self) -> None:
                self.terminated += 1

            def kill(self) -> None:
                self.killed += 1

        process = TimeoutProcess()
        signals = SimpleNamespace(SIGTERM="TERM", SIGKILL="KILL")
        with patch.object(tools, "signal", signals), patch.object(tools.os, "name", "posix"), patch.object(tools.os, "killpg", create=True) as killpg:
            self.assertEqual(tools._terminate_video_analyze_process_group(process), ("final stdout", "final stderr"))
        self.assertEqual(process.calls, [5, None])
        self.assertEqual(killpg.call_args_list, [call(process.pid, signals.SIGTERM), call(process.pid, signals.SIGKILL)])

        process = TimeoutProcess()
        with patch.object(tools.os, "name", "nt"):
            self.assertEqual(tools._terminate_video_analyze_process_group(process), ("final stdout", "final stderr"))
        self.assertEqual(process.calls, [5, None])
        self.assertEqual((process.terminated, process.killed), (1, 1))

    def test_direct_executor_and_execute_tool_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir, stdout_dir, default_dir, invalid_dir = (
                Path(directory) / name for name in ("artifact", "stdout", "default", "invalid")
            )
            run_calls: list[tuple[list[str], dict]] = []

            def run(command: list[str], **kwargs: object) -> SimpleNamespace:
                run_calls.append((command, kwargs))
                if len(run_calls) == 1:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    (artifact_dir / "analysis.json").write_text(json.dumps({"direct": True}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="direct stdout", stderr="")

            with patch.dict(os.environ, {"VIDEO_ANALYZE_TEST_ENV": "kept"}), patch.object(tools, "_video_output_dir", side_effect=[artifact_dir, stdout_dir, default_dir, invalid_dir]), patch.object(tools.subprocess, "run", side_effect=run), patch.object(tools, "mark_extracted") as mark:
                self.assertEqual(tools._run_video_direct_analyze("fixture.mp4", "none", 9999, "prompt", "https://cdn.example/video.mp4"), {"direct": True})
                self.assertEqual(tools._run_video_direct_analyze("stdout.mp4", timeout_seconds=1), {"output": "direct stdout"})
                self.assertEqual(tools._run_video_direct_analyze("default.mp4"), {"output": "direct stdout"})
                self.assertEqual(tools._run_video_direct_analyze("invalid.mp4", timeout_seconds="invalid"), {"output": "direct stdout"})
            command, kwargs = run_calls[0]
            self.assertEqual(command, ["python", str(tools.SCRIPTS_DIR / "direct_video_analyze.py"), "fixture.mp4", "--output-dir", str(artifact_dir), "--audio-mode", "none", "--prompt", "prompt", "--public-url", "https://cdn.example/video.mp4"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertEqual(kwargs["cwd"], tools.ROOT)
            self.assertEqual(kwargs["env"]["VIDEO_ANALYZE_TEST_ENV"], "kept")
            self.assertEqual(run_calls[2][0], ["python", str(tools.SCRIPTS_DIR / "direct_video_analyze.py"), "default.mp4", "--output-dir", str(default_dir), "--audio-mode", "whisper"])
            for empty_optional_command in (run_calls[1][0], run_calls[2][0], run_calls[3][0]):
                self.assertNotIn("--prompt", empty_optional_command)
                self.assertNotIn("--public-url", empty_optional_command)
            self.assertEqual([kwargs["timeout"] for _, kwargs in run_calls], [1800, 30, 600, 600])
            self.assertEqual(mark.call_args_list, [call("fixture.mp4", artifact_dir.name), call("stdout.mp4", stdout_dir.name), call("default.mp4", default_dir.name), call("invalid.mp4", invalid_dir.name)])

        for stdout, stderr, expected in (("stdout", "stderr", "stderr"), ("stdout", "", "stdout"), ("", "", "Exit code 9")):
            with self.subTest(stdout=stdout, stderr=stderr), tempfile.TemporaryDirectory() as directory:
                with patch.object(tools, "_video_output_dir", return_value=Path(directory) / "output"), patch.object(tools.subprocess, "run", return_value=SimpleNamespace(returncode=9, stdout=stdout, stderr=stderr)), patch.object(tools, "mark_extracted") as mark:
                    with self.assertRaisesRegex(RuntimeError, expected):
                        tools._run_video_direct_analyze("fixture.mp4")
                mark.assert_not_called()

        with tempfile.TemporaryDirectory() as directory, patch.object(tools, "OUTPUT_DIR", Path(directory)):
            standard_calls: list[tuple[str, object]] = []
            direct_calls: list[tuple[str, str, object, str, str]] = []
            with patch.object(tools, "_run_video_analyze", side_effect=lambda filename, timeout: standard_calls.append((filename, timeout)) or {"kind": "standard"}), patch.object(tools, "_run_video_direct_analyze", side_effect=lambda filename, audio, timeout, prompt, public: direct_calls.append((filename, audio, timeout, prompt, public)) or {"kind": "direct"}):
                standard_default = tools.execute_tool("video_analyze", {"filename": 7})
                standard = tools.execute_tool("video_analyze", {"filename": 8, "timeout_seconds": "42"})
                direct_default = tools.execute_tool("video_direct_analyze", {"filename": 9})
                direct = tools.execute_tool("video_direct_analyze", {"filename": 10, "audio_mode": "none", "timeout_seconds": "31", "prompt": 11, "public_url": 12})
            for result, expected in ((standard_default, {"kind": "standard"}), (standard, {"kind": "standard"}), (direct_default, {"kind": "direct"}), (direct, {"kind": "direct"})):
                self.assertEqual(set(result), {"ok", "data", "elapsed"})
                self.assertTrue(result["ok"])
                self.assertEqual(result["data"], expected)
                self.assertIsInstance(result["elapsed"], (int, float))
            self.assertEqual(standard_calls, [("7", None), ("8", "42")])
            self.assertEqual(direct_calls, [("9", "whisper", None, "", ""), ("10", "none", "31", "11", "12")])
            with patch.object(tools, "_run_video_analyze", side_effect=RuntimeError("standard failure")):
                failed = tools.execute_tool("video_analyze", {"filename": "fixture.mp4"})
            self.assertEqual(set(failed), {"ok", "error", "elapsed"})
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["error"], "standard failure")
            self.assertIsInstance(failed["elapsed"], (int, float))
            with patch.object(tools, "_run_video_direct_analyze", side_effect=RuntimeError("direct failure")):
                failed = tools.execute_tool("video_direct_analyze", {"filename": "fixture.mp4"})
            self.assertEqual(set(failed), {"ok", "error", "elapsed"})
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["error"], "direct failure")
            self.assertIsInstance(failed["elapsed"], (int, float))

            timeout = subprocess.TimeoutExpired("direct-video", 600)
            with patch.object(tools, "_video_output_dir", return_value=Path(directory) / "timeout"), patch.object(tools.subprocess, "run", side_effect=timeout):
                failed = tools.execute_tool("video_direct_analyze", {"filename": "timeout.mp4"})
            self.assertEqual(set(failed), {"ok", "error", "elapsed"})
            self.assertFalse(failed["ok"])
            self.assertIn("timed out", failed["error"])
            self.assertIsInstance(failed["elapsed"], (int, float))


if __name__ == "__main__":
    unittest.main()
