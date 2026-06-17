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
DEFAULT_API_BASE = "https://api.sociavault.com"
DEFAULT_TZ = "Asia/Shanghai"

_scheduler_started = False
_scheduler_lock = threading.Lock()
_job_queue: queue.Queue[str] = queue.Queue()
_active_job_lock = threading.Lock()
_active_job: str | None = None


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
        rows = conn.execute(
            """
            SELECT report_id, report_date, platform, video_id, source_endpoint, source_label,
                   source_rank, hot_score, metrics_json, raw_json, created_at, updated_at
            FROM hot_videos
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO hot_report_videos (
                    report_id, report_date, platform, video_id, source_endpoint, source_label,
                    source_rank, report_rank, hot_score, metrics_json, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*row[:7], int(row[6] or 0), *row[7:]),
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
        "dynamic_cover",
        "play_addr",
    )
    found = _find_nested(node, names)
    if isinstance(found, str) and found.startswith(("http://", "https://")):
        return found
    if isinstance(found, dict):
        url = _find_nested(found, ("url", "uri", "download_url", "display_url"))
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
        urls = _find_nested(found, ("url_list", "urlList"))
        if isinstance(urls, list):
            for item in urls:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    return item
    if isinstance(found, list):
        for item in found:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
    return ""


def _looks_like_video(node: dict[str, Any]) -> bool:
    video_id = _first_present(node, ("aweme_id", "awemeId", "video_id", "videoId", "item_id", "itemId", "id"))
    has_text = _first_present(node, ("desc", "description", "title", "caption")) not in (None, "", [], {})
    has_stats = any(
        _metric(node, names) > 0
        for names in (
            ("play_count", "playCount", "view_count", "viewCount"),
            ("digg_count", "diggCount", "like_count", "likeCount"),
            ("comment_count", "commentCount"),
        )
    )
    return bool(video_id and (has_text or has_stats))


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
    hot_score = (
        metrics["play_count"]
        + metrics["like_count"] * 8
        + metrics["comment_count"] * 15
        + metrics["share_count"] * 20
        + metrics["favorite_count"] * 10
        + max(0, 100 - rank) * 1_000
    )
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


def get_report(report_date: str | None = None, include_raw: bool = False, detail: bool = True) -> dict[str, Any]:
    date = report_date or today_key()
    with _connect() as conn:
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
    report["videos"] = [_row_to_video(row, include_raw=include_raw) for row in rows] if detail else []
    return report


def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    settings = get_settings()
    retention_days = int(settings["retention_days"])
    cutoff = (datetime.now(ZoneInfo(settings["timezone"])) - timedelta(days=retention_days - 1)).strftime("%Y-%m-%d")
    with _connect() as conn:
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


def _source_requests(region: str, count: int) -> list[tuple[str, dict[str, Any], str]]:
    keywords = ["viral"]
    requests = [("trending", {"region": region, "count": count, "trim": "true"}, f"trending:{region}")]
    for keyword in keywords:
        requests.append(("search-top", {"query": keyword, "region": region, "count": count, "trim": "true"}, f"search-top:{keyword}"))
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
    if audit_path.is_file():
        return _json_loads(audit_path.read_text(encoding="utf-8"), {})
    cmd = [sys.executable, str(SCRIPTS_DIR / "deepseek_postprocess.py"), str(output_dir)]
    subprocess.run(cmd, cwd=ROOT, check=True, env=os.environ.copy())
    return _json_loads(audit_path.read_text(encoding="utf-8") if audit_path.is_file() else "", {})


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
        conn.execute(
            """
            UPDATE hot_video_master
            SET local_filename = COALESCE(NULLIF(?, ''), local_filename),
                extraction_dir = COALESCE(NULLIF(?, ''), extraction_dir),
                updated_at = ?
            WHERE platform = ? AND video_id = ?
            """,
            (filename, extraction_dir, now, platform, video_id),
        )
        conn.execute(
            """
            UPDATE hot_report_videos
            SET process_status = 'complete', process_error = NULL, local_filename = ?, extraction_dir = ?,
                analysis_json = ?, audit_json = ?, updated_at = ?
            WHERE report_date = ? AND platform = ? AND video_id = ?
            """,
            (
                filename,
                extraction_dir,
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
    response = call_deepseek(
        api_key=api_key,
        prompt=prompt,
        api_url=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
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


def run_report(report_date: str | None = None, scheduled: bool = False) -> dict[str, Any]:
    settings = get_settings()
    date = report_date or today_key()
    region = os.getenv("SOCIAVAULT_REGION", "US").strip() or "US"
    count = 30
    detail_limit = 20
    analysis_limit = int(settings["analysis_limit"])
    api_key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE).rstrip("/")
    sources = [{"endpoint": endpoint, "label": label, "params": params} for endpoint, params, label in _source_requests(region, count)]

    with _active_job_lock:
        global _active_job
        _active_job = date
    try:
        with _connect() as conn:
            _cleanup_old_reports(conn)
            report_id = _start_report(conn, date, region, sources, scheduled=scheduled)
            if not api_key:
                error = "Missing required environment variable: SOCIAVAULT_API_KEY"
                _finish_report(conn, report_id, date, "failed", error)
                return get_report(date, include_raw=True)
            try:
                candidates: dict[tuple[str, str], dict[str, Any]] = {}
                for endpoint, params, label in _source_requests(region, count):
                    payload = call_api(api_key, api_base, endpoint, params, float(os.getenv("SOCIAVAULT_TIMEOUT", "180")))
                    for rank, node in enumerate(_iter_video_nodes(payload), start=1):
                        item = _normalize_video(node, endpoint, label, rank)
                        if not item["video_id"]:
                            continue
                        key = (item["platform"], item["video_id"])
                        if key not in candidates or item["hot_score"] > candidates[key]["hot_score"]:
                            candidates[key] = item
                ranked = sorted(candidates.values(), key=lambda item: item["hot_score"], reverse=True)[:detail_limit]
                for report_rank, item in enumerate(ranked, start=1):
                    _upsert_video(conn, report_id, date, item, report_rank)
                conn.commit()
                for item in ranked[:analysis_limit]:
                    _process_video(conn, date, item)
                success_videos = _load_success_videos(conn, date)
                if len(success_videos) >= 3:
                    report_json, markdown = _generate_daily_summary(date, success_videos)
                    _finish_report(conn, report_id, date, "complete", report_json=report_json, report_markdown=markdown)
                elif success_videos:
                    _finish_report(conn, report_id, date, "partial_failed", f"Only {len(success_videos)} videos analyzed successfully")
                else:
                    _finish_report(conn, report_id, date, "failed", "No videos analyzed successfully")
            except Exception as exc:
                _finish_report(conn, report_id, date, "failed", str(exc))
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
    return {"queued": True, "report_date": date, **get_report_runtime_status()}


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
        threading.Thread(target=_scheduler_worker, daemon=True).start()
        threading.Thread(target=_scheduler_loop, daemon=True).start()
