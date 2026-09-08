"""SQLite schema and existing data migrations for the proxy subsystem."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable


def init_db(
    conn: sqlite3.Connection,
    *,
    clean_port_scope: Callable[[Any], str],
    port_range: Callable[[str], tuple[int, int]],
    now_iso: Callable[[], str],
    system_proxy_dialer: str,
    status_duplicate: str,
    status_error: str,
) -> None:
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
            port_scope TEXT NOT NULL DEFAULT 'default',
            detected_exit_ip TEXT NOT NULL DEFAULT '',
            detected_country TEXT NOT NULL DEFAULT '',
            detected_region TEXT NOT NULL DEFAULT '',
            detected_city TEXT NOT NULL DEFAULT '',
            detected_address TEXT NOT NULL DEFAULT '',
            detected_at TEXT NOT NULL DEFAULT '',
            auto_check_failures INTEGER NOT NULL DEFAULT 0,
            next_auto_check_at TEXT NOT NULL DEFAULT '',
            last_auto_check_at TEXT NOT NULL DEFAULT '',
            deleted_at TEXT NOT NULL DEFAULT ''
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
            login_platform TEXT NOT NULL DEFAULT '',
            last_activity_at TEXT NOT NULL DEFAULT '',
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
            platform TEXT NOT NULL DEFAULT 'tiktok',
            trigger_type TEXT NOT NULL DEFAULT 'manual',
            schedule_date TEXT NOT NULL DEFAULT '',
            max_videos INTEGER NOT NULL DEFAULT 0,
            publish_date_start TEXT NOT NULL DEFAULT '',
            publish_date_end TEXT NOT NULL DEFAULT '',
            feishu_target_json TEXT NOT NULL DEFAULT '{}',
            auto_sync INTEGER NOT NULL DEFAULT 1,
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
        "port_scope": "TEXT NOT NULL DEFAULT 'default'",
        "detected_exit_ip": "TEXT NOT NULL DEFAULT ''",
        "detected_country": "TEXT NOT NULL DEFAULT ''",
        "detected_region": "TEXT NOT NULL DEFAULT ''",
        "detected_city": "TEXT NOT NULL DEFAULT ''",
        "detected_address": "TEXT NOT NULL DEFAULT ''",
        "detected_at": "TEXT NOT NULL DEFAULT ''",
        "auto_check_failures": "INTEGER NOT NULL DEFAULT 0",
        "next_auto_check_at": "TEXT NOT NULL DEFAULT ''",
        "last_auto_check_at": "TEXT NOT NULL DEFAULT ''",
        "deleted_at": "TEXT NOT NULL DEFAULT ''",
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
    conn.execute("DROP INDEX IF EXISTS idx_tiktok_accounts_feishu_user")
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
        "login_platform": "TEXT NOT NULL DEFAULT ''",
        "last_activity_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in existing_session_cols:
            conn.execute(f"ALTER TABLE browser_sessions ADD COLUMN {name} {definition}")
    conn.execute(
        """UPDATE browser_sessions
           SET last_activity_at = COALESCE(NULLIF(last_activity_at, ''), updated_at, created_at)
           WHERE last_activity_at = ''"""
    )
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
            "platform": "TEXT NOT NULL DEFAULT 'tiktok'",
            "feishu_target_json": "TEXT NOT NULL DEFAULT '{}'",
            "publish_date_start": "TEXT NOT NULL DEFAULT ''",
            "publish_date_end": "TEXT NOT NULL DEFAULT ''",
            "status_detail": "TEXT NOT NULL DEFAULT ''",
            "auto_sync": "INTEGER NOT NULL DEFAULT 1",
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
    for row in conn.execute(
        "SELECT id, source_type, dialer_proxy, mihomo_proxy_json FROM proxy_profiles WHERE deleted_at = ''"
    ):
        try:
            mihomo_proxy = json.loads(row["mihomo_proxy_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            mihomo_proxy = {}
        if not isinstance(mihomo_proxy, dict):
            mihomo_proxy = {}
        dialer_proxy = system_proxy_dialer if row["source_type"] == "static" else ""
        if dialer_proxy:
            mihomo_proxy["dialer-proxy"] = dialer_proxy
        else:
            mihomo_proxy.pop("dialer-proxy", None)
        serialized = json.dumps(mihomo_proxy, ensure_ascii=False, separators=(",", ":"))
        if row["dialer_proxy"] != dialer_proxy or row["mihomo_proxy_json"] != serialized:
            conn.execute(
                """UPDATE proxy_profiles
                   SET dialer_proxy = ?, mihomo_proxy_json = ?
                   WHERE id = ?""",
                (dialer_proxy, serialized, row["id"]),
            )
    used_ports: set[int] = set()
    for row in conn.execute("SELECT id, local_port, port_scope FROM proxy_profiles WHERE deleted_at = '' ORDER BY id"):
        port_scope = clean_port_scope(row["port_scope"])
        start_port, end_port = port_range(port_scope)
        local_port = int(row["local_port"] or 0)
        if start_port <= local_port <= end_port and local_port not in used_ports:
            if str(row["port_scope"] or "") != port_scope:
                conn.execute("UPDATE proxy_profiles SET port_scope = ?, updated_at = ? WHERE id = ?", (port_scope, now_iso(), row["id"]))
            used_ports.add(local_port)
            continue
        replacement = next(
            (port for port in range(start_port, end_port + 1) if port not in used_ports),
            0,
        )
        if not replacement:
            raise ValueError(f"{port_scope} proxy port range exhausted: {start_port}-{end_port}")
        conn.execute(
            "UPDATE proxy_profiles SET local_port = ?, port_scope = ?, updated_at = ? WHERE id = ?",
            (replacement, port_scope, now_iso(), row["id"]),
        )
        used_ports.add(replacement)
    for row in conn.execute(
        """SELECT a.id, a.profile_json, p.local_port
           FROM tiktok_accounts a
           JOIN proxy_profiles p ON p.id = a.proxy_profile_id
           WHERE a.deleted_at = '' AND a.proxy_bound = 1 AND p.deleted_at = ''"""
    ):
        try:
            profile = json.loads(row["profile_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        local_port = int(row["local_port"] or 0)
        proxy_binding = profile.get("proxy_binding")
        if not isinstance(proxy_binding, dict):
            proxy_binding = {}
        proxy_binding["local_port"] = local_port
        profile["proxy_binding"] = proxy_binding
        browser_settings = profile.get("browser_settings")
        if not isinstance(browser_settings, dict):
            browser_settings = {}
        browser_settings["proxy_server"] = f"127.0.0.1:{local_port}"
        profile["browser_settings"] = browser_settings
        serialized = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
        if serialized != row["profile_json"]:
            conn.execute(
                "UPDATE tiktok_accounts SET profile_json = ?, updated_at = ? WHERE id = ?",
                (serialized, now_iso(), row["id"]),
            )
    conn.execute(
        """UPDATE proxy_profiles
           SET status = ?,
               parse_error = REPLACE(
                   parse_error,
                   '，重复 IP 已强制标记为不可用',
                   '，状态已标记为 IP重复'
               ),
               auto_check_failures = 0,
               next_auto_check_at = '',
               updated_at = ?
           WHERE deleted_at = '' AND status IN (?, ?)
             AND parse_error LIKE '出口 IP %已被代理%重复 IP%'""",
        (status_duplicate, now_iso(), status_error, status_duplicate),
    )
    conn.commit()
