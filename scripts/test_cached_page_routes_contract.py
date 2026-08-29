"""Contracts for cached page snapshot route registrars."""

import ast
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import unittest

from routes.router import Router
from routes.metrics import register_metrics_page
from routes.shop import register_shop_page
from routes.taobao import register_taobao_page


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


class CachedPageRoutesTests(unittest.TestCase):
    def test_snapshots_render_with_exact_paths_and_headers(self) -> None:
        router, calls = Router(), []

        def inject(html: str, path: str) -> str:
            calls.append((html, path))
            return f"{html}|{path}"

        cases = (
            (register_shop_page, "/shop", "shop"),
            (register_metrics_page, "/metrics", "metrics"),
            (register_taobao_page, "/taobao", ""),
        )
        for registrar, _path, snapshot in cases:
            registrar(router, html_snapshot=snapshot, inject_nav=inject)
        for _registrar, path, snapshot in cases:
            match, handler = router.resolve("GET", path), Handler()
            self.assertEqual(dict(match.params), {})
            match.handler(handler, match.params)
            body = f"{snapshot}|{path}".encode()
            headers = dict(handler.headers)
            self.assertEqual(handler.responses, [200])
            self.assertEqual(handler.wfile.getvalue(), body)
            self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(headers["Content-Length"], str(len(body)))
            self.assertEqual(headers["Cache-Control"], "no-cache, no-store, must-revalidate")
        self.assertEqual(calls, [("shop", "/shop"), ("metrics", "/metrics"), ("", "/taobao")])

    def test_web_app_keeps_snapshot_loading_and_late_registration_contracts(self) -> None:
        root = Path(__file__).resolve().parent
        source = (root / "web_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments: dict[str, ast.expr] = {}
        calls: dict[str, int] = {}
        main_line = next(
            node.lineno for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = node.value
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                function = node.value.func
                if isinstance(function, ast.Name) and function.id.startswith("register_"):
                    calls[function.id] = node.lineno
        metrics = ast.unparse(assignments["METRICS_HTML"])
        shop = ast.unparse(assignments["SHOP_HTML"])
        taobao = ast.unparse(assignments["TAOBAO_HTML"])
        self.assertEqual(metrics, "(SCRIPTS_DIR / 'static' / 'metrics.html').read_text(encoding='utf-8')")
        self.assertIn("SHOP_HTML_PATH.is_file()", shop)
        self.assertTrue(shop.endswith("else ''"))
        self.assertIn("TAOBAO_HTML_PATH.is_file()", taobao)
        self.assertTrue(taobao.endswith("else ''"))
        for registrar, snapshot in (
            ("register_shop_page", "SHOP_HTML"),
            ("register_metrics_page", "METRICS_HTML"),
            ("register_taobao_page", "TAOBAO_HTML"),
        ):
            self.assertGreater(calls[registrar], next(
                node.lineno for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == snapshot for target in node.targets)
            ))
            self.assertLess(calls[registrar], main_line)

    def test_cached_route_modules_do_not_read_files_or_import_web_app(self) -> None:
        routes_dir = Path(__file__).resolve().parent / "routes"
        for name in ("shop.py", "metrics.py", "taobao.py"):
            source = (routes_dir / name).read_text(encoding="utf-8")
            for forbidden in ("Path", "read_text", "open(", "web_app"):
                self.assertNotIn(forbidden, source, name)

if __name__ == "__main__":
    unittest.main()
