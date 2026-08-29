"""Contract coverage for the only Phase 2.2A Router-wired endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
import unittest

from routes.health import register_health_route
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


class HealthRouteContractTests(unittest.TestCase):
    def test_health_response_is_exact_for_true_and_false_modes(self) -> None:
        for ui_test_mode, expected in (
            (True, b'{\n  "status": "ok",\n  "ui_test_mode": true\n}'),
            (False, b'{\n  "status": "ok",\n  "ui_test_mode": false\n}'),
        ):
            with self.subTest(ui_test_mode=ui_test_mode):
                router = Router()
                register_health_route(router, ui_test_mode=ui_test_mode)
                match = router.resolve("GET", "/healthz")
                handler = RecordingHandler()
                self.assertEqual(dict(match.params), {})
                match.handler(handler, match.params)
                headers = dict(handler.headers)
                self.assertEqual(handler.responses, [200])
                self.assertEqual(handler.wfile.getvalue(), expected)
                self.assertEqual(len(expected), 44 if ui_test_mode else 45)
                self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
                self.assertEqual(headers.get("Content-Length"), str(len(expected)))
                self.assertNotIn("Cache-Control", headers)
                self.assertNotIn("Allow", headers)


if __name__ == "__main__":
    unittest.main()
