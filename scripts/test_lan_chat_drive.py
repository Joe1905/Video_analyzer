"""User drive isolation, expiry and Proxy selection contract checks."""
import ast
import cgi
import io
import sqlite3
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_owner_and_expiry(self):
        item = self.upload()
        self.assertEqual(item["expires_at"] - item["created_at"], 7 * 86400)
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
            request = SimpleNamespace(headers={"Content-Length": str(len(body)),
                                      "Content-Type": "multipart/form-data; boundary=test",
                                      "X-Lan-Chat-Token": token}, rfile=io.BytesIO(body))
            return namespace["handle_proxy_api_post"](request, "/api/proxy/publish/jobs")
        self.assertEqual(call(self.b)[0], 404)
        self.assertEqual(created, [])
        self.assertEqual(call(self.a), (202, {"name": "测试.mp4"}))
        self.store.cleanup_expired_files(item["expires_at"])
        self.assertEqual(created[0].read_bytes(), b"test video")
        self.assertEqual(call(self.a)[0], 404)


if __name__ == "__main__":
    unittest.main()
