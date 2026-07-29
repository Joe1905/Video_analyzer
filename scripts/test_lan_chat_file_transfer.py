#!/usr/bin/env python3
"""Ad-hoc verification for LAN chat media and temporary file transfers."""

from __future__ import annotations

import base64
import gc
import sqlite3
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path

from lan_chat import (
    DEFAULT_FEISHU_USER_ID,
    FILE_TRANSFER_MAX_BYTES,
    FILE_TRANSFER_RETENTION_SECONDS,
    MESSAGE_MEDIA_MAX_BYTES,
    MESSAGE_MEDIA_RETENTION_SECONDS,
    LanChatError,
    LanChatStore,
)


class LanChatFileTransferTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = LanChatStore(root / "lan_chat.sqlite")
        self.store.initialize()
        self.sender = self.store.create_account(DEFAULT_FEISHU_USER_ID, "发送者")
        self.receiver = self.store.create_account(DEFAULT_FEISHU_USER_ID, "接收者")

    def tearDown(self) -> None:
        gc.collect()
        self.temp_dir.cleanup()

    def test_initialize_migrates_legacy_inline_media_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.sqlite"
            created_at = time.time()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """CREATE TABLE messages (
                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                           room_id TEXT NOT NULL,
                           sender_id TEXT NOT NULL,
                           content TEXT NOT NULL,
                           image_filename TEXT,
                           image_mime_type TEXT,
                           file_id TEXT,
                           created_at REAL NOT NULL
                       )"""
                )
                conn.execute(
                    """INSERT INTO messages
                       (room_id, sender_id, content, image_filename,
                        image_mime_type, file_id, created_at)
                       VALUES ('public', 'legacy', '', ?, 'image/jpeg', NULL, ?)""",
                    ("0" * 32 + ".jpg", created_at),
                )
            conn.close()

            store = LanChatStore(db_path)
            store.initialize()
            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(messages)")
                }
                expires_at, deleted_at = conn.execute(
                    "SELECT media_expires_at, media_deleted_at FROM messages"
                ).fetchone()
            conn.close()
            self.assertIn("media_expires_at", columns)
            self.assertIn("media_deleted_at", columns)
            self.assertIn("client_upload_id", columns)
            self.assertEqual(expires_at, created_at + MESSAGE_MEDIA_RETENTION_SECONDS)
            self.assertIsNone(deleted_at)

    def test_group_file_is_immediately_available_and_expires(self) -> None:
        room = self.store.create_group(
            self.sender["sessionToken"],
            "文件群",
            [self.receiver["user"]["id"]],
        )
        message, created = self.store.send_file(
            self.sender["sessionToken"],
            room["id"],
            "报表 2026.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            BytesIO(b"group-file"),
            "请查收",
        )
        self.assertTrue(created)

        attachment = message["file"]
        self.assertEqual(attachment["receiptStatus"], "available")
        self.assertTrue(attachment["downloadAllowed"])
        received = self.store.list_messages(
            self.receiver["sessionToken"], room["id"]
        )["messages"][0]["file"]
        self.assertFalse(received["requiresAcceptance"])
        self.assertTrue(received["downloadAllowed"])

        path, name, content_type, size = self.store.file_download_info(
            self.receiver["sessionToken"], attachment["id"]
        )
        self.assertEqual(path.read_bytes(), b"group-file")
        self.assertEqual(name, "报表 2026.xlsx")
        self.assertEqual(size, len(b"group-file"))
        self.assertTrue(content_type.startswith("application/"))

        cleaned = self.store.cleanup_expired_files(
            message["createdAt"] + FILE_TRANSFER_RETENTION_SECONDS + 1
        )
        self.assertEqual(cleaned, 1)
        self.assertFalse(path.exists())
        expired = self.store.list_messages(
            self.receiver["sessionToken"], room["id"]
        )["messages"][0]["file"]
        self.assertTrue(expired["expired"])
        self.assertFalse(expired["downloadAllowed"])
        with self.assertRaises(LanChatError) as context:
            self.store.file_download_info(
                self.receiver["sessionToken"], attachment["id"]
            )
        self.assertEqual(context.exception.status, 410)

    def test_direct_file_requires_receiver_acceptance(self) -> None:
        room = self.store.open_direct(
            self.sender["sessionToken"], self.receiver["user"]["id"]
        )
        message, created = self.store.send_file(
            self.sender["sessionToken"],
            room["id"],
            "方案.pdf",
            "application/pdf",
            BytesIO(b"direct-file"),
        )
        self.assertTrue(created)
        file_id = message["file"]["id"]
        self.assertEqual(message["file"]["receiptStatus"], "pending")
        self.assertTrue(message["file"]["downloadAllowed"])

        receiver_message = self.store.list_messages(
            self.receiver["sessionToken"], room["id"]
        )["messages"][0]
        self.assertTrue(receiver_message["file"]["requiresAcceptance"])
        self.assertFalse(receiver_message["file"]["downloadAllowed"])
        with self.assertRaises(LanChatError) as context:
            self.store.file_download_info(self.receiver["sessionToken"], file_id)
        self.assertEqual(context.exception.status, 403)

        accepted = self.store.accept_file(self.receiver["sessionToken"], file_id)
        self.assertEqual(accepted["file"]["receiptStatus"], "accepted")
        self.assertTrue(accepted["file"]["downloadAllowed"])
        path, _, _, _ = self.store.file_download_info(
            self.receiver["sessionToken"], file_id
        )
        self.assertEqual(path.read_bytes(), b"direct-file")

    def test_small_mp4_remains_inline_media(self) -> None:
        bootstrap = self.store.bootstrap(self.sender["sessionToken"])
        self.assertEqual(bootstrap["inlineMediaMaxBytes"], 100 * 1024 * 1024)
        self.assertEqual(MESSAGE_MEDIA_MAX_BYTES, bootstrap["inlineMediaMaxBytes"])
        self.assertEqual(
            MESSAGE_MEDIA_RETENTION_SECONDS,
            bootstrap["inlineMediaRetentionSeconds"],
        )
        self.assertEqual(bootstrap["messagePollIntervalMs"], 3000)
        self.assertEqual(bootstrap["bootstrapPollIntervalMs"], 10000)
        self.assertEqual(bootstrap["fileMaxBytes"], 10 * 1024 * 1024 * 1024)
        self.assertEqual(FILE_TRANSFER_MAX_BYTES, bootstrap["fileMaxBytes"])
        payload = b"\x00\x00\x00\x18ftypisom" + b"small-video"
        media_data = "data:video/mp4;base64," + base64.b64encode(payload).decode("ascii")
        message, created = self.store.send_message(
            self.sender["sessionToken"], "public", "", media_data
        )
        self.assertTrue(created)
        self.assertEqual(message["mediaKind"], "video")
        self.assertFalse(message["mediaExpired"])
        self.assertEqual(
            message["mediaExpiresAt"],
            message["createdAt"] + MESSAGE_MEDIA_RETENTION_SECONDS,
        )
        self.assertTrue(message["mediaUrl"].endswith(".mp4"))
        self.assertEqual(message["mediaPosterUrl"], f"{message['mediaUrl']}/poster")
        self.assertEqual(message["mediaDownloadUrl"], f"{message['mediaUrl']}/download")
        filename = message["mediaUrl"].rsplit("/", 1)[-1]
        path, stored_name, stored_type, stored_size = self.store.message_media_info(filename)
        self.assertEqual(stored_name, filename)
        self.assertEqual(stored_type, "video/mp4")
        self.assertEqual(stored_size, len(payload))
        self.assertEqual(path.read_bytes(), payload)
        body, content_type = self.store.message_media_bytes(filename)
        self.assertEqual(body, payload)
        self.assertEqual(content_type, "video/mp4")

        poster_path = path.with_name(f"{path.stem}.poster.jpg")
        poster_path.write_bytes(b"poster")
        cleaned = self.store.cleanup_expired_media(
            message["createdAt"] + MESSAGE_MEDIA_RETENTION_SECONDS + 1
        )
        self.assertEqual(cleaned, 1)
        self.assertFalse(path.exists())
        self.assertFalse(poster_path.exists())
        expired = self.store.list_messages(
            self.sender["sessionToken"], "public"
        )["messages"][0]
        self.assertTrue(expired["mediaExpired"])
        self.assertEqual(expired["mediaKind"], "video")
        self.assertEqual(expired["mediaUrl"], "")
        self.assertEqual(expired["mediaPosterUrl"], "")
        self.assertEqual(expired["mediaDownloadUrl"], "")
        with self.assertRaises(LanChatError) as context:
            self.store.message_media_info(filename)
        self.assertEqual(context.exception.status, 410)

    def test_inline_media_request_uses_multipart_streaming(self) -> None:
        template = (Path(__file__).parent / "static" / "lan_chat.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('isFile?"files":"media"', template)
        self.assertIn('form.append(isFile?"file":"media",prepared.file,prepared.name)', template)
        self.assertNotIn("readAsDataUrl", template)
        self.assertNotIn("mediaData:prepared.dataUrl", template)

    def test_streamed_media_is_paginated_and_emits_events(self) -> None:
        payload = b"\x00\x00\x00\x18ftypisom" + b"streamed-video"
        sent = []
        for index in range(3):
            message, created = self.store.send_media_file(
                self.sender["sessionToken"],
                "public",
                f"clip-{index}.mp4",
                BytesIO(payload),
                f"视频 {index}",
                f"stream_upload_{index:02d}_abcdefghijkl",
            )
            self.assertTrue(created)
            sent.append(message)
        latest = self.store.list_messages(
            self.sender["sessionToken"], "public", limit=2
        )
        self.assertEqual([item["id"] for item in latest["messages"]], [sent[1]["id"], sent[2]["id"]])
        self.assertTrue(latest["hasMoreBefore"])
        older = self.store.list_messages(
            self.sender["sessionToken"], "public", before_id=latest["oldestId"], limit=2
        )
        self.assertEqual([item["id"] for item in older["messages"]], [sent[0]["id"]])
        events = self.store.wait_for_message_events(self.receiver["sessionToken"], 0, 0.01)
        self.assertEqual([event["id"] for event in events], [item["id"] for item in sent])
        filename = sent[0]["mediaUrl"].rsplit("/", 1)[-1]
        path, _, content_type, size = self.store.message_media_info(filename)
        self.assertEqual(content_type, "video/mp4")
        self.assertEqual(size, len(payload))
        self.assertEqual(path.read_bytes(), payload)

    def test_inline_media_client_upload_id_is_idempotent(self) -> None:
        upload_id = "inline_upload_1234567890"
        media_data = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\ninline"
        ).decode("ascii")
        first, first_created = self.store.send_message(
            self.sender["sessionToken"],
            "public",
            "图片",
            media_data,
            upload_id,
        )
        second, second_created = self.store.send_message(
            self.sender["sessionToken"],
            "public",
            "不会覆盖",
            media_data,
            upload_id,
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["clientUploadId"], upload_id)
        self.assertEqual(len(list(self.store.media_dir.iterdir())), 1)
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_direct_file_client_upload_id_does_not_duplicate_receipt(self) -> None:
        room = self.store.open_direct(
            self.sender["sessionToken"], self.receiver["user"]["id"]
        )
        upload_id = "direct_file_1234567890"
        first, first_created = self.store.send_file(
            self.sender["sessionToken"],
            room["id"],
            "原始.pdf",
            "application/pdf",
            BytesIO(b"first-file"),
            "正文",
            upload_id,
        )
        second, second_created = self.store.send_file(
            self.sender["sessionToken"],
            room["id"],
            "重复.pdf",
            "application/pdf",
            BytesIO(b"second-file"),
            "不同正文",
            upload_id,
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["file"]["id"], second["file"]["id"])
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_attachments").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_receipts").fetchone()[0], 1)

    def test_client_upload_id_rejects_cross_room_reuse(self) -> None:
        room = self.store.create_group(
            self.sender["sessionToken"],
            "另一个房间",
            [self.receiver["user"]["id"]],
        )
        upload_id = "cross_room_1234567890"
        self.store.send_message(
            self.sender["sessionToken"], "public", "第一条", "", upload_id
        )
        with self.assertRaises(LanChatError) as context:
            self.store.send_message(
                self.sender["sessionToken"], room["id"], "第二条", "", upload_id
            )
        self.assertEqual(context.exception.status, 409)

    def test_legacy_messages_without_client_upload_id_are_not_deduplicated(self) -> None:
        first, first_created = self.store.send_message(
            self.sender["sessionToken"], "public", "旧客户端"
        )
        second, second_created = self.store.send_message(
            self.sender["sessionToken"], "public", "旧客户端"
        )
        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["clientUploadId"], "")
        self.assertEqual(second["clientUploadId"], "")


if __name__ == "__main__":
    unittest.main()
