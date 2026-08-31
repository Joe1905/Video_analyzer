#!/usr/bin/env python3
"""Safely demote invalid completed hot-report checkpoints for a single report.

Run a dry run first:
  docker-compose -p short-video-analyzer-ui-4004 run --rm analyzer python scripts/repair_hot_report_checkpoint.py --date YYYY-MM-DD

Apply only after an online SQLite backup has been created and verified:
  docker-compose -p short-video-analyzer-ui-4004 run --rm analyzer python scripts/repair_hot_report_checkpoint.py --date YYYY-MM-DD --apply --backup-id <backup-path-or-id> --expected-rows N
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "hot_video_report.sqlite"


def _loads(value: str | None) -> Any:
    try:
        return json.loads(value) if value else None
    except Exception:
        return None


def _invalid_reason(row: sqlite3.Row, root: Path) -> str | None:
    if not _loads(row["raw_json"]) or not isinstance(_loads(row["metrics_json"]), dict):
        return "raw or metrics JSON is missing/invalid"
    filename = str(row["local_filename"] or "").strip()
    if not filename or not (root / "videos" / filename).is_file():
        return "local video file is missing"
    analysis = _loads(row["analysis_json"])
    if not isinstance(analysis, dict) or not analysis:
        return "analysis JSON is missing/invalid"
    if not (root / "output" / str(row["extraction_dir"] or filename) / "analysis.json").is_file():
        return "analysis artifact is missing"
    insight = _loads(row["insight_json"])
    if not isinstance(insight, dict) or not insight or insight.get("error"):
        return "insight is missing/failed"
    insight_text = json.dumps(insight, ensure_ascii=False).lower()
    if "generated failed" in insight_text or "生成失败" in insight_text:
        return "insight contains a failure placeholder"
    return None


def _checkpoint_update_sql(conn: sqlite3.Connection) -> tuple[str, set[str]]:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(hot_report_videos)").fetchall()}
    required = {"process_status", "process_error", "updated_at"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(f"hot_report_videos is missing required columns: {', '.join(missing)}")
    assignments = ["process_status='pending'", "process_error=?", "updated_at=?"]
    if "process_step" in columns:
        assignments.insert(1, "process_step='pending'")
    if "last_error_at" in columns:
        assignments.insert(-1, "last_error_at=?")
    return ", ".join(assignments), columns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--date", help="exact report date, YYYY-MM-DD")
    selector.add_argument("--report-id", help="exact daily_reports.id")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="perform the transactional update")
    parser.add_argument("--backup-id", help="verified online-backup path or identifier; required with --apply")
    parser.add_argument("--expected-rows", type=int, help="required exact invalid-row count with --apply")
    args = parser.parse_args()
    if args.apply and (not args.backup_id or args.expected_rows is None):
        parser.error("--apply requires --backup-id and --expected-rows")
    if not args.db.is_file():
        parser.error(f"database does not exist: {args.db}")

    root = args.db.resolve().parent.parent
    conn = sqlite3.connect(args.db, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"database integrity check failed: {integrity}")
        update_assignments, _ = _checkpoint_update_sql(conn)
        where, value = ("dr.report_date = ?", args.date) if args.date else ("dr.id = ?", args.report_id)
        rows = conn.execute(
            f"""
            SELECT rv.*, dr.id AS daily_report_id
            FROM hot_report_videos rv
            JOIN daily_reports dr ON dr.id = rv.report_id
            WHERE {where} AND rv.process_status = 'complete'
            ORDER BY rv.report_rank ASC
            """,
            (value,),
        ).fetchall()
        invalid = [(row, _invalid_reason(row, root)) for row in rows]
        invalid = [(row, reason) for row, reason in invalid if reason]
        print(json.dumps({
            "mode": "apply" if args.apply else "dry-run",
            "selector": {"date": args.date, "report_id": args.report_id},
            "complete_rows": len(rows),
            "invalid_rows": len(invalid),
            "invalid": [{"platform": row["platform"], "video_id": row["video_id"], "reason": reason} for row, reason in invalid],
        }, ensure_ascii=False, indent=2))
        if not args.apply:
            return 0
        if len(invalid) != args.expected_rows:
            raise RuntimeError(f"expected {args.expected_rows} invalid rows, found {len(invalid)}")
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        for row, reason in invalid:
            params: list[Any] = [f"checkpoint repair: {reason}"]
            if "last_error_at=?" in update_assignments:
                params.append(now)
            params.append(now)
            params.extend([row["report_id"], row["platform"], row["video_id"]])
            conn.execute(
                f"""UPDATE hot_report_videos SET {update_assignments}
                WHERE report_id=? AND platform=? AND video_id=? AND process_status='complete'""",
                params,
            )
        conn.commit()
        print(json.dumps({"applied": len(invalid), "backup_id": args.backup_id}, ensure_ascii=False))
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
