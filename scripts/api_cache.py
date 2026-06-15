#!/usr/bin/env python3
"""Small SQLite-backed cache for external API responses."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from entity_registry import register_entities_from_payload, register_entity

ROOT = Path.cwd()
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "api_cache.sqlite"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
LOG_PREFIX = "[API_CACHE]"


def cache_enabled() -> bool:
    return os.getenv("API_CACHE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def default_ttl_seconds() -> int:
    raw = os.getenv("API_CACHE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def db_path() -> Path:
    return Path(os.getenv("API_CACHE_DB", str(DEFAULT_DB_PATH)))


def _log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def _short_key(cache_key: str) -> str:
    return cache_key[:16]


def _safe_log_value(value: Any, limit: int = 120) -> str:
    text = str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_cache_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_cache_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalize_cache_value(child) for child in value]
    if isinstance(value, str):
        text = value.strip()
        parsed = urlparse(text)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            host = (parsed.hostname or "").lower().rstrip(".")
            netloc = host
            if parsed.port:
                netloc = f"{host}:{parsed.port}"
            return urlunparse((parsed.scheme.lower(), netloc, parsed.path or "/", "", parsed.query, ""))
        return text
    return value


def make_cache_key(provider: str, endpoint: str, request: dict[str, Any]) -> str:
    payload = {"provider": provider, "endpoint": endpoint, "request": _normalize_cache_value(request)}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    if conn is None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_cache_entries (
                cache_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                request_json TEXT NOT NULL,
                response_blob BLOB NOT NULL,
                compression TEXT NOT NULL,
                api_called_at REAL NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at REAL,
                entity_type TEXT,
                entity_id TEXT,
                title TEXT,
                author TEXT,
                source_url TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_cache_provider_endpoint ON api_cache_entries(provider, endpoint)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_cache_expires_at ON api_cache_entries(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_cache_entity ON api_cache_entries(entity_type, entity_id)")
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def _decode_response(blob: bytes, compression: str) -> Any:
    if compression == "zlib":
        raw = zlib.decompress(blob)
    elif compression == "none":
        raw = blob
    else:
        raise ValueError(f"Unsupported compression: {compression}")
    return json.loads(raw.decode("utf-8"))


def _encode_response(payload: Any) -> tuple[bytes, int, int]:
    raw = canonical_json(payload).encode("utf-8")
    compressed = zlib.compress(raw)
    return compressed, len(raw), len(compressed)


def _with_cache_meta(payload: Any, *, hit: bool, provider: str, endpoint: str) -> Any:
    meta = {
        "hit": hit,
        "provider": provider,
        "endpoint": endpoint,
        "label": "缓存命中" if hit else "实时调用",
    }
    if isinstance(payload, dict):
        result = dict(payload)
        result["_cache"] = meta
        return result
    return payload


def get_cached(provider: str, endpoint: str, request: dict[str, Any], ttl_seconds: int | None = None) -> Any | None:
    if not cache_enabled():
        _log(f"bypass reason=disabled provider={provider} endpoint={endpoint}")
        return None
    cache_key = make_cache_key(provider, endpoint, request)
    now = time.time()
    ttl = default_ttl_seconds() if ttl_seconds is None else ttl_seconds
    _log(f"lookup provider={provider} endpoint={endpoint} key={_short_key(cache_key)}")
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        row = conn.execute(
            """
            SELECT response_blob, compression, api_called_at, expires_at, hit_count
            FROM api_cache_entries
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
        if row is None:
            _log(f"miss provider={provider} endpoint={endpoint}")
            return None
        blob, compression, api_called_at, expires_at, hit_count = row
        if expires_at <= now or ttl == 0:
            _log(
                "expired "
                f"provider={provider} endpoint={endpoint} "
                f"age={int(now - api_called_at)} expired_at={int(expires_at)}"
            )
            return None
        try:
            payload = _decode_response(blob, compression)
        except Exception as exc:
            _log(f"error phase=decompress provider={provider} endpoint={endpoint} message={_safe_log_value(exc)}")
            return None
        conn.execute(
            "UPDATE api_cache_entries SET hit_count = ?, last_hit_at = ?, updated_at = ? WHERE cache_key = ?",
            (int(hit_count or 0) + 1, now, now, cache_key),
        )
        conn.commit()
        _log(
            "hit "
            f"provider={provider} endpoint={endpoint} age={int(now - api_called_at)} "
            f"expires_in={int(expires_at - now)}"
        )
        return _with_cache_meta(payload, hit=True, provider=provider, endpoint=endpoint)
    except Exception as exc:
        _log(f"error phase=read provider={provider} endpoint={endpoint} message={_safe_log_value(exc)}")
        return None
    finally:
        if conn is not None:
            conn.close()


def store_response(
    provider: str,
    endpoint: str,
    request: dict[str, Any],
    response: Any,
    *,
    ttl_seconds: int | None = None,
    elapsed_ms: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not cache_enabled():
        _log(f"bypass reason=disabled provider={provider} endpoint={endpoint}")
        return
    ttl = default_ttl_seconds() if ttl_seconds is None else ttl_seconds
    now = time.time()
    expires_at = now + ttl
    cache_key = make_cache_key(provider, endpoint, request)
    metadata = metadata or {}
    try:
        response_blob, raw_bytes, compressed_bytes = _encode_response(response)
        request_json = canonical_json(_normalize_cache_value(request))
    except Exception as exc:
        _log(f"bypass reason=unserializable provider={provider} endpoint={endpoint} message={_safe_log_value(exc)}")
        return
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO api_cache_entries (
                cache_key, provider, endpoint, request_json, response_blob, compression,
                api_called_at, elapsed_ms, expires_at, hit_count, last_hit_at,
                entity_type, entity_id, title, author, source_url, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'zlib', ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                request_json = excluded.request_json,
                response_blob = excluded.response_blob,
                compression = excluded.compression,
                api_called_at = excluded.api_called_at,
                elapsed_ms = excluded.elapsed_ms,
                expires_at = excluded.expires_at,
                hit_count = 0,
                last_hit_at = NULL,
                entity_type = excluded.entity_type,
                entity_id = excluded.entity_id,
                title = excluded.title,
                author = excluded.author,
                source_url = excluded.source_url,
                updated_at = excluded.updated_at
            """,
            (
                cache_key,
                provider,
                endpoint,
                request_json,
                response_blob,
                now,
                int(elapsed_ms),
                expires_at,
                metadata.get("entity_type"),
                metadata.get("entity_id"),
                metadata.get("title"),
                metadata.get("author"),
                metadata.get("source_url"),
                now,
                now,
            ),
        )
        conn.commit()
        registered_entities = register_entities_from_payload(response, provider=provider, endpoint=endpoint)
        if metadata.get("entity_type") and metadata.get("entity_id"):
            if register_entity(
                str(metadata.get("entity_type")),
                metadata.get("entity_id"),
                source_url=str(metadata.get("source_url") or ""),
                title=str(metadata.get("title") or ""),
                author=str(metadata.get("author") or ""),
                provider=provider,
                endpoint=endpoint,
                ttl_seconds=ttl,
            ):
                registered_entities += 1
        _log(
            "store "
            f"provider={provider} endpoint={endpoint} elapsed_ms={int(elapsed_ms)} "
            f"raw_bytes={raw_bytes} compressed_bytes={compressed_bytes} expires_at={int(expires_at)} "
            f"entities={registered_entities}"
        )
    except Exception as exc:
        _log(f"error phase=store provider={provider} endpoint={endpoint} message={_safe_log_value(exc)}")
    finally:
        if conn is not None:
            conn.close()


def get_cached_or_call(
    provider: str,
    endpoint: str,
    request: dict[str, Any],
    caller: Callable[[], Any],
    *,
    ttl_seconds: int | None = None,
    metadata_builder: Callable[[Any], dict[str, Any]] | None = None,
    cache_policy: str = "read_write",
) -> Any:
    if cache_policy == "record_only":
        _log(f"bypass reason=record_only provider={provider} endpoint={endpoint}")
        started = time.monotonic()
        response = caller()
        metadata = metadata_builder(response) if metadata_builder else {}
        store_response(provider, endpoint, request, response, ttl_seconds=ttl_seconds, elapsed_ms=int((time.monotonic() - started) * 1000), metadata=metadata)
        return _with_cache_meta(response, hit=False, provider=provider, endpoint=endpoint)
    cached = get_cached(provider, endpoint, request, ttl_seconds)
    if cached is not None:
        return cached
    started = time.monotonic()
    response = caller()
    metadata = metadata_builder(response) if metadata_builder else {}
    store_response(provider, endpoint, request, response, ttl_seconds=ttl_seconds, elapsed_ms=int((time.monotonic() - started) * 1000), metadata=metadata)
    return _with_cache_meta(response, hit=False, provider=provider, endpoint=endpoint)


def record_api_call(
    provider: str,
    endpoint: str,
    request: dict[str, Any],
    response: Any,
    *,
    elapsed_ms: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    _log(f"bypass reason=record_only provider={provider} endpoint={endpoint}")
    store_response(provider, endpoint, request, response, elapsed_ms=elapsed_ms, metadata=metadata)
