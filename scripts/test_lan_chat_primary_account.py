#!/usr/bin/env python3
"""Ad-hoc verification for automatic LAN chat primary-account creation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lan_chat import LanChatStore


class LanChatPrimaryAccountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LanChatStore(Path(self.temp_dir.name) / "lan_chat.sqlite")
        self.store.initialize()
        self.store.sync_feishu_users([{"openId": "ou_primary", "name": "张三"}])
        self.owner = next(
            owner
            for owner in self.store.login_options()["feishuUsers"]
            if owner["feishuId"] == "ou_primary"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_first_entry_creates_one_same_named_primary_account(self) -> None:
        first = self.store.enter_primary_account(self.owner["id"])
        again = self.store.enter_primary_account(self.owner["id"])

        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["user"]["nickname"], "张三")
        self.assertEqual(again["user"]["id"], first["user"]["id"])
        refreshed = next(
            owner
            for owner in self.store.login_options()["feishuUsers"]
            if owner["id"] == self.owner["id"]
        )
        self.assertEqual([account["nickname"] for account in refreshed["accounts"]], ["张三"])


if __name__ == "__main__":
    unittest.main()
