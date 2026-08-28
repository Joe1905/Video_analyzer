#!/usr/bin/env python3
"""Focused regression tests for proxy IP uniqueness and safe proxy deletion."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import proxy_pool  # noqa: E402


@contextmanager
def isolated_proxy_db() -> Iterator[None]:
    original_data_dir = proxy_pool.DATA_DIR
    original_db_path = proxy_pool.DB_PATH
    original_lookup = proxy_pool.lookup_ip_geo
    original_remove = proxy_pool._remove_mihomo_pool_config
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        data_dir = Path(temp_dir)
        proxy_pool.DATA_DIR = data_dir
        proxy_pool.DB_PATH = data_dir / "proxy_pool.sqlite"
        proxy_pool.lookup_ip_geo = lambda _ip: {
            "country": "",
            "region": "",
            "city": "",
            "address": "",
        }
        proxy_pool._remove_mihomo_pool_config = lambda pool: (
            {"removed": True, "port": int(pool["local_port"] or 0)},
            None,
        )
        try:
            yield
        finally:
            proxy_pool.DATA_DIR = original_data_dir
            proxy_pool.DB_PATH = original_db_path
            proxy_pool.lookup_ip_geo = original_lookup
            proxy_pool._remove_mihomo_pool_config = original_remove


def create_manual_pool(name: str, expected_ip: str) -> dict:
    return proxy_pool.upsert_pool(
        {
            "name": name,
            "source_type": "vless",
            "expected_exit_ip": expected_ip,
            "status": proxy_pool.STATUS_ACTIVE,
        }
    )["pool"]


def test_duplicate_exit_ip_is_terminal_until_manual_recheck() -> None:
    with isolated_proxy_db():
        older = create_manual_pool("existing", "203.0.113.10")
        newer = create_manual_pool("duplicate", "203.0.113.10")

        assert older["status"] == proxy_pool.STATUS_ACTIVE
        assert newer["status"] == proxy_pool.STATUS_DUPLICATE
        assert newer["auto_check_failures"] == 0
        assert newer["next_auto_check_at"] == ""
        assert "状态已标记为 IP重复" in newer["parse_error"]

        scheduled_at = proxy_pool.schedule_proxy_recheck_for_pending_job(
            newer["id"],
            "network error",
        )
        assert scheduled_at == ""
        unchanged = proxy_pool.get_pool(newer["id"])
        assert unchanged["status"] == proxy_pool.STATUS_DUPLICATE
        assert unchanged["next_auto_check_at"] == ""

        checked = proxy_pool.check_binding(
            {
                "proxy_profile_id": newer["id"],
                "observed_ip": "203.0.113.10",
                "bind": True,
            }
        )
        assert checked["allowed"] is False
        assert checked["pool"]["status"] == proxy_pool.STATUS_DUPLICATE
        assert checked["pool"]["next_auto_check_at"] == ""
        assert "existing" in checked["reason"]
        assert str(older["local_port"]) in checked["reason"]
        assert proxy_pool.recheck_unavailable_proxies()["attempted"] == 0

        with proxy_pool.connect() as conn:
            conn.execute(
                "UPDATE proxy_profiles SET deleted_at = ?, status = ? WHERE id = ?",
                (proxy_pool.now_iso(), proxy_pool.STATUS_PAUSED, older["id"]),
            )
            conn.commit()

        recovered = proxy_pool.check_binding(
            {
                "proxy_profile_id": newer["id"],
                "observed_ip": "203.0.113.10",
                "bind": True,
            }
        )
        assert recovered["allowed"] is True
        assert recovered["pool"]["status"] == proxy_pool.STATUS_ACTIVE


def test_existing_duplicate_error_is_migrated_without_retry() -> None:
    with isolated_proxy_db():
        older = create_manual_pool("existing", "203.0.113.30")
        newer = create_manual_pool("duplicate", "203.0.113.30")
        old_reason = (
            f"出口 IP 203.0.113.30 已被代理「existing」（本地端口 {older['local_port']}）使用，"
            "重复 IP 已强制标记为不可用"
        )
        with proxy_pool.connect() as conn:
            conn.execute(
                """UPDATE proxy_profiles
                   SET status = ?, parse_error = ?, auto_check_failures = 3,
                       next_auto_check_at = '2099-01-01T00:00:00Z'
                   WHERE id = ?""",
                (proxy_pool.STATUS_ERROR, old_reason, newer["id"]),
            )
            conn.commit()

        migrated = proxy_pool.get_pool(newer["id"])
        assert migrated["status"] == proxy_pool.STATUS_DUPLICATE
        assert migrated["auto_check_failures"] == 0
        assert migrated["next_auto_check_at"] == ""
        assert "状态已标记为 IP重复" in migrated["parse_error"]
        assert "标记为不可用" not in migrated["parse_error"]

        with proxy_pool.connect() as conn:
            conn.execute(
                "UPDATE proxy_profiles SET parse_error = ? WHERE id = ?",
                (old_reason, newer["id"]),
            )
            conn.commit()

        remigrated = proxy_pool.get_pool(newer["id"])
        assert remigrated["status"] == proxy_pool.STATUS_DUPLICATE
        assert "状态已标记为 IP重复" in remigrated["parse_error"]
        assert "标记为不可用" not in remigrated["parse_error"]


def test_delete_pool_preserves_archived_history_and_releases_port() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("deletable", "203.0.113.20")
        now = proxy_pool.now_iso()
        with proxy_pool.connect() as conn:
            account_id = int(
                conn.execute(
                    """INSERT INTO tiktok_accounts (
                           username, proxy_profile_id, status, created_at, updated_at, deleted_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    ("archived", pool["id"], proxy_pool.ACCOUNT_STATUS_PAUSED, now, now, now),
                ).lastrowid
            )
            conn.execute(
                """INSERT INTO publish_assets (id, account_id, original_name, stored_path, created_at)
                   VALUES ('asset-1', ?, 'video.mp4', 'data/video.mp4', ?)""",
                (account_id, now),
            )
            conn.execute(
                """INSERT INTO publish_jobs (
                       id, account_id, proxy_profile_id, asset_id, scheduled_at,
                       status, created_at, updated_at
                   ) VALUES ('publish-1', ?, ?, 'asset-1', ?, 'published', ?, ?)""",
                (account_id, pool["id"], now, now, now),
            )
            conn.execute(
                """INSERT INTO collect_jobs (
                       id, account_id, proxy_profile_id, status, created_at, updated_at
                   ) VALUES ('collect-1', ?, ?, 'complete', ?, ?)""",
                (account_id, pool["id"], now, now),
            )
            conn.commit()

        deleted = proxy_pool.delete_pool(pool["id"])
        assert deleted["pools"] == []

        with proxy_pool.connect() as conn:
            archived_pool = conn.execute(
                "SELECT deleted_at FROM proxy_profiles WHERE id = ?",
                (pool["id"],),
            ).fetchone()
            assert archived_pool and archived_pool["deleted_at"]
            assert conn.execute("SELECT COUNT(*) FROM tiktok_accounts").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM collect_jobs").fetchone()[0] == 1

        replacement = create_manual_pool("replacement", "203.0.113.21")
        assert replacement["local_port"] == pool["local_port"]


def test_account_state_exposes_instagram_login_without_cookie_value() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("instagram", "203.0.113.40")
        profile_dir = proxy_pool.DATA_DIR / "tiktok_browser_profiles" / "account" / "user-data"
        cookie_path = profile_dir / "Default" / "Cookies"
        cookie_path.parent.mkdir(parents=True)
        with sqlite3.connect(cookie_path) as cookie_conn:
            cookie_conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
            cookie_conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?)",
                (".instagram.com", "sessionid", "must-not-leak"),
            )
            cookie_conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?)",
                (".tiktok.com", "sessionid", "also-must-not-leak"),
            )
            cookie_conn.commit()
        now = proxy_pool.now_iso()
        with proxy_pool.connect() as conn:
            account_id = int(
                conn.execute(
                    """INSERT INTO tiktok_accounts (
                           username, proxy_profile_id, status, profile_json, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "instagram_account",
                        pool["id"],
                        proxy_pool.ACCOUNT_STATUS_ACTIVE,
                        json.dumps({"isolation": {"user_data_dir": str(profile_dir)}}),
                        now,
                        now,
                    ),
                ).lastrowid
            )
            conn.commit()

        account = proxy_pool.get_account(account_id)
        assert account["platforms"]["instagram"] == {
            "status": "logged_in",
            "profile_available": True,
            "logged_in": True,
        }
        assert account["platforms"]["tiktok"]["linked"] is True
        assert "must-not-leak" not in str(account)

        instagram_deleted = proxy_pool.delete_account_platform(account_id, "instagram")
        kept_account = next(item for item in instagram_deleted["accounts"] if item["id"] == account_id)
        assert kept_account["platforms"]["instagram"]["logged_in"] is False
        assert kept_account["platforms"]["tiktok"]["linked"] is True
        assert instagram_deleted["deleted_account"] is False

        tiktok_deleted = proxy_pool.delete_account_platform(account_id, "tiktok")
        assert tiktok_deleted["deleted_account"] is True
        assert not any(item["id"] == account_id for item in tiktok_deleted["accounts"])


def test_unified_upstream_keeps_backup_group_dynamic() -> None:
    original_request = proxy_pool._mihomo_request
    calls: list[str] = []

    def fake_request(method: str, path: str, body=None, timeout: float = 5.0):
        calls.append(path)
        return True, {
            "name": proxy_pool.SYSTEM_PROXY_DIALER,
            "type": "Fallback",
            "now": "backup-us",
            "all": ["backup-us", "backup-jp"],
        }, ""

    proxy_pool._mihomo_request = fake_request
    try:
        assert proxy_pool._resolve_system_proxy_dialer("managed-static") == proxy_pool.SYSTEM_PROXY_DIALER
        assert calls == [f"/proxies/{proxy_pool.quote_path(proxy_pool.SYSTEM_PROXY_DIALER)}"]
        try:
            proxy_pool._resolve_system_proxy_dialer("backup-us")
        except proxy_pool.ProxyConfigurationError as exc:
            assert "无法建立无环代理链" in str(exc)
        else:
            raise AssertionError("backup group members must not dial through their own group")
    finally:
        proxy_pool._mihomo_request = original_request


def test_all_non_direct_pool_types_store_unified_upstream() -> None:
    with isolated_proxy_db():
        for source_type in ("vless", "vmess", "static"):
            pool = proxy_pool.upsert_pool(
                {
                    "name": f"{source_type}-manual",
                    "source_type": source_type,
                    "status": proxy_pool.STATUS_ACTIVE,
                }
            )["pool"]
            assert pool["dialer_proxy"] == proxy_pool.SYSTEM_PROXY_DIALER


def test_mihomo_upstream_listener_targets_backup_group() -> None:
    original_config_path = proxy_pool._mihomo_config_path
    original_reload = proxy_pool._reload_mihomo_config
    original_port_open = proxy_pool._port_open
    original_request = proxy_pool._mihomo_request
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        config_path = Path(temp_dir) / "config.yaml"
        config_path.write_text("proxies:\nproxy-groups:\nrules:\n", encoding="utf-8")
        proxy_pool._mihomo_config_path = lambda: config_path
        proxy_pool._reload_mihomo_config = lambda: None
        proxy_pool._port_open = lambda host, port, timeout=1.5: port == proxy_pool.SYSTEM_PROXY_PORT
        proxy_pool._mihomo_request = lambda *args, **kwargs: (
            True,
            {
                "name": proxy_pool.SYSTEM_PROXY_DIALER,
                "type": "Fallback",
                "now": "backup-us",
                "all": ["backup-us", "backup-jp"],
            },
            "",
        )
        try:
            result = proxy_pool._sync_mihomo_upstream_listener()
            rendered = config_path.read_text(encoding="utf-8")
            assert result["dialer_proxy"] == proxy_pool.SYSTEM_PROXY_DIALER
            assert result["port"] == proxy_pool.SYSTEM_PROXY_PORT
            assert f"port: {proxy_pool.SYSTEM_PROXY_PORT}" in rendered
            assert f"proxy: {proxy_pool.SYSTEM_PROXY_DIALER}" in rendered
            assert "proxy-pool-managed-formal-id: -1" in rendered
        finally:
            proxy_pool._mihomo_config_path = original_config_path
            proxy_pool._reload_mihomo_config = original_reload
            proxy_pool._port_open = original_port_open
            proxy_pool._mihomo_request = original_request


def test_reality_export_uses_unified_backup_upstream() -> None:
    original_reality_core = os.environ.get("PROXY_REALITY_CORE")
    original_ensure = proxy_pool.ensure_proxy_cores
    os.environ["PROXY_REALITY_CORE"] = "sing-box"
    proxy_pool.ensure_proxy_cores = lambda *args, **kwargs: {"sing_box": {"enabled": True}}
    try:
        with isolated_proxy_db():
            pool = proxy_pool.upsert_pool(
                {
                    "name": "reality-test",
                    "source_type": "vless",
                    "source_uri": (
                        "vless://00000000-0000-4000-8000-000000000001@example.com:443"
                        "?type=tcp&security=reality&sni=example.com&fp=safari&pbk=test-key#reality-test"
                    ),
                    "status": proxy_pool.STATUS_ACTIVE,
                }
            )["pool"]
            assert pool["dialer_proxy"] == proxy_pool.SYSTEM_PROXY_DIALER

            exported = proxy_pool.sing_box_export()
            outbounds = exported["config"]["outbounds"]
            reality = next(item for item in outbounds if item.get("tag") == f"reality-out-{pool['id']}")
            upstream = next(item for item in outbounds if item.get("tag") == "proxy-pool-upstream")
            assert reality["detour"] == "proxy-pool-upstream"
            assert upstream == {
                "type": "http",
                "tag": "proxy-pool-upstream",
                "server": "127.0.0.1",
                "server_port": proxy_pool.SYSTEM_PROXY_PORT,
            }
    finally:
        proxy_pool.ensure_proxy_cores = original_ensure
        if original_reality_core is None:
            os.environ.pop("PROXY_REALITY_CORE", None)
        else:
            os.environ["PROXY_REALITY_CORE"] = original_reality_core


def main() -> None:
    test_duplicate_exit_ip_is_terminal_until_manual_recheck()
    test_existing_duplicate_error_is_migrated_without_retry()
    test_delete_pool_preserves_archived_history_and_releases_port()
    test_account_state_exposes_instagram_login_without_cookie_value()
    test_unified_upstream_keeps_backup_group_dynamic()
    test_all_non_direct_pool_types_store_unified_upstream()
    test_mihomo_upstream_listener_targets_backup_group()
    test_reality_export_uses_unified_backup_upstream()
    print("proxy pool lifecycle tests passed")


if __name__ == "__main__":
    main()
