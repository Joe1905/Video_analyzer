"""Ensure the dedicated browser cannot inherit a subscription or direct fallback."""
import unittest

from remote_browser import proxy_config


class ProxyConfigTests(unittest.TestCase):
    def test_only_fixed_hysteria_node_is_copied(self):
        node = {"name": "apps-HY2", "type": "hysteria2", "password": "test-only"}
        source = {"proxies": [{"name": "other"}, node],
                  "proxy-providers": {"subscription": {"url": "https://invalid"}},
                  "rules": ["MATCH,DIRECT"], "external-controller": "0.0.0.0:9090"}
        config = proxy_config(source, 18003)
        self.assertEqual(config["proxies"], [node])
        self.assertEqual(config["rules"], ["MATCH,apps-HY2"])
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertFalse(config["allow-lan"])
        self.assertNotIn("proxy-providers", config)
        self.assertNotIn("external-controller", config)

    def test_missing_wrong_type_or_chained_node_fails_closed(self):
        for node in (None, {"name": "apps-HY2", "type": "direct"},
                     {"name": "apps-HY2", "type": "hysteria2", "dialer-proxy": "subscription"}):
            with self.subTest(node=node), self.assertRaises(ValueError):
                proxy_config({"proxies": [node] if node else []}, 18003)


if __name__ == "__main__":
    unittest.main()
