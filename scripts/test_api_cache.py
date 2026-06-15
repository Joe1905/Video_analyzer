#!/usr/bin/env python3
"""Smoke tests for the SQLite API cache."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import api_cache  # noqa: E402


def test_miss_hit_and_canonical_key() -> None:
    calls = {"count": 0}

    def fetch() -> dict:
        calls["count"] += 1
        return {"ok": True, "items": [1, 2, 3]}

    request_a = {"params": {"b": 2, "a": 1}}
    request_b = {"params": {"a": 1, "b": 2}}
    first = api_cache.get_cached_or_call("fake", "endpoint", request_a, fetch, ttl_seconds=60)
    second = api_cache.get_cached_or_call("fake", "endpoint", request_b, fetch, ttl_seconds=60)
    assert first["ok"] is True and first["items"] == [1, 2, 3]
    assert second["ok"] is True and second["items"] == [1, 2, 3]
    assert first["_cache"]["hit"] is False
    assert second["_cache"]["hit"] is True
    assert calls["count"] == 1


def test_url_request_normalization() -> None:
    key_a = api_cache.make_cache_key("fake", "url", {"url": " HTTPS://Example.COM/a?b=1#frag ", "q": " test "})
    key_b = api_cache.make_cache_key("fake", "url", {"q": "test", "url": "https://example.com/a?b=1"})
    assert key_a == key_b


def test_expired_refreshes_and_resets_ttl() -> None:
    calls = {"count": 0}

    def fetch() -> dict:
        calls["count"] += 1
        return {"value": calls["count"]}

    request = {"id": "ttl"}
    first = api_cache.get_cached_or_call("fake", "ttl", request, fetch, ttl_seconds=1)
    time.sleep(1.1)
    second = api_cache.get_cached_or_call("fake", "ttl", request, fetch, ttl_seconds=1)
    assert first["value"] == 1
    assert second["value"] == 2
    assert first["_cache"]["hit"] is False
    assert second["_cache"]["hit"] is False
    assert calls["count"] == 2

    key = api_cache.make_cache_key("fake", "ttl", request)
    conn = sqlite3.connect(api_cache.db_path())
    try:
        api_called_at, expires_at = conn.execute(
            "SELECT api_called_at, expires_at FROM api_cache_entries WHERE cache_key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    assert expires_at > api_called_at


def test_failure_does_not_overwrite_old_cache() -> None:
    request = {"id": "stable"}
    api_cache.store_response("fake", "stable", request, {"value": "old"}, ttl_seconds=60, elapsed_ms=3)

    def failing_fetch() -> dict:
        raise RuntimeError("temporary outage")

    try:
        api_cache.get_cached_or_call("fake", "stable", request, failing_fetch, ttl_seconds=0)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected failure")

    key = api_cache.make_cache_key("fake", "stable", request)
    conn = sqlite3.connect(api_cache.db_path())
    try:
        blob, compression = conn.execute(
            "SELECT response_blob, compression FROM api_cache_entries WHERE cache_key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    assert compression == "zlib"
    assert json.loads(zlib.decompress(blob).decode("utf-8")) == {"value": "old"}


def test_disabled_bypasses_cache() -> None:
    os.environ["API_CACHE_ENABLED"] = "0"
    try:
        calls = {"count": 0}

        def fetch() -> dict:
            calls["count"] += 1
            return {"count": calls["count"]}

        first = api_cache.get_cached_or_call("fake", "disabled", {}, fetch)
        second = api_cache.get_cached_or_call("fake", "disabled", {}, fetch)
        assert first["count"] == 1
        assert second["count"] == 2
        assert first["_cache"]["hit"] is False
        assert second["_cache"]["hit"] is False
    finally:
        os.environ["API_CACHE_ENABLED"] = "1"


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["API_CACHE_DB"] = str(Path(temp_dir) / "api_cache.sqlite")
        os.environ["API_CACHE_ENABLED"] = "1"
        os.environ["API_CACHE_TTL_SECONDS"] = "60"
        test_miss_hit_and_canonical_key()
        test_url_request_normalization()
        test_expired_refreshes_and_resets_ttl()
        test_failure_does_not_overwrite_old_cache()
        test_disabled_bypasses_cache()
    print("api cache tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
