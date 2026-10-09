"""Read-only bridge to the independently managed phone service."""
import http.client
import json
from urllib.parse import urlsplit


def get_status():
    connection = http.client.HTTPConnection("127.0.0.1", 18444, timeout=3)
    try:
        connection.request("GET", "/api/status", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError("Invalid phone status response")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid phone status payload")
        state = payload.get("state")
        if state not in {"ready", "starting", "stopped", "error"}:
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
                "embed_allowed": payload.get("embed_allowed") is True}
    except (OSError, ValueError, http.client.HTTPException):
        return {"state": "unavailable", "message": "暂时无法连接虚拟手机服务，请稍后刷新。",
                "viewer_url": "", "embed_allowed": False}
    finally:
        connection.close()
