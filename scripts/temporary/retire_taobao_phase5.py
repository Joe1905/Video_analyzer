#!/usr/bin/env python3
"""One-time, backup-first retirement of Taobao-only proxy database state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


TAOBAO_TABLES = ("taobao_sessions", "taobao_proxy_bindings", "taobao_profiles")
REQUIRED_TABLES = ("proxy_profiles", "tiktok_accounts", "browser_sessions", "publish_jobs", "collect_jobs", *TAOBAO_TABLES)
ACTIVE_SESSION_STATUSES = ("starting", "running", "observing")


class RetirementRefused(RuntimeError):
    pass


def _json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _open_database(data_dir: Path) -> tuple[Path, sqlite3.Connection]:
    directory = data_dir.expanduser().resolve()
    if not directory.is_dir():
        raise RetirementRefused("data directory does not exist")
    database = directory / "proxy_pool.sqlite"
    if not database.is_file():
        raise RetirementRefused("proxy_pool.sqlite does not exist in the supplied data directory")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise RetirementRefused("foreign key enforcement is unavailable")
    return database, connection


def _require_schema(connection: sqlite3.Connection) -> None:
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [name for name in REQUIRED_TABLES if name not in names]
    if missing:
        raise RetirementRefused("database does not contain the expected retirement schema")


def _foreign_keys_valid(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RetirementRefused("foreign key check failed")


def _count(connection: sqlite3.Connection, query: str, params: tuple = ()) -> int:
    return int(connection.execute(query, params).fetchone()[0])


def _preflight(connection: sqlite3.Connection) -> dict[str, int]:
    _require_schema(connection)
    _foreign_keys_valid(connection)
    placeholders = ", ".join("?" for _ in ACTIVE_SESSION_STATUSES)
    active_sessions = _count(
        connection,
        f"SELECT COUNT(*) FROM taobao_sessions WHERE status IN ({placeholders})",
        ACTIVE_SESSION_STATUSES,
    )
    if active_sessions:
        raise RetirementRefused("active Taobao sessions remain")
    associations = {
        "linked_tiktok_accounts": _count(connection, "SELECT COUNT(*) FROM tiktok_accounts a JOIN proxy_profiles p ON p.id = a.proxy_profile_id WHERE p.port_scope = 'taobao'"),
        "linked_browser_sessions": _count(connection, "SELECT COUNT(*) FROM browser_sessions s JOIN proxy_profiles p ON p.id = s.proxy_profile_id WHERE p.port_scope = 'taobao'"),
        "linked_publish_jobs": _count(connection, "SELECT COUNT(*) FROM publish_jobs j JOIN proxy_profiles p ON p.id = j.proxy_profile_id WHERE p.port_scope = 'taobao'"),
        "linked_collect_jobs": _count(connection, "SELECT COUNT(*) FROM collect_jobs j JOIN proxy_profiles p ON p.id = j.proxy_profile_id WHERE p.port_scope = 'taobao'"),
    }
    if any(associations.values()):
        raise RetirementRefused("Taobao-scope proxy rows are still referenced by active domains")
    return {
        "taobao_sessions": _count(connection, "SELECT COUNT(*) FROM taobao_sessions"),
        "taobao_proxy_bindings": _count(connection, "SELECT COUNT(*) FROM taobao_proxy_bindings"),
        "taobao_profiles": _count(connection, "SELECT COUNT(*) FROM taobao_profiles"),
        "taobao_scope_proxy_profiles": _count(connection, "SELECT COUNT(*) FROM proxy_profiles WHERE port_scope = 'taobao'"),
        **associations,
    }


def _backup(database: Path, source: sqlite3.Connection, counts: dict[str, int]) -> Path:
    backup_dir = database.parent / "taobao-retirement-backups" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    )
    backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    backup_path = backup_dir / "proxy_pool.sqlite"
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
    backup_path.chmod(0o600)
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RetirementRefused("backup was not created")
    check = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        check.execute("PRAGMA foreign_keys = ON")
        if check.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RetirementRefused("backup foreign key enforcement is unavailable")
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RetirementRefused("backup integrity check failed")
        _require_schema(check)
        _foreign_keys_valid(check)
        for name, expected in counts.items():
            if name.startswith("linked_"):
                continue
            query = "SELECT COUNT(*) FROM proxy_profiles WHERE port_scope = 'taobao'" if name == "taobao_scope_proxy_profiles" else f"SELECT COUNT(*) FROM {name}"
            if _count(check, query) != expected:
                raise RetirementRefused("backup verification failed")
    finally:
        check.close()
    return backup_path


def retire(data_dir: Path, *, apply: bool) -> dict:
    database, connection = _open_database(data_dir)
    try:
        counts = _preflight(connection)
        if not apply:
            return {"ok": True, "mode": "dry-run", "database": str(database), "would_remove": counts}
        backup_path = _backup(database, connection, counts)
        connection.execute("BEGIN IMMEDIATE")
        try:
            removed = _preflight(connection)
            connection.execute("DROP TABLE taobao_sessions")
            connection.execute("DROP TABLE taobao_proxy_bindings")
            connection.execute("DROP TABLE taobao_profiles")
            connection.execute("DELETE FROM proxy_profiles WHERE port_scope = 'taobao'")
            _foreign_keys_valid(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {"ok": True, "mode": "applied", "backup": str(backup_path), "removed": removed}
    finally:
        connection.close()


def _self_test_schema(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE proxy_profiles (id INTEGER PRIMARY KEY, port_scope TEXT NOT NULL);
            CREATE TABLE tiktok_accounts (proxy_profile_id INTEGER REFERENCES proxy_profiles(id));
            CREATE TABLE browser_sessions (proxy_profile_id INTEGER REFERENCES proxy_profiles(id));
            CREATE TABLE publish_jobs (proxy_profile_id INTEGER REFERENCES proxy_profiles(id));
            CREATE TABLE collect_jobs (proxy_profile_id INTEGER REFERENCES proxy_profiles(id));
            CREATE TABLE taobao_profiles (owner_id TEXT PRIMARY KEY, proxy_profile_id INTEGER REFERENCES proxy_profiles(id));
            CREATE TABLE taobao_proxy_bindings (owner_id TEXT REFERENCES taobao_profiles(owner_id));
            CREATE TABLE taobao_sessions (status TEXT NOT NULL, owner_id TEXT REFERENCES taobao_profiles(owner_id));
            """
        )
        connection.execute("INSERT INTO proxy_profiles VALUES (1, 'default')")
        connection.execute("INSERT INTO proxy_profiles VALUES (2, 'taobao')")
        connection.execute("INSERT INTO taobao_profiles VALUES ('fixture', 1)")
        connection.execute("INSERT INTO taobao_proxy_bindings VALUES ('fixture')")
        connection.execute("INSERT INTO taobao_sessions VALUES ('stopped', 'fixture')")
        connection.commit()
    finally:
        connection.close()


def _expect_refusal(data_dir: Path) -> None:
    try:
        retire(data_dir, apply=False)
    except RetirementRefused:
        return
    raise AssertionError("unsafe fixture was not refused")


def self_test() -> dict:
    with tempfile.TemporaryDirectory(prefix="retire-taobao-phase5-") as root:
        data_dir = Path(root) / "data"
        data_dir.mkdir()
        database = data_dir / "proxy_pool.sqlite"
        _self_test_schema(database)
        dry_run = retire(data_dir, apply=False)
        assert dry_run["mode"] == "dry-run"
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE taobao_sessions SET status = 'running'")
            connection.commit()
        finally:
            connection.close()
        _expect_refusal(data_dir)
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE taobao_sessions SET status = 'stopped'")
            connection.execute("INSERT INTO tiktok_accounts VALUES (2)")
            connection.commit()
        finally:
            connection.close()
        _expect_refusal(data_dir)
        connection = sqlite3.connect(database)
        try:
            connection.execute("DELETE FROM tiktok_accounts")
            connection.commit()
        finally:
            connection.close()
        applied = retire(data_dir, apply=True)
        backup = Path(applied["backup"])
        assert backup.is_file() and backup.stat().st_size > 0
        check = sqlite3.connect(database)
        try:
            names = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert not set(TAOBAO_TABLES) & names
            assert _count(check, "SELECT COUNT(*) FROM proxy_profiles WHERE id = 1 AND port_scope = 'default'") == 1
            assert _count(check, "SELECT COUNT(*) FROM proxy_profiles WHERE port_scope = 'taobao'") == 0
            assert check.execute("PRAGMA foreign_key_check").fetchone() is None
        finally:
            check.close()
        backup_check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            names = {row[0] for row in backup_check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert set(TAOBAO_TABLES) <= names
            assert _count(backup_check, "SELECT COUNT(*) FROM taobao_sessions") == 1
        finally:
            backup_check.close()
    return {"ok": True, "mode": "self-test"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Retire Taobao-only rows from one explicit proxy_pool.sqlite data directory.")
    parser.add_argument("data_dir", nargs="?", type=Path, help="directory containing proxy_pool.sqlite")
    parser.add_argument("--apply", action="store_true", help="create a verified backup and perform the retirement")
    parser.add_argument("--self-test", action="store_true", help="run the isolated retirement fixture")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            if args.apply or args.data_dir is not None:
                raise RetirementRefused("--self-test cannot be combined with a data directory or --apply")
            _json(self_test())
            return 0
        if args.data_dir is None:
            raise RetirementRefused("an explicit data directory is required")
        _json(retire(args.data_dir, apply=args.apply))
        return 0
    except (RetirementRefused, sqlite3.Error, OSError) as exc:
        _json({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
