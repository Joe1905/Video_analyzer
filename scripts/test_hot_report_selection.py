#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import types
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("requests", types.SimpleNamespace())
sys.modules.setdefault(
    "deepseek_postprocess",
    types.SimpleNamespace(
        DEFAULT_API_URL="",
        DEFAULT_MODEL="",
        call_deepseek=None,
        extract_content=None,
        parse_json_content=None,
    ),
)
sys.modules.setdefault("sociavault_tiktok", types.SimpleNamespace(call_api=None))
sys.modules.setdefault(
    "tools",
    types.SimpleNamespace(
        _iter_media_url_candidates=None,
        _run_video_analyze=None,
        execute_tool=None,
    ),
)
sys.modules.setdefault(
    "video_registry",
    types.SimpleNamespace(
        get_video=None,
        get_video_by_filename=None,
        register_video=None,
        set_hidden_from_analyzer=None,
    ),
)

import hot_video_report


def candidate(video_id: str, hot_score: int, bucket: str, label: str) -> dict:
    return {
        "platform": "tiktok",
        "video_id": video_id,
        "hot_score": hot_score,
        "selection_bucket": bucket,
        "source_label": label,
    }


def insert_report_video(conn, report_id: str, report_date: str, video_id: str, status: str, published_at: float | None) -> None:
    metrics = {} if published_at is None else {"published_at": published_at}
    now = time.time()
    conn.execute(
        """
        INSERT INTO hot_report_videos (
            report_id, report_date, platform, video_id, source_endpoint, source_label,
            source_rank, report_rank, hot_score, metrics_json, raw_json, process_status,
            created_at, updated_at
        ) VALUES (?, ?, 'tiktok', ?, 'test', 'test', 1, 1, 100, ?, '{}', ?, ?, ?)
        """,
        (report_id, report_date, video_id, hot_video_report.json.dumps(metrics), status, now, now),
    )


class HotReportSelectionTests(unittest.TestCase):
    def test_default_parallel_processing_budget_stays_under_one_hour(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REPORT_VIDEO_TIMEOUT", None)
            os.environ.pop("REPORT_VIDEO_MAX_WORKERS", None)
            timeout = hot_video_report._report_video_timeout_seconds()
            workers = hot_video_report._report_video_max_workers()

        self.assertEqual(timeout, 480)
        self.assertEqual(workers, 5)
        self.assertLessEqual(math.ceil(30 / workers) * timeout, 60 * 60)

    def test_partial_report_resume_preserves_only_completed_videos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            hot_video_report,
            "DB_PATH",
            Path(temp_dir) / "report.sqlite",
        ):
            conn = hot_video_report._connect()
            try:
                report_id = hot_video_report._start_report(conn, "2026-07-14", "US", [])
                insert_report_video(conn, report_id, "2026-07-14", "complete", "complete", time.time())
                insert_report_video(conn, report_id, "2026-07-14", "failed", "failed", time.time())
                conn.execute("UPDATE daily_reports SET status = 'partial_failed' WHERE id = ?", (report_id,))
                conn.commit()

                resumed_id = hot_video_report._start_report(conn, "2026-07-14", "US", [])
                rows = conn.execute(
                    "SELECT video_id, process_status FROM hot_report_videos WHERE report_date = '2026-07-14'"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(resumed_id, report_id)
        self.assertEqual(rows, [("complete", "complete")])

    def test_resume_rechecks_saved_videos_against_current_recency_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            hot_video_report,
            "DB_PATH",
            Path(temp_dir) / "report.sqlite",
        ):
            conn = hot_video_report._connect()
            try:
                report_id = hot_video_report._start_report(conn, "2026-07-14", "US", [])
                insert_report_video(conn, report_id, "2026-07-14", "recent", "complete", time.time() - 86400)
                insert_report_video(conn, report_id, "2026-07-14", "stale", "complete", time.time() - 8 * 86400)
                insert_report_video(conn, report_id, "2026-07-14", "missing", "complete", None)
                conn.commit()

                removed = hot_video_report._prune_stale_resumable_videos(conn, "2026-07-14", 7)
                remaining = conn.execute(
                    "SELECT video_id FROM hot_report_videos WHERE report_date = '2026-07-14' ORDER BY video_id"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(removed, 2)
        self.assertEqual(remaining, [("recent",)])

    def test_interrupted_report_remains_recoverable_until_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            hot_video_report,
            "DB_PATH",
            Path(temp_dir) / "report.sqlite",
        ):
            conn = hot_video_report._connect()
            try:
                report_id = hot_video_report._start_report(conn, "2026-07-14", "US", [])
                insert_report_video(conn, report_id, "2026-07-14", "inflight", "processing", time.time())
                conn.commit()
            finally:
                conn.close()

            recovered = hot_video_report.recover_interrupted_reports()
            conn = hot_video_report._connect()
            try:
                report_status = conn.execute(
                    "SELECT status FROM daily_reports WHERE report_date = '2026-07-14'"
                ).fetchone()[0]
                video_status = conn.execute(
                    "SELECT process_status FROM hot_report_videos WHERE video_id = 'inflight'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(recovered, {"recovered": ["2026-07-14"]})
        self.assertEqual(report_status, "running")
        self.assertEqual(video_status, "failed")

    def test_video_scheduler_runs_five_workers_at_a_time(self) -> None:
        class FakeCursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, sql, _params=()):
                return FakeCursor((0,) if "MAX(report_rank)" in sql else ("complete",))

            def commit(self):
                return None

        active = 0
        max_active = 0
        launched = 0

        def fake_launch(_report_date, item):
            nonlocal active, max_active, launched
            active += 1
            launched += 1
            max_active = max(max_active, active)
            return {"item": item, "process": types.SimpleNamespace(poll=lambda: 0)}

        def fake_close(_worker, _success):
            nonlocal active
            active -= 1

        ranked = [candidate(str(index), 100 - index, "stream", "trending") for index in range(10)]
        counts = {"analyzed_success": 0, "analyzed_failed": 0}
        with (
            patch("hot_video_report._upsert_video"),
            patch("hot_video_report._launch_report_video_worker", side_effect=fake_launch),
            patch("hot_video_report._close_report_video_worker", side_effect=fake_close),
            patch("hot_video_report._enqueue_report_video_translation"),
            patch("hot_video_report._progress_payload"),
        ):
            hot_video_report._process_ranked_videos(
                FakeConnection(),
                "report-id",
                "2026-07-14",
                ranked,
                10,
                counts,
            )

        self.assertEqual(launched, 10)
        self.assertEqual(max_active, 5)
        self.assertEqual(counts, {"analyzed_success": 10, "analyzed_failed": 0})

    def test_topic_guarantees_do_not_occupy_all_remaining_slots(self) -> None:
        candidates = [
            candidate("a1", 100, "topic", "topic-search-top:A"),
            candidate("a2", 90, "topic", "topic-search-top:A"),
            candidate("b1", 70, "topic", "topic-search-top:B"),
            candidate("s1", 95, "stream", "videos-popular:views:p1"),
            candidate("s2", 85, "stream", "trending:US:p1"),
        ]

        ranked = hot_video_report._rank_with_topic_guarantees(candidates, ["A", "B"], 3)

        self.assertEqual([item["video_id"] for item in ranked[:3]], ["a1", "s1", "b1"])
        self.assertEqual(len(ranked), len(candidates))

    def test_popular_and_trending_are_sampled_when_topics_are_already_full(self) -> None:
        called_endpoints: list[str] = []

        def fake_call_api(_key, _base, endpoint, _params, _timeout, **_kwargs):
            called_endpoints.append(endpoint)
            if endpoint == "topic":
                return [{"id": f"topic-{index}"} for index in range(3)]
            return []

        def fake_normalize(node, endpoint, label, rank):
            return {
                "platform": "tiktok",
                "video_id": node["id"],
                "source_endpoint": endpoint,
                "source_label": label,
                "source_rank": rank,
                "hot_score": 100_000 - rank,
                "metrics": {"play_count": 100_000, "published_at": time.time()},
                "raw": {},
            }

        counts: defaultdict[str, int] = defaultdict(int)
        with (
            patch("hot_video_report.call_api", side_effect=fake_call_api),
            patch("hot_video_report._iter_video_nodes", side_effect=lambda payload: payload),
            patch("hot_video_report._normalize_video", side_effect=fake_normalize),
            patch("hot_video_report._is_photo_mode_post", return_value=False),
            patch("hot_video_report._has_usable_video_media", return_value=True),
            patch("hot_video_report._topic_source_requests", return_value=[("topic", {}, "topic-search-top:A")]),
            patch("hot_video_report._popular_source_requests", return_value=[("popular", {}, "videos-popular:views:p1")]),
            patch("hot_video_report._trending_source_requests", return_value=[("trending", {}, "trending:US:p1")]),
        ):
            hot_video_report._collect_hot_video_candidates(
                "2026-07-14",
                "US",
                2,
                7,
                ["A"],
                "key",
                "base",
                10,
                counts,
            )

        self.assertEqual(called_endpoints, ["topic", "popular", "trending"])

    def test_topic_search_defaults_to_view_sorting(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOT_VIDEO_TOPIC_SORT_BY", None)
            requests = hot_video_report._topic_source_requests("AI", "US", 20, 7)
        self.assertEqual(requests[0][1]["sort_by"], "views")


if __name__ == "__main__":
    unittest.main()
