"""Small, dependency-free primitives for safely storing JSON files."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterator


_path_locks: dict[str, tuple[threading.Lock, int]] = {}
_path_locks_guard = threading.Lock()


def _normalized_path(path: str | os.PathLike[str]) -> Path:
    """Return a stable absolute path for the same-process lock registry."""
    absolute = os.path.abspath(os.fspath(path))
    return Path(os.path.normcase(os.path.realpath(absolute)))


@contextmanager
def _path_write_lock(path: Path) -> Iterator[None]:
    key = os.fspath(path)
    with _path_locks_guard:
        lock, users = _path_locks.get(key, (threading.Lock(), 0))
        _path_locks[key] = (lock, users + 1)

    acquired = False
    try:
        lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        with _path_locks_guard:
            current_lock, current_users = _path_locks[key]
            if current_lock is not lock:
                raise RuntimeError("JSON path lock registry was corrupted")
            if current_users == 1:
                del _path_locks[key]
            else:
                _path_locks[key] = (lock, current_users - 1)


def read_json(path: str | os.PathLike[str]) -> Any | None:
    """Read UTF-8 JSON, returning ``None`` when the file does not exist."""
    target = _normalized_path(path)
    if not target.is_file():
        return None
    with target.open("r", encoding="utf-8") as file:
        return json.load(file)


def atomic_write_json(path: str | os.PathLike[str], payload: Any) -> None:
    """Atomically replace *path* with formatted UTF-8 JSON within this process."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    target = _normalized_path(path)

    with _path_write_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, raw_temp_path = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temp_path = Path(raw_temp_path)
            try:
                file = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = None
                raise
            descriptor = None
            with file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, target)
            temp_path = None
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
