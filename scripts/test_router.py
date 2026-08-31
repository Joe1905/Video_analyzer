"""Contract tests for the Phase 2.1 pure route matcher."""

import unittest
from pathlib import Path
import ast
from dataclasses import FrozenInstanceError

from routes.router import (
    MethodNotAllowed,
    RouteConflictError,
    RouteNotFound,
    Router,
)


def _handler(*args: object, **kwargs: object) -> object:
    raise AssertionError("resolve must not execute handlers")


class RouterTests(unittest.TestCase):
    def test_get_post_delete_and_exact_path(self) -> None:
        router = Router()
        router.get("/", _handler)
        router.get("/items", _handler)
        router.post("/items", _handler)
        router.delete("/items", _handler)
        self.assertIs(router.resolve("GET", "/").handler, _handler)
        for method in ("GET", "POST", "DELETE"):
            self.assertIs(router.resolve(method, "/items").handler, _handler)

    def test_single_and_multiple_path_parameters(self) -> None:
        router = Router()
        router.get("/items/{item_id}", _handler)
        router.get("/shops/{shop}/items/{item}", _handler)
        self.assertEqual(dict(router.resolve("GET", "/items/42").params), {"item_id": "42"})
        self.assertEqual(dict(router.resolve("GET", "/shops/a/items/b").params), {"shop": "a", "item": "b"})

    def test_raw_percent_encoding_and_path_shape_are_preserved(self) -> None:
        router = Router()
        router.get("/items/{item}", _handler)
        router.get("/items/", _handler)
        router.get("/double//slash", _handler)
        self.assertEqual(dict(router.resolve("GET", "/items/a%2Fb").params), {"item": "a%2Fb"})
        self.assertIs(router.resolve("GET", "/items/").handler, _handler)
        self.assertIs(router.resolve("GET", "/double//slash").handler, _handler)
        with self.assertRaises(RouteNotFound):
            router.resolve("GET", "/items")

    def test_parameter_neither_accepts_empty_segments_nor_crosses_slashes(self) -> None:
        router = Router()
        router.get("/items/{item}", _handler)
        with self.assertRaises(RouteNotFound):
            router.resolve("GET", "/items/")
        with self.assertRaises(RouteNotFound):
            router.resolve("GET", "/items/a/b")

    def test_route_match_and_params_are_immutable(self) -> None:
        router = Router()
        router.get("/items/{item}", _handler)
        match = router.resolve("GET", "/items/a")
        with self.assertRaises(FrozenInstanceError):
            match.handler = _handler  # type: ignore[misc]
        with self.assertRaises(TypeError):
            match.params["item"] = "changed"  # type: ignore[index]

    def test_noncallable_handler_is_rejected_without_registering_a_route(self) -> None:
        router = Router()
        with self.assertRaises(TypeError):
            router.get("/blocked", object())  # type: ignore[arg-type]
        with self.assertRaises(RouteNotFound):
            router.resolve("GET", "/blocked")

    def test_invalid_inputs_and_templates_are_rejected(self) -> None:
        router = Router()
        for method in ("get", "PUT", "", None):
            with self.assertRaises(ValueError):
                router.add(method, "/x", _handler)  # type: ignore[arg-type]
        for path in ("x", "/x?query", "/x#fragment"):
            with self.assertRaises(ValueError):
                router.get(path, _handler)
            with self.assertRaises(ValueError):
                router.resolve("GET", path)
        for pattern in ("/x/{bad-name}", "/x/a{id}", "/x/{id}/{id}"):
            with self.assertRaises(ValueError):
                router.get(pattern, _handler)

    def test_literal_specificity_wins_independent_of_registration_order(self) -> None:
        def parameter_handler() -> None:
            pass

        def literal_handler() -> None:
            pass

        for first, second in ((parameter_handler, literal_handler), (literal_handler, parameter_handler)):
            router = Router()
            for handler in (first, second):
                if handler is parameter_handler:
                    router.get("/items/{item}", handler)
                else:
                    router.get("/items/current", handler)
            self.assertIs(router.resolve("GET", "/items/current").handler, literal_handler)

    def test_equal_specificity_overlap_and_canonical_parameter_names_conflict(self) -> None:
        router = Router()
        router.get("/items/{id}", _handler)
        with self.assertRaises(RouteConflictError):
            router.get("/items/{name}", _handler)
        self.assertIs(router.resolve("GET", "/items/1").handler, _handler)
        router = Router()
        router.get("/items/{id}/detail", _handler)
        with self.assertRaises(RouteConflictError):
            router.get("/items/current/{part}", _handler)

    def test_different_methods_are_allowed_and_404_405_are_stable(self) -> None:
        router = Router()
        router.post("/items/{item}", _handler)
        router.get("/items/current", _handler)
        router.delete("/items/{item}", _handler)
        self.assertIs(router.resolve("POST", "/items/current").handler, _handler)
        with self.assertRaises(MethodNotAllowed) as captured:
            router.resolve("HEAD", "/items/current")
        self.assertEqual(captured.exception.allowed_methods, ("GET", "POST", "DELETE"))
        with self.assertRaises(RouteNotFound):
            router.resolve("GET", "/missing")

    def test_get_prefix_preserves_raw_suffix_and_uses_longest_match(self) -> None:
        def assets_handler() -> None:
            pass

        def vendor_handler() -> None:
            pass

        for registration in (("assets", "vendor"), ("vendor", "assets")):
            with self.subTest(registration=registration):
                router = Router()
                for route_name in registration:
                    if route_name == "assets":
                        router.get_prefix("/assets/", assets_handler)
                    else:
                        router.get_prefix("/assets/vendor/", vendor_handler)
                assets = router.resolve("GET", "/assets/nested//raw%2Fbundle.js")
                vendor = router.resolve("GET", "/assets/vendor/markdown-it.min.js")
                empty_suffix = router.resolve("GET", "/assets/")
                self.assertIs(assets.handler, assets_handler)
                self.assertEqual(
                    dict(assets.params), {"suffix": "nested//raw%2Fbundle.js"}
                )
                self.assertIs(vendor.handler, vendor_handler)
                self.assertEqual(dict(vendor.params), {"suffix": "markdown-it.min.js"})
                self.assertEqual(dict(empty_suffix.params), {"suffix": ""})
                with self.assertRaises(TypeError):
                    assets.params["suffix"] = "changed"  # type: ignore[index]

    def test_exact_and_template_routes_take_precedence_over_prefix_routes(self) -> None:
        for first, second in (("prefix", "routes"), ("routes", "prefix")):
            with self.subTest(first=first):
                router = Router()

                def prefix_handler() -> None:
                    pass

                def exact_handler() -> None:
                    pass

                def template_handler() -> None:
                    pass

                for registration in (first, second):
                    if registration == "prefix":
                        router.get_prefix("/assets/", prefix_handler)
                    else:
                        router.get("/assets/current", exact_handler)
                        router.get("/assets/{name}", template_handler)
                self.assertIs(router.resolve("GET", "/assets/current").handler, exact_handler)
                self.assertIs(router.resolve("GET", "/assets/other").handler, template_handler)
                prefix_match = router.resolve("GET", "/assets/nested/item")
                self.assertIs(prefix_match.handler, prefix_handler)
                self.assertEqual(dict(prefix_match.params), {"suffix": "nested/item"})

        router = Router()
        router.post("/assets/{name}", _handler)
        router.delete("/assets/{name}", _handler)
        router.get_prefix("/assets/", _handler)
        for method in ("GET", "HEAD"):
            with self.subTest(method=method), self.assertRaises(MethodNotAllowed) as captured:
                router.resolve(method, "/assets/current")
            self.assertEqual(captured.exception.allowed_methods, ("POST", "DELETE"))

    def test_get_prefix_rejects_invalid_or_duplicate_definitions_and_keeps_methods_strict(self) -> None:
        router = Router()
        for prefix in (
            "assets/",
            "/assets",
            "/",
            "/assets?query",
            "/assets#fragment",
            "/assets/{name}/",
            "/assets/*.css",
            "/assets/[name]/",
        ):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ValueError):
                    router.get_prefix(prefix, _handler)
        with self.assertRaises(TypeError):
            router.get_prefix("/assets/", object())  # type: ignore[arg-type]
        with self.assertRaises(RouteNotFound):
            router.resolve("GET", "/assets/item.css")
        router.get_prefix("/assets/", _handler)
        with self.assertRaises(RouteConflictError):
            router.get_prefix("/assets/", _handler)
        for method in ("POST", "DELETE", "HEAD"):
            with self.subTest(method=method), self.assertRaises(MethodNotAllowed) as captured:
                router.resolve(method, "/assets/item.css")
            self.assertEqual(captured.exception.allowed_methods, ("GET",))
        with self.assertRaises(RouteNotFound):
            router.resolve("GET", "/assets")

    def test_routes_are_one_way_and_web_app_only_imports_registered_route_modules(self) -> None:
        root = Path(__file__).resolve().parent
        for route_file in (root / "routes").glob("*.py"):
            module = ast.parse(route_file.read_text(encoding="utf-8"))
            allowed_imports = {"dataclasses", "types", "typing", "re", "pathlib"}
            allowed_from_modules = {
                "__future__",
                "dataclasses",
                "http",
                "pathlib",
                "types",
                "typing",
                "core.http",
                "routes.router",
                "urllib.parse",
            }
            if route_file.name == "shop.py":
                allowed_imports.update({"json", "os", "time"})
                allowed_from_modules.add("services.shop")
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], allowed_imports)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertIn(node.module, allowed_from_modules)
                    self.assertNotIn("web_app", node.module)
                    if route_file.name != "shop.py":
                        self.assertFalse(node.module.startswith("services."))
            if route_file.name == "router.py":
                imports = {
                    alias.name.split(".")[0]
                    for node in ast.walk(module)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                }
                imports.update(
                    node.module.split(".")[0]
                    for node in ast.walk(module)
                    if isinstance(node, ast.ImportFrom) and node.module
                )
                self.assertEqual(imports, {"__future__", "dataclasses", "types", "typing", "re"})
        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        route_imports: set[str] = set()
        for node in ast.walk(web_app):
            if isinstance(node, ast.Import):
                route_imports.update(
                    alias.name for alias in node.names
                    if alias.name == "routes" or alias.name.startswith("routes.")
                )
            elif isinstance(node, ast.ImportFrom):
                if node.module == "routes" or bool(node.module and node.module.startswith("routes.")):
                    route_imports.add(node.module)
        self.assertEqual(
            route_imports,
            {
                "routes.health",
                "routes.extract",
                "routes.harness",
                "routes.harness_certificate",
                "routes.lan_chat",
                "routes.metrics",
                "routes.report_pages",
                "routes.router",
                "routes.static_assets",
                "routes.shop",
                "routes.taobao",
                "routes.tool",
            },
        )
        handler = next(
            node for node in web_app.body
            if isinstance(node, ast.ClassDef) and node.name == "Handler"
        )
        get_method = next(
            node for node in handler.body
            if isinstance(node, ast.FunctionDef) and node.name == "do_GET"
        )
        exact_route_branches = {
            value.value
            for node in ast.walk(get_method)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            for value in node.test.comparators
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        }
        self.assertNotIn("/report", exact_route_branches)
        self.assertNotIn("/report/player", exact_route_branches)
        self.assertNotIn("/lan-chat", exact_route_branches)
        self.assertNotIn("/tool", exact_route_branches)
        self.assertNotIn("/harness", exact_route_branches)
        self.assertNotIn("/harness-ca.crt", exact_route_branches)
        self.assertNotIn("/extract", exact_route_branches)
        self.assertNotIn("/shop", exact_route_branches)
        self.assertNotIn("/metrics", exact_route_branches)
        self.assertNotIn("/taobao", exact_route_branches)
        source = (root / "web_app.py").read_text(encoding="utf-8")
        self.assertIn('parsed.path.startswith("/api/lan-chat/") and handle_lan_chat_get', source)
        self.assertIn('if parsed.path == "/api/tool/convert":', source)
        self.assertIn('if parsed.path.startswith("/api/taobao/"):', source)
        self.assertIn("register_shop_api_routes(WEB_ROUTER, shop_service)", source)
        self.assertNotIn('if parsed.path == "/api/shop-job":', source)
        self.assertIn('if parsed.path == "/api/video-metrics-job":', source)
        self.assertIn('if parsed.path == "/api/analyze":', source)


if __name__ == "__main__":
    unittest.main()
