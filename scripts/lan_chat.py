"""Persistent account-based chat for trusted local-area networks."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO
from urllib.request import Request, urlopen


PUBLIC_ROOM_ID = "public"
DEFAULT_FEISHU_USER_ID = "local-default"
DEFAULT_FEISHU_USER_NAME = "本地用户（待接入飞书）"
ONLINE_WINDOW_SECONDS = 90
AVATAR_COLORS = (
    "#E76F51",
    "#2A9D8F",
    "#E9C46A",
    "#4C78A8",
    "#8E6C9E",
    "#587B5B",
    "#D9825B",
    "#5A7D9A",
)
MESSAGE_MEDIA_MAX_BYTES = 100 * 1024 * 1024
MESSAGE_MEDIA_RETENTION_SECONDS = 7 * 24 * 60 * 60
MESSAGE_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
}
FILE_TRANSFER_MAX_BYTES = 10 * 1024 * 1024 * 1024
FILE_TRANSFER_RETENTION_SECONDS = 7 * 24 * 60 * 60
FILE_TRANSFER_CLEANUP_INTERVAL_SECONDS = 60 * 60
FILE_COPY_CHUNK_BYTES = 1024 * 1024
FEISHU_AVATAR_MAX_BYTES = 5 * 1024 * 1024
FEISHU_AVATAR_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


class LanChatError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class LanChatStore:
    def __init__(
        self,
        db_path: Path,
        avatar_dir: Path | None = None,
        media_dir: Path | None = None,
        file_dir: Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.avatar_dir = Path(avatar_dir or self.db_path.parent / "lan_chat_avatars")
        self.media_dir = Path(media_dir or self.db_path.parent / "lan_chat_media")
        self.file_dir = Path(file_dir or self.db_path.parent / "lan_chat_files")
        self._avatar_lock = threading.Lock()
        self._feishu_avatar_lock = threading.Lock()
        self._avatar_jobs: set[str] = set()
        self._media_poster_lock = threading.Lock()
        self._file_janitor_lock = threading.Lock()
        self._file_janitor_started = False
        self._message_event_condition = threading.Condition()
        self._message_event_sequence = 0

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.file_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS feishu_users (
                    id TEXT PRIMARY KEY,
                    open_id TEXT UNIQUE,
                    name TEXT NOT NULL,
                    avatar_url TEXT,
                    source TEXT NOT NULL DEFAULT 'local',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    device_token_hash TEXT NOT NULL UNIQUE,
                    feishu_user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    avatar_color TEXT NOT NULL,
                    avatar_status TEXT NOT NULL DEFAULT 'fallback',
                    avatar_filename TEXT,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    FOREIGN KEY (feishu_user_id) REFERENCES feishu_users(id)
                );
                CREATE TABLE IF NOT EXISTS account_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('public', 'direct', 'group')),
                    name TEXT NOT NULL DEFAULT '',
                    created_by TEXT,
                    system_kind TEXT NOT NULL DEFAULT 'custom',
                    feishu_user_id TEXT,
                    admin_user_id TEXT,
                    direct_key TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (created_by) REFERENCES users(id),
                    FOREIGN KEY (feishu_user_id) REFERENCES feishu_users(id),
                    FOREIGN KEY (admin_user_id) REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS room_members (
                    room_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    joined_at REAL NOT NULL,
                    PRIMARY KEY (room_id, user_id),
                    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    image_filename TEXT,
                    image_mime_type TEXT,
                    media_expires_at REAL,
                    media_deleted_at REAL,
                    file_id TEXT,
                    client_upload_id TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                    FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS file_attachments (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    stored_filename TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    deleted_at REAL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                    FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS file_receipts (
                    file_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted')),
                    decided_at REAL,
                    PRIMARY KEY (file_id, user_id),
                    FOREIGN KEY (file_id) REFERENCES file_attachments(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS room_reads (
                    room_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    last_read_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (room_id, user_id),
                    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS messages_room_id_idx
                    ON messages(room_id, id);
                CREATE INDEX IF NOT EXISTS room_members_user_id_idx
                    ON room_members(user_id, room_id);
                CREATE INDEX IF NOT EXISTS account_sessions_user_id_idx
                    ON account_sessions(user_id);
                CREATE INDEX IF NOT EXISTS room_reads_user_id_idx
                    ON room_reads(user_id, room_id);
                CREATE INDEX IF NOT EXISTS file_attachments_expiry_idx
                    ON file_attachments(deleted_at, expires_at);
                CREATE INDEX IF NOT EXISTS file_receipts_user_id_idx
                    ON file_receipts(user_id, status);
                """
            )
            now = time.time()
            feishu_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(feishu_users)").fetchall()
            }
            if "active" not in feishu_columns:
                conn.execute(
                    "ALTER TABLE feishu_users ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            conn.execute(
                """INSERT OR IGNORE INTO feishu_users
                   (id, open_id, name, avatar_url, source, created_at, updated_at)
                   VALUES (?, NULL, ?, NULL, 'local', ?, ?)""",
                (DEFAULT_FEISHU_USER_ID, DEFAULT_FEISHU_USER_NAME, now, now),
            )
            user_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "feishu_user_id" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN feishu_user_id TEXT")
            conn.execute(
                """UPDATE users SET feishu_user_id = ?
                   WHERE feishu_user_id IS NULL OR feishu_user_id = ''""",
                (DEFAULT_FEISHU_USER_ID,),
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS users_feishu_user_id_idx ON users(feishu_user_id)"
            )
            room_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(rooms)").fetchall()
            }
            if "system_kind" not in room_columns:
                conn.execute(
                    "ALTER TABLE rooms ADD COLUMN system_kind TEXT NOT NULL DEFAULT 'custom'"
                )
            if "feishu_user_id" not in room_columns:
                conn.execute("ALTER TABLE rooms ADD COLUMN feishu_user_id TEXT")
            if "admin_user_id" not in room_columns:
                conn.execute("ALTER TABLE rooms ADD COLUMN admin_user_id TEXT")
            conn.execute(
                "UPDATE rooms SET system_kind = 'public' WHERE kind = 'public'"
            )
            conn.execute(
                "UPDATE rooms SET system_kind = 'direct' WHERE kind = 'direct'"
            )
            conn.execute(
                """UPDATE rooms SET system_kind = 'custom',
                                      admin_user_id = COALESCE(NULLIF(admin_user_id, ''), created_by)
                   WHERE kind = 'group'
                     AND (system_kind IS NULL OR system_kind = '' OR system_kind = 'custom')"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS rooms_feishu_default_idx
                   ON rooms(feishu_user_id) WHERE system_kind = 'feishu'"""
            )
            message_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "image_filename" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN image_filename TEXT")
            if "image_mime_type" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN image_mime_type TEXT")
            if "media_expires_at" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN media_expires_at REAL")
            if "media_deleted_at" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN media_deleted_at REAL")
            if "file_id" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN file_id TEXT")
            if "client_upload_id" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN client_upload_id TEXT")
            conn.execute(
                """UPDATE messages SET media_expires_at = created_at + ?
                   WHERE image_filename IS NOT NULL AND media_expires_at IS NULL""",
                (MESSAGE_MEDIA_RETENTION_SECONDS,),
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS messages_file_id_idx ON messages(file_id)"
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS messages_sender_client_upload_idx
                   ON messages(sender_id, client_upload_id)
                   WHERE client_upload_id IS NOT NULL AND client_upload_id != ''"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS messages_media_expiry_idx
                   ON messages(media_deleted_at, media_expires_at)"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO rooms
                   (id, kind, name, created_by, system_kind, feishu_user_id,
                    admin_user_id, direct_key, created_at, updated_at)
                   VALUES (?, 'public', ?, NULL, 'public', NULL, NULL, NULL, ?, ?)""",
                (PUBLIC_ROOM_ID, "公共频道", now, now),
            )
            conn.execute(
                """UPDATE rooms SET system_kind = 'public', feishu_user_id = NULL,
                                      admin_user_id = NULL
                   WHERE id = ?""",
                (PUBLIC_ROOM_ID,),
            )
            owners = conn.execute("SELECT id, name FROM feishu_users").fetchall()
            for owner in owners:
                self._ensure_feishu_default_group(
                    conn, str(owner["id"]), str(owner["name"]), now
                )
        self.cleanup_expired_files()
        self.cleanup_expired_media()
        self._start_file_janitor()

    @staticmethod
    def _ensure_feishu_default_group(
        conn: sqlite3.Connection, owner_id: str, owner_name: str, now: float
    ) -> str:
        room_id = "feishu_" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:20]
        room_name = f"{owner_name}的群组"
        conn.execute(
            """INSERT OR IGNORE INTO rooms
               (id, kind, name, created_by, system_kind, feishu_user_id,
                admin_user_id, direct_key, created_at, updated_at)
               VALUES (?, 'group', ?, NULL, 'feishu', ?, NULL, NULL, ?, ?)""",
            (room_id, room_name, owner_id, now, now),
        )
        conn.execute(
            """UPDATE rooms SET name = ?, system_kind = 'feishu',
                                feishu_user_id = ?, admin_user_id = NULL
               WHERE id = ?""",
            (room_name, owner_id, room_id),
        )
        conn.execute(
            """INSERT OR IGNORE INTO room_members(room_id, user_id, joined_at)
               SELECT ?, id, created_at FROM users WHERE feishu_user_id = ?""",
            (room_id, owner_id),
        )
        return room_id

    def register(self, device_token: str, nickname: str = "") -> tuple[dict[str, Any], bool]:
        token_hash = self._token_hash(device_token)
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM users WHERE device_token_hash = ?", (token_hash,)
            ).fetchone()
            created = row is None
            if row is None:
                user_id = uuid.uuid4().hex[:16]
                clean_name = self._nickname(nickname, default=f"访客-{token_hash[:4].upper()}")
                self._require_nickname_available(conn, clean_name)
                avatar_status = "pending" if self._avatar_configured() else "fallback"
                conn.execute(
                    """INSERT INTO users
                       (id, device_token_hash, feishu_user_id, nickname, avatar_color, avatar_status,
                        avatar_filename, created_at, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (
                        user_id,
                        token_hash,
                        DEFAULT_FEISHU_USER_ID,
                        clean_name,
                        AVATAR_COLORS[int(token_hash[:8], 16) % len(AVATAR_COLORS)],
                        avatar_status,
                        now,
                        now,
                    ),
                )
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            else:
                conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, row["id"]))
                row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
            self._ensure_feishu_default_group(
                conn, DEFAULT_FEISHU_USER_ID, DEFAULT_FEISHU_USER_NAME, now
            )
        user = self._public_user(row)
        if user["avatarStatus"] == "pending":
            self._start_avatar_generation(user["id"], user["nickname"])
        return user, created

    def login_options(self) -> dict[str, Any]:
        with self._connect() as conn:
            owners = conn.execute(
                """SELECT * FROM feishu_users WHERE active = 1
                   ORDER BY created_at ASC, name COLLATE NOCASE"""
            ).fetchall()
            result = []
            for owner in owners:
                accounts = conn.execute(
                    """SELECT * FROM users WHERE feishu_user_id = ?
                       ORDER BY last_seen DESC, created_at ASC""",
                    (owner["id"],),
                ).fetchall()
                result.append(
                    {
                        "id": owner["id"],
                        "feishuId": owner["open_id"] or owner["id"],
                        "name": owner["name"],
                        "avatarUrl": f"/api/lan-chat/feishu-avatars/{owner['id']}",
                        "source": owner["source"],
                        "accounts": [self._public_user(row) for row in accounts],
                    }
                )
        return {"feishuUsers": result}

    def sync_feishu_users(self, external_users: list[dict[str, Any]]) -> int:
        now = time.time()
        synced = 0
        with self._connect() as conn:
            conn.execute("UPDATE feishu_users SET active = 0")
            for item in external_users:
                if not isinstance(item, dict):
                    continue
                open_id = str(
                    item.get("openId") or item.get("userId") or item.get("unionId") or ""
                ).strip()
                if not open_id:
                    continue
                existing = conn.execute(
                    "SELECT id FROM feishu_users WHERE open_id = ?", (open_id,)
                ).fetchone()
                owner_id = (
                    str(existing["id"])
                    if existing is not None
                    else f"feishu-{hashlib.sha256(open_id.encode('utf-8')).hexdigest()[:20]}"
                )
                fallback_id = str(item.get("userId") or open_id).strip()
                name = str(item.get("name") or item.get("enName") or "").strip()
                if not name:
                    name = f"飞书用户-{fallback_id[:8]}"
                avatar_url = str(item.get("avatarUrl") or "").strip() or None
                conn.execute(
                    """INSERT INTO feishu_users
                       (id, open_id, name, avatar_url, source, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'feishu', 1, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           open_id = excluded.open_id,
                           name = excluded.name,
                           avatar_url = excluded.avatar_url,
                           source = 'feishu',
                           active = 1,
                           updated_at = excluded.updated_at""",
                    (owner_id, open_id, name, avatar_url, now, now),
                )
                self._ensure_feishu_default_group(conn, owner_id, name, now)
                synced += 1
        return synced

    def select_account(self, feishu_user_id: str, account_id: str) -> dict[str, Any]:
        owner_id = str(feishu_user_id or "").strip()
        user_id = str(account_id or "").strip()
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ? AND feishu_user_id = ?",
                (user_id, owner_id),
            ).fetchone()
            if row is None:
                raise LanChatError("设备账户不存在或不属于该飞书用户", 404)
            owner = conn.execute(
                "SELECT name FROM feishu_users WHERE id = ?", (owner_id,)
            ).fetchone()
            if owner is not None:
                self._ensure_feishu_default_group(
                    conn, owner_id, str(owner["name"]), now
                )
            session_token = self._create_session(conn, user_id, now)
            conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, user_id))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return {"sessionToken": session_token, "user": self._public_user(row)}

    def create_account(self, feishu_user_id: str, nickname: str) -> dict[str, Any]:
        owner_id = str(feishu_user_id or "").strip()
        clean_name = self._nickname(nickname)
        now = time.time()
        user_id = uuid.uuid4().hex[:16]
        legacy_token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        avatar_status = "pending" if self._avatar_configured() else "fallback"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT id, name FROM feishu_users WHERE id = ? AND active = 1", (owner_id,)
            ).fetchone()
            if owner is None:
                raise LanChatError("飞书用户不存在", 404)
            self._require_nickname_available(conn, clean_name)
            conn.execute(
                """INSERT INTO users
                   (id, device_token_hash, feishu_user_id, nickname, avatar_color, avatar_status,
                    avatar_filename, created_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    user_id,
                    legacy_token_hash,
                    owner_id,
                    clean_name,
                    AVATAR_COLORS[int(legacy_token_hash[:8], 16) % len(AVATAR_COLORS)],
                    avatar_status,
                    now,
                    now,
                ),
            )
            self._ensure_feishu_default_group(conn, owner_id, str(owner["name"]), now)
            session_token = self._create_session(conn, user_id, now)
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        user = self._public_user(row)
        if user["avatarStatus"] == "pending":
            self._start_avatar_generation(user["id"], user["nickname"])
        return {"sessionToken": session_token, "user": user}

    def authenticate(self, device_token: str) -> dict[str, Any]:
        token_hash = self._token_hash(device_token)
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT u.* FROM account_sessions s
                   JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ?""",
                (token_hash,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM users WHERE device_token_hash = ?", (token_hash,)
                ).fetchone()
            else:
                conn.execute(
                    "UPDATE account_sessions SET last_seen = ? WHERE token_hash = ?",
                    (now, token_hash),
                )
            if row is None:
                raise LanChatError("登录状态已失效，请重新选择账户", 401)
            conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, row["id"]))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        return self._public_user(row)

    def bootstrap(self, device_token: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        return {
            "currentUser": current,
            "users": self.list_users(current["id"]),
            "rooms": self.list_rooms(current["id"]),
            "publicRoomId": PUBLIC_ROOM_ID,
            "pollIntervalMs": 3000,
            "messagePollIntervalMs": 3000,
            "bootstrapPollIntervalMs": 10000,
            "inlineMediaMaxBytes": MESSAGE_MEDIA_MAX_BYTES,
            "inlineMediaRetentionSeconds": MESSAGE_MEDIA_RETENTION_SECONDS,
            "fileMaxBytes": FILE_TRANSFER_MAX_BYTES,
            "fileRetentionSeconds": FILE_TRANSFER_RETENTION_SECONDS,
        }

    def update_profile(self, device_token: str, nickname: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        clean_name = self._nickname(nickname)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_nickname_available(conn, clean_name, current["id"])
            conn.execute("UPDATE users SET nickname = ? WHERE id = ?", (clean_name, current["id"]))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (current["id"],)).fetchone()
        return self._public_user(row)

    def list_users(self, current_user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY last_seen DESC, created_at ASC"
            ).fetchall()
        return [
            {**self._public_user(row), "isCurrent": row["id"] == current_user_id}
            for row in rows
        ]

    def list_rooms(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT r.*
                   FROM rooms r
                   LEFT JOIN room_members rm ON rm.room_id = r.id
                   WHERE r.kind = 'public' OR rm.user_id = ?
                   ORDER BY CASE r.system_kind
                                WHEN 'public' THEN 0
                                WHEN 'feishu' THEN 1
                                ELSE 2
                            END,
                            r.updated_at DESC""",
                (user_id,),
            ).fetchall()
            return [self._room_payload(conn, row, user_id) for row in rows]

    def open_direct(self, device_token: str, target_user_id: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        target_user_id = str(target_user_id or "").strip()
        if not target_user_id or target_user_id == current["id"]:
            raise LanChatError("请选择其他成员")
        member_ids = sorted((current["id"], target_user_id))
        direct_key = ":".join(member_ids)
        now = time.time()
        with self._connect() as conn:
            target = conn.execute("SELECT id FROM users WHERE id = ?", (target_user_id,)).fetchone()
            if target is None:
                raise LanChatError("成员不存在", 404)
            row = conn.execute("SELECT * FROM rooms WHERE direct_key = ?", (direct_key,)).fetchone()
            if row is None:
                room_id = "dm_" + hashlib.sha256(direct_key.encode()).hexdigest()[:20]
                conn.execute(
                    """INSERT INTO rooms
                       (id, kind, name, created_by, system_kind, feishu_user_id,
                        admin_user_id, direct_key, created_at, updated_at)
                       VALUES (?, 'direct', '', ?, 'direct', NULL, NULL, ?, ?, ?)""",
                    (room_id, current["id"], direct_key, now, now),
                )
                conn.executemany(
                    "INSERT INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
                    [(room_id, member_id, now) for member_id in member_ids],
                )
                row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            return self._room_payload(conn, row, current["id"])

    def create_group(
        self, device_token: str, name: str, member_ids: list[str] | None
    ) -> dict[str, Any]:
        current = self.authenticate(device_token)
        clean_name = " ".join(str(name or "").split())
        if not clean_name or len(clean_name) > 32:
            raise LanChatError("群组名称需要 1-32 个字符")
        requested = {str(item).strip() for item in (member_ids or []) if str(item).strip()}
        requested.discard(current["id"])
        if len(requested) > 99:
            raise LanChatError("群组成员过多")
        now = time.time()
        room_id = "group_" + uuid.uuid4().hex[:20]
        with self._connect() as conn:
            if requested:
                placeholders = ",".join("?" for _ in requested)
                valid = {
                    row["id"]
                    for row in conn.execute(
                        f"SELECT id FROM users WHERE id IN ({placeholders})", tuple(requested)
                    ).fetchall()
                }
                if valid != requested:
                    raise LanChatError("部分群组成员不存在", 404)
            members = [current["id"], *sorted(requested)]
            conn.execute(
                """INSERT INTO rooms
                   (id, kind, name, created_by, system_kind, feishu_user_id,
                    admin_user_id, direct_key, created_at, updated_at)
                   VALUES (?, 'group', ?, ?, 'custom', NULL, ?, NULL, ?, ?)""",
                (room_id, clean_name, current["id"], current["id"], now, now),
            )
            conn.executemany(
                "INSERT INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
                [(room_id, member_id, now) for member_id in members],
            )
            row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            return self._room_payload(conn, row, current["id"])

    def rename_group(self, device_token: str, room_id: str, name: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        clean_name = " ".join(str(name or "").split())
        if not clean_name or len(clean_name) > 32:
            raise LanChatError("群组名称需要 1-32 个字符")
        with self._connect() as conn:
            room = self._require_custom_group(conn, room_id, current["id"], admin=True)
            conn.execute(
                "UPDATE rooms SET name = ?, updated_at = ? WHERE id = ?",
                (clean_name, time.time(), room["id"]),
            )
            room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room["id"],)).fetchone()
            return self._room_payload(conn, room, current["id"])

    def remove_group_member(
        self, device_token: str, room_id: str, target_user_id: str
    ) -> dict[str, Any]:
        current = self.authenticate(device_token)
        target_id = str(target_user_id or "").strip()
        if not target_id:
            raise LanChatError("请选择要移出的成员")
        with self._connect() as conn:
            room = self._require_custom_group(conn, room_id, current["id"], admin=True)
            if target_id == current["id"]:
                raise LanChatError("管理员请使用退出群组，管理员身份会自动移交")
            member = conn.execute(
                "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
                (room["id"], target_id),
            ).fetchone()
            if member is None:
                raise LanChatError("该用户不是群组成员", 404)
            conn.execute(
                "DELETE FROM room_members WHERE room_id = ? AND user_id = ?",
                (room["id"], target_id),
            )
            conn.execute(
                "DELETE FROM room_reads WHERE room_id = ? AND user_id = ?",
                (room["id"], target_id),
            )
            conn.execute(
                "UPDATE rooms SET updated_at = ? WHERE id = ?", (time.time(), room["id"])
            )
            room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room["id"],)).fetchone()
            return self._room_payload(conn, room, current["id"])

    def leave_group(self, device_token: str, room_id: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        with self._connect() as conn:
            room = self._require_custom_group(conn, room_id, current["id"])
            new_admin_id = None
            if room["admin_user_id"] == current["id"]:
                successor = conn.execute(
                    """SELECT user_id FROM room_members
                       WHERE room_id = ? AND user_id != ?
                       ORDER BY joined_at ASC, user_id ASC LIMIT 1""",
                    (room["id"], current["id"]),
                ).fetchone()
                if successor is None:
                    raise LanChatError("最后一位成员请直接解散群组")
                new_admin_id = str(successor["user_id"])
                conn.execute(
                    "UPDATE rooms SET admin_user_id = ?, updated_at = ? WHERE id = ?",
                    (new_admin_id, time.time(), room["id"]),
                )
            conn.execute(
                "DELETE FROM room_members WHERE room_id = ? AND user_id = ?",
                (room["id"], current["id"]),
            )
            conn.execute(
                "DELETE FROM room_reads WHERE room_id = ? AND user_id = ?",
                (room["id"], current["id"]),
            )
        return {"roomId": room_id, "newAdminUserId": new_admin_id}

    def dissolve_group(self, device_token: str, room_id: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        with self._connect() as conn:
            room = self._require_custom_group(conn, room_id, current["id"], admin=True)
            media_names = [
                str(row["image_filename"])
                for row in conn.execute(
                    """SELECT image_filename FROM messages
                       WHERE room_id = ? AND image_filename IS NOT NULL""",
                    (room["id"],),
                ).fetchall()
            ]
            stored_names = [
                str(row["stored_filename"])
                for row in conn.execute(
                    "SELECT stored_filename FROM file_attachments WHERE room_id = ?",
                    (room["id"],),
                ).fetchall()
            ]
            conn.execute("DELETE FROM rooms WHERE id = ?", (room["id"],))
        for filename in media_names:
            try:
                (self.media_dir / filename).unlink(missing_ok=True)
                (self.media_dir / f"{Path(filename).stem}.poster.jpg").unlink(missing_ok=True)
            except OSError as exc:
                print(f"LAN chat media cleanup failed for {filename}: {exc}", flush=True)
        for filename in stored_names:
            try:
                self._stored_file_path(filename).unlink(missing_ok=True)
            except (LanChatError, OSError) as exc:
                print(f"LAN chat file cleanup failed for {filename}: {exc}", flush=True)
        return {"roomId": room_id, "dissolved": True}

    def list_messages(
        self,
        device_token: str,
        room_id: str,
        after_id: int = 0,
        limit: int = 100,
        before_id: int = 0,
    ) -> dict[str, Any]:
        current = self.authenticate(device_token)
        after_id = max(0, int(after_id or 0))
        before_id = max(0, int(before_id or 0))
        if after_id and before_id:
            raise LanChatError("before 和 after 不能同时使用")
        limit = max(1, min(int(limit or 100), 200))
        with self._connect() as conn:
            self._require_room_access(conn, room_id, current["id"])
            if after_id:
                rows = conn.execute(
                    """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                       FROM messages m JOIN users u ON u.id = m.sender_id
                       WHERE m.room_id = ? AND m.id > ?
                       ORDER BY m.id ASC LIMIT ?""",
                    (room_id, after_id, limit),
                ).fetchall()
            else:
                upper_bound = before_id if before_id else 2**63 - 1
                rows = conn.execute(
                    """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                       FROM messages m JOIN users u ON u.id = m.sender_id
                       WHERE m.room_id = ? AND m.id < ?
                       ORDER BY m.id DESC LIMIT ?""",
                    (room_id, upper_bound, limit),
                ).fetchall()
                rows.reverse()
            last_id = int(rows[-1]["id"]) if rows else after_id
            oldest_id = int(rows[0]["id"]) if rows else before_id
            has_more_before = bool(
                oldest_id
                and conn.execute(
                    "SELECT 1 FROM messages WHERE room_id = ? AND id < ? LIMIT 1",
                    (room_id, oldest_id),
                ).fetchone()
            )
            if last_id > 0:
                conn.execute(
                    """INSERT INTO room_reads
                       (room_id, user_id, last_read_message_id, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(room_id, user_id) DO UPDATE SET
                           last_read_message_id = MAX(
                               room_reads.last_read_message_id,
                               excluded.last_read_message_id
                           ),
                           updated_at = excluded.updated_at""",
                    (room_id, current["id"], last_id, time.time()),
                )
            messages = [self._message_payload(conn, row, current["id"]) for row in rows]
        return {
            "messages": messages,
            "lastId": messages[-1]["id"] if messages else after_id,
            "oldestId": messages[0]["id"] if messages else before_id,
            "hasMoreBefore": has_more_before,
        }

    def wait_for_message_events(
        self, device_token: str, after_id: int, timeout_seconds: float = 20.0
    ) -> list[dict[str, int | str]]:
        """Wait for authorized message IDs; database catch-up makes reconnects lossless."""
        current = self.authenticate(device_token)
        after_id = max(0, int(after_id or 0))
        with self._message_event_condition:
            observed_sequence = self._message_event_sequence
        events = self._message_events_for_user(current["id"], after_id)
        if events:
            return events
        with self._message_event_condition:
            if self._message_event_sequence == observed_sequence:
                self._message_event_condition.wait(max(1.0, min(float(timeout_seconds), 20.0)))
        return self._message_events_for_user(current["id"], after_id)

    def _message_events_for_user(self, user_id: str, after_id: int) -> list[dict[str, int | str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.id, m.room_id
                   FROM messages m
                   JOIN rooms r ON r.id = m.room_id
                   LEFT JOIN room_members rm
                     ON rm.room_id = m.room_id AND rm.user_id = ?
                   WHERE m.id > ? AND (r.kind = 'public' OR rm.user_id IS NOT NULL)
                   ORDER BY m.id ASC LIMIT 100""",
                (user_id, after_id),
            ).fetchall()
        return [{"id": int(row["id"]), "roomId": str(row["room_id"])} for row in rows]

    def _notify_message_event(self) -> None:
        with self._message_event_condition:
            self._message_event_sequence += 1
            self._message_event_condition.notify_all()

    def send_message(
        self,
        device_token: str,
        room_id: str,
        content: str,
        image_data: str = "",
        client_upload_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        current = self.authenticate(device_token)
        clean_upload_id = self._clean_client_upload_id(client_upload_id)
        clean_content = str(content or "").strip()
        if len(clean_content) > 4000:
            raise LanChatError("消息不能超过 4000 个字符")
        with self._connect() as conn:
            self._require_room_access(conn, room_id, current["id"])
            existing = self._client_upload_message(
                conn, current["id"], room_id, clean_upload_id
            )
            if existing is not None:
                return existing, False
        media = self._decode_message_media(image_data)
        if not clean_content and media is None:
            raise LanChatError("消息或媒体不能为空")
        now = time.time()
        media_filename = ""
        media_mime_type = ""
        if media is not None:
            media_bytes, media_mime_type, extension = media
            media_filename = f"{uuid.uuid4().hex}.{extension}"
            (self.media_dir / media_filename).write_bytes(media_bytes)
        try:
            with self._connect() as conn:
                self._require_room_access(conn, room_id, current["id"])
                cursor = conn.execute(
                    """INSERT INTO messages
                       (room_id, sender_id, content, image_filename, image_mime_type,
                        media_expires_at, media_deleted_at, client_upload_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (
                        room_id,
                        current["id"],
                        clean_content,
                        media_filename or None,
                        media_mime_type or None,
                        now + MESSAGE_MEDIA_RETENTION_SECONDS if media_filename else None,
                        clean_upload_id or None,
                        now,
                    ),
                )
                conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
                row = conn.execute(
                    """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                       FROM messages m JOIN users u ON u.id = m.sender_id
                       WHERE m.id = ?""",
                    (cursor.lastrowid,),
                ).fetchone()
                payload = self._message_payload(conn, row, current["id"])
        except sqlite3.IntegrityError:
            if media_filename:
                (self.media_dir / media_filename).unlink(missing_ok=True)
            if clean_upload_id:
                with self._connect() as conn:
                    self._require_room_access(conn, room_id, current["id"])
                    existing = self._client_upload_message(
                        conn, current["id"], room_id, clean_upload_id
                    )
                    if existing is not None:
                        return existing, False
            raise
        except Exception:
            if media_filename:
                (self.media_dir / media_filename).unlink(missing_ok=True)
            raise
        self._notify_message_event()
        return payload, True

    def send_media_file(
        self,
        device_token: str,
        room_id: str,
        original_name: str,
        file_stream: BinaryIO,
        content: str = "",
        client_upload_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Store inline image/video media without buffering a Base64 payload in memory."""
        current = self.authenticate(device_token)
        clean_upload_id = self._clean_client_upload_id(client_upload_id)
        clean_content = str(content or "").strip()
        if len(clean_content) > 4000:
            raise LanChatError("消息不能超过 4000 个字符")
        with self._connect() as conn:
            self._require_room_access(conn, room_id, current["id"])
            existing = self._client_upload_message(
                conn, current["id"], room_id, clean_upload_id
            )
            if existing is not None:
                return existing, False

        upload_id = uuid.uuid4().hex
        temp_path = self.media_dir / f".{upload_id}.upload"
        size_bytes = 0
        header = b""
        final_path: Path | None = None
        try:
            with temp_path.open("wb") as output:
                while True:
                    chunk = file_stream.read(FILE_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise LanChatError("上传媒体数据无效")
                    size_bytes += len(chunk)
                    if size_bytes > MESSAGE_MEDIA_MAX_BYTES:
                        raise LanChatError("图片或视频不能超过 100MB，请改用文件发送", 413)
                    if len(header) < 16:
                        header += chunk[: 16 - len(header)]
                    output.write(chunk)
            if size_bytes <= 0:
                raise LanChatError("上传媒体不能为空")
            extension = self._message_media_extension(header)
            media_filename = f"{upload_id}.{extension}"
            final_path = self.media_dir / media_filename
            temp_path.replace(final_path)
            now = time.time()
            with self._connect() as conn:
                self._require_room_access(conn, room_id, current["id"])
                existing = self._client_upload_message(
                    conn, current["id"], room_id, clean_upload_id
                )
                if existing is not None:
                    final_path.unlink(missing_ok=True)
                    return existing, False
                cursor = conn.execute(
                    """INSERT INTO messages
                       (room_id, sender_id, content, image_filename, image_mime_type,
                        media_expires_at, media_deleted_at, client_upload_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (
                        room_id,
                        current["id"],
                        clean_content,
                        media_filename,
                        MESSAGE_MEDIA_TYPES[extension],
                        now + MESSAGE_MEDIA_RETENTION_SECONDS,
                        clean_upload_id or None,
                        now,
                    ),
                )
                conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
                row = conn.execute(
                    """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                       FROM messages m JOIN users u ON u.id = m.sender_id
                       WHERE m.id = ?""",
                    (cursor.lastrowid,),
                ).fetchone()
                payload = self._message_payload(conn, row, current["id"])
        except Exception:
            temp_path.unlink(missing_ok=True)
            if final_path is not None:
                final_path.unlink(missing_ok=True)
            raise
        self._notify_message_event()
        return payload, True

    def send_file(
        self,
        device_token: str,
        room_id: str,
        original_name: str,
        mime_type: str,
        file_stream: BinaryIO,
        content: str = "",
        client_upload_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        current = self.authenticate(device_token)
        clean_upload_id = self._clean_client_upload_id(client_upload_id)
        clean_content = str(content or "").strip()
        if len(clean_content) > 4000:
            raise LanChatError("消息不能超过 4000 个字符")
        clean_name = self._clean_file_name(original_name)
        clean_mime = str(mime_type or "application/octet-stream").strip().lower()
        if not clean_mime or len(clean_mime) > 127 or any(char in clean_mime for char in "\r\n"):
            clean_mime = "application/octet-stream"

        with self._connect() as conn:
            room = self._require_room_access(conn, room_id, current["id"])
            existing = self._client_upload_message(
                conn, current["id"], room_id, clean_upload_id
            )
            if existing is not None:
                return existing, False
            receiver_ids = []
            if room["kind"] == "direct":
                receiver_ids = [
                    str(row["user_id"])
                    for row in conn.execute(
                        "SELECT user_id FROM room_members WHERE room_id = ? AND user_id != ?",
                        (room_id, current["id"]),
                    ).fetchall()
                ]
                if len(receiver_ids) != 1:
                    raise LanChatError("私信接收人不存在", 409)

        attachment_id = uuid.uuid4().hex
        final_path = self.file_dir / attachment_id
        temp_path = self.file_dir / f".{attachment_id}.upload"
        size_bytes = 0
        try:
            with temp_path.open("wb") as output:
                while True:
                    chunk = file_stream.read(FILE_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise LanChatError("上传文件数据无效")
                    size_bytes += len(chunk)
                    if size_bytes > FILE_TRANSFER_MAX_BYTES:
                        raise LanChatError("文件不能超过 10GB", 413)
                    output.write(chunk)
            if size_bytes <= 0:
                raise LanChatError("上传文件不能为空")
            temp_path.replace(final_path)

            now = time.time()
            expires_at = now + FILE_TRANSFER_RETENTION_SECONDS
            with self._connect() as conn:
                room = self._require_room_access(conn, room_id, current["id"])
                existing = self._client_upload_message(
                    conn, current["id"], room_id, clean_upload_id
                )
                if existing is not None:
                    final_path.unlink(missing_ok=True)
                    return existing, False
                conn.execute(
                    """INSERT INTO file_attachments
                       (id, room_id, sender_id, stored_filename, original_name, mime_type,
                        size_bytes, expires_at, deleted_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (
                        attachment_id,
                        room_id,
                        current["id"],
                        attachment_id,
                        clean_name,
                        clean_mime,
                        size_bytes,
                        expires_at,
                        now,
                    ),
                )
                cursor = conn.execute(
                    """INSERT INTO messages
                       (room_id, sender_id, content, image_filename, image_mime_type,
                        file_id, client_upload_id, created_at)
                       VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)""",
                    (
                        room_id,
                        current["id"],
                        clean_content,
                        attachment_id,
                        clean_upload_id or None,
                        now,
                    ),
                )
                if room["kind"] == "direct":
                    conn.execute(
                        """INSERT INTO file_receipts
                           (file_id, user_id, status, decided_at)
                           VALUES (?, ?, 'pending', NULL)""",
                        (attachment_id, receiver_ids[0]),
                    )
                conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
                row = conn.execute(
                    """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                       FROM messages m JOIN users u ON u.id = m.sender_id
                       WHERE m.id = ?""",
                    (cursor.lastrowid,),
                ).fetchone()
                payload = self._message_payload(conn, row, current["id"])
        except sqlite3.IntegrityError:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            if clean_upload_id:
                with self._connect() as conn:
                    self._require_room_access(conn, room_id, current["id"])
                    existing = self._client_upload_message(
                        conn, current["id"], room_id, clean_upload_id
                    )
                    if existing is not None:
                        return existing, False
            raise
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        self._notify_message_event()
        return payload, True

    def accept_file(self, device_token: str, file_id: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        clean_id = self._clean_file_id(file_id)
        self.cleanup_expired_files()
        with self._connect() as conn:
            attachment = conn.execute(
                "SELECT * FROM file_attachments WHERE id = ?", (clean_id,)
            ).fetchone()
            if attachment is None:
                raise LanChatError("文件不存在", 404)
            room = self._require_room_access(conn, attachment["room_id"], current["id"])
            if room["kind"] != "direct" or attachment["sender_id"] == current["id"]:
                raise LanChatError("该文件不需要确认接收", 409)
            if attachment["deleted_at"] is not None or float(attachment["expires_at"]) <= time.time():
                raise LanChatError("文件已过期并从服务器清理", 410)
            receipt = conn.execute(
                "SELECT status FROM file_receipts WHERE file_id = ? AND user_id = ?",
                (clean_id, current["id"]),
            ).fetchone()
            if receipt is None:
                raise LanChatError("你不是该文件的接收人", 403)
            conn.execute(
                """UPDATE file_receipts SET status = 'accepted', decided_at = ?
                   WHERE file_id = ? AND user_id = ?""",
                (time.time(), clean_id, current["id"]),
            )
            row = conn.execute(
                """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                   FROM messages m JOIN users u ON u.id = m.sender_id
                   WHERE m.file_id = ?""",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise LanChatError("文件消息不存在", 404)
            return self._message_payload(conn, row, current["id"])

    def file_download_info(
        self, device_token: str, file_id: str
    ) -> tuple[Path, str, str, int]:
        current = self.authenticate(device_token)
        clean_id = self._clean_file_id(file_id)
        self.cleanup_expired_files()
        with self._connect() as conn:
            attachment = conn.execute(
                "SELECT * FROM file_attachments WHERE id = ?", (clean_id,)
            ).fetchone()
            if attachment is None:
                raise LanChatError("文件不存在", 404)
            room = self._require_room_access(conn, attachment["room_id"], current["id"])
            if attachment["deleted_at"] is not None or float(attachment["expires_at"]) <= time.time():
                raise LanChatError("文件已过期并从服务器清理", 410)
            if room["kind"] == "direct" and attachment["sender_id"] != current["id"]:
                receipt = conn.execute(
                    "SELECT status FROM file_receipts WHERE file_id = ? AND user_id = ?",
                    (clean_id, current["id"]),
                ).fetchone()
                if receipt is None or receipt["status"] != "accepted":
                    raise LanChatError("请先确认接收该文件", 403)
            path = self._stored_file_path(attachment["stored_filename"])
            if not path.is_file():
                raise LanChatError("文件已从服务器清理", 410)
            return (
                path,
                str(attachment["original_name"]),
                str(attachment["mime_type"]),
                int(attachment["size_bytes"]),
            )

    def cleanup_expired_files(self, now: float | None = None) -> int:
        cutoff = float(now if now is not None else time.time())
        cleaned = 0
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, stored_filename FROM file_attachments
                   WHERE deleted_at IS NULL AND expires_at <= ?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                try:
                    self._stored_file_path(row["stored_filename"]).unlink(missing_ok=True)
                except OSError as exc:
                    print(f"LAN chat file cleanup failed for {row['id']}: {exc}", flush=True)
                    continue
                conn.execute(
                    "UPDATE file_attachments SET deleted_at = ? WHERE id = ?",
                    (cutoff, row["id"]),
                )
                cleaned += 1
        return cleaned

    def cleanup_expired_media(self, now: float | None = None) -> int:
        cutoff = float(now if now is not None else time.time())
        cleaned = 0
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, image_filename FROM messages
                   WHERE image_filename IS NOT NULL
                     AND media_deleted_at IS NULL
                     AND media_expires_at <= ?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                filename = str(row["image_filename"] or "")
                try:
                    (self.media_dir / filename).unlink(missing_ok=True)
                    (self.media_dir / f"{Path(filename).stem}.poster.jpg").unlink(
                        missing_ok=True
                    )
                except OSError as exc:
                    print(
                        f"LAN chat media cleanup failed for {row['id']}: {exc}",
                        flush=True,
                    )
                    continue
                conn.execute(
                    "UPDATE messages SET media_deleted_at = ? WHERE id = ?",
                    (cutoff, row["id"]),
                )
                cleaned += 1
        return cleaned

    def _start_file_janitor(self) -> None:
        with self._file_janitor_lock:
            if self._file_janitor_started:
                return
            self._file_janitor_started = True

        def run() -> None:
            while True:
                time.sleep(FILE_TRANSFER_CLEANUP_INTERVAL_SECONDS)
                try:
                    self.cleanup_expired_files()
                    self.cleanup_expired_media()
                except Exception as exc:
                    print(f"LAN chat storage janitor failed: {exc}", flush=True)

        threading.Thread(target=run, name="lan-chat-file-janitor", daemon=True).start()

    def message_media_info(self, filename: str) -> tuple[Path, str, str, int]:
        clean_name = str(filename or "").strip().lower()
        stem, separator, extension = clean_name.rpartition(".")
        if (
            not separator
            or len(stem) != 32
            or extension not in MESSAGE_MEDIA_TYPES
            or any(char not in "0123456789abcdef" for char in stem)
        ):
            raise LanChatError("媒体不存在", 404)
        with self._connect() as conn:
            media = conn.execute(
                """SELECT media_expires_at, media_deleted_at FROM messages
                   WHERE image_filename = ? LIMIT 1""",
                (clean_name,),
            ).fetchone()
        if media is not None and (
            media["media_deleted_at"] is not None
            or (
                media["media_expires_at"] is not None
                and float(media["media_expires_at"]) <= time.time()
            )
        ):
            self.cleanup_expired_media()
            raise LanChatError("媒体已过期并从服务器清理", 410)
        path = (self.media_dir / clean_name).resolve()
        if path.parent != self.media_dir.resolve() or not path.is_file():
            raise LanChatError("媒体不存在", 404)
        return path, clean_name, MESSAGE_MEDIA_TYPES[extension], path.stat().st_size

    def message_media_bytes(self, filename: str) -> tuple[bytes, str]:
        path, _, content_type, _ = self.message_media_info(filename)
        return path.read_bytes(), content_type

    def message_video_poster_bytes(self, filename: str) -> tuple[bytes, str]:
        video_path, clean_name, content_type, _ = self.message_media_info(filename)
        if not content_type.startswith("video/"):
            raise LanChatError("视频封面不存在", 404)
        poster_path = self.media_dir / f"{Path(clean_name).stem}.poster.jpg"
        with self._media_poster_lock:
            if not poster_path.is_file():
                temp_path = self.media_dir / f".{Path(clean_name).stem}.{uuid.uuid4().hex}.poster.jpg"
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "0.1",
                            "-i", str(video_path), "-frames:v", "1", "-an", "-vf",
                            "scale=960:-2:force_original_aspect_ratio=decrease",
                            "-q:v", "4", "-y", str(temp_path),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=30,
                        check=False,
                    )
                    if result.returncode != 0 or not temp_path.is_file() or temp_path.stat().st_size <= 0:
                        raise LanChatError("视频封面生成失败", 404)
                    temp_path.replace(poster_path)
                except (OSError, subprocess.SubprocessError):
                    raise LanChatError("视频封面生成失败", 404)
                finally:
                    temp_path.unlink(missing_ok=True)
        return poster_path.read_bytes(), "image/jpeg"

    def message_image_bytes(self, filename: str) -> tuple[bytes, str]:
        """Backward-compatible alias for callers predating inline video support."""
        return self.message_media_bytes(filename)

    def avatar_bytes(self, user_id: str) -> tuple[bytes, str]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise LanChatError("头像不存在", 404)
        filename = str(row["avatar_filename"] or "")
        if filename:
            path = (self.avatar_dir / filename).resolve()
            if path.parent == self.avatar_dir.resolve() and path.is_file():
                return path.read_bytes(), "image/png"
        return self._fallback_avatar(row["nickname"], row["avatar_color"]), "image/svg+xml; charset=utf-8"

    def feishu_avatar_bytes(self, owner_id: str) -> tuple[bytes, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name, avatar_url FROM feishu_users WHERE id = ? AND active = 1",
                (owner_id,),
            ).fetchone()
        if row is None:
            raise LanChatError("飞书头像不存在", 404)

        color = AVATAR_COLORS[
            int(hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:8], 16)
            % len(AVATAR_COLORS)
        ]
        fallback = self._fallback_avatar(row["name"], color)
        avatar_url = str(row["avatar_url"] or "").strip()
        if not avatar_url.startswith(("https://", "http://")):
            return fallback, "image/svg+xml; charset=utf-8"

        digest = hashlib.sha256(avatar_url.encode("utf-8")).hexdigest()[:24]
        with self._feishu_avatar_lock:
            for content_type, extension in FEISHU_AVATAR_TYPES.items():
                path = self.avatar_dir / f"feishu-{digest}.{extension}"
                if path.is_file():
                    return path.read_bytes(), content_type
            try:
                request = Request(
                    avatar_url,
                    headers={"Accept": "image/*", "User-Agent": "Short-Video-Analyzer/1.0"},
                )
                with urlopen(request, timeout=15) as response:
                    content_type = str(response.headers.get_content_type()).lower()
                    image = response.read(FEISHU_AVATAR_MAX_BYTES + 1)
                extension = FEISHU_AVATAR_TYPES.get(content_type)
                if not extension or not image or len(image) > FEISHU_AVATAR_MAX_BYTES:
                    raise ValueError("invalid Feishu avatar response")
                path = self.avatar_dir / f"feishu-{digest}.{extension}"
                tmp_path = self.avatar_dir / f".{path.name}.tmp"
                tmp_path.write_bytes(image)
                tmp_path.replace(path)
                return image, content_type
            except Exception as exc:
                print(f"Feishu avatar fetch failed for {owner_id}: {exc}", flush=True)
                return fallback, "image/svg+xml; charset=utf-8"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _token_hash(device_token: str) -> str:
        token = str(device_token or "").strip()
        if len(token) < 20 or len(token) > 200:
            raise LanChatError("登录状态无效，请重新选择账户", 401)
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _create_session(
        self, conn: sqlite3.Connection, user_id: str, now: float
    ) -> str:
        for _ in range(3):
            session_token = secrets.token_urlsafe(32)
            try:
                conn.execute(
                    """INSERT INTO account_sessions
                       (token_hash, user_id, created_at, last_seen)
                       VALUES (?, ?, ?, ?)""",
                    (self._token_hash(session_token), user_id, now, now),
                )
                return session_token
            except sqlite3.IntegrityError:
                continue
        raise LanChatError("无法创建登录会话，请重试", 500)

    @staticmethod
    def _nickname(value: str, default: str = "") -> str:
        nickname = " ".join(str(value or "").split()) or default
        if not nickname or len(nickname) > 24:
            raise LanChatError("昵称需要 1-24 个字符")
        return nickname

    @staticmethod
    def _nickname_key(value: str) -> str:
        return " ".join(str(value or "").split()).casefold()

    def _require_nickname_available(
        self, conn: sqlite3.Connection, nickname: str, exclude_user_id: str = ""
    ) -> None:
        nickname_key = self._nickname_key(nickname)
        rows = conn.execute("SELECT id, nickname FROM users").fetchall()
        if any(
            str(row["id"]) != exclude_user_id
            and self._nickname_key(row["nickname"]) == nickname_key
            for row in rows
        ):
            raise LanChatError("昵称已被使用，请换一个", 409)

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, Any]:
        last_seen = float(row["last_seen"])
        return {
            "id": row["id"],
            "feishuUserId": row["feishu_user_id"],
            "nickname": row["nickname"],
            "avatarUrl": f"/api/lan-chat/avatars/{row['id']}?v={row['avatar_status']}",
            "avatarColor": row["avatar_color"],
            "avatarStatus": row["avatar_status"],
            "createdAt": float(row["created_at"]),
            "lastSeen": last_seen,
            "online": time.time() - last_seen <= ONLINE_WINDOW_SECONDS,
        }

    def _room_payload(
        self, conn: sqlite3.Connection, room: sqlite3.Row, current_user_id: str
    ) -> dict[str, Any]:
        members = conn.execute(
            """SELECT u.*, rm.joined_at FROM users u
               JOIN room_members rm ON rm.user_id = u.id
               WHERE rm.room_id = ? ORDER BY rm.joined_at ASC, u.id ASC""",
            (room["id"],),
        ).fetchall()
        system_kind = str(room["system_kind"] or "custom")
        admin_user_id = str(room["admin_user_id"] or "") or None
        is_custom_group = room["kind"] == "group" and system_kind == "custom"
        current_user_is_admin = is_custom_group and admin_user_id == current_user_id
        name = room["name"]
        if room["kind"] == "direct":
            other = next((item for item in members if item["id"] != current_user_id), None)
            name = other["nickname"] if other is not None else "私信"
        latest = conn.execute(
            """SELECT m.content, m.image_filename, m.image_mime_type,
                      m.media_expires_at, m.media_deleted_at, m.file_id,
                      m.created_at, u.nickname
               FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.room_id = ? ORDER BY m.id DESC LIMIT 1""",
            (room["id"],),
        ).fetchone()
        unread_count = int(
            conn.execute(
                """SELECT COUNT(*) FROM messages m
                   WHERE m.room_id = ? AND m.sender_id != ?
                     AND m.id > COALESCE(
                         (SELECT rr.last_read_message_id FROM room_reads rr
                          WHERE rr.room_id = ? AND rr.user_id = ?),
                         0
                     )""",
                (room["id"], current_user_id, room["id"], current_user_id),
            ).fetchone()[0]
        )
        latest_media_expired = bool(
            latest is not None
            and latest["image_filename"]
            and (
                latest["media_deleted_at"] is not None
                or (
                    latest["media_expires_at"] is not None
                    and float(latest["media_expires_at"]) <= time.time()
                )
            )
        )
        return {
            "id": room["id"],
            "kind": room["kind"],
            "name": name,
            "memberCount": len(members) if room["kind"] != "public" else self._user_count(conn),
            "members": [
                {
                    **self._public_user(item),
                    "joinedAt": float(item["joined_at"]),
                    "isAdmin": item["id"] == admin_user_id,
                    "isCurrent": item["id"] == current_user_id,
                }
                for item in members
            ],
            "createdBy": room["created_by"],
            "systemKind": system_kind,
            "isDefault": system_kind in {"public", "feishu"},
            "adminUserId": admin_user_id,
            "currentUserIsAdmin": current_user_is_admin,
            "updatedAt": float(room["updated_at"]),
            "unreadCount": unread_count,
            "latestMessage": (
                {
                    "content": latest["content"],
                    "hasImage": bool(latest["image_filename"]) and not latest_media_expired,
                    "mediaExpired": latest_media_expired,
                    "mediaKind": (
                        "video"
                        if str(latest["image_mime_type"] or "").startswith("video/")
                        else "image" if latest["image_filename"] else ""
                    ),
                    "hasFile": bool(latest["file_id"]),
                    "nickname": latest["nickname"],
                    "createdAt": float(latest["created_at"]),
                }
                if latest is not None
                else None
            ),
            "canRename": current_user_is_admin,
            "canRemoveMembers": current_user_is_admin,
            "canLeave": is_custom_group,
            "canDissolve": current_user_is_admin,
        }

    @staticmethod
    def _user_count(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def _message_payload(
        self, conn: sqlite3.Connection, row: sqlite3.Row, current_user_id: str
    ) -> dict[str, Any]:
        media_filename = str(row["image_filename"] or "")
        media_mime_type = str(row["image_mime_type"] or "")
        media_expires_at = (
            float(row["media_expires_at"])
            if row["media_expires_at"] is not None
            else None
        )
        media_expired = bool(
            media_filename
            and (
                row["media_deleted_at"] is not None
                or (
                    media_expires_at is not None
                    and media_expires_at <= time.time()
                )
            )
        )
        available_media_filename = media_filename if not media_expired else ""
        file_payload = None
        file_id = str(row["file_id"] or "")
        if file_id:
            attachment = conn.execute(
                """SELECT f.*, r.kind AS room_kind
                   FROM file_attachments f JOIN rooms r ON r.id = f.room_id
                   WHERE f.id = ?""",
                (file_id,),
            ).fetchone()
            if attachment is not None:
                is_sender = str(attachment["sender_id"]) == current_user_id
                receipt = None
                if attachment["room_kind"] == "direct":
                    if is_sender:
                        receipt = conn.execute(
                            "SELECT status FROM file_receipts WHERE file_id = ? LIMIT 1",
                            (file_id,),
                        ).fetchone()
                    else:
                        receipt = conn.execute(
                            """SELECT status FROM file_receipts
                               WHERE file_id = ? AND user_id = ?""",
                            (file_id, current_user_id),
                        ).fetchone()
                expired = (
                    attachment["deleted_at"] is not None
                    or float(attachment["expires_at"]) <= time.time()
                )
                receipt_status = (
                    str(receipt["status"])
                    if receipt is not None
                    else "available" if attachment["room_kind"] != "direct" else "pending"
                )
                download_allowed = (
                    not expired
                    and (
                        attachment["room_kind"] != "direct"
                        or is_sender
                        or receipt_status == "accepted"
                    )
                )
                file_payload = {
                    "id": file_id,
                    "name": attachment["original_name"],
                    "mimeType": attachment["mime_type"],
                    "size": int(attachment["size_bytes"]),
                    "expiresAt": float(attachment["expires_at"]),
                    "expired": expired,
                    "requiresAcceptance": attachment["room_kind"] == "direct" and not is_sender,
                    "receiptStatus": receipt_status,
                    "downloadAllowed": download_allowed,
                    "downloadUrl": f"/api/lan-chat/files/{file_id}" if download_allowed else "",
                }
        return {
            "id": int(row["id"]),
            "roomId": row["room_id"],
            "clientUploadId": str(row["client_upload_id"] or ""),
            "senderId": row["sender_id"],
            "senderName": row["nickname"],
            "senderAvatarUrl": f"/api/lan-chat/avatars/{row['sender_id']}",
            "content": row["content"],
            "imageUrl": (
                f"/api/lan-chat/media/{available_media_filename}"
                if available_media_filename and media_mime_type.startswith("image/")
                else ""
            ),
            "mediaUrl": (
                f"/api/lan-chat/media/{available_media_filename}"
                if available_media_filename
                else ""
            ),
            "mediaPosterUrl": (
                f"/api/lan-chat/media/{available_media_filename}/poster"
                if available_media_filename and media_mime_type.startswith("video/")
                else ""
            ),
            "mediaDownloadUrl": (
                f"/api/lan-chat/media/{available_media_filename}/download"
                if available_media_filename
                else ""
            ),
            "mediaMimeType": media_mime_type,
            "mediaKind": (
                "video" if media_mime_type.startswith("video/")
                else "image" if media_filename else ""
            ),
            "mediaExpiresAt": media_expires_at,
            "mediaExpired": media_expired,
            "file": file_payload,
            "createdAt": float(row["created_at"]),
            "isMine": row["sender_id"] == current_user_id,
        }

    def _client_upload_message(
        self,
        conn: sqlite3.Connection,
        sender_id: str,
        room_id: str,
        client_upload_id: str,
    ) -> dict[str, Any] | None:
        if not client_upload_id:
            return None
        row = conn.execute(
            """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
               FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.sender_id = ? AND m.client_upload_id = ?""",
            (sender_id, client_upload_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["room_id"]) != room_id:
            raise LanChatError("clientUploadId 已用于其他房间", 409)
        return self._message_payload(conn, row, sender_id)

    @staticmethod
    def _clean_client_upload_id(value: str) -> str:
        clean_value = str(value or "").strip()
        if not clean_value:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", clean_value):
            raise LanChatError("clientUploadId 格式无效")
        return clean_value

    @staticmethod
    def _decode_message_media(media_data: str) -> tuple[bytes, str, str] | None:
        value = str(media_data or "").strip()
        if not value:
            return None
        if "," in value:
            header, value = value.split(",", 1)
            if not header.lower().startswith(("data:image/", "data:video/")):
                raise LanChatError("媒体数据无效")
        if len(value) > (MESSAGE_MEDIA_MAX_BYTES * 4 // 3) + 8:
            raise LanChatError("图片或视频不能超过 100MB，请改用文件发送", 413)
        try:
            payload = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise LanChatError("媒体数据无效") from exc
        if not payload or len(payload) > MESSAGE_MEDIA_MAX_BYTES:
            raise LanChatError("图片或视频不能超过 100MB，请改用文件发送", 413)
        extension = LanChatStore._message_media_extension(payload[:16])
        return payload, MESSAGE_MEDIA_TYPES[extension], extension

    @staticmethod
    def _message_media_extension(header: bytes) -> str:
        if header.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "webp"
        if len(header) >= 12 and header[4:8] == b"ftyp":
            return "mp4"
        if header.startswith(b"\x1a\x45\xdf\xa3"):
            return "webm"
        raise LanChatError("仅支持 JPG、PNG、GIF、WebP、MP4 或 WebM 媒体")

    @staticmethod
    def _clean_file_name(value: str) -> str:
        filename = Path(str(value or "").replace("\\", "/")).name
        filename = "".join(char for char in filename if ord(char) >= 32 and char != "\x7f").strip()
        if not filename or filename in {".", ".."}:
            raise LanChatError("文件名无效")
        if len(filename.encode("utf-8")) > 255:
            raise LanChatError("文件名不能超过 255 字节")
        return filename

    @staticmethod
    def _clean_file_id(value: str) -> str:
        file_id = str(value or "").strip().lower()
        if len(file_id) != 32 or any(char not in "0123456789abcdef" for char in file_id):
            raise LanChatError("文件不存在", 404)
        return file_id

    def _stored_file_path(self, value: str) -> Path:
        filename = self._clean_file_id(value)
        path = (self.file_dir / filename).resolve()
        if path.parent != self.file_dir.resolve():
            raise LanChatError("文件不存在", 404)
        return path

    @staticmethod
    def _require_custom_group(
        conn: sqlite3.Connection, room_id: str, user_id: str, admin: bool = False
    ) -> sqlite3.Row:
        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if room is None:
            raise LanChatError("群组不存在", 404)
        if room["kind"] != "group":
            raise LanChatError("该频道不是群组")
        if room["system_kind"] != "custom":
            raise LanChatError("默认群组由系统维护，不能修改、退出或解散", 403)
        member = conn.execute(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        ).fetchone()
        if member is None:
            raise LanChatError("无权访问此群组", 403)
        if admin and room["admin_user_id"] != user_id:
            raise LanChatError("只有群组管理员可以执行此操作", 403)
        return room

    @staticmethod
    def _require_room_access(
        conn: sqlite3.Connection, room_id: str, user_id: str
    ) -> sqlite3.Row:
        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if room is None:
            raise LanChatError("频道不存在", 404)
        if room["kind"] != "public":
            member = conn.execute(
                "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
                (room_id, user_id),
            ).fetchone()
            if member is None:
                raise LanChatError("无权访问此频道", 403)
        return room

    @staticmethod
    def _fallback_avatar(nickname: str, color: str) -> bytes:
        initial = html.escape((nickname.strip() or "邻")[0])
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
<rect width="128" height="128" rx="32" fill="{html.escape(color)}"/>
<circle cx="96" cy="28" r="30" fill="#ffffff" opacity=".12"/>
<circle cx="28" cy="104" r="36" fill="#0f172a" opacity=".08"/>
<text x="64" y="76" text-anchor="middle" font-family="system-ui,sans-serif" font-size="52" font-weight="700" fill="#ffffff">{initial}</text>
</svg>"""
        return svg.encode("utf-8")

    @staticmethod
    def _avatar_configured() -> bool:
        return bool(
            os.getenv("LAN_CHAT_AVATAR_API_URL", "").strip()
            and os.getenv("LAN_CHAT_AVATAR_API_KEY", "").strip()
            and os.getenv("LAN_CHAT_AVATAR_MODEL", "").strip()
        )

    def _start_avatar_generation(self, user_id: str, nickname: str) -> None:
        if not self._avatar_configured():
            return
        with self._avatar_lock:
            if user_id in self._avatar_jobs:
                return
            self._avatar_jobs.add(user_id)
        threading.Thread(
            target=self._generate_avatar,
            args=(user_id, nickname),
            daemon=True,
            name=f"lan-avatar-{user_id[:6]}",
        ).start()

    def _generate_avatar(self, user_id: str, nickname: str) -> None:
        try:
            endpoint = os.environ["LAN_CHAT_AVATAR_API_URL"].strip()
            api_key = os.environ["LAN_CHAT_AVATAR_API_KEY"].strip()
            model = os.environ["LAN_CHAT_AVATAR_MODEL"].strip()
            prompt = (
                "Create one friendly editorial illustrated profile avatar for a local chat app. "
                f"The account nickname is {nickname!r}; do not render any text. "
                "Centered head-and-shoulders portrait, warm natural expression, simple muted color "
                "background, mature polished digital gouache style, square crop, no logo, no watermark."
            )
            body = json.dumps(
                {"model": model, "prompt": prompt, "size": "1024x1024", "response_format": "b64_json"}
            ).encode("utf-8")
            request = Request(
                endpoint,
                data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            timeout = max(10, min(int(os.getenv("LAN_CHAT_AVATAR_TIMEOUT", "90")), 300))
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read(10 * 1024 * 1024).decode("utf-8"))
            first = (payload.get("data") or [{}])[0]
            if first.get("b64_json"):
                image = base64.b64decode(first["b64_json"], validate=True)
            elif first.get("url"):
                with urlopen(first["url"], timeout=timeout) as response:
                    image = response.read(8 * 1024 * 1024)
            else:
                raise ValueError("avatar API returned no image")
            if not image or len(image) > 8 * 1024 * 1024:
                raise ValueError("avatar image size is invalid")
            filename = f"{user_id}.png"
            tmp_path = self.avatar_dir / f".{filename}.tmp"
            tmp_path.write_bytes(image)
            tmp_path.replace(self.avatar_dir / filename)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE users SET avatar_status = 'ready', avatar_filename = ? WHERE id = ?",
                    (filename, user_id),
                )
        except Exception as exc:
            print(f"LAN chat avatar generation failed for {user_id}: {exc}", flush=True)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE users SET avatar_status = 'fallback' WHERE id = ?", (user_id,)
                )
        finally:
            with self._avatar_lock:
                self._avatar_jobs.discard(user_id)
