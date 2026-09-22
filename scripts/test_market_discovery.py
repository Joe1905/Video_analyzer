"""Isolated persistence/HTTP/tool-boundary checks; uses temporary data, no API credits."""
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlparse

import market_discovery as md


def seller_tools(_domain):
    return [{"name": name, "description": name, "inputSchema": {"type": "object", "properties": {
        "request": {"type": "object"}}, "required": ["request"]}}
            for name in ("google_trend", "aba_research_weekly", "delete_account")]


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = md.Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_alias_fuzzy_search_and_workspace_isolation(self):
        row = self.store.save_category("one", {"record": {"name": "旅行冲洗", "aliases": ["portable bidet", "便携冲洗器"],
                                                         "audience": "旅行者", "region": "US"}})
        self.assertEqual(self.store.search("one", "portble bidet")[0]["id"], row["id"])
        self.assertEqual(self.store.search("one", "便携式冲洗器")[0]["id"], row["id"])
        self.assertEqual(self.store.search("two", "portable bidet"), [])
        self.assertEqual(self.store.search("one", "%' OR 1=1 --"), [])

    def test_category_revision_and_history(self):
        row = self.store.save_category("one", {"record": {"name": "市场", "status": "待查"}})
        changed = self.store.save_category("one", {"id": row["id"], "expected_revision": 1,
                                                  "record": {"name": "市场", "status": "暂缓"}})
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.store.category("one", row["id"], 1)["status"], "待查")
        with self.assertRaises(md.ApiError) as err:
            self.store.save_category("one", {"id": row["id"], "expected_revision": 1, "record": {"name": "stale"}})
        self.assertEqual(err.exception.status, 409)

    def test_document_versions_and_reopen(self):
        p = {"key": "reports/2026-09-22.md", "content": "# 中文日报\n真实证据"}
        self.store.save_document("one", p)
        self.store.save_document("one", {**p, "content": "新版", "expected_revision": 1})
        reopened = md.Store(self.temp.name)
        self.assertEqual(reopened.documents("one", p["key"], 1)["content"], p["content"])
        self.assertEqual(reopened.documents("one", p["key"])["content"], "新版")
        self.assertEqual(reopened.documents("two"), [])
        for key in ("../secret.md", "/tmp/file.md", "C:/file.md", "a\\b.md", "a/./b.md"):
            with self.assertRaises(md.ApiError):
                reopened.save_document("one", {"key": key, "content": "no"})

    def test_competing_updates_do_not_silently_overwrite(self):
        self.store.save_document("one", {"key": "research.md", "content": "initial"})
        def update(text):
            try:
                return self.store.save_document("one", {"key": "research.md", "content": text, "expected_revision": 1})["revision"]
            except md.ApiError as exc:
                return exc.status
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(update, ("first", "second")))
        self.assertEqual(sorted(results), [2, 409])

    def test_tools_enforced_at_execution(self):
        calls = []
        def execute(name, args):
            calls.append(name)
            return {"ok": True, "data": {"content": [{"type": "text", "text": "real-shape"}]}}
        exposed = md.catalog(seller_tools)
        self.assertNotIn("sellersprite__delete_account", [t["name"] for t in exposed["tools"]])
        for name in ("sellersprite__delete_account", "fastmoss__google_trend", "function__web_search"):
            with self.assertRaises(md.ApiError):
                md.execute(name, {"request": {}}, seller_tools, execute)
        self.assertEqual(calls, [])
        result = md.execute("sellersprite__google_trend", {"request": {"marketplace": "US"}}, seller_tools, execute)
        self.assertEqual(calls, ["sellersprite__google_trend"])
        self.assertIn("content", result)
        with self.assertRaises(md.ApiError):
            md.execute("sociavault__tiktok_trending", {"api_key": "never-forward"}, seller_tools, execute)

    def test_manifest_isolated_and_complete(self):
        manifest = md.manifest()
        self.assertFalse(manifest["frontend"])
        self.assertEqual(manifest["execution_mode"], "external_agent")
        self.assertIn("market-feasibility/SKILL.md", manifest["skills"])
        self.assertIn("sociavault-market-discovery/references/category-memory.md", manifest["skills"])

    def test_expanded_social_routes_and_argument_boundary(self):
        specs = md.socia_tools()
        self.assertEqual(len({s["name"] for s in specs}), len(specs))
        examples = [
            ("twitter_search", {"query": "removable wallpaper"}, "/v1/scrape/twitter/search"),
            ("facebook_group_posts", {"url": "https://www.facebook.com/groups/example"}, "/v1/scrape/facebook/group/posts"),
            ("youtube_video_transcript", {"url": "https://www.youtube.com/watch?v=example"}, "/v1/scrape/youtube/video/transcript"),
        ]
        for name, args, path in examples:
            response = Mock(ok=True)
            response.json.return_value = {"success": True, "data": {"items": []}}
            with patch.dict(md.os.environ, {"SOCIAVAULT_API_KEY": "test-only", "SOCIAVAULT_API_BASE": "https://api.sociavault.com"}), \
                 patch("requests.get", return_value=response) as get, \
                 patch("sociavault_usage.update_sociavault_usage_from_response"):
                md.execute("sociavault__" + name, args, seller_tools, lambda *a: None)
                self.assertEqual(get.call_args.args[0], "https://api.sociavault.com" + path)
                for key, value in args.items():
                    self.assertEqual(get.call_args.kwargs["params"][key], value)
                with self.assertRaises(md.ApiError):
                    md.execute("sociavault__" + name, {**args, "endpoint": "/override"}, seller_tools, lambda *a: None)
                self.assertEqual(get.call_count, 1)

    def test_http_success_can_contain_business_failure(self):
        envelope = {"isError": False, "content": [{"type": "text", "text": '{"code":"ERROR_PARAM","message":"日期参数错误"}'}]}
        self.assertFalse(md.provider_ok(envelope))
        self.assertTrue(md.provider_ok({"content": [{"type": "text", "text": '{"code":"OK","data":{}}'}]}))
        self.assertFalse(md.provider_ok({"success": True, "data": {"success": False}}))
        self.assertIsNone(md.provider_ok({"content": [{"type": "text", "text": "unknown"}]}))

    def test_http_markdown_history_and_raw_call(self):
        store = self.store
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def route(self):
                if not md.handle(self, urlparse(self.path), seller_tools,
                                 lambda *_: {"ok": True, "data": {"isError": True, "content": []}}, store):
                    self.send_response(404)
                    self.end_headers()
            do_GET = route
            do_POST = route
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, route, payload=None):
            connection = http.client.HTTPConnection(*server.server_address)
            raw = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
            connection.request(method, route, raw, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = response.status, response.getheader("Content-Type"), response.read().decode()
            connection.close()
            return result
        try:
            p = {"workspace": "test", "key": "reports/day.md", "content": "# 日报\n保留未知"}
            self.assertEqual(request("POST", md.PREFIX + "documents", p)[0], 200)
            status, mime, text = request("GET", md.PREFIX + "documents?" + urlencode({"workspace": "test", "key": p["key"], "format": "markdown"}))
            self.assertEqual((status, text), (200, p["content"]))
            self.assertTrue(mime.startswith("text/markdown"))
            self.assertEqual(request("POST", md.PREFIX + "documents", p)[0], 409)
            response = request("POST", md.PREFIX + "tools/call", {"workspace": "test", "name": "sellersprite__google_trend", "arguments": {"request": {}}})
            result = json.loads(response[2])
            recorded = json.loads(request("GET", md.PREFIX + "calls/" + result["call_id"] + "?workspace=test")[2])
            self.assertTrue(recorded["result"]["isError"])
            self.assertEqual(request("GET", "/api/chat/sessions")[0], 404)
            self.assertEqual(request("GET", md.PREFIX + "calls/" + result["call_id"] + "?workspace=other")[0], 404)
            self.assertEqual(request("POST", md.PREFIX + "tools/call", {"name": "sellersprite__delete_account"})[0], 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
