#!/usr/bin/env python3
"""One-time Phase 5 proxy UI verification against an isolated SQLite database."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}


def artifact_directory(value: str) -> Path:
    path = Path(value).resolve()
    output_root = (ROOT / "output").resolve()
    try:
        path.relative_to(output_root)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"artifacts-dir must be under {output_root}") from exc
    return path


def close_connections(connections: list[sqlite3.Connection]) -> None:
    for connection in connections:
        connection.close()


def seed_proxy_state(proxy_pool: Any, settings: Any) -> tuple[int, int]:
    pool = proxy_pool.upsert_pool({
        "name": "phase5-ui-exit",
        "source_type": "demo",
        "status": settings.STATUS_ACTIVE,
    })["pool"]
    account = proxy_pool.upsert_account({
        "username": "phase5-ui-account",
        "proxy_profile_id": pool["id"],
        "feishu_user_id": "phase5-ui-user",
        "feishu_user_name": "Phase 5 UI",
        "feishu_avatar_url": "",
        "status": settings.ACCOUNT_STATUS_ACTIVE,
    })["account"]
    now = settings.now_iso()
    with proxy_pool.connect() as connection:
        connection.execute(
            """INSERT INTO publish_assets (
                   id, account_id, original_name, stored_path, content_type,
                   size_bytes, sha256, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "phase5-ui-asset", account["id"], "phase5-ui.mp4",
                "output/phase5-ui.mp4", "video/mp4", 0, "", now,
            ),
        )
        connection.execute(
            """INSERT INTO publish_jobs (
                   id, account_id, proxy_profile_id, asset_id, scheduled_at,
                   status, stage, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', ?, ?)""",
            ("phase5-ui-publish", account["id"], pool["id"], "phase5-ui-asset", now, now, now),
        )
        connection.execute(
            """INSERT INTO collect_jobs (
                   id, account_id, proxy_profile_id, status, stage, created_at, updated_at
               ) VALUES (?, ?, ?, 'queued', 'queued', ?, ?)""",
            ("phase5-ui-collect", account["id"], pool["id"], now, now),
        )
        connection.commit()
    return int(pool["id"]), int(account["id"])


def assert_after_delete(proxy_pool: Any, pool_id: int, account_id: int) -> None:
    with proxy_pool.connect() as connection:
        pool = connection.execute(
            "SELECT status, deleted_at FROM proxy_profiles WHERE id = ?", (pool_id,)
        ).fetchone()
        account = connection.execute(
            "SELECT proxy_bound, last_check_status, profile_json FROM tiktok_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        jobs = connection.execute(
            """SELECT id, proxy_profile_id, status, stage FROM publish_jobs WHERE id = ?
               UNION ALL
               SELECT id, proxy_profile_id, status, stage FROM collect_jobs WHERE id = ?""",
            ("phase5-ui-publish", "phase5-ui-collect"),
        ).fetchall()
    assert pool is not None and pool["status"] == "禁用" and pool["deleted_at"]
    assert account is not None and int(account["proxy_bound"] or 0) == 0
    assert account["last_check_status"] == "未绑定"
    profile = json.loads(account["profile_json"] or "{}")
    assert "proxy_binding" not in profile
    assert "proxy_server" not in profile.get("browser_settings", {})
    assert {(row["id"], row["status"], row["stage"]) for row in jobs} == {
        ("phase5-ui-publish", "delayed", "waiting_proxy"),
        ("phase5-ui-collect", "delayed", "waiting_proxy"),
    }
    assert {int(row["proxy_profile_id"]) for row in jobs} == {pool_id}


def assert_after_rebind(proxy_pool: Any, account_id: int) -> None:
    with proxy_pool.connect() as connection:
        account = connection.execute(
            "SELECT proxy_profile_id, proxy_bound, profile_json FROM tiktok_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        assert account is not None and int(account["proxy_bound"] or 0) == 1
        direct_pool = connection.execute(
            "SELECT id, source_type FROM proxy_profiles WHERE id = ?", (account["proxy_profile_id"],)
        ).fetchone()
        jobs = connection.execute(
            """SELECT id, proxy_profile_id, status, stage FROM publish_jobs WHERE id = ?
               UNION ALL
               SELECT id, proxy_profile_id, status, stage FROM collect_jobs WHERE id = ?""",
            ("phase5-ui-publish", "phase5-ui-collect"),
        ).fetchall()
    assert direct_pool is not None and direct_pool["source_type"] == "direct"
    direct_id = int(direct_pool["id"])
    profile = json.loads(account["profile_json"] or "{}")
    assert int(profile["proxy_binding"]["proxy_profile_id"]) == direct_id
    assert profile["browser_settings"]["proxy_server"].endswith(str(profile["proxy_binding"]["local_port"]))
    assert {(row["id"], row["status"], row["stage"]) for row in jobs} == {
        ("phase5-ui-publish", "queued", "proxy_rebound"),
        ("phase5-ui-collect", "queued", "proxy_rebound"),
    }
    assert {int(row["proxy_profile_id"]) for row in jobs} == {direct_id}


def exercise_ui(
    browser: Browser,
    base_url: str,
    viewport_name: str,
    viewport: dict[str, int],
    artifact_dir: Path,
    pool_id: int,
    after_delete: Callable[[], None],
) -> None:
    run_dir = artifact_dir / viewport_name
    run_dir.mkdir(parents=True, exist_ok=True)
    context = browser.new_context(viewport=viewport)
    unexpected_requests: list[str] = []
    allowed_origin = urlparse(base_url)

    def block_external(route: Any) -> None:
        target = urlparse(route.request.url)
        if (target.scheme, target.hostname, target.port) == (allowed_origin.scheme, allowed_origin.hostname, allowed_origin.port):
            route.continue_()
            return
        unexpected_requests.append(route.request.url)
        route.abort()

    context.route("**/*", block_external)
    page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        page.goto(f"{base_url}/proxy", wait_until="networkidle")
        identity_modal = page.locator("#ui-global-user-modal")
        if identity_modal.is_visible():
            identity_modal.locator(".ui-global-user-panel [data-global-user-close]").click()
            expect(identity_modal).to_be_hidden()
        pool_row = page.locator(f'[data-proxy-pool-detail="{pool_id}"]')
        if viewport_name == "mobile":
            expect(pool_row).to_be_hidden()
        else:
            expect(pool_row).to_be_visible()
        page.screenshot(path=str(run_dir / "initial.png"), full_page=False, animations="disabled")
        (run_dir / "initial-dom.html").write_text(page.content(), encoding="utf-8")

        if viewport_name == "mobile":
            # SYS-004: no visible delete entry below 820px; exercise the real HTTP boundary.
            deleted = context.request.post(f"{base_url}/api/proxy/pools/delete", data={"id": pool_id})
            assert deleted.status == 200
            page.get_by_role("button", name="刷新运行状态").click()
        else:
            pool_row.click()
            delete_button = page.locator(f'[data-proxy-pool-delete="{pool_id}"]')
            expect(delete_button).to_be_visible()
            page.once("dialog", lambda dialog: dialog.accept())
            with page.expect_response(lambda response: response.url == f"{base_url}/api/proxy/pools/delete" and response.request.method == "POST") as deleted:
                delete_button.click()
            assert deleted.value.status == 200
        expect(pool_row).to_have_count(0)
        # SYS-003: unchanged UI calls a private success helper, then reopens the missing pool.
        if viewport_name != "mobile":
            expect(page.locator("#pool-drawer")).to_contain_text("该出口信息暂时无法读取")
        page.screenshot(path=str(run_dir / "after-delete.png"), full_page=False, animations="disabled")
        (run_dir / "after-delete-dom.html").write_text(page.content(), encoding="utf-8")
        after_delete()
        if viewport_name != "mobile":
            page.locator("#pool-drawer [data-close]").click()
            expect(page.locator("#pool-overlay")).not_to_have_class(re.compile(r".*\bopen\b.*"))

        binding_action = page.locator("[data-open='binding']").first
        expect(binding_action).to_be_visible()
        binding_action.click()
        expect(page.locator("#drawer-body")).to_contain_text("当前状态：未绑定")
        page.locator("#live-binding-pool").select_option("direct")
        submit = page.locator("[data-live-binding-submit]")
        if viewport_name == "mobile":
            # SYS-004: footer is outside the viewport; retain keyboard activation coverage.
            submit.focus()
            submit.press("Enter")
        else:
            submit.click()
        expect(page.locator("#live-binding-notice")).to_contain_text("2 个等待任务已恢复排队")
        page.screenshot(path=str(run_dir / "after-rebind.png"), full_page=False, animations="disabled")
        (run_dir / "after-rebind-dom.html").write_text(page.content(), encoding="utf-8")
        if unexpected_requests:
            raise AssertionError(f"{viewport_name} unexpected external requests: {unexpected_requests}")
        if console_errors or page_errors:
            errors = {"consoleErrors": console_errors, "pageErrors": page_errors}
            raise AssertionError(f"{viewport_name} browser errors: {errors}")
    except Exception as exc:
        page.screenshot(path=str(run_dir / "failed.png"), full_page=False, animations="disabled")
        if unexpected_requests:
            raise AssertionError(f"{viewport_name} unexpected external requests: {unexpected_requests}") from exc
        raise
    finally:
        (run_dir / "console.json").write_text(
            json.dumps(console_errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "page-errors.json").write_text(
            json.dumps(page_errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "unexpected-requests.json").write_text(
            json.dumps(unexpected_requests, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        context.close()


def run_viewport(browser: Browser, web_app: Any, proxy_pool: Any, settings: Any, pools: Any, viewport_name: str, viewport: dict[str, int], artifact_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=f"phase5-proxy-ui-{viewport_name}-") as temporary_name:
        data_dir = Path(temporary_name)
        original_connect = sqlite3.connect
        connections: list[sqlite3.Connection] = []

        def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            kwargs["check_same_thread"] = False
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with ExitStack() as patches:
            patches.enter_context(patch.object(settings, "DATA_DIR", data_dir))
            patches.enter_context(patch.object(settings, "DB_PATH", data_dir / "proxy_pool.sqlite"))
            patches.enter_context(patch.object(sqlite3, "connect", side_effect=tracked_connect))
            patches.enter_context(patch.object(pools, "lookup_ip_geo", return_value={"country": "", "region": "", "city": "", "address": ""}))
            patches.enter_context(patch.object(pools, "_remove_mihomo_pool_config", return_value=({"removed": True}, None)))
            patches.enter_context(patch.object(pools, "_sync_mihomo_pool_config", return_value={"loaded": True}))
            from proxy import runtime
            patches.enter_context(patch.object(runtime, "_http_get_json", return_value=(False, {}, "isolated")))
            patches.enter_context(patch.object(runtime, "_port_open", return_value=False))
            patches.enter_context(patch.object(web_app, "ui_test_mode_allows_live_write", side_effect=lambda path: path.startswith("/api/proxy/")))
            patches.enter_context(patch.object(web_app, "global_user_payload", return_value={"currentUser": {"id": "public", "name": "公开用户"}, "users": []}))
            patches.enter_context(patch.object(web_app, "_feishu_users", return_value=[]))
            patches.enter_context(patch.object(web_app.lan_chat_store, "login_options", return_value={"feishuUsers": []}))
            patches.enter_context(patch.dict(os.environ, {"PROXY_REALITY_CORE": "mihomo"}, clear=False))

            server = None
            thread = None
            try:
                pool_id, account_id = seed_proxy_state(proxy_pool, settings)
                server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
                server.daemon_threads = False
                thread = threading.Thread(target=server.serve_forever, daemon=False)
                thread.start()
                exercise_ui(
                    browser,
                    f"http://127.0.0.1:{server.server_port}",
                    viewport_name,
                    viewport,
                    artifact_dir,
                    pool_id,
                    lambda: assert_after_delete(proxy_pool, pool_id, account_id),
                )
                assert_after_rebind(proxy_pool, account_id)
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if thread is not None:
                    thread.join(timeout=5)
                    assert not thread.is_alive(), "temporary proxy UI server did not stop"
                close_connections(connections)
    assert not data_dir.exists(), f"temporary proxy data remains: {data_dir}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=artifact_directory, required=True)
    args = parser.parse_args()
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)

    original_cwd = Path.cwd()
    original_env = {key: os.environ.get(key) for key in ("APP_TEST_ROOT", "UI_TEST_MODE", "PROXY_POOL_ENABLED")}
    os.chdir(ROOT)
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        with tempfile.TemporaryDirectory(prefix="phase5-proxy-ui-app-") as app_root:
            os.environ["APP_TEST_ROOT"] = app_root
            os.environ["UI_TEST_MODE"] = "1"
            os.environ["PROXY_POOL_ENABLED"] = "1"
            import web_app
            import proxy_pool
            from proxy import pools, settings

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for name, viewport in VIEWPORTS.items():
                        run_viewport(browser, web_app, proxy_pool, settings, pools, name, viewport, args.artifacts_dir)
                finally:
                    browser.close()
    finally:
        os.chdir(original_cwd)
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print(f"phase5 proxy UI verification passed; artifacts: {args.artifacts_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
