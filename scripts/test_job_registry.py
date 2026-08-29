#!/usr/bin/env python3
"""Pure contracts for the zero-runtime-switch JobRegistry."""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from pathlib import Path
import sys
import threading
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from jobs.registry import JobRegistry  # noqa: E402


@dataclass
class FixtureJob:
    status: str = "queued"
    updated_at: float = 1.0
    log: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=lambda: {"nested": {"value": 1}})


class CopyBomb(FixtureJob):
    copy_count = 0

    def __deepcopy__(self, memo: dict[int, Any]) -> "CopyBomb":
        type(self).copy_count += 1
        if type(self).copy_count > 1:
            raise RuntimeError("copy bomb")
        clone = type(self)(
            status=self.status,
            updated_at=self.updated_at,
            log=copy.deepcopy(self.log, memo),
            extra=copy.deepcopy(self.extra, memo),
        )
        memo[id(self)] = clone
        return clone


class OverlapCopyJob(FixtureJob):
    def __init__(self, entered: threading.Event, release: threading.Event, *, block: bool = False) -> None:
        super().__init__()
        self.entered = entered
        self.release = release
        self.block = block

    def __deepcopy__(self, memo: dict[int, Any]) -> "OverlapCopyJob":
        if self.block:
            self.entered.set()
            assert self.release.wait(timeout=3)
        clone = type(self)(self.entered, self.release, block=True)
        clone.status = self.status
        clone.updated_at = self.updated_at
        clone.log = copy.deepcopy(self.log, memo)
        clone.extra = copy.deepcopy(self.extra, memo)
        memo[id(self)] = clone
        return clone


def assert_module_structure() -> None:
    source_path = SCRIPTS_DIR / "jobs" / "registry.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {"__future__", "copy", "typing"}
    direct_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert direct_imports == {"threading", "time"}
    assert "class JobRegistry:" in source
    assert "Protocol" not in source
    assert "Generic" not in source
    assert "TypeVar" not in source
    job_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "job"
    }
    assert job_attributes <= {"log", "status", "updated_at"}
    forbidden = (
        "web_app",
        "routes",
        "services",
        "snapshot_",
        "adapter",
        "Path",
        ".open(",
        "read_",
        "write_",
        "subprocess",
        ".start(",
        "sqlite",
        "HTTP",
        "SSE",
        "result",
        "artifact",
        "output_dir",
    )
    assert not any(value in source for value in forbidden)


def assert_register_duplicate_missing_and_isolation() -> None:
    registry = JobRegistry(clock=lambda: 10.0)
    original = FixtureJob(extra={"nested": {"value": 1}})
    registry.register("one", original)
    original.log.append("input-only")
    original.extra["nested"]["value"] = 2

    first = registry.snapshot("one")
    assert first is not None
    assert first.log == []
    assert first.extra == {"nested": {"value": 1}}
    first.log.append("snapshot-only")
    first.extra["nested"]["value"] = 3
    second = registry.snapshot("one")
    assert second is not None
    assert second.log == []
    assert second.extra == {"nested": {"value": 1}}

    try:
        registry.register("one", FixtureJob(status="replacement"))
    except ValueError as exc:
        assert str(exc) == "job already registered: one"
    else:
        raise AssertionError("duplicate registration must fail")
    assert registry.status("one") == "queued"
    assert registry.snapshot("missing") is None
    assert registry.status("missing") is None
    try:
        registry.append_log("missing", "nope")
    except KeyError as exc:
        assert exc.args == ("missing",)
    else:
        raise AssertionError("missing append must raise KeyError")


def assert_log_order_and_clock() -> None:
    times = iter((11.0, 12.0))
    registry = JobRegistry(clock=lambda: next(times))
    registry.register("one", FixtureJob())
    registry.append_log("one", "first  \n\t")
    registry.append_log("one", "second\r\n")
    snapshot = registry.snapshot("one")
    assert snapshot is not None
    assert snapshot.log == ["first", "second"]
    assert snapshot.updated_at == 12.0


def assert_concurrent_append_and_snapshot_isolation() -> None:
    registry = JobRegistry(clock=lambda: 20.0)
    registry.register("one", FixtureJob(extra={"nested": {"value": 1}}))
    workers = 8
    entries_per_worker = 40
    start = threading.Barrier(workers)
    threads: list[threading.Thread] = []

    def append_entries(worker: int) -> None:
        start.wait()
        for entry in range(entries_per_worker):
            registry.append_log("one", f"{worker}:{entry}")

    for worker in range(workers):
        thread = threading.Thread(target=append_entries, args=(worker,))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    snapshot_start = threading.Barrier(workers)
    snapshots: list[FixtureJob] = []
    snapshots_lock = threading.Lock()
    snapshot_threads: list[threading.Thread] = []

    def take_snapshot(worker: int) -> None:
        snapshot_start.wait()
        snapshot = registry.snapshot("one")
        assert snapshot is not None
        snapshot.extra["nested"]["value"] = worker
        with snapshots_lock:
            snapshots.append(snapshot)

    for worker in range(workers):
        thread = threading.Thread(target=take_snapshot, args=(worker,))
        snapshot_threads.append(thread)
        thread.start()
    for thread in snapshot_threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    final = registry.snapshot("one")
    assert final is not None
    expected = {f"{worker}:{entry}" for worker in range(workers) for entry in range(entries_per_worker)}
    assert len(final.log) == len(expected)
    assert set(final.log) == expected
    assert final.extra == {"nested": {"value": 1}}
    assert {snapshot.extra["nested"]["value"] for snapshot in snapshots} == set(range(workers))


def assert_snapshot_blocks_append_until_release() -> None:
    entered = threading.Event()
    release = threading.Event()
    append_started = threading.Event()
    append_crossed = threading.Event()
    registry = JobRegistry(clock=lambda: (append_crossed.set() or 40.0))
    registry.register("overlap", OverlapCopyJob(entered, release))
    snapshots: list[OverlapCopyJob] = []

    def take_snapshot() -> None:
        snapshot = registry.snapshot("overlap")
        assert snapshot is not None
        snapshots.append(snapshot)

    def append_while_snapshot_holds_lock() -> None:
        append_started.set()
        registry.append_log("overlap", "after snapshot")

    snapshot_thread = threading.Thread(target=take_snapshot)
    snapshot_thread.start()
    assert entered.wait(timeout=3)
    append_thread = threading.Thread(target=append_while_snapshot_holds_lock)
    append_thread.start()
    assert append_started.wait(timeout=3)
    assert not append_crossed.wait(timeout=0.1)
    release.set()
    snapshot_thread.join(timeout=3)
    append_thread.join(timeout=3)
    assert not snapshot_thread.is_alive()
    assert not append_thread.is_alive()
    assert snapshots[0].log == []
    later = registry.snapshot("overlap")
    assert later is not None
    assert later.log == ["after snapshot"]


def assert_deepcopy_failure_releases_lock() -> None:
    CopyBomb.copy_count = 0
    registry = JobRegistry(clock=lambda: 30.0)
    registry.register("bomb", CopyBomb())
    try:
        registry.snapshot("bomb")
    except RuntimeError as exc:
        assert str(exc) == "copy bomb"
    else:
        raise AssertionError("snapshot copy must fail")

    completed = threading.Event()

    def append_after_failure() -> None:
        registry.append_log("bomb", "still unlocked")
        completed.set()

    thread = threading.Thread(target=append_after_failure)
    thread.start()
    thread.join(timeout=3)
    assert completed.is_set()
    assert not thread.is_alive()
    assert registry.status("bomb") == "queued"


def main() -> int:
    assert_module_structure()
    assert_register_duplicate_missing_and_isolation()
    assert_log_order_and_clock()
    assert_concurrent_append_and_snapshot_isolation()
    assert_snapshot_blocks_append_until_release()
    assert_deepcopy_failure_releases_lock()
    print("job registry tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
