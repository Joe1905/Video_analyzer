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

    def test_routes_are_one_way_and_web_app_only_imports_registered_route_modules(self) -> None:
        root = Path(__file__).resolve().parent
        for route_file in (root / "routes").glob("*.py"):
            module = ast.parse(route_file.read_text(encoding="utf-8"))
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], {"dataclasses", "types", "typing", "re", "pathlib"})
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertIn(
                        node.module,
                        {
                            "__future__",
                            "dataclasses",
                            "http",
                            "pathlib",
                            "types",
                            "typing",
                            "core.http",
                            "routes.router",
                        },
                    )
                    self.assertNotIn("web_app", node.module)
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
                "routes.lan_chat",
                "routes.report_pages",
                "routes.router",
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
        self.assertTrue(any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and any(isinstance(value, ast.Constant) and value.value == "/harness" for value in node.test.comparators)
            for node in ast.walk(get_method)
        ))
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
        source = (root / "web_app.py").read_text(encoding="utf-8")
        self.assertIn('parsed.path.startswith("/api/lan-chat/") and handle_lan_chat_get', source)
        self.assertIn('if parsed.path == "/api/tool/convert":', source)


if __name__ == "__main__":
    unittest.main()
