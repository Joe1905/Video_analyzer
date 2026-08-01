#!/usr/bin/env python3
"""Run with: docker compose -p short-video-analyzer run --rm analyzer python scripts/test_hot_report_db_lifecycle.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import hot_video_report as report


def _fd_count() -> int | None:
    fd_dir = Path("/proc/self/fd")
    return len(list(fd_dir.iterdir())) if fd_dir.is_dir() else None


def main() -> int:
    original_path = report.DB_PATH
    original_initialized = report._initialized_db_path
    with tempfile.TemporaryDirectory() as directory:
        report.DB_PATH = Path(directory) / "hot_video_report.sqlite"
        report._initialized_db_path = None
        report.initialize_hot_report_db()
        assert report.DB_PATH.is_file()

        for _ in range(20):
            payload = report.get_report("2026-08-01", detail=False)
            assert payload["status"] == "missing"
        before_fd = _fd_count()
        timings: list[float] = []
        for _ in range(1000):
            started = time.monotonic()
            payload = report.get_report("2026-08-01", detail=False)
            assert payload["status"] == "missing"
            timings.append(time.monotonic() - started)
        time.sleep(float(os.getenv("HOT_REPORT_TEST_SETTLE_SECONDS", "30")))
        after_fd = _fd_count()
        p95 = sorted(timings)[int(len(timings) * 0.95) - 1]
        assert p95 < 1.0, f"get_report p95 was {p95:.3f}s"
        if before_fd is not None and after_fd is not None:
            assert after_fd - before_fd <= 5, f"file descriptor growth was {after_fd - before_fd}"

        with closing(report._connect()) as writer:
            writer.execute("BEGIN IMMEDIATE")
            payload = report.get_report("2026-08-01", detail=False)
            assert payload["status"] == "missing"
            writer.rollback()

        with closing(report._connect()) as conn:
            first_id = report._start_report(conn, "2026-08-01", "US", [], worker_lease="first")
            second_id = report._start_report(conn, "2026-08-01", "US", [], worker_lease="second")
            assert first_id == second_id
            try:
                report._finish_report(conn, "wrong-id", "2026-08-01", "failed", "expected mismatch")
            except RuntimeError:
                pass
            else:
                raise AssertionError("_finish_report accepted a non-authoritative report id")
    report.DB_PATH = original_path
    report._initialized_db_path = original_initialized
    print(f"hot report database lifecycle: OK (p95={p95:.3f}s, fd_delta={None if before_fd is None else after_fd - before_fd})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
