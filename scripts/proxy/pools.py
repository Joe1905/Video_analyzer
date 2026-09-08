"""Proxy-pool workflows and binding checks."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from proxy.nodes import (
    _clean_port_scope,
    _clean_status,
    _clean_text,
    _json_loads,
    _row_to_pool,
    parse_static_proxy_uri,
    parse_vless_uri,
    parse_vmess_uri,
)
from proxy.repository import (
    allocate_port as _allocate_port,
    connect,
    duplicate_exit_ip_pool as _duplicate_exit_ip_pool,
)
from proxy.runtime import (
    _abs_workspace_path,
    _detect_exit_ip_with_single_repair,
    _proxy_json_with_cookies,
    _remove_mihomo_pool_config,
    _restore_mihomo_listener_config,
    _sing_box_reality_pool,
    _sync_mihomo_pool_config,
    _tiktok_identity,
    _tiktok_profile_cookies,
    ensure_proxy_cores,
    lookup_ip_geo,
)
from proxy.settings import (
    ACCOUNT_STATUS_ACTIVE,
    PROXY_QUEUE_RECHECK_SECONDS,
    PROXY_RECHECK_DELAYS_SECONDS,
    STATUS_ACTIVE,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_PAUSED,
    SYSTEM_PROXY_DIALER,
    now_iso,
)
from proxy.state import _active_sessions, list_state, _row_to_account


def _normal_username(value: Any) -> str:
    username = _clean_text(value, 120).lstrip("@")
    if not username:
        raise ValueError("username is required")
    return username


def _proxy_pending_job_count(conn: sqlite3.Connection, pool_id: int) -> int:
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM publish_jobs
           WHERE proxy_profile_id = ? AND deleted_at = ''
             AND status IN ('queued','delayed','preparing','uploading','publishing'))
          +
          (SELECT COUNT(*) FROM collect_jobs
           WHERE proxy_profile_id = ?
             AND status IN ('queued','delayed','preparing','collecting')) AS count
        """,
        (pool_id, pool_id),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _duplicate_exit_ip_reason(duplicate: sqlite3.Row, observed_ip: str) -> str:
    return (
        f"出口 IP {observed_ip} 已被代理「{duplicate['name']}」"
        f"（本地端口 {int(duplicate['local_port'] or 0)}）使用，状态已标记为 IP重复"
    )


def upsert_pool(payload: dict[str, Any]) -> dict[str, Any]:
    pool_id = int(payload.get("id") or 0)
    port_scope = _clean_port_scope(payload.get("port_scope"))
    name = _clean_text(payload.get("name"), 160)
    source_uri = _clean_text(payload.get("source_uri"), 10000)
    expected_exit_ip = _clean_text(payload.get("expected_exit_ip"), 80)
    source_type = _clean_text(payload.get("source_type"), 40)
    # 已同步但尚未录入 URI 的出口保留“默认解析”状态；只改名称、地区或禁用时，
    # 不应把它无意改写成 VLESS。
    existing = None
    if pool_id:
        with connect() as conn:
            existing = conn.execute(
                "SELECT source_type, port_scope FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
                (pool_id,),
            ).fetchone()
        if existing and not source_type and not source_uri and str(existing["source_type"] or "") == "demo":
            source_type = "demo"
        if existing and "port_scope" not in payload:
            port_scope = _clean_port_scope(existing["port_scope"])
    lowered_uri = source_uri.lower()
    if not source_type and lowered_uri.startswith("vless://"):
        source_type = "vless"
    elif not source_type and lowered_uri.startswith("vmess://"):
        source_type = "vmess"
    elif not source_type and lowered_uri.startswith(("socks://", "socks5://", "socks5h://", "http://", "https://")):
        source_type = "static"
    elif not source_type:
        source_type = "vless"
    if source_type not in {"vless", "vmess", "static", "direct", "demo"}:
        raise ValueError("代理类型必须为 vless、vmess、static 或 direct")
    dialer_proxy = SYSTEM_PROXY_DIALER if source_type == "static" else ""

    parse_status = "manual"
    parse_error = ""
    parsed: dict[str, Any] = {}
    mihomo_proxy: dict[str, Any] = {}
    mihomo_name = name
    if source_type == "direct":
        source_uri = ""
        expected_exit_ip = ""
        parse_status = "ok"
        parsed = {"mode": "server_global"}
        mihomo_name = SYSTEM_PROXY_DIALER
    elif source_uri:
        try:
            if source_type == "vless":
                parsed_result = parse_vless_uri(source_uri, fallback_name=name)
            elif source_type == "vmess":
                parsed_result = parse_vmess_uri(source_uri, fallback_name=name)
            else:
                parsed_result = parse_static_proxy_uri(source_uri, fallback_name=name)
            parse_status = str(parsed_result["parse_status"])
            parsed = parsed_result["parsed"]
            mihomo_proxy = parsed_result["mihomo_proxy"]
            mihomo_name = str(parsed_result.get("mihomo_name") or name)
        except Exception as exc:
            raise ValueError(f"代理 URI 解析失败：{exc}") from exc
    if not name:
        name = mihomo_name or expected_exit_ip
    if mihomo_proxy and not mihomo_proxy.get("name"):
        mihomo_proxy["name"] = name
    if source_type == "static" and mihomo_proxy:
        mihomo_proxy["dialer-proxy"] = dialer_proxy

    now = now_iso()
    normalized_status = _clean_status(payload.get("status"))
    if parse_status == "ok" and normalized_status in {STATUS_ERROR, STATUS_DUPLICATE}:
        normalized_status = STATUS_ACTIVE
    values = {
        "name": name,
        "source_type": source_type,
        "source_uri": source_uri,
        "dialer_proxy": dialer_proxy if source_type == "static" else "",
        "expected_exit_ip": expected_exit_ip,
        "region": _clean_text(payload.get("region"), 80),
        "status": normalized_status,
        "notes": _clean_text(payload.get("notes"), 2000),
        "parse_status": parse_status,
        "parse_error": parse_error,
        "mihomo_name": mihomo_name or name,
        "parsed_json": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
        "mihomo_proxy_json": json.dumps(mihomo_proxy, ensure_ascii=False, separators=(",", ":")),
        "port_scope": port_scope,
        "updated_at": now,
    }
    with connect() as conn:
        if pool_id:
            exists = conn.execute(
                "SELECT id FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
                (pool_id,),
            ).fetchone()
            if not exists:
                raise ValueError("proxy profile not found")
            values["local_port"] = _allocate_port(conn, pool_id, port_scope)
            conn.execute(
                """
                UPDATE proxy_profiles
                SET name=:name, source_type=:source_type, source_uri=:source_uri, dialer_proxy=:dialer_proxy,
                    expected_exit_ip=:expected_exit_ip, region=:region, status=:status, local_port=:local_port, port_scope=:port_scope,
                    notes=:notes, parse_status=:parse_status, parse_error=:parse_error,
                    mihomo_name=:mihomo_name, parsed_json=:parsed_json,
                    mihomo_proxy_json=:mihomo_proxy_json, updated_at=:updated_at
                WHERE id=:id
                """,
                {**values, "id": pool_id},
            )
        else:
            values["local_port"] = _allocate_port(conn, 0, port_scope)
            cur = conn.execute(
                """
                INSERT INTO proxy_profiles (
                    name, source_type, source_uri, dialer_proxy, expected_exit_ip, region, status, notes, local_port, port_scope,
                    parse_status, parse_error, mihomo_name, parsed_json, mihomo_proxy_json,
                    created_at, updated_at
                ) VALUES (
                    :name, :source_type, :source_uri, :dialer_proxy, :expected_exit_ip, :region, :status, :notes, :local_port, :port_scope,
                    :parse_status, :parse_error, :mihomo_name, :parsed_json, :mihomo_proxy_json,
                    :created_at, :updated_at
                )
                """,
                {**values, "created_at": now},
            )
            pool_id = int(cur.lastrowid)
        duplicate = _duplicate_exit_ip_pool(conn, pool_id, expected_exit_ip)
        if duplicate:
            duplicate_reason = _duplicate_exit_ip_reason(duplicate, expected_exit_ip)
            conn.execute(
                """UPDATE proxy_profiles
                   SET status = ?, parse_error = ?, next_auto_check_at = '', updated_at = ?
                   WHERE id = ?""",
                (STATUS_DUPLICATE, duplicate_reason, now, pool_id),
            )
        conn.commit()
    pool = get_pool(pool_id)
    if _sing_box_reality_pool(pool):
        core = ensure_proxy_cores(restart=True, required=True)
    elif pool.get("parse_status") == "ok" and (pool.get("mihomo_proxy") or pool.get("source_type") == "direct"):
        if _clean_status(pool.get("status")) == STATUS_PAUSED:
            cleanup, _backup = _remove_mihomo_pool_config(pool)
            core = {"mihomo_cleanup": cleanup}
        else:
            core = {"mihomo_sync": _sync_mihomo_pool_config(pool)}
    else:
        core = {}
    return {"pool": get_pool(pool_id), **list_state(), **core}


def get_pool(pool_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
            (pool_id,),
        ).fetchone()
        if not row:
            raise ValueError("proxy profile not found")
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM tiktok_accounts WHERE proxy_profile_id = ? AND deleted_at = ''",
            (pool_id,),
        ).fetchone()["count"]
        names = [
            str(item["username"])
            for item in conn.execute(
                "SELECT username FROM tiktok_accounts WHERE proxy_profile_id = ? AND deleted_at = '' ORDER BY username",
                (pool_id,),
            )
        ]
        return _row_to_pool(row, int(count), names, _proxy_pending_job_count(conn, pool_id))


def _profile_without_proxy_binding(raw_profile: str) -> str:
    profile = _json_loads(raw_profile, {})
    if not isinstance(profile, dict):
        profile = {}
    profile.pop("proxy_binding", None)
    browser_settings = profile.get("browser_settings")
    if isinstance(browser_settings, dict):
        browser_settings.pop("proxy_server", None)
        if browser_settings:
            profile["browser_settings"] = browser_settings
        else:
            profile.pop("browser_settings", None)
    return json.dumps(profile, ensure_ascii=False, separators=(",", ":"))


def delete_pool(pool_id: int) -> dict[str, Any]:
    with connect() as conn:
        pool = conn.execute(
            "SELECT * FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
            (pool_id,),
        ).fetchone()
        if not pool:
            raise ValueError("proxy profile not found")

        _active_sessions(conn)
        active_session = conn.execute(
            "SELECT id FROM browser_sessions WHERE proxy_profile_id = ? AND status IN ('starting','running','observing') LIMIT 1",
            (pool_id,),
        ).fetchone()
        if active_session:
            raise ValueError("代理仍有运行中的浏览器或观测通道，请先释放")

        conn.execute("BEGIN IMMEDIATE")
        active_job = conn.execute(
            """SELECT id FROM publish_jobs
               WHERE proxy_profile_id = ? AND deleted_at = ''
                 AND status IN ('preparing','uploading','publishing')
               UNION ALL
               SELECT id FROM collect_jobs
               WHERE proxy_profile_id = ? AND status IN ('preparing','collecting')
               LIMIT 1""",
            (pool_id, pool_id),
        ).fetchone()
        if active_job:
            conn.rollback()
            raise ValueError("代理仍有执行中的任务，请等待完成后再删除")
        bound_accounts = conn.execute(
            """SELECT id, profile_json FROM tiktok_accounts
               WHERE proxy_profile_id = ? AND proxy_bound = 1 AND deleted_at = ''""",
            (pool_id,),
        ).fetchall()

        sing_box_managed = _sing_box_reality_pool(pool)
        cleanup, backup = ({"removed": False, "port": int(pool["local_port"] or 0)}, None)
        if not sing_box_managed:
            cleanup, backup = _remove_mihomo_pool_config(pool)
        try:
            unbound_reason = "原绑定代理已删除，请重新选择代理或直连"
            for account in bound_accounts:
                conn.execute(
                    """UPDATE tiktok_accounts
                       SET proxy_bound = 0, profile_json = ?, last_checked_ip = '',
                           last_check_status = '未绑定', last_check_at = '',
                           last_error = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        _profile_without_proxy_binding(str(account["profile_json"] or "{}")),
                        unbound_reason,
                        now_iso(),
                        int(account["id"]),
                    ),
                )
                conn.execute(
                    "UPDATE collect_settings SET enabled = 0, updated_at = ? WHERE account_id = ?",
                    (now_iso(), int(account["id"])),
                )
            publish_jobs = conn.execute(
                """UPDATE publish_jobs
                   SET status = 'delayed', stage = 'waiting_proxy', next_attempt_at = '',
                       last_error = ?, updated_at = ?
                   WHERE proxy_profile_id = ? AND deleted_at = ''
                     AND status IN ('queued','delayed')""",
                (unbound_reason, now_iso(), pool_id),
            ).rowcount
            collect_jobs = conn.execute(
                """UPDATE collect_jobs
                   SET status = 'delayed', stage = 'waiting_proxy', next_attempt_at = '',
                       last_error = ?, updated_at = ?
                   WHERE proxy_profile_id = ? AND status IN ('queued','delayed')""",
                (unbound_reason, now_iso(), pool_id),
            ).rowcount
            conn.execute("DELETE FROM browser_sessions WHERE proxy_profile_id = ?", (pool_id,))
            deleted_at = now_iso()
            conn.execute(
                """UPDATE proxy_profiles
                   SET status = ?, next_auto_check_at = '', deleted_at = ?, updated_at = ?
                   WHERE id = ?""",
                (STATUS_PAUSED, deleted_at, deleted_at, pool_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            if backup is not None:
                try:
                    _restore_mihomo_listener_config(*backup)
                except Exception:
                    pass
            raise
    core = ensure_proxy_cores(restart=True, required=True) if sing_box_managed else {}
    return {
        **list_state(),
        "mihomo_cleanup": cleanup,
        "unbound_accounts": len(bound_accounts),
        "delayed_jobs": {"publish": publish_jobs, "collect": collect_jobs},
        **core,
    }


def account_proxy_bound(account: sqlite3.Row | dict[str, Any]) -> bool:
    try:
        return bool(account["proxy_bound"])
    except (KeyError, IndexError):
        return True


def require_account_proxy_bound(account: sqlite3.Row | dict[str, Any]) -> None:
    if not account_proxy_bound(account):
        raise ValueError("账号未绑定代理，无法执行任务或观测")


def _pool_for_check(conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    account = None
    if payload.get("account_id"):
        account = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (int(payload["account_id"]),)).fetchone()
        if not account:
            raise ValueError("account not found")
        require_account_proxy_bound(account)
        pool_id = int(account["proxy_profile_id"])
    elif payload.get("username"):
        account = conn.execute("SELECT * FROM tiktok_accounts WHERE username = ?", (_normal_username(payload["username"]),)).fetchone()
        if not account:
            raise ValueError("account not found")
        require_account_proxy_bound(account)
        pool_id = int(account["proxy_profile_id"])
    else:
        pool_id = int(payload.get("proxy_profile_id") or payload.get("pool_id") or 0)
    if not pool_id:
        raise ValueError("proxy profile is required")
    pool = conn.execute(
        "SELECT * FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
        (pool_id,),
    ).fetchone()
    if not pool:
        raise ValueError("proxy profile not found")
    return pool, account


def _stored_account_identity(account: sqlite3.Row, pool: sqlite3.Row) -> dict[str, str]:
    profile = _json_loads(account["profile_json"], {})
    isolation = profile.get("isolation") if isinstance(profile.get("isolation"), dict) else {}
    user_data_dir = str(isolation.get("user_data_dir") or "").strip()
    if not user_data_dir:
        return {}
    cookies = _tiktok_profile_cookies(str(_abs_workspace_path(user_data_dir)))
    if not cookies:
        return {}
    account_info_url = os.getenv(
        "TIKTOK_ACCOUNT_INFO_URL",
        "https://www.tiktok.com/passport/web/account/info/?aid=1459&app_language=en&device_platform=web_pc",
    )
    ok, body, _ = _proxy_json_with_cookies(
        account_info_url, int(pool["local_port"] or 0), cookies
    )
    return _tiktok_identity(body) if ok else {}


def _proxy_recheck_at(delay_seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delay_seconds))


def _schedule_proxy_recheck(
    conn: sqlite3.Connection,
    pool_id: int,
    error: str,
    expected_token: str = "",
) -> str:
    row = conn.execute(
        """SELECT status, auto_check_failures, next_auto_check_at
           FROM proxy_profiles WHERE id = ? AND deleted_at = ''""",
        (pool_id,),
    ).fetchone()
    if not row or _clean_status(row["status"]) in {STATUS_PAUSED, STATUS_DUPLICATE}:
        return ""
    if expected_token and (
        _clean_status(row["status"]) != STATUS_ERROR
        or str(row["next_auto_check_at"] or "") != expected_token
    ):
        return ""
    failures = int(row["auto_check_failures"] or 0) + 1 if row else 1
    delay = (
        PROXY_QUEUE_RECHECK_SECONDS
        if _proxy_pending_job_count(conn, pool_id)
        else PROXY_RECHECK_DELAYS_SECONDS[min(failures - 1, len(PROXY_RECHECK_DELAYS_SECONDS) - 1)]
    )
    checked_at = now_iso()
    next_check_at = _proxy_recheck_at(delay)
    query = """UPDATE proxy_profiles
               SET status = ?, parse_error = ?, auto_check_failures = ?,
                   last_auto_check_at = ?, next_auto_check_at = ?, updated_at = ?
               WHERE id = ?"""
    params: list[Any] = [
        STATUS_ERROR,
        _clean_text(error, 1000),
        failures,
        checked_at,
        next_check_at,
        checked_at,
        pool_id,
    ]
    if expected_token:
        query += " AND status = ? AND next_auto_check_at = ?"
        params.extend((STATUS_ERROR, expected_token))
    updated = conn.execute(query, params)
    if expected_token and updated.rowcount == 0:
        return ""
    return next_check_at


def schedule_proxy_recheck_for_pending_job(pool_id: int, error: Exception | str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
            (pool_id,),
        ).fetchone()
        if not row or _clean_status(row["status"]) in {STATUS_PAUSED, STATUS_DUPLICATE}:
            return ""
        next_check_at = _schedule_proxy_recheck(conn, pool_id, str(error))
        conn.commit()
        return next_check_at


def _clear_proxy_recheck(conn: sqlite3.Connection, pool_id: int, observed_ip: str, checked_at: str) -> dict[str, int]:
    conn.execute(
        """UPDATE proxy_profiles
           SET status = ?, parse_error = '', auto_check_failures = 0,
               last_auto_check_at = ?, next_auto_check_at = '', updated_at = ?
           WHERE id = ?""",
        (STATUS_ACTIVE, checked_at, checked_at, pool_id),
    )
    conn.execute(
        """UPDATE tiktok_accounts
           SET last_checked_ip = ?, last_check_status = '通过', last_check_at = ?,
               last_error = CASE
                   WHEN last_check_status IN ('阻断', 'blocked')
                        OR last_error LIKE '%出口 IP%'
                        OR last_error LIKE '%代理 IP%'
                        OR last_error LIKE '%mihomo%'
                   THEN '' ELSE last_error END,
               status = CASE
                   WHEN last_check_status IN ('阻断', 'blocked')
                        OR last_error LIKE '%出口 IP%'
                        OR last_error LIKE '%代理 IP%'
                        OR last_error LIKE '%mihomo%'
                   THEN ? ELSE status END,
               updated_at = ?
           WHERE proxy_profile_id = ? AND deleted_at = ''""",
        (observed_ip, checked_at, ACCOUNT_STATUS_ACTIVE, checked_at, pool_id),
    )
    publish_jobs = conn.execute(
        """UPDATE publish_jobs
           SET status = 'queued', stage = 'proxy_recovered', next_attempt_at = '',
               last_error = '', updated_at = ?
           WHERE proxy_profile_id = ? AND status = 'delayed'
             AND stage = 'waiting_proxy' AND deleted_at = ''""",
        (checked_at, pool_id),
    ).rowcount
    collect_jobs = conn.execute(
        """UPDATE collect_jobs
           SET status = 'queued', stage = 'proxy_recovered', next_attempt_at = '',
               last_error = '', updated_at = ?
           WHERE proxy_profile_id = ? AND status = 'delayed'
             AND stage = 'waiting_proxy'""",
        (checked_at, pool_id),
    ).rowcount
    return {"publish_jobs": publish_jobs, "collect_jobs": collect_jobs}


def check_binding(payload: dict[str, Any], require_account: bool = False) -> dict[str, Any]:
    observed_ip = _clean_text(payload.get("observed_ip") or payload.get("current_ip"), 80)
    recheck_token = _clean_text(payload.get("_recheck_token"), 80)
    detected: dict[str, Any] = {}
    now = now_iso()
    with connect() as conn:
        pool, account = _pool_for_check(conn, payload)
        if require_account and account is None:
            raise ValueError("account_id or username is required")
        if not observed_ip:
            try:
                detected = _detect_exit_ip_with_single_repair(pool)
            except Exception as exc:
                if _clean_status(pool["status"]) != STATUS_PAUSED:
                    _schedule_proxy_recheck(conn, int(pool["id"]), str(exc), recheck_token)
                    conn.commit()
                raise
            observed_ip = str(detected.get("ip") or "")
        if not observed_ip:
            raise ValueError("服务器未能自动查询到出口 IP")
        if recheck_token:
            current = conn.execute(
                "SELECT status, next_auto_check_at FROM proxy_profiles WHERE id = ?",
                (int(pool["id"]),),
            ).fetchone()
            if (
                not current
                or _clean_status(current["status"]) != STATUS_ERROR
                or str(current["next_auto_check_at"] or "") != recheck_token
            ):
                return {
                    "allowed": False,
                    "reason": "自动校验结果已过期",
                    "stale": True,
                    "observed_ip": observed_ip,
                    "expected_exit_ip": str(pool["expected_exit_ip"] or "").strip(),
                }
        direct = str(pool["source_type"] or "") == "direct"
        expected_ip = "" if direct else str(pool["expected_exit_ip"] or "").strip()
        should_bind = str(payload.get("bind") or "").lower() in {"1", "true", "yes", "on"}
        if not direct and not expected_ip and should_bind:
            expected_ip = observed_ip
        pool_status = _clean_status(pool["status"])
        if direct:
            # Reaching this branch means both the exit-IP lookup and TikTok
            # reachability checks succeeded. A transient failure must not leave
            # the server-global outlet permanently stuck in the error state.
            next_pool_status = STATUS_PAUSED if pool_status == STATUS_PAUSED else STATUS_ACTIVE
            allowed = next_pool_status == STATUS_ACTIVE
            reason = "通过（使用服务器代理出口，不绑定固定 IP）" if allowed else f"代理状态为 {next_pool_status}"
        else:
            ip_matches = bool(expected_ip and observed_ip == expected_ip)
            next_pool_status = STATUS_ACTIVE if ip_matches and pool_status != STATUS_PAUSED else pool_status
            allowed = bool(expected_ip and observed_ip == expected_ip and next_pool_status == STATUS_ACTIVE)
            if not expected_ip:
                reason = "代理还没有绑定出口 IP"
            elif observed_ip != expected_ip:
                reason = f"当前出口 IP {observed_ip} 与绑定 IP {expected_ip} 不一致"
            elif next_pool_status != STATUS_ACTIVE:
                reason = f"代理状态为 {next_pool_status}"
            else:
                reason = "通过"
            duplicate = _duplicate_exit_ip_pool(conn, int(pool["id"]), observed_ip)
            if duplicate:
                next_pool_status = STATUS_DUPLICATE
                allowed = False
                reason = _duplicate_exit_ip_reason(duplicate, observed_ip)
        if should_bind and not allowed and next_pool_status not in {STATUS_PAUSED, STATUS_DUPLICATE}:
            next_pool_status = STATUS_ERROR
        geo = detected.get("geo") or lookup_ip_geo(observed_ip)
        updated = conn.execute("""
                UPDATE proxy_profiles
                SET expected_exit_ip = ?, detected_exit_ip = ?, detected_country = ?,
                    detected_region = ?, detected_city = ?, detected_address = ?, detected_at = ?,
                    status = ?, region = COALESCE(NULLIF(?, ''), region), updated_at = ?
                WHERE id = ? AND (? = '' OR (status = ? AND next_auto_check_at = ?))
                """, (expected_ip, observed_ip, geo.get("country", ""), geo.get("region", ""), geo.get("city", ""), geo.get("address", ""), now, next_pool_status, geo.get("region", ""), now, pool["id"], recheck_token, STATUS_ERROR, recheck_token))
        if recheck_token and updated.rowcount == 0:
            return {
                "allowed": False,
                "reason": "自动校验结果已过期",
                "stale": True,
                "observed_ip": observed_ip,
                "expected_exit_ip": expected_ip,
            }
        resumed = {"publish_jobs": 0, "collect_jobs": 0}
        if allowed:
            resumed = _clear_proxy_recheck(conn, int(pool["id"]), observed_ip, now)
        elif next_pool_status == STATUS_ERROR:
            _schedule_proxy_recheck(conn, int(pool["id"]), reason, recheck_token)
        if account is not None:
            identity = _stored_account_identity(account, pool) if allowed else {}
            conn.execute(
                """
                UPDATE tiktok_accounts
                SET last_checked_ip = ?, last_check_status = ?, last_check_at = ?,
                    last_error = ?,
                    status = CASE WHEN ? THEN ? ELSE status END,
                    display_name = COALESCE(NULLIF(?, ''), display_name),
                    tiktok_avatar_url = COALESCE(NULLIF(?, ''), tiktok_avatar_url),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    observed_ip,
                    "通过" if allowed else "阻断",
                    now,
                    "" if allowed else reason,
                    1 if allowed else 0,
                    ACCOUNT_STATUS_ACTIVE,
                    identity.get("display_name", ""),
                    identity.get("avatar_url", ""),
                    now,
                    account["id"],
                ),
            )
        conn.commit()
        pool = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (pool["id"],)).fetchone()
        if account is not None:
            account = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (account["id"],)).fetchone()
        return {
            "allowed": allowed,
            "reason": reason,
            "observed_ip": observed_ip,
            "expected_exit_ip": expected_ip,
            "checked_at": now,
            "pool": _row_to_pool(pool),
            "account": _row_to_account(account) if account is not None else None,
            "detected": detected,
            "resumed": resumed,
        }


def recheck_unavailable_proxies() -> dict[str, Any]:
    now = now_iso()
    with connect() as conn:
        queue_retry_at = _proxy_recheck_at(PROXY_QUEUE_RECHECK_SECONDS)
        conn.execute(
            """
            UPDATE proxy_profiles
            SET next_auto_check_at = ?, updated_at = ?
            WHERE status = ?
              AND deleted_at = ''
              AND (next_auto_check_at = '' OR next_auto_check_at > ?)
              AND (
                EXISTS (
                    SELECT 1 FROM publish_jobs
                    WHERE proxy_profile_id = proxy_profiles.id AND deleted_at = ''
                      AND status IN ('queued','delayed','preparing','uploading','publishing')
                )
                OR EXISTS (
                    SELECT 1 FROM collect_jobs
                    WHERE proxy_profile_id = proxy_profiles.id
                      AND status IN ('queued','delayed','preparing','collecting')
                )
              )
            """,
            (queue_retry_at, now, STATUS_ERROR, queue_retry_at),
        )
        unscheduled = conn.execute(
            """SELECT id, parse_error FROM proxy_profiles
               WHERE status = ? AND next_auto_check_at = '' AND deleted_at = ''""",
            (STATUS_ERROR,),
        ).fetchall()
        for row in unscheduled:
            _schedule_proxy_recheck(conn, int(row["id"]), str(row["parse_error"] or "代理当前不可用"))
        conn.commit()
        due_pools = [
            (int(row["id"]), str(row["next_auto_check_at"] or ""))
            for row in conn.execute(
                """SELECT id, next_auto_check_at FROM proxy_profiles
                   WHERE status = ? AND next_auto_check_at <> '' AND next_auto_check_at <= ?
                     AND deleted_at = ''
                   ORDER BY next_auto_check_at, id""",
                (STATUS_ERROR, now),
            ).fetchall()
        ]
    recovered: list[int] = []
    failed: list[dict[str, Any]] = []
    for pool_id, recheck_token in due_pools:
        try:
            result = check_binding(
                {"proxy_profile_id": pool_id, "bind": True, "_recheck_token": recheck_token}
            )
            if result.get("allowed"):
                recovered.append(pool_id)
            elif result.get("stale"):
                continue
            else:
                failed.append({"id": pool_id, "error": str(result.get("reason") or "校验未通过")})
        except Exception as exc:
            failed.append({"id": pool_id, "error": str(exc)})
    return {"checked_at": now, "attempted": len(due_pools), "recovered": recovered, "failed": failed}


def _direct_login_pool_id() -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT p.id
               FROM proxy_profiles p
               WHERE p.source_type = 'direct' AND p.status = ? AND p.parse_status = 'ok'
                 AND p.deleted_at = ''
                 AND NOT EXISTS (
                     SELECT 1 FROM tiktok_accounts a
                     WHERE a.proxy_profile_id = p.id AND a.proxy_bound = 1 AND a.deleted_at = ''
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM browser_sessions s
                     WHERE s.proxy_profile_id = p.id AND s.status IN ('starting', 'running', 'observing')
                 )
               ORDER BY p.id
               LIMIT 1""",
            (STATUS_ACTIVE,),
        ).fetchone()
    if row:
        return int(row["id"])
    result = upsert_pool(
        {
            "name": "直连外网（服务器代理）",
            "source_type": "direct",
            "status": STATUS_ACTIVE,
            "notes": "新增账号时自动创建；使用服务器 GLOBAL 代理出口，不绑定固定 IP",
        }
    )
    return int(result["pool"]["id"])
