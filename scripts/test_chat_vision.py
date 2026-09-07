#!/usr/bin/env python3
"""Focused checks for native DeepSeek chat image messages."""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import web_app
from chat_session import Message


def test_chat_attachments_are_sent_as_native_image_parts() -> None:
    original_dir = web_app.CHAT_ATTACHMENT_DIR
    payload = b"small-png-payload"
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            web_app.CHAT_ATTACHMENT_DIR = Path(temp_dir)
            attachments = web_app.process_chat_attachments([{
                "name": "chart.png",
                "dataUrl": "data:image/png;base64," + base64.b64encode(payload).decode("ascii"),
            }], "分析这张图")

            assert "ocr_text" not in attachments[0]
            message = Message(id="u1", role="user", content="分析这张图", attachments=attachments)
            content = web_app.chat_message_content_for_model(message, include_images=True)

            assert content[0] == {"type": "text", "text": "分析这张图"}
            assert content[1]["type"] == "image_url"
            assert content[1]["image_url"]["detail"] == "auto"
            assert content[1]["image_url"]["url"] == (
                "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
            )
    finally:
        web_app.CHAT_ATTACHMENT_DIR = original_dir


def test_history_keeps_only_the_latest_image_message_native() -> None:
    original_dir = web_app.CHAT_ATTACHMENT_DIR
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            web_app.CHAT_ATTACHMENT_DIR = Path(temp_dir)
            first = web_app.process_chat_attachments([{
                "name": "first.png",
                "dataUrl": "data:image/png;base64," + base64.b64encode(b"first").decode("ascii"),
            }], "第一张")
            latest = web_app.process_chat_attachments([{
                "name": "latest.png",
                "dataUrl": "data:image/png;base64," + base64.b64encode(b"latest").decode("ascii"),
            }], "第二张")
            messages = [
                Message(id="u1", role="user", content="第一张", attachments=first),
                Message(id="a1", role="assistant", content="收到"),
                Message(id="u2", role="user", content="第二张", attachments=latest),
                Message(id="a2", role="assistant", content="", status="pending"),
            ]

            history, _ = web_app.build_chat_history_context(messages, "a2")
            native = [item for item in history if isinstance(item["content"], list)]

            assert len(native) == 1
            assert native[0]["content"][0]["text"] == "第二张"
            assert native[0]["_context_priority"] == "keep"
            assert web_app.chat_messages_have_images(history) is True
            assert web_app.estimate_chat_context_tokens(history, []) < 2000
    finally:
        web_app.CHAT_ATTACHMENT_DIR = original_dir


if __name__ == "__main__":
    test_chat_attachments_are_sent_as_native_image_parts()
    test_history_keeps_only_the_latest_image_message_native()
    print("chat vision tests passed")
