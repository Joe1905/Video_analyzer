"""HTTP registration for the proxy operations console."""

from __future__ import annotations

import cgi
import re
import sqlite3
from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from core.http import binary_response, json_response, text_response
from routes.router import Router


def register_proxy_routes(
    router: Router,
    *,
    proxy_pool: Any,
    tiktok_studio_publish: Any,
    tiktok_studio_collect: Any,
    instagram_content_collect: Any,
    scoped_proxy_state: Callable[[Any], dict[str, Any]],
    _proxy_feishu_binding: Callable[..., dict[str, Any]],
    current_global_user: Callable[[Any], dict[str, Any]],
    search_shop_catalog_products: Callable[[dict[str, Any]], Any],
    enabled: Callable[[], bool],
    render_proxy_page: Callable[[], str],
) -> Callable[[Any], None]:
    """Register GET routes and return the post-gate proxy POST dispatcher."""

    def proxy_page(self: Any, _params: Mapping[str, str]) -> None:
        if not enabled():
            return text_response(self, HTTPStatus.NOT_FOUND, "Not found", "text/plain; charset=utf-8")
        return text_response(self, HTTPStatus.OK, render_proxy_page(), "text/html; charset=utf-8")

    def handle_proxy_api_get(self: Any, _params: Mapping[str, str]) -> None:
        if not enabled():
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query
        try:
            if path == "/api/proxy/pools":
                return json_response(self, HTTPStatus.OK, scoped_proxy_state(self))
            if path == "/api/proxy/mihomo-export":
                return json_response(self, HTTPStatus.OK, proxy_pool.mihomo_export())
            if path == "/api/proxy/runtime":
                return json_response(self, HTTPStatus.OK, proxy_pool.runtime_status())
            avatar_match = re.fullmatch(r"/api/proxy/accounts/avatar/(\d+)", path)
            if avatar_match:
                try:
                    body, content_type = proxy_pool.account_avatar_bytes(int(avatar_match.group(1)))
                except FileNotFoundError:
                    return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Account avatar not found"})
                return binary_response(self, HTTPStatus.OK, body, content_type)
            if path == "/api/proxy/publish/jobs":
                account_id = int(parse_qs(query).get("account_id", ["0"])[0] or 0)
                return json_response(self, HTTPStatus.OK, tiktok_studio_publish.list_jobs(account_id))
            if path == "/api/proxy/products":
                return json_response(self, HTTPStatus.OK, proxy_pool.list_products())
            if path == "/api/proxy/publish/runtime":
                return json_response(self, HTTPStatus.OK, tiktok_studio_publish.runtime_status())
            if path == "/api/proxy/collect/dashboard":
                account_id = int(parse_qs(query).get("account_id", ["0"])[0] or 0)
                platform = parse_qs(query).get("platform", ["tiktok"])[0]
                return json_response(self, HTTPStatus.OK, tiktok_studio_collect.dashboard(account_id, platform))
            if path == "/api/proxy/collect/runtime":
                return json_response(self, HTTPStatus.OK, tiktok_studio_collect.runtime_status())
            if path.startswith("/api/proxy/publish/videos/"):
                asset_id = unquote(path.removeprefix("/api/proxy/publish/videos/"))
                return self.serve_video(tiktok_studio_publish.video_path(asset_id))
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def proxy_post(self: Any) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/proxy/publish/jobs":
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                if content_length <= 0 or content_length > tiktok_studio_publish.MAX_UPLOAD_BYTES + 2 * 1024 * 1024:
                    raise ValueError("上传内容为空或超过 2GB 限制")
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                        "CONTENT_LENGTH": str(content_length),
                    },
                )
                return json_response(self, HTTPStatus.ACCEPTED, tiktok_studio_publish.create_job(form))
            payload = self.read_json_body()
            if path == "/api/proxy/pools":
                return json_response(self, HTTPStatus.OK, proxy_pool.upsert_pool(payload))
            if path == "/api/proxy/pools/delete":
                return json_response(self, HTTPStatus.OK, proxy_pool.delete_pool(int(payload.get("id") or payload.get("proxy_profile_id") or 0)))
            if path == "/api/proxy/mihomo-reconcile":
                return json_response(self, HTTPStatus.OK, proxy_pool.reconcile_mihomo_pool_configs())
            if path == "/api/proxy/accounts":
                payload = _proxy_feishu_binding(payload, required=not int(payload.get("id") or 0), global_user=current_global_user(self))
                return json_response(self, HTTPStatus.OK, proxy_pool.upsert_account(payload))
            if path == "/api/proxy/accounts/delete":
                return json_response(self, HTTPStatus.OK, proxy_pool.delete_account(int(payload.get("id") or payload.get("account_id") or 0)))
            if path == "/api/proxy/accounts/platform/delete":
                return json_response(
                    self,
                    HTTPStatus.OK,
                    proxy_pool.delete_account_platform(
                        int(payload.get("id") or payload.get("account_id") or 0),
                        str(payload.get("platform") or ""),
                    ),
                )
            if path == "/api/proxy/instagram/collect":
                max_videos = int(payload.get("max_videos") or 5)
                if not 1 <= max_videos <= 20:
                    raise ValueError("max_videos 必须在 1 至 20 之间")
                try:
                    result = instagram_content_collect.run_simulation(
                        int(payload.get("account_id") or 0),
                        max_videos,
                        False,
                        int(payload.get("session_id") or 0),
                    )
                except instagram_content_collect.InstagramCollectionError as exc:
                    return json_response(self, HTTPStatus.CONFLICT, {"error": str(exc)})
                login = result.get("login")
                if isinstance(login, dict):
                    result["login"] = {"profile_has_instagram_login": bool(login.get("profile_has_instagram_login"))}
                return json_response(
                    self,
                    HTTPStatus.OK,
                    result,
                )
            if path == "/api/proxy/accounts/proxy-binding":
                return json_response(self, HTTPStatus.OK, proxy_pool.update_account_proxy_binding(payload))
            if path == "/api/proxy/check":
                return json_response(self, HTTPStatus.OK, proxy_pool.check_binding(payload, require_account=False))
            if path == "/api/proxy/accounts/preflight":
                return json_response(self, HTTPStatus.OK, proxy_pool.check_binding(payload, require_account=True))
            if path == "/api/proxy/accounts/status":
                return json_response(self, HTTPStatus.OK, proxy_pool.update_account_status(payload))
            if path == "/api/proxy/products/search":
                return json_response(self, HTTPStatus.OK, search_shop_catalog_products(payload))
            if path == "/api/proxy/products":
                action = str(payload.get("action") or "create").strip().lower()
                if action == "create":
                    return json_response(self, HTTPStatus.CREATED, proxy_pool.create_product(payload))
                if action == "update":
                    return json_response(self, HTTPStatus.OK, proxy_pool.update_product(payload))
                raise ValueError("商品操作必须是 create 或 update")
            if path == "/api/proxy/products/delete":
                return json_response(self, HTTPStatus.OK, proxy_pool.delete_product(str(payload.get("product_id") or "")))
            if path == "/api/proxy/login-session/start":
                payload = _proxy_feishu_binding(payload, required=not int(payload.get("account_id") or 0), global_user=current_global_user(self))
                return json_response(self, HTTPStatus.OK, proxy_pool.start_login_session(payload))
            if path == "/api/proxy/login-session/open-platform":
                return json_response(self, HTTPStatus.OK, proxy_pool.open_observation_platform(payload))
            if path == "/api/proxy/login-session/stop":
                return json_response(self, HTTPStatus.OK, proxy_pool.stop_login_session(payload))
            if path == "/api/proxy/login-session/status":
                return json_response(self, HTTPStatus.OK, proxy_pool.inspect_login_session(payload))
            if path == "/api/proxy/login-session/capture":
                return json_response(self, HTTPStatus.OK, proxy_pool.inspect_login_session(payload))
            if path == "/api/proxy/publish/jobs/update":
                return json_response(self, HTTPStatus.OK, tiktok_studio_publish.update_job(payload))
            if path == "/api/proxy/publish/jobs/cancel":
                return json_response(self, HTTPStatus.OK, tiktok_studio_publish.cancel_job(payload))
            if path == "/api/proxy/publish/jobs/retry":
                return json_response(self, HTTPStatus.OK, tiktok_studio_publish.retry_job(payload))
            if path == "/api/proxy/publish/jobs/delete":
                return json_response(self, HTTPStatus.OK, tiktok_studio_publish.delete_job(payload))
            if path == "/api/proxy/collect/settings":
                return json_response(self, HTTPStatus.OK, tiktok_studio_collect.save_settings(payload))
            if path == "/api/proxy/collect/jobs":
                return json_response(self, HTTPStatus.ACCEPTED, tiktok_studio_collect.create_job(payload))
            if path == "/api/proxy/collect/jobs/retry":
                return json_response(self, HTTPStatus.OK, tiktok_studio_collect.retry_job(payload))
            if path == "/api/proxy/collect/jobs/rescan-discovery":
                return json_response(self, HTTPStatus.ACCEPTED, tiktok_studio_collect.start_discovery_rescans(payload))
            if path == "/api/proxy/collect/jobs/cancel":
                return json_response(self, HTTPStatus.OK, tiktok_studio_collect.cancel_job(payload))
            if path == "/api/proxy/collect/results/resync":
                return json_response(self, HTTPStatus.OK, tiktok_studio_collect.retry_failed_feishu_sync(payload))
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except ValueError as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    router.get("/proxy", proxy_page)
    router.get_prefix("/api/proxy/", handle_proxy_api_get)
    return proxy_post
