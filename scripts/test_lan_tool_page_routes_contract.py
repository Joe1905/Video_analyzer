"""Contracts for the Router-wired LAN chat and utility page routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from routes.lan_chat import register_lan_chat_page
from routes.router import Router
from routes.tool import register_tool_page


@dataclass
class RecordingHandler:
    responses: list[object] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    wfile: BytesIO = field(default_factory=BytesIO)

    def send_response(self, status: object) -> None:
        self.responses.append(status)

    def send_header(self, key: str, value: str) -> None:
        self.headers.append((key, value))

    def end_headers(self) -> None:
        pass


class LanToolPageRouteContractTests(unittest.TestCase):
    def test_pages_read_fresh_templates_and_render_exact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scripts_dir = Path(temporary)
            static_dir = scripts_dir / "static"
            static_dir.mkdir()
            templates = {
                "/lan-chat": static_dir / "lan_chat.html",
                "/tool": static_dir / "tool.html",
            }
            calls: list[tuple[str, str]] = []

            def inject_nav(html: str, path: str) -> str:
                calls.append((html, path))
                return f"{html}|nav:{path}"

            router = Router()
            register_lan_chat_page(router, scripts_dir=scripts_dir, inject_nav=inject_nav)
            register_tool_page(router, scripts_dir=scripts_dir, inject_nav=inject_nav)
            for path, template in templates.items():
                for content in (f"first:{path}", f"second:{path}"):
                    template.write_text(content, encoding="utf-8")
                    match = router.resolve("GET", path)
                    handler = RecordingHandler()
                    self.assertEqual(dict(match.params), {})
                    match.handler(handler, match.params)
                    expected = f"{content}|nav:{path}".encode("utf-8")
                    headers = dict(handler.headers)
                    self.assertEqual(handler.responses, [200])
                    self.assertEqual(handler.wfile.getvalue(), expected)
                    self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
                    self.assertEqual(headers.get("Content-Length"), str(len(expected)))
                    self.assertEqual(headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")
            self.assertEqual(
                calls,
                [
                    ("first:/lan-chat", "/lan-chat"),
                    ("second:/lan-chat", "/lan-chat"),
                    ("first:/tool", "/tool"),
                    ("second:/tool", "/tool"),
                ],
            )

    def test_missing_templates_raise_without_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scripts_dir = Path(temporary)
            (scripts_dir / "static").mkdir()
            router = Router()
            register_lan_chat_page(router, scripts_dir=scripts_dir, inject_nav=lambda html, path: html)
            register_tool_page(router, scripts_dir=scripts_dir, inject_nav=lambda html, path: html)
            for path in ("/lan-chat", "/tool"):
                with self.subTest(path=path):
                    match = router.resolve("GET", path)
                    with self.assertRaises(FileNotFoundError):
                        match.handler(RecordingHandler(), match.params)


if __name__ == "__main__":
    unittest.main()
