"""Registry for stable external entity ids discovered in API responses."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "entity_registry.sqlite"
DAY_SECONDS = 24 * 60 * 60
DEFAULT_TTL_SECONDS = DAY_SECONDS
STABLE_TTL_SECONDS = 10 * 365 * DAY_SECONDS


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            canonical_key TEXT NOT NULL UNIQUE,
            source_url TEXT,
            title TEXT,
            author TEXT,
            provider TEXT,
            endpoint TEXT,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            extra_json TEXT,
            PRIMARY KEY (entity_type, entity_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_expires_at ON entities(expires_at)")
    conn.commit()
    return conn


def _clean_id(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"[^A-Za-z0-9_.:@/-]+", "_", text).strip("_")[:180]


def ttl_for_entity(entity_type: str) -> int:
    configured = os.getenv(f"ENTITY_TTL_{entity_type.upper()}_SECONDS", "").strip()
    if configured:
        try:
            return max(0, int(configured))
        except ValueError:
            pass
    if entity_type in {"tiktok_video", "tiktok_shop_product", "tiktok_user", "tiktok_music", "amazon_product"}:
        return int(os.getenv("ENTITY_STABLE_TTL_SECONDS", str(STABLE_TTL_SECONDS)))
    return int(os.getenv("ENTITY_DEFAULT_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))


def _first_present(value: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        found = value.get(name)
        if found not in (None, "", [], {}):
            return found
    return None


def _nested(value: dict[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        child = value.get(name)
        if isinstance(child, dict):
            return child
    return {}


def _compact_extra(value: dict[str, Any], limit: int = 50000) -> dict[str, Any]:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return {}
    if len(text) <= limit:
        return value
    return {"_truncated": True, "keys": sorted(map(str, value.keys()))[:80]}


def _stats(value: dict[str, Any]) -> dict[str, Any]:
    stats = _nested(value, "statistics", "stats", "statistics_info", "stats_info")
    out: dict[str, Any] = {}
    for key in (
        "play_count", "playCount", "digg_count", "diggCount", "like_count", "likeCount",
        "comment_count", "commentCount", "share_count", "shareCount", "collect_count", "collectCount",
    ):
        found = value.get(key, stats.get(key) if isinstance(stats, dict) else None)
        if found not in (None, "", [], {}):
            out[key] = found
    return out


def register_entity(
    entity_type: str,
    entity_id: Any,
    *,
    source_url: str = "",
    title: str = "",
    author: str = "",
    provider: str = "",
    endpoint: str = "",
    ttl_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_id = _clean_id(entity_id)
    if not clean_id:
        return {}
    entity_type = _clean_id(entity_type).lower()
    if not entity_type:
        return {}
    ttl = ttl_for_entity(entity_type) if ttl_seconds is None else ttl_seconds
    now = time.time()
    expires_at = now + ttl
    canonical_key = f"{entity_type}:{clean_id}"
    extra_json = json.dumps(extra or {}, ensure_ascii=False, sort_keys=True) if extra else ""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO entities (
                entity_type, entity_id, canonical_key, source_url, title, author,
                provider, endpoint, first_seen_at, last_seen_at, expires_at, hit_count, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                source_url = COALESCE(NULLIF(excluded.source_url, ''), source_url),
                title = COALESCE(NULLIF(excluded.title, ''), title),
                author = COALESCE(NULLIF(excluded.author, ''), author),
                provider = COALESCE(NULLIF(excluded.provider, ''), provider),
                endpoint = COALESCE(NULLIF(excluded.endpoint, ''), endpoint),
                last_seen_at = excluded.last_seen_at,
                expires_at = excluded.expires_at,
                hit_count = hit_count + 1,
                extra_json = COALESCE(NULLIF(excluded.extra_json, ''), extra_json)
            """,
            (
                entity_type,
                clean_id,
                canonical_key,
                str(source_url or "")[:1000],
                str(title or "")[:500],
                str(author or "")[:300],
                provider,
                endpoint,
                now,
                now,
                expires_at,
                extra_json,
            ),
        )
        conn.commit()
    return {"entity_type": entity_type, "entity_id": clean_id, "expires_at": expires_at}


def _register_product(value: dict[str, Any], provider: str, endpoint: str) -> int:
    product_id = _first_present(value, ("product_id", "productId", "productIdStr"))
    if not product_id:
        return 0
    seo = _nested(value, "seo_url", "seoUrl")
    seller = _nested(value, "seller_info", "sellerInfo")
    register_entity(
        "tiktok_shop_product",
        product_id,
        source_url=str(_first_present(seo, ("canonical_url", "url")) or _first_present(value, ("url", "product_url")) or ""),
        title=str(_first_present(value, ("title", "product_name", "name")) or ""),
        author=str(_first_present(seller, ("shop_name", "seller_name", "name")) or ""),
        provider=provider,
        endpoint=endpoint,
        extra={"seller_id": _first_present(seller, ("seller_id", "sellerId"))},
    )
    return 1


def _register_user(value: dict[str, Any], provider: str, endpoint: str) -> int:
    user_id = _first_present(value, ("uid", "user_id", "userId", "sec_uid", "secUid", "unique_id", "uniqueId"))
    if not user_id:
        return 0
    handle = str(_first_present(value, ("unique_id", "uniqueId", "handle", "nickname")) or "")
    register_entity(
        "tiktok_user",
        user_id,
        source_url=str(_first_present(value, ("url", "share_url", "shareUrl")) or ""),
        title=handle,
        author=handle,
        provider=provider,
        endpoint=endpoint,
        extra={
            "handle": handle,
            "nickname": _first_present(value, ("nickname", "nickName", "display_name", "displayName")),
            "snapshot": _compact_extra(value),
        },
    )
    return 1


def _register_music(value: dict[str, Any], provider: str, endpoint: str) -> int:
    music_id = _first_present(value, ("music_id", "musicId", "sound_id", "soundId", "id", "mid"))
    if not music_id:
        return 0
    register_entity(
        "tiktok_music",
        music_id,
        title=str(_first_present(value, ("title", "music_name", "musicName", "name")) or ""),
        author=str(_first_present(value, ("author", "author_name", "authorName")) or ""),
        provider=provider,
        endpoint=endpoint,
    )
    return 1


def _register_video(value: dict[str, Any], provider: str, endpoint: str) -> int:
    video_id = _first_present(value, ("aweme_id", "awemeId", "video_id", "videoId", "item_id", "itemId"))
    if not video_id:
        return 0
    author = _nested(value, "author")
    music = _nested(value, "music", "added_sound_music_info", "music_info")
    author_handle = str(_first_present(author, ("unique_id", "uniqueId", "nickname")) or _first_present(value, ("author_unique_id", "authorUniqueId")) or "")
    author_id = _first_present(author, ("uid", "user_id", "userId", "sec_uid", "secUid", "id")) or _first_present(value, ("author_id", "authorId", "sec_uid", "secUid"))
    source_url = str(_first_present(value, ("share_url", "shareUrl", "webpage_url", "url")) or "")
    if not source_url and author_handle:
        source_url = f"https://www.tiktok.com/@{author_handle}/video/{video_id}"
    register_entity(
        "tiktok_video",
        video_id,
        source_url=source_url,
        title=str(_first_present(value, ("desc", "description", "title")) or ""),
        author=author_handle,
        provider=provider,
        endpoint=endpoint,
        extra={
            "author_id": author_id,
            "author_handle": author_handle,
            "music_id": _first_present(music, ("id", "mid", "music_id", "musicId")),
            "stats": _stats(value),
            "snapshot": _compact_extra(value),
        },
    )
    return 1


def _walk(value: Any, provider: str, endpoint: str, seen: set[tuple[str, str]]) -> int:
    count = 0
    if isinstance(value, dict):
        candidates: list[tuple[str, dict[str, Any]]] = []
        if any(key in value for key in ("product_id", "productId", "productIdStr")):
            candidates.append(("product", value))
        if any(key in value for key in ("uid", "user_id", "userId", "sec_uid", "secUid", "unique_id", "uniqueId")):
            candidates.append(("user", value))
        if any(key in value for key in ("music_id", "musicId", "sound_id", "soundId", "mid")) and any(key in value for key in ("title", "music_name", "musicName", "name")):
            candidates.append(("music", value))
        if any(key in value for key in ("aweme_id", "awemeId", "video_id", "videoId", "item_id", "itemId")):
            candidates.append(("video", value))

        for kind, candidate in candidates:
            before = len(seen)
            if kind == "product":
                entity_id = _clean_id(_first_present(candidate, ("product_id", "productId", "productIdStr")))
                key = ("tiktok_shop_product", entity_id)
                if entity_id and key not in seen:
                    seen.add(key)
                    count += _register_product(candidate, provider, endpoint)
            elif kind == "user":
                entity_id = _clean_id(_first_present(candidate, ("uid", "user_id", "userId", "sec_uid", "secUid", "unique_id", "uniqueId")))
                key = ("tiktok_user", entity_id)
                if entity_id and key not in seen:
                    seen.add(key)
                    count += _register_user(candidate, provider, endpoint)
            elif kind == "music":
                entity_id = _clean_id(_first_present(candidate, ("music_id", "musicId", "sound_id", "soundId", "id", "mid")))
                key = ("tiktok_music", entity_id)
                if entity_id and key not in seen:
                    seen.add(key)
                    count += _register_music(candidate, provider, endpoint)
            elif kind == "video":
                entity_id = _clean_id(_first_present(candidate, ("aweme_id", "awemeId", "video_id", "videoId", "item_id", "itemId")))
                key = ("tiktok_video", entity_id)
                if entity_id and key not in seen:
                    seen.add(key)
                    count += _register_video(candidate, provider, endpoint)
            if len(seen) == before:
                continue

        for child in value.values():
            count += _walk(child, provider, endpoint, seen)
    elif isinstance(value, list):
        for child in value:
            count += _walk(child, provider, endpoint, seen)
    return count


def register_entities_from_payload(payload: Any, *, provider: str = "", endpoint: str = "") -> int:
    try:
        return _walk(payload, provider, endpoint, set())
    except Exception as exc:
        print(f"[ENTITY_REGISTRY] scan failed provider={provider} endpoint={endpoint}: {exc}", flush=True)
        return 0



def _row_to_entity(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    keys = ["entity_type", "entity_id", "canonical_key", "source_url", "title", "author", "provider", "endpoint", "first_seen_at", "last_seen_at", "expires_at", "hit_count", "extra_json"]
    data = dict(zip(keys, row))
    extra_raw = data.pop("extra_json", "") or ""
    try:
        data["extra"] = json.loads(extra_raw) if extra_raw else {}
    except Exception:
        data["extra"] = {}
    return data


def get_entity(entity_type: str, entity_id: Any) -> dict[str, Any] | None:
    clean_id = _clean_id(entity_id)
    if not clean_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT entity_type, entity_id, canonical_key, source_url, title, author,
                   provider, endpoint, first_seen_at, last_seen_at, expires_at, hit_count, extra_json
            FROM entities WHERE entity_type = ? AND entity_id = ?
            """,
            (_clean_id(entity_type).lower(), clean_id),
        ).fetchone()
    return _row_to_entity(row) if row else None


def find_user(identifier: str) -> dict[str, Any] | None:
    needle = str(identifier or "").strip().lstrip("@").lower()
    if not needle:
        return None
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT entity_type, entity_id, canonical_key, source_url, title, author,
                   provider, endpoint, first_seen_at, last_seen_at, expires_at, hit_count, extra_json
            FROM entities WHERE entity_type = 'tiktok_user'
            """
        ).fetchall()
    for row in rows:
        entity = _row_to_entity(row)
        extra = entity.get("extra") or {}
        values = [entity.get("entity_id"), entity.get("title"), entity.get("author"), extra.get("handle"), extra.get("nickname")]
        if any(str(v or "").strip().lstrip("@").lower() == needle for v in values):
            return entity
    return None


def videos_for_user(identifier: str, limit: int = 50) -> list[dict[str, Any]]:
    needle = str(identifier or "").strip().lstrip("@").lower()
    if not needle:
        return []
    user = find_user(needle)
    user_ids = {needle}
    if user:
        user_ids.add(str(user.get("entity_id") or "").lower())
        extra = user.get("extra") or {}
        user_ids.add(str(extra.get("handle") or "").strip().lstrip("@").lower())
        user_ids.add(str(extra.get("nickname") or "").strip().lstrip("@").lower())
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT entity_type, entity_id, canonical_key, source_url, title, author,
                   provider, endpoint, first_seen_at, last_seen_at, expires_at, hit_count, extra_json
            FROM entities WHERE entity_type = 'tiktok_video'
            ORDER BY last_seen_at DESC
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        entity = _row_to_entity(row)
        extra = entity.get("extra") or {}
        values = [entity.get("author"), extra.get("author_handle"), extra.get("author_id")]
        if not any(str(v or "").strip().lstrip("@").lower() in user_ids for v in values):
            continue
        snapshot = extra.get("snapshot") if isinstance(extra.get("snapshot"), dict) else {}
        item = dict(snapshot) if snapshot else {}
        item.setdefault("aweme_id", entity.get("entity_id"))
        item.setdefault("video_id", entity.get("entity_id"))
        item.setdefault("desc", entity.get("title"))
        item.setdefault("url", entity.get("source_url"))
        item.setdefault("author", {"unique_id": entity.get("author")})
        if extra.get("stats"):
            item.setdefault("statistics", extra.get("stats"))
        item["_entity_registry"] = {"hit": True, "last_seen_at": entity.get("last_seen_at"), "source_url": entity.get("source_url")}
        out.append(item)
        if len(out) >= limit:
            break
    return out
