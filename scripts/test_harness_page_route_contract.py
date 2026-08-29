"""Contract tests for the Router-wired standalone harness page."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from routes.harness import register_harness_page
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


class HarnessPageRouteContractTests(unittest.TestCase):
    def test_harness_reads_fresh_template_without_navigation_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scripts_dir = Path(temporary)
            static_dir = scripts_dir / "static"
            static_dir.mkdir()
            template = static_dir / "harness.html"
            router = Router()
            register_harness_page(router, scripts_dir=scripts_dir)
            for content in ("first harness", "second harness"):
                template.write_text(content, encoding="utf-8")
                match = router.resolve("GET", "/harness")
                handler = RecordingHandler()
                self.assertEqual(dict(match.params), {})
                match.handler(handler, match.params)
                expected = content.encode("utf-8")
                headers = dict(handler.headers)
                self.assertEqual(handler.responses, [200])
                self.assertEqual(handler.wfile.getvalue(), expected)
                self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
                self.assertEqual(headers.get("Content-Length"), str(len(expected)))
                self.assertEqual(headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")

    def test_missing_template_error_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scripts_dir = Path(temporary)
            (scripts_dir / "static").mkdir()
            router = Router()
            register_harness_page(router, scripts_dir=scripts_dir)
            match = router.resolve("GET", "/harness")
            with self.assertRaises(FileNotFoundError):
                match.handler(RecordingHandler(), match.params)


if __name__ == "__main__":
    unittest.main()
