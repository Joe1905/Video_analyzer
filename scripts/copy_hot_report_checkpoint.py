#!/usr/bin/env python3
"""Copy one interrupted hot-report checkpoint into an isolated development workspace.

The source database and artifacts are read-only. Run a dry-run first, then apply
only to an empty target date:

  python scripts/copy_hot_report_checkpoint.py --date YYYY-MM-DD \
    --source-db /source-data/hot_video_report.sqlite \
    --source-videos /source-videos --source-output /source-output

  python scripts/copy_hot_report_checkpoint.py ... --apply \
    --expected-videos 10 --expected-complete 5
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hot_video_report import DB_PATH, OUTPUT_DIR, REPORT_COVER_DIR, VIDEOS_DIR, initialize_hot_report_db
from video_registry import register_video


ROOT = Path(__file__).resolve().parent.parent
TARGET_DB = ROOT / "data" / "hot_video_report.sqlite"


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _safe_name(value: Any, label: str) -> str:
    text = str(value or "").strip()
    name = Path(text).name
    if not name or name != text:
        raise RuntimeError(f"invalid {label}: {text!r}")
    return name


def _copy_file(source: Path, target: Path) -> None:
    if target.exists():
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            return
        raise RuntimeError(f"target artifact already exists and differs: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_tree(source: Path, target: Path) -> None:
    if target.exists() and not target.is_dir():
        raise RuntimeError(f"target output path is not a directory: {target}")
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _copy_file(path, destination)


def _verify_copyable(source: Path, target: Path) -> None:
    if target.exists() and (not target.is_file() or target.stat().st_size != source.stat().st_size):
        raise RuntimeError(f"target artifact already exists and differs: {target}")


def _verify_tree_copyable(source: Path, target: Path) -> None:
    if target.exists() and not target.is_dir():
        raise RuntimeError(f"target output path is not a directory: {target}")
    for path in source.rglob("*"):
        if path.is_file():
            _verify_copyable(path, target / path.relative_to(source))


def _backup_database(source_db: Path, backup_db: Path) -> None:
    if backup_db.exists():
        raise RuntimeError(f"refusing to overwrite existing backup: {backup_db}")
    backup_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_db, timeout=3) as source, sqlite3.connect(backup_db, timeout=3) as backup:
        source.backup(backup)
        integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"target backup integrity check failed: {integrity}")


def _fetch_source(source: sqlite3.Connection, report_date: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    source.row_factory = sqlite3.Row
    report = source.execute(
        "SELECT * FROM daily_reports WHERE report_date = ?", (report_date,)
    ).fetchone()
    if not report:
        raise RuntimeError(f"source report does not exist for {report_date}")
    rows = source.execute(
        """
        SELECT rv.*, m.title AS master_title, m.author AS master_author,
               m.source_url AS master_source_url, m.cover_url AS master_cover_url,
               m.local_filename AS master_local_filename,
               m.extraction_dir AS master_extraction_dir
        FROM hot_report_videos rv
        LEFT JOIN hot_video_master m ON m.platform = rv.platform AND m.video_id = rv.video_id
        WHERE rv.report_date = ?
        ORDER BY rv.report_rank ASC
        """,
        (report_date,),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"source report has no video rows for {report_date}")
    return report, rows


def _source_artifacts(rows: list[sqlite3.Row], source_videos: Path, source_output: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for row in rows:
        filename = _safe_name(row["master_local_filename"] or row["local_filename"], "local filename")
        extraction_dir = _safe_name(row["master_extraction_dir"] or row["extraction_dir"] or filename, "extraction directory")
        video_path = source_videos / filename
        analysis_path = source_output / extraction_dir / "analysis.json"
        if not video_path.is_file():
            raise RuntimeError(f"source video artifact is missing: {video_path}")
        if not analysis_path.is_file():
            raise RuntimeError(f"source analysis artifact is missing: {analysis_path}")
        artifacts.append({"filename": filename, "extraction_dir": extraction_dir})
    return artifacts


def _source_covers(rows: list[sqlite3.Row], source_covers: Path) -> list[str]:
    names: set[str] = set()
    for row in rows:
        reference = str(row["master_cover_url"] or row["cover_url"] or "").strip()
        path = urlparse(reference).path
        if "/report-cover/" not in path:
            continue
        name = _safe_name(Path(path).name, "cover filename")
        source = source_covers / name
        if not source.is_file():
            raise RuntimeError(f"source cover artifact is missing: {source}")
        names.add(name)
    return sorted(names)


def _insert_common(conn: sqlite3.Connection, table: str, row: sqlite3.Row, extra: dict[str, Any] | None = None) -> None:
    target_columns = set(_columns(conn, table))
    values = dict(row)
    if extra:
        values.update(extra)
    names = [name for name in values if name in target_columns]
    placeholders = ", ".join("?" for _ in names)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
        [values[name] for name in names],
    )


def _apply(
    target_db: Path,
    report: sqlite3.Row,
    rows: list[sqlite3.Row],
    artifacts: list[dict[str, str]],
    source_videos: Path,
    source_output: Path,
    source_covers: Path,
    cover_names: list[str],
    replace_target: bool,
    backup_db: Path | None,
    expected_target_videos: int | None,
) -> None:
    initialize_hot_report_db()
    if target_db.resolve() != DB_PATH.resolve():
        raise RuntimeError("target database must be the development workspace hot_video_report.sqlite")
    for artifact in artifacts:
        _verify_copyable(source_videos / artifact["filename"], VIDEOS_DIR / artifact["filename"])
        _verify_tree_copyable(source_output / artifact["extraction_dir"], OUTPUT_DIR / artifact["extraction_dir"])
    for name in cover_names:
        _verify_copyable(source_covers / name, REPORT_COVER_DIR / name)

    with sqlite3.connect(target_db, timeout=3) as target:
        existing = target.execute(
            "SELECT id FROM daily_reports WHERE report_date = ?", (report["report_date"],)
        ).fetchone()
        existing_count = target.execute(
            "SELECT count(*) FROM hot_report_videos WHERE report_date = ?", (report["report_date"],)
        ).fetchone()[0]
        if existing and not replace_target:
            raise RuntimeError(f"target report date already exists: {report['report_date']}")
        if existing and expected_target_videos != existing_count:
            raise RuntimeError(
                f"target checkpoint mismatch: expected {expected_target_videos} videos, got {existing_count}"
            )
    if existing:
        if not backup_db:
            raise RuntimeError("--replace-target requires --backup-db")
        _backup_database(target_db, backup_db)
    for artifact in artifacts:
        _copy_file(source_videos / artifact["filename"], VIDEOS_DIR / artifact["filename"])
        _copy_tree(source_output / artifact["extraction_dir"], OUTPUT_DIR / artifact["extraction_dir"])
    for name in cover_names:
        _copy_file(source_covers / name, REPORT_COVER_DIR / name)

    with sqlite3.connect(target_db, timeout=3) as target:
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("BEGIN IMMEDIATE")
        if existing:
            target.execute("DELETE FROM hot_report_videos WHERE report_date = ?", (report["report_date"],))
            target.execute("DELETE FROM daily_reports WHERE report_date = ?", (report["report_date"],))
        _insert_common(target, "daily_reports", report)
        for row, artifact in zip(rows, artifacts):
            master = {
                "platform": row["platform"],
                "video_id": row["video_id"],
                "title": row["master_title"] or "",
                "author": row["master_author"] or "",
                "source_url": row["master_source_url"] or "",
                "cover_url": row["master_cover_url"] or row["cover_url"] or "",
                "local_filename": artifact["filename"],
                "extraction_dir": artifact["extraction_dir"],
                "first_seen_date": report["report_date"],
                "last_seen_date": report["report_date"],
                "latest_hot_score": row["hot_score"],
                "max_hot_score": row["hot_score"],
                "latest_metrics_json": row["metrics_json"],
                "raw_json": row["raw_json"],
                "hidden_from_analyzer": 1,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            target.execute(
                """
                INSERT INTO hot_video_master (
                    platform, video_id, title, author, source_url, cover_url, local_filename,
                    extraction_dir, first_seen_date, last_seen_date, latest_hot_score, max_hot_score,
                    latest_metrics_json, raw_json, hidden_from_analyzer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, video_id) DO UPDATE SET
                    title=excluded.title, author=excluded.author, source_url=excluded.source_url,
                    cover_url=excluded.cover_url, local_filename=excluded.local_filename,
                    extraction_dir=excluded.extraction_dir, last_seen_date=excluded.last_seen_date,
                    latest_hot_score=excluded.latest_hot_score, max_hot_score=excluded.max_hot_score,
                    latest_metrics_json=excluded.latest_metrics_json, raw_json=excluded.raw_json,
                    hidden_from_analyzer=excluded.hidden_from_analyzer, updated_at=excluded.updated_at
                """,
                tuple(master.values()),
            )
            _insert_common(
                target,
                "hot_report_videos",
                row,
                {"local_filename": artifact["filename"], "extraction_dir": artifact["extraction_dir"]},
            )
        target.commit()

    for row, artifact in zip(rows, artifacts):
        register_video(
            video_id=str(row["video_id"]),
            platform=str(row["platform"]),
            source_url=str(row["master_source_url"] or ""),
            filename=artifact["filename"],
            title=str(row["master_title"] or ""),
            author=str(row["master_author"] or ""),
            extraction_dir=artifact["extraction_dir"],
            source="hot_report",
            hidden_from_analyzer=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="exact report date, YYYY-MM-DD")
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--source-videos", type=Path, required=True)
    parser.add_argument("--source-output", type=Path, required=True)
    parser.add_argument("--source-covers", type=Path, help="source report_covers directory; defaults beside --source-db")
    parser.add_argument("--target-db", type=Path, default=TARGET_DB)
    parser.add_argument("--apply", action="store_true", help="copy into the current development workspace")
    parser.add_argument("--replace-target", action="store_true", help="replace an existing target date after an online backup")
    parser.add_argument("--backup-db", type=Path, help="new SQLite backup path; required with --replace-target")
    parser.add_argument("--expected-target-videos", type=int, help="required exact existing target row count when replacing")
    parser.add_argument("--sync-covers-only", action="store_true", help="copy only the local report-cover assets for the selected date")
    parser.add_argument("--expected-videos", type=int, default=10)
    parser.add_argument("--expected-complete", type=int, default=5)
    args = parser.parse_args()
    if args.replace_target and (not args.apply or not args.backup_db or args.expected_target_videos is None):
        parser.error("--replace-target requires --apply, --backup-db, and --expected-target-videos")
    if args.sync_covers_only and not args.apply:
        parser.error("--sync-covers-only requires --apply")

    if not args.source_db.is_file():
        parser.error(f"source database does not exist: {args.source_db}")
    if not args.source_videos.is_dir() or not args.source_output.is_dir():
        parser.error("source video and output directories must exist")
    if args.source_db.resolve() == args.target_db.resolve():
        parser.error("source and target databases must differ")
    if args.source_db.with_name(f"{args.source_db.name}-wal").exists():
        parser.error("source database has an active WAL file; create a verified SQLite backup before copying")
    source_covers = args.source_covers or args.source_db.parent / "report_covers"

    # The source is a read-only bind mount. immutable=1 avoids SQLite attempting
    # to create a lock sidecar; an active WAL is rejected above so no committed
    # checkpoint data is omitted.
    source_uri = f"file://{args.source_db.resolve().as_posix()}?mode=ro&immutable=1"
    with sqlite3.connect(source_uri, uri=True, timeout=3) as source:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"source database integrity check failed: {integrity}")
        report, rows = _fetch_source(source, args.date)
    complete_count = sum(row["process_status"] == "complete" for row in rows)
    if len(rows) != args.expected_videos or complete_count != args.expected_complete:
        raise RuntimeError(
            f"source checkpoint mismatch: expected {args.expected_videos} videos / "
            f"{args.expected_complete} complete, got {len(rows)} / {complete_count}"
        )
    artifacts = _source_artifacts(rows, args.source_videos, args.source_output)
    cover_names = _source_covers(rows, source_covers)
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "date": args.date,
        "report_status": report["status"],
        "video_count": len(rows),
        "complete_count": complete_count,
        "downloaded_artifacts": len(artifacts),
        "cover_artifacts": len(cover_names),
        "target_db": str(args.target_db),
    }, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    if args.sync_covers_only:
        for name in cover_names:
            _verify_copyable(source_covers / name, REPORT_COVER_DIR / name)
        for name in cover_names:
            _copy_file(source_covers / name, REPORT_COVER_DIR / name)
        print(json.dumps({"copied_covers": len(cover_names), "date": args.date}, ensure_ascii=False))
        return 0
    _apply(
        args.target_db, report, rows, artifacts, args.source_videos, args.source_output, source_covers, cover_names,
        args.replace_target, args.backup_db, args.expected_target_videos,
    )
    print(json.dumps({"copied": len(rows), "date": args.date, "backup_db": str(args.backup_db or "")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"checkpoint copy failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
