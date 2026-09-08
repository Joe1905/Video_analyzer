#!/usr/bin/env python3
"""Focused regression tests for proxy IP uniqueness and safe proxy deletion."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import proxy_pool  # noqa: E402
from proxy import accounts, pools, repository, runtime, settings, state  # noqa: E402


@contextmanager
def isolated_proxy_db() -> Iterator[None]:
    original_settings_data_dir = settings.DATA_DIR
    original_settings_db_path = settings.DB_PATH
    original_lookup = pools.lookup_ip_geo
    original_remove = pools._remove_mihomo_pool_config
    original_sync = pools._sync_mihomo_pool_config
    original_sqlite_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = original_sqlite_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    data_dir = Path(temporary.name)
    sqlite3.connect = tracked_connect
    settings.DATA_DIR = data_dir
    settings.DB_PATH = data_dir / "proxy_pool.sqlite"
    pools.lookup_ip_geo = lambda _ip: {
        "country": "",
        "region": "",
        "city": "",
        "address": "",
    }
    pools._remove_mihomo_pool_config = lambda pool: (
        {"removed": True, "port": int(pool["local_port"] or 0)},
        None,
    )
    pools._sync_mihomo_pool_config = lambda pool: {
        "loaded": True,
        "listener_port": int(pool["local_port"] or 0),
    }
    try:
        yield
    finally:
        settings.DATA_DIR = original_settings_data_dir
        settings.DB_PATH = original_settings_db_path
        pools.lookup_ip_geo = original_lookup
        pools._remove_mihomo_pool_config = original_remove
        pools._sync_mihomo_pool_config = original_sync
        sqlite3.connect = original_sqlite_connect
        for conn in connections:
            conn.close()
        temporary.cleanup()
        assert not data_dir.exists()


def create_legacy_proxy_db(path: Path, source_type: str = "vless") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE proxy_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'vless',
                source_uri TEXT NOT NULL DEFAULT '',
                expected_exit_ip TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                notes TEXT NOT NULL DEFAULT '',
                parse_status TEXT NOT NULL DEFAULT 'manual',
                parse_error TEXT NOT NULL DEFAULT '',
                mihomo_name TEXT NOT NULL DEFAULT '',
                parsed_json TEXT NOT NULL DEFAULT '{}',
                mihomo_proxy_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO proxy_profiles
               (name, source_type, expected_exit_ip, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("legacy", source_type, "203.0.113.60", proxy_pool.now_iso(), proxy_pool.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def copy_sqlite(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def sqlite_snapshot(path: Path) -> tuple[str, ...]:
    conn = sqlite3.connect(path)
    try:
        return tuple(conn.iterdump())
    finally:
        conn.close()


def test_legacy_schema_upgrade_is_idempotent_and_readable() -> None:
    with isolated_proxy_db():
        legacy_path = settings.DATA_DIR / "legacy.sqlite"
        create_legacy_proxy_db(legacy_path)
        legacy_snapshot = sqlite_snapshot(legacy_path)
        copy_sqlite(legacy_path, settings.DB_PATH)
        assert sqlite_snapshot(legacy_path) == legacy_snapshot

        with patch.object(settings, "now_iso", return_value="2026-09-08T00:00:00Z"):
            conn = sqlite3.connect(settings.DB_PATH)
            try:
                conn.row_factory = sqlite3.Row
                repository.init_db(conn)
                conn.close()
                first_snapshot = sqlite_snapshot(settings.DB_PATH)

                conn = sqlite3.connect(settings.DB_PATH)
                conn.row_factory = sqlite3.Row
                repository.init_db(conn)
                conn.close()
                assert sqlite_snapshot(settings.DB_PATH) == first_snapshot

                upgraded = proxy_pool.get_pool(1)
                assert upgraded["name"] == "legacy"
                assert sqlite_snapshot(settings.DB_PATH) == first_snapshot
            finally:
                if conn:
                    conn.close()

        conn = sqlite3.connect(settings.DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT name, local_port, port_scope, dialer_proxy FROM proxy_profiles WHERE name = 'legacy'"
            ).fetchone()
            columns = {item[1] for item in conn.execute("PRAGMA table_info(proxy_profiles)")}
        finally:
            conn.close()
        assert row is not None
        assert row["local_port"] == settings.PROXY_PORT_START
        assert row["port_scope"] == proxy_pool.PORT_SCOPE_DEFAULT
        assert row["dialer_proxy"] == ""
        assert {"local_port", "port_scope", "dialer_proxy", "deleted_at"} <= columns


def test_migration_dml_failure_rolls_back_after_existing_schema() -> None:
    class FailingMigrationConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
            if "UPDATE proxy_profiles SET status = ?" in " ".join(sql.split()):
                raise sqlite3.OperationalError("injected migration DML failure")
            return super().execute(sql, parameters)

    with isolated_proxy_db():
        legacy_path = settings.DATA_DIR / "legacy-dml.sqlite"
        create_legacy_proxy_db(legacy_path, source_type="static")
        copy_sqlite(legacy_path, settings.DB_PATH)

        # Early schema DDL commits before this fault. This freezes that
        # existing non-atomic boundary while requiring later port/dialer
        # backfill DML to roll back.
        conn = sqlite3.connect(settings.DB_PATH, factory=FailingMigrationConnection)
        conn.row_factory = sqlite3.Row
        try:
            try:
                repository.init_db(conn)
            except sqlite3.OperationalError as exc:
                assert str(exc) == "injected migration DML failure"
            else:
                raise AssertionError("migration DML failure was not injected")
        finally:
            conn.close()

        conn = sqlite3.connect(settings.DB_PATH)
        try:
            columns = {item[1] for item in conn.execute("PRAGMA table_info(proxy_profiles)")}
            local_port, dialer_proxy = conn.execute(
                "SELECT local_port, dialer_proxy FROM proxy_profiles WHERE name = 'legacy'"
            ).fetchone()
        finally:
            conn.close()
        assert {"local_port", "port_scope", "dialer_proxy"} <= columns
        assert local_port == 0
        assert dialer_proxy == ""


def test_migration_commit_failure_rolls_back_migration_dml() -> None:
    class FailingCommitConnection(sqlite3.Connection):
        def commit(self) -> None:
            raise sqlite3.OperationalError("injected migration commit failure")

    with isolated_proxy_db():
        legacy_path = settings.DATA_DIR / "legacy-commit.sqlite"
        create_legacy_proxy_db(legacy_path, source_type="static")
        copy_sqlite(legacy_path, settings.DB_PATH)

        conn = sqlite3.connect(settings.DB_PATH, factory=FailingCommitConnection)
        conn.row_factory = sqlite3.Row
        try:
            try:
                repository.init_db(conn)
            except sqlite3.OperationalError as exc:
                assert str(exc) == "injected migration commit failure"
            else:
                raise AssertionError("migration commit failure was not injected")
        finally:
            conn.close()

        conn = sqlite3.connect(settings.DB_PATH)
        try:
            local_port, dialer_proxy = conn.execute(
                "SELECT local_port, dialer_proxy FROM proxy_profiles WHERE name = 'legacy'"
            ).fetchone()
        finally:
            conn.close()
        assert local_port == 0
        assert dialer_proxy == ""


def test_proxy_uri_parsers_preserve_node_and_mihomo_fields() -> None:
    vless = proxy_pool.parse_vless_uri(
        " vless://123e4567-e89b-12d3-a456-426614174000@node.example:8443?type=ws&security=reality&pbk=public-key&sid=abcd&sni=edge.example&fp=safari&flow=xtls-rprx-vision&path=%2Fsocket&host=cdn.example#Reality%20WS "
    )
    assert set(vless) == {"parse_status", "mihomo_name", "parsed", "mihomo_proxy"}
    assert vless == {
        "parse_status": "ok",
        "mihomo_name": "Reality WS",
        "parsed": {
            "uuid": "123e4567-e89b-12d3-a456-426614174000",
            "server": "node.example",
            "port": 8443,
            "network": "ws",
            "security": "reality",
            "query": {
                "type": "ws", "security": "reality", "pbk": "public-key", "sid": "abcd",
                "sni": "edge.example", "fp": "safari", "flow": "xtls-rprx-vision",
                "path": "/socket", "host": "cdn.example",
            },
            "name": "Reality WS",
        },
        "mihomo_proxy": {
            "name": "Reality WS", "type": "vless", "server": "node.example", "port": 8443,
            "uuid": "123e4567-e89b-12d3-a456-426614174000", "network": "ws", "udp": True,
            "flow": "xtls-rprx-vision", "tls": True,
            "reality-opts": {"public-key": "public-key", "short-id": "abcd"},
            "servername": "edge.example", "client-fingerprint": "safari",
            "ws-opts": {"path": "/socket", "headers": {"Host": "cdn.example"}},
        },
    }

    vmess_json = base64.urlsafe_b64encode(json.dumps({
        "ps": "VMess JSON", "add": "vmess.example", "port": "443",
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "aid": "0", "scy": "auto",
        "net": "ws", "tls": "tls", "host": "cdn.example", "path": "/ws",
        "sni": "edge.example", "fp": "chrome",
    }, separators=(",", ":")).encode()).decode()
    assert proxy_pool.parse_vmess_uri(f"vmess://{vmess_json}") == {
        "parse_status": "ok", "mihomo_name": "VMess JSON",
        "parsed": {
            "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "server": "vmess.example",
            "port": 443, "alter_id": 0, "cipher": "auto", "network": "ws",
            "security": "tls", "query": {}, "name": "VMess JSON",
        },
        "mihomo_proxy": {
            "name": "VMess JSON", "type": "vmess", "server": "vmess.example", "port": 443,
            "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "alterId": 0, "cipher": "auto",
            "network": "ws", "udp": True, "tls": True, "servername": "edge.example",
            "client-fingerprint": "chrome", "ws-opts": {"path": "/ws", "headers": {"Host": "cdn.example"}},
        },
    }

    authority = base64.urlsafe_b64encode(
        b"aes-128-gcm:11111111-2222-3333-4444-555555555555@authority.example:8443"
    ).decode()
    assert proxy_pool.parse_vmess_uri(
        f"vmess://{authority}?remarks=Authority%20VMess&type=grpc&security=tls&serviceName=grpc-service&udp=0&peer=edge.example&fp=firefox&alterId=4"
    ) == {
        "parse_status": "ok", "mihomo_name": "Authority VMess",
        "parsed": {
            "uuid": "11111111-2222-3333-4444-555555555555", "server": "authority.example",
            "port": 8443, "alter_id": 4, "cipher": "aes-128-gcm", "network": "grpc",
            "security": "tls",
            "query": {"remarks": "Authority VMess", "type": "grpc", "security": "tls", "serviceName": "grpc-service", "udp": "0", "peer": "edge.example", "fp": "firefox", "alterId": "4"},
            "name": "Authority VMess",
        },
        "mihomo_proxy": {
            "name": "Authority VMess", "type": "vmess", "server": "authority.example", "port": 8443,
            "uuid": "11111111-2222-3333-4444-555555555555", "alterId": 4,
            "cipher": "aes-128-gcm", "network": "grpc", "udp": False, "tls": True,
            "servername": "edge.example", "client-fingerprint": "firefox",
            "grpc-opts": {"grpc-service-name": "grpc-service"},
        },
    }

    encoded_socks = base64.urlsafe_b64encode(b"user%20name:p%40ss@[2001:db8::1]:1080").decode()
    assert proxy_pool.parse_static_proxy_uri(f"socks://{encoded_socks}", "Encoded SOCKS") == {
        "parse_status": "ok", "mihomo_name": "Encoded SOCKS",
        "parsed": {"scheme": "socks5", "server": "2001:db8::1", "port": 1080, "username": "user name", "has_password": True, "name": "Encoded SOCKS"},
        "mihomo_proxy": {"name": "Encoded SOCKS", "type": "socks5", "server": "2001:db8::1", "port": 1080, "username": "user name", "password": "p@ss", "udp": True},
    }
    assert proxy_pool.parse_static_proxy_uri("  https://user%20name:p%40ss@[2001:db8::2]:443#HTTPS%20Node\n") == {
        "parse_status": "ok", "mihomo_name": "HTTPS Node",
        "parsed": {"scheme": "https", "server": "2001:db8::2", "port": 443, "username": "user name", "has_password": True, "name": "HTTPS Node"},
        "mihomo_proxy": {"name": "HTTPS Node", "type": "http", "server": "2001:db8::2", "port": 443, "username": "user name", "password": "p@ss", "tls": True},
    }

    for parser in (proxy_pool.parse_vless_uri, proxy_pool.parse_vmess_uri, proxy_pool.parse_static_proxy_uri):
        assert parser("", "fallback") == {
            "parse_status": "manual", "parsed": {}, "mihomo_proxy": {}, "mihomo_name": "fallback",
        }


def test_proxy_uri_parser_errors_are_explicit() -> None:
    for parser, uri, expected in (
        (proxy_pool.parse_vless_uri, "https://node.example:443", "Only vless:// URI is supported"),
        (proxy_pool.parse_vmess_uri, "vmess://a", "VMess URI payload is not valid Base64"),
        (proxy_pool.parse_static_proxy_uri, "ftp://node.example:21", "静态代理仅支持 socks://、socks5://、http:// 或 https://"),
    ):
        try:
            parser(uri)
        except ValueError as exc:
            assert str(exc) == expected
        else:
            raise AssertionError(f"expected ValueError for {uri}")


def test_upsert_pool_persists_static_node_and_rejects_invalid_vless_before_insert() -> None:
    with isolated_proxy_db():
        saved = proxy_pool.upsert_pool({
            "name": "stored static",
            "source_uri": "https://user%20name:p%40ss@proxy.example:8443#Stored%20Static",
            "status": proxy_pool.STATUS_ACTIVE,
        })["pool"]
        assert saved["source_type"] == "static"
        assert saved["parse_status"] == "ok"
        assert saved["dialer_proxy"] == proxy_pool.SYSTEM_PROXY_DIALER
        assert saved["parsed"] == {
            "scheme": "https", "server": "proxy.example", "port": 8443,
            "username": "user name", "has_password": True, "name": "Stored Static",
        }
        assert saved["mihomo_proxy"] == {
            "name": "Stored Static", "type": "http", "server": "proxy.example", "port": 8443,
            "username": "user name", "password": "p@ss", "tls": True,
            "dialer-proxy": proxy_pool.SYSTEM_PROXY_DIALER,
        }

        try:
            proxy_pool.upsert_pool({"name": "invalid vless", "source_uri": "vless://uuid@node.example"})
        except ValueError as exc:
            assert str(exc) == "代理 URI 解析失败：VLESS URI must include uuid, server and port"
        else:
            raise AssertionError("invalid VLESS URI was accepted")

        with proxy_pool.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM proxy_profiles WHERE name = 'invalid vless'").fetchone()[0] == 0


def create_manual_pool(name: str, expected_ip: str) -> dict:
    return proxy_pool.upsert_pool(
        {
            "name": name,
            "source_type": "vless",
            "expected_exit_ip": expected_ip,
            "status": proxy_pool.STATUS_ACTIVE,
        }
    )["pool"]


def seed_bound_delete_target(pool: dict, suffix: str) -> tuple[int, str, str]:
    now = proxy_pool.now_iso()
    publish_id = f"publish-delete-{suffix}"
    collect_id = f"collect-delete-{suffix}"
    with proxy_pool.connect() as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO tiktok_accounts (
                       username, proxy_profile_id, proxy_bound, status, profile_json,
                       created_at, updated_at
                   ) VALUES (?, ?, 1, ?, ?, ?, ?)""",
                (
                    f"delete_target_{suffix}",
                    pool["id"],
                    proxy_pool.ACCOUNT_STATUS_ACTIVE,
                    json.dumps({"proxy_binding": {"proxy_profile_id": pool["id"]}}),
                    now,
                    now,
                ),
            ).lastrowid
        )
        asset_id = f"asset-delete-{suffix}"
        conn.execute(
            """INSERT INTO publish_assets (id, account_id, original_name, stored_path, created_at)
               VALUES (?, ?, 'video.mp4', 'data/video.mp4', ?)""",
            (asset_id, account_id, now),
        )
        conn.execute(
            """INSERT INTO publish_jobs (
                   id, account_id, proxy_profile_id, asset_id, scheduled_at,
                   status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (publish_id, account_id, pool["id"], asset_id, now, now, now),
        )
        conn.execute(
            """INSERT INTO collect_jobs (
                   id, account_id, proxy_profile_id, status, created_at, updated_at
               ) VALUES (?, ?, ?, 'delayed', ?, ?)""",
            (collect_id, account_id, pool["id"], now, now),
        )
        conn.commit()
    return account_id, publish_id, collect_id


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


def test_delete_bound_pool_unbinds_account_until_explicit_rebind() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("removed-static", "203.0.113.25")
        now = proxy_pool.now_iso()
        with proxy_pool.connect() as conn:
            account_id = int(
                conn.execute(
                    """INSERT INTO tiktok_accounts (
                           username, proxy_profile_id, proxy_bound, status, profile_json,
                           last_checked_ip, last_check_status, created_at, updated_at
                       ) VALUES (?, ?, 1, ?, ?, ?, '通过', ?, ?)""",
                    (
                        "needs_rebind",
                        pool["id"],
                        proxy_pool.ACCOUNT_STATUS_ACTIVE,
                        json.dumps({
                            "proxy_binding": {"proxy_profile_id": pool["id"]},
                            "browser_settings": {"proxy_server": f"127.0.0.1:{pool['local_port']}", "locale": "en-US"},
                        }),
                        "203.0.113.25",
                        now,
                        now,
                    ),
                ).lastrowid
            )
            conn.execute(
                """INSERT INTO publish_assets (id, account_id, original_name, stored_path, created_at)
                   VALUES ('asset-rebind', ?, 'video.mp4', 'data/video.mp4', ?)""",
                (account_id, now),
            )
            conn.execute(
                """INSERT INTO publish_jobs (
                       id, account_id, proxy_profile_id, asset_id, scheduled_at,
                       status, created_at, updated_at
                   ) VALUES ('publish-rebind', ?, ?, 'asset-rebind', ?, 'queued', ?, ?)""",
                (account_id, pool["id"], now, now, now),
            )
            conn.execute(
                """INSERT INTO collect_jobs (
                       id, account_id, proxy_profile_id, status, created_at, updated_at
                   ) VALUES ('collect-rebind', ?, ?, 'delayed', ?, ?)""",
                (account_id, pool["id"], now, now),
            )
            conn.commit()

        deleted = proxy_pool.delete_pool(pool["id"])
        account = next(item for item in deleted["accounts"] if item["id"] == account_id)
        assert account["proxy_bound"] is False
        assert account["last_check_status"] == "未绑定"
        assert account["last_checked_ip"] == ""
        assert "proxy_binding" not in account["profile"]
        assert "proxy_server" not in account["profile"]["browser_settings"]
        assert deleted["unbound_accounts"] == 1
        assert deleted["delayed_jobs"] == {"publish": 1, "collect": 1}

        with proxy_pool.connect() as conn:
            publish = conn.execute("SELECT * FROM publish_jobs WHERE id = 'publish-rebind'").fetchone()
            collect = conn.execute("SELECT * FROM collect_jobs WHERE id = 'collect-rebind'").fetchone()
            assert (publish["status"], publish["stage"]) == ("delayed", "waiting_proxy")
            assert (collect["status"], collect["stage"]) == ("delayed", "waiting_proxy")

        rebound = proxy_pool.update_account_proxy_binding({
            "action": "bind",
            "account_id": account_id,
            "proxy_profile_id": "direct",
        })
        assert rebound["account"]["proxy_bound"] is True
        direct_pool = next(item for item in rebound["pools"] if item["id"] == rebound["account"]["proxy_profile_id"])
        assert direct_pool["source_type"] == "direct"
        assert rebound["resumed"] == {"publish_jobs": 1, "collect_jobs": 1}
        with proxy_pool.connect() as conn:
            publish = conn.execute("SELECT * FROM publish_jobs WHERE id = 'publish-rebind'").fetchone()
            collect = conn.execute("SELECT * FROM collect_jobs WHERE id = 'collect-rebind'").fetchone()
            assert publish["proxy_profile_id"] == direct_pool["id"]
            assert (publish["status"], publish["stage"]) == ("queued", "proxy_rebound")
            assert collect["proxy_profile_id"] == direct_pool["id"]
            assert (collect["status"], collect["stage"]) == ("queued", "proxy_rebound")


def test_delete_pool_commit_failure_rolls_back_db_and_restores_mihomo_backup() -> None:
    class FailingDeleteCommitConnection(sqlite3.Connection):
        fail_delete_commit = False

        def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if " ".join(sql.split()).upper() == "BEGIN IMMEDIATE":
                self.fail_delete_commit = True
            return cursor

        def commit(self) -> None:
            if self.fail_delete_commit:
                raise sqlite3.OperationalError("injected delete commit failure")
            super().commit()

    @contextmanager
    def failing_delete_connect() -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(settings.DB_PATH, factory=FailingDeleteCommitConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    with isolated_proxy_db():
        pool = create_manual_pool("commit-failure", "203.0.113.26")
        account_id, publish_id, collect_id = seed_bound_delete_target(pool, "commit")
        backup = (Path("ignored-mihomo.yaml"), b"before-delete", 0o600)
        with ExitStack() as stack:
            stack.enter_context(patch.object(pools, "connect", failing_delete_connect))
            stack.enter_context(patch.object(pools, "_remove_mihomo_pool_config", return_value=({"removed": True}, backup)))
            restore = stack.enter_context(patch.object(pools, "_restore_mihomo_listener_config"))
            try:
                proxy_pool.delete_pool(pool["id"])
            except sqlite3.OperationalError as exc:
                assert str(exc) == "injected delete commit failure"
            else:
                raise AssertionError("delete commit failure was not injected")

        assert restore.call_args_list == [((backup[0], backup[1], backup[2]), {})]
        with proxy_pool.connect() as conn:
            pool_row = conn.execute("SELECT deleted_at FROM proxy_profiles WHERE id = ?", (pool["id"],)).fetchone()
            account = conn.execute("SELECT proxy_bound FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone()
            publish = conn.execute("SELECT status, stage FROM publish_jobs WHERE id = ?", (publish_id,)).fetchone()
            collect = conn.execute("SELECT status, stage FROM collect_jobs WHERE id = ?", (collect_id,)).fetchone()
        assert pool_row["deleted_at"] == ""
        assert account["proxy_bound"] == 1
        assert (publish["status"], publish["stage"]) == ("queued", "")
        assert (collect["status"], collect["stage"]) == ("delayed", "")


def test_delete_pool_mihomo_cleanup_failure_leaves_db_unchanged() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("cleanup-failure", "203.0.113.27")
        account_id, publish_id, collect_id = seed_bound_delete_target(pool, "cleanup")
        with ExitStack() as stack:
            stack.enter_context(patch.object(pools, "_remove_mihomo_pool_config", side_effect=ValueError("injected mihomo cleanup failure")))
            restore = stack.enter_context(patch.object(pools, "_restore_mihomo_listener_config"))
            try:
                proxy_pool.delete_pool(pool["id"])
            except ValueError as exc:
                assert str(exc) == "injected mihomo cleanup failure"
            else:
                raise AssertionError("mihomo cleanup failure was not injected")

        assert restore.call_count == 0
        with proxy_pool.connect() as conn:
            pool_row = conn.execute("SELECT deleted_at FROM proxy_profiles WHERE id = ?", (pool["id"],)).fetchone()
            account = conn.execute("SELECT proxy_bound FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone()
            publish = conn.execute("SELECT status, stage FROM publish_jobs WHERE id = ?", (publish_id,)).fetchone()
            collect = conn.execute("SELECT status, stage FROM collect_jobs WHERE id = ?", (collect_id,)).fetchone()
        assert pool_row["deleted_at"] == ""
        assert account["proxy_bound"] == 1
        assert (publish["status"], publish["stage"]) == ("queued", "")
        assert (collect["status"], collect["stage"]) == ("delayed", "")


def test_delete_sing_box_restart_failure_keeps_committed_delete_unretryable() -> None:
    with isolated_proxy_db(), patch.dict(os.environ, {"PROXY_REALITY_CORE": "mihomo"}, clear=False):
        pool = proxy_pool.upsert_pool(
            {
                "name": "sing-box-delete-failure",
                "source_uri": "vless://123e4567-e89b-12d3-a456-426614174000@node.example:443?security=reality&pbk=key&sid=id&sni=edge.example#Reality",
                "status": proxy_pool.STATUS_ACTIVE,
            }
        )["pool"]
        account_id, publish_id, collect_id = seed_bound_delete_target(pool, "singbox")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PROXY_REALITY_CORE": "sing-box"}, clear=False))
            core = stack.enter_context(patch.object(pools, "ensure_proxy_cores", side_effect=ValueError("injected sing-box restart failure")))
            try:
                proxy_pool.delete_pool(pool["id"])
            except ValueError as exc:
                assert str(exc) == "injected sing-box restart failure"
            else:
                raise AssertionError("sing-box restart failure was not injected")
        assert core.call_args_list == [((), {"restart": True, "required": True})]

        with proxy_pool.connect() as conn:
            pool_row = conn.execute("SELECT deleted_at FROM proxy_profiles WHERE id = ?", (pool["id"],)).fetchone()
            account = conn.execute("SELECT proxy_bound FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone()
            publish = conn.execute("SELECT status, stage FROM publish_jobs WHERE id = ?", (publish_id,)).fetchone()
            collect = conn.execute("SELECT status, stage FROM collect_jobs WHERE id = ?", (collect_id,)).fetchone()
        assert pool_row["deleted_at"]
        assert account["proxy_bound"] == 0
        assert (publish["status"], publish["stage"]) == ("delayed", "waiting_proxy")
        assert (collect["status"], collect["stage"]) == ("delayed", "waiting_proxy")

        try:
            proxy_pool.delete_pool(pool["id"])
        except ValueError as exc:
            assert str(exc) == "proxy profile not found"
        else:
            raise AssertionError("committed sing-box delete unexpectedly retried")


def test_direct_pool_recovers_after_successful_recheck() -> None:
    with isolated_proxy_db():
        pool = proxy_pool.upsert_pool({
            "name": "server-global",
            "source_type": "direct",
            "status": proxy_pool.STATUS_ACTIVE,
        })["pool"]
        with proxy_pool.connect() as conn:
            conn.execute(
                "UPDATE proxy_profiles SET status = ?, parse_error = ? WHERE id = ?",
                (proxy_pool.STATUS_ERROR, "transient failure", pool["id"]),
            )
            conn.commit()

        with patch.object(runtime, "detect_exit_ip_for_pool", return_value={"ip": "203.0.113.50"}) as detect:
            checked = proxy_pool.check_binding({"proxy_profile_id": pool["id"]})
        assert detect.call_count == 1
        assert checked["allowed"] is True
        assert checked["pool"]["status"] == proxy_pool.STATUS_ACTIVE
        assert checked["pool"]["parse_error"] == ""


def test_login_session_start_failure_marks_persisted_session_failed() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("session-start", "203.0.113.61")
        with ExitStack() as stack:
            stack.enter_context(patch.object(accounts, "check_binding", return_value={"allowed": True}))
            stack.enter_context(patch.object(state, "_slot_ports_available", return_value=True))
            launch = stack.enter_context(patch.object(accounts, "_launch_observation_channel", side_effect=RuntimeError("channel failed")))
            terminate = stack.enter_context(patch.object(accounts, "_terminate_session_processes"))
            remove_profile = stack.enter_context(patch.object(accounts, "_remove_unbound_session_profile"))
            try:
                proxy_pool.start_login_session({
                    "proxy_profile_id": pool["id"], "username": "pending-user", "feishu_user_id": "user-1",
                })
            except ValueError as exc:
                assert str(exc) == "观测浏览器启动失败：channel failed"
            else:
                raise AssertionError("failed channel start was accepted")
        assert launch.call_count == 1 and terminate.call_count == 1 and remove_profile.call_count == 1
        with proxy_pool.connect() as conn:
            row = conn.execute("SELECT status, last_error FROM browser_sessions").fetchone()
        assert (row["status"], row["last_error"]) == ("failed", "channel failed")


def test_stop_and_expired_session_cleanup_release_process_state() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("session-stop", "203.0.113.62")
        now = proxy_pool.now_iso()
        with proxy_pool.connect() as conn:
            stopped_id = int(conn.execute(
                """INSERT INTO browser_sessions
                   (slot, proxy_profile_id, username, status, runtime_id, pid, xvfb_pid, x11vnc_pid,
                    websockify_pid, created_at, updated_at)
                   VALUES (1, ?, 'stopped-user', 'observing', ?, 11, 12, 13, 14, ?, ?)""",
                (pool["id"], proxy_pool.RUNTIME_ID, now, now),
            ).lastrowid)
            expired_id = int(conn.execute(
                """INSERT INTO browser_sessions
                   (slot, proxy_profile_id, username, status, runtime_id, created_at, updated_at)
                   VALUES (2, ?, 'expired-user', 'running', ?, '1970-01-01T00:00:00Z', ?)""",
                (pool["id"], proxy_pool.RUNTIME_ID, now),
            ).lastrowid)
            conn.commit()
        with ExitStack() as stack:
            terminate = stack.enter_context(patch.object(accounts, "_terminate_session_processes"))
            remove_profile = stack.enter_context(patch.object(accounts, "_remove_unbound_session_profile"))
            stack.enter_context(patch.object(state, "_terminate_session_processes", terminate))
            stack.enter_context(patch.object(state, "_remove_unbound_session_profile", remove_profile))
            proxy_pool.stop_login_session({"session_id": stopped_id, "reason": "operator stop"})
            with proxy_pool.connect() as conn:
                assert proxy_pool._active_sessions(conn) == []
        assert terminate.call_count == 2 and remove_profile.call_count == 2
        assert terminate.call_args_list[0].args[0]["pid"] == 11
        with proxy_pool.connect() as conn:
            rows = {
                row["id"]: (row["status"], row["last_error"])
                for row in conn.execute("SELECT id, status, last_error FROM browser_sessions")
            }
        assert rows[stopped_id] == ("stopped", "operator stop")
        assert rows[expired_id][0] == "stopped"
        assert rows[expired_id][1] == "未完成账号登记，临时登录通道超时自动释放"


def test_v2_proxy_page_exposes_account_rebind_flow() -> None:
    source = (ROOT / "scripts" / "static" / "proxy.html").read_text(encoding="utf-8")
    assert "/api/proxy/accounts/proxy-binding" in source
    assert "data-live-binding-submit" in source
    assert "data-live-binding-unbind" in source
    assert "target.dataset.open==='binding'&&window.proxyRealWorkflow" in source
    assert "重新选择后，因原代理删除而暂停的等待任务会自动恢复排队" in source


def test_v2_sing_box_default_stays_in_4004_project() -> None:
    source = Path(settings.__file__).read_text(encoding="utf-8")
    assert 'os.getenv("SING_BOX_COMPOSE_PROJECT", "short-video-analyzer-ui-4004")' in source
    assert 'os.getenv("PROXY_POOL_CONFIG_NAMESPACE", "v2")' in source
    assert 'os.getenv("PROXY_POOL_MIHOMO_PREFIX", "v2-")' in source
    assert 'os.getenv("PROXY_POOL_PORT_START", "19300")' in source
    assert 'os.getenv("PROXY_POOL_PORT_END", "19399")' in source

    compose_path = ROOT / "docker-compose.yml"
    if compose_path.is_file():
        compose = compose_path.read_text(encoding="utf-8")
        assert compose.count("PROXY_POOL_CONFIG_NAMESPACE: ${PROXY_POOL_CONFIG_NAMESPACE:-v2}") == 2
        assert compose.count("PROXY_POOL_MIHOMO_PREFIX: ${PROXY_POOL_MIHOMO_PREFIX:-v2-}") == 2
        assert compose.count("PROXY_POOL_PORT_START: ${PROXY_POOL_PORT_START:-19300}") == 2
        assert "WEB_PORT: ${WEB_PORT:-4004}" in compose

    deploy = (ROOT / "scripts" / "deploy_ui_4004.sh").read_text(encoding="utf-8")
    assert 'legacy_preview="${UI4004_LEGACY_PREVIEW:-0}"' in deploy
    assert 'if [[ "$current_branch" != "v2" ]]; then' in deploy
    assert "UI4004_BRANCH" not in deploy
    assert "ALLOW_NON_UI4004_BRANCH" not in deploy
    assert 'env_file="${UI4004_ENV_FILE:-.env}"' in deploy
    assert 'expected_image="short-video-analyzer-ui-4004:latest"' in deploy
    assert 'image_name="${ANALYZER_IMAGE:-$expected_image}"' in deploy
    assert 'if [[ "$image_name" != "$expected_image" ]]; then' in deploy
    assert 'compose_args=(-p "$project_name" --env-file "$env_file" -f docker-compose.yml)' in deploy
    assert 'if [[ "$legacy_preview" != "0" ]]; then' in deploy
    assert 'if [[ "$project_name" != "short-video-analyzer-ui-4004" ]]; then' in deploy
    assert 'if [[ "$web_port" != "4004" ]]; then' in deploy
    assert 'ANALYZER_IMAGE="$image_name" WEB_PORT="$web_port"' in deploy
    assert "docker-compose.ui-4004.yml" not in deploy
    first_docker_probe = deploy.index("if command -v docker")
    assert deploy.index('if [[ "$legacy_preview" != "0" ]]; then') < first_docker_probe
    assert deploy.index('if [[ "$current_branch" != "v2" ]]; then') < first_docker_probe
    assert deploy.index('if [[ "$project_name" != "short-video-analyzer-ui-4004" ]]; then') < first_docker_probe
    assert deploy.index('if [[ "$web_port" != "4004" ]]; then') < first_docker_probe
    assert deploy.index('if [[ "$image_name" != "$expected_image" ]]; then') < first_docker_probe


def test_port_migration_updates_bound_account_profile() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("migrated", "203.0.113.50")
        now = proxy_pool.now_iso()
        with proxy_pool.connect() as conn:
            conn.execute("UPDATE proxy_profiles SET local_port = 18900 WHERE id = ?", (pool["id"],))
            account_id = int(
                conn.execute(
                    """INSERT INTO tiktok_accounts
                       (username, proxy_profile_id, proxy_bound, status, profile_json, created_at, updated_at)
                       VALUES (?, ?, 1, ?, ?, ?, ?)""",
                    (
                        "migrated_account",
                        pool["id"],
                        proxy_pool.ACCOUNT_STATUS_ACTIVE,
                        json.dumps(
                            {
                                "proxy_binding": {"local_port": 18900},
                                "browser_settings": {"proxy_server": "127.0.0.1:18900"},
                                "preserved": True,
                            }
                        ),
                        now,
                        now,
                    ),
                ).lastrowid
            )
            conn.commit()

        with proxy_pool.connect() as conn:
            migrated = conn.execute(
                """SELECT a.profile_json, p.local_port
                   FROM tiktok_accounts a
                   JOIN proxy_profiles p ON p.id = a.proxy_profile_id
                   WHERE a.id = ?""",
                (account_id,),
            ).fetchone()

        profile = json.loads(migrated["profile_json"])
        assert migrated["local_port"] == settings.PROXY_PORT_START
        assert profile["proxy_binding"]["local_port"] == settings.PROXY_PORT_START
        assert profile["browser_settings"]["proxy_server"] == f"127.0.0.1:{settings.PROXY_PORT_START}"
        assert profile["preserved"] is True


def test_sing_box_detection_accepts_serialized_pool_dict() -> None:
    previous = os.environ.get("PROXY_REALITY_CORE")
    os.environ["PROXY_REALITY_CORE"] = "sing-box"
    try:
        assert runtime._sing_box_reality_pool(
            {
                "source_type": "vless",
                "parsed": {},
                "local_port": settings.PROXY_PORT_START,
            }
        ) is False
    finally:
        if previous is None:
            os.environ.pop("PROXY_REALITY_CORE", None)
        else:
            os.environ["PROXY_REALITY_CORE"] = previous


def test_mihomo_managed_yaml_is_namespaced_and_restored_after_failed_reload() -> None:
    module = sys.modules[runtime._sync_mihomo_pool_config.__module__]
    original = (
        b"proxies:\n"
        b"  - name: foreign\n"
        b"    type: ss\n"
        b"listeners:\n"
        b"  - name: foreign-listener\n"
        b"    type: mixed\n"
        b"    port: 19301\n"
        b"    proxy: foreign\n"
    )
    pool = {
        "id": 8, "name": "managed", "mihomo_name": "managed", "source_type": "vless",
        "local_port": 19302, "mihomo_proxy": {"type": "vless", "server": "node.example", "port": 443},
    }
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "config.yaml"
        config.write_bytes(original)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"MIHOMO_CONFIG_PATH": str(config)}, clear=False))
            stack.enter_context(patch.object(module, "_mihomo_listener_matches", return_value=True))
            stack.enter_context(patch.object(module, "_reload_mihomo_config"))
            stack.enter_context(patch.object(module, "_mihomo_request", return_value=(True, {"proxies": {f"{settings.PROXY_MIHOMO_NAME_PREFIX}managed": {}, f"{settings.PROXY_MIHOMO_NAME_PREFIX}replacement": {}, settings.SYSTEM_PROXY_DIALER: {}}}, "")))
            stack.enter_context(patch.object(module, "_port_open", return_value=True))
            runtime._sync_mihomo_pool_config(pool)
            pool["mihomo_name"] = "replacement"
            runtime._sync_mihomo_pool_config(pool)
            direct = {**pool, "source_type": "direct", "mihomo_proxy": {}}
            runtime._sync_mihomo_pool_config(direct)
        updated = config.read_text(encoding="utf-8")
        assert "foreign-listener" in updated and "proxy: foreign" in updated
        assert "proxy-pool-managed-v2-id: 8" in updated
        assert "v2-managed" not in updated
        assert "v2-replacement" not in updated

        config.write_bytes(original)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"MIHOMO_CONFIG_PATH": str(config)}, clear=False))
            stack.enter_context(patch.object(module, "_mihomo_listener_matches", return_value=True))
            stack.enter_context(patch.object(module, "_reload_mihomo_config", side_effect=ValueError("reload failed")))
            try:
                runtime._sync_mihomo_pool_config(pool)
            except runtime.ProxyConfigurationError as exc:
                assert str(exc) == "mihomo 自动同步失败：reload failed"
            else:
                raise AssertionError("reload failure was accepted")
        assert config.read_bytes() == original
        assert config.with_name("config.yaml.proxy-pool.bak").read_bytes() == original


def test_sing_box_export_and_restart_stay_local_to_4004() -> None:
    module = sys.modules[runtime._restart_sing_box_container.__module__]
    with isolated_proxy_db(), patch.dict(os.environ, {"PROXY_REALITY_CORE": "mihomo", "PROXY_REALITY_DEFAULT_FINGERPRINT": "safari"}), tempfile.TemporaryDirectory() as directory:
        pool = proxy_pool.upsert_pool({
            "name": "reality", "source_uri": "vless://123e4567-e89b-12d3-a456-426614174000@node.example:443?security=reality&pbk=key&sid=id&sni=edge.example#Reality",
            "status": proxy_pool.STATUS_ACTIVE,
        })["pool"]
        config = Path(directory) / "sing-box" / "config.json"
        success = module.subprocess.CompletedProcess([], 0, "container-4004\n", "")
        failed = module.subprocess.CompletedProcess([], 1, "", "blocked")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PROXY_REALITY_CORE": "sing-box"}, clear=False))
            stack.enter_context(patch.object(settings, "SING_BOX_CONFIG_PATH", config))
            run = stack.enter_context(patch.object(module.subprocess, "run", side_effect=[success, success]))
            exported = runtime._write_sing_box_config()
            restarted = runtime._restart_sing_box_container(required=True)
        assert exported["pools"] == [{"id": pool["id"], "name": "reality", "local_port": pool["local_port"], "fingerprint": "safari"}]
        assert json.loads(config.read_text(encoding="utf-8"))["inbounds"][0]["listen_port"] == pool["local_port"]
        assert restarted == {"restarted": True, "containers": ["container-4004"]}
        assert run.call_args_list[0].args[0] == ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=short-video-analyzer-ui-4004", "--filter", "label=com.docker.compose.service=sing-box"]
        assert run.call_args_list[1].args[0] == ["docker", "restart", "container-4004"]
        with patch.object(module.subprocess, "run", side_effect=[success, failed]):
            try:
                runtime._restart_sing_box_container(required=True)
            except ValueError as exc:
                assert str(exc) == "重启 sing-box 代理核心失败：blocked"
            else:
                raise AssertionError("sing-box restart failure was accepted")


def test_exit_ip_repair_is_attempted_once() -> None:
    module = sys.modules[runtime._detect_exit_ip_with_single_repair.__module__]
    pool = {"id": 8}
    with patch.object(module, "detect_exit_ip_for_pool", side_effect=[ValueError("first"), {"ip": "203.0.113.8"}]) as detect, patch.object(module, "_repair_proxy_core_once", return_value={"attempted": True, "core": "mihomo", "result": {}}) as repair:
        assert runtime._detect_exit_ip_with_single_repair(pool)["auto_repair"]["core"] == "mihomo"
        assert detect.call_count == 2 and repair.call_count == 1
    with patch.object(module, "detect_exit_ip_for_pool", side_effect=[ValueError("first"), ValueError("second")]), patch.object(module, "_repair_proxy_core_once", return_value={"attempted": True, "core": "mihomo", "result": {}}) as repair:
        try:
            runtime._detect_exit_ip_with_single_repair(pool)
        except runtime.ProxyConfigurationError as exc:
            assert str(exc) == "mihomo 已自动修复一次，但出口校验仍失败：second"
        else:
            raise AssertionError("second exit IP failure was accepted")
        assert repair.call_count == 1


def test_runtime_status_freezes_public_keys_and_dynamic_vnc_values() -> None:
    module = sys.modules[proxy_pool.runtime_status.__module__]
    expected_keys = {
        "checked_at", "novnc_url", "novnc_local_port", "novnc_ports", "vnc_port",
        "mihomo_proxy_port", "mihomo_api_url", "checks", "mihomo_version", "mihomo_error",
        "port_range", "pending_login_ttl_seconds", "manual_observation_idle_seconds",
        "browser_locale", "browser_notice",
    }
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "TIKTOK_BROWSER_MAX_SLOTS": "5", "TIKTOK_BROWSER_HIDDEN_AUTOMATION_SLOTS": "2",
            "VNC_PORT": "5999", "MIHOMO_PROXY_PORT": "7999",
            "TIKTOK_PENDING_LOGIN_TTL_SECONDS": "120", "TIKTOK_MANUAL_OBSERVATION_IDLE_SECONDS": "180",
        }, clear=False))
        stack.enter_context(patch.object(settings, "now_iso", return_value="2026-09-08T00:00:00Z"))
        stack.enter_context(patch.object(settings, "NOVNC_PORT", 6080))
        stack.enter_context(patch.object(settings, "NOVNC_MANUAL_PORTS", 1))
        request = stack.enter_context(patch.object(module, "_http_get_json", return_value=(True, {"version": "test"}, "")))
        stack.enter_context(patch.object(module, "_port_open", side_effect=lambda _host, port: port == 5999))
        status = proxy_pool.runtime_status()
    assert set(status) == expected_keys
    assert status["checked_at"] == "2026-09-08T00:00:00Z"
    assert status["vnc_port"] == 5999 and status["mihomo_proxy_port"] == 7999
    assert status["novnc_ports"] == {
        "base_port": 6081, "reserved_port": 6080, "manual_ports": 1, "max_slots": 5,
        "hidden_automation_slots": 2, "visible_observation_slots": 3, "total_ports": 4,
        "allowed_ports": [6081, 6082, 6083, 6084], "allowed_range": "6081-6084",
    }
    assert status["checks"] == {
        "novnc_local": False, "novnc_ports": {"6081": False, "6082": False, "6083": False, "6084": False},
        "vnc_local": True, "mihomo_proxy_local": False, "mihomo_api_local": True,
    }
    assert status["mihomo_version"] == "test" and status["mihomo_error"] == ""
    assert status["pending_login_ttl_seconds"] == 120 and status["manual_observation_idle_seconds"] == 180
    assert request.call_args.args == (settings.DEFAULT_MIHOMO_API.rstrip("/") + "/version",)


def test_account_state_exposes_instagram_login_without_cookie_value() -> None:
    with isolated_proxy_db():
        pool = create_manual_pool("instagram", "203.0.113.40")
        profile_dir = settings.DATA_DIR / "tiktok_browser_profiles" / "account" / "user-data"
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


def main() -> None:
    test_legacy_schema_upgrade_is_idempotent_and_readable()
    test_migration_dml_failure_rolls_back_after_existing_schema()
    test_migration_commit_failure_rolls_back_migration_dml()
    test_proxy_uri_parsers_preserve_node_and_mihomo_fields()
    test_proxy_uri_parser_errors_are_explicit()
    test_upsert_pool_persists_static_node_and_rejects_invalid_vless_before_insert()
    test_duplicate_exit_ip_is_terminal_until_manual_recheck()
    test_existing_duplicate_error_is_migrated_without_retry()
    test_delete_pool_preserves_archived_history_and_releases_port()
    test_delete_bound_pool_unbinds_account_until_explicit_rebind()
    test_delete_pool_commit_failure_rolls_back_db_and_restores_mihomo_backup()
    test_delete_pool_mihomo_cleanup_failure_leaves_db_unchanged()
    test_delete_sing_box_restart_failure_keeps_committed_delete_unretryable()
    test_direct_pool_recovers_after_successful_recheck()
    test_login_session_start_failure_marks_persisted_session_failed()
    test_stop_and_expired_session_cleanup_release_process_state()
    test_v2_proxy_page_exposes_account_rebind_flow()
    test_v2_sing_box_default_stays_in_4004_project()
    test_port_migration_updates_bound_account_profile()
    test_sing_box_detection_accepts_serialized_pool_dict()
    test_mihomo_managed_yaml_is_namespaced_and_restored_after_failed_reload()
    test_sing_box_export_and_restart_stay_local_to_4004()
    test_exit_ip_repair_is_attempted_once()
    test_runtime_status_freezes_public_keys_and_dynamic_vnc_values()
    test_account_state_exposes_instagram_login_without_cookie_value()
    print("proxy pool lifecycle tests passed")


if __name__ == "__main__":
    main()
