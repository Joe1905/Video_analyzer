#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import proxy_pool


ROOT = Path.cwd()
PUBLISH_ROOT = ROOT / "videos" / "tiktok_publish"
LOG_ROOT = ROOT / "data" / "tiktok_publish_jobs"
MAX_UPLOAD_BYTES = int(os.getenv("TIKTOK_PUBLISH_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
TIMEZONE_NAME = os.getenv("TZ", "America/Los_Angeles") or "America/Los_Angeles"
NATIVE_MIN_MINUTES = max(15, int(os.getenv("TIKTOK_NATIVE_SCHEDULE_MIN_MINUTES", "20") or "20"))
DRY_RUN = os.getenv("TIKTOK_PUBLISH_DRY_RUN", "1").strip().lower() in {"1", "true", "yes", "on"}
UPLOAD_TIMEOUT_SECONDS = max(60, int(os.getenv("TIKTOK_PUBLISH_UPLOAD_TIMEOUT_SECONDS", "1800") or "1800"))
ALLOWED_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
EDITABLE_STATUSES = {"draft", "queued", "delayed", "failed", "cancelled", "dry_run", "manual_ready"}
RETRYABLE_STATUSES = {"failed", "product_link_failed"}
DELETE_BLOCKED_STATUSES = {"preparing", "uploading", "publishing", "scheduled_on_tiktok"}
STATUS_LABELS = {
    "draft": "草稿箱",
    "queued": "待发布",
    "delayed": "延迟等待",
    "preparing": "准备中",
    "uploading": "上传中",
    "publishing": "发布中",
    "published": "已发布",
    "failed": "发布失败",
    "result_uncertain": "结果待确认",
    "product_link_review": "商品绑定待确认",
    "product_link_failed": "商品未绑定成功",
    "cancelled": "已取消",
    "scheduled_on_tiktok": "TikTok已排程",
    "dry_run": "演练完成",
    "manual_ready": "等待手动发布",
}

SAFE_POPUP_BUTTONS = {
    "allow": "allow",
    "cancel": "cancel",
    "close": "close",
    "got it": "got it",
    "later": "later",
    "maybe later": "maybe later",
    "not now": "not now",
    "skip": "skip",
}

_worker_started = False
_worker_lock = threading.Lock()
_active_jobs: set[str] = set()


class ManualReviewRequired(RuntimeError):
    pass


class ProductLinkReviewRequired(RuntimeError):
    pass


class ProductLinkUnavailable(RuntimeError):
    pass


class ManualPublishReady(RuntimeError):
    pass


class ResultUncertain(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_schedule(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return _utc_now()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("发布时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(TIMEZONE_NAME))
    return parsed.astimezone(timezone.utc)


def _normalize_native_schedule(value: datetime) -> datetime:
    normalized = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    remainder = normalized.minute % 5
    if remainder:
        normalized += timedelta(minutes=5 - remainder)
    return normalized


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clean_product_link(value: Any) -> str:
    return _clean_text(value, 2000)


def _row_to_job(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "proxy_profile_id": row["proxy_profile_id"],
        "asset_id": row["asset_id"],
        "original_name": row["original_name"],
        "size_bytes": row["size_bytes"],
        "content_type": row["content_type"],
        "description": row["description"],
        "ai_generated": bool(row["ai_generated"]),
        "product_link": row["product_link"],
        "product_link_status": "待绑定" if row["product_link"] else "不添加",
        "keep_observing": bool(row["keep_observing"]),
        "manual_publish": bool(row["manual_publish"]),
        "schedule_mode": row["schedule_mode"],
        "scheduled_at": row["scheduled_at"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "stage": row["stage"],
        "attempt_count": row["attempt_count"],
        "session_id": row["session_id"],
        "actual_publish_at": row["actual_publish_at"],
        "result_url": row["result_url"],
        "last_error": row["last_error"],
        "preview_url": f"/api/proxy/publish/videos/{row['asset_id']}",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _job_query(where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    sql = (
        "SELECT j.*, a.original_name, a.size_bytes, a.content_type, a.stored_path "
        "FROM publish_jobs j JOIN publish_assets a ON a.id = j.asset_id "
        "WHERE j.deleted_at = '' "
    )
    if where:
        sql += "AND (" + where + ") "
    sql += "ORDER BY j.created_at DESC"
    with proxy_pool.connect() as conn:
        return [_row_to_job(row) for row in conn.execute(sql, params).fetchall()]


def list_jobs(account_id: int) -> dict[str, Any]:
    if not account_id:
        raise ValueError("account_id is required")
    return {"jobs": _job_query("j.account_id = ?", (account_id,)), "timezone": TIMEZONE_NAME, "dry_run": DRY_RUN}


def _validate_schedule(mode: str, scheduled_at: datetime, queued: bool) -> None:
    if mode not in {"server", "tiktok"}:
        raise ValueError("发布模式配置无效")
    if queued and mode == "tiktok" and scheduled_at < _utc_now() + timedelta(minutes=NATIVE_MIN_MINUTES):
        raise ValueError(f"TikTok 定时发布至少需要提前 {NATIVE_MIN_MINUTES} 分钟")


def _resolve_schedule_mode(mode: str, scheduled_at: datetime, queued: bool) -> str:
    if queued and mode == "tiktok" and scheduled_at <= _utc_now():
        return "server"
    return mode


def create_job(form: Any) -> dict[str, Any]:
    account_id = int(form.getfirst("account_id") or 0)
    if not account_id:
        raise ValueError("account_id is required")
    action = _clean_text(form.getfirst("action"), 20) or "queue"
    if action not in {"draft", "queue", "immediate", "manual"}:
        raise ValueError("发布操作无效")
    queued = action != "draft"
    manual_publish = action == "manual"
    keep_observing = action in {"immediate", "manual"}
    requested_session_id = int(form.getfirst("observation_session_id") or 0)
    schedule_mode = "server" if action == "immediate" else "tiktok"
    scheduled_at = _parse_schedule(form.getfirst("scheduled_at"))
    schedule_mode = _resolve_schedule_mode(schedule_mode, scheduled_at, queued)
    if not manual_publish:
        if queued and schedule_mode == "tiktok":
            scheduled_at = _normalize_native_schedule(scheduled_at)
        _validate_schedule(schedule_mode, scheduled_at, queued)
    try:
        file_item = form["video"]
    except KeyError as exc:
        raise ValueError("请选择视频文件") from exc
    if isinstance(file_item, list):
        file_item = file_item[0]
    original_name = Path(str(getattr(file_item, "filename", "") or "")).name
    suffix = Path(original_name).suffix.lower()
    if not original_name or suffix not in ALLOWED_SUFFIXES:
        raise ValueError("仅支持 MP4、MOV、M4V 或 WebM 视频")

    with proxy_pool.connect() as conn:
        account = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ? AND deleted_at = ''", (account_id,)).fetchone()
        if not account:
            raise ValueError("account not found")
        proxy_profile_id = int(account["proxy_profile_id"])

    asset_id = uuid.uuid4().hex
    job_id = uuid.uuid4().hex
    target_dir = PUBLISH_ROOT / str(account_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{asset_id}{suffix}"
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as output:
            while True:
                chunk = file_item.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError("视频超过 2GB 上传限制")
                digest.update(chunk)
                output.write(chunk)
        if size <= 0:
            raise ValueError("视频文件为空")
        now = _iso()
        stored_path = target.relative_to(ROOT).as_posix()
        content_type = _clean_text(getattr(file_item, "type", ""), 120) or mimetypes.guess_type(original_name)[0] or "video/mp4"
        with proxy_pool.connect() as conn:
            conn.execute(
                "INSERT INTO publish_assets (id, account_id, original_name, stored_path, content_type, size_bytes, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, account_id, original_name, stored_path, content_type, size, digest.hexdigest(), now),
            )
            conn.execute(
                """
                INSERT INTO publish_jobs (
                    id, account_id, proxy_profile_id, asset_id, description, ai_generated,
                    product_link, keep_observing, manual_publish, schedule_mode, scheduled_at, status, stage, attempt_count, next_attempt_at,
                    session_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, '', ?, ?, ?)
                """,
                (
                    job_id,
                    account_id,
                    proxy_profile_id,
                    asset_id,
                    _clean_text(form.getfirst("description"), 2200),
                    1 if str(form.getfirst("ai_generated") or "").lower() in {"1", "true", "yes", "on"} else 0,
                    _clean_product_link(form.getfirst("product_link")),
                    1 if keep_observing else 0,
                    1 if manual_publish else 0,
                    schedule_mode,
                    _iso(scheduled_at),
                    "queued" if queued else "draft",
                    requested_session_id or None,
                    now,
                    now,
                ),
            )
            conn.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"job": _job_query("j.id = ?", (job_id,))[0], **list_jobs(account_id)}


def update_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = _clean_text(payload.get("id") or payload.get("job_id"), 80)
    if not job_id:
        raise ValueError("job_id is required")
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT * FROM publish_jobs WHERE id = ? AND deleted_at = ''", (job_id,)).fetchone()
        if not row:
            raise ValueError("publish job not found")
        if row["status"] not in EDITABLE_STATUSES:
            raise ValueError("当前任务状态不能编辑")
        scheduled = _parse_schedule(payload.get("scheduled_at") or row["scheduled_at"])
        product_link = _clean_product_link(payload["product_link"]) if "product_link" in payload else str(row["product_link"] or "")
        manual_publish = bool(payload.get("manual_publish"))
        keep_observing = bool(payload["keep_observing"]) if "keep_observing" in payload else bool(row["keep_observing"])
        requested_session_id = (
            int(payload.get("observation_session_id") or 0) or None
            if "observation_session_id" in payload
            else row["session_id"]
        )
        queue = bool(payload.get("queue"))
        mode = "server" if keep_observing else "tiktok"
        mode = _resolve_schedule_mode(mode, scheduled, queue)
        if manual_publish:
            keep_observing = True
            mode = "tiktok"
        else:
            if queue and mode == "tiktok":
                scheduled = _normalize_native_schedule(scheduled)
            _validate_schedule(mode, scheduled, queue)
        status = "queued" if queue else "draft"
        conn.execute(
            "UPDATE publish_jobs SET description = ?, ai_generated = ?, product_link = ?, keep_observing = ?, manual_publish = ?, schedule_mode = ?, scheduled_at = ?, status = ?, stage = '', attempt_count = 0, next_attempt_at = '', session_id = ?, final_click_at = '', actual_publish_at = '', result_url = '', last_error = '', updated_at = ? WHERE id = ?",
            (
                _clean_text(payload.get("description"), 2200),
                1 if payload.get("ai_generated") else 0,
                product_link,
                1 if keep_observing else 0,
                1 if manual_publish else 0,
                mode,
                _iso(scheduled),
                status,
                requested_session_id,
                _iso(),
                job_id,
            ),
        )
        conn.commit()
        account_id = int(row["account_id"])
    return {"job": _job_query("j.id = ?", (job_id,))[0], **list_jobs(account_id)}


def cancel_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = _clean_text(payload.get("id") or payload.get("job_id"), 80)
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT * FROM publish_jobs WHERE id = ? AND deleted_at = ''", (job_id,)).fetchone()
        if not row:
            raise ValueError("publish job not found")
        if row["status"] not in EDITABLE_STATUSES:
            raise ValueError("只能取消尚未开始的发布任务")
        conn.execute("UPDATE publish_jobs SET status = 'cancelled', stage = '', updated_at = ? WHERE id = ?", (_iso(), job_id))
        conn.commit()
        account_id = int(row["account_id"])
    return list_jobs(account_id)


def retry_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = _clean_text(payload.get("id") or payload.get("job_id"), 80)
    if not job_id:
        raise ValueError("job_id is required")
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT * FROM publish_jobs WHERE id = ? AND deleted_at = ''", (job_id,)).fetchone()
        if not row:
            raise ValueError("publish job not found")
        if row["status"] not in RETRYABLE_STATUSES:
            raise ValueError("只有发布失败的任务可以重试")
        account_id = int(row["account_id"])
        scheduled = _parse_schedule(row["scheduled_at"])
        now = _utc_now()
        mode = "tiktok"
        if scheduled <= now:
            mode = "server"
            scheduled = now
        else:
            scheduled = _normalize_native_schedule(scheduled)
            _validate_schedule(mode, scheduled, True)
        requested_session_id = int(payload.get("observation_session_id") or 0) or None
        conn.execute(
            """
            UPDATE publish_jobs
            SET keep_observing = ?, manual_publish = 0, schedule_mode = ?, scheduled_at = ?, status = 'queued',
                stage = 'retry_queued', next_attempt_at = '', session_id = ?, final_click_at = '',
                actual_publish_at = '', result_url = '', last_error = '', updated_at = ?
            WHERE id = ?
            """,
            (1 if requested_session_id else 0, mode, _iso(scheduled), requested_session_id, _iso(now), job_id),
        )
        conn.execute(
            "UPDATE tiktok_accounts SET last_error = '', updated_at = ? WHERE id = ?",
            (_iso(now), account_id),
        )
        conn.commit()
    return {"job": _job_query("j.id = ?", (job_id,))[0], **list_jobs(account_id)}


def delete_job(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = _clean_text(payload.get("id") or payload.get("job_id"), 80)
    if not job_id:
        raise ValueError("job_id is required")
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT * FROM publish_jobs WHERE id = ? AND deleted_at = ''", (job_id,)).fetchone()
        if not row:
            raise ValueError("publish job not found")
        if row["status"] in DELETE_BLOCKED_STATUSES:
            raise ValueError("运行中或 TikTok 已排程的任务不能删除")
        now = _iso()
        status = "cancelled" if row["status"] in {"draft", "queued", "delayed"} else row["status"]
        conn.execute(
            "UPDATE publish_jobs SET status = ?, stage = '', deleted_at = ?, updated_at = ? WHERE id = ?",
            (status, now, now, job_id),
        )
        conn.commit()
        account_id = int(row["account_id"])
    return list_jobs(account_id)


def video_path(asset_id: str) -> Path:
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT stored_path FROM publish_assets WHERE id = ?", (_clean_text(asset_id, 80),)).fetchone()
    if not row:
        raise ValueError("publish video not found")
    path = (ROOT / str(row["stored_path"])).resolve()
    root = PUBLISH_ROOT.resolve()
    if root != path.parent and root not in path.parents:
        raise ValueError("invalid publish video path")
    if not path.is_file():
        raise ValueError("publish video file is missing")
    return path


def runtime_status() -> dict[str, Any]:
    with proxy_pool.connect() as conn:
        counts = {row["status"]: int(row["count"]) for row in conn.execute("SELECT status, COUNT(*) AS count FROM publish_jobs WHERE deleted_at = '' GROUP BY status")}
    with _worker_lock:
        active = sorted(_active_jobs)
    return {
        "worker_started": _worker_started,
        "dry_run": DRY_RUN,
        "timezone": TIMEZONE_NAME,
        "max_automatic_slots": proxy_pool.browser_max_slots(),
        "active_jobs": active,
        "counts": counts,
        "native_schedule_lead_seconds": 0,
        "native_schedule_starts_immediately": True,
    }


def _set_job(job_id: str, status: str, stage: str = "", error: str = "", **values: Any) -> None:
    fields = ["status = ?", "stage = ?", "last_error = ?", "updated_at = ?"]
    params: list[Any] = [status, stage, _clean_text(error, 2000), _iso()]
    allowed = {"session_id", "final_click_at", "actual_publish_at", "result_url", "next_attempt_at"}
    for key, value in values.items():
        if key in allowed:
            fields.append(f"{key} = ?")
            params.append(value)
    params.append(job_id)
    with proxy_pool.connect() as conn:
        conn.execute(f"UPDATE publish_jobs SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()


def _update_account(account_id: int, error: str = "", published_at: str = "") -> None:
    with proxy_pool.connect() as conn:
        conn.execute(
            "UPDATE tiktok_accounts SET last_error = ?, last_publish_at = COALESCE(NULLIF(?, ''), last_publish_at), updated_at = ? WHERE id = ?",
            (_clean_text(error, 2000), published_at, _iso(), account_id),
        )
        conn.commit()


def _first_visible(locators: list[Any]) -> Any | None:
    for locator in locators:
        try:
            count = min(locator.count(), 8)
            for index in range(count):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except Exception:
            continue
    return None


def _skip_onboarding(page: Any) -> None:
    pattern = re.compile(r"^(skip|skip for now|not now|got it|later|跳过|暂不|稍后|知道了)$", re.I)
    for _ in range(4):
        button = _first_visible([page.get_by_role("button", name=pattern), page.get_by_text(pattern, exact=True)])
        if not button:
            return
        button.click(timeout=3000)
        page.wait_for_timeout(500)


def _dismiss_upload_prompts(page: Any) -> None:
    for _ in range(6):
        got_it = _first_visible([
            page.get_by_role("button", name=re.compile(r"^got it$|^知道了$", re.I)),
        ])
        if not got_it:
            break
        got_it.click(timeout=3000, force=True)
        page.wait_for_timeout(500)

    automatic_checks = _first_visible([
        page.get_by_text(re.compile(r"turn on automatic content checks|开启自动内容检查", re.I)),
    ])
    if automatic_checks:
        cancel = _first_visible([
            page.get_by_role("button", name=re.compile(r"^cancel$|^取消$", re.I)),
        ])
        if cancel:
            cancel.click(timeout=3000)
            page.wait_for_timeout(500)


def _assert_account_ready(page: Any) -> None:
    url = page.url.lower()
    if "/login" in url:
        raise ManualReviewRequired("TikTok 登录已失效，请从观测通道重新登录")
    challenge = _first_visible([
        page.get_by_text(re.compile(r"captcha|verify to continue|security verification|验证码|安全验证", re.I)),
        page.locator("iframe[src*='captcha']"),
    ])
    if challenge:
        raise ManualReviewRequired("TikTok 要求验证码或安全验证，请从观测通道人工处理")


def _set_description(page: Any, description: str) -> None:
    if not description:
        return
    target = _first_visible([
        page.locator("[data-e2e*='caption'] [contenteditable='true']"),
        page.locator(".public-DraftEditor-content[contenteditable='true']"),
        page.locator("[contenteditable='true'][role='combobox']"),
        page.locator("[contenteditable='true'][role='textbox']"),
        page.get_by_label(re.compile(r"description|caption|说明|描述", re.I)),
        page.locator("textarea[placeholder*='caption' i], textarea[placeholder*='description' i]"),
    ])
    if not target:
        raise RuntimeError("未找到 TikTok Studio 的 Description 输入框")
    target.click()
    try:
        target.fill(description)
    except Exception:
        target.press("Control+A")
        target.type(description)


def _set_ai_generated(page: Any, enabled: bool) -> None:
    if not enabled:
        return
    show_more = _first_visible([
        page.get_by_role("button", name=re.compile(r"show more|更多|展开", re.I)),
        page.get_by_text(re.compile(r"^show more$|^显示更多$|^更多设置$", re.I), exact=True),
    ])
    if show_more:
        show_more.click()
        page.wait_for_timeout(500)
    label_pattern = re.compile(r"AI.generated content|AI 生成|人工智能生成", re.I)
    checkbox = _first_visible([
        page.locator("[data-e2e='aigc_container'] input[role='switch']"),
        page.get_by_role("checkbox", name=label_pattern),
        page.get_by_label(label_pattern),
    ])
    if checkbox:
        confirmation = _first_visible([
            page.get_by_text(re.compile(r"labeling AI.generated content|标记 AI 生成内容", re.I)),
        ])
        if not checkbox.is_checked() and not confirmation:
            switch = _first_visible([
                page.locator("[data-e2e='aigc_container'] .Switch__content"),
                page.locator("[data-e2e='aigc_container'] [data-layout='switch-root']"),
            ])
            if switch:
                switch.click(timeout=3000)
            else:
                checkbox.click(timeout=3000, force=True)
            page.wait_for_timeout(300)
            confirmation = _first_visible([
                page.get_by_text(re.compile(r"labeling AI.generated content|标记 AI 生成内容", re.I)),
            ])
        if confirmation:
            turn_on = _first_visible([
                page.get_by_role("button", name=re.compile(r"^turn on$|^开启$", re.I)),
            ])
            if not turn_on:
                raise RuntimeError("未找到 AI-generated content 确认按钮")
            turn_on.click(timeout=3000)
            page.wait_for_timeout(300)
        if not checkbox.is_checked() and checkbox.get_attribute("aria-checked") != "true":
            raise RuntimeError("无法启用 AI-generated content 设置")
        return
    label = _first_visible([page.get_by_text(label_pattern)])
    if not label:
        raise RuntimeError("未找到 AI-generated content 设置")
    label.click()


def _radio_selected(locator: Any) -> bool:
    try:
        if not locator.count():
            return False
        item = locator.first
        try:
            if item.is_checked():
                return True
        except Exception:
            pass
        if str(item.get_attribute("aria-checked") or "").lower() == "true":
            return True
        classes = str(item.get_attribute("class") or "")
        return bool(re.search(r"(?:^|[-_\s])(checked|selected|active)(?:$|[-_\s])", classes, re.I))
    except Exception:
        return False


def _popup_snapshot(page: Any, log_dir: Path, step: str) -> Path:
    target = log_dir / f"parameter-{step}-{int(time.time())}.png"
    page.screenshot(path=str(target), full_page=False)
    return target


def _visible_dialog(page: Any) -> Any | None:
    return _first_visible([
        page.get_by_role("dialog"),
        page.locator("[role='dialog']"),
        page.locator("[aria-modal='true']"),
    ])


def _dialog_details(dialog: Any) -> tuple[str, list[str]]:
    try:
        text = _clean_text(dialog.inner_text(), 2000)
    except Exception:
        text = ""
    buttons: list[str] = []
    try:
        locator = dialog.get_by_role("button")
        for index in range(min(locator.count(), 12)):
            label = _clean_text(locator.nth(index).inner_text(), 80)
            if label:
                buttons.append(label)
    except Exception:
        pass
    return text, buttons


def _vision_popup_decision(snapshot: Path, step: str, dialog_text: str, buttons: list[str]) -> dict[str, Any]:
    api_key = os.getenv("VISION_API_KEY", "").strip()
    api_url = os.getenv("VISION_API_URL", "").strip().rstrip("/")
    model = os.getenv("VISION_MODEL", "qwen3-vl-flash").strip() or "qwen3-vl-flash"
    if not api_key or not api_url:
        return {"action": "manual_review", "reason": "视觉模型未配置"}
    if not api_url.endswith("/chat/completions"):
        api_url += "/chat/completions"
    encoded = base64.b64encode(snapshot.read_bytes()).decode("ascii")
    prompt = (
        "分析 TikTok Studio 参数页的弹窗。只能返回 JSON："
        '{"action":"click|manual_review","button_text":"","x":0,"y":0,"reason":""}。'
        "仅当弹窗是说明、提示或可安全处理的参数授权时才选择 click；"
        "button_text 必须原样来自候选按钮，x/y 是截图中的按钮中心坐标。"
        "只有弹窗明确询问 scheduled posting storage 时才可选择 Allow。"
        "禁止选择 Post、Publish、Next、Confirm、Delete。"
        f"\n当前步骤：{step}\n弹窗文字：{dialog_text}\n候选按钮：{buttons}"
    )
    payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            }],
            "temperature": 0,
            "max_tokens": 240,
        }
    request = Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        response_body = json.loads(response.read().decode("utf-8"))
    content = str(response_body["choices"][0]["message"]["content"])
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        return {"action": "manual_review", "reason": "视觉模型未返回 JSON"}
    parsed = json.loads(match.group(0))
    return {
        "action": _clean_text(parsed.get("action"), 40),
        "button_text": _clean_text(parsed.get("button_text"), 80),
        "x": parsed.get("x"),
        "y": parsed.get("y"),
        "reason": _clean_text(parsed.get("reason"), 300),
    }


def _click_popup_coordinate(page: Any, dialog: Any, button_text: str, x: Any, y: Any) -> bool:
    button = _first_visible([
        dialog.get_by_role("button", name=re.compile(rf"^{re.escape(button_text)}$", re.I)),
    ])
    if not button:
        return False
    box = button.bounding_box()
    dialog_box = dialog.bounding_box()
    if not box or not dialog_box:
        return False
    try:
        click_x = float(x)
        click_y = float(y)
    except (TypeError, ValueError):
        click_x = box["x"] + box["width"] / 2
        click_y = box["y"] + box["height"] / 2
    inside_button = box["x"] <= click_x <= box["x"] + box["width"] and box["y"] <= click_y <= box["y"] + box["height"]
    inside_dialog = dialog_box["x"] <= click_x <= dialog_box["x"] + dialog_box["width"] and dialog_box["y"] <= click_y <= dialog_box["y"] + dialog_box["height"]
    if not inside_button or not inside_dialog:
        click_x = box["x"] + box["width"] / 2
        click_y = box["y"] + box["height"] / 2
    page.mouse.click(click_x, click_y)
    page.wait_for_timeout(500)
    return True


def _handle_parameter_popup(page: Any, log_dir: Path, step: str) -> bool:
    dialog = _visible_dialog(page)
    if not dialog:
        return False
    dialog_text, buttons = _dialog_details(dialog)
    snapshot = _popup_snapshot(page, log_dir, step)
    normalized = dialog_text.lower()
    try:
        decision = _vision_popup_decision(snapshot, step, dialog_text, buttons)
    except Exception as exc:
        decision = {"action": "manual_review", "reason": f"视觉模型调用失败：{exc}"}
    known_schedule_permission = "allow your video to be saved for scheduled posting" in normalized
    if known_schedule_permission and not (
        decision.get("action") == "click" and str(decision.get("button_text") or "").strip().lower() == "allow"
    ):
        decision = {
            "source": "known_popup_fallback",
            "action": "click",
            "button_text": "Allow",
            "x": None,
            "y": None,
            "reason": "已确认的 TikTok 定时发布存储授权弹窗",
        }
    (log_dir / f"parameter-{step}-decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    button_text = str(decision.get("button_text") or "").strip()
    safe_name = SAFE_POPUP_BUTTONS.get(button_text.lower())
    allow_allowed = button_text.lower() != "allow" or known_schedule_permission
    if decision.get("action") == "click" and safe_name and allow_allowed and button_text in buttons:
        if _click_popup_coordinate(page, dialog, button_text, decision.get("x"), decision.get("y")):
            return True
    reason = str(decision.get("reason") or "弹窗不属于可自动处理的安全类型")
    raise ManualReviewRequired(f"参数页出现未识别弹窗，已保留观测通道：{reason}")


def _run_parameter_step(page: Any, log_dir: Path, step: str, action: Any) -> None:
    try:
        action()
        return
    except ManualReviewRequired:
        raise
    except Exception as first_error:
        _popup_snapshot(page, log_dir, f"{step}-error")
        if _handle_parameter_popup(page, log_dir, step):
            try:
                action()
                return
            except Exception as retry_error:
                raise ManualReviewRequired(
                    f"{step} 参数设置失败，已保留观测通道：{retry_error}"
                ) from retry_error
        raise ManualReviewRequired(f"{step} 参数设置失败，已保留观测通道：{first_error}") from first_error


def _select_schedule_radio(page: Any, value: str, label_pattern: re.Pattern[str], log_dir: Path) -> None:
    selector = f"input[name='postSchedule'][value='{value}']"
    input_locator = page.locator(selector)
    role_locator = page.get_by_role("radio", name=label_pattern)
    label_locator = page.locator(f"label:has({selector})")
    state_locators = [input_locator, role_locator, label_locator]
    if any(_radio_selected(locator) for locator in state_locators):
        return
    candidates = [label_locator, role_locator, page.get_by_text(label_pattern, exact=True)]
    if input_locator.count():
        item = input_locator.first
        candidates.extend([
            item.locator("xpath=ancestor::label[1]"),
            item.locator("xpath=.."),
            item.locator("xpath=../.."),
            input_locator,
        ])
    for locator in candidates:
        option = _first_visible([locator])
        if not option:
            continue
        try:
            option.click(timeout=3000)
        except Exception:
            continue
        _handle_parameter_popup(page, log_dir, "schedule")
        for _ in range(10):
            if any(_radio_selected(state) for state in state_locators):
                return
            page.wait_for_timeout(150)
    label = "定时发布" if value == "schedule" else "立即发布"
    raise RuntimeError(f"无法选择 TikTok Studio 的{label}选项")


def _custom_schedule_fields(page: Any) -> tuple[Any | None, Any | None]:
    container = page.locator("[data-e2e='schedule_container']")
    deadline = time.time() + 5
    while time.time() < deadline:
        time_field = None
        date_field = None
        fields = container.locator("input.TUXTextInputCore-input[readonly]")
        for index in range(fields.count()):
            field = fields.nth(index)
            try:
                if not field.is_visible():
                    continue
                value = field.input_value()
            except Exception:
                continue
            if re.fullmatch(r"\d{2}:\d{2}", value):
                time_field = field
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                date_field = field
        if time_field and date_field:
            return time_field, date_field
        page.wait_for_timeout(200)
    return None, None


def _select_custom_date(page: Any, date_field: Any, target: datetime) -> None:
    expected = target.strftime("%Y-%m-%d")
    if date_field.input_value() == expected:
        return
    date_field.click(timeout=5000)
    calendar = _first_visible([page.locator(".calendar-wrapper")])
    if not calendar:
        raise RuntimeError("TikTok 日期选择器未打开")
    target_month = (target.year, target.month)
    for _ in range(24):
        month_text = calendar.locator(".month-title").inner_text().strip()
        year_text = calendar.locator(".year-title").inner_text().strip()
        try:
            current = datetime.strptime(f"{month_text} {year_text}", "%B %Y")
        except ValueError as exc:
            raise RuntimeError(f"无法识别 TikTok 日历月份：{month_text} {year_text}") from exc
        current_month = (current.year, current.month)
        if current_month == target_month:
            break
        arrows = calendar.locator(".month-header-wrapper .arrow")
        if arrows.count() < 2:
            raise RuntimeError("TikTok 日期选择器缺少月份切换按钮")
        arrows.first.click() if current_month > target_month else arrows.last.click()
        page.wait_for_timeout(200)
    else:
        raise RuntimeError("目标日期超出 TikTok 日期选择范围")
    day = calendar.locator("span.day.valid").filter(has_text=re.compile(rf"^{target.day}$"))
    if not day.count():
        raise RuntimeError(f"TikTok 日期选择器中没有可选日期 {expected}")
    day.first.click(timeout=5000)
    page.wait_for_timeout(200)
    if date_field.input_value() != expected:
        raise RuntimeError(f"TikTok 日期设置未生效：期望 {expected}，当前 {date_field.input_value()}")


def _select_custom_time(page: Any, time_field: Any, target: datetime) -> None:
    expected = target.strftime("%H:%M")
    if time_field.input_value() == expected:
        return
    picker_locator = page.locator(".tiktok-timepicker-time-picker-container")

    def open_picker() -> Any | None:
        for index in range(picker_locator.count()):
            candidate = picker_locator.nth(index)
            try:
                classes = str(candidate.get_attribute("class") or "")
                box = candidate.bounding_box()
                if "tiktok-timepicker-invisible" not in classes and box and box["height"] > 20:
                    return candidate
            except Exception:
                continue
        return None

    picker = open_picker()
    if not picker:
        time_field.locator("xpath=..").click(timeout=5000)
        deadline = time.time() + 3
        while time.time() < deadline and not picker:
            page.wait_for_timeout(100)
            picker = open_picker()
    if not picker:
        raise RuntimeError("TikTok 时间选择器未打开")
    option_lists = picker.locator(".tiktok-timepicker-option-list")
    if option_lists.count() < 2:
        raise RuntimeError("TikTok 时间选择器结构异常")
    values = (target.strftime("%H"), target.strftime("%M"))
    for index, value in enumerate(values):
        option = option_lists.nth(index).locator(".tiktok-timepicker-option-text").filter(
            has_text=re.compile(rf"^{re.escape(value)}$")
        )
        if not option.count():
            raise RuntimeError(f"TikTok 时间选择器中没有可选值 {value}")
        option.first.scroll_into_view_if_needed()
        option.first.click(timeout=5000)
        page.wait_for_timeout(150)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    if time_field.input_value() != expected:
        raise RuntimeError(f"TikTok 时间设置未生效：期望 {expected}，当前 {time_field.input_value()}")


def _set_schedule(page: Any, mode: str, scheduled_at: str, log_dir: Path) -> None:
    if mode == "server":
        _select_schedule_radio(page, "post_now", re.compile(r"^now$|立即|现在", re.I), log_dir)
        return

    _select_schedule_radio(page, "schedule", re.compile(r"^schedule$|定时发布", re.I), log_dir)
    local = _normalize_native_schedule(_parse_schedule(scheduled_at)).astimezone(ZoneInfo(TIMEZONE_NAME))
    date_value = local.strftime("%Y-%m-%d")
    time_value = local.strftime("%H:%M")
    custom_time, custom_date = _custom_schedule_fields(page)
    if custom_time and custom_date:
        _select_custom_date(page, custom_date, local)
        _select_custom_time(page, custom_time, local)
        return
    date_input = None
    time_input = None
    deadline = time.time() + 5
    while time.time() < deadline and (not date_input or not time_input):
        date_input = date_input or _first_visible([
            page.locator("input[type='date']"),
            page.locator("input[placeholder*='MM/DD'], input[placeholder*='YYYY']"),
            page.get_by_label(re.compile(r"date|日期", re.I)),
        ])
        time_input = time_input or _first_visible([
            page.locator("input[type='time']"),
            page.locator("input[placeholder*='HH'], input[placeholder*='hh']"),
            page.get_by_label(re.compile(r"time|时间", re.I)),
        ])
        if not date_input or not time_input:
            page.wait_for_timeout(200)
    if not date_input or not time_input:
        raise RuntimeError("未找到 TikTok 定时发布的日期或时间输入框")
    placeholder = str(date_input.get_attribute("placeholder") or "")
    if date_input.get_attribute("type") != "date" and "/" in placeholder:
        date_value = local.strftime("%m/%d/%Y")
    date_input.fill(date_value)
    time_input.fill(time_value)


def _set_video_file(page: Any, video: Path) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        file_inputs = page.locator("input[type='file'][accept*='video']")
        if not file_inputs.count():
            file_inputs = page.locator("input[type='file']")
        if file_inputs.count():
            file_inputs.first.set_input_files(str(video))
            return
        select_button = _first_visible([
            page.locator("button[data-e2e='select_video_button']"),
            page.get_by_role("button", name=re.compile(r"^select video$|^选择视频$", re.I)),
        ])
        if select_button:
            try:
                with page.expect_file_chooser(timeout=5000) as chooser_info:
                    select_button.click()
                chooser_info.value.set_files(str(video))
                return
            except Exception:
                pass
        page.wait_for_timeout(500)
    raise RuntimeError("未找到 TikTok Studio 视频选择控件")


def _selected_product(product_id: str) -> dict[str, Any]:
    conn = proxy_pool.connect()
    try:
        row = conn.execute(
            "SELECT product_id, product_name FROM tiktok_products WHERE product_id = ?",
            (_clean_text(product_id, 120),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ProductLinkUnavailable(f"公共商品库中未找到商品 ID：{product_id}")
    return {"product_id": str(row["product_id"]), "product_name": str(row["product_name"])}


def _cancel_product_link(page: Any) -> None:
    cancel = _first_visible([
        page.get_by_role("button", name=re.compile(r"^cancel$|^取消$", re.I)),
    ])
    if cancel:
        cancel.click(timeout=5000)
        page.wait_for_timeout(400)


def _find_product_row(page: Any, product_id: str) -> Any | None:
    rows = page.locator("tr")
    for index in range(min(rows.count(), 80)):
        row = rows.nth(index)
        try:
            if row.is_visible() and product_id in row.inner_text():
                return row
        except Exception:
            continue
    return None


def _open_product_link_review(page: Any, product_id: str, log_dir: Path) -> None:
    product = _selected_product(product_id)
    add_link = _first_visible([
        page.locator("button[data-e2e*='add-link' i]"),
        page.get_by_role("button", name=re.compile(r"^add$|^add link$|^添加$", re.I)),
        page.locator("button:has-text('Add')"),
    ])
    if not add_link:
        raise ManualReviewRequired("已填写商品信息，但未找到 TikTok Studio 的 Add link 控件")
    add_link.scroll_into_view_if_needed(timeout=3000)
    add_link.click(timeout=5000)
    page.wait_for_timeout(800)
    dialog = _first_visible([
        page.get_by_role("dialog"),
        page.get_by_text(re.compile(r"^add link$|^添加链接$", re.I), exact=True),
    ])
    if not dialog:
        raise ManualReviewRequired("已点击 Add link，但未检测到商品绑定弹窗")
    page.screenshot(path=str(log_dir / "product-link-dialog.png"), full_page=True)
    product_list_title = _first_visible([
        page.get_by_text(re.compile(r"^add product links$|^添加商品链接$", re.I), exact=True),
    ])
    if not product_list_title:
        next_button = _first_visible([
            page.get_by_role("button", name=re.compile(r"^next$|^下一步$", re.I)),
        ])
        if not next_button or not next_button.is_enabled():
            _cancel_product_link(page)
            raise ProductLinkUnavailable("Add link 的 Products 步骤未就绪，已取消商品绑定")
        next_button.click(timeout=5000)
        page.wait_for_timeout(900)

    row = _find_product_row(page, product["product_id"])
    if not row:
        search = _first_visible([
            page.get_by_placeholder(re.compile(r"search products|搜索商品", re.I)),
            page.locator("input[placeholder*='search products' i]"),
        ])
        if not search:
            _cancel_product_link(page)
            raise ProductLinkUnavailable(f"当前账号没有商品 ID {product['product_id']}，且页面没有搜索框，已取消 Add link")
        search.fill(product["product_id"])
        page.wait_for_timeout(700)
        row = _find_product_row(page, product["product_id"])
    if not row:
        _cancel_product_link(page)
        raise ProductLinkUnavailable(f"当前账号未找到商品 ID {product['product_id']}，已取消 Add link")

    row.scroll_into_view_if_needed(timeout=3000)
    selector = _first_visible([row.locator("input[type='radio']")])
    if selector:
        selector.check(timeout=5000)
    else:
        row.click(timeout=5000)
    page.wait_for_timeout(500)
    next_button = _first_visible([
        page.get_by_role("button", name=re.compile(r"^next$|^下一步$", re.I)),
    ])
    if not next_button or not next_button.is_enabled():
        _cancel_product_link(page)
        raise ProductLinkUnavailable(f"商品 ID {product['product_id']} 未能选中，已取消 Add link")
    page.screenshot(path=str(log_dir / "product-selected.png"), full_page=True)
    raise ProductLinkReviewRequired(
        f"已勾选商品：{product['product_name']}（ID {product['product_id']}），等待人工确认后点击 Next；系统未点击最终发布"
    )


def _execute_browser(job: dict[str, Any], session: dict[str, Any]) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    log_dir = LOG_ROOT / job["id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    video = video_path(job["asset_id"])
    final_clicked = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{session['debug_port']}")
        try:
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            _assert_account_ready(page)
            page.goto("https://www.tiktok.com/tiktokstudio?lang=en", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            _assert_account_ready(page)
            _skip_onboarding(page)
            upload_link = _first_visible([
                page.locator("a[href*='/tiktokstudio/upload']"),
                page.get_by_role("link", name=re.compile(r"upload|create|上传|发布", re.I)),
                page.get_by_role("button", name=re.compile(r"upload|create|上传|发布", re.I)),
            ])
            if upload_link:
                upload_link.click()
                page.wait_for_timeout(1500)
            if "/tiktokstudio/upload" not in page.url:
                page.goto("https://www.tiktok.com/tiktokstudio/upload?from=creator_center&tab=video", wait_until="domcontentloaded", timeout=60000)
            _skip_onboarding(page)
            _assert_account_ready(page)
            _set_job(job["id"], "uploading", "uploading", session_id=session["id"])
            _set_video_file(page, video)
            page.wait_for_timeout(3000)
            _dismiss_upload_prompts(page)
            if job["manual_publish"]:
                page.screenshot(path=str(log_dir / "manual-ready.png"), full_page=True)
                raise ManualPublishReady("视频已上传，等待在 noVNC 中手动填写参数并发布")
            _run_parameter_step(page, log_dir, "description", lambda: _set_description(page, job["description"]))
            _run_parameter_step(page, log_dir, "ai-generated", lambda: _set_ai_generated(page, bool(job["ai_generated"])))
            _run_parameter_step(
                page,
                log_dir,
                "schedule",
                lambda: _set_schedule(page, job["schedule_mode"], job["scheduled_at"], log_dir),
            )
            post_button = _first_visible([
                page.get_by_role("button", name=re.compile(r"^post$|^publish$|^发布$", re.I)),
                page.locator("button[data-e2e*='post']"),
            ])
            if not post_button:
                raise RuntimeError("未找到 TikTok Studio 最终发布按钮")
            deadline = time.time() + UPLOAD_TIMEOUT_SECONDS
            while time.time() < deadline:
                _assert_account_ready(page)
                upload_error = _first_visible([
                    page.get_by_text(re.compile(r"upload failed|couldn't upload|上传失败|处理失败", re.I)),
                ])
                if upload_error:
                    raise RuntimeError("TikTok Studio 报告视频上传或处理失败")
                try:
                    if post_button.is_enabled():
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            else:
                raise RuntimeError(f"等待视频处理完成超过 {UPLOAD_TIMEOUT_SECONDS} 秒")
            page.screenshot(path=str(log_dir / "ready-to-publish.png"), full_page=True)
            if job["product_link"]:
                _open_product_link_review(page, str(job["product_link"]), log_dir)
            if DRY_RUN:
                return "dry_run", page.url
            _set_job(job["id"], "publishing", "final_click", session_id=session["id"], final_click_at=_iso())
            post_button.click()
            final_clicked = True
            try:
                page.wait_for_url(re.compile(r"tiktokstudio(?!/upload)|manage|content"), timeout=45000)
            except Exception:
                success = _first_visible([
                    page.get_by_text(re.compile(r"uploaded|published|scheduled|上传成功|发布成功|已定时", re.I)),
                ])
                if not success:
                    raise ResultUncertain("已点击发布，但未收到明确成功信号，请人工确认，系统不会自动重试")
            return ("scheduled_on_tiktok" if job["schedule_mode"] == "tiktok" else "published"), page.url
        except (ManualReviewRequired, ManualPublishReady, ProductLinkReviewRequired, ProductLinkUnavailable, ResultUncertain):
            raise
        except Exception as exc:
            if final_clicked:
                raise ResultUncertain(f"最终发布后页面异常：{exc}") from exc
            raise
        finally:
            try:
                current_page = context.pages[0] if context.pages else None
                if current_page:
                    current_page.screenshot(path=str(log_dir / "last-state.png"), full_page=True)
            except Exception:
                pass
            # Leaving the CDP browser alive here lets proxy_pool own process cleanup
            # and preserves the same noVNC channel for manual review when required.


def _run_job(job_id: str) -> None:
    session_id = 0
    keep_for_review = False
    reused_observation = False
    keep_observing = False
    try:
        jobs = _job_query("j.id = ?", (job_id,))
        if not jobs:
            return
        job = jobs[0]
        keep_observing = bool(job.get("keep_observing"))
        requested_session_id = int(job.get("session_id") or 0)
        session = proxy_pool.claim_observation_session_for_job(int(job["account_id"]), requested_session_id, job_id)
        if session is not None:
            reused_observation = True
        else:
            session = proxy_pool.start_automation_session(int(job["account_id"]), job_id)["session"]
        session_id = int(session["id"])
        _set_job(job_id, "preparing", "browser_ready", session_id=session_id)
        status, result_url = _execute_browser(job, session)
        actual = job["scheduled_at"] if status == "scheduled_on_tiktok" else (_iso() if status == "published" else "")
        _set_job(job_id, status, "complete", result_url=result_url, actual_publish_at=actual)
        if status in {"published", "scheduled_on_tiktok"}:
            _update_account(int(job["account_id"]), published_at=actual)
        if status == "dry_run" and session_id:
            keep_for_review = True
            proxy_pool.handoff_automation_session(session_id, "演练已到达最终发布前，保留观测通道")
        elif keep_observing and session_id:
            keep_for_review = True
            proxy_pool.handoff_automation_session(session_id, "立即发布完成，保留观测通道")
    except ProductLinkReviewRequired as exc:
        keep_for_review = True
        _set_job(job_id, "product_link_review", "product_link_review", str(exc), session_id=session_id or None)
        if session_id:
            proxy_pool.handoff_automation_session(session_id, str(exc))
    except ManualPublishReady as exc:
        keep_for_review = True
        _set_job(job_id, "manual_ready", "manual_ready", str(exc), session_id=session_id or None)
        if session_id:
            proxy_pool.handoff_automation_session(session_id, str(exc))
    except ProductLinkUnavailable as exc:
        keep_for_review = True
        _set_job(job_id, "product_link_failed", "product_link_failed", str(exc), session_id=session_id or None)
        _update_account(int(job["account_id"]), error=str(exc))
        if session_id:
            proxy_pool.handoff_automation_session(session_id, str(exc))
    except ManualReviewRequired as exc:
        keep_for_review = True
        _set_job(job_id, "failed", "manual_review", str(exc), session_id=session_id or None)
        _update_account(int(job["account_id"]), error=str(exc))
        if session_id:
            proxy_pool.handoff_automation_session(session_id, str(exc))
    except ResultUncertain as exc:
        keep_for_review = True
        _set_job(job_id, "result_uncertain", "confirm_required", str(exc), session_id=session_id or None)
        _update_account(int(job["account_id"]), error=str(exc))
        if session_id:
            proxy_pool.handoff_automation_session(session_id, str(exc))
    except Exception as exc:
        message = str(exc)
        if "槽位已满" in message or "已经处于唤醒状态" in message:
            _set_job(job_id, "delayed", "waiting_slot", message, next_attempt_at=_iso(_utc_now() + timedelta(seconds=30)))
        else:
            _set_job(job_id, "failed", "failed", message, session_id=session_id or None)
            if 'job' in locals():
                _update_account(int(job["account_id"]), error=message)
            if session_id and (DRY_RUN or reused_observation or keep_observing):
                keep_for_review = True
                proxy_pool.handoff_automation_session(session_id, f"发布失败，保留观测通道：{message}")
    finally:
        if session_id and reused_observation and not keep_for_review:
            try:
                proxy_pool.release_observation_session_job(session_id, job_id)
            except Exception as exc:
                print(f"Publish observation session release failed for {job_id}: {exc}", flush=True)
        elif session_id and not keep_for_review:
            try:
                proxy_pool.finish_automation_session(session_id)
            except Exception as exc:
                print(f"Publish session cleanup failed for {job_id}: {exc}", flush=True)
        with _worker_lock:
            _active_jobs.discard(job_id)


def _claim_due_jobs() -> list[str]:
    now = _utc_now()
    with _worker_lock:
        capacity = max(0, proxy_pool.browser_max_slots() - len(_active_jobs))
    if capacity <= 0:
        return []
    claimed: list[str] = []
    with proxy_pool.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id FROM publish_jobs
            WHERE status IN ('queued','delayed')
              AND (next_attempt_at = '' OR next_attempt_at <= ?)
              AND (manual_publish = 1
                OR schedule_mode = 'tiktok'
                OR (schedule_mode = 'server' AND scheduled_at <= ?))
            ORDER BY scheduled_at ASC LIMIT ?
            """,
            (_iso(now), _iso(now), capacity),
        ).fetchall()
        for row in rows:
            job_id = str(row["id"])
            conn.execute(
                "UPDATE publish_jobs SET status = 'preparing', stage = 'claimed', attempt_count = attempt_count + 1, next_attempt_at = '', updated_at = ? WHERE id = ? AND status IN ('queued','delayed')",
                (_iso(), job_id),
            )
            claimed.append(job_id)
        conn.commit()
    return claimed


def _recover_interrupted() -> None:
    now = _iso()
    with proxy_pool.connect() as conn:
        conn.execute(
            "UPDATE publish_jobs SET status = CASE WHEN final_click_at <> '' THEN 'result_uncertain' ELSE 'queued' END, stage = 'recovered', last_error = '服务器重启后恢复任务', session_id = NULL, next_attempt_at = '', updated_at = ? WHERE status IN ('preparing','uploading','publishing')",
            (now,),
        )
        conn.commit()


def _worker_loop() -> None:
    while True:
        try:
            for job_id in _claim_due_jobs():
                with _worker_lock:
                    if job_id in _active_jobs:
                        continue
                    _active_jobs.add(job_id)
                threading.Thread(target=_run_job, args=(job_id,), daemon=True, name=f"tiktok-publish-{job_id[:8]}").start()
        except Exception as exc:
            print(f"TikTok publish scheduler failed: {exc}", flush=True)
        time.sleep(5)


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    PUBLISH_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    with proxy_pool.connect():
        pass
    _recover_interrupted()
    threading.Thread(target=_worker_loop, daemon=True, name="tiktok-publish-scheduler").start()
