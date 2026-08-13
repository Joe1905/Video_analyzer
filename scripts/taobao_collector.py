#!/usr/bin/env python3
"""User-scoped Taobao evidence collection on top of the existing proxy pool.

This module deliberately reuses the pool's local proxy, Chromium and noVNC
primitives, but keeps Taobao identities in separate tables and a separate port
range.  A Taobao login profile belongs to one Feishu user only.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import ProxyHandler, Request, build_opener

import proxy_pool


MAX_IMAGES = max(1, min(12, int(os.getenv("TAOBAO_ARCHIVE_MAX_IMAGES", "8") or "8")))
MAX_IMAGE_BYTES = max(1024 * 1024, int(os.getenv("TAOBAO_ARCHIVE_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)) or str(8 * 1024 * 1024)))
MAX_SLOTS = max(1, int(os.getenv("TAOBAO_BROWSER_MAX_SLOTS", "4") or "4"))
MAX_SEARCH_CANDIDATES = max(10, min(120, int(os.getenv("TAOBAO_SEARCH_MAX_CANDIDATES", "80") or "80")))
NOVNC_PORT_START = max(1024, int(os.getenv("TAOBAO_NOVNC_PORT_START", "6101") or "6101"))
VNC_PORT_START = max(1024, int(os.getenv("TAOBAO_VNC_PORT_START", "5911") or "5911"))
CDP_PORT_START = max(1024, int(os.getenv("TAOBAO_CDP_PORT_START", "19301") or "19301"))
XVFB_DISPLAY_START = max(100, int(os.getenv("TAOBAO_XVFB_DISPLAY_START", "120") or "120"))
PROFILE_ROOT = os.getenv("TAOBAO_BROWSER_PROFILE_ROOT", "data/taobao_browser_profiles").rstrip("/")
ARCHIVE_ROOT = Path(os.getenv("TAOBAO_ARCHIVE_DIR", str(proxy_pool.DATA_DIR / "taobao_archives")))
_LOCK = threading.RLock()


def _connect():
    conn = proxy_pool.connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS taobao_profiles (
            owner_id TEXT PRIMARY KEY,
            feishu_user_id TEXT NOT NULL,
            feishu_user_name TEXT NOT NULL DEFAULT '',
            proxy_profile_id INTEGER NOT NULL REFERENCES proxy_profiles(id) ON DELETE RESTRICT,
            profile_key TEXT NOT NULL UNIQUE,
            user_data_dir TEXT NOT NULL,
            fingerprint_id TEXT NOT NULL,
            profile_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_taobao_profiles_proxy ON taobao_profiles(proxy_profile_id);
        CREATE TABLE IF NOT EXISTS taobao_proxy_bindings (
            owner_id TEXT PRIMARY KEY REFERENCES taobao_profiles(owner_id) ON DELETE CASCADE,
            slot INTEGER NOT NULL UNIQUE,
            local_port INTEGER NOT NULL UNIQUE,
            upstream_mode TEXT NOT NULL DEFAULT 'global',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS taobao_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL REFERENCES taobao_profiles(owner_id) ON DELETE CASCADE,
            slot INTEGER NOT NULL,
            proxy_profile_id INTEGER NOT NULL REFERENCES proxy_profiles(id) ON DELETE RESTRICT,
            status TEXT NOT NULL DEFAULT 'starting',
            channel_url TEXT NOT NULL DEFAULT '',
            pid INTEGER NOT NULL DEFAULT 0,
            xvfb_pid INTEGER NOT NULL DEFAULT 0,
            x11vnc_pid INTEGER NOT NULL DEFAULT 0,
            websockify_pid INTEGER NOT NULL DEFAULT 0,
            display TEXT NOT NULL DEFAULT '',
            vnc_port INTEGER NOT NULL DEFAULT 0,
            novnc_port INTEGER NOT NULL DEFAULT 0,
            debug_port INTEGER NOT NULL DEFAULT 0,
            current_job_id TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_taobao_sessions_owner ON taobao_sessions(owner_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_taobao_sessions_status ON taobao_sessions(status);
        """
    )
    conn.commit()
    return conn


def _owner(user: dict[str, Any]) -> tuple[str, str, str]:
    owner_id = str(user.get("id") or "").strip()
    if not owner_id or owner_id == "public":
        raise ValueError("请先在左侧导航切换到飞书用户，再使用淘宝采集")
    return owner_id, str(user.get("feishuId") or owner_id).strip(), str(user.get("name") or "飞书用户").strip()


def _safe_key(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")
    return cleaned[:40] or "user"


def _profile_payload(owner_id: str, feishu_id: str, proxy_id: int) -> tuple[str, str, str, dict[str, Any]]:
    digest = hashlib.sha256(f"taobao:{owner_id}:{feishu_id}:{proxy_id}".encode("utf-8")).hexdigest()
    key = f"tb-{_safe_key(owner_id)}-{digest[:10]}"
    chrome_version = 124 + (int(digest[10:12], 16) % 5)
    viewport = ((1280, 900), (1365, 900), (1440, 960), (1536, 960))[int(digest[12:14], 16) % 4]
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
    )
    profile = {
        "isolation": {
            "browser_profile_key": key,
            "user_data_dir": f"{PROFILE_ROOT}/{key}/user-data",
            "browser_context": "per_feishu_user_required",
            "storage_state": "per_feishu_user_required",
            "cookie_jar": "per_feishu_user_required",
            "local_storage": "per_feishu_user_required",
            "indexed_db": "per_feishu_user_required",
        },
        "browser_settings": {
            "locale": "zh-CN",
            "accept_language": "zh-CN,zh,en-US,en",
            "user_agent": user_agent,
            "window_size": list(viewport),
            "disable_non_proxied_udp": True,
        },
    }
    return key, str(profile["isolation"]["user_data_dir"]), f"taobao-chrome-{digest[:12]}", profile


def _allocate_proxy(conn, owner_id: str):
    row = conn.execute(
        """
        SELECT p.* FROM proxy_profiles p
        WHERE p.deleted_at = '' AND p.status = ? AND p.parse_status = 'ok'
          AND p.source_type <> 'direct'
          AND NOT EXISTS (
              SELECT 1 FROM tiktok_accounts a
              WHERE a.proxy_profile_id = p.id AND a.proxy_bound = 1 AND a.deleted_at = ''
          )
          AND NOT EXISTS (
              SELECT 1 FROM taobao_profiles t
              WHERE t.proxy_profile_id = p.id AND t.owner_id <> ?
          )
        ORDER BY CASE WHEN p.port_scope = ? THEN 0 ELSE 1 END, p.id
        LIMIT 1
        """,
        (proxy_pool.STATUS_ACTIVE, owner_id, proxy_pool.PORT_SCOPE_TAOBAO),
    ).fetchone()
    if not row:
        raise ValueError("没有可分配的独立代理出口；请先在账号运营台添加启用的专用 IP 池")
    return row


def _ready_proxy(conn, profile: dict[str, Any]):
    row = conn.execute(
        """SELECT * FROM proxy_profiles
           WHERE id = ? AND deleted_at = '' AND status = ? AND parse_status = 'ok'
             AND port_scope = ?""",
        (int(profile["proxy_profile_id"]), proxy_pool.STATUS_ACTIVE, proxy_pool.PORT_SCOPE_TAOBAO),
    ).fetchone()
    if not row:
        return None
    start_port, end_port = proxy_pool._port_range(proxy_pool.PORT_SCOPE_TAOBAO)
    return row if start_port <= int(row["local_port"] or 0) <= end_port else None


def _sync_taobao_listener(binding: dict[str, Any]) -> None:
    port = int(binding["local_port"])
    # A migrated user may already have a working listener from the previous
    # implementation. Reusing it avoids an unnecessary Mihomo reload and keeps
    # the active browser's proxy connection stable.
    if proxy_pool._port_open("127.0.0.1", port, timeout=0.5):
        return
    path = proxy_pool._mihomo_config_path()
    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    lines = original.decode("utf-8").splitlines(keepends=True)
    value = {"name": f"taobao-user-{int(binding['slot'])}", "type": "mixed", "port": port, "proxy": proxy_pool.SYSTEM_PROXY_DIALER}
    updated = proxy_pool._replace_mihomo_yaml_item(lines, "listeners", 900000 + int(binding["slot"]), value, match_port=port)
    body = "".join(updated).encode("utf-8")
    if body == original:
        return
    proxy_pool._atomic_write(path, body, mode)
    try:
        proxy_pool._reload_mihomo_config()
        proxy_pool._wait_for_port(port, "淘宝代理监听", timeout=10.0)
    except Exception:
        proxy_pool._atomic_write(path, original, mode)
        try:
            proxy_pool._reload_mihomo_config()
        except Exception:
            pass
        raise


def _ensure_taobao_proxy(owner_id: str) -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM taobao_proxy_bindings WHERE owner_id = ?", (owner_id,)).fetchone()
        if row:
            binding = dict(row)
        else:
            start_port, end_port = proxy_pool._port_range(proxy_pool.PORT_SCOPE_TAOBAO)
            used = {int(item["slot"]) for item in conn.execute("SELECT slot FROM taobao_proxy_bindings")}
            legacy = conn.execute(
                """SELECT p.local_port FROM taobao_profiles t
                   JOIN proxy_profiles p ON p.id = t.proxy_profile_id
                   WHERE t.owner_id = ? AND p.deleted_at = '' AND p.port_scope = ?""",
                (owner_id, proxy_pool.PORT_SCOPE_TAOBAO),
            ).fetchone()
            legacy_port = int(legacy["local_port"] or 0) if legacy else 0
            slot = legacy_port - start_port + 1 if start_port <= legacy_port <= end_port and (legacy_port - start_port + 1) not in used else 0
            slot = slot or next((item for item in range(1, end_port - start_port + 2) if item not in used), 0)
            if not slot:
                raise ValueError("淘宝独立代理端口已用完，请联系管理员扩容")
            now = proxy_pool.now_iso()
            port = start_port + slot - 1
            conn.execute(
                "INSERT INTO taobao_proxy_bindings (owner_id, slot, local_port, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (owner_id, slot, port, now, now),
            )
            conn.commit()
            binding = {"owner_id": owner_id, "slot": slot, "local_port": port, "upstream_mode": "global", "status": "active"}
    _sync_taobao_listener(binding)
    return binding


def _has_taobao_login_state(profile: dict[str, Any]) -> bool:
    isolation = profile.get("isolation") if isinstance(profile, dict) else {}
    user_data_value = str((isolation or {}).get("user_data_dir") or "").strip()
    if not user_data_value:
        return False
    user_data_dir = proxy_pool._abs_workspace_path(user_data_value)
    cookie_names = ("cookie2", "_tb_token_", "tracknick", "lgc", "unb")
    placeholders = ", ".join("?" for _ in cookie_names)
    for cookie_path in (user_data_dir / "Default" / "Cookies", user_data_dir / "Default" / "Network" / "Cookies"):
        if not cookie_path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True, timeout=1)
            try:
                row = conn.execute(
                    f"SELECT 1 FROM cookies WHERE lower(host_key) LIKE ? AND lower(name) IN ({placeholders}) LIMIT 1",
                    ("%taobao.com%", *cookie_names),
                ).fetchone()
            finally:
                conn.close()
            if row:
                return True
        except sqlite3.Error:
            continue
    return False


def _profile_for_user(user: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
    owner_id, feishu_id, name = _owner(user)
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM taobao_profiles WHERE owner_id = ?", (owner_id,)).fetchone()
        if row:
            return dict(row)
        if not create:
            return None
        # Legacy column retained for database compatibility only; Taobao no
        # longer reads this record for routing or allocation.
        legacy = conn.execute("SELECT id FROM proxy_profiles WHERE deleted_at = '' ORDER BY id LIMIT 1").fetchone()
        if not legacy:
            raise ValueError("淘宝代理系统尚未初始化")
        key, data_dir, fingerprint_id, profile = _profile_payload(owner_id, feishu_id, 0)
        now = proxy_pool.now_iso()
        conn.execute(
            """INSERT INTO taobao_profiles (
                   owner_id, feishu_user_id, feishu_user_name, proxy_profile_id,
                   profile_key, user_data_dir, fingerprint_id, profile_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, feishu_id, name, int(legacy["id"]), key, data_dir, fingerprint_id,
             json.dumps(profile, ensure_ascii=False, separators=(",", ":")), now, now),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM taobao_profiles WHERE owner_id = ?", (owner_id,)).fetchone())


def _slot_ports(slot: int) -> dict[str, int | str]:
    index = slot - 1
    return {
        "display": f":{XVFB_DISPLAY_START + index}",
        "vnc_port": VNC_PORT_START + index,
        "novnc_port": NOVNC_PORT_START + index,
        "debug_port": CDP_PORT_START + index,
    }


def _ports_available(ports: dict[str, int | str]) -> bool:
    display = str(ports["display"]).lstrip(":")
    display_socket = Path(f"/tmp/.X11-unix/X{display}")
    if display_socket.exists() and _display_socket_active(display_socket):
        return False
    return not any(proxy_pool._port_open("127.0.0.1", int(ports[key]), timeout=0.15) for key in ("vnc_port", "novnc_port", "debug_port"))


def _display_socket_active(path: Path) -> bool:
    """Return whether an Xvfb Unix socket is live, removing a stale marker."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.15)
            client.connect(str(path))
        return True
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A path we cannot remove is conservatively treated as occupied.
            return True
        return False


def _active_rows(conn) -> list[Any]:
    rows = conn.execute("SELECT * FROM taobao_sessions WHERE status IN ('starting','running','observing') ORDER BY updated_at DESC").fetchall()
    active = []
    now = proxy_pool.now_iso()
    for row in rows:
        if proxy_pool._pid_alive(int(row["pid"] or 0)):
            active.append(row)
            continue
        _terminate(row)
        conn.execute("UPDATE taobao_sessions SET status = 'stopped', last_error = ?, updated_at = ? WHERE id = ?", ("浏览器进程已退出", now, row["id"]))
    conn.commit()
    return active


def _allocate_slot(conn) -> tuple[int, dict[str, int | str]]:
    used = {int(row["slot"] or 0) for row in _active_rows(conn)}
    for slot in range(1, MAX_SLOTS + 1):
        ports = _slot_ports(slot)
        if slot not in used and _ports_available(ports):
            return slot, ports
    raise ValueError("淘宝浏览器观测槽位已满，请稍后再试")


def _terminate(row: Any) -> None:
    for key in ("pid", "websockify_pid", "x11vnc_pid", "xvfb_pid"):
        proxy_pool._terminate_pid(int(row[key] or 0))


def _launch_channel(ports: dict[str, int | str], session_id: int) -> dict[str, Any]:
    log_dir = proxy_pool._abs_workspace_path(f"data/taobao_browser_sessions/{session_id}")
    display = str(ports["display"])
    xvfb = proxy_pool._required_binary("Xvfb")
    xvfb_proc = proxy_pool._open_process(log_dir, "xvfb", [xvfb, display, "-screen", "0", "1280x900x24", "-nolisten", "tcp"])
    try:
        time.sleep(0.8)
        if not proxy_pool._pid_alive(int(xvfb_proc.pid)):
            raise ValueError("淘宝独立显示通道启动失败")
        x11vnc = proxy_pool._required_binary("x11vnc")
        websockify = proxy_pool._required_binary("websockify")
        x11vnc_proc = proxy_pool._open_process(log_dir, "x11vnc", [
            x11vnc, "-display", display, "-rfbport", str(ports["vnc_port"]), "-localhost", "-forever", "-shared", "-nopw", "-quiet"
        ])
        proxy_pool._wait_for_port(int(ports["vnc_port"]), "淘宝 VNC")
        websockify_proc = proxy_pool._open_process(log_dir, "websockify", [
            websockify, "--web", proxy_pool._novnc_web_dir(), str(ports["novnc_port"]), f"127.0.0.1:{ports['vnc_port']}"
        ])
        proxy_pool._wait_for_port(int(ports["novnc_port"]), "淘宝 noVNC")
        return {
            "xvfb_pid": int(xvfb_proc.pid), "x11vnc_pid": int(x11vnc_proc.pid),
            "websockify_pid": int(websockify_proc.pid), "channel_url": proxy_pool._public_novnc_url(int(ports["novnc_port"])),
        }
    except Exception:
        for process in (locals().get("websockify_proc"), locals().get("x11vnc_proc"), xvfb_proc):
            if process is not None:
                proxy_pool._terminate_pid(int(process.pid))
        raise


def _session_public(row: Any) -> dict[str, Any]:
    return {
        "active": str(row["status"]) in {"starting", "running", "observing"},
        "status": str(row["status"]),
        "lastError": str(row["last_error"] or ""),
        "updatedAt": str(row["updated_at"]),
    }


def state(user: dict[str, Any]) -> dict[str, Any]:
    profile = _profile_for_user(user, create=False)
    if not profile:
        return {"configured": False, "session": {"active": False, "status": "not_started"}}
    with _connect() as conn:
        _active_rows(conn)
        row = conn.execute("SELECT * FROM taobao_sessions WHERE owner_id = ? ORDER BY id DESC LIMIT 1", (profile["owner_id"],)).fetchone()
        binding = conn.execute("SELECT * FROM taobao_proxy_bindings WHERE owner_id = ?", (profile["owner_id"],)).fetchone()
    try:
        profile_json = json.loads(str(profile["profile_json"] or "{}"))
    except json.JSONDecodeError:
        profile_json = {}
    return {
        "configured": True,
        "profile": {
            "fingerprintId": profile["fingerprint_id"],
            "proxyReady": bool(binding and binding["status"] == "active"),
            "loginPersisted": _has_taobao_login_state(profile_json),
        },
        "session": _session_public(row) if row else {"active": False, "status": "stopped"},
    }


def start_session(user: dict[str, Any]) -> dict[str, Any]:
    profile = _profile_for_user(user, create=True)
    with _LOCK, _connect() as conn:
        for row in _active_rows(conn):
            if str(row["owner_id"]) == str(profile["owner_id"]):
                return {"session": _session_public(row), "openUrl": str(row["channel_url"]), **state(user)}
        binding = _ensure_taobao_proxy(str(profile["owner_id"]))
        pool = {"local_port": int(binding["local_port"]), "expected_exit_ip": "", "source_type": "direct"}
        slot, ports = _allocate_slot(conn)
        now = proxy_pool.now_iso()
        cur = conn.execute(
            """INSERT INTO taobao_sessions (owner_id, slot, proxy_profile_id, status, display, vnc_port, novnc_port, debug_port, created_at, updated_at)
               VALUES (?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?)""",
            # proxy_profile_id is a legacy non-null column. It is retained for
            # session-schema compatibility only; browser routing uses the
            # Taobao-only binding above (pool["local_port"]).
            (profile["owner_id"], slot, int(profile["proxy_profile_id"]), str(ports["display"]), int(ports["vnc_port"]), int(ports["novnc_port"]), int(ports["debug_port"]), now, now),
        )
        session_id = int(cur.lastrowid)
        conn.commit()
        try:
            channel = _launch_channel(ports, session_id)
            profile_json = json.loads(str(profile["profile_json"] or "{}"))
            start_url = "https://www.taobao.com/" if _has_taobao_login_state(profile_json) else "https://login.taobao.com/member/login.jhtml"
            pid, user_data_dir = proxy_pool._launch_browser_for_session(
                profile_json, pool, session_id, str(ports["display"]), int(ports["debug_port"]), start_url
            )
            time.sleep(2.0)
            if not proxy_pool._pid_alive(pid):
                raise ValueError("Chrome 启动后立即退出，请检查淘宝浏览器日志")
            proxy_pool._wait_for_port(int(ports["debug_port"]), "淘宝 Chrome CDP", timeout=10.0)
            observed_ip = proxy_pool._detect_browser_exit_ip(int(ports["debug_port"]))
            expected_ip = str(pool["expected_exit_ip"] or "")
            if str(pool["source_type"] or "") != "direct" and expected_ip and observed_ip != expected_ip:
                raise ValueError(f"浏览器出口 IP {observed_ip} 与绑定 IP 不一致")
            conn.execute(
                """UPDATE taobao_sessions SET status = 'observing', channel_url = ?, pid = ?, xvfb_pid = ?, x11vnc_pid = ?, websockify_pid = ?, updated_at = ? WHERE id = ?""",
                (channel["channel_url"], pid, channel["xvfb_pid"], channel["x11vnc_pid"], channel["websockify_pid"], proxy_pool.now_iso(), session_id),
            )
            conn.commit()
        except Exception as exc:
            row = conn.execute("SELECT * FROM taobao_sessions WHERE id = ?", (session_id,)).fetchone()
            if row:
                _terminate(row)
            conn.execute("UPDATE taobao_sessions SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?", (str(exc), proxy_pool.now_iso(), session_id))
            conn.commit()
            raise ValueError(f"淘宝隔离浏览器启动失败：{exc}") from exc
        row = conn.execute("SELECT * FROM taobao_sessions WHERE id = ?", (session_id,)).fetchone()
    return {"session": _session_public(row), "openUrl": str(row["channel_url"]), **state(user)}


def stop_session(user: dict[str, Any]) -> dict[str, Any]:
    profile = _profile_for_user(user, create=False)
    if not profile:
        return state(user)
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM taobao_sessions WHERE owner_id = ? AND status IN ('starting','running','observing') ORDER BY id DESC LIMIT 1", (profile["owner_id"],)).fetchone()
        if row:
            _terminate(row)
            _reclaim_stale_display_socket(str(row["display"] or ""))
            conn.execute("UPDATE taobao_sessions SET status = 'stopped', current_job_id = '', last_error = '手动关闭浏览器', updated_at = ? WHERE id = ?", (proxy_pool.now_iso(), row["id"]))
            conn.commit()
    return state(user)


def _active_session(user: dict[str, Any]):
    profile = _profile_for_user(user, create=False)
    if not profile:
        raise ValueError("请先启动淘宝登录浏览器")
    with _connect() as conn:
        rows = _active_rows(conn)
        row = next((item for item in rows if str(item["owner_id"]) == str(profile["owner_id"])), None)
        if not row:
            raise ValueError("淘宝浏览器未运行，请先启动并完成登录")
        binding = conn.execute("SELECT * FROM taobao_proxy_bindings WHERE owner_id = ?", (profile["owner_id"],)).fetchone()
        if not binding:
            raise ValueError("淘宝代理监听尚未初始化")
        return profile, dict(row), {"local_port": int(binding["local_port"]), "expected_exit_ip": "", "source_type": "direct"}


def open_login(user: dict[str, Any]) -> dict[str, Any]:
    profile, session, _pool = _active_session(user)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{int(session['debug_port'])}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.taobao.com/", wait_until="domcontentloaded", timeout=60000)
        page.bring_to_front()
    with _connect() as conn:
        conn.execute("UPDATE taobao_sessions SET updated_at = ? WHERE id = ?", (proxy_pool.now_iso(), int(session["id"])))
        conn.commit()
    return {"openUrl": session["channel_url"], **state(user)}


def _validate_url(value: Any) -> str:
    parsed = urlparse(str(value or "").strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname or not (hostname == "taobao.com" or hostname.endswith(".taobao.com") or hostname == "tmall.com" or hostname.endswith(".tmall.com")):
        raise ValueError("仅支持 HTTPS 淘宝或天猫商品详情链接")
    if not parsed.path.endswith("item.htm"):
        raise ValueError("请输入淘宝或天猫商品详情页链接")
    return parsed.geturl()


def _validate_search_keyword(value: Any) -> str:
    keyword = re.sub(r"\s+", " ", str(value or "").strip())
    if not keyword:
        raise ValueError("搜索名为必填项")
    if len(keyword) > 100:
        raise ValueError("搜索名不能超过 100 个字符")
    return keyword


def _validate_optional_url(value: Any) -> str:
    cleaned = str(value or "").strip()
    return _validate_url(cleaned) if cleaned else ""


def _product_key(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    product_id = (parse_qs(parsed.query).get("id") or [""])[0].strip()
    if product_id:
        return f"id:{product_id}"
    retained = [(key, item) for key, item in parse_qs(parsed.query, keep_blank_values=True).items()
                if key.lower() not in {"spm", "ali_refid", "utparam", "ns", "abbucket"}]
    query = urlencode(retained, doseq=True)
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", query, ""))


def _first_visible_locator(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            for index in range(min(locator.count(), 12)):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except Exception:
            continue
    return None


def _random_point(box: dict[str, float], padding: int = 8) -> dict[str, float]:
    x_min, x_max = box["x"] + padding, box["x"] + max(padding + 1, box["width"] - padding)
    y_min, y_max = box["y"] + padding, box["y"] + max(padding + 1, box["height"] - padding)
    return {"x": random.uniform(x_min, x_max), "y": random.uniform(y_min, y_max)}


def _move_with_curve(page: Any, source: dict[str, float], target: dict[str, float]) -> None:
    control = {
        "x": (source["x"] + target["x"]) / 2 + random.uniform(-60, 60),
        "y": (source["y"] + target["y"]) / 2 + random.uniform(-40, 40),
    }
    steps = random.randint(8, 16)
    for index in range(1, steps + 1):
        ratio = index / steps
        x = (1 - ratio) ** 2 * source["x"] + 2 * (1 - ratio) * ratio * control["x"] + ratio ** 2 * target["x"]
        y = (1 - ratio) ** 2 * source["y"] + 2 * (1 - ratio) * ratio * control["y"] + ratio ** 2 * target["y"]
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(25, 80))


def _type_search_keyword(page: Any, keyword: str) -> dict[str, float]:
    search = _first_visible_locator(page, ("input#q", "input[name='q']", "input[type='search']", "input[placeholder*='搜索']"))
    if not search:
        raise ValueError("未找到淘宝搜索框，请在新页签确认淘宝首页已正常加载后重试")
    search.scroll_into_view_if_needed(timeout=5000)
    box = search.bounding_box()
    if not box:
        raise ValueError("淘宝搜索框不可见，请在新页签确认页面状态后重试")
    viewport = page.viewport_size or {"width": 1365, "height": 900}
    source = {"x": random.uniform(viewport["width"] * .38, viewport["width"] * .62), "y": random.uniform(viewport["height"] * .36, viewport["height"] * .58)}
    target = _random_point(box)
    _move_with_curve(page, source, target)
    page.mouse.click(target["x"], target["y"])
    page.wait_for_timeout(random.randint(90, 210))
    page.keyboard.press("Control+A")
    page.keyboard.type(keyword, delay=random.randint(55, 115))
    page.wait_for_timeout(random.randint(180, 420))
    page.keyboard.press("Enter")
    return target


def _search_candidates(page: Any) -> list[dict[str, Any]]:
    links = page.locator("a[href]")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(min(links.count(), MAX_SEARCH_CANDIDATES * 8)):
        link = links.nth(index)
        try:
            href = str(link.get_attribute("href") or "").strip()
            absolute_url = urljoin(page.url, href)
            try:
                _validate_url(absolute_url)
            except ValueError:
                continue
            key = _product_key(absolute_url)
            if key in seen:
                continue
            seen.add(key)
            text = str(link.get_attribute("title") or link.get_attribute("aria-label") or link.inner_text() or "").strip()
            if not text:
                text = str(link.locator("xpath=..").inner_text() or "").strip()
            candidates.append({"index": index, "url": absolute_url, "key": key, "title": re.sub(r"\s+", " ", text)[:300]})
            if len(candidates) >= MAX_SEARCH_CANDIDATES:
                break
        except Exception:
            continue
    return candidates


def _select_search_candidate(candidates: list[dict[str, Any]], keyword: str, requested_url: str) -> tuple[dict[str, Any] | None, int]:
    if requested_url:
        target_key = _product_key(requested_url)
        for checked, candidate in enumerate(candidates, start=1):
            if candidate["key"] == target_key:
                return candidate, checked
        return None, len(candidates)
    needle = re.sub(r"\s+", "", keyword).lower()
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            needle not in re.sub(r"\s+", "", str(item[1]["title"])).lower(),
            -sum(char in re.sub(r"\s+", "", str(item[1]["title"])).lower() for char in set(needle)),
            item[0],
        ),
    )
    return (ranked[0][1] if ranked else None), 1 if ranked else 0


def _open_search_candidate(page: Any, candidate: dict[str, Any], mouse: dict[str, float]) -> None:
    link = page.locator("a[href]").nth(int(candidate["index"]))
    link.scroll_into_view_if_needed(timeout=5000)
    page.wait_for_timeout(random.randint(180, 460))
    box = link.bounding_box()
    if not box:
        raise ValueError("匹配商品已不在可点击区域，请重试")
    target = _random_point(box, padding=5)
    _move_with_curve(page, mouse, target)
    page.mouse.click(target["x"], target["y"])
    page.wait_for_timeout(random.randint(900, 1600))
    page.wait_for_load_state("domcontentloaded", timeout=60000)


def _risk_status(page) -> str:
    text = page.locator("body").inner_text(timeout=5000)[:8000]
    title = page.title()
    haystack = f"{title}\n{text}".lower()
    if any(item in haystack for item in ("验证码", "请完成验证", "访问受限", "滑动验证", "安全验证", "security check")):
        return "verification_required"
    if any(item in haystack for item in ("登录淘宝", "扫码登录", "请登录")):
        return "login_required"
    return ""


def _archive_id() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def _owner_archive_dir(owner_id: str) -> Path:
    path = ARCHIVE_ROOT / _safe_key(owner_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _image_extension(content_type: str, source_url: str) -> str:
    if "png" in content_type: return "png"
    if "webp" in content_type: return "webp"
    if "gif" in content_type: return "gif"
    suffix = Path(urlparse(source_url).path).suffix.lower().lstrip(".")
    return suffix if suffix in {"jpg", "jpeg", "avif"} else "jpg"


def _save_image(source_url: str, target: Path, referer: str, pool: dict[str, Any], user_agent: str) -> tuple[str, int]:
    proxy_url = f"http://127.0.0.1:{int(pool['local_port'] or 0)}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    request = Request(source_url, headers={"User-Agent": user_agent, "Referer": referer, "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"})
    with opener.open(request, timeout=20) as response:
        payload = response.read(MAX_IMAGE_BYTES + 1)
        if len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过归档大小限制")
        target.write_bytes(payload)
        return str(response.headers.get("Content-Type") or ""), len(payload)


def collect(user: dict[str, Any], raw_keyword: Any, raw_url: Any = "") -> dict[str, Any]:
    keyword = _validate_search_keyword(raw_keyword)
    requested_url = _validate_optional_url(raw_url)
    profile, session, pool = _active_session(user)
    archive_id = _archive_id()
    archive_dir = _owner_archive_dir(str(profile["owner_id"])) / archive_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {"metadata": "metadata.json", "dom": "dom.html", "screenshot": "page.png", "images": []}
    metadata: dict[str, Any] = {
        "id": archive_id,
        "requestedUrl": requested_url or None,
        "searchKeyword": keyword,
        "createdAt": proxy_pool.now_iso(),
        "files": files,
        "status": "running",
    }
    with _connect() as conn:
        conn.execute("UPDATE taobao_sessions SET current_job_id = ?, updated_at = ? WHERE id = ?", (archive_id, proxy_pool.now_iso(), int(session["id"])))
        conn.commit()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{int(session['debug_port'])}")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.taobao.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(random.randint(900, 1600))
            risk = _risk_status(page)
            if risk:
                page.screenshot(path=str(archive_dir / files["screenshot"]), full_page=False)
                metadata.update({"ok": False, "status": risk, "humanReviewRequired": True, "message": "请在新页签的浏览器中完成人工登录或验证后重试"})
                _write_json(archive_dir / files["metadata"], metadata)
                return metadata
            mouse = _type_search_keyword(page, keyword)
            page.wait_for_timeout(random.randint(1200, 2400))
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            risk = _risk_status(page)
            if risk:
                page.screenshot(path=str(archive_dir / files["screenshot"]), full_page=False)
                metadata.update({"ok": False, "status": risk, "humanReviewRequired": True, "message": "搜索过程中需要登录或验证，请在新页签处理后重试"})
                _write_json(archive_dir / files["metadata"], metadata)
                return metadata
            candidates = _search_candidates(page)
            candidate, checked = _select_search_candidate(candidates, keyword, requested_url)
            metadata["search"] = {
                "keyword": keyword,
                "candidateCount": len(candidates),
                "checkedCandidateCount": checked,
                "requestedProductUrl": requested_url or None,
                "matchedProductUrl": str(candidate["url"]) if candidate else None,
                "matchedTitle": str(candidate["title"]) if candidate else None,
            }
            if not candidate:
                dom = page.content()
                (archive_dir / files["dom"]).write_text(dom, encoding="utf-8")
                page.screenshot(path=str(archive_dir / files["screenshot"]), full_page=True)
                message = "搜索列表中未找到与链接一致的商品" if requested_url else "搜索列表中未找到可采集的商品"
                metadata.update({
                    "ok": False,
                    "status": "search_no_match",
                    "message": message,
                    "detail": {"title": keyword, "shopName": None, "price": None},
                    "domBytes": len(dom.encode("utf-8")),
                    "imageCount": 0,
                })
                _write_json(archive_dir / files["metadata"], metadata)
                return metadata
            _open_search_candidate(page, candidate, mouse)
            page.wait_for_timeout(random.randint(1100, 2200))
            risk = _risk_status(page)
            metadata["finalUrl"] = page.url
            if risk:
                page.screenshot(path=str(archive_dir / files["screenshot"]), full_page=False)
                metadata.update({"ok": False, "status": risk, "humanReviewRequired": True, "message": "打开匹配商品时需要登录或验证，请在新页签处理后重试"})
                _write_json(archive_dir / files["metadata"], metadata)
                return metadata
            dom = page.content()
            (archive_dir / files["dom"]).write_text(dom, encoding="utf-8")
            page.screenshot(path=str(archive_dir / files["screenshot"]), full_page=True)
            detail = page.evaluate("""() => {
                const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                const body = clean(document.body?.innerText || '');
                const price = body.match(/(?:¥|￥)\\s*([0-9]+(?:\\.[0-9]{1,2})?)/);
                const shop = Array.from(document.querySelectorAll('[class*=shop], [class*=seller], [data-spm*=shop]')).map(node => clean(node.innerText || node.textContent)).find(value => value && value.length < 100) || '';
                const images = Array.from(document.images).map((image, index) => {
                    const rect = image.getBoundingClientRect();
                    return { source: String(image.currentSrc || image.src || image.dataset.src || '').trim(), index, width: Math.max(image.naturalWidth || 0, rect.width || 0), height: Math.max(image.naturalHeight || 0, rect.height || 0), hint: /main|gallery|thumb|pic|image|sku|detail/i.test(`${image.className} ${image.parentElement?.className || ''}`) };
                }).filter(item => /^https?:/i.test(item.source) && item.width >= 160 && item.height >= 160).sort((a, b) => (b.width*b.height + (b.hint ? 1e8 : 0)) - (a.width*a.height + (a.hint ? 1e8 : 0)));
                const unique = []; const seen = new Set(); for (const image of images) { if (!seen.has(image.source)) { seen.add(image.source); unique.push(image); } }
                return { title: document.title || '', shopName: shop || null, price: price ? price[1] : null, images: unique.slice(0, 20) };
            }""")
            profile_data = json.loads(str(profile["profile_json"] or "{}"))
            user_agent = str((profile_data.get("browser_settings") or {}).get("user_agent") or "Mozilla/5.0")
            for index, image in enumerate((detail.get("images") or [])[:MAX_IMAGES], start=1):
                source_url = str(image.get("source") or "")
                try:
                    ext = _image_extension("", source_url)
                    filename = f"product-{index:02d}.{ext}"
                    content_type, size = _save_image(source_url, archive_dir / filename, page.url, pool, user_agent)
                    files["images"].append({"filename": filename, "sourceUrl": source_url, "width": image.get("width"), "height": image.get("height"), "contentType": content_type, "size": size})
                except Exception as exc:
                    files["images"].append({"sourceUrl": source_url, "error": str(exc)[:240]})
            detail.pop("images", None)
            metadata.update({"ok": True, "status": "archived", "detail": detail, "domBytes": len(dom.encode("utf-8")), "imageCount": len([item for item in files["images"] if item.get("filename")])})
            _write_json(archive_dir / files["metadata"], metadata)
            return metadata
    except Exception as exc:
        metadata.update({"ok": False, "status": "failed", "error": str(exc)[:1000]})
        _write_json(archive_dir / files["metadata"], metadata)
        return metadata
    finally:
        with _connect() as conn:
            conn.execute("UPDATE taobao_sessions SET current_job_id = '', updated_at = ? WHERE id = ?", (proxy_pool.now_iso(), int(session["id"])))
            conn.commit()


def list_archives(user: dict[str, Any]) -> list[dict[str, Any]]:
    owner_id, _, _ = _owner(user)
    root = _owner_archive_dir(owner_id)
    archives: list[dict[str, Any]] = []
    for path in root.iterdir() if root.exists() else []:
        if not path.is_dir() or not re.fullmatch(r"[0-9]{14}-[a-f0-9]{8}", path.name):
            continue
        try:
            item = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        files = item.get("files") if isinstance(item.get("files"), dict) else {}
        archives.append({
            "id": path.name, "ok": bool(item.get("ok")), "status": str(item.get("status") or "unknown"),
            "requestedUrl": item.get("requestedUrl"), "finalUrl": item.get("finalUrl"), "createdAt": item.get("createdAt"),
            "detail": item.get("detail") if isinstance(item.get("detail"), dict) else {}, "imageCount": int(item.get("imageCount") or 0),
            "humanReviewRequired": bool(item.get("humanReviewRequired")), "files": files,
        })
    return sorted(archives, key=lambda item: str(item.get("id") or ""), reverse=True)


def archive_path(user: dict[str, Any], archive_id: str, filename: str) -> Path:
    owner_id, _, _ = _owner(user)
    if not re.fullmatch(r"[0-9]{14}-[a-f0-9]{8}", archive_id) or not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        raise FileNotFoundError("归档文件不存在")
    root = _owner_archive_dir(owner_id).resolve()
    path = (root / archive_id / filename).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError("归档文件不存在")
    return path


def export_markdown(user: dict[str, Any], archive_id: str) -> str:
    metadata = json.loads(archive_path(user, archive_id, "metadata.json").read_text(encoding="utf-8"))
    detail = metadata.get("detail") if isinstance(metadata.get("detail"), dict) else {}
    images = metadata.get("files", {}).get("images", []) if isinstance(metadata.get("files"), dict) else []
    return "\n".join([
        f"# 淘宝商品采集归档 {archive_id}", "",
        f"- 采集时间：{metadata.get('createdAt') or ''}", f"- 状态：{metadata.get('status') or ''}",
        f"- 原始链接：{metadata.get('requestedUrl') or ''}", f"- 最终链接：{metadata.get('finalUrl') or ''}",
        f"- 商品标题：{detail.get('title') or ''}", f"- 店铺：{detail.get('shopName') or ''}", f"- 价格：{detail.get('price') or ''}",
        f"- 已归档产品图片：{len([item for item in images if isinstance(item, dict) and item.get('filename')])}", "",
        "## 留档文件", "", "- metadata.json", "- dom.html", "- page.png",
    ]) + "\n"


def cleanup_expired_sessions() -> int:
    with _LOCK, _connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS count FROM taobao_sessions WHERE status IN ('starting','running','observing')").fetchone()["count"]
        active_rows = _active_rows(conn)
        after = conn.execute("SELECT COUNT(*) AS count FROM taobao_sessions WHERE status IN ('starting','running','observing')").fetchone()["count"]
        active_displays = {str(row["display"] or "") for row in active_rows}
        released_sockets = _reclaim_stale_display_sockets(active_displays)
        return max(0, int(before) - int(after)) + released_sockets


def _reclaim_stale_display_sockets(active_displays: set[str]) -> int:
    """Remove dead Xvfb sockets not owned by a live Taobao browser session."""
    released = 0
    for slot in range(1, MAX_SLOTS + 1):
        display = str(_slot_ports(slot)["display"])
        if display in active_displays:
            continue
        if _reclaim_stale_display_socket(display):
            released += 1
    return released


def _reclaim_stale_display_socket(display: str) -> bool:
    if not display:
        return False
    path = Path(f"/tmp/.X11-unix/X{display.lstrip(':')}")
    return path.exists() and not _display_socket_active(path)
