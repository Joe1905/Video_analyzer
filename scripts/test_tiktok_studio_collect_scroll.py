#!/usr/bin/env python3
"""Ad-hoc checks for TikTok Studio date-boundary scrolling."""

from unittest.mock import patch

import tiktok_studio_collect as collect


class FakePage:
    url = "https://www.tiktok.com/tiktokstudio/content?lang=en"

    def evaluate(self, _script: str) -> None:
        return None

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


def row(video_id: str, published_date: str) -> dict[str, str]:
    month, day = (int(value) for value in published_date.split("-")[1:])
    month_name = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )[month - 1]
    return {
        "id": video_id,
        "url": f"https://www.tiktok.com/tiktokstudio/analytics/{video_id}",
        "title_hint": f"video {video_id}\n{month_name} {day}, 1:00 PM",
    }


def assert_waits_for_date_boundary_after_stalls() -> None:
    rounds = [
        [row("17", "2026-07-17"), row("16", "2026-07-16")],
        [row("17", "2026-07-17"), row("16", "2026-07-16")],
        [row("17", "2026-07-17"), row("16", "2026-07-16")],
        [row("17", "2026-07-17"), row("16", "2026-07-16")],
        [row("01", "2026-07-01")],
        [row("30", "2026-06-30")],
    ]
    with (
        patch.object(collect, "_discover_links_on_page", side_effect=rounds),
        patch.object(collect, "_declared_post_count", return_value=0),
        patch.object(
            collect,
            "_scroll_video_list_once",
            return_value={"moved": True, "at_end": False},
        ),
    ):
        result = collect._discover_video_links(FakePage(), "2026-07-01", "2026-07-17")
    assert {item["id"] for item in result} == {"17", "16", "01"}


def assert_declared_total_still_requires_scroll_end() -> None:
    rows = [row("17", "2026-07-17"), row("16", "2026-07-16")]
    with (
        patch.object(collect, "VIDEO_LIST_END_CONFIRMATIONS", 2),
        patch.object(collect, "_discover_links_on_page", return_value=rows),
        patch.object(collect, "_declared_post_count", return_value=2),
        patch.object(
            collect,
            "_scroll_video_list_once",
            return_value={"moved": False, "at_end": True},
        ) as scroll,
    ):
        result = collect._discover_video_links(FakePage(), "2026-07-01", "2026-07-17")
    assert len(result) == 2
    assert scroll.call_count == 2


def assert_unconfirmed_partial_list_fails() -> None:
    rows = [row("17", "2026-07-17")]
    with (
        patch.object(collect, "VIDEO_LIST_MAX_SCROLL_ROUNDS", 3),
        patch.object(collect, "_discover_links_on_page", return_value=rows),
        patch.object(collect, "_declared_post_count", return_value=0),
        patch.object(
            collect,
            "_scroll_video_list_once",
            return_value={"moved": True, "at_end": False},
        ),
    ):
        try:
            collect._discover_video_links(FakePage(), "2026-07-01", "2026-07-17")
        except RuntimeError as exc:
            assert "尚未确认到达列表末尾" in str(exc)
        else:
            raise AssertionError("an unconfirmed partial list must not be treated as complete")


def main() -> int:
    assert_waits_for_date_boundary_after_stalls()
    assert_declared_total_still_requires_scroll_end()
    assert_unconfirmed_partial_list_fails()
    print("TikTok Studio scroll checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
