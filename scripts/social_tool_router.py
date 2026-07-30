"""High-availability SociaVault platform and capability routing.

The router may reduce the tool window, but it must never make an uncertain
classification a hard dependency. Unknown tools, invalid model decisions and
empty candidate sets therefore fall back to the complete runtime catalog.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable


SOCIAL_PLATFORMS = frozenset({
    "tiktok",
    "tiktok_shop",
    "instagram",
    "youtube",
    "twitter",
    "linkedin",
    "facebook",
    "reddit",
    "threads",
    "pinterest",
    "twitch",
    "google",
    "ad_library",
    "account",
})
SOCIAL_CAPABILITIES = frozenset({
    "profile",
    "content",
    "search",
    "comments",
    "transcript",
    "trend",
    "audience",
    "live",
    "ads",
    "commerce",
    "account",
})
SOCIAL_TOOL_ROUTER_MODES = frozenset({"off", "shadow", "enforce"})
SOCIAVAULT_OFFICIAL_TOOL_NAMES = (
    "tiktok_profile", "tiktok_demographics", "tiktok_videos", "tiktok_video_info",
    "tiktok_transcript", "tiktok_live", "tiktok_comments", "tiktok_comment_replies",
    "tiktok_following", "tiktok_followers", "tiktok_search_users",
    "tiktok_search_hashtag", "tiktok_search_keyword", "tiktok_search_music",
    "tiktok_search_top", "tiktok_music_popular", "tiktok_music_details",
    "tiktok_music_videos", "tiktok_trending", "tiktok_creators_popular",
    "tiktok_videos_popular", "tiktok_hashtags_popular", "tiktok_shop_products",
    "tiktok_shop_product_details", "tiktok_shop_search", "tiktok_shop_product_reviews",
    "instagram_profile", "instagram_posts", "instagram_post_info", "instagram_transcript",
    "instagram_comments", "instagram_reels", "instagram_highlights",
    "instagram_highlight_detail", "instagram_reels_by_song", "youtube_channel",
    "youtube_channel_videos", "youtube_channel_shorts", "youtube_video",
    "youtube_video_transcript", "youtube_search", "youtube_search_hashtag",
    "youtube_video_comments", "youtube_video_comment_replies", "youtube_shorts_trending",
    "youtube_channel_playlists", "youtube_channel_lives",
    "youtube_channel_community_posts", "twitch_profile", "twitch_user_videos",
    "twitch_user_schedule", "twitch_clip", "tiktok_ad_library_search",
    "tiktok_ad_library_ad", "linkedin_profile", "linkedin_company", "linkedin_post",
    "facebook_profile", "facebook_profile_posts", "facebook_comment_replies",
    "facebook_profile_reels", "facebook_group_posts", "facebook_post",
    "facebook_post_transcript", "facebook_post_comments",
    "facebook_ad_library_ad_details", "facebook_ad_library_search",
    "facebook_ad_library_company_ads", "facebook_ad_library_search_companies",
    "facebook_marketplace_location_search", "facebook_marketplace_search",
    "facebook_marketplace_item", "google_ad_library_company_ads",
    "google_ad_library_ad_details", "google_ad_library_search_advertisers",
    "linkedin_ad_library_search", "linkedin_ad_library_ad_details", "twitter_profile",
    "twitter_user_tweets", "twitter_user_tweets_all", "twitter_tweet",
    "twitter_tweet_transcript", "twitter_comments", "twitter_quotes", "twitter_retweets",
    "twitter_search", "twitter_followers", "twitter_followings", "twitter_community",
    "twitter_community_tweets", "reddit_subreddit_details", "reddit_subreddit",
    "reddit_subreddit_search", "reddit_post_comments", "reddit_post_transcript",
    "reddit_search", "threads_profile", "threads_user_posts", "threads_post",
    "threads_search", "threads_search_users", "google_search", "pinterest_search",
    "pinterest_pin", "pinterest_user_boards", "pinterest_board", "check_credits",
)


@dataclass(frozen=True)
class SocialToolRoute:
    platforms: tuple[str, ...]
    capabilities: tuple[str, ...]
    source: str
    confidence: float
    fallback_reason: str
    candidate_tools: tuple[str, ...]

    def log_payload(self, *, mode: str, full_tool_count: int) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("candidate_tools", None)
        payload.update({
            "mode": mode,
            "candidate_count": len(self.candidate_tools),
            "full_tool_count": int(full_tool_count),
        })
        return payload


_PLATFORM_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("instagram", (r"\binstagram\b", r"instagram\.com", r"\binsta\b", r"(?<![a-z0-9])(?:ig|ins)(?![a-z0-9])", r"照片墙")),
    ("youtube", (r"\byoutube\b", r"youtube\.com", r"youtu\.be", r"(?<![a-z0-9])yt(?![a-z0-9])", r"油管")),
    ("twitter", (r"\btwitter\b", r"twitter\.com", r"(?:^|[/:.])x\.com", r"(?<![a-z0-9])x(?:平台)?(?![a-z0-9])", r"推特")),
    ("linkedin", (r"\blinkedin\b", r"linkedin\.com", r"领英")),
    ("facebook", (r"\bfacebook\b", r"facebook\.com", r"(?<![a-z0-9])fb(?![a-z0-9])", r"脸书")),
    ("reddit", (r"\breddit\b", r"reddit\.com")),
    ("threads", (r"\bthreads\b", r"threads\.net")),
    ("pinterest", (r"\bpinterest\b", r"pinterest\.com")),
    ("twitch", (r"\btwitch\b", r"twitch\.tv")),
    ("google", (r"\bgoogle\b", r"谷歌")),
)
_TIKTOK_SHOP_PATTERN = re.compile(
    r"\btik\s*tok\s*shop\b|(?<![a-z0-9])tk\s*shop(?![a-z0-9])|tiktok小店|tk小店|抖音小店",
    re.IGNORECASE,
)
_TIKTOK_PATTERN = re.compile(
    r"\btik\s*tok\b|tiktok\.com|(?<![a-z0-9])tk(?![a-z0-9])|抖音",
    re.IGNORECASE,
)
_AD_LIBRARY_PATTERN = re.compile(
    r"\bad\s*library\b|\bads\s*library\b|广告库|广告资料库",
    re.IGNORECASE,
)

_CAPABILITY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account", (r"\bcredits?\b", r"\bbalance\b", r"余额", r"积分")),
    ("ads", (r"\bad\s*library\b", r"\bads?\b", r"广告库", r"广告资料库", r"广告素材")),
    ("commerce", (r"\bshop\b", r"\bmarketplace\b", r"\bproducts?\b", r"商品", r"产品", r"店铺", r"小店", r"商城")),
    ("transcript", (r"\btranscripts?\b", r"\bsubtitles?\b", r"转录", r"字幕", r"逐字稿")),
    ("comments", (r"\bcomments?\b", r"\brepl(?:y|ies)\b", r"\breviews?\b", r"评论", r"回复", r"评价")),
    ("audience", (r"\bfollowers?\b", r"\bfollowings?\b", r"\bdemographics?\b", r"粉丝", r"关注", r"受众", r"人群画像")),
    ("live", (r"\blives?\b", r"\bstream(?:s|ing)?\b", r"直播", r"开播")),
    ("trend", (r"\btrends?\b", r"\btrending\b", r"\bviral\b", r"\bpopular\b", r"\btop\b", r"\brank(?:ing)?\b", r"热门", r"趋势", r"热度", r"榜单", r"排行")),
    ("search", (r"\bsearch\b", r"\bfind\b", r"\bhashtag\b", r"\bkeyword\b", r"搜索", r"查找", r"关键词", r"话题", r"标签")),
    ("profile", (r"\bprofiles?\b", r"\baccounts?\b", r"\bchannels?\b", r"\bcreators?\b", r"主页", r"账号", r"用户", r"作者", r"达人", r"频道")),
    ("content", (r"\bvideos?\b", r"\bposts?\b", r"\breels?\b", r"\bshorts?\b", r"\btweets?\b", r"\bpins?\b", r"\bmusic\b", r"\bclips?\b", r"内容", r"视频", r"帖子", r"作品", r"推文", r"短视频", r"音乐")),
)

_TOOL_PLATFORM_PREFIXES: tuple[tuple[str, str], ...] = (
    ("tiktok_shop_", "tiktok_shop"),
    ("tiktok_", "tiktok"),
    ("instagram_", "instagram"),
    ("youtube_", "youtube"),
    ("twitter_", "twitter"),
    ("linkedin_", "linkedin"),
    ("facebook_", "facebook"),
    ("reddit_", "reddit"),
    ("threads_", "threads"),
    ("pinterest_", "pinterest"),
    ("twitch_", "twitch"),
    ("google_", "google"),
)
_CONTENT_MARKERS = (
    "video", "videos", "post", "posts", "reels", "shorts", "tweet", "tweets",
    "pin", "board", "playlist", "music", "clip", "community", "subreddit",
    "quotes", "retweets", "highlight",
)
_PROFILE_MARKERS = ("profile", "channel", "company", "subreddit_details", "user_boards")
_DETAIL_DEPENDENCIES = frozenset({
    "tiktok_video_info",
    "instagram_post_info",
    "youtube_video",
    "twitter_tweet",
    "facebook_post",
    "threads_post",
    "pinterest_pin",
})
_DISCOVERY_DEPENDENCIES: dict[str, frozenset[str]] = {
    "tiktok": frozenset({
        "tiktok_profile", "tiktok_videos", "tiktok_video_info",
        "tiktok_search_users", "tiktok_search_keyword",
    }),
    "instagram": frozenset({
        "instagram_profile", "instagram_posts", "instagram_post_info",
    }),
    "youtube": frozenset({
        "youtube_channel", "youtube_channel_videos", "youtube_video", "youtube_search",
    }),
    "twitter": frozenset({
        "twitter_profile", "twitter_user_tweets", "twitter_tweet", "twitter_search",
    }),
    "facebook": frozenset({
        "facebook_profile", "facebook_profile_posts", "facebook_post",
    }),
    "reddit": frozenset({
        "reddit_subreddit_details", "reddit_subreddit", "reddit_subreddit_search",
        "reddit_search",
    }),
    "threads": frozenset({
        "threads_profile", "threads_user_posts", "threads_post", "threads_search",
    }),
    "pinterest": frozenset({
        "pinterest_search", "pinterest_pin",
    }),
}


def normalize_router_mode(value: str | None) -> str:
    mode = str(value or "off").strip().lower()
    return mode if mode in SOCIAL_TOOL_ROUTER_MODES else "off"


def detect_social_platforms(text: str) -> tuple[str, ...]:
    lowered = str(text or "").lower()
    platforms: list[str] = []
    shop_match = _TIKTOK_SHOP_PATTERN.search(lowered)
    tiktok_text = _TIKTOK_SHOP_PATTERN.sub(" ", lowered)
    if shop_match:
        platforms.append("tiktok_shop")
    if _TIKTOK_PATTERN.search(tiktok_text):
        platforms.append("tiktok")
    for platform, patterns in _PLATFORM_PATTERNS:
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns):
            platforms.append(platform)
    if _AD_LIBRARY_PATTERN.search(lowered) and not any(
        platform in {"tiktok", "linkedin", "facebook", "google"}
        for platform in platforms
    ):
        platforms.append("ad_library")
    return tuple(dict.fromkeys(platforms))


def detect_social_capabilities(text: str) -> tuple[str, ...]:
    lowered = str(text or "").lower()
    capabilities = [
        capability
        for capability, patterns in _CAPABILITY_PATTERNS
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)
    ]
    return tuple(dict.fromkeys(capabilities))


def _tool_platforms(name: str) -> frozenset[str]:
    if name == "check_credits":
        return frozenset({"account"})
    for prefix, platform in _TOOL_PLATFORM_PREFIXES:
        if name.startswith(prefix):
            platforms = {platform}
            if "_ad_library_" in name:
                platforms.add("ad_library")
            return frozenset(platforms)
    return frozenset()


def _tool_capabilities(name: str) -> frozenset[str]:
    if name == "check_credits":
        return frozenset({"account"})
    capabilities: set[str] = set()
    if "_ad_library_" in name:
        capabilities.add("ads")
    if name.startswith("tiktok_shop_") or "_marketplace_" in name:
        capabilities.add("commerce")
    if "transcript" in name:
        capabilities.add("transcript")
    if any(marker in name for marker in ("comment", "replies", "reviews")):
        capabilities.add("comments")
    if any(marker in name for marker in ("followers", "following", "followings", "demographics")):
        capabilities.add("audience")
    if any(marker in name for marker in ("live", "schedule")):
        capabilities.add("live")
    if any(marker in name for marker in ("trending", "popular")) or name.endswith("_top"):
        capabilities.add("trend")
    if "search" in name:
        capabilities.add("search")
    if any(marker in name for marker in _PROFILE_MARKERS):
        capabilities.add("profile")
    if any(marker in name for marker in _CONTENT_MARKERS):
        capabilities.add("content")
    return frozenset(capabilities)


def sociavault_tool_metadata(tool_names: Iterable[str]) -> tuple[dict[str, dict[str, frozenset[str]]], tuple[str, ...]]:
    metadata: dict[str, dict[str, frozenset[str]]] = {}
    unknown: list[str] = []
    for raw_name in tool_names:
        name = str(raw_name or "").split("__", 1)[-1]
        platforms = _tool_platforms(name)
        capabilities = _tool_capabilities(name)
        if not name or not platforms or not capabilities:
            unknown.append(name)
            continue
        metadata[name] = {"platforms": platforms, "capabilities": capabilities}
    return metadata, tuple(sorted(set(unknown)))


def sociavault_catalog_issues(tool_names: Iterable[str]) -> tuple[str, ...]:
    actual = {
        str(name or "").split("__", 1)[-1]
        for name in tool_names
        if str(name or "").strip()
    }
    expected = set(SOCIAVAULT_OFFICIAL_TOOL_NAMES)
    return tuple(
        [f"missing:{name}" for name in sorted(expected - actual)]
        + [f"unexpected:{name}" for name in sorted(actual - expected)]
    )


def candidate_tool_names(
    platforms: Iterable[str],
    capabilities: Iterable[str],
    available_tool_names: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    available = tuple(sorted({str(name or "").split("__", 1)[-1] for name in available_tool_names if name}))
    catalog_issues = sociavault_catalog_issues(available)
    if catalog_issues:
        return (), catalog_issues
    metadata, unknown = sociavault_tool_metadata(available)
    if unknown:
        return (), unknown
    platform_set = set(platforms)
    capability_set = set(capabilities)
    selected = {
        name
        for name, item in metadata.items()
        if item["platforms"] & platform_set
        and (not capability_set or item["capabilities"] & capability_set)
    }
    if capability_set.intersection({"comments", "transcript"}):
        selected.update(
            name for name in _DETAIL_DEPENDENCIES
            if name in metadata and metadata[name]["platforms"] & platform_set
        )
        for platform in platform_set:
            selected.update(
                name
                for name in _DISCOVERY_DEPENDENCIES.get(platform, ())
                if name in metadata
            )
    if "audience" in capability_set:
        selected.update(
            name for name, item in metadata.items()
            if item["platforms"] & platform_set and "profile" in item["capabilities"]
        )
    return tuple(sorted(selected)), ()


def _fallback_all(available_tool_names: Iterable[str], reason: str) -> SocialToolRoute:
    available = tuple(sorted({str(name or "").split("__", 1)[-1] for name in available_tool_names if name}))
    return SocialToolRoute(
        platforms=(),
        capabilities=(),
        source="fallback_all",
        confidence=0.0,
        fallback_reason=reason,
        candidate_tools=available,
    )


def rule_social_tool_route(
    text: str,
    available_tool_names: Iterable[str],
    inherited_platforms: Iterable[str] = (),
) -> SocialToolRoute | None:
    available = tuple(available_tool_names)
    platforms = detect_social_platforms(text)
    capabilities = detect_social_capabilities(text)
    if "account" in capabilities:
        platforms = ("account",)
        capabilities = ("account",)
    elif not platforms and capabilities:
        platforms = tuple(platform for platform in inherited_platforms if platform in SOCIAL_PLATFORMS)
    if not platforms:
        return None
    candidates, catalog_issues = candidate_tool_names(platforms, capabilities, available)
    if catalog_issues:
        return _fallback_all(available, "runtime_catalog_mismatch")
    if not candidates:
        return _fallback_all(available, "empty_rule_candidates")
    return SocialToolRoute(
        platforms=tuple(platforms),
        capabilities=tuple(capabilities),
        source="rules",
        confidence=1.0,
        fallback_reason="",
        candidate_tools=candidates,
    )


def model_social_tool_route(
    decision: Any,
    available_tool_names: Iterable[str],
    confidence_threshold: float = 0.8,
) -> SocialToolRoute:
    available = tuple(available_tool_names)
    if not isinstance(decision, dict):
        return _fallback_all(available, "invalid_model_output")
    raw_platforms = decision.get("platforms")
    raw_capabilities = decision.get("capabilities")
    if not isinstance(raw_platforms, list) or not isinstance(raw_capabilities, list):
        return _fallback_all(available, "invalid_model_schema")
    platforms = tuple(dict.fromkeys(str(item or "").strip().lower() for item in raw_platforms))
    capabilities = tuple(dict.fromkeys(str(item or "").strip().lower() for item in raw_capabilities))
    try:
        confidence = float(decision.get("confidence"))
    except (TypeError, ValueError):
        return _fallback_all(available, "invalid_model_confidence")
    if not platforms or any(platform not in SOCIAL_PLATFORMS - {"account"} for platform in platforms):
        return _fallback_all(available, "invalid_model_platforms")
    if any(capability not in SOCIAL_CAPABILITIES - {"account"} for capability in capabilities):
        return _fallback_all(available, "invalid_model_capabilities")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return _fallback_all(available, "invalid_model_confidence")
    if confidence < confidence_threshold:
        return _fallback_all(available, "low_model_confidence")
    candidates, catalog_issues = candidate_tool_names(platforms, capabilities, available)
    if catalog_issues:
        return _fallback_all(available, "runtime_catalog_mismatch")
    if not candidates:
        return _fallback_all(available, "empty_model_candidates")
    return SocialToolRoute(
        platforms=platforms,
        capabilities=capabilities,
        source="model",
        confidence=round(max(0.0, min(confidence, 1.0)), 4),
        fallback_reason="",
        candidate_tools=candidates,
    )


def fallback_social_tool_route(available_tool_names: Iterable[str], reason: str) -> SocialToolRoute:
    return _fallback_all(available_tool_names, reason)


def apply_social_route_mode(
    mode: str,
    full_tool_ids: Iterable[str],
    legacy_selected_tool_ids: Iterable[str],
    route: SocialToolRoute,
) -> set[str]:
    normalized_mode = normalize_router_mode(mode)
    full = set(full_tool_ids)
    if normalized_mode == "off":
        return set(legacy_selected_tool_ids)
    if normalized_mode == "shadow":
        return full
    candidates = {f"sociavault__{name}" for name in route.candidate_tools}
    return {
        tool_id for tool_id in full
        if not str(tool_id).startswith("sociavault__")
    } | (candidates & full)


def platforms_from_tool_names(tool_names: Iterable[str]) -> tuple[str, ...]:
    platforms: list[str] = []
    for raw_name in tool_names:
        name = str(raw_name or "").split("__", 1)[-1]
        for platform in _tool_platforms(name):
            if platform not in {"account", "ad_library"}:
                platforms.append(platform)
    return tuple(dict.fromkeys(platforms))
