"""Offline regression for provider-backed system proxy groups."""
from unittest.mock import patch

import proxy_pool as pool


def main():
    def resolve(responses):
        with patch.object(pool, "_mihomo_request", side_effect=responses):
            return pool._resolve_system_proxy_dialer("static-node")

    def rejected(responses):
        try:
            resolve(responses)
        except pool.ProxyConfigurationError:
            return
        raise AssertionError("unsafe or unresolved dialer accepted")

    group = (True, {"now": "backup-group"}, "")
    leaf = (True, {"now": "provider-node"}, "")
    missing = (False, {}, "HTTP 404: not found")
    assert resolve([group, leaf, missing, (True, {
        "providers": {"subscription": {"proxies": [{"name": "provider-node"}]}}
    }, "")]) == "backup-group"
    assert resolve([group, (True, {"type": "Vmess"}, "")]) == "backup-group"
    rejected([group, leaf, missing, (True, {"providers": {}}, "")])
    rejected([group, leaf, missing, (False, {}, "timeout")])
    rejected([(True, {"now": "DIRECT"}, "")])
    rejected([(True, {"now": "static-node"}, "")])
    rejected([group, (True, {"now": "GLOBAL"}, "")])
    print("PASS: provider dialer resolution and unsafe-chain rejection")


if __name__ == "__main__":
    main()
