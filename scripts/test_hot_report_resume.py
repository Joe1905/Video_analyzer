#!/usr/bin/env python3
"""Run with: docker compose -p short-video-analyzer run --rm analyzer python scripts/test_hot_report_resume.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import hot_video_report as report


def _item(index: int) -> dict:
    return {
        "platform": "tiktok", "video_id": f"video-{index}", "title": f"video {index}", "author": "tester",
        "source_url": f"https://example.test/{index}", "cover_url": "", "source_endpoint": "fixture",
        "source_label": "fixture", "source_rank": index, "report_rank": index, "hot_score": 100 - index,
        "metrics": {"play_count": index}, "raw": {"id": index},
    }


def main() -> int:
    original = (
        report.DB_PATH, report.VIDEOS_DIR, report.OUTPUT_DIR, report._initialized_db_path,
        report.get_video_by_filename, report._collect_hot_video_candidates, report._process_video,
        report._generate_daily_summary, os.environ.get("SOCIAVAULT_API_KEY"),
    )
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        report_date = report.today_key()
        report.DB_PATH, report.VIDEOS_DIR, report.OUTPUT_DIR = base / "report.sqlite", base / "videos", base / "output"
        report.VIDEOS_DIR.mkdir()
        report.OUTPUT_DIR.mkdir()
        report._initialized_db_path = None
        report.get_video_by_filename = lambda filename: None
        report.initialize_hot_report_db()
        with closing(report._connect()) as conn:
            conn.execute("CREATE TABLE hot_videos (report_id TEXT, report_date TEXT, platform TEXT, video_id TEXT)")
            conn.execute(
                "INSERT INTO hot_videos (report_id, report_date, platform, video_id) VALUES (?, ?, ?, ?)",
                ("orphan-report", "2026-07-31", "tiktok", "orphan-video"),
            )
            conn.commit()
        report._initialized_db_path = None
        report.initialize_hot_report_db()
        with closing(report._connect()) as conn:
            migrated_orphan = conn.execute(
                "SELECT COUNT(*) FROM hot_report_videos WHERE report_date='2026-07-31' AND video_id='orphan-video'"
            ).fetchone()[0]
            assert migrated_orphan == 0
        with closing(report._connect()) as conn:
            report_id = report._start_report(conn, report_date, "US", [], worker_lease="lease")
            for index in range(1, 6):
                item = _item(index)
                report._upsert_video(conn, report_id, report_date, item, index)
                filename = f"video-{index}.mp4"
                (report.VIDEOS_DIR / filename).write_bytes(b"fixture")
                output = report.OUTPUT_DIR / filename
                output.mkdir()
                (output / "analysis.json").write_text(json.dumps({"summary": "fixture"}), encoding="utf-8")
                conn.execute(
                    """UPDATE hot_report_videos SET process_status='complete', process_step='complete',
                    local_filename=?, analysis_json=?, insight_json=?, social_context_json=?
                    WHERE report_date=? AND platform=? AND video_id=?""",
                    (filename, json.dumps({"summary": "fixture"}), json.dumps({"one_sentence": "ok"}), "{}", report_date, "tiktok", f"video-{index}"),
                )
            conn.commit()
            assert len(report._load_success_videos(conn, report_date)) == 5
            invalid = _item(6)
            invalid.update({"local_filename": "missing.mp4", "analysis": {}, "insight": {"error": "generated failed"}, "social_context": {}})
            assert not report._is_video_checkpoint_valid(invalid)[0]
            report._mark_video_pending(conn, report_date, "tiktok", "video-1", "fixture invalidation")
            status = conn.execute("SELECT process_status FROM hot_report_videos WHERE video_id='video-1'").fetchone()[0]
            assert status == "pending"
            conn.execute("UPDATE hot_report_videos SET process_status='complete', process_step='complete' WHERE video_id='video-1'")
            conn.commit()

        processed: list[str] = []
        video6_attempts = {"count": 0}
        def fake_collect(*args, **kwargs):
            return {("tiktok", f"video-{index}"): _item(index) for index in range(6, 12)}, []
        def fake_process(conn, report_date, item):
            processed.append(item["video_id"])
            if item["video_id"] == "video-6":
                video6_attempts["count"] += 1
                if video6_attempts["count"] == 1:
                    # 第一次失败 -> 触发同轮立即重试
                    conn.execute(
                        """UPDATE hot_report_videos SET process_status='failed', process_step='failed',
                        process_error=?, attempt_count=attempt_count+1, last_attempt_at=?
                        WHERE report_date=? AND platform=? AND video_id=?""",
                        ("video analysis subprocess timed out after 240 seconds", 1_700_000_000, report_date, item["platform"], item["video_id"]),
                    )
                    conn.commit()
                    return
                # 同轮重试成功 -> 备份顶上机制救回
            filename = f"{item['video_id']}.mp4"
            (report.VIDEOS_DIR / filename).write_bytes(b"fixture")
            output = report.OUTPUT_DIR / filename
            output.mkdir(exist_ok=True)
            (output / "analysis.json").write_text(json.dumps({"summary": "fixture"}), encoding="utf-8")
            conn.execute(
                """UPDATE hot_report_videos SET process_status='complete', process_step='complete',
                local_filename=?, analysis_json=?, insight_json=?, social_context_json=?,
                attempt_count=attempt_count+1
                WHERE report_date=? AND platform=? AND video_id=?""",
                (filename, json.dumps({"summary": "fixture"}), json.dumps({"one_sentence": "ok"}), "{}", report_date, item["platform"], item["video_id"]),
            )
            conn.commit()
        def fake_summary(report_date, videos):
            body = {key: [] for key in report.REQUIRED_DAILY_REPORT_KEYS}
            body["summary"] = f"{report_date} fixture"
            return body, "# fixture"
        report._collect_hot_video_candidates = fake_collect
        report._process_video = fake_process
        report._generate_daily_summary = fake_summary
        os.environ["SOCIAVAULT_API_KEY"] = "fixture"
        first_run = report.run_report(report_date)
        assert first_run["status"] == "complete"
        # video-6 首次失败后同轮立即重试成功,因此 processed 中出现两次 video-6;
        # v6+v7..v10 共 5 个新成功 + 已有 5 个 complete = 达标 10,提前 break,v11 不处理
        assert processed == ["video-6", "video-6"] + [f"video-{index}" for index in range(7, 11)]
        assert video6_attempts["count"] == 2
        with closing(report._connect()) as conn:
            row = conn.execute("SELECT process_status, attempt_count FROM hot_report_videos WHERE video_id='video-6'").fetchone()
            # 重试救回:最终 complete,attempt_count=2
            assert row == ("complete", 2)
        # backoff 场景:attempt=1、last_attempt 在未来 -> 等待退避;attempt 达 max -> 不再重试
        with closing(report._connect()) as conn:
            assert report._failed_video_retry_state(conn, report_date, "tiktok", "video-6", now=1_700_000_001) is None
            fake_conn = type(
                "FC", (),
                {"execute": lambda self, sql, params=(): type("C", (), {"fetchone": lambda self: ("failed", 2, 1_700_000_000)})()},
            )()
            assert report._failed_video_retry_state(fake_conn, "d", "p", "v", now=1_700_000_001) == "retry_exhausted"
        with closing(report._connect()) as conn:
            conn.execute("UPDATE daily_reports SET status='failed', report_json=NULL, report_markdown=NULL WHERE report_date=?", (report_date,))
            conn.commit()
        processed.clear()
        second_run = report.run_report(report_date)
        assert second_run["status"] == "complete"
        assert processed == []
        assert all(report._is_recoverable_external_error(message) for message in ("HTTP 402 payment required", "429 Client Error", "HTTP status 503"))
        assert not report._is_recoverable_external_error("analyze_one.sh timed out after 600 seconds")
        assert not report._is_recoverable_external_error("request timeout")
        with closing(report._connect()) as conn:
            conn.execute("UPDATE daily_reports SET status='failed', report_json=NULL, report_markdown=NULL WHERE report_date=?", (report_date,))
            conn.commit()
        report._generate_daily_summary = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("429 Client Error"))
        paused_run = report.run_report(report_date)
        assert paused_run["status"] == "paused_external"
        assert processed == []
    (
        report.DB_PATH, report.VIDEOS_DIR, report.OUTPUT_DIR, report._initialized_db_path,
        report.get_video_by_filename, report._collect_hot_video_candidates, report._process_video,
        report._generate_daily_summary, previous_api_key,
    ) = original
    if previous_api_key is None:
        os.environ.pop("SOCIAVAULT_API_KEY", None)
    else:
        os.environ["SOCIAVAULT_API_KEY"] = previous_api_key
    print("hot report resume checkpoints: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
