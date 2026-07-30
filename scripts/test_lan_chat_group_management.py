#!/usr/bin/env python3
"""Ad-hoc verification for LAN chat default groups and group governance."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lan_chat import DEFAULT_FEISHU_USER_ID, LanChatError, LanChatStore


class LanChatGroupManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LanChatStore(Path(self.temp_dir.name) / "lan_chat.sqlite")
        self.store.initialize()
        self.owner = self.store.create_account(DEFAULT_FEISHU_USER_ID, "组织者")
        self.second = self.store.create_account(DEFAULT_FEISHU_USER_ID, "第二位")
        self.third = self.store.create_account(DEFAULT_FEISHU_USER_ID, "第三位")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_forbidden(self, action) -> None:
        with self.assertRaises(LanChatError) as context:
            action()
        self.assertEqual(context.exception.status, 403)

    def test_every_account_gets_two_immutable_default_groups(self) -> None:
        owner_rooms = self.store.list_rooms(self.owner["user"]["id"])
        second_rooms = self.store.list_rooms(self.second["user"]["id"])
        defaults = [room for room in owner_rooms if room["isDefault"]]
        self.assertEqual([room["systemKind"] for room in defaults], ["public", "feishu"])
        self.assertTrue(all(room["pinned"] for room in defaults))

        feishu_room = next(room for room in defaults if room["systemKind"] == "feishu")
        second_feishu_room = next(
            room for room in second_rooms if room["systemKind"] == "feishu"
        )
        self.assertEqual(feishu_room["id"], second_feishu_room["id"])
        self.assertEqual(feishu_room["memberCount"], 3)
        self.assertFalse(feishu_room["canLeave"])
        self.assertFalse(feishu_room["canDissolve"])

        token = self.owner["sessionToken"]
        room_id = feishu_room["id"]
        self.assert_forbidden(lambda: self.store.rename_group(token, room_id, "不能改名"))
        self.assert_forbidden(lambda: self.store.leave_group(token, room_id))
        self.assert_forbidden(lambda: self.store.dissolve_group(token, room_id))
        self.assert_forbidden(
            lambda: self.store.remove_group_member(
                token, room_id, self.second["user"]["id"]
            )
        )

    def test_default_room_backfill_preserves_manual_unpin(self) -> None:
        public_room = next(
            room
            for room in self.store.list_rooms(self.owner["user"]["id"])
            if room["systemKind"] == "public"
        )
        updated = self.store.update_room_preferences(
            self.owner["sessionToken"], public_room["id"], pinned=False
        )
        self.assertFalse(updated["pinned"])

        newcomer = self.store.create_account(DEFAULT_FEISHU_USER_ID, "新账户")
        newcomer_defaults = [
            room
            for room in self.store.list_rooms(newcomer["user"]["id"])
            if room["isDefault"]
        ]
        self.assertEqual(len(newcomer_defaults), 2)
        self.assertTrue(all(room["pinned"] for room in newcomer_defaults))

        owner_public_after_backfill = next(
            room
            for room in self.store.list_rooms(self.owner["user"]["id"])
            if room["systemKind"] == "public"
        )
        self.assertFalse(owner_public_after_backfill["pinned"])

    def test_admin_transfers_by_join_order_and_successor_can_govern(self) -> None:
        room = self.store.create_group(
            self.owner["sessionToken"],
            "项目群",
            [self.second["user"]["id"], self.third["user"]["id"]],
        )
        self.assertEqual(room["adminUserId"], self.owner["user"]["id"])
        self.assertTrue(room["currentUserIsAdmin"])
        self.assertTrue(room["canRename"])
        self.assertTrue(room["canDissolve"])

        second_token = self.second["sessionToken"]
        self.assert_forbidden(
            lambda: self.store.rename_group(second_token, room["id"], "越权改名")
        )
        self.assert_forbidden(lambda: self.store.dissolve_group(second_token, room["id"]))
        self.assert_forbidden(
            lambda: self.store.remove_group_member(
                second_token, room["id"], self.third["user"]["id"]
            )
        )

        ordered_remaining = [
            member["id"]
            for member in room["members"]
            if member["id"] != self.owner["user"]["id"]
        ]
        result = self.store.leave_group(self.owner["sessionToken"], room["id"])
        self.assertEqual(result["newAdminUserId"], ordered_remaining[0])

        accounts = {
            self.second["user"]["id"]: self.second,
            self.third["user"]["id"]: self.third,
        }
        successor = accounts[ordered_remaining[0]]
        other_id = ordered_remaining[1]
        renamed = self.store.rename_group(
            successor["sessionToken"], room["id"], "继任管理员群"
        )
        self.assertEqual(renamed["name"], "继任管理员群")
        self.assertEqual(renamed["adminUserId"], successor["user"]["id"])

        after_removal = self.store.remove_group_member(
            successor["sessionToken"], room["id"], other_id
        )
        self.assertEqual(after_removal["memberCount"], 1)
        dissolved = self.store.dissolve_group(successor["sessionToken"], room["id"])
        self.assertTrue(dissolved["dissolved"])
        self.assertFalse(
            any(
                item["id"] == room["id"]
                for item in self.store.list_rooms(successor["user"]["id"])
            )
        )

    def test_admin_can_transfer_ownership_explicitly(self) -> None:
        room = self.store.create_group(
            self.owner["sessionToken"],
            "转移测试",
            [self.second["user"]["id"], self.third["user"]["id"]],
        )
        updated = self.store.transfer_group_admin(
            self.owner["sessionToken"],
            room["id"],
            self.second["user"]["id"],
        )
        self.assertEqual(updated["adminUserId"], self.second["user"]["id"])
        self.assertFalse(updated["currentUserIsAdmin"])
        self.assert_forbidden(
            lambda: self.store.rename_group(
                self.owner["sessionToken"], room["id"], "无权修改"
            )
        )
        governed = self.store.rename_group(
            self.second["sessionToken"], room["id"], "新管理员已接管"
        )
        self.assertEqual(governed["name"], "新管理员已接管")

    def test_room_preferences_are_per_user_and_pinned_rooms_sort_first(self) -> None:
        room = self.store.create_group(
            self.owner["sessionToken"],
            "偏好测试",
            [self.second["user"]["id"]],
        )
        updated = self.store.update_room_preferences(
            self.owner["sessionToken"], room["id"], pinned=True, muted=True
        )
        self.assertTrue(updated["pinned"])
        self.assertTrue(updated["muted"])
        owner_rooms = self.store.list_rooms(self.owner["user"]["id"])
        self.assertIn(
            room["id"],
            [item["id"] for item in owner_rooms if item["pinned"]],
        )
        second_rooms = self.store.list_rooms(self.second["user"]["id"])
        second_room = next(
            item
            for item in second_rooms
            if item["id"] == room["id"]
        )
        second_room_index = second_rooms.index(second_room)
        self.assertTrue(all(item["pinned"] for item in second_rooms[:second_room_index]))
        self.assertFalse(second_room["pinned"])
        self.assertFalse(second_room["muted"])

    def test_message_reply_is_structured_and_limited_to_the_same_room(self) -> None:
        public_room = next(
            room
            for room in self.store.list_rooms(self.owner["user"]["id"])
            if room["systemKind"] == "public"
        )
        original, created = self.store.send_message(
            self.owner["sessionToken"],
            public_room["id"],
            "日报已更新，大家可以开始查看了。",
        )
        self.assertTrue(created)

        reply, created = self.store.send_message(
            self.second["sessionToken"],
            public_room["id"],
            "收到。",
            reply_to_message_id=original["id"],
        )
        self.assertTrue(created)
        self.assertEqual(
            reply["reply"],
            {
                "id": original["id"],
                "senderName": "组织者",
                "content": "日报已更新，大家可以开始查看了。",
            },
        )

        listed = self.store.list_messages(
            self.second["sessionToken"], public_room["id"]
        )
        listed_reply = next(
            message for message in listed["messages"] if message["id"] == reply["id"]
        )
        self.assertEqual(listed_reply["reply"], reply["reply"])

        other_room = self.store.create_group(
            self.owner["sessionToken"],
            "其他群组",
            [self.second["user"]["id"]],
        )
        with self.assertRaises(LanChatError) as context:
            self.store.send_message(
                self.second["sessionToken"],
                other_room["id"],
                "不能跨会话引用",
                reply_to_message_id=original["id"],
            )
        self.assertEqual(context.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
