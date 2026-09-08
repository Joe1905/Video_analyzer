"""LAN Chat HTTP registration and request handling."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from feishu_capabilities import FeishuCapabilityError
from lan_chat import LanChatError
from core.http import binary_response, file_response, json_response, text_response
from routes.router import Router


def register_lan_chat_page(
    router: Router,
    *,
    scripts_dir: Path,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the LAN chat page while reading its template per request."""

    def lan_chat_page(handler: Any, params: Mapping[str, str]) -> None:
        html = (scripts_dir / "static" / "lan_chat.html").read_text(encoding="utf-8")
        text_response(handler, 200, inject_nav(html, "/lan-chat"), "text/html; charset=utf-8")

    router.get("/lan-chat", lan_chat_page)


def register_lan_chat_api_routes(
    router: Router,
    *,
    store: Any,
    current_global_user: Callable[[BaseHTTPRequestHandler], dict[str, Any]],
    current_global_owner_id: Callable[[BaseHTTPRequestHandler], str],
    cookie_value: Callable[[BaseHTTPRequestHandler, str], str],
    media_cookie: str,
    feishu_login_options: Callable[[], dict[str, Any]],
    message_media_max_bytes: int,
    file_transfer_max_bytes: int,
    profile_avatar_max_bytes: int,
    file_archive_max_files: int,
    field_storage: Callable[..., Any],
) -> tuple[Callable[[BaseHTTPRequestHandler], bool], Callable[[BaseHTTPRequestHandler], bool], Callable[[BaseHTTPRequestHandler, int], None]]:
    """Register GET LAN APIs and return the two narrow non-GET dispatchers."""

    lan_chat_store = store
    LAN_CHAT_MEDIA_COOKIE = media_cookie
    MESSAGE_MEDIA_MAX_BYTES = message_media_max_bytes
    FILE_TRANSFER_MAX_BYTES = file_transfer_max_bytes
    PROFILE_AVATAR_MAX_BYTES = profile_avatar_max_bytes
    FILE_ARCHIVE_MAX_FILES = file_archive_max_files
    _cookie_value = cookie_value
    _feishu_login_options = feishu_login_options
    def _lan_chat_token(handler: BaseHTTPRequestHandler) -> str:
        return (
            handler.headers.get("X-Lan-Chat-Token", "").strip()
            or _cookie_value(handler, LAN_CHAT_MEDIA_COOKIE).strip()
        )


    def _require_lan_global_user(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        global_user = current_global_user(handler)
        if global_user["id"] == "public":
            raise LanChatError("公共账户为只读模式", 403)
        device_user = lan_chat_store.authenticate(_lan_chat_token(handler))
        if str(device_user.get("feishuUserId") or "") != global_user["id"]:
            raise LanChatError("设备账户不属于当前飞书用户，请重新选择账户", 401)
        return device_user


    def _lan_chat_request_json(
        handler: BaseHTTPRequestHandler, max_bytes: int = 65536
    ) -> dict[str, Any]:
        try:
            length = int(handler.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise LanChatError("请求长度无效") from exc
        if length < 0 or length > max_bytes:
            raise LanChatError("请求内容过大", 413)
        try:
            payload = json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LanChatError("请求 JSON 无效") from exc
        if not isinstance(payload, dict):
            raise LanChatError("请求内容必须是对象")
        return payload


    def stream_lan_chat_events(handler: BaseHTTPRequestHandler, after_id: int) -> None:
        """Long-poll-like SSE backed by the message database for lossless reconnects."""
        token = _lan_chat_token(handler)
        _require_lan_global_user(handler)
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache, no-store")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        cursor = max(0, int(after_id or 0))
        try:
            while not handler.wfile.closed:
                events = lan_chat_store.wait_for_message_events(token, cursor, 20.0)
                if events:
                    for event in events:
                        cursor = max(cursor, int(event["id"]))
                        handler.wfile.write(b"event: message\n")
                        handler.wfile.write(
                            b"data: "
                            + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                            + b"\n\n"
                        )
                    handler.wfile.flush()
                else:
                    handler.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                    handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            handler.close_connection = True



    def handle_lan_chat_get(handler: BaseHTTPRequestHandler, parsed) -> bool:
        path = parsed.path
        try:
            if path == "/api/lan-chat/login-options":
                try:
                    options = _feishu_login_options()
                except FeishuCapabilityError as exc:
                    raise LanChatError(f"无法读取飞书用户列表：{exc}", 502) from exc
                json_response(handler, HTTPStatus.OK, options)
                return True
            if path == "/api/lan-chat/bootstrap":
                if current_global_owner_id(handler) == "public":
                    json_response(handler, HTTPStatus.OK, lan_chat_store.public_bootstrap())
                else:
                    _require_lan_global_user(handler)
                    json_response(handler, HTTPStatus.OK, lan_chat_store.bootstrap(_lan_chat_token(handler)))
                return True
            if path == "/api/lan-chat/account-options":
                global_user = current_global_user(handler)
                if global_user["id"] == "public":
                    raise LanChatError("公共账户没有设备账户", 403)
                options = lan_chat_store.login_options().get("feishuUsers", [])
                owner = next((item for item in options if str(item.get("id") or "") == global_user["id"]), None)
                json_response(handler, HTTPStatus.OK, {"currentUser": global_user, "accounts": (owner or {}).get("accounts", [])})
                return True
            feishu_avatar_match = re.fullmatch(
                r"/api/lan-chat/feishu-avatars/([a-z0-9-]{1,64})", path
            )
            if feishu_avatar_match:
                body, content_type = lan_chat_store.feishu_avatar_bytes(feishu_avatar_match.group(1))
                binary_response(handler, HTTPStatus.OK, body, content_type)
                return True
            avatar_match = re.fullmatch(r"/api/lan-chat/avatars/(public|[0-9a-f]{16})", path)
            if avatar_match:
                body, content_type = lan_chat_store.avatar_bytes(avatar_match.group(1))
                binary_response(handler, HTTPStatus.OK, body, content_type)
                return True
            group_avatar_match = re.fullmatch(
                r"/api/lan-chat/group-avatars/([A-Za-z0-9_-]{1,80})", path
            )
            if group_avatar_match:
                body, content_type = lan_chat_store.group_avatar_bytes(
                    group_avatar_match.group(1)
                )
                binary_response(handler, HTTPStatus.OK, body, content_type)
                return True
            media_poster_match = re.fullmatch(
                r"/api/lan-chat/media/([0-9a-f]{32}\.(?:mp4|webm))/poster", path
            )
            if media_poster_match:
                filename = media_poster_match.group(1)
                if current_global_owner_id(handler) == "public":
                    # Validate public-room membership before the poster generator opens the media.
                    lan_chat_store.public_message_media_info(filename)
                else:
                    _require_lan_global_user(handler)
                body, content_type = lan_chat_store.message_video_poster_bytes(filename)
                binary_response(handler, HTTPStatus.OK, body, content_type)
                return True
            media_download_match = re.fullmatch(
                r"/api/lan-chat/media/([0-9a-f]{32}\.(?:jpg|png|gif|webp|mp4|webm))/download",
                path,
            )
            if media_download_match:
                media_id = media_download_match.group(1)
                if current_global_owner_id(handler) == "public":
                    file_path, filename, content_type, size = lan_chat_store.public_message_media_info(media_id)
                else:
                    _require_lan_global_user(handler)
                    file_path, filename, content_type, size = lan_chat_store.message_media_info(media_id)
                file_response(handler, file_path, content_type, filename, size)
                return True
            media_match = re.fullmatch(
                r"/api/lan-chat/media/([0-9a-f]{32}\.(?:jpg|png|gif|webp|mp4|webm))", path
            )
            if media_match:
                media_id = media_match.group(1)
                if current_global_owner_id(handler) == "public":
                    file_path, filename, content_type, size = lan_chat_store.public_message_media_info(media_id)
                else:
                    _require_lan_global_user(handler)
                    file_path, filename, content_type, size = lan_chat_store.message_media_info(media_id)
                file_response(handler, file_path, content_type, filename, size, download=False)
                return True
            file_match = re.fullmatch(r"/api/lan-chat/files/([0-9a-f]{32})", path)
            if file_match:
                if current_global_owner_id(handler) == "public":
                    file_path, filename, content_type, size = lan_chat_store.public_file_download_info(file_match.group(1))
                else:
                    _require_lan_global_user(handler)
                    file_path, filename, content_type, size = lan_chat_store.file_download_info(_lan_chat_token(handler), file_match.group(1))
                file_response(handler, file_path, content_type, filename, size)
                return True
            message_match = re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/messages", path)
            if message_match:
                query = parse_qs(parsed.query)
                try:
                    after_id = int(query.get("after", ["0"])[0])
                    before_id = int(query.get("before", ["0"])[0])
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError as exc:
                    raise LanChatError("分页参数无效") from exc
                room_id = unquote(message_match.group(1))
                if current_global_owner_id(handler) == "public":
                    payload = lan_chat_store.public_list_messages(room_id, after_id=after_id, before_id=before_id, limit=limit)
                else:
                    _require_lan_global_user(handler)
                    payload = lan_chat_store.list_messages(_lan_chat_token(handler), room_id, after_id=after_id, before_id=before_id, limit=limit)
                json_response(handler, HTTPStatus.OK, payload)
                return True
            if path == "/api/lan-chat/events":
                query = parse_qs(parsed.query)
                try:
                    after_id = int(query.get("after", ["0"])[0])
                except ValueError as exc:
                    raise LanChatError("事件游标无效") from exc
                stream_lan_chat_events(handler, after_id)
                return True
        except LanChatError as exc:
            json_response(handler, exc.status, {"error": str(exc)})
            return True
        return False


    def handle_lan_chat_post(handler: BaseHTTPRequestHandler, parsed) -> bool:
        path = parsed.path
        if not path.startswith("/api/lan-chat/"):
            return False
        try:
            global_user = current_global_user(handler)
            public_file_download = bool(re.fullmatch(r"/api/lan-chat/files/[0-9a-f]{32}/download", path))
            if global_user["id"] == "public" and not public_file_download:
                raise LanChatError("公共账户为只读模式", 403)
            selecting_account = path in {
                "/api/lan-chat/select-account",
                "/api/lan-chat/accounts",
                "/api/lan-chat/primary-account",
            }
            if not selecting_account and not public_file_download:
                # Reject writes before consuming multipart bodies or other large payloads.
                _require_lan_global_user(handler)
            download_match = re.fullmatch(r"/api/lan-chat/files/([0-9a-f]{32})/download", path)
            if download_match:
                try:
                    content_length = int(handler.headers.get("Content-Length", "0") or "0")
                except ValueError as exc:
                    raise LanChatError("请求长度无效") from exc
                if content_length <= 0 or content_length > 1024:
                    raise LanChatError("下载请求无效")
                try:
                    form_body = handler.rfile.read(content_length).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise LanChatError("下载请求无效") from exc
                form_data = parse_qs(form_body)
                download_token = str(form_data.get("token", [""])[0] or "")
                if global_user["id"] == "public":
                    file_path, filename, content_type, size = lan_chat_store.public_file_download_info(download_match.group(1))
                else:
                    file_path, filename, content_type, size = lan_chat_store.file_download_info(download_token, download_match.group(1))
                file_response(handler, file_path, content_type, filename, size)
                return True

            media_upload_match = re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/media", path)
            if media_upload_match:
                try:
                    content_length = int(handler.headers.get("Content-Length", "0") or "0")
                except ValueError as exc:
                    raise LanChatError("请求长度无效") from exc
                if content_length <= 0 or content_length > MESSAGE_MEDIA_MAX_BYTES + 2 * 1024 * 1024:
                    raise LanChatError("上传内容为空或超过 100MB 限制", 413)
                if not handler.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
                    raise LanChatError("媒体上传必须使用 multipart/form-data")
                form = field_storage(
                    fp=handler.rfile,
                    headers=handler.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
                        "CONTENT_LENGTH": str(content_length),
                    },
                )
                if "media" not in form:
                    raise LanChatError("请选择要发送的图片或视频")
                media_item = form["media"]
                if isinstance(media_item, list) or not getattr(media_item, "file", None):
                    raise LanChatError("每次只能发送一个媒体文件")
                message, created = lan_chat_store.send_media_file(
                    _lan_chat_token(handler),
                    unquote(media_upload_match.group(1)),
                    str(getattr(media_item, "filename", "") or ""),
                    media_item.file,
                    str(form.getfirst("content", "") or ""),
                    str(form.getfirst("clientUploadId", "") or ""),
                    str(form.getfirst("replyToMessageId", "") or ""),
                )
                json_response(
                    handler,
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {"message": message, "created": created},
                )
                return True

            upload_match = re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/files", path)
            if upload_match:
                try:
                    content_length = int(handler.headers.get("Content-Length", "0") or "0")
                except ValueError as exc:
                    raise LanChatError("请求长度无效") from exc
                if content_length <= 0 or content_length > FILE_TRANSFER_MAX_BYTES + 2 * 1024 * 1024:
                    raise LanChatError("上传内容为空或超过 10GB 限制", 413)
                if not handler.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
                    raise LanChatError("文件上传必须使用 multipart/form-data")
                form = field_storage(
                    fp=handler.rfile,
                    headers=handler.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
                        "CONTENT_LENGTH": str(content_length),
                    },
                )
                if "file" not in form:
                    raise LanChatError("请选择要发送的文件")
                file_item = form["file"]
                if isinstance(file_item, list) or not getattr(file_item, "file", None):
                    raise LanChatError("每次只能发送一个文件")
                message, created = lan_chat_store.send_file(
                    _lan_chat_token(handler),
                    unquote(upload_match.group(1)),
                    str(getattr(file_item, "filename", "") or ""),
                    str(getattr(file_item, "type", "") or "application/octet-stream"),
                    file_item.file,
                    str(form.getfirst("content", "") or ""),
                    str(form.getfirst("clientUploadId", "") or ""),
                    str(form.getfirst("replyToMessageId", "") or ""),
                )
                json_response(
                    handler,
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {"message": message, "created": created},
                )
                return True

            archive_upload_match = re.fullmatch(
                r"/api/lan-chat/rooms/([^/]+)/file-archives", path
            )
            if archive_upload_match:
                try:
                    content_length = int(handler.headers.get("Content-Length", "0") or "0")
                except ValueError as exc:
                    raise LanChatError("请求长度无效") from exc
                if content_length <= 0 or content_length > FILE_TRANSFER_MAX_BYTES + 2 * 1024 * 1024:
                    raise LanChatError("上传内容为空或超过 10GB 限制", 413)
                if not handler.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
                    raise LanChatError("压缩包上传必须使用 multipart/form-data")
                form = field_storage(
                    fp=handler.rfile,
                    headers=handler.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
                        "CONTENT_LENGTH": str(content_length),
                    },
                )
                if "files" not in form:
                    raise LanChatError("请选择要打包的文件")
                file_items = form["files"]
                if not isinstance(file_items, list):
                    file_items = [file_items]
                if len(file_items) < 2:
                    raise LanChatError("至少选择 2 个文件才能打包")
                if len(file_items) > FILE_ARCHIVE_MAX_FILES:
                    raise LanChatError(f"一次最多打包 {FILE_ARCHIVE_MAX_FILES} 个文件")
                archive_files = []
                for item in file_items:
                    if not getattr(item, "file", None):
                        raise LanChatError("压缩包中包含无效文件")
                    archive_files.append(
                        (str(getattr(item, "filename", "") or ""), item.file)
                    )
                message, created = lan_chat_store.send_file_archive(
                    _lan_chat_token(handler),
                    unquote(archive_upload_match.group(1)),
                    str(form.getfirst("archiveName", "") or ""),
                    archive_files,
                    str(form.getfirst("content", "") or ""),
                    str(form.getfirst("clientUploadId", "") or ""),
                    str(form.getfirst("replyToMessageId", "") or ""),
                )
                json_response(
                    handler,
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {"message": message, "created": created},
                )
                return True

            is_message_request = bool(
                re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/messages", path)
            )
            is_profile_request = path == "/api/lan-chat/profile" or bool(
                re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/avatar", path)
            )
            if is_message_request:
                json_max_bytes = (MESSAGE_MEDIA_MAX_BYTES * 4 // 3) + 2 * 1024 * 1024
            elif is_profile_request:
                json_max_bytes = (PROFILE_AVATAR_MAX_BYTES * 4 // 3) + 256 * 1024
            else:
                json_max_bytes = 65536
            payload = _lan_chat_request_json(handler, max_bytes=json_max_bytes)
            if path == "/api/lan-chat/select-account":
                legacy_owner = str(payload.get("feishuUserId") or "").strip()
                if legacy_owner and legacy_owner != global_user["id"]:
                    raise LanChatError("不能选择其他飞书用户的设备账户", 403)
                result = lan_chat_store.select_account(
                    global_user["id"],
                    str(payload.get("accountId") or ""),
                )
                json_response(handler, HTTPStatus.OK, result)
                return True
            if path == "/api/lan-chat/accounts":
                result = lan_chat_store.create_account(
                    global_user["id"],
                    str(payload.get("nickname") or ""),
                )
                json_response(handler, HTTPStatus.CREATED, result)
                return True
            if path == "/api/lan-chat/primary-account":
                result = lan_chat_store.enter_primary_account(global_user["id"])
                json_response(handler, HTTPStatus.CREATED if result["created"] else HTTPStatus.OK, result)
                return True
            if path == "/api/lan-chat/register":
                user, created = lan_chat_store.register(
                    str(payload.get("deviceToken") or ""), str(payload.get("nickname") or "")
                )
                json_response(handler, HTTPStatus.CREATED if created else HTTPStatus.OK, {
                    "user": user,
                    "created": created,
                })
                return True
            if path == "/api/lan-chat/profile":
                user = lan_chat_store.update_profile(
                    _lan_chat_token(handler),
                    str(payload.get("nickname") or ""),
                    str(payload.get("avatarDataUrl") or ""),
                )
                json_response(handler, HTTPStatus.OK, {"user": user})
                return True
            if path == "/api/lan-chat/direct":
                room = lan_chat_store.open_direct(
                    _lan_chat_token(handler), str(payload.get("targetUserId") or "")
                )
                json_response(handler, HTTPStatus.OK, {"room": room})
                return True
            if path == "/api/lan-chat/rooms":
                member_ids = payload.get("memberIds")
                if member_ids is not None and not isinstance(member_ids, list):
                    raise LanChatError("memberIds 必须是数组")
                room = lan_chat_store.create_group(
                    _lan_chat_token(handler), str(payload.get("name") or ""), member_ids
                )
                json_response(handler, HTTPStatus.CREATED, {"room": room})
                return True
            rename_group_match = re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/rename", path)
            if rename_group_match:
                room = lan_chat_store.rename_group(
                    _lan_chat_token(handler),
                    unquote(rename_group_match.group(1)),
                    str(payload.get("name") or ""),
                )
                json_response(handler, HTTPStatus.OK, {"room": room})
                return True
            group_avatar_match = re.fullmatch(
                r"/api/lan-chat/rooms/([^/]+)/avatar", path
            )
            if group_avatar_match:
                room = lan_chat_store.update_group_avatar(
                    _lan_chat_token(handler),
                    unquote(group_avatar_match.group(1)),
                    str(payload.get("avatarDataUrl") or ""),
                )
                json_response(handler, HTTPStatus.OK, {"room": room})
                return True
            announcement_match = re.fullmatch(
                r"/api/lan-chat/rooms/([^/]+)/announcement", path
            )
            if announcement_match:
                room = lan_chat_store.update_group_announcement(
                    _lan_chat_token(handler),
                    unquote(announcement_match.group(1)),
                    str(payload.get("announcement") or ""),
                )
                json_response(handler, HTTPStatus.OK, {"room": room})
                return True
            remove_member_match = re.fullmatch(
                r"/api/lan-chat/rooms/([^/]+)/members/remove", path
            )
            if remove_member_match:
                room = lan_chat_store.remove_group_member(
                    _lan_chat_token(handler),
                    unquote(remove_member_match.group(1)),
                    str(payload.get("targetUserId") or ""),
                )
                json_response(handler, HTTPStatus.OK, {"room": room})
                return True
            transfer_admin_match = re.fullmatch(
                r"/api/lan-chat/rooms/([^/]+)/members/transfer", path
            )
            if transfer_admin_match:
                room = lan_chat_store.transfer_group_admin(
                    _lan_chat_token(handler),
                    unquote(transfer_admin_match.group(1)),
                    str(payload.get("targetUserId") or ""),
                )
                json_response(handler, HTTPStatus.OK, {"room": room})
                return True
            preferences_match = re.fullmatch(
                r"/api/lan-chat/rooms/([^/]+)/preferences", path
            )
            if preferences_match:
                pinned = payload.get("pinned") if "pinned" in payload else None
                muted = payload.get("muted") if "muted" in payload else None
                if pinned is not None and not isinstance(pinned, bool):
                    raise LanChatError("pinned 必须是布尔值")
                if muted is not None and not isinstance(muted, bool):
                    raise LanChatError("muted 必须是布尔值")
                room = lan_chat_store.update_room_preferences(
                    _lan_chat_token(handler),
                    unquote(preferences_match.group(1)),
                    pinned=pinned,
                    muted=muted,
                )
                json_response(handler, HTTPStatus.OK, {"room": room})
                return True
            leave_group_match = re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/leave", path)
            if leave_group_match:
                result = lan_chat_store.leave_group(
                    _lan_chat_token(handler), unquote(leave_group_match.group(1))
                )
                json_response(handler, HTTPStatus.OK, result)
                return True
            dissolve_group_match = re.fullmatch(
                r"/api/lan-chat/rooms/([^/]+)/dissolve", path
            )
            if dissolve_group_match:
                result = lan_chat_store.dissolve_group(
                    _lan_chat_token(handler), unquote(dissolve_group_match.group(1))
                )
                json_response(handler, HTTPStatus.OK, result)
                return True
            accept_match = re.fullmatch(r"/api/lan-chat/files/([0-9a-f]{32})/accept", path)
            if accept_match:
                message = lan_chat_store.accept_file(
                    _lan_chat_token(handler), accept_match.group(1)
                )
                json_response(handler, HTTPStatus.OK, {"message": message})
                return True
            message_match = re.fullmatch(r"/api/lan-chat/rooms/([^/]+)/messages", path)
            if message_match:
                message, created = lan_chat_store.send_message(
                    _lan_chat_token(handler),
                    unquote(message_match.group(1)),
                    str(payload.get("content") or ""),
                    str(payload.get("mediaData") or payload.get("imageData") or ""),
                    str(payload.get("clientUploadId") or ""),
                    payload.get("replyToMessageId"),
                )
                json_response(
                    handler,
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {"message": message, "created": created},
                )
                return True
            json_response(handler, HTTPStatus.NOT_FOUND, {"error": "LAN chat API not found"})
            return True
        except LanChatError as exc:
            json_response(handler, exc.status, {"error": str(exc)})
            return True



    def get_api(handler: BaseHTTPRequestHandler, _params: Mapping[str, str]) -> None:
        parsed = urlparse(handler.path)
        if not handle_lan_chat_get(handler, parsed):
            json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def post_api(handler: BaseHTTPRequestHandler) -> bool:
        return handle_lan_chat_post(handler, urlparse(handler.path))

    def head_api(handler: BaseHTTPRequestHandler) -> bool:
        return handle_lan_chat_get(handler, urlparse(handler.path))

    router.get_prefix("/api/lan-chat/", get_api)
    return post_api, head_api, stream_lan_chat_events