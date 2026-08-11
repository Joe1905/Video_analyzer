#!/usr/bin/env python3
"""Run a read-only Instagram Insights content-page click simulation.

Usage:
  python scripts/instagram_content_collect.py --account-id 4 --max-videos 5
  python scripts/instagram_content_collect.py --account-id 4 --check-login

The script intentionally stores canonical URLs only: query strings and fragments
(including any short-lived query token) are removed before a value is logged or
written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import proxy_pool


ROOT = Path.cwd()
DATA_DIR = ROOT / "data"
OUTPUT_ROOT = DATA_DIR / "instagram_content_simulations"
INSTAGRAM_CONTENT_URL = "https://www.instagram.com/accounts/insights/content/"
LOGIN_URL_MARKERS = ("/accounts/login", "/login/")
MEDIA_PATH_PATTERN = re.compile(r"^/(?:p|reel|tv|insights/media)/([^/?#]+)/?$")
HTTP_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
VIEW_LABEL_PATTERN = re.compile(r"^\d+(?:\.\d+)?(?:[KMB])?$")


class InstagramCollectionError(RuntimeError):
    """Raised for a safe, user-actionable collection failure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_url(value: str) -> str:
    """Remove query strings/fragments so tokens never enter results or logs."""
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def media_id_from_url(value: str) -> str:
    parsed = urlsplit(canonical_url(value))
    match = MEDIA_PATH_PATTERN.fullmatch(parsed.path)
    return match.group(1) if match else ""


def _safe_text(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_error(value: Any, limit: int = 500) -> str:
    """Prevent a browser error that embeds a URL from retaining its query token."""
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,;:)]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        return canonical_url(raw) + suffix

    return _safe_text(HTTP_URL_PATTERN.sub(replace, str(value or "")), limit)


def _resolve_workspace_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    resolved = path.resolve()
    profiles_root = (DATA_DIR / "tiktok_browser_profiles").resolve()
    if resolved.parent != profiles_root and profiles_root not in resolved.parents:
        raise InstagramCollectionError("Instagram profile 必须位于 data/tiktok_browser_profiles 中")
    return resolved


def _account_and_profile(account_id: int) -> tuple[sqlite3.Row, Path, int]:
    with proxy_pool.connect() as conn:
        account = conn.execute(
            """SELECT a.*, p.local_port, p.status AS proxy_status
               FROM tiktok_accounts a
               JOIN proxy_profiles p ON p.id = a.proxy_profile_id
               WHERE a.id = ? AND a.deleted_at = ''""",
            (account_id,),
        ).fetchone()
        if not account:
            raise InstagramCollectionError("账号不存在或已删除")
        if not int(account["proxy_bound"] or 0):
            raise InstagramCollectionError("账号未绑定代理，不能启动隔离浏览器")
        if str(account["proxy_status"] or "") != proxy_pool.STATUS_ACTIVE:
            raise InstagramCollectionError("绑定代理未启用，不能启动隔离浏览器")
        try:
            profile = json.loads(str(account["profile_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise InstagramCollectionError("账号 profile 配置不是有效 JSON") from exc
        isolation = profile.get("isolation") if isinstance(profile, dict) else {}
        user_data_dir = str((isolation or {}).get("user_data_dir") or "").strip()
        if not user_data_dir:
            row = conn.execute(
                """SELECT user_data_dir FROM browser_sessions
                   WHERE account_id = ? AND user_data_dir <> ''
                   ORDER BY updated_at DESC, id DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
            user_data_dir = str(row["user_data_dir"] or "") if row else ""
    if not user_data_dir:
        raise InstagramCollectionError("账号没有可用的持久化浏览器 profile")
    profile_path = _resolve_workspace_path(user_data_dir)
    if not profile_path.is_dir():
        raise InstagramCollectionError("账号的浏览器 profile 目录不存在")
    port = int(account["local_port"] or 0)
    if not port:
        raise InstagramCollectionError("绑定代理没有本地端口")
    return account, profile_path, port


def instagram_login_status(profile_dir: Path) -> dict[str, Any]:
    """Check only cookie metadata; encrypted values are never read or returned."""
    candidates = (profile_dir / "Default" / "Cookies", profile_dir / "Default" / "Network" / "Cookies")
    for cookie_path in candidates:
        if not cookie_path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True, timeout=1)
            try:
                rows = conn.execute(
                    """SELECT name FROM cookies
                       WHERE host_key LIKE '%instagram.com%'
                       ORDER BY name"""
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return {"profile_has_instagram_login": False, "reason": f"无法读取 Instagram Cookie 元数据：{exc}"}
        names = [str(row[0]) for row in rows]
        return {
            "profile_has_instagram_login": "sessionid" in names,
            "cookie_count": len(names),
            "cookie_names": names,
            "cookie_store": str(cookie_path.relative_to(profile_dir)),
        }
    return {"profile_has_instagram_login": False, "reason": "profile 中没有 Chrome Cookies 文件"}


def _borrow_observation_session(account_id: int, session_id: int, job_id: str) -> dict[str, Any] | None:
    """Reserve a Web-owned observation session without applying a new runtime ID.

    This collector is an external CDP client. Calling proxy_pool's normal claim
    helper here would see the collector process's runtime ID and incorrectly
    release the browser that is owned by the Web service.
    """
    with proxy_pool.connect() as conn:
        row = conn.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row or int(row["account_id"] or 0) != int(account_id):
            raise InstagramCollectionError("观测通道不属于当前账号")
        if str(row["status"] or "") not in {"starting", "running", "observing"}:
            return None
        current_job_id = str(row["current_job_id"] or "")
        if current_job_id and current_job_id != job_id:
            raise InstagramCollectionError("观测通道正在执行其他任务")
        conn.execute(
            "UPDATE browser_sessions SET current_job_id = ?, last_activity_at = ?, updated_at = ? WHERE id = ?",
            (job_id, now_iso(), now_iso(), session_id),
        )
        conn.commit()
        return proxy_pool._row_to_session(conn.execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone())


def _exclusive_lock(profile_dir: Path) -> Path:
    lock_path = profile_dir.parent / ".instagram-content-simulation.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise InstagramCollectionError("该 profile 已有 INS 采集模拟在运行") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
        lock_file.write(now_iso())
    return lock_path


def _is_login_page(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(marker in path for marker in LOGIN_URL_MARKERS)


def _discover_media_cards(page: Any, limit: int) -> list[dict[str, Any]]:
    """Find insight cards by their view labels without navigating the list page."""
    raw_buttons = page.locator("[role=button]").evaluate_all(
        """buttons => buttons.map((button, index) => ({
            index,
            text: (button.innerText || button.getAttribute('aria-label') || '').trim()
        }))"""
    )
    cards: list[dict[str, Any]] = []
    for item in raw_buttons:
        view_label = _safe_text(item.get("text"), 40)
        if not VIEW_LABEL_PATTERN.fullmatch(view_label):
            continue
        cards.append({
            "rank": len(cards) + 1,
            "button_index": int(item["index"]),
            "views": view_label,
        })
        if len(cards) >= limit:
            break
    return cards


def _wait_for_content_cards(page: Any, limit: int) -> list[dict[str, Any]]:
    """Wait for content-card controls without page.evaluate polling (blocked by INS CSP)."""
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        cards = _discover_media_cards(page, limit)
        if cards:
            return cards
        page.wait_for_timeout(500)
    return []


def _page_diagnostics(page: Any) -> dict[str, Any]:
    """Return token-safe evidence when Instagram changes its content-card DOM."""
    snapshot = page.evaluate(
        """() => ({
            body_text: (document.body?.innerText || '').slice(0, 2500),
            counts: {
                anchors: document.querySelectorAll('a[href]').length,
                buttons: document.querySelectorAll('[role=button]').length,
                images: document.querySelectorAll('img').length,
                iframes: document.querySelectorAll('iframe').length
            },
            clickables: [...document.querySelectorAll('a[href], [role=button]')]
                .slice(0, 40)
                .map(node => ({
                    tag: node.tagName.toLowerCase(),
                    href: node.href || '',
                    label: node.getAttribute('aria-label') || '',
                    text: (node.innerText || '').trim().slice(0, 240)
                }))
        })"""
    )
    clickables = []
    for item in snapshot.get("clickables") or []:
        clickables.append({
            "tag": _safe_text(item.get("tag"), 20),
            "href": canonical_url(str(item.get("href") or "")),
            "label": _safe_text(item.get("label"), 240),
            "text": _safe_text(item.get("text"), 240),
        })
    return {
        "body_text": _safe_text(snapshot.get("body_text"), 2500),
        "counts": snapshot.get("counts") or {},
        "clickables": clickables,
    }


def _click_one_media(context: Any, source: dict[str, Any]) -> dict[str, Any]:
    """Open one disposable detail tab, preserving the master content list page."""
    detail_page = context.new_page()
    try:
        detail_page.goto(INSTAGRAM_CONTENT_URL, wait_until="domcontentloaded", timeout=45000)
        cards = _wait_for_content_cards(detail_page, source["rank"])
        if len(cards) < source["rank"]:
            return {**source, "clicked": False, "error": "新建详情页未加载到对应内容卡片"}
        card = cards[source["rank"] - 1]
        locator = detail_page.locator("[role=button]").nth(card["button_index"])
        locator.scroll_into_view_if_needed(timeout=5000)
        locator.click(timeout=7000)
        detail_page.wait_for_timeout(1200)
        detail_url = canonical_url(detail_page.url)
        result = {
            **source,
            "clicked": True,
            "detail_url": detail_url,
            "media_id": media_id_from_url(detail_url),
            "page_title": _safe_text(detail_page.title(), 200),
            "clicked_at": now_iso(),
        }
        return result
    except Exception as exc:
        return {**source, "clicked": False, "error": _safe_error(exc, 500), "clicked_at": now_iso()}
    finally:
        detail_page.close()


def run_simulation(account_id: int, max_videos: int, check_login_only: bool, session_id: int = 0) -> dict[str, Any]:
    account, profile_dir, _proxy_port = _account_and_profile(account_id)
    login = instagram_login_status(profile_dir)
    result: dict[str, Any] = {
        "account": {"id": int(account["id"]), "username": str(account["username"] or "")},
        "content_url": INSTAGRAM_CONTENT_URL,
        "checked_at": now_iso(),
        "login": login,
    }
    if check_login_only or not login.get("profile_has_instagram_login"):
        return result
    job_id = f"ins_sim_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    log_dir = OUTPUT_ROOT / job_id
    log_dir.mkdir(parents=True, exist_ok=False)
    lock_path = _exclusive_lock(profile_dir)
    active_session_id = 0
    borrowed_observation = False
    try:
        if session_id:
            session = _borrow_observation_session(account_id, session_id, job_id)
            if not session:
                raise InstagramCollectionError("指定观测通道未运行，无法执行 INS 采集")
            borrowed_observation = True
        else:
            started = proxy_pool.start_automation_session(account_id, job_id)
            session = started["session"]
        active_session_id = int(session["id"])
        debug_port = int(session["debug_port"])
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            context = browser.contexts[0]
            list_page = context.new_page()
            list_page.goto(INSTAGRAM_CONTENT_URL, wait_until="domcontentloaded", timeout=45000)
            list_page.wait_for_timeout(1200)
            if _is_login_page(list_page.url):
                result["login"] = {**login, "profile_has_instagram_login": False, "reason": "Instagram 已跳转到登录页"}
                return result
            cards = _wait_for_content_cards(list_page, max_videos)
            if not cards:
                screenshot_path = log_dir / "content-page-no-media-links.png"
                list_page.screenshot(path=str(screenshot_path), full_page=False)
                result["diagnostics"] = _page_diagnostics(list_page)
                result["diagnostic_screenshot"] = str(screenshot_path.relative_to(ROOT))
            result.update({
                "job_id": job_id,
                "session_id": active_session_id,
                "content_page_url": canonical_url(list_page.url),
                "discovered_videos": len(cards),
                "detail_tabs_opened": len(cards),
                "videos": [_click_one_media(context, source) for source in cards],
            })
            # A borrowed observation browser remains open on the content list so
            # the operator can keep watching it after this read-only simulation.
            if not borrowed_observation:
                browser.close()
        output_path = log_dir / "result.json"
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["result_path"] = str(output_path.relative_to(ROOT))
        return result
    finally:
        if active_session_id:
            if borrowed_observation:
                proxy_pool.release_observation_session_job(active_session_id, job_id)
            else:
                proxy_pool.finish_automation_session(active_session_id, "Instagram 内容采集模拟结束")
        lock_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Instagram Insights 内容页逐视频点击采集模拟")
    parser.add_argument("--account-id", type=int, required=True, help="代理池账号 ID")
    parser.add_argument("--max-videos", type=int, default=5, help="最多点击的视频数（默认 5，最大 20）")
    parser.add_argument("--session-id", type=int, default=0, help="复用运行中的观测浏览器会话 ID")
    parser.add_argument("--check-login", action="store_true", help="只检测 INS 登录资料，不启动浏览器")
    args = parser.parse_args()
    if not 1 <= args.max_videos <= 20:
        parser.error("--max-videos 必须在 1 至 20 之间")
    try:
        result = run_simulation(args.account_id, args.max_videos, args.check_login, args.session_id)
    except InstagramCollectionError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
