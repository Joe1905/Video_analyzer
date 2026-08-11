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
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import proxy_pool
from browser_page_state import (
    BrowserPageBlocked,
    BrowserPageLoadError,
    BrowserPageTimeout,
    navigate_with_retries,
    wait_for_page_state,
)
from feishu_capabilities import FeishuCapabilityClient, FeishuCapabilityError


ROOT = Path.cwd()
LOG_ROOT = ROOT / "data" / "tiktok_collect_jobs"
TIMEZONE_NAME = os.getenv("TZ", "America/Los_Angeles") or "America/Los_Angeles"
DEFAULT_DAILY_TIME = os.getenv("TIKTOK_COLLECT_DAILY_TIME", "03:00").strip() or "03:00"
DEFAULT_DATE_RULE = "previous_day"
DATE_RULES = {"previous_day", "same_day"}
RETENTION_MAX_SECONDS = max(10, int(os.getenv("TIKTOK_COLLECT_RETENTION_MAX_SECONDS", "300") or "300"))
DETAIL_SECTIONS_TIMEOUT_SECONDS = max(
    5, int(os.getenv("TIKTOK_COLLECT_DETAIL_SECTIONS_TIMEOUT_SECONDS", "20") or "20")
)
WORKER_INTERVAL_SECONDS = max(3, int(os.getenv("TIKTOK_COLLECT_WORKER_INTERVAL_SECONDS", "10") or "10"))
LIST_SCROLL_STEP_PX = max(60, min(240, int(os.getenv("TIKTOK_COLLECT_LIST_SCROLL_STEP_PX", "120") or "120")))
LIST_SCROLL_WAIT_MS = max(300, min(3000, int(os.getenv("TIKTOK_COLLECT_LIST_SCROLL_WAIT_MS", "550") or "550")))
LIST_SCROLL_MAX_ROUNDS = max(30, min(600, int(os.getenv("TIKTOK_COLLECT_LIST_SCROLL_MAX_ROUNDS", "240") or "240")))
PENDING_COLLECTION_STATUS = "not_collected"
JOB_ACTIVE_STATUSES = {"queued", "delayed", "preparing", "collecting"}
JOB_RETRYABLE_STATUSES = {"failed", "partial", "cancelled"}
COLLECT_PLATFORMS = {"tiktok", "instagram"}
STATUS_LABELS = {
    "queued": "待采集",
    "delayed": "延迟等待",
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
_active_rescans: set[str] = set()
_feishu_client = FeishuCapabilityClient(timeout=30)


class AccountReviewRequired(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _collect_platform(value: Any) -> str:
    platform = _clean_text(value, 32).lower() or "tiktok"
    if platform not in COLLECT_PLATFORMS:
        raise ValueError("采集平台仅支持 TikTok 或 Instagram")
    return platform


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


def _local_today() -> str:
    return _utc_now().astimezone(ZoneInfo(TIMEZONE_NAME)).date().isoformat()


def _validate_publish_range(start_value: Any, end_value: Any) -> tuple[str, str]:
    start = _clean_text(start_value, 10) or _local_today()
    end = _clean_text(end_value, 10) or start
    for label, value in (("开始", start), ("结束", end)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"发布{label}日期必须为 YYYY-MM-DD") from exc
    if end < start:
        raise ValueError("发布结束日期不能早于开始日期")
    return start, end


def _validate_date_rule(value: Any) -> str:
    rule = _clean_text(value, 30) or DEFAULT_DATE_RULE
    if rule not in DATE_RULES:
        raise ValueError("自动采集日期规则无效")
    return rule


def _automatic_publish_range(rule: str, local_date: Any) -> tuple[str, str]:
    target_date = local_date - timedelta(days=1) if rule == "previous_day" else local_date
    value = target_date.isoformat()
    return value, value


def _setting_row(row: Any | None, account_id: int) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "enabled": bool(row["enabled"]) if row else False,
        "daily_time": str(row["daily_time"]) if row else DEFAULT_DAILY_TIME,
        "date_rule": _validate_date_rule(row["date_rule"] if row else DEFAULT_DATE_RULE),
        "feishu_target": _json_loads(row["feishu_target_json"], {}) if row else {},
        "last_scheduled_date": str(row["last_scheduled_date"]) if row else "",
        "timezone": TIMEZONE_NAME,
        "updated_at": str(row["updated_at"]) if row else "",
    }


def _job_row(row: Any) -> dict[str, Any]:
    columns = set(row.keys()) if hasattr(row, "keys") else set()
    return {
        "id": str(row["id"]),
        "account_id": int(row["account_id"]),
        "proxy_profile_id": int(row["proxy_profile_id"]),
        "platform": _collect_platform(row["platform"] if "platform" in columns else "tiktok"),
        "trigger_type": str(row["trigger_type"]),
        "schedule_date": str(row["schedule_date"]),
        "max_videos": int(row["max_videos"]),
        "publish_date_start": str(row["publish_date_start"]),
        "publish_date_end": str(row["publish_date_end"]),
        "feishu_target": _json_loads(row["feishu_target_json"], {}),
        "auto_sync": bool(row["auto_sync"]),
        "status": str(row["status"]),
        "status_label": STATUS_LABELS.get(str(row["status"]), str(row["status"])),
        "stage": str(row["stage"]),
        "status_detail": str(row["status_detail"]),
        "attempt_count": int(row["attempt_count"]),
        "session_id": int(row["session_id"] or 0),
        "total_videos": int(row["total_videos"]),
        "completed_videos": int(row["completed_videos"]),
        "failed_videos": int(row["failed_videos"]),
        "current_video_id": str(row["current_video_id"]),
        "started_at": str(row["started_at"]),
        "completed_at": str(row["completed_at"]),
        "last_error": str(row["last_error"]),
        "feishu_failed_results": int(row["feishu_failed_results"] or 0) if "feishu_failed_results" in columns else 0,
        "feishu_unsynced_results": int(row["feishu_unsynced_results"] or 0) if "feishu_unsynced_results" in columns else 0,
        "uncollected_videos": int(row["uncollected_videos"] or 0) if "uncollected_videos" in columns else 0,
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
        "feishu_target": _json_loads(row["feishu_target_json"], {}),
        "feishu_record_id": str(row["feishu_record_id"]),
        "feishu_sync_status": str(row["feishu_sync_status"]),
        "feishu_sync_error": str(row["feishu_sync_error"]),
        "feishu_synced_at": str(row["feishu_synced_at"]),
        "payload": payload,
    }


def list_feishu_targets() -> dict[str, Any]:
    return _feishu_client.list_bitable_targets()


def _target_key(target: dict[str, Any]) -> str:
    app = _clean_text(target.get("appToken") or target.get("wikiToken"), 200)
    table = _clean_text(target.get("tableId"), 200)
    return f"{app}:{table}" if app and table else ""


def _job_feishu_target(job: dict[str, Any]) -> dict[str, Any]:
    target = job.get("feishu_target")
    if isinstance(target, str):
        target = _json_loads(target, {})
    if isinstance(target, dict) and _target_key(target):
        return target
    target = _json_loads(job.get("feishu_target_json"), {})
    return target if isinstance(target, dict) else {}


def _validate_feishu_target(value: Any) -> dict[str, Any]:
    requested = value if isinstance(value, dict) else _json_loads(value, {})
    key = _target_key(requested)
    if not key:
        raise ValueError("请选择采集结果要写入的飞书多维表格")
    targets = list_feishu_targets().get("targets") or []
    matched = next((item for item in targets if isinstance(item, dict) and _target_key(item) == key), None)
    if not matched:
        raise ValueError("选择的多维表格不在当前写入白名单中")
    return {
        "appToken": _clean_text(matched.get("appToken"), 200),
        "appName": _clean_text(matched.get("appName"), 200),
        "wikiToken": _clean_text(matched.get("wikiToken"), 200),
        "tableId": _clean_text(matched.get("tableId"), 200),
        "tableName": _clean_text(matched.get("tableName"), 200),
        "url": _clean_text(matched.get("url"), 1000),
        "fields": [
            {
                "id": _clean_text(field.get("id"), 200),
                "name": _clean_text(field.get("name"), 200),
                "type": int(field.get("type") or 0),
                "uiType": _clean_text(field.get("uiType"), 100),
                "isPrimary": bool(field.get("isPrimary")),
            }
            for field in matched.get("fields") or []
            if isinstance(field, dict) and _clean_text(field.get("name"), 200)
        ],
    }


def _account(conn: Any, account_id: int) -> Any:
    row = conn.execute(
        "SELECT * FROM tiktok_accounts WHERE id = ? AND deleted_at = ''",
        (account_id,),
    ).fetchone()
    if not row:
        raise ValueError("account not found")
    return row


def dashboard(account_id: int, platform: str = "tiktok") -> dict[str, Any]:
    if not account_id:
        raise ValueError("account_id is required")
    platform = _collect_platform(platform)
    with proxy_pool.connect() as conn:
        account = _account(conn, account_id)
        setting = conn.execute("SELECT * FROM collect_settings WHERE account_id = ?", (account_id,)).fetchone()
        jobs = [
            _job_row(row)
            for row in conn.execute(
                """
                SELECT j.*,
                       (SELECT COUNT(*) FROM collect_results r
                        WHERE r.job_id = j.id AND r.feishu_sync_status = 'failed') AS feishu_failed_results
                       ,(SELECT COUNT(*) FROM collect_results r
                         WHERE r.job_id = j.id AND r.feishu_sync_status = 'not_synced') AS feishu_unsynced_results,
                       (SELECT COUNT(*) FROM collect_results r
                         WHERE r.job_id = j.id AND r.feishu_sync_status = 'not_collected') AS uncollected_videos
                FROM collect_jobs j
                WHERE j.account_id = ? AND j.platform = ?
                ORDER BY j.created_at DESC
                LIMIT 40
                """,
                (account_id, platform),
            ).fetchall()
        ]
        results = [
            _result_row(row)
            for row in conn.execute(
                """SELECT r.* FROM collect_results r
                   JOIN collect_jobs j ON j.id = r.job_id
                   WHERE r.account_id = ? AND j.platform = ?
                   ORDER BY r.collected_at DESC, r.id DESC LIMIT 100""",
                (account_id, platform),
            ).fetchall()
        ]
        errors = [
            dict(row)
            for row in conn.execute(
                """SELECT e.* FROM collect_errors e
                   JOIN collect_jobs j ON j.id = e.job_id
                   WHERE e.account_id = ? AND j.platform = ?
                   ORDER BY e.created_at DESC, e.id DESC LIMIT 30""",
                (account_id, platform),
            ).fetchall()
        ]
    setting_data = _setting_row(setting, account_id)
    if not _target_key(setting_data.get("feishu_target") or {}):
        last_target = next(
            (job.get("feishu_target") for job in jobs if _target_key(job.get("feishu_target") or {})),
            {},
        )
        setting_data["feishu_target"] = last_target
    return {
        "account": {"id": int(account["id"]), "username": str(account["username"])},
        "platform": platform,
        "setting": setting_data,
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
    date_rule = _validate_date_rule(payload.get("date_rule"))
    feishu_target = _validate_feishu_target(payload.get("feishu_target"))
    feishu_target_json = json.dumps(feishu_target, ensure_ascii=False, separators=(",", ":"))
    now = _iso()
    with proxy_pool.connect() as conn:
        account = _account(conn, account_id)
        proxy_pool.require_account_proxy_bound(account)
        conn.execute(
            """
            INSERT INTO collect_settings (
                account_id, enabled, daily_time, max_videos, date_rule, publish_date_start, publish_date_end,
                feishu_target_json, last_scheduled_date, created_at, updated_at
            ) VALUES (?, ?, ?, 0, ?, '', '', ?, '', ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                enabled = excluded.enabled,
                daily_time = excluded.daily_time,
                max_videos = 0,
                date_rule = excluded.date_rule,
                feishu_target_json = excluded.feishu_target_json,
                updated_at = excluded.updated_at
            """,
            (
                account_id, enabled, daily_time, date_rule, feishu_target_json, now, now,
            ),
        )
        conn.commit()
    return dashboard(account_id)


def _insert_job(
    conn: Any,
    account: Any,
    trigger_type: str,
    publish_date_start: str,
    publish_date_end: str,
    feishu_target_json: str,
    schedule_date: str = "",
    session_id: int = 0,
    auto_sync: bool = True,
    platform: str = "tiktok",
) -> str:
    job_id = f"collect_{uuid.uuid4().hex}"
    now = _iso()
    conn.execute(
        """
        INSERT INTO collect_jobs (
            id, account_id, proxy_profile_id, platform, trigger_type, schedule_date, max_videos,
            publish_date_start, publish_date_end, feishu_target_json,
            auto_sync,
            status, stage, attempt_count, next_attempt_at, session_id,
            total_videos, completed_videos, failed_videos, current_video_id,
            started_at, completed_at, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', '', 0, '', ?, 0, 0, 0, '', '', '', '', ?, ?)
        """,
        (
            job_id,
            int(account["id"]),
            int(account["proxy_profile_id"]),
            _collect_platform(platform),
            trigger_type,
            schedule_date,
            0,
            publish_date_start,
            publish_date_end,
            feishu_target_json,
            1 if auto_sync else 0,
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
    platform = _collect_platform(payload.get("platform"))
    with proxy_pool.connect() as conn:
        account = _account(conn, account_id)
        proxy_pool.require_account_proxy_bound(account)
        publish_date_start, publish_date_end = _validate_publish_range(
            payload.get("publish_date_start"), payload.get("publish_date_end")
        )
        feishu_target = _validate_feishu_target(payload.get("feishu_target"))
        feishu_target_json = json.dumps(feishu_target, ensure_ascii=False, separators=(",", ":"))
        job_id = _insert_job(
            conn,
            account,
            "manual",
            publish_date_start,
            publish_date_end,
            feishu_target_json,
            session_id=int(payload.get("observation_session_id") or 0),
            auto_sync=payload.get("write_to_feishu") is not False,
            platform=platform,
        )
        now = _iso()
        conn.execute(
            """
            INSERT INTO collect_settings (account_id, feishu_target_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                feishu_target_json = excluded.feishu_target_json,
                updated_at = excluded.updated_at
            """,
            (account_id, feishu_target_json, now, now),
        )
        conn.commit()
    data = dashboard(account_id, platform)
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
        account = _account(conn, int(row["account_id"]))
        proxy_pool.require_account_proxy_bound(account)
        if str(row["status"]) not in JOB_RETRYABLE_STATUSES:
            raise ValueError("只有失败、部分失败或已取消的采集任务可以重试")
        now = _iso()
        conn.execute(
            """
            UPDATE collect_jobs
            SET status = 'queued', stage = 'retry_queued', attempt_count = 0,
                status_detail = '', next_attempt_at = '', session_id = ?, current_video_id = '',
                completed_at = '', last_error = '', updated_at = ?
            WHERE id = ?
            """,
            (int(payload.get("observation_session_id") or 0) or None, now, job_id),
        )
        conn.commit()
        account_id = int(row["account_id"])
        platform = _collect_platform(row["platform"] if "platform" in row.keys() else "tiktok")
    return dashboard(account_id, platform)


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
            "UPDATE collect_jobs SET status = 'cancelled', stage = '', status_detail = '', completed_at = ?, updated_at = ? WHERE id = ?",
            (_iso(), _iso(), job_id),
        )
        conn.commit()
        account_id = int(row["account_id"])
        platform = _collect_platform(row["platform"] if "platform" in row.keys() else "tiktok")
    return dashboard(account_id, platform)


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
    status_detail = _clean_text(values.pop("status_detail", ""), 500)
    fields = ["status = ?", "stage = ?", "status_detail = ?", "last_error = ?", "updated_at = ?"]
    params: list[Any] = [status, stage, status_detail, _clean_text(error), _iso()]
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


def _metric_number(value: Any) -> int | float | None:
    raw = _clean_text(value, 80).replace(",", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMB]?)", raw, re.I)
    if not match:
        return None
    number = float(match.group(1))
    number *= {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()]
    return int(number) if number.is_integer() else number


def _datetime_millis(value: Any) -> int | None:
    raw = _clean_text(value, 120)
    if not raw:
        return None
    if raw.isdigit() and len(raw) >= 10:
        number = int(raw)
        return number if len(raw) >= 13 else number * 1000
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, pattern)
                break
            except ValueError:
                continue
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(TIMEZONE_NAME))
    return int(parsed.timestamp() * 1000)


def _feishu_fields(target: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    account = payload.get("account") or {}
    video = payload.get("video") or {}
    time_filter = payload.get("time_filter") or {}
    overview = payload.get("overview") or {}
    engagement = payload.get("engagement") or {}
    retention = payload.get("retention") or {}
    available = {str(field.get("name") or ""): field for field in target.get("fields") or []}
    is_instagram = _collect_platform(payload.get("platform") or (payload.get("collection_job") or {}).get("platform")) == "instagram"
    username = _clean_text(account.get("username"), 200)
    values = {
        "账号名称": f"ins_{username}" if is_instagram and username else username,
        "发布时间": time_filter.get("applied") or video.get("published_at") or "",
        "24小时播放量": _metric_number(overview.get("play_count")),
        "视频完播率": "" if is_instagram else overview.get("completion_rate") or "",
        "头3秒播放率": "" if is_instagram else retention.get("0:03") or "",
        "头6秒播放率": "" if is_instagram else retention.get("0:06") or "",
        "点赞": _metric_number(engagement.get("likes")),
        "评论": _metric_number(engagement.get("comments")),
        "收藏": _metric_number(engagement.get("favorites")),
        "分享": _metric_number(engagement.get("shares")),
        "解析状态": "已解析" if payload.get("retention_complete") else "需人工确认",
    }
    mapped: dict[str, Any] = {}
    for name, value in values.items():
        definition = available.get(name)
        if not definition or value is None or value == "":
            continue
        field_type = int(definition.get("type") or 0)
        if field_type == 5:
            value = _datetime_millis(value)
        elif field_type == 1:
            value = str(value)
        if value is not None and value != "":
            mapped[name] = value
    if not mapped:
        raise ValueError("目标多维表格没有可映射的视频统计字段")
    return mapped


def _set_result_sync(result_id: int, status: str, record_id: str = "", error: str = "") -> None:
    with proxy_pool.connect() as conn:
        conn.execute(
            """
            UPDATE collect_results
            SET feishu_sync_status = ?, feishu_record_id = COALESCE(NULLIF(?, ''), feishu_record_id),
                feishu_sync_error = ?, feishu_synced_at = ?
            WHERE id = ?
            """,
            (status, record_id, _clean_text(error), _iso() if status == "synced" else "", result_id),
        )
        conn.commit()


def _is_feishu_record_not_found(exc: FeishuCapabilityError) -> bool:
    message = str(exc).lower()
    return "recordidnotfound" in message or "1254043" in message


def _sync_result_to_feishu(result_id: int, job: dict[str, Any], payload: dict[str, Any]) -> None:
    target = _job_feishu_target(job)
    try:
        fields = _feishu_fields(target, payload)
        request = {
            "appToken": target.get("appToken"),
            "wikiToken": target.get("wikiToken"),
            "tableId": target.get("tableId"),
            "fields": fields,
        }
        with proxy_pool.connect() as conn:
            current = conn.execute(
                "SELECT feishu_record_id FROM collect_results WHERE id = ?",
                (result_id,),
            ).fetchone()
            previous = conn.execute(
                """
                SELECT feishu_record_id, feishu_target_json
                FROM collect_results
                WHERE account_id = ? AND video_id = ? AND id <> ? AND feishu_record_id <> ''
                ORDER BY collected_at DESC, id DESC
                """,
                (int(job["account_id"]), _clean_text((payload.get("video") or {}).get("id"), 120), result_id),
            ).fetchall()
        record_id = _clean_text(current["feishu_record_id"], 200) if current else ""
        record_id = record_id or next(
            (
                str(row["feishu_record_id"])
                for row in previous
                if _target_key(_json_loads(row["feishu_target_json"], {})) == _target_key(target)
            ),
            "",
        )
        _set_result_sync(result_id, "syncing", record_id)
        if record_id:
            request["recordId"] = record_id
            try:
                result = _feishu_client.update_bitable_record(request)
            except FeishuCapabilityError as exc:
                if not _is_feishu_record_not_found(exc):
                    raise
                request.pop("recordId", None)
                result = _feishu_client.create_bitable_record(request)
                record_id = _clean_text(result.get("recordId"), 200)
        else:
            result = _feishu_client.create_bitable_record(request)
            record_id = _clean_text(result.get("recordId"), 200)
        _set_result_sync(result_id, "synced", record_id)
    except (FeishuCapabilityError, ValueError, TypeError) as exc:
        _set_result_sync(result_id, "failed", error=str(exc))


def _save_result(job: dict[str, Any], payload: dict[str, Any]) -> None:
    video = payload.get("video") or {}
    target_json = json.dumps(
        _job_feishu_target(job), ensure_ascii=False, separators=(",", ":")
    )
    sync_status = "pending" if job.get("auto_sync", True) else "not_synced"
    with proxy_pool.connect() as conn:
        conn.execute(
            """
            INSERT INTO collect_results (
                job_id, account_id, video_id, video_url, title, published_at,
                collected_at, retention_complete, payload_json, feishu_target_json,
                feishu_sync_status, feishu_sync_error, feishu_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')
            ON CONFLICT(job_id, video_id) DO UPDATE SET
                video_url = excluded.video_url,
                title = excluded.title,
                published_at = excluded.published_at,
                collected_at = excluded.collected_at,
                retention_complete = excluded.retention_complete,
                payload_json = excluded.payload_json,
                feishu_target_json = excluded.feishu_target_json,
                feishu_sync_status = excluded.feishu_sync_status,
                feishu_sync_error = '',
                feishu_synced_at = ''
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
                target_json,
                sync_status,
            ),
        )
        result = conn.execute(
            "SELECT id FROM collect_results WHERE job_id = ? AND video_id = ?",
            (job["id"], _clean_text(video.get("id"), 120)),
        ).fetchone()
        conn.commit()
    if result and job.get("auto_sync", True):
        _sync_result_to_feishu(int(result["id"]), job, payload)


def restore_uncollected_videos(
    job_id: str,
    sources: list[dict[str, Any]],
    reason: str = "列表发现补录",
) -> dict[str, int]:
    """Persist verified-but-uncollected videos so the task can be safely retried."""
    clean_job_id = _clean_text(job_id, 80)
    if not clean_job_id:
        raise ValueError("job_id is required")
    recovered = 0
    skipped = 0
    now = _iso()
    with proxy_pool.connect() as conn:
        job_row = conn.execute("SELECT * FROM collect_jobs WHERE id = ?", (clean_job_id,)).fetchone()
        if not job_row:
            raise ValueError("collect job not found")
        job = _job_row(job_row)
        target_json = json.dumps(_job_feishu_target(job), ensure_ascii=False, separators=(",", ":"))
        for raw_source in sources:
            video_id = _clean_text(raw_source.get("id") or raw_source.get("video_id"), 120)
            video_url = _clean_text(raw_source.get("url") or raw_source.get("video_url"), 1000)
            title = _clean_text(raw_source.get("title") or raw_source.get("title_hint"), 2000)
            published_at = _clean_text(raw_source.get("published_date") or raw_source.get("published_at"), 120)
            if not video_id or not video_url or not published_at:
                raise ValueError("待补录视频必须包含视频 ID、链接和发布日期")
            existing = conn.execute(
                "SELECT feishu_sync_status FROM collect_results WHERE job_id = ? AND video_id = ?",
                (clean_job_id, video_id),
            ).fetchone()
            if existing and str(existing["feishu_sync_status"]) != PENDING_COLLECTION_STATUS:
                skipped += 1
                continue
            payload = {
                "collection_status": PENDING_COLLECTION_STATUS,
                "collection_note": _clean_text(reason, 500),
                "reconciled_at": now,
                "video": {
                    "id": video_id,
                    "url": video_url,
                    "title": title,
                    "published_at": published_at,
                },
                "time_filter": {
                    "requested": {
                        "start": job["publish_date_start"],
                        "end": job["publish_date_end"],
                    },
                    "applied": published_at,
                },
            }
            conn.execute(
                """
                INSERT INTO collect_results (
                    job_id, account_id, video_id, video_url, title, published_at,
                    collected_at, retention_complete, payload_json, feishu_target_json,
                    feishu_sync_status, feishu_sync_error, feishu_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', 0, ?, ?, ?, '', '')
                ON CONFLICT(job_id, video_id) DO UPDATE SET
                    video_url = excluded.video_url,
                    title = excluded.title,
                    published_at = excluded.published_at,
                    collected_at = '',
                    retention_complete = 0,
                    payload_json = excluded.payload_json,
                    feishu_target_json = excluded.feishu_target_json,
                    feishu_sync_status = excluded.feishu_sync_status,
                    feishu_sync_error = '',
                    feishu_synced_at = ''
                WHERE collect_results.feishu_sync_status = 'not_collected'
                """,
                (
                    clean_job_id,
                    job["account_id"],
                    video_id,
                    video_url,
                    title,
                    published_at,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    target_json,
                    PENDING_COLLECTION_STATUS,
                ),
            )
            recovered += 1
        completed = int(conn.execute(
            "SELECT COUNT(*) FROM collect_results WHERE job_id = ? AND feishu_sync_status != ?",
            (clean_job_id, PENDING_COLLECTION_STATUS),
        ).fetchone()[0])
        pending = int(conn.execute(
            "SELECT COUNT(*) FROM collect_results WHERE job_id = ? AND feishu_sync_status = ?",
            (clean_job_id, PENDING_COLLECTION_STATUS),
        ).fetchone()[0])
        total = completed + pending
        detail = f"{_clean_text(reason, 120)}：已采集 {completed} 条，待补采 {pending} 条"
        status = "partial" if pending else "complete"
        conn.execute(
            """
            UPDATE collect_jobs
            SET status = ?, stage = ?, status_detail = ?, last_error = '',
                total_videos = ?, completed_videos = ?, failed_videos = 0,
                current_video_id = '', completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, "recovery_pending" if pending else "complete", detail, total, completed, now, now, clean_job_id),
        )
        conn.commit()
    return {"recovered": recovered, "skipped": skipped, "total": total, "completed": completed, "pending": pending}


def _scan_job_video_list(job: dict[str, Any], session: dict[str, Any]) -> list[dict[str, str]]:
    from playwright.sync_api import sync_playwright

    log_dir = LOG_ROOT / job["id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{session['debug_port']}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        target = "https://www.tiktok.com/tiktokstudio/content?lang=en"
        navigate_with_retries(page, target, label="TikTok 视频列表重扫")
        _skip_onboarding(page)
        try:
            list_state = wait_for_page_state(
                page,
                label="TikTok 视频列表重扫",
                ready=lambda: bool(_discover_links_on_page(page)),
                empty=lambda: bool(_first_visible([
                    page.get_by_text(re.compile(r"no posts|no videos|haven't posted|暂无(?:作品|视频)|没有(?:作品|视频)", re.I)),
                ])),
                allow_ocr_empty=True,
                timeout_seconds=75,
                reload_attempts=1,
                retry_action=lambda: navigate_with_retries(page, target, label="TikTok 视频列表重扫"),
                diagnostic_dir=log_dir,
                diagnostic_step="video-list-rescan",
            )
        except BrowserPageBlocked as exc:
            raise AccountReviewRequired(str(exc)) from exc
        _assert_account_ready(page)
        if list_state.state == "empty":
            raise RuntimeError("TikTok Studio 视频列表已加载，但账号当前没有视频")
        return _discover_video_links(
            page,
            job["publish_date_start"],
            job["publish_date_end"],
            diagnostic_path=log_dir / "video-list-discovery.json",
        )


def _run_discovery_rescan(job_id: str) -> None:
    session_id = 0
    reused_observation = False
    job: dict[str, Any] | None = None
    try:
        job = _load_job(job_id)
        if not job:
            return
        original_status = str(job["status"])
        _set_job(job_id, original_status, "rescan_discovery", "正在按新滚动逻辑重新扫描视频列表")
        requested_session_id = int(job.get("session_id") or 0)
        session = proxy_pool.claim_observation_session_for_job(job["account_id"], requested_session_id, job_id)
        if session is not None:
            reused_observation = True
        else:
            session = proxy_pool.start_automation_session(job["account_id"], f"rescan_{job_id}")["session"]
        session_id = int(session["id"])
        sources = _scan_job_video_list(job, session)
        completed_ids = _completed_video_ids(job_id)
        missing = [source for source in sources if source["id"] not in completed_ids]
        restore_uncollected_videos(job_id, missing, "新列表扫描对账")
    except Exception as exc:
        if job:
            _set_job(
                job_id,
                str(job["status"]),
                "rescan_failed",
                f"列表重扫失败：{_clean_text(exc, 400)}",
                session_id=session_id or None,
            )
    finally:
        if session_id and reused_observation:
            try:
                proxy_pool.release_observation_session_job(session_id, job_id)
            except Exception:
                pass
        elif session_id:
            try:
                proxy_pool.finish_automation_session(session_id, "列表重扫结束")
            except Exception:
                pass
        with _worker_lock:
            _active_rescans.discard(job_id)


def start_discovery_rescans(payload: dict[str, Any]) -> dict[str, Any]:
    requested = payload.get("job_ids") or []
    if isinstance(requested, str):
        requested = [requested]
    job_ids = [_clean_text(value, 80) for value in requested if _clean_text(value, 80)]
    if not job_ids:
        raise ValueError("job_ids is required")
    queued: list[str] = []
    with _worker_lock:
        for job_id in dict.fromkeys(job_ids):
            if job_id in _active_rescans:
                continue
            _active_rescans.add(job_id)
            queued.append(job_id)

    def run_batch() -> None:
        for job_id in queued:
            _run_discovery_rescan(job_id)

    threading.Thread(target=run_batch, daemon=True, name="tiktok-collect-rescan").start()
    return {"queued": len(queued), "job_ids": queued}


def retry_failed_feishu_sync(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = _clean_text(payload.get("job_id"), 80)
    account_id = int(payload.get("account_id") or 0)
    raw_result_ids = payload.get("result_ids") or []
    if not isinstance(raw_result_ids, list):
        raw_result_ids = [raw_result_ids]
    if payload.get("result_id"):
        raw_result_ids = [payload.get("result_id"), *raw_result_ids]
    try:
        result_ids = sorted({int(value) for value in raw_result_ids if int(value) > 0})
    except (TypeError, ValueError) as exc:
        raise ValueError("result_id must be a positive integer") from exc
    if not job_id and not account_id and not result_ids:
        raise ValueError("job_id, account_id or result_id is required")
    clauses = ["r.feishu_sync_status IN ('failed', 'not_synced')"]
    params: list[Any] = []
    if job_id:
        clauses.append("r.job_id = ?")
        params.append(job_id)
    if account_id:
        clauses.append("r.account_id = ?")
        params.append(account_id)
    if result_ids:
        clauses.append(f"r.id IN ({','.join('?' for _ in result_ids)})")
        params.extend(result_ids)
    with proxy_pool.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT r.id, r.account_id, r.payload_json, r.feishu_target_json,
                   j.feishu_target_json AS job_target_json
            FROM collect_results r
            JOIN collect_jobs j ON j.id = r.job_id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.id
            LIMIT 200
            """,
            params,
        ).fetchall()
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        target = _json_loads(row["job_target_json"], {})
        if not isinstance(target, dict) or not _target_key(target):
            target = _json_loads(row["feishu_target_json"], {})
        if not isinstance(target, dict):
            target = {}
        result_id = int(row["id"])
        with proxy_pool.connect() as conn:
            conn.execute(
                """
                UPDATE collect_results
                SET feishu_target_json = ?, feishu_sync_status = 'pending',
                    feishu_sync_error = '', feishu_synced_at = ''
                WHERE id = ?
                """,
                (json.dumps(target, ensure_ascii=False, separators=(",", ":")), result_id),
            )
            conn.commit()
        _sync_result_to_feishu(
            result_id,
            {"account_id": int(row["account_id"]), "feishu_target": target},
            _json_loads(row["payload_json"], {}),
        )
        with proxy_pool.connect() as conn:
            updated = conn.execute(
                """
                SELECT id, feishu_sync_status, feishu_record_id,
                       feishu_sync_error, feishu_synced_at
                FROM collect_results WHERE id = ?
                """,
                (result_id,),
            ).fetchone()
        outcomes.append(dict(updated))
    return {
        "attempted": len(outcomes),
        "synced": sum(1 for item in outcomes if item["feishu_sync_status"] == "synced"),
        "failed": sum(1 for item in outcomes if item["feishu_sync_status"] == "failed"),
        "results": outcomes,
    }


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


def _video_id_published_date(video_id: str) -> str:
    """Recover TikTok's publish date from the timestamp embedded in a video ID."""
    try:
        published = datetime.fromtimestamp(int(video_id) >> 32, timezone.utc).astimezone(
            ZoneInfo(TIMEZONE_NAME)
        )
    except (OSError, OverflowError, ValueError):
        return ""
    latest_valid = _utc_now().astimezone(ZoneInfo(TIMEZONE_NAME)) + timedelta(days=1)
    if published.year < 2016 or published > latest_valid:
        return ""
    return published.date().isoformat()


_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _source_published_date(hint: str, range_start: str, range_end: str) -> str:
    text = " ".join(_lines(hint))
    start = datetime.strptime(range_start, "%Y-%m-%d").date()
    end = datetime.strptime(range_end, "%Y-%m-%d").date()

    match = re.search(r"\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date().isoformat()
        except ValueError:
            return ""

    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b", text)
    month_name = re.search(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(20\d{2}))?\b",
        text,
        re.I,
    )
    if numeric:
        month, day = int(numeric.group(1)), int(numeric.group(2))
        explicit_year = int(numeric.group(3)) if numeric.group(3) else 0
    elif month_name:
        month = _MONTH_NUMBERS[month_name.group(1)[:3].lower()]
        day = int(month_name.group(2))
        explicit_year = int(month_name.group(3)) if month_name.group(3) else 0
    else:
        return ""

    years = [explicit_year] if explicit_year else sorted({start.year, end.year})
    candidates = []
    for year in years:
        try:
            candidates.append(datetime(year, month, day).date())
        except ValueError:
            continue
    if not candidates:
        return ""
    chosen = min(
        candidates,
        key=lambda value: 0 if start <= value <= end else min(abs((value - start).days), abs((value - end).days)),
    )
    return chosen.isoformat()


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


def _write_discovery_snapshot(path: Path | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _list_scroll(page: Any, step_px: int, reset: bool = False) -> dict[str, Any]:
    return page.evaluate(
        """({stepPx, reset}) => {
            const links = [...document.querySelectorAll(
                "a[href*='/tiktokstudio/analytics/'], a[href*='/video/']"
            )].filter(link => /\/(?:tiktokstudio\/analytics|video)\/\d{10,}/.test(link.getAttribute("href") || ""));
            let root = document.scrollingElement;
            const candidates = new Map();
            for (const link of links) {
                const videoId = ((link.getAttribute("href") || "").match(/\/(?:tiktokstudio\/analytics|video)\/(\d{10,})/) || [])[1];
                let node = link.parentElement;
                while (node && node !== document.documentElement) {
                    if (node.scrollHeight > node.clientHeight + 20) {
                        const candidate = candidates.get(node) || { node, videoIds: new Set() };
                        if (videoId) candidate.videoIds.add(videoId);
                        candidates.set(node, candidate);
                    }
                    node = node.parentElement;
                }
            }
            const ranked = [...candidates.values()].sort((left, right) =>
                right.videoIds.size - left.videoIds.size ||
                (right.node.scrollHeight - right.node.clientHeight) - (left.node.scrollHeight - left.node.clientHeight)
            );
            if (ranked.length) root = ranked[0].node;
            const beforePx = Number(root?.scrollTop || 0);
            if (root) {
                root.scrollTop = reset ? 0 : Math.min(
                    root.scrollHeight,
                    root.scrollTop + stepPx
                );
            }
            const afterPx = Number(root?.scrollTop || 0);
            const rootName = root === document.scrollingElement
                ? "document"
                : `${root?.tagName || "unknown"}.${String(root?.className || "").split(/\\s+/).filter(Boolean).slice(0, 3).join(".")}`;
            return {
                root: rootName,
                before_px: beforePx,
                after_px: afterPx,
                delta_px: afterPx - beforePx,
                scroll_height_px: Number(root?.scrollHeight || 0),
                client_height_px: Number(root?.clientHeight || 0),
                at_end: Boolean(root && afterPx + root.clientHeight >= root.scrollHeight - 2),
                stalled: Boolean(root && root.scrollHeight > root.clientHeight + 20 && afterPx === beforePx),
                candidate_video_links: ranked.length ? ranked[0].videoIds.size : 0,
            };
        }""",
        {"stepPx": step_px, "reset": reset},
    )


def _discover_video_links(
    page: Any,
    publish_date_start: str,
    publish_date_end: str,
    diagnostic_path: Path | None = None,
) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    scroll_events: list[dict[str, Any]] = []
    stop_reason = "scan_limit"

    def collect() -> None:
        for row in _discover_links_on_page(page):
            row["published_date"] = _source_published_date(
                row.get("title_hint", ""), publish_date_start, publish_date_end
            ) or _video_id_published_date(row["id"])
            existing = found.get(row["id"])
            if not existing or (not existing.get("published_date") and row["published_date"]):
                found[row["id"]] = row

    def matching() -> list[dict[str, str]]:
        return [
            row for row in found.values()
            if publish_date_start <= row.get("published_date", "") <= publish_date_end
        ]

    current_path = urlparse(page.url).path
    if not (current_path.startswith("/tiktokstudio/content") or current_path.startswith("/tiktokstudio/manage")):
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

    try:
        reset_event = _list_scroll(page, LIST_SCROLL_STEP_PX, reset=True)
        reset_event.update({"round": 0, "action": "reset", "discovered_videos": 0})
        scroll_events.append(reset_event)
        page.wait_for_timeout(LIST_SCROLL_WAIT_MS)

        unchanged_rounds = 0
        for round_index in range(1, LIST_SCROLL_MAX_ROUNDS + 1):
            before = len(found)
            collect()
            unchanged_rounds = unchanged_rounds + 1 if len(found) == before else 0
            scroll_event = _list_scroll(page, LIST_SCROLL_STEP_PX)
            scroll_event.update({
                "round": round_index,
                "action": "scroll",
                "discovered_videos": len(found),
                "unchanged_rounds": unchanged_rounds,
            })
            scroll_events.append(scroll_event)
            if scroll_event["at_end"] and unchanged_rounds >= 6:
                stop_reason = "list_end_reached"
                break
            if scroll_event["stalled"] and unchanged_rounds >= 6:
                stop_reason = "scroll_stalled"
                break
            page.wait_for_timeout(LIST_SCROLL_WAIT_MS)
        else:
            raise RuntimeError(
                f"TikTok Studio 视频列表扫描超过 {LIST_SCROLL_MAX_ROUNDS} 轮，未到达列表底部"
            )
        collect()
        return matching()
    finally:
        _write_discovery_snapshot(
            diagnostic_path,
            {
                "captured_at": _iso(),
                "requested_publish_date_start": publish_date_start,
                "requested_publish_date_end": publish_date_end,
                "scroll_step_px": LIST_SCROLL_STEP_PX,
                "scroll_wait_ms": LIST_SCROLL_WAIT_MS,
                "stop_reason": stop_reason,
                "discovered_video_count": len(found),
                "matched_video_count": len(matching()),
                "scroll_events": scroll_events,
                "videos": sorted(found.values(), key=lambda row: (row.get("published_date", ""), row["id"])),
            },
        )


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


def _wait_for_detail_sections(
    page: Any,
) -> tuple[list[str], dict[str, str], dict[str, str], str, str]:
    """Wait for TikTok's lazy-loaded lower analytics sections."""
    deadline = time.monotonic() + DETAIL_SECTIONS_TIMEOUT_SECONDS
    latest_lines: list[str] = []
    latest_search: dict[str, str] = {}
    traffic_reason = ""
    search_reason = ""
    heading_pattern = re.compile(r"^(?:Traffic source|Traffic sources|流量来源)$", re.I)
    while time.monotonic() < deadline:
        body = page.locator("body").inner_text(timeout=15000)
        latest_lines = _lines(body)
        traffic = _percent_section(
            latest_lines,
            ["Traffic source", "Traffic sources", "流量来源"],
            ["Search queries", "搜索查询"],
        )
        latest_search = _percent_section(
            latest_lines,
            ["Search queries", "搜索查询"],
            ["Viewer types", "Audience", "观众"],
        )
        if traffic:
            if not latest_search and re.search(
                r"search quer(?:y|ies).*(?:low traffic|enough traffic)|搜索(?:查询|词).*(?:流量不足|足够流量)",
                body,
                re.I | re.S,
            ):
                search_reason = "搜索词流量不足，TikTok 暂未提供查询明细"
            return latest_lines, traffic, latest_search, "", search_reason
        if re.search(
            r"data will show when video views reach\s*100|(?:播放量|视频观看量).*(?:达到|满)\s*100.*(?:显示|提供)",
            body,
            re.I | re.S,
        ):
            traffic_reason = "播放量未达到 100，TikTok 暂未提供流量来源"
            if re.search(
                r"search quer(?:y|ies).*(?:low traffic|enough traffic)|搜索(?:查询|词).*(?:流量不足|足够流量)",
                body,
                re.I | re.S,
            ):
                search_reason = "搜索词流量不足，TikTok 暂未提供查询明细"
            return latest_lines, {}, latest_search, traffic_reason, search_reason
        heading = _first_visible([page.get_by_text(heading_pattern)])
        if heading:
            try:
                heading.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                page.mouse.wheel(0, 1200)
        else:
            page.mouse.wheel(0, 1200)
        page.wait_for_timeout(800)
    return latest_lines, {}, latest_search, traffic_reason, search_reason


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
    title, detail_published_at = _title_and_date(lines, source.get("title_hint", ""))
    published_at = source.get("published_date", "") or detail_published_at
    retention, retention_complete, missing, retention_reason = _sample_retention(
        page, _duration_seconds(source.get("title_hint", ""))
    )
    detail_lines, traffic_sources, search_queries, traffic_reason, search_reason = _wait_for_detail_sections(page)
    if not traffic_sources and not traffic_reason:
        page.screenshot(path=str(log_dir / f"{source['id']}-missing-traffic-sources.png"), full_page=True)
        raise RuntimeError("视频分析页已加载概览，但流量来源区域在等待后仍未完整渲染")
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
        "time_filter": {
            "requested": {
                "publish_date_start": job["publish_date_start"],
                "publish_date_end": job["publish_date_end"],
                "timezone": TIMEZONE_NAME,
            },
            "applied": source.get("published_date", ""),
            "scope": "video_publish_date",
            "applied_successfully": True,
        },
        "overview": overview,
        "engagement": _engagement(page, detail_lines or lines, overview),
        "retention": retention,
        "retention_complete": retention_complete,
        "missing_retention_seconds": missing,
        "retention_reason": retention_reason,
        "traffic_sources": traffic_sources,
        "traffic_sources_available": bool(traffic_sources),
        "traffic_sources_reason": traffic_reason,
        "search_queries": search_queries,
        "search_queries_available": bool(search_queries),
        "search_queries_reason": search_reason,
        "data_complete": bool(retention_complete and (traffic_sources or traffic_reason)),
        "updated_at": _value_after_label(detail_lines or lines, ["Updated", "Last updated", "更新时间"]),
        "collected_at": _iso(),
    }
    page.screenshot(path=str(log_dir / f"{source['id']}-collected.png"), full_page=True)
    return payload


def _load_job(job_id: str) -> dict[str, Any] | None:
    with proxy_pool.connect() as conn:
        row = conn.execute(
            """
            SELECT j.*, a.username, a.last_checked_ip, a.profile_json
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
        job["profile"] = _json_loads(row["profile_json"], {})
        return job


def _completed_video_ids(job_id: str) -> set[str]:
    with proxy_pool.connect() as conn:
        return {
            str(row["video_id"])
            for row in conn.execute(
                "SELECT video_id FROM collect_results WHERE job_id = ? AND feishu_sync_status != ?",
                (job_id, PENDING_COLLECTION_STATUS),
            ).fetchall()
        }


def _pending_video_sources(job_id: str) -> list[dict[str, str]]:
    with proxy_pool.connect() as conn:
        rows = conn.execute(
            """
            SELECT video_id, video_url, title, published_at
            FROM collect_results
            WHERE job_id = ? AND feishu_sync_status = ?
            ORDER BY published_at, video_id
            """,
            (job_id, PENDING_COLLECTION_STATUS),
        ).fetchall()
    return [
        {
            "id": str(row["video_id"]),
            "url": str(row["video_url"]),
            "title_hint": str(row["title"]),
            "published_date": str(row["published_at"]),
        }
        for row in rows
    ]


def _instagram_reels_url(job: dict[str, Any]) -> str:
    profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
    instagram = profile.get("instagram") if isinstance(profile.get("instagram"), dict) else {}
    value = _clean_text(instagram.get("reels_url"), 1000)
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc.lower().endswith("instagram.com") and re.fullmatch(r"/[A-Za-z0-9._]+/reels/?", parsed.path):
        return f"https://www.instagram.com{parsed.path.rstrip('/')}/"
    raise RuntimeError("Instagram Reels 采集入口尚未初始化，请重新保存该账号以定位头像主页")


def _instagram_relative_date(text: str, anchor: datetime) -> tuple[str, str]:
    """Convert Instagram's relative post time using one fixed job-start clock."""
    # Instagram's body text starts with its navigation and the reel caption.  The
    # publication label (for example, "4 hours ago") is frequently well after
    # the first 500 characters, so do not truncate it out before parsing.
    raw = _clean_text(text, 12000)
    normalized = raw.lower().replace("\u00a0", " ")
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9,
        "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    match = re.search(
        r"\b(" + "|".join(month_names) + r")\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
        normalized,
    )
    if match:
        month = month_names[match.group(1)]
        day = int(match.group(2))
        year = int(match.group(3) or anchor.year)
        try:
            value = datetime(year, month, day, tzinfo=anchor.tzinfo)
        except ValueError:
            return "", ""
        # Instagram omits the year for recent posts.  A resulting future date is
        # necessarily from the previous calendar year.
        if not match.group(3) and value.date() > anchor.date():
            value = value.replace(year=year - 1)
        return value.date().isoformat(), match.group(0)
    match = re.search(r"\b(\d+)\s*(?:h|hr|hrs|hour|hours)\s*(?:ago)?\b", normalized)
    if match:
        return (anchor - timedelta(hours=int(match.group(1)))).date().isoformat(), match.group(0)
    match = re.search(r"\b(\d+)\s*(?:m|min|mins|minute|minutes)\s*(?:ago)?\b", normalized)
    if match:
        return (anchor - timedelta(minutes=int(match.group(1)))).date().isoformat(), match.group(0)
    match = re.search(r"\b(\d+)\s*(?:d|day|days)\s*(?:ago)?\b", normalized)
    if match:
        return (anchor - timedelta(days=int(match.group(1)))).date().isoformat(), match.group(0)
    match = re.search(r"\b(\d+)\s*(?:w|week|weeks)\s*(?:ago)?\b", normalized)
    if match:
        return (anchor - timedelta(weeks=int(match.group(1)))).date().isoformat(), match.group(0)
    return "", ""


def _instagram_reel_sources(page: Any) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    links = page.locator("a[href*='/reel/']")
    for index in range(min(links.count(), 160)):
        item = links.nth(index)
        try:
            href = str(item.get_attribute("href") or "")
            parsed = urlparse(href)
            path = parsed.path if parsed.scheme else href.split("?", 1)[0]
            # Profile Reels grids use /<username>/reel/<shortcode>/ while
            # individual pages use /reel/<shortcode>/.  Keep the source path
            # so scrolling can locate the grid tile before opening its detail.
            match = re.fullmatch(r"/(?:[A-Za-z0-9._]+/)?reel/([A-Za-z0-9_-]+)/?", path)
            if not match:
                continue
            shortcode = match.group(1)
            found.setdefault(shortcode, {"id": f"ins_{shortcode}", "shortcode": shortcode, "href": path})
        except Exception:
            continue
    return list(found.values())


def _instagram_collect_payload(page: Any, job: dict[str, Any], source: dict[str, str], published_date: str, relative_time: str) -> dict[str, Any]:
    insight = _first_visible([
        page.get_by_text(re.compile(r"^view insights$", re.I)),
        page.get_by_role("button", name=re.compile(r"view insights", re.I)),
        page.get_by_role("link", name=re.compile(r"view insights", re.I)),
    ])
    if not insight:
        raise RuntimeError("该 Reels 未显示 View insights，无法读取账号洞察数据")
    insight.click(timeout=7000)
    page.wait_for_timeout(1500)
    body = page.locator("body").inner_text(timeout=15000)
    lines = _lines(body)
    views = _value_after_label(lines, ["Views", "观看次数", "播放次数"])
    likes = _value_after_label(lines, ["Likes", "赞", "点赞"])
    comments = _value_after_label(lines, ["Comments", "评论"])
    shares = _value_after_label(lines, ["Shares", "分享"])
    saves = _value_after_label(lines, ["Saves", "收藏"])
    if not any((views, likes, comments, shares, saves)):
        raise RuntimeError("View insights 已打开，但未识别到可采集指标")
    canonical_url = page.url.split("?", 1)[0].split("#", 1)[0]
    if "/insights/" in urlparse(canonical_url).path:
        canonical_url = f"https://www.instagram.com/reel/{source['shortcode']}/"
    title = ""
    try:
        title = _clean_text(page.locator("meta[property='og:description']").get_attribute("content"), 2000)
    except Exception:
        pass
    return {
        "platform": "instagram",
        "account": {
            "id": job["account_id"],
            "username": job.get("instagram_username") or job["username"],
            "proxy_profile_id": job["proxy_profile_id"],
            "observed_ip": job["observed_ip"],
            "browser_session_id": job["session_id"],
        },
        "collection_job": {"job_id": job["id"], "trigger_type": job["trigger_type"], "platform": "instagram"},
        "video": {"id": source["id"], "title": title, "published_at": published_date, "url": canonical_url},
        "time_filter": {
            "requested": {"publish_date_start": job["publish_date_start"], "publish_date_end": job["publish_date_end"], "timezone": TIMEZONE_NAME},
            "applied": published_date,
            "source_relative_time": relative_time,
            "scope": "instagram_reel_publish_date",
            "applied_successfully": True,
        },
        "overview": {"play_count": views, "completion_rate": ""},
        "engagement": {"likes": likes, "comments": comments, "favorites": saves, "shares": shares},
        "retention": {},
        "retention_complete": True,
        "data_complete": True,
        "collected_at": _iso(),
    }


def _execute_instagram_browser(job: dict[str, Any], session: dict[str, Any]) -> tuple[int, int, int]:
    from playwright.sync_api import sync_playwright

    log_dir = LOG_ROOT / job["id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
    instagram = profile.get("instagram") if isinstance(profile.get("instagram"), dict) else {}
    try:
        reels_url = _instagram_reels_url(job)
    except RuntimeError:
        # Binding/login success performs the avatar click once and persists the
        # Reels entry.  A collection run only needs that saved entry; retry the
        # bootstrap solely for legacy accounts where it is absent.
        bootstrap = proxy_pool.bootstrap_instagram_profile(int(job["account_id"]), int(session["id"]))
        if not bootstrap.get("configured"):
            raise RuntimeError(str(bootstrap.get("reason") or "Instagram 账号主页初始化失败"))
        refreshed = _load_job(job["id"])
        if refreshed:
            job.update(refreshed)
        profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
        instagram = profile.get("instagram") if isinstance(profile.get("instagram"), dict) else {}
        reels_url = _instagram_reels_url(job)
    job["instagram_username"] = _clean_text(instagram.get("username"), 200)
    anchor = _utc_now().astimezone(ZoneInfo(TIMEZONE_NAME))
    seen: set[str] = set()
    completed_ids = _completed_video_ids(job["id"])
    completed = len(completed_ids)
    failed = 0
    matched = 0
    unparsed = 0
    stop_scan = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{session['debug_port']}")
        context = browser.contexts[0]
        list_page = context.pages[0] if context.pages else context.new_page()
        _set_job(
            job["id"], "collecting", "load_instagram_reels",
            session_id=session["id"],
            status_detail="正在打开已保存的 Instagram Reels 列表…",
        )
        list_page.goto(reels_url, wait_until="domcontentloaded", timeout=60000)
        try:
            list_page.locator("a[href*='/reel/']").first.wait_for(state="visible", timeout=15000)
        except Exception:
            # A zero-video profile remains a valid scan result; the loop below
            # will finish it with a clear summary instead of treating it as a
            # page-load failure.
            pass
        collect_page = context.new_page()
        try:
            for round_index in range(LIST_SCROLL_MAX_ROUNDS):
                sources = _instagram_reel_sources(list_page)
                if sources:
                    _set_job(
                        job["id"], "collecting", "scan_instagram_reels",
                        session_id=session["id"],
                        status_detail=f"正在核对 Reels 发布时间，已发现 {len(seen | {item['id'] for item in sources})} 条链接。",
                    )
                for source in sources:
                    if source["id"] in seen:
                        continue
                    seen.add(source["id"])
                    try:
                        list_page.locator(f'a[href="{source["href"]}"]').first.scroll_into_view_if_needed(timeout=5000)
                        collect_page.goto(f"https://www.instagram.com{source['href']}", wait_until="domcontentloaded", timeout=30000)
                        collect_page.wait_for_timeout(700)
                        body = collect_page.locator("body").inner_text(timeout=15000)
                        published_date, relative_time = _instagram_relative_date(body, anchor)
                        if not published_date:
                            unparsed += 1
                            continue
                        if published_date < job["publish_date_start"]:
                            # Reels are scanned newest to oldest.  Once the
                            # first resolved date falls below the requested
                            # range, every following item is out of range.
                            stop_scan = True
                            break
                        if published_date > job["publish_date_end"]:
                            continue
                        matched += 1
                        _set_job(job["id"], "collecting", "collect_instagram_reel", session_id=session["id"], total_videos=matched, completed_videos=completed, failed_videos=failed, current_video_id=source["id"])
                        if source["id"] not in completed_ids:
                            payload = _instagram_collect_payload(collect_page, job, source, published_date, relative_time)
                            _save_result(job, payload)
                            completed += 1
                            completed_ids.add(source["id"])
                    except Exception as exc:
                        failed += 1
                        _record_error(job, source["id"], f"https://www.instagram.com{source['href']}", "collect_instagram_reel", exc)
                if stop_scan:
                    break
                scroll = list_page.evaluate("""() => {
                    const root = document.scrollingElement || document.documentElement;
                    const before = root.scrollTop;
                    root.scrollBy(0, Math.max(520, Math.round(window.innerHeight * .82)));
                    return {before, after: root.scrollTop, end: root.scrollTop + root.clientHeight >= root.scrollHeight - 4};
                }""")
                list_page.wait_for_timeout(LIST_SCROLL_WAIT_MS)
                if bool(scroll.get("end")) and round_index >= 4:
                    break
        finally:
            try:
                collect_page.close()
            except Exception:
                pass
    if matched:
        job["_execution_status_detail"] = f"已扫描 {len(seen)} 条 Reels，命中 {matched} 条目标日期视频。"
    elif seen:
        suffix = f"，其中 {unparsed} 条未识别到发布时间" if unparsed else ""
        job["_execution_status_detail"] = f"已扫描 {len(seen)} 条 Reels，未找到 {job['publish_date_start']} 的视频{suffix}。"
    else:
        job["_execution_status_detail"] = "Reels 页面未发现可采集的视频链接。"
    return matched, completed, failed


def _execute_browser(job: dict[str, Any], session: dict[str, Any]) -> tuple[int, int, int]:
    if _collect_platform(job.get("platform")) == "instagram":
        return _execute_instagram_browser(job, session)
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
        def page_status(event: dict[str, Any]) -> None:
            _set_job(
                job["id"],
                "preparing",
                "loading_video_list",
                session_id=session["id"],
                status_detail=event.get("message", ""),
            )

        target = "https://www.tiktok.com/tiktokstudio/content?lang=en"
        navigate_with_retries(page, target, label="TikTok 视频列表", on_status=page_status)
        _skip_onboarding(page)
        try:
            list_state = wait_for_page_state(
                page,
                label="TikTok 视频列表",
                ready=lambda: bool(_discover_links_on_page(page)),
                empty=lambda: bool(_first_visible([
                    page.get_by_text(re.compile(r"no posts|no videos|haven't posted|暂无(?:作品|视频)|没有(?:作品|视频)", re.I)),
                ])),
                allow_ocr_empty=True,
                on_status=page_status,
                timeout_seconds=75,
                reload_attempts=1,
                retry_action=lambda: navigate_with_retries(
                    page, target, label="TikTok 视频列表", on_status=page_status
                ),
                diagnostic_dir=log_dir,
                diagnostic_step="video-list",
            )
        except BrowserPageBlocked as exc:
            raise AccountReviewRequired(str(exc)) from exc
        _assert_account_ready(page)
        if list_state.state == "empty":
            raise RuntimeError("TikTok Studio 视频列表已加载，但账号当前没有视频")
        links = _discover_video_links(
            page,
            job["publish_date_start"],
            job["publish_date_end"],
            diagnostic_path=log_dir / "video-list-discovery.json",
        )
        known_links = {source["id"] for source in links}
        for source in _pending_video_sources(job["id"]):
            if source["id"] not in known_links:
                links.append(source)
                known_links.add(source["id"])
        if not links:
            page.screenshot(path=str(log_dir / "no-video-links.png"), full_page=True)
            raise RuntimeError(
                "TikTok Studio 没有发现发布日期位于 "
                f"{job['publish_date_start']} 至 {job['publish_date_end']} 的视频"
            )
        total_videos = len(known_links | completed_ids)
        _set_job(
            job["id"],
            "collecting",
            "video_list_ready",
            session_id=session["id"],
            total_videos=total_videos,
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
                total_videos=total_videos,
                completed_videos=completed,
                failed_videos=failed,
                current_video_id=source["id"],
            )
            job["session_id"] = int(session["id"])
            collect_page = context.new_page()
            try:
                # Keep the Studio content list on the observation page. The
                # individual analytics page is an isolated temporary tab.
                payload = _collect_video(collect_page, job, source, log_dir)
                _save_result(job, payload)
                completed += 1
                completed_ids.add(source["id"])
            except AccountReviewRequired:
                raise
            except Exception as exc:
                failed += 1
                _record_error(job, source["id"], source["url"], "collect_video", exc)
            finally:
                try:
                    collect_page.close()
                except Exception:
                    pass
            _set_job(
                job["id"],
                "collecting",
                "collect_video",
                session_id=session["id"],
                total_videos=total_videos,
                completed_videos=completed,
                failed_videos=failed,
                current_video_id="",
            )
    return total_videos, completed, failed


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


def _delay_for_proxy(job_id: str, job: dict[str, Any], error: Exception | str) -> None:
    message = str(error)
    next_attempt_at = _iso(_utc_now() + timedelta(seconds=proxy_pool.PROXY_QUEUE_RECHECK_SECONDS))
    _set_job(
        job_id,
        "delayed",
        "waiting_proxy",
        message,
        session_id=None,
        completed_at="",
        next_attempt_at=next_attempt_at,
    )
    _update_account(int(job["account_id"]), error=message)
    proxy_pool.schedule_proxy_recheck_for_pending_job(int(job["proxy_profile_id"]), message)


def _delay_for_page(job_id: str, job: dict[str, Any], error: Exception | str) -> None:
    message = str(error)
    _set_job(
        job_id,
        "delayed",
        "waiting_page",
        message,
        session_id=None,
        completed_at="",
        next_attempt_at=_iso(_utc_now() + timedelta(seconds=60)),
        status_detail="页面加载超时，60 秒后自动重试",
    )
    _update_account(int(job["account_id"]), error=message)


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
            session = proxy_pool.start_automation_session(
                job["account_id"], job_id, start_platform=_collect_platform(job.get("platform"))
            )["session"]
        session_id = int(session["id"])
        _set_job(job_id, "preparing", "browser_ready", session_id=session_id, started_at=_iso())
        total, completed, failed = _execute_browser(job, session)
        current = _load_job(job_id)
        if current and current.get("status") == "cancelled":
            return
        missing = max(0, total - completed)
        status = "complete" if failed == 0 and missing == 0 else ("partial" if completed else "failed")
        message = "" if status == "complete" else "；".join(
            part for part in (
                f"{failed} 个视频采集失败" if failed else "",
                f"{missing} 个视频待补采" if missing else "",
            ) if part
        )
        _set_job(
            job_id,
            status,
            "complete" if status == "complete" else "recovery_pending",
            message,
            session_id=session_id,
            total_videos=total,
            completed_videos=completed,
            failed_videos=failed,
            current_video_id="",
            completed_at=_iso(),
            status_detail=_clean_text(job.get("_execution_status_detail"), 500),
        )
        _update_account(job["account_id"], collected_at=_iso() if completed else "", error=message)
    except Exception as exc:
        message = str(exc)
        if "槽位已满" in message or "已经处于唤醒状态" in message:
            _set_job(job_id, "delayed", "waiting_slot", message, next_attempt_at=_iso(_utc_now() + timedelta(seconds=30)))
        elif 'job' in locals() and proxy_pool.is_retryable_proxy_error(message):
            _delay_for_proxy(job_id, job, message)
        elif 'job' in locals() and isinstance(exc, (BrowserPageTimeout, BrowserPageLoadError)) and int(job.get("attempt_count") or 0) < 3:
            _delay_for_page(job_id, job, message)
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
    local_day = local_now.date()
    local_date = local_day.isoformat()
    local_time = local_now.strftime("%H:%M")
    with proxy_pool.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = conn.execute(
            """
            SELECT s.*, a.proxy_profile_id, a.deleted_at
            FROM collect_settings s JOIN tiktok_accounts a ON a.id = s.account_id
            WHERE s.enabled = 1 AND a.proxy_bound = 1
            """
        ).fetchall()
        for setting in settings:
            if setting["deleted_at"] or str(setting["last_scheduled_date"]) == local_date:
                continue
            if local_time < str(setting["daily_time"]):
                continue
            account = _account(conn, int(setting["account_id"]))
            try:
                target_json = str(setting["feishu_target_json"] or "{}")
                if not _target_key(_json_loads(target_json, {})):
                    continue
                publish_date_start, publish_date_end = _automatic_publish_range(
                    _validate_date_rule(setting["date_rule"]), local_day
                )
                _insert_job(
                    conn,
                    account,
                    "daily",
                    publish_date_start,
                    publish_date_end,
                    target_json,
                    schedule_date=local_date,
                )
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
            SELECT c.id, c.account_id FROM collect_jobs c
            WHERE c.status IN ('queued','delayed')
              AND EXISTS (
                  SELECT 1 FROM tiktok_accounts a
                  WHERE a.id = c.account_id AND a.deleted_at = '' AND a.proxy_bound = 1
              )
              AND (c.next_attempt_at = '' OR c.next_attempt_at <= ?)
              AND NOT EXISTS (
                  SELECT 1 FROM collect_jobs running
                  WHERE running.account_id = c.account_id
                    AND running.status IN ('preparing','collecting')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM publish_jobs p
                  WHERE p.account_id = c.account_id AND p.deleted_at = ''
                    AND p.status IN ('preparing','uploading','publishing')
              )
            ORDER BY c.created_at ASC LIMIT ?
            """,
            (_iso(), capacity * 4),
        ).fetchall()
        claimed_accounts: set[int] = set()
        for row in rows:
            account_id = int(row["account_id"])
            if account_id in claimed_accounts or len(claimed) >= capacity:
                continue
            job_id = str(row["id"])
            changed = conn.execute(
                """
                UPDATE collect_jobs
                SET status = 'preparing', stage = 'claimed', attempt_count = attempt_count + 1,
                    status_detail = '', next_attempt_at = '', updated_at = ?
                WHERE id = ? AND status IN ('queued','delayed')
                """,
                (_iso(), job_id),
            ).rowcount
            if changed:
                claimed.append(job_id)
                claimed_accounts.add(account_id)
        conn.commit()
    return claimed


def _recover_interrupted() -> None:
    proxy_failures: list[tuple[int, str]] = []
    with proxy_pool.connect() as conn:
        conn.execute(
            """
            UPDATE collect_jobs
            SET status = 'queued', stage = 'recovered', session_id = NULL,
                status_detail = '服务器重启，采集任务已重新排队', next_attempt_at = '',
                last_error = '服务器重启后恢复采集任务', updated_at = ?
            WHERE status IN ('preparing','collecting')
            """,
            (_iso(),),
        )
        now = _iso()
        retry_at = _iso(_utc_now() + timedelta(seconds=proxy_pool.PROXY_QUEUE_RECHECK_SECONDS))
        rows = conn.execute(
            "SELECT id, proxy_profile_id, last_error FROM collect_jobs WHERE status = 'failed' AND completed_videos = 0"
        ).fetchall()
        for row in rows:
            message = str(row["last_error"] or "")
            if not proxy_pool.is_retryable_proxy_error(message):
                continue
            conn.execute(
                "UPDATE collect_jobs SET status = 'delayed', stage = 'waiting_proxy', status_detail = '', session_id = NULL, completed_at = '', next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (retry_at, now, row["id"]),
            )
            proxy_failures.append((int(row["proxy_profile_id"]), message))
        conn.commit()
    for pool_id, message in proxy_failures:
        proxy_pool.schedule_proxy_recheck_for_pending_job(pool_id, message)


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
