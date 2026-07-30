#!/usr/bin/env python3
"""Regression tests for the high-availability SociaVault tool router."""

from social_tool_router import (
    SOCIAVAULT_OFFICIAL_TOOL_NAMES,
    apply_social_route_mode,
    candidate_tool_names,
    detect_social_capabilities,
    detect_social_platforms,
    model_social_tool_route,
    normalize_router_mode,
    rule_social_tool_route,
    sociavault_catalog_issues,
    sociavault_tool_metadata,
)


OFFICIAL_SOCIALVAULT_TOOLS = SOCIAVAULT_OFFICIAL_TOOL_NAMES


def test_official_catalog_is_fully_mapped() -> None:
    assert len(OFFICIAL_SOCIALVAULT_TOOLS) == 107
    metadata, unknown = sociavault_tool_metadata(OFFICIAL_SOCIALVAULT_TOOLS)
    assert unknown == ()
    assert len(metadata) == 107
    assert sociavault_catalog_issues(OFFICIAL_SOCIALVAULT_TOOLS) == ()


def test_platform_and_capability_rules_are_multilabel() -> None:
    assert detect_social_platforms(
        "比较 TikTok、Instagram 和 youtube.com 的热门视频评论"
    ) == ("tiktok", "instagram", "youtube")
    assert detect_social_platforms("查 TikTok Shop 商品") == ("tiktok_shop",)
    assert detect_social_platforms("分析 Facebook 广告库") == ("facebook",)
    assert detect_social_platforms("看一下广告库趋势") == ("ad_library",)
    assert set(detect_social_capabilities("热门视频的评论和字幕")) == {
        "content", "comments", "transcript", "trend",
    }


def test_explicit_platform_routes_without_model() -> None:
    route = rule_social_tool_route(
        "分析 instagram.com/creator 的最新帖子",
        OFFICIAL_SOCIALVAULT_TOOLS,
    )
    assert route is not None
    assert route.source == "rules"
    assert route.platforms == ("instagram",)
    assert "instagram_posts" in route.candidate_tools
    assert all(name.startswith("instagram_") for name in route.candidate_tools)

    full_platform = rule_social_tool_route(
        "帮我看看 YouTube",
        OFFICIAL_SOCIALVAULT_TOOLS,
    )
    assert full_platform is not None
    youtube_count = sum(name.startswith("youtube_") for name in OFFICIAL_SOCIALVAULT_TOOLS)
    assert len(full_platform.candidate_tools) == youtube_count


def test_followup_inherits_last_confirmed_platform() -> None:
    route = rule_social_tool_route(
        "再看看评论",
        OFFICIAL_SOCIALVAULT_TOOLS,
        inherited_platforms=("youtube",),
    )
    assert route is not None
    assert route.platforms == ("youtube",)
    assert "youtube_video_comments" in route.candidate_tools
    assert "youtube_video" in route.candidate_tools
    assert "youtube_search" in route.candidate_tools
    assert "youtube_channel_videos" in route.candidate_tools
    assert not any(name.startswith("tiktok_") for name in route.candidate_tools)


def test_multi_intent_does_not_collapse_to_one_capability() -> None:
    route = rule_social_tool_route(
        "查 TikTok 热门商品、竞品视频趋势和评论",
        OFFICIAL_SOCIALVAULT_TOOLS,
    )
    assert route is not None
    assert {"commerce", "content", "comments", "trend"} <= set(route.capabilities)
    assert "tiktok_videos_popular" in route.candidate_tools
    assert "tiktok_comments" in route.candidate_tools


def test_controlled_regression_corpus_has_complete_required_tool_recall() -> None:
    cases = (
        ("TK热门视频评论", {"tiktok_comments", "tiktok_videos", "tiktok_search_keyword"}),
        ("IG帖子字幕", {"instagram_transcript", "instagram_posts", "instagram_post_info"}),
        ("YouTube 视频评论", {"youtube_video_comments", "youtube_search", "youtube_video"}),
        ("X平台粉丝关系", {"twitter_followers", "twitter_followings", "twitter_profile"}),
        ("Facebook 广告库公司广告", {"facebook_ad_library_company_ads"}),
        ("Reddit 帖子评论和转录", {"reddit_post_comments", "reddit_post_transcript", "reddit_search"}),
        ("Threads 搜索用户", {"threads_search", "threads_search_users", "threads_profile"}),
        ("Pinterest 搜索 pin", {"pinterest_search", "pinterest_pin"}),
        ("Twitch 直播计划", {"twitch_user_schedule"}),
        ("TikTok Shop 商品评价", {"tiktok_shop_product_reviews"}),
        ("Google 广告库 advertiser", {"google_ad_library_search_advertisers"}),
        ("LinkedIn 公司主页", {"linkedin_company", "linkedin_profile"}),
        ("检查 SociaVault 余额", {"check_credits"}),
        (
            "比较 Instagram 和 YouTube 的账号及粉丝",
            {"instagram_profile", "youtube_channel"},
        ),
    )
    recalled = 0
    required = 0
    for text, expected_tools in cases:
        route = rule_social_tool_route(text, OFFICIAL_SOCIALVAULT_TOOLS)
        assert route is not None, text
        recalled += len(expected_tools & set(route.candidate_tools))
        required += len(expected_tools)
        assert expected_tools <= set(route.candidate_tools), text
    assert recalled == required


def test_model_route_and_high_availability_fallbacks() -> None:
    valid = model_social_tool_route(
        {
            "platforms": ["instagram", "youtube"],
            "capabilities": ["profile", "audience"],
            "confidence": 0.94,
        },
        OFFICIAL_SOCIALVAULT_TOOLS,
    )
    assert valid.source == "model"
    assert "instagram_profile" in valid.candidate_tools
    assert "youtube_channel" in valid.candidate_tools

    for invalid in (
        None,
        {"platforms": [], "capabilities": [], "confidence": 1},
        {"platforms": ["instagram"], "capabilities": [], "confidence": 0.79},
        {"platforms": ["unknown"], "capabilities": ["content"], "confidence": 1},
        {"platforms": ["youtube"], "capabilities": ["unknown"], "confidence": 1},
        {"platforms": ["account"], "capabilities": ["account"], "confidence": 1},
        {"platforms": ["youtube"], "capabilities": [], "confidence": float("nan")},
    ):
        fallback = model_social_tool_route(invalid, OFFICIAL_SOCIALVAULT_TOOLS)
        assert fallback.source == "fallback_all"
        assert len(fallback.candidate_tools) == 107

    unknown_runtime = model_social_tool_route(
        {"platforms": ["youtube"], "capabilities": ["content"], "confidence": 1},
        OFFICIAL_SOCIALVAULT_TOOLS + ("new_unclassified_tool",),
    )
    assert unknown_runtime.source == "fallback_all"
    assert len(unknown_runtime.candidate_tools) == 108


def test_mode_application_preserves_non_social_tools() -> None:
    route = rule_social_tool_route(
        "YouTube 视频评论",
        OFFICIAL_SOCIALVAULT_TOOLS,
    )
    assert route is not None
    full = {
        "system__current_time",
        "function__video_analyze",
        *(f"sociavault__{name}" for name in OFFICIAL_SOCIALVAULT_TOOLS),
    }
    legacy = {"system__current_time", "sociavault__youtube_video_comments"}
    assert apply_social_route_mode("off", full, legacy, route) == legacy
    assert apply_social_route_mode("shadow", full, legacy, route) == full
    enforced = apply_social_route_mode("enforce", full, legacy, route)
    assert "system__current_time" in enforced
    assert "function__video_analyze" in enforced
    assert "sociavault__youtube_video_comments" in enforced
    assert "sociavault__tiktok_comments" not in enforced
    assert len(enforced) <= 128
    assert normalize_router_mode("unexpected") == "off"


def test_candidate_builder_rejects_unknown_runtime_tools() -> None:
    candidates, unknown = candidate_tool_names(
        ("tiktok",),
        ("content",),
        OFFICIAL_SOCIALVAULT_TOOLS + ("future_tool",),
    )
    assert candidates == ()
    assert unknown == ("unexpected:future_tool",)


if __name__ == "__main__":
    test_official_catalog_is_fully_mapped()
    test_platform_and_capability_rules_are_multilabel()
    test_explicit_platform_routes_without_model()
    test_followup_inherits_last_confirmed_platform()
    test_multi_intent_does_not_collapse_to_one_capability()
    test_controlled_regression_corpus_has_complete_required_tool_recall()
    test_model_route_and_high_availability_fallbacks()
    test_mode_application_preserves_non_social_tools()
    test_candidate_builder_rejects_unknown_runtime_tools()
    print("social tool router tests passed")
