#!/usr/bin/env python3
"""Contract tests for the chat video-analysis subprocess executors."""
from __future__ import annotations

import ast
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

from services.analyzer_execution import AnalyzerExecutionService
import tools


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


def make_service(
    root: Path,
    *,
    get_video_by_filename=lambda _filename: None,
    mark_extracted=lambda _filename, _output: None,
    environ: dict[str, str] | None = None,
    popen_factory=lambda *_args, **_kwargs: FinishedProcess(),
    run_factory=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    fcntl_module=None,
    available_memory=lambda: None,
    clock=lambda: 0.0,
    sleep=lambda _seconds: None,
    operating_system="posix",
    kill_process_group=None,
    term_signal="TERM",
    kill_signal="KILL",
) -> AnalyzerExecutionService:
    return AnalyzerExecutionService(
        root=root,
        scripts_dir=ROOT / "scripts",
        get_video_by_filename=get_video_by_filename,
        mark_extracted=mark_extracted,
        environ={} if environ is None else environ,
        popen_factory=popen_factory,
        run_factory=run_factory,
        lock_path=root / "data" / "video_analyze.lock",
        fcntl_module=fcntl_module,
        available_memory=available_memory,
        clock=clock,
        sleep=sleep,
        operating_system=operating_system,
        kill_process_group=kill_process_group,
        term_signal=term_signal,
        kill_signal=kill_signal,
    )


class VideoAnalyzeExecutorTests(unittest.TestCase):
    def test_standard_cache_timeout_and_success_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output" / "registry-output"
            output_dir.mkdir(parents=True)
            analysis = output_dir / "analysis.json"
            popen = Mock(side_effect=AssertionError("cache must not spawn a subprocess"))
            memory = Mock(side_effect=AssertionError("cache must not inspect memory"))
            class CacheFcntl:
                LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4

                def __init__(self) -> None:
                    self.calls: list[int] = []

                def flock(self, _fd: int, flags: int) -> None:
                    self.calls.append(flags)
                    raise AssertionError("cache must not acquire the analysis lock")

            fcntl = CacheFcntl()
            marked = Mock(side_effect=AssertionError("cache must not mark extraction"))
            service = make_service(
                root,
                get_video_by_filename=lambda _filename: {"extraction_dir": "registry-output"},
                mark_extracted=marked,
                popen_factory=popen,
                available_memory=memory,
                fcntl_module=fcntl,
            )
            analysis.write_text(json.dumps({"summary": "cached"}), encoding="utf-8")
            self.assertEqual(service.run_standard("fixture.mp4"), {"summary": "cached", "_cache": {"hit": True, "provider": "video_registry", "endpoint": "analysis"}})
            analysis.write_text(json.dumps(["non-dict cached payload"]), encoding="utf-8")
            self.assertEqual(service.run_standard("fixture.mp4"), ["non-dict cached payload"])
            popen.assert_not_called()
            memory.assert_not_called()
            marked.assert_not_called()
            self.assertEqual(fcntl.calls, [])

        for record, filename in (({"extraction_dir": ""}, "empty.mp4"), (None, "missing.mp4")):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls: list[dict] = []
                marked: list[tuple[str, str]] = []

                def fallback_popen(_command: list[str], **kwargs: object) -> FinishedProcess:
                    calls.append(kwargs)
                    return FinishedProcess("fallback stdout")

                service = make_service(
                    root,
                    get_video_by_filename=lambda _filename, record=record: record,
                    mark_extracted=lambda name, output: marked.append((name, output)),
                    popen_factory=fallback_popen,
                )
                self.assertEqual(service.run_standard(filename), {"output": "fallback stdout"})
                self.assertEqual(calls[0]["env"]["ANALYSIS_OUTPUT_DIR"], str(root / "output" / filename))
                self.assertEqual(marked, [(filename, filename)])

        for raw, expected in (("2", 30), ("9999", 1800), ("bad", 600), ("61", 61)):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                process = FinishedProcess("stdout")
                service = make_service(Path(directory), environ={"VIDEO_ANALYZE_TIMEOUT": raw}, popen_factory=lambda *_args, **_kwargs: process)
                self.assertEqual(service.run_standard("fixture.mp4"), {"output": "stdout"})
                self.assertEqual(process.timeouts, [expected])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output" / "fixture.mp4"
            calls: list[tuple[list[str], dict]] = []
            marks: list[tuple[str, str]] = []
            processes: list[FinishedProcess] = []

            def popen(command: list[str], **kwargs: object) -> FinishedProcess:
                calls.append((command, kwargs))
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "analysis.json").write_text(json.dumps({"artifact": True}), encoding="utf-8")
                process = FinishedProcess("raw stdout", "raw stderr")
                processes.append(process)
                return process

            service = make_service(root, environ={"VIDEO_ANALYZE_TIMEOUT": "61", "VIDEO_ANALYZE_TEST_ENV": "kept"}, popen_factory=popen, mark_extracted=lambda filename, output: marks.append((filename, output)))
            self.assertEqual(service.run_standard("fixture.mp4", timeout_seconds=70), {"artifact": True})
            command, kwargs = calls[0]
            self.assertEqual(command, ["bash", str(ROOT / "scripts" / "analyze_one.sh"), "fixture.mp4"])
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertIs(kwargs["stderr"], subprocess.PIPE)
            self.assertTrue(kwargs["text"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["cwd"], root)
            self.assertEqual(kwargs["env"]["ANALYSIS_OUTPUT_DIR"], str(output_dir))
            self.assertEqual(kwargs["env"]["VIDEO_ANALYZE_TEST_ENV"], "kept")
            self.assertEqual(processes[0].timeouts, [70])
            self.assertEqual(marks, [("fixture.mp4", output_dir.name)])

    def test_standard_failures_lock_and_timeout_termination_contract(self) -> None:
        for stdout, stderr, expected in (("stdout", "stderr", "stderr"), ("stdout", "", "stdout"), ("", "", "Exit code 7")):
            with self.subTest(stdout=stdout, stderr=stderr), tempfile.TemporaryDirectory() as directory:
                marked = Mock()
                service = make_service(Path(directory), popen_factory=lambda *_args, **_kwargs: FinishedProcess(stdout, stderr, 7), mark_extracted=marked)
                with self.assertRaisesRegex(RuntimeError, expected):
                    service.run_standard("fixture.mp4")
                marked.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            popen = Mock(side_effect=AssertionError("Popen must not be reached"))
            service = make_service(Path(directory), environ={"VIDEO_ANALYZE_MIN_AVAILABLE_MB": "4096"}, popen_factory=popen, available_memory=lambda: 1)
            with self.assertRaisesRegex(RuntimeError, "memory available"):
                service.run_standard("fixture.mp4")
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
            root = Path(directory)
            fcntl = Fcntl()
            service = make_service(root, fcntl_module=fcntl, popen_factory=lambda *_args, **_kwargs: FinishedProcess("stdout"))
            self.assertEqual(service.run_standard("fixture.mp4"), {"output": "stdout"})
            self.assertEqual(fcntl.calls, [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN])

            blocked = Fcntl(blocked=True)
            popen = Mock(side_effect=AssertionError("Popen must not be reached"))
            service = make_service(root, environ={"VIDEO_ANALYZE_LOCK_TIMEOUT_SECONDS": "0"}, fcntl_module=blocked, popen_factory=popen, clock=iter([0, 0]).__next__)
            with self.assertRaisesRegex(RuntimeError, "resource lock wait timed out"):
                service.run_standard("fixture.mp4")
            self.assertEqual(blocked.calls, [blocked.LOCK_EX | blocked.LOCK_NB])
            popen.assert_not_called()

        class TimeoutProcess:
            pid, returncode = 12345, None

            def __init__(self, stdout: object, stderr: object) -> None:
                self.stdout, self.stderr = stdout, stderr
                self.calls: list[int | None] = []
                self.terminated = self.killed = 0

            def communicate(self, timeout: int | None = None) -> tuple[str, str]:
                self.calls.append(timeout)
                if len(self.calls) <= 2:
                    raise subprocess.TimeoutExpired("analysis", timeout, output=self.stdout, stderr=self.stderr)
                return self.stdout, self.stderr

            def terminate(self) -> None:
                self.terminated += 1

            def kill(self) -> None:
                self.killed += 1

        for stdout, stderr in ((b"bytes stdout", b"bytes stderr"), ("string stdout", "string stderr")):
            for operating_system in ("posix", "nt"):
                with self.subTest(stdout=stdout, operating_system=operating_system), tempfile.TemporaryDirectory() as directory:
                    process = TimeoutProcess(stdout, stderr)
                    killed: list[tuple[int, int]] = []
                    service = make_service(Path(directory), environ={"VIDEO_ANALYZE_TIMEOUT": "61"}, popen_factory=lambda *_args, **_kwargs: process, operating_system=operating_system, kill_process_group=lambda pid, sig: killed.append((pid, sig)))
                    with self.assertRaises(RuntimeError) as raised:
                        service.run_standard("fixture.mp4", timeout_seconds=123)
                    self.assertIsInstance(raised.exception.__cause__, subprocess.TimeoutExpired)
                    self.assertIn("process group terminated", str(raised.exception))
                    self.assertIn("diagnostic log:", str(raised.exception))
                    self.assertEqual(process.calls, [123, 5, None])
                    diagnostic = (Path(directory) / "output" / "fixture.mp4" / "analysis_timeout.log").read_text(encoding="utf-8")
                    self.assertIn("timeout_seconds=123", diagnostic)
                    self.assertIn("bytes stdout" if isinstance(stdout, bytes) else stdout, diagnostic)
                    self.assertIn("bytes stderr" if isinstance(stderr, bytes) else stderr, diagnostic)
                    if operating_system == "posix":
                        self.assertEqual(killed, [(process.pid, "TERM"), (process.pid, "KILL")])
                    else:
                        self.assertEqual(killed, [])
                        self.assertEqual((process.terminated, process.killed), (1, 1))

    def test_direct_executor_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir, stdout_dir, default_dir, invalid_dir = (root / "output" / name for name in ("artifact", "stdout", "default", "invalid"))
            calls: list[tuple[list[str], dict]] = []
            marks: list[tuple[str, str]] = []

            def run(command: list[str], **kwargs: object) -> SimpleNamespace:
                calls.append((command, kwargs))
                if len(calls) == 1:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    (artifact_dir / "analysis.json").write_text(json.dumps({"direct": True}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="direct stdout", stderr="")

            output_dirs = iter((artifact_dir, stdout_dir, default_dir, invalid_dir))
            names = iter(("artifact.mp4", "stdout.mp4", "default.mp4", "invalid.mp4"))
            service = make_service(root, environ={"VIDEO_ANALYZE_TEST_ENV": "kept"}, get_video_by_filename=lambda _filename: {"extraction_dir": next(output_dirs).name}, mark_extracted=lambda filename, output: marks.append((filename, output)), run_factory=run)
            self.assertEqual(service.run_direct(next(names), "none", 9999, "prompt", "https://cdn.example/video.mp4"), {"direct": True})
            self.assertEqual(service.run_direct(next(names), timeout_seconds=1), {"output": "direct stdout"})
            self.assertEqual(service.run_direct(next(names)), {"output": "direct stdout"})
            self.assertEqual(service.run_direct(next(names), timeout_seconds="invalid"), {"output": "direct stdout"})
            command, kwargs = calls[0]
            self.assertEqual(command, ["python", str(ROOT / "scripts" / "direct_video_analyze.py"), "artifact.mp4", "--output-dir", str(artifact_dir), "--audio-mode", "none", "--prompt", "prompt", "--public-url", "https://cdn.example/video.mp4"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertEqual(kwargs["cwd"], root)
            self.assertEqual(kwargs["env"]["VIDEO_ANALYZE_TEST_ENV"], "kept")
            self.assertEqual(calls[2][0], ["python", str(ROOT / "scripts" / "direct_video_analyze.py"), "default.mp4", "--output-dir", str(default_dir), "--audio-mode", "whisper"])
            self.assertEqual([kwargs["timeout"] for _, kwargs in calls], [1800, 30, 600, 600])
            self.assertEqual(marks, [("artifact.mp4", artifact_dir.name), ("stdout.mp4", stdout_dir.name), ("default.mp4", default_dir.name), ("invalid.mp4", invalid_dir.name)])
            for empty_optional_command in (calls[1][0], calls[2][0], calls[3][0]):
                self.assertNotIn("--prompt", empty_optional_command)
                self.assertNotIn("--public-url", empty_optional_command)

        for stdout, stderr, expected in (("stdout", "stderr", "stderr"), ("stdout", "", "stdout"), ("", "", "Exit code 9")):
            with self.subTest(stdout=stdout, stderr=stderr), tempfile.TemporaryDirectory() as directory:
                marked = Mock()
                service = make_service(Path(directory), run_factory=lambda *_args, **_kwargs: SimpleNamespace(returncode=9, stdout=stdout, stderr=stderr), mark_extracted=marked)
                with self.assertRaisesRegex(RuntimeError, expected):
                    service.run_direct("fixture.mp4")
                marked.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            service = make_service(Path(directory), run_factory=Mock(side_effect=subprocess.TimeoutExpired("direct-video", 600)))
            with self.assertRaises(subprocess.TimeoutExpired):
                service.run_direct("timeout.mp4")

    def test_tools_facades_execute_tool_and_structure(self) -> None:
        class RecordingService:
            def __init__(self) -> None:
                self.standard_calls: list[tuple[str, object]] = []
                self.direct_calls: list[tuple[str, str, object, str, str]] = []

            def run_standard(self, filename: str, timeout: object) -> dict[str, str]:
                self.standard_calls.append((filename, timeout))
                if filename == "standard-failure.mp4":
                    raise RuntimeError("standard failure")
                return {"kind": "standard"}

            def run_direct(self, filename: str, audio: str, timeout: object, prompt: str, public_url: str) -> dict[str, str]:
                self.direct_calls.append((filename, audio, timeout, prompt, public_url))
                if filename == "timeout.mp4":
                    raise subprocess.TimeoutExpired("direct-video", 600)
                if filename == "direct-failure.mp4":
                    raise RuntimeError("direct failure")
                return {"kind": "direct"}

        recording = RecordingService()
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, "video_analyzer_execution", recording), patch.object(tools, "OUTPUT_DIR", Path(directory)):
            self.assertEqual(tools._run_video_analyze("facade.mp4", "42"), {"kind": "standard"})
            self.assertEqual(tools._run_video_direct_analyze("facade.mp4", "none", "31", "prompt", "url"), {"kind": "direct"})
            standard = tools.execute_tool("video_analyze", {"filename": 7, "timeout_seconds": "42"})
            direct = tools.execute_tool("video_direct_analyze", {"filename": 8, "audio_mode": "none", "timeout_seconds": "31", "prompt": 9, "public_url": 10})
            for result, expected in ((standard, {"kind": "standard"}), (direct, {"kind": "direct"})):
                self.assertEqual(set(result), {"ok", "data", "elapsed"})
                self.assertTrue(result["ok"])
                self.assertEqual(result["data"], expected)
                self.assertIsInstance(result["elapsed"], (int, float))
            failed_standard = tools.execute_tool("video_analyze", {"filename": "standard-failure.mp4"})
            failed_timeout = tools.execute_tool("video_direct_analyze", {"filename": "timeout.mp4"})
            failed_direct = tools.execute_tool("video_direct_analyze", {"filename": "direct-failure.mp4"})
            for result, expected in (
                (failed_standard, "standard failure"),
                (failed_timeout, "timed out"),
                (failed_direct, "direct failure"),
            ):
                self.assertEqual(set(result), {"ok", "error", "elapsed"})
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["error"])
                self.assertIsInstance(result["elapsed"], (int, float))
        self.assertEqual(recording.standard_calls, [("facade.mp4", "42"), ("7", "42"), ("standard-failure.mp4", None)])
        self.assertEqual(recording.direct_calls, [("facade.mp4", "none", "31", "prompt", "url"), ("8", "none", "31", "9", "10"), ("timeout.mp4", "whisper", None, "", ""), ("direct-failure.mp4", "whisper", None, "", "")])

        tools_tree = ast.parse((ROOT / "scripts" / "tools.py").read_text(encoding="utf-8"))
        service_tree = ast.parse((ROOT / "scripts" / "services" / "analyzer_execution.py").read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name in {"_video_output_dir", "_video_analyze_timeout_seconds", "_timeout_output_text", "_available_memory_bytes", "_require_video_analyze_memory", "_video_analyze_lock", "_terminate_video_analyze_process_group", "_write_video_analyze_timeout_log"} for node in tools_tree.body))
        constructors = [
            node for node in ast.walk(tools_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AnalyzerExecutionService"
        ]
        self.assertEqual(len(constructors), 1)
        self.assertEqual(
            [(keyword.arg, ast.unparse(keyword.value)) for keyword in constructors[0].keywords],
            [
                ("root", "ROOT"),
                ("scripts_dir", "SCRIPTS_DIR"),
                ("get_video_by_filename", "get_video_by_filename"),
                ("mark_extracted", "mark_extracted"),
                ("environ", "os.environ"),
                ("popen_factory", "subprocess.Popen"),
                ("run_factory", "subprocess.run"),
            ],
        )
        execution_class = next(node for node in service_tree.body if isinstance(node, ast.ClassDef) and node.name == "AnalyzerExecutionService")
        self.assertEqual([node.name for node in execution_class.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")], ["run_standard", "run_direct"])
        for node in ast.walk(service_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name in {"tools", "web_app", "routes", "hot_video_report"} for alias in node.names))
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module, {"tools", "web_app", "hot_video_report"})
                self.assertFalse(node.module == "routes" or node.module.startswith("routes."))
                self.assertNotEqual(node.module, "video_registry")
        facades = {node.name: node for node in tools_tree.body if isinstance(node, ast.FunctionDef) and node.name in {"_run_video_analyze", "_run_video_direct_analyze"}}
        self.assertEqual(set(facades), {"_run_video_analyze", "_run_video_direct_analyze"})
        self.assertEqual(ast.unparse(facades["_run_video_analyze"].body[0].value), "video_analyzer_execution.run_standard(filename, timeout_seconds)")
        self.assertEqual(ast.unparse(facades["_run_video_direct_analyze"].body[0].value), "video_analyzer_execution.run_direct(filename, audio_mode, timeout_seconds, prompt, public_url)")


if __name__ == "__main__":
    unittest.main()
