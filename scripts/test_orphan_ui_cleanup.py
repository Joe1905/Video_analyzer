"""Route-level contract for the v2 orphan-UI cleanup."""

from __future__ import annotations

import http.client
import threading
import unittest
from http import HTTPStatus
from http.server import ThreadingHTTPServer

import web_app


class TestOrphanUiCleanup(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    @classmethod
    def request(cls, method: str, path: str) -> tuple[int, dict[str, str], str]:
        connection = http.client.HTTPConnection("127.0.0.1", cls.server.server_port, timeout=10)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, headers, body

    def test_fastmoss_only_redirects_to_canonical_chuhaijiang(self) -> None:
        for method, path in (
            ("GET", "/fastmoss"),
            ("GET", "/fastmoss/"),
            ("GET", "/fastmoss/api/ask"),
            ("POST", "/fastmoss/api/chat/export-pdf"),
        ):
            with self.subTest(method=method, path=path):
                status, headers, body = self.request(method, path)
                self.assertEqual(status, HTTPStatus.TEMPORARY_REDIRECT)
                self.assertEqual(headers.get("location"), "/chuhaijiang")
                self.assertNotIn("FastMoss", body)

    def test_ai_chat_routes_use_the_shared_canonical_shell(self) -> None:
        for path, canonical in (("/amazon/", "/amazon"), ("/chuhaijiang/", "/chuhaijiang")):
            with self.subTest(path=path):
                status, headers, _body = self.request("GET", path)
                self.assertEqual(status, HTTPStatus.TEMPORARY_REDIRECT)
                self.assertEqual(headers.get("location"), canonical)

        pages = {
            path: self.request("GET", path)
            for path in ("/", "/amazon", "/chuhaijiang")
        }
        for path, (status, _headers, body) in pages.items():
            with self.subTest(path=path):
                self.assertEqual(status, HTTPStatus.OK)
                self.assertIn('class="chat-shell"', body)
                self.assertIn('id="ui-system-css"', body)
                self.assertIn('id="ui-system-js"', body)
                self.assertIn('/assets/ui-system.css?v=', body)
                self.assertIn('/assets/ui-system.js?v=', body)

        chuhaijiang = pages["/chuhaijiang"][2]
        self.assertIn('const CHAT_PROVIDER="chuhaijiang"', chuhaijiang)
        self.assertNotIn('/fastmoss/api/', chuhaijiang)
        self.assertNotIn('data-chuhaijiang-independent="1"', chuhaijiang)
        self.assertNotIn('const BASE_PATH=', chuhaijiang)

    def test_active_pages_and_shared_assets_remain_available(self) -> None:
        for path in (
            "/", "/chat", "/amazon", "/lan-chat", "/report", "/report/player",
            "/extract", "/shop", "/tool", "/metrics",
            "/assets/ui-system.css", "/assets/ui-system.js", "/assets/lan-chat.css",
            "/assets/vendor/markdown-it.min.js", "/assets/default-avatars/01.png",
        ):
            with self.subTest(path=path):
                status, _headers, _body = self.request("GET", path)
                self.assertEqual(status, HTTPStatus.OK)

    def test_removed_static_resources_return_not_found(self) -> None:
        for path in (
            "/assets/nav/nav-amazon.png",
            "/assets/nav/nav-analysis.png",
            "/assets/nav/nav-data.png",
            "/assets/nav/nav-home.png",
            "/assets/nav/nav-report.png",
            "/assets/nav/nav-shop.png",
        ):
            with self.subTest(path=path):
                status, _headers, _body = self.request("GET", path)
                self.assertEqual(status, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
