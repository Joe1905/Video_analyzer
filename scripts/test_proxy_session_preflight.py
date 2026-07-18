#!/usr/bin/env python3
"""Ad-hoc checks for connectivity-only proxy session preflight."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import proxy_pool


EXPECTED_IP = "203.0.113.10"


def seed_database(path: Path) -> None:
    proxy_pool.DB_PATH = path
    with proxy_pool.connect() as conn:
        proxy_pool.init_db(conn)
        now = proxy_pool.now_iso()
        conn.execute(
            """INSERT INTO proxy_profiles (
                   id, name, source_type, expected_exit_ip, status, parse_status,
                   created_at, updated_at, local_port, detected_exit_ip
               ) VALUES (1, 'test-line', 'vless', ?, ?, 'ok', ?, ?, 18900, ?)""",
            (EXPECTED_IP, proxy_pool.STATUS_ERROR, now, now, EXPECTED_IP),
        )
        conn.execute(
            """INSERT INTO tiktok_accounts (
                   id, username, proxy_profile_id, proxy_bound, status,
                   last_checked_ip, last_check_status, last_error, created_at, updated_at
               ) VALUES (1, 'test.account', 1, 1, ?, ?, '通过', ?, ?, ?)""",
            (
                proxy_pool.ACCOUNT_STATUS_ERROR,
                EXPECTED_IP,
                "浏览器出口 IP 校验失败：公共检测站不可用",
                now,
                now,
            ),
        )
        conn.commit()


def assert_connectivity_recovery() -> None:
    with TemporaryDirectory() as folder:
        seed_database(Path(folder) / "proxy.sqlite")
        with (
            patch.object(proxy_pool, "_port_open", return_value=True),
            patch.object(proxy_pool, "_proxy_url_reachable", return_value=(True, "HTTP 200")),
        ):
            result = proxy_pool.check_account_connectivity(1)
        assert result["allowed"] is True
        assert result["mode"] == "connectivity"
        assert result["observed_ip"] == EXPECTED_IP
        with proxy_pool.connect() as conn:
            account = conn.execute(
                "SELECT status, last_error, last_check_status FROM tiktok_accounts WHERE id = 1"
            ).fetchone()
            pool = conn.execute(
                "SELECT status, parse_error, auto_check_failures FROM proxy_profiles WHERE id = 1"
            ).fetchone()
        assert account["status"] == proxy_pool.ACCOUNT_STATUS_ACTIVE
        assert account["last_error"] == ""
        assert account["last_check_status"] == "通过"
        assert pool["status"] == proxy_pool.STATUS_ACTIVE
        assert pool["parse_error"] == ""
        assert int(pool["auto_check_failures"]) == 0


def assert_known_ip_mismatch_still_blocks() -> None:
    with TemporaryDirectory() as folder:
        seed_database(Path(folder) / "proxy.sqlite")
        with proxy_pool.connect() as conn:
            conn.execute(
                "UPDATE proxy_profiles SET detected_exit_ip = '203.0.113.11' WHERE id = 1"
            )
            conn.commit()
        with (
            patch.object(proxy_pool, "_port_open", return_value=True),
            patch.object(proxy_pool, "_proxy_url_reachable", return_value=(True, "HTTP 200")),
        ):
            try:
                proxy_pool.check_account_connectivity(1)
            except ValueError as exc:
                assert "与绑定 IP" in str(exc)
            else:
                raise AssertionError("known IP mismatch must remain blocked")


def main() -> int:
    assert_connectivity_recovery()
    assert_known_ip_mismatch_still_blocks()
    print("proxy session preflight checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
