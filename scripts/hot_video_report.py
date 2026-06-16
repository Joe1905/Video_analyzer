"""Daily hot-video report storage and TikTok collection."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sociavault_tiktok import call_api

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "hot_video_report.sqlite"
DEFAULT_API_BASE = "https://api.sociavault.com"


def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


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
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hot_videos (
            report_id TEXT NOT NULL,
            report_date TEXT NOT NULL,
            platform TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT,
            author TEXT,
            source_url TEXT,
            source_endpoint TEXT NOT NULL,
            source_label TEXT NOT NULL,
            source_rank INTEGER NOT NULL,
            hot_score INTEGER NOT NULL,
            metrics_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (report_date, platform, video_id),
            FOREIGN KEY (report_id) REFERENCES daily_reports(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hot_videos_report_score ON hot_videos(report_date, hot_score DESC)")
    conn.commit()
    return conn


def _json_loads(value: str, fallback: Any) -> Any:
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


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


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
        "source_endpoint": endpoint,
        "source_label": label,
        "source_rank": rank,
        "hot_score": int(hot_score),
        "metrics": metrics,
        "raw": node,
    }


def _row_to_report(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    keys = ("id", "report_date", "status", "region", "sources_json", "video_count", "error", "created_at", "updated_at")
    data = dict(zip(keys, row))
    data["sources"] = _json_loads(str(data.pop("sources_json") or ""), [])
    return data


def _row_to_video(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    keys = (
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
    )
    data = dict(zip(keys, row))
    data["metrics"] = _json_loads(str(data.pop("metrics_json") or ""), {})
    data["raw"] = _json_loads(str(data.pop("raw_json") or ""), {})
    return data


def get_report(report_date: str | None = None, include_raw: bool = False) -> dict[str, Any]:
    date = report_date or today_key()
    with _connect() as conn:
        report_row = conn.execute(
            """
            SELECT id, report_date, status, region, sources_json, video_count, error, created_at, updated_at
            FROM daily_reports WHERE report_date = ?
            """,
            (date,),
        ).fetchone()
        if not report_row:
            return {"exists": False, "report_date": date, "status": "missing", "videos": []}
        rows = conn.execute(
            """
            SELECT platform, video_id, title, author, source_url, source_endpoint, source_label,
                   source_rank, hot_score, metrics_json, raw_json, created_at, updated_at
            FROM hot_videos
            WHERE report_date = ?
            ORDER BY hot_score DESC
            """,
            (date,),
        ).fetchall()
    report = _row_to_report(report_row)
    videos = [_row_to_video(row) for row in rows]
    if not include_raw:
        for video in videos:
            video.pop("raw", None)
    report["exists"] = True
    report["videos"] = videos
    return report


def list_reports(limit: int = 30) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, report_date, status, region, sources_json, video_count, error, created_at, updated_at
            FROM daily_reports
            ORDER BY report_date DESC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
    return [_row_to_report(row) for row in rows]


def _source_requests(region: str, count: int) -> list[tuple[str, dict[str, Any], str]]:
    keywords = [item.strip() for item in os.getenv("HOT_VIDEO_KEYWORDS", "viral").split(",") if item.strip()]
    requests = [("trending", {"region": region, "count": count, "trim": "true"}, f"trending:{region}")]
    for keyword in keywords:
        requests.append(
            (
                "search-top",
                {"query": keyword, "region": region, "count": count, "trim": "true"},
                f"search-top:{keyword}",
            )
        )
    return requests


def _start_report(conn: sqlite3.Connection, report_date: str, region: str, sources: list[dict[str, Any]]) -> str:
    now = time.time()
    report_id = uuid.uuid4().hex
    conn.execute("DELETE FROM hot_videos WHERE report_date = ?", (report_date,))
    conn.execute(
        """
        INSERT INTO daily_reports (id, report_date, status, region, sources_json, video_count, error, created_at, updated_at)
        VALUES (?, ?, 'running', ?, ?, 0, NULL, ?, ?)
        ON CONFLICT(report_date) DO UPDATE SET
            id = excluded.id,
            status = 'running',
            region = excluded.region,
            sources_json = excluded.sources_json,
            video_count = 0,
            error = NULL,
            updated_at = excluded.updated_at
        """,
        (report_id, report_date, region, json.dumps(sources, ensure_ascii=False), now, now),
    )
    conn.commit()
    return report_id


def _finish_report(conn: sqlite3.Connection, report_id: str, report_date: str, status: str, error: str = "") -> None:
    now = time.time()
    row = conn.execute("SELECT COUNT(*) FROM hot_videos WHERE report_date = ?", (report_date,)).fetchone()
    conn.execute(
        """
        UPDATE daily_reports
        SET status = ?, video_count = ?, error = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, int(row[0] if row else 0), error, now, report_id),
    )
    conn.commit()


def run_report(report_date: str | None = None) -> dict[str, Any]:
    date = report_date or today_key()
    region = os.getenv("HOT_VIDEO_REGION", os.getenv("SOCIAVAULT_REGION", "US")).strip() or "US"
    count = _env_int("HOT_VIDEO_SOURCE_COUNT", 30, 1, 100)
    limit = _env_int("HOT_VIDEO_REPORT_LIMIT", 20, 1, 100)
    api_key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE).rstrip("/")
    sources = [{"endpoint": endpoint, "label": label, "params": params} for endpoint, params, label in _source_requests(region, count)]

    with _connect() as conn:
        report_id = _start_report(conn, date, region, sources)
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

            ranked = sorted(candidates.values(), key=lambda item: item["hot_score"], reverse=True)[:limit]
            now = time.time()
            for item in ranked:
                conn.execute(
                    """
                    INSERT INTO hot_videos (
                        report_id, report_date, platform, video_id, title, author, source_url,
                        source_endpoint, source_label, source_rank, hot_score, metrics_json,
                        raw_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(report_date, platform, video_id) DO UPDATE SET
                        title = excluded.title,
                        author = excluded.author,
                        source_url = excluded.source_url,
                        source_endpoint = excluded.source_endpoint,
                        source_label = excluded.source_label,
                        source_rank = excluded.source_rank,
                        hot_score = excluded.hot_score,
                        metrics_json = excluded.metrics_json,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        report_id,
                        date,
                        item["platform"],
                        item["video_id"],
                        item["title"],
                        item["author"],
                        item["source_url"],
                        item["source_endpoint"],
                        item["source_label"],
                        item["source_rank"],
                        item["hot_score"],
                        json.dumps(item["metrics"], ensure_ascii=False, sort_keys=True),
                        json.dumps(item["raw"], ensure_ascii=False, sort_keys=True, default=str),
                        now,
                        now,
                    ),
                )
            conn.commit()
            _finish_report(conn, report_id, date, "complete")
        except Exception as exc:
            _finish_report(conn, report_id, date, "failed", str(exc))
    return get_report(date, include_raw=True)
