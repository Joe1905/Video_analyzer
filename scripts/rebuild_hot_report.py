#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from hot_video_report import rebuild_report_from_downloads


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild an existing hot-video report from downloaded media.")
    parser.add_argument("--date", required=True, help="Report date in YYYY-MM-DD format.")
    parser.add_argument("--reuse-downloads", action="store_true", help="Required safety flag; never collect or download media.")
    parser.add_argument("--force-analysis", action="store_true", help="Ignore cached output and analyze every local video again.")
    parser.add_argument(
        "--resume-analysis",
        action="store_true",
        help="Reuse only cached schema 1.1 analyses that pass rebuild validation.",
    )
    args = parser.parse_args()
    result = rebuild_report_from_downloads(
        args.date,
        reuse_downloads=args.reuse_downloads,
        force_analysis=args.force_analysis,
        resume_analysis=args.resume_analysis,
    )
    print(
        json.dumps(
            {
                "report_date": result.get("report_date"),
                "status": result.get("status"),
                "rebuilt_video_count": result.get("rebuilt_video_count"),
                "rebuild_source": result.get("rebuild_source"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
