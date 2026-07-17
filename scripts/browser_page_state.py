#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OCR_API_URL = os.getenv("OCR_API_URL", "http://127.0.0.1:4000/v1/ocr/extract").strip()
OCR_SHARED_DIR = Path(os.getenv("OCR_SHARED_DIR", "/home/openclaw/ocr-shared"))
OCR_SERVER_SHARED_DIR = os.getenv("OCR_SERVER_SHARED_DIR", "/home/openclaw/ocr-shared").rstrip("/")
OCR_ENABLED = os.getenv("TIKTOK_BROWSER_STATE_OCR_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_TIMEOUT_SECONDS = max(15, int(os.getenv("TIKTOK_BROWSER_PAGE_TIMEOUT_SECONDS", "60") or "60"))
DEFAULT_POLL_MILLISECONDS = max(250, int(os.getenv("TIKTOK_BROWSER_PAGE_POLL_MILLISECONDS", "1000") or "1000"))
DEFAULT_OCR_INTERVAL_SECONDS = max(5, int(os.getenv("TIKTOK_BROWSER_STATE_OCR_INTERVAL_SECONDS", "10") or "10"))
DEFAULT_STATUS_INTERVAL_SECONDS = max(1, int(os.getenv("TIKTOK_BROWSER_STATE_STATUS_INTERVAL_SECONDS", "5") or "5"))


class BrowserPageStateError(RuntimeError):
    pass


class BrowserPageBlocked(BrowserPageStateError):
    pass


class BrowserPageLoadError(BrowserPageStateError):
    pass


class BrowserPageTimeout(BrowserPageStateError):
    pass


@dataclass(frozen=True)
class PageStateResult:
    state: str
    label: str
    message: str
    elapsed_seconds: int
    attempt: int
    source: str = "dom"
    ocr_state: str = ""
    ocr_text: str = ""


StatusCallback = Callable[[dict[str, Any]], None]
StatePredicate = Callable[[], bool]
FailurePredicate = Callable[[], str]


def _compact_text(value: Any, max_chars: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()[:max_chars]
    if isinstance(value, list):
        return "\n".join(filter(None, (_compact_text(item, max_chars) for item in value)))[:max_chars]
    if isinstance(value, dict):
        preferred = []
        for key in ("text", "markdown", "content", "plainText", "plain_text", "fullText", "full_text", "result"):
            if key in value:
                text = _compact_text(value.get(key), max_chars)
                if text:
                    preferred.append(text)
        if preferred:
            return "\n".join(preferred)[:max_chars]
        parts = []
        for key, child in value.items():
            if str(key).lower() in {"image", "base64", "dataurl", "data_url"}:
                continue
            text = _compact_text(child, max_chars)
            if text:
                parts.append(f"{key}: {text}")
        return "\n".join(parts)[:max_chars]
    return str(value).strip()[:max_chars]


def classify_ocr_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not normalized:
        return "unknown"
    if re.search(r"captcha|verify to continue|security verification|log in|sign in|验证码|安全验证|登录", normalized):
        return "blocked"
    if re.search(r"something went wrong|try again|couldn['’]?t load|failed to load|network error|出错了|重试|加载失败|网络错误", normalized):
        return "error"
    if re.search(r"no posts|no videos|no content|nothing here|haven['’]?t posted|暂无(?:作品|视频|内容)|没有(?:作品|视频|内容)", normalized):
        return "empty"
    if re.search(r"loading|please wait|加载中|正在加载|请稍候|处理中", normalized):
        return "loading"
    if (
        re.search(r"posts?\s*\d+", normalized)
        and re.search(r"drafts?\s*\d+", normalized)
        and not re.search(r"\b(?:privacy|views|actions|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", normalized)
    ):
        return "loading"
    return "unknown"


def _ocr_server_path(local_path: Path) -> str:
    relative = local_path.relative_to(OCR_SHARED_DIR).as_posix()
    return f"{OCR_SERVER_SHARED_DIR}/{relative}"


def _ocr_page(page: Any, label: str) -> tuple[str, str]:
    if not OCR_ENABLED or not OCR_API_URL:
        return "unknown", ""
    folder = OCR_SHARED_DIR / "incoming" / "browser-state"
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / f"state-{uuid.uuid4().hex}.png"
    try:
        page.screenshot(path=str(snapshot), full_page=False)
        server_path = _ocr_server_path(snapshot)
        payload = {
            "filePath": server_path,
            "serverFilePath": server_path,
            "documentHint": f"TikTok browser state: {label}",
            "structured": True,
        }
        request = Request(
            OCR_API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        text = _compact_text(body)
        return classify_ocr_text(text), text
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return "unavailable", ""
    finally:
        snapshot.unlink(missing_ok=True)


def _page_text(page: Any) -> str:
    try:
        return _compact_text(page.locator("body").inner_text(timeout=2500), 6000)
    except Exception:
        return ""


def _dom_terminal_state(page: Any) -> tuple[str, str]:
    url = str(getattr(page, "url", "") or "").lower()
    text = _page_text(page)
    normalized = text.lower()
    if "/login" in url or re.search(r"captcha|verify to continue|security verification|验证码|安全验证", normalized):
        return "blocked", "TikTok 要求登录或安全验证"
    if re.search(r"something went wrong|couldn['’]?t load|failed to load|network error|出错了|加载失败|网络错误", normalized):
        return "error", "TikTok 页面报告加载错误"
    return "", ""


def _dom_loading_signal(page: Any) -> bool:
    selectors = (
        "[aria-busy='true']",
        "[role='progressbar']",
        "[data-e2e*='loading' i]",
        "[class*='loading' i]",
        "[class*='spinner' i]",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 12)):
                if locator.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def _safe_predicate(predicate: StatePredicate | None) -> bool:
    if predicate is None:
        return False
    try:
        return bool(predicate())
    except Exception:
        return False


def _emit(callback: StatusCallback | None, result: PageStateResult) -> None:
    if callback is None:
        return
    try:
        callback(asdict(result))
    except Exception:
        pass


def _write_diagnostic(
    page: Any,
    directory: Path | None,
    step: str,
    result: PageStateResult,
) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    suffix = f"{step}-{result.state}-{int(time.time())}"
    try:
        page.screenshot(path=str(directory / f"{suffix}.png"), full_page=True)
    except Exception:
        pass
    try:
        (directory / f"{suffix}.json").write_text(
            json.dumps({**asdict(result), "url": str(getattr(page, "url", "") or "")}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def navigate_with_retries(
    page: Any,
    target: str,
    *,
    label: str,
    on_status: StatusCallback | None = None,
    attempts: int = 3,
    timeout_milliseconds: int = 60000,
) -> Any:
    attempts = max(1, int(attempts))
    last_error = ""
    for attempt in range(1, attempts + 1):
        _emit(
            on_status,
            PageStateResult("navigating", label, f"正在打开{label}（{attempt}/{attempts}）", 0, attempt),
        )
        try:
            return page.goto(target, wait_until="domcontentloaded", timeout=timeout_milliseconds)
        except Exception as exc:
            last_error = str(exc)
            if attempt >= attempts:
                break
            _emit(
                on_status,
                PageStateResult("retrying", label, f"{label}连接中断，正在重试（{attempt}/{attempts}）", 0, attempt),
            )
            page.wait_for_timeout(min(2000, 500 * attempt))
    raise BrowserPageTimeout(f"{label}导航重试 {attempts} 次仍失败：{last_error}")


def wait_for_page_state(
    page: Any,
    *,
    label: str,
    ready: StatePredicate,
    empty: StatePredicate | None = None,
    failure: FailurePredicate | None = None,
    allow_ocr_empty: bool = False,
    on_status: StatusCallback | None = None,
    timeout_seconds: int | None = None,
    reload_attempts: int = 1,
    retry_action: Callable[[], Any] | None = None,
    poll_milliseconds: int = DEFAULT_POLL_MILLISECONDS,
    ocr_interval_seconds: int = DEFAULT_OCR_INTERVAL_SECONDS,
    status_interval_seconds: int = DEFAULT_STATUS_INTERVAL_SECONDS,
    diagnostic_dir: Path | None = None,
    diagnostic_step: str = "page",
) -> PageStateResult:
    timeout_seconds = max(1, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
    reload_attempts = max(0, int(reload_attempts))
    last_ocr_state = ""
    last_ocr_text = ""
    for attempt_index in range(reload_attempts + 1):
        attempt = attempt_index + 1
        started = time.monotonic()
        next_ocr_at = started + min(3, ocr_interval_seconds)
        next_status_at = 0.0
        while True:
            elapsed = int(time.monotonic() - started)
            terminal_state, terminal_message = _dom_terminal_state(page)
            if terminal_state == "blocked":
                result = PageStateResult("blocked", label, terminal_message, elapsed, attempt, "dom", last_ocr_state, last_ocr_text)
                _write_diagnostic(page, diagnostic_dir, diagnostic_step, result)
                _emit(on_status, result)
                raise BrowserPageBlocked(terminal_message)
            if _safe_predicate(ready):
                result = PageStateResult("ready", label, f"{label}已加载", elapsed, attempt, "dom", last_ocr_state, last_ocr_text)
                _emit(on_status, result)
                return result
            if _safe_predicate(empty):
                result = PageStateResult("empty", label, f"{label}已加载，当前为空", elapsed, attempt, "dom", last_ocr_state, last_ocr_text)
                _emit(on_status, result)
                return result
            failure_message = ""
            if failure is not None:
                try:
                    failure_message = str(failure() or "").strip()
                except Exception:
                    failure_message = ""
            if failure_message:
                result = PageStateResult(
                    "error", label, failure_message, elapsed, attempt, "dom", last_ocr_state, last_ocr_text
                )
                _write_diagnostic(page, diagnostic_dir, diagnostic_step, result)
                _emit(on_status, result)
                raise BrowserPageLoadError(failure_message)

            now = time.monotonic()
            if now >= next_ocr_at:
                last_ocr_state, last_ocr_text = _ocr_page(page, label)
                next_ocr_at = now + max(5, int(ocr_interval_seconds))
                if last_ocr_state == "blocked":
                    message = f"OCR 识别到{label}需要登录或安全验证"
                    result = PageStateResult("blocked", label, message, elapsed, attempt, "ocr", last_ocr_state, last_ocr_text)
                    _write_diagnostic(page, diagnostic_dir, diagnostic_step, result)
                    _emit(on_status, result)
                    raise BrowserPageBlocked(message)
                if allow_ocr_empty and last_ocr_state == "empty":
                    result = PageStateResult("empty", label, f"OCR 识别到{label}为空", elapsed, attempt, "ocr", last_ocr_state, last_ocr_text)
                    _emit(on_status, result)
                    return result
                if last_ocr_state == "error":
                    terminal_state, terminal_message = "error", f"OCR 识别到{label}加载失败"

            if terminal_state == "error":
                result = PageStateResult("error", label, terminal_message, elapsed, attempt, "ocr" if last_ocr_state == "error" else "dom", last_ocr_state, last_ocr_text)
                _write_diagnostic(page, diagnostic_dir, diagnostic_step, result)
                if attempt_index >= reload_attempts:
                    _emit(on_status, result)
                    raise BrowserPageLoadError(terminal_message)
                retry_result = PageStateResult(
                    "retrying", label, f"{terminal_message}，正在刷新重试（{attempt}/{reload_attempts + 1}）", elapsed, attempt,
                    result.source, last_ocr_state, last_ocr_text,
                )
                _emit(on_status, retry_result)
                (retry_action or (lambda: page.reload(wait_until="domcontentloaded", timeout=60000)))()
                break

            if now >= next_status_at:
                signal = ""
                if last_ocr_state == "loading":
                    signal = "，OCR 识别为加载中"
                elif _dom_loading_signal(page):
                    signal = "，检测到加载动画"
                message = f"正在等待{label}加载（{elapsed} 秒{signal}）"
                _emit(on_status, PageStateResult("loading", label, message, elapsed, attempt, "ocr" if last_ocr_state else "dom", last_ocr_state, last_ocr_text))
                next_status_at = now + max(1, int(status_interval_seconds))

            if elapsed >= timeout_seconds:
                timeout_result = PageStateResult(
                    "timeout", label, f"{label}加载超过 {timeout_seconds} 秒", elapsed, attempt,
                    "ocr" if last_ocr_state else "dom", last_ocr_state, last_ocr_text,
                )
                _write_diagnostic(page, diagnostic_dir, diagnostic_step, timeout_result)
                if attempt_index >= reload_attempts:
                    _emit(on_status, timeout_result)
                    raise BrowserPageTimeout(timeout_result.message)
                retry_result = PageStateResult(
                    "retrying", label,
                    f"{label}加载超时，正在刷新重试（{attempt}/{reload_attempts + 1}）",
                    elapsed, attempt, timeout_result.source, last_ocr_state, last_ocr_text,
                )
                _emit(on_status, retry_result)
                (retry_action or (lambda: page.reload(wait_until="domcontentloaded", timeout=60000)))()
                break
            page.wait_for_timeout(max(250, int(poll_milliseconds)))

    raise BrowserPageTimeout(f"{label}加载超时")
