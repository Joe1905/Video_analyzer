#!/usr/bin/env python3
"""End-to-end regression for AI chat main/tool-list scroll preservation."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from playwright.async_api import Page, Route, async_playwright


TEST_QUERY = "ui_test_scenario=chat-scroll-regression"
VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "mobile": {"width": 390, "height": 844},
}


def close_enough(actual: float, expected: float, tolerance: float, label: str) -> None:
    if abs(actual - expected) > tolerance:
        raise AssertionError(
            f"{label}: expected {expected:.2f}±{tolerance:.2f}, got {actual:.2f}"
        )


def print_trace_window(trace: list[dict], label: str, size: int = 10) -> None:
    """Keep CI failures readable without dumping the complete browser trace."""
    print(f"[{label}] 最近 {min(size, len(trace))} 个滚动采样：")
    for item in trace[-size:]:
        print(
            "  "
            f"{item.get('phase')} / {item.get('reason')} "
            f"main={item.get('mainTop')}/{item.get('mainMax')} "
            f"tool={item.get('toolTop')}/{item.get('toolMax')} "
            f"count={item.get('toolCount')} message={item.get('messageId')}"
        )


async def install_scroll_monitor(page: Page) -> None:
    await page.evaluate(
        """
        () => {
          const messages = document.getElementById('messages');
          window.__chatScrollTrace = [];
          window.__chatScrollPhase = 'initial-follow';
          let queued = false;
          const sample = (reason) => {
            queued = false;
            const lists = [...messages.querySelectorAll('.msg.assistant .tool-card-list')];
            const list = lists.at(-1) || null;
            const node = list?.closest('.msg') || null;
            window.__chatScrollTrace.push({
              at: performance.now(),
              phase: window.__chatScrollPhase,
              reason,
              mainTop: messages.scrollTop,
              mainMax: Math.max(0, messages.scrollHeight - messages.clientHeight),
              toolTop: list?.scrollTop ?? null,
              toolMax: list ? Math.max(0, list.scrollHeight - list.clientHeight) : null,
              toolCount: list?.querySelectorAll('.tool-card').length ?? 0,
              messageId: node?.dataset.msgId || node?.dataset.clientMsgId || '',
            });
          };
          const queueSample = (reason) => {
            if (queued) return;
            queued = true;
            requestAnimationFrame(() => sample(reason));
          };
          new MutationObserver(() => queueSample('mutation')).observe(messages, {
            childList: true,
            subtree: true,
            characterData: true,
            attributes: true,
          });
          messages.addEventListener('scroll', () => queueSample('main-scroll'), {passive: true});
          messages.addEventListener('scroll', (event) => {
            if (event.target instanceof Element && event.target.matches('.tool-card-list')) {
              queueSample('tool-scroll');
            }
          }, true);
          window.__sampleChatScroll = sample;
          sample('installed');
        }
        """
    )


async def tool_state(page: Page) -> dict:
    return await page.evaluate(
        """
        () => {
          const lists = [...document.querySelectorAll('#messages .msg.assistant .tool-card-list')];
          const list = lists.at(-1);
          if (!list) return {top: 0, max: 0, count: 0};
          return {
            top: list.scrollTop,
            max: Math.max(0, list.scrollHeight - list.clientHeight),
            count: list.querySelectorAll('.tool-card').length,
          };
        }
        """
    )


async def main_anchor(page: Page) -> dict:
    return await page.evaluate(
        """
        () => {
          const messages = document.getElementById('messages');
          const viewport = messages.getBoundingClientRect();
          const node = [...messages.querySelectorAll('.msg')]
            .find(item => item.getBoundingClientRect().bottom > viewport.top + 1);
          return {
            top: messages.scrollTop,
            max: Math.max(0, messages.scrollHeight - messages.clientHeight),
            id: node?.dataset.msgId || node?.dataset.clientMsgId || '',
            offset: node ? node.getBoundingClientRect().top - viewport.top : 0,
          };
        }
        """
    )


async def route_test_ask(route: Route) -> None:
    separator = "&" if "?" in route.request.url else "?"
    await route.continue_(url=f"{route.request.url}{separator}{TEST_QUERY}")


async def run_viewport(browser, api, base_url: str, output_dir: Path, name: str) -> None:
    setup = await api.post(
        f"/api/ui-test/chat-scroll/setup?{TEST_QUERY}", data={}
    )
    if setup.status != 201:
        raise AssertionError(f"setup failed ({setup.status}): {await setup.text()}")
    setup_payload = await setup.json()
    session_id = setup_payload["sessionId"]
    run_dir = output_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)

    context = await browser.new_context(viewport=VIEWPORTS[name])
    page = await context.new_page()
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    await page.route("**/api/chat/ask", route_test_ask)

    try:
        await page.goto(f"{base_url}/amazon", wait_until="domcontentloaded")
        await page.evaluate(
            "sessionId => localStorage.setItem('videoAnalyzer.chat.sessionId.amazon', sessionId)",
            session_id,
        )
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_function(
            "document.querySelectorAll('#messages .msg').length >= 2"
        )
        await page.wait_for_selector("#messages .msg")

        historical_tops = await page.eval_on_selector_all(
            "#messages .msg.assistant .tool-card-list",
            "lists => lists.map(list => list.scrollTop)",
        )
        if any(abs(value) > 2 for value in historical_tops):
            raise AssertionError(
                f"historical tool lists must start at top, got {historical_tops}"
            )

        await install_scroll_monitor(page)
        await page.evaluate(
            """
            () => {
              const box = document.getElementById('messages');
              box.scrollTop = box.scrollHeight;
              window.__sampleChatScroll('before-send');
            }
            """
        )
        await page.locator("#input").fill("执行双滚动条回归测试")
        await page.locator("#sendBtn").click()

        await page.wait_for_function(
            """
            () => {
              const lists = [...document.querySelectorAll('#messages .msg.assistant .tool-card-list')];
              return (lists.at(-1)?.querySelectorAll('.tool-card').length || 0) === 8;
            }
            """,
            timeout=10000,
        )
        following = await tool_state(page)
        if following["max"] <= 10:
            raise AssertionError(f"test tool list did not overflow: {following}")
        if following["max"] - following["top"] > 8:
            raise AssertionError(f"live tool list did not follow bottom: {following}")

        await page.evaluate(
            """
            () => {
              window.__chatScrollPhase = 'manual-preserve';
              const lists = [...document.querySelectorAll('#messages .msg.assistant .tool-card-list')];
              const list = lists.at(-1);
              list.scrollTop = Math.max(1, Math.floor((list.scrollHeight - list.clientHeight) * .42));
              const messages = document.getElementById('messages');
              messages.scrollTop = Math.max(40, Math.floor((messages.scrollHeight - messages.clientHeight) * .45));
              window.__sampleChatScroll('manual-position');
            }
            """
        )
        await page.wait_for_timeout(100)
        manual_tool = await tool_state(page)
        manual_anchor = await main_anchor(page)
        if not manual_anchor["id"]:
            raise AssertionError("main scroll anchor was not found")

        await page.evaluate("refreshCurrentMessages(false)")
        await page.wait_for_timeout(150)
        after_refresh_tool = await tool_state(page)
        after_refresh_anchor = await main_anchor(page)
        close_enough(after_refresh_tool["top"], manual_tool["top"], 2, "tool refresh")
        if after_refresh_anchor["id"] != manual_anchor["id"]:
            raise AssertionError(
                f"main anchor changed after refresh: {manual_anchor['id']} -> {after_refresh_anchor['id']}"
            )
        close_enough(
            after_refresh_anchor["offset"], manual_anchor["offset"], 4, "main refresh anchor"
        )

        await page.wait_for_function(
            """
            () => {
              const lists = [...document.querySelectorAll('#messages .msg.assistant .tool-card-list')];
              return (lists.at(-1)?.querySelectorAll('.tool-card').length || 0) === 9;
            }
            """,
            timeout=10000,
        )
        after_ninth_tool = await tool_state(page)
        after_ninth_anchor = await main_anchor(page)
        close_enough(after_ninth_tool["top"], manual_tool["top"], 2, "ninth tool while paused")
        if after_ninth_anchor["id"] != manual_anchor["id"]:
            raise AssertionError("main anchor changed while automatic following was paused")
        close_enough(
            after_ninth_anchor["offset"], manual_anchor["offset"], 4, "ninth tool main anchor"
        )

        await page.evaluate(
            """
            () => {
              window.__chatScrollPhase = 'resume-follow';
              const messages = document.getElementById('messages');
              messages.scrollTop = messages.scrollHeight;
              const lists = [...messages.querySelectorAll('.msg.assistant .tool-card-list')];
              const list = lists.at(-1);
              list.scrollTop = list.scrollHeight;
              window.__sampleChatScroll('resume-bottom');
            }
            """
        )
        await page.wait_for_function(
            """
            () => {
              const lists = [...document.querySelectorAll('#messages .msg.assistant .tool-card-list')];
              return (lists.at(-1)?.querySelectorAll('.tool-card').length || 0) === 10;
            }
            """,
            timeout=10000,
        )
        tenth_tool = await tool_state(page)
        if tenth_tool["max"] - tenth_tool["top"] > 8:
            raise AssertionError(f"tool list did not resume following: {tenth_tool}")
        tenth_main = await main_anchor(page)
        if tenth_main["max"] - tenth_main["top"] > 8:
            raise AssertionError(f"main list did not resume following: {tenth_main}")

        await page.wait_for_function(
            """
            () => [...document.querySelectorAll('#messages .msg.assistant .bubble')]
              .some(node => node.textContent.includes('滚动回归测试完成'))
            """,
            timeout=10000,
        )
        await page.evaluate("window.__sampleChatScroll('completed')")
        trace = await page.evaluate("window.__chatScrollTrace")

        initial_trace = [
            item
            for item in trace
            if item["phase"] == "initial-follow"
            and item["toolMax"] is not None
            and item["toolMax"] > 4
        ]
        seen_positive = False
        for item in initial_trace:
            if item["toolTop"] and item["toolTop"] > 2:
                seen_positive = True
            if seen_positive and item["toolTop"] is not None and item["toolTop"] < 2:
                raise AssertionError(f"tool list reset to zero during follow: {item}")

        manual_trace = [
            item
            for item in trace
            if item["phase"] == "manual-preserve" and item["toolCount"] == 8
        ]
        for item in manual_trace[1:]:
            close_enough(item["toolTop"], manual_tool["top"], 2, "duplicate update trace")

        (run_dir / "scroll-trace.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await page.screenshot(path=str(run_dir / "final.png"), full_page=False)
        if console_errors:
            raise AssertionError(f"console errors: {console_errors}")
    except Exception:
        try:
            trace = await page.evaluate("window.__chatScrollTrace || []")
            (run_dir / "scroll-trace-failed.json").write_text(
                json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print_trace_window(trace, f"{name} failure")
            await page.screenshot(path=str(run_dir / "failed.png"), full_page=False)
        except Exception:
            pass
        raise
    finally:
        await context.close()
        await api.post(
            f"/api/ui-test/chat-scroll/cleanup?{TEST_QUERY}",
            data={"sessionId": session_id},
        )


async def main(base_url: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        api = await playwright.request.new_context(base_url=base_url)
        browser = await playwright.chromium.launch(headless=True)
        try:
            for name in VIEWPORTS:
                await run_viewport(browser, api, base_url, output_dir, name)
        finally:
            await browser.close()
            await api.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.1.254:4004")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output-dev") / "ui-scroll-regression" / str(int(time.time())),
    )
    args = parser.parse_args()
    asyncio.run(main(args.base_url.rstrip("/"), args.output_dir))
