#!/usr/bin/env python3
"""Regenerate one completed hot report's final summary without re-downloading videos.

Run a dry-run first. Applying requires an exact completed-checkpoint count and
an empty backup destination; the script creates and verifies an online SQLite
backup before any DeepSeek request or report update.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import hot_video_report as report


def _backup_database(source_db: Path, backup_db: Path) -> None:
    if backup_db.exists():
        raise RuntimeError(f"refusing to overwrite existing backup: {backup_db}")
    backup_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_db, timeout=3) as source, sqlite3.connect(backup_db, timeout=3) as backup:
        source.backup(backup)
        integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="exact report date, YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true", help="regenerate and persist the final summary")
    parser.add_argument("--expected-success", type=int, help="required with --apply")
    parser.add_argument("--backup-db", type=Path, help="new verified backup path; required with --apply")
    args = parser.parse_args()
    if args.apply and (args.expected_success is None or not args.backup_db):
        parser.error("--apply requires --expected-success and --backup-db")

    with sqlite3.connect(report.DB_PATH, timeout=3) as conn:
        row = conn.execute(
            "SELECT id, status FROM daily_reports WHERE report_date = ?", (args.date,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"report not found for {args.date}")
        completed = conn.execute(
            "SELECT count(*) FROM hot_report_videos WHERE report_date = ? AND process_status = 'complete'",
            (args.date,),
        ).fetchone()[0]
    print({"mode": "apply" if args.apply else "dry-run", "date": args.date, "status": row[1], "completed": completed})
    if not args.apply:
        return 0
    if completed != args.expected_success:
        raise RuntimeError(f"expected {args.expected_success} completed checkpoints, found {completed}")
    _backup_database(report.DB_PATH, args.backup_db)
    result = report.regenerate_daily_report_summary(args.date)
    print({
        "date": args.date,
        "status": result.get("status"),
        "video_count": result.get("video_count"),
        "backup_db": str(args.backup_db),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
