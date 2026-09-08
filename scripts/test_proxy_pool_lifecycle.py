#!/usr/bin/env python3
"""Focused regression tests for proxy IP uniqueness and safe proxy deletion."""
from __future__ import annotations

import base64
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
    original_sync = proxy_pool._sync_mihomo_pool_config
    original_sqlite_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = original_sqlite_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    data_dir = Path(temporary.name)
    sqlite3.connect = tracked_connect
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
    proxy_pool._sync_mihomo_pool_config = lambda pool: {
        "loaded": True,
        "listener_port": int(pool["local_port"] or 0),
    }
    try:
        yield
    finally:
        proxy_pool.DATA_DIR = original_data_dir
        proxy_pool.DB_PATH = original_db_path
        proxy_pool.lookup_ip_geo = original_lookup
        proxy_pool._remove_mihomo_pool_config = original_remove
        proxy_pool._sync_mihomo_pool_config = original_sync
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
        legacy_path = proxy_pool.DATA_DIR / "legacy.sqlite"
        create_legacy_proxy_db(legacy_path)
        legacy_snapshot = sqlite_snapshot(legacy_path)
        copy_sqlite(legacy_path, proxy_pool.DB_PATH)
        assert sqlite_snapshot(legacy_path) == legacy_snapshot

        original_now_iso = proxy_pool.now_iso
        proxy_pool.now_iso = lambda: "2026-09-08T00:00:00Z"
        conn = sqlite3.connect(proxy_pool.DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            proxy_pool.init_db(conn)
            conn.close()
            first_snapshot = sqlite_snapshot(proxy_pool.DB_PATH)

            conn = sqlite3.connect(proxy_pool.DB_PATH)
            conn.row_factory = sqlite3.Row
            proxy_pool.init_db(conn)
            conn.close()
            assert sqlite_snapshot(proxy_pool.DB_PATH) == first_snapshot

            upgraded = proxy_pool.get_pool(1)
            assert upgraded["name"] == "legacy"
            assert sqlite_snapshot(proxy_pool.DB_PATH) == first_snapshot
        finally:
            if conn:
                conn.close()
            proxy_pool.now_iso = original_now_iso

        conn = sqlite3.connect(proxy_pool.DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT name, local_port, port_scope, dialer_proxy FROM proxy_profiles WHERE name = 'legacy'"
            ).fetchone()
            columns = {item[1] for item in conn.execute("PRAGMA table_info(proxy_profiles)")}
        finally:
            conn.close()
        assert row is not None
        assert row["local_port"] == proxy_pool.PROXY_PORT_START
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
        legacy_path = proxy_pool.DATA_DIR / "legacy-dml.sqlite"
        create_legacy_proxy_db(legacy_path, source_type="static")
        copy_sqlite(legacy_path, proxy_pool.DB_PATH)

        # Early schema DDL commits before this fault. This freezes that
        # existing non-atomic boundary while requiring later port/dialer
        # backfill DML to roll back.
        conn = sqlite3.connect(proxy_pool.DB_PATH, factory=FailingMigrationConnection)
        conn.row_factory = sqlite3.Row
        try:
            try:
                proxy_pool.init_db(conn)
            except sqlite3.OperationalError as exc:
                assert str(exc) == "injected migration DML failure"
            else:
                raise AssertionError("migration DML failure was not injected")
        finally:
            conn.close()

        conn = sqlite3.connect(proxy_pool.DB_PATH)
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
        legacy_path = proxy_pool.DATA_DIR / "legacy-commit.sqlite"
        create_legacy_proxy_db(legacy_path, source_type="static")
        copy_sqlite(legacy_path, proxy_pool.DB_PATH)

        conn = sqlite3.connect(proxy_pool.DB_PATH, factory=FailingCommitConnection)
        conn.row_factory = sqlite3.Row
        try:
            try:
                proxy_pool.init_db(conn)
            except sqlite3.OperationalError as exc:
                assert str(exc) == "injected migration commit failure"
            else:
                raise AssertionError("migration commit failure was not injected")
        finally:
            conn.close()

        conn = sqlite3.connect(proxy_pool.DB_PATH)
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

        checked = proxy_pool.check_binding({
            "proxy_profile_id": pool["id"],
            "observed_ip": "203.0.113.50",
        })
        assert checked["allowed"] is True
        assert checked["pool"]["status"] == proxy_pool.STATUS_ACTIVE
        assert checked["pool"]["parse_error"] == ""


def test_v2_proxy_page_exposes_account_rebind_flow() -> None:
    source = (ROOT / "scripts" / "static" / "proxy.html").read_text(encoding="utf-8")
    assert "/api/proxy/accounts/proxy-binding" in source
    assert "data-live-binding-submit" in source
    assert "data-live-binding-unbind" in source
    assert "target.dataset.open==='binding'&&window.proxyRealWorkflow" in source
    assert "重新选择后，因原代理删除而暂停的等待任务会自动恢复排队" in source


def test_v2_sing_box_default_stays_in_4004_project() -> None:
    source = (ROOT / "scripts" / "proxy_pool.py").read_text(encoding="utf-8")
    assert 'os.getenv("SING_BOX_COMPOSE_PROJECT", "short-video-analyzer-ui-4004")' in source
    assert 'os.getenv("PROXY_POOL_CONFIG_NAMESPACE", "v2")' in source
    assert 'os.getenv("PROXY_POOL_MIHOMO_PREFIX", "v2-")' in source
    assert 'os.getenv("PROXY_POOL_PORT_START", "19300")' in source
    assert 'os.getenv("PROXY_POOL_PORT_END", "19399")' in source
    assert 'os.getenv("TAOBAO_PROXY_PORT_START", "19400")' in source
    assert 'os.getenv("TAOBAO_PROXY_PORT_END", "19419")' in source

    compose_path = ROOT / "docker-compose.yml"
    if compose_path.is_file():
        compose = compose_path.read_text(encoding="utf-8")
        assert compose.count("PROXY_POOL_CONFIG_NAMESPACE: ${PROXY_POOL_CONFIG_NAMESPACE:-v2}") == 2
        assert compose.count("PROXY_POOL_MIHOMO_PREFIX: ${PROXY_POOL_MIHOMO_PREFIX:-v2-}") == 2
        assert compose.count("PROXY_POOL_PORT_START: ${PROXY_POOL_PORT_START:-19300}") == 2
        assert compose.count("TAOBAO_PROXY_PORT_START: ${TAOBAO_PROXY_PORT_START:-19400}") == 2
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
        assert migrated["local_port"] == proxy_pool.PROXY_PORT_START
        assert profile["proxy_binding"]["local_port"] == proxy_pool.PROXY_PORT_START
        assert profile["browser_settings"]["proxy_server"] == f"127.0.0.1:{proxy_pool.PROXY_PORT_START}"
        assert profile["preserved"] is True


def test_sing_box_detection_accepts_serialized_pool_dict() -> None:
    previous = os.environ.get("PROXY_REALITY_CORE")
    os.environ["PROXY_REALITY_CORE"] = "sing-box"
    try:
        assert proxy_pool._sing_box_reality_pool(
            {
                "source_type": "vless",
                "parsed": {},
                "local_port": proxy_pool.PROXY_PORT_START,
            }
        ) is False
    finally:
        if previous is None:
            os.environ.pop("PROXY_REALITY_CORE", None)
        else:
            os.environ["PROXY_REALITY_CORE"] = previous


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
    test_direct_pool_recovers_after_successful_recheck()
    test_v2_proxy_page_exposes_account_rebind_flow()
    test_v2_sing_box_default_stays_in_4004_project()
    test_port_migration_updates_bound_account_profile()
    test_sing_box_detection_accepts_serialized_pool_dict()
    test_account_state_exposes_instagram_login_without_cookie_value()
    print("proxy pool lifecycle tests passed")


if __name__ == "__main__":
    main()
