#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from proxy.repository import (
    allocate_port as _allocate_port,
    connect,
    duplicate_exit_ip_pool as _duplicate_exit_ip_pool,
    init_db,
)
from proxy.nodes import (
    _clean_account_status,
    _clean_port_scope,
    _clean_status,
    _clean_text,
    _json_loads,
    _row_to_pool,
    parse_static_proxy_uri,
    parse_vless_uri,
    parse_vmess_uri,
)
from proxy.runtime import (
    ProxyConfigurationError,
    _abs_workspace_path,
    _atomic_write,
    _browser_account_avatar,
    _detect_browser_exit_ip,
    _detect_exit_ip_with_single_repair,
    _hidden_automation_slot,
    _hidden_slot_capacity_error,
    _http_get_json,
    _iso_epoch,
    _launch_browser_for_session,
    _launch_observation_channel,
    _manual_session_idle_expired,
    _mihomo_listener_matches,
    _mihomo_request,
    _pid_alive,
    _port_open,
    _proxy_json_with_cookies,
    _public_novnc_url,
    _reload_mihomo_config,
    _remove_mihomo_pool_config,
    _repair_proxy_core_once,
    _restart_sing_box_container,
    _restore_mihomo_listener_config,
    _sing_box_reality_pool,
    _slot_ports,
    _slot_ports_available,
    _sync_mihomo_pool_config,
    _terminate_session_processes,
    _tiktok_identity,
    _tiktok_profile_cookies,
    _wait_for_port,
    _write_sing_box_config,
    detect_exit_ip_for_pool,
    ensure_proxy_cores,
    ensure_static_proxy_configs,
    lookup_ip_geo,
    mihomo_export,
    reconcile_mihomo_pool_configs,
    runtime_status,
    sing_box_export,
)
from proxy.settings import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_ERROR,
    ACCOUNT_STATUS_PAUSED,
    DATA_DIR,
    DB_PATH,
    DEFAULT_MIHOMO_API,
    PLATFORM_START_URLS,
    PORT_SCOPE_DEFAULT,
    PROXY_MIHOMO_NAME_PREFIX,
    PROXY_PORT_START,
    PROXY_QUEUE_RECHECK_SECONDS,
    PROXY_RECHECK_DELAYS_SECONDS,
    RUNTIME_ID,
    ROOT,
    SING_BOX_COMPOSE_PROJECT,
    SING_BOX_CONFIG_PATH,
    STATUS_ACTIVE,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_PAUSED,
    SYSTEM_PROXY_DIALER,
    TIKTOK_BROWSER_ACCEPT_LANGUAGE,
    TIKTOK_BROWSER_LOCALE,
    VNC_PORT,
    browser_max_slots,
    hidden_automation_slots,
    is_retryable_proxy_error,
    manual_observation_idle_seconds,
    now_iso,
    pending_login_ttl_seconds,
    visible_observation_slots,
)


_LOGIN_CAPTURE_LOCK = threading.Lock()


def _normal_username(value: Any) -> str:
    username = _clean_text(value, 120).lstrip("@")
    if not username:
        raise ValueError("username is required")
    return username


def _safe_profile_key(username: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in username.lower())
    return safe.strip("._-") or "account"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _isolation_profile(username: str, proxy_profile_id: int, pool: sqlite3.Row | None) -> dict[str, Any]:
    key = _safe_profile_key(username)
    profile_root = os.getenv("TIKTOK_BROWSER_PROFILE_ROOT", "data/tiktok_browser_profiles").rstrip("/")
    session_root = os.getenv("TIKTOK_SESSION_ROOT", "data/tiktok_sessions").rstrip("/")
    max_slots = browser_max_slots()
    return {
        "manual_login": {
            "surface": "novnc",
            "official_site": "https://www.tiktok.com/",
            "updated_at": now_iso(),
        },
        "proxy_binding": {
            "proxy_profile_id": proxy_profile_id,
            "proxy_name": str(pool["name"] or "") if pool else "",
            "local_port": int(pool["local_port"] or 0) if pool else 0,
            "expected_exit_ip": str(pool["expected_exit_ip"] or "") if pool else "",
            "detected_address": str(pool["detected_address"] or pool["region"] or "") if pool else "",
        },
        "isolation": {
            "browser_profile_key": f"tiktok-{key}",
            "user_data_dir": f"{profile_root}/{key}/user-data",
            "cookie_store_dir": f"{profile_root}/{key}/cookies",
            "session_dir": f"{session_root}/{key}",
            "cache_dir": f"{profile_root}/{key}/cache",
            "download_dir": f"{profile_root}/{key}/downloads",
            "browser_context": "per_account_required",
            "storage_state": "per_account_required",
            "cookie_jar": "per_account_required",
            "local_storage": "per_account_required",
            "indexed_db": "per_account_required",
            "service_workers": "per_account_required",
            "web_rtc_policy": "disable_non_proxied_udp_required",
            "runner_must_preflight_proxy_ip": True,
        },
        "browser_settings": {
            "locale": TIKTOK_BROWSER_LOCALE,
            "timezone": os.getenv("TZ", "America/Los_Angeles"),
            "accept_language": TIKTOK_BROWSER_ACCEPT_LANGUAGE,
            "geolocation": "deny_or_match_proxy_region",
            "permissions": "per_account_profile_only",
            "proxy_server": f"127.0.0.1:{int(pool['local_port'] or 0) if pool else 0}",
            "disable_background_networking": True,
            "disable_default_apps": True,
            "disable_sync": True,
            "disable_translate": True,
            "disable_non_proxied_udp": True,
        },
        "system_settings": {
            "notes": "Use a per-account browser process/profile. OS-level global settings are shared unless the runner starts isolated containers or desktops.",
            "preferred_desktop_mode": "per_slot_novnc_or_container",
            "clipboard_isolation": "avoid_cross_account_copy_paste",
            "downloads_isolation": "per_account_download_dir",
        },
        "worker": {
            "mode": "slot_pool",
            "max_parallel_slots": max_slots,
            "one_account_per_browser_context": True,
            "one_account_per_browser_process": True,
            "novnc_observation": "per_slot_or_selected_account",
        },
    }


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


def _browser_profile_dir(profile: dict[str, Any]) -> Path | None:
    isolation = profile.get("isolation") if isinstance(profile, dict) else {}
    user_data_dir = str((isolation or {}).get("user_data_dir") or "").strip()
    if not user_data_dir:
        return None
    profile_dir = Path(user_data_dir)
    if not profile_dir.is_absolute():
        profile_dir = ROOT / profile_dir
    try:
        profile_dir = profile_dir.resolve()
        profiles_root = (DATA_DIR / "tiktok_browser_profiles").resolve()
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


def _chrome_bookmark_timestamp() -> str:
    return str(int((time.time() + 11644473600) * 1_000_000))


def _save_instagram_reels_bookmark(profile: dict[str, Any], reels_url: str) -> bool:
    """Persist the account's Reels entry in Chrome's bookmark bar without tokens."""
    profile_dir = _browser_profile_dir(profile)
    if profile_dir is None:
        return False
    bookmark_path = profile_dir / "Default" / "Bookmarks"
    try:
        data = _json_loads(bookmark_path.read_text(encoding="utf-8"), {}) if bookmark_path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
        roots = data.setdefault("roots", {})
        bar = roots.setdefault(
            "bookmark_bar",
            {"children": [], "date_added": _chrome_bookmark_timestamp(), "date_modified": "0", "id": "1", "name": "Bookmarks bar", "type": "folder"},
        )
        children = bar.setdefault("children", [])
        if not isinstance(children, list):
            children = []
            bar["children"] = children
        if any(isinstance(item, dict) and str(item.get("url") or "") == reels_url for item in children):
            return True
        children.append(
            {
                "date_added": _chrome_bookmark_timestamp(),
                "guid": str(uuid.uuid4()),
                "id": str(int(time.time() * 1_000_000)),
                "name": "Instagram Reels 采集入口",
                "type": "url",
                "url": reels_url,
            }
        )
        bar["date_modified"] = _chrome_bookmark_timestamp()
        data.setdefault("version", 1)
        temporary = bookmark_path.with_suffix(".tmp")
        bookmark_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(bookmark_path)
        return True
    except OSError:
        return False


def bootstrap_instagram_profile(account_id: int, session_id: int) -> dict[str, Any]:
    """Click the logged-in Instagram avatar and retain the profile Reels entry point."""
    with connect() as conn:
        account = conn.execute("SELECT profile_json FROM tiktok_accounts WHERE id = ? AND deleted_at = ''", (account_id,)).fetchone()
        session = _session_by_id(conn, session_id)
        if not account or int(session["account_id"] or 0) != int(account_id):
            raise ValueError("观测通道不属于当前账号")
        profile = _json_loads(account["profile_json"], {})
        if not _instagram_login_metadata(profile).get("logged_in"):
            return {"configured": False, "reason": "当前 Profile 未检测到 Instagram 登录"}
        debug_port = int(session["debug_port"] or 0)
    if not debug_port:
        return {"configured": False, "reason": "Chrome 调试端口不可用"}
    profile_url = ""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            if not browser.contexts:
                raise ValueError("Chrome 没有可用的浏览器上下文")
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()
            page.goto(PLATFORM_START_URLS["instagram"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
            href = ""
            excluded = {"", "/", "/accounts/", "/direct/", "/explore/", "/reels/", "/stories/"}
            preferred = page.locator("a[aria-label*='Profile' i][href], a[aria-label*='profile' i][href]")
            if preferred.count():
                href = str(preferred.first.get_attribute("href") or "").split("?", 1)[0]
            if not href:
                links = page.locator("a[href]")
                for index in range(min(links.count(), 240)):
                    candidate = str(links.nth(index).get_attribute("href") or "").strip()
                    parsed = urlparse(candidate)
                    path = parsed.path if parsed.scheme else candidate.split("?", 1)[0]
                    if path in excluded or not re.fullmatch(r"/[A-Za-z0-9._]+/", path):
                        continue
                    href = path
                    break
            if not href:
                raise ValueError("未找到 Instagram 头像主页入口")
            page.locator(f'a[href="{href}"]').first.click(timeout=5000)
            page.wait_for_timeout(1000)
            parsed = urlparse(page.url)
            if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"} or not re.fullmatch(r"/[A-Za-z0-9._]+/", parsed.path):
                raise ValueError("点击头像后未进入 Instagram 账号主页")
            profile_url = f"https://www.instagram.com{parsed.path}"
    except Exception as exc:
        return {"configured": False, "reason": _clean_text(exc, 500)}
    handle = profile_url.rstrip("/").rsplit("/", 1)[-1]
    reels_url = f"https://www.instagram.com/{handle}/reels/"
    bookmark_saved = _save_instagram_reels_bookmark(profile, reels_url)
    profile["instagram"] = {
        "username": handle,
        "profile_url": profile_url,
        "reels_url": reels_url,
        "bookmark_name": "Instagram Reels 采集入口",
        "bookmark_saved": bookmark_saved,
        "configured_at": now_iso(),
    }
    with connect() as conn:
        conn.execute(
            "UPDATE tiktok_accounts SET profile_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(profile, ensure_ascii=False, separators=(",", ":")), now_iso(), account_id),
        )
        conn.commit()
    return {"configured": True, "profile_url": profile_url, "reels_url": reels_url, "bookmark_saved": bookmark_saved}


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
    profiles_root = (DATA_DIR / "tiktok_browser_profiles").resolve()
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


def _avatar_content_type(body: bytes) -> str:
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("TikTok 头像不是支持的图片格式")


def _account_avatar_path(account_id: int) -> Path:
    if account_id <= 0:
        raise ValueError("account_id is required")
    return DATA_DIR / "tiktok_account_avatars" / f"{account_id}.img"


def account_avatar_bytes(account_id: int) -> tuple[bytes, str]:
    path = _account_avatar_path(account_id)
    body = path.read_bytes()
    return body, _avatar_content_type(body)


def _write_account_avatar(account_id: int, body: bytes) -> str:
    if not 256 <= len(body) <= 2 * 1024 * 1024:
        raise ValueError("TikTok 头像文件大小异常")
    _avatar_content_type(body)
    path = _account_avatar_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(body)
    temporary.replace(path)
    return f"/api/proxy/accounts/avatar/{account_id}?v={path.stat().st_mtime_ns}"


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


def _row_to_product(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "product_id": row["product_id"],
        "product_name": row["product_name"],
        "product_url": row["product_url"],
        "image_url": row["image_url"],
        "price": row["price"],
        "stock": row["stock"],
        "status": row["status"],
        "source": row["source"],
        "sort_order": int(row["sort_order"]),
        "updated_at": row["updated_at"],
    }


def list_products() -> dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM tiktok_products ORDER BY source, sort_order, product_name").fetchall()
    finally:
        conn.close()
    return {"products": [_row_to_product(row) for row in rows]}


def _product_payload(raw: dict[str, Any], *, source: str = "universal") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("商品数据格式不正确")
    product_id = _clean_text(raw.get("product_id"), 120)
    product_name = _clean_text(raw.get("product_name") or raw.get("title"), 2000)
    if not product_id or not product_name:
        raise ValueError("商品必须包含 product_id 和 product_name")
    if not product_id.isdigit():
        raise ValueError("TikTok Shop 商品 ID 必须是纯数字")
    product_url = _clean_text(raw.get("product_url") or raw.get("url"), 4000)
    if product_url:
        parsed = urlparse(product_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            raise ValueError("商品链接必须是 TikTok Shop 的 http/https 链接")
    return {
        "product_id": product_id,
        "product_name": product_name,
        "product_url": product_url,
        "image_url": _clean_text(raw.get("image_url"), 4000),
        "price": _clean_text(raw.get("price"), 80),
        "stock": _clean_text(raw.get("stock"), 80),
        "status": _clean_text(raw.get("status"), 80) or "Active",
        "source": _clean_text(source, 80) or "universal",
        "sort_order": int(raw.get("sort_order") or 0),
    }


def create_product(raw: dict[str, Any]) -> dict[str, Any]:
    product = _product_payload(raw)
    now = now_iso()
    with connect() as conn:
        duplicate = conn.execute(
            "SELECT product_id FROM tiktok_products WHERE product_id = ?",
            (product["product_id"],),
        ).fetchone()
        if duplicate:
            raise ValueError(f"商品 ID {product['product_id']} 已存在于通用商品库")
        conn.execute(
            """
            INSERT INTO tiktok_products (
                product_id, product_name, product_url, image_url, price, stock,
                status, source, sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product["product_id"], product["product_name"], product["product_url"],
                product["image_url"], product["price"], product["stock"], product["status"],
                product["source"], product["sort_order"], now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tiktok_products WHERE product_id = ?", (product["product_id"],)).fetchone()
    return {"product": _row_to_product(row), **list_products()}


def update_product(raw: dict[str, Any]) -> dict[str, Any]:
    product = _product_payload(raw)
    now = now_iso()
    with connect() as conn:
        existing = conn.execute("SELECT * FROM tiktok_products WHERE product_id = ?", (product["product_id"],)).fetchone()
        if not existing:
            raise ValueError("通用商品不存在或已被删除")
        conn.execute(
            """
            UPDATE tiktok_products
            SET product_name = ?, product_url = ?, image_url = ?, price = ?, stock = ?,
                status = ?, sort_order = ?, updated_at = ?
            WHERE product_id = ?
            """,
            (
                product["product_name"], product["product_url"], product["image_url"],
                product["price"], product["stock"], product["status"], product["sort_order"],
                now, product["product_id"],
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tiktok_products WHERE product_id = ?", (product["product_id"],)).fetchone()
    return {"product": _row_to_product(row), **list_products()}


def delete_product(product_id: str) -> dict[str, Any]:
    cleaned_id = _clean_text(product_id, 120)
    if not cleaned_id:
        raise ValueError("product_id is required")
    with connect() as conn:
        existing = conn.execute("SELECT product_id FROM tiktok_products WHERE product_id = ?", (cleaned_id,)).fetchone()
        if not existing:
            raise ValueError("通用商品不存在或已被删除")
        pending = conn.execute(
            """
            SELECT id FROM publish_jobs
            WHERE product_link = ? AND deleted_at = ''
              AND status NOT IN ('published','scheduled_on_tiktok','cancelled','dry_run')
            LIMIT 1
            """,
            (cleaned_id,),
        ).fetchone()
        if pending:
            raise ValueError("商品仍被待处理或可重试的发布任务使用，请先移除任务中的商品")
        conn.execute("DELETE FROM tiktok_products WHERE product_id = ?", (cleaned_id,))
        conn.commit()
    return {"deleted_product_id": cleaned_id, **list_products()}


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


def upsert_account(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("id") or 0)
    username = _normal_username(payload.get("username"))
    proxy_profile_id = int(payload.get("proxy_profile_id") or 0)
    if not proxy_profile_id:
        raise ValueError("proxy_profile_id is required")
    profile_provided = "profile" in payload
    profile = payload.get("profile", {})
    if isinstance(profile, str):
        profile = _json_loads(profile, {})
    if not isinstance(profile, dict):
        raise ValueError("profile must be a JSON object")

    session_id = int(payload.get("session_id") or 0)
    feishu_binding_provided = "feishu_user_id" in payload
    now = now_iso()
    values = {
        "username": username,
        "display_name": _clean_text(payload.get("display_name"), 160),
        "tiktok_avatar_url": _clean_text(payload.get("tiktok_avatar_url"), 2000),
        "feishu_user_id": _clean_text(payload.get("feishu_user_id"), 256),
        "feishu_user_name": _clean_text(payload.get("feishu_user_name"), 160),
        "feishu_avatar_url": _clean_text(payload.get("feishu_avatar_url"), 2000),
        "feishu_user_active": 1 if payload.get("feishu_user_id") else 0,
        "feishu_user_synced_at": now if payload.get("feishu_user_id") else "",
        "proxy_profile_id": proxy_profile_id,
        "status": _clean_account_status(payload.get("status")),
        "profile_json": "{}",
        "notes": _clean_text(payload.get("notes"), 2000),
        "updated_at": now,
    }
    with connect() as conn:
        existing_account = None
        if account_id:
            existing_account = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone()
            if not existing_account:
                raise ValueError("account not found")
            for name in (
                "tiktok_avatar_url",
                "feishu_user_id",
                "feishu_user_name",
                "feishu_avatar_url",
            ):
                if name not in payload:
                    values[name] = str(existing_account[name] or "")
            if not feishu_binding_provided:
                values["feishu_user_active"] = int(existing_account["feishu_user_active"] or 0)
                values["feishu_user_synced_at"] = str(existing_account["feishu_user_synced_at"] or "")
        pool_row = conn.execute(
            "SELECT * FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
            (proxy_profile_id,),
        ).fetchone()
        if not pool_row:
            raise ValueError("proxy profile not found")
        profile = _deep_merge(_isolation_profile(username, proxy_profile_id, pool_row), profile)
        if session_id:
            session_row = _session_by_id(conn, session_id)
            if int(session_row["proxy_profile_id"] or 0) != proxy_profile_id:
                raise ValueError("登录会话与账号绑定代理不一致")
            if session_row["status"] not in {"starting", "running", "observing"}:
                raise ValueError("登录会话已经结束，不能保存为账号 profile")
            session_user_data_value = str(session_row["user_data_dir"] or "")
            if not session_user_data_value:
                raise ValueError("登录会话没有浏览器 profile 路径")
            if str(session_row["feishu_user_id"] or ""):
                values["feishu_user_id"] = str(session_row["feishu_user_id"])
                values["feishu_user_name"] = str(session_row["feishu_user_name"] or "")
                values["feishu_avatar_url"] = str(session_row["feishu_avatar_url"] or "")
                values["feishu_user_active"] = 1
                values["feishu_user_synced_at"] = now
            session_user_data = Path(session_user_data_value)
            session_root = session_user_data.parent
            profile["isolation"] = {
                **(profile.get("isolation") if isinstance(profile.get("isolation"), dict) else {}),
                "browser_profile_key": str(session_row["profile_key"] or f"tiktok-{_safe_profile_key(username)}"),
                "user_data_dir": str(session_user_data),
                "cookie_store_dir": str(session_root / "cookies"),
                "session_dir": str(session_root / "session"),
                "cache_dir": str(session_root / "cache"),
                "download_dir": str(session_root / "downloads"),
            }
            profile["observation_session"] = {"session_id": session_id, "bound_at": now, "persisted": True}
        if not account_id and not values["feishu_user_id"]:
            raise ValueError("新增账号必须选择飞书用户")
        values["profile_json"] = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
        if account_id:
            existing_profile = _json_loads(existing_account["profile_json"], {})
            if not profile_provided:
                values["profile_json"] = json.dumps(_deep_merge(_isolation_profile(username, proxy_profile_id, pool_row), existing_profile), ensure_ascii=False, separators=(",", ":"))
            elif not session_id and isinstance(existing_profile.get("isolation"), dict):
                profile["isolation"] = existing_profile["isolation"]
                values["profile_json"] = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """
                UPDATE tiktok_accounts
                SET username=:username, display_name=:display_name,
                    tiktok_avatar_url=:tiktok_avatar_url,
                    feishu_user_id=:feishu_user_id,
                    feishu_user_name=:feishu_user_name,
                    feishu_avatar_url=:feishu_avatar_url,
                    feishu_user_active=:feishu_user_active,
                    feishu_user_synced_at=:feishu_user_synced_at,
                    proxy_profile_id=:proxy_profile_id,
                    status=:status, profile_json=:profile_json, notes=:notes, updated_at=:updated_at
                WHERE id=:id
                """,
                {**values, "id": account_id},
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO tiktok_accounts (
                    username, display_name, tiktok_avatar_url,
                    feishu_user_id, feishu_user_name, feishu_avatar_url,
                    feishu_user_active, feishu_user_synced_at,
                    proxy_profile_id, status, profile_json, notes,
                    created_at, updated_at
                ) VALUES (
                    :username, :display_name, :tiktok_avatar_url,
                    :feishu_user_id, :feishu_user_name, :feishu_avatar_url,
                    :feishu_user_active, :feishu_user_synced_at,
                    :proxy_profile_id, :status, :profile_json, :notes,
                    :created_at, :updated_at
                )
                """,
                {**values, "created_at": now},
            )
            account_id = int(cur.lastrowid)
        if session_id:
            conn.execute("UPDATE browser_sessions SET account_id = ?, username = ?, updated_at = ? WHERE id = ?", (account_id, username, now, session_id))
        conn.commit()
    result = {"account": get_account(account_id), **list_state()}
    if session_id:
        try:
            result["instagram_bootstrap"] = bootstrap_instagram_profile(account_id, session_id)
            result["account"] = get_account(account_id)
            result.update(list_state())
        except Exception as exc:
            # TikTok account binding must remain available even when an optional
            # Instagram profile bootstrap cannot be completed.
            result["instagram_bootstrap"] = {"configured": False, "reason": _clean_text(exc, 500)}
    return result


def get_account(account_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise ValueError("account not found")
        return _row_to_account(row)


def sync_feishu_directory(users: list[dict[str, Any]]) -> dict[str, int]:
    normalized: dict[str, tuple[str, str]] = {}
    for item in users:
        if not isinstance(item, dict):
            continue
        user_id = _clean_text(item.get("feishuId") or item.get("id"), 256)
        if not user_id:
            continue
        normalized[user_id] = (
            _clean_text(item.get("name"), 160),
            _clean_text(item.get("avatarUrl"), 2000),
        )

    now = now_iso()
    updated_accounts = 0
    inactive_accounts = 0
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, feishu_user_id, feishu_user_name, feishu_avatar_url,
                      feishu_user_active
               FROM tiktok_accounts
               WHERE feishu_user_id <> '' AND deleted_at = ''"""
        ).fetchall()
        for row in rows:
            user_id = str(row["feishu_user_id"] or "")
            user = normalized.get(user_id)
            is_active = 1 if user else 0
            name = user[0] if user else str(row["feishu_user_name"] or "")
            avatar_url = user[1] if user else str(row["feishu_avatar_url"] or "")
            if not is_active:
                inactive_accounts += 1
            if (
                name != str(row["feishu_user_name"] or "")
                or avatar_url != str(row["feishu_avatar_url"] or "")
                or is_active != int(row["feishu_user_active"] or 0)
            ):
                updated_accounts += 1
            conn.execute(
                """UPDATE tiktok_accounts
                   SET feishu_user_name = ?, feishu_avatar_url = ?,
                       feishu_user_active = ?, feishu_user_synced_at = ?
                   WHERE id = ?""",
                (name, avatar_url, is_active, now, int(row["id"])),
            )
        conn.commit()
    return {
        "active_users": len(normalized),
        "updated_accounts": updated_accounts,
        "inactive_accounts": inactive_accounts,
    }


def delete_account(account_id: int) -> dict[str, Any]:
    with connect() as conn:
        if any(int(row["account_id"] or 0) == account_id for row in _active_sessions(conn)):
            raise ValueError("账号仍处于唤醒或运行状态，请先休眠账号")
        active_job = conn.execute(
            "SELECT id FROM publish_jobs WHERE account_id = ? AND status NOT IN ('published','failed','cancelled','scheduled_on_tiktok','dry_run') LIMIT 1",
            (account_id,),
        ).fetchone()
        if active_job:
            raise ValueError("账号仍有草稿、待发布或运行中的发布任务，请先处理任务")
        active_collect = conn.execute(
            "SELECT id FROM collect_jobs WHERE account_id = ? AND status IN ('queued','delayed','preparing','collecting') LIMIT 1",
            (account_id,),
        ).fetchone()
        if active_collect:
            raise ValueError("账号仍有待执行或运行中的统计采集任务，请先处理任务")
        account = conn.execute("SELECT username FROM tiktok_accounts WHERE id = ? AND deleted_at = ''", (account_id,)).fetchone()
        if not account:
            raise ValueError("account not found")
        now = now_iso()
        conn.execute(
            "UPDATE tiktok_accounts SET username = ?, status = ?, deleted_at = ?, updated_at = ? WHERE id = ?",
            (f"{account['username']}__deleted_{account_id}", ACCOUNT_STATUS_PAUSED, now, now, account_id),
        )
        conn.commit()
    return list_state()


def delete_account_platform(account_id: int, platform: str) -> dict[str, Any]:
    """Remove one platform's login data while preserving another platform's profile."""
    platform = str(platform or "").strip().lower()
    definitions = {
        "tiktok": ("%tiktok.com%", ("sessionid", "sessionid_ss", "sid_tt")),
        "instagram": ("%instagram.com%", ("sessionid",)),
    }
    if platform not in definitions:
        raise ValueError("仅支持删除 TikTok 或 Instagram 登录资料")
    with connect() as conn:
        _require_account_binding_idle(conn, account_id)
        account = conn.execute(
            "SELECT * FROM tiktok_accounts WHERE id = ? AND deleted_at = ''",
            (account_id,),
        ).fetchone()
        if not account:
            raise ValueError("account not found")
        profile = _json_loads(account["profile_json"], {})
        profile_dir = _browser_profile_dir(profile)
        if profile_dir is None:
            raise ValueError("该账号没有可删除的平台浏览器 Profile")
        host_pattern, cookie_names = definitions[platform]
        removed = 0
        placeholders = ", ".join("?" for _ in cookie_names)
        for cookie_path in (profile_dir / "Default" / "Cookies", profile_dir / "Default" / "Network" / "Cookies"):
            if not cookie_path.is_file():
                continue
            try:
                cookie_conn = sqlite3.connect(cookie_path, timeout=3)
                try:
                    cursor = cookie_conn.execute(
                        f"DELETE FROM cookies WHERE lower(host_key) LIKE ? AND name IN ({placeholders})",
                        (host_pattern.lower(), *cookie_names),
                    )
                    cookie_conn.commit()
                    removed += max(0, int(cursor.rowcount or 0))
                finally:
                    cookie_conn.close()
            except sqlite3.Error as exc:
                raise ValueError(f"无法删除 {platform} 登录资料，请稍后重试：{exc}") from exc
        if not removed and platform != "tiktok":
            raise ValueError(f"未找到可删除的 {platform} 登录资料")
        now = now_iso()
        if not isinstance(profile, dict):
            profile = {}
        deleted_platforms = profile.get("platform_deletions")
        if not isinstance(deleted_platforms, dict):
            deleted_platforms = {}
        if platform == "tiktok":
            deleted_platforms["tiktok"] = now
        profile["platform_deletions"] = deleted_platforms
        remaining_tiktok = not bool(deleted_platforms.get("tiktok"))
        remaining_instagram = _instagram_login_metadata(profile)["logged_in"]
        deleted_account = not remaining_tiktok and not remaining_instagram
        if deleted_account:
            conn.execute(
                "UPDATE tiktok_accounts SET username = ?, status = ?, deleted_at = ?, updated_at = ? WHERE id = ?",
                (f"{account['username']}__deleted_{account_id}", ACCOUNT_STATUS_PAUSED, now, now, account_id),
            )
        else:
            conn.execute(
                "UPDATE tiktok_accounts SET profile_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(profile, ensure_ascii=False, separators=(",", ":")), now, account_id),
            )
        conn.commit()
    result = list_state()
    result.update({"deleted_platform": platform, "deleted_account": deleted_account})
    return result


def account_proxy_bound(account: sqlite3.Row | dict[str, Any]) -> bool:
    try:
        return bool(account["proxy_bound"])
    except (KeyError, IndexError):
        return True


def require_account_proxy_bound(account: sqlite3.Row | dict[str, Any]) -> None:
    if not account_proxy_bound(account):
        raise ValueError("账号未绑定代理，无法执行任务或观测")


def _require_account_binding_idle(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    allow_waiting_proxy: bool = False,
) -> None:
    active_session = conn.execute(
        """SELECT id FROM browser_sessions
           WHERE account_id = ? AND status IN ('starting','running','observing')
           LIMIT 1""",
        (account_id,),
    ).fetchone()
    if active_session:
        raise ValueError("账号仍处于唤醒或运行状态，请先休眠账号")
    publish_statuses = (
        "('preparing','uploading','publishing')"
        if allow_waiting_proxy
        else "('queued','delayed','preparing','uploading','publishing')"
    )
    active_publish = conn.execute(
        f"""SELECT id FROM publish_jobs
            WHERE account_id = ? AND deleted_at = ''
              AND status IN {publish_statuses}
            LIMIT 1""",
        (account_id,),
    ).fetchone()
    if active_publish:
        raise ValueError("账号仍有待发布或运行中的发布任务，请先取消或等待完成")
    collect_statuses = (
        "('preparing','collecting')"
        if allow_waiting_proxy
        else "('queued','delayed','preparing','collecting')"
    )
    active_collect = conn.execute(
        f"""SELECT id FROM collect_jobs
            WHERE account_id = ?
              AND status IN {collect_statuses}
            LIMIT 1""",
        (account_id,),
    ).fetchone()
    if active_collect:
        raise ValueError("账号仍有待执行或运行中的统计采集任务，请先取消或等待完成")


def update_account_proxy_binding(payload: dict[str, Any]) -> dict[str, Any]:
    action = _clean_text(payload.get("action"), 20).lower()
    requested_pool = str(payload.get("proxy_profile_id") or payload.get("pool_id") or "").strip()
    pool_id = _direct_login_pool_id() if action == "bind" and requested_pool == "direct" else int(requested_pool or 0)
    if action not in {"bind", "unbind"}:
        raise ValueError("action must be bind or unbind")
    if not pool_id:
        raise ValueError("proxy_profile_id is required")

    account_id = 0
    now = now_iso()
    with connect() as conn:
        _active_sessions(conn)
        conn.execute("BEGIN IMMEDIATE")
        pool = conn.execute(
            "SELECT * FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
            (pool_id,),
        ).fetchone()
        if not pool:
            raise ValueError("proxy profile not found")
        bound_account = conn.execute(
            """SELECT * FROM tiktok_accounts
               WHERE proxy_profile_id = ? AND proxy_bound = 1 AND deleted_at = ''
               LIMIT 1""",
            (pool_id,),
        ).fetchone()

        if action == "unbind":
            if not bound_account:
                raise ValueError("该代理当前未绑定账号")
            account_id = int(bound_account["id"])
            _require_account_binding_idle(conn, account_id)
            profile_json = _profile_without_proxy_binding(str(bound_account["profile_json"] or "{}"))
            conn.execute(
                """UPDATE tiktok_accounts
                   SET proxy_bound = 0, profile_json = ?, last_checked_ip = '',
                       last_check_status = '未绑定', last_check_at = '',
                       last_error = '账号未绑定代理，无法执行任务或观测', updated_at = ?
                   WHERE id = ?""",
                (profile_json, now, account_id),
            )
            conn.execute(
                "UPDATE collect_settings SET enabled = 0, updated_at = ? WHERE account_id = ?",
                (now, account_id),
            )
        else:
            account_id = int(payload.get("account_id") or 0)
            if not account_id:
                raise ValueError("请选择要绑定的账号")
            if bound_account:
                raise ValueError(f"该代理已绑定 @{bound_account['username']}")
            account = conn.execute(
                "SELECT * FROM tiktok_accounts WHERE id = ? AND deleted_at = ''",
                (account_id,),
            ).fetchone()
            if not account:
                raise ValueError("account not found")
            if account_proxy_bound(account):
                raise ValueError("该账号已绑定其他代理，请先解绑")
            _require_account_binding_idle(conn, account_id, allow_waiting_proxy=True)
            profile = _json_loads(account["profile_json"], {})
            if not isinstance(profile, dict):
                profile = {}
            profile["proxy_binding"] = {
                "proxy_profile_id": pool_id,
                "proxy_name": str(pool["name"] or ""),
                "local_port": int(pool["local_port"] or 0),
                "expected_exit_ip": str(pool["expected_exit_ip"] or ""),
                "detected_address": str(pool["detected_address"] or pool["region"] or ""),
            }
            browser_settings = profile.get("browser_settings")
            if not isinstance(browser_settings, dict):
                browser_settings = {}
            browser_settings["proxy_server"] = f"127.0.0.1:{int(pool['local_port'] or 0)}"
            profile["browser_settings"] = browser_settings
            conn.execute(
                """UPDATE tiktok_accounts
                   SET proxy_profile_id = ?, proxy_bound = 1, profile_json = ?,
                       last_checked_ip = '', last_check_status = '待校验', last_check_at = '',
                       last_error = '', updated_at = ?
                   WHERE id = ?""",
                (
                    pool_id,
                    json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
                    now,
                    account_id,
                ),
            )
            publish_jobs = conn.execute(
                """UPDATE publish_jobs
                   SET proxy_profile_id = ?, status = 'queued', stage = 'proxy_rebound',
                       next_attempt_at = '', last_error = '', updated_at = ?
                   WHERE account_id = ? AND deleted_at = ''
                     AND status = 'delayed' AND stage = 'waiting_proxy'""",
                (pool_id, now, account_id),
            ).rowcount
            collect_jobs = conn.execute(
                """UPDATE collect_jobs
                   SET proxy_profile_id = ?, status = 'queued', stage = 'proxy_rebound',
                       next_attempt_at = '', last_error = '', updated_at = ?
                   WHERE account_id = ? AND status = 'delayed' AND stage = 'waiting_proxy'""",
                (pool_id, now, account_id),
            ).rowcount
        conn.commit()
    result = {"account": get_account(account_id), **list_state()}
    if action == "bind":
        result["resumed"] = {"publish_jobs": publish_jobs, "collect_jobs": collect_jobs}
    return result


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


def _session_by_id(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise ValueError("browser session not found")
    return row


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


def start_login_session(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    start_platform = _clean_text(payload.get("start_platform"), 32).lower() or "tiktok"
    login_platform = _clean_text(payload.get("login_platform"), 32).lower()
    if start_platform not in PLATFORM_START_URLS:
        raise ValueError("启动平台仅支持 TikTok 或 Instagram")
    if login_platform and login_platform not in PLATFORM_START_URLS:
        raise ValueError("新增登录仅支持 TikTok 或 Instagram")
    if login_platform and not account_id:
        raise ValueError("新增平台登录必须指定已有账号")
    if login_platform:
        start_platform = login_platform
    requested_pool = str(payload.get("proxy_profile_id") or payload.get("pool_id") or "").strip()
    proxy_profile_id = _direct_login_pool_id() if not account_id and requested_pool == "direct" else int(requested_pool or 0)
    saved_profile: dict[str, Any] = {}
    if account_id:
        with connect() as conn:
            account_row = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone()
        if not account_row:
            raise ValueError("account not found")
        if "deleted_at" in account_row.keys() and account_row["deleted_at"]:
            raise ValueError("account has been deleted")
        require_account_proxy_bound(account_row)
        if str(account_row["feishu_user_id"] or "") and not bool(account_row["feishu_user_active"]):
            raise ValueError("账号绑定的飞书用户已从白名单移除，请先重新绑定")
        bound_proxy_id = int(account_row["proxy_profile_id"] or 0)
        if proxy_profile_id and proxy_profile_id != bound_proxy_id:
            raise ValueError("账号与请求代理不一致")
        proxy_profile_id = bound_proxy_id
        username = str(account_row["username"] or "")
        saved_profile = _json_loads(account_row["profile_json"], {})
        deleted_platforms = saved_profile.get("platform_deletions") if isinstance(saved_profile, dict) else {}
        if not isinstance(deleted_platforms, dict):
            deleted_platforms = {}
        if login_platform == "instagram" and _instagram_login_metadata(saved_profile).get("logged_in"):
            raise ValueError("当前 Chrome Profile 已登录 Instagram")
        if login_platform == "tiktok" and not bool(deleted_platforms.get("tiktok")):
            raise ValueError("当前 Chrome Profile 已登录 TikTok")
        if start_platform == "instagram" and not login_platform and not _instagram_login_metadata(saved_profile).get("logged_in"):
            raise ValueError("当前 Chrome Profile 未检测到 Instagram 登录")
        feishu_user_id = str(account_row["feishu_user_id"] or "")
        feishu_user_name = str(account_row["feishu_user_name"] or "")
        feishu_avatar_url = str(account_row["feishu_avatar_url"] or "")
    else:
        username = _clean_text(payload.get("username"), 120).lstrip("@")
        feishu_user_id = _clean_text(payload.get("feishu_user_id"), 256)
        feishu_user_name = _clean_text(payload.get("feishu_user_name"), 160)
        feishu_avatar_url = _clean_text(payload.get("feishu_avatar_url"), 2000)
        if not feishu_user_id:
            raise ValueError("新增账号必须选择飞书用户")
    if not proxy_profile_id:
        raise ValueError("proxy_profile_id is required")
    try:
        preflight_payload = {"account_id": account_id} if account_id else {"proxy_profile_id": proxy_profile_id, "bind": True}
        preflight = check_binding(preflight_payload, require_account=bool(account_id))
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE proxy_profiles SET status = ?, parse_error = COALESCE(NULLIF(parse_error, ''), ?), updated_at = ? WHERE id = ?", (STATUS_ERROR, str(exc), now_iso(), proxy_profile_id))
            conn.commit()
        raise
    if not preflight.get("allowed"):
        reason = str(preflight.get("reason") or "代理 IP 校验未通过")
        with connect() as conn:
            conn.execute("UPDATE proxy_profiles SET status = ?, parse_error = ?, updated_at = ? WHERE id = ?", (STATUS_ERROR, reason, now_iso(), proxy_profile_id))
            conn.commit()
        raise ValueError(reason)
    now = now_iso()
    with connect() as conn:
        pool = conn.execute(
            "SELECT * FROM proxy_profiles WHERE id = ? AND deleted_at = ''",
            (proxy_profile_id,),
        ).fetchone()
        if not pool:
            raise ValueError("proxy profile not found")
        if _clean_status(pool["status"]) != STATUS_ACTIVE:
            raise ValueError(f"代理状态为 {_clean_status(pool['status'])}")
        if account_id:
            for active_row in _active_sessions(conn):
                if int(active_row["account_id"] or 0) == account_id:
                    raise ValueError("账号已经处于唤醒状态")
        owner = "automation" if payload.get("_automation") else "manual"
        current_job_id = _clean_text(payload.get("_current_job_id"), 80) if owner == "automation" else ""
        slot = _allocate_session_slot(conn, owner)
        pending_name = f"pending-{proxy_profile_id}-{slot}-{int(time.time())}" if not username else username
        profile = _deep_merge(_isolation_profile(pending_name, proxy_profile_id, pool), saved_profile) if account_id else _isolation_profile(pending_name, proxy_profile_id, pool)
        profile_key = str((profile.get("isolation") or {}).get("browser_profile_key") or "")
        slot_ports = _slot_ports(slot)
        channel_url = "" if _hidden_automation_slot(slot) else _public_novnc_url(int(slot_ports["novnc_port"]))
        cur = conn.execute(
            """
            INSERT INTO browser_sessions (
                slot, proxy_profile_id, account_id, username, status, channel_url, runtime_id,
                pid, xvfb_pid, x11vnc_pid, websockify_pid, display, vnc_port, novnc_port,
                debug_port, owner, current_job_id, feishu_user_id, feishu_user_name,
                feishu_avatar_url, profile_key, user_data_dir, login_platform, last_activity_at,
                last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                slot,
                proxy_profile_id,
                account_id or None,
                username,
                "starting",
                channel_url,
                RUNTIME_ID,
                str(slot_ports["display"]),
                int(slot_ports["vnc_port"]),
                int(slot_ports["novnc_port"]),
                int(slot_ports["debug_port"]),
                owner,
                current_job_id,
                feishu_user_id,
                feishu_user_name,
                feishu_avatar_url,
                profile_key,
                str((profile.get("isolation") or {}).get("user_data_dir") or ""),
                login_platform,
                now,
                now,
                now,
            ),
        )
        session_id = int(cur.lastrowid)
        conn.commit()
        try:
            log_dir = _abs_workspace_path(f"data/tiktok_browser_sessions/{session_id}")
            channel = _launch_observation_channel(slot, session_id, log_dir)
            # Persist the channel as soon as it exists. If browser launch or
            # the browser-side IP check fails, the failure handler can then
            # terminate the exact processes that were created for this session.
            conn.execute(
                """
                UPDATE browser_sessions
                SET channel_url = ?,
                    xvfb_pid = ?,
                    x11vnc_pid = ?,
                    websockify_pid = ?,
                    display = ?,
                    vnc_port = ?,
                    novnc_port = ?,
                    debug_port = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(channel["channel_url"]),
                    int(channel["xvfb_pid"]),
                    int(channel["x11vnc_pid"]),
                    int(channel["websockify_pid"]),
                    str(channel["display"]),
                    int(channel["vnc_port"]),
                    int(channel["novnc_port"]),
                    int(slot_ports["debug_port"]),
                    now_iso(),
                    session_id,
                ),
            )
            conn.commit()
            login_start_urls = {
                "tiktok": "https://www.tiktok.com/login?lang=en",
                "instagram": "https://www.instagram.com/accounts/login/",
            }
            start_url = login_start_urls[start_platform] if login_platform else (PLATFORM_START_URLS[start_platform] if account_id else login_start_urls["tiktok"])
            pid, user_data_dir = _launch_browser_for_session(
                profile,
                pool,
                session_id,
                str(channel["display"]),
                int(slot_ports["debug_port"]),
                start_url,
            )
            conn.execute(
                "UPDATE browser_sessions SET pid = ?, user_data_dir = ?, updated_at = ? WHERE id = ?",
                (pid, user_data_dir, now_iso(), session_id),
            )
            conn.commit()
            time.sleep(2.0)
            if not _pid_alive(pid):
                raise ValueError("Chrome 启动后立即退出，请检查 browser.err.log")
            _wait_for_port(int(slot_ports["debug_port"]), "Chrome CDP", timeout=10.0)
            browser_observed_ip = _detect_browser_exit_ip(int(slot_ports["debug_port"]))
            expected_exit_ip = str(pool["expected_exit_ip"] or "").strip()
            if str(pool["source_type"] or "") != "direct" and browser_observed_ip != expected_exit_ip:
                reason = f"浏览器出口 IP {browser_observed_ip} 与绑定 IP {expected_exit_ip} 不一致"
                conn.execute(
                    "UPDATE proxy_profiles SET status = ?, parse_error = ?, detected_exit_ip = ?, detected_at = ?, updated_at = ? WHERE id = ?",
                    (STATUS_ERROR, reason, browser_observed_ip, now_iso(), now_iso(), proxy_profile_id),
                )
                if account_id:
                    conn.execute(
                        "UPDATE tiktok_accounts SET last_checked_ip = ?, last_check_status = '阻断', last_error = ?, updated_at = ? WHERE id = ?",
                        (browser_observed_ip, reason, now_iso(), account_id),
                    )
                conn.commit()
                raise ValueError(reason)
            if account_id:
                cookies = _tiktok_profile_cookies(user_data_dir)
                account_info_url = os.getenv(
                    "TIKTOK_ACCOUNT_INFO_URL",
                    "https://www.tiktok.com/passport/web/account/info/?aid=1459&app_language=en&device_platform=web_pc",
                )
                identity_ok, identity_body, _ = _proxy_json_with_cookies(
                    account_info_url, int(pool["local_port"] or 0), cookies
                )
                identity = _tiktok_identity(identity_body) if identity_ok else {}
                if identity:
                    conn.execute(
                        """UPDATE tiktok_accounts
                           SET display_name = COALESCE(NULLIF(?, ''), display_name),
                               tiktok_avatar_url = COALESCE(NULLIF(?, ''), tiktok_avatar_url),
                               updated_at = ?
                           WHERE id = ?""",
                        (
                            identity.get("display_name", ""),
                            identity.get("avatar_url", ""),
                            now_iso(),
                            account_id,
                        ),
                    )
                stored_avatar = str(account_row["tiktok_avatar_url"] or "")
                if not stored_avatar.startswith("/api/proxy/accounts/avatar/"):
                    try:
                        avatar_path = _account_avatar_path(account_id)
                        avatar_url = (
                            f"/api/proxy/accounts/avatar/{account_id}?v={avatar_path.stat().st_mtime_ns}"
                            if avatar_path.is_file()
                            else _write_account_avatar(
                                account_id,
                                _browser_account_avatar(int(slot_ports["debug_port"])),
                            )
                        )
                        conn.execute(
                            "UPDATE tiktok_accounts SET tiktok_avatar_url = ?, updated_at = ? WHERE id = ?",
                            (avatar_url, now_iso(), account_id),
                        )
                    except Exception:
                        pass
            preflight["browser_observed_ip"] = browser_observed_ip
            conn.execute(
                """
                UPDATE browser_sessions
                SET status = 'observing',
                    channel_url = ?,
                    pid = ?,
                    xvfb_pid = ?,
                    x11vnc_pid = ?,
                    websockify_pid = ?,
                    display = ?,
                    vnc_port = ?,
                    novnc_port = ?,
                    debug_port = ?,
                    user_data_dir = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(channel["channel_url"]),
                    pid,
                    int(channel["xvfb_pid"]),
                    int(channel["x11vnc_pid"]),
                    int(channel["websockify_pid"]),
                    str(channel["display"]),
                    int(channel["vnc_port"]),
                    int(channel["novnc_port"]),
                    int(slot_ports["debug_port"]),
                    user_data_dir,
                    now_iso(),
                    session_id,
                ),
            )
            conn.commit()
        except Exception as exc:
            err = str(exc)
            row = conn.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
            if row:
                _terminate_session_processes(row)
                _remove_unbound_session_profile(row)
            conn.execute("UPDATE browser_sessions SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?", (err, now_iso(), session_id))
            if account_id:
                conn.execute("UPDATE tiktok_accounts SET status = ?, last_error = ?, updated_at = ? WHERE id = ?", (ACCOUNT_STATUS_ERROR, err, now_iso(), account_id))
            conn.commit()
            raise ValueError(f"观测浏览器启动失败：{err}")
        session = _session_by_id(conn, session_id)
    return {"session": _row_to_session(session), "preflight": preflight, **list_state()}


def stop_login_session(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = int(payload.get("session_id") or payload.get("id") or 0)
    mark_failed = bool(payload.get("failed") or payload.get("login_failed"))
    reason = _clean_text(payload.get("reason") or ("登录失败" if mark_failed else "手动关闭观测通道"), 1000)
    if not session_id:
        raise ValueError("session_id is required")
    now = now_iso()
    with connect() as conn:
        row = _session_by_id(conn, session_id)
        if row["current_job_id"] and not payload.get("force"):
            raise ValueError("账号正在发布，确认终止任务后才能休眠")
        _terminate_session_processes(row)
        _remove_unbound_session_profile(row)
        status = "failed" if mark_failed else "stopped"
        conn.execute("UPDATE browser_sessions SET status = ?, last_error = ?, updated_at = ? WHERE id = ?", (status, reason, now, session_id))
        account_id = int(row["account_id"] or 0)
        if account_id and mark_failed:
            conn.execute("UPDATE tiktok_accounts SET status = ?, last_error = ?, updated_at = ? WHERE id = ?", (ACCOUNT_STATUS_ERROR, reason, now, account_id))
        conn.commit()
    return list_state()


def open_observation_platform(payload: dict[str, Any]) -> dict[str, Any]:
    """Open a platform tab in an existing account observation browser."""
    session_id = int(payload.get("session_id") or payload.get("id") or 0)
    platform = _clean_text(payload.get("platform"), 32).lower()
    login_platform = bool(payload.get("login_platform"))
    targets = {
        "tiktok": "https://www.tiktok.com/?lang=en",
        "instagram": "https://www.instagram.com/",
    }
    if not session_id:
        raise ValueError("session_id is required")
    if platform not in targets:
        raise ValueError("仅支持打开 TikTok 或 Instagram 观测页面")
    with connect() as conn:
        row = _session_by_id(conn, session_id)
        if row["status"] not in {"starting", "running", "observing"}:
            raise ValueError("观测通道当前不可用，请先唤醒账号")
        account_id = int(row["account_id"] or 0)
        if not account_id:
            raise ValueError("未绑定账号的登录通道不能打开平台观测")
        account = conn.execute(
            "SELECT profile_json FROM tiktok_accounts WHERE id = ? AND deleted_at = ''",
            (account_id,),
        ).fetchone()
        if not account:
            raise ValueError("account not found")
        profile = _json_loads(account["profile_json"], {})
        deleted_platforms = profile.get("platform_deletions") if isinstance(profile, dict) else {}
        if login_platform and platform == "tiktok" and not bool((deleted_platforms or {}).get("tiktok")):
            raise ValueError("当前 Chrome Profile 已登录 TikTok")
        if login_platform and platform == "instagram" and _instagram_login_metadata(profile)["logged_in"]:
            raise ValueError("当前 Chrome Profile 已登录 Instagram")
        if not login_platform and platform == "tiktok" and bool((deleted_platforms or {}).get("tiktok")):
            raise ValueError("TikTok 登录资料已删除")
        if not login_platform and platform == "instagram" and not _instagram_login_metadata(profile)["logged_in"]:
            raise ValueError("当前 Chrome Profile 未检测到 Instagram 登录")
        debug_port = int(row["debug_port"] or 0)
        if not debug_port:
            raise ValueError("观测浏览器调试端口不可用")
    if login_platform:
        preflight = check_binding({"account_id": account_id}, require_account=True)
        if not preflight.get("allowed"):
            raise ValueError(str(preflight.get("reason") or "代理 IP 校验未通过"))
        with connect() as conn:
            conn.execute(
                "UPDATE browser_sessions SET login_platform = ?, updated_at = ? WHERE id = ?",
                (platform, now_iso(), session_id),
            )
            conn.commit()
    page = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            if not browser.contexts:
                raise ValueError("Chrome 没有可用的浏览器上下文")
            # Reuse the browser's initial business page. A platform switch must
            # not leave a startup TikTok tab plus a newly-created Instagram tab.
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()
            target_url = (
                "https://www.tiktok.com/login?lang=en"
                if login_platform and platform == "tiktok"
                else "https://www.instagram.com/accounts/login/"
                if login_platform
                else targets[platform]
            )
            response = page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            if response is not None and not response.ok:
                raise ValueError(f"{platform} 页面打开失败：HTTP {response.status}")
            page.bring_to_front()
    except Exception as exc:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        raise ValueError(f"打开 {platform} 观测页面失败：{exc}") from exc
    with connect() as conn:
        conn.execute(
            "UPDATE browser_sessions SET last_activity_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), session_id),
        )
        conn.commit()
        session = _session_by_id(conn, session_id)
    return {"session": _row_to_session(session), "platform": platform, "login_platform": login_platform, **list_state()}


def start_automation_session(account_id: int, job_id: str, start_platform: str = "tiktok") -> dict[str, Any]:
    return start_login_session(
        {"account_id": account_id, "_automation": True, "_current_job_id": job_id, "start_platform": start_platform}
    )


def claim_observation_session_for_job(account_id: int, session_id: int, job_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    conn = connect()
    try:
        _active_sessions(conn)
        row = _session_by_id(conn, session_id)
        if int(row["account_id"] or 0) != int(account_id):
            raise ValueError("观测通道不属于当前账号")
        if row["status"] not in {"starting", "running", "observing"}:
            return None
        current_job_id = str(row["current_job_id"] or "")
        if current_job_id and current_job_id != job_id:
            raise ValueError("观测通道正在执行其他任务")
        conn.execute(
            "UPDATE browser_sessions SET current_job_id = ?, last_activity_at = ?, updated_at = ? WHERE id = ?",
            (_clean_text(job_id, 80), now_iso(), now_iso(), session_id),
        )
        conn.commit()
        return _row_to_session(_session_by_id(conn, session_id))
    finally:
        conn.close()


def release_observation_session_job(session_id: int, job_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    conn = connect()
    try:
        row = _session_by_id(conn, session_id)
        if str(row["current_job_id"] or "") not in {"", str(job_id)}:
            raise ValueError("观测通道正在执行其他任务")
        conn.execute(
            "UPDATE browser_sessions SET current_job_id = '', last_activity_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), session_id),
        )
        conn.commit()
        return _row_to_session(_session_by_id(conn, session_id))
    finally:
        conn.close()


def finish_automation_session(session_id: int, reason: str = "自动发布任务结束") -> dict[str, Any]:
    return stop_login_session({"session_id": session_id, "force": True, "reason": reason})


def handoff_automation_session(session_id: int, reason: str) -> dict[str, Any]:
    now = now_iso()
    with connect() as conn:
        row = _session_by_id(conn, session_id)
        conn.execute(
            "UPDATE browser_sessions SET owner = 'manual_review', current_job_id = '', last_activity_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (now, _clean_text(reason, 1000), now, session_id),
        )
        conn.commit()
        return _row_to_session(_session_by_id(conn, session_id))


def _inspect_login_session(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = int(payload.get("session_id") or payload.get("id") or 0)
    if not session_id:
        raise ValueError("session_id is required")
    with connect() as conn:
        row = _session_by_id(conn, session_id)
        if row["status"] not in {"starting", "running", "observing"}:
            return {"active": False, "bound": False, "status": row["status"], "reason": str(row["last_error"] or "登录通道已结束")}
        if row["account_id"]:
            account = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (row["account_id"],)).fetchone()
            login_platform = str(row["login_platform"] or "").lower()
            if not login_platform:
                return {"active": True, "bound": True, "status": "bound", "account": _row_to_account(account) if account else None, **list_state()}
            user_data_dir = str(row["user_data_dir"] or "")
            if not account or not user_data_dir:
                return {"active": True, "bound": False, "status": "waiting", "reason": "浏览器 profile 尚未就绪"}
            cookie_names = ("sessionid",) if login_platform == "instagram" else ("sessionid", "sessionid_ss", "sid_tt", "sid_guard")
            login = _platform_login_metadata(
                {"isolation": {"user_data_dir": user_data_dir}},
                "%instagram.com%" if login_platform == "instagram" else "%tiktok.com%",
                cookie_names,
            )
            if not login.get("logged_in"):
                return {"active": True, "bound": False, "status": "waiting_login", "platform": login_platform}
            profile = _json_loads(account["profile_json"], {})
            if not isinstance(profile, dict):
                profile = {}
            deleted_platforms = profile.get("platform_deletions")
            if not isinstance(deleted_platforms, dict):
                deleted_platforms = {}
            deleted_platforms.pop(login_platform, None)
            profile["platform_deletions"] = deleted_platforms
            updated_at = now_iso()
            conn.execute(
                """UPDATE tiktok_accounts
                   SET profile_json = ?, last_login_at = ?, last_error = '', updated_at = ?
                   WHERE id = ?""",
                (json.dumps(profile, ensure_ascii=False, separators=(",", ":")), updated_at, updated_at, int(row["account_id"])),
            )
            conn.execute(
                "UPDATE browser_sessions SET login_platform = '', updated_at = ? WHERE id = ?",
                (updated_at, session_id),
            )
            conn.commit()
            bootstrap = {}
            if login_platform == "instagram":
                try:
                    bootstrap = bootstrap_instagram_profile(int(row["account_id"]), session_id)
                except Exception as exc:
                    bootstrap = {"configured": False, "reason": _clean_text(exc, 500)}
            result = {
                "active": True,
                "bound": True,
                "status": "bound",
                "platform": login_platform,
                "account": get_account(int(row["account_id"])),
                **list_state(),
            }
            if bootstrap and not bootstrap.get("configured"):
                result["reason"] = str(bootstrap.get("reason") or "Instagram 登录已保存，Reels 入口将在下次打开时补齐")
            return result
        pool = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (row["proxy_profile_id"],)).fetchone()
        user_data_dir = str(row["user_data_dir"] or "")
    if not pool or not user_data_dir:
        return {"active": True, "bound": False, "status": "waiting", "reason": "浏览器 profile 尚未就绪"}

    cookies = _tiktok_profile_cookies(user_data_dir)
    login_cookie_names = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}
    if not any(cookies.get(name) for name in login_cookie_names):
        return {"active": True, "bound": False, "status": "waiting_login"}

    account_info_url = os.getenv(
        "TIKTOK_ACCOUNT_INFO_URL",
        "https://www.tiktok.com/passport/web/account/info/?aid=1459&app_language=en&device_platform=web_pc",
    )
    ok, body, error = _proxy_json_with_cookies(account_info_url, int(pool["local_port"] or 0), cookies)
    identity = _tiktok_identity(body) if ok else {}
    if not identity:
        return {
            "active": True,
            "bound": False,
            "status": "login_detected",
            "reason": error or "已检测到 TikTok 登录 Cookie，正在读取账号身份",
        }

    username = _normal_username(identity["username"])
    with connect() as conn:
        existing = conn.execute("SELECT * FROM tiktok_accounts WHERE username = ?", (username,)).fetchone()
        if existing:
            return {
                "active": True,
                "bound": False,
                "status": "duplicate_account",
                "reason": f"@{username} 已在账号池中，请关闭此次通道并从账号列表唤醒",
            }
    result = upsert_account(
        {
            "username": username,
            "display_name": identity.get("display_name", ""),
            "tiktok_avatar_url": identity.get("avatar_url", ""),
            "feishu_user_id": str(row["feishu_user_id"] or ""),
            "feishu_user_name": str(row["feishu_user_name"] or ""),
            "feishu_avatar_url": str(row["feishu_avatar_url"] or ""),
            "proxy_profile_id": int(pool["id"]),
            "status": ACCOUNT_STATUS_ACTIVE,
            "session_id": session_id,
            "notes": "TikTok 登录成功后自动绑定",
        }
    )
    account_id = int(result["account"]["id"])
    avatar_url = ""
    try:
        avatar_body = _browser_account_avatar(int(row["debug_port"] or 0))
        if avatar_body:
            avatar_url = _write_account_avatar(account_id, avatar_body)
    except Exception:
        pass
    updated_at = now_iso()
    with connect() as conn:
        conn.execute(
            """UPDATE tiktok_accounts
               SET last_login_at = ?,
                   last_error = '',
                   tiktok_avatar_url = COALESCE(NULLIF(?, ''), tiktok_avatar_url),
                   updated_at = ?
               WHERE id = ?""",
            (updated_at, avatar_url, updated_at, account_id),
        )
        conn.commit()
    return {
        "active": True,
        "bound": True,
        "status": "bound",
        "account": get_account(account_id),
        **list_state(),
    }


def inspect_login_session(payload: dict[str, Any]) -> dict[str, Any]:
    with _LOGIN_CAPTURE_LOCK:
        return _inspect_login_session(payload)


def capture_pending_login_sessions() -> dict[str, Any]:
    with connect() as conn:
        session_ids = [
            int(row["id"])
            for row in conn.execute(
                """SELECT id FROM browser_sessions
                   WHERE status = 'observing' AND owner = 'manual'
                     AND (account_id IS NULL OR login_platform <> '')
                   ORDER BY id"""
            ).fetchall()
        ]
    bound: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for session_id in session_ids:
        try:
            result = inspect_login_session({"session_id": session_id})
            if result.get("bound"):
                account = result.get("account") or {}
                bound.append({"session_id": session_id, "account_id": account.get("id"), "username": account.get("username", "")})
        except Exception as exc:
            errors.append({"session_id": session_id, "error": str(exc)})
    return {"attempted": len(session_ids), "bound": bound, "errors": errors}


def update_account_status(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or payload.get("id") or 0)
    if not account_id:
        raise ValueError("account_id is required")
    now = now_iso()
    with connect() as conn:
        if not conn.execute("SELECT id FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone():
            raise ValueError("account not found")
        conn.execute(
            """
            UPDATE tiktok_accounts
            SET status = COALESCE(NULLIF(?, ''), status),
                last_login_at = COALESCE(NULLIF(?, ''), last_login_at),
                last_collect_at = COALESCE(NULLIF(?, ''), last_collect_at),
                last_error = COALESCE(NULLIF(?, ''), last_error),
                updated_at = ?
            WHERE id = ?
            """,
            (
                _clean_account_status(payload.get("status"), ""),
                _clean_text(payload.get("last_login_at"), 80),
                _clean_text(payload.get("last_collect_at"), 80),
                _clean_text(payload.get("last_error"), 1000),
                now,
                account_id,
            ),
        )
        conn.commit()
    return {"account": get_account(account_id), **list_state()}
