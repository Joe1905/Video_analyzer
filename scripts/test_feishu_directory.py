#!/usr/bin/env python3
import unittest
from unittest.mock import patch

import web_app
from feishu_capabilities import FeishuCapabilityError


class FakeFeishuClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"users": []}
        self.error = error
        self.calls = 0

    def list_users(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


class FakeLanChatStore:
    def __init__(self, options):
        self.options = options
        self.synced = []

    def sync_feishu_users(self, users):
        self.synced.append(users)

    def login_options(self):
        return self.options


class FeishuDirectoryTest(unittest.TestCase):
    def setUp(self):
        web_app.feishu_directory_cache_payload = None
        web_app.feishu_directory_cache_expires_at = 0.0

    def test_directory_is_shared_and_cached(self):
        client = FakeFeishuClient({"users": [{"openId": "ou_demo"}]})
        store = FakeLanChatStore(
            {
                "feishuUsers": [
                    {
                        "id": "feishu-demo",
                        "feishuId": "ou_demo",
                        "name": "测试用户",
                        "avatarUrl": "/avatar/demo",
                        "accounts": [],
                    }
                ]
            }
        )
        with (
            patch.object(web_app, "feishu_capability_client", client),
            patch.object(web_app, "lan_chat_store", store),
            patch.object(web_app.proxy_pool, "sync_feishu_directory") as sync_proxy,
        ):
            first = web_app._feishu_login_options()
            second = web_app._feishu_login_options()

        self.assertEqual(client.calls, 1)
        self.assertEqual(first["feishuUsers"], second["feishuUsers"])
        self.assertEqual(first["directoryStatus"], {"source": "synced", "stale": False})
        self.assertEqual(store.synced, [[{"openId": "ou_demo"}]])
        sync_proxy.assert_called_once_with(first["feishuUsers"])

    def test_read_only_directory_falls_back_to_local_cache(self):
        store = FakeLanChatStore(
            {
                "feishuUsers": [
                    {
                        "id": "feishu-cached",
                        "feishuId": "ou_cached",
                        "name": "缓存用户",
                        "avatarUrl": "/avatar/cached",
                        "accounts": [],
                    }
                ]
            }
        )
        client = FakeFeishuClient(error=FeishuCapabilityError("upstream unavailable"))
        with (
            patch.object(web_app, "feishu_capability_client", client),
            patch.object(web_app, "lan_chat_store", store),
        ):
            result = web_app._feishu_login_options()

        self.assertEqual(result["feishuUsers"], store.options["feishuUsers"])
        self.assertEqual(
            result["directoryStatus"], {"source": "local-cache", "stale": True}
        )

    def test_empty_local_directory_preserves_upstream_error(self):
        client = FakeFeishuClient(error=FeishuCapabilityError("upstream unavailable"))
        store = FakeLanChatStore({"feishuUsers": []})
        with (
            patch.object(web_app, "feishu_capability_client", client),
            patch.object(web_app, "lan_chat_store", store),
        ):
            with self.assertRaises(FeishuCapabilityError):
                web_app._feishu_login_options()


if __name__ == "__main__":
    unittest.main()
