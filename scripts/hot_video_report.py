"""Daily hot-video report storage, collection, analysis, and summary."""
from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from deepseek_postprocess import DEFAULT_API_URL, DEFAULT_MODEL, call_deepseek, extract_content, parse_json_content
from sociavault_tiktok import call_api
from tools import execute_tool
from video_registry import get_video, get_video_by_filename, register_video, set_hidden_from_analyzer

ROOT = Path.cwd()
SCRIPTS_DIR = ROOT / "scripts"
OUTPUT_DIR = ROOT / "output"
VIDEOS_DIR = ROOT / "videos"
DB_PATH = ROOT / "data" / "hot_video_report.sqlite"
REPORT_COVER_DIR = ROOT / "data" / "report_covers"
DEFAULT_API_BASE = "https://api.sociavault.com"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_REPORT_JOB_TIMEOUT_SECONDS = 30 * 60

_scheduler_started = False
_scheduler_lock = threading.Lock()
_job_queue: queue.Queue[str] = queue.Queue()
_active_job_lock = threading.Lock()
_active_job: str | None = None
_progress_lock = threading.Lock()
_progress_by_date: dict[str, dict[str, Any]] = {}


def today_key() -> str:
    return datetime.now(ZoneInfo(DEFAULT_TZ)).strftime("%Y-%m-%d")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_reports (
            id TEXT PRIMARY KEY,
            report_date TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            region TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            video_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            report_json TEXT,
            report_markdown TEXT,
            analysis_success_count INTEGER NOT NULL DEFAULT 0,
            analysis_failed_count INTEGER NOT NULL DEFAULT 0,
            llm_generated_at REAL,
            scheduled_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hot_video_master (
            platform TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT,
            author TEXT,
            source_url TEXT,
            cover_url TEXT,
            local_filename TEXT,
            extraction_dir TEXT,
            first_seen_date TEXT NOT NULL,
            last_seen_date TEXT NOT NULL,
            latest_hot_score INTEGER NOT NULL DEFAULT 0,
            max_hot_score INTEGER NOT NULL DEFAULT 0,
            latest_metrics_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            hidden_from_analyzer INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (platform, video_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hot_report_videos (
            report_id TEXT NOT NULL,
            report_date TEXT NOT NULL,
            platform TEXT NOT NULL,
            video_id TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_label TEXT NOT NULL,
            source_rank INTEGER NOT NULL,
            report_rank INTEGER NOT NULL,
            hot_score INTEGER NOT NULL,
            metrics_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            process_status TEXT NOT NULL DEFAULT 'pending',
            process_error TEXT,
            local_filename TEXT,
            extraction_dir TEXT,
            analysis_json TEXT,
            audit_json TEXT,
            cover_url TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (report_date, platform, video_id),
            FOREIGN KEY (report_id) REFERENCES daily_reports(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    _ensure_columns(conn)
    _ensure_default_settings(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hot_report_videos_score ON hot_report_videos(report_date, hot_score DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hot_video_master_last_seen ON hot_video_master(last_seen_date DESC)")
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    def add_missing(table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    add_missing(
        "daily_reports",
        {
            "report_json": "TEXT",
            "report_markdown": "TEXT",
            "analysis_success_count": "INTEGER NOT NULL DEFAULT 0",
            "analysis_failed_count": "INTEGER NOT NULL DEFAULT 0",
            "llm_generated_at": "REAL",
            "scheduled_at": "REAL",
        },
    )
    existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "hot_videos" in existing_tables:
        hot_columns = {row[1] for row in conn.execute("PRAGMA table_info(hot_videos)").fetchall()}
        aliases = [
            "report_id",
            "report_date",
            "platform",
            "video_id",
            "title",
            "author",
            "source_url",
            "source_endpoint",
            "source_label",
            "source_rank",
            "hot_score",
            "metrics_json",
            "raw_json",
            "created_at",
            "updated_at",
        ]

        def select_expr(name: str, fallback: str = "''") -> str:
            if name in hot_columns:
                return name
            return f"{fallback} AS {name}"

        rows = conn.execute(
            f"""
            SELECT {", ".join(select_expr(name, "'{}'" if name.endswith("_json") else "0" if name.endswith("_at") or name in {"source_rank", "hot_score"} else "''") for name in aliases)}
            FROM hot_videos
            """
        ).fetchall()
        for row in rows:
            item = dict(zip(aliases, row))
            conn.execute(
                """
                INSERT OR IGNORE INTO hot_report_videos (
                    report_id, report_date, platform, video_id, source_endpoint, source_label,
                    source_rank, report_rank, hot_score, metrics_json, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["report_id"],
                    item["report_date"],
                    item["platform"],
                    item["video_id"],
                    item["source_endpoint"],
                    item["source_label"],
                    int(item["source_rank"] or 0),
                    int(item["source_rank"] or 0),
                    int(item["hot_score"] or 0),
                    item["metrics_json"] or "{}",
                    item["raw_json"] or "{}",
                    float(item["created_at"] or time.time()),
                    float(item["updated_at"] or time.time()),
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO hot_video_master (
                    platform, video_id, title, author, source_url, cover_url, first_seen_date,
                    last_seen_date, latest_hot_score, max_hot_score, latest_metrics_json,
                    raw_json, hidden_from_analyzer, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    item["platform"],
                    item["video_id"],
                    item["title"],
                    item["author"],
                    item["source_url"],
                    item["report_date"],
                    item["report_date"],
                    int(item["hot_score"] or 0),
                    int(item["hot_score"] or 0),
                    item["metrics_json"] or "{}",
                    item["raw_json"] or "{}",
                    float(item["created_at"] or time.time()),
                    float(item["updated_at"] or time.time()),
                ),
            )


def _ensure_default_settings(conn: sqlite3.Connection) -> None:
    now = time.time()
    defaults = {
        "schedule_time": "05:00",
        "timezone": DEFAULT_TZ,
        "analysis_limit": "10",
        "retention_days": "30",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO report_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def _compact(value: Any, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _clean_id(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"[^A-Za-z0-9_.:@/-]+", "_", text).strip("_")[:180]


def _to_int(value: Any) -> int:
    if value in (None, "") or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip().lower()
        multiplier = 1
        suffixes = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "w": 10_000}
        if cleaned and cleaned[-1] in suffixes:
            multiplier = suffixes[cleaned[-1]]
            cleaned = cleaned[:-1]
        try:
            return max(0, int(float(cleaned) * multiplier))
        except ValueError:
            return 0
    return 0


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "") or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _recent_window_days() -> int:
    return max(1, _to_int(os.getenv("HOT_VIDEO_RECENT_DAYS", "7")))


def _progress_payload(
    report_date: str,
    status: str,
    stage: str,
    progress: int,
    message: str,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    payload = {
        "report_date": report_date,
        "status": status,
        "stage": stage,
        "progress": max(0, min(100, int(progress))),
        "message": message,
        "counts": counts or {},
        "updated_at": time.time(),
    }
    with _progress_lock:
        previous = _progress_by_date.get(report_date) or {}
        merged_counts = dict(previous.get("counts") or {})
        merged_counts.update(payload["counts"])
        payload["counts"] = merged_counts
        _progress_by_date[report_date] = payload
    return payload


def get_report_progress(report_date: str | None = None) -> dict[str, Any]:
    date = report_date or today_key()
    with _progress_lock:
        payload = dict(_progress_by_date.get(date) or {})
    if payload:
        return payload
    report = get_report(date, include_raw=False, detail=False)
    return {
        "report_date": date,
        "status": report.get("status", "missing"),
        "stage": "finished" if report.get("status") in {"complete", "partial_failed", "failed"} else "queued",
        "progress": 100 if report.get("status") in {"complete", "partial_failed", "failed"} else 0,
        "message": str(report.get("error") or report.get("status") or "missing"),
        "counts": {},
        "updated_at": report.get("updated_at") or time.time(),
    }


def _first_present(data: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _find_nested(value: Any, names: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        found = _first_present(value, names)
        if found not in (None, "", [], {}):
            return found
        for child in value.values():
            found = _find_nested(child, names)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nested(child, names)
            if found not in (None, "", [], {}):
                return found
    return None


def _metric(node: dict[str, Any], names: tuple[str, ...]) -> int:
    found = _first_present(node, names)
    if found not in (None, "", [], {}):
        return _to_int(found)
    for key in ("statistics", "stats", "metrics", "statistics_info", "stats_info"):
        child = node.get(key)
        if isinstance(child, dict):
            found = _first_present(child, names)
            if found not in (None, "", [], {}):
                return _to_int(found)
    return 0


def _to_timestamp(value: Any) -> float | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if re.fullmatch(r"\d+", value):
            numeric = float(value)
            if numeric > 1_000_000_000_000:
                return numeric / 1000
            return numeric
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            try:
                return float(value)
            except Exception:
                return None
    return None


def _extract_publish_time(node: dict[str, Any]) -> float | None:
    found = _find_nested(
        node,
        (
            "create_time",
            "createTime",
            "create_at",
            "createAt",
            "publish_time",
            "publishTime",
            "create_time_utc",
            "origin_create_at",
            "origin_create_time",
            "create_time_ms",
            "publish_time_ms",
        ),
    )
    ts = _to_timestamp(found)
    if ts is not None:
        return ts
    found = _find_nested(node, ("video",))
    if isinstance(found, dict):
        nested = _find_nested(
            found,
            (
                "create_time",
                "createTime",
                "create_at",
                "createAt",
                "publish_time",
                "publishTime",
                "origin_create_at",
                "origin_create_time",
                "create_time_ms",
                "publish_time_ms",
            ),
        )
        return _to_timestamp(nested)
    return None


def _parse_report_date_to_ts(report_date: str) -> float | None:
    if not report_date:
        return None
    try:
        return datetime.fromisoformat(report_date).replace(tzinfo=ZoneInfo(DEFAULT_TZ)).timestamp()
    except Exception:
        return None


def _published_at_from_row(metrics_json: str | None, raw_json: str | None, report_date: str = "") -> float | None:
    metrics = _json_loads(metrics_json, {})
    if isinstance(metrics, dict):
        published_at = _to_timestamp(metrics.get("published_at"))
        if published_at is not None:
            return published_at
    raw = _json_loads(raw_json, {})
    if isinstance(raw, dict):
        published_at = _extract_publish_time(raw)
        if published_at is not None:
            return published_at
    return None


def _cleanup_expired_video_records(conn: sqlite3.Connection, recency_days: int | None = None) -> dict[str, int]:
    days = max(1, _to_int(recency_days if recency_days is not None else os.getenv("HOT_VIDEO_RECENT_DAYS", "7")))
    cutoff_ts = time.time() - days * 86400
    latest_publish_by_key: dict[tuple[str, str], float] = {}
    seen_keys: set[tuple[str, str]] = set()

    report_rows = conn.execute(
        """
        SELECT report_date, platform, video_id, metrics_json, raw_json
        FROM hot_report_videos
        """
    ).fetchall()
    for report_date, platform, video_id, metrics_json, raw_json in report_rows:
        key = (str(platform), str(video_id))
        seen_keys.add(key)
        published_at = _published_at_from_row(metrics_json, raw_json, str(report_date or ""))
        if published_at is None:
            latest_publish_by_key[key] = 0
            continue
        if key not in latest_publish_by_key or published_at > latest_publish_by_key[key]:
            latest_publish_by_key[key] = float(published_at)

    stale_keys = {
        (platform, video_id)
        for (platform, video_id), published_at in latest_publish_by_key.items()
        if published_at <= 0 or published_at < cutoff_ts
    }
    # Remove stale records from all report days by identity.
    for platform, video_id in stale_keys:
        conn.execute("DELETE FROM hot_report_videos WHERE platform = ? AND video_id = ?", (platform, video_id))
    if stale_keys:
        conn.executemany(
            "DELETE FROM hot_video_master WHERE platform = ? AND video_id = ?",
            list(stale_keys),
        )

    # Remove master items that are only historical and now too old by last_seen_date.
    master_rows = conn.execute(
        """
        SELECT platform, video_id, last_seen_date
        FROM hot_video_master
        """
    ).fetchall()
    stale_from_master: list[tuple[str, str]] = []
    for platform, video_id, last_seen_date in master_rows:
        key = (str(platform), str(video_id))
        if key in seen_keys:
            continue
        last_seen_ts = _parse_report_date_to_ts(str(last_seen_date or ""))
        if last_seen_ts is not None and last_seen_ts < cutoff_ts:
            stale_from_master.append((str(platform), str(video_id)))

    if stale_from_master:
        conn.executemany("DELETE FROM hot_video_master WHERE platform = ? AND video_id = ?", stale_from_master)

    conn.execute(
        "DELETE FROM daily_reports WHERE status != 'running' AND report_date NOT IN (SELECT DISTINCT report_date FROM hot_report_videos)"
    )
    conn.commit()
    return {
        "expired_report_videos": len(stale_keys),
        "expired_master_videos": len(stale_from_master),
    }


def _existing_report_video_keys(conn: sqlite3.Connection, report_date: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT platform, video_id
        FROM hot_report_videos
        WHERE report_date != ?
        """,
        (report_date,),
    ).fetchall()
    return {(str(platform), str(video_id)) for platform, video_id in rows}


def _source_url(node: dict[str, Any]) -> str:
    found = _first_present(node, ("share_url", "shareUrl", "webpage_url", "url"))
    if isinstance(found, str) and found.startswith(("http://", "https://")):
        return found
    found = _find_nested(node, ("share_url", "shareUrl", "webpage_url", "url"))
    if isinstance(found, str) and found.startswith(("http://", "https://")):
        return found
    return ""


def _cover_url(node: dict[str, Any]) -> str:
    names = (
        "cover",
        "cover_url",
        "coverUrl",
        "thumbnail",
        "thumbnail_url",
        "thumbnailUrl",
        "origin_cover",
        "originCover",
        "dynamic_cover",
        "dynamicCover",
        "play_addr",
        "playAddr",
    )
    found = _find_nested(node, names)
    if isinstance(found, str) and found.startswith(("http://", "https://")):
        return found
    if isinstance(found, dict):
        url = _find_nested(found, ("url", "uri", "download_url", "downloadUrl", "display_url", "displayUrl"))
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
        urls = _find_nested(found, ("url_list", "urlList"))
        if isinstance(urls, list):
            for item in urls:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    return item
        if isinstance(urls, dict):
            for item in urls.values():
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    return item
    if isinstance(found, list):
        for item in found:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
    return ""


def _cover_asset_name(platform: str, video_id: str, suffix: str = ".jpg") -> str:
    safe_platform = re.sub(r"[^A-Za-z0-9_-]+", "_", platform or "video").strip("_") or "video"
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", video_id or uuid.uuid4().hex).strip("_") or uuid.uuid4().hex
    return f"{safe_platform}_{safe_id}{suffix}"


def _cover_asset_url(filename: str) -> str:
    return "/report-cover/" + urllib.parse.quote(filename)


def _existing_cover_asset(platform: str, video_id: str) -> str:
    REPORT_COVER_DIR.mkdir(parents=True, exist_ok=True)
    prefix = _cover_asset_name(platform, video_id, "").rstrip(".")
    for path in REPORT_COVER_DIR.glob(prefix + ".*"):
        if path.is_file() and path.stat().st_size > 0:
            return _cover_asset_url(path.name)
    return ""


def _cover_suffix(url: str, content_type: str = "") -> str:
    parsed = urllib.parse.urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if ext == ".jpeg" else ext
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def _download_cover_asset(url: str, platform: str, video_id: str) -> str:
    if not url or url.startswith("/report-cover/"):
        return url
    cached = _existing_cover_asset(platform, video_id)
    if cached:
        return cached
    REPORT_COVER_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read(4_000_000)
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""
    if not body or not (content_type.startswith("image/") or body[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\x89PNG")):
        return ""
    name = _cover_asset_name(platform, video_id, _cover_suffix(url, content_type))
    path = REPORT_COVER_DIR / name
    path.write_bytes(body)
    return _cover_asset_url(name)


def _snapshot_cover_asset(filename: str, platform: str, video_id: str) -> str:
    if not filename:
        return ""
    cached = _existing_cover_asset(platform, video_id)
    if cached:
        return cached
    video_path = VIDEOS_DIR / filename
    if not video_path.is_file():
        return ""
    REPORT_COVER_DIR.mkdir(parents=True, exist_ok=True)
    name = _cover_asset_name(platform, video_id, ".jpg")
    path = REPORT_COVER_DIR / name
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "00:00:01",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=540:-1",
        str(path),
    ]
    try:
        subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=True)
    except (subprocess.SubprocessError, OSError):
        return ""
    return _cover_asset_url(name) if path.is_file() and path.stat().st_size > 0 else ""


def _prepare_cover_asset(video: dict[str, Any]) -> dict[str, Any]:
    cover = str(video.get("cover_url") or "")
    platform = str(video.get("platform") or "")
    video_id = str(video.get("video_id") or "")
    local = _existing_cover_asset(platform, video_id)
    if local:
        video["cover_url"] = local
    elif cover:
        video["cover_url"] = cover
    return video


def _looks_like_video(node: dict[str, Any]) -> bool:
    video_id = _first_present(node, ("aweme_id", "awemeId", "video_id", "videoId", "item_id", "itemId", "id"))
    if not video_id:
        return False
    keys = set(node.keys())
    stat_only_keys = {
        "aweme_id",
        "video_id",
        "id",
        "collect_count",
        "comment_count",
        "digg_count",
        "download_count",
        "forward_count",
        "lose_comment_count",
        "lose_count",
        "play_count",
        "repost_count",
        "share_count",
        "whatsapp_share_count",
    }
    if keys and keys.issubset(stat_only_keys):
        return False
    has_main_shape = any(
        key in node
        for key in (
            "desc",
            "description",
            "caption",
            "url",
            "share_url",
            "shareUrl",
            "webpage_url",
            "video",
            "statistics",
            "stats",
            "author",
            "create_time",
            "createTime",
            "create_time_utc",
            "publish_time",
            "publishTime",
        )
    )
    has_video_media = isinstance(node.get("video"), dict) and any(
        key in node["video"] for key in ("play_addr", "download_addr", "cover", "origin_cover", "dynamic_cover")
    )
    has_stats = isinstance(node.get("statistics"), dict) or isinstance(node.get("stats"), dict)
    has_title_only_music_shape = "play_url" in keys and "matched_song" not in keys and "video" not in keys
    return bool(has_main_shape and (has_video_media or has_stats or "create_time" in keys or "url" in keys) and not has_title_only_music_shape)


def _is_photo_mode_post(node: dict[str, Any]) -> bool:
    """TikTok Photo Mode posts can expose audio URLs but no analyzable video stream."""
    if not isinstance(node, dict):
        return False
    if node.get("image_post_info") or node.get("imagePostInfo"):
        return True
    video = node.get("video")
    if not isinstance(video, dict):
        return False
    return bool(video.get("image_post_info") or video.get("imagePostInfo"))


def _iter_video_nodes(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if _looks_like_video(value):
            out.append(value)
        for child in value.values():
            out.extend(_iter_video_nodes(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_iter_video_nodes(child))
    return out


def _normalize_video(node: dict[str, Any], endpoint: str, label: str, rank: int) -> dict[str, Any]:
    video_id = _clean_id(_first_present(node, ("aweme_id", "awemeId", "video_id", "videoId", "item_id", "itemId", "id")))
    title = _compact(_first_present(node, ("desc", "description", "title", "caption")))
    author = _compact(_find_nested(node, ("unique_id", "uniqueId", "nickname", "author", "authorName")), 160)
    metrics = {
        "play_count": _metric(node, ("play_count", "playCount", "view_count", "viewCount")),
        "like_count": _metric(node, ("digg_count", "diggCount", "like_count", "likeCount")),
        "comment_count": _metric(node, ("comment_count", "commentCount", "comments")),
        "share_count": _metric(node, ("share_count", "shareCount", "repost_count", "repostCount")),
        "favorite_count": _metric(node, ("collect_count", "collectCount", "favorite_count", "favoriteCount")),
    }
    hot_score = _score_hot_video(metrics, rank)
    published_at = _extract_publish_time(node)
    if published_at:
        metrics["published_at"] = published_at
    return {
        "platform": "tiktok",
        "video_id": video_id,
        "title": title,
        "author": author,
        "source_url": _source_url(node),
        "cover_url": _cover_url(node),
        "source_endpoint": endpoint,
        "source_label": label,
        "source_rank": rank,
        "hot_score": int(hot_score),
        "metrics": metrics,
        "raw": node,
    }


def _score_hot_video(metrics: dict[str, int], rank: int) -> int:
    play_count = int(metrics.get("play_count") or 0)
    like_count = int(metrics.get("like_count") or 0)
    comment_count = int(metrics.get("comment_count") or 0)
    share_count = int(metrics.get("share_count") or 0)
    favorite_count = int(metrics.get("favorite_count") or 0)
    engagement = like_count + comment_count * 3 + share_count * 2 + favorite_count
    engagement_rate_bonus = int((engagement / max(play_count, 1)) * 100_000) if play_count else 0
    return int(
        play_count
        + like_count * 20
        + comment_count * 80
        + share_count * 60
        + favorite_count * 20
        + engagement_rate_bonus
        + max(0, 50 - rank) * 100
    )


def _deep_merge_dict(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _extract_video_info_node(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("aweme", "aweme_detail", "item", "video", "data"):
        value = payload.get(key)
        if isinstance(value, dict) and _looks_like_video(value):
            return value
        if isinstance(value, dict):
            nested = _extract_video_info_node(value)
            if nested:
                return nested
    nodes = [node for node in _iter_video_nodes(payload) if _looks_like_video(node)]
    return nodes[0] if nodes else {}


def _enrich_missing_publish_time(
    item: dict[str, Any],
    api_key: str,
    api_base: str,
    timeout: float,
) -> tuple[dict[str, Any], bool]:
    if item.get("metrics", {}).get("published_at"):
        return item, False
    source_url = str(item.get("source_url") or "").strip()
    if not source_url and item.get("video_id"):
        source_url = f"https://www.tiktok.com/@unknown/video/{item['video_id']}"
    if not source_url:
        return item, False
    try:
        payload = call_api(api_key, api_base, "video-info", {"url": source_url}, timeout)
    except Exception:
        return item, False
    node = _extract_video_info_node(payload)
    if not node:
        return item, False
    enriched = _normalize_video(node, item["source_endpoint"], item["source_label"], int(item["source_rank"]))
    if enriched.get("video_id") and enriched["video_id"] != item["video_id"]:
        return item, False
    merged = dict(item)
    merged["title"] = enriched.get("title") or item.get("title", "")
    merged["author"] = enriched.get("author") or item.get("author", "")
    merged["source_url"] = enriched.get("source_url") or item.get("source_url", "")
    merged["cover_url"] = enriched.get("cover_url") or item.get("cover_url", "")
    merged["metrics"] = _deep_merge_dict(item.get("metrics") or {}, enriched.get("metrics") or {})
    merged["raw"] = _deep_merge_dict(item.get("raw") or {}, enriched.get("raw") or {})
    published_at = _extract_publish_time(merged["raw"])
    if published_at:
        merged["metrics"]["published_at"] = published_at
    return merged, True


def _collect_hot_video_candidates(
    report_date: str,
    region: str,
    target_count: int,
    recency_days: int,
    api_key: str,
    api_base: str,
    api_timeout: float,
    counts: dict[str, int],
    excluded_keys: set[tuple[str, str]] | None = None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    excluded_keys = excluded_keys or set()
    cutoff_ts = time.time() - recency_days * 86400
    max_pages = max(1, _to_int(os.getenv("HOT_VIDEO_POPULAR_MAX_PAGES", "15")))
    source_errors: list[str] = []
    topic_keywords = _split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys")
    topic_min_views = max(0, _to_int(os.getenv("HOT_VIDEO_TOPIC_MIN_PLAY_COUNT", "5000")))
    stream_min_views = max(0, _to_int(os.getenv("HOT_VIDEO_STREAM_MIN_PLAY_COUNT", "10000")))

    def collect_from(endpoint: str, params: dict[str, Any], label: str, min_play_count: int, bucket: str) -> None:
        _progress_payload(report_date, "running", "collecting", 6, f"Collecting source: {label}", counts)
        cache_policy = os.getenv("HOT_VIDEO_SOURCE_CACHE_POLICY", "record_only").strip() or "record_only"
        payload = call_api(api_key, api_base, endpoint, params, api_timeout, cache_policy=cache_policy)
        for rank, node in enumerate(_iter_video_nodes(payload), start=1):
            counts["collected"] += 1
            if _is_photo_mode_post(node):
                counts["skipped_photo_mode"] = counts.get("skipped_photo_mode", 0) + 1
                continue
            item = _normalize_video(node, endpoint, label, rank)
            if not item["video_id"]:
                continue
            counts["candidate_count"] += 1
            published_at = item.get("metrics", {}).get("published_at")
            if not isinstance(published_at, (int, float)) or published_at <= 0:
                item, enriched = _enrich_missing_publish_time(item, api_key, api_base, api_timeout)
                if enriched:
                    counts["enriched_count"] += 1
                published_at = item.get("metrics", {}).get("published_at")
            if not isinstance(published_at, (int, float)) or published_at <= 0:
                counts["skipped_missing_time"] += 1
                continue
            if published_at < cutoff_ts:
                counts["skipped_old"] += 1
                continue
            counts["recent_count"] += 1
            play_count = int(item.get("metrics", {}).get("play_count") or 0)
            if play_count < min_play_count:
                counts[f"skipped_low_views_{bucket}"] = counts.get(f"skipped_low_views_{bucket}", 0) + 1
                continue
            item["selection_bucket"] = bucket
            item["metrics"]["selection_min_play_count"] = min_play_count
            key = (item["platform"], item["video_id"])
            if key in excluded_keys:
                counts["skipped_duplicate_report"] = counts.get("skipped_duplicate_report", 0) + 1
                continue
            if key not in candidates or item["hot_score"] > candidates[key]["hot_score"]:
                candidates[key] = item
        _progress_payload(report_date, "running", "filtering", 18, f"{label} complete, valid candidates: {len(candidates)}", counts)

    topic_fetch = max(target_count * 2, 20)
    for keyword in topic_keywords:
        try:
            collect_from(
                "search-top",
                {"query": keyword, "region": region, "count": topic_fetch},
                f"topic-search:{keyword}",
                topic_min_views,
                "topic",
            )
        except Exception as exc:
            source_errors.append(f"topic-search:{keyword}: {exc}")

    page = 1
    while len(candidates) < target_count and page <= max_pages:
        remaining = target_count - len(candidates)
        fetch_count = max(20, remaining * 3)
        page_success = False
        for endpoint, params, label in _trending_source_requests(region, fetch_count, page):
            try:
                collect_from(endpoint, params, label, stream_min_views, "stream")
                page_success = True
            except Exception as exc:
                source_errors.append(f"{label}: {exc}")
        if not page_success:
            break
        page += 1

    page = 1
    popular_source_count = max(1, len(_split_csv_env("HOT_VIDEO_POPULAR_SORTS", "views,likes")))
    while len(candidates) < target_count and page <= max_pages:
        remaining = target_count - len(candidates)
        total_fetch = max(20, remaining * 3)
        per_source_count = max(1, (total_fetch + popular_source_count - 1) // popular_source_count)
        page_success = False
        for endpoint, params, label in _popular_source_requests(region, per_source_count, page, recency_days):
            try:
                collect_from(endpoint, params, label, stream_min_views, "stream")
                page_success = True
            except Exception as exc:
                source_errors.append(f"{label}: {exc}")
        if not page_success:
            break
        page += 1

    if len(candidates) < target_count:
        fallback_max = max(target_count * 2, _to_int(os.getenv("HOT_VIDEO_FALLBACK_SOURCE_COUNT_MAX", "80")))
        fallback_count = max((target_count - len(candidates)) * 2, 10)
        used_counts: set[int] = set()
        while len(candidates) < target_count and fallback_count <= fallback_max:
            if fallback_count in used_counts:
                fallback_count += max((target_count - len(candidates)) * 2, 10)
                continue
            used_counts.add(fallback_count)
            before = len(candidates)
            _progress_payload(
                report_date,
                "running",
                "collecting",
                12,
                f"Popular source insufficient, fallback count={fallback_count}, current valid={len(candidates)}/{target_count}",
                counts,
            )
            for endpoint, params, label in _legacy_source_requests(region, fallback_count):
                try:
                    collect_from(endpoint, params, f"fallback:{label}:c{fallback_count}", stream_min_views, "stream")
                except Exception as exc:
                    source_errors.append(f"fallback:{label}:c{fallback_count}: {exc}")
            if len(candidates) >= target_count:
                break
            remaining = target_count - len(candidates)
            next_count = fallback_count + max(remaining * 2, 10)
            if len(candidates) == before and next_count <= fallback_count:
                break
            fallback_count = next_count

    return candidates, source_errors


def _row_to_report(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "report_date",
        "status",
        "region",
        "sources_json",
        "video_count",
        "error",
        "report_json",
        "report_markdown",
        "analysis_success_count",
        "analysis_failed_count",
        "llm_generated_at",
        "scheduled_at",
        "created_at",
        "updated_at",
    )
    data = dict(zip(keys, row))
    data["sources"] = _json_loads(data.pop("sources_json"), [])
    data["report"] = _json_loads(data.pop("report_json"), None)
    return data


def _row_to_video(row: sqlite3.Row | tuple[Any, ...], include_raw: bool = False) -> dict[str, Any]:
    keys = (
        "platform",
        "video_id",
        "title",
        "author",
        "source_url",
        "cover_url",
        "local_filename",
        "extraction_dir",
        "source_endpoint",
        "source_label",
        "source_rank",
        "report_rank",
        "hot_score",
        "metrics_json",
        "raw_json",
        "process_status",
        "process_error",
        "analysis_json",
        "audit_json",
        "created_at",
        "updated_at",
    )
    data = dict(zip(keys, row))
    data["metrics"] = _json_loads(data.pop("metrics_json"), {})
    raw = _json_loads(data.pop("raw_json"), {})
    data["analysis"] = _json_loads(data.pop("analysis_json"), None)
    data["audit_result"] = _json_loads(data.pop("audit_json"), None)
    if include_raw:
        data["raw"] = raw
    return data


def get_settings() -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM report_settings").fetchall()
    values = {str(k): str(v) for k, v in rows}
    return {
        "schedule_time": values.get("schedule_time", "05:00"),
        "timezone": values.get("timezone", DEFAULT_TZ),
        "analysis_limit": _to_int(values.get("analysis_limit", "10")) or 10,
        "retention_days": _to_int(values.get("retention_days", "30")) or 30,
    }


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = get_settings()
    schedule_time = str(payload.get("schedule_time", current["schedule_time"])).strip()
    if not re.fullmatch(r"[0-2]\d:[0-5]\d", schedule_time):
        raise ValueError("schedule_time must be HH:MM")
    hour = int(schedule_time.split(":", 1)[0])
    if hour > 23:
        raise ValueError("schedule_time hour must be 00-23")
    analysis_limit = max(1, min(_to_int(payload.get("analysis_limit", current["analysis_limit"])), 20))
    retention_days = max(1, min(_to_int(payload.get("retention_days", current["retention_days"])), 30))
    timezone = str(payload.get("timezone", current["timezone"]) or DEFAULT_TZ)
    ZoneInfo(timezone)
    now = time.time()
    with _connect() as conn:
        for key, value in {
            "schedule_time": schedule_time,
            "timezone": timezone,
            "analysis_limit": str(analysis_limit),
            "retention_days": str(retention_days),
        }.items():
            conn.execute(
                """
                INSERT INTO report_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
        conn.commit()
    return get_settings()


def backfill_cover_urls(report_date: str | None = None) -> dict[str, Any]:
    date = report_date or today_key()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT rv.platform, rv.video_id, rv.raw_json, COALESCE(rv.local_filename, m.local_filename),
                   COALESCE(rv.cover_url, m.cover_url, '')
            FROM hot_report_videos rv
            LEFT JOIN hot_video_master m ON m.platform = rv.platform AND m.video_id = rv.video_id
            WHERE rv.report_date = ? AND COALESCE(rv.cover_url, m.cover_url, '') NOT LIKE '/report-cover/%'
            """,
            (date,),
        ).fetchall()
        if not rows:
            master_rows = conn.execute(
                """
                SELECT platform, video_id, raw_json, cover_url
                FROM hot_video_master
                WHERE COALESCE(cover_url, '') NOT LIKE '/report-cover/%'
                """
            ).fetchall()
            updated = 0
            for platform, video_id, raw_json, current_cover in master_rows:
                raw = _json_loads(raw_json, {})
                cover_url = str(current_cover or "") or _cover_url(raw)
                cover_asset = _download_cover_asset(cover_url, platform, video_id) if cover_url else ""
                if cover_asset:
                    conn.execute(
                        "UPDATE hot_video_master SET cover_url = ?, updated_at = ? WHERE platform = ? AND video_id = ?",
                        (cover_asset, time.time(), platform, video_id),
                    )
                    updated += 1
            conn.commit()
            return {"updated": updated, "report_date": date, "source": "master"}
        updated = 0
        now = time.time()
        for platform, video_id, raw_json, filename, current_cover in rows:
            raw = _json_loads(raw_json, {})
            cover_url = str(current_cover or "") or _cover_url(raw)
            cover_asset = _download_cover_asset(cover_url, platform, video_id) if cover_url else ""
            if not cover_asset:
                cover_asset = _snapshot_cover_asset(str(filename or ""), platform, video_id)
            if cover_asset:
                conn.execute(
                    "UPDATE hot_report_videos SET cover_url = ?, updated_at = ? WHERE report_date = ? AND platform = ? AND video_id = ?",
                    (cover_asset, now, date, platform, video_id),
                )
                conn.execute(
                    "UPDATE hot_video_master SET cover_url = ?, updated_at = ? WHERE platform = ? AND video_id = ?",
                    (cover_asset, now, platform, video_id),
                )
                updated += 1
        conn.commit()
        return {"updated": updated, "report_date": date, "source": "report_videos"}


def get_report(report_date: str | None = None, include_raw: bool = False, detail: bool = True) -> dict[str, Any]:
    date = report_date or today_key()
    with _connect() as conn:
        _cleanup_expired_video_records(conn, _recent_window_days())
        report_row = conn.execute(
            """
            SELECT id, report_date, status, region, sources_json, video_count, error,
                   report_json, report_markdown, analysis_success_count, analysis_failed_count,
                   llm_generated_at, scheduled_at, created_at, updated_at
            FROM daily_reports WHERE report_date = ?
            """,
            (date,),
        ).fetchone()
        if not report_row:
            return {"exists": False, "report_date": date, "status": "missing", "videos": []}
        rows = []
        if detail:
            rows = conn.execute(
                """
                SELECT m.platform, m.video_id, COALESCE(m.title, v.title), COALESCE(m.author, v.author),
                       COALESCE(m.source_url, v.source_url), COALESCE(rv.cover_url, m.cover_url),
                       COALESCE(rv.local_filename, m.local_filename), COALESCE(rv.extraction_dir, m.extraction_dir),
                       rv.source_endpoint, rv.source_label, rv.source_rank, rv.report_rank, rv.hot_score,
                       rv.metrics_json, rv.raw_json, rv.process_status, rv.process_error,
                       rv.analysis_json, rv.audit_json, rv.created_at, rv.updated_at
                FROM hot_report_videos rv
                JOIN hot_video_master m ON m.platform = rv.platform AND m.video_id = rv.video_id
                LEFT JOIN hot_video_master v ON v.platform = rv.platform AND v.video_id = rv.video_id
                WHERE rv.report_date = ?
                ORDER BY rv.report_rank ASC, rv.hot_score DESC
                """,
                (date,),
            ).fetchall()
    report = _row_to_report(report_row)
    report["exists"] = True
    videos = [_row_to_video(row, include_raw=include_raw) for row in rows] if detail else []
    report["videos"] = [_prepare_cover_asset(video) for video in videos] if detail else []
    return report


def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    settings = get_settings()
    retention_days = int(settings["retention_days"])
    cutoff = (datetime.now(ZoneInfo(settings["timezone"])) - timedelta(days=retention_days - 1)).strftime("%Y-%m-%d")
    with _connect() as conn:
        _cleanup_expired_video_records(conn, _recent_window_days())
        rows = conn.execute(
            """
            SELECT id, report_date, status, region, sources_json, video_count, error,
                   report_json, report_markdown, analysis_success_count, analysis_failed_count,
                   llm_generated_at, scheduled_at, created_at, updated_at
            FROM daily_reports
            WHERE report_date >= ?
            ORDER BY report_date DESC
            LIMIT ?
            """,
            (cutoff, max(1, min(limit, retention_days))),
        ).fetchall()
    items = []
    for row in rows:
        item = _row_to_report(row)
        report = item.pop("report", None) or {}
        item["summary"] = report.get("summary") or report.get("overall_conclusion") or item.get("report_markdown") or ""
        item["sources"] = []
        item.pop("report_markdown", None)
        items.append(item)
    return items


def _split_csv_env(name: str, default: str) -> list[str]:
    values = [item.strip() for item in os.getenv(name, default).split(",")]
    return [item for item in values if item]


def _popular_source_requests(region: str, count: int, page: int, recency_days: int) -> list[tuple[str, dict[str, Any], str]]:
    sorts = _split_csv_env("HOT_VIDEO_POPULAR_SORTS", "views,likes")
    requests = []
    for sort in sorts:
        params = {
            "region": region,
            "count": count,
            "page": page,
            "days": recency_days,
            "sort_by": sort,
        }
        requests.append(("videos-popular", params, f"videos-popular:{sort}:p{page}"))
    return requests


def _trending_source_requests(region: str, count: int, page: int) -> list[tuple[str, dict[str, Any], str]]:
    params = {"region": region, "count": count}
    if page > 1:
        params["page"] = page
    return [("trending", params, f"trending:{region}:p{page}")]


def _legacy_source_requests(region: str, count: int) -> list[tuple[str, dict[str, Any], str]]:
    keywords = _split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys")
    requests: list[tuple[str, dict[str, Any], str]] = []
    if os.getenv("HOT_VIDEO_INCLUDE_TRENDING", "0").strip() in {"1", "true", "yes"}:
        requests.append(("trending", {"region": region, "count": count}, f"trending:{region}"))
    for keyword in keywords:
        requests.append(("search-top", {"query": keyword, "region": region, "count": count}, f"search-top:{keyword}"))
    return requests


def _source_requests(region: str, count: int) -> list[tuple[str, dict[str, Any], str]]:
    requests: list[tuple[str, dict[str, Any], str]] = []
    for keyword in _split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys"):
        requests.append(("search-top", {"query": keyword, "region": region, "count": count}, f"topic-search:{keyword}"))
    requests.extend(_trending_source_requests(region, count, 1))
    requests.extend(_popular_source_requests(region, max(1, count // 2), 1, _recent_window_days()))
    return requests


def _start_report(conn: sqlite3.Connection, report_date: str, region: str, sources: list[dict[str, Any]], scheduled: bool = False) -> str:
    now = time.time()
    report_id = uuid.uuid4().hex
    conn.execute("DELETE FROM hot_report_videos WHERE report_date = ?", (report_date,))
    conn.execute(
        """
        INSERT INTO daily_reports (
            id, report_date, status, region, sources_json, video_count, error,
            report_json, report_markdown, analysis_success_count, analysis_failed_count,
            llm_generated_at, scheduled_at, created_at, updated_at
        )
        VALUES (?, ?, 'running', ?, ?, 0, NULL, NULL, NULL, 0, 0, NULL, ?, ?, ?)
        ON CONFLICT(report_date) DO UPDATE SET
            id = excluded.id,
            status = 'running',
            region = excluded.region,
            sources_json = excluded.sources_json,
            video_count = 0,
            error = NULL,
            report_json = NULL,
            report_markdown = NULL,
            analysis_success_count = 0,
            analysis_failed_count = 0,
            llm_generated_at = NULL,
            scheduled_at = excluded.scheduled_at,
            updated_at = excluded.updated_at
        """,
        (report_id, report_date, region, json.dumps(sources, ensure_ascii=False), now if scheduled else None, now, now),
    )
    conn.commit()
    return report_id


def _finish_report(
    conn: sqlite3.Connection,
    report_id: str,
    report_date: str,
    status: str,
    error: str = "",
    report_json: dict[str, Any] | None = None,
    report_markdown: str = "",
) -> None:
    now = time.time()
    row = conn.execute(
        """
        SELECT
          COUNT(*),
          SUM(CASE WHEN process_status = 'complete' THEN 1 ELSE 0 END),
          SUM(CASE WHEN process_status = 'failed' THEN 1 ELSE 0 END)
        FROM hot_report_videos WHERE report_date = ?
        """,
        (report_date,),
    ).fetchone()
    video_count = int(row[0] if row else 0)
    success_count = int(row[1] if row and row[1] is not None else 0)
    failed_count = int(row[2] if row and row[2] is not None else 0)
    llm_generated_marker = 1 if report_json else None
    conn.execute(
        """
        UPDATE daily_reports
        SET status = ?, video_count = ?, error = ?, report_json = COALESCE(?, report_json),
            report_markdown = COALESCE(?, report_markdown), analysis_success_count = ?,
            analysis_failed_count = ?, llm_generated_at = CASE WHEN ? IS NULL THEN llm_generated_at ELSE ? END,
            updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            video_count,
            error,
            json.dumps(report_json, ensure_ascii=False, sort_keys=True) if report_json else None,
            report_markdown or None,
            success_count,
            failed_count,
            llm_generated_marker,
            now,
            now,
            report_id,
        ),
    )
    conn.commit()


def _upsert_video(conn: sqlite3.Connection, report_id: str, report_date: str, item: dict[str, Any], report_rank: int) -> None:
    now = time.time()
    metrics_json = json.dumps(item["metrics"], ensure_ascii=False, sort_keys=True)
    raw_json = json.dumps(item["raw"], ensure_ascii=False, sort_keys=True, default=str)
    existing = conn.execute(
        "SELECT first_seen_date, max_hot_score, local_filename, extraction_dir FROM hot_video_master WHERE platform = ? AND video_id = ?",
        (item["platform"], item["video_id"]),
    ).fetchone()
    first_seen = existing[0] if existing else report_date
    max_hot_score = max(int(existing[1] if existing else 0), int(item["hot_score"]))
    conn.execute(
        """
        INSERT INTO hot_video_master (
            platform, video_id, title, author, source_url, cover_url, first_seen_date,
            last_seen_date, latest_hot_score, max_hot_score, latest_metrics_json,
            raw_json, hidden_from_analyzer, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(platform, video_id) DO UPDATE SET
            title = COALESCE(NULLIF(excluded.title, ''), title),
            author = COALESCE(NULLIF(excluded.author, ''), author),
            source_url = COALESCE(NULLIF(excluded.source_url, ''), source_url),
            cover_url = COALESCE(NULLIF(excluded.cover_url, ''), cover_url),
            last_seen_date = excluded.last_seen_date,
            latest_hot_score = excluded.latest_hot_score,
            max_hot_score = CASE WHEN excluded.max_hot_score > max_hot_score THEN excluded.max_hot_score ELSE max_hot_score END,
            latest_metrics_json = excluded.latest_metrics_json,
            raw_json = excluded.raw_json,
            updated_at = excluded.updated_at
        """,
        (
            item["platform"],
            item["video_id"],
            item["title"],
            item["author"],
            item["source_url"],
            item["cover_url"],
            first_seen,
            report_date,
            item["hot_score"],
            max_hot_score,
            metrics_json,
            raw_json,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO hot_report_videos (
            report_id, report_date, platform, video_id, source_endpoint, source_label,
            source_rank, report_rank, hot_score, metrics_json, raw_json, cover_url,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(report_date, platform, video_id) DO UPDATE SET
            source_endpoint = excluded.source_endpoint,
            source_label = excluded.source_label,
            source_rank = excluded.source_rank,
            report_rank = excluded.report_rank,
            hot_score = excluded.hot_score,
            metrics_json = excluded.metrics_json,
            raw_json = excluded.raw_json,
            cover_url = COALESCE(NULLIF(excluded.cover_url, ''), cover_url),
            updated_at = excluded.updated_at
        """,
        (
            report_id,
            report_date,
            item["platform"],
            item["video_id"],
            item["source_endpoint"],
            item["source_label"],
            item["source_rank"],
            report_rank,
            item["hot_score"],
            metrics_json,
            raw_json,
            item["cover_url"],
            now,
            now,
        ),
    )


def _output_dir_for_filename(filename: str) -> Path:
    record = get_video_by_filename(filename)
    if record:
        return OUTPUT_DIR / str(record.get("extraction_dir") or filename)
    return OUTPUT_DIR / filename


def _run_deepseek_report(output_dir: Path) -> dict[str, Any]:
    audit_path = output_dir / "audit_result.json"
    prompt = _load_current_analysis_prompt()
    if audit_path.is_file() and _output_prompt_matches(output_dir, prompt):
        return _json_loads(audit_path.read_text(encoding="utf-8"), {})
    cmd = [sys.executable, str(SCRIPTS_DIR / "deepseek_postprocess.py"), str(output_dir)]
    if prompt:
        cmd.extend(["--prompt", prompt])
    timeout = _to_float(os.getenv("REPORT_DEEPSEEK_TIMEOUT", "180"), 180.0)
    try:
        subprocess.run(cmd, cwd=ROOT, check=True, env=os.environ.copy(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"DeepSeek postprocess timed out after {timeout}s: {exc}")
    return _json_loads(audit_path.read_text(encoding="utf-8") if audit_path.is_file() else "", {})


def _load_current_analysis_prompt() -> str:
    for path in (ROOT / "data" / "analysis_prompt.txt", ROOT / "analysis_prompt.txt"):
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
    return ""


def _output_prompt_matches(output_dir: Path, prompt: str) -> bool:
    if not prompt:
        return True
    prompt_path = output_dir / "analysis_prompt.txt"
    return prompt_path.is_file() and prompt_path.read_text(encoding="utf-8").strip() == prompt.strip()


def _prepare_output_prompt(output_dir: Path, prompt: str) -> bool:
    if not prompt:
        return False
    prompt_path = output_dir / "analysis_prompt.txt"
    previous = prompt_path.read_text(encoding="utf-8").strip() if prompt_path.is_file() else ""
    changed = previous != prompt.strip()
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt.strip() + "\n", encoding="utf-8")
    if changed:
        for name in ("analysis.json", "analysis_zh.json", "audit_result.json", "audit_result_zh.json"):
            path = output_dir / name
            if path.is_file():
                path.unlink()
    return changed


def _process_video(conn: sqlite3.Connection, report_date: str, item: dict[str, Any]) -> None:
    now = time.time()
    platform = item["platform"]
    video_id = item["video_id"]
    record = get_video(platform, video_id) or {}
    filename = str(record.get("filename") or "")
    extraction_dir = str(record.get("extraction_dir") or "")
    source_url = item.get("source_url") or record.get("source_url") or ""
    was_visible_manual_video = bool(filename and not int(record.get("hidden_from_analyzer") or 0))
    try:
        if not filename:
            if not source_url:
                raise RuntimeError("missing source_url")
            result = execute_tool("video_download", {"url": source_url})
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "download failed"))
            data = result.get("data") or {}
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            filename = str(payload.get("filename") or "")
            if not filename:
                raise RuntimeError("download did not return filename")
            register_video(
                platform=platform,
                video_id=video_id,
                source_url=source_url,
                filename=filename,
                title=item.get("title", ""),
                author=item.get("author", ""),
                source="hot_report",
                hidden_from_analyzer=True,
            )
            if not was_visible_manual_video:
                set_hidden_from_analyzer(platform, video_id, True)
        output_dir = _output_dir_for_filename(filename)
        prompt = _load_current_analysis_prompt()
        _prepare_output_prompt(output_dir, prompt)
        analysis_path = output_dir / "analysis.json"
        if not analysis_path.is_file():
            result = execute_tool("video_analyze", {"filename": filename})
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "analysis failed"))
        output_dir = _output_dir_for_filename(filename)
        analysis = _json_loads((output_dir / "analysis.json").read_text(encoding="utf-8") if (output_dir / "analysis.json").is_file() else "", {})
        audit = _run_deepseek_report(output_dir)
        registry = get_video(platform, video_id) or get_video_by_filename(filename) or {}
        extraction_dir = str(registry.get("extraction_dir") or output_dir.name)
        cover_asset = _download_cover_asset(str(item.get("cover_url") or ""), platform, video_id)
        if not cover_asset:
            cover_asset = _snapshot_cover_asset(filename, platform, video_id)
        conn.execute(
            """
            UPDATE hot_video_master
            SET local_filename = COALESCE(NULLIF(?, ''), local_filename),
                extraction_dir = COALESCE(NULLIF(?, ''), extraction_dir),
                cover_url = COALESCE(NULLIF(?, ''), cover_url),
                updated_at = ?
            WHERE platform = ? AND video_id = ?
            """,
            (filename, extraction_dir, cover_asset, now, platform, video_id),
        )
        conn.execute(
            """
            UPDATE hot_report_videos
            SET process_status = 'complete', process_error = NULL, local_filename = ?, extraction_dir = ?,
                cover_url = COALESCE(NULLIF(?, ''), cover_url),
                analysis_json = ?, audit_json = ?, updated_at = ?
            WHERE report_date = ? AND platform = ? AND video_id = ?
            """,
            (
                filename,
                extraction_dir,
                cover_asset,
                json.dumps(analysis, ensure_ascii=False, sort_keys=True),
                json.dumps(audit, ensure_ascii=False, sort_keys=True),
                now,
                report_date,
                platform,
                video_id,
            ),
        )
    except Exception as exc:
        conn.execute(
            """
            UPDATE hot_report_videos
            SET process_status = 'failed', process_error = ?, updated_at = ?
            WHERE report_date = ? AND platform = ? AND video_id = ?
            """,
            (str(exc), now, report_date, platform, video_id),
        )
    conn.commit()


def _build_summary_prompt(report_date: str, videos: list[dict[str, Any]]) -> str:
    compact_videos = []
    for video in videos:
        compact_videos.append(
            {
                "rank": video.get("report_rank"),
                "title": video.get("title"),
                "author": video.get("author"),
                "metrics": video.get("metrics"),
                "hot_score": video.get("hot_score"),
                "analysis": video.get("analysis"),
                "audit_result": video.get("audit_result"),
            }
        )
    return (
        "You are a senior short-video growth analyst. Based on multiple hot-video "
        "extraction results and analysis results, generate a daily report in Chinese. "
        "Return strict JSON only. Do not return Markdown. Required JSON keys: "
        "summary, common_patterns, hook_analysis, visual_patterns, topic_angles, "
        "execution_tactics, reusable_ideas, risks, next_actions. Focus on why these "
        "videos became hits, what patterns they share, and what can be reused in topic "
        "selection, hooks, scripts, visual rhythm, and interaction design.\n\n"
        f"report_date: {report_date}\nvideo_items:\n"
        f"{json.dumps(compact_videos, ensure_ascii=False, indent=2)}"
    )
    return (
        "你是短视频爆款研究员。请基于多个热视频的结构化提取内容和分析结果，生成一份中文日报。"
        "只返回严格 JSON，不要 Markdown。JSON keys 必须包含：summary, common_patterns, hook_analysis, "
        "visual_patterns, topic_angles, execution_tactics, reusable_ideas, risks, next_actions。"
        "重点解释这些视频为什么成为爆款、共通性是什么、可以复用到选题和脚本里的方法。"
        f"\n\nreport_date: {report_date}\nvideo_items:\n"
        f"{json.dumps(compact_videos, ensure_ascii=False, indent=2)}"
    )


def _markdown_from_report(report: dict[str, Any]) -> str:
    parts = [f"# {report.get('summary') or '爆款视频日报'}"]
    labels = {
        "common_patterns": "共通性",
        "hook_analysis": "开头与钩子",
        "visual_patterns": "视觉与节奏",
        "topic_angles": "选题角度",
        "execution_tactics": "执行手法",
        "reusable_ideas": "可复用选题",
        "risks": "风险",
        "next_actions": "下一步",
    }
    for key, label in labels.items():
        value = report.get(key)
        if not value:
            continue
        parts.append(f"## {label}")
        if isinstance(value, list):
            parts.extend(f"- {item}" for item in value)
        else:
            parts.append(str(value))
    return "\n\n".join(parts)


def _generate_daily_summary(report_date: str, success_videos: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing required environment variable: DEEPSEEK_API_KEY")
    prompt = _build_summary_prompt(report_date, success_videos)
    max_tokens = _to_int(os.getenv("REPORT_DEEPSEEK_MAX_TOKENS", os.getenv("DEEPSEEK_POSTPROCESS_MAX_TOKENS", "4096")))
    response = call_deepseek(
        api_key=api_key,
        prompt=prompt,
        api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        max_tokens=max_tokens,
    )
    content = extract_content(response)
    try:
        report = parse_json_content(content)
    except Exception:
        report = {"summary": content, "raw_result": content}
    return report, _markdown_from_report(report)


def _load_success_videos(conn: sqlite3.Connection, report_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.platform, m.video_id, m.title, m.author, m.source_url, COALESCE(rv.cover_url, m.cover_url),
               rv.local_filename, rv.extraction_dir, rv.source_endpoint, rv.source_label,
               rv.source_rank, rv.report_rank, rv.hot_score, rv.metrics_json, rv.raw_json,
               rv.process_status, rv.process_error, rv.analysis_json, rv.audit_json,
               rv.created_at, rv.updated_at
        FROM hot_report_videos rv
        JOIN hot_video_master m ON m.platform = rv.platform AND m.video_id = rv.video_id
        WHERE rv.report_date = ? AND rv.process_status = 'complete'
        ORDER BY rv.report_rank ASC
        """,
        (report_date,),
    ).fetchall()
    return [_row_to_video(row, include_raw=False) for row in rows]


def _cleanup_old_reports(conn: sqlite3.Connection) -> None:
    settings = get_settings()
    tz = ZoneInfo(settings["timezone"])
    cutoff = (datetime.now(tz) - timedelta(days=int(settings["retention_days"]) - 1)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM hot_report_videos WHERE report_date < ?", (cutoff,))
    conn.execute("DELETE FROM daily_reports WHERE report_date < ?", (cutoff,))
    conn.commit()


def delete_report(report_date: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date or ""):
        raise ValueError("report_date must be YYYY-MM-DD")
    with _connect() as conn:
        row = conn.execute("SELECT id FROM daily_reports WHERE report_date = ?", (report_date,)).fetchone()
        conn.execute("DELETE FROM hot_report_videos WHERE report_date = ?", (report_date,))
        conn.execute("DELETE FROM daily_reports WHERE report_date = ?", (report_date,))
        conn.commit()
    with _progress_lock:
        _progress_by_date.pop(report_date, None)
    return {"deleted": bool(row), "report_date": report_date}


def recover_interrupted_reports() -> dict[str, Any]:
    recovered: list[str] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, report_date FROM daily_reports WHERE status = 'running'"
        ).fetchall()
        for report_id, report_date in rows:
            counts = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN process_status = 'complete' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN process_status = 'failed' THEN 1 ELSE 0 END)
                FROM hot_report_videos WHERE report_date = ?
                """,
                (report_date,),
            ).fetchone()
            success_count = int(counts[0] if counts and counts[0] is not None else 0)
            failed_count = int(counts[1] if counts and counts[1] is not None else 0)
            if success_count:
                status = "partial_failed"
                error = "Report task was interrupted before the daily summary was generated. Click generate to retry."
            elif failed_count:
                status = "failed"
                error = "Report task was interrupted after video processing failures. Click generate to retry."
            else:
                status = "failed"
                error = "Report task was interrupted before video processing started. Click generate to retry."
            _finish_report(conn, str(report_id), str(report_date), status, error)
            recovered.append(str(report_date))
    return {"recovered": recovered}


def run_report(report_date: str | None = None, scheduled: bool = False) -> dict[str, Any]:
    settings = get_settings()
    date = report_date or today_key()
    region = os.getenv("SOCIAVAULT_REGION", "US").strip() or "US"
    analysis_limit = int(settings["analysis_limit"])
    target_count = analysis_limit
    recency_days = _recent_window_days()
    api_key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE).rstrip("/")
    sources = [
        {"endpoint": endpoint, "label": label, "params": params}
        for endpoint, params, label in _source_requests(region, target_count * 2)
    ]
    counts = {
        "collected": 0,
        "candidate_count": 0,
        "recent_count": 0,
        "enriched_count": 0,
        "skipped_old": 0,
        "skipped_missing_time": 0,
        "skipped_photo_mode": 0,
        "skipped_low_views_topic": 0,
        "skipped_low_views_stream": 0,
        "skipped_duplicate_report": 0,
        "analyzed_success": 0,
        "analyzed_failed": 0,
    }

    with _active_job_lock:
        global _active_job
        _active_job = date
    _progress_payload(date, "running", "collecting", 3, "开始采集热点视频", counts)
    try:
        with _connect() as conn:
            _cleanup_old_reports(conn)
            _cleanup_expired_video_records(conn, recency_days)
            report_id = _start_report(conn, date, region, sources, scheduled=scheduled)
            excluded_keys = _existing_report_video_keys(conn, date)
            if not api_key:
                error = "Missing required environment variable: SOCIAVAULT_API_KEY"
                _finish_report(conn, report_id, date, "failed", error)
                _progress_payload(date, "failed", "finished", 100, error, counts)
                return get_report(date, include_raw=True)
            try:
                api_timeout = float(os.getenv("SOCIAVAULT_TIMEOUT", "180"))
                candidates, source_errors = _collect_hot_video_candidates(
                    date,
                    region,
                    target_count,
                    recency_days,
                    api_key,
                    api_base,
                    api_timeout,
                    counts,
                    excluded_keys,
                )
                ranked = sorted(candidates.values(), key=lambda item: item["hot_score"], reverse=True)[:target_count]
                if not ranked:
                    suffix = f"; source errors: {' | '.join(source_errors[:3])}" if source_errors else ""
                    duplicate_note = (
                        f"; skipped already reported videos: {counts.get('skipped_duplicate_report', 0)}"
                        if counts.get("skipped_duplicate_report")
                        else ""
                    )
                    error = f"No new unique hot videos published in the last {recency_days} days{duplicate_note}{suffix}"
                    _finish_report(conn, report_id, date, "failed", error)
                    _progress_payload(date, "failed", "finished", 100, error, counts)
                    return get_report(date, include_raw=True)
                _progress_payload(date, "running", "filtering", 24, f"筛选出最近 {recency_days} 天热点视频 {len(ranked)} 条", counts)
                for report_rank, item in enumerate(ranked, start=1):
                    _upsert_video(conn, report_id, date, item, report_rank)
                conn.commit()
                _progress_payload(date, "running", "downloading", 30, f"已入库 {len(ranked)} 条，开始处理 Top {min(analysis_limit, len(ranked))}", counts)

                job_timeout = _to_float(os.getenv("REPORT_JOB_TIMEOUT", str(DEFAULT_REPORT_JOB_TIMEOUT_SECONDS)), DEFAULT_REPORT_JOB_TIMEOUT_SECONDS)
                deadline = time.time() + max(60.0, job_timeout)
                timed_out = False
                total_to_process = max(1, min(analysis_limit, len(ranked)))
                for index, item in enumerate(ranked[:analysis_limit], start=1):
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    record = get_video(item["platform"], item["video_id"]) or {}
                    filename = str(record.get("filename") or "")
                    extraction_dir = str(record.get("extraction_dir") or "")
                    output_dir = _output_dir_for_filename(filename) if filename else None
                    has_analysis = bool(output_dir and (output_dir / "analysis.json").is_file())
                    if not filename:
                        stage = "downloading"
                        message = f"下载视频 {index}/{total_to_process}"
                    elif not extraction_dir or not has_analysis:
                        stage = "extracting"
                        message = f"解析视频 {index}/{total_to_process}"
                    else:
                        stage = "analyzing"
                        message = f"分析视频 {index}/{total_to_process}"
                    _progress_payload(date, "running", stage, 30 + int(index / total_to_process * 50), message, counts)
                    _process_video(conn, date, item)
                    row = conn.execute(
                        "SELECT process_status FROM hot_report_videos WHERE report_date = ? AND platform = ? AND video_id = ?",
                        (date, item["platform"], item["video_id"]),
                    ).fetchone()
                    if row and row[0] == "complete":
                        counts["analyzed_success"] += 1
                    else:
                        counts["analyzed_failed"] += 1

                success_videos = _load_success_videos(conn, date)
                if timed_out:
                    if len(success_videos) >= 3:
                        _progress_payload(date, "running", "summarizing", 88, "处理超时，使用成功项生成日报", counts)
                        report_json, markdown = _generate_daily_summary(date, success_videos)
                        _finish_report(
                            conn,
                            report_id,
                            date,
                            "partial_failed",
                            f"Report job reached timeout ({int(job_timeout)}s), kept partial results",
                            report_json=report_json,
                            report_markdown=markdown,
                        )
                        _progress_payload(date, "partial_failed", "finished", 100, "任务超时，已保留部分日报结果", counts)
                        return get_report(date, include_raw=True)
                    _finish_report(
                        conn,
                        report_id,
                        date,
                        "partial_failed" if success_videos else "failed",
                        f"Report job reached timeout ({int(job_timeout)}s) before enough videos were processed",
                    )
                    _progress_payload(date, "partial_failed" if success_videos else "failed", "finished", 100, "任务超时，成功分析视频不足", counts)
                    return get_report(date, include_raw=True)
                if len(success_videos) >= 3:
                    _progress_payload(date, "running", "summarizing", 88, "开始生成爆款日报", counts)
                    report_json, markdown = _generate_daily_summary(date, success_videos)
                    _finish_report(conn, report_id, date, "complete", report_json=report_json, report_markdown=markdown)
                    _progress_payload(date, "complete", "finished", 100, "日报生成完成", counts)
                elif success_videos:
                    error = f"Only {len(success_videos)} videos analyzed successfully"
                    _finish_report(conn, report_id, date, "partial_failed", error)
                    _progress_payload(date, "partial_failed", "finished", 100, error, counts)
                else:
                    error = "No videos analyzed successfully"
                    _finish_report(conn, report_id, date, "failed", error)
                    _progress_payload(date, "failed", "finished", 100, error, counts)
            except Exception as exc:
                _finish_report(conn, report_id, date, "failed", str(exc))
                _progress_payload(date, "failed", "finished", 100, str(exc), counts)
    finally:
        with _active_job_lock:
            _active_job = None
    return get_report(date, include_raw=True)


def get_report_runtime_status() -> dict[str, Any]:
    with _active_job_lock:
        active = _active_job
    return {"active_date": active, "queued": list(_job_queue.queue)}


def enqueue_report(report_date: str | None = None) -> dict[str, Any]:
    date = report_date or today_key()
    _job_queue.put(date)
    _progress_payload(date, "queued", "queued", 0, "日报任务已排队", {})
    status = get_report_runtime_status()
    return {"queued": True, "report_date": date, "active_date": status.get("active_date"), "queued_dates": status.get("queued", [])}


def _scheduler_worker() -> None:
    while True:
        date = _job_queue.get()
        try:
            run_report(date, scheduled=True)
        except Exception as exc:
            print(f"Scheduled hot report failed for {date}: {exc}", flush=True)
        finally:
            _job_queue.task_done()


def _scheduler_loop() -> None:
    last_key = ""
    while True:
        try:
            settings = get_settings()
            tz = ZoneInfo(settings["timezone"])
            now = datetime.now(tz)
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if now.strftime("%H:%M") == settings["schedule_time"] and minute_key != last_key:
                last_key = minute_key
                enqueue_report(now.strftime("%Y-%m-%d"))
        except Exception as exc:
            print(f"Hot report scheduler error: {exc}", flush=True)
        time.sleep(60)


def start_report_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
        recover_interrupted_reports()
        threading.Thread(target=_scheduler_worker, daemon=True).start()
        threading.Thread(target=_scheduler_loop, daemon=True).start()
