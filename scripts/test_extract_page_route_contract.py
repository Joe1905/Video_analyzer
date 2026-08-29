"""Contracts for the Router-wired extraction page."""

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from routes.extract import register_extract_page
from routes.router import Router


@dataclass
class Handler:
    responses: list[object] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    wfile: BytesIO = field(default_factory=BytesIO)
    def send_response(self, status: object) -> None:
        self.responses.append(status)

    def send_header(self, key: str, value: str) -> None:
        self.headers.append((key, value))

    def end_headers(self) -> None:
        pass


class ExtractPageRouteTests(unittest.TestCase):
    def test_template_and_mode_are_fresh_and_replaced_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "page.html"
            modes = iter(("one", "two"))
            calls: list[tuple[str, str]] = []

            def mode() -> str:
                return next(modes)

            def nav(html: str, active: str) -> str:
                calls.append((html, active))
                return html + "|nav"

            router = Router()
            register_extract_page(
                router,
                template_path=path,
                analysis_mode=mode,
                inject_nav=nav,
            )
            cases = (
                ("__DEFAULT_ANALYSIS_MODE__ __DEFAULT_ANALYSIS_MODE__", "one one"),
                ("plain", "plain"),
            )
            for template, expected in cases:
                path.write_text(template, encoding="utf-8")
                match = router.resolve("GET", "/extract")
                handler = Handler()
                self.assertEqual(dict(match.params), {})
                match.handler(handler, match.params)
                body = (expected + "|nav").encode()
                headers = dict(handler.headers)
                self.assertEqual(handler.responses, [200])
                self.assertEqual(handler.wfile.getvalue(), body)
                self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(headers["Content-Length"], str(len(body)))
                self.assertEqual(headers["Cache-Control"], "no-cache, no-store, must-revalidate")
            self.assertEqual(calls, [("one one", "/extract"), ("plain", "/extract")])

    def test_file_errors_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.html"
            router = Router()
            register_extract_page(
                router,
                template_path=path,
                analysis_mode=lambda: "x",
                inject_nav=lambda html, active_path: html,
            )
            route = router.resolve("GET", "/extract")
            with self.assertRaises(FileNotFoundError):
                route.handler(Handler(), {})
            path.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                route.handler(Handler(), {})

    def test_route_module_has_no_config_or_web_app_dependency(self) -> None:
        source = (Path(__file__).resolve().parent / "routes" / "extract.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("import os", "getenv", "web_app", "APP_CONFIG"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
