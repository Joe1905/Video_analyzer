"""Synchronous standard and direct video-analysis subprocess execution."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Callable, Mapping

try:
    import fcntl as system_fcntl
except ImportError:  # pragma: no cover - production runs on Linux.
    system_fcntl = None


def _available_memory_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return None
    try:
        values = dict(
            line.split(":", 1)
            for line in meminfo.read_text(encoding="utf-8").splitlines()
            if ":" in line
        )
        return int(values["MemAvailable"].strip().split()[0]) * 1024
    except (KeyError, ValueError):
        return None


class AnalyzerExecutionService:
    """Own the two existing chat-video analysis execution paths."""

    def __init__(
        self,
        *,
        root: Path,
        scripts_dir: Path,
        get_video_by_filename: Callable[[str], Any],
        mark_extracted: Callable[[str, str], None],
        environ: Mapping[str, str] = os.environ,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        run_factory: Callable[..., Any] = subprocess.run,
        lock_path: Path | None = None,
        fcntl_module: Any = system_fcntl,
        available_memory: Callable[[], int | None] = _available_memory_bytes,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        operating_system: str = os.name,
        kill_process_group: Callable[[int, int], None] | None = getattr(os, "killpg", None),
        term_signal: int | None = getattr(signal, "SIGTERM", None),
        kill_signal: int | None = getattr(signal, "SIGKILL", None),
    ) -> None:
        self._root = root
        self._scripts_dir = scripts_dir
        self._get_video_by_filename = get_video_by_filename
        self._mark_extracted = mark_extracted
        self._environ = environ
        self._popen_factory = popen_factory
        self._run_factory = run_factory
        self._lock_path = lock_path or root / "data" / "video_analyze.lock"
        self._fcntl = fcntl_module
        self._available_memory = available_memory
        self._clock = clock
        self._sleep = sleep
        self._operating_system = operating_system
        self._kill_process_group = kill_process_group
        self._term_signal = term_signal
        self._kill_signal = kill_signal

    def run_standard(self, filename: str, timeout_seconds: Any | None = None) -> dict:
        output_dir = self._output_dir(filename)
        analysis = output_dir / "analysis.json"
        if analysis.is_file():
            data = json.loads(analysis.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["_cache"] = {"hit": True, "provider": "video_registry", "endpoint": "analysis"}
            return data

        command = ["bash", str(self._scripts_dir / "analyze_one.sh"), filename]
        environment = dict(self._environ)
        environment["ANALYSIS_OUTPUT_DIR"] = str(output_dir)
        timeout = self._timeout_seconds(timeout_seconds)
        with self._lock():
            self._require_memory()
            process = self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self._root,
                env=environment,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = self._terminate_process_group(process)
                exc.stdout = stdout
                exc.stderr = stderr
                log_path = self._write_timeout_log(output_dir, command, timeout, exc)
                raise RuntimeError(
                    f"video analysis subprocess timed out after {timeout} seconds; process group terminated; "
                    f"diagnostic log: {log_path}"
                ) from exc

        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or f"Exit code {result.returncode}")
        self._mark_extracted(filename, output_dir.name)
        if analysis.is_file():
            return json.loads(analysis.read_text(encoding="utf-8"))
        return {"output": result.stdout}

    def run_direct(
        self,
        filename: str,
        audio_mode: str = "whisper",
        timeout_seconds: Any | None = None,
        prompt: str = "",
        public_url: str = "",
    ) -> dict:
        output_dir = self._output_dir(filename)
        command = [
            "python", str(self._scripts_dir / "direct_video_analyze.py"), filename,
            "--output-dir", str(output_dir), "--audio-mode", audio_mode,
        ]
        if prompt:
            command.extend(["--prompt", prompt])
        if public_url:
            command.extend(["--public-url", public_url])
        timeout = self._timeout_seconds(timeout_seconds) if timeout_seconds is not None else 600
        result = self._run_factory(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=self._root,
            env=dict(self._environ),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or f"Exit code {result.returncode}")
        analysis = output_dir / "analysis.json"
        self._mark_extracted(filename, output_dir.name)
        if analysis.is_file():
            return json.loads(analysis.read_text(encoding="utf-8"))
        return {"output": result.stdout}

    def _output_dir(self, filename: str) -> Path:
        record = self._get_video_by_filename(filename)
        if record:
            return self._root / "output" / str(record.get("extraction_dir") or filename)
        return self._root / "output" / filename

    def _timeout_seconds(self, value: Any | None = None) -> int:
        raw = value if value is not None else self._environ.get("VIDEO_ANALYZE_TIMEOUT", "600")
        try:
            return max(30, min(int(raw), 1800))
        except (TypeError, ValueError):
            return 600

    def _require_memory(self) -> None:
        try:
            minimum_mb = max(0, int(self._environ.get("VIDEO_ANALYZE_MIN_AVAILABLE_MB", "4096")))
        except ValueError:
            minimum_mb = 4096
        available = self._available_memory()
        if available is not None and available < minimum_mb * 1024 * 1024:
            raise RuntimeError(
                f"video analysis not started: only {available / 1024 / 1024:.0f}MB memory available, "
                f"below VIDEO_ANALYZE_MIN_AVAILABLE_MB={minimum_mb}"
            )

    @contextmanager
    def _lock(self):
        if self._fcntl is None:
            yield
            return
        try:
            wait_seconds = max(0, int(self._environ.get("VIDEO_ANALYZE_LOCK_TIMEOUT_SECONDS", "30")))
        except ValueError:
            wait_seconds = 30
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            deadline = self._clock() + wait_seconds
            while True:
                try:
                    self._fcntl.flock(lock_file.fileno(), self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self._clock() >= deadline:
                        raise RuntimeError("video analysis already running; resource lock wait timed out")
                    self._sleep(0.25)
            try:
                yield
            finally:
                self._fcntl.flock(lock_file.fileno(), self._fcntl.LOCK_UN)

    def _terminate_process_group(self, process: Any) -> tuple[str, str]:
        if self._operating_system == "posix":
            try:
                if self._kill_process_group is not None and self._term_signal is not None:
                    self._kill_process_group(process.pid, self._term_signal)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            if self._operating_system == "posix":
                try:
                    if self._kill_process_group is not None and self._kill_signal is not None:
                        self._kill_process_group(process.pid, self._kill_signal)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            return process.communicate()

    def _write_timeout_log(
        self,
        output_dir: Path,
        command: list[str],
        timeout_seconds: int,
        exc: subprocess.TimeoutExpired,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / "analysis_timeout.log"
        stdout = self._timeout_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
        stderr = self._timeout_text(getattr(exc, "stderr", None))
        log_path.write_text(
            "\n".join(
                [
                    "stage=video_analyze",
                    f"timeout_seconds={timeout_seconds}",
                    f"command={' '.join(command)}",
                    f"recorded_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
                    "",
                    "--- stdout ---",
                    stdout,
                    "",
                    "--- stderr ---",
                    stderr,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return log_path

    @staticmethod
    def _timeout_text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")
