"""Client for Feishu capabilities exposed by the co-located LAN service."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:4000"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class FeishuCapabilityError(Exception):
    pass


class FeishuCapabilityClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (
            base_url or os.getenv("FEISHU_CAPABILITY_API_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout or float(
            os.getenv("FEISHU_CAPABILITY_TIMEOUT_SECONDS", "15")
        )

    def list_users(self) -> dict[str, Any]:
        users: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_token = ""

        while True:
            query = {"page_size": "50"}
            if page_token:
                query["page_token"] = page_token
            payload = self._request("GET", f"/v1/feishu/users?{urlencode(query)}")
            page_users = payload.get("users")
            if not isinstance(page_users, list):
                raise FeishuCapabilityError("飞书用户接口返回格式无效")
            for item in page_users:
                if not isinstance(item, dict):
                    continue
                identity = str(
                    item.get("openId") or item.get("userId") or item.get("unionId") or ""
                ).strip()
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                users.append(item)
            if not payload.get("hasMore"):
                break
            next_token = str(payload.get("pageToken") or "").strip()
            if not next_token or next_token == page_token:
                raise FeishuCapabilityError("飞书用户接口分页标记无效")
            page_token = next_token

        return {"users": users, "count": len(users), "hasMore": False, "pageToken": ""}

    def update_bitable_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/feishu/bitable/records/update", payload)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES)
            message = self._error_message(raw) or f"上游返回 HTTP {exc.code}"
            raise FeishuCapabilityError(message) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FeishuCapabilityError(f"无法连接同机飞书能力服务：{exc}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FeishuCapabilityError("飞书能力服务响应过大")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FeishuCapabilityError("飞书能力服务返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise FeishuCapabilityError("飞书能力服务返回格式无效")
        return result

    @staticmethod
    def _error_message(raw: bytes) -> str:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("error") or payload.get("message") or "").strip()
