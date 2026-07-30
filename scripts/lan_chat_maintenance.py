#!/usr/bin/env python3
"""Safe snapshot, audit, and repair operations for LAN chat data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


CHAT_FILES = (
    "lan_chat.sqlite",
    "lan_chat_media",
    "lan_chat_files",
    "lan_chat_avatars",
    "lan_chat_group_avatars",
)


@contextmanager
def _connect(path: Path, read_only: bool = False):
    resolved = path.resolve()
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True) if read_only else sqlite3.connect(resolved)
    try:
        yield conn
    finally:
        conn.close()


def _db_path(data_dir: Path) -> Path:
    path = data_dir / "lan_chat.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"邻聊数据库不存在：{path}")
    return path


def _sqlite_backup(source: Path, destination: Path) -> None:
    with _connect(source, read_only=True) as source_conn, _connect(destination) as target_conn:
        source_conn.backup(target_conn)


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.mkdir(parents=True, exist_ok=True)


def _active_references(data_dir: Path, read_only: bool = False) -> tuple[set[str], set[str]]:
    now = time.time()
    with _connect(_db_path(data_dir), read_only=read_only) as conn:
        conn.row_factory = sqlite3.Row
        media = {
            str(row["image_filename"])
            for row in conn.execute(
                """SELECT image_filename FROM messages
                   WHERE image_filename IS NOT NULL AND media_deleted_at IS NULL
                     AND (media_expires_at IS NULL OR media_expires_at > ?)""",
                (now,),
            )
        }
        files = {
            str(row["stored_filename"])
            for row in conn.execute(
                """SELECT stored_filename FROM file_attachments
                   WHERE deleted_at IS NULL AND expires_at > ?""",
                (now,),
            )
        }
    return media, files


def audit(data_dir: Path, read_only: bool = False) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    media_refs, file_refs = _active_references(data_dir, read_only=read_only)
    media_dir = data_dir / "lan_chat_media"
    file_dir = data_dir / "lan_chat_files"
    now = time.time()
    with _connect(_db_path(data_dir), read_only=read_only) as conn:
        conn.row_factory = sqlite3.Row
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [list(row) for row in conn.execute("PRAGMA foreign_key_check")]
        expired_media = int(conn.execute(
            """SELECT COUNT(*) FROM messages WHERE image_filename IS NOT NULL
               AND media_deleted_at IS NULL AND media_expires_at <= ?""", (now,)
        ).fetchone()[0])
        expired_files = int(conn.execute(
            "SELECT COUNT(*) FROM file_attachments WHERE deleted_at IS NULL AND expires_at <= ?", (now,)
        ).fetchone()[0])
        file_sizes = {
            str(row["stored_filename"]): int(row["size_bytes"])
            for row in conn.execute(
                """SELECT stored_filename, size_bytes FROM file_attachments
                   WHERE deleted_at IS NULL AND expires_at > ?""", (now,)
            )
        }
    missing_media = sorted(name for name in media_refs if not (media_dir / name).is_file())
    missing_files = sorted(name for name in file_refs if not (file_dir / name).is_file())
    size_mismatches = sorted(
        name for name, expected in file_sizes.items()
        if (file_dir / name).is_file() and (file_dir / name).stat().st_size != expected
    )
    media_orphans = sorted(
        path.name for path in media_dir.glob("*")
        if path.is_file() and not path.name.endswith(".poster.jpg") and not path.name.startswith(".")
        and path.name not in media_refs
    )
    file_orphans = sorted(
        path.name for path in file_dir.glob("*")
        if path.is_file() and not path.name.startswith(".") and path.name not in file_refs
    )
    return {
        "dataDir": str(data_dir),
        "checkedAt": now,
        "sqliteIntegrity": integrity,
        "foreignKeyErrors": foreign_keys,
        "activeMediaCount": len(media_refs),
        "activeFileCount": len(file_refs),
        "missingMedia": missing_media,
        "missingFiles": missing_files,
        "fileSizeMismatches": size_mismatches,
        "expiredMediaPendingCleanup": expired_media,
        "expiredFilesPendingCleanup": expired_files,
        "orphanMedia": media_orphans,
        "orphanFiles": file_orphans,
        "healthy": not any((missing_media, missing_files, size_mismatches, foreign_keys))
        and integrity == ["ok"],
    }


def _write_report(report: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


def snapshot(source_dir: Path, target_dir: Path, sanitize_auth: bool) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    target_dir = target_dir.resolve()
    _db_path(source_dir)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise ValueError(f"快照目标必须为空：{target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    source_audit = audit(source_dir, read_only=True)
    _sqlite_backup(source_dir / "lan_chat.sqlite", target_dir / "lan_chat.sqlite")
    for name in CHAT_FILES[1:]:
        _copy_tree(source_dir / name, target_dir / name)
    copied_audit = audit(target_dir)
    retried_assets = 0
    for filename, directory_name in (
        *((name, "lan_chat_media") for name in copied_audit["missingMedia"]),
        *((name, "lan_chat_files") for name in copied_audit["missingFiles"]),
    ):
        source_asset = source_dir / directory_name / filename
        target_asset = target_dir / directory_name / filename
        if source_asset.is_file():
            target_asset.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_asset, target_asset)
            retried_assets += 1
    if sanitize_auth:
        with _connect(target_dir / "lan_chat.sqlite") as conn:
            conn.execute("DELETE FROM account_sessions")
            rows = conn.execute("SELECT id FROM users").fetchall()
            for (user_id,) in rows:
                replacement = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                conn.execute("UPDATE users SET device_token_hash = ? WHERE id = ?", (replacement, user_id))
            conn.commit()
    report = audit(target_dir)
    report.update({
        "operation": "snapshot",
        "sourceDir": str(source_dir),
        "authSanitized": sanitize_auth,
        "sourceAudit": source_audit,
        "retriedAssets": retried_assets,
        "copyComplete": not any((report["missingMedia"], report["missingFiles"])),
    })
    return report


def backup(data_dir: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    _sqlite_backup(_db_path(data_dir), backup_dir / "lan_chat.sqlite")
    for name in CHAT_FILES[1:]:
        _copy_tree(data_dir / name, backup_dir / name)


def repair(data_dir: Path, backup_dir: Path, quarantine_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    backup_dir = backup_dir.resolve()
    quarantine_dir = quarantine_dir.resolve()
    before = audit(data_dir)
    backup(data_dir, backup_dir)
    now = time.time()
    media_dir = data_dir / "lan_chat_media"
    file_dir = data_dir / "lan_chat_files"
    with _connect(_db_path(data_dir)) as conn:
        conn.row_factory = sqlite3.Row
        expired_media = conn.execute(
            """SELECT id, image_filename FROM messages WHERE image_filename IS NOT NULL
               AND media_deleted_at IS NULL AND media_expires_at <= ?""", (now,)
        ).fetchall()
        expired_files = conn.execute(
            """SELECT id, stored_filename FROM file_attachments WHERE deleted_at IS NULL
               AND expires_at <= ?""", (now,)
        ).fetchall()
        missing_media = conn.execute(
            """SELECT id, image_filename FROM messages WHERE image_filename IS NOT NULL
               AND media_deleted_at IS NULL AND (media_expires_at IS NULL OR media_expires_at > ?)""", (now,)
        ).fetchall()
        missing_files = conn.execute(
            """SELECT id, stored_filename FROM file_attachments WHERE deleted_at IS NULL
               AND expires_at > ?""", (now,)
        ).fetchall()
        for row in expired_media:
            (media_dir / str(row["image_filename"])).unlink(missing_ok=True)
            (media_dir / f"{Path(str(row['image_filename'])).stem}.poster.jpg").unlink(missing_ok=True)
            conn.execute("UPDATE messages SET media_deleted_at = ? WHERE id = ?", (now, row["id"]))
        for row in expired_files:
            (file_dir / str(row["stored_filename"])).unlink(missing_ok=True)
            conn.execute("UPDATE file_attachments SET deleted_at = ? WHERE id = ?", (now, row["id"]))
        for row in missing_media:
            if not (media_dir / str(row["image_filename"])).is_file():
                conn.execute("UPDATE messages SET media_deleted_at = ? WHERE id = ?", (now, row["id"]))
        for row in missing_files:
            if not (file_dir / str(row["stored_filename"])).is_file():
                conn.execute("UPDATE file_attachments SET deleted_at = ? WHERE id = ?", (now, row["id"]))
        conn.commit()
    media_refs, file_refs = _active_references(data_dir)
    moved = {"media": 0, "files": 0}
    for directory, references, key in ((media_dir, media_refs, "media"), (file_dir, file_refs, "files")):
        target = quarantine_dir / directory.name
        for path in directory.glob("*"):
            if not path.is_file() or path.name.startswith(".") or path.name.endswith(".poster.jpg"):
                continue
            if path.name not in references:
                target.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target / path.name))
                moved[key] += 1
    after = audit(data_dir)
    return {"operation": "repair", "before": before, "after": after, "backupDir": str(backup_dir), "quarantineDir": str(quarantine_dir), "movedOrphans": moved}


def import_snapshot(snapshot_dir: Path, data_dir: Path, backup_dir: Path) -> dict[str, Any]:
    """Atomically replace only LAN chat data; the caller must stop the web service first."""
    snapshot_dir = snapshot_dir.resolve()
    data_dir = data_dir.resolve()
    backup_dir = backup_dir.resolve()
    _db_path(snapshot_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if backup_dir.exists():
        raise ValueError(f"导入备份目录已存在：{backup_dir}")
    if (data_dir / "lan_chat.sqlite").exists():
        backup(data_dir, backup_dir)
    else:
        backup_dir.mkdir(parents=True)
    staging = data_dir / f".lan-chat-import-{uuid.uuid4().hex}"
    previous = backup_dir / "previous-live"
    staging.mkdir()
    try:
        _sqlite_backup(snapshot_dir / "lan_chat.sqlite", staging / "lan_chat.sqlite")
        for name in CHAT_FILES[1:]:
            _copy_tree(snapshot_dir / name, staging / name)
        staged_audit = audit(staging)
        if staged_audit["missingMedia"] or staged_audit["missingFiles"]:
            raise ValueError("快照附件不完整，拒绝导入开发版")
        previous.mkdir()
        for name in CHAT_FILES:
            destination = data_dir / name
            staged = staging / name
            if destination.exists():
                os.replace(destination, previous / name)
            os.replace(staged, destination)
        for suffix in ("-wal", "-shm"):
            (data_dir / f"lan_chat.sqlite{suffix}").unlink(missing_ok=True)
    except Exception:
        if previous.exists():
            for name in CHAT_FILES:
                prior = previous / name
                destination = data_dir / name
                if prior.exists() and not destination.exists():
                    os.replace(prior, destination)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    report = audit(data_dir)
    return {"operation": "import", "snapshotDir": str(snapshot_dir), "dataDir": str(data_dir), "backupDir": str(backup_dir), "after": report}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--source-dir", type=Path, required=True)
    snapshot_parser.add_argument("--target-dir", type=Path, required=True)
    snapshot_parser.add_argument("--sanitize-auth", action="store_true")
    snapshot_parser.add_argument("--report", type=Path)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--data-dir", type=Path, required=True)
    audit_parser.add_argument("--report", type=Path)
    repair_parser = subparsers.add_parser("repair")
    repair_parser.add_argument("--data-dir", type=Path, required=True)
    repair_parser.add_argument("--backup-dir", type=Path, required=True)
    repair_parser.add_argument("--quarantine-dir", type=Path, required=True)
    repair_parser.add_argument("--report", type=Path)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--snapshot-dir", type=Path, required=True)
    import_parser.add_argument("--data-dir", type=Path, required=True)
    import_parser.add_argument("--backup-dir", type=Path, required=True)
    import_parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        report = snapshot(args.source_dir, args.target_dir, args.sanitize_auth)
    elif args.command == "audit":
        report = {"operation": "audit", **audit(args.data_dir)}
    elif args.command == "repair":
        report = repair(args.data_dir, args.backup_dir, args.quarantine_dir)
    else:
        report = import_snapshot(args.snapshot_dir, args.data_dir, args.backup_dir)
    _write_report(report, args.report)


if __name__ == "__main__":
    main()
