"""Restricted LAN bridge to the independently managed phone service."""
import http.client
import ipaddress
import json
from urllib.parse import urlsplit


def allowed_request(handler, write=False):
    try:
        peer = ipaddress.ip_address(handler.client_address[0])
    except ValueError:
        return False
    if not (peer.is_loopback or peer in ipaddress.ip_network("192.168.0.0/23")):
        return False
    hosts = {"192.168.1.254:4003", "127.0.0.1:4003", "localhost:4003"}
    if handler.headers.get("Host") not in hosts:
        return False
    return not write or handler.headers.get("Origin") in {"http://" + host for host in hosts}


def power(action):
    if not isinstance(action, str) or action not in {"start", "stop", "restart"}:
        return 400, {"error": "不支持的手机操作。"}
    connection = http.client.HTTPConnection("127.0.0.1", 18445, timeout=10)
    try:
        connection.request("POST", "/api/lifecycle", body=json.dumps({"action": action}),
                           headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:18445"})
        response = connection.getresponse()
        raw = response.read(65537)
        payload = json.loads(raw) if len(raw) <= 65536 else None
        if not isinstance(payload, dict):
            raise ValueError("Invalid phone power response")
        if response.status not in {200, 202, 400, 403, 409, 503}:
            raise ValueError("Invalid phone power status")
        return response.status, {"ok": payload.get("ok") is True,
                                 "message": str(payload.get("message") or "")[:1000],
                                 "error": str(payload.get("error") or "")[:1000]}
    except (OSError, ValueError, http.client.HTTPException):
        return 503, {"error": "无法确认手机操作结果，请刷新状态后再操作。"}
    finally:
        connection.close()


def get_status():
    connection = http.client.HTTPConnection("127.0.0.1", 18445, timeout=3)
    try:
        connection.request("GET", "/api/status", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError("Invalid phone status response")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid phone status payload")
        if (not isinstance(payload.get("binding", {}), dict)
                or not isinstance(payload.get("allowed_actions", []), list)
                or not isinstance(payload.get("operation") or {}, dict)):
            raise ValueError("Invalid phone status metadata")
        state = payload.get("state")
        if state not in {"ready", "starting", "stopping", "stopped", "error"}:
            raise ValueError("Invalid phone state")
        viewer_url = str(payload.get("viewer_url") or "")
        viewer = urlsplit(viewer_url)
        if viewer_url and (viewer.scheme not in {"http", "https"}
                           or viewer.hostname != "192.168.1.254"
                           or viewer.username or viewer.password):
            raise ValueError("Invalid phone viewer URL")
        return {"state": state,
                "message": str(payload.get("message") or "")[:1000],
                "viewer_url": viewer_url if state == "ready" else "",
                "embed_allowed": payload.get("embed_allowed") is True,
                "network_verified": payload.get("network_verified") is True,
                "control_mode": str(payload.get("control_mode") or "")[:80],
                "operation": {key: str((payload.get("operation") or {}).get(key) or "")[:1000]
                              for key in ("id", "action", "state", "message")},
                "binding": {key: str((payload.get("binding") or {}).get(key) or "")[:100]
                            for key in ("device_id", "expected_exit_ip")},
                "allowed_actions": [action for action in payload.get("allowed_actions", [])
                                    if action in {"start", "stop", "restart"}]}
    except (OSError, ValueError, http.client.HTTPException):
        return {"state": "unavailable", "message": "暂时无法连接虚拟手机服务，请稍后刷新。",
                "viewer_url": "", "embed_allowed": False}
    finally:
        connection.close()
