"""Video analysis queue and status persistence."""
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path.cwd()
STATUS_FILE = ROOT / "data" / "video_status.json"

ALLOWED_STATUSES = {
    "idle",
    "queued_analyze",
    "analyzing",
    "analyzed",
    "queued_report",
    "reporting",
    "complete",
}

STATUS_META: dict[str, dict[str, str]] = {
    "idle":           {"label": "未分析", "color": "#dc2626", "bg": "#fef2f2"},
    "queued_analyze": {"label": "待分析", "color": "#7c3aed", "bg": "#f5f3ff"},
    "analyzing":      {"label": "解析中", "color": "#7c3aed", "bg": "#f5f3ff"},
    "analyzed":       {"label": "未报告", "color": "#2563eb", "bg": "#eff6ff"},
    "queued_report":  {"label": "待报告", "color": "#d97706", "bg": "#fffbeb"},
    "reporting":      {"label": "报告中", "color": "#d97706", "bg": "#fffbeb"},
    "complete":       {"label": "已完成", "color": "#059669", "bg": "#ecfdf5"},
}

STEP_LABELS: dict[str, str] = {
    "queued": "已加入队列",
    "starting": "开始执行",
    "extracting": "正在解析视频",
    "translating": "正在翻译提取结果",
    "titling": "正在生成短标题",
    "auditing": "正在生成分析报告",
    "translating_audit": "正在翻译报告",
    "completed": "任务完成",
    "failed": "任务失败",
}


@dataclass
class QueueItem:
    filename: str
    job_type: str  # "analyze" | "report"
    added_at: float = field(default_factory=time.time)


class VideoQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[QueueItem] = []
        self._running = False
        self._current: QueueItem | None = None
        self._progress: dict[str, Any] = {"current": None, "step": "", "percent": 0}
        self._status: dict[str, str] = {}
        self._titles: dict[str, str] = {}
        self._executor: Callable | None = None
        self._sse_clients: set = set()
        self._load_status()

    def _load_status(self) -> None:
        try:
            if STATUS_FILE.is_file():
                with open(STATUS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                self._status = data.get("status", {})
                self._titles = data.get("titles", {})
        except Exception:
            pass

    def _save_status(self) -> None:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump({"status": self._status, "titles": self._titles}, f, ensure_ascii=False, indent=2)

    def get_status(self, filename: str) -> str:
        return self._status.get(filename, "idle")

    def set_status(self, filename: str, status: str) -> None:
        if status in ALLOWED_STATUSES:
            self._status[filename] = status
            self._save_status()

    def set_progress(
        self,
        filename: str | None,
        step: str,
        percent: int = 0,
        job_type: str = "",
        message: str = "",
    ) -> None:
        label = STEP_LABELS.get(step, step)
        current = filename or None
        self._progress = {
            "current": current,
            "step": step,
            "label": label,
            "percent": max(0, min(int(percent), 100)),
            "job_type": job_type,
            "message": message or (f"{current}: {label}" if current else label),
            "updated_at": time.time(),
        }
        self.broadcast()

    def broadcast(self) -> None:
        """Broadcast status change to all SSE clients (call WITHOUT lock held)."""
        data = json.dumps({"type": "status_update", "queue": self.get_queue_state(), "progress": self.get_progress()}, ensure_ascii=False)
        dead = []
        clients = list(self._sse_clients)
        for client in clients:
            try:
                client.wfile.write(f"data: {data}\n\n".encode())
                client.wfile.flush()
            except Exception:
                dead.append(client)
        for c in dead:
            self._sse_clients.discard(c)

    def register_sse(self, client) -> None:
        self._sse_clients.add(client)

    def unregister_sse(self, client) -> None:
        self._sse_clients.discard(client)

    def get_status_meta(self, filename: str) -> dict[str, str]:
        return STATUS_META.get(self.get_status(filename), STATUS_META["idle"])

    def get_title(self, filename: str) -> str:
        return self._titles.get(filename, filename)

    def set_title(self, filename: str, title: str) -> None:
        if title:
            t = str(title).strip()[:80]
            if t:
                self._titles[filename] = t
                self._save_status()

    def all_statuses(self) -> dict[str, str]:
        return dict(self._status)

    def get_progress(self) -> dict[str, Any]:
        return dict(self._progress)

    def get_queue_state(self) -> list[dict[str, Any]]:
        with self._lock:
            result = [{"filename": q.filename, "job_type": q.job_type} for q in self._queue]
            if self._current:
                result.insert(0, {"filename": self._current.filename, "job_type": self._current.job_type, "active": True})
            return result

    def enqueue(self, filename: str, job_type: str) -> None:
        with self._lock:
            self._queue = [q for q in self._queue if not (q.filename == filename and q.job_type == job_type)]
            self._queue.append(QueueItem(filename=filename, job_type=job_type))
            if job_type == "analyze":
                self.set_status(filename, "queued_analyze")
            else:
                self.set_status(filename, "queued_report")
            self._progress = {
                "current": filename,
                "step": "queued",
                "label": STEP_LABELS["queued"],
                "percent": 0,
                "job_type": job_type,
                "message": f"{filename}: 已加入{'解析' if job_type == 'analyze' else '报告'}队列",
                "updated_at": time.time(),
            }
        self.broadcast()

    def start(self, executor: Callable) -> None:
        self._executor = executor
        self._running = True
        thread = threading.Thread(target=self._worker, daemon=True)
        thread.start()

    def stop(self) -> None:
        self._running = False

    def _worker(self) -> None:
        while self._running:
            item = None
            with self._lock:
                if self._queue:
                    item = self._queue.pop(0)
                    self._current = item
            if item is None:
                time.sleep(1)
                continue
            if item.job_type == "analyze":
                self.set_status(item.filename, "analyzing")
            else:
                self.set_status(item.filename, "reporting")
            self.set_progress(item.filename, "starting", 5, item.job_type)
            try:
                if self._executor:
                    self._executor(item.filename, item.job_type, self._progress)
            except Exception as e:
                print(f"Queue job failed: {item.filename} {item.job_type}: {e}", flush=True)
                # Reset status on failure so it can be retried
                self.set_status(item.filename, "idle")
                self.set_progress(item.filename, "failed", 100, item.job_type, f"{item.filename}: {e}")
            finally:
                with self._lock:
                    self._current = None
                self._progress = {"current": None, "step": "", "label": "", "percent": 0, "job_type": "", "message": "", "updated_at": time.time()}
                self.broadcast()


# Singleton
video_queue = VideoQueue()
