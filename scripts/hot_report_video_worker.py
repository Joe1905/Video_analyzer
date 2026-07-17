#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hot_video_report import _analyze_report_video, _connect, _download_report_video, _process_video


def main() -> int:
    parser = argparse.ArgumentParser(description="Process one hot-report video.")
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--phase", choices=("all", "download", "analyze"), default="all")
    args = parser.parse_args()

    item = json.loads(Path(args.task_file).read_text(encoding="utf-8"))
    platform = str(item["platform"])
    video_id = str(item["video_id"])
    with _connect() as conn:
        if args.phase == "download":
            _download_report_video(conn, args.report_date, item)
            expected_status = "downloaded"
        elif args.phase == "analyze":
            _analyze_report_video(conn, args.report_date, item, enqueue_translation=False)
            expected_status = "complete"
        else:
            _process_video(conn, args.report_date, item, enqueue_translation=False)
            expected_status = "complete"
        row = conn.execute(
            "SELECT process_status, process_error FROM hot_report_videos "
            "WHERE report_date = ? AND platform = ? AND video_id = ?",
            (args.report_date, platform, video_id),
        ).fetchone()

    status = str(row[0] if row else "missing")
    error = str(row[1] or "") if row else "video row missing after processing"
    print(json.dumps({"platform": platform, "video_id": video_id, "status": status, "error": error}, ensure_ascii=False), flush=True)
    return 0 if status == expected_status else 1


if __name__ == "__main__":
    raise SystemExit(main())
