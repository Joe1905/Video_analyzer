"""Black-box contracts for the Router-wired daily-report pages."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from routes.report_pages import register_report_pages
from routes.router import Router


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


class ReportPageRouteContractTests(unittest.TestCase):
    def test_report_pages_read_and_render_fresh_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scripts_dir = Path(temporary)
            static_dir = scripts_dir / "static"
            static_dir.mkdir()
            templates = {
                "/report": static_dir / "report.html",
                "/report/player": static_dir / "report_player.html",
            }
            for path, template in templates.items():
                template.write_text(f"first:{path}", encoding="utf-8")
            calls: list[tuple[str, str]] = []

            def inject_nav(html: str, path: str) -> str:
                calls.append((html, path))
                return f"{html}|nav:{path}"

            router = Router()
            register_report_pages(router, scripts_dir=scripts_dir, inject_nav=inject_nav)
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
                    ("first:/report", "/report"),
                    ("second:/report", "/report"),
                    ("first:/report/player", "/report/player"),
                    ("second:/report/player", "/report/player"),
                ],
            )

    def test_missing_template_error_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scripts_dir = Path(temporary)
            (scripts_dir / "static").mkdir()
            router = Router()
            register_report_pages(router, scripts_dir=scripts_dir, inject_nav=lambda html, path: html)
            match = router.resolve("GET", "/report")
            with self.assertRaises(FileNotFoundError):
                match.handler(RecordingHandler(), match.params)


if __name__ == "__main__":
    unittest.main()
