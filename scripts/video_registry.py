"""Registry for mapping external video ids to local files and extraction state."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "video_registry.sqlite"
SOURCE_WEB_MANUAL = "web_manual"
SOURCE_HOT_REPORT = "hot_report"
SOURCE_API_UPLOAD = "api_upload"
VISIBLE_ANALYZER_SOURCES = {SOURCE_WEB_MANUAL, ""}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            platform TEXT NOT NULL,
            video_id TEXT NOT NULL,
            canonical_key TEXT NOT NULL UNIQUE,
            source_url TEXT,
            filename TEXT,
            title TEXT,
            author TEXT,
            extraction_dir TEXT,
            extracted_at REAL,
            source TEXT,
            hidden_from_analyzer INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (platform, video_id)
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    if "source" not in existing:
        conn.execute("ALTER TABLE videos ADD COLUMN source TEXT")
    if "hidden_from_analyzer" not in existing:
        conn.execute("ALTER TABLE videos ADD COLUMN hidden_from_analyzer INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_filename ON videos(filename)")
    conn.commit()
    return conn


def _clean_id(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:120]


def video_id_from_url(url: str) -> str | None:
    match = re.search(r"/video/(\d+)", str(url or ""))
    return match.group(1) if match else None


def platform_for_url(url: str) -> str:
    host = (urlparse(str(url or "")).hostname or "").lower()
    if "douyin" in host:
        return "douyin"
    return "tiktok"


def find_video_id(payload: Any, fallback_url: str = "") -> str | None:
    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("aweme_id", "video_id", "item_id", "id"):
                found = _clean_id(value.get(key))
                if found:
                    return found
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

    return walk(payload) or video_id_from_url(fallback_url)


def register_video(
    *,
    video_id: str,
    platform: str = "tiktok",
    source_url: str = "",
    filename: str = "",
    title: str = "",
    author: str = "",
    extraction_dir: str = "",
    source: str = "",
    hidden_from_analyzer: bool | None = None,
) -> dict[str, Any]:
    clean_video_id = _clean_id(video_id)
    if not clean_video_id:
        return {}
    platform = (platform or "tiktok").strip().lower()
    canonical_key = f"{platform}:{clean_video_id}"
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO videos (
                platform, video_id, canonical_key, source_url, filename, title, author,
                extraction_dir, source, hidden_from_analyzer, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, video_id) DO UPDATE SET
                source_url = COALESCE(NULLIF(excluded.source_url, ''), source_url),
                filename = COALESCE(NULLIF(excluded.filename, ''), filename),
                title = COALESCE(NULLIF(excluded.title, ''), title),
                author = COALESCE(NULLIF(excluded.author, ''), author),
                extraction_dir = COALESCE(NULLIF(excluded.extraction_dir, ''), extraction_dir),
                source = CASE
                    WHEN source = 'web_manual' AND excluded.source != 'web_manual' THEN source
                    ELSE COALESCE(NULLIF(excluded.source, ''), source)
                END,
                hidden_from_analyzer = CASE
                    WHEN hidden_from_analyzer = 0 THEN 0
                    WHEN excluded.hidden_from_analyzer = 0 THEN hidden_from_analyzer
                    ELSE excluded.hidden_from_analyzer
                END,
                updated_at = excluded.updated_at
            """,
            (
                platform,
                clean_video_id,
                canonical_key,
                source_url,
                filename,
                title,
                author,
                extraction_dir,
                source,
                1 if hidden_from_analyzer else 0,
                now,
                now,
            ),
        )
        conn.commit()
    return get_video(platform, clean_video_id) or {}


def register_from_payload(
    payload: Any,
    source_url: str = "",
    filename: str = "",
    source: str = "",
    hidden_from_analyzer: bool | None = None,
) -> dict[str, Any]:
    video_id = find_video_id(payload, source_url)
    if not video_id:
        return {}
    title = ""
    author = ""
    if isinstance(payload, dict):
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > 0:
            def first_key(value: Any, keys: tuple[str, ...]) -> Any:
                if isinstance(value, dict):
                    for key in keys:
                        if value.get(key):
                            return value.get(key)
                    for child in value.values():
                        found = first_key(child, keys)
                        if found:
                            return found
                elif isinstance(value, list):
                    for child in value:
                        found = first_key(child, keys)
                        if found:
                            return found
                return None

            title = str(first_key(payload, ("desc", "description", "title")) or "")[:300]
            author = str(first_key(payload, ("unique_id", "nickname", "author")) or "")[:160]
    return register_video(
        video_id=video_id,
        platform=platform_for_url(source_url),
        source_url=source_url,
        filename=filename,
        title=title,
        author=author,
        source=source,
        hidden_from_analyzer=hidden_from_analyzer,
    )


def get_video(platform: str, video_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT platform, video_id, canonical_key, source_url, filename, title, author,
                   extraction_dir, extracted_at, source, hidden_from_analyzer, created_at, updated_at
            FROM videos
            WHERE platform = ? AND video_id = ?
            """,
            ((platform or "tiktok").lower(), _clean_id(video_id)),
        ).fetchone()
    if not row:
        return None
    keys = (
        "platform", "video_id", "canonical_key", "source_url", "filename", "title",
        "author", "extraction_dir", "extracted_at", "source", "hidden_from_analyzer", "created_at", "updated_at",
    )
    return dict(zip(keys, row))


def get_video_by_filename(filename: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT platform, video_id, canonical_key, source_url, filename, title, author,
                   extraction_dir, extracted_at, source, hidden_from_analyzer, created_at, updated_at
            FROM videos
            WHERE filename = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (Path(filename).name,),
        ).fetchone()
    if not row:
        return None
    keys = (
        "platform", "video_id", "canonical_key", "source_url", "filename", "title",
        "author", "extraction_dir", "extracted_at", "source", "hidden_from_analyzer", "created_at", "updated_at",
    )
    return dict(zip(keys, row))


def mark_extracted(filename: str, extraction_dir: str) -> None:
    record = get_video_by_filename(filename)
    if not record:
        return
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "UPDATE videos SET extraction_dir = ?, extracted_at = ?, updated_at = ? WHERE platform = ? AND video_id = ?",
            (extraction_dir, now, now, record["platform"], record["video_id"]),
        )
        conn.commit()


def is_hidden_from_analyzer(filename: str) -> bool:
    record = get_video_by_filename(filename)
    return bool(record and int(record.get("hidden_from_analyzer") or 0))


def analyzer_visible_source(filename: str) -> bool:
    record = get_video_by_filename(filename)
    if not record:
        return True
    if int(record.get("hidden_from_analyzer") or 0):
        return False
    return str(record.get("source") or "") in VISIBLE_ANALYZER_SOURCES


def set_hidden_from_analyzer(platform: str, video_id: str, hidden: bool) -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET hidden_from_analyzer = ?, updated_at = ?
            WHERE platform = ? AND video_id = ?
            """,
            (1 if hidden else 0, now, platform, video_id),
        )
        conn.commit()

