"""Daily hot-video report storage, collection, analysis, and summary."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from contextlib import closing
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from deepseek_postprocess import DEFAULT_API_URL, DEFAULT_MODEL, call_deepseek, extract_content, parse_json_content
from sociavault_tiktok import call_api
from tools import _iter_media_url_candidates, execute_tool
from video_registry import get_video, get_video_by_filename, register_video, set_hidden_from_analyzer

ROOT = Path.cwd()
OUTPUT_DIR = ROOT / "output"
VIDEOS_DIR = ROOT / "videos"
DB_PATH = ROOT / "data" / "hot_video_report.sqlite"
REPORT_COVER_DIR = ROOT / "data" / "report_covers"
DEFAULT_API_BASE = "https://api.sociavault.com"
DEFAULT_TZ = "Asia/Shanghai"

_scheduler_started = False
_scheduler_lock = threading.Lock()
_job_queue: queue.Queue[str] = queue.Queue()
_active_job_lock = threading.Lock()
_active_job: str | None = None
_progress_lock = threading.Lock()
_progress_by_date: dict[str, dict[str, Any]] = {}
_translation_job_lock = threading.Lock()
_translation_jobs: set[tuple[str, str, str]] = set()
_db_initialize_lock = threading.Lock()
_initialized_db_path: Path | None = None


def today_key() -> str:
    return datetime.now(ZoneInfo(DEFAULT_TZ)).strftime("%Y-%m-%d")


def initialize_hot_report_db() -> None:
    """Create or migrate the report database once for the current process/path."""
    global _initialized_db_path
    target_path = DB_PATH.resolve()
    with _db_initialize_lock:
        if _initialized_db_path == target_path:
            return
        _initialize_hot_report_db(target_path)
        _initialized_db_path = target_path


def _initialize_hot_report_db(target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path, timeout=3)
    try:
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
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
            worker_lease TEXT,
            heartbeat_at REAL,
            resume_step TEXT,
            resume_error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
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
            analysis_sha256 TEXT,
            analysis_zh_json TEXT,
            analysis_zh_source_sha256 TEXT,
            audit_json TEXT,
            social_context_json TEXT,
            insight_json TEXT,
            insight_generated_at REAL,
            process_step TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at REAL,
            last_error_at REAL,
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
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    """Open a request/job connection after startup initialization has completed."""
    initialize_hot_report_db()
    conn = sqlite3.connect(DB_PATH, timeout=3)
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA foreign_keys=ON")
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
            "worker_lease": "TEXT",
            "heartbeat_at": "REAL",
            "resume_step": "TEXT",
            "resume_error": "TEXT",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    add_missing(
        "hot_report_videos",
        {
            "analysis_zh_json": "TEXT",
            "analysis_sha256": "TEXT",
            "analysis_zh_source_sha256": "TEXT",
            "social_context_json": "TEXT",
            "insight_json": "TEXT",
            "insight_generated_at": "REAL",
            "process_step": "TEXT NOT NULL DEFAULT 'pending'",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "last_attempt_at": "REAL",
            "last_error_at": "REAL",
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
            report_exists = bool(
                item["report_id"]
                and conn.execute("SELECT 1 FROM daily_reports WHERE id = ?", (item["report_id"],)).fetchone()
            )
            if report_exists:
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
        "topic_keywords": json.dumps(_split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys"), ensure_ascii=False),
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
    protected_report_keys: set[tuple[str, str]] = set()
    seen_keys: set[tuple[str, str]] = set()

    report_rows = conn.execute(
        """
        SELECT report_date, platform, video_id, report_rank, metrics_json, raw_json
        FROM hot_report_videos
        """
    ).fetchall()
    for report_date, platform, video_id, report_rank, metrics_json, raw_json in report_rows:
        key = (str(platform), str(video_id))
        seen_keys.add(key)
        if _to_int(report_rank) > 0:
            protected_report_keys.add(key)
        published_at = _published_at_from_row(metrics_json, raw_json, str(report_date or ""))
        if published_at is None:
            latest_publish_by_key[key] = 0
            continue
        if key not in latest_publish_by_key or published_at > latest_publish_by_key[key]:
            latest_publish_by_key[key] = float(published_at)

    stale_keys = {
        (platform, video_id)
        for (platform, video_id), published_at in latest_publish_by_key.items()
        if (platform, video_id) not in protected_report_keys and (published_at <= 0 or published_at < cutoff_ts)
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
    video = node.get("video")
    if isinstance(video, dict) and any(key in video for key in ("bit_rate", "play_addr", "download_addr")):
        return False
    if node.get("image_post_info") or node.get("imagePostInfo"):
        return True
    if not isinstance(video, dict):
        return False
    return bool(video.get("image_post_info") or video.get("imagePostInfo"))


def _has_usable_video_media(node: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    return bool(_iter_media_url_candidates(node))


def _long_video_max_seconds() -> int:
    return max(0, _to_int(os.getenv("REPORT_VIDEO_MAX_LONG_SECONDS", "180")) or 180)


def _is_long_video(item: dict[str, Any]) -> bool:
    """Reject videos longer than the configured cap (default 180s).

    Duration is read from ``item["duration_ms"]`` (already normalized).
    When the duration is missing/unknown we cannot prove it is too long,
    so it is conservatively kept (callers track it via counts).
    """
    duration_ms = item.get("duration_ms")
    if duration_ms is None:
        return False
    max_seconds = _long_video_max_seconds()
    if max_seconds <= 0:
        return False
    return int(duration_ms) > max_seconds * 1000


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


def _extract_duration_ms(node: dict[str, Any]) -> int | None:
    """Extract the video duration in milliseconds from a SociaVault node.

    The canonical field is ``node["video"]["duration"]`` (milliseconds).
    Fall back to top-level duration-ish fields for other providers.
    """
    video = node.get("video")
    if isinstance(video, dict):
        duration = video.get("duration")
        if duration not in (None, "", 0):
            try:
                return max(0, int(duration))
            except (TypeError, ValueError):
                pass
    duration = _first_present(node, ("duration", "duration_ms", "video_duration_ms"))
    if duration not in (None, "", 0):
        try:
            return max(0, int(duration))
        except (TypeError, ValueError):
            pass
    return None


def _normalize_video(node: dict[str, Any], endpoint: str, label: str, rank: int) -> dict[str, Any]:
    video_id = _clean_id(_first_present(node, ("aweme_id", "awemeId", "video_id", "videoId", "item_id", "itemId", "id")))
    title = _compact(_first_present(node, ("desc", "description", "title", "caption")))
    author = _compact(_find_nested(node, ("unique_id", "uniqueId", "nickname", "author", "authorName")), 160)
    duration_ms = _extract_duration_ms(node)
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
        "duration_ms": duration_ms,
        "metrics": metrics,
        "raw": node,
    }


def _score_hot_video(metrics: dict[str, int], rank: int) -> int:
    play_count = int(metrics.get("play_count") or 0)
    like_count = int(metrics.get("like_count") or 0)
    comment_count = int(metrics.get("comment_count") or 0)
    share_count = int(metrics.get("share_count") or 0)
    favorite_count = int(metrics.get("favorite_count") or 0)
    return (
        play_count
        + like_count * 8
        + comment_count * 15
        + share_count * 20
        + favorite_count * 10
        + max(0, 100 - rank) * 1_000
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
    topic_keywords: list[str],
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
    topic_max_pages = max(1, _to_int(os.getenv("HOT_VIDEO_TOPIC_MAX_PAGES", "3")))
    source_errors: list[str] = []
    topic_min_views = max(0, _to_int(os.getenv("HOT_VIDEO_TOPIC_MIN_PLAY_COUNT", "5000")))
    stream_min_views = max(0, _to_int(os.getenv("HOT_VIDEO_STREAM_MIN_PLAY_COUNT", "10000")))

    def collect_from(
        endpoint: str,
        params: dict[str, Any],
        label: str,
        min_play_count: int,
        bucket: str,
    ) -> tuple[int, Any | None]:
        _progress_payload(report_date, "running", "collecting", 6, f"Collecting source: {label}", counts)
        cache_policy = os.getenv("HOT_VIDEO_SOURCE_CACHE_POLICY", "record_only").strip() or "record_only"
        payload = call_api(api_key, api_base, endpoint, params, api_timeout, cache_policy=cache_policy)
        before_count = len(candidates)
        for rank, node in enumerate(_iter_video_nodes(payload), start=1):
            counts["collected"] += 1
            if _is_photo_mode_post(node):
                counts["skipped_photo_mode"] = counts.get("skipped_photo_mode", 0) + 1
                continue
            item = _normalize_video(node, endpoint, label, rank)
            if not item["video_id"]:
                continue
            counts["candidate_count"] += 1
            if item.get("duration_ms") is None:
                counts["unknown_duration"] = counts.get("unknown_duration", 0) + 1
            if _is_long_video(item):
                counts["skipped_long_video"] = counts.get("skipped_long_video", 0) + 1
                continue
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
            if not _has_usable_video_media(item.get("raw") or {}):
                item, enriched = _enrich_missing_publish_time(item, api_key, api_base, api_timeout)
                if enriched:
                    counts["enriched_count"] += 1
            if not _has_usable_video_media(item.get("raw") or {}):
                counts["skipped_no_video_media"] = counts.get("skipped_no_video_media", 0) + 1
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
        return len(candidates) - before_count, _response_next_cursor(payload)

    def collect_topic_pages(
        endpoint: str,
        params: dict[str, Any],
        label: str,
    ) -> int:
        added = 0
        cursor: Any | None = None
        seen_cursors: set[str] = set()
        for page in range(1, topic_max_pages + 1):
            page_params = dict(params)
            if cursor not in (None, ""):
                page_params["cursor"] = cursor
            page_label = label if page == 1 else f"{label}:p{page}"
            page_added, next_cursor = collect_from(endpoint, page_params, page_label, topic_min_views, "topic")
            added += page_added
            cursor_key = str(next_cursor or "")
            if not cursor_key or cursor_key in seen_cursors:
                break
            seen_cursors.add(cursor_key)
            cursor = next_cursor
        return added

    topic_fetch = max(target_count * 2, 20)
    for keyword in topic_keywords:
        before_topic = len(candidates)
        try:
            for endpoint, params, label in _topic_source_requests(keyword, region, topic_fetch, recency_days, fallback=False):
                collect_topic_pages(endpoint, params, label)
        except Exception as exc:
            source_errors.append(f"topic-search:{keyword}: {exc}")
        if len(candidates) > before_topic:
            continue
        counts["topic_fallback_sources"] = counts.get("topic_fallback_sources", 0) + 1
        for endpoint, params, label in _topic_source_requests(keyword, region, topic_fetch, recency_days, fallback=True):
            try:
                collect_topic_pages(endpoint, params, label)
                if len(candidates) > before_topic:
                    break
            except Exception as exc:
                source_errors.append(f"{label}: {exc}")

    page = 1
    while page <= max_pages and (page == 1 or len(candidates) < target_count):
        remaining = target_count - len(candidates)
        total_fetch = max(20, remaining * 3)
        popular_source_count = max(1, len(_split_csv_env("HOT_VIDEO_POPULAR_SORTS", "views,likes")))
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

    page = 1
    while page <= max_pages and (page == 1 or len(candidates) < target_count):
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
    report = _json_loads(data.pop("report_json"), None)
    if isinstance(report, dict):
        report = _normalize_report_for_display(report)
    data["report"] = report
    data["target_video_count"] = int(get_settings().get("analysis_limit") or 10)
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
        "analysis_zh_json",
        "audit_json",
        "social_context_json",
        "insight_json",
        "insight_generated_at",
        "created_at",
        "updated_at",
    )
    data = dict(zip(keys, row))
    data["metrics"] = _json_loads(data.pop("metrics_json"), {})
    raw = _json_loads(data.pop("raw_json"), {})
    data["analysis"] = _json_loads(data.pop("analysis_json"), None)
    data["analysis_zh"] = _json_loads(data.pop("analysis_zh_json"), None)
    data["audit_result"] = _json_loads(data.pop("audit_json"), None)
    data["social_context"] = _json_loads(data.pop("social_context_json"), None)
    data["insight"] = _json_loads(data.pop("insight_json"), None)
    if include_raw:
        data["raw"] = raw
    return data


def _report_player_base_url() -> str:
    configured = (
        os.getenv("REPORT_LAN_BASE_URL")
        or os.getenv("REPORT_PUBLIC_BASE_URL")
        or os.getenv("WEB_PUBLIC_BASE_URL")
        or ""
    ).strip()
    if configured:
        return configured.rstrip("/")
    host = (os.getenv("REPORT_LAN_HOST") or os.getenv("LAN_HOST") or "192.168.1.254").strip()
    port = (os.getenv("REPORT_LAN_PORT") or os.getenv("WEB_PORT") or "4000").strip()
    return f"http://{host}:{port}".rstrip("/")


def _report_player_url(report_date: str, video: dict[str, Any]) -> str:
    params = urllib.parse.urlencode(
        {
            "date": report_date,
            "platform": video.get("platform") or "",
            "video_id": video.get("video_id") or "",
        }
    )
    return f"{_report_player_base_url()}/report/player?{params}"


def _link_report_video_heading(text: str, rank: int, url: str) -> str:
    if not text or not rank or not url or url in text:
        return text
    linked = f"[**视频 {rank}**]({url})"
    pattern = re.compile(rf"(?<!\[)\*\*视频\s*{re.escape(str(rank))}\*\*")
    updated, count = pattern.subn(linked, text, count=1)
    if count:
        return updated
    pattern = re.compile(rf"(?<!\[)\b视频\s*{re.escape(str(rank))}\b")
    return pattern.sub(linked, text, count=1)


def _attach_report_player_links(report: dict[str, Any], videos: list[dict[str, Any]], report_date: str) -> None:
    if not videos:
        return
    rank_links: dict[int, str] = {}
    for index, video in enumerate(videos, start=1):
        if not video.get("platform") or not video.get("video_id"):
            continue
        rank = _to_int(video.get("report_rank")) or index
        rank_links[rank] = _report_player_url(report_date, video)

    def link_value(value: Any) -> Any:
        if isinstance(value, list):
            linked = []
            for index, item in enumerate(value, start=1):
                rank = _to_int(item.get("rank")) if isinstance(item, dict) else index
                if isinstance(item, str) and rank in rank_links:
                    linked.append(_link_report_video_heading(item, rank, rank_links[rank]))
                elif isinstance(item, dict):
                    linked.append({key: link_value(child) for key, child in item.items()})
                else:
                    linked.append(item)
            return linked
        if isinstance(value, dict):
            return {key: link_value(child) for key, child in value.items()}
        return value

    body = report.get("report")
    if isinstance(body, dict):
        report["report"] = link_value(body)
    markdown = str(report.get("report_markdown") or "")
    for rank, url in rank_links.items():
        markdown = _link_report_video_heading(markdown, rank, url)
    if markdown:
        report["report_markdown"] = markdown


def get_settings() -> dict[str, Any]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT key, value FROM report_settings").fetchall()
    values = {str(k): str(v) for k, v in rows}
    topics = _normalize_topic_keywords(
        values.get("topic_keywords"),
        fallback=_split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys"),
    )
    return {
        "schedule_time": values.get("schedule_time", "05:00"),
        "timezone": values.get("timezone", DEFAULT_TZ),
        "analysis_limit": _to_int(values.get("analysis_limit", "10")) or 10,
        "retention_days": _to_int(values.get("retention_days", "30")) or 30,
        "topic_keywords": topics,
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
    topic_keywords = _normalize_topic_keywords(payload.get("topic_keywords", current.get("topic_keywords", [])))
    if not topic_keywords:
        raise ValueError("topic_keywords must contain at least one topic")
    if len(topic_keywords) > analysis_limit:
        raise ValueError("topic_keywords count must be less than or equal to analysis_limit")
    now = time.time()
    with closing(_connect()) as conn:
        for key, value in {
            "schedule_time": schedule_time,
            "timezone": timezone,
            "analysis_limit": str(analysis_limit),
            "retention_days": str(retention_days),
            "topic_keywords": json.dumps(topic_keywords, ensure_ascii=False),
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
    with closing(_connect()) as conn:
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
    with closing(_connect()) as conn:
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
                       rv.analysis_json, rv.analysis_zh_json, rv.audit_json, rv.social_context_json, rv.insight_json,
                       rv.insight_generated_at, rv.created_at, rv.updated_at
                FROM hot_report_videos rv
                JOIN hot_video_master m ON m.platform = rv.platform AND m.video_id = rv.video_id
                LEFT JOIN hot_video_master v ON v.platform = rv.platform AND v.video_id = rv.video_id
                WHERE rv.report_date = ? AND rv.process_status = 'complete'
                ORDER BY rv.report_rank ASC, rv.hot_score DESC
                """,
                (date,),
            ).fetchall()
    report = _row_to_report(report_row)
    report["exists"] = True
    videos = [_row_to_video(row, include_raw=True) for row in rows] if detail else []
    videos = [video for video in videos if _is_video_checkpoint_valid(video)[0]] if detail else []
    if detail and not include_raw:
        for video in videos:
            video.pop("raw", None)
    report["videos"] = [_prepare_cover_asset(video) for video in videos] if detail else []
    if detail:
        _attach_report_player_links(report, report["videos"], date)
    return report


def _translate_analysis_payload(analysis: Any) -> Any:
    if not analysis:
        raise ValueError("analysis not found for report video")

    from translate_analysis import DEFAULT_BATCH_CHARS, DEFAULT_MAX_TOKENS, compact_for_translation, translate_in_batches

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required for translation")
    return translate_in_batches(
        api_key=api_key,
        api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        payload=compact_for_translation(analysis),
        max_chars=int(os.getenv("TRANSLATION_BATCH_CHARS", str(DEFAULT_BATCH_CHARS))),
        max_tokens=int(os.getenv("TRANSLATION_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
    )


def _analysis_sha256(analysis: Any) -> str:
    payload = json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def translate_report_video_analysis(report_date: str, platform: str, video_id: str, force: bool = False) -> dict[str, Any]:
    date = str(report_date or today_key()).strip()
    platform = str(platform or "").strip()
    video_id = str(video_id or "").strip()
    if not date or not platform or not video_id:
        raise ValueError("report_date, platform and video_id are required")
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT analysis_json, analysis_zh_json, analysis_sha256, analysis_zh_source_sha256
            FROM hot_report_videos
            WHERE report_date = ? AND platform = ? AND video_id = ?
            """,
            (date, platform, video_id),
        ).fetchone()
        if not row:
            raise ValueError("report video not found")
        analysis = _json_loads(row[0], None)
        cached = _json_loads(row[1], None)
        if not analysis:
            raise ValueError("analysis not found for report video")
        source_sha256 = _analysis_sha256(analysis)
        stored_source_sha256 = str(row[2] or "")
        translated_source_sha256 = str(row[3] or "")
        cache_matches_source = bool(cached and translated_source_sha256 == source_sha256)
        if force or not cache_matches_source:
            conn.execute(
                """
                UPDATE hot_report_videos
                SET analysis_sha256 = ?, analysis_zh_json = NULL,
                    analysis_zh_source_sha256 = NULL, updated_at = ?
                WHERE report_date = ? AND platform = ? AND video_id = ?
                """,
                (source_sha256, time.time(), date, platform, video_id),
            )
            conn.commit()
            cached = None
        elif stored_source_sha256 != source_sha256:
            conn.execute(
                "UPDATE hot_report_videos SET analysis_sha256 = ?, updated_at = ? WHERE report_date = ? AND platform = ? AND video_id = ?",
                (source_sha256, time.time(), date, platform, video_id),
            )
            conn.commit()
        if cached:
            return {"status": "cached", "report_date": date, "platform": platform, "video_id": video_id, "analysis_zh": cached}
        translated = _translate_analysis_payload(analysis)
        cursor = conn.execute(
            """
            UPDATE hot_report_videos
            SET analysis_zh_json = ?, analysis_zh_source_sha256 = ?, updated_at = ?
            WHERE report_date = ? AND platform = ? AND video_id = ? AND analysis_sha256 = ?
            """,
            (json.dumps(translated, ensure_ascii=False, sort_keys=True), source_sha256, time.time(), date, platform, video_id, source_sha256),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("analysis changed while translation was running; stale translation was discarded")
        conn.commit()
    return {"status": "translated", "report_date": date, "platform": platform, "video_id": video_id, "analysis_zh": translated}


def _auto_translate_enabled() -> bool:
    return os.getenv("REPORT_AUTO_TRANSLATE_ANALYSIS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _run_report_video_translation_job(report_date: str, platform: str, video_id: str) -> None:
    key = (report_date, platform, video_id)
    try:
        result = translate_report_video_analysis(report_date, platform, video_id)
        print(f"[hot-report] background translation {result.get('status')} for {platform}:{video_id}")
    except Exception as exc:
        print(f"[hot-report] background translation failed for {platform}:{video_id}: {exc}")
    finally:
        with _translation_job_lock:
            _translation_jobs.discard(key)


def _enqueue_report_video_translation(report_date: str, platform: str, video_id: str) -> None:
    if not _auto_translate_enabled() or not report_date or not platform or not video_id:
        return
    key = (str(report_date), str(platform), str(video_id))
    with _translation_job_lock:
        if key in _translation_jobs:
            return
        _translation_jobs.add(key)
    threading.Thread(
        target=_run_report_video_translation_job,
        args=key,
        daemon=True,
        name=f"hot-report-translate-{key[1]}-{key[2]}",
    ).start()


def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    settings = get_settings()
    retention_days = int(settings["retention_days"])
    cutoff = (datetime.now(ZoneInfo(settings["timezone"])) - timedelta(days=retention_days - 1)).strftime("%Y-%m-%d")
    with closing(_connect()) as conn:
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


def _response_next_cursor(payload: dict[str, Any]) -> Any | None:
    containers: list[dict[str, Any]] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
        nested = data.get("data")
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        has_more = _first_present(container, ("has_more", "hasMore", "more"))
        if has_more not in (None, "", [], {}):
            normalized = str(has_more).strip().lower()
            if normalized in {"0", "false", "no", "off"}:
                return None
        cursor = _first_present(container, ("cursor", "next_cursor", "nextCursor"))
        if cursor not in (None, "", 0, "0", [], {}):
            return cursor
    return None


def _sociavault_time_window(recency_days: int) -> str:
    days = max(1, _to_int(recency_days))
    if days <= 1:
        return "yesterday"
    if days <= 7:
        return "this-week"
    if days <= 31:
        return "this-month"
    if days <= 90:
        return "last-3-months"
    if days <= 180:
        return "last-6-months"
    return "all-time"


def _topic_sort_by() -> str:
    value = (os.getenv("HOT_VIDEO_TOPIC_SORT_BY", "most-liked").strip() or "most-liked").lower()
    aliases = {
        "create_time": "date-posted",
        "create-time": "date-posted",
        "views": "most-liked",
        "likes": "most-liked",
    }
    value = aliases.get(value, value)
    return value if value in {"relevance", "most-liked", "date-posted"} else "most-liked"


def _normalize_topic_keywords(value: Any, fallback: list[str] | None = None) -> list[str]:
    if value in (None, ""):
        raw_items = fallback or []
    elif isinstance(value, str):
        parsed = _json_loads(value, None)
        raw_items = parsed if isinstance(parsed, list) else value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    topics: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        topic = re.sub(r"\s+", " ", str(item or "")).strip()
        if not topic:
            continue
        topic = topic[:80]
        key = topic.casefold()
        if key in seen:
            continue
        seen.add(key)
        topics.append(topic)
    return topics


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


def _topic_source_requests(
    keyword: str,
    region: str,
    count: int,
    recency_days: int,
    fallback: bool = False,
) -> list[tuple[str, dict[str, Any], str]]:
    topic = str(keyword or "").strip()
    if not topic:
        return []
    time_window = _sociavault_time_window(recency_days)
    if not fallback:
        params = {
            "query": topic,
            "region": region,
            "publish_time": time_window,
            "sort_by": _topic_sort_by(),
        }
        return [("search-top", params, f"topic-search-top:{topic}")]
    requests: list[tuple[str, dict[str, Any], str]] = [
        (
            "search-keyword",
            {
                "query": topic,
                "region": region,
                "date_posted": time_window,
                "sort_by": _topic_sort_by(),
            },
            f"topic-search-keyword:{topic}",
        ),
    ]
    hashtag = re.sub(r"^\s*#", "", topic).strip()
    if hashtag and " " not in hashtag:
        requests.append(
            (
                "search-hashtag",
                {"hashtag": hashtag, "region": region},
                f"topic-search-hashtag:{hashtag}",
            )
        )
    return requests


def _trending_source_requests(region: str, count: int, page: int) -> list[tuple[str, dict[str, Any], str]]:
    params = {"region": region, "count": count}
    if page > 1:
        params["page"] = page
    return [("trending", params, f"trending:{region}:p{page}")]


def _legacy_source_requests(region: str, count: int) -> list[tuple[str, dict[str, Any], str]]:
    keywords = get_settings().get("topic_keywords") or _split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys")
    requests: list[tuple[str, dict[str, Any], str]] = []
    if os.getenv("HOT_VIDEO_INCLUDE_TRENDING", "0").strip() in {"1", "true", "yes"}:
        requests.append(("trending", {"region": region, "count": count}, f"trending:{region}"))
    for keyword in keywords:
        requests.append(("search-top", {"query": keyword, "region": region, "count": count}, f"search-top:{keyword}"))
    return requests


def _source_requests(region: str, count: int, topic_keywords: list[str] | None = None) -> list[tuple[str, dict[str, Any], str]]:
    requests: list[tuple[str, dict[str, Any], str]] = []
    for keyword in (topic_keywords or _split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys")):
        requests.extend(_topic_source_requests(keyword, region, count, _recent_window_days(), fallback=False))
    requests.extend(_popular_source_requests(region, max(1, count // 2), 1, _recent_window_days()))
    requests.extend(_trending_source_requests(region, count, 1))
    return requests


def _report_backup_count() -> int:
    return max(0, _to_int(os.getenv("REPORT_VIDEO_BACKUP_COUNT", "10")) or 10)


def _rank_with_topic_guarantees(
    candidates: list[dict[str, Any]],
    topic_keywords: list[str],
    target_count: int,
    backup_count: int | None = None,
) -> list[dict[str, Any]]:
    backup_count = _report_backup_count() if backup_count is None else max(0, int(backup_count))
    pool_size = target_count + backup_count
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    sorted_candidates = sorted(candidates, key=lambda item: item["hot_score"], reverse=True)
    if topic_keywords and target_count >= len(topic_keywords):
        for keyword in topic_keywords:
            label_prefix = "topic-search-"
            topic_names = {str(keyword or "").strip(), re.sub(r"^\s*#", "", str(keyword or "")).strip()}
            topic_names.discard("")
            topic_items = [
                item
                for item in sorted_candidates
                if item.get("selection_bucket") == "topic"
                and str(item.get("source_label") or "").startswith(label_prefix)
                and any(
                    str(item.get("source_label") or "").endswith(f":{name}")
                    or f":{name}:" in str(item.get("source_label") or "")
                    for name in topic_names
                )
            ]
            if not topic_items:
                continue
            item = topic_items[0]
            key = (item["platform"], item["video_id"])
            if key not in selected_keys:
                selected.append(item)
                selected_keys.add(key)
    for item in sorted_candidates:
        if len(selected) >= pool_size:
            break
        key = (item["platform"], item["video_id"])
        if key in selected_keys:
            continue
        selected.append(item)
        selected_keys.add(key)
    # Mark primary (first target_count) vs backup (rest). Primary is processed
    # first; backups fill in when a primary fails all its retries.
    for index, item in enumerate(selected[:pool_size]):
        item["selection_tier"] = "primary" if index < target_count else "backup"
    return selected[:pool_size]


def _start_report(
    conn: sqlite3.Connection,
    report_date: str,
    region: str,
    sources: list[dict[str, Any]],
    scheduled: bool = False,
    force_reset: bool = False,
    worker_lease: str | None = None,
) -> str:
    now = time.time()
    report_id = uuid.uuid4().hex
    existing = conn.execute(
        "SELECT id, status FROM daily_reports WHERE report_date = ?",
        (report_date,),
    ).fetchone()
    if existing and str(existing[1]) == "complete" and not force_reset:
        return str(existing[0])
    if force_reset:
        conn.execute("DELETE FROM hot_report_videos WHERE report_date = ?", (report_date,))
    conn.execute(
        """
        INSERT INTO daily_reports (
            id, report_date, status, region, sources_json, video_count, error,
            report_json, report_markdown, analysis_success_count, analysis_failed_count,
            llm_generated_at, scheduled_at, worker_lease, heartbeat_at, resume_step, resume_error,
            attempt_count, created_at, updated_at
        )
        VALUES (?, ?, 'running', ?, ?, 0, NULL, NULL, NULL, 0, 0, NULL, ?, ?, ?, NULL, NULL, 1, ?, ?)
        ON CONFLICT(report_date) DO UPDATE SET
            status = 'running',
            region = excluded.region,
            sources_json = excluded.sources_json,
            error = NULL,
            scheduled_at = excluded.scheduled_at,
            worker_lease = excluded.worker_lease,
            heartbeat_at = excluded.heartbeat_at,
            resume_step = NULL,
            resume_error = NULL,
            attempt_count = daily_reports.attempt_count + 1,
            updated_at = excluded.updated_at
        """,
        (
            report_id, report_date, region, json.dumps(sources, ensure_ascii=False), now if scheduled else None,
            worker_lease, now, now, now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM daily_reports WHERE report_date = ?",
        (report_date,),
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"daily report was not persisted for {report_date}")
    return str(row[0])


def notify_daily_report_completed(report_date: str, status: str) -> dict[str, Any]:
    url = os.getenv("VIDEO_DAILY_REPORT_COMPLETED_URL", "").strip()
    if not url:
        return {"sent": False, "reason": "url_not_configured"}
    payload = json.dumps({"date": report_date, "status": status}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.getenv("VIDEO_DAILY_REPORT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = float(os.getenv("VIDEO_DAILY_REPORT_CALLBACK_TIMEOUT", "10"))
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"sent": True, "status_code": getattr(resp, "status", None)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"sent": False, "error": str(exc)}


def _finish_report(
    conn: sqlite3.Connection,
    report_id: str,
    report_date: str,
    status: str,
    error: str = "",
    report_json: dict[str, Any] | None = None,
    report_markdown: str = "",
    resume_step: str = "",
) -> None:
    now = time.time()
    videos = _load_report_videos(conn, report_date, include_raw=True)
    success_count = sum(
        1 for video in videos
        if video.get("process_status") == "complete" and _is_video_checkpoint_valid(video)[0]
    )
    failed_count = sum(1 for video in videos if video.get("process_status") in {"failed", "paused_external"})
    video_count = success_count
    llm_generated_marker = 1 if report_json else None
    cursor = conn.execute(
        """
        UPDATE daily_reports
        SET status = ?, video_count = ?, error = ?, report_json = COALESCE(?, report_json),
            report_markdown = COALESCE(?, report_markdown), analysis_success_count = ?,
            analysis_failed_count = ?, llm_generated_at = CASE WHEN ? IS NULL THEN llm_generated_at ELSE ? END,
            worker_lease = NULL, heartbeat_at = NULL,
            resume_step = CASE WHEN ? = '' THEN NULL ELSE ? END,
            resume_error = CASE WHEN ? = '' THEN NULL ELSE ? END,
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
            resume_step,
            resume_step,
            resume_step,
            error,
            now,
            report_id,
        ),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError(f"daily report ID mismatch while finishing {report_date}: {report_id}")
    conn.commit()
    if status == "complete":
        callback = notify_daily_report_completed(report_date, status)
        if callback.get("sent"):
            print(f"Video daily report completed callback sent for {report_date}", flush=True)
        elif callback.get("error"):
            print(f"Video daily report completed callback failed for {report_date}: {callback.get('error')}", flush=True)


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


def _is_video_checkpoint_valid(video: dict[str, Any]) -> tuple[bool, str]:
    """Return whether a completed row has every artifact required for reuse."""
    if not str(video.get("platform") or "").strip() or not str(video.get("video_id") or "").strip():
        return False, "missing video identity"
    if _to_int(video.get("report_rank")) < 1:
        return False, "missing report rank"
    if not isinstance(video.get("raw"), dict) or not video.get("raw"):
        return False, "missing raw payload"
    if not isinstance(video.get("metrics"), dict):
        return False, "invalid metrics payload"
    filename = str(video.get("local_filename") or "").strip()
    if not filename or not (VIDEOS_DIR / filename).is_file():
        return False, "missing local video file"
    analysis = video.get("analysis")
    if not isinstance(analysis, dict) or not analysis:
        return False, "missing analysis"
    if not (_output_dir_for_filename(filename) / "analysis.json").is_file():
        return False, "missing analysis artifact"
    if not _is_valid_video_insight(video.get("insight")):
        return False, "missing or failed insight"
    social_context = video.get("social_context")
    if social_context is not None and not isinstance(social_context, dict):
        return False, "invalid social context"
    return True, ""


def _mark_video_pending(conn: sqlite3.Connection, report_date: str, platform: str, video_id: str, reason: str) -> None:
    conn.execute(
        """
        UPDATE hot_report_videos
        SET process_status = 'pending', process_step = 'pending', process_error = ?, updated_at = ?
        WHERE report_date = ? AND platform = ? AND video_id = ?
        """,
        (reason, time.time(), report_date, platform, video_id),
    )
    conn.commit()


def _is_recoverable_external_error(error: BaseException | str) -> bool:
    text = str(error).lower()
    return bool(
        re.search(
            r"\b(?:http(?:\s+(?:status|error))?|status(?:\s+code)?|client error|server error)\D{0,16}"
            r"(?:402|408|429|500|502|503|504)\b|\b(?:402|408|429|500|502|503|504)\s+(?:client|server)\s+error\b",
            text,
        )
    )


def _report_video_analyze_timeout_seconds() -> int:
    return max(30, min(_to_int(os.getenv("REPORT_VIDEO_ANALYZE_TIMEOUT", "240")) or 240, 1800))


def _report_video_download_timeout_seconds() -> int | None:
    raw = os.getenv("REPORT_VIDEO_DOWNLOAD_TIMEOUT", "")
    if raw in (None, "", "0"):
        return None
    return max(30, min(_to_int(raw) or 240, 1800))


def _failed_video_retry_state(
    conn: sqlite3.Connection,
    report_date: str,
    platform: str,
    video_id: str,
    now: float | None = None,
) -> str | None:
    row = conn.execute(
        """
        SELECT process_status, attempt_count, last_attempt_at
        FROM hot_report_videos
        WHERE report_date = ? AND platform = ? AND video_id = ?
        """,
        (report_date, platform, video_id),
    ).fetchone()
    if not row or str(row[0] or "") not in {"failed", "paused_external"}:
        return None
    attempts = _to_int(row[1])
    max_attempts = max(1, _to_int(os.getenv("REPORT_VIDEO_MAX_ATTEMPTS", "2")) or 2)
    if attempts >= max_attempts:
        return "retry_exhausted"
    last_attempt_at = _to_float(row[2], 0.0)
    base_delay = max(0.0, _to_float(os.getenv("REPORT_VIDEO_RETRY_BACKOFF_SECONDS", "0"), 0.0))
    retry_at = last_attempt_at + base_delay * (2 ** max(0, attempts - 1))
    if last_attempt_at and (now if now is not None else time.time()) < retry_at:
        return "retry_backoff"
    return None


def _prioritize_retry_candidates(conn: sqlite3.Connection, report_date: str, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    primary: list[dict[str, Any]] = []
    retries: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for item in ranked:
        state = _failed_video_retry_state(conn, report_date, str(item.get("platform") or ""), str(item.get("video_id") or ""))
        if state:
            deferred.append(item)
        elif str(item.get("process_status") or "") in {"failed", "paused_external"}:
            retries.append(item)
        else:
            primary.append(item)
    return primary + retries + deferred


def _process_video(conn: sqlite3.Connection, report_date: str, item: dict[str, Any]) -> None:
    now = time.time()
    platform = item["platform"]
    video_id = item["video_id"]
    completed = False
    record = get_video(platform, video_id) or {}
    filename = str(record.get("filename") or "")
    extraction_dir = str(record.get("extraction_dir") or "")
    source_url = item.get("source_url") or record.get("source_url") or ""
    was_visible_manual_video = bool(filename and not int(record.get("hidden_from_analyzer") or 0))
    conn.execute(
        """
        UPDATE hot_report_videos
        SET process_status = 'processing', process_step = 'processing', process_error = NULL,
            attempt_count = attempt_count + 1, last_attempt_at = ?, updated_at = ?
        WHERE report_date = ? AND platform = ? AND video_id = ?
        """,
        (now, now, report_date, platform, video_id),
    )
    conn.commit()
    try:
        if not filename:
            if not source_url:
                raise RuntimeError("missing source_url")
            result = execute_tool(
                "video_download",
                {"url": source_url, "timeout_seconds": _report_video_download_timeout_seconds()},
            )
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
        analysis_path = output_dir / "analysis.json"
        if not analysis_path.is_file():
            result = execute_tool(
                "video_analyze",
                {"filename": filename, "timeout_seconds": _report_video_analyze_timeout_seconds()},
            )
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "video extraction failed"))
        output_dir = _output_dir_for_filename(filename)
        analysis = _json_loads((output_dir / "analysis.json").read_text(encoding="utf-8") if (output_dir / "analysis.json").is_file() else "", {})
        analysis_sha256 = _analysis_sha256(analysis)
        registry = get_video(platform, video_id) or get_video_by_filename(filename) or {}
        extraction_dir = str(registry.get("extraction_dir") or output_dir.name)
        cover_asset = _download_cover_asset(str(item.get("cover_url") or ""), platform, video_id)
        if not cover_asset:
            cover_asset = _snapshot_cover_asset(filename, platform, video_id)
        video_for_insight = {
            "platform": platform,
            "video_id": video_id,
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "source_url": source_url,
            "source_label": item.get("source_label", ""),
            "report_rank": item.get("report_rank"),
            "metrics": item.get("metrics") or {},
            "hot_score": item.get("hot_score"),
            "raw": item.get("raw") or {},
            "analysis": analysis,
        }
        social_context, insight = _ensure_video_insight(conn, report_date, platform, video_id, video_for_insight)
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
            SET process_status = 'complete', process_step = 'complete', process_error = NULL, local_filename = ?, extraction_dir = ?,
                cover_url = COALESCE(NULLIF(?, ''), cover_url),
                analysis_json = ?, analysis_sha256 = ?,
                analysis_zh_json = NULL, analysis_zh_source_sha256 = NULL,
                audit_json = ?, social_context_json = ?, insight_json = ?,
                insight_generated_at = ?, updated_at = ?
            WHERE report_date = ? AND platform = ? AND video_id = ?
            """,
            (
                filename,
                extraction_dir,
                cover_asset,
                json.dumps(analysis, ensure_ascii=False, sort_keys=True),
                analysis_sha256,
                None,
                json.dumps(social_context, ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(insight, ensure_ascii=False, sort_keys=True, default=str),
                now,
                now,
                report_date,
                platform,
                video_id,
            ),
        )
        completed = True
    except Exception as exc:
        conn.execute(
            """
            UPDATE hot_report_videos
            SET process_status = ?, process_step = 'failed', process_error = ?, last_error_at = ?, updated_at = ?
            WHERE report_date = ? AND platform = ? AND video_id = ?
            """,
            ("paused_external" if _is_recoverable_external_error(exc) else "failed", str(exc), now, now, report_date, platform, video_id),
        )
    conn.commit()
    if completed:
        _enqueue_report_video_translation(report_date, platform, video_id)


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
        "video_deep_dives": "逐条爆点拆解",
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


REPORT_SECTION_LABELS = {
    "summary": "\u603b\u4f53\u7ed3\u8bba",
    "overall_conclusion": "\u603b\u4f53\u7ed3\u8bba",
    "common_patterns": "\u7206\u6b3e\u5171\u901a\u6027",
    "hook_analysis": "\u5f00\u5934\u4e0e\u94a9\u5b50",
    "visual_patterns": "\u89c6\u89c9\u4e0e\u8282\u594f",
    "topic_angles": "\u9009\u9898\u89d2\u5ea6",
    "execution_tactics": "\u6267\u884c\u624b\u6cd5",
    "video_deep_dives": "\u9010\u6761\u7206\u70b9\u62c6\u89e3",
    "reusable_ideas": "\u53ef\u590d\u7528\u9009\u9898",
    "risks": "\u98ce\u9669",
    "next_actions": "\u4e0b\u4e00\u6b65",
}

REPORT_VALUE_LABELS = {
    "rank": "\u6392\u540d",
    "title": "\u6807\u9898",
    "boom_reason": "\u7206\u70b9\u539f\u56e0",
    "core_boom_reason": "\u6838\u5fc3\u7206\u70b9",
    "hook": "\u5f00\u5934\u94a9\u5b50",
    "structure": "\u5185\u5bb9\u7ed3\u6784",
    "content_structure": "\u5185\u5bb9\u7ed3\u6784",
    "visual_language": "\u89c6\u89c9\u8bed\u8a00",
    "audience_trigger": "\u53d7\u4f17\u89e6\u53d1\u70b9",
    "comment_signal": "\u8bc4\u8bba\u4fe1\u53f7",
    "creator_context": "\u535a\u4e3b\u80cc\u666f",
    "engagement_driver": "\u4e92\u52a8\u9a71\u52a8",
    "replicable_formula": "\u53ef\u590d\u7528\u516c\u5f0f",
    "adaptation_ideas": "\u6539\u7f16\u65b9\u5411",
    "weakness_or_risk": "\u98ce\u9669\u6216\u4e0d\u53ef\u590d\u5236\u70b9",
    "risk": "\u98ce\u9669",
    "evidence_quotes": "\u8bc1\u636e\u6458\u8981",
    "one_sentence": "\u4e00\u53e5\u8bdd\u6982\u62ec",
}


def _label_for_report_key(key: str) -> str:
    return REPORT_VALUE_LABELS.get(key) or REPORT_SECTION_LABELS.get(key) or str(key)


def _inline_report_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _inline_report_text(item)
            if text:
                parts.append(f"{_label_for_report_key(str(key))}\uff1a{text}")
        return "\uff1b".join(parts)
    if isinstance(value, list):
        return "\uff1b".join(_inline_report_text(item) for item in value if item)
    return str(value).strip()


def _report_item_to_text(item: Any) -> str:
    if not isinstance(item, dict):
        return _inline_report_text(item)
    title = _inline_report_text(item.get("title"))
    rank = _inline_report_text(item.get("rank"))
    head = f"**\u89c6\u9891 {rank}**" if rank else "**\u89c6\u9891**"
    if title:
        head = f"{head}\uff5c{title}"
    parts = [head]
    for key, value in item.items():
        if key in {"rank", "title"} or value in (None, "", [], {}):
            continue
        text = _inline_report_text(value)
        if text:
            parts.append(f"  - **{_label_for_report_key(str(key))}**\uff1a{text}")
    return "\n\n".join(parts)


def _bold_report_line_labels(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix = match.group(1)
        label = match.group(2)
        if label.startswith("**"):
            return match.group(0)
        return f"{prefix}**{label}**"

    text = str(text or "")
    text = re.sub(r"(^|\n)(\s*视频\s*\d+｜[^\n]+)", repl, text)
    return re.sub(r"(^|\n)(\s*[\u4e00-\u9fa5A-Za-z0-9/]{2,14}：)", repl, text)


def _normalize_report_for_display(report: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(report)
    for key, value in list(normalized.items()):
        if key == "generation" and isinstance(value, dict):
            continue
        if key == "video_deep_dives" and isinstance(value, list):
            normalized[key] = [
                _report_item_to_text(item) if isinstance(item, dict) else _bold_report_line_labels(_inline_report_text(item))
                for item in value
                if item
            ]
        elif isinstance(value, list):
            normalized[key] = [
                _report_item_to_text(item) if isinstance(item, dict) else _bold_report_line_labels(_inline_report_text(item))
                for item in value
                if item
            ]
        elif isinstance(value, dict):
            normalized[key] = _inline_report_text(value)
        elif isinstance(value, str):
            normalized[key] = _bold_report_line_labels(value)
    return normalized


def _markdown_value(value: Any, depth: int = 0) -> list[str]:
    if value in (None, "", [], {}):
        return []
    indent = "  " * depth
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            if isinstance(item, dict):
                head = _report_item_to_text({"rank": item.get("rank"), "title": item.get("title")}).strip()
                item_title = head or "\u9879\u76ee"
                lines.append(f"{indent}- {item_title}")
                for key, child in item.items():
                    if key in {"rank", "title"} or child in (None, "", [], {}):
                        continue
                    child_text = _inline_report_text(child)
                    if child_text:
                        lines.append(f"{indent}  - **{_label_for_report_key(str(key))}**\uff1a{child_text}")
            else:
                text = _bold_report_line_labels(_inline_report_text(item))
                if text:
                    if "\n" in text:
                        lines.append(f"{indent}{text}")
                    else:
                        lines.append(f"{indent}- {text}")
        return lines
    if isinstance(value, dict):
        lines = []
        for key, child in value.items():
            child_text = _inline_report_text(child)
            if child_text:
                lines.append(f"{indent}- **{_label_for_report_key(str(key))}**\uff1a{child_text}")
        return lines
    return [f"{indent}{value}"]


def _markdown_from_report(report: dict[str, Any]) -> str:
    title = _inline_report_text(report.get("summary") or report.get("overall_conclusion")) or "\u7206\u6b3e\u89c6\u9891\u65e5\u62a5"
    parts = [f"# {title}"]
    for key, label in REPORT_SECTION_LABELS.items():
        if key in {"summary", "overall_conclusion"}:
            continue
        value = report.get(key)
        if not value:
            continue
        lines = _markdown_value(value)
        if lines:
            parts.append(f"## {label}")
            parts.extend(lines)
    return "\n\n".join(parts)


REQUIRED_DAILY_REPORT_KEYS = {
    "summary",
    "common_patterns",
    "hook_analysis",
    "visual_patterns",
    "topic_angles",
    "execution_tactics",
    "reusable_ideas",
    "risks",
    "next_actions",
    "video_deep_dives",
}


def _extract_json_object_text(content: str) -> str:
    stripped = str(content or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def _escape_likely_inner_json_quotes(content: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(content):
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\" and in_string:
            out.append(char)
            escaped = True
            continue
        if in_string and char in {"\n", "\r", "\t"}:
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
            continue
        if char == '"':
            if not in_string:
                in_string = True
                out.append(char)
                continue
            next_index = index + 1
            while next_index < len(content) and content[next_index].isspace():
                next_index += 1
            next_char = content[next_index] if next_index < len(content) else ""
            if next_char in {":", ",", "}", "]", ""}:
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
            continue
        out.append(char)
    return "".join(out)


def _validate_daily_report_shape(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("Daily report JSON must be an object")
    missing = sorted(key for key in REQUIRED_DAILY_REPORT_KEYS if key not in report)
    if missing:
        raise ValueError(f"Daily report JSON is missing required keys: {', '.join(missing)}")
    if "video_deep_dives" in report and not isinstance(report.get("video_deep_dives"), list):
        raise ValueError("Daily report video_deep_dives must be an array when present")
    return report


def _repair_report_json_content(content: str) -> dict[str, Any]:
    candidate = _extract_json_object_text(content)
    try:
        return _validate_daily_report_shape(json.loads(candidate))
    except json.JSONDecodeError:
        repaired = _escape_likely_inner_json_quotes(candidate)
        return _validate_daily_report_shape(json.loads(repaired))


def _llm_repair_report_json_content(
    content: str,
    parse_error: Exception,
    api_key: str,
    api_url: str,
    model: str,
) -> dict[str, Any]:
    prompt = (
        "Repair the following malformed JSON so it becomes strict parseable JSON. "
        "Do not rewrite, summarize, translate, add facts, or remove substantive content. "
        "Only fix JSON syntax issues such as unescaped quotes, trailing commas, or code fences. "
        "Return JSON only with the original daily report keys.\n\n"
        f"Parse error: {parse_error}\n\n"
        "Malformed JSON:\n"
        f"{content}"
    )
    response = call_deepseek(
        api_key=api_key,
        prompt=prompt,
        api_url=api_url,
        model=model,
        max_tokens=_to_int(os.getenv("REPORT_JSON_REPAIR_MAX_TOKENS", "8192")),
    )
    repaired_content = extract_content(response)
    return _validate_daily_report_shape(parse_json_content(repaired_content))


def _parse_daily_report_content(
    content: str,
    api_key: str,
    api_url: str,
    model: str,
) -> dict[str, Any]:
    try:
        return _validate_daily_report_shape(parse_json_content(content))
    except Exception as parse_error:
        try:
            return _repair_report_json_content(content)
        except Exception as repair_error:
            try:
                return _llm_repair_report_json_content(content, parse_error, api_key, api_url, model)
            except Exception as llm_error:
                return {
                    "summary": "日报 JSON 解析失败，已保留原始模型输出供排查。",
                    "raw_result": content,
                    "parse_error": str(parse_error),
                    "repair_error": str(repair_error),
                    "llm_repair_error": str(llm_error),
                }


def _trim_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _trim_json_payload(value: Any, limit: int) -> Any:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "text": text[:limit].rstrip() + "..."}


def _nested_list(data: Any, names: tuple[str, ...]) -> list[Any]:
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
            found = _nested_list(value, names)
            if found:
                return found
    for value in data.values():
        found = _nested_list(value, names)
        if found:
            return found
    return []


def _compact_comments_payload(payload: dict[str, Any]) -> dict[str, Any]:
    items = _nested_list(payload, ("comments", "items", "data", "comment_list"))
    comments: list[dict[str, Any]] = []
    limit = max(0, _to_int(os.getenv("REPORT_VIDEO_COMMENT_COUNT", "20")))
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or item.get("author") or {}
        if not isinstance(user, dict):
            user = {}
        comments.append(
            {
                "text": _trim_text(item.get("text") or item.get("comment") or item.get("content"), 500),
                "like_count": _to_int(item.get("digg_count") or item.get("like_count") or item.get("likes")),
                "reply_count": _to_int(item.get("reply_comment_total") or item.get("reply_count")),
                "created_at": item.get("create_time") or item.get("created_at"),
                "user": user.get("unique_id") or user.get("nickname") or user.get("name"),
            }
        )
    return {"count": len(items), "sample_count": len(comments), "items": comments}


def _compact_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
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
        "signature": _trim_text(source.get("signature") or source.get("bio"), 500),
        "verified": source.get("verified"),
        "region": source.get("region"),
        "metrics": {
            "follower_count": _to_int(stats.get("follower_count") or stats.get("followerCount") or source.get("follower_count")),
            "following_count": _to_int(stats.get("following_count") or stats.get("followingCount") or source.get("following_count")),
            "heart_count": _to_int(stats.get("heart_count") or stats.get("heartCount") or source.get("heart_count")),
            "video_count": _to_int(stats.get("video_count") or stats.get("videoCount") or source.get("video_count")),
            "digg_count": _to_int(stats.get("digg_count") or stats.get("diggCount") or source.get("digg_count")),
        },
    }


def _creator_handle_from_video(video: dict[str, Any]) -> str:
    raw = video.get("raw") if isinstance(video.get("raw"), dict) else {}
    candidates = [
        _find_nested(raw, ("unique_id", "uniqueId", "sec_uid", "secUid")),
        video.get("author"),
    ]
    for value in candidates:
        handle = str(value or "").strip().lstrip("@")
        if handle and "/" not in handle and " " not in handle:
            return handle
    return ""


def _fetch_video_social_context(video: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE).rstrip("/")
    timeout = float(os.getenv("SOCIAVAULT_TIMEOUT", "180"))
    source_url = str(video.get("source_url") or "").strip()
    context: dict[str, Any] = {
        "video_url": source_url,
        "comments": {"status": "unavailable", "error": ""},
        "creator_profile": {"status": "unavailable", "error": ""},
    }
    if not api_key:
        context["comments"]["error"] = "Missing SOCIAVAULT_API_KEY"
        context["creator_profile"]["error"] = "Missing SOCIAVAULT_API_KEY"
        return context
    if source_url:
        try:
            payload = call_api(api_key, api_base, "comments", {"url": source_url}, timeout)
            context["comments"] = {"status": "ok", "data": _compact_comments_payload(payload)}
        except Exception as exc:
            context["comments"] = {"status": "failed", "error": str(exc)}
    else:
        context["comments"]["error"] = "missing source_url"
    handle = _creator_handle_from_video(video)
    if handle:
        try:
            payload = call_api(api_key, api_base, "profile", {"handle": handle}, timeout)
            context["creator_profile"] = {"status": "ok", "handle": handle, "data": _compact_profile_payload(payload)}
        except Exception as exc:
            context["creator_profile"] = {"status": "failed", "handle": handle, "error": str(exc)}
    else:
        context["creator_profile"]["error"] = "missing creator handle"
    return context


def _compact_extraction(analysis: Any) -> dict[str, Any]:
    if not isinstance(analysis, dict):
        return {"raw": _trim_text(analysis, 1600)}
    transcript = analysis.get("transcript") if isinstance(analysis.get("transcript"), dict) else {}
    timeline = analysis.get("timeline") if isinstance(analysis.get("timeline"), list) else []
    evidence = analysis.get("visual_evidence") if isinstance(analysis.get("visual_evidence"), list) else []
    frames = analysis.get("frame_analyses") if isinstance(analysis.get("frame_analyses"), list) else []
    return {
        "summary": _trim_text(analysis.get("summary") or analysis.get("video_description"), 900),
        "transcript": _trim_text(transcript.get("text") if isinstance(transcript, dict) else "", 1600),
        "timeline": timeline[:8],
        "visual_evidence": evidence[:8],
        "frame_analyses": [_trim_text(item, 700) for item in frames[:6]],
    }


def _compact_summary_video(video: dict[str, Any]) -> dict[str, Any]:
    insight = video.get("insight")
    valid_insight = insight if _is_valid_video_insight(insight) else {}
    return {
        "rank": video.get("report_rank"),
        "title": video.get("title"),
        "author": video.get("author"),
        "metrics": video.get("metrics"),
        "hot_score": video.get("hot_score"),
        "source_label": video.get("source_label"),
        "insight": valid_insight,
        "extraction_fallback": _compact_extraction(video.get("analysis")) if not valid_insight else {},
    }


def _video_insight_prompt(video: dict[str, Any], social_context: dict[str, Any]) -> str:
    analysis_limit = max(3000, _to_int(os.getenv("REPORT_VIDEO_INSIGHT_ANALYSIS_CHAR_LIMIT", "18000")))
    payload = {
        "video": {
            "rank": video.get("report_rank"),
            "platform": video.get("platform"),
            "video_id": video.get("video_id"),
            "title": video.get("title"),
            "author": video.get("author"),
            "source_url": video.get("source_url"),
            "source_label": video.get("source_label"),
            "metrics": video.get("metrics"),
            "hot_score": video.get("hot_score"),
            "raw_video_metadata": _trim_json_payload(video.get("raw") or {}, 6000),
        },
        "full_video_extraction": _trim_json_payload(video.get("analysis") or {}, analysis_limit),
        "social_context": social_context,
    }
    return (
        "你是资深短视频爆款拆解分析师。请基于输入中的完整单视频解析内容、评论区样本、博主信息和视频指标，"
        "为这一条视频生成深度中文拆解。\n"
        "要求：\n"
        "1. 只使用输入证据，不要编造未出现的信息；证据不足时明确写“证据不足”。\n"
        "2. 不要泛泛写“节奏快、情绪强”，必须说清楚具体画面、台词、评论或指标如何支撑判断。\n"
        "3. 输出要能被日报直接引用，重点解释为什么它可能成为爆款，以及如何复用。\n"
        "4. 只返回严格 JSON，不要 Markdown，不要代码块。\n"
        "JSON keys 必须包含：one_sentence, core_boom_reason, hook, content_structure, visual_language, "
        "audience_trigger, comment_signal, creator_context, engagement_driver, replicable_formula, "
        "adaptation_ideas, weakness_or_risk, evidence_quotes。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
    )


def _step1_skeleton_prompt(video: dict[str, Any]) -> str:
    analysis_payload = video.get("analysis") or {}
    return (
        "你是短视频结构化整理助手。请针对以下原始视频数据（字幕、视觉帧、评论、指标），"
        "提炼一份高度精炼、事实准确的内容骨架摘要。\n"
        "输出包含：1.视频核心内容与主题 2.关键台词与场景节点 3.评论区焦点与受众情绪。\n"
        "只返回严格 JSON，不要 Markdown。Keys: content_summary, scene_nodes, audience_feedback。\n\n"
        f"{json.dumps(analysis_payload, ensure_ascii=False, indent=2, default=str)}"
    )


def _generate_video_insight(video: dict[str, Any], social_context: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing required environment variable: DEEPSEEK_API_KEY")

    raw_analysis_str = json.dumps(video.get("analysis") or {}, ensure_ascii=False)
    step1_max_tokens = int(max(2500, min(8192, len(raw_analysis_str) * 0.38)))

    # Step 1: Low-thinking skeleton extraction
    step1_response = call_deepseek(
        api_key=api_key,
        prompt=_step1_skeleton_prompt(video),
        api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        max_tokens=step1_max_tokens,
        reasoning_effort="low",
    )
    step1_content = extract_content(step1_response)

    # Step 2: High-thinking deep insight analysis
    step2_video = dict(video)
    step2_video["analysis"] = step1_content
    step2_prompt = _video_insight_prompt(step2_video, social_context)

    step2_max_tokens = int(max(4800, min(8192, 5700 + len(step1_content) * 1.2)))

    try:
        response = call_deepseek(
            api_key=api_key,
            prompt=step2_prompt,
            api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
            model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
            max_tokens=step2_max_tokens,
            reasoning_effort="high",
        )
        content = extract_content(response)
    except ValueError as exc:
        if "truncated" not in str(exc):
            raise
        # Attempt 1: Additional 10% dynamic max_tokens allocation
        boosted_tokens = min(8192, int(step2_max_tokens * 1.15))
        try:
            response = call_deepseek(
                api_key=api_key,
                prompt=step2_prompt,
                api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
                model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
                max_tokens=boosted_tokens,
                reasoning_effort="high",
            )
            content = extract_content(response)
        except ValueError as exc2:
            if "truncated" not in str(exc2):
                raise
            # Attempt 2: Reasoning effort downgrade to disabled
            response = call_deepseek(
                api_key=api_key,
                prompt=step2_prompt,
                api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
                model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
                max_tokens=boosted_tokens,
                reasoning_effort="disabled",
            )
            content = extract_content(response)

    try:
        return parse_json_content(content)
    except Exception:
        return {"raw_result": content}


def _is_valid_video_insight(insight: Any) -> bool:
    if not isinstance(insight, dict) or not insight:
        return False
    if str(insight.get("error") or "").strip():
        return False
    failure_markers = (
        "generated failed",
        "generation failed",
        "生成失败",
        "拆解生成失败",
        "API错误",
        "API 错误",
        "Client Error",
        "Not Found",
    )
    text = json.dumps(insight, ensure_ascii=False, default=str)
    return not any(marker in text for marker in failure_markers)


def _ensure_video_insight(
    conn: sqlite3.Connection,
    report_date: str,
    platform: str,
    video_id: str,
    video: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = conn.execute(
        """
        SELECT social_context_json, insight_json
        FROM hot_report_videos
        WHERE report_date = ? AND platform = ? AND video_id = ?
        """,
        (report_date, platform, video_id),
    ).fetchone()
    social_context = _json_loads(row[0], None) if row else None
    insight = _json_loads(row[1], None) if row else None
    if _is_valid_video_insight(insight):
        return social_context or {}, insight
    if not isinstance(social_context, dict) or not social_context:
        social_context = _fetch_video_social_context(video)
    try:
        insight = _generate_video_insight(video, social_context)
    except Exception as exc:
        insight = {
            "error": str(exc),
            "one_sentence": video.get("title") or "",
            "core_boom_reason": "单视频拆解生成失败，日报将退回使用解析摘要。",
            "extraction_fallback": _compact_extraction(video.get("analysis")),
        }
    now = time.time()
    conn.execute(
        """
        UPDATE hot_report_videos
        SET social_context_json = ?, insight_json = ?, insight_generated_at = ?, updated_at = ?
        WHERE report_date = ? AND platform = ? AND video_id = ?
        """,
        (
            json.dumps(social_context, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(insight, ensure_ascii=False, sort_keys=True, default=str),
            now,
            now,
            report_date,
            platform,
            video_id,
        ),
    )
    conn.commit()
    return social_context, insight


def _summary_prompt(report_date: str, video_items: list[dict[str, Any]], partial_summaries: list[dict[str, Any]] | None = None) -> str:
    payload: dict[str, Any] = {"report_date": report_date}
    if partial_summaries:
        payload["partial_summaries"] = partial_summaries
    else:
        payload["video_insights"] = video_items
    return (
        "你是短视频研究总监。请根据输入的每日爆款视频拆解，撰写一份结构化每日热榜总结报告。\n"
        "要求：\n"
        "1. 只依赖所给证据，不得捏造未出现的数值或现象。\n"
        "2. key_observations 总结今日最核心的跨视频规律，重点看题材、受众反应、爆发机制。\n"
        "3. video_deep_dives 必须对每条视频给出准确核心洞察，不可漏项；视频拆解缺失时必须降级退回使用解析摘要。\n"
        "4. patterns/reusable_points/risks 必须给运营与剪辑团队可执行的提炼。\n"
        "5. 只返回严格 JSON，不要 Markdown，不要代码块。\n"
        "JSON keys 必须包含：summary, key_observations, video_deep_dives, patterns, reusable_points, risks。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _chunk_summary_prompt(report_date: str, chunk_index: int, video_items: list[dict[str, Any]]) -> str:
    payload = {"report_date": report_date, "chunk_index": chunk_index, "video_items": video_items}
    return (
        "你是短视频研究助理。请把这一组热视频提取内容压缩成可供最终日报使用的中文结构化摘要。"
        "只保留爆款原因、开头钩子、节奏/视觉、选题角度、互动机制、风险。"
        "只返回严格 JSON，不要 Markdown。JSON keys: key_observations, patterns, reusable_points, risks。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _summary_prompt_v2(report_date: str, video_items: list[dict[str, Any]], partial_summaries: list[dict[str, Any]] | None = None) -> str:
    payload: dict[str, Any] = {"report_date": report_date}
    if partial_summaries:
        payload["partial_summaries"] = partial_summaries
    else:
        payload["video_insights"] = video_items
    return (
        "你是短视频研究总监。请把输入的爆款视频拆解汇总成中文结构化日报。\n"
        "要求：\n"
        "1. 严格基于输入数据，禁止编造事实。\n"
        "2. 这是最终日报阶段，必须输出完整日报，不得输出 key_observations、patterns、reusable_points 等分组摘要字段。\n"
        "3. common_patterns、hook_analysis、visual_patterns、topic_angles、execution_tactics、reusable_ideas、risks、next_actions 各给出 4 至 6 条具体、可执行的中文要点。\n"
        "4. video_deep_dives 必须覆盖每条输入视频，按 rank 返回对象；每条至少说明核心爆点、开头钩子、内容结构、受众触发、互动机制、可复用公式和风险。不要在叙述中重算或猜测播放、点赞等数值。\n"
        "5. 只返回严格 JSON，不要 Markdown。JSON keys: summary, common_patterns, hook_analysis, visual_patterns, topic_angles, execution_tactics, reusable_ideas, risks, next_actions, video_deep_dives。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _compact_summary_video_for_retry(video: dict[str, Any]) -> dict[str, Any]:
    insight = video.get("insight") or {}
    compact_insight: dict[str, Any] = {}
    if isinstance(insight, dict):
        field_limits = {
            "one_sentence": 160,
            "core_boom_reason": 240,
            "hook": 180,
            "content_structure": 220,
            "audience_trigger": 180,
            "engagement_driver": 180,
            "replicable_formula": 200,
            "weakness_or_risk": 140,
        }
        for key, limit in field_limits.items():
            value = insight.get(key)
            if value not in (None, ""):
                compact_insight[key] = _trim_text(value, limit)
        if not compact_insight and insight.get("raw_result"):
            compact_insight["raw_result"] = _trim_text(insight.get("raw_result"), 700)
    elif insight:
        compact_insight["raw"] = _trim_text(insight, 700)
    fallback = video.get("extraction_fallback") if not compact_insight else {}
    return {
        "rank": video.get("rank"),
        "title": _trim_text(video.get("title"), 140),
        "metrics": video.get("metrics"),
        "hot_score": video.get("hot_score"),
        "source_label": video.get("source_label"),
        "insight": compact_insight,
        "extraction_fallback": _trim_json_payload(fallback, 1600) if fallback else {},
    }


def _extraction_fallback_deep_dive(video: dict[str, Any]) -> dict[str, Any]:
    analysis = _compact_extraction(video.get("analysis"))
    metrics = video.get("metrics") if isinstance(video.get("metrics"), dict) else {}
    return {
        "rank": _to_int(video.get("report_rank")),
        "title": video.get("title"),
        "author": video.get("author"),
        "one_sentence": video.get("title") or "标题缺失",
        "core_boom_reason": f"单视频拆解生成失败，退回解析摘要。原始摘要：{analysis.get('summary') or '无解析摘要'}",
        "hook": (
            f"台词片段：{_trim_text(analysis.get('transcript'), 200)} | "
            f"镜头节点：{_trim_text(json.dumps(analysis.get('timeline') or [], ensure_ascii=False), 200)}"
        ),
        "content_structure": f"核心描述：{_trim_text(analysis.get('summary'), 300)}",
        "visual_language": f"视觉画面：{_trim_text(json.dumps(analysis.get('visual_evidence') or [], ensure_ascii=False), 300)}",
        "audience_trigger": "退回解析摘要，评论与受众触发锚点无法提炼。",
        "engagement_driver": (
            f"分享 {_to_int(metrics.get('share_count'))}、评论 {_to_int(metrics.get('comment_count'))}、"
            f"点赞 {_to_int(metrics.get('like_count'))}。"
        ),
        "replicable_formula": "证据不足，不从失败的单条洞察中推导复用公式。",
        "risk": "本条使用当前视频解析的确定性回退，未采用汇总模型生成的画面描述。",
    }


def _merge_report_deep_dives(report: dict[str, Any], videos: list[dict[str, Any]]) -> None:
    deep_dives = report.get("video_deep_dives")
    if not isinstance(deep_dives, list):
        deep_dives = []
    by_rank = {
        _to_int(item.get("rank")): item
        for item in deep_dives
        if isinstance(item, dict) and _to_int(item.get("rank"))
    }
    merged: list[dict[str, Any]] = []
    for video in sorted(videos, key=lambda item: _to_int(item.get("report_rank"))):
        rank = _to_int(video.get("report_rank"))
        existing = by_rank.get(rank)
        if _is_valid_video_insight(video.get("insight")):
            detail = dict(existing) if isinstance(existing, dict) else dict(video["insight"])
            detail["rank"] = rank
            detail["title"] = video.get("title") or detail.get("title") or ""
        else:
            detail = _extraction_fallback_deep_dive(video)
        metrics = video.get("metrics") if isinstance(video.get("metrics"), dict) else {}
        detail["verified_metrics"] = {
            "play_count": _to_int(metrics.get("play_count")),
            "like_count": _to_int(metrics.get("like_count")),
            "comment_count": _to_int(metrics.get("comment_count")),
            "share_count": _to_int(metrics.get("share_count")),
            "favorite_count": _to_int(metrics.get("favorite_count")),
            "hot_score": _to_int(video.get("hot_score")),
        }
        merged.append(detail)
    report["video_deep_dives"] = merged


def _chunk_summary_prompt_v2(report_date: str, chunk_index: int, video_items: list[dict[str, Any]], compact: bool = False) -> str:
    payload_items = [_compact_summary_video_for_retry(video) for video in video_items] if compact else video_items
    payload = {"report_date": report_date, "chunk_index": chunk_index, "video_insights": payload_items}
    if compact:
        length_instruction = (
            "降级压缩模式：key_observations 不超过 3 条，"
            "video_deep_dives 每条不超过 100 个中文字，"
            "patterns/reusable_points/risks 各不超过 3 条。"
        )
    else:
        length_instruction = (
            "输出长度必须受控：key_observations 不超过 4 条，"
            "video_deep_dives 每条不超过 160 个中文字，"
            "patterns/reusable_points/risks 各不超过 4 条。"
        )
    return (
        "你是短视频研究助理。"
        "请把这一组单视频爆款拆解压缩成可供最终日报使用的中文结构化摘要。"
        "必须保留每条视频的核心爆点、开头钩子、结构、互动机制、可复用公式和风险。"
        f"{length_instruction}"
        "只返回严格 JSON，不要 Markdown。JSON keys: key_observations, video_deep_dives, patterns, reusable_points, risks。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _local_daily_summary(report_date: str, videos: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    """Build a complete, explicitly labelled fallback without inventing LLM findings."""
    deep_dives = [
        _extraction_fallback_deep_dive(video)
        if not _is_valid_video_insight(video.get("insight"))
        else {
            "rank": _to_int(video.get("report_rank")),
            "title": video.get("title") or "",
            **dict(video.get("insight") or {}),
        }
        for video in videos
    ]
    titles = "、".join(_trim_text(video.get("title"), 70) for video in videos if video.get("title")) or "已处理视频"
    return {
        "summary": f"{report_date} 爆款视频日报（本地降级汇总）：已整理 {len(videos)} 条有效视频。",
        "common_patterns": ["模型汇总未完成，本节只保留已验证视频身份、指标和单视频解析，不推断跨视频因果。"],
        "hook_analysis": ["请查看逐条爆点拆解中的标题、台词和首屏证据；本地降级不生成新的钩子结论。"],
        "visual_patterns": ["请查看逐条解析的视觉证据；本地降级不生成跨视频视觉归纳。"],
        "topic_angles": [f"本次有效视频标题：{titles}"],
        "execution_tactics": ["优先复核逐条已落盘证据，再决定可复用脚本与镜头策略。"],
        "reusable_ideas": ["本结果为确定性降级，不新增未经模型或人工验证的可复用结论。"],
        "risks": [f"DeepSeek 日报汇总已降级：{_trim_text(reason, 300)}"],
        "next_actions": ["外部服务恢复后可从 summarizing 步骤重新触发；有效单视频 Checkpoint 不会重算。"],
        "video_deep_dives": deep_dives,
        "generation": {"mode": "local_fallback", "reason": _trim_text(reason, 500)},
    }


def _finalize_daily_report(report: dict[str, Any], videos: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    validated = _validate_daily_report_shape(report)
    _merge_report_deep_dives(validated, videos)
    markdown = _markdown_from_report(validated)
    return _normalize_report_for_display(validated), markdown


def _generate_daily_summary(report_date: str, success_videos: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing required environment variable: DEEPSEEK_API_KEY")
    normalized_videos = []
    for index, video in enumerate(success_videos, start=1):
        normalized = dict(video)
        normalized["report_rank"] = _to_int(video.get("report_rank")) or index
        normalized_videos.append(normalized)
    video_items = [_compact_summary_video(video) for video in normalized_videos]

    # Unconditional fixed chunking (chunk_size = 4)
    chunk_size = max(2, _to_int(os.getenv("REPORT_SUMMARY_CHUNK_SIZE", "4")))
    partials: list[dict[str, Any]] = []
    for index in range(0, len(video_items), chunk_size):
        chunk_items = video_items[index : index + chunk_size]
        chunk_prompt = _chunk_summary_prompt_v2(report_date, index // chunk_size + 1, chunk_items)
        chunk_max_tokens = int(max(3500, min(8192, len(chunk_items) * 1100)))
        try:
            chunk_response = call_deepseek(
                api_key=api_key,
                prompt=chunk_prompt,
                api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
                model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
                max_tokens=chunk_max_tokens,
                reasoning_effort="low",
            )
            chunk_content = extract_content(chunk_response)
        except ValueError as exc:
            if "truncated" not in str(exc):
                raise
            retry_prompt = _chunk_summary_prompt_v2(report_date, index // chunk_size + 1, chunk_items, compact=True)
            boosted_chunk_tokens = min(8192, int(chunk_max_tokens * 1.15))
            try:
                retry_response = call_deepseek(
                    api_key=api_key,
                    prompt=retry_prompt,
                    api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
                    model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
                    max_tokens=boosted_chunk_tokens,
                    reasoning_effort="low",
                )
                chunk_content = extract_content(retry_response)
            except ValueError as exc2:
                if "truncated" not in str(exc2):
                    raise
                try:
                    retry_response = call_deepseek(
                        api_key=api_key,
                        prompt=retry_prompt,
                        api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
                        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
                        max_tokens=boosted_chunk_tokens,
                        reasoning_effort="disabled",
                    )
                    chunk_content = extract_content(retry_response)
                except ValueError as exc3:
                    if "truncated" not in str(exc3):
                        raise
                    partials.append({"local_fallback": True, "reason": str(exc3), "videos": chunk_items})
                    continue
        try:
            partials.append(parse_json_content(chunk_content))
        except Exception:
            partials.append({"raw_result": chunk_content})

    chunk_fallback = next((item for item in partials if item.get("local_fallback")), None)
    if chunk_fallback:
        return _finalize_daily_report(
            _local_daily_summary(report_date, normalized_videos, str(chunk_fallback.get("reason") or "chunk summary truncated")),
            normalized_videos,
        )

    prompt = _summary_prompt_v2(report_date, [], partial_summaries=partials)
    final_max_tokens = int(max(4500, min(8192, len(video_items) * 900)))
    try:
        response = call_deepseek(
            api_key=api_key,
            prompt=prompt,
            api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
            model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
            max_tokens=final_max_tokens,
            reasoning_effort="low",
        )
        content = extract_content(response)
    except ValueError as exc:
        if "truncated" not in str(exc):
            raise
        boosted_final_tokens = min(8192, int(final_max_tokens * 1.15))
        try:
            response = call_deepseek(
                api_key=api_key,
                prompt=prompt,
                api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
                model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
                max_tokens=boosted_final_tokens,
                reasoning_effort="low",
            )
            content = extract_content(response)
        except ValueError as exc2:
            if "truncated" not in str(exc2):
                raise
            try:
                response = call_deepseek(
                    api_key=api_key,
                    prompt=prompt,
                    api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
                    model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
                    max_tokens=boosted_final_tokens,
                    reasoning_effort="disabled",
                )
                content = extract_content(response)
            except ValueError as exc3:
                if "truncated" not in str(exc3):
                    raise
                return _finalize_daily_report(_local_daily_summary(report_date, normalized_videos, str(exc3)), normalized_videos)

    try:
        report = _parse_daily_report_content(
            content=content,
            api_key=api_key,
            api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
            model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        )
        return _finalize_daily_report(report, normalized_videos)
    except Exception as exc:
        if _is_recoverable_external_error(exc):
            raise
        return _finalize_daily_report(_local_daily_summary(report_date, normalized_videos, str(exc)), normalized_videos)


def _cached_deep_dive_for_video(video: dict[str, Any]) -> dict[str, Any]:
    metrics = video.get("metrics") if isinstance(video.get("metrics"), dict) else {}
    play_count = _to_int(metrics.get("play_count"))
    like_count = _to_int(metrics.get("like_count"))
    comment_count = _to_int(metrics.get("comment_count"))
    share_count = _to_int(metrics.get("share_count"))
    stats = []
    if play_count:
        stats.append(f"播放 {play_count}")
    if like_count:
        stats.append(f"点赞 {like_count}")
    if comment_count:
        stats.append(f"评论 {comment_count}")
    if share_count:
        stats.append(f"分享 {share_count}")
    metric_text = "，".join(stats) if stats else "指标暂缺"
    title = str(video.get("title") or "").strip()
    source_label = str(video.get("source_label") or "").strip()
    return {
        "rank": _to_int(video.get("report_rank")) or 0,
        "title": title,
        "boom_reason": f"元数据已按 video_id 绑定：{metric_text}；热度 {video.get('hot_score') or 0}。",
        "hook": f"标题/入口：{_trim_text(title, 180)}",
        "structure": "当前展示元数据视图：视频身份、标题、来源、互动指标和原视频链接已对齐。",
        "audience_trigger": f"来源：{source_label or '未知来源'}；互动强弱以播放、点赞、评论、分享等已入库指标为准。",
        "engagement_driver": f"分享 {share_count}、评论 {comment_count}、点赞 {like_count}。",
        "replicable_formula": "先按正确 video_id 和标题完成选题归类，再在需要时补充重新解析后的画面结构。",
        "risk": "脏的单条 analysis/insight 已从展示重建中剔除，不再引用旧画面描述。",
    }


def _cached_report_from_videos(current_report: dict[str, Any], videos: list[dict[str, Any]]) -> dict[str, Any]:
    rebuilt = dict(current_report) if isinstance(current_report, dict) else {}
    count = len(videos)
    rebuilt["summary"] = (
        f"本日报已按 video_id、标题、来源和互动指标重新绑定 {count} 条视频；"
        "当前为元数据视图，未重新调用 LLM，也未引用旧的脏画面解析。"
    )
    rebuilt["common_patterns"] = [
        "逐条展示已按 hot_report_videos.report_rank 与 video_id 重新生成，避免按数组顺序误挂链接。",
        "本次重建使用确定绑定字段：video_id、title、source_url、metrics、hot_score。",
        "旧的画面解析和单条 insight 不参与当前展示，防止继续传播错配画面描述。",
    ]
    rebuilt["hook_analysis"] = ["当前按标题、标签和原视频入口展示，不复用旧画面钩子。"]
    rebuilt["visual_patterns"] = ["旧画面解析未被引用；当前只展示元数据，不展示疑似错配的时间轴画面描述。"]
    rebuilt["topic_angles"] = ["按当前视频标题、标签和来源重新对齐，避免把其他视频题材挂到当前视频。"]
    rebuilt["execution_tactics"] = ["后续日报生成将保留原始 report_rank，不再把成功视频列表重新从 1 编号。"]
    rebuilt["reusable_ideas"] = ["先确保 video_id、标题、指标三者一致，再沉淀可复用脚本公式。"]
    rebuilt["risks"] = ["旧 insight/analysis 已视为脏数据处理；当前展示以刷新后的元数据为准。"]
    rebuilt["next_actions"] = ["如后续需要画面级时间轴，再对对应视频重新执行视频解析并刷新翻译缓存。"]
    return rebuilt


def refresh_report_metadata(report_date: str | None = None) -> dict[str, Any]:
    """Refresh report video metadata and clear dirty analysis/insight caches without video LLM work."""
    date = report_date or today_key()
    api_key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing required environment variable: SOCIAVAULT_API_KEY")
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE).rstrip("/")
    api_timeout = float(os.getenv("SOCIAVAULT_TIMEOUT", "180"))
    with closing(_connect()) as conn:
        row = conn.execute("SELECT id FROM daily_reports WHERE report_date = ?", (date,)).fetchone()
        if not row:
            raise ValueError(f"report not found for {date}")
        report_id = row[0]
        rows = conn.execute(
            """
            SELECT rv.platform, rv.video_id, rv.report_rank, rv.source_endpoint, rv.source_label,
                   rv.source_rank, COALESCE(m.source_url, rv.raw_json), rv.raw_json
            FROM hot_report_videos rv
            JOIN hot_video_master m ON m.platform = rv.platform AND m.video_id = rv.video_id
            WHERE rv.report_date = ?
            ORDER BY rv.report_rank ASC
            """,
            (date,),
        ).fetchall()
        refreshed = 0
        failed: list[dict[str, str]] = []
        for platform, video_id, report_rank, source_endpoint, source_label, source_rank, source_url, raw_json in rows:
            if platform != "tiktok" or not video_id:
                continue
            url = str(source_url or "").strip()
            if not url.startswith("http"):
                raw = _json_loads(raw_json, {})
                url = _source_url(raw) if isinstance(raw, dict) else ""
            if not url:
                url = f"https://www.tiktok.com/@unknown/video/{video_id}"
            try:
                payload = call_api(api_key, api_base, "video-info", {"url": url}, api_timeout, cache_policy="record_only")
                node = _extract_video_info_node(payload)
                if not node:
                    raise RuntimeError("video-info returned no video node")
                item = _normalize_video(node, source_endpoint or "video-info", source_label or "metadata-refresh", _to_int(source_rank) or _to_int(report_rank) or 1)
                if item.get("video_id") and item["video_id"] != video_id:
                    raise RuntimeError(f"video-info id mismatch: {item['video_id']} != {video_id}")
                item["video_id"] = video_id
                _upsert_video(conn, report_id, date, item, _to_int(report_rank) or refreshed + 1)
                refreshed += 1
            except Exception as exc:
                failed.append({"video_id": str(video_id), "error": str(exc)})
        now = time.time()
        conn.execute(
            """
            UPDATE hot_report_videos
            SET analysis_json = NULL, analysis_zh_json = NULL, audit_json = NULL,
                analysis_sha256 = NULL, analysis_zh_source_sha256 = NULL,
                social_context_json = NULL, insight_json = NULL, insight_generated_at = NULL,
                updated_at = ?
            WHERE report_date = ?
            """,
            (now, date),
        )
        conn.commit()
    rebuilt = rebuild_report_from_cached(date)
    rebuilt["metadata_refreshed_count"] = refreshed
    rebuilt["metadata_failed"] = failed
    return rebuilt


def rebuild_report_from_cached(report_date: str | None = None) -> dict[str, Any]:
    """Rebuild report display fields from cached per-video records without LLM calls."""
    date = report_date or today_key()
    with closing(_connect()) as conn:
        report_row = conn.execute(
            "SELECT report_json FROM daily_reports WHERE report_date = ?",
            (date,),
        ).fetchone()
        if not report_row:
            raise ValueError(f"report not found for {date}")
        current_report = _json_loads(report_row[0], {}) or {}
        videos = _load_success_videos(conn, date)
        if not videos:
            raise ValueError(f"no complete report videos for {date}")
        sorted_videos = sorted(videos, key=lambda item: _to_int(item.get("report_rank")) or 999999)
        rebuilt = _cached_report_from_videos(current_report, sorted_videos)
        rebuilt["video_deep_dives"] = [
            _cached_deep_dive_for_video(video)
            for video in sorted_videos
        ]
        markdown = _markdown_from_report(rebuilt)
        normalized = _normalize_report_for_display(rebuilt)
        now = time.time()
        conn.execute(
            """
            UPDATE daily_reports
            SET report_json = ?, report_markdown = ?, video_count = ?,
                analysis_success_count = ?, updated_at = ?
            WHERE report_date = ?
            """,
            (
                json.dumps(rebuilt, ensure_ascii=False, sort_keys=True, default=str),
                markdown,
                len(videos),
                len(videos),
                now,
                date,
            ),
        )
        conn.commit()
    payload = get_report(date, include_raw=True)
    payload["rebuild_source"] = "cached"
    payload["rebuilt_video_count"] = len(videos)
    payload["rebuilt_report"] = normalized
    return payload


def _load_report_videos(conn: sqlite3.Connection, report_date: str, *, include_raw: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.platform, m.video_id, m.title, m.author, m.source_url, COALESCE(rv.cover_url, m.cover_url),
               rv.local_filename, rv.extraction_dir, rv.source_endpoint, rv.source_label,
               rv.source_rank, rv.report_rank, rv.hot_score, rv.metrics_json, rv.raw_json,
               rv.process_status, rv.process_error, rv.analysis_json, rv.analysis_zh_json, rv.audit_json,
               rv.social_context_json, rv.insight_json, rv.insight_generated_at,
               rv.created_at, rv.updated_at
        FROM hot_report_videos rv
        JOIN hot_video_master m ON m.platform = rv.platform AND m.video_id = rv.video_id
        WHERE rv.report_date = ?
        ORDER BY rv.report_rank ASC
        """,
        (report_date,),
    ).fetchall()
    return [_row_to_video(row, include_raw=include_raw) for row in rows]


def _load_success_videos(conn: sqlite3.Connection, report_date: str) -> list[dict[str, Any]]:
    return [
        video
        for video in _load_report_videos(conn, report_date, include_raw=True)
        if video.get("process_status") == "complete" and _is_video_checkpoint_valid(video)[0]
    ]


def refresh_report_hot_scores(report_date: str) -> int:
    """Recalculate persisted report scores with the active raw-score formula."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT platform, video_id, report_rank, metrics_json
            FROM hot_report_videos
            WHERE report_date = ?
            ORDER BY report_rank ASC
            """,
            (report_date,),
        ).fetchall()
        if not rows:
            raise ValueError(f"report has no video rows for {report_date}")
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        for platform, video_id, rank, metrics_json in rows:
            metrics = _json_loads(metrics_json, {})
            if not isinstance(metrics, dict):
                raise ValueError(f"invalid metrics JSON for {platform}/{video_id}")
            hot_score = _score_hot_video(metrics, _to_int(rank))
            conn.execute(
                """
                UPDATE hot_report_videos
                SET hot_score = ?, updated_at = ?
                WHERE report_date = ? AND platform = ? AND video_id = ?
                """,
                (hot_score, now, report_date, platform, video_id),
            )
            conn.execute(
                """
                UPDATE hot_video_master
                SET latest_hot_score = (
                        SELECT rv.hot_score
                        FROM hot_report_videos rv
                        WHERE rv.platform = hot_video_master.platform
                          AND rv.video_id = hot_video_master.video_id
                        ORDER BY rv.report_date DESC, rv.updated_at DESC
                        LIMIT 1
                    ),
                    max_hot_score = (
                        SELECT MAX(rv.hot_score)
                        FROM hot_report_videos rv
                        WHERE rv.platform = hot_video_master.platform
                          AND rv.video_id = hot_video_master.video_id
                    ),
                    updated_at = ?
                WHERE platform = ? AND video_id = ?
                """,
                (now, platform, video_id),
            )
        conn.commit()
    return len(rows)


def regenerate_daily_report_summary(report_date: str) -> dict[str, Any]:
    """Regenerate only the final daily summary from completed video checkpoints."""
    refresh_report_hot_scores(report_date)
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id FROM daily_reports WHERE report_date = ?", (report_date,)
        ).fetchone()
        if not row:
            raise ValueError(f"report not found for {report_date}")
        report_id = str(row[0])
        success_videos = _load_success_videos(conn, report_date)
    if not success_videos:
        raise ValueError(f"report has no completed video checkpoints for {report_date}")
    report_json, report_markdown = _generate_daily_summary(report_date, success_videos)
    with closing(_connect()) as conn:
        _finish_report(
            conn,
            report_id=report_id,
            report_date=report_date,
            status="complete",
            report_json=report_json,
            report_markdown=report_markdown,
            resume_step="complete",
        )
    return get_report(report_date)


def _cleanup_old_reports(conn: sqlite3.Connection) -> None:
    settings = get_settings()
    tz = ZoneInfo(settings["timezone"])
    cutoff = (datetime.now(tz) - timedelta(days=int(settings["retention_days"]) - 1)).strftime("%Y-%m-%d")
    existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "hot_videos" in existing_tables:
        conn.execute("DELETE FROM hot_videos WHERE report_date < ?", (cutoff,))
    conn.execute("DELETE FROM hot_report_videos WHERE report_date < ?", (cutoff,))
    conn.execute("DELETE FROM daily_reports WHERE report_date < ?", (cutoff,))
    conn.commit()


def delete_report(report_date: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date or ""):
        raise ValueError("report_date must be YYYY-MM-DD")
    with closing(_connect()) as conn:
        row = conn.execute("SELECT id FROM daily_reports WHERE report_date = ?", (report_date,)).fetchone()
        conn.execute("DELETE FROM hot_report_videos WHERE report_date = ?", (report_date,))
        conn.execute("DELETE FROM daily_reports WHERE report_date = ?", (report_date,))
        conn.commit()
    with _progress_lock:
        _progress_by_date.pop(report_date, None)
    return {"deleted": bool(row), "report_date": report_date}


def recover_interrupted_reports() -> dict[str, Any]:
    recovered: list[str] = []
    with _active_job_lock:
        active_date = _active_job
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, report_date, worker_lease, heartbeat_at FROM daily_reports WHERE status = 'running'"
        ).fetchall()
        for report_id, report_date, worker_lease, heartbeat_at in rows:
            if str(report_date) == active_date:
                continue
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
            status = "partial_failed"
            heartbeat_note = f"last heartbeat={heartbeat_at}" if heartbeat_at else "no heartbeat"
            if success_count:
                error = f"Report worker lease {worker_lease or 'missing'} was orphaned ({heartbeat_note}) before daily summary. Click generate to resume."
            elif failed_count:
                error = f"Report worker lease {worker_lease or 'missing'} was orphaned ({heartbeat_note}) after processing failures. Click generate to resume."
            else:
                error = f"Report worker lease {worker_lease or 'missing'} was orphaned ({heartbeat_note}) before processing started. Click generate to resume."
            _finish_report(conn, str(report_id), str(report_date), status, error)
            recovered.append(str(report_date))
    return {"recovered": recovered}


def _heartbeat_report(conn: sqlite3.Connection, report_id: str, report_date: str, worker_lease: str, step: str) -> None:
    cursor = conn.execute(
        """
        UPDATE daily_reports
        SET heartbeat_at = ?, resume_step = ?, updated_at = ?
        WHERE id = ? AND report_date = ? AND status = 'running' AND worker_lease = ?
        """,
        (time.time(), step, time.time(), report_id, report_date, worker_lease),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError(f"report worker lease lost for {report_date}")
    conn.commit()


def run_report(report_date: str | None = None, scheduled: bool = False) -> dict[str, Any]:
    settings = get_settings()
    date = report_date or today_key()
    region = os.getenv("SOCIAVAULT_REGION", "US").strip() or "US"
    analysis_limit = int(settings["analysis_limit"])
    topic_keywords = list(settings.get("topic_keywords") or _split_csv_env("HOT_VIDEO_KEYWORDS", "AI toys"))
    target_count = analysis_limit
    backup_count = _report_backup_count()
    candidate_target_count = target_count + backup_count
    recency_days = _recent_window_days()
    api_key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE).rstrip("/")
    sources = [
        {"endpoint": endpoint, "label": label, "params": params}
        for endpoint, params, label in _source_requests(region, candidate_target_count, topic_keywords)
    ]
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
        "topic_fallback_sources": 0,
        "target_count": target_count,
        "analyzed_success": 0,
        "analyzed_failed": 0,
        "skipped_retry_backoff": 0,
        "skipped_retry_exhausted": 0,
    }
    existing_report = get_report(date, include_raw=False, detail=False)
    if existing_report.get("status") == "complete":
        return existing_report
    worker_lease = uuid.uuid4().hex
    report_id: str | None = None

    with _active_job_lock:
        global _active_job
        _active_job = date
    _progress_payload(date, "running", "collecting", 3, "开始采集热点视频", counts)
    try:
        with closing(_connect()) as conn:
            _cleanup_old_reports(conn)
            _cleanup_expired_video_records(conn, recency_days)
            report_id = _start_report(conn, date, region, sources, scheduled=scheduled, worker_lease=worker_lease)
            _heartbeat_report(conn, report_id, date, worker_lease, "collecting")
            excluded_keys = _existing_report_video_keys(conn, date)
            try:
                api_timeout = float(os.getenv("SOCIAVAULT_TIMEOUT", "180"))
                ranked = _load_report_videos(conn, date, include_raw=True)
                valid_existing = [video for video in ranked if video.get("process_status") == "complete" and _is_video_checkpoint_valid(video)[0]]
                counts["analyzed_success"] = len(valid_existing)
                if ranked:
                    counts["candidate_count"] = len(ranked)
                    _progress_payload(date, "running", "resuming", 30, f"自动识别到断点：找到已有 {len(ranked)} 条视频记录，已验证完成 {len(valid_existing)} 条", counts)
                if len(valid_existing) < analysis_limit:
                    if not api_key:
                        error = "Missing required environment variable: SOCIAVAULT_API_KEY"
                        _finish_report(conn, report_id, date, "failed", error)
                        _progress_payload(date, "failed", "finished", 100, error, counts)
                        return get_report(date, include_raw=True)
                    candidates, source_errors = _collect_hot_video_candidates(
                        date,
                        region,
                        candidate_target_count,
                        recency_days,
                        topic_keywords,
                        api_key,
                        api_base,
                        api_timeout,
                        counts,
                        excluded_keys,
                    )
                    collected_ranked = _rank_with_topic_guarantees(list(candidates.values()), topic_keywords, candidate_target_count)
                    existing_keys = {(str(item.get("platform")), str(item.get("video_id"))) for item in ranked}
                    ranked.extend(item for item in collected_ranked if (str(item.get("platform")), str(item.get("video_id"))) not in existing_keys)
                    counts["candidate_count"] = len(ranked)
                    counts["topic_guaranteed_count"] = sum(1 for item in ranked[:target_count] if item.get("selection_bucket") == "topic")
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
                ranked = _prioritize_retry_candidates(conn, date, ranked)
                if not ranked:
                    error = f"No resumable or newly collected hot videos for {date}"
                    _finish_report(conn, report_id, date, "failed", error)
                    _progress_payload(date, "failed", "finished", 100, error, counts)
                    return get_report(date, include_raw=True)
                _heartbeat_report(conn, report_id, date, worker_lease, "processing")
                _progress_payload(date, "running", "downloading", 30, f"开始处理候选视频，目标成功 {analysis_limit} 条", counts)

                # 无总限时:处理完整个候选池(primary + backup),成功数达标则提前完成。
                # ranked includes an over-fetched candidate pool for fallback. Progress
                # must describe the processed pool size, not only the target.
                total_to_process = max(1, len(ranked))
                for index, item in enumerate(ranked, start=1):
                    if counts["analyzed_success"] >= analysis_limit:
                        break
                    item["report_rank"] = _to_int(item.get("report_rank")) or index
                    checkpoint_valid, checkpoint_reason = _is_video_checkpoint_valid(item)
                    if item.get("process_status") == "complete" and checkpoint_valid:
                        continue
                    if item.get("process_status") == "complete":
                        _mark_video_pending(conn, date, item["platform"], item["video_id"], checkpoint_reason)
                    retry_state = _failed_video_retry_state(conn, date, item["platform"], item["video_id"])
                    if retry_state:
                        counts[f"skipped_{retry_state}"] += 1
                        continue
                    _upsert_video(conn, report_id, date, item, item["report_rank"])
                    conn.commit()
                    _heartbeat_report(conn, report_id, date, worker_lease, "processing")
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
                        stage = "extracting"
                        message = f"分析视频 {index}/{total_to_process}"
                    _progress_payload(date, "running", stage, 30 + int(index / total_to_process * 50), message, counts)
                    _process_video(conn, date, item)
                    # 同轮立即重试:失败且未达 REPORT_VIDEO_MAX_ATTEMPTS 时马上再试,
                    # 重试仍失败则放弃该视频,由后续备份候选顶上。
                    max_attempts = max(1, _to_int(os.getenv("REPORT_VIDEO_MAX_ATTEMPTS", "2")) or 2)
                    for _ in range(max(0, max_attempts - 1)):
                        retry_row = conn.execute(
                            "SELECT process_status FROM hot_report_videos WHERE report_date = ? AND platform = ? AND video_id = ?",
                            (date, item["platform"], item["video_id"]),
                        ).fetchone()
                        if retry_row and retry_row[0] == "complete":
                            break
                        if _failed_video_retry_state(conn, date, item["platform"], item["video_id"]):
                            break
                        _process_video(conn, date, item)
                    row = conn.execute(
                        "SELECT process_status FROM hot_report_videos WHERE report_date = ? AND platform = ? AND video_id = ?",
                        (date, item["platform"], item["video_id"]),
                    ).fetchone()
                    current = next(
                        (video for video in _load_report_videos(conn, date, include_raw=True)
                         if video.get("platform") == item["platform"] and video.get("video_id") == item["video_id"]),
                        None,
                    )
                    if row and row[0] == "complete" and current and _is_video_checkpoint_valid(current)[0]:
                        counts["analyzed_success"] += 1
                    else:
                        counts["analyzed_failed"] += 1

                success_videos = _load_success_videos(conn, date)
                if success_videos:
                    _progress_payload(date, "running", "summarizing", 88, "开始生成爆款日报", counts)
                    _heartbeat_report(conn, report_id, date, worker_lease, "summarizing")
                    try:
                        report_json, markdown = _generate_daily_summary(date, success_videos[:analysis_limit])
                    except Exception as exc:
                        status = "paused_external" if _is_recoverable_external_error(exc) else "failed"
                        _finish_report(conn, report_id, date, status, str(exc), resume_step="summarizing")
                        _progress_payload(date, status, "finished", 100, str(exc), counts)
                        return get_report(date, include_raw=True)
                    _finish_report(conn, report_id, date, "complete", report_json=report_json, report_markdown=markdown)
                    _progress_payload(date, "complete", "finished", 100, "日报生成完成", counts)
                else:
                    error = "No videos analyzed successfully"
                    _finish_report(conn, report_id, date, "failed", error)
                    _progress_payload(date, "failed", "finished", 100, error, counts)
            except Exception as exc:
                status = "paused_external" if _is_recoverable_external_error(exc) else "failed"
                if report_id:
                    _finish_report(conn, report_id, date, status, str(exc), resume_step="processing")
                _progress_payload(date, status, "finished", 100, str(exc), counts)
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


def enqueue_missed_today_report() -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz)
    schedule_hour, schedule_minute = [int(part) for part in str(settings["schedule_time"]).split(":", 1)]
    scheduled_at = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
    date = now.strftime("%Y-%m-%d")
    if now < scheduled_at:
        return {"queued": False, "report_date": date, "reason": "schedule_not_reached"}
    status = get_report_runtime_status()
    if status.get("active_date") == date or date in set(status.get("queued", [])):
        return {"queued": False, "report_date": date, "reason": "already_running_or_queued"}
    report = get_report(date, include_raw=False, detail=False)
    if report.get("exists"):
        return {"queued": False, "report_date": date, "reason": f"report_{report.get('status') or 'exists'}"}
    payload = enqueue_report(date)
    payload["reason"] = "missed_schedule"
    return payload


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


def start_report_scheduler(enable_timer: bool = True) -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
        recover_interrupted_reports()
        threading.Thread(target=_scheduler_worker, daemon=True).start()
        if enable_timer:
            try:
                missed = enqueue_missed_today_report()
                if missed.get("queued"):
                    print(f"Queued missed hot report for {missed.get('report_date')}", flush=True)
            except Exception as exc:
                print(f"Hot report missed-schedule recovery failed: {exc}", flush=True)
            threading.Thread(target=_scheduler_loop, daemon=True).start()
