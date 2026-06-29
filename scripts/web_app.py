#!/usr/bin/env python3
import json
import hmac
import http.client
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.parse import unquote
import cgi
from html import escape as html_escape

# SociaVault TikTok endpoints (mirrored from sociavault_tiktok.py)
TIKTOK_ENDPOINTS: dict[str, str] = {
    "profile": "/v1/scrape/tiktok/profile",
    "videos": "/v1/scrape/tiktok/videos",
    "videos-popular": "/v1/scrape/tiktok/videos/popular",
    "followers": "/v1/scrape/tiktok/followers",
    "following": "/v1/scrape/tiktok/following",
    "video-info": "/v1/scrape/tiktok/video-info",
    "comments": "/v1/scrape/tiktok/comments",
    "comment-replies": "/v1/scrape/tiktok/comment-replies",
    "transcript": "/v1/scrape/tiktok/transcript",
    "demographics": "/v1/scrape/tiktok/demographics",
    "live": "/v1/scrape/tiktok/live",
    "search-users": "/v1/scrape/tiktok/search/users",
    "search-hashtag": "/v1/scrape/tiktok/search/hashtag",
    "search-keyword": "/v1/scrape/tiktok/search/keyword",
    "search-music": "/v1/scrape/tiktok/search/music",
    "search-top": "/v1/scrape/tiktok/search/top",
    "trending": "/v1/scrape/tiktok/trending",
    "creators-popular": "/v1/scrape/tiktok/creators/popular",
    "hashtags-popular": "/v1/scrape/tiktok/hashtags/popular",
    "music-popular": "/v1/scrape/tiktok/music/popular",
    "music-info": "/v1/scrape/tiktok/music/info",
    "music-videos": "/v1/scrape/tiktok/music/videos",
}

ROOT = Path.cwd()
DATA_DIR = ROOT / "data"
VIDEOS_DIR = ROOT / "videos"
OUTPUT_DIR = ROOT / "output"
SCRIPTS_DIR = ROOT / "scripts"
INDEX_HTML_PATH = SCRIPTS_DIR / "web_index.html"
SELLERSPRITE_CHAT_DIR = ROOT / "sellersprite_mcp_chat"
SELLERSPRITE_CHAT_DATA_DIR = DATA_DIR / "sellersprite_mcp"
SELLERSPRITE_CHAT_PROCESS: subprocess.Popen | None = None
SELLERSPRITE_CHAT_LOCK = threading.Lock()

import sys
sys.path.insert(0, str(SCRIPTS_DIR))
from chat_session import ChatStore, Message, load_sessions_from_disk
from sociavault_usage import read_sociavault_usage
from sociavault_tiktok import call_api as call_sociavault_tiktok_api
from tools import execute_tool, get_tools_for_model, list_tools
from video_queue import video_queue, STATUS_META
from api_cache import get_cached_or_call, record_api_call
from api_cache import get_cached, store_response
from hot_video_report import (
    REPORT_COVER_DIR,
    backfill_cover_urls,
    delete_report,
    enqueue_report,
    get_report,
    get_report_progress,
    get_report_runtime_status,
    get_settings as get_report_settings,
    list_reports,
    recover_interrupted_reports,
    run_report,
    save_settings as save_report_settings,
    start_report_scheduler,
    translate_report_video_analysis,
)
from tiktok_download import video_cache_metadata, video_cache_request, with_download_cache_meta
from video_registry import (
    get_video_by_filename,
    is_hidden_from_analyzer,
    mark_extracted,
    platform_for_url,
    register_from_payload,
    register_video,
)
from proxy_state import ensure_us_proxy
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
AUDIO_ONLY_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
ANALYZER_VIDEO_SUFFIXES = {".m4v", ".mov", ".mp4", ".webm"}
ALLOWED_SHORT_VIDEO_HOST_SUFFIXES = ("tiktok.com", "tiktokv.com", "douyin.com", "iesdouyin.com")
ALLOWED_AMAZON_HOST_SUFFIXES = ("amazon.com",)
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
PROMPT_FILE = DATA_DIR / "analysis_prompt.txt"
FEEDBACK_PROMPT_FILE = DATA_DIR / "feedback_prompt.txt"
LEGACY_PROMPT_FILE = ROOT / "analysis_prompt.txt"
ANALYZER_MEDIA_FLAGS_FILE = DATA_DIR / "analyzer_media_flags.json"
MAX_PROMPT_CHARS = 50000
DEFAULT_ANALYSIS_PROMPT = (
    "Analyze this short video directly. Return strict JSON only, no Markdown. "
    "Use these exact keys: summary, timeline, visual_evidence. "
    "timeline must be an array of short chronological events with time_range, visual, audio fields. "
    "visual_evidence must be an array of concrete observations from the video frames. "
    "Be specific and do not invent unsupported facts."
)
DEFAULT_FEEDBACK_PROMPT = """请基于视频提取内容和分析结果，给出可执行的视频改进反馈。重点指出内容表达、开头吸引力、节奏、画面、字幕/口播、卖点呈现、信任感和转化引导的问题，并给出优先级明确的修改建议。

只返回严格可解析 JSON，不要 Markdown，不要代码块，不要额外解释。JSON 结构必须符合：
{
  "summary": "一句话总结视频当前最大改进方向",
  "overall_score": 0-100,
  "priority_actions": [
    {
      "priority": "high|medium|low",
      "problem": "具体问题",
      "why_it_matters": "为什么影响完播/互动/转化",
      "fix": "可直接执行的修改建议",
      "example": "可替换的文案、镜头或剪辑示例"
    }
  ],
  "opening_feedback": {
    "problem": "开头前3秒的问题",
    "fix": "开头改法",
    "example_hook": "建议使用的新开头钩子"
  },
  "content_feedback": {
    "strengths": ["已有优点"],
    "issues": ["内容表达问题"],
    "fixes": ["内容改进建议"]
  },
  "visual_feedback": {
    "issues": ["画面、构图、节奏、字幕问题"],
    "fixes": ["视觉改进建议"]
  },
  "audio_feedback": {
    "issues": ["口播、音乐、音效问题"],
    "fixes": ["音频改进建议"]
  },
  "conversion_feedback": {
    "issues": ["卖点、信任感、行动引导问题"],
    "fixes": ["转化改进建议"],
    "cta_examples": ["可直接使用的行动引导文案"]
  },
  "rewrite_brief": "给剪辑/拍摄人员的改版说明"
}"""
DEFAULT_SOCIA_VAULT_API_BASE = "https://api.sociavault.com"
VIDEO_INFO_TTL_SECONDS = 24 * 60 * 60
VIDEO_MEDIA_TTL_SECONDS = int(os.getenv("VIDEO_MEDIA_TTL_SECONDS", "900"))
SOCIAL_COMMENT_COUNT = int(os.getenv("SOCIAL_COMMENT_COUNT", "50"))
SOCIAL_API_TIMEOUT = float(os.getenv("SOCIAL_API_TIMEOUT", "45"))


def load_prompt() -> str:
    if PROMPT_FILE.is_file():
        content = PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    if LEGACY_PROMPT_FILE.is_file():
        content = LEGACY_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    return DEFAULT_ANALYSIS_PROMPT


def save_prompt(text: str) -> None:
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_FILE.write_text(text.strip() + "\n", encoding="utf-8")


def load_feedback_prompt() -> str:
    if FEEDBACK_PROMPT_FILE.is_file():
        content = FEEDBACK_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    return DEFAULT_FEEDBACK_PROMPT


def save_feedback_prompt(text: str) -> None:
    FEEDBACK_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK_PROMPT_FILE.write_text(text.strip() + "\n", encoding="utf-8")


@dataclass
class Job:
    id: str
    filename: str
    postprocess: bool
    analysis_mode: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None
    analysis_prompt: str = ""


@dataclass
class DownloadJob:
    id: str
    url: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    filename: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


@dataclass
class ShopJob:
    id: str
    url: str
    source_type: str
    region: str
    max_pages: int
    review_pages: int
    analyze: bool
    related_videos: bool
    prompt: str = ""
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


@dataclass
class MetricsJob:
    id: str
    target: str
    endpoint: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


@dataclass
class AmazonJob:
    id: str
    target: str
    target_type: str
    url: str
    pages: int
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
download_jobs: dict[str, DownloadJob] = {}
download_jobs_lock = threading.Lock()
shop_jobs: dict[str, ShopJob] = {}
shop_jobs_lock = threading.Lock()
metrics_jobs: dict[str, MetricsJob] = {}
metrics_jobs_lock = threading.Lock()
amazon_jobs: dict[str, AmazonJob] = {}
amazon_jobs_lock = threading.Lock()
social_jobs_lock = threading.Lock()
social_jobs_running: set[str] = set()

# Chat system
chat_store = ChatStore()
chat_tool_config: set[str] | None = None  # None = all tools enabled


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    if not name:
        raise ValueError("Missing filename")
    cleaned = "".join(ch for ch in name if ch in SAFE_CHARS)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Invalid filename")
    return cleaned


def validate_short_video_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https short-video URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_SHORT_VIDEO_HOST_SUFFIXES):
        raise ValueError("Only TikTok or Douyin URLs are supported")
    if len(cleaned) > 2048:
        raise ValueError("URL is too long")
    return cleaned


def output_dir_for_filename(filename: str) -> Path:
    registry_record = get_video_by_filename(filename)
    if registry_record:
        return OUTPUT_DIR / str(registry_record.get("extraction_dir") or filename)
    return OUTPUT_DIR / filename


def nested_get(data: Any, names: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return None
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    for value in data.values():
        if isinstance(value, dict):
            found = nested_get(value, names)
            if found not in (None, ""):
                return found
    return None


def nested_list(data: Any, names: tuple[str, ...]) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data and all(str(key).isdigit() for key in data):
        return [data[key] for key in sorted(data, key=lambda item: int(str(item)))]
    if not isinstance(data, dict):
        return []
    for name in names:
        value = data.get(name)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            found = nested_list(value, names)
            if found:
                return found
    for value in data.values():
        found = nested_list(value, names)
        if found:
            return found
    return []


def compact_video_info(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("video") or payload.get("data") or payload
    if isinstance(source, dict) and isinstance(source.get("aweme_detail"), dict):
        source = source["aweme_detail"]
    if not isinstance(source, dict):
        source = payload
    stats = source.get("statistics") or source.get("stats") or {}
    author = source.get("author") or source.get("author_info") or {}
    if not isinstance(stats, dict):
        stats = {}
    if not isinstance(author, dict):
        author = {}
    return {
        "id": source.get("id") or source.get("aweme_id") or source.get("video_id") or source.get("item_id"),
        "description": source.get("desc") or source.get("description") or source.get("title"),
        "url": source.get("url") or source.get("share_url") or source.get("webpage_url"),
        "created_at": source.get("create_time") or source.get("created_at") or source.get("createTime"),
        "duration": source.get("duration") or source.get("video_duration"),
        "author": {
            "id": author.get("id") or author.get("uid") or author.get("sec_uid"),
            "unique_id": author.get("unique_id") or author.get("uniqueId") or author.get("nickname"),
            "nickname": author.get("nickname") or author.get("name"),
        },
        "metrics": {
            "play_count": stats.get("play_count") or stats.get("playCount") or source.get("play_count"),
            "like_count": stats.get("digg_count") or stats.get("like_count") or stats.get("likeCount") or source.get("like_count"),
            "comment_count": stats.get("comment_count") or stats.get("commentCount") or source.get("comment_count"),
            "share_count": stats.get("share_count") or stats.get("shareCount") or source.get("share_count"),
            "collect_count": stats.get("collect_count") or stats.get("collectCount") or source.get("collect_count"),
        },
    }


def creator_handle_from_video_info(payload: dict[str, Any]) -> str:
    info = compact_video_info(payload)
    author = info.get("author") if isinstance(info.get("author"), dict) else {}
    for value in (author.get("unique_id"), author.get("nickname")):
        handle = str(value or "").strip().lstrip("@")
        if handle:
            return handle
    return ""


def compact_comments(payload: dict[str, Any]) -> dict[str, Any]:
    items = nested_list(payload, ("comments", "items", "data", "comment_list"))
    comments: list[dict[str, Any]] = []
    for item in items[:SOCIAL_COMMENT_COUNT]:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or item.get("author") or {}
        if not isinstance(user, dict):
            user = {}
        comments.append({
            "text": item.get("text") or item.get("comment") or item.get("content"),
            "like_count": item.get("digg_count") or item.get("like_count") or item.get("likes"),
            "reply_count": item.get("reply_comment_total") or item.get("reply_count"),
            "created_at": item.get("create_time") or item.get("created_at"),
            "user": user.get("unique_id") or user.get("nickname") or user.get("name"),
        })
    return {"count": len(items), "sample_count": len(comments), "items": comments}


def compact_profile(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("profile") or payload.get("user") or payload.get("data") or payload
    if not isinstance(source, dict):
        source = payload
    stats = source.get("stats") or source.get("statistics") or source.get("statsV2") or {}
    if not isinstance(stats, dict):
        stats = {}
    return {
        "id": source.get("id") or source.get("uid") or source.get("sec_uid"),
        "unique_id": source.get("unique_id") or source.get("uniqueId") or source.get("username"),
        "nickname": source.get("nickname") or source.get("name"),
        "signature": source.get("signature") or source.get("bio"),
        "verified": source.get("verified"),
        "region": source.get("region"),
        "metrics": {
            "follower_count": stats.get("follower_count") or stats.get("followerCount") or source.get("follower_count"),
            "following_count": stats.get("following_count") or stats.get("followingCount") or source.get("following_count"),
            "heart_count": stats.get("heart_count") or stats.get("heartCount") or source.get("heart_count"),
            "video_count": stats.get("video_count") or stats.get("videoCount") or source.get("video_count"),
            "digg_count": stats.get("digg_count") or stats.get("diggCount") or source.get("digg_count"),
        },
    }


def social_source_url(filename: str) -> str:
    record = get_video_by_filename(filename) or {}
    url = str(record.get("source_url") or "").strip()
    if not url:
        return ""
    try:
        return validate_short_video_url(url)
    except ValueError:
        return ""


def social_status_label(status: str) -> dict[str, str]:
    labels = {
        "complete": ("数据已获取", "#087443", "#ecfdf3"),
        "partial": ("部分缺失", "#a15c07", "#fff7ed"),
        "unavailable": ("无原始链接", "#64748b", "#f1f5f9"),
        "failed": ("获取失败", "#b42318", "#fff1f2"),
        "running": ("数据获取中", "#2563eb", "#eaf1ff"),
        "missing": ("未获取", "#94a3b8", "#f1f5f9"),
    }
    label, color, bg = labels.get(status, labels["missing"])
    return {"social_status": status, "social_label": label, "social_color": color, "social_bg": bg}


def summarize_social_status(context: Any) -> dict[str, str]:
    if not isinstance(context, dict):
        return social_status_label("missing")
    return social_status_label(str(context.get("status") or "missing"))


def write_social_running(filename: str, source_url: str) -> None:
    output_dir = output_dir_for_filename(filename)
    write_json(output_dir / "social_context.json", {
        "filename": filename,
        "source_url": source_url,
        "status": "running",
        "updated_at": time.time(),
        "items": {
            "video_info": {"status": "running"},
            "comments": {"status": "running"},
            "creator_profile": {"status": "running"},
        },
    })


def build_social_unavailable(filename: str, reason: str) -> dict[str, Any]:
    return {
        "filename": filename,
        "source_url": "",
        "status": "unavailable",
        "updated_at": time.time(),
        "items": {
            "video_info": {"status": "unavailable", "error": reason},
            "comments": {"status": "unavailable", "error": reason},
            "creator_profile": {"status": "unavailable", "error": reason},
        },
    }


def social_item(status: str, data: Any = None, error: str = "") -> dict[str, Any]:
    item = {"status": status}
    if data not in (None, ""):
        item["data"] = data
    if error:
        item["error"] = error
    return item


def fetch_social_context(filename: str, generate_insights: bool = True) -> dict[str, Any]:
    source_url = social_source_url(filename)
    output_dir = output_dir_for_filename(filename)
    if not source_url:
        context = build_social_unavailable(filename, "No TikTok/Douyin source URL is available for this video.")
        write_json(output_dir / "social_context.json", context)
        return context

    api_key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    if not api_key:
        context = build_social_unavailable(filename, "Missing SOCIAVAULT_API_KEY.")
        context["source_url"] = source_url
        context["status"] = "failed"
        write_json(output_dir / "social_context.json", context)
        return context

    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_SOCIA_VAULT_API_BASE)
    items: dict[str, dict[str, Any]] = {}
    video_info_payload: dict[str, Any] | None = None
    try:
        video_info_payload = call_sociavault_tiktok_api(
            api_key,
            api_base,
            "video-info",
            {"url": source_url},
            SOCIAL_API_TIMEOUT,
        )
        items["video_info"] = social_item("ok", compact_video_info(video_info_payload))
    except Exception as exc:
        items["video_info"] = social_item("failed", error=str(exc))

    try:
        comments_payload = call_sociavault_tiktok_api(
            api_key,
            api_base,
            "comments",
            {"url": source_url, "count": SOCIAL_COMMENT_COUNT},
            SOCIAL_API_TIMEOUT,
        )
        items["comments"] = social_item("ok", compact_comments(comments_payload))
    except Exception as exc:
        items["comments"] = social_item("failed", error=str(exc))

    record = get_video_by_filename(filename) or {}
    handle = ""
    if video_info_payload:
        handle = creator_handle_from_video_info(video_info_payload)
    if not handle:
        handle = str(record.get("author") or "").strip().lstrip("@")
    if handle:
        try:
            profile_payload = call_sociavault_tiktok_api(
                api_key,
                api_base,
                "profile",
                {"handle": handle},
                SOCIAL_API_TIMEOUT,
            )
            items["creator_profile"] = social_item("ok", compact_profile(profile_payload))
        except Exception as exc:
            items["creator_profile"] = social_item("failed", error=str(exc))
    else:
        items["creator_profile"] = social_item("unavailable", error="Creator handle was not available.")

    ok_count = sum(1 for item in items.values() if item.get("status") == "ok")
    status = "complete" if ok_count == 3 else ("partial" if ok_count else "failed")
    context = {
        "filename": filename,
        "source_url": source_url,
        "status": status,
        "updated_at": time.time(),
        "items": items,
    }
    write_json(output_dir / "social_context.json", context)
    if generate_insights and ok_count:
        try:
            generate_social_insights(filename)
        except Exception as exc:
            context["insights_error"] = str(exc)
            write_json(output_dir / "social_context.json", context)
    return context


def generate_social_insights(filename: str) -> dict[str, Any] | None:
    output_dir = output_dir_for_filename(filename)
    context_path = output_dir / "social_context.json"
    if not context_path.is_file():
        raise FileNotFoundError(f"social_context.json not found for {filename}")
    subprocess.run(
        [
            "python",
            str(SCRIPTS_DIR / "deepseek_social_insights.py"),
            str(output_dir),
            "--output",
            str(output_dir / "social_insights.json"),
        ],
        cwd=ROOT,
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    return read_json(output_dir / "social_insights.json")


def run_social_context_job(filename: str, generate_insights: bool = True) -> None:
    try:
        fetch_social_context(filename, generate_insights=generate_insights)
    except Exception as exc:
        output_dir = output_dir_for_filename(filename)
        context = read_json(output_dir / "social_context.json")
        if not isinstance(context, dict):
            context = {"filename": filename, "source_url": social_source_url(filename), "items": {}}
        context["status"] = "failed"
        context["updated_at"] = time.time()
        context["error"] = str(exc)
        write_json(output_dir / "social_context.json", context)
    finally:
        with social_jobs_lock:
            social_jobs_running.discard(filename)


def start_social_context_job(filename: str, generate_insights: bool = True) -> bool:
    with social_jobs_lock:
        if filename in social_jobs_running:
            return False
        social_jobs_running.add(filename)
    write_social_running(filename, social_source_url(filename))
    thread = threading.Thread(target=run_social_context_job, args=(filename, generate_insights), daemon=True)
    thread.start()
    return True


def social_tab_payload(filename: str, tab: str) -> dict[str, Any]:
    output_dir = output_dir_for_filename(filename)
    context = read_json(output_dir / "social_context.json") or {}
    insights = read_json(output_dir / "social_insights.json") or {}
    items = context.get("items") if isinstance(context, dict) else {}
    if not isinstance(items, dict):
        items = {}
    key_map = {"comments": "comments", "data": "video_info", "creator": "creator_profile"}
    insight_keys = {
        "comments": ("comment_insights", "comment_analysis"),
        "data": ("data_insights", "data_analysis"),
        "creator": ("creator_insights", "creator_analysis"),
    }
    selected = items.get(key_map.get(tab, ""), {}) if isinstance(items, dict) else {}
    if not isinstance(selected, dict):
        selected = {}
    insight = None
    if isinstance(insights, dict):
        for key in insight_keys.get(tab, ()):
            if insights.get(key):
                insight = insights.get(key)
                break
    titles = {"comments": "评论区分析", "data": "数据分析", "creator": "博主分析"}
    payload = {
        "summary": context.get("status") if isinstance(context, dict) else "missing",
        "status": selected.get("status") or context.get("status") if isinstance(context, dict) else "missing",
        "updated_at": context.get("updated_at") if isinstance(context, dict) else None,
        "source_url": context.get("source_url") if isinstance(context, dict) else "",
        titles.get(tab, "外部数据"): selected.get("data") or selected.get("error") or "无可用数据",
    }
    if insight:
        payload["DeepSeek 洞察"] = insight
    elif insights:
        payload["DeepSeek 洞察"] = insights
    return payload


def validate_amazon_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https Amazon URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_AMAZON_HOST_SUFFIXES):
        raise ValueError("Only amazon.com URLs are supported")
    if len(cleaned) > 2048:
        raise ValueError("URL is too long")
    return cleaned


def amazon_url_for_target(target: str, target_type: str) -> str:
    cleaned = target.strip()
    if not cleaned:
        raise ValueError("Amazon URL, ASIN, or keyword is required")
    if target_type == "url":
        return validate_amazon_url(cleaned)
    if target_type == "asin":
        asin = cleaned.upper()
        if not ASIN_RE.match(asin):
            raise ValueError("ASIN must be 10 letters or digits")
        return f"https://www.amazon.com/dp/{asin}"
    if target_type == "keyword":
        if len(cleaned) > 200:
            raise ValueError("Keyword is too long")
        return f"https://www.amazon.com/s?k={quote_plus(cleaned)}"
    raise ValueError("target_type must be url, asin, or keyword")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _report_bot_authorized(handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> bool:
    expected = os.getenv("REPORT_BOT_TOKEN", "").strip()
    if not expected:
        return True
    auth = handler.headers.get("Authorization", "").strip()
    supplied = ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = query.get("token", [""])[0].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _report_detail_url(handler: BaseHTTPRequestHandler, report_date: str) -> str:
    host = handler.headers.get("Host", "").strip()
    path = f"/report?date={report_date}"
    if not host:
        return path
    proto = "https" if handler.headers.get("X-Forwarded-Proto", "").lower() == "https" else "http"
    return f"{proto}://{host}{path}"


def _coerce_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _compact_report_text(value: Any, max_len: int = 600) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, list):
        text = "；".join(_compact_report_text(item, max_len=max_len) for item in value if item)
    elif isinstance(value, dict):
        parts = []
        for key, item in value.items():
            item_text = _compact_report_text(item, max_len=max_len)
            if item_text:
                parts.append(f"{key}: {item_text}")
        text = "；".join(parts)
    else:
        text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len].rstrip() + ("..." if len(text) > max_len else "")


def _metric_from_video(video: dict[str, Any], key: str) -> int:
    return _coerce_int((video.get("metrics") or {}).get(key))


def _format_report_count(value: int) -> str:
    value = _coerce_int(value)
    if value >= 10000:
        rounded = round(value / 10000, 1)
        return f"{rounded:g}万"
    return str(value)


def _build_feishu_report_payload(
    handler: BaseHTTPRequestHandler,
    report_date: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    report = get_report(report_date, include_raw=False, detail=True)
    videos = list(report.get("videos") or [])[: max(1, min(limit, 20))]
    report_body = report.get("report") or {}
    markdown = str(report.get("report_markdown") or "").strip()
    summary = (
        _compact_report_text(report_body.get("summary") if isinstance(report_body, dict) else "")
        or _compact_report_text(report_body.get("overall_conclusion") if isinstance(report_body, dict) else "")
        or _compact_report_text(markdown, max_len=800)
    )
    date = str(report.get("report_date") or report_date or "")
    title = f"{date} 爆款视频日报" if date else "爆款视频日报"
    compact_videos = []
    for index, video in enumerate(videos, start=1):
        compact_videos.append(
            {
                "rank": _coerce_int(video.get("report_rank")) or index,
                "platform": video.get("platform") or "",
                "video_id": video.get("video_id") or "",
                "title": video.get("title") or "无标题",
                "author": video.get("author") or "",
                "source_label": video.get("source_label") or "",
                "source_endpoint": video.get("source_endpoint") or "",
                "source_url": video.get("source_url") or "",
                "cover_url": video.get("cover_url") or "",
                "hot_score": _coerce_int(video.get("hot_score")),
                "play_count": _metric_from_video(video, "play_count"),
                "like_count": _metric_from_video(video, "like_count"),
                "comment_count": _metric_from_video(video, "comment_count"),
                "share_count": _metric_from_video(video, "share_count"),
                "favorite_count": _metric_from_video(video, "favorite_count"),
                "published_at": (video.get("metrics") or {}).get("published_at"),
                "insight": video.get("insight") or {},
            }
        )
    lines = [
        f"**{title}**",
        f"状态：{report.get('status', 'missing')}｜视频：{report.get('video_count', len(compact_videos))}｜成功：{report.get('analysis_success_count', 0)}｜失败：{report.get('analysis_failed_count', 0)}",
    ]
    if summary:
        lines.extend(["", f"总体结论：{summary}"])
    if compact_videos:
        lines.extend(["", "Top 视频："])
        for item in compact_videos[:10]:
            title_text = _compact_report_text(item["title"], max_len=80) or "无标题"
            lines.append(
                f"{item['rank']}. {title_text}｜播放 {_format_report_count(item['play_count'])}｜热度 {_format_report_count(item['hot_score'])}"
            )
    detail_url = _report_detail_url(handler, date) if date else "/report"
    lines.extend(["", f"详情：{detail_url}"])
    feishu_text = markdown or "\n".join(lines)
    return {
        "ok": bool(report.get("exists")),
        "exists": bool(report.get("exists")),
        "report_date": date,
        "status": report.get("status", "missing"),
        "title": title,
        "summary": summary,
        "url": detail_url,
        "generated_at": report.get("llm_generated_at") or report.get("updated_at") or "",
        "video_count": report.get("video_count", len(compact_videos)),
        "analysis_success_count": report.get("analysis_success_count", 0),
        "analysis_failed_count": report.get("analysis_failed_count", 0),
        "error": report.get("error") or "",
        "report": report_body,
        "report_markdown": markdown,
        "videos": compact_videos,
        "feishu_text": feishu_text,
    }


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.end_headers()
    handler.wfile.write(encoded)


def binary_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
    filename: str | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        quoted = filename.replace('"', "")
        handler.send_header("Content-Disposition", f'attachment; filename="{quoted}"')
    handler.end_headers()
    handler.wfile.write(body)


def write_sse_event(handler: BaseHTTPRequestHandler, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.wfile.write(b"data: ")
    handler.wfile.write(body)
    handler.wfile.write(b"\n\n")
    handler.wfile.flush()


def read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def _media_flag_key(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return path.name


def _read_media_flags() -> dict[str, Any]:
    data = read_json(ANALYZER_MEDIA_FLAGS_FILE)
    return data if isinstance(data, dict) else {}


def _write_media_flags(flags: dict[str, Any]) -> None:
    write_json(ANALYZER_MEDIA_FLAGS_FILE, flags)


def _probe_analyzer_video(path: Path) -> tuple[bool, str]:
    if path.suffix.lower() not in ANALYZER_VIDEO_SUFFIXES:
        return False, f"unsupported suffix {path.suffix.lower()}"
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False, "ffprobe unavailable"
    result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=ROOT,
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or f"ffprobe exit {result.returncode}")[:300]
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return False, f"ffprobe json error: {exc}"
    streams = payload.get("streams") if isinstance(payload, dict) else []
    has_video = any(isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams)
    return (True, "") if has_video else (False, "no video stream")


def analyzer_media_is_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in ANALYZER_VIDEO_SUFFIXES:
        return False
    stat = path.stat()
    key = _media_flag_key(path)
    flags = _read_media_flags()
    cached = flags.get(key)
    if (
        isinstance(cached, dict)
        and cached.get("size") == stat.st_size
        and abs(float(cached.get("mtime") or 0) - stat.st_mtime) < 0.001
    ):
        return bool(cached.get("valid"))
    valid, reason = _probe_analyzer_video(path)
    flags[key] = {
        "valid": valid,
        "reason": reason,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "checked_at": time.time(),
    }
    _write_media_flags(flags)
    return valid


def ensure_analyzer_media_or_delete(path: Path) -> None:
    if analyzer_media_is_valid(path):
        return
    flags = _read_media_flags()
    reason = ""
    cached = flags.get(_media_flag_key(path))
    if isinstance(cached, dict):
        reason = str(cached.get("reason") or "")
    path.unlink(missing_ok=True)
    raise RuntimeError(f"invalid analyzer video: {reason or path.name}")


def clean_report_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("```"):
            stripped = stripped.removeprefix("```json").removeprefix("```").strip()
            stripped = stripped.removesuffix("```").strip()
        return stripped
    return json.dumps(value, ensure_ascii=False, indent=2)


def report_section(title: str, value: Any) -> str:
    text = clean_report_value(value)
    if not text:
        return ""
    return (
        '<section class="report-section">'
        f"<h3>{html_escape(title)}</h3>"
        f'<div class="content">{html_escape(text)}</div>'
        "</section>"
    )


def report_list(title: str, values: Any) -> str:
    if not isinstance(values, list) or not values:
        return ""
    lines: list[str] = []
    for item in values:
        if isinstance(item, dict):
            parts = []
            if item.get("time_range") or item.get("timestamp"):
                parts.append(str(item.get("time_range") or item.get("timestamp")))
            if item.get("visual") or item.get("description"):
                parts.append(f"画面：{item.get('visual') or item.get('description')}")
            if item.get("audio"):
                parts.append(f"音频：{item.get('audio')}")
            lines.append("\n".join(parts) or clean_report_value(item))
        else:
            lines.append(clean_report_value(item))
    return report_section(title, "\n\n".join(f"- {line}" for line in lines if line))


def metric_item(label: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    return (
        '<div class="metric">'
        f"<span>{html_escape(label)}</span>"
        f"<b>{html_escape(str(value))}</b>"
        "</div>"
    )


def build_report_html(filename: str, tab: str, payload: dict[str, Any]) -> str:
    is_audit = tab in {"audit", "feedback", "comments", "data", "creator"}
    title_map = {
        "direct": "直接提取内容报告",
        "feedback": "反馈结果报告",
        "audit": "分析结果报告",
        "comments": "评论区分析报告",
        "data": "数据分析报告",
        "creator": "博主分析报告",
    }
    eyebrow_map = {
        "direct": "Direct LLM Extraction",
        "feedback": "DeepSeek 反馈",
        "audit": "DeepSeek 分析",
        "comments": "SociaVault Comments",
        "data": "SociaVault Video Data",
        "creator": "SociaVault Creator Profile",
    }
    title = title_map.get(tab, "提取内容报告")
    eyebrow = eyebrow_map.get(tab, "Qwen Video Extraction")
    summary = clean_report_value(payload.get("summary")) or "暂无摘要。"

    if is_audit:
        # Generic render: any JSON key → section/metric/list
        metrics_parts = []
        sections_parts = []
        for key, val in payload.items():
            if key == "raw_result":
                continue
            if val is None or val == "":
                continue
            if isinstance(val, str):
                sections_parts.append(report_section(key, val))
            elif isinstance(val, list):
                sections_parts.append(report_list(key, val))
            elif isinstance(val, (int, float)):
                metrics_parts.append(metric_item(key, val))
            elif isinstance(val, dict):
                sections_parts.append(report_section(key, json.dumps(val, ensure_ascii=False, indent=2)))
        metrics = "".join(metrics_parts)
        sections = "".join(sections_parts)
    else:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        transcript = payload.get("transcript") if isinstance(payload.get("transcript"), dict) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        metrics = "".join(
            [
                metric_item("处理模式", payload.get("processing_mode")),
                metric_item("视觉模型", payload.get("vision_model") or metadata.get("model")),
                metric_item("音频模式", payload.get("audio_mode")),
                metric_item("处理帧数", metadata.get("frames_processed") or metadata.get("frames_extracted")),
                metric_item("音频语言", transcript.get("language") or metadata.get("audio_language")),
                metric_item("输入 Tokens", usage.get("input_tokens")),
                metric_item("输出 Tokens", usage.get("output_tokens")),
                metric_item("总 Tokens", usage.get("total_tokens")),
                metric_item("API 调用", usage.get("api_calls")),
                metric_item("总耗时", f"{usage.get('elapsed_seconds')}s" if usage.get("elapsed_seconds") is not None else None),
            ]
        )
        sections = "".join(
            [
                report_section("模型总结", payload.get("summary")),
                report_section(
                    "视频画面总述",
                    payload.get("video_description")
                    or payload.get("opening_description")
                    or payload.get("narrative_development"),
                ),
                report_list("时间线", payload.get("timeline")),
                report_list("视觉证据", payload.get("visual_evidence")),
                report_section("转写文本", transcript.get("text") or "无转写文本"),
            ]
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
body{{margin:0;background:#f6f8fb;color:#111827;font-family:"Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif}}
.page{{padding:34px}}.doc-head{{margin-bottom:18px;padding-bottom:14px;border-bottom:2px solid #1d4ed8}}
.doc-head h1{{margin:0;font-size:26px}}.doc-head p{{margin:8px 0 0;color:#64748b}}
.report{{display:flex;flex-direction:column;gap:14px}}.hero{{border:1px solid #d6deea;border-radius:12px;padding:18px;background:#fff}}
.eyebrow{{color:#64748b;font-size:12px;font-weight:800;text-transform:uppercase}}.hero h2{{margin:8px 0;font-size:24px}}
.hero p{{margin:0;line-height:1.75}}.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.metric,.report-section{{border:1px solid #d6deea;border-radius:10px;background:#fff}}.metric{{padding:11px}}
.metric span{{display:block;color:#64748b;font-size:12px;font-weight:700}}.metric b{{display:block;margin-top:5px}}
.report-section{{overflow:hidden;break-inside:avoid}}.report-section h3{{margin:0;padding:11px 13px;border-bottom:1px solid #d6deea;background:#f8fafc;font-size:15px}}
.report-section .content{{padding:12px 13px;line-height:1.8;white-space:pre-wrap}}
</style>
</head>
<body>
<main class="page">
<div class="doc-head"><h1>{html_escape(title)} - {html_escape(filename)}</h1><p>导出时间：{time.strftime("%Y-%m-%d %H:%M:%S")}</p></div>
<article class="report">
<div class="hero"><div class="eyebrow">{html_escape(eyebrow)}</div><h2>{html_escape(title)}</h2><p>{html_escape(summary)}</p></div>
<div class="metrics">{metrics}</div>
{sections}
</article>
</main>
</body>
</html>"""


def chat_split_table_row(line: str) -> list[str]:
    text = str(line or "").strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def chat_is_table_separator(line: str) -> bool:
    cells = chat_split_table_row(line)
    return len(cells) > 1 and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def chat_inline_markdown(text: str) -> str:
    escaped = html_escape(str(text or ""))
    tokens: list[str] = []

    def keep(html: str) -> str:
        token = f"\x00{len(tokens)}\x00"
        tokens.append(html)
        return token

    escaped = re.sub(
        r"`([^`]+)`",
        lambda match: keep(f"<code>{match.group(1)}</code>"),
        escaped,
    )
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda match: keep(
            f'<a href="{html_escape(match.group(2), quote=True)}">{match.group(1)}</a>'
        ),
        escaped,
    )

    def auto_link(match: re.Match[str]) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in "),.;:!?，。；：！？）":
            tail = url[-1] + tail
            url = url[:-1]
        safe = html_escape(url, quote=True)
        return f'<a href="{safe}">{url}</a>{tail}'

    escaped = re.sub(r"https?://[^\s<]+", auto_link, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(^|[^\*])\*([^\*]+)\*", r"\1<em>\2</em>", escaped)
    return re.sub(r"\x00(\d+)\x00", lambda match: tokens[int(match.group(1))], escaped)


def chat_render_table(lines: list[str], start: int) -> tuple[str, int]:
    headers = chat_split_table_row(lines[start])
    index = start + 2
    rows: list[list[str]] = []
    while index < len(lines) and "|" in lines[index] and lines[index].strip():
        rows.append(chat_split_table_row(lines[index]))
        index += 1
    head_html = "".join(f"<th>{chat_inline_markdown(header)}</th>" for header in headers)
    body_parts = []
    for row in rows:
        body_parts.append(
            "<tr>"
            + "".join(
                f"<td>{chat_inline_markdown(row[col] if col < len(row) else '')}</td>"
                for col in range(len(headers))
            )
            + "</tr>"
        )
    html = (
        '<div class="md-table-wrap"><table class="md-table"><thead><tr>'
        + head_html
        + "</tr></thead><tbody>"
        + "".join(body_parts)
        + "</tbody></table></div>"
    )
    return html, index


def chat_markdown_to_html(markdown: str) -> str:
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    index = 0
    in_code = False
    code_lang = ""
    code_lines: list[str] = []

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        fence = re.match(r"^```([\w+-]*)\s*$", stripped)
        if fence:
            if in_code:
                lang_class = f' class="language-{html_escape(code_lang, quote=True)}"' if code_lang else ""
                out.append(f"<pre><code{lang_class}>{html_escape(chr(10).join(code_lines))}</code></pre>")
                in_code = False
                code_lang = ""
                code_lines = []
            else:
                in_code = True
                code_lang = fence.group(1)
                code_lines = []
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue
        if not stripped:
            index += 1
            continue
        if index + 1 < len(lines) and "|" in line and chat_is_table_separator(lines[index + 1]):
            table_html, index = chat_render_table(lines, index)
            out.append(table_html)
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{chat_inline_markdown(heading.group(2))}</h{level}>")
            index += 1
            continue
        if re.fullmatch(r"---+", stripped):
            out.append("<hr>")
            index += 1
            continue
        if re.match(r"^[-*]\s+", stripped):
            items: list[str] = []
            while index < len(lines) and re.match(r"^[-*]\s+", lines[index].strip()):
                item = re.sub(r"^[-*]\s+", "", lines[index].strip())
                items.append(f"<li>{chat_inline_markdown(item)}</li>")
                index += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"^\d+[.)]\s+", stripped):
            items = []
            while index < len(lines) and re.match(r"^\d+[.)]\s+", lines[index].strip()):
                item = re.sub(r"^\d+[.)]\s+", "", lines[index].strip())
                items.append(f"<li>{chat_inline_markdown(item)}</li>")
                index += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue

        paragraph = [stripped]
        index += 1
        while index < len(lines):
            next_line = lines[index]
            next_trim = next_line.strip()
            if (
                not next_trim
                or re.match(r"^(#{1,3})\s+", next_trim)
                or re.match(r"^[-*]\s+", next_trim)
                or re.match(r"^\d+[.)]\s+", next_trim)
                or re.match(r"^```", next_trim)
                or (index + 1 < len(lines) and "|" in next_line and chat_is_table_separator(lines[index + 1]))
            ):
                break
            paragraph.append(next_trim)
            index += 1
        out.append("<p>" + "<br>".join(chat_inline_markdown(part) for part in paragraph) + "</p>")

    if in_code:
        lang_class = f' class="language-{html_escape(code_lang, quote=True)}"' if code_lang else ""
        out.append(f"<pre><code{lang_class}>{html_escape(chr(10).join(code_lines))}</code></pre>")
    return "".join(out)


def build_chat_reply_pdf_html(message: Message) -> str:
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(message.created_at or time.time())))
    exported = time.strftime("%Y-%m-%d %H:%M:%S")
    content_html = chat_markdown_to_html(message.content)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
body{{margin:0;background:#f6f8fb;color:#111827;font-family:"Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif}}
.page{{padding:34px}}.doc-head{{margin-bottom:18px;padding-bottom:14px;border-bottom:2px solid #2563eb}}
.doc-head h1{{margin:0;font-size:26px}}.doc-head p{{margin:8px 0 0;color:#64748b;font-size:12px}}
.reply{{border:1px solid #d6deea;border-radius:12px;background:#fff;padding:20px 22px;line-height:1.75;font-size:14px}}
.reply h1,.reply h2,.reply h3{{margin:4px 0 12px;line-height:1.35}}.reply h1{{font-size:24px}}.reply h2{{font-size:20px}}.reply h3{{font-size:17px}}
.reply p{{margin:10px 0}}.reply ul,.reply ol{{margin:10px 0;padding-left:24px}}.reply li{{margin:4px 0}}
.reply a{{color:#2563eb;text-decoration:none;font-weight:600;word-break:break-all}}
.reply code{{background:#f1f5f9;border:1px solid #e2e8f0;border-radius:5px;padding:1px 5px;font-size:12px}}
.reply pre{{margin:12px 0;padding:12px 14px;border-radius:8px;background:#0f172a;color:#e2e8f0;white-space:pre-wrap;word-break:break-word}}
.reply pre code{{background:transparent;border:0;color:inherit;padding:0}}
.reply hr{{border:0;border-top:1px solid #d6deea;margin:16px 0}}
.md-table-wrap{{overflow:hidden;margin:12px 0;border:1px solid #d6deea;border-radius:8px}}
.md-table{{width:100%;border-collapse:collapse;font-size:13px}}.md-table th,.md-table td{{border-bottom:1px solid #d6deea;border-right:1px solid #d6deea;padding:8px 10px;text-align:left;vertical-align:top}}
.md-table th:last-child,.md-table td:last-child{{border-right:0}}.md-table tr:last-child td{{border-bottom:0}}.md-table th{{background:#f8fafc;font-weight:800}}
</style>
</head>
<body>
<main class="page">
<div class="doc-head"><h1>AI 回复导出</h1><p>消息时间：{html_escape(created)} · 导出时间：{html_escape(exported)}</p></div>
<article class="reply">{content_html}</article>
</main>
</body>
</html>"""


def render_pdf_bytes(html: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1240, "height": 1754})
            page.set_content(html, wait_until="load")
            return page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "14mm", "right": "14mm", "bottom": "14mm", "left": "14mm"},
            )
        finally:
            browser.close()


def mode_from_analysis(analysis: Any) -> str | None:
    if isinstance(analysis, dict):
        return analysis.get("processing_mode")
    return None


def append_log(job: Job, line: str) -> None:
    with jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_command(job: Job, command: list[str], env_extra: dict[str, str] | None = None) -> None:
    append_log(job, f"$ {' '.join(command)}")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        append_log(job, line)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def append_download_log(job: DownloadJob, line: str) -> None:
    with download_jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_download_command(job: DownloadJob, command: list[str]) -> None:
    append_download_log(job, f"$ {' '.join(command)}")
    timeout = int(os.getenv("DOWNLOAD_COMMAND_TIMEOUT", "210"))
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            for line in str(exc.stdout).splitlines():
                append_download_log(job, line)
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc
    for line in (result.stdout or "").splitlines():
        append_download_log(job, line)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(command)}")


def cache_log_label(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    cache = payload.get("_cache")
    if isinstance(cache, dict) and "hit" in cache:
        label = "缓存命中" if cache.get("hit") else "实时调用"
        provider = cache.get("provider") or ""
        endpoint = cache.get("endpoint") or ""
        suffix = f" ({provider}/{endpoint})" if provider or endpoint else ""
        return label + suffix
    return None


def _score_media_candidate(path: str, url: str) -> int:
    lowered = f"{path} {url}".lower()
    score = 0
    if ".video.play_addr.url_list" in lowered and ".bit_rate." not in lowered:
        score += 260
    if "download_no_watermark_addr.url_list" in lowered:
        score += 240
    if "download_addr.url_list" in lowered:
        score += 220
    if ".bit_rate." in lowered:
        score -= 120
    for word, points in (
        ("h264", 90),
        ("download", 100),
        ("no_watermark", 80),
        ("nowatermark", 80),
        ("play_addr", 70),
        ("playaddr", 70),
        ("video_url", 60),
        ("video", 40),
        (".mp4", 30),
        ("bytevc2", -160),
        ("bytevc1", -80),
        ("watermark", -40),
    ):
        if word in lowered:
            score += points
    return score


def _iter_media_url_candidates(value: Any, path: str = "") -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            candidates.extend(_iter_media_url_candidates(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            candidates.extend(_iter_media_url_candidates(child, f"{path}[{index}]"))
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        lowered = f"{path} {value}".lower()
        if any(word in lowered for word in ("cover", "avatar", "thumbnail", "image", "music", "audio", "subtitle")):
            return []
        if not any(word in lowered for word in ("video", "download", "play", ".mp4", "mime_type=video", "mime=video")):
            return []
        candidates.append((_score_media_candidate(path, value), path, value))
    return candidates


def _sociavault_video_id(payload: Any, fallback_url: str) -> str:
    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("id", "video_id", "aweme_id", "item_id"):
                raw = value.get(key)
                if raw not in (None, ""):
                    return str(raw)
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None

    found = walk(payload)
    if found:
        return re.sub(r"[^A-Za-z0-9_-]+", "_", found).strip("_")[:80] or "unknown"
    match = re.search(r"/video/(\d+)", fallback_url)
    if match:
        return match.group(1)
    return uuid.uuid4().hex[:12]


def _download_direct_media(job: DownloadJob, media_url: str, source_url: str, payload: Any) -> dict[str, Any]:
    import requests

    ensure_us_proxy("tiktok", log=lambda line: append_download_log(job, line))
    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("SociaVault media URL is not http/https")
    max_bytes = int(os.getenv("TIKTOK_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
    video_id = _sociavault_video_id(payload, source_url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".webm"}:
        suffix = ".mp4"
    target = VIDEOS_DIR / safe_filename(f"shortvideo_SociaVault_{video_id}{suffix}")
    temp_target = target.with_suffix(target.suffix + ".part")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Referer": source_url,
    }
    proxy = os.getenv("TIKTOK_PROXY_URL", "").strip()
    attempts: list[tuple[str, dict[str, str] | None]] = []
    if proxy:
        attempts.append((f"proxy={proxy}", {"http": proxy, "https": proxy}))
    attempts.append(("direct", None))

    append_download_log(job, f"SociaVault 媒体直链下载：{media_url[:180]}")
    try:
        errors = []
        for attempt_label, proxies in attempts:
            temp_target.unlink(missing_ok=True)
            try:
                append_download_log(job, f"SociaVault media direct attempt: {attempt_label}")
                with requests.get(media_url, headers=headers, proxies=proxies, stream=True, timeout=(8, 60)) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("Content-Length") or 0)
                    if content_length > max_bytes:
                        raise RuntimeError(f"SociaVault media is too large: {content_length} bytes")
                    downloaded = 0
                    with temp_target.open("wb") as file:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise RuntimeError(f"SociaVault media exceeded max size: {downloaded} bytes")
                            file.write(chunk)
                break
            except Exception as exc:
                errors.append(f"{attempt_label}: {exc}")
        else:
            raise RuntimeError(" / ".join(errors))
        if temp_target.stat().st_size < 500 * 1024:
            size = temp_target.stat().st_size
            raise RuntimeError(f"SociaVault media file is too small: {size} bytes")
        temp_target.replace(target)
        ensure_analyzer_media_or_delete(target)
    except Exception:
        temp_target.unlink(missing_ok=True)
        raise
    return {
        "filename": target.name,
        "path": str(target),
        "size": target.stat().st_size,
        "id": video_id,
        "title": None,
        "uploader": None,
        "duration": None,
        "webpage_url": source_url,
        "downloader": "sociavault-video-info",
        "media_url": media_url,
    }


def _sociavault_video_info_request(url: str) -> dict[str, Any]:
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_SOCIA_VAULT_API_BASE).rstrip("/")
    return {"api_base": api_base, "endpoint": "video-info", "params": {"url": url}}


def try_cached_download_result(job: DownloadJob, result_path: Path) -> bool:
    cached = get_cached("short_video_download", "download", video_cache_request(job.url))
    if not isinstance(cached, dict) or not cached.get("filename"):
        return False
    filename = safe_filename(str(cached["filename"]))
    cached_path = VIDEOS_DIR / filename
    if not cached_path.is_file():
        append_download_log(job, f"下载结果缓存文件不存在，继续重新下载：{filename}")
        return False
    if cached_path.suffix.lower() in AUDIO_ONLY_SUFFIXES:
        cached_path.unlink(missing_ok=True)
        append_download_log(job, f"删除缓存命中的无效音频文件，重新下载：{filename}")
        return False
    if not analyzer_media_is_valid(cached_path):
        cached_path.unlink(missing_ok=True)
        append_download_log(job, f"删除缓存命中的无效视频文件，重新下载：{filename}")
        return False
    result = with_download_cache_meta(dict(cached), True)
    result["path"] = str(cached_path)
    write_json(result_path, result)
    append_download_log(job, "下载结果缓存命中，复用本地视频文件。")
    return True


def store_download_result(job: DownloadJob, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("id"):
        register_video(
            video_id=str(result.get("id")),
            platform=platform_for_url(job.url),
            source_url=str(result.get("webpage_url") or job.url),
            filename=str(result.get("filename") or ""),
            title=str(result.get("title") or ""),
            author=str(result.get("uploader") or ""),
        )
    store_response(
        "short_video_download",
        "download",
        video_cache_request(job.url),
        result,
        metadata=video_cache_metadata(result, job.url),
    )
    return with_download_cache_meta(result, False)


def _media_cache_payload(url: str, payload: Any) -> dict[str, Any]:
    candidates = sorted(_iter_media_url_candidates(payload), key=lambda item: item[0], reverse=True)
    return {
        "source_url": url,
        "video_id": _sociavault_video_id(payload, url),
        "candidates": [
            {"score": score, "path": path, "url": media_url}
            for score, path, media_url in candidates[:12]
        ],
    }


def _try_media_cache_payload_download(job: DownloadJob, payload: Any, result_path: Path, source_label: str) -> bool:
    if source_label.startswith("缓存") and media_cache_is_stale(payload):
        append_download_log(job, "媒体地址缓存已过期，刷新 SociaVault video-info。")
        return False
    raw_candidates = payload.get("candidates") if isinstance(payload, dict) else []
    candidates = [item for item in raw_candidates if isinstance(item, dict) and item.get("url")]
    for item in candidates:
        item["score"] = _score_media_candidate(str(item.get("path") or ""), str(item.get("url") or ""))
    candidates.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    append_download_log(job, f"{source_label} 媒体地址缓存返回 {len(candidates)} 个候选地址。")
    for item in candidates[:12]:
        path = str(item.get("path") or "")
        media_url = str(item.get("url") or "")
        score = item.get("score")
        try:
            append_download_log(job, f"{source_label} 尝试候选地址 score={score} path={path}")
            result = _download_direct_media(job, media_url, job.url, payload)
            result["video_info_source"] = source_label
            if isinstance(payload, dict) and isinstance(payload.get("_cache"), dict):
                result["media_cache"] = payload["_cache"]
            result = store_download_result(job, result)
            write_json(result_path, result)
            return True
        except Exception as exc:
            append_download_log(job, f"{source_label} 候选地址不可用：{exc}")
    return False


def media_cache_is_stale(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    cache_meta = payload.get("_cache")
    if not isinstance(cache_meta, dict):
        return False
    age = cache_meta.get("age_seconds")
    if age is None:
        return False
    return float(age) > VIDEO_MEDIA_TTL_SECONDS


def _try_video_info_payload_download(job: DownloadJob, payload: Any, result_path: Path, source_label: str) -> bool:
    register_from_payload(payload, source_url=job.url)
    media_payload = _media_cache_payload(job.url, payload)
    if media_payload["candidates"]:
        store_response(
            "sociavault_tiktok_media",
            "video-info-media",
            _sociavault_video_info_request(job.url),
            media_payload,
            ttl_seconds=VIDEO_MEDIA_TTL_SECONDS,
            metadata={"entity_type": "tiktok_video_media", "entity_id": media_payload.get("video_id"), "source_url": job.url},
        )
    return _try_media_cache_payload_download(job, media_payload, result_path, source_label)


def try_cached_video_info_download(job: DownloadJob, result_path: Path) -> bool:
    payload = get_cached(
        "sociavault_tiktok_media",
        "video-info-media",
        _sociavault_video_info_request(job.url),
        ttl_seconds=VIDEO_MEDIA_TTL_SECONDS,
    )
    if not isinstance(payload, dict):
        append_download_log(job, "媒体地址缓存未命中。")
        return False
    return _try_media_cache_payload_download(job, payload, result_path, "缓存")


def try_sociavault_video_info_download(job: DownloadJob, result_path: Path) -> bool:
    if not os.getenv("SOCIAVAULT_API_KEY", "").strip():
        append_download_log(job, "未配置 SOCIAVAULT_API_KEY，跳过 SociaVault video-info。")
        return False
    output_path = result_path.with_suffix(".sociavault-video-info.json")
    try:
        run_download_command(
            job,
            [
                "python",
                str(SCRIPTS_DIR / "sociavault_tiktok.py"),
                "--endpoint",
                "video-info",
                "--url",
                job.url,
                "--output",
                str(output_path),
            ],
        )
        payload = read_json(output_path)
        if _try_video_info_payload_download(job, payload, result_path, "SociaVault API"):
            result = read_json(result_path)
            if isinstance(result, dict):
                result["sociavault_video_info"] = str(output_path.relative_to(ROOT))
                write_json(result_path, result)
            return True
        return False
    except Exception as exc:
        append_download_log(job, f"SociaVault video-info 下载链路失败，回退原下载器：{exc}")
        return False


def append_shop_log(job: ShopJob, line: str) -> None:
    with shop_jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_shop_command(job: ShopJob, command: list[str]) -> None:
    append_shop_log(job, f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        append_shop_log(job, line)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def run_shop_job(job_id: str) -> None:
    with shop_jobs_lock:
        job = shop_jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    output_dir = OUTPUT_DIR / "tiktok_shop" / job_id
    extract_path = output_dir / "shop_extract.json"
    analysis_path = output_dir / "shop_analysis.json"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with shop_jobs_lock:
            job.output_dir = str(output_dir.relative_to(ROOT))
            job.updated_at = time.time()

        command = [
            "python",
            str(SCRIPTS_DIR / "sociavault_tiktok_shop.py"),
            job.url,
            "--source-type",
            job.source_type,
            "--region",
            job.region,
            "--max-pages",
            str(job.max_pages),
            "--review-pages",
            str(job.review_pages),
            "--output",
            str(extract_path),
        ]
        if job.related_videos:
            command.append("--related-videos")
        run_shop_command(job, command)

        if job.analyze:
            run_shop_command(
                job,
                [
                    "python",
                    str(SCRIPTS_DIR / "deepseek_shop_analyze.py"),
                    str(extract_path),
                    "--output",
                    str(analysis_path),
                    "--prompt",
                    job.prompt,
                ],
            )

        with shop_jobs_lock:
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        with shop_jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def append_metrics_log(job: MetricsJob, line: str) -> None:
    with metrics_jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_metrics_command(job: MetricsJob, command: list[str]) -> None:
    append_metrics_log(job, f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        append_metrics_log(job, line)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def run_metrics_job(job_id: str) -> None:
    with metrics_jobs_lock:
        job = metrics_jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    output_dir = OUTPUT_DIR / "tiktok_api" / job_id
    metrics_path = output_dir / "result.json"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with metrics_jobs_lock:
            job.output_dir = str(output_dir.relative_to(ROOT))
            job.updated_at = time.time()

        cmd = [
            "python",
            str(SCRIPTS_DIR / "sociavault_tiktok.py"),
            "--endpoint", job.endpoint,
            "--output", str(metrics_path),
        ]
        if job.target:
            if job.target.startswith("http"):
                cmd.extend(["--url", job.target])
            elif job.target.startswith("#"):
                cmd.extend(["--hashtag", job.target.lstrip("#")])
            elif job.target.startswith("@"):
                cmd.extend(["--handle", job.target.lstrip("@")])
            elif job.endpoint in ("music-info", "music-videos"):
                cmd.extend(["--sound-id", job.target])
            elif job.endpoint.startswith("search-"):
                cmd.extend(["--query", job.target])
            else:
                cmd.extend(["--handle", job.target])

        run_metrics_command(job, cmd)
        if job.endpoint == "video-info" and metrics_path.is_file():
            register_from_payload(read_json(metrics_path), source_url=job.target)

        with metrics_jobs_lock:
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        with metrics_jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def append_amazon_log(job: AmazonJob, line: str) -> None:
    with amazon_jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def parse_json_from_process_output(output: str) -> Any:
    text = output.strip()
    if not text:
        raise ValueError("amazon-scraper returned no output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed_values = []
        for match in re.finditer(r"{", text):
            try:
                value, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            parsed_values.append(value)
        if not parsed_values:
            raise ValueError("amazon-scraper output did not contain JSON")
        # Return the largest parsed object — the scraper result is always the
        # largest JSON, while nested empty objects like "details":{} are tiny.
        parsed_values.sort(key=lambda v: len(json.dumps(v)), reverse=True)
        return parsed_values[0]


def run_amazon_command(job: AmazonJob, command: list[str]) -> tuple[str, int]:
    append_amazon_log(job, f"$ {' '.join(command)}")
    env = os.environ.copy()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    output_lines = []
    for line in process.stdout:
        output_lines.append(line)
        append_amazon_log(job, line)
    code = process.wait()
    output = "".join(output_lines)
    if code != 0:
        append_amazon_log(job, f"Command exited with code {code}")
    return output, code


def run_amazon_job(job_id: str) -> None:
    with amazon_jobs_lock:
        job = amazon_jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    output_dir = OUTPUT_DIR / "amazon" / job_id
    result_path = output_dir / "result.json"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with amazon_jobs_lock:
            job.output_dir = str(output_dir.relative_to(ROOT))
            job.updated_at = time.time()

        def normalized_amazon_url(value: str) -> str:
            parsed = urlparse(value.strip())
            host = (parsed.hostname or "").lower()
            return parsed._replace(scheme=(parsed.scheme or "https").lower(), netloc=host, fragment="").geturl()

        def fetch_amazon() -> dict[str, Any]:
            ensure_us_proxy("amazon", log=lambda line: append_amazon_log(job, line))
            command = [
                "docker",
                "run",
                "--rm",
                "--network", "host",
                "-e", "AMAZON_PROXY",
                "-e", "AMAZON_PROXIES",
                "amazon-scraper",
                "node",
                "assets/amazon_handler.js",
                job.url,
                "--pages",
                str(job.pages),
            ]
            output, code = run_amazon_command(job, command)
            parsed = parse_json_from_process_output(output)
            if code != 0 and not (isinstance(parsed, dict) and parsed.get("status") == "ERROR"):
                raise RuntimeError(f"amazon-scraper exited with code {code}")
            return parsed

        result = get_cached_or_call(
            "amazon_scraper",
            "web",
            {"url": normalized_amazon_url(job.url), "pages": int(job.pages)},
            fetch_amazon,
            metadata_builder=lambda payload: {
                "entity_type": "amazon",
                "entity_id": str((payload.get("products") or [{}])[0].get("asin") or normalized_amazon_url(job.url)) if isinstance(payload, dict) else normalized_amazon_url(job.url),
                "title": str((payload.get("products") or [{}])[0].get("title") or "") if isinstance(payload, dict) else "",
                "source_url": normalized_amazon_url(job.url),
            },
        )
        cache_label = cache_log_label(result)
        if cache_label:
            append_amazon_log(job, cache_label)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        with amazon_jobs_lock:
            if not (isinstance(result, dict) and result.get("status") == "ERROR"):
                job.status = "complete"
            else:
                job.status = "failed"
                job.error = str(result.get("message") or "amazon-scraper failed") if isinstance(result, dict) else "amazon-scraper failed"
            job.updated_at = time.time()
    except FileNotFoundError:
        message = "Docker CLI is not available in the web container"
        with amazon_jobs_lock:
            job.status = "failed"
            job.error = message
            job.updated_at = time.time()
            job.log.append(message)
    except Exception as exc:
        with amazon_jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def run_download_job(job_id: str) -> None:
    with download_jobs_lock:
        job = download_jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    result_path = OUTPUT_DIR / "download_jobs" / f"{job_id}.json"
    try:
        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if not try_cached_download_result(job, result_path) and not try_cached_video_info_download(job, result_path):
            crawler_error: Exception | None = None
            try:
                append_download_log(job, "缓存地址不可用，使用原下载器下载。")
                run_download_command(
                    job,
                    [
                        "python",
                        str(SCRIPTS_DIR / "tiktok_download.py"),
                        job.url,
                        "--output-dir",
                        str(VIDEOS_DIR),
                        "--result-json",
                        str(result_path),
                    ],
                )
                result = read_json(result_path)
                filename = safe_filename(str(result.get("filename") or "")) if isinstance(result, dict) else ""
                if filename and Path(filename).suffix.lower() in AUDIO_ONLY_SUFFIXES:
                    audio_path = VIDEOS_DIR / filename
                    audio_path.unlink(missing_ok=True)
                    crawler_error = RuntimeError(f"original downloader returned audio-only media: {filename}")
                    append_download_log(job, f"删除无效音频文件并降级到 SociaVault video-info：{filename}")
                    if not try_sociavault_video_info_download(job, result_path):
                        raise RuntimeError(
                            "视频下载失败：原下载器只返回音频文件，SociaVault video-info 也没有可用下载地址。"
                        ) from crawler_error
                elif filename:
                    try:
                        ensure_analyzer_media_or_delete(VIDEOS_DIR / filename)
                    except Exception as exc:
                        crawler_error = exc
                        append_download_log(job, f"删除无效视频文件并降级到 SociaVault video-info：{filename}，原因：{exc}")
                        if not try_sociavault_video_info_download(job, result_path):
                            raise RuntimeError(
                                "视频下载失败：原下载器返回的文件不可分析，SociaVault video-info 也没有可用下载地址。"
                            ) from crawler_error
            except Exception as exc:
                crawler_error = exc
                append_download_log(job, f"原下载器失败，最后降级调用 SociaVault video-info：{exc}")
                if not try_sociavault_video_info_download(job, result_path):
                    raise RuntimeError(
                        "视频下载失败：缓存地址不可用，原下载器失败，SociaVault video-info 也没有可用下载地址。"
                    ) from crawler_error
        result = read_json(result_path)
        if not isinstance(result, dict) or not result.get("filename"):
            raise RuntimeError("Downloader did not return a video filename")
        cache_label = cache_log_label(result)
        if cache_label:
            append_download_log(job, cache_label)
        filename = safe_filename(str(result["filename"]))
        if not (VIDEOS_DIR / filename).is_file():
            raise FileNotFoundError(f"Downloaded file not found: {filename}")
        if result.get("id"):
            register_video(
                video_id=str(result.get("id")),
                platform=platform_for_url(job.url),
                source_url=str(result.get("webpage_url") or job.url),
                filename=filename,
                title=str(result.get("title") or ""),
                author=str(result.get("uploader") or ""),
            )
        with download_jobs_lock:
            job.filename = filename
            job.result = result
            job.status = "complete"
            job.updated_at = time.time()
        start_social_context_job(filename, generate_insights=True)
    except Exception as exc:
        useful_log = next(
            (
                line
                for line in reversed(job.log)
                if line and not line.startswith("$ ") and not line.startswith("Command failed with exit code")
            ),
            "",
        )
        with download_jobs_lock:
            job.status = "failed"
            job.error = useful_log or str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def run_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    try:
        output_dir = output_dir_for_filename(job.filename)
        job.output_dir = str(output_dir.relative_to(ROOT))
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt = job.analysis_prompt.strip() or DEFAULT_ANALYSIS_PROMPT
        prompt_file = output_dir / "analysis_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        if job.analysis_mode == "direct_video":
            run_command(
                job,
                [
                    "python",
                    str(SCRIPTS_DIR / "direct_video_analyze.py"),
                    job.filename,
                    "--output-dir",
                    str(output_dir),
                    "--prompt-file",
                    str(prompt_file),
                ],
            )
        else:
            run_command(
                job,
                ["bash", str(SCRIPTS_DIR / "analyze_one.sh"), job.filename],
                env_extra={"ANALYSIS_PROMPT_FILE": str(prompt_file), "ANALYSIS_OUTPUT_DIR": str(output_dir)},
            )
        mark_extracted(job.filename, output_dir.name)
        if job.postprocess:
            run_command(job, ["python", str(SCRIPTS_DIR / "deepseek_postprocess.py"), str(output_dir)])
        with jobs_lock:
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        with jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def run_postprocess_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    try:
        output_dir = output_dir_for_filename(job.filename)
        job.output_dir = str(output_dir.relative_to(ROOT))
        if not (output_dir / "analysis.json").is_file():
            raise FileNotFoundError(f"analysis.json not found: {output_dir / 'analysis.json'}")

        run_command(job, ["python", str(SCRIPTS_DIR / "deepseek_postprocess.py"), str(output_dir)])
        with jobs_lock:
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        with jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def public_job(job: Job) -> dict[str, Any]:
    output_dir = output_dir_for_filename(job.filename)
    return {
        "id": job.id,
        "filename": job.filename,
        "postprocess": job.postprocess,
        "analysis_mode": job.analysis_mode,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-200:],
        "analysis": read_json(output_dir / "analysis.json"),
        "analysis_zh": read_json(output_dir / "analysis_zh.json"),
        "audit_result": read_json(output_dir / "audit_result.json"),
        "audit_result_zh": read_json(output_dir / "audit_result_zh.json"),
        "feedback_result": read_json(output_dir / "feedback_result.json"),
        "feedback_result_zh": read_json(output_dir / "feedback_result_zh.json"),
    }


def public_download_job(job: DownloadJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "url": job.url,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "filename": job.filename,
        "error": job.error,
        "log": job.log[-80:],
        "result": job.result,
    }


def public_shop_job(job: ShopJob) -> dict[str, Any]:
    output_dir = OUTPUT_DIR / "tiktok_shop" / job.id
    return {
        "id": job.id,
        "url": job.url,
        "source_type": job.source_type,
        "region": job.region,
        "max_pages": job.max_pages,
        "review_pages": job.review_pages,
        "analyze": job.analyze,
        "related_videos": job.related_videos,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-120:],
        "extract": read_json(output_dir / "shop_extract.json"),
        "analysis": read_json(output_dir / "shop_analysis.json"),
    }


def public_metrics_job(job: MetricsJob) -> dict[str, Any]:
    output_dir = OUTPUT_DIR / "tiktok_api" / job.id
    return {
        "id": job.id,
        "target": job.target,
        "endpoint": job.endpoint,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-120:],
        "result": read_json(output_dir / "result.json"),
    }


def public_amazon_job(job: AmazonJob) -> dict[str, Any]:
    output_dir = OUTPUT_DIR / "amazon" / job.id
    return {
        "id": job.id,
        "target": job.target,
        "target_type": job.target_type,
        "url": job.url,
        "pages": job.pages,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-120:],
        "result": read_json(output_dir / "result.json"),
    }


def check_ip_route(name: str, proxy_url: str | None = None) -> dict[str, Any]:
    import httpx

    payload: dict[str, Any] = {
        "name": name,
        "proxy_url": proxy_url or "",
        "ok": False,
        "ip": "",
        "country": "",
        "country_name": "",
        "is_us": False,
        "error": "",
    }
    client_kwargs: dict[str, Any] = {
        "timeout": 12.0,
        "follow_redirects": True,
        "trust_env": False,
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    try:
        with httpx.Client(**client_kwargs) as client:
            response = client.get("https://ipapi.co/json/")
            response.raise_for_status()
            data = response.json()
        country = str(data.get("country_code") or data.get("country") or "").upper()
        payload.update(
            {
                "ok": True,
                "ip": str(data.get("ip") or ""),
                "country": country,
                "country_name": str(data.get("country_name") or ""),
                "is_us": country == "US",
            }
        )
    except Exception as exc:
        payload["error"] = str(exc) or repr(exc)
    return payload


def public_network_check() -> dict[str, Any]:
    tiktok_proxy = os.getenv("TIKTOK_PROXY_URL", "").strip()
    direct = check_ip_route("direct")
    proxy = check_ip_route("proxy", tiktok_proxy) if tiktok_proxy else None
    return {
        "tiktok_proxy_url": tiktok_proxy,
        "direct": direct,
        "proxy": proxy,
        "proxy_is_us": bool(proxy and proxy.get("is_us")),
    }


def slim_tool_result(obj: Any, depth: int = 0) -> Any:
    """Compress tool result by removing bloat (long URLs, deep nested objects)."""
    if isinstance(obj, dict):
        filtered = {}
        for k, v in obj.items():
            if isinstance(v, str) and len(v) > 500 and any(x in v for x in ("tiktokcdn", "x-expires", "x-signature")):
                continue
            if depth >= 2 and isinstance(v, (dict, list)):
                continue
            if depth >= 3:
                continue
            filtered[k] = slim_tool_result(v, depth + 1) if isinstance(v, (dict, list)) else v
        return filtered
    if isinstance(obj, list):
        if len(obj) > 20:
            obj = obj[:20]
        return [slim_tool_result(item, depth + 1) if isinstance(item, (dict, list)) else item for item in obj]
    return obj


def _unwrap_tool_data(result: dict[str, Any]) -> tuple[Any, str | None, int | None]:
    """Return actual tool payload plus optional raw file metadata."""
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict) and "data" in data and ("raw_ref" in data or "raw_bytes" in data):
        return data.get("data"), data.get("raw_ref"), data.get("raw_bytes")
    return data, None, None


def _tool_cache_info(result: dict[str, Any], payload: Any) -> dict[str, Any] | None:
    def find_cache(value: Any, depth: int = 0) -> dict[str, Any] | None:
        if depth > 5:
            return None
        if isinstance(value, dict):
            cache = value.get("_cache")
            if isinstance(cache, dict) and "hit" in cache:
                return cache
            for child in value.values():
                found = find_cache(child, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value[:20]:
                found = find_cache(child, depth + 1)
                if found:
                    return found
        return None

    candidates = []
    if isinstance(payload, dict):
        candidates.append(payload.get("_cache"))
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        candidates.append(data.get("_cache"))
        nested = data.get("data")
        if isinstance(nested, dict):
            candidates.append(nested.get("_cache"))
    for candidate in candidates:
        if isinstance(candidate, dict) and "hit" in candidate:
            return {
                "hit": bool(candidate.get("hit")),
                "label": str(candidate.get("label") or ("缓存命中" if candidate.get("hit") else "实时调用")),
                "provider": candidate.get("provider"),
                "endpoint": candidate.get("endpoint"),
            }
    candidate = find_cache(result)
    if isinstance(candidate, dict) and "hit" in candidate:
        return {
            "hit": bool(candidate.get("hit")),
            "label": str(candidate.get("label") or ("cache_hit" if candidate.get("hit") else "live_call")),
            "provider": candidate.get("provider"),
            "endpoint": candidate.get("endpoint"),
        }
    return None


def _compact_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _as_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        def sort_key(item: tuple[Any, Any]) -> tuple[int, str]:
            key, _ = item
            try:
                return (0, f"{int(key):08d}")
            except (TypeError, ValueError):
                return (1, str(key))
        return [v for _, v in sorted(value.items(), key=sort_key)]
    return []


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _first_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        urls = value.get("url_list")
        if isinstance(urls, list) and urls:
            return str(urls[0])
        for key in ("url", "uri"):
            if value.get(key):
                return str(value[key])
    return ""


def _tiktok_video_url(aweme: dict[str, Any], author: dict[str, Any] | None = None) -> str:
    direct = _first_present(
        aweme.get("share_url"),
        aweme.get("shareUrl"),
        aweme.get("url"),
        aweme.get("web_url"),
        aweme.get("webUrl"),
    )
    if direct:
        return str(direct).strip()
    video_id = _first_present(aweme.get("aweme_id"), aweme.get("id"), aweme.get("video_id"), aweme.get("item_id"))
    author = author or (aweme.get("author") if isinstance(aweme.get("author"), dict) else {})
    handle = _first_present(author.get("unique_id"), author.get("uniqueId"), aweme.get("author_unique_id"))
    if video_id and handle:
        return f"https://www.tiktok.com/@{handle}/video/{video_id}"
    if video_id:
        return f"https://www.tiktok.com/video/{video_id}"
    return ""


def _extract_media_urls(value: Any, limit: int = 8) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: Any) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return
        lowered = url.lower()
        if any(word in lowered for word in ("avatar", "cover", "jpeg", ".jpg", ".png", ".webp")):
            return
        if url in seen:
            return
        seen.add(url)
        urls.append(url)

    def walk(obj: Any, path: str = "") -> None:
        if len(urls) >= limit:
            return
        if isinstance(obj, dict):
            if path and any(word in path.lower() for word in ("play_addr", "download_addr", "play_url", "download_url", "video")):
                candidate = _first_url(obj)
                if candidate:
                    add(candidate)
            for key, child in obj.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(obj, list):
            for child in obj:
                walk(child, path)
        elif isinstance(obj, str):
            add(obj)

    walk(value)
    return urls[:limit]


def _tiktok_count(stats: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in stats:
            return stats.get(key)
    return None


def _extract_tiktok_items(payload: Any, limit: int = 20) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    search_list = None
    if isinstance(data, dict):
        search_list = _first_present(
            data.get("search_item_list"),
            data.get("item_list"),
            data.get("aweme_list"),
            data.get("items"),
            data.get("videos"),
        )
    items: list[dict[str, Any]] = []
    for raw in _as_items(search_list)[:limit]:
        aweme = raw.get("aweme_info", raw) if isinstance(raw, dict) else {}
        if not isinstance(aweme, dict):
            continue
        author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
        stats = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
        music = aweme.get("music") or aweme.get("added_sound_music_info") or {}
        hashtags = []
        for tag in _as_items(aweme.get("text_extra")):
            if isinstance(tag, dict):
                name = _first_present(tag.get("hashtag_name"), tag.get("hashtagName"))
                if name:
                    hashtags.append(str(name))
        items.append({
            "id": _first_present(aweme.get("aweme_id"), aweme.get("id")),
            "description": _compact_text(_first_present(aweme.get("desc"), aweme.get("description"), aweme.get("title")), 320),
            "author": _compact_text(_first_present(author.get("nickname"), aweme.get("author"), author.get("unique_id")), 120),
            "handle": _compact_text(_first_present(author.get("unique_id"), author.get("sec_uid")), 120),
            "play_count": _tiktok_count(stats, "play_count", "playCount"),
            "like_count": _tiktok_count(stats, "digg_count", "like_count", "likeCount"),
            "comment_count": _tiktok_count(stats, "comment_count", "commentCount"),
            "share_count": _tiktok_count(stats, "share_count", "shareCount"),
            "duration": _first_present(aweme.get("duration"), aweme.get("video", {}).get("duration") if isinstance(aweme.get("video"), dict) else None),
            "create_time": aweme.get("create_time"),
            "hashtags": hashtags[:8],
            "music": _compact_text(_first_present(music.get("title") if isinstance(music, dict) else None, music.get("music_name") if isinstance(music, dict) else None), 160),
            "url": _tiktok_video_url(aweme, author),
        })
    return [item for item in items if any(v not in (None, "", [], {}) for v in item.values())]


def _extract_tiktok_video_info(payload: Any) -> dict[str, Any]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    aweme = data
    if isinstance(data, dict):
        aweme = _first_present(
            data.get("aweme_detail"),
            data.get("aweme_info"),
            data.get("itemInfo", {}).get("itemStruct") if isinstance(data.get("itemInfo"), dict) else None,
            data.get("video"),
            data.get("item"),
            data,
        )
    if not isinstance(aweme, dict):
        return {}
    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    stats = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
    music = aweme.get("music") or aweme.get("added_sound_music_info") or {}
    video = aweme.get("video") if isinstance(aweme.get("video"), dict) else {}
    media_urls = _extract_media_urls(video or aweme)
    return {
        "id": _first_present(aweme.get("aweme_id"), aweme.get("id"), aweme.get("video_id"), aweme.get("item_id")),
        "description": _compact_text(_first_present(aweme.get("desc"), aweme.get("description"), aweme.get("title")), 500),
        "author": _compact_text(_first_present(author.get("nickname"), author.get("unique_id")), 160),
        "handle": _compact_text(_first_present(author.get("unique_id"), author.get("uniqueId"), author.get("sec_uid")), 160),
        "url": _tiktok_video_url(aweme, author),
        "play_count": _tiktok_count(stats, "play_count", "playCount"),
        "like_count": _tiktok_count(stats, "digg_count", "like_count", "likeCount"),
        "comment_count": _tiktok_count(stats, "comment_count", "commentCount"),
        "share_count": _tiktok_count(stats, "share_count", "shareCount"),
        "duration": _first_present(aweme.get("duration"), video.get("duration")),
        "music_title": _compact_text(_first_present(music.get("title") if isinstance(music, dict) else None, music.get("music_name") if isinstance(music, dict) else None), 180),
        "music_url": _first_url(_first_present(music.get("play_url") if isinstance(music, dict) else None, music.get("playUrl") if isinstance(music, dict) else None)),
        "media_urls": media_urls,
    }


def _extract_tiktok_music_items(payload: Any, limit: int = 12) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    music_list = None
    if isinstance(data, dict):
        music_list = _first_present(data.get("music"), data.get("music_info_list"), data.get("items"), data.get("sounds"))
    items: list[dict[str, Any]] = []
    for raw in _as_items(music_list)[:limit]:
        music = raw.get("music", raw) if isinstance(raw, dict) else {}
        if not isinstance(music, dict):
            continue
        play_url = _first_url(_first_present(music.get("play_url"), music.get("playUrl"), music.get("preview_url")))
        cover_url = _first_url(_first_present(music.get("cover_thumb"), music.get("cover_medium"), music.get("cover_large")))
        sound_id = _first_present(music.get("id_str"), music.get("id"), music.get("mid"), music.get("music_id"))
        items.append({
            "sound_id": sound_id,
            "title": _compact_text(_first_present(music.get("title"), music.get("music_name"), music.get("name")), 180),
            "author": _compact_text(_first_present(music.get("author"), music.get("authorName"), music.get("owner_nickname")), 140),
            "album": _compact_text(music.get("album"), 160),
            "duration": _first_present(music.get("duration"), music.get("duration_high_precision")),
            "play_url": play_url,
            "cover_url": cover_url,
            "tiktok_music_url": f"https://www.tiktok.com/music/{quote_plus(str(_first_present(music.get('title'), 'sound')))}-{sound_id}" if sound_id else "",
        })
    return [item for item in items if any(v not in (None, "", [], {}) for v in item.values())]


def _extract_amazon_products(payload: Any, limit: int = 20) -> list[dict[str, Any]]:
    products = payload.get("products") if isinstance(payload, dict) else []
    items: list[dict[str, Any]] = []
    for product in _as_items(products)[:limit]:
        if not isinstance(product, dict):
            continue
        details = product.get("details") if isinstance(product.get("details"), dict) else {}
        items.append({
            "asin": product.get("asin"),
            "title": _compact_text(_first_present(product.get("title"), product.get("name")), 320),
            "price": _first_present(product.get("priceStr"), product.get("price")),
            "rating": product.get("rating"),
            "reviews": _first_present(product.get("reviews"), product.get("reviewCount")),
            "bsr": _first_present(product.get("bsr"), details.get("Best Sellers Rank")),
            "bought_past_month": product.get("boughtPastMonth"),
            "date_first_available": _first_present(product.get("dateFirstAvailable"), details.get("Date First Available")),
            "category": _first_present(product.get("category"), payload.get("category") if isinstance(payload, dict) else None),
            "bullets": [_compact_text(v, 220) for v in _as_items(product.get("bullets"))[:8]],
            "url": _compact_text(_first_present(product.get("url"), product.get("productUrl")), 300),
        })
    return [item for item in items if any(v not in (None, "", [], {}) for v in item.values())]


def _extract_shop_products(payload: Any, limit: int = 20) -> list[dict[str, Any]]:
    products = payload.get("products") if isinstance(payload, dict) else []
    items: list[dict[str, Any]] = []
    for product in _as_items(products)[:limit]:
        if not isinstance(product, dict):
            continue
        price = product.get("product_price_info") if isinstance(product.get("product_price_info"), dict) else {}
        sold = product.get("sold_info") if isinstance(product.get("sold_info"), dict) else {}
        seller = product.get("seller_info") if isinstance(product.get("seller_info"), dict) else {}
        rating = product.get("rate_info") if isinstance(product.get("rate_info"), dict) else {}
        seo = product.get("seo_url") if isinstance(product.get("seo_url"), dict) else {}
        labels = product.get("product_label_info") if isinstance(product.get("product_label_info"), dict) else {}
        label_texts = []
        for label in _as_items(labels):
            if isinstance(label, dict) and label.get("text"):
                label_texts.append(str(label["text"]))
        items.append({
            "product_id": product.get("product_id"),
            "title": _compact_text(product.get("title"), 360),
            "price": _first_present(price.get("sale_price_format"), price.get("single_product_price_format")),
            "currency": _first_present(price.get("currency_symbol"), price.get("currency_name")),
            "discount": price.get("discount_format"),
            "sold_count": sold.get("sold_count"),
            "rating": _first_present(rating.get("score"), rating.get("rating")),
            "review_count": _first_present(rating.get("review_count"), rating.get("reviewCount")),
            "shop_name": seller.get("shop_name"),
            "labels": label_texts[:4],
            "category": product.get("category_breadcrumb"),
            "url": _first_present(seo.get("canonical_url"), product.get("product_url")),
        })
    return [item for item in items if any(v not in (None, "", [], {}) for v in item.values())]


def normalize_tool_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Keep analysis-ready fields for the model/UI while storing raw payloads by reference."""
    if not isinstance(result, dict):
        return {"ok": False, "error": "Invalid tool result", "summary": result}
    if ("kind" in result or "summary" in result) and "data" not in result:
        return result
    normalized: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "elapsed": result.get("elapsed"),
    }
    if not result.get("ok"):
        normalized["error"] = result.get("error", "Tool failed")
        return normalized

    payload, raw_ref, raw_bytes = _unwrap_tool_data(result)
    cache_info = _tool_cache_info(result, payload)
    if cache_info:
        normalized["cache"] = cache_info
    if raw_ref:
        normalized["raw_ref"] = raw_ref
    if raw_bytes is not None:
        normalized["raw_bytes"] = raw_bytes

    if tool_name in {"tiktok_search_music", "tiktok_music_popular", "tiktok_music_info"}:
        music_items = _extract_tiktok_music_items(payload)
        normalized.update({
            "kind": "tiktok_music",
            "music_total": len(music_items),
            "music": music_items,
            "enough_data": bool(music_items),
            "suggested_next_action": "answer_from_results" if music_items else "try_different_query",
        })
        return normalized

    if tool_name == "tiktok_video_info":
        video_info = _extract_tiktok_video_info(payload)
        normalized.update({
            "kind": "tiktok_video",
            "video": video_info,
            "enough_data": bool(video_info),
            "suggested_next_action": "answer_from_results" if video_info else "try_different_query",
        })
        return normalized

    if tool_name.startswith("tiktok_search_") or tool_name in {"tiktok_trending", "tiktok_videos", "tiktok_videos_popular"}:
        items = _extract_tiktok_items(payload)
        normalized.update({
            "kind": "tiktok_items",
            "items_total": len(items),
            "items": items,
            "enough_data": bool(items),
            "suggested_next_action": "answer_from_results" if items else "try_different_query",
        })
        return normalized

    if tool_name.startswith("amazon_"):
        products = _extract_amazon_products(payload)
        has_product_detail = any(
            item.get("title") or item.get("price") or item.get("rating") or item.get("reviews") or item.get("bullets")
            for item in products
        )
        is_not_found = isinstance(payload, dict) and str(payload.get("category", "")).lower() == "page not found"
        normalized.update({
            "kind": "amazon_products",
            "status": payload.get("status") if isinstance(payload, dict) else None,
            "page_type": payload.get("type") if isinstance(payload, dict) else None,
            "category": payload.get("category") if isinstance(payload, dict) else None,
            "breadcrumbs": payload.get("breadcrumbs") if isinstance(payload, dict) else None,
            "products_total": len(products),
            "products": products,
            "enough_data": bool(products) and has_product_detail and not is_not_found,
            "suggested_next_action": "answer_from_results" if has_product_detail and not is_not_found else "try_different_query",
        })
        return normalized

    if tool_name.startswith("tiktok_shop_"):
        products = _extract_shop_products(payload)
        normalized.update({
            "kind": "tiktok_shop_products",
            "source_type": payload.get("source_type") if isinstance(payload, dict) else None,
            "query": payload.get("query") if isinstance(payload, dict) else None,
            "products_total": len(products),
            "products": products,
            "enough_data": bool(products),
            "suggested_next_action": "answer_from_results" if products else "try_different_query",
        })
        return normalized

    if tool_name == "video_download" and isinstance(payload, dict):
        normalized.update({
            "kind": "video_download",
            "filename": payload.get("filename"),
            "size": payload.get("size"),
            "downloader": payload.get("downloader"),
            "raw_ref": raw_ref or payload.get("raw_ref"),
            "enough_data": bool(payload.get("filename")),
        })
        return normalized

    normalized["summary"] = slim_tool_result(result)
    normalized["enough_data"] = True
    return normalized


def normalize_stored_chat_tool_results() -> int:
    """Migrate old sessions that persisted full raw tool payloads."""
    changed = 0
    with chat_store._lock:
        for session in chat_store.sessions.values():
            for message in session.messages:
                if not message.tool_results:
                    continue
                for tool_result in message.tool_results:
                    if not isinstance(tool_result, dict):
                        continue
                    tool_name = tool_result.get("tool_name", "")
                    result = tool_result.get("result")
                    if not isinstance(result, dict):
                        continue
                    normalized = normalize_tool_result(tool_name, result)
                    if normalized != result:
                        tool_result["result"] = normalized
                        changed += 1
    if changed:
        chat_store._schedule_save()
        print(f"[CHAT] normalized {changed} stored tool results", flush=True)
    return changed

def mark_interrupted_chat_messages() -> int:
    """Mark assistant messages that could not finish before a restart/interruption."""
    changed = 0
    interrupted_text = "服务器中断，稍后再试。"
    incomplete_tools_text = "服务器中断，稍后再试。"
    with chat_store._lock:
        for session in chat_store.sessions.values():
            for message in session.messages:
                if message.role != "assistant":
                    continue
                tool_calls = message.tool_calls or []
                tool_results = message.tool_results or []
                has_incomplete_tools = bool(tool_calls) and len(tool_results) < len(tool_calls)
                if message.status == "pending" or has_incomplete_tools:
                    if message.status != "error":
                        message.status = "error"
                        changed += 1
                    if not message.content:
                        message.content = incomplete_tools_text if has_incomplete_tools else interrupted_text
                        changed += 1
    if changed:
        chat_store._schedule_save()
        print(f"[CHAT] marked {changed} interrupted stored messages", flush=True)
    return changed


def is_music_link_query(text: str) -> bool:
    lowered = (text or "").lower()
    has_music = any(word in lowered for word in ("music", "sound", "audio", "bgm", "song", "remix", "音乐", "音频", "原声", "歌曲"))
    has_link = any(word in lowered for word in ("link", "url", "链接", "地址", "有没有", "有吗", "哪里"))
    return has_music and has_link


def music_link_search_query(text: str) -> str:
    query = str(text or "")
    query = re.sub(
        r"(有没有|有无|是否有|音频链接|音乐链接|声音链接|下载链接|链接|地址|url|URL|吗|么|呢|\?)",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\s+", " ", query).strip(" -—_:：，,。")
    return query or str(text or "").strip()


def is_media_availability_query(text: str) -> bool:
    lowered = (text or "").lower()
    has_media = any(word in lowered for word in ("video", "audio", "music", "sound", "bgm", "视频", "音频", "音乐", "链接"))
    asks_exists = any(word in lowered for word in ("有没有", "有无", "是否有", "有没有", "有吗", "有么", "find", "show me"))
    return has_media and asks_exists


MUSIC_QUERY_TOOLS = {"tiktok_search_music", "tiktok_music_info", "tiktok_music_videos", "tiktok_music_popular"}

AMAZON_TOOLS = {"amazon_scrape_url", "amazon_scrape_asin", "amazon_search_keyword"}
TIKTOK_SHOP_TOOLS = {"tiktok_shop_product", "tiktok_shop_details", "tiktok_shop_reviews", "tiktok_shop_search"}
TIKTOK_USER_TOOLS = {
    "tiktok_profile",
    "tiktok_videos",
    "tiktok_videos_popular",
    "tiktok_followers",
    "tiktok_following",
    "tiktok_demographics",
    "tiktok_live",
}
TIKTOK_VIDEO_TOOLS = {"tiktok_video_info", "tiktok_comments", "tiktok_comment_replies", "tiktok_transcript"}
TIKTOK_CONTENT_TOOLS = {
    "tiktok_search_users",
    "tiktok_search_hashtag",
    "tiktok_search_keyword",
    "tiktok_search_top",
    "tiktok_trending",
    "tiktok_creators_popular",
    "tiktok_hashtags_popular",
    "tiktok_videos_popular",
}
VIDEO_ANALYSIS_TOOLS = {"video_download", "video_analyze", "video_direct_analyze", "tiktok_video_info"}
PRODUCT_RESEARCH_TOOLS = (
    AMAZON_TOOLS
    | TIKTOK_SHOP_TOOLS
    | {"tiktok_search_keyword", "tiktok_search_top", "tiktok_trending", "tiktok_hashtags_popular"}
)


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in words)


def route_chat_intent(text: str) -> dict[str, Any]:
    lowered = (text or "").lower()
    has_tiktok_url = "tiktok.com" in lowered or "douyin.com" in lowered
    has_video_url = has_tiktok_url and ("/video/" in lowered or "/v/" in lowered or "vm.tiktok.com" in lowered)
    has_amazon = "amazon." in lowered or _contains_any(lowered, ("asin",))
    has_shop = _contains_any(lowered, ("tiktok shop", "shop/pdp", "商品", "店铺", "小店", "橱窗"))
    has_product = _contains_any(
        lowered,
        ("product", "market", "category", "research", "competitor", "selection", "选品", "商品", "品类", "市场", "竞品", "调研", "大卖", "热卖", "热度", "爆款"),
    )
    has_analysis = _contains_any(lowered, ("analyze", "analysis", "download", "report", "解析", "分析", "下载", "报告", "提取"))
    has_user = _contains_any(lowered, ("profile", "user", "creator", "followers", "达人", "用户", "账号", "作者", "粉丝", "主页"))
    has_trend = _contains_any(lowered, ("trend", "trending", "hot", "viral", "hashtag", "keyword", "热门", "趋势", "热搜", "话题", "标签", "搜索"))

    if is_music_link_query(text):
        return {"intent": "music_link", "tools": MUSIC_QUERY_TOOLS, "max_rounds": 2}
    if is_media_availability_query(text):
        return {"intent": "media_availability", "tools": TIKTOK_CONTENT_TOOLS | TIKTOK_VIDEO_TOOLS | MUSIC_QUERY_TOOLS, "max_rounds": 3}
    if has_video_url and has_analysis:
        return {"intent": "video_analysis", "tools": VIDEO_ANALYSIS_TOOLS, "max_rounds": 3}
    if has_video_url:
        return {"intent": "tiktok_video", "tools": TIKTOK_VIDEO_TOOLS | MUSIC_QUERY_TOOLS, "max_rounds": 3}
    if has_shop and not has_amazon and not has_product:
        return {"intent": "tiktok_shop", "tools": TIKTOK_SHOP_TOOLS, "max_rounds": 3}
    if has_amazon and not has_shop and not has_product:
        return {"intent": "amazon_product", "tools": AMAZON_TOOLS, "max_rounds": 3}
    if has_product:
        return {"intent": "product_research", "tools": PRODUCT_RESEARCH_TOOLS, "max_rounds": 4}
    if has_user:
        return {"intent": "tiktok_user", "tools": TIKTOK_USER_TOOLS | {"tiktok_search_users"}, "max_rounds": 4}
    if has_tiktok_url or has_trend:
        return {"intent": "tiktok_content", "tools": TIKTOK_CONTENT_TOOLS | MUSIC_QUERY_TOOLS, "max_rounds": 4}
    return {"intent": "general", "tools": None, "max_rounds": 5}


def tools_for_chat_intent(user_text: str, enabled: set[str] | None) -> tuple[list[dict], dict[str, Any]]:
    route = route_chat_intent(user_text)
    route_tools = route.get("tools")
    if route_tools is None:
        selected = enabled
    elif enabled is None:
        selected = set(route_tools)
    else:
        selected = set(route_tools) & enabled
    return get_tools_for_model(selected), route


def run_chat_deepseek(session, assistant_msg, user_text: str) -> None:
    """Background thread: call DeepSeek with tool calling, stream results via SSE."""
    import requests as req
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    api_url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    if not api_key:
        chat_store.update_message(session, assistant_msg, "错误：未配置 DEEPSEEK_API_KEY", status="error")
        return

    messages = [{"role": "system", "content": (
        "你是短视频分析助手。使用提供的工具帮助用户分析 Amazon 商品、TikTok 数据和视频内容。"
        "工具结果已经被整理成可分析摘要；当结果里 enough_data=true 或 suggested_next_action=answer_from_results 时，"
        "必须基于现有结果直接分析，不要继续调用同类搜索工具。用中文回复，简洁专业。"
        "如果 video_download 或 video_analyze 返回失败，必须明确说明没有完成真实视频下载/画面分析；"
        "此时只能基于 TikTok 元数据、账号信息或评论做初步判断，不得声称已经看过视频内容。"
    )}]
    for m in session.messages[-20:]:
        if m.id == assistant_msg.id:
            continue
        tool_calls = m.tool_calls or []
        tool_results = m.tool_results or []
        if tool_calls and len(tool_results) < len(tool_calls):
            if m.content:
                messages.append({"role": m.role, "content": m.content})
            else:
                messages.append({"role": m.role, "content": "上一次工具调用被中断，结果不完整，已忽略这轮工具上下文。"})
            continue

        md = {"role": m.role, "content": m.content}
        if tool_calls:
            md["tool_calls"] = tool_calls
        messages.append(md)
        # Add tool result messages for each tool call (required by DeepSeek API)
        if tool_results:
            for i, tr in enumerate(tool_results):
                tc = tool_calls[i] if i < len(tool_calls) else None
                tid = tc["id"] if tc else f"call_{i}"
                tr_content = json.dumps(normalize_tool_result(tr.get("tool_name", ""), tr.get("result", {})), ensure_ascii=False)
                messages.append({"role": "tool", "tool_call_id": tid, "content": tr_content})

    tools, intent_route = tools_for_chat_intent(user_text, chat_tool_config)
    music_link_query = intent_route.get("intent") == "music_link"
    max_tool_rounds = int(intent_route.get("max_rounds") or 5)
    messages.append({
        "role": "system",
        "content": (
            f"Intent route: {intent_route.get('intent')}. Only use the exposed tools. "
            "If a tool result has enough_data=true or suggested_next_action=answer_from_results, answer directly. "
            "For product research, compare TikTok Shop demand/sales signals and Amazon product/price/review signals when both are available. "
            "For TikTok Shop category research that asks for comments/reviews, first use tiktok_shop_search, then use product_id or canonical_url/product URL from those search results to call tiktok_shop_product for selected products; do not call tiktok_shop_reviews alone unless product details are already known. "
            "For media availability/link questions, if tool results include url, play_url, tiktok_music_url, media_urls, or music_url, paste the usable links directly in the answer."
        ),
    })
    print(
        f"[CHAT] intent={intent_route.get('intent')} tools={len(tools) if tools else 0} max_rounds={max_tool_rounds}",
        flush=True,
    )
    if music_link_query:
        music_query = music_link_search_query(user_text)
        tool_call = {
            "id": f"call_music_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": "tiktok_search_music",
                "arguments": json.dumps({"query": music_query, "count": 10}, ensure_ascii=False),
            },
        }
        assistant_msg.tool_calls = [tool_call]
        assistant_msg.tool_results = []
        chat_store.broadcast(
            session.id,
            "update",
            {"messageId": assistant_msg.id, "tool_calls": assistant_msg.tool_calls, "tool_results": []},
        )
        result = execute_tool("tiktok_search_music", {"query": music_query, "count": 10})
        normalized_result = normalize_tool_result("tiktok_search_music", result)
        assistant_msg.tool_results.append({"tool_name": "tiktok_search_music", "result": normalized_result})
        messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
        messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps(normalized_result, ensure_ascii=False)})
        chat_store.broadcast(
            session.id,
            "update",
            {"messageId": assistant_msg.id, "tool_calls": assistant_msg.tool_calls, "tool_results": assistant_msg.tool_results},
        )
        tools = []
        max_tool_rounds = 1
        messages.append({
            "role": "system",
            "content": "用户只是在问音乐/音频链接。必须基于刚才的 tiktok_search_music 工具结果回答，直接列出最匹配音乐的 play_url 和 TikTok music URL；不要说没有能力提供链接，除非工具结果里确实没有 URL。",
        })
    for _ in range(max_tool_rounds):
        try:
            payload = {"model": model, "messages": messages, "tools": tools or None, "temperature": 0.2}
            payload_str = json.dumps(payload, ensure_ascii=False)
            print(f"[CHAT] DeepSeek request: {len(messages)} msgs, {len(payload_str)} bytes, tools={len(tools) if tools else 0}", flush=True)
            request_started = time.monotonic()
            resp = req.post(
                api_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                data=payload_str.encode("utf-8"),
                timeout=120,
            )
            if resp.status_code >= 400:
                print(f"[CHAT] DeepSeek {resp.status_code}: {resp.text[:500]}", flush=True)
            resp.raise_for_status()
            body = resp.json()
            record_api_call(
                "deepseek",
                "chat",
                {
                    "api_url": api_url.rstrip("/") + "/chat/completions",
                    "model": model,
                    "payload_sha256": __import__("hashlib").sha256(payload_str.encode("utf-8")).hexdigest(),
                    "message_count": len(messages),
                    "tool_count": len(tools) if tools else 0,
                },
                body,
                elapsed_ms=int((time.monotonic() - request_started) * 1000),
            )
            choice = body["choices"][0]
            msg = choice["message"]

            if msg.get("tool_calls"):
                tool_calls = msg["tool_calls"]
                assistant_msg.tool_calls = list(assistant_msg.tool_calls or []) + tool_calls
                messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
                assistant_msg.tool_results = list(assistant_msg.tool_results or [])
                chat_store.broadcast(session.id, "update", {"messageId": assistant_msg.id, "tool_calls": assistant_msg.tool_calls, "tool_results": assistant_msg.tool_results})

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}
                    result = execute_tool(fn_name, fn_args)
                    normalized_result = normalize_tool_result(fn_name, result)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(normalized_result, ensure_ascii=False)})
                    assistant_msg.tool_results.append({"tool_name": fn_name, "result": normalized_result})
                    chat_store.broadcast(session.id, "update", {"messageId": assistant_msg.id, "tool_calls": assistant_msg.tool_calls, "tool_results": assistant_msg.tool_results})
                if music_link_query and any(
                    isinstance(tr.get("result"), dict) and tr["result"].get("enough_data")
                    for tr in assistant_msg.tool_results
                ):
                    tools = []
                    messages.append({
                        "role": "system",
                        "content": "用户只是在问音乐/音频链接。已有音乐搜索结果后必须直接回答，列出最匹配的音乐名称、作者、sound_id、play_url 或 TikTok music URL；不要再调用任何工具。",
                    })
            else:
                content = msg.get("content", "")
                chat_store.update_message(session, assistant_msg, content, status="done")
                chat_store.broadcast(session.id, "done", {"messageId": assistant_msg.id, "content": content})
                return
        except Exception as exc:
            err_text = str(exc)
            if hasattr(exc, 'response'):
                try:
                    err_text += " | body: " + exc.response.text[:300]
                except Exception:
                    pass
            print(f"[CHAT] DeepSeek error: {err_text}", flush=True)
            chat_store.update_message(session, assistant_msg, f"请求失败：{exc}", status="error")
            return

    chat_store.update_message(session, assistant_msg, "工具调用次数过多，请缩小问题范围。", status="error")


def execute_queue_job(filename: str, job_type: str, progress: dict) -> None:
    """Called by VideoQueue worker to run an analysis or report job sequentially."""
    registry_record = get_video_by_filename(filename)
    extraction_dir_name = str(registry_record.get("extraction_dir") or filename) if registry_record else filename
    output_dir = OUTPUT_DIR / extraction_dir_name
    output_dir.mkdir(parents=True, exist_ok=True)

    if job_type == "analyze":
        video_queue.set_progress(filename, "extracting", 10, job_type, f"{filename}: 开始解析视频")
        mode_file = output_dir / "analysis_mode.txt"
        analysis_mode = os.getenv("ANALYSIS_MODE", "analyzer")
        if mode_file.is_file():
            mode_value = mode_file.read_text(encoding="utf-8").strip()
            if mode_value in {"analyzer", "direct_video"}:
                analysis_mode = mode_value
        is_direct = analysis_mode == "direct_video"
        analysis_path = output_dir / ("direct_analysis.json" if is_direct else "analysis.json")
        # Skip if already analyzed or complete
        current = video_queue.get_status(filename)
        if current in ("analyzed", "complete") and analysis_path.is_file():
            video_queue.set_progress(filename, "completed", 100, job_type, f"{filename}: 已有解析结果，跳过")
            return
        if not is_direct and registry_record and registry_record.get("extracted_at") and registry_record.get("extraction_dir"):
            existing_dir = OUTPUT_DIR / str(registry_record["extraction_dir"])
            if (existing_dir / "analysis.json").is_file():
                video_queue.set_status(filename, "analyzed")
                video_queue.set_progress(filename, "completed", 100, job_type, f"{filename}: 同一 TikTok 视频已提取，跳过重复提取")
                return
        if analysis_path.is_file():
            video_queue.set_status(filename, "analyzed")
            video_queue.set_progress(filename, "completed", 100, job_type, f"{filename}: 已加载已有解析结果")
            return

        # Load prompt from file if exists, else use default
        prompt = DEFAULT_ANALYSIS_PROMPT
        prompt_file = output_dir / "analysis_prompt.txt"
        if not prompt_file.is_file():
            root_prompt = ROOT / "analysis_prompt.txt"
            if root_prompt.is_file():
                prompt = root_prompt.read_text(encoding="utf-8").strip() or prompt
            prompt_file.write_text(prompt, encoding="utf-8")

        video_queue.set_progress(filename, "extracting", 20, job_type, f"{filename}: 正在调用视频解析脚本")
        env = os.environ.copy()
        env["ANALYSIS_PROMPT_FILE"] = str(prompt_file)
        env["ANALYSIS_OUTPUT_DIR"] = str(output_dir)
        if is_direct:
            with tempfile.TemporaryDirectory(prefix="direct_", dir=str(output_dir)) as tmp:
                tmp_dir = Path(tmp)
                cmd = [
                    "python",
                    str(SCRIPTS_DIR / "direct_video_analyze.py"),
                    filename,
                    "--output-dir",
                    str(tmp_dir),
                    "--prompt-file",
                    str(prompt_file),
                ]
                subprocess.run(cmd, cwd=ROOT, check=True, env=env)
                direct_source = tmp_dir / "analysis.json"
                if not direct_source.is_file():
                    raise FileNotFoundError(f"direct analysis.json not found: {direct_source}")
                shutil.move(str(direct_source), str(output_dir / "direct_analysis.json"))
        else:
            cmd = ["bash", str(SCRIPTS_DIR / "analyze_one.sh"), filename]
            subprocess.run(cmd, cwd=ROOT, check=True, env=env)
            mark_extracted(filename, extraction_dir_name)
        video_queue.set_status(filename, "analyzed")
        video_queue.set_progress(filename, "extracting", 65, job_type, f"{filename}: 视频解析完成")

        # Generate short Chinese title via LLM
        import re as _re
        title = filename[:6]
        try:
            import requests as _req
            _api_key = os.getenv("DEEPSEEK_API_KEY", "")
            if _api_key:
                video_queue.set_progress(filename, "titling", 88, job_type, f"{filename}: 正在生成短标题")
                _text = ""
                for _src in ["audit_result_zh.json", "analysis_zh.json", "analysis.json", "direct_analysis_zh.json", "direct_analysis.json"]:
                    _sp = output_dir / _src
                    if _sp.is_file():
                        _data = json.loads(_sp.read_text(encoding="utf-8"))
                        _text = _data.get("content_overview") or _data.get("summary") or ""
                        if isinstance(_text, dict):
                            _text = str(_text.get("response", ""))
                        if _text:
                            break
                if _text and len(_text) > 20:
                    _text = _text[:500]
                    _title_payload = {"model": "deepseek-chat", "messages": [
                        {"role": "system", "content": "你是一个标题生成器。根据视频内容描述，生成6字以内的中文短标题。格式：地点+人物/动作。只输出标题本身，不要引号不要解释不要标点。"},
                        {"role": "user", "content": f"视频内容：{_text}\n\n6字以内短标题："}
                    ], "temperature": 0.3, "max_tokens": 15}
                    _title_started = time.monotonic()
                    _resp = _req.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
                        json=_title_payload,
                        timeout=15,
                    )
                    _title_body = _resp.json()
                    record_api_call(
                        "deepseek",
                        "video_title",
                        {
                            "api_url": "https://api.deepseek.com/v1/chat/completions",
                            "model": "deepseek-chat",
                            "payload_sha256": __import__("hashlib").sha256(json.dumps(_title_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
                        },
                        _title_body,
                        elapsed_ms=int((time.monotonic() - _title_started) * 1000),
                        metadata={"entity_type": "video", "entity_id": filename, "source_url": filename},
                    )
                    _title = _title_body["choices"][0]["message"]["content"].strip()
                    _title = _title.replace('"','').replace('「','').replace('」','').replace('\n','').replace(' ','')
                    _title = "".join(_re.findall(r"[一-鿿\w]", _title))[:6]
                    if _title:
                        title = _title
        except Exception:
            pass
        video_queue.set_title(filename, title)
        video_queue.set_progress(filename, "completed", 100, job_type, f"{filename}: 解析完成")

    elif job_type == "report":
        video_queue.set_progress(filename, "auditing", 10, job_type, f"{filename}: 开始生成报告")
        current = video_queue.get_status(filename)
        if current == "complete":
            video_queue.set_progress(filename, "completed", 100, job_type, f"{filename}: 已有报告，跳过")
            return
        if (output_dir / "audit_result.json").is_file():
            video_queue.set_status(filename, "complete")
            video_queue.set_progress(filename, "completed", 100, job_type, f"{filename}: 已加载已有报告")
            return
        video_queue.set_progress(filename, "auditing", 25, job_type, f"{filename}: 正在调用 DeepSeek 生成报告")
        cmd = ["python", str(SCRIPTS_DIR / "deepseek_postprocess.py"), str(output_dir)]
        prompt_file = output_dir / "analysis_prompt.txt"
        if prompt_file.is_file():
            cmd.extend(["--prompt", prompt_file.read_text(encoding="utf-8").strip()])
        subprocess.run(cmd, cwd=ROOT, check=True, env=os.environ.copy())
        video_queue.set_progress(filename, "auditing", 70, job_type, f"{filename}: 报告生成完成")
        video_queue.set_status(filename, "complete")
        video_queue.set_progress(filename, "completed", 100, job_type, f"{filename}: 报告完成")


def sellersprite_chat_port() -> int:
    try:
        return int(os.getenv("SELLERSPRITE_CHAT_PORT", "4101"))
    except ValueError:
        return 4101


def ensure_sellersprite_chat_server() -> tuple[bool, str]:
    global SELLERSPRITE_CHAT_PROCESS
    if not (SELLERSPRITE_CHAT_DIR / "server.js").is_file():
        return False, f"SellerSprite chat server not found: {SELLERSPRITE_CHAT_DIR / 'server.js'}"

    port = sellersprite_chat_port()
    with SELLERSPRITE_CHAT_LOCK:
        if SELLERSPRITE_CHAT_PROCESS and SELLERSPRITE_CHAT_PROCESS.poll() is None:
            return True, ""
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.6)
            conn.request("GET", "/api/sessions")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status < 500:
                return True, ""
        except Exception:
            pass

        node = shutil.which("node")
        if not node:
            return False, "node executable not found in web container"

        SELLERSPRITE_CHAT_DATA_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "HOST": "127.0.0.1",
                "PORT": str(port),
                "DATA_DIR": str(SELLERSPRITE_CHAT_DATA_DIR),
                "SELLERSPRITE_MCP_URL": os.getenv("SELLERSPRITE_MCP_URL", "https://mcp.sellersprite.com/mcp"),
                "SELLERSPRITE_CACHE_TTL_SECONDS": os.getenv("SELLERSPRITE_CACHE_TTL_SECONDS", "86400"),
            }
        )
        SELLERSPRITE_CHAT_PROCESS = subprocess.Popen(
            [node, "server.js"],
            cwd=SELLERSPRITE_CHAT_DIR,
            env=env,
        )
        for _ in range(50):
            if SELLERSPRITE_CHAT_PROCESS.poll() is not None:
                return False, f"SellerSprite chat server exited with code {SELLERSPRITE_CHAT_PROCESS.returncode}"
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
                conn.request("GET", "/api/sessions")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status < 500:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            return False, "SellerSprite chat server did not become ready"
        print(f"[SELLERSPRITE] chat server listening on 127.0.0.1:{port}", flush=True)
        return True, ""


def proxy_sellersprite_chat(handler: BaseHTTPRequestHandler) -> None:
    ok, error = ensure_sellersprite_chat_server()
    if not ok:
        return json_response(handler, HTTPStatus.BAD_GATEWAY, {"error": error})

    parsed = urlparse(handler.path)
    target_path = parsed.path.removeprefix("/amazon") or "/"
    target = target_path + (f"?{parsed.query}" if parsed.query else "")
    body = None
    if handler.command in {"POST", "PUT", "PATCH"}:
        length = int(handler.headers.get("Content-Length", "0") or "0")
        body = handler.rfile.read(length) if length else b""

    headers = {
        key: value
        for key, value in handler.headers.items()
        if key.lower() not in {"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade"}
    }
    headers["Host"] = f"127.0.0.1:{sellersprite_chat_port()}"

    conn_timeout = None if target_path == "/api/events" else 180
    conn = http.client.HTTPConnection("127.0.0.1", sellersprite_chat_port(), timeout=conn_timeout)
    try:
        conn.request(handler.command, target, body=body, headers=headers)
        resp = conn.getresponse()
        handler.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade"}:
                continue
            handler.send_header(key, value)
        handler.end_headers()
        if target_path == "/api/events":
            while True:
                line = resp.readline()
                if not line:
                    break
                handler.wfile.write(line)
                handler.wfile.flush()
        else:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as exc:
        if not handler.wfile.closed:
            return json_response(handler, HTTPStatus.BAD_GATEWAY, {"error": f"SellerSprite proxy failed: {exc}"})
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "ShortVideoAnalyzer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/amazon" or parsed.path.startswith("/amazon/"):
            return proxy_sellersprite_chat(self)
        if parsed.path == "/" or parsed.path == "/chat":
            chat_html = (SCRIPTS_DIR / "static" / "chat.html").read_text(encoding="utf-8")
            return text_response(self, HTTPStatus.OK, chat_html, "text/html; charset=utf-8")
        if parsed.path == "/report":
            report_html = (SCRIPTS_DIR / "static" / "report.html").read_text(encoding="utf-8")
            return text_response(self, HTTPStatus.OK, report_html, "text/html; charset=utf-8")
        if parsed.path == "/report/player":
            player_html = (SCRIPTS_DIR / "static" / "report_player.html").read_text(encoding="utf-8")
            return text_response(self, HTTPStatus.OK, player_html, "text/html; charset=utf-8")
        if parsed.path == "/extract":
            template = INDEX_HTML_PATH.read_text(encoding="utf-8") if INDEX_HTML_PATH.is_file() else INDEX_HTML
            html = template.replace(
                "__DEFAULT_ANALYSIS_MODE__",
                os.getenv("ANALYSIS_MODE", "analyzer"),
            )
            return text_response(self, HTTPStatus.OK, html, "text/html; charset=utf-8")
        if parsed.path == "/shop":
            return text_response(self, HTTPStatus.OK, SHOP_HTML, "text/html; charset=utf-8")
        if parsed.path == "/metrics":
            return text_response(self, HTTPStatus.OK, METRICS_HTML, "text/html; charset=utf-8")
        if parsed.path.startswith("/assets/"):
            return self.serve_static_asset(parsed.path.removeprefix("/assets/"))
        if parsed.path == "/api/prompt":
            return json_response(self, HTTPStatus.OK, {"prompt": load_prompt(), "feedback_prompt": load_feedback_prompt()})
        if parsed.path == "/api/chat/sessions":
            return json_response(self, HTTPStatus.OK, chat_store.list_sessions())
        if parsed.path == "/api/chat/tools":
            return json_response(self, HTTPStatus.OK, list_tools())
        if parsed.path == "/api/chat/tool-config":
            return json_response(self, HTTPStatus.OK, {"enabled": list(chat_tool_config) if chat_tool_config is not None else None})
        if parsed.path.startswith("/api/chat/sessions/") and "/messages" in parsed.path:
            parts = parsed.path.split("/")
            sid = parts[4] if len(parts) > 4 else ""
            session = chat_store.get_session(sid)
            if not session:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Session not found"})
            qs = parse_qs(parsed.query)
            def public_message(m: Message) -> dict[str, Any]:
                return {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "tool_calls": m.tool_calls,
                    "tool_results": m.tool_results,
                    "status": m.status,
                    "created_at": m.created_at,
                }

            use_legacy_paging = "offset" in qs or "limit" in qs
            if use_legacy_paging and "max_bytes" not in qs:
                offset = max(0, int(qs.get("offset", [0])[0]))
                limit = max(1, min(int(qs.get("limit", [50])[0]), 100))
                msgs = session.messages[offset:offset + limit]
                payload_messages = [public_message(m) for m in msgs]
                loaded_bytes = len(json.dumps(payload_messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                return json_response(self, HTTPStatus.OK, {
                    "messages": payload_messages,
                    "total": len(session.messages),
                    "has_more": (offset + limit) < len(session.messages),
                    "next_before": msgs[0].created_at if msgs else None,
                    "loaded_bytes": loaded_bytes,
                })

            max_bytes = max(1024, min(int(qs.get("max_bytes", [120000])[0]), 500000))
            before_raw = qs.get("before", [""])[0]
            before = float(before_raw) if before_raw not in ("", None) else None
            selected: list[tuple[int, dict[str, Any], int]] = []
            loaded_bytes = 2
            for index in range(len(session.messages) - 1, -1, -1):
                message = session.messages[index]
                created_at = float(message.created_at or 0)
                if before is not None and created_at >= before:
                    continue
                item = public_message(message)
                item_bytes = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
                if selected and loaded_bytes + item_bytes > max_bytes:
                    break
                selected.append((index, item, item_bytes))
                loaded_bytes += item_bytes
                if loaded_bytes >= max_bytes:
                    break
            selected.reverse()
            payload_messages = [item for _, item, _ in selected]
            first_index = selected[0][0] if selected else None
            return json_response(self, HTTPStatus.OK, {
                "messages": payload_messages,
                "total": len(session.messages),
                "has_more": bool(first_index is not None and first_index > 0),
                "next_before": payload_messages[0]["created_at"] if payload_messages else None,
                "loaded_bytes": loaded_bytes,
            })
        if parsed.path == "/api/chat/events":
            return self.stream_chat_events(parse_qs(parsed.query).get("session", [""])[0])
        if parsed.path.startswith("/api/chat/sessions/") and parsed.path.endswith("/delete"):
            sid = parsed.path.split("/")[4]
            deleted = chat_store.delete_session(sid)
            return json_response(self, HTTPStatus.OK, {"deleted": deleted})
        if parsed.path == "/api/network-check":
            return json_response(self, HTTPStatus.OK, public_network_check())
        if parsed.path == "/api/sociavault-usage":
            return json_response(self, HTTPStatus.OK, read_sociavault_usage())
        if parsed.path == "/api/report/today":
            include_raw = parse_qs(parsed.query).get("raw", ["0"])[0] in {"1", "true", "yes"}
            return json_response(self, HTTPStatus.OK, get_report(include_raw=include_raw, detail=include_raw))
        if parsed.path == "/api/report/feishu":
            qs = parse_qs(parsed.query)
            if not _report_bot_authorized(self, qs):
                return json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            report_date = qs.get("date", [""])[0].strip() or None
            if report_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "date must be YYYY-MM-DD"})
            try:
                limit = int(qs.get("limit", ["10"])[0])
            except ValueError:
                limit = 10
            return json_response(self, HTTPStatus.OK, _build_feishu_report_payload(self, report_date, limit=limit))
        if parsed.path == "/api/report":
            qs = parse_qs(parsed.query)
            include_raw = qs.get("raw", ["0"])[0] in {"1", "true", "yes"}
            report_date = qs.get("date", [""])[0] or None
            return json_response(self, HTTPStatus.OK, get_report(report_date, include_raw=include_raw, detail=True))
        if parsed.path == "/api/report/history":
            try:
                limit = int(parse_qs(parsed.query).get("limit", ["30"])[0])
            except ValueError:
                limit = 30
            return json_response(self, HTTPStatus.OK, list_reports(limit))
        if parsed.path == "/api/report/settings":
            return json_response(self, HTTPStatus.OK, {**get_report_settings(), **get_report_runtime_status()})
        if parsed.path == "/api/report/events":
            report_date = parse_qs(parsed.query).get("date", [""])[0] or None
            return self.stream_report_events(report_date)
        if parsed.path == "/api/report/backfill-covers" and self.command == "POST":
            result = backfill_cover_urls()
            return json_response(self, HTTPStatus.OK, result)
        if parsed.path.startswith("/report-cover/"):
            try:
                filename = safe_filename(unquote(parsed.path.removeprefix("/report-cover/")))
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            path = REPORT_COVER_DIR / filename
            if not path.is_file():
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Cover not found"})
            content_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            return binary_response(self, HTTPStatus.OK, path.read_bytes(), content_type)
        if parsed.path.startswith("/video/"):
            try:
                filename = safe_filename(unquote(parsed.path.removeprefix("/video/")))
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return self.serve_video(VIDEOS_DIR / filename)
        if parsed.path == "/api/jobs":
            with jobs_lock:
                payload = [public_job(job) for job in sorted(jobs.values(), key=lambda item: item.created_at, reverse=True)]
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with jobs_lock:
                job = jobs.get(job_id)
                payload = public_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/job-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_job_events(job_id)
        if parsed.path == "/api/download-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with download_jobs_lock:
                job = download_jobs.get(job_id)
                payload = public_download_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Download job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/download-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_download_events(job_id)
        if parsed.path == "/api/shop-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with shop_jobs_lock:
                job = shop_jobs.get(job_id)
                payload = public_shop_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "TikTok Shop job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/shop-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_shop_events(job_id)
        if parsed.path == "/api/video-metrics-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with metrics_jobs_lock:
                job = metrics_jobs.get(job_id)
                payload = public_metrics_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Video metrics job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/video-metrics-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_metrics_events(job_id)
        if parsed.path == "/api/amazon-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with amazon_jobs_lock:
                job = amazon_jobs.get(job_id)
                payload = public_amazon_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Amazon job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/amazon-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_amazon_events(job_id)
        if parsed.path == "/api/files":
            files = []
            for path in sorted(VIDEOS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
                if path.is_file():
                    if path.suffix.lower() not in ANALYZER_VIDEO_SUFFIXES:
                        continue
                    if not analyzer_media_is_valid(path):
                        continue
                    name = path.name
                    if is_hidden_from_analyzer(name):
                        continue
                    meta = video_queue.get_status_meta(name)
                    social_meta = summarize_social_status(read_json(output_dir_for_filename(name) / "social_context.json"))
                    files.append({
                        "name": name, "size": path.stat().st_size, "mtime": path.stat().st_mtime,
                        "status": video_queue.get_status(name),
                        "status_label": meta["label"], "status_color": meta["color"], "status_bg": meta["bg"],
                        "title": video_queue.get_title(name),
                        **social_meta,
                    })
            return json_response(self, HTTPStatus.OK, files)
        if parsed.path == "/api/queue-state":
            return json_response(self, HTTPStatus.OK, video_queue.get_queue_state())
        if parsed.path == "/api/queue-progress":
            return json_response(self, HTTPStatus.OK, video_queue.get_progress())
        if parsed.path == "/api/status-stream":
            return self.stream_status_events()
        if parsed.path == "/api/result":
            try:
                filename = safe_filename(parse_qs(parsed.query).get("filename", [""])[0])
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            output_dir = output_dir_for_filename(filename)
            analysis = read_json(output_dir / "analysis.json")
            social_context = read_json(output_dir / "social_context.json")
            return json_response(
                self,
                HTTPStatus.OK,
                {
                    "filename": filename,
                    "status": "saved",
                    "output_dir": str(output_dir.relative_to(ROOT)),
                    "analysis_mode": mode_from_analysis(analysis),
                    "analysis": analysis,
                    "analysis_zh": read_json(output_dir / "analysis_zh.json"),
                    "direct_analysis": read_json(output_dir / "direct_analysis.json"),
                    "direct_analysis_zh": read_json(output_dir / "direct_analysis_zh.json"),
                    "audit_result": read_json(output_dir / "audit_result.json"),
                    "audit_result_zh": read_json(output_dir / "audit_result_zh.json"),
                    "feedback_result": read_json(output_dir / "feedback_result.json"),
                    "feedback_result_zh": read_json(output_dir / "feedback_result_zh.json"),
                    "social_context": social_context,
                    "social_insights": read_json(output_dir / "social_insights.json"),
                    "log": [],
                },
            )
        if parsed.path == "/api/social-context":
            try:
                filename = safe_filename(parse_qs(parsed.query).get("filename", [""])[0])
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            output_dir = output_dir_for_filename(filename)
            return json_response(self, HTTPStatus.OK, {
                "filename": filename,
                "social_context": read_json(output_dir / "social_context.json"),
                "social_insights": read_json(output_dir / "social_insights.json"),
            })
        if parsed.path == "/api/export-pdf":
            query = parse_qs(parsed.query)
            try:
                filename = safe_filename(query.get("filename", [""])[0])
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            tab = query.get("tab", ["audit"])[0]
            if tab not in {"audit", "content", "direct", "feedback", "comments", "data", "creator"}:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid tab"})
            output_dir = output_dir_for_filename(filename)
            sources = {
                "content": ("analysis_zh.json", "analysis.json"),
                "direct": ("direct_analysis_zh.json", "direct_analysis.json"),
                "audit": ("audit_result_zh.json", "audit_result.json"),
                "feedback": ("feedback_result_zh.json", "feedback_result.json"),
            }
            if tab in sources:
                source, fallback = sources[tab]
                payload = read_json(output_dir / source) or read_json(output_dir / fallback)
            else:
                payload = social_tab_payload(filename, tab)
            if not isinstance(payload, dict):
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": f"Report not found for {filename}"})
            try:
                html = build_report_html(filename, tab, payload)
                pdf = render_pdf_bytes(html)
            except Exception as exc:
                return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"PDF export failed: {exc}"})
            suffix = {"content": "analysis", "direct": "direct_analysis", "audit": "audit", "feedback": "feedback", "comments": "comments", "data": "data", "creator": "creator"}[tab]
            return binary_response(
                self,
                HTTPStatus.OK,
                pdf,
                "application/pdf",
                filename=f"{filename}.{suffix}.pdf",
            )
        return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def stream_job_events(self, job_id: str) -> None:
        self.stream_events(job_id, jobs_lock, jobs, public_job, "Job not found")

    def stream_download_events(self, job_id: str) -> None:
        self.stream_events(job_id, download_jobs_lock, download_jobs, public_download_job, "Download job not found")

    def stream_shop_events(self, job_id: str) -> None:
        self.stream_events(job_id, shop_jobs_lock, shop_jobs, public_shop_job, "TikTok Shop job not found")

    def stream_metrics_events(self, job_id: str) -> None:
        self.stream_events(job_id, metrics_jobs_lock, metrics_jobs, public_metrics_job, "Video metrics job not found")

    def stream_amazon_events(self, job_id: str) -> None:
        self.stream_events(job_id, amazon_jobs_lock, amazon_jobs, public_amazon_job, "Amazon job not found")

    def stream_report_events(self, report_date: str | None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_marker: tuple[Any, ...] | None = None
        while True:
            payload = get_report_progress(report_date)
            marker = (
                payload.get("status"),
                payload.get("stage"),
                payload.get("progress"),
                payload.get("message"),
                payload.get("updated_at"),
            )
            try:
                if marker != last_marker:
                    write_sse_event(self, payload)
                    last_marker = marker
                if payload.get("status") not in {"queued", "running"}:
                    self.close_connection = True
                    return
                time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return

    def stream_events(self, job_id: str, lock: threading.Lock, store: dict[str, Any], serializer: Any, missing_message: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_marker: tuple[Any, ...] | None = None
        while True:
            with lock:
                job = store.get(job_id)
                payload = serializer(job) if job else None

            if payload is None:
                try:
                    write_sse_event(self, {"status": "missing", "error": missing_message})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return

            marker = (
                payload.get("status"),
                payload.get("updated_at"),
                len(payload.get("log") or []),
                payload.get("error"),
            )
            try:
                if marker != last_marker:
                    write_sse_event(self, payload)
                    last_marker = marker
                if payload.get("status") not in {"queued", "running"}:
                    self.close_connection = True
                    return
                time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return

    def serve_static_asset(self, relative_path: str) -> None:
        asset_root = (SCRIPTS_DIR / "static" / "assets").resolve()
        asset_path = (asset_root / unquote(relative_path)).resolve()
        if asset_path != asset_root and asset_root not in asset_path.parents:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid asset path"})
        if not asset_path.is_file():
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Asset not found"})
        content_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
        return binary_response(self, HTTPStatus.OK, asset_path.read_bytes(), content_type)

    def serve_video(self, path: Path) -> None:
        if not path.is_file():
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Video not found"})

        file_size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK

        if range_header and range_header.startswith("bytes="):
            status = HTTPStatus.PARTIAL_CONTENT
            range_value = range_header.removeprefix("bytes=").split(",", 1)[0]
            start_text, _, end_text = range_value.partition("-")
            if start_text:
                start = int(start_text)
            if end_text:
                end = int(end_text)
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with path.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/amazon/api/chat/export-pdf":
            return self.handle_sellersprite_chat_export_pdf()
        if parsed.path.startswith("/amazon/"):
            return proxy_sellersprite_chat(self)
        if parsed.path == "/api/upload":
            return self.handle_upload()
        if parsed.path == "/api/download":
            return self.handle_download()
        if parsed.path == "/api/chat/ask":
            return self.handle_chat_ask()
        if parsed.path == "/api/chat/export-pdf":
            return self.handle_chat_export_pdf()
        if parsed.path == "/api/chat/tool-config":
            return self.handle_chat_tool_config()
        if parsed.path == "/api/shop-extract":
            return self.handle_shop_extract()
        if parsed.path == "/api/video-metrics":
            return self.handle_video_metrics()
        if parsed.path == "/api/report/run":
            return self.handle_report_run()
        if parsed.path == "/api/report/delete":
            return self.handle_report_delete()
        if parsed.path == "/api/report/settings":
            return self.handle_report_settings()
        if parsed.path == "/api/report/translate":
            return self.handle_report_translate()
        if parsed.path == "/api/report/backfill-covers":
            result = backfill_cover_urls()
            return json_response(self, HTTPStatus.OK, result)
        if parsed.path == "/api/amazon-scrape":
            return self.handle_amazon_scrape()
        if parsed.path == "/api/analyze":
            return self.handle_analyze()
        if parsed.path == "/api/postprocess":
            return self.handle_postprocess()
        if parsed.path == "/api/translate":
            return self.handle_translate()
        if parsed.path == "/api/feedback":
            return self.handle_feedback()
        if parsed.path == "/api/social-context/refresh":
            return self.handle_social_context_refresh()
        if parsed.path == "/api/social-insights":
            return self.handle_social_insights()
        if parsed.path == "/api/prompt":
            return self.handle_save_prompt()
        if parsed.path == "/api/delete":
            return self.handle_delete()
        return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/amazon/"):
            return proxy_sellersprite_chat(self)
        return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def handle_report_run(self) -> None:
        try:
            recover_interrupted_reports()
            payload = enqueue_report()
            payload["report"] = get_report(include_raw=False, detail=False)
            return json_response(self, HTTPStatus.ACCEPTED, payload)
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_report_delete(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            result = delete_report(str(payload.get("date") or payload.get("report_date") or ""))
            return json_response(self, HTTPStatus.OK, result)
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_report_settings(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            settings = save_report_settings(payload)
            return json_response(self, HTTPStatus.OK, settings)
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_report_translate(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            result = translate_report_video_analysis(
                str(payload.get("date") or payload.get("report_date") or ""),
                str(payload.get("platform") or ""),
                str(payload.get("video_id") or ""),
            )
            return json_response(self, HTTPStatus.OK, result)
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_download(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        attempted_url = ""
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            attempted_url = str(payload.get("url", ""))
            url = validate_short_video_url(attempted_url)
        except (json.JSONDecodeError, ValueError) as exc:
            job = DownloadJob(id=str(uuid.uuid4()), url=attempted_url, status="failed")
            job.error = str(exc)
            job.log.append(str(exc))
            with download_jobs_lock:
                download_jobs[job.id] = job
                write_download_job_log(job)
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = DownloadJob(id=str(uuid.uuid4()), url=url)
        with download_jobs_lock:
            download_jobs[job.id] = job
        thread = threading.Thread(target=run_download_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_download_job(job))

    def handle_shop_extract(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            url = str(payload.get("url", "")).strip()
            source_type = str(payload.get("source_type") or "product")
            region = str(payload.get("region") or os.getenv("SOCIAVAULT_REGION", "US")).strip().upper()
            max_pages = int(payload.get("max_pages") or os.getenv("SOCIAVAULT_MAX_PAGES", "1"))
            review_pages = int(payload.get("review_pages") or os.getenv("SOCIAVAULT_REVIEW_PAGES", "1"))
            prompt = str(payload.get("prompt") or "").strip()
            analyze = bool(payload.get("analyze", True))
            related_videos = bool(payload.get("related_videos", False))
            if source_type not in {"product", "details", "reviews", "shop", "search"}:
                raise ValueError("source_type must be product, details, reviews, shop, or search")
            if not url or len(url) > 2048:
                raise ValueError("A TikTok Shop URL is required")
            if max_pages < 1 or max_pages > 20:
                raise ValueError("max_pages must be between 1 and 20")
            if review_pages < 0 or review_pages > 20:
                raise ValueError("review_pages must be between 0 and 20")
            if len(prompt) > 6000:
                raise ValueError("prompt is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = ShopJob(
            id=str(uuid.uuid4()),
            url=url,
            source_type=source_type,
            region=region,
            max_pages=max_pages,
            review_pages=review_pages,
            analyze=analyze,
            related_videos=related_videos,
            prompt=prompt,
        )
        with shop_jobs_lock:
            shop_jobs[job.id] = job
        thread = threading.Thread(target=run_shop_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_shop_job(job))

    def handle_video_metrics(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            target = str(payload.get("target", "")).strip()
            endpoint = str(payload.get("endpoint", "video-info")).strip()
            if endpoint not in TIKTOK_ENDPOINTS:
                raise ValueError(f"Unknown endpoint: {endpoint}")
            if not target and endpoint not in ("trending", "music-popular"):
                raise ValueError("target is required for this endpoint")
            if len(target) > 2048:
                raise ValueError("target is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = MetricsJob(id=str(uuid.uuid4()), target=target, endpoint=endpoint)
        with metrics_jobs_lock:
            metrics_jobs[job.id] = job
        thread = threading.Thread(target=run_metrics_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_metrics_job(job))

    def handle_amazon_scrape(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            target = str(payload.get("target", "")).strip()
            target_type = str(payload.get("target_type") or "url").strip()
            max_pages = int(os.getenv("AMAZON_MAX_PAGES", "1") or "1")
            max_pages = max(1, min(max_pages, 5))
            pages = int(payload.get("pages") or max_pages)
            if pages < 1 or pages > 5:
                raise ValueError("pages must be between 1 and 5")
            url = amazon_url_for_target(target, target_type)
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = AmazonJob(
            id=str(uuid.uuid4()),
            target=target,
            target_type=target_type,
            url=url,
            pages=pages,
        )
        with amazon_jobs_lock:
            amazon_jobs[job.id] = job
        thread = threading.Thread(target=run_amazon_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_amazon_job(job))

    def handle_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid upload size"})

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )
        try:
            raw_file_items = form["video"]
        except KeyError:
            raw_file_items = []
        if not isinstance(raw_file_items, list):
            raw_file_items = [raw_file_items]
        file_items = [item for item in raw_file_items if getattr(item, "filename", None)]
        if not file_items:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Missing video file"})

        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        files = []
        errors = []
        for file_item in file_items:
            original_name = str(getattr(file_item, "filename", ""))
            try:
                filename = safe_filename(original_name)
                target = VIDEOS_DIR / filename
                with target.open("wb") as file:
                    shutil.copyfileobj(file_item.file, file)
                ensure_analyzer_media_or_delete(target)
                files.append({"filename": filename, "size": target.stat().st_size})
                start_social_context_job(filename, generate_insights=False)
            except Exception as exc:
                errors.append({"filename": original_name, "error": str(exc)})

        status = HTTPStatus.OK if files else HTTPStatus.BAD_REQUEST
        return json_response(self, status, {"files": files, "errors": errors})

    def handle_analyze(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
            postprocess = bool(payload.get("postprocess", False))
            reset_output = bool(payload.get("reset_output", False))
            analysis_mode = str(payload.get("analysis_mode") or os.getenv("ANALYSIS_MODE", "analyzer"))
            analysis_prompt = str(payload.get("analysis_prompt") or "").strip()
            if analysis_mode not in {"analyzer", "direct_video"}:
                raise ValueError("analysis_mode must be analyzer or direct_video")
            if len(analysis_prompt) > 12000:
                raise ValueError("analysis_prompt is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        if not (VIDEOS_DIR / filename).is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"Video file not found: {filename}"})

        output_dir = output_dir_for_filename(filename)
        if reset_output:
            if analysis_mode == "direct_video":
                output_dir.mkdir(parents=True, exist_ok=True)
                for output_name in ("direct_analysis.json", "direct_analysis_zh.json"):
                    output_path = output_dir / output_name
                    if output_path.is_file():
                        output_path.unlink()
            elif output_dir.is_dir():
                shutil.rmtree(output_dir)

        # Save user prompt to file so queue executor can use it
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "analysis_mode.txt").write_text(analysis_mode, encoding="utf-8")
        if analysis_prompt:
            (output_dir / "analysis_prompt.txt").write_text(analysis_prompt, encoding="utf-8")

        video_queue.enqueue(filename, "analyze")
        return json_response(self, HTTPStatus.ACCEPTED, {"status": "queued", "filename": filename})

    def handle_postprocess(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
            analysis_prompt = str(payload.get("analysis_prompt") or "").strip()
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        output_dir = output_dir_for_filename(filename)
        if not (output_dir / "analysis.json").is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"analysis.json not found for {filename}"})

        # Save user prompt for DeepSeek report
        if analysis_prompt:
            (output_dir / "analysis_prompt.txt").write_text(analysis_prompt, encoding="utf-8")

        for report_name in ("audit_result.json", "audit_result_zh.json"):
            report_path = output_dir / report_name
            if report_path.is_file():
                report_path.unlink()

        video_queue.enqueue(filename, "report")
        return json_response(self, HTTPStatus.ACCEPTED, {"status": "queued", "filename": filename})

    def handle_translate(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
            tab = str(payload.get("tab") or "").strip()
            if tab not in {"content", "direct", "audit", "feedback"}:
                raise ValueError("tab must be content, direct, audit, or feedback")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        output_dir = output_dir_for_filename(filename)
        files = {
            "content": ("analysis.json", "analysis_zh.json"),
            "direct": ("direct_analysis.json", "direct_analysis_zh.json"),
            "audit": ("audit_result.json", "audit_result_zh.json"),
            "feedback": ("feedback_result.json", "feedback_result_zh.json"),
        }
        source_name, output_name = files[tab]
        source_path = output_dir / source_name
        output_path = output_dir / output_name
        if not source_path.is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"{source_name} not found for {filename}"})

        try:
            subprocess.run(
                [
                    "python",
                    str(SCRIPTS_DIR / "translate_analysis.py"),
                    str(source_path),
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                check=True,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": message or "Translation failed"})

        return json_response(self, HTTPStatus.OK, {"status": "translated", "filename": filename, "tab": tab})

    def handle_feedback(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
            feedback_prompt = str(payload.get("feedback_prompt") or "").strip()
            if len(feedback_prompt) > 12000:
                raise ValueError("feedback_prompt is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        output_dir = output_dir_for_filename(filename)
        if not (output_dir / "analysis.json").is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"analysis.json not found for {filename}"})
        if not (output_dir / "audit_result.json").is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"audit_result.json not found for {filename}"})

        cmd = [
            "python",
            str(SCRIPTS_DIR / "deepseek_feedback.py"),
            str(output_dir),
            "--output",
            str(output_dir / "feedback_result.json"),
        ]
        if feedback_prompt:
            cmd.extend(["--prompt", feedback_prompt])
        try:
            subprocess.run(
                cmd,
                cwd=ROOT,
                check=True,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": message or "Feedback generation failed"})

        return json_response(self, HTTPStatus.OK, {"status": "generated", "filename": filename})

    def handle_social_context_refresh(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        if not (VIDEOS_DIR / filename).is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"Video file not found: {filename}"})
        started = start_social_context_job(filename, generate_insights=True)
        output_dir = output_dir_for_filename(filename)
        return json_response(self, HTTPStatus.ACCEPTED, {
            "status": "queued" if started else "running",
            "filename": filename,
            "social_context": read_json(output_dir / "social_context.json"),
            "social_insights": read_json(output_dir / "social_insights.json"),
        })

    def handle_social_insights(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        try:
            insights = generate_social_insights(filename)
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": message or "Social insights generation failed"})
        except Exception as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return json_response(self, HTTPStatus.OK, {"status": "generated", "filename": filename, "social_insights": insights})

    def handle_save_prompt(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            kind = str(payload.get("kind") or "analysis").strip()
            if "feedback_prompt" in payload:
                kind = "feedback"
                text = str(payload.get("feedback_prompt") or "").strip()
            else:
                text = str(payload.get("prompt", "")).strip()
            if not text:
                raise ValueError("prompt is required")
            if len(text) > MAX_PROMPT_CHARS:
                raise ValueError(f"prompt is too long; max {MAX_PROMPT_CHARS} characters")
            if kind == "feedback":
                save_feedback_prompt(text)
                return json_response(self, HTTPStatus.OK, {"status": "saved", "kind": "feedback"})
            save_prompt(text)
            return json_response(self, HTTPStatus.OK, {"status": "saved", "kind": "analysis"})
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def handle_chat_ask(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            session_id = str(payload.get("sessionId", "default")).strip() or "default"
            text = str(payload.get("message", "")).strip()
            if not text:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "消息不能为空"})
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        session = chat_store.get_or_create(session_id)
        user_msg = Message(id=str(uuid.uuid4()), role="user", content=text)
        chat_store.add_message(session, user_msg)
        if not session.title:
            session.title = text[:40] + ("..." if len(text) > 40 else "")

        assistant_msg = Message(id=str(uuid.uuid4()), role="assistant", content="", status="pending")
        chat_store.add_message(session, assistant_msg)

        thread = threading.Thread(target=run_chat_deepseek, args=(session, assistant_msg, text), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, {
            "sessionId": session_id,
            "userMessage": {"id": user_msg.id, "role": "user", "content": user_msg.content, "status": user_msg.status, "created_at": user_msg.created_at},
            "message": {"id": assistant_msg.id, "role": "assistant", "content": "", "status": "pending"},
        })

    def handle_chat_export_pdf(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            session_id = str(payload.get("sessionId", "")).strip()
            message_id = str(payload.get("messageId", "")).strip()
            if not session_id or not message_id:
                raise ValueError("sessionId and messageId are required")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        session = chat_store.get_session(session_id)
        if not session:
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Session not found"})

        message = next((m for m in session.messages if m.id == message_id), None)
        if not message:
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Message not found"})
        if message.role != "assistant":
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Only assistant replies can be exported"})
        if str(message.status or "done").lower() in {"pending", "error"} or not str(message.content or "").strip():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Assistant reply is not ready"})

        try:
            html = build_chat_reply_pdf_html(message)
            pdf = render_pdf_bytes(html)
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"PDF export failed: {exc}"})

        stamp = time.strftime("%Y%m%d-%H%M%S")
        return binary_response(
            self,
            HTTPStatus.OK,
            pdf,
            "application/pdf",
            filename=f"chat-reply-{stamp}.pdf",
        )

    def handle_sellersprite_chat_export_pdf(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            session_id = str(payload.get("sessionId", "")).strip()
            message_id = str(payload.get("messageId", "")).strip()
            if not session_id or not message_id:
                raise ValueError("sessionId and messageId are required")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        sessions_path = SELLERSPRITE_CHAT_DATA_DIR / "sessions.json"
        try:
            stored = json.loads(sessions_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Session data not found"})
        except json.JSONDecodeError as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Session data is invalid: {exc}"})

        sessions = stored if isinstance(stored, list) else stored.get("sessions", [])
        session = next((item for item in sessions if str(item.get("id", "")) == session_id), None)
        if not session:
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Session not found"})
        messages = session.get("messages") if isinstance(session, dict) else []
        message = next((item for item in messages or [] if str(item.get("id", "")) == message_id), None)
        if not message:
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Message not found"})
        if message.get("role") != "assistant":
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Only assistant replies can be exported"})
        if str(message.get("status") or "done").lower() in {"pending", "error"} or not str(message.get("content") or "").strip():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Assistant reply is not ready"})

        created_raw = message.get("createdAt")
        try:
            created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00")).timestamp() if created_raw else time.time()
        except ValueError:
            created_at = time.time()

        try:
            html = build_chat_reply_pdf_html(Message(
                id=message_id,
                role="assistant",
                content=str(message.get("content") or ""),
                status=str(message.get("status") or "done"),
                created_at=created_at,
            ))
            pdf = render_pdf_bytes(html)
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"PDF export failed: {exc}"})

        stamp = time.strftime("%Y%m%d-%H%M%S")
        return binary_response(
            self,
            HTTPStatus.OK,
            pdf,
            "application/pdf",
            filename=f"sellersprite-reply-{stamp}.pdf",
        )

    def handle_chat_tool_config(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            enabled = payload.get("enabled")
            global chat_tool_config
            if enabled is None:
                chat_tool_config = None
            elif isinstance(enabled, list):
                chat_tool_config = set(enabled)
            return json_response(self, HTTPStatus.OK, {"status": "saved"})
        except Exception as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def stream_status_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        video_queue.register_sse(self)
        try:
            write_sse_event(self, {"type": "status_update", "queue": video_queue.get_queue_state(), "progress": video_queue.get_progress()})
            while not self.wfile.closed:
                time.sleep(15)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            video_queue.unregister_sse(self)
            self.close_connection = True

    def stream_chat_events(self, session_id: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        chat_store.register_sse(session_id, self)
        try:
            while not self.wfile.closed:
                time.sleep(5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            chat_store.unregister_sse(session_id, self)
            self.close_connection = True

    def handle_delete(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        video_path = VIDEOS_DIR / filename
        output_dir = OUTPUT_DIR / filename
        deleted_video = False
        deleted_output = False
        if video_path.is_file():
            video_path.unlink()
            deleted_video = True
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
            deleted_output = True

        with jobs_lock:
            for job_id in [job_id for job_id, job in jobs.items() if job.filename == filename]:
                del jobs[job_id]

        return json_response(
            self,
            HTTPStatus.OK,
            {
                "filename": filename,
                "deleted_video": deleted_video,
                "deleted_output": deleted_output,
            },
        )


class SellerSpriteRedirectHandler(BaseHTTPRequestHandler):
    server_version = "SellerSpriteRedirect/1.0"
    target_port: int = 4000

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _target_location(self) -> str:
        parsed = urlparse(self.path)
        host = self.headers.get("Host", "localhost")
        if host.startswith("["):
            hostname = host.split("]", 1)[0] + "]"
        else:
            hostname = host.split(":", 1)[0]

        if parsed.path == "/" or parsed.path == "":
            target_path = "/amazon"
        elif parsed.path.startswith("/amazon"):
            target_path = parsed.path
        elif parsed.path == "/api" or parsed.path.startswith("/api/"):
            target_path = "/amazon" + parsed.path
        else:
            target_path = "/amazon" + parsed.path

        query = f"?{parsed.query}" if parsed.query else ""
        return f"http://{hostname}:{self.target_port}{target_path}{query}"

    def _redirect(self) -> None:
        status = HTTPStatus.FOUND if self.command in {"GET", "HEAD"} else HTTPStatus.TEMPORARY_REDIRECT
        self.send_response(status)
        self.send_header("Location", self._target_location())
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        self._redirect()

    def do_HEAD(self) -> None:
        self._redirect()

    def do_POST(self) -> None:
        self._redirect()

    def do_DELETE(self) -> None:
        self._redirect()


METRICS_HTML = (SCRIPTS_DIR / "static" / "metrics.html").read_text(encoding="utf-8")

INDEX_HTML = '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Short Video Analyzer</title>\n<style>\n:root{--bg:#eef3f8;--card:#fff;--soft:#f7f9fc;--line:#d7e0ec;--text:#142033;--muted:#607089;--blue:#2563eb;--blue2:#1d4ed8;--blueSoft:#eaf1ff;--red:#b42318;--green:#087443;--dark:#0d1628;--shadow:0 18px 45px rgba(15,23,42,.10)}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,rgba(37,99,235,.10),transparent 34%),var(--bg);color:var(--text);font-family:"Segoe UI",system-ui,sans-serif}header{height:66px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.92);position:sticky;top:0;z-index:5}h1{font-size:20px;margin:0}.page{display:none;min-height:calc(100vh - 66px);padding:18px}.page.active{display:block}.grid{display:grid;grid-template-columns:minmax(320px,430px) minmax(0,1fr);gap:18px}.detail-grid{display:grid;grid-template-columns:minmax(260px,360px) minmax(0,1fr);gap:18px;height:calc(100vh - 102px)}.card{border:1px solid var(--line);border-radius:12px;background:var(--card);box-shadow:var(--shadow);overflow:hidden}.stack{display:grid;gap:16px;padding:18px}.title{font-weight:800;margin:0 0 10px}label{display:block;margin-bottom:7px;color:var(--muted);font-size:13px;font-weight:650}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--text);outline:none}input,select{min-height:40px;padding:8px 11px}textarea{min-height:170px;padding:10px 12px;resize:vertical;font:13px/1.55 Consolas,monospace}button{min-height:40px;border:1px solid var(--blue);border-radius:9px;background:var(--blue);color:#fff;padding:8px 13px;font-weight:750;cursor:pointer;box-shadow:0 8px 18px rgba(37,99,235,.18)}button.secondary{background:#fff;color:var(--blue);box-shadow:none}button.danger{background:#fff;border-color:#fecaca;color:var(--red);box-shadow:none}button.small{min-height:32px;padding:5px 10px;font-size:13px}button:disabled{opacity:.55;cursor:not-allowed}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.muted{color:var(--muted)}.status{min-height:42px;border:1px solid var(--line);border-radius:9px;padding:10px 12px;background:var(--soft);color:var(--muted);font-size:13px;overflow-wrap:anywhere}.status.ok{background:#ecfdf3;color:var(--green)}.status.bad{background:#fff1f2;color:var(--red)}.check{display:flex;align-items:center;gap:9px;color:var(--text);font-size:14px;font-weight:650}.check input{width:auto;min-height:auto}.prompt{display:none}.prompt.active{display:block}.log-wrap{display:grid;grid-template-rows:auto minmax(360px,1fr);min-height:calc(100vh - 102px)}.head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 18px;border-bottom:1px solid var(--line);background:#fff}.head h2{margin:0;font-size:18px}.log{margin:0;overflow:auto;padding:18px;background:var(--dark);color:#e6edf7;font:13px/1.7 Consolas,monospace;white-space:pre-wrap;word-break:break-word}.files{display:grid;gap:8px;max-height:260px;overflow:auto}.detail-files{padding:14px;overflow:auto}.file{display:flex;justify-content:space-between;align-items:center;gap:12px;border:1px solid var(--line);border-radius:9px;padding:10px;background:#fff;cursor:pointer}.file.selected{border-color:var(--blue);background:var(--blueSoft)}.file-name{font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-meta{min-width:0;display:grid;gap:4px}.file-actions{display:flex;gap:6px}.tabs{display:flex;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line)}.tab{background:#fff;color:var(--text);border-color:var(--line);box-shadow:none}.tab.active{color:var(--blue);border-color:var(--blue);background:var(--blueSoft)}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line);background:var(--soft)}.out{min-height:0;overflow:auto;padding:22px 24px;border-left:4px solid rgba(37,99,235,.22);white-space:pre-wrap;word-break:break-word;line-height:1.75}.out.raw{background:var(--dark);color:#e6edf7;font-family:Consolas,monospace}.report{display:grid;gap:14px;max-width:1180px}.hero,.section,.metric{border:1px solid var(--line);border-radius:12px;background:#fff}.hero{padding:18px 20px;background:linear-gradient(135deg,rgba(37,99,235,.10),transparent 42%),#fff}.hero h2{margin:4px 0;font-size:22px}.hero p{margin:0;color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.metric{padding:10px 12px}.metric span{display:block;color:var(--muted);font-size:12px;font-weight:750}.metric strong{display:block;margin-top:5px}.section h3{margin:0;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--soft);font-size:15px}.section div{padding:14px 16px}.drop{position:fixed;inset:14px;z-index:20;display:none;align-items:center;justify-content:center;border:2px dashed rgba(37,99,235,.55);border-radius:18px;background:rgba(239,246,255,.86);color:var(--blue2);pointer-events:none}.drop.active{display:flex}.drop>div{padding:26px 30px;border-radius:14px;background:#fff;text-align:center}@media(max-width:900px){.grid,.detail-grid{grid-template-columns:1fr;height:auto}.log-wrap{min-height:520px}.card.result{height:72vh;min-height:520px}}\n</style>\n</head>\n<body>\n<div id="drop" class="drop"><div><strong>??????</strong><br><span class="muted">??????????</span></div></div>\n<header><h1>Short Video Analyzer</h1><div id="current" class="muted">??</div></header>\n<main id="home" class="page active"><div class="grid"><section class="card stack">\n<div><p class="title">TikTok / ??????</p><label>??????</label><input id="url" type="url" placeholder="https://www.tiktok.com/@user/video/... ? https://v.douyin.com/..."></div><div class="row"><button id="download">????</button><button id="network" class="secondary">??????</button></div>\n<div><p class="title">??????</p><label>??? videos/</label><input id="videoFile" type="file" accept="video/*" multiple></div><div class="row"><button id="upload">??</button><button id="refresh" class="secondary">????</button></div>\n<div><p class="title">?????</p><div id="homeFiles" class="files"></div></div>\n<div><p class="title">????</p><label>????</label><select id="mode"><option value="analyzer">????????video-analyzer?</option><option value="direct_video">?????????Qwen?</option></select></div><button id="promptBtn" class="secondary">???????</button><div id="promptPanel" class="prompt"><label>?????</label><textarea id="prompt"></textarea></div>\n<label class="check"><input id="autoPost" type="checkbox">???? DeepSeek ??</label><div class="row"><button id="analyze" disabled>????</button><button id="post" class="secondary" disabled>????????</button></div><div id="status" class="status">?????????????</div>\n</section><section class="card log-wrap"><div class="head"><div><h2>????</h2><div class="muted">?????????????????????</div></div><button id="clearLog" class="secondary small">????</button></div><pre id="log" class="log">????...</pre></section></div></main>\n<main id="detail" class="page"><div class="detail-grid"><section class="card" style="display:grid;grid-template-rows:auto 1fr"><div class="head" style="display:grid"><button id="back" class="secondary">????</button><div><h2>?????</h2><div class="muted">???????</div></div></div><div id="detailFiles" class="detail-files files"></div></section><section class="card result" style="display:grid;grid-template-rows:auto auto minmax(0,1fr)"><div class="tabs"><button class="tab active" data-tab="content">????????</button><button class="tab" data-tab="audit">????????</button></div><div class="toolbar"><b id="outTitle">Qwen ?????DeepSeek ??</b><div class="row"><button id="source" class="secondary small">????</button><button id="json" class="secondary small">???? JSON</button></div></div><div id="out" class="out">{}</div></section></div></main>\n<script>\nwindow.DEFAULT_ANALYSIS_MODE="__DEFAULT_ANALYSIS_MODE__";\nconst S={file:"",files:[],result:null,job:null,tab:"content",raw:false,has:false,logs:[]};\nconst $=id=>document.getElementById(id), home=$(\'home\'), detail=$(\'detail\'), current=$(\'current\'), status=$(\'status\'), log=$(\'log\'), out=$(\'out\'); let de=null, je=null, drag=0;\nfunction esc(v){return String(v??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]))} function pretty(v){return v==null?\'{}\':typeof v===\'string\'?v:JSON.stringify(v,null,2)} function clean(v){let s=typeof v===\'string\'?v:(v&&typeof v.response===\'string\'?v.response:pretty(v));return s.replace(/^```(?:json)?\\s*/i,\'\').replace(/\\s*```$/i,\'\').trim()} function bytes(n){return `${Math.round(Number(n||0)/1024/1024*10)/10} MB`} function setStatus(m,k=\'\'){status.className=\'status \'+k;status.textContent=m} function addLog(m){S.logs.push(`[${new Date().toLocaleTimeString()}] ${m}`);if(S.logs.length>500)S.logs.splice(0,S.logs.length-500);log.textContent=S.logs.join(\'\\n\')||\'????...\';log.scrollTop=log.scrollHeight}\nfunction metric(k,v){return v==null||v===\'\'?\'\':`<div class="metric"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`} function sec(t,b){b=clean(b);return b?`<section class="section"><h3>${esc(t)}</h3><div>${esc(b)}</div></section>`:\'\'} function list(t,a,map=x=>x){if(!Array.isArray(a)||!a.length)return\'\';return `<section class="section"><h3>${esc(t)}</h3><div>${a.map((x,i)=>`- ${esc(clean(map(x,i)))}`).join(\'\\n\')}</div></section>`} function has(r){return !!(r&&(r.analysis||r.analysis_zh||r.audit_result||r.audit_result_zh))}\nfunction extraction(v){if(!v||typeof v!==\'object\')return pretty(v);const md=v.metadata||{},tr=v.transcript||{},u=v.usage||{},tl=Array.isArray(v.timeline)?v.timeline:[],ve=Array.isArray(v.visual_evidence)?v.visual_evidence:[],fa=Array.isArray(v.frame_analyses)?v.frame_analyses:[];return `<article class="report"><div class="hero"><small>Qwen Video Extraction</small><h2>??????</h2><p>${esc(clean(v.summary)||\'?????????????????????\')}</p></div><div class="metrics">${metric(\'????\',v.processing_mode)}${metric(\'????\',v.vision_model||md.model)}${metric(\'????\',v.audio_mode)}${metric(\'????\',md.frames_processed||md.frames_extracted)}${metric(\'????\',tr.language||md.audio_language)}${metric(\'?? Tokens\',u.input_tokens)}${metric(\'?? Tokens\',u.output_tokens)}${metric(\'? Tokens\',u.total_tokens)}${metric(\'API ??\',u.api_calls)}${metric(\'???\',u.elapsed_seconds==null?\'\':u.elapsed_seconds+\'s\')}</div>${sec(\'????\',v.summary)}${sec(\'??????\',v.video_description)}${list(\'???\',tl,x=>typeof x===\'string\'?x:`${x.time_range||x.timestamp||\'\'}\\n${x.visual||\'\'}\\n${x.audio||\'\'}`)}${list(\'????\',ve,x=>typeof x===\'string\'?x:(x.description||x.visual||pretty(x)))}${list(\'??????\',fa,(x,i)=>`[? ${i+1}]\\n${clean(x)}`)}${sec(\'????\',tr.text||\'?????\')}</article>`}\nfunction audit(v){if(!v||typeof v!==\'object\')return pretty(v);return `<article class="report"><div class="hero"><small>DeepSeek Audit</small><h2>??????</h2><p>${esc(v.summary||\'?????????????????\')}</p></div><div class="metrics">${metric(\'????\',v.risk_level)}${metric(\'????\',v.recommended_action)}${metric(\'????\',v.publish_suggestion)}</div>${sec(\'????\',v.summary)}${sec(\'????\',v.content_overview)}${sec(\'????\',v.transcript_notes)}${sec(\'????\',v.visual_notes)}${list(\'????\',v.risk_reasons)}${list(\'???\',v.issues)}</article>`}\nfunction renderOut(r){S.result=r;out.className=S.raw?\'out raw\':\'out\';let v;if(S.tab===\'content\'){v=S.raw?r?.analysis:(r?.analysis_zh||r?.analysis);$(\'json\').style.display=\'inline-flex\';$(\'outTitle\').textContent=S.raw?\'Qwen ??????? JSON\':\'Qwen ?????DeepSeek ??\';S.raw?out.textContent=pretty(v):out.innerHTML=extraction(v)}else{v=S.raw?r?.audit_result:(r?.audit_result_zh||r?.audit_result);$(\'json\').style.display=\'none\';$(\'outTitle\').textContent=S.raw?\'DeepSeek ???????\':\'DeepSeek ???????\';S.raw?out.textContent=pretty(v):out.innerHTML=audit(v)}$(\'source\').textContent=S.raw?\'????\':\'????\'}\nfunction buttons(){ $(\'analyze\').textContent=S.has?\'????\':\'????\'; $(\'analyze\').disabled=!S.file; $(\'post\').disabled=!S.file||!S.has||!!S.job }\nfunction renderFiles(){for(const [id,detailMode] of [[\'homeFiles\',false],[\'detailFiles\',true]]){const box=$(id);box.innerHTML=\'\';if(!S.files.length){box.innerHTML=\'<div class="muted">videos/ ??????</div>\';continue}S.files.forEach(f=>{const el=document.createElement(\'div\');el.className=\'file\'+(f.name===S.file?\' selected\':\'\');el.innerHTML=`<span class="file-meta"><span class="file-name">${esc(f.name)}</span><span class="muted">${bytes(f.size)}</span></span>${detailMode?\'\':`<span class="file-actions"><button class="secondary small">??</button><button class="danger small">??</button></span>`}`;el.onclick=()=>toDetail(f.name);if(!detailMode){const b=el.querySelectorAll(\'button\');b[0].onclick=e=>{e.stopPropagation();open(\'/video/\'+encodeURIComponent(f.name),\'_blank\',\'noopener\')};b[1].onclick=e=>{e.stopPropagation();delFile(f.name)}}box.appendChild(el)})}buttons()}\nfunction view(v,f=\'\'){home.classList.toggle(\'active\',v===\'home\');detail.classList.toggle(\'active\',v===\'detail\');current.textContent=v===\'detail\'&&f?f:\'??\'} function toHome(){location.hash=\'\';view(\'home\')} function toDetail(f){location.hash=\'detail=\'+encodeURIComponent(f)} function route(){const h=location.hash.slice(1);if(h.startsWith(\'detail=\')){select(decodeURIComponent(h.slice(7)),false);view(\'detail\',S.file)}else{view(\'home\');renderFiles()}}\nasync function refresh(){const r=await fetch(\'/api/files\');S.files=await r.json();if(!Array.isArray(S.files))S.files=[];renderFiles()} async function loadResult(name){const r=await fetch(\'/api/result?filename=\'+encodeURIComponent(name)),j=await r.json();if(r.ok&&has(j)){S.result=j;S.has=true;const p=j.analysis&&j.analysis.metadata&&j.analysis.metadata.analysis_prompt;if(p)$(\'prompt\').value=p;renderOut(j);setStatus(name+\': ???????\',\'ok\')}else{S.result=null;S.has=false;out.textContent=\'{}\';setStatus(name+\': ????\')}buttons()} function select(name,openDetail=true){S.file=name;current.textContent=name||\'??\';S.has=false;renderFiles();if(name)loadResult(name).catch(e=>setStatus(e.message,\'bad\'));if(openDetail&&name)toDetail(name)}\nasync function upload(files=null){const input=$(\'videoFile\'),arr=Array.from(files||input.files||[]);if(!arr.length)return setStatus(\'????????????\',\'bad\');const bad=arr.filter(f=>!f.type.startsWith(\'video/\'));if(bad.length){addLog(\'??????????????\'+bad.map(f=>f.name).join(\', \'));return setStatus(\'????????\',\'bad\')}const form=new FormData();arr.forEach(f=>form.append(\'video\',f));addLog(`???? ${arr.length} ????`);setStatus(\'????...\');const r=await fetch(\'/api/upload\',{method:\'POST\',body:form}),p=await r.json(),ok=Array.isArray(p.files)?p.files:[],err=Array.isArray(p.errors)?p.errors:[];ok.forEach(f=>addLog(`?????${f.filename} (${bytes(f.size)})`));err.forEach(e=>addLog(`?????${e.filename||\'????\'} - ${e.error||\'????\'}`));if(!r.ok&&!ok.length)return setStatus(p.error||\'????\',\'bad\');setStatus(`??????? ${ok.length} ???? ${err.length} ?`,err.length?\'bad\':\'ok\');input.value=\'\';await refresh();if(ok.length)select(ok.at(-1).filename,false)}\nasync function delFile(name){if(!confirm(`?? ${name} ?????????`))return;const r=await fetch(\'/api/delete\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({filename:name})}),p=await r.json();if(!r.ok)return setStatus(p.error||\'????\',\'bad\');addLog(\'?????\'+name);if(S.file===name){S.file=\'\';S.result=null;S.has=false;toHome()}await refresh()}\nfunction closeD(){if(de){de.close();de=null}}function closeJ(){if(je){je.close();je=null}} function lastLog(j){return j&&Array.isArray(j.log)&&j.log.length?j.log.at(-1):\'\'}\nasync function startDownload(){const url=$(\'url\').value.trim();if(!url)return setStatus(\'??? TikTok ????????\',\'bad\');$(\'download\').disabled=true;addLog(\'???????\'+url);const r=await fetch(\'/api/download\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({url})}),j=await r.json();if(!r.ok){$(\'download\').disabled=false;return setStatus(j.error||\'????????\',\'bad\')}closeD();de=new EventSource(\'/api/download-events?id=\'+encodeURIComponent(j.id));de.onmessage=async e=>{const j=JSON.parse(e.data),l=lastLog(j);if(l)addLog(\'???\'+l);if(j.status===\'running\'||j.status===\'queued\')return setStatus(`??????${j.status}`);closeD();$(\'download\').disabled=false;if(j.status!==\'complete\')return setStatus(\'???????\'+(j.error||\'????\'),\'bad\');setStatus(j.filename+\': ????\',\'ok\');$(\'url\').value=\'\';await refresh();select(j.filename,false)};de.onerror=()=>{closeD();$(\'download\').disabled=false;setStatus(\'????????\',\'bad\')}}\nasync function checkNet(){$(\'network\').disabled=true;setStatus(\'??????????????...\');try{const r=await fetch(\'/api/network-check\'),p=await r.json();const fmt=x=>!x?\'???\':(!x.ok?\'???\'+(x.error||\'????\'):`${x.ip||\'?? IP\'} / ${x.country_name||x.country||\'????\'} / ${x.is_us?\'????\':\'?????\'}`);addLog(\'???\'+fmt(p.direct));addLog(\'???\'+fmt(p.proxy));setStatus(`???${fmt(p.direct)}????${fmt(p.proxy)}`,p.proxy&&p.proxy.ok&&p.proxy.is_us?\'ok\':\'bad\')}catch(e){setStatus(e.message,\'bad\')}finally{$(\'network\').disabled=false}}\nasync function analyze(){if(!S.file)return;$(\'analyze\').disabled=true;$(\'post\').disabled=true;const reset=S.has;addLog(`${S.file}: ${reset?\'??????????\':\'??????\'}`);const r=await fetch(\'/api/analyze\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({filename:S.file,analysis_mode:$(\'mode\').value,analysis_prompt:$(\'prompt\').value,postprocess:$(\'autoPost\').checked,reset_output:reset})}),j=await r.json();if(!r.ok){setStatus(j.error||\'????\',\'bad\');return buttons()}S.job=j.id;openJob(j.id)}\nasync function postprocess(){if(!S.file||!S.has)return;const r=await fetch(\'/api/postprocess\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({filename:S.file})}),j=await r.json();if(!r.ok)return setStatus(j.error||\'????\',\'bad\');S.tab=\'audit\';document.querySelectorAll(\'.tab\').forEach(x=>x.classList.toggle(\'active\',x.dataset.tab===\'audit\'));S.job=j.id;openJob(j.id)}\nfunction openJob(id){closeJ();je=new EventSource(\'/api/job-events?id=\'+encodeURIComponent(id));je.onmessage=e=>{const j=JSON.parse(e.data),l=lastLog(j);if(l)addLog(`${j.filename}: ${l}`);S.result=j;if(location.hash.startsWith(\'#detail=\'))renderOut(j);if(j.status===\'running\'||j.status===\'queued\')return setStatus(`${j.filename}: ${j.status}`);closeJ();S.job=null;S.has=j.status===\'complete\'||has(j);buttons();setStatus(j.status===\'complete\'?`${j.filename}: ??`:`${j.filename}: ${j.error||\'??\'}`,j.status===\'complete\'?\'ok\':\'bad\')};je.onerror=()=>{closeJ();buttons();setStatus(\'????????\',\'bad\')}}\nfunction downloadJson(){const a=S.result&&S.result.analysis;if(!a)return setStatus(\'??????? analysis.json?\',\'bad\');const name=`${S.file||\'video\'}.analysis.json`,blob=new Blob([JSON.stringify(a,null,2)],{type:\'application/json;charset=utf-8\'}),url=URL.createObjectURL(blob),link=document.createElement(\'a\');link.href=url;link.download=name;document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);addLog(\'???? JSON?\'+name)}\n$(\'download\').onclick=startDownload;$(\'network\').onclick=checkNet;$(\'upload\').onclick=()=>upload();$(\'refresh\').onclick=()=>refresh().then(()=>addLog(\'????????\'));$(\'analyze\').onclick=analyze;$(\'post\').onclick=postprocess;$(\'back\').onclick=toHome;$(\'clearLog\').onclick=()=>{S.logs=[];log.textContent=\'????...\'};$(\'source\').onclick=()=>{S.raw=!S.raw;renderOut(S.result)};$(\'json\').onclick=downloadJson;$(\'mode\').value=window.DEFAULT_ANALYSIS_MODE||\'analyzer\';$(\'promptBtn\').onclick=()=>{const p=$(\'promptPanel\');p.classList.toggle(\'active\');$(\'promptBtn\').textContent=p.classList.contains(\'active\')?\'???????\':\'???????\'};document.querySelectorAll(\'.tab\').forEach(t=>t.onclick=()=>{document.querySelectorAll(\'.tab\').forEach(x=>x.classList.remove(\'active\'));t.classList.add(\'active\');S.tab=t.dataset.tab;S.raw=false;renderOut(S.result)});addEventListener(\'hashchange\',route);addEventListener(\'dragenter\',e=>{e.preventDefault();drag++;$(\'drop\').classList.add(\'active\')});addEventListener(\'dragover\',e=>e.preventDefault());addEventListener(\'dragleave\',e=>{e.preventDefault();drag=Math.max(0,drag-1);if(!drag)$(\'drop\').classList.remove(\'active\')});addEventListener(\'drop\',e=>{e.preventDefault();drag=0;$(\'drop\').classList.remove(\'active\');if(e.dataTransfer.files.length)upload(e.dataTransfer.files)});fetch(\'/api/prompt\').then(r=>r.json()).then(p=>$(\'prompt\').value=p.prompt||\'\').catch(()=>{});refresh().then(route).catch(e=>setStatus(e.message,\'bad\'));\n</script>\n</body>\n</html>'


 
AMAZON_HTML_PATH = SCRIPTS_DIR / "static" / "amazon.html"
AMAZON_HTML = AMAZON_HTML_PATH.read_text(encoding="utf-8") if AMAZON_HTML_PATH.is_file() else ""


SHOP_HTML_PATH = SCRIPTS_DIR / "static" / "shop.html"
SHOP_HTML = SHOP_HTML_PATH.read_text(encoding="utf-8") if SHOP_HTML_PATH.is_file() else ""


def main() -> int:
    load_env_file()
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    load_sessions_from_disk(chat_store)
    mark_interrupted_chat_messages()
    normalize_stored_chat_tool_results()
    video_queue.start(execute_queue_job)
    start_report_scheduler()
    port = int(os.getenv("WEB_PORT", "4000"))
    sellersprite_redirect_port = int(os.getenv("SELLERSPRITE_REDIRECT_PORT", "0") or "0")
    if sellersprite_redirect_port and sellersprite_redirect_port != port:
        SellerSpriteRedirectHandler.target_port = port
        redirect_server = ThreadingHTTPServer(("0.0.0.0", sellersprite_redirect_port), SellerSpriteRedirectHandler)
        threading.Thread(target=redirect_server.serve_forever, daemon=True).start()
        print(f"SellerSprite redirect listening on http://0.0.0.0:{sellersprite_redirect_port} -> /amazon")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Web UI listening on http://0.0.0.0:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
