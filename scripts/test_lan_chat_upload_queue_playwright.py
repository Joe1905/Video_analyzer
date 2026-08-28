#!/usr/bin/env python3
"""Playwright regression checks for the LAN chat multi-file upload queue."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import time
from pathlib import Path

from playwright.async_api import Route, async_playwright, expect


AVATAR = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2n1cAAAAASUVORK5CYII="
)
TEST_GLOBAL_USER = {
    "id": "test-owner",
    "name": "测试飞书用户",
    "kind": "feishu",
}


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
        self.archive_members: list[list[str]] = []
        self.media_request_cookies: list[str] = []
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

        second = {
            "id": "user-2",
            "nickname": "产品同事",
            "avatarUrl": AVATAR,
            "avatarStatus": "ready",
            "isCurrent": False,
            "online": True,
            "lastSeen": time.time(),
        }
        third = {
            "id": "user-3",
            "nickname": "设计同事",
            "avatarUrl": AVATAR,
            "avatarStatus": "ready",
            "isCurrent": False,
            "online": False,
            "lastSeen": time.time() - 3600,
        }
        users = [user, second, third]
        return {
            "currentUser": user,
            "users": users,
            "rooms": [
                {
                    "id": "public",
                    "kind": "public",
                    "systemKind": "public",
                    "name": "公共频道",
                    "memberCount": len(users),
                    "members": users,
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
                    "memberCount": len(users),
                    "members": users,
                    "unreadCount": 0,
                    "latestMessage": None,
                    "currentUserIsAdmin": True,
                    "canRename": True,
                    "canRemoveMembers": True,
                    "canTransferAdmin": True,
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

    @staticmethod
    async def handle_global_user(route: Route) -> None:
        await route.fulfill(
            json={"currentUser": TEST_GLOBAL_USER, "users": [TEST_GLOBAL_USER]}
        )

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
        media_get_match = re.search(
            r"/api/lan-chat/media/[0-9a-f]{32}\.jpg(?:/download)?$", path
        )
        if media_get_match and request.method == "GET":
            cookie = request.headers.get("cookie", "")
            self.media_request_cookies.append(cookie)
            if "video_analyzer_lan_chat_media=test-token" not in cookie:
                await route.fulfill(status=401, json={"error": "missing media cookie"})
                return
            await route.fulfill(body=PNG_BYTES, content_type="image/png")
            return
        message_match = re.search(r"/api/lan-chat/rooms/([^/]+)/messages$", path)
        if message_match and request.method == "GET":
            room_id = message_match.group(1)
            messages = self.messages.get(room_id, [])
            await route.fulfill(
                json={"messages": messages, "lastId": messages[-1]["id"] if messages else 0}
            )
            return
        archive_match = re.search(
            r"/api/lan-chat/rooms/([^/]+)/file-archives$", path
        )
        if archive_match and request.method == "POST":
            await self._handle_upload(route, archive_match.group(1), archive=True)
            return
        media_match = re.search(r"/api/lan-chat/rooms/([^/]+)/media$", path)
        if media_match and request.method == "POST":
            await self._handle_upload(route, media_match.group(1), media=True)
            return
        file_match = re.search(r"/api/lan-chat/rooms/([^/]+)/files$", path)
        if file_match and request.method == "POST":
            await self._handle_upload(route, file_match.group(1))
            return
        await route.fulfill(status=404, json={"error": "mock route not found"})

    async def _handle_upload(
        self,
        route: Route,
        room_id: str,
        *,
        archive: bool = False,
        media: bool = False,
    ) -> None:
        body = (route.request.post_data_buffer or b"").decode("utf-8", "replace")
        filenames = re.findall(r'filename="([^"]+)"', body)
        archive_name_match = re.search(
            r'name="archiveName"\r\n\r\n([^\r\n]+)', body
        )
        upload_id_match = re.search(
            r'name="clientUploadId"\r\n\r\n([^\r\n]+)', body
        )
        filename = (
            archive_name_match.group(1)
            if archive and archive_name_match
            else filenames[0] if filenames else "unknown.bin"
        )
        if archive:
            self.archive_members.append(filenames)
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
            media_id = f"{self.next_message_id:032x}.jpg" if media else ""
            message = {
                "id": self.next_message_id,
                "roomId": room_id,
                "clientUploadId": client_upload_id,
                "senderId": "user-1",
                "senderName": "测试手机",
                "senderAvatarUrl": AVATAR,
                "content": "批次说明" if "批次说明" in body else "",
                "imageUrl": f"/api/lan-chat/media/{media_id}" if media else "",
                "mediaUrl": f"/api/lan-chat/media/{media_id}" if media else "",
                "mediaPosterUrl": "",
                "mediaDownloadUrl": (
                    f"/api/lan-chat/media/{media_id}/download" if media else ""
                ),
                "mediaMimeType": "image/jpeg" if media else "",
                "mediaKind": "image" if media else "",
                "mediaExpiresAt": time.time() + 7 * 86400 if media else None,
                "mediaExpired": False,
                "file": None if media else {
                    "id": f"file-{self.next_message_id}",
                    "name": filename,
                    "mimeType": "application/zip" if archive else "text/plain",
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
        "localStorage.setItem('videoAnalyzer.lanChat.sessionToken.v3.test-owner','test-token');"
        "localStorage.setItem('ui-global-user-picker-completed','1')"
    )
    page = await context.new_page()
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    await page.route("**/api/global-user", MockLanChatApi.handle_global_user)
    await page.route("**/api/lan-chat/**", api.handle)
    target_url = (
        base_url
        if base_url.rstrip("/").endswith((".html", "/lan-chat"))
        else f"{base_url.rstrip('/')}/lan-chat"
    )
    await page.goto(target_url, wait_until="domcontentloaded")
    await expect(page.locator("#loginModal")).not_to_have_class(re.compile("show"))
    await expect(page.locator("body")).not_to_have_class(re.compile("lan-read-only"))
    await expect(page.locator("#newChat")).to_be_visible()
    return context, page, console_errors


async def desktop_scenario(browser, base_url: str, screenshot_dir: Path) -> None:
    api = MockLanChatApi(delay=0.15)
    context, page, console_errors = await open_chat(
        browser, base_url, {"width": 1280, "height": 820}, api
    )
    try:
        await page.locator("#newChat").click()
        picker_inputs = page.locator("#groupMemberPicker input")
        await expect(picker_inputs).to_have_count(2)
        await expect(page.locator("#groupNameField")).to_be_hidden()
        await picker_inputs.nth(0).check()
        await expect(page.locator("#createConversationButton")).to_have_text(
            "开始私聊"
        )
        await expect(page.locator("#groupNameField")).to_be_hidden()
        await picker_inputs.nth(1).check()
        await expect(page.locator("#createConversationButton")).to_have_text(
            "创建群组"
        )
        await expect(page.locator("#groupNameField")).to_be_visible()
        await page.locator("#groupModal .modal-head [data-close-modal]").click()

        file_input = page.locator("#imageInput")
        assert await file_input.get_attribute("multiple") is not None

        await file_input.set_input_files(
            [{"name": "single.txt", "mimeType": "text/plain", "buffer": b"one"}]
        )
        await expect(page.locator("#imageDraft")).to_be_visible()
        await expect(page.locator("#archiveOption")).to_be_hidden()
        assert not api.upload_names
        await page.locator("#removeImage").click()

        await file_input.set_input_files(
            [
                {"name": "one.png", "mimeType": "image/png", "buffer": PNG_BYTES},
                {"name": "two.png", "mimeType": "image/png", "buffer": PNG_BYTES},
                {"name": "three.png", "mimeType": "image/png", "buffer": PNG_BYTES},
            ]
        )
        await expect(page.locator("#imageDraft")).to_be_visible()
        await expect(page.locator("#imageDraftName")).to_have_text("已选择 3 张图片")
        await expect(page.locator("#draftHint")).to_contain_text("点发送后逐个发送")
        await expect(page.locator("#archiveOption")).to_be_visible()
        await expect(page.locator("#archiveFiles")).not_to_be_checked()
        await expect(page.locator("#draftPreview .draft-thumb img")).to_have_count(3)
        assert not api.upload_names
        await page.locator("#archiveFiles").check()
        await expect(page.locator("#imageDraftName")).to_have_text(
            re.compile(r"邻聊文件-\d{8}-\d{6}\.zip")
        )
        await expect(page.locator("#draftHint")).to_contain_text(
            "将作为 1 个 ZIP 压缩包发送"
        )
        await expect(page.locator("#draftPreview")).to_have_text("ZIP")
        await page.locator("#archiveFiles").uncheck()
        await expect(page.locator("#imageDraftName")).to_have_text("已选择 3 张图片")
        await expect(page.locator("#draftPreview .draft-thumb img")).to_have_count(3)
        await page.screenshot(
            path=str(screenshot_dir / "lan-chat-multi-image-draft-desktop.png")
        )
        await page.locator("#sendButton").click()
        await wait_until(lambda: len(api.upload_names) == 3)
        await expect(page.locator("[data-message-id]")).to_have_count(3)
        await expect(page.locator(".message-image img")).to_have_count(3)
        await page.wait_for_function(
            "[...document.querySelectorAll('.message-image img')].every(img => img.complete && img.naturalWidth > 0)"
        )
        assert api.media_request_cookies
        assert all(
            "video_analyzer_lan_chat_media=test-token" in cookie
            for cookie in api.media_request_cookies
        )

        await page.locator("#messageInput").fill("批次说明")
        await file_input.set_input_files(
            [
                {"name": "a.txt", "mimeType": "text/plain", "buffer": b"a"},
                {"name": "b.txt", "mimeType": "text/plain", "buffer": b"b"},
                {"name": "c.txt", "mimeType": "text/plain", "buffer": b"c"},
            ]
        )
        await expect(page.locator("#imageDraft")).to_be_visible()
        await expect(page.locator("#archiveOption")).to_be_visible()
        await expect(page.locator("#archiveFiles")).not_to_be_checked()
        assert not api.upload_names
        await page.locator("#archiveFiles").check()
        await expect(page.locator("#imageDraftName")).to_have_text(
            re.compile(r"邻聊文件-\d{8}-\d{6}\.zip")
        )
        await expect(page.locator("#draftHint")).to_contain_text(
            "将作为 1 个 ZIP 压缩包发送"
        )
        await page.locator("#sendButton").click()
        await expect(page.locator("#uploadQueue")).to_be_visible()
        await expect(page.locator(".queue-placeholder")).to_have_count(1)
        await expect(page.locator("#queueSummaryStatus")).to_contain_text("上传中")
        await expect(page.locator(".queue-progress").first).to_be_visible()
        await page.locator('[data-room-id="group-1"]').click()
        await expect(page.locator("#queueSummaryText")).to_contain_text("公共频道")
        await expect(page.locator(".queue-placeholder")).to_have_count(0)
        await wait_until(lambda: len(api.upload_names) == 4)
        assert re.fullmatch(r"邻聊文件-\d{8}-\d{6}\.zip", api.upload_names[-1])
        assert api.archive_members == [["a.txt", "b.txt", "c.txt"]]
        assert api.max_active_uploads == 1
        assert sum("批次说明" in body for body in api.upload_bodies) == 1
        await page.locator('[data-room-id="public"]').click()
        await expect(page.locator("[data-message-id]")).to_have_count(4)
        await expect(page.locator(".file-card strong")).to_have_text(
            re.compile(r"邻聊文件-\d{8}-\d{6}\.zip")
        )
        await page.evaluate("mergeServerMessage(state.messages.at(-1)); renderMessages()")
        await expect(page.locator("[data-message-id]")).to_have_count(4)
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
        await expect(page.locator("#archiveOption")).to_be_visible()
        await expect(page.locator("#archiveFiles")).not_to_be_checked()
        await page.locator("#sendButton").click()
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
    parser.add_argument("--base-url", default="http://127.0.0.1:4004")
    parser.add_argument(
        "--output-dir",
        "--screenshot-dir",
        dest="output_dir",
        type=Path,
        default=Path("/tmp"),
    )
    arguments = parser.parse_args()
    asyncio.run(main(arguments.base_url.rstrip("/"), arguments.output_dir))
