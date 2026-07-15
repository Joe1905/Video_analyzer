"""Persistent device-based chat for trusted local-area networks."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


PUBLIC_ROOM_ID = "public"
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


class LanChatError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class LanChatStore:
    def __init__(self, db_path: Path, avatar_dir: Path | None = None):
        self.db_path = Path(db_path)
        self.avatar_dir = Path(avatar_dir or self.db_path.parent / "lan_chat_avatars")
        self._avatar_lock = threading.Lock()
        self._avatar_jobs: set[str] = set()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    device_token_hash TEXT NOT NULL UNIQUE,
                    nickname TEXT NOT NULL,
                    avatar_color TEXT NOT NULL,
                    avatar_status TEXT NOT NULL DEFAULT 'fallback',
                    avatar_filename TEXT,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('public', 'direct', 'group')),
                    name TEXT NOT NULL DEFAULT '',
                    created_by TEXT,
                    direct_key TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (created_by) REFERENCES users(id)
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
                    created_at REAL NOT NULL,
                    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
                    FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS messages_room_id_idx
                    ON messages(room_id, id);
                CREATE INDEX IF NOT EXISTS room_members_user_id_idx
                    ON room_members(user_id, room_id);
                """
            )
            now = time.time()
            conn.execute(
                """INSERT OR IGNORE INTO rooms
                   (id, kind, name, created_by, direct_key, created_at, updated_at)
                   VALUES (?, 'public', ?, NULL, NULL, ?, ?)""",
                (PUBLIC_ROOM_ID, "公共频道", now, now),
            )

    def register(self, device_token: str, nickname: str = "") -> tuple[dict[str, Any], bool]:
        token_hash = self._token_hash(device_token)
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE device_token_hash = ?", (token_hash,)
            ).fetchone()
            created = row is None
            if row is None:
                user_id = uuid.uuid4().hex[:16]
                clean_name = self._nickname(nickname, default=f"访客-{token_hash[:4].upper()}")
                avatar_status = "pending" if self._avatar_configured() else "fallback"
                conn.execute(
                    """INSERT INTO users
                       (id, device_token_hash, nickname, avatar_color, avatar_status,
                        avatar_filename, created_at, last_seen)
                       VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (
                        user_id,
                        token_hash,
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
        user = self._public_user(row)
        if user["avatarStatus"] == "pending":
            self._start_avatar_generation(user["id"], user["nickname"])
        return user, created

    def authenticate(self, device_token: str) -> dict[str, Any]:
        token_hash = self._token_hash(device_token)
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE device_token_hash = ?", (token_hash,)
            ).fetchone()
            if row is None:
                raise LanChatError("设备尚未注册", 401)
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
            "pollIntervalMs": 2000,
        }

    def update_profile(self, device_token: str, nickname: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        clean_name = self._nickname(nickname)
        with self._connect() as conn:
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
                   ORDER BY CASE WHEN r.kind = 'public' THEN 0 ELSE 1 END,
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
                       (id, kind, name, created_by, direct_key, created_at, updated_at)
                       VALUES (?, 'direct', '', ?, ?, ?, ?)""",
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
                   (id, kind, name, created_by, direct_key, created_at, updated_at)
                   VALUES (?, 'group', ?, ?, NULL, ?, ?)""",
                (room_id, clean_name, current["id"], now, now),
            )
            conn.executemany(
                "INSERT INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
                [(room_id, member_id, now) for member_id in members],
            )
            row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            return self._room_payload(conn, row, current["id"])

    def list_messages(
        self, device_token: str, room_id: str, after_id: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        current = self.authenticate(device_token)
        after_id = max(0, int(after_id or 0))
        limit = max(1, min(int(limit or 100), 200))
        with self._connect() as conn:
            self._require_room_access(conn, room_id, current["id"])
            rows = conn.execute(
                """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                   FROM messages m
                   JOIN users u ON u.id = m.sender_id
                   WHERE m.room_id = ? AND m.id > ?
                   ORDER BY m.id ASC LIMIT ?""",
                (room_id, after_id, limit),
            ).fetchall()
        messages = [self._message_payload(row, current["id"]) for row in rows]
        return {"messages": messages, "lastId": messages[-1]["id"] if messages else after_id}

    def send_message(self, device_token: str, room_id: str, content: str) -> dict[str, Any]:
        current = self.authenticate(device_token)
        clean_content = str(content or "").strip()
        if not clean_content:
            raise LanChatError("消息不能为空")
        if len(clean_content) > 4000:
            raise LanChatError("消息不能超过 4000 个字符")
        now = time.time()
        with self._connect() as conn:
            self._require_room_access(conn, room_id, current["id"])
            cursor = conn.execute(
                "INSERT INTO messages(room_id, sender_id, content, created_at) VALUES (?, ?, ?, ?)",
                (room_id, current["id"], clean_content, now),
            )
            conn.execute("UPDATE rooms SET updated_at = ? WHERE id = ?", (now, room_id))
            row = conn.execute(
                """SELECT m.*, u.nickname, u.avatar_color, u.avatar_status
                   FROM messages m JOIN users u ON u.id = m.sender_id
                   WHERE m.id = ?""",
                (cursor.lastrowid,),
            ).fetchone()
        return self._message_payload(row, current["id"])

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
            raise LanChatError("设备令牌无效", 401)
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _nickname(value: str, default: str = "") -> str:
        nickname = " ".join(str(value or "").split()) or default
        if not nickname or len(nickname) > 24:
            raise LanChatError("昵称需要 1-24 个字符")
        return nickname

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, Any]:
        last_seen = float(row["last_seen"])
        return {
            "id": row["id"],
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
            """SELECT u.* FROM users u
               JOIN room_members rm ON rm.user_id = u.id
               WHERE rm.room_id = ? ORDER BY u.nickname COLLATE NOCASE""",
            (room["id"],),
        ).fetchall()
        name = room["name"]
        if room["kind"] == "direct":
            other = next((item for item in members if item["id"] != current_user_id), None)
            name = other["nickname"] if other is not None else "私信"
        latest = conn.execute(
            """SELECT m.content, m.created_at, u.nickname
               FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.room_id = ? ORDER BY m.id DESC LIMIT 1""",
            (room["id"],),
        ).fetchone()
        return {
            "id": room["id"],
            "kind": room["kind"],
            "name": name,
            "memberCount": len(members) if room["kind"] != "public" else self._user_count(conn),
            "members": [self._public_user(item) for item in members],
            "createdBy": room["created_by"],
            "updatedAt": float(room["updated_at"]),
            "latestMessage": (
                {
                    "content": latest["content"],
                    "nickname": latest["nickname"],
                    "createdAt": float(latest["created_at"]),
                }
                if latest is not None
                else None
            ),
            "canLeave": False if room["kind"] == "public" else True,
        }

    @staticmethod
    def _user_count(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    @staticmethod
    def _message_payload(row: sqlite3.Row, current_user_id: str) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "roomId": row["room_id"],
            "senderId": row["sender_id"],
            "senderName": row["nickname"],
            "senderAvatarUrl": f"/api/lan-chat/avatars/{row['sender_id']}",
            "content": row["content"],
            "createdAt": float(row["created_at"]),
            "isMine": row["sender_id"] == current_user_id,
        }

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
