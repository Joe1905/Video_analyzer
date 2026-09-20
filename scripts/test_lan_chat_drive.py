"""User drive isolation, expiry and Proxy selection contract checks."""
import ast
import cgi
import io
import json
import re
import sqlite3
import tempfile
import unittest
from http import HTTPStatus
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

from lan_chat import DEFAULT_FEISHU_USER_ID, LanChatError, LanChatStore


class DriveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LanChatStore(Path(self.temp.name) / "chat.sqlite")
        with patch.object(self.store, "_start_file_janitor"):
            self.store.initialize()
        self.a = self.store.create_account(DEFAULT_FEISHU_USER_ID, "A")["sessionToken"]
        self.b = self.store.create_account(DEFAULT_FEISHU_USER_ID, "B")["sessionToken"]

    def upload(self):
        return self.store.drive_upload(self.a, "测试.mp4", io.BytesIO(b"test video"))

    def test_preview_permissions_expiry_and_file_binding(self):
        item = self.upload()
        with self.assertRaises(LanChatError):
            self.store.drive_preview(self.b, item["id"])
        ticket = self.store.drive_preview(self.a, item["id"])
        self.assertEqual(self.store.drive_preview_info(item["id"], ticket)[0].read_bytes(), b"test video")
        for file_id, value in (("0" * 32, ticket), (item["id"], ticket + "bad"), (item["id"], "")):
            with self.assertRaises(LanChatError) as error:
                self.store.drive_preview_info(file_id, value)
            self.assertEqual(error.exception.status, 403)
        with patch("lan_chat.time.time", return_value=int(ticket.split(":")[1])):
            with self.assertRaises(LanChatError):
                self.store.drive_preview_info(item["id"], ticket)
        self.store.drive_delete(self.a, item["id"])
        with self.assertRaises(LanChatError):
            self.store.drive_preview_info(item["id"], ticket)

    def test_owner_and_expiry(self):
        item = self.upload()
        self.assertEqual(self.store.drive_info(self.a, item["id"])[2], "video/mp4")
        self.assertEqual(item["expires_at"] - item["created_at"], 15 * 86400)
        self.assertEqual(self.store.drive_list(self.b), [])
        for operation in (self.store.drive_info, self.store.drive_delete):
            with self.assertRaises(LanChatError) as error:
                operation(self.b, item["id"])
            self.assertEqual(error.exception.status, 404)
        with patch("lan_chat.time.time", return_value=item["expires_at"]):
            self.assertEqual(self.store.drive_list(self.a), [])
            with self.assertRaises(LanChatError):
                self.store.drive_info(self.a, item["id"])
        self.store.cleanup_expired_files(item["expires_at"])
        self.assertFalse((self.store.drive_dir / item["id"]).exists())

    def test_existing_active_files_extend_from_upload_time_once(self):
        active = self.upload()
        expired = self.upload()
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE drive_files SET expires_at = created_at + ? WHERE id = ?",
                         (7 * 86400, active["id"]))
            conn.execute("UPDATE drive_files SET created_at = ?, expires_at = ? WHERE id = ?",
                         (expired["created_at"] - 8 * 86400, expired["created_at"] - 86400, expired["id"]))
        for _ in range(2):
            with patch.object(self.store, "_start_file_janitor"):
                self.store.initialize()
            files = self.store.drive_list(self.a)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["expires_at"], active["created_at"] + 15 * 86400)
        self.assertFalse((self.store.drive_dir / expired["id"]).exists())

    def test_upload_failure_cleanup_and_delete(self):
        for name, data in (("x.mp4", b""), ("bad\n.mp4", b"x")):
            with self.assertRaises(LanChatError):
                self.store.drive_upload(self.a, name, io.BytesIO(data))
        with patch("lan_chat.FILE_TRANSFER_MAX_BYTES", 2):
            with self.assertRaises(LanChatError):
                self.upload()
        self.assertEqual(list(self.store.drive_dir.iterdir()), [])
        item = self.upload()
        with self.assertRaises(LanChatError):
            self.store.drive_info("invalid", item["id"])
        with self.assertRaises(LanChatError):
            self.store.drive_info(self.a, "../other")
        self.store.drive_delete(self.a, item["id"])
        self.assertEqual(self.store.drive_list(self.a), [])

    def test_proxy_copies_authenticated_selection(self):
        # Run the actual HTTP handler method without starting unrelated workers.
        source = Path(__file__).with_name("web_app.py").read_text(encoding="utf-8")
        handler = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        method = next(n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == "handle_proxy_api_post")
        created = []
        def create_job(form):
            destination = Path(self.temp.name) / "task-video.mp4"
            destination.write_bytes(form["video"].file.read())
            created.append(destination)
            return {"name": form["video"].filename}
        namespace = {"cgi": cgi, "HTTPStatus": HTTPStatus, "LanChatError": LanChatError,
                     "sqlite3": sqlite3, "lan_chat_store": self.store,
                     "_lan_chat_token": lambda h: h.headers["X-Lan-Chat-Token"],
                     "json_response": lambda h, status, payload: (status, payload),
                     "tiktok_studio_publish": SimpleNamespace(MAX_UPLOAD_BYTES=2 * 1024**3, create_job=create_job)}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "web_app.py", "exec"), namespace)
        item = self.upload()
        body = ("--test\r\nContent-Disposition: form-data; name=\"drive_file_id\"\r\n\r\n"
                + item["id"] + "\r\n--test--\r\n").encode()
        def call(token):
            headers = Message()
            headers["Content-Length"] = str(len(body))
            headers["Content-Type"] = "multipart/form-data; boundary=test"
            headers["X-Lan-Chat-Token"] = token
            request = SimpleNamespace(headers=headers, rfile=io.BytesIO(body))
            return namespace["handle_proxy_api_post"](request, "/api/proxy/publish/jobs")
        self.assertEqual(call(self.b)[0], 404)
        self.assertEqual(created, [])
        self.assertEqual(call(self.a), (202, {"name": "测试.mp4"}))
        self.store.cleanup_expired_files(item["expires_at"])
        self.assertEqual(created[0].read_bytes(), b"test video")
        self.assertEqual(call(self.a)[0], 404)

    def test_http_upload_list_download_delete(self):
        source = Path(__file__).with_name("web_app.py").read_text(encoding="utf-8")
        functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                     and n.name in {"handle_lan_chat_get", "handle_lan_chat_post"}]
        namespace = {"cgi": cgi, "HTTPStatus": HTTPStatus, "LanChatError": LanChatError,
                     "BaseHTTPRequestHandler": object, "re": re, "parse_qs": parse_qs,
                     "FILE_TRANSFER_MAX_BYTES": 10 * 1024**3, "lan_chat_store": self.store,
                     "_lan_chat_token": lambda h: h.headers.get("X-Lan-Chat-Token", ""),
                     "_lan_chat_request_json": lambda h: json.loads(h.rfile.read()),
                     "json_response": lambda h, status, payload: setattr(h, "result", (status, payload)),
                     "file_response": lambda h, path, kind, name, size, download=True: setattr(h, "result", (200, path.read_bytes(), kind, name, size))}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "web_app.py", "exec"), namespace)
        def call(method, path, token, body=b"{}", kind="application/json"):
            headers = Message()
            headers["Content-Length"] = str(len(body))
            headers["Content-Type"] = kind
            headers["X-Lan-Chat-Token"] = token
            handler = SimpleNamespace(headers=headers, rfile=io.BytesIO(body))
            namespace[f"handle_lan_chat_{method}"](handler, urlparse(path))
            return handler.result
        self.assertEqual(call("get", "/api/lan-chat/drive", "")[0], 401)
        body = b'--drive\r\nContent-Disposition: form-data; name="file"; filename="video.mp4"\r\nContent-Type: video/mp4\r\n\r\nvideo bytes\r\n--drive--\r\n'
        status, uploaded = call("post", "/api/lan-chat/drive/upload", self.a, body, "multipart/form-data; boundary=drive")
        self.assertEqual(status, 201)
        file_id = uploaded["file"]["id"]
        self.assertEqual(call("get", "/api/lan-chat/drive", self.a)[1]["files"][0]["id"], file_id)
        path = f"/api/lan-chat/drive/{file_id}"
        preview_path = path + "/preview"
        self.assertEqual(call("post", preview_path, self.b)[0], 404)
        status, preview = call("post", preview_path, self.a, b"")
        self.assertEqual(status, 200)
        self.assertEqual(call("get", preview["url"], "")[1], b"video bytes")
        self.assertEqual(call("get", preview_path, "")[0], 403)
        body = urlencode({"token": self.a}).encode()
        self.assertEqual(call("post", path + "/download", "", body, "application/x-www-form-urlencoded"),
                         (200, b"video bytes", "video/mp4", "video.mp4", 11))
        self.assertEqual(call("post", path + "/delete", self.b)[0], 404)
        self.assertEqual(call("post", path + "/delete", self.a)[0], 200)


if __name__ == "__main__":
    unittest.main()
