"""Shared account snapshots and session cleanup used by pool workflows."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from proxy import settings

from proxy.nodes import _clean_account_status, _json_loads, _row_to_pool
from proxy.repository import connect
from proxy.runtime import (
    _hidden_slot_capacity_error,
    _iso_epoch,
    _manual_session_idle_expired,
    _pid_alive,
    _slot_ports_available,
    _terminate_session_processes,
)
from proxy.settings import (
    RUNTIME_ID,
    browser_max_slots,
    hidden_automation_slots,
    manual_observation_idle_seconds,
    now_iso,
    pending_login_ttl_seconds,
    visible_observation_slots,
)


def _browser_profile_dir(profile: dict[str, Any]) -> Path | None:
    isolation = profile.get("isolation") if isinstance(profile, dict) else {}
    user_data_dir = str((isolation or {}).get("user_data_dir") or "").strip()
    if not user_data_dir:
        return None
    profile_dir = Path(user_data_dir)
    if not profile_dir.is_absolute():
        profile_dir = settings.ROOT / profile_dir
    try:
        profile_dir = profile_dir.resolve()
        profiles_root = (settings.DATA_DIR / "tiktok_browser_profiles").resolve()
    except OSError:
        return None
    if profile_dir != profiles_root and profiles_root not in profile_dir.parents:
        return None
    return profile_dir


def _platform_login_metadata(profile: dict[str, Any], host_pattern: str, cookie_names: tuple[str, ...]) -> dict[str, Any]:
    """Expose login presence only; cookie values never leave Chrome."""
    profile_dir = _browser_profile_dir(profile)
    if profile_dir is None:
        return {"status": "no_profile", "profile_available": False, "logged_in": False}
    placeholders = ", ".join("?" for _ in cookie_names)
    for cookie_path in (profile_dir / "Default" / "Cookies", profile_dir / "Default" / "Network" / "Cookies"):
        if not cookie_path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True, timeout=1)
            try:
                row = conn.execute(
                    f"SELECT 1 FROM cookies WHERE lower(host_key) LIKE ? AND name IN ({placeholders}) LIMIT 1",
                    (host_pattern.lower(), *cookie_names),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return {"status": "unavailable", "profile_available": True, "logged_in": False}
        return {
            "status": "logged_in" if row else "not_logged_in",
            "profile_available": True,
            "logged_in": bool(row),
        }
    return {"status": "not_logged_in", "profile_available": True, "logged_in": False}


def _instagram_login_metadata(profile: dict[str, Any]) -> dict[str, Any]:
    return _platform_login_metadata(profile, "%instagram.com%", ("sessionid",))


def _row_to_account(row: sqlite3.Row) -> dict[str, Any]:
    profile = _json_loads(row["profile_json"], {})
    deleted_platforms = profile.get("platform_deletions") if isinstance(profile, dict) else {}
    tiktok_linked = not bool((deleted_platforms or {}).get("tiktok"))
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "tiktok_avatar_url": row["tiktok_avatar_url"],
        "feishu_user_id": row["feishu_user_id"],
        "feishu_user_name": row["feishu_user_name"],
        "feishu_avatar_url": row["feishu_avatar_url"],
        "feishu_user_active": bool(row["feishu_user_active"]),
        "feishu_user_synced_at": row["feishu_user_synced_at"],
        "proxy_profile_id": row["proxy_profile_id"],
        "proxy_bound": bool(row["proxy_bound"]),
        "status": _clean_account_status(row["status"]),
        "profile": profile,
        "platforms": {
            "tiktok": {"linked": tiktok_linked, "status": "linked" if tiktok_linked else "deleted"},
            "instagram": _instagram_login_metadata(profile),
        },
        "notes": row["notes"],
        "last_checked_ip": row["last_checked_ip"],
        "last_check_status": row["last_check_status"],
        "last_check_at": row["last_check_at"],
        "last_login_at": row["last_login_at"],
        "last_collect_at": row["last_collect_at"],
        "last_publish_at": row["last_publish_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "slot": row["slot"],
        "proxy_profile_id": row["proxy_profile_id"],
        "account_id": row["account_id"],
        "username": row["username"],
        "status": row["status"],
        "channel_url": row["channel_url"],
        "pid": row["pid"],
        "xvfb_pid": row["xvfb_pid"],
        "x11vnc_pid": row["x11vnc_pid"],
        "websockify_pid": row["websockify_pid"],
        "display": row["display"],
        "vnc_port": row["vnc_port"],
        "novnc_port": row["novnc_port"],
        "debug_port": row["debug_port"],
        "owner": row["owner"],
        "current_job_id": row["current_job_id"],
        "feishu_user_id": row["feishu_user_id"],
        "feishu_user_name": row["feishu_user_name"],
        "feishu_avatar_url": row["feishu_avatar_url"],
        "profile_key": row["profile_key"],
        "user_data_dir": row["user_data_dir"],
        "login_platform": row["login_platform"],
        "last_activity_at": row["last_activity_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if not row["account_id"] and row["status"] in {"starting", "running", "observing"}:
        created_at = _iso_epoch(str(row["created_at"] or ""))
        expires_at = created_at + pending_login_ttl_seconds()
        result["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at))
        result["expires_in_seconds"] = max(0, int(expires_at - time.time()))
    return result


def _remove_unbound_session_profile(row: sqlite3.Row | dict[str, Any]) -> None:
    try:
        if int(row["account_id"] or 0):
            return
        user_data_value = str(row["user_data_dir"] or "")
    except Exception:
        return
    if not user_data_value:
        return
    profiles_root = (settings.DATA_DIR / "tiktok_browser_profiles").resolve()
    profile_root = Path(user_data_value).resolve().parent
    if profile_root == profiles_root or profiles_root not in profile_root.parents:
        return
    shutil.rmtree(profile_root, ignore_errors=True)


def _active_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM browser_sessions WHERE status IN ('starting','running','observing') ORDER BY updated_at DESC").fetchall()
    active = []
    now = now_iso()
    now_epoch = time.time()
    for row in rows:
        pid = int(row["pid"] or 0)
        pending_expired = not row["account_id"] and _iso_epoch(str(row["created_at"] or "")) + pending_login_ttl_seconds() <= time.time()
        if str(row["runtime_id"] or "") != RUNTIME_ID:
            _terminate_session_processes(row)
            _remove_unbound_session_profile(row)
            conn.execute("UPDATE browser_sessions SET status = 'stopped', last_error = ?, updated_at = ? WHERE id = ?", ("服务已重启，浏览器和观测通道已释放", now, row["id"]))
        elif pending_expired:
            _terminate_session_processes(row)
            _remove_unbound_session_profile(row)
            conn.execute("UPDATE browser_sessions SET status = 'stopped', last_error = ?, updated_at = ? WHERE id = ?", ("未完成账号登记，临时登录通道超时自动释放", now, row["id"]))
        elif pid and not _pid_alive(pid):
            _terminate_session_processes(row)
            _remove_unbound_session_profile(row)
            conn.execute("UPDATE browser_sessions SET status = 'stopped', last_error = COALESCE(NULLIF(last_error, ''), 'browser process exited'), updated_at = ? WHERE id = ?", (now, row["id"]))
        elif _manual_session_idle_expired(row, now_epoch):
            _terminate_session_processes(row)
            _remove_unbound_session_profile(row)
            idle_minutes = max(1, manual_observation_idle_seconds() // 60)
            conn.execute(
                "UPDATE browser_sessions SET status = 'stopped', last_error = ?, updated_at = ? WHERE id = ?",
                (f"观测界面 {idle_minutes} 分钟无操作且无任务，已自动休眠", now, row["id"]),
            )
        else:
            active.append(row)
    conn.commit()
    return active


def _cleanup_terminated_unbound_profiles(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT * FROM browser_sessions "
        "WHERE account_id IS NULL AND status IN ('stopped','failed') AND user_data_dir <> ''"
    ).fetchall()
    cleaned = 0
    for row in rows:
        user_data_value = str(row["user_data_dir"] or "")
        profile_root = Path(user_data_value).resolve().parent
        _remove_unbound_session_profile(row)
        if not profile_root.exists():
            conn.execute("UPDATE browser_sessions SET user_data_dir = '' WHERE id = ?", (row["id"],))
            cleaned += 1
    conn.commit()
    return cleaned


def cleanup_expired_sessions() -> int:
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS count FROM browser_sessions WHERE status IN ('starting','running','observing')").fetchone()["count"]
        active = _active_sessions(conn)
        cleaned_profiles = _cleanup_terminated_unbound_profiles(conn)
        return max(0, int(before) - len(active)) + cleaned_profiles


def _allocate_session_slot(conn: sqlite3.Connection, owner: str) -> int:
    max_slots = browser_max_slots()
    active_sessions = _active_sessions(conn)
    if len(active_sessions) >= max_slots:
        raise ValueError(f"浏览器观测槽位已满，当前最多同时运行 {max_slots} 个")
    used = {int(row["slot"] or 0) for row in active_sessions}
    allocatable_slots = max_slots if owner == "automation" else visible_observation_slots()
    for slot in range(1, allocatable_slots + 1):
        capacity_error = _hidden_slot_capacity_error(slot)
        if slot not in used and not capacity_error and _slot_ports_available(slot):
            return slot
        if slot not in used and capacity_error:
            raise ValueError(capacity_error)
    if owner != "automation" and hidden_automation_slots():
        raise ValueError(
            f"人工观测槽位已满，当前最多同时运行 {visible_observation_slots()} 个；"
            "后台自动槽位仅供采集和发布任务使用"
        )
    raise ValueError(f"浏览器观测槽位已满或端口被占用，当前最多同时运行 {max_slots} 个")


def list_state() -> dict[str, Any]:
    with connect() as conn:
        _active_sessions(conn)
        counts = {
            int(row["proxy_profile_id"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT proxy_profile_id, COUNT(*) AS count
                FROM tiktok_accounts
                WHERE (deleted_at = '' OR deleted_at IS NULL) AND proxy_bound = 1
                GROUP BY proxy_profile_id
                """
            )
        }
        names: dict[int, list[str]] = {}
        for row in conn.execute(
            """
            SELECT proxy_profile_id, username
            FROM tiktok_accounts
            WHERE (deleted_at = '' OR deleted_at IS NULL) AND proxy_bound = 1
            ORDER BY username
            """
        ):
            names.setdefault(int(row["proxy_profile_id"]), []).append(str(row["username"]))
        pending_jobs = {
            int(row["proxy_profile_id"]): int(row["count"] or 0)
            for row in conn.execute(
                """
                SELECT proxy_profile_id, SUM(job_count) AS count
                FROM (
                    SELECT proxy_profile_id, COUNT(*) AS job_count
                    FROM publish_jobs
                    WHERE deleted_at = ''
                      AND status IN ('queued','delayed','preparing','uploading','publishing')
                    GROUP BY proxy_profile_id
                    UNION ALL
                    SELECT proxy_profile_id, COUNT(*) AS job_count
                    FROM collect_jobs
                    WHERE status IN ('queued','delayed','preparing','collecting')
                    GROUP BY proxy_profile_id
                )
                GROUP BY proxy_profile_id
                """
            )
        }
        pools = [
            _row_to_pool(
                row,
                counts.get(int(row["id"]), 0),
                names.get(int(row["id"]), []),
                pending_jobs.get(int(row["id"]), 0),
            )
            for row in conn.execute(
                "SELECT * FROM proxy_profiles WHERE deleted_at = '' ORDER BY updated_at DESC, id DESC"
            )
        ]
        accounts = [_row_to_account(row) for row in conn.execute(
            """
            SELECT *
            FROM tiktok_accounts
            WHERE deleted_at = '' OR deleted_at IS NULL
            ORDER BY updated_at DESC, id DESC
            """
        )]
        sessions = [_row_to_session(row) for row in conn.execute("SELECT * FROM browser_sessions ORDER BY updated_at DESC, id DESC LIMIT 20")]
    return {
        "pools": pools,
        "accounts": accounts,
        "sessions": sessions,
        "stats": {
            "pool_count": len(pools),
            "account_count": len(accounts),
            "blocked_accounts": sum(1 for item in accounts if item["last_check_status"] in {"阻断", "blocked"}),
        },
    }
