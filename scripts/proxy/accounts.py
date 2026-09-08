"""Account bindings, browser profiles, and login-session workflows."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from urllib.parse import urlparse

from proxy import settings
from proxy.nodes import _clean_account_status, _clean_status, _clean_text, _json_loads
from proxy.repository import connect
from proxy.runtime import (
    _abs_workspace_path,
    _browser_account_avatar,
    _detect_browser_exit_ip,
    _hidden_automation_slot,
    _launch_browser_for_session,
    _launch_observation_channel,
    _pid_alive,
    _proxy_json_with_cookies,
    _public_novnc_url,
    _slot_ports,
    _terminate_session_processes,
    _tiktok_identity,
    _tiktok_profile_cookies,
    _wait_for_port,
)
from proxy.settings import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_ERROR,
    ACCOUNT_STATUS_PAUSED,
    PLATFORM_START_URLS,
    RUNTIME_ID,
    STATUS_ACTIVE,
    STATUS_ERROR,
    TIKTOK_BROWSER_ACCEPT_LANGUAGE,
    TIKTOK_BROWSER_LOCALE,
    browser_max_slots,
    now_iso,
)
from proxy.state import (
    _active_sessions,
    _allocate_session_slot,
    _browser_profile_dir,
    _instagram_login_metadata,
    _platform_login_metadata,
    _remove_unbound_session_profile,
    _row_to_account,
    _row_to_session,
    list_state,
)
from proxy.pools import (
    _direct_login_pool_id,
    _profile_without_proxy_binding,
    _normal_username,
    account_proxy_bound,
    check_binding,
    require_account_proxy_bound,
)


_LOGIN_CAPTURE_LOCK = threading.Lock()


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
    return settings.DATA_DIR / "tiktok_account_avatars" / f"{account_id}.img"


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


def _session_by_id(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise ValueError("browser session not found")
    return row


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
