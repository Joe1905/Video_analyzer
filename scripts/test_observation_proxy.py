"""Transport tests without real account browsers or external services."""
import http.client
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import observation_proxy


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.received = (self.path, dict(self.headers))
        if self.headers.get("Upgrade") == "websocket":
            self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n\x82\x03RFB")
            self.wfile.flush()
            data = self.rfile.read(5)
            self.wfile.write(data)
            self.wfile.flush()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"asset")


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if not observation_proxy.handle(self, [self.server.upstream_port]):
            self.send_error(404)


class ObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        cls.proxy.upstream_port = cls.upstream.server_port
        for server in (cls.upstream, cls.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.proxy, cls.upstream):
            server.shutdown()
            server.server_close()

    def request(self, suffix):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=3)
        conn.request("GET", suffix, headers={"Cookie": "secret", "Authorization": "secret"})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_assets_and_credential_isolation(self):
        prefix = f"/proxy/observation/{self.upstream.server_port}"
        self.assertEqual(self.request(prefix + "/app/ui.js?v=1"), (200, b"asset"))
        path, headers = self.upstream.received
        self.assertEqual(path, "/app/ui.js?v=1")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)

    def test_reject_ports_and_traversal(self):
        self.assertEqual(self.request("/proxy/observation/22/vnc.html")[0], 404)
        self.assertEqual(self.request("/proxy/observation/no/vnc.html")[0], 404)
        prefix = f"/proxy/observation/{self.upstream.server_port}"
        for path in ("/%2e%2e/private", "/%2fprivate", "/a%5cb"):
            self.assertEqual(self.request(prefix + path)[0], 400)

    def test_websocket_bidirectional_and_immediate_frame(self):
        with socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=3) as client:
            path = f"/proxy/observation/{self.upstream.server_port}/websockify"
            client.sendall((f"GET {path} HTTP/1.1\r\nHost: apps.example\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: test\r\nSec-WebSocket-Version: 13\r\nCookie: secret\r\n\r\n").encode())
            stream = client.makefile("rb")
            self.assertIn(b"101", stream.readline())
            while stream.readline() != b"\r\n":
                pass
            self.assertEqual(stream.read(5), b"\x82\x03RFB")
            client.sendall(b"hello")
            self.assertEqual(stream.read(5), b"hello")
            self.assertNotIn("Cookie", self.upstream.received[1])
            stream.close()


if __name__ == "__main__":
    unittest.main()
