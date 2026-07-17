#!/usr/bin/env python3
"""Ad-hoc verification for LAN chat media and temporary file transfers."""

from __future__ import annotations

import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from lan_chat import (
    DEFAULT_FEISHU_USER_ID,
    FILE_TRANSFER_MAX_BYTES,
    FILE_TRANSFER_RETENTION_SECONDS,
    MESSAGE_MEDIA_MAX_BYTES,
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
        self.temp_dir.cleanup()

    def test_group_file_is_immediately_available_and_expires(self) -> None:
        room = self.store.create_group(
            self.sender["sessionToken"],
            "文件群",
            [self.receiver["user"]["id"]],
        )
        message = self.store.send_file(
            self.sender["sessionToken"],
            room["id"],
            "报表 2026.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            BytesIO(b"group-file"),
            "请查收",
        )

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
        message = self.store.send_file(
            self.sender["sessionToken"],
            room["id"],
            "方案.pdf",
            "application/pdf",
            BytesIO(b"direct-file"),
        )
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
        self.assertEqual(bootstrap["fileMaxBytes"], 10 * 1024 * 1024 * 1024)
        self.assertEqual(FILE_TRANSFER_MAX_BYTES, bootstrap["fileMaxBytes"])
        payload = b"\x00\x00\x00\x18ftypisom" + b"small-video"
        media_data = "data:video/mp4;base64," + base64.b64encode(payload).decode("ascii")
        message = self.store.send_message(
            self.sender["sessionToken"], "public", "", media_data
        )
        self.assertEqual(message["mediaKind"], "video")
        self.assertTrue(message["mediaUrl"].endswith(".mp4"))
        filename = message["mediaUrl"].rsplit("/", 1)[-1]
        body, content_type = self.store.message_media_bytes(filename)
        self.assertEqual(body, payload)
        self.assertEqual(content_type, "video/mp4")


if __name__ == "__main__":
    unittest.main()
