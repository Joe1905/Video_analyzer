#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import proxy_pool


ROOT = Path.cwd()
LOG_ROOT = ROOT / "data" / "tiktok_collect_jobs"
TIMEZONE_NAME = os.getenv("TZ", "America/Los_Angeles") or "America/Los_Angeles"
DEFAULT_DAILY_TIME = os.getenv("TIKTOK_COLLECT_DAILY_TIME", "03:00").strip() or "03:00"
DEFAULT_MAX_VIDEOS = max(1, min(50, int(os.getenv("TIKTOK_COLLECT_MAX_VIDEOS", "20") or "20")))
RETENTION_MAX_SECONDS = max(10, int(os.getenv("TIKTOK_COLLECT_RETENTION_MAX_SECONDS", "300") or "300"))
WORKER_INTERVAL_SECONDS = max(3, int(os.getenv("TIKTOK_COLLECT_WORKER_INTERVAL_SECONDS", "10") or "10"))
JOB_ACTIVE_STATUSES = {"queued", "delayed", "preparing", "collecting"}
JOB_RETRYABLE_STATUSES = {"failed", "partial", "cancelled"}
STATUS_LABELS = {
    "queued": "待采集",
    "delayed": "等待槽位",
    "preparing": "准备中",
    "collecting": "采集中",
    "complete": "采集完成",
    "partial": "部分失败",
    "failed": "采集失败",
    "cancelled": "已取消",
}

_worker_started = False
_worker_lock = threading.Lock()
_active_jobs: set[str] = set()


class AccountReviewRequired(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return fallback


def _validate_daily_time(value: Any) -> str:
    raw = _clean_text(value, 5)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw):
        raise ValueError("每日采集时间必须为 HH:MM")
    return raw


def _max_videos(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_VIDEOS
    return max(1, min(50, parsed))


def _setting_row(row: Any | None, account_id: int) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "enabled": bool(row["enabled"]) if row else False,
        "daily_time": str(row["daily_time"]) if row else DEFAULT_DAILY_TIME,
        "max_videos": int(row["max_videos"]) if row else DEFAULT_MAX_VIDEOS,
        "last_scheduled_date": str(row["last_scheduled_date"]) if row else "",
        "timezone": TIMEZONE_NAME,
        "updated_at": str(row["updated_at"]) if row else "",
    }


def _job_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "account_id": int(row["account_id"]),
        "proxy_profile_id": int(row["proxy_profile_id"]),
        "trigger_type": str(row["trigger_type"]),
        "schedule_date": str(row["schedule_date"]),
        "max_videos": int(row["max_videos"]),
        "status": str(row["status"]),
        "status_label": STATUS_LABELS.get(str(row["status"]), str(row["status"])),
        "stage": str(row["stage"]),
        "attempt_count": int(row["attempt_count"]),
        "session_id": int(row["session_id"] or 0),
        "total_videos": int(row["total_videos"]),
        "completed_videos": int(row["completed_videos"]),
        "failed_videos": int(row["failed_videos"]),
        "current_video_id": str(row["current_video_id"]),
        "started_at": str(row["started_at"]),
        "completed_at": str(row["completed_at"]),
        "last_error": str(row["last_error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _result_row(row: Any) -> dict[str, Any]:
    payload = _json_loads(row["payload_json"], {})
    return {
        "id": int(row["id"]),
        "job_id": str(row["job_id"]),
        "account_id": int(row["account_id"]),
        "video_id": str(row["video_id"]),
        "video_url": str(row["video_url"]),
        "title": str(row["title"]),
        "published_at": str(row["published_at"]),
        "collected_at": str(row["collected_at"]),
        "retention_complete": bool(row["retention_complete"]),
        "payload": payload,
    }


def _account(conn: Any, account_id: int) -> Any:
    row = conn.execute(
        "SELECT * FROM tiktok_accounts WHERE id = ? AND deleted_at = ''",
        (account_id,),
    ).fetchone()
    if not row:
        raise ValueError("account not found")
    return row


def dashboard(account_id: int) -> dict[str, Any]:
    if not account_id:
        raise ValueError("account_id is required")
    with proxy_pool.connect() as conn:
        account = _account(conn, account_id)
        setting = conn.execute("SELECT * FROM collect_settings WHERE account_id = ?", (account_id,)).fetchone()
        jobs = [
            _job_row(row)
            for row in conn.execute(
                "SELECT * FROM collect_jobs WHERE account_id = ? ORDER BY created_at DESC LIMIT 40",
                (account_id,),
            ).fetchall()
        ]
        results = [
            _result_row(row)
            for row in conn.execute(
                "SELECT * FROM collect_results WHERE account_id = ? ORDER BY collected_at DESC, id DESC LIMIT 100",
                (account_id,),
            ).fetchall()
        ]
        errors = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM collect_errors WHERE account_id = ? ORDER BY created_at DESC, id DESC LIMIT 30",
                (account_id,),
            ).fetchall()
        ]
    return {
        "account": {"id": int(account["id"]), "username": str(account["username"])},
        "setting": _setting_row(setting, account_id),
        "jobs": jobs,
        "results": results,
        "errors": errors,
        "worker": runtime_status(),
    }


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    if not account_id:
        raise ValueError("account_id is required")
    enabled = 1 if payload.get("enabled") else 0
    daily_time = _validate_daily_time(payload.get("daily_time") or DEFAULT_DAILY_TIME)
    max_videos = _max_videos(payload.get("max_videos"))
    now = _iso()
    with proxy_pool.connect() as conn:
        _account(conn, account_id)
        conn.execute(
            """
            INSERT INTO collect_settings (account_id, enabled, daily_time, max_videos, last_scheduled_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, '', ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                enabled = excluded.enabled,
                daily_time = excluded.daily_time,
                max_videos = excluded.max_videos,
                updated_at = excluded.updated_at
            """,
            (account_id, enabled, daily_time, max_videos, now, now),
        )
        conn.commit()
    return dashboard(account_id)


def _insert_job(
    conn: Any,
    account: Any,
    trigger_type: str,
    max_videos: int,
    schedule_date: str = "",
    session_id: int = 0,
) -> str:
    active = conn.execute(
        "SELECT id FROM collect_jobs WHERE account_id = ? AND status IN ('queued','delayed','preparing','collecting') LIMIT 1",
        (int(account["id"]),),
    ).fetchone()
    if active:
        raise ValueError("该账号已有待执行或运行中的采集任务")
    active_publish = conn.execute(
        """
        SELECT id FROM publish_jobs
        WHERE account_id = ? AND deleted_at = ''
          AND status IN ('queued','delayed','preparing','uploading','publishing')
        LIMIT 1
        """,
        (int(account["id"]),),
    ).fetchone()
    if active_publish:
        raise ValueError("该账号已有待执行或运行中的发布任务")
    job_id = f"collect_{uuid.uuid4().hex}"
    now = _iso()
    conn.execute(
        """
        INSERT INTO collect_jobs (
            id, account_id, proxy_profile_id, trigger_type, schedule_date, max_videos,
            status, stage, attempt_count, next_attempt_at, session_id,
            total_videos, completed_videos, failed_videos, current_video_id,
            started_at, completed_at, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', '', 0, '', ?, 0, 0, 0, '', '', '', '', ?, ?)
        """,
        (
            job_id,
            int(account["id"]),
            int(account["proxy_profile_id"]),
            trigger_type,
            schedule_date,
            max_videos,
            session_id or None,
            now,
            now,
        ),
    )
    return job_id


def create_job(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    if not account_id:
        raise ValueError("account_id is required")
    with proxy_pool.connect() as conn:
        account = _account(conn, account_id)
        setting = conn.execute("SELECT * FROM collect_settings WHERE account_id = ?", (account_id,)).fetchone()
        max_videos = _max_videos(payload.get("max_videos") or (setting["max_videos"] if setting else DEFAULT_MAX_VIDEOS))
        job_id = _insert_job(
            conn,
            account,
            "manual",
            max_videos,
            session_id=int(payload.get("observation_session_id") or 0),
        )
        conn.commit()
    data = dashboard(account_id)
    data["job"] = next(job for job in data["jobs"] if job["id"] == job_id)
    return data


def retry_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = _clean_text(payload.get("job_id") or payload.get("id"), 80)
    if not job_id:
        raise ValueError("job_id is required")
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT * FROM collect_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError("collect job not found")
        if str(row["status"]) not in JOB_RETRYABLE_STATUSES:
            raise ValueError("只有失败、部分失败或已取消的采集任务可以重试")
        now = _iso()
        conn.execute(
            """
            UPDATE collect_jobs
            SET status = 'queued', stage = 'retry_queued', attempt_count = 0,
                next_attempt_at = '', session_id = ?, current_video_id = '',
                completed_at = '', last_error = '', updated_at = ?
            WHERE id = ?
            """,
            (int(payload.get("observation_session_id") or 0) or None, now, job_id),
        )
        conn.commit()
        account_id = int(row["account_id"])
    return dashboard(account_id)


def cancel_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = _clean_text(payload.get("job_id") or payload.get("id"), 80)
    if not job_id:
        raise ValueError("job_id is required")
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT * FROM collect_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError("collect job not found")
        if str(row["status"]) not in {"queued", "delayed"}:
            raise ValueError("只能取消尚未开始的采集任务")
        conn.execute(
            "UPDATE collect_jobs SET status = 'cancelled', stage = '', completed_at = ?, updated_at = ? WHERE id = ?",
            (_iso(), _iso(), job_id),
        )
        conn.commit()
        account_id = int(row["account_id"])
    return dashboard(account_id)


def runtime_status() -> dict[str, Any]:
    with proxy_pool.connect() as conn:
        counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM collect_jobs GROUP BY status").fetchall()
        }
    with _worker_lock:
        active = sorted(_active_jobs)
    return {
        "worker_started": _worker_started,
        "timezone": TIMEZONE_NAME,
        "active_jobs": active,
        "counts": counts,
        "max_automatic_slots": proxy_pool.browser_max_slots(),
        "retention_max_seconds": RETENTION_MAX_SECONDS,
    }


def _set_job(job_id: str, status: str, stage: str = "", error: str = "", **values: Any) -> None:
    fields = ["status = ?", "stage = ?", "last_error = ?", "updated_at = ?"]
    params: list[Any] = [status, stage, _clean_text(error), _iso()]
    allowed = {
        "session_id", "total_videos", "completed_videos", "failed_videos",
        "current_video_id", "started_at", "completed_at", "next_attempt_at",
    }
    for key, value in values.items():
        if key in allowed:
            fields.append(f"{key} = ?")
            params.append(value)
    params.append(job_id)
    with proxy_pool.connect() as conn:
        conn.execute(f"UPDATE collect_jobs SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()


def _record_error(job: dict[str, Any], video_id: str, video_url: str, stage: str, error: Exception | str) -> None:
    with proxy_pool.connect() as conn:
        conn.execute(
            "INSERT INTO collect_errors (job_id, account_id, video_id, video_url, stage, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job["id"],
                job["account_id"],
                _clean_text(video_id, 120),
                _clean_text(video_url, 1000),
                _clean_text(stage, 120),
                _clean_text(error),
                _iso(),
            ),
        )
        conn.commit()


def _save_result(job: dict[str, Any], payload: dict[str, Any]) -> None:
    video = payload.get("video") or {}
    with proxy_pool.connect() as conn:
        conn.execute(
            """
            INSERT INTO collect_results (
                job_id, account_id, video_id, video_url, title, published_at,
                collected_at, retention_complete, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, video_id) DO UPDATE SET
                video_url = excluded.video_url,
                title = excluded.title,
                published_at = excluded.published_at,
                collected_at = excluded.collected_at,
                retention_complete = excluded.retention_complete,
                payload_json = excluded.payload_json
            """,
            (
                job["id"],
                job["account_id"],
                _clean_text(video.get("id"), 120),
                _clean_text(video.get("url"), 1000),
                _clean_text(video.get("title"), 2000),
                _clean_text(video.get("published_at"), 120),
                payload["collected_at"],
                1 if payload.get("retention_complete") else 0,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()


def _first_visible(locators: list[Any]) -> Any | None:
    for locator in locators:
        try:
            for index in range(min(locator.count(), 10)):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except Exception:
            continue
    return None


def _assert_account_ready(page: Any) -> None:
    if "/login" in page.url.lower():
        raise AccountReviewRequired("TikTok 登录已失效，请先从观测通道重新登录")
    challenge = _first_visible([
        page.get_by_text(re.compile(r"captcha|verify to continue|security verification|验证码|安全验证", re.I)),
        page.locator("iframe[src*='captcha']"),
    ])
    if challenge:
        raise AccountReviewRequired("TikTok 要求验证码或安全验证，请从观测通道人工处理")


def _skip_onboarding(page: Any) -> None:
    pattern = re.compile(r"^(skip|skip for now|not now|got it|later|跳过|暂不|稍后|知道了)$", re.I)
    for _ in range(4):
        button = _first_visible([page.get_by_role("button", name=pattern), page.get_by_text(pattern, exact=True)])
        if not button:
            return
        button.click(timeout=3000)
        page.wait_for_timeout(400)


def _video_id(url: str) -> str:
    match = re.search(r"/(?:analytics|video)/(\d+)", url)
    return match.group(1) if match else ""


def _discover_links_on_page(page: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    links = page.locator("a[href*='/tiktokstudio/analytics/'], a[href*='/video/']")
    for index in range(min(links.count(), 200)):
        link = links.nth(index)
        try:
            href = str(link.get_attribute("href") or "")
            if not href:
                continue
            absolute_href = urljoin(page.url, href)
            video_id = _video_id(absolute_href)
            if not video_id:
                continue
            text = _clean_text(link.inner_text(), 2000)
            if "/video/" in absolute_href:
                for levels in ("..", "../..", "../../..", "../../../..", "../../../../..", "../../../../../.."):
                    try:
                        candidate = _clean_text(link.locator(f"xpath={levels}").inner_text(), 2000)
                        if len(candidate) <= 1000 and re.search(r"(?m)^\d{1,2}:\d{2}$", candidate):
                            text = candidate
                            break
                    except Exception:
                        continue
            elif not text:
                for levels in ("..", "../..", "../../.."):
                    try:
                        text = _clean_text(link.locator(f"xpath={levels}").inner_text(), 2000)
                        if text:
                            break
                    except Exception:
                        continue
            analytics_url = absolute_href if "/tiktokstudio/analytics/" in absolute_href else urljoin(
                page.url, f"/tiktokstudio/analytics/{video_id}"
            )
            rows.append({"id": video_id, "url": analytics_url, "title_hint": text})
        except Exception:
            continue
    return rows


def _discover_video_links(page: Any, max_videos: int) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}

    def collect() -> None:
        for row in _discover_links_on_page(page):
            found.setdefault(row["id"], row)

    collect()
    for _ in range(5):
        if len(found) >= max_videos:
            break
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(700)
        collect()

    if len(found) < max_videos:
        content_link = _first_visible([
            page.locator("a[href*='/tiktokstudio/content']"),
            page.locator("a[href*='/tiktokstudio/manage']"),
            page.get_by_role("link", name=re.compile(r"content|posts|manage|内容|作品", re.I)),
            page.get_by_role("button", name=re.compile(r"^recent posts$|^posts$|最近作品|近期作品", re.I)),
        ])
        if content_link:
            href = str(content_link.get_attribute("href") or "")
            if href:
                page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=60000)
            else:
                content_link.click(timeout=5000)
            page.wait_for_timeout(1800)
            _assert_account_ready(page)
            for _ in range(8):
                collect()
                if len(found) >= max_videos:
                    break
                page.mouse.wheel(0, 1000)
                page.wait_for_timeout(700)

    return list(found.values())[:max_videos]


def _lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]


def _value_after_label(lines: list[str], labels: list[str]) -> str:
    label_pattern = re.compile(r"^(?:" + "|".join(re.escape(label) for label in labels) + r")$", re.I)
    inline_pattern = re.compile(r"^(?:" + "|".join(re.escape(label) for label in labels) + r")\s*[:：]?\s+(.+)$", re.I)
    for index, line in enumerate(lines):
        inline = inline_pattern.match(line)
        if inline:
            return inline.group(1).strip()
        if label_pattern.match(line):
            for candidate in lines[index + 1:index + 5]:
                if not label_pattern.match(candidate):
                    return candidate
    return ""


def _percent_section(lines: list[str], headings: list[str], stop_headings: list[str]) -> dict[str, str]:
    start = -1
    heading_pattern = re.compile(r"^(?:" + "|".join(re.escape(item) for item in headings) + r")$", re.I)
    stop_pattern = re.compile(r"^(?:" + "|".join(re.escape(item) for item in stop_headings) + r")$", re.I) if stop_headings else None
    for index, line in enumerate(lines):
        if heading_pattern.match(line):
            start = index + 1
            break
    if start < 0:
        return {}
    section: list[str] = []
    for line in lines[start:start + 80]:
        if stop_pattern and stop_pattern.match(line):
            break
        section.append(line)
    values: dict[str, str] = {}
    percent_pattern = re.compile(r"^<?\d+(?:\.\d+)?%$")
    for index, line in enumerate(section):
        if not percent_pattern.match(line) or index == 0:
            continue
        label = section[index - 1]
        if not percent_pattern.match(label) and not re.fullmatch(r"\d+", label):
            values[label] = line
    return values


def _locator_metric(page: Any, names: list[str]) -> str:
    pattern = re.compile("|".join(re.escape(name) for name in names), re.I)
    target = _first_visible([
        page.get_by_text(pattern, exact=False),
        page.locator("[aria-label]").filter(has_text=pattern),
    ])
    if not target:
        return ""
    for ancestor in [target, target.locator("xpath=.."), target.locator("xpath=../..")]:
        try:
            text = _clean_text(ancestor.inner_text(), 300)
            numbers = re.findall(r"(?:\d[\d,.]*[KMB]?|<\d+(?:\.\d+)?%)", text, re.I)
            if numbers:
                return numbers[-1]
        except Exception:
            continue
    return ""


def _overview(lines: list[str]) -> dict[str, str]:
    return {
        "play_count": _value_after_label(lines, ["Video views", "Views", "播放量"]),
        "total_play_time": _value_after_label(lines, ["Total play time", "总播放时间"]),
        "average_watch_time": _value_after_label(lines, ["Average watch time", "平均观看时间"]),
        "completion_rate": _value_after_label(lines, ["Watched full video", "Full video watched", "已观看完整视频", "完播率"]),
        "new_followers": _value_after_label(lines, ["New followers", "新增粉丝"]),
    }


def _engagement(page: Any, lines: list[str], overview: dict[str, str]) -> dict[str, str]:
    values: list[str] = []
    for index, line in enumerate(lines):
        if not re.match(r"^(?:Posted on|发布于)", line, re.I):
            continue
        for candidate in lines[index + 1:index + 12]:
            if re.match(r"^(?:Video views|播放量)$", candidate, re.I):
                break
            if re.fullmatch(r"\d[\d,.]*[KMB]?", candidate, re.I):
                values.append(candidate)
            if len(values) >= 5:
                break
        break
    if len(values) >= 5:
        return dict(zip(("play", "likes", "comments", "shares", "favorites"), values[:5]))
    return {
        "play": overview.get("play_count", ""),
        "likes": _locator_metric(page, ["Likes", "Like", "点赞"]),
        "comments": _locator_metric(page, ["Comments", "Comment", "评论"]),
        "shares": _locator_metric(page, ["Shares", "Share", "分享"]),
        "favorites": _locator_metric(page, ["Favorites", "Favorite", "Saves", "收藏"]),
    }


def _duration_seconds(text: str) -> int:
    values: list[int] = []
    for minutes, seconds in re.findall(r"\b(\d+):(\d{2})\b(?!\s*(?:AM|PM)\b)", text, re.I):
        values.append(int(minutes) * 60 + int(seconds))
    return max(values) if values else 0


def _tooltip_value(text: str) -> tuple[int, str] | None:
    match = re.search(r"(\d+):(\d{2})\s*(\d+(?:\.\d+)?%)", str(text or ""))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2)), match.group(3)


def _retention_chart(page: Any) -> tuple[Any | None, int, str]:
    charts = page.locator(".echarts-for-react")
    fallback: tuple[Any | None, int, str] = (None, 0, "未找到留存率图表")
    for index in range(min(charts.count(), 12)):
        chart = charts.nth(index)
        try:
            if not chart.is_visible():
                continue
            chart.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(250)
            box = chart.bounding_box()
            if not box or box["width"] < 280 or box["height"] > 120:
                continue
            context_text = ""
            for levels in ("..", "../..", "../../..", "../../../.."):
                try:
                    context_text = _clean_text(chart.locator(f"xpath={levels}").inner_text(), 5000)
                    if re.search(r"retention rate|观众留存|留存率", context_text, re.I):
                        break
                except Exception:
                    continue
            duration = _duration_seconds(context_text)
            candidate = (chart, duration, "")
            if re.search(r"retention rate|观众留存|留存率", context_text, re.I):
                return candidate
            fallback = candidate
        except Exception:
            continue
    return fallback


def _sample_retention(page: Any, duration_hint: int = 0) -> tuple[dict[str, str], bool, list[str], str]:
    chart, duration, reason = _retention_chart(page)
    if not chart:
        return {}, False, [], reason
    duration = max(duration, duration_hint)
    if duration <= 0:
        return {}, False, [], "留存率图表没有可识别的视频时长"
    if duration > RETENTION_MAX_SECONDS:
        return {}, False, [], f"视频时长 {duration} 秒超过逐秒采集上限 {RETENTION_MAX_SECONDS} 秒"
    box = chart.bounding_box()
    if not box:
        return {}, False, [], "留存率图表不可见"
    plot_left = box["x"] + 10
    plot_width = max(1.0, box["width"] - 80)
    y = box["y"] + box["height"] / 2
    rows: dict[int, str] = {}
    targets = range(duration + 1)

    def read() -> tuple[int, str] | None:
        try:
            return _tooltip_value(chart.text_content() or "")
        except Exception:
            return None

    for second in targets:
        x = plot_left + plot_width * second / max(1, duration)
        page.mouse.move(x, y)
        page.wait_for_timeout(85)
        parsed = read()
        if parsed and parsed[0] == second:
            rows[second] = parsed[1]

    missing = [second for second in targets if second not in rows]
    for second in missing:
        target_x = plot_left + plot_width * second / max(1, duration)
        offsets = list(range(-36, 37, 6))
        if second in {0, duration}:
            offsets += list(range(-60, 61, 4))
        seen: set[int] = set()
        for offset in offsets:
            if offset in seen:
                continue
            seen.add(offset)
            page.mouse.move(target_x + offset, y)
            page.wait_for_timeout(60)
            parsed = read()
            if parsed and parsed[0] == second:
                rows[second] = parsed[1]
                break

    unresolved = {second for second in targets if second not in rows}
    if unresolved:
        scan_left = int(box["x"])
        scan_right = int(box["x"] + box["width"])
        for x in range(scan_left, scan_right + 1, 3):
            page.mouse.move(x, y)
            page.wait_for_timeout(55)
            parsed = read()
            if parsed and parsed[0] in unresolved:
                rows[parsed[0]] = parsed[1]
                unresolved.discard(parsed[0])
                if not unresolved:
                    break

    missing_labels = [f"{second // 60}:{second % 60:02d}" for second in targets if second not in rows]
    output = {f"{second // 60}:{second % 60:02d}": rows[second] for second in sorted(rows)}
    return output, not missing_labels, missing_labels, "" if not missing_labels else "部分秒点未命中 ECharts tooltip"


def _title_and_date(lines: list[str], hint: str) -> tuple[str, str]:
    cleaned_hint = _lines(hint)
    title = ""
    date_pattern = re.compile(r"(?:[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}/\d{1,2}/\d{4})")
    for candidate in cleaned_hint:
        if re.fullmatch(r"\d{1,2}:\d{2}", candidate) or date_pattern.search(candidate):
            continue
        if candidate.lower() in {"everyone", "friends", "only you"} or re.fullmatch(r"[\d,.]+", candidate):
            continue
        title = candidate
        break
    published = ""
    for line in cleaned_hint + lines[:80]:
        match = date_pattern.search(line)
        if match:
            published = match.group(0)
            break
    if not title:
        for line in lines[:50]:
            if len(line) >= 8 and not re.match(r"^(TikTok Studio|Video analytics|Analytics)$", line, re.I):
                title = line
                break
    return title, published


def _collect_video(page: Any, job: dict[str, Any], source: dict[str, str], log_dir: Path) -> dict[str, Any]:
    page.goto(source["url"], wait_until="domcontentloaded", timeout=60000)
    _assert_account_ready(page)
    try:
        page.get_by_text(re.compile(r"^(?:Video views|播放量)$", re.I)).first.wait_for(
            state="visible", timeout=15000
        )
        page.wait_for_timeout(1200)
    except Exception:
        page.wait_for_timeout(2200)
    _assert_account_ready(page)
    body = page.locator("body").inner_text(timeout=15000)
    lines = _lines(body)
    overview = _overview(lines)
    if not any(overview.values()):
        page.screenshot(path=str(log_dir / f"{source['id']}-missing-overview.png"), full_page=True)
        raise RuntimeError("视频分析页没有识别到概览指标")
    title, published_at = _title_and_date(lines, source.get("title_hint", ""))
    retention, retention_complete, missing, retention_reason = _sample_retention(
        page, _duration_seconds(source.get("title_hint", ""))
    )
    payload = {
        "account": {
            "id": job["account_id"],
            "username": job["username"],
            "proxy_profile_id": job["proxy_profile_id"],
            "observed_ip": job["observed_ip"],
            "browser_session_id": job["session_id"],
        },
        "collection_job": {"job_id": job["id"], "trigger_type": job["trigger_type"]},
        "video": {"id": source["id"], "title": title, "published_at": published_at, "url": page.url},
        "time_filter": {"requested": None, "applied": None, "scope": "video_lifetime", "applied_successfully": False},
        "overview": overview,
        "engagement": _engagement(page, lines, overview),
        "retention": retention,
        "retention_complete": retention_complete,
        "missing_retention_seconds": missing,
        "retention_reason": retention_reason,
        "traffic_sources": _percent_section(lines, ["Traffic source", "Traffic sources", "流量来源"], ["Search queries", "搜索查询"]),
        "search_queries": _percent_section(lines, ["Search queries", "搜索查询"], ["Viewer types", "Audience", "观众"]),
        "updated_at": _value_after_label(lines, ["Updated", "Last updated", "更新时间"]),
        "collected_at": _iso(),
    }
    page.screenshot(path=str(log_dir / f"{source['id']}-collected.png"), full_page=True)
    return payload


def _load_job(job_id: str) -> dict[str, Any] | None:
    with proxy_pool.connect() as conn:
        row = conn.execute(
            """
            SELECT j.*, a.username, a.last_checked_ip
            FROM collect_jobs j JOIN tiktok_accounts a ON a.id = j.account_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if not row:
            return None
        job = _job_row(row)
        job["username"] = str(row["username"])
        job["observed_ip"] = str(row["last_checked_ip"])
        return job


def _completed_video_ids(job_id: str) -> set[str]:
    with proxy_pool.connect() as conn:
        return {
            str(row["video_id"])
            for row in conn.execute("SELECT video_id FROM collect_results WHERE job_id = ?", (job_id,)).fetchall()
        }


def _execute_browser(job: dict[str, Any], session: dict[str, Any]) -> tuple[int, int, int]:
    from playwright.sync_api import sync_playwright

    log_dir = LOG_ROOT / job["id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    completed_ids = _completed_video_ids(job["id"])
    completed = len(completed_ids)
    failed = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{session['debug_port']}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.tiktok.com/tiktokstudio?lang=en", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2200)
        _assert_account_ready(page)
        _skip_onboarding(page)
        links = _discover_video_links(page, int(job["max_videos"]))
        if not links:
            page.screenshot(path=str(log_dir / "no-video-links.png"), full_page=True)
            raise RuntimeError("TikTok Studio 没有发现可采集的视频分析入口")
        _set_job(
            job["id"],
            "collecting",
            "video_list_ready",
            session_id=session["id"],
            total_videos=len(links),
            completed_videos=completed,
            failed_videos=0,
        )
        for source in links:
            with proxy_pool.connect() as conn:
                current = conn.execute("SELECT status FROM collect_jobs WHERE id = ?", (job["id"],)).fetchone()
            if current and str(current["status"]) == "cancelled":
                break
            if source["id"] in completed_ids:
                continue
            _set_job(
                job["id"],
                "collecting",
                "collect_video",
                session_id=session["id"],
                total_videos=len(links),
                completed_videos=completed,
                failed_videos=failed,
                current_video_id=source["id"],
            )
            job["session_id"] = int(session["id"])
            try:
                payload = _collect_video(page, job, source, log_dir)
                _save_result(job, payload)
                completed += 1
                completed_ids.add(source["id"])
            except AccountReviewRequired:
                raise
            except Exception as exc:
                failed += 1
                _record_error(job, source["id"], source["url"], "collect_video", exc)
            _set_job(
                job["id"],
                "collecting",
                "collect_video",
                session_id=session["id"],
                total_videos=len(links),
                completed_videos=completed,
                failed_videos=failed,
                current_video_id="",
            )
    return len(links), completed, failed


def _update_account(account_id: int, collected_at: str = "", error: str = "") -> None:
    with proxy_pool.connect() as conn:
        conn.execute(
            """
            UPDATE tiktok_accounts
            SET last_collect_at = COALESCE(NULLIF(?, ''), last_collect_at),
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (collected_at, _clean_text(error), _iso(), account_id),
        )
        conn.commit()


def _run_job(job_id: str) -> None:
    session_id = 0
    reused_observation = False
    try:
        job = _load_job(job_id)
        if not job:
            return
        requested_session_id = int(job.get("session_id") or 0)
        session = proxy_pool.claim_observation_session_for_job(job["account_id"], requested_session_id, job_id)
        if session is not None:
            reused_observation = True
        else:
            session = proxy_pool.start_automation_session(job["account_id"], job_id)["session"]
        session_id = int(session["id"])
        _set_job(job_id, "preparing", "browser_ready", session_id=session_id, started_at=_iso())
        total, completed, failed = _execute_browser(job, session)
        status = "complete" if failed == 0 else ("partial" if completed else "failed")
        message = "" if failed == 0 else f"{failed} 个视频采集失败"
        _set_job(
            job_id,
            status,
            "complete",
            message,
            session_id=session_id,
            total_videos=total,
            completed_videos=completed,
            failed_videos=failed,
            current_video_id="",
            completed_at=_iso(),
        )
        _update_account(job["account_id"], collected_at=_iso() if completed else "", error=message)
    except Exception as exc:
        message = str(exc)
        if "槽位已满" in message or "已经处于唤醒状态" in message:
            _set_job(job_id, "delayed", "waiting_slot", message, next_attempt_at=_iso(_utc_now() + timedelta(seconds=30)))
        else:
            _set_job(job_id, "failed", "failed", message, session_id=session_id or None, completed_at=_iso())
            job = _load_job(job_id)
            if job:
                _record_error(job, "", "", "job", message)
                _update_account(job["account_id"], error=message)
    finally:
        if session_id and reused_observation:
            try:
                proxy_pool.release_observation_session_job(session_id, job_id)
            except Exception as exc:
                print(f"Collect observation session release failed for {job_id}: {exc}", flush=True)
        elif session_id:
            try:
                proxy_pool.finish_automation_session(session_id, "自动采集任务结束")
            except Exception as exc:
                print(f"Collect session cleanup failed for {job_id}: {exc}", flush=True)
        with _worker_lock:
            _active_jobs.discard(job_id)


def _schedule_daily_jobs() -> None:
    local_now = _utc_now().astimezone(ZoneInfo(TIMEZONE_NAME))
    local_date = local_now.date().isoformat()
    local_time = local_now.strftime("%H:%M")
    with proxy_pool.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = conn.execute(
            """
            SELECT s.*, a.proxy_profile_id, a.deleted_at
            FROM collect_settings s JOIN tiktok_accounts a ON a.id = s.account_id
            WHERE s.enabled = 1
            """
        ).fetchall()
        for setting in settings:
            if setting["deleted_at"] or str(setting["last_scheduled_date"]) == local_date:
                continue
            if local_time < str(setting["daily_time"]):
                continue
            account = _account(conn, int(setting["account_id"]))
            try:
                _insert_job(conn, account, "daily", int(setting["max_videos"]), schedule_date=local_date)
            except ValueError:
                # Keep trying after the account becomes idle; recording the date
                # here would silently drop today's scheduled collection.
                continue
            conn.execute(
                "UPDATE collect_settings SET last_scheduled_date = ?, updated_at = ? WHERE account_id = ?",
                (local_date, _iso(), int(setting["account_id"])),
            )
        conn.commit()


def _claim_due_jobs() -> list[str]:
    with _worker_lock:
        capacity = max(0, proxy_pool.browser_max_slots() - len(_active_jobs))
    if capacity <= 0:
        return []
    claimed: list[str] = []
    with proxy_pool.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id FROM collect_jobs
            WHERE status IN ('queued','delayed')
              AND (next_attempt_at = '' OR next_attempt_at <= ?)
            ORDER BY created_at ASC LIMIT ?
            """,
            (_iso(), capacity),
        ).fetchall()
        for row in rows:
            job_id = str(row["id"])
            changed = conn.execute(
                """
                UPDATE collect_jobs
                SET status = 'preparing', stage = 'claimed', attempt_count = attempt_count + 1,
                    next_attempt_at = '', updated_at = ?
                WHERE id = ? AND status IN ('queued','delayed')
                """,
                (_iso(), job_id),
            ).rowcount
            if changed:
                claimed.append(job_id)
        conn.commit()
    return claimed


def _recover_interrupted() -> None:
    with proxy_pool.connect() as conn:
        conn.execute(
            """
            UPDATE collect_jobs
            SET status = 'queued', stage = 'recovered', session_id = NULL,
                next_attempt_at = '', last_error = '服务器重启后恢复采集任务', updated_at = ?
            WHERE status IN ('preparing','collecting')
            """,
            (_iso(),),
        )
        conn.commit()


def _worker_loop() -> None:
    while True:
        try:
            _schedule_daily_jobs()
            for job_id in _claim_due_jobs():
                with _worker_lock:
                    if job_id in _active_jobs:
                        continue
                    _active_jobs.add(job_id)
                threading.Thread(target=_run_job, args=(job_id,), daemon=True, name=f"tiktok-collect-{job_id[-8:]}").start()
        except Exception as exc:
            print(f"TikTok collect scheduler failed: {exc}", flush=True)
        time.sleep(WORKER_INTERVAL_SECONDS)


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    _recover_interrupted()
    threading.Thread(target=_worker_loop, daemon=True, name="tiktok-collect-worker").start()
