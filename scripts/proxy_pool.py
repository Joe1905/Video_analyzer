#!/usr/bin/env python3
from __future__ import annotations

import base64
import binascii
import calendar
import http.client
import hashlib
import ipaddress
import json
import os
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path.cwd()
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "proxy_pool.sqlite"
DEFAULT_NOVNC_PUBLIC_URL = os.getenv("NOVNC_PUBLIC_URL", "http://192.168.1.254:6080/vnc.html?autoconnect=1&resize=scale")
DEFAULT_MIHOMO_API = os.getenv("MIHOMO_API_URL", "http://127.0.0.1:9090")
SING_BOX_CONFIG_PATH = Path(os.getenv("SING_BOX_CONFIG_PATH", str(DATA_DIR / "sing-box" / "config.json")))
SING_BOX_COMPOSE_PROJECT = os.getenv("SING_BOX_COMPOSE_PROJECT", "short-video-analyzer").strip() or "short-video-analyzer"
SING_BOX_COMPOSE_SERVICE = os.getenv("SING_BOX_COMPOSE_SERVICE", "sing-box").strip() or "sing-box"
PROXY_PORT_START = int(os.getenv("PROXY_POOL_PORT_START", "18900") or "18900")
PROXY_PORT_END = int(os.getenv("PROXY_POOL_PORT_END", "18999") or "18999")
PROXY_REQUEST_ATTEMPTS = max(1, int(os.getenv("PROXY_REQUEST_ATTEMPTS", "3") or "3"))
PROXY_REACHABILITY_ATTEMPTS = max(1, int(os.getenv("PROXY_REACHABILITY_ATTEMPTS", "10") or "10"))
PROXY_RECHECK_DELAYS_SECONDS = (5 * 60, 10 * 60, 30 * 60)
PROXY_QUEUE_RECHECK_SECONDS = 5 * 60
PROXY_RETRYABLE_ERROR_MARKERS = (
    "通过服务器 mihomo 查询出口 ip 失败",
    "无法读取服务器 mihomo 节点",
    "ip 查询接口没有返回出口 ip",
    "服务器未能自动查询到出口 ip",
    "浏览器出口 ip 校验失败",
    "浏览器出口 ip",
    "当前出口 ip",
    "代理 ip 校验未通过",
    "tiktok 连通性校验失败",
    "代理状态为 不可用",
    "代理状态为 异常",
)
NOVNC_PORT = int(os.getenv("NOVNC_PORT", "6080") or "6080")
NOVNC_MANUAL_PORTS = int(os.getenv("NOVNC_MANUAL_PORTS", "1") or "1")
VNC_PORT = int(os.getenv("VNC_PORT", "5900") or "5900")
CDP_PORT = int(os.getenv("TIKTOK_CDP_PORT_START", "19220") or "19220")
XVFB_DISPLAY_BASE = int(os.getenv("TIKTOK_XVFB_DISPLAY_BASE", "90") or "90")
TIKTOK_BROWSER_UID = int(os.getenv("TIKTOK_BROWSER_UID", "10001") or "10001")
TIKTOK_BROWSER_GID = int(os.getenv("TIKTOK_BROWSER_GID", "10001") or "10001")
TIKTOK_BROWSER_LOCALE = os.getenv("TIKTOK_BROWSER_LOCALE", "en-US").strip() or "en-US"
TIKTOK_BROWSER_ACCEPT_LANGUAGE = os.getenv("TIKTOK_BROWSER_ACCEPT_LANGUAGE", "en-US,en").strip() or "en-US,en"
RUNTIME_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
STATUS_ACTIVE = "启用"
STATUS_PAUSED = "禁用"
STATUS_ERROR = "不可用"
STATUS_MAP = {
    "active": STATUS_ACTIVE,
    "enabled": STATUS_ACTIVE,
    "ok": STATUS_ACTIVE,
    "bound": STATUS_ACTIVE,
    "可用": STATUS_ACTIVE,
    "已绑定": STATUS_ACTIVE,
    "未绑定": STATUS_ACTIVE,
    "启用": STATUS_ACTIVE,
    "paused": STATUS_PAUSED,
    "disabled": STATUS_PAUSED,
    "disable": STATUS_PAUSED,
    "暂停": STATUS_PAUSED,
    "禁用": STATUS_PAUSED,
    "blocked": STATUS_ERROR,
    "error": STATUS_ERROR,
    "异常": STATUS_ERROR,
    "不可用": STATUS_ERROR,
}
ACCOUNT_STATUS_ACTIVE = "可用"
ACCOUNT_STATUS_PAUSED = "暂停"
ACCOUNT_STATUS_ERROR = "异常"
ACCOUNT_STATUS_MAP = {
    "active": ACCOUNT_STATUS_ACTIVE,
    "enabled": ACCOUNT_STATUS_ACTIVE,
    "ok": ACCOUNT_STATUS_ACTIVE,
    "可用": ACCOUNT_STATUS_ACTIVE,
    "paused": ACCOUNT_STATUS_PAUSED,
    "disabled": ACCOUNT_STATUS_PAUSED,
    "暂停": ACCOUNT_STATUS_PAUSED,
    "blocked": ACCOUNT_STATUS_ERROR,
    "error": ACCOUNT_STATUS_ERROR,
    "异常": ACCOUNT_STATUS_ERROR,
}


def browser_max_slots() -> int:
    return max(1, int(os.getenv("TIKTOK_BROWSER_MAX_SLOTS", "2") or "2"))


def pending_login_ttl_seconds() -> int:
    return max(60, int(os.getenv("TIKTOK_PENDING_LOGIN_TTL_SECONDS", "900") or "900"))


def novnc_port_plan() -> dict[str, Any]:
    max_slots = browser_max_slots()
    manual_ports = max(1, NOVNC_MANUAL_PORTS)
    total_ports = max_slots + manual_ports
    # NOVNC_PORT is reserved for the existing server-level desktop. Account
    # sessions use the following ports so they cannot attach to another app.
    session_base = NOVNC_PORT + 1
    end_port = session_base + total_ports - 1
    return {
        "base_port": session_base,
        "reserved_port": NOVNC_PORT,
        "manual_ports": manual_ports,
        "max_slots": max_slots,
        "total_ports": total_ports,
        "allowed_ports": list(range(session_base, end_port + 1)),
        "allowed_range": f"{session_base}-{end_port}" if end_port != session_base else str(session_base),
    }


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_retryable_proxy_error(error: Exception | str) -> bool:
    message = str(error or "").strip().lower()
    return bool(message and any(marker in message for marker in PROXY_RETRYABLE_ERROR_MARKERS))


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS proxy_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'vless',
            source_uri TEXT NOT NULL DEFAULT '',
            dialer_proxy TEXT NOT NULL DEFAULT '',
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
            updated_at TEXT NOT NULL,
            local_port INTEGER NOT NULL DEFAULT 0,
            detected_exit_ip TEXT NOT NULL DEFAULT '',
            detected_country TEXT NOT NULL DEFAULT '',
            detected_region TEXT NOT NULL DEFAULT '',
            detected_city TEXT NOT NULL DEFAULT '',
            detected_address TEXT NOT NULL DEFAULT '',
            detected_at TEXT NOT NULL DEFAULT '',
            auto_check_failures INTEGER NOT NULL DEFAULT 0,
            next_auto_check_at TEXT NOT NULL DEFAULT '',
            last_auto_check_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tiktok_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            tiktok_avatar_url TEXT NOT NULL DEFAULT '',
            feishu_user_id TEXT NOT NULL DEFAULT '',
            feishu_user_name TEXT NOT NULL DEFAULT '',
            feishu_avatar_url TEXT NOT NULL DEFAULT '',
            feishu_user_active INTEGER NOT NULL DEFAULT 0,
            feishu_user_synced_at TEXT NOT NULL DEFAULT '',
            proxy_profile_id INTEGER NOT NULL REFERENCES proxy_profiles(id) ON DELETE RESTRICT,
            proxy_bound INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            profile_json TEXT NOT NULL DEFAULT '{}',
            notes TEXT NOT NULL DEFAULT '',
            last_checked_ip TEXT NOT NULL DEFAULT '',
            last_check_status TEXT NOT NULL DEFAULT '',
            last_check_at TEXT NOT NULL DEFAULT '',
            last_login_at TEXT NOT NULL DEFAULT '',
            last_collect_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tiktok_accounts_proxy ON tiktok_accounts(proxy_profile_id);
        CREATE INDEX IF NOT EXISTS idx_tiktok_accounts_status ON tiktok_accounts(status);
        CREATE TABLE IF NOT EXISTS browser_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot INTEGER NOT NULL,
            proxy_profile_id INTEGER NOT NULL REFERENCES proxy_profiles(id) ON DELETE RESTRICT,
            account_id INTEGER REFERENCES tiktok_accounts(id) ON DELETE SET NULL,
            username TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'starting',
            channel_url TEXT NOT NULL DEFAULT '',
            runtime_id TEXT NOT NULL DEFAULT '',
            pid INTEGER NOT NULL DEFAULT 0,
            xvfb_pid INTEGER NOT NULL DEFAULT 0,
            x11vnc_pid INTEGER NOT NULL DEFAULT 0,
            websockify_pid INTEGER NOT NULL DEFAULT 0,
            display TEXT NOT NULL DEFAULT '',
            vnc_port INTEGER NOT NULL DEFAULT 0,
            novnc_port INTEGER NOT NULL DEFAULT 0,
            feishu_user_id TEXT NOT NULL DEFAULT '',
            feishu_user_name TEXT NOT NULL DEFAULT '',
            feishu_avatar_url TEXT NOT NULL DEFAULT '',
            profile_key TEXT NOT NULL DEFAULT '',
            user_data_dir TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_browser_sessions_status ON browser_sessions(status);
        CREATE INDEX IF NOT EXISTS idx_browser_sessions_proxy ON browser_sessions(proxy_profile_id);
        CREATE TABLE IF NOT EXISTS tiktok_products (
            product_id TEXT PRIMARY KEY,
            product_name TEXT NOT NULL DEFAULT '',
            product_url TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '',
            price TEXT NOT NULL DEFAULT '',
            stock TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'my_shop',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tiktok_products_source ON tiktok_products(source, sort_order);
        CREATE TABLE IF NOT EXISTS publish_assets (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tiktok_accounts(id) ON DELETE RESTRICT,
            original_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'video/mp4',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS publish_jobs (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tiktok_accounts(id) ON DELETE RESTRICT,
            proxy_profile_id INTEGER NOT NULL REFERENCES proxy_profiles(id) ON DELETE RESTRICT,
            asset_id TEXT NOT NULL REFERENCES publish_assets(id) ON DELETE RESTRICT,
            description TEXT NOT NULL DEFAULT '',
            ai_generated INTEGER NOT NULL DEFAULT 0,
            product_link TEXT NOT NULL DEFAULT '',
            keep_observing INTEGER NOT NULL DEFAULT 0,
            manual_publish INTEGER NOT NULL DEFAULT 0,
            schedule_mode TEXT NOT NULL DEFAULT 'server',
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            stage TEXT NOT NULL DEFAULT '',
            status_detail TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL DEFAULT '',
            session_id INTEGER REFERENCES browser_sessions(id) ON DELETE SET NULL,
            final_click_at TEXT NOT NULL DEFAULT '',
            actual_publish_at TEXT NOT NULL DEFAULT '',
            result_url TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            deleted_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_publish_jobs_account ON publish_jobs(account_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_publish_jobs_due ON publish_jobs(status, scheduled_at);
        CREATE TABLE IF NOT EXISTS collect_settings (
            account_id INTEGER PRIMARY KEY REFERENCES tiktok_accounts(id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0,
            daily_time TEXT NOT NULL DEFAULT '03:00',
            max_videos INTEGER NOT NULL DEFAULT 0,
            date_rule TEXT NOT NULL DEFAULT 'previous_day',
            publish_date_start TEXT NOT NULL DEFAULT '',
            publish_date_end TEXT NOT NULL DEFAULT '',
            feishu_target_json TEXT NOT NULL DEFAULT '{}',
            last_scheduled_date TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collect_jobs (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tiktok_accounts(id) ON DELETE RESTRICT,
            proxy_profile_id INTEGER NOT NULL REFERENCES proxy_profiles(id) ON DELETE RESTRICT,
            trigger_type TEXT NOT NULL DEFAULT 'manual',
            schedule_date TEXT NOT NULL DEFAULT '',
            max_videos INTEGER NOT NULL DEFAULT 0,
            publish_date_start TEXT NOT NULL DEFAULT '',
            publish_date_end TEXT NOT NULL DEFAULT '',
            feishu_target_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT '',
            status_detail TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL DEFAULT '',
            session_id INTEGER REFERENCES browser_sessions(id) ON DELETE SET NULL,
            total_videos INTEGER NOT NULL DEFAULT 0,
            completed_videos INTEGER NOT NULL DEFAULT 0,
            failed_videos INTEGER NOT NULL DEFAULT 0,
            current_video_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_collect_jobs_account ON collect_jobs(account_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_collect_jobs_due ON collect_jobs(status, next_attempt_at, created_at);
        CREATE TABLE IF NOT EXISTS collect_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES collect_jobs(id) ON DELETE RESTRICT,
            account_id INTEGER NOT NULL REFERENCES tiktok_accounts(id) ON DELETE RESTRICT,
            video_id TEXT NOT NULL,
            video_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            collected_at TEXT NOT NULL,
            retention_complete INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            feishu_target_json TEXT NOT NULL DEFAULT '{}',
            feishu_record_id TEXT NOT NULL DEFAULT '',
            feishu_sync_status TEXT NOT NULL DEFAULT '',
            feishu_sync_error TEXT NOT NULL DEFAULT '',
            feishu_synced_at TEXT NOT NULL DEFAULT '',
            UNIQUE(job_id, video_id)
        );
        CREATE INDEX IF NOT EXISTS idx_collect_results_account ON collect_results(account_id, collected_at DESC);
        CREATE INDEX IF NOT EXISTS idx_collect_results_video ON collect_results(account_id, video_id, collected_at DESC);
        CREATE TABLE IF NOT EXISTS collect_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES collect_jobs(id) ON DELETE RESTRICT,
            account_id INTEGER NOT NULL REFERENCES tiktok_accounts(id) ON DELETE RESTRICT,
            video_id TEXT NOT NULL DEFAULT '',
            video_url TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_collect_errors_job ON collect_errors(job_id, created_at DESC);
        """
    )
    for name, definition in {
        "dialer_proxy": "TEXT NOT NULL DEFAULT ''",
        "local_port": "INTEGER NOT NULL DEFAULT 0",
        "detected_exit_ip": "TEXT NOT NULL DEFAULT ''",
        "detected_country": "TEXT NOT NULL DEFAULT ''",
        "detected_region": "TEXT NOT NULL DEFAULT ''",
        "detected_city": "TEXT NOT NULL DEFAULT ''",
        "detected_address": "TEXT NOT NULL DEFAULT ''",
        "detected_at": "TEXT NOT NULL DEFAULT ''",
        "auto_check_failures": "INTEGER NOT NULL DEFAULT 0",
        "next_auto_check_at": "TEXT NOT NULL DEFAULT ''",
        "last_auto_check_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        existing = {row[1] for row in conn.execute("PRAGMA table_info(proxy_profiles)")}
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE proxy_profiles ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    existing_account_cols = {row[1] for row in conn.execute("PRAGMA table_info(tiktok_accounts)")}
    if "deleted_at" not in existing_account_cols:
        conn.execute("ALTER TABLE tiktok_accounts ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    if "last_publish_at" not in existing_account_cols:
        conn.execute("ALTER TABLE tiktok_accounts ADD COLUMN last_publish_at TEXT NOT NULL DEFAULT ''")
    if "proxy_bound" not in existing_account_cols:
        conn.execute("ALTER TABLE tiktok_accounts ADD COLUMN proxy_bound INTEGER NOT NULL DEFAULT 1")
    for name in (
        "tiktok_avatar_url",
        "feishu_user_id",
        "feishu_user_name",
        "feishu_avatar_url",
    ):
        if name not in existing_account_cols:
            try:
                conn.execute(f"ALTER TABLE tiktok_accounts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    for name, definition in {
        "feishu_user_active": "INTEGER NOT NULL DEFAULT 0",
        "feishu_user_synced_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in existing_account_cols:
            conn.execute(f"ALTER TABLE tiktok_accounts ADD COLUMN {name} {definition}")
    conn.execute(
        """UPDATE tiktok_accounts
           SET feishu_user_active = 1
           WHERE feishu_user_id <> '' AND feishu_user_synced_at = ''"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_tiktok_accounts_feishu_user
           ON tiktok_accounts(feishu_user_id)
           WHERE feishu_user_id <> '' AND deleted_at = ''"""
    )
    existing_session_cols = {row[1] for row in conn.execute("PRAGMA table_info(browser_sessions)")}
    for name, definition in {
        "xvfb_pid": "INTEGER NOT NULL DEFAULT 0",
        "x11vnc_pid": "INTEGER NOT NULL DEFAULT 0",
        "websockify_pid": "INTEGER NOT NULL DEFAULT 0",
        "display": "TEXT NOT NULL DEFAULT ''",
        "vnc_port": "INTEGER NOT NULL DEFAULT 0",
        "novnc_port": "INTEGER NOT NULL DEFAULT 0",
        "debug_port": "INTEGER NOT NULL DEFAULT 0",
        "owner": "TEXT NOT NULL DEFAULT 'manual'",
        "current_job_id": "TEXT NOT NULL DEFAULT ''",
        "feishu_user_id": "TEXT NOT NULL DEFAULT ''",
        "feishu_user_name": "TEXT NOT NULL DEFAULT ''",
        "feishu_avatar_url": "TEXT NOT NULL DEFAULT ''",
        "runtime_id": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in existing_session_cols:
            conn.execute(f"ALTER TABLE browser_sessions ADD COLUMN {name} {definition}")
    existing_publish_cols = {row[1] for row in conn.execute("PRAGMA table_info(publish_jobs)")}
    if "next_attempt_at" not in existing_publish_cols:
        conn.execute("ALTER TABLE publish_jobs ADD COLUMN next_attempt_at TEXT NOT NULL DEFAULT ''")
    if "deleted_at" not in existing_publish_cols:
        conn.execute("ALTER TABLE publish_jobs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    if "product_link" not in existing_publish_cols:
        conn.execute("ALTER TABLE publish_jobs ADD COLUMN product_link TEXT NOT NULL DEFAULT ''")
    existing_product_cols = {row[1] for row in conn.execute("PRAGMA table_info(tiktok_products)")}
    if "product_url" not in existing_product_cols:
        conn.execute("ALTER TABLE tiktok_products ADD COLUMN product_url TEXT NOT NULL DEFAULT ''")
    if "keep_observing" not in existing_publish_cols:
        try:
            conn.execute("ALTER TABLE publish_jobs ADD COLUMN keep_observing INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    if "manual_publish" not in existing_publish_cols:
        conn.execute("ALTER TABLE publish_jobs ADD COLUMN manual_publish INTEGER NOT NULL DEFAULT 0")
    if "status_detail" not in existing_publish_cols:
        conn.execute("ALTER TABLE publish_jobs ADD COLUMN status_detail TEXT NOT NULL DEFAULT ''")
    collect_columns = {
        "collect_settings": {
            "feishu_target_json": "TEXT NOT NULL DEFAULT '{}'",
            "date_rule": "TEXT NOT NULL DEFAULT 'previous_day'",
            "publish_date_start": "TEXT NOT NULL DEFAULT ''",
            "publish_date_end": "TEXT NOT NULL DEFAULT ''",
        },
        "collect_jobs": {
            "feishu_target_json": "TEXT NOT NULL DEFAULT '{}'",
            "publish_date_start": "TEXT NOT NULL DEFAULT ''",
            "publish_date_end": "TEXT NOT NULL DEFAULT ''",
            "status_detail": "TEXT NOT NULL DEFAULT ''",
        },
        "collect_results": {
            "feishu_target_json": "TEXT NOT NULL DEFAULT '{}'",
            "feishu_record_id": "TEXT NOT NULL DEFAULT ''",
            "feishu_sync_status": "TEXT NOT NULL DEFAULT ''",
            "feishu_sync_error": "TEXT NOT NULL DEFAULT ''",
            "feishu_synced_at": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in collect_columns.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    conn.commit()


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _clean_text(value: Any, max_len: int = 1000) -> str:
    return str(value or "").strip()[:max_len]


def _clean_status(value: Any, default: str = STATUS_ACTIVE) -> str:
    raw = _clean_text(value, 40)
    if not raw:
        return default
    return STATUS_MAP.get(raw.lower(), STATUS_MAP.get(raw, raw if raw in {STATUS_ACTIVE, STATUS_PAUSED, STATUS_ERROR} else default))


def _clean_account_status(value: Any, default: str = ACCOUNT_STATUS_ACTIVE) -> str:
    raw = _clean_text(value, 40)
    if not raw:
        return default
    return ACCOUNT_STATUS_MAP.get(raw.lower(), ACCOUNT_STATUS_MAP.get(raw, raw if raw in {ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_PAUSED, ACCOUNT_STATUS_ERROR} else default))


def _allocate_port(conn: sqlite3.Connection, current_id: int = 0) -> int:
    if current_id:
        row = conn.execute("SELECT local_port FROM proxy_profiles WHERE id = ?", (current_id,)).fetchone()
        if row and int(row["local_port"] or 0):
            return int(row["local_port"])
    used = {int(row["local_port"]) for row in conn.execute("SELECT local_port FROM proxy_profiles WHERE local_port > 0")}
    for port in range(PROXY_PORT_START, PROXY_PORT_END + 1):
        if port not in used:
            return port
    raise ValueError(f"proxy port range exhausted: {PROXY_PORT_START}-{PROXY_PORT_END}")


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


def _row_to_pool(
    row: sqlite3.Row,
    account_count: int = 0,
    account_names: list[str] | None = None,
    pending_job_count: int = 0,
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "source_type": row["source_type"],
        "source_uri": row["source_uri"],
        "dialer_proxy": row["dialer_proxy"],
        "expected_exit_ip": row["expected_exit_ip"],
        "region": row["region"],
        "local_port": row["local_port"],
        "detected_exit_ip": row["detected_exit_ip"],
        "detected_country": row["detected_country"],
        "detected_region": row["detected_region"],
        "detected_city": row["detected_city"],
        "detected_address": row["detected_address"],
        "detected_at": row["detected_at"],
        "auto_check_failures": int(row["auto_check_failures"] or 0),
        "next_auto_check_at": row["next_auto_check_at"],
        "last_auto_check_at": row["last_auto_check_at"],
        "status": _clean_status(row["status"]),
        "notes": row["notes"],
        "parse_status": row["parse_status"],
        "parse_error": row["parse_error"],
        "mihomo_name": row["mihomo_name"],
        "parsed": _json_loads(row["parsed_json"], {}),
        "mihomo_proxy": _json_loads(row["mihomo_proxy_json"], {}),
        "account_count": account_count,
        "account_names": account_names or [],
        "pending_job_count": pending_job_count,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_account(row: sqlite3.Row) -> dict[str, Any]:
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
        "profile": _json_loads(row["profile_json"], {}),
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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            parts = stat_path.read_text(encoding="utf-8", errors="ignore").split()
            if len(parts) > 2 and parts[2] == "Z":
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _pid_alive(pid):
            break
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(0.3)
    for _ in range(10):
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited:
                return
        except (ChildProcessError, OSError):
            return
        time.sleep(0.1)


def _terminate_session_processes(row: sqlite3.Row | dict[str, Any]) -> None:
    for key in ("pid", "websockify_pid", "x11vnc_pid", "xvfb_pid"):
        try:
            pid = int(row[key] or 0)
        except Exception:
            pid = 0
        if pid:
            _terminate_pid(pid)


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


def _iso_epoch(value: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def _active_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM browser_sessions WHERE status IN ('starting','running','observing') ORDER BY updated_at DESC").fetchall()
    active = []
    now = now_iso()
    for row in rows:
        pid = int(row["pid"] or 0)
        pending_expired = not row["account_id"] and _iso_epoch(str(row["created_at"] or "")) + pending_login_ttl_seconds() <= time.time()
        if str(row["runtime_id"] or "") != RUNTIME_ID:
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


def _allocate_session_slot(conn: sqlite3.Connection) -> int:
    max_slots = browser_max_slots()
    used = {int(row["slot"] or 0) for row in _active_sessions(conn)}
    for slot in range(1, max_slots + 1):
        if slot not in used and _slot_ports_available(slot):
            return slot
    raise ValueError(f"浏览器观测槽位已满或端口被占用，当前最多同时运行 {max_slots} 个")


def _allocate_manual_slot(conn: sqlite3.Connection) -> int:
    if any(int(row["slot"] or 0) == 0 for row in _active_sessions(conn)):
        raise ValueError("手动登录观测通道正在使用，请先完成或关闭当前登录")
    if not _slot_ports_available(0):
        ports = _slot_ports(0)
        raise ValueError(f"手动登录观测通道端口被占用：VNC {ports['vnc_port']} / noVNC {ports['novnc_port']} / CDP {ports['debug_port']}")
    return 0


def _browser_binary() -> str:
    configured = os.getenv("TIKTOK_BROWSER_BIN", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(["google-chrome-stable", "google-chrome", "chromium-browser", "chromium"])
    for item in candidates:
        if not item:
            continue
        found = shutil.which(item)
        if found:
            return found
        if Path(item).exists():
            return item
    for root in (
        Path.home() / ".cache" / "ms-playwright",
        Path("/root/.cache/ms-playwright"),
        Path("/ms-playwright"),
    ):
        if root.exists():
            for pattern in ("chromium-*/chrome-linux64/chrome", "chromium-*/chrome-linux/chrome"):
                for chrome in root.glob(pattern):
                    return str(chrome)
    raise ValueError("服务器未找到 Chromium/Chrome；请配置 TIKTOK_BROWSER_BIN 或安装浏览器")


def _required_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    raise ValueError(f"服务器未找到 {name}；请重建镜像或安装 noVNC 隔离依赖")


def _novnc_web_dir() -> str:
    configured = os.getenv("NOVNC_WEB_DIR", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(["/usr/share/novnc", "/usr/local/share/novnc"])
    for item in candidates:
        if item and Path(item).exists():
            return item
    raise ValueError("服务器未找到 noVNC Web 目录；请安装 novnc")


def _slot_ports(slot: int) -> dict[str, Any]:
    # Auto slots occupy the first ports after the reserved server desktop;
    # the single manual login slot is placed after all auto slots.
    max_slots = browser_max_slots()
    offset = slot if slot > 0 else max_slots + max(1, NOVNC_MANUAL_PORTS)
    return {
        "display": f":{XVFB_DISPLAY_BASE + slot}",
        "vnc_port": VNC_PORT + offset,
        "novnc_port": NOVNC_PORT + offset,
        "debug_port": CDP_PORT + slot,
    }


def _display_socket_active(path: Path) -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.15)
    try:
        client.connect(str(path))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True
    finally:
        client.close()


def _slot_ports_available(slot: int) -> bool:
    ports = _slot_ports(slot)
    display_number = str(ports["display"]).lstrip(":")
    display_socket = Path(f"/tmp/.X11-unix/X{display_number}")
    if display_socket.exists():
        if _display_socket_active(display_socket):
            return False
        try:
            display_socket.unlink()
            Path(f"/tmp/.X{display_number}-lock").unlink(missing_ok=True)
        except OSError:
            return False
    return not any(
        _port_open("127.0.0.1", int(ports[key]), timeout=0.15)
        for key in ("vnc_port", "novnc_port", "debug_port")
    )


def _public_novnc_url(port: int) -> str:
    parsed = urlparse(DEFAULT_NOVNC_PUBLIC_URL)
    host = parsed.hostname or "192.168.1.254"
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}:{port}/vnc.html?autoconnect=1&resize=scale"


def _wait_for_port(port: int, label: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open("127.0.0.1", port, timeout=0.4):
            return
        time.sleep(0.2)
    raise ValueError(f"{label} 端口 {port} 未在 {timeout:.0f}s 内启动")


def _open_process(
    log_dir: Path,
    name: str,
    args: list[str],
    env: dict[str, str] | None = None,
    user: int | None = None,
    group: int | None = None,
) -> subprocess.Popen:
    stdout = open(log_dir / f"{name}.log", "ab")
    stderr = open(log_dir / f"{name}.err.log", "ab")
    return subprocess.Popen(
        args,
        cwd=str(ROOT),
        env=env or os.environ.copy(),
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        start_new_session=True,
        user=user,
        group=group,
    )


def _launch_observation_channel(slot: int, session_id: int, log_dir: Path) -> dict[str, Any]:
    ports = _slot_ports(slot)
    display = str(ports["display"])
    vnc_port = int(ports["vnc_port"])
    novnc_port = int(ports["novnc_port"])
    xvfb = _required_binary("Xvfb")
    x11vnc = _required_binary("x11vnc")
    websockify = _required_binary("websockify")
    novnc_web = _novnc_web_dir()

    if not _slot_ports_available(slot):
        raise ValueError(f"观测槽位 {slot} 的显示或端口已被其他服务占用")

    xvfb_proc = _open_process(log_dir, "xvfb", [xvfb, display, "-screen", "0", "1280x900x24", "-nolisten", "tcp"])
    time.sleep(0.8)
    if not _pid_alive(int(xvfb_proc.pid)):
        raise ValueError("独立 Xvfb 显示通道启动失败")

    x11vnc_proc = _open_process(
        log_dir,
        "x11vnc",
        [x11vnc, "-display", display, "-rfbport", str(vnc_port), "-localhost", "-forever", "-shared", "-nopw", "-quiet"],
    )
    websockify_proc = None
    try:
        _wait_for_port(vnc_port, "VNC")
        time.sleep(0.2)
        if not _pid_alive(int(x11vnc_proc.pid)):
            raise ValueError(f"独立 VNC 进程未能监听端口 {vnc_port}")
        websockify_proc = _open_process(
            log_dir,
            "websockify",
            [websockify, "--web", novnc_web, str(novnc_port), f"127.0.0.1:{vnc_port}"],
        )
        _wait_for_port(novnc_port, "noVNC")
        time.sleep(0.2)
        if not _pid_alive(int(websockify_proc.pid)):
            raise ValueError(f"独立 noVNC 进程未能监听端口 {novnc_port}")
    except Exception:
        if websockify_proc is not None:
            _terminate_pid(int(websockify_proc.pid))
        _terminate_pid(int(x11vnc_proc.pid))
        _terminate_pid(int(xvfb_proc.pid))
        raise

    return {
        "display": display,
        "vnc_port": vnc_port,
        "novnc_port": novnc_port,
        "channel_url": _public_novnc_url(novnc_port),
        "xvfb_pid": int(xvfb_proc.pid),
        "x11vnc_pid": int(x11vnc_proc.pid),
        "websockify_pid": int(websockify_proc.pid),
    }


def _abs_workspace_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prepare_browser_profile_dir(user_data_dir: Path) -> dict[str, Path]:
    profiles_root = (DATA_DIR / "tiktok_browser_profiles").resolve()
    profile_root = user_data_dir.parent.resolve()
    if profile_root != profiles_root and profiles_root not in profile_root.parents:
        raise ValueError("浏览器 profile 必须位于 data/tiktok_browser_profiles 目录")

    paths = {
        "home": profile_root / "home",
        "config": profile_root / "config",
        "cache": profile_root / "cache",
        "downloads": profile_root / "downloads",
        "runtime": profile_root / "runtime",
        "user_data": user_data_dir,
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    def chown_path(path: Path | str) -> None:
        try:
            os.chown(path, TIKTOK_BROWSER_UID, TIKTOK_BROWSER_GID, follow_symlinks=False)
        except FileNotFoundError:
            pass

    for current_root, dirs, files in os.walk(profile_root, followlinks=False):
        chown_path(current_root)
        for name in dirs:
            chown_path(Path(current_root) / name)
        for name in files:
            chown_path(Path(current_root) / name)
    paths["runtime"].chmod(0o700)
    return paths


def _configure_browser_preferences(user_data_dir: Path) -> None:
    preferences_path = user_data_dir / "Default" / "Preferences"
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    preferences = _json_loads(preferences_path.read_text(encoding="utf-8") if preferences_path.is_file() else "", {})
    if not isinstance(preferences, dict):
        preferences = {}
    if not isinstance(preferences.get("intl"), dict):
        preferences["intl"] = {}
    if not isinstance(preferences.get("translate"), dict):
        preferences["translate"] = {}
    preferences["intl"]["accept_languages"] = TIKTOK_BROWSER_ACCEPT_LANGUAGE
    preferences["translate"]["enabled"] = False
    preferences_path.write_text(json.dumps(preferences, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _decrypt_chrome_cookie(host: str, value: str, encrypted_value: bytes) -> str:
    if value:
        return value
    encrypted = bytes(encrypted_value or b"")
    if not encrypted.startswith((b"v10", b"v11")):
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
        padding = plain[-1]
        if not 1 <= padding <= 16:
            return ""
        plain = plain[:-padding]
        host_digest = hashlib.sha256(host.encode("utf-8")).digest()
        if plain.startswith(host_digest):
            plain = plain[len(host_digest):]
        return plain.decode("utf-8", errors="strict")
    except Exception:
        return ""


def _tiktok_profile_cookies(user_data_dir: str) -> dict[str, str]:
    root = Path(user_data_dir)
    candidates = (root / "Default" / "Cookies", root / "Default" / "Network" / "Cookies")
    for cookie_path in candidates:
        if not cookie_path.is_file():
            continue
        try:
            cookie_conn = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True, timeout=1)
            try:
                rows = cookie_conn.execute(
                    "SELECT host_key, name, value, encrypted_value FROM cookies WHERE host_key LIKE '%tiktok%'"
                ).fetchall()
            finally:
                cookie_conn.close()
        except sqlite3.Error:
            continue
        cookies: dict[str, str] = {}
        for host, name, value, encrypted_value in rows:
            decoded = _decrypt_chrome_cookie(str(host), str(value or ""), encrypted_value)
            if decoded:
                cookies[str(name)] = decoded
        return cookies
    return {}


def _proxy_json_with_cookies(url: str, proxy_port: int, cookies: dict[str, str], timeout: float = 10.0) -> tuple[bool, Any, str]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": TIKTOK_BROWSER_ACCEPT_LANGUAGE,
            "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
            "Referer": "https://www.tiktok.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
        return True, json.loads(raw), ""
    except HTTPError as exc:
        return False, None, f"HTTP {exc.code}"
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return False, None, str(exc)


def _tiktok_avatar_url(item: dict[str, Any]) -> str:
    for key in (
        "avatar_url",
        "avatarUrl",
        "avatar_thumb",
        "avatarThumb",
        "avatar_larger",
        "avatarLarger",
        "avatar_medium",
    ):
        value = item.get(key)
        candidates: list[Any]
        if isinstance(value, dict):
            candidates = [value.get("url"), *(value.get("url_list") or [])]
        elif isinstance(value, list):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            url = str(candidate or "").strip()
            if url.startswith(("https://", "http://")):
                return url
    return ""


def _tiktok_identity(body: Any) -> dict[str, str]:
    if not isinstance(body, dict):
        return {}
    candidates: list[dict[str, Any]] = []
    for item in (body.get("data"), body.get("user"), body):
        if isinstance(item, dict):
            candidates.append(item)
            if isinstance(item.get("user"), dict):
                candidates.append(item["user"])
    for item in candidates:
        username = str(item.get("username") or item.get("unique_id") or item.get("uniqueId") or "").strip().lstrip("@")
        user_id = str(item.get("user_id") or item.get("user_id_str") or item.get("uid") or "").strip()
        display_name = str(item.get("screen_name") or item.get("nickname") or item.get("display_name") or "").strip()
        if username or user_id:
            return {
                "username": username or f"uid_{user_id}",
                "display_name": display_name,
                "user_id": user_id,
                "avatar_url": _tiktok_avatar_url(item),
            }
    return {}


def _launch_browser_for_session(profile: dict[str, Any], pool: sqlite3.Row, session_id: int, display: str, debug_port: int, start_url: str) -> tuple[int, str]:
    isolation = profile.get("isolation") if isinstance(profile.get("isolation"), dict) else {}
    user_data_dir = _abs_workspace_path(str(isolation.get("user_data_dir") or f"data/tiktok_browser_profiles/session-{session_id}/user-data"))
    _configure_browser_preferences(user_data_dir)
    profile_paths = _prepare_browser_profile_dir(user_data_dir)
    log_dir = _abs_workspace_path(f"data/tiktok_browser_sessions/{session_id}")
    browser = _browser_binary()
    proxy_port = int(pool["local_port"] or 0)
    if not proxy_port:
        raise ValueError("代理没有专用本地端口，不能启动独立浏览器")
    args = [
        browser,
        f"--user-data-dir={user_data_dir}",
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        f"--lang={TIKTOK_BROWSER_LOCALE}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-translate",
        "--disable-background-networking",
        "--disable-default-apps",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-session-crashed-bubble",
        "--window-size=1280,900",
        "--new-window",
        start_url,
    ]
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["HOME"] = str(profile_paths["home"])
    env["XDG_CONFIG_HOME"] = str(profile_paths["config"])
    env["XDG_CACHE_HOME"] = str(profile_paths["cache"])
    env["XDG_RUNTIME_DIR"] = str(profile_paths["runtime"])
    env["LANGUAGE"] = "en_US:en"
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    proc = _open_process(
        log_dir,
        "browser",
        args,
        env=env,
        user=TIKTOK_BROWSER_UID,
        group=TIKTOK_BROWSER_GID,
    )
    return int(proc.pid), str(user_data_dir)


def _browser_ip_check_urls() -> list[str]:
    configured = os.getenv("PROXY_BROWSER_IP_CHECK_URLS", "").strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]
    return _unique_urls([
        "https://ifconfig.co/json",
        "https://ipinfo.io/json",
        "https://httpbin.org/ip",
        "https://api.ipify.org?format=json",
        "https://icanhazip.com",
        *_proxy_ip_check_urls(),
    ])


def _unique_urls(urls: list[str]) -> list[str]:
    unique: list[str] = []
    for url in urls:
        value = str(url or "").strip()
        if value and value not in unique:
            unique.append(value)
    return unique


def _proxy_ip_check_urls() -> list[str]:
    configured = os.getenv("PROXY_IP_CHECK_URLS", "").strip()
    if configured:
        return _unique_urls([item.strip() for item in configured.split(",")])
    primary = os.getenv("PROXY_IP_CHECK_URL", "https://ifconfig.co/json").strip()
    return _unique_urls([
        primary,
        "https://ipinfo.io/json",
        "http://ip-api.com/json/?fields=status,country,regionName,city,query",
        "https://api.ipify.org?format=json",
    ])


def _browser_ip_from_response(raw: str) -> str:
    value = raw.strip()
    try:
        body = json.loads(value)
    except json.JSONDecodeError:
        body = value
    candidates: list[str] = []
    if isinstance(body, dict):
        candidates.extend(str(body.get(key) or "") for key in ("query", "ip", "origin"))
    elif isinstance(body, str):
        candidates.append(body)
    for candidate in candidates:
        for item in candidate.replace(",", " ").split():
            try:
                return str(ipaddress.ip_address(item.strip()))
            except ValueError:
                continue
    return ""


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


def _browser_account_avatar(debug_port: int) -> bytes:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            if not browser.contexts or not browser.contexts[0].pages:
                raise ValueError("Chrome 没有可用的 TikTok 页面")
            page = browser.contexts[0].pages[0]
            try:
                page.wait_for_function(
                    """() => [...document.images].some((image) => {
                        const rect = image.getBoundingClientRect();
                        const src = image.currentSrc || image.src || "";
                        return src.startsWith("https://") && src.includes("-avt-") &&
                            !image.alt && rect.width >= 24 && rect.width <= 44 &&
                            rect.height >= 24 && rect.height <= 44;
                    })""",
                    timeout=5000,
                )
            except Exception:
                pass
            result = page.evaluate(
                """async () => {
                    const rows = [...document.images].map((image) => {
                        const rect = image.getBoundingClientRect();
                        return {
                            src: image.currentSrc || image.src || "",
                            alt: image.alt || "",
                            width: rect.width,
                            height: rect.height,
                        };
                    });
                    const candidates = rows.filter((row) =>
                        row.src.startsWith("https://") &&
                        row.src.includes("-avt-") &&
                        !row.alt &&
                        row.width >= 24 && row.width <= 44 &&
                        row.height >= 24 && row.height <= 44
                    );
                    const counts = new Map();
                    for (const candidate of candidates) {
                        counts.set(candidate.src, (counts.get(candidate.src) || 0) + 1);
                    }
                    candidates.sort((left, right) =>
                        (counts.get(right.src) - counts.get(left.src)) ||
                        (Math.abs(left.width - 32) - Math.abs(right.width - 32))
                    );
                    if (!candidates.length) return {error: "当前 TikTok 页面未找到账号头像"};
                    try {
                        const response = await fetch(candidates[0].src, {cache: "force-cache"});
                        if (!response.ok) return {error: `头像请求返回 HTTP ${response.status}`};
                        const bytes = new Uint8Array(await response.arrayBuffer());
                        let binary = "";
                        for (let offset = 0; offset < bytes.length; offset += 8192) {
                            binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
                        }
                        return {data: btoa(binary)};
                    } catch (error) {
                        return {error: String(error)};
                    }
                }"""
            )
        if not isinstance(result, dict) or not result.get("data"):
            raise ValueError(str((result or {}).get("error") or "TikTok 头像读取失败"))
        return base64.b64decode(str(result["data"]), validate=True)
    except Exception as exc:
        raise ValueError(f"TikTok 头像读取失败：{exc}") from exc


def _detect_browser_exit_ip(debug_port: int) -> str:
    failures: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            if not browser.contexts:
                raise ValueError("Chrome 没有可用的浏览器上下文")
            for target in _browser_ip_check_urls():
                page = None
                try:
                    page = browser.contexts[0].new_page()
                    response = page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    if response is not None and not response.ok:
                        raise ValueError(f"HTTP {response.status}")
                    observed_ip = _browser_ip_from_response(page.locator("body").inner_text(timeout=5000))
                    if not observed_ip:
                        raise ValueError("没有返回合法出口 IP")
                    return observed_ip
                except Exception as exc:
                    failures.append(f"{urlparse(target).netloc or target}: {exc}")
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
        raise ValueError("；".join(failures) or "没有可用的 IP 查询接口")
    except Exception as exc:
        raise ValueError(f"浏览器出口 IP 校验失败：{exc}") from exc


def parse_vless_uri(uri: str, fallback_name: str = "") -> dict[str, Any]:
    uri = _clean_text(uri, 10000)
    if not uri:
        return {"parse_status": "manual", "parsed": {}, "mihomo_proxy": {}, "mihomo_name": fallback_name}
    if not uri.startswith("vless://"):
        raise ValueError("Only vless:// URI is supported")

    parsed = urlparse(uri)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    if not parsed.username and not parsed.port:
        encoded = (parsed.netloc + parsed.path).strip("/")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
            if "@" in decoded:
                userinfo, hostinfo = decoded.rsplit("@", 1)
                uuid_part = userinfo.split(":", 1)[-1]
                rebuilt = f"vless://{uuid_part}@{hostinfo}"
                parsed = urlparse(rebuilt)
        except (binascii.Error, UnicodeError, ValueError):
            pass
    uuid = unquote(parsed.username or "")
    server = parsed.hostname or ""
    port = parsed.port
    name = unquote(parsed.fragment or "") or unquote(query.get("remarks") or query.get("remark") or "") or fallback_name or server or "vless-node"
    if not uuid or not server or not port:
        raise ValueError("VLESS URI must include uuid, server and port")

    network = query.get("type") or query.get("network") or "tcp"
    security = query.get("security", "")
    tls_enabled = security in {"tls", "reality"} or query.get("tls") in {"1", "true", "tls"}
    reality_enabled = security == "reality" or bool(query.get("pbk"))
    mihomo: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": server,
        "port": int(port),
        "uuid": uuid,
        "network": network,
        "udp": True,
    }
    if query.get("flow"):
        mihomo["flow"] = query["flow"]
    if tls_enabled or reality_enabled:
        mihomo["tls"] = True
    if reality_enabled:
        mihomo["reality-opts"] = {}
        if query.get("pbk"):
            mihomo["reality-opts"]["public-key"] = query["pbk"]
        if query.get("sid"):
            mihomo["reality-opts"]["short-id"] = query["sid"]
    if query.get("sni") or query.get("peer"):
        mihomo["servername"] = query.get("sni") or query.get("peer")
    if query.get("fp"):
        mihomo["client-fingerprint"] = query["fp"]
    elif reality_enabled:
        mihomo["client-fingerprint"] = "chrome"
    if network == "ws":
        ws_opts: dict[str, Any] = {}
        if query.get("path"):
            ws_opts["path"] = query["path"]
        if query.get("host"):
            ws_opts["headers"] = {"Host": query["host"]}
        if ws_opts:
            mihomo["ws-opts"] = ws_opts
    if network == "grpc" and query.get("serviceName"):
        mihomo["grpc-opts"] = {"grpc-service-name": query["serviceName"]}

    return {
        "parse_status": "ok",
        "mihomo_name": name,
        "parsed": {
            "uuid": uuid,
            "server": server,
            "port": int(port),
            "network": network,
            "security": security,
            "query": query,
            "name": name,
        },
        "mihomo_proxy": mihomo,
    }


def parse_static_proxy_uri(uri: str, fallback_name: str = "") -> dict[str, Any]:
    uri = _clean_text(uri, 10000)
    if not uri:
        return {"parse_status": "manual", "parsed": {}, "mihomo_proxy": {}, "mihomo_name": fallback_name}

    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks", "socks5", "socks5h"}:
        raise ValueError("静态代理仅支持 socks://、socks5://、http:// 或 https://")

    if scheme == "socks" and not parsed.username and "@" not in parsed.netloc and ":" not in parsed.netloc:
        encoded = (parsed.netloc + parsed.path).strip("/")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            authority = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            parsed = urlparse(f"socks5://{authority}")
            scheme = "socks5"
        except (binascii.Error, UnicodeError, ValueError) as exc:
            raise ValueError("socks:// 订阅内容不是有效的 Base64 代理地址") from exc

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("静态代理端口无效") from exc
    server = parsed.hostname or ""
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not server or not port:
        raise ValueError("静态代理必须包含服务器和端口")

    name = unquote(parsed.fragment or "") or fallback_name or server or "static-proxy"
    mihomo_type = "socks5" if scheme in {"socks", "socks5", "socks5h"} else "http"
    mihomo: dict[str, Any] = {
        "name": name,
        "type": mihomo_type,
        "server": server,
        "port": int(port),
    }
    if username:
        mihomo["username"] = username
    if password:
        mihomo["password"] = password
    if mihomo_type == "socks5":
        mihomo["udp"] = True
    if scheme == "https":
        mihomo["tls"] = True

    return {
        "parse_status": "ok",
        "mihomo_name": name,
        "parsed": {
            "scheme": scheme,
            "server": server,
            "port": int(port),
            "username": username,
            "has_password": bool(password),
            "name": name,
        },
        "mihomo_proxy": mihomo,
    }


def list_state() -> dict[str, Any]:
    with connect() as conn:
        _active_sessions(conn)
        counts = {
            int(row["proxy_profile_id"]): int(row["count"])
            for row in conn.execute("SELECT proxy_profile_id, COUNT(*) AS count FROM tiktok_accounts WHERE deleted_at = '' AND proxy_bound = 1 GROUP BY proxy_profile_id")
        }
        names: dict[int, list[str]] = {}
        for row in conn.execute("SELECT proxy_profile_id, username FROM tiktok_accounts WHERE deleted_at = '' AND proxy_bound = 1 ORDER BY username"):
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
            for row in conn.execute("SELECT * FROM proxy_profiles ORDER BY updated_at DESC, id DESC")
        ]
        accounts = [_row_to_account(row) for row in conn.execute("SELECT * FROM tiktok_accounts WHERE deleted_at = '' ORDER BY updated_at DESC, id DESC")]
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


def upsert_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(products, list):
        raise ValueError("products must be a list")
    now = now_iso()
    conn = connect()
    try:
        for position, raw in enumerate(products):
            if not isinstance(raw, dict):
                continue
            product = _product_payload(raw, source=_clean_text(raw.get("source"), 80) or "my_shop")
            conn.execute(
                """
                INSERT INTO tiktok_products (
                    product_id, product_name, product_url, image_url, price, stock, status, source, sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    product_name = excluded.product_name,
                    product_url = excluded.product_url,
                    image_url = excluded.image_url,
                    price = excluded.price,
                    stock = excluded.stock,
                    status = excluded.status,
                    source = excluded.source,
                    sort_order = excluded.sort_order,
                    updated_at = excluded.updated_at
                """,
                (
                    product["product_id"],
                    product["product_name"],
                    product["product_url"],
                    product["image_url"],
                    product["price"],
                    product["stock"],
                    product["status"],
                    product["source"],
                    int(raw.get("sort_order") or position),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return list_products()


def upsert_pool(payload: dict[str, Any]) -> dict[str, Any]:
    pool_id = int(payload.get("id") or 0)
    name = _clean_text(payload.get("name"), 160)
    source_uri = _clean_text(payload.get("source_uri"), 10000)
    expected_exit_ip = _clean_text(payload.get("expected_exit_ip"), 80)
    dialer_proxy = _clean_text(payload.get("dialer_proxy"), 160)
    source_type = _clean_text(payload.get("source_type"), 40)
    if not source_type:
        source_type = "static" if source_uri.lower().startswith(("socks://", "socks5://", "socks5h://", "http://", "https://")) else "vless"
    if source_type not in {"vless", "static"}:
        raise ValueError("代理类型必须为 vless 或 static")

    parse_status = "manual"
    parse_error = ""
    parsed: dict[str, Any] = {}
    mihomo_proxy: dict[str, Any] = {}
    mihomo_name = name
    if source_uri:
        try:
            parsed_result = parse_vless_uri(source_uri, fallback_name=name) if source_type == "vless" else parse_static_proxy_uri(source_uri, fallback_name=name)
            parse_status = str(parsed_result["parse_status"])
            parsed = parsed_result["parsed"]
            mihomo_proxy = parsed_result["mihomo_proxy"]
            mihomo_name = str(parsed_result.get("mihomo_name") or name)
        except Exception as exc:
            parse_status = "error"
            parse_error = str(exc)
    if not name:
        name = mihomo_name or expected_exit_ip
    if mihomo_proxy and not mihomo_proxy.get("name"):
        mihomo_proxy["name"] = name
    if source_type == "static" and dialer_proxy and mihomo_proxy:
        mihomo_proxy["dialer-proxy"] = dialer_proxy

    now = now_iso()
    values = {
        "name": name,
        "source_type": source_type,
        "source_uri": source_uri,
        "dialer_proxy": dialer_proxy if source_type == "static" else "",
        "expected_exit_ip": expected_exit_ip,
        "region": _clean_text(payload.get("region"), 80),
        "status": _clean_status(payload.get("status")),
        "notes": _clean_text(payload.get("notes"), 2000),
        "parse_status": parse_status,
        "parse_error": parse_error,
        "mihomo_name": mihomo_name or name,
        "parsed_json": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
        "mihomo_proxy_json": json.dumps(mihomo_proxy, ensure_ascii=False, separators=(",", ":")),
        "updated_at": now,
    }
    with connect() as conn:
        if pool_id:
            exists = conn.execute("SELECT id FROM proxy_profiles WHERE id = ?", (pool_id,)).fetchone()
            if not exists:
                raise ValueError("proxy profile not found")
            values["local_port"] = _allocate_port(conn, pool_id)
            conn.execute(
                """
                UPDATE proxy_profiles
                SET name=:name, source_type=:source_type, source_uri=:source_uri, dialer_proxy=:dialer_proxy,
                    expected_exit_ip=:expected_exit_ip, region=:region, status=:status, local_port=:local_port,
                    notes=:notes, parse_status=:parse_status, parse_error=:parse_error,
                    mihomo_name=:mihomo_name, parsed_json=:parsed_json,
                    mihomo_proxy_json=:mihomo_proxy_json, updated_at=:updated_at
                WHERE id=:id
                """,
                {**values, "id": pool_id},
            )
        else:
            values["local_port"] = _allocate_port(conn, 0)
            cur = conn.execute(
                """
                INSERT INTO proxy_profiles (
                    name, source_type, source_uri, dialer_proxy, expected_exit_ip, region, status, notes, local_port,
                    parse_status, parse_error, mihomo_name, parsed_json, mihomo_proxy_json,
                    created_at, updated_at
                ) VALUES (
                    :name, :source_type, :source_uri, :dialer_proxy, :expected_exit_ip, :region, :status, :notes, :local_port,
                    :parse_status, :parse_error, :mihomo_name, :parsed_json, :mihomo_proxy_json,
                    :created_at, :updated_at
                )
                """,
                {**values, "created_at": now},
            )
            pool_id = int(cur.lastrowid)
        conn.commit()
    core = ensure_proxy_cores(restart=True, required=True) if _sing_box_reality_enabled() else {}
    return {"pool": get_pool(pool_id), **list_state(), **core}


def get_pool(pool_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (pool_id,)).fetchone()
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


def _sing_box_reality_enabled() -> bool:
    return os.getenv("PROXY_REALITY_CORE", "mihomo").strip().lower() == "sing-box"


def _sing_box_reality_pool(row: sqlite3.Row | dict[str, Any]) -> bool:
    if not _sing_box_reality_enabled() or str(row["source_type"] or "") != "vless":
        return False
    parsed = _json_loads(str(row["parsed_json"] or ""), {})
    query = parsed.get("query") if isinstance(parsed.get("query"), dict) else {}
    return (
        str(parsed.get("network") or "tcp") == "tcp"
        and bool(query.get("pbk"))
        and bool(parsed.get("uuid"))
        and bool(parsed.get("server"))
        and int(parsed.get("port") or 0) > 0
        and int(row["local_port"] or 0) > 0
    )


def sing_box_export() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM proxy_profiles WHERE status <> ? ORDER BY id",
            (STATUS_PAUSED,),
        ).fetchall()
    inbounds: list[dict[str, Any]] = []
    outbounds: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    pools: list[dict[str, Any]] = []
    default_fingerprint = os.getenv("PROXY_REALITY_DEFAULT_FINGERPRINT", "safari").strip() or "safari"
    for row in rows:
        if not _sing_box_reality_pool(row):
            continue
        parsed = _json_loads(str(row["parsed_json"] or ""), {})
        query = parsed.get("query") if isinstance(parsed.get("query"), dict) else {}
        inbound_tag = f"reality-in-{int(row['id'])}"
        outbound_tag = f"reality-out-{int(row['id'])}"
        tls = {
            "enabled": True,
            "server_name": str(query.get("sni") or query.get("peer") or parsed.get("server") or ""),
            "utls": {
                "enabled": True,
                "fingerprint": str(query.get("fp") or default_fingerprint),
            },
            "reality": {
                "enabled": True,
                "public_key": str(query.get("pbk") or ""),
                "short_id": str(query.get("sid") or ""),
            },
        }
        outbound: dict[str, Any] = {
            "type": "vless",
            "tag": outbound_tag,
            "server": str(parsed.get("server") or ""),
            "server_port": int(parsed.get("port") or 0),
            "uuid": str(parsed.get("uuid") or ""),
            "network": "tcp",
            "tls": tls,
        }
        if query.get("flow"):
            outbound["flow"] = str(query["flow"])
        inbounds.append(
            {
                "type": "mixed",
                "tag": inbound_tag,
                "listen": "127.0.0.1",
                "listen_port": int(row["local_port"] or 0),
            }
        )
        outbounds.append(outbound)
        rules.append({"inbound": [inbound_tag], "action": "route", "outbound": outbound_tag})
        pools.append(
            {
                "id": int(row["id"]),
                "name": str(row["name"] or ""),
                "local_port": int(row["local_port"] or 0),
                "fingerprint": tls["utls"]["fingerprint"],
            }
        )
    outbounds.append({"type": "direct", "tag": "direct"})
    return {
        "config": {
            "log": {"level": "info", "timestamp": True},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": {"rules": rules, "final": "direct"},
        },
        "pools": pools,
        "generated_at": now_iso(),
    }


def _write_sing_box_config() -> dict[str, Any]:
    exported = sing_box_export()
    SING_BOX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(SING_BOX_CONFIG_PATH.parent, 0o700)
    temporary = SING_BOX_CONFIG_PATH.with_suffix(SING_BOX_CONFIG_PATH.suffix + ".tmp")
    temporary.write_text(
        json.dumps(exported["config"], ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, SING_BOX_CONFIG_PATH)
    return exported


def _restart_sing_box_container(required: bool = False) -> dict[str, Any]:
    lookup = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={SING_BOX_COMPOSE_PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={SING_BOX_COMPOSE_SERVICE}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if lookup.returncode != 0:
        raise ValueError(f"查询 sing-box 容器失败：{lookup.stderr.strip() or lookup.stdout.strip()}")
    container_ids = [item.strip() for item in lookup.stdout.splitlines() if item.strip()]
    if not container_ids:
        if required:
            raise ValueError("sing-box 代理核心容器未启动")
        return {"restarted": False, "containers": []}
    restarted = subprocess.run(
        ["docker", "restart", *container_ids],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if restarted.returncode != 0:
        raise ValueError(f"重启 sing-box 代理核心失败：{restarted.stderr.strip() or restarted.stdout.strip()}")
    return {"restarted": True, "containers": container_ids}


def ensure_proxy_cores(restart: bool = False, required: bool = False) -> dict[str, Any]:
    if not _sing_box_reality_enabled():
        return {"sing_box": {"enabled": False}}
    exported = _write_sing_box_config()
    runtime = _restart_sing_box_container(required=required) if restart else {"restarted": False, "containers": []}
    return {
        "sing_box": {
            "enabled": True,
            "config_path": str(SING_BOX_CONFIG_PATH),
            "pools": exported["pools"],
            **runtime,
        }
    }


def delete_pool(pool_id: int) -> dict[str, Any]:
    with connect() as conn:
        pool = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (pool_id,)).fetchone()
        if not pool:
            raise ValueError("proxy profile not found")

        _active_sessions(conn)
        active_session = conn.execute(
            "SELECT id FROM browser_sessions WHERE proxy_profile_id = ? AND status IN ('starting','running','observing') LIMIT 1",
            (pool_id,),
        ).fetchone()
        if active_session:
            raise ValueError("代理仍有运行中的浏览器或观测通道，请先释放")

        count = conn.execute(
            "SELECT COUNT(*) AS count FROM tiktok_accounts WHERE proxy_profile_id = ? AND deleted_at = ''",
            (pool_id,),
        ).fetchone()["count"]
        if int(count):
            raise ValueError("代理仍绑定账号，请先删除或迁移账号")

        archived_account = conn.execute(
            """
            SELECT a.id
            FROM tiktok_accounts AS a
            WHERE a.proxy_profile_id = ? AND a.deleted_at <> ''
              AND (
                EXISTS (SELECT 1 FROM publish_assets WHERE account_id = a.id)
                OR EXISTS (SELECT 1 FROM publish_jobs WHERE account_id = a.id)
                OR EXISTS (SELECT 1 FROM collect_jobs WHERE account_id = a.id)
                OR EXISTS (SELECT 1 FROM collect_results WHERE account_id = a.id)
                OR EXISTS (SELECT 1 FROM collect_errors WHERE account_id = a.id)
              )
            LIMIT 1
            """,
            (pool_id,),
        ).fetchone()
        if archived_account:
            raise ValueError("代理关联的已删除账号仍有发布或采集记录，不能删除")

        sing_box_managed = _sing_box_reality_pool(pool)
        cleanup, backup = ({"removed": False, "port": int(pool["local_port"] or 0)}, None)
        if not sing_box_managed:
            cleanup, backup = _remove_mihomo_listener_config(int(pool["local_port"] or 0))
        try:
            conn.execute("DELETE FROM browser_sessions WHERE proxy_profile_id = ?", (pool_id,))
            conn.execute("DELETE FROM tiktok_accounts WHERE proxy_profile_id = ? AND deleted_at <> ''", (pool_id,))
            conn.execute("DELETE FROM proxy_profiles WHERE id = ?", (pool_id,))
            conn.commit()
        except Exception:
            if backup is not None:
                try:
                    _restore_mihomo_listener_config(*backup)
                except Exception:
                    pass
            raise
    core = ensure_proxy_cores(restart=True, required=True) if sing_box_managed else {}
    return {**list_state(), "mihomo_cleanup": cleanup, **core}


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
        pool_row = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (proxy_profile_id,)).fetchone()
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
        if values["feishu_user_id"]:
            conflict = conn.execute(
                """SELECT username FROM tiktok_accounts
                   WHERE feishu_user_id = ? AND deleted_at = '' AND id <> ?""",
                (values["feishu_user_id"], account_id),
            ).fetchone()
            if conflict:
                raise ValueError(f"该飞书用户已绑定 @{conflict['username']}")
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
    return {"account": get_account(account_id), **list_state()}


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


def account_proxy_bound(account: sqlite3.Row | dict[str, Any]) -> bool:
    try:
        return bool(account["proxy_bound"])
    except (KeyError, IndexError):
        return True


def require_account_proxy_bound(account: sqlite3.Row | dict[str, Any]) -> None:
    if not account_proxy_bound(account):
        raise ValueError("账号未绑定代理，无法执行任务或观测")


def _require_account_binding_idle(conn: sqlite3.Connection, account_id: int) -> None:
    active_session = conn.execute(
        """SELECT id FROM browser_sessions
           WHERE account_id = ? AND status IN ('starting','running','observing')
           LIMIT 1""",
        (account_id,),
    ).fetchone()
    if active_session:
        raise ValueError("账号仍处于唤醒或运行状态，请先休眠账号")
    active_publish = conn.execute(
        """SELECT id FROM publish_jobs
           WHERE account_id = ? AND deleted_at = ''
             AND status IN ('queued','delayed','preparing','uploading','publishing')
           LIMIT 1""",
        (account_id,),
    ).fetchone()
    if active_publish:
        raise ValueError("账号仍有待发布或运行中的发布任务，请先取消或等待完成")
    active_collect = conn.execute(
        """SELECT id FROM collect_jobs
           WHERE account_id = ?
             AND status IN ('queued','delayed','preparing','collecting')
           LIMIT 1""",
        (account_id,),
    ).fetchone()
    if active_collect:
        raise ValueError("账号仍有待执行或运行中的统计采集任务，请先取消或等待完成")


def update_account_proxy_binding(payload: dict[str, Any]) -> dict[str, Any]:
    action = _clean_text(payload.get("action"), 20).lower()
    pool_id = int(payload.get("proxy_profile_id") or payload.get("pool_id") or 0)
    if action not in {"bind", "unbind"}:
        raise ValueError("action must be bind or unbind")
    if not pool_id:
        raise ValueError("proxy_profile_id is required")

    account_id = 0
    now = now_iso()
    with connect() as conn:
        _active_sessions(conn)
        conn.execute("BEGIN IMMEDIATE")
        pool = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (pool_id,)).fetchone()
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
            conn.execute(
                """UPDATE tiktok_accounts
                   SET proxy_bound = 0, last_check_status = '未绑定',
                       last_error = '账号未绑定代理，无法执行任务或观测', updated_at = ?
                   WHERE id = ?""",
                (now, account_id),
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
            _require_account_binding_idle(conn, account_id)
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
        conn.commit()
    return {"account": get_account(account_id), **list_state()}


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
    pool = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (pool_id,)).fetchone()
    if not pool:
        raise ValueError("proxy profile not found")
    return pool, account



def _mihomo_headers() -> dict[str, str]:
    secret = os.getenv("MIHOMO_SECRET", "").strip()
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def _mihomo_request(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 5.0) -> tuple[bool, Any, str]:
    parsed = urlparse(DEFAULT_MIHOMO_API.rstrip("/"))
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 9090, timeout=timeout)
    headers = _mihomo_headers()
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        text = response.read(1024 * 1024).decode("utf-8", errors="replace")
        if not (200 <= response.status < 300):
            return False, None, f"HTTP {response.status}: {text[:300]}"
        return True, json.loads(text) if text else {}, ""
    except Exception as exc:
        return False, None, str(exc)
    finally:
        conn.close()


def _atomic_write(path: Path, body: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(body)
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _reload_mihomo_config() -> None:
    reload_path = os.getenv("MIHOMO_RELOAD_PATH", "/etc/mihomo/config.yaml").strip() or "/etc/mihomo/config.yaml"
    ok, _body, error = _mihomo_request("PUT", "/configs?force=true", {"path": reload_path}, timeout=15)
    if not ok:
        raise ValueError(f"mihomo 配置重载失败：{error}")


def _restore_mihomo_listener_config(path: Path, original: bytes, mode: int) -> None:
    _atomic_write(path, original, mode)
    _reload_mihomo_config()


def _listener_port(block: list[str]) -> int:
    for line in block:
        match = re.match(r"^\s+port:\s*['\"]?(\d+)", line)
        if match:
            return int(match.group(1))
    return 0


def _remove_mihomo_listener_config(
    required_port: int,
) -> tuple[dict[str, Any], tuple[Path, bytes, int] | None]:
    managed_ports = set(range(PROXY_PORT_START, PROXY_PORT_END + 1))
    if required_port not in managed_ports:
        return {"configured": False, "removed_ports": []}, None
    config_value = os.getenv("MIHOMO_CONFIG_PATH", "").strip()
    if not config_value:
        if _port_open("127.0.0.1", required_port, timeout=0.5):
            raise ValueError("mihomo 配置未挂载，无法同步删除正在监听的代理端口")
        return {"configured": False, "removed_ports": []}, None
    path = Path(config_value)
    if not path.is_file():
        raise ValueError(f"mihomo 配置文件不存在：{path}")

    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    lines = original.decode("utf-8").splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.startswith("listeners:")), -1)
    if start < 0:
        if _port_open("127.0.0.1", required_port, timeout=0.5):
            raise ValueError(f"mihomo 仍监听 {required_port}，但配置中找不到 listeners 段")
        return {"configured": True, "removed_ports": []}, None
    end = next(
        (index for index in range(start + 1, len(lines)) if re.match(r"^[A-Za-z0-9_-]+:", lines[index])),
        len(lines),
    )
    item_starts = [index for index in range(start + 1, end) if lines[index].startswith("-")]
    remove_ranges: list[tuple[int, int]] = []
    removed_ports: list[int] = []
    for position, item_start in enumerate(item_starts):
        item_end = item_starts[position + 1] if position + 1 < len(item_starts) else end
        port = _listener_port(lines[item_start:item_end])
        if port == required_port:
            remove_ranges.append((item_start, item_end))
            removed_ports.append(port)
    if not remove_ranges:
        if _port_open("127.0.0.1", required_port, timeout=0.5):
            raise ValueError(f"mihomo 仍监听 {required_port}，但没有找到可安全删除的配置块")
        return {"configured": True, "removed_ports": []}, None

    remove_indexes = {index for left, right in remove_ranges for index in range(left, right)}
    updated = "".join(line for index, line in enumerate(lines) if index not in remove_indexes).encode("utf-8")
    _atomic_write(path, updated, mode)
    try:
        _reload_mihomo_config()
    except Exception:
        _atomic_write(path, original, mode)
        try:
            _reload_mihomo_config()
        except Exception:
            pass
        raise
    return {"configured": True, "removed_ports": sorted(removed_ports)}, (path, original, mode)


def _switch_mihomo_node(node_name: str) -> dict[str, Any]:
    ok, body, error = _mihomo_request("GET", "/proxies")
    if not ok or not isinstance(body, dict):
        raise ValueError(f"无法读取服务器 mihomo 节点：{error}")
    proxies = body.get("proxies") if isinstance(body.get("proxies"), dict) else {}
    if node_name not in proxies:
        raise ValueError(f"节点 {node_name} 没有加载到服务器 mihomo；请先把导出的配置导入 mihomo 并重载")
    preferred = ["GLOBAL", "Proxy", "代理", "CoffeeCloud", "自动选择"]
    candidates = []
    for name, item in proxies.items():
        all_nodes = item.get("all") if isinstance(item, dict) else None
        if isinstance(all_nodes, list) and node_name in all_nodes:
            candidates.append(str(name))
    candidates.sort(key=lambda item: (0 if item in preferred else 1, preferred.index(item) if item in preferred else 999, item))
    switched = []
    for group in candidates[:3]:
        ok, _body, error = _mihomo_request("PUT", f"/proxies/{quote_path(group)}", {"name": node_name})
        if ok:
            switched.append(group)
    if candidates and not switched:
        raise ValueError(f"mihomo 找到节点 {node_name}，但切换策略组失败")
    return {"node": node_name, "groups": switched, "loaded": True}


def quote_path(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _proxy_get_json(url: str, proxy_port: int, timeout: float = 10.0) -> tuple[bool, Any, str]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    request = Request(url, headers={"User-Agent": "ShortVideoAnalyzer/1.0"})
    last_error = ""
    for _attempt in range(PROXY_REQUEST_ATTEMPTS):
        try:
            with opener.open(request, timeout=timeout) as response:
                text = response.read(1024 * 1024).decode("utf-8", errors="replace")
            return True, json.loads(text), ""
        except HTTPError as exc:
            text = exc.read(300).decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {text}"
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
    return False, None, last_error


def _proxy_url_reachable(url: str, proxy_port: int, timeout: float = 10.0) -> tuple[bool, str]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": TIKTOK_BROWSER_ACCEPT_LANGUAGE,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
        },
    )
    last_error = ""
    for attempt in range(PROXY_REACHABILITY_ATTEMPTS):
        try:
            with opener.open(request, timeout=timeout) as response:
                response.read(1)
                return True, f"HTTP {response.status}"
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                return True, f"HTTP {exc.code}"
            last_error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        if attempt + 1 < PROXY_REACHABILITY_ATTEMPTS:
            time.sleep(min(2.0, 0.5 * (attempt + 1)))
    return False, last_error


def detect_exit_ip_for_pool(pool: sqlite3.Row) -> dict[str, Any]:
    node_name = str(pool["mihomo_name"] or pool["name"] or "").strip()
    if not node_name:
        raise ValueError("代理没有 mihomo 节点名")
    local_port = int(pool["local_port"] or 0)
    if local_port and _port_open("127.0.0.1", local_port, timeout=1.0):
        proxy_port = local_port
        switch = {"node": node_name, "groups": [], "loaded": True, "listener_port": local_port}
    else:
        switch = _switch_mihomo_node(node_name)
        proxy_port = int(os.getenv("MIHOMO_PROXY_PORT", "7890") or "7890")
    failures: list[str] = []
    detected: dict[str, Any] | None = None
    for target in _proxy_ip_check_urls():
        ok, body, error = _proxy_get_json(target, proxy_port)
        if not ok or not isinstance(body, dict):
            failures.append(f"{urlparse(target).netloc or target}: {error or '返回格式异常'}")
            continue
        ip = _browser_ip_from_response(json.dumps(body, ensure_ascii=False))
        if not ip:
            failures.append(f"{urlparse(target).netloc or target}: 没有返回合法出口 IP")
            continue
        country = str(body.get("country") or "")
        region = str(body.get("regionName") or body.get("region") or "")
        city = str(body.get("city") or "")
        address = " / ".join(item for item in (country, region, city) if item)
        detected = {
            "ip": ip,
            "geo": {"country": country, "region": region, "city": city, "address": address},
            "mihomo": switch,
            "raw": body,
            "check_url": target,
            "fallback_failures": failures,
        }
        break
    if detected is None:
        raise ValueError(f"通过服务器 mihomo 查询出口 IP 失败：{'；'.join(failures) or '没有可用的 IP 查询接口'}")
    tiktok_url = os.getenv("PROXY_TIKTOK_CHECK_URL", "https://www.tiktok.com/").strip()
    if tiktok_url:
        reachable, reachability = _proxy_url_reachable(tiktok_url, proxy_port)
        if not reachable:
            raise ValueError(
                f"TikTok 连通性校验失败（出口 IP {detected['ip']}）："
                f"{urlparse(tiktok_url).netloc or tiktok_url}: {reachability}"
            )
        detected["tiktok_check"] = {"url": tiktok_url, "result": reachability}
    return detected


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
        "SELECT status, auto_check_failures, next_auto_check_at FROM proxy_profiles WHERE id = ?",
        (pool_id,),
    ).fetchone()
    if expected_token and (
        not row
        or _clean_status(row["status"]) != STATUS_ERROR
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
        row = conn.execute("SELECT status FROM proxy_profiles WHERE id = ?", (pool_id,)).fetchone()
        if not row or _clean_status(row["status"]) == STATUS_PAUSED:
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
                detected = detect_exit_ip_for_pool(pool)
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
        expected_ip = str(pool["expected_exit_ip"] or "").strip()
        should_bind = str(payload.get("bind") or "").lower() in {"1", "true", "yes", "on"}
        if not expected_ip and should_bind:
            expected_ip = observed_ip
        pool_status = _clean_status(pool["status"])
        ip_matches = bool(expected_ip and observed_ip == expected_ip)
        next_pool_status = STATUS_ACTIVE if ip_matches and pool_status != STATUS_PAUSED else pool_status
        allowed = bool(expected_ip and observed_ip == expected_ip and next_pool_status == STATUS_ACTIVE)
        reason = ""
        if not expected_ip:
            reason = "代理还没有绑定出口 IP"
        elif observed_ip != expected_ip:
            reason = f"当前出口 IP {observed_ip} 与绑定 IP {expected_ip} 不一致"
        elif next_pool_status != STATUS_ACTIVE:
            reason = f"代理状态为 {next_pool_status}"
        else:
            reason = "通过"
        if should_bind and not allowed and next_pool_status != STATUS_PAUSED:
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
            "SELECT id, parse_error FROM proxy_profiles WHERE status = ? AND next_auto_check_at = ''",
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


def start_login_session(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    proxy_profile_id = int(payload.get("proxy_profile_id") or payload.get("pool_id") or 0)
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
        pool = conn.execute("SELECT * FROM proxy_profiles WHERE id = ?", (proxy_profile_id,)).fetchone()
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
        slot = _allocate_session_slot(conn) if account_id else _allocate_manual_slot(conn)
        pending_name = f"pending-{proxy_profile_id}-{slot}-{int(time.time())}" if not username else username
        profile = _deep_merge(_isolation_profile(pending_name, proxy_profile_id, pool), saved_profile) if account_id else _isolation_profile(pending_name, proxy_profile_id, pool)
        profile_key = str((profile.get("isolation") or {}).get("browser_profile_key") or "")
        slot_ports = _slot_ports(slot)
        channel_url = _public_novnc_url(int(slot_ports["novnc_port"]))
        cur = conn.execute(
            """
            INSERT INTO browser_sessions (
                slot, proxy_profile_id, account_id, username, status, channel_url, runtime_id,
                pid, xvfb_pid, x11vnc_pid, websockify_pid, display, vnc_port, novnc_port,
                debug_port, owner, current_job_id, feishu_user_id, feishu_user_name,
                feishu_avatar_url, profile_key, user_data_dir, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
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
            start_url = "https://www.tiktok.com/?lang=en" if account_id else "https://www.tiktok.com/login?lang=en"
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
            if browser_observed_ip != expected_exit_ip:
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


def start_automation_session(account_id: int, job_id: str) -> dict[str, Any]:
    return start_login_session({"account_id": account_id, "_automation": True, "_current_job_id": job_id})


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
            "UPDATE browser_sessions SET current_job_id = ?, updated_at = ? WHERE id = ?",
            (_clean_text(job_id, 80), now_iso(), session_id),
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
            "UPDATE browser_sessions SET current_job_id = '', updated_at = ? WHERE id = ?",
            (now_iso(), session_id),
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
            "UPDATE browser_sessions SET owner = 'manual_review', current_job_id = '', last_error = ?, updated_at = ? WHERE id = ?",
            (_clean_text(reason, 1000), now, session_id),
        )
        conn.commit()
        return _row_to_session(_session_by_id(conn, session_id))


def inspect_login_session(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = int(payload.get("session_id") or payload.get("id") or 0)
    if not session_id:
        raise ValueError("session_id is required")
    with connect() as conn:
        row = _session_by_id(conn, session_id)
        if row["status"] not in {"starting", "running", "observing"}:
            return {"active": False, "bound": False, "status": row["status"], "reason": str(row["last_error"] or "登录通道已结束")}
        if row["account_id"]:
            account = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (row["account_id"],)).fetchone()
            return {"active": True, "bound": True, "status": "bound", "account": _row_to_account(account) if account else None, **list_state()}
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
    avatar_body = b""
    try:
        avatar_body = _browser_account_avatar(int(row["debug_port"] or 0))
    except Exception:
        pass
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
    if avatar_body:
        try:
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


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or any(ch in text for ch in ":#{}[],-&*?!|>'\"%@`") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    space = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{space}{key}:")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{space}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{space}-")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{space}- {_yaml_scalar(item)}")
        return lines
    return [f"{space}{_yaml_scalar(value)}"]


def mihomo_export() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM proxy_profiles WHERE status IN (?, 'active', '可用', '已绑定', '未绑定') ORDER BY id", (STATUS_ACTIVE,)).fetchall()
    proxies = []
    skipped = []
    for row in rows:
        proxy = _json_loads(row["mihomo_proxy_json"], {})
        if proxy:
            proxies.append(proxy)
        else:
            skipped.append({"id": row["id"], "name": row["name"], "reason": row["parse_error"] or "no parsed mihomo proxy"})
    yaml = "proxies:\n"
    for proxy in proxies:
        lines = _yaml_lines(proxy, 4)
        if lines:
            first = lines[0].lstrip()
            yaml += f"  - {first}\n"
            yaml += "\n".join(lines[1:]) + ("\n" if len(lines) > 1 else "")
    listeners = [
        {"name": f"tiktok-{row['name']}", "type": "mixed", "port": int(row["local_port"] or 0), "proxy": row["mihomo_name"] or row["name"]}
        for row in rows
        if int(row["local_port"] or 0) and not _sing_box_reality_pool(row)
    ]
    if listeners:
        yaml += "listeners:\n"
        for listener in listeners:
            lines = _yaml_lines(listener, 4)
            first = lines[0].lstrip()
            yaml += f"  - {first}\n"
            yaml += "\n".join(lines[1:]) + ("\n" if len(lines) > 1 else "")
    return {"proxies": proxies, "listeners": listeners, "skipped": skipped, "yaml": yaml, "port_range": f"{PROXY_PORT_START}-{PROXY_PORT_END}", "generated_at": now_iso()}


def lookup_ip_geo(ip: str) -> dict[str, str]:
    ok, body, _error = _http_get_json(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,query", timeout=4)
    if not ok or not isinstance(body, dict) or body.get("status") != "success":
        return {"country": "", "region": "", "city": "", "address": ""}
    country = str(body.get("country") or "")
    region = str(body.get("regionName") or "")
    city = str(body.get("city") or "")
    address = " / ".join(item for item in (country, region, city) if item)
    return {"country": country, "region": region, "city": city, "address": address}


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _http_get_json(url: str, timeout: float = 3.0) -> tuple[bool, Any, str]:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=timeout)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    headers = {}
    secret = os.getenv("MIHOMO_SECRET", "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read(8192).decode("utf-8", errors="replace")
        if not (200 <= response.status < 300):
            return False, None, f"HTTP {response.status}: {body[:200]}"
        return True, json.loads(body), ""
    except Exception as exc:
        return False, None, str(exc)
    finally:
        conn.close()


def runtime_status() -> dict[str, Any]:
    mihomo_api = DEFAULT_MIHOMO_API.rstrip("/")
    mihomo_ok, mihomo_body, mihomo_error = _http_get_json(mihomo_api + "/version")
    novnc_ports = novnc_port_plan()
    novnc_checks = {str(port): _port_open("127.0.0.1", port) for port in novnc_ports["allowed_ports"]}
    return {
        "checked_at": now_iso(),
        "novnc_url": DEFAULT_NOVNC_PUBLIC_URL,
        "novnc_local_port": NOVNC_PORT,
        "novnc_ports": novnc_ports,
        "vnc_port": int(os.getenv("VNC_PORT", "5900")),
        "mihomo_proxy_port": int(os.getenv("MIHOMO_PROXY_PORT", "7890")),
        "mihomo_api_url": mihomo_api,
        "checks": {
            "novnc_local": novnc_checks.get(str(NOVNC_PORT), False),
            "novnc_ports": novnc_checks,
            "vnc_local": _port_open("127.0.0.1", int(os.getenv("VNC_PORT", "5900"))),
            "mihomo_proxy_local": _port_open("127.0.0.1", int(os.getenv("MIHOMO_PROXY_PORT", "7890"))),
            "mihomo_api_local": mihomo_ok,
        },
        "mihomo_version": (mihomo_body or {}).get("version") if isinstance(mihomo_body, dict) else "",
        "mihomo_error": mihomo_error,
        "port_range": f"{PROXY_PORT_START}-{PROXY_PORT_END}",
        "pending_login_ttl_seconds": pending_login_ttl_seconds(),
        "browser_locale": TIKTOK_BROWSER_LOCALE,
        "browser_notice": f"noVNC 放行端口按账号并发 {novnc_ports['max_slots']} + 手动 {novnc_ports['manual_ports']} 计算：{novnc_ports['allowed_range']}；服务器本机检测为准。",
    }
