#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
import types
import unittest
from collections import defaultdict
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


class HotReportSelectionTests(unittest.TestCase):
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
