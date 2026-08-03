"""Session management and persistence for AI chat system."""
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
DATA_DIR = ROOT / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"


@dataclass
class Message:
    id: str
    role: str  # "user" | "assistant" | "tool"
    content: str
    attachments: list[dict] | None = None
    tool_calls: list[dict] | None = None
    tool_results: list[dict] | None = None
    official_preset: dict[str, str] | None = None
    status: str = "done"  # "pending" | "done" | "error"
    request: dict | None = None
    raw: dict | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    title: str = ""
    title_is_custom: bool = False
    created_at: str = ""
    updated_at: str = ""
    messages: list[Message] = field(default_factory=list)

    def __post_init__(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


class ChatStore:
    def __init__(self, sessions_file: Path | None = None):
        self.sessions_file = sessions_file or SESSIONS_FILE
        self.sessions: dict[str, Session] = {}
        self.sse_clients: dict[str, set] = {}
        self._save_timer: threading.Timer | None = None
        self._save_timer_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._lock = threading.Lock()

    def create_session(self, session_id: str) -> Session:
        with self._lock:
            session = Session(id=session_id)
            self.sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            return self.sessions.get(session_id)

    def get_or_create(self, session_id: str) -> Session:
        session = self.get_session(session_id)
        if session is None:
            session = self.create_session(session_id)
        return session

    def add_message(self, session: Session, message: Message) -> None:
        with self._lock:
            session.messages.append(message)
            session.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._schedule_save()

    def update_message(self, session: Session, msg: Message, content: str, status: str = "done") -> None:
        with self._lock:
            msg.content = content
            msg.status = status
            session.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._schedule_save()

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                self._schedule_save()
                return True
            return False

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return sorted(
                [{
                    "id": s.id, "title": s.title or self._auto_title(s),
                    "title_is_custom": getattr(s, "title_is_custom", False),
                    "created_at": s.created_at, "updated_at": s.updated_at,
                    "message_count": len(s.messages),
                } for s in self.sessions.values()],
                key=lambda x: x["updated_at"], reverse=True,
            )

    @staticmethod
    def _auto_title(session) -> str:
        for msg in session.messages:
            if msg.role == "user" and msg.content:
                text = msg.content
                import re
                m = re.search(r"Skill\s*[\u300c\u300e\"'](.+?)[\u300d\u300f\"']", text)
                if m:
                    skill_name = m.group(1).strip()
                    target_m = re.search(r"\u76ee\u6807\s*[\uff1a:]\s*(.+)", text)
                    if target_m and target_m.group(1).strip():
                        target_str = target_m.group(1).strip().splitlines()[0][:20]
                        return f"{skill_name} \u00b7 {target_str}"
                    return skill_name
                return text[:40] + ("..." if len(text) > 40 else "")
            if msg.role == "user" and msg.attachments:
                name = str((msg.attachments[0] or {}).get("name") or "Image")
                return name[:40] + ("..." if len(name) > 40 else "")
        return "新对话"

    def _schedule_save(self):
        with self._save_timer_lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.1, self._do_save)
            self._save_timer.start()

    def _do_save(self):
        try:
            save_sessions_to_disk(self)
        except Exception as e:
            print(f"Chat session save failed: {e}", flush=True)

    def register_sse(self, session_id: str, client):
        if session_id not in self.sse_clients:
            self.sse_clients[session_id] = set()
        self.sse_clients[session_id].add(client)

    def unregister_sse(self, session_id: str, client):
        clients = self.sse_clients.get(session_id)
        if clients:
            clients.discard(client)

    def broadcast(self, session_id: str, event: str, data: Any):
        clients = self.sse_clients.get(session_id, set())
        dead = []
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        for client in list(clients):
            try:
                client.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
                client.wfile.flush()
            except Exception:
                dead.append(client)
        for c in dead:
            clients.discard(c)


def save_sessions_to_disk(store: ChatStore):
    sessions_file = getattr(store, "sessions_file", SESSIONS_FILE)
    sessions_file.parent.mkdir(parents=True, exist_ok=True)
    with store._save_lock:
        with store._lock:
            serialized = []
            for s in store.sessions.values():
                serialized.append({
                    "id": s.id, "title": s.title or ChatStore._auto_title(s),
                    "title_is_custom": getattr(s, "title_is_custom", False),
                    "created_at": s.created_at, "updated_at": s.updated_at,
                    "messages": [{
                        "id": m.id, "role": m.role, "content": m.content,
                        "attachments": m.attachments,
                        "tool_calls": m.tool_calls, "tool_results": m.tool_results,
                        "official_preset": m.official_preset,
                        "status": m.status, "created_at": m.created_at,
                    } for m in s.messages],
                })

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=sessions_file.parent,
                prefix=f".{sessions_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                temp_path = Path(f.name)
                json.dump(serialized, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, sessions_file)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def load_sessions_from_disk(store: ChatStore):
    sessions_file = getattr(store, "sessions_file", SESSIONS_FILE)
    if not sessions_file.is_file():
        return
    try:
        with open(sessions_file, encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, list):
            return
        for item in saved:
            sid = str(item.get("id", ""))
            if not sid or not item.get("messages"):
                continue
            session = Session(id=sid, title=str(item.get("title", "")),
                              title_is_custom=bool(item.get("title_is_custom", False)),
                              created_at=item.get("created_at", ""),
                              updated_at=item.get("updated_at", ""))
            for m in item.get("messages", []):
                session.messages.append(Message(
                    id=m.get("id", ""), role=m.get("role", "user"),
                    content=m.get("content", ""),
                    attachments=m.get("attachments"),
                    tool_calls=m.get("tool_calls"), tool_results=m.get("tool_results"),
                    official_preset=m.get("official_preset"),
                    status=m.get("status", "done"),
                    created_at=m.get("created_at", time.time()),
                ))
            store.sessions[sid] = session
        print(f"Loaded {len(store.sessions)} chat sessions from disk.")
    except Exception as e:
        print(f"Could not load chat sessions: {e}")

