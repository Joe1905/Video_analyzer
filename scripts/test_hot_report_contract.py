#!/usr/bin/env python3
"""Run with: docker compose -p short-video-analyzer run --rm analyzer python scripts/test_hot_report_contract.py"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import deepseek_postprocess
import hot_video_report as report


def _video_node(index: int) -> dict:
    return {
        "id": f"video-{index}",
        "desc": f"video {index}",
        "create_time": time.time(),
        "statistics": {
            "play_count": 10_000 + index,
            "digg_count": 100,
            "comment_count": 10,
            "share_count": 5,
            "collect_count": 20,
        },
        "video": {"play_addr": {"url_list": [f"https://example.test/video-{index}.mp4"]}, "duration": 30_000},
    }


def main() -> int:
    payload = {key: [] for key in report.REQUIRED_DAILY_REPORT_KEYS}
    payload["summary"] = "fixture"
    assert report._validate_daily_report_shape(payload) == payload
    assert "# fixture" in report._markdown_from_report(payload)
    try:
        report._validate_daily_report_shape({"summary": "missing"})
    except ValueError:
        pass
    else:
        raise AssertionError("missing required report keys were accepted")

    metrics = {
        "play_count": 1000,
        "like_count": 100,
        "comment_count": 10,
        "share_count": 5,
        "favorite_count": 20,
    }
    assert report._score_hot_video(metrics, 1) == 101_250

    # 长视频过滤:>180s 剔除,时长缺失保守放行
    assert report._is_long_video({"duration_ms": 180_000}) is False
    assert report._is_long_video({"duration_ms": 180_001}) is True
    assert report._is_long_video({"duration_ms": None}) is False
    assert report._extract_duration_ms({"video": {"duration": 64_565}}) == 64_565
    assert report._extract_duration_ms({"duration": 180_000}) == 180_000
    assert report._extract_duration_ms({"desc": "x"}) is None

    # 候选池分层:10 主 + 10 备
    pool = [{"platform": "tiktok", "video_id": f"v{i}", "hot_score": 1000 - i, "selection_bucket": "stream", "source_label": "x"} for i in range(25)]
    layered = report._rank_with_topic_guarantees(pool, [], 10, backup_count=10)
    assert len(layered) == 20
    assert [item["selection_tier"] for item in layered] == ["primary"] * 10 + ["backup"] * 10
    assert layered[0]["video_id"] == "v0" and layered[19]["video_id"] == "v19"
    layered0 = report._rank_with_topic_guarantees(pool, [], 10, backup_count=0)
    assert len(layered0) == 10 and all(item["selection_tier"] == "primary" for item in layered0)

    # 备份数动态规则:目标 <10 时备份=目标数,>=10 时备份=10
    assert report._report_backup_count(3) == 3
    assert report._report_backup_count(9) == 9
    assert report._report_backup_count(10) == 10
    assert report._report_backup_count(20) == 10
    assert report._report_backup_count() == 10

    # 下载/解析限时解析
    assert isinstance(report._report_video_download_timeout_seconds(), int)
    assert report._report_video_analyze_timeout_seconds() >= 30
    # 动态限时:时长越长限时越久,且固定附加 REPORT_VIDEO_TIMEOUT_EXTRA_SECONDS(默认 20s)
    assert report._report_video_analyze_timeout_seconds(60000) > report._report_video_analyze_timeout_seconds(20000)
    assert report._report_video_download_timeout_seconds(180000) > report._report_video_download_timeout_seconds(20000)
    assert report._report_video_analyze_timeout_seconds(None) >= 30
    assert report._report_video_download_timeout_seconds(None) >= 30
    assert report._report_timeout_extra_seconds() >= 0


    previous_sort = os.environ.get("HOT_VIDEO_TOPIC_SORT_BY")
    previous_pages = os.environ.get("HOT_VIDEO_TOPIC_MAX_PAGES")
    os.environ["HOT_VIDEO_TOPIC_SORT_BY"] = "most-liked"
    os.environ["HOT_VIDEO_TOPIC_MAX_PAGES"] = "3"
    try:
        primary = report._topic_source_requests("AI toys", "US", 60, 7)
        assert primary == [
            (
                "search-top",
                {
                    "query": "AI toys",
                    "region": "US",
                    "publish_time": "this-week",
                    "sort_by": "most-liked",
                },
                "topic-search-top:AI toys",
            )
        ]
        fallback = report._topic_source_requests("AI toys", "US", 60, 7, fallback=True)
        assert fallback[0][1] == {
            "query": "AI toys",
            "region": "US",
            "date_posted": "this-week",
            "sort_by": "most-liked",
        }
        assert "count" not in primary[0][1] and "days" not in primary[0][1] and "page" not in primary[0][1]
        assert report._response_next_cursor({"data": {"cursor": 20, "has_more": 1}}) == 20
        assert report._response_next_cursor({"data": {"cursor": 40, "has_more": 0}}) is None

        ranked = report._rank_with_topic_guarantees(
            [
                {"platform": "tiktok", "video_id": "topic-a", "hot_score": 100, "selection_bucket": "topic", "source_label": "topic-search-top:A"},
                {"platform": "tiktok", "video_id": "topic-b", "hot_score": 90, "selection_bucket": "topic", "source_label": "topic-search-top:B"},
                {"platform": "tiktok", "video_id": "stream", "hot_score": 1_000, "selection_bucket": "stream", "source_label": "trending:US"},
                {"platform": "tiktok", "video_id": "topic-extra", "hot_score": 80, "selection_bucket": "topic", "source_label": "topic-search-top:A"},
            ],
            ["A", "B"],
            4,
        )
        assert [item["video_id"] for item in ranked] == ["topic-a", "topic-b", "stream", "topic-extra"]

        calls: list[tuple[str, dict]] = []
        original_call_api = report.call_api
        def fake_call_api(api_key, api_base, endpoint, params, timeout, cache_policy="read_write"):
            calls.append((endpoint, dict(params)))
            if endpoint != "search-top":
                return {"data": {"items": [], "has_more": 0}}
            cursor = params.get("cursor")
            index = {None: 1, "cursor-1": 2, "cursor-2": 3}[cursor]
            return {
                "data": {
                    "items": [_video_node(index)],
                    "cursor": f"cursor-{index}",
                    "has_more": 0 if index == 3 else 1,
                }
            }
        report.call_api = fake_call_api
        counts = {
            "collected": 0,
            "candidate_count": 0,
            "recent_count": 0,
            "enriched_count": 0,
            "skipped_old": 0,
            "skipped_missing_time": 0,
            "skipped_photo_mode": 0,
            "skipped_no_video_media": 0,
            "skipped_low_views_topic": 0,
            "skipped_low_views_stream": 0,
            "skipped_duplicate_report": 0,
        }
        try:
            candidates, errors = report._collect_hot_video_candidates(
                "2026-08-04", "US", 3, 7, ["AI"], "fixture", "https://example.test", 10, counts
            )
        finally:
            report.call_api = original_call_api
        assert not errors
        assert len(candidates) == 3
        topic_calls = [params for endpoint, params in calls if endpoint == "search-top"]
        assert [call.get("cursor") for call in topic_calls] == [None, "cursor-1", "cursor-2"]
        assert any(endpoint == "videos-popular" for endpoint, _ in calls)
        assert any(endpoint == "trending" for endpoint, _ in calls)
    finally:
        if previous_sort is None:
            os.environ.pop("HOT_VIDEO_TOPIC_SORT_BY", None)
        else:
            os.environ["HOT_VIDEO_TOPIC_SORT_BY"] = previous_sort
        if previous_pages is None:
            os.environ.pop("HOT_VIDEO_TOPIC_MAX_PAGES", None)
        else:
            os.environ["HOT_VIDEO_TOPIC_MAX_PAGES"] = previous_pages

    final_prompt = report._summary_prompt_v2("2026-08-01", [], partial_summaries=[{}])
    assert "common_patterns" in final_prompt
    assert "video_deep_dives" in final_prompt
    assert "不得输出 key_observations" in final_prompt

    captured = {}
    original_post = deepseek_postprocess.requests.post
    original_record = deepseek_postprocess.record_api_call
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "{}"}}]}
    def fake_post(*args, **kwargs):
        captured.update(kwargs["json"])
        return Response()
    deepseek_postprocess.requests.post = fake_post
    deepseek_postprocess.record_api_call = lambda *args, **kwargs: None
    try:
        deepseek_postprocess.call_deepseek("fixture", "fixture", "https://example.test/v1", "fixture", 100, reasoning_effort="disabled")
    finally:
        deepseek_postprocess.requests.post = original_post
        deepseek_postprocess.record_api_call = original_record
    assert captured.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in captured
    print("hot report contract and DeepSeek parameters: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
