import http.client
import os
import socketserver
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Queue
from unittest.mock import patch

import page_editor_proxy


ATTACHMENT = bytes(range(256)) * 512
DISPOSITION = 'attachment; filename="export.zip"'
HOST = "192.168.1.254:4003"
ORIGIN = "http://" + HOST


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


class EditorHandler(QuietHandler):
    def serve(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.put((self.command, self.path, self.headers, body))
        if self.path == "/redirect":
            self.send_response(303)
            self.send_header("Location", "/downloads/latest?id=1")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", DISPOSITION)
        self.send_header("Content-Length", str(len(ATTACHMENT)))
        self.send_header("Cache-Control", "public, max-age=120")
        self.send_header("Set-Cookie", "editor-session=example")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(ATTACHMENT)

    do_GET = do_HEAD = do_POST = serve


class ProxyHandler(QuietHandler):
    do_GET = do_HEAD = do_POST = page_editor_proxy.proxy_page_editor


class PageEditorProxyTest(unittest.TestCase):
    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.socket_path = os.path.join(temp.name, "editor.sock")
        editor = socketserver.UnixStreamServer(self.socket_path, EditorHandler)
        editor.requests = self.requests = Queue()
        self.start_server(editor)
        socket_patch = patch.object(page_editor_proxy, "SOCKET_PATH", self.socket_path)
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        self.proxy = self.start_server(HTTPServer(("127.0.0.1", 0), ProxyHandler))

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.proxy.server_address, timeout=3)
        self.addCleanup(conn.close)
        request_headers = {
            "Host": HOST, "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin",
            "Cookie": "analyzer-session=example", "Authorization": "Bearer example",
        }
        request_headers.update(headers or {})
        conn.request(method, path, body, request_headers)
        with conn.getresponse() as response:
            return response.status, response.headers, response.read()

    def test_download_and_head(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                path = "/attachments/a%20b.zip?name=a%2Fb&v=2"
                status, headers, body = self.request(method, "/page-editor" + path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "application/octet-stream")
                self.assertEqual(headers["Content-Disposition"], DISPOSITION)
                self.assertEqual(headers["Content-Length"], str(len(ATTACHMENT)))
                self.assertEqual(headers.get_all("Cache-Control"), ["no-store"])
                self.assertIsNone(headers["Set-Cookie"])
                self.assertEqual(body, ATTACHMENT if method == "GET" else b"")
                actual_method, target, upstream, _ = self.requests.get(timeout=1)
                self.assertEqual((actual_method, target), (method, path))
                self.assertEqual(upstream["Host"], HOST)
                self.assertEqual(upstream["Origin"], ORIGIN)
                self.assertEqual(upstream["Sec-Fetch-Site"], "same-origin")
                self.assertIsNone(upstream["Cookie"])
                self.assertIsNone(upstream["Authorization"])

    def test_multipart_upload(self):
        content_type = "multipart/form-data; boundary=editor-boundary"
        payload = (b'--editor-boundary\r\nContent-Disposition: form-data; name="file"; '
                   b'filename="source.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
                   + ATTACHMENT + b'\r\n--editor-boundary--\r\n')
        status, _, _ = self.request("POST", "/page-editor/upload?draft=1", payload,
                                    {"Content-Type": content_type})
        self.assertEqual(status, 200)
        method, target, upstream, body = self.requests.get(timeout=1)
        self.assertEqual((method, target), ("POST", "/upload?draft=1"))
        self.assertEqual(upstream["Content-Type"], content_type)
        self.assertEqual(upstream["Content-Length"], str(len(payload)))
        self.assertEqual(upstream["Origin"], ORIGIN)
        self.assertEqual(body, payload)

    def test_editor_unavailable(self):
        with patch.object(page_editor_proxy, "SOCKET_PATH", self.socket_path + ".absent"):
            status, headers, _ = self.request("GET", "/page-editor/")
        self.assertIn(status, (502, 503))
        self.assertEqual(headers.get_all("Cache-Control"), ["no-store"])

    def test_redirect_stays_in_editor(self):
        status, headers, _ = self.request("GET", "/page-editor/redirect")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/page-editor/downloads/latest?id=1")


if __name__ == "__main__":
    unittest.main()
