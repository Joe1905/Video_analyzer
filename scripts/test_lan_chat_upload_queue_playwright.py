#!/usr/bin/env python3
"""Playwright regression checks for the LAN chat multi-file upload queue."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from playwright.async_api import Route, async_playwright, expect


AVATAR = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"


async def wait_until(predicate, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.05)


class MockLanChatApi:
    def __init__(self, *, fail_once: set[str] | None = None, delay: float = 0.0):
        self.fail_once = set(fail_once or set())
        self.failed: set[str] = set()
        self.delay = delay
        self.messages: dict[str, list[dict]] = {"public": [], "group-1": []}
        self.upload_names: list[str] = []
        self.upload_bodies: list[str] = []
        self.active_uploads = 0
        self.max_active_uploads = 0
        self.next_message_id = 1

    @staticmethod
    def bootstrap_payload() -> dict:
        user = {
            "id": "user-1",
            "nickname": "测试手机",
            "avatarUrl": AVATAR,
            "avatarStatus": "ready",
            "isCurrent": True,
            "online": True,
            "lastSeen": time.time(),
        }
        return {
            "currentUser": user,
            "users": [user],
            "rooms": [
                {
                    "id": "public",
                    "kind": "public",
                    "systemKind": "public",
                    "name": "公共频道",
                    "memberCount": 1,
                    "members": [user],
                    "unreadCount": 0,
                    "latestMessage": None,
                    "pinned": False,
                    "muted": False,
                    "recentFiles": [],
                },
                {
                    "id": "group-1",
                    "kind": "group",
                    "systemKind": "custom",
                    "name": "测试群",
                    "memberCount": 1,
                    "members": [user],
                    "unreadCount": 0,
                    "latestMessage": None,
                    "currentUserIsAdmin": True,
                    "canRename": True,
                    "canRemoveMembers": True,
                    "canTransferAdmin": False,
                    "canLeave": True,
                    "canDissolve": True,
                    "pinned": False,
                    "muted": False,
                    "recentFiles": [],
                },
            ],
            "publicRoomId": "public",
            "inlineMediaMaxBytes": 100 * 1024 * 1024,
            "inlineMediaRetentionSeconds": 7 * 86400,
            "fileMaxBytes": 10 * 1024 * 1024 * 1024,
            "fileRetentionSeconds": 7 * 86400,
            "messagePollIntervalMs": 3000,
            "bootstrapPollIntervalMs": 10000,
        }

    async def handle(self, route: Route) -> None:
        request = route.request
        path = request.url.split("?", 1)[0]
        if path.endswith("/api/lan-chat/bootstrap"):
            await route.fulfill(json=self.bootstrap_payload())
            return
        if path.endswith("/api/lan-chat/events"):
            await route.fulfill(
                status=200,
                content_type="text/event-stream",
                body=": mock stream ready\n\n",
            )
            return
        message_match = re.search(r"/api/lan-chat/rooms/([^/]+)/messages$", path)
        if message_match and request.method == "GET":
            room_id = message_match.group(1)
            messages = self.messages.get(room_id, [])
            await route.fulfill(
                json={"messages": messages, "lastId": messages[-1]["id"] if messages else 0}
            )
            return
        file_match = re.search(r"/api/lan-chat/rooms/([^/]+)/files$", path)
        if file_match and request.method == "POST":
            await self._handle_upload(route, file_match.group(1))
            return
        await route.fulfill(status=404, json={"error": "mock route not found"})

    async def _handle_upload(self, route: Route, room_id: str) -> None:
        body = (route.request.post_data_buffer or b"").decode("utf-8", "replace")
        filename_match = re.search(r'filename="([^"]+)"', body)
        upload_id_match = re.search(
            r'name="clientUploadId"\r\n\r\n([^\r\n]+)', body
        )
        filename = filename_match.group(1) if filename_match else "unknown.bin"
        client_upload_id = upload_id_match.group(1) if upload_id_match else ""
        self.upload_names.append(filename)
        self.upload_bodies.append(body)
        self.active_uploads += 1
        self.max_active_uploads = max(self.max_active_uploads, self.active_uploads)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if filename in self.fail_once and filename not in self.failed:
                self.failed.add(filename)
                await route.fulfill(status=400, json={"error": "模拟的 4xx 失败"})
                return
            message = {
                "id": self.next_message_id,
                "roomId": room_id,
                "clientUploadId": client_upload_id,
                "senderId": "user-1",
                "senderName": "测试手机",
                "senderAvatarUrl": AVATAR,
                "content": "批次说明" if "批次说明" in body else "",
                "imageUrl": "",
                "mediaUrl": "",
                "mediaPosterUrl": "",
                "mediaDownloadUrl": "",
                "mediaMimeType": "",
                "mediaKind": "",
                "mediaExpiresAt": None,
                "mediaExpired": False,
                "file": {
                    "id": f"file-{self.next_message_id}",
                    "name": filename,
                    "mimeType": "text/plain",
                    "size": 4,
                    "expiresAt": time.time() + 7 * 86400,
                    "expired": False,
                    "requiresAcceptance": False,
                    "receiptStatus": "available",
                    "downloadAllowed": True,
                    "downloadUrl": f"/mock/{self.next_message_id}",
                },
                "createdAt": time.time(),
                "isMine": True,
            }
            self.next_message_id += 1
            self.messages.setdefault(room_id, []).append(message)
            await route.fulfill(status=201, json={"message": message, "created": True})
        finally:
            self.active_uploads -= 1


async def open_chat(browser, base_url: str, viewport: dict, api: MockLanChatApi):
    context = await browser.new_context(viewport=viewport)
    await context.add_init_script(
        "localStorage.setItem('videoAnalyzer.lanChat.sessionToken.v2','test-token')"
    )
    page = await context.new_page()
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    await page.route("**/api/lan-chat/**", api.handle)
    await page.goto(f"{base_url}/lan-chat", wait_until="domcontentloaded")
    await expect(page.locator("#loginModal")).not_to_have_class(re.compile("show"))
    return context, page, console_errors


async def desktop_scenario(browser, base_url: str, screenshot_dir: Path) -> None:
    api = MockLanChatApi(delay=0.15)
    context, page, console_errors = await open_chat(
        browser, base_url, {"width": 1280, "height": 820}, api
    )
    try:
        file_input = page.locator("#imageInput")
        assert await file_input.get_attribute("multiple") is not None

        await file_input.set_input_files(
            [{"name": "single.txt", "mimeType": "text/plain", "buffer": b"one"}]
        )
        await expect(page.locator("#imageDraft")).to_be_visible()
        assert not api.upload_names
        await page.locator("#removeImage").click()

        await page.locator("#messageInput").fill("批次说明")
        await file_input.set_input_files(
            [
                {"name": "a.txt", "mimeType": "text/plain", "buffer": b"a"},
                {"name": "b.txt", "mimeType": "text/plain", "buffer": b"b"},
                {"name": "c.txt", "mimeType": "text/plain", "buffer": b"c"},
            ]
        )
        await expect(page.locator("#uploadQueue")).to_be_visible()
        await expect(page.locator(".queue-placeholder")).to_have_count(3)
        await expect(page.locator("#queueSummaryStatus")).to_contain_text("上传中")
        await expect(page.locator(".queue-progress").first).to_be_visible()
        await page.locator('[data-room-id="group-1"]').click()
        await expect(page.locator("#queueSummaryText")).to_contain_text("公共频道")
        await expect(page.locator(".queue-placeholder")).to_have_count(0)
        await wait_until(lambda: len(api.upload_names) == 3)
        assert api.upload_names == ["a.txt", "b.txt", "c.txt"]
        assert api.max_active_uploads == 1
        assert sum("批次说明" in body for body in api.upload_bodies) == 1
        await page.locator('[data-room-id="public"]').click()
        await expect(page.locator("[data-message-id]")).to_have_count(3)
        await page.evaluate("mergeServerMessage(state.messages[0]); renderMessages()")
        await expect(page.locator("[data-message-id]")).to_have_count(3)
        await page.screenshot(path=str(screenshot_dir / "lan-chat-upload-queue-desktop.png"))
        assert not console_errors, console_errors
    finally:
        await context.close()


async def mobile_network_retry_scenario(
    browser, base_url: str, screenshot_dir: Path
) -> None:
    api = MockLanChatApi(fail_once={"retry.txt"})
    context, page, console_errors = await open_chat(
        browser, base_url, {"width": 390, "height": 844}, api
    )
    try:
        assert await page.evaluate(
            "document.body.scrollWidth === document.body.clientWidth"
        )
        await expect(page.locator("#headerRoomName")).to_be_visible()
        await page.locator("#mobileChannels").click()
        await expect(page.locator("#leftRail")).to_have_class(re.compile("open"))
        await page.wait_for_timeout(240)
        left_rect = await page.locator("#leftRail").bounding_box()
        assert left_rect is not None
        assert abs(left_rect["x"]) < 1 and left_rect["width"] >= 320, left_rect
        await page.screenshot(
            path=str(screenshot_dir / "lan-chat-mobile-channels.png")
        )
        await page.locator("#drawerBackdrop").click(position={"x": 380, "y": 100})
        await page.locator("#mobileMembers").click()
        await expect(page.locator("#rightRail")).to_have_class(re.compile("open"))
        await page.wait_for_timeout(240)
        right_rect = await page.locator("#rightRail").bounding_box()
        assert right_rect is not None
        assert right_rect["x"] <= 70 and right_rect["width"] >= 320, right_rect
        await page.screenshot(
            path=str(screenshot_dir / "lan-chat-mobile-details.png")
        )
        await page.locator("#drawerBackdrop").click(position={"x": 10, "y": 100})
        await context.set_offline(True)
        await page.locator("#messageInput").fill("批次说明")
        await page.locator("#imageInput").set_input_files(
            [
                {"name": "cancel.txt", "mimeType": "text/plain", "buffer": b"x"},
                {"name": "retry.txt", "mimeType": "text/plain", "buffer": b"y"},
                {"name": "ok.txt", "mimeType": "text/plain", "buffer": b"z"},
            ]
        )
        await expect(page.locator("#queueSummaryStatus")).to_have_text("等待网络")
        await expect(page.locator(".queue-placeholder-status").first).to_contain_text(
            "等待网络"
        )
        await page.screenshot(path=str(screenshot_dir / "lan-chat-upload-queue-mobile.png"))
        await page.locator("[data-queue-cancel]").first.click()
        await context.set_offline(False)
        await expect(page.locator("[data-queue-retry]").first).to_be_visible(timeout=8000)
        await page.locator("[data-queue-retry]").first.click()
        await wait_until(lambda: len(api.messages["public"]) == 2)
        await expect(page.locator("[data-message-id]")).to_have_count(2)
        assert api.upload_names == ["retry.txt", "ok.txt", "retry.txt"]
        assert sum("批次说明" in body for body in api.upload_bodies) == 2
        assert api.max_active_uploads == 1
        unexpected_errors = [
            error for error in console_errors if "400 (Bad Request)" not in error
        ]
        assert not unexpected_errors, unexpected_errors
    finally:
        await context.close()


async def main(base_url: str, screenshot_dir: Path) -> None:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            await desktop_scenario(browser, base_url, screenshot_dir)
            await mobile_network_retry_scenario(browser, base_url, screenshot_dir)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4003")
    parser.add_argument("--screenshot-dir", type=Path, default=Path("/tmp"))
    arguments = parser.parse_args()
    asyncio.run(main(arguments.base_url, arguments.screenshot_dir))
