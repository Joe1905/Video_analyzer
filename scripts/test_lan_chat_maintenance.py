#!/usr/bin/env python3
"""Regression checks for LAN chat snapshot, audit, and repair tooling."""

from __future__ import annotations

import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path

from lan_chat import DEFAULT_FEISHU_USER_ID, LanChatStore
from lan_chat_maintenance import audit, import_snapshot, repair, snapshot


class LanChatMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "production-data"
        self.source.mkdir()
        self.store = LanChatStore(self.source / "lan_chat.sqlite")
        self.store.initialize()
        self.sender = self.store.create_account(DEFAULT_FEISHU_USER_ID, "发送者")
        payload = b"\x00\x00\x00\x18ftypisom" + b"maintenance"
        self.message, _ = self.store.send_media_file(
            self.sender["sessionToken"],
            "public",
            "clip.mp4",
            BytesIO(payload),
            client_upload_id="maintenance_upload_abcdefghijkl",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_snapshot_sanitizes_auth_and_repair_quarantines_orphans(self) -> None:
        target = self.root / "development-snapshot"
        report = snapshot(self.source, target, sanitize_auth=True)
        self.assertTrue(report["healthy"])
        copied = LanChatStore(target / "lan_chat.sqlite")
        with self.assertRaises(Exception):
            copied.authenticate(self.sender["sessionToken"])
        orphan = target / "lan_chat_media" / "orphan.mp4"
        orphan.write_bytes(b"orphan")
        filename = self.message["mediaUrl"].rsplit("/", 1)[-1]
        (target / "lan_chat_media" / filename).unlink()
        before = audit(target)
        self.assertIn(filename, before["missingMedia"])
        result = repair(
            target,
            self.root / "backup-before-repair",
            self.root / "quarantine",
        )
        self.assertTrue((self.root / "backup-before-repair" / "lan_chat.sqlite").is_file())
        self.assertTrue((self.root / "quarantine" / "lan_chat_media" / "orphan.mp4").is_file())
        self.assertNotIn(filename, result["after"]["missingMedia"])
        self.assertGreaterEqual(time.time(), result["after"]["checkedAt"])

    def test_import_replaces_only_chat_dataset_and_keeps_restore_copy(self) -> None:
        snapshot_dir = self.root / "snapshot"
        snapshot(self.source, snapshot_dir, sanitize_auth=True)
        development = self.root / "development-data"
        development.mkdir()
        (development / "proxy_pool.sqlite").write_bytes(b"keep-this-data")
        old_store = LanChatStore(development / "lan_chat.sqlite")
        old_store.initialize()
        result = import_snapshot(
            snapshot_dir,
            development,
            self.root / "backup-before-import",
        )
        self.assertTrue(result["after"]["healthy"])
        self.assertEqual((development / "proxy_pool.sqlite").read_bytes(), b"keep-this-data")
        self.assertTrue((self.root / "backup-before-import" / "previous-live" / "lan_chat.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
