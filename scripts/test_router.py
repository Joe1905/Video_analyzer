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
from routes.upload import MAX_UPLOAD_BYTES


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
            elif route_file.name == "metrics.py":
                allowed_imports.update({"json", "time"})
                allowed_from_modules.add("services.metrics")
            elif route_file.name == "amazon.py":
                allowed_imports.update({"json", "os", "time"})
                allowed_from_modules.add("services.amazon")
            elif route_file.name == "downloads.py":
                allowed_imports.update({"json", "time"})
                allowed_from_modules.add("services.downloads")
            elif route_file.name == "upload.py":
                allowed_imports.add("cgi")
                allowed_from_modules.add("services.upload")
            elif route_file.name == "analyze.py":
                allowed_imports.update({"json", "os"})
                allowed_from_modules.add("services.analyze")
            elif route_file.name == "translate.py":
                allowed_imports.add("json")
                allowed_from_modules.add("services.translate")
            elif route_file.name == "postprocess.py":
                allowed_imports.add("json")
                allowed_from_modules.add("services.postprocess")
            elif route_file.name == "report.py":
                allowed_imports.update({"json", "time"})
                allowed_from_modules.add("services.report")
            elif route_file.name == "video_files.py":
                allowed_from_modules.add("services.video_files")
            elif route_file.name == "video_result.py":
                allowed_from_modules.add("services.video_result")
            elif route_file.name == "video_delete.py":
                allowed_imports.add("json")
                allowed_from_modules.add("services.video_delete")
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], allowed_imports)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertIn(node.module, allowed_from_modules)
                    self.assertNotIn("web_app", node.module)
                    if route_file.name not in {"shop.py", "metrics.py", "amazon.py", "downloads.py", "upload.py", "analyze.py", "translate.py", "postprocess.py", "report.py", "video_files.py", "video_result.py", "video_delete.py"}:
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
                "routes.amazon",
                "routes.analyze",
                "routes.downloads",
                "routes.postprocess",
                "routes.translate",
                "routes.upload",
                "routes.video_delete",
                "routes.video_files",
                "routes.video_result",
                "routes.video_stream",
                "routes.health",
                "routes.extract",
                "routes.harness",
                "routes.harness_certificate",
                "routes.lan_chat",
                "routes.metrics",
                "routes.report",
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
        self.assertIn("register_metrics_api_routes(WEB_ROUTER, metrics_service)", source)
        self.assertIn("register_amazon_routes(WEB_ROUTER, amazon_service)", source)
        self.assertIn("register_download_routes(WEB_ROUTER, download_service)", source)
        self.assertIn("register_analyze_routes(WEB_ROUTER, analyze_service)", source)
        self.assertIn("register_translate_routes(WEB_ROUTER, translate_service)", source)
        self.assertIn("register_postprocess_routes(WEB_ROUTER, postprocess_service)", source)
        self.assertIn("register_video_files_routes(WEB_ROUTER, video_files_service)", source)
        self.assertIn("register_video_result_routes(WEB_ROUTER, video_result_service, safe_filename=safe_filename)", source)
        self.assertIn("register_video_delete_routes(WEB_ROUTER, video_delete_service, safe_filename=safe_filename)", source)
        self.assertIn("register_report_routes(WEB_ROUTER, report_service)", source)
        self.assertNotIn('if parsed.path == "/api/shop-job":', source)
        self.assertNotIn('if parsed.path == "/api/video-metrics-job":', source)
        self.assertNotIn('if parsed.path == "/api/video-metrics-events":', source)
        self.assertNotIn('if parsed.path == "/api/video-metrics":', source)
        self.assertNotIn("def stream_metrics_events(", source)
        self.assertNotIn("def handle_video_metrics(", source)
        self.assertNotIn('if parsed.path == "/api/amazon-job":', source)
        self.assertNotIn('if parsed.path == "/api/amazon-events":', source)
        self.assertNotIn('if parsed.path == "/api/amazon-scrape":', source)
        self.assertNotIn("def stream_amazon_events(", source)
        self.assertNotIn("def handle_amazon_scrape(", source)
        self.assertNotIn('if parsed.path == "/api/download-job":', source)
        self.assertNotIn('if parsed.path == "/api/download-events":', source)
        self.assertNotIn('if parsed.path == "/api/download":', source)
        self.assertNotIn('if parsed.path == "/api/result":', source)
        self.assertNotIn("def stream_download_events(", source)
        self.assertNotIn("def handle_download(", source)
        self.assertNotIn('if parsed.path == "/api/analyze":', source)
        self.assertNotIn('if parsed.path == "/api/translate":', source)
        self.assertNotIn('if parsed.path == "/api/postprocess":', source)
        self.assertNotIn('if parsed.path == "/api/files":', source)
        self.assertNotIn('if parsed.path == "/api/delete":', source)
        self.assertNotIn("def handle_delete(", source)
        for path in ("/api/report/today", "/api/report", "/api/report/history", "/api/report/settings", "/api/report/events", "/api/report/run", "/api/report/delete", "/api/report/translate", "/api/report/backfill-covers"):
            self.assertNotIn(f'if parsed.path == "{path}"', source)
        for name in ("stream_report_events", "handle_report_run", "handle_report_delete", "handle_report_settings", "handle_report_translate"):
            self.assertNotIn(f"def {name}(", source)

    def test_report_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        route_path = root / "routes" / "report.py"
        service_path = root / "services" / "report.py"
        route_tree = ast.parse(route_path.read_text(encoding="utf-8"))
        registrations = [
            (node.func.attr, node.args[0].value)
            for node in ast.walk(route_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "router"
            and node.func.attr in {"get", "post"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        self.assertEqual(registrations, [
            ("get", "/api/report/today"), ("get", "/api/report"), ("get", "/api/report/history"),
            ("get", "/api/report/settings"), ("get", "/api/report/events"), ("post", "/api/report/run"),
            ("post", "/api/report/delete"), ("post", "/api/report/settings"),
            ("post", "/api/report/translate"), ("post", "/api/report/backfill-covers"),
        ])
        route_source = route_path.read_text(encoding="utf-8")
        self.assertNotIn("web_app", route_source)
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(service_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("hot_video_report", imports)
        self.assertNotIn("web_app", imports)
        self.assertFalse(any(module.startswith("routes") for module in imports))

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.report", "register_report_routes"), imported)
        self.assertIn(("services.report", "ReportService"), imported)
        service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ReportService"
        ]
        self.assertEqual(len(service_calls), 1)
        self.assertEqual(
            [(keyword.arg, ast.unparse(keyword.value)) for keyword in service_calls[0].keywords],
            [
                ("is_enabled", "hot_report_enabled"), ("get_report", "get_report"), ("list_reports", "list_reports"),
                ("get_settings", "get_report_settings"), ("get_runtime_status", "get_report_runtime_status"),
                ("get_progress", "get_report_progress"), ("recover", "recover_interrupted_reports"),
                ("enqueue", "enqueue_report"), ("delete", "delete_report"), ("save", "save_report_settings"),
                ("translate", "translate_report_video_analysis"), ("backfill", "backfill_cover_urls"),
            ],
        )
        registrations = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "register_report_routes"
        ]
        self.assertEqual(len(registrations), 1)
        self.assertEqual([ast.unparse(arg) for arg in registrations[0].args], ["WEB_ROUTER", "report_service"])
        self.assertEqual(registrations[0].keywords, [])
        service_class = next(
            node for node in service_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ReportService"
        )
        methods = {
            node.name for node in service_class.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertTrue({"today", "dated_report", "run"}.issubset(methods))
        today_method = next(node for node in service_class.body if isinstance(node, ast.FunctionDef) and node.name == "today")
        self.assertFalse(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_get_report"
            and bool(node.args)
            for node in ast.walk(today_method)
        ))
        route_run = next(node for node in route_tree.body if isinstance(node, ast.FunctionDef) and node.name == "register_report_routes")
        self.assertIn("ReportDisabledError", ast.unparse(route_run))
        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        post_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        self.assertNotIn(
            "/api/report/run",
            {node.value for node in ast.walk(post_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        registered_post = next(node for node in web_app.body if isinstance(node, ast.FunctionDef) and node.name == "is_registered_post_route")
        for path in ("/api/report/run", "/api/report/delete", "/api/report/settings", "/api/report/translate", "/api/report/backfill-covers"):
            self.assertNotIn(path, {node.value for node in ast.walk(registered_post) if isinstance(node, ast.Constant) and isinstance(node.value, str)})

    def test_video_stream_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        route_path = root / "routes" / "video_stream.py"
        route_tree = ast.parse(route_path.read_text(encoding="utf-8"))
        prefix_paths = [
            node.args[0].value
            for node in ast.walk(route_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "router"
            and node.func.attr == "get_prefix"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(prefix_paths, ["/video/"])
        route_source = route_path.read_text(encoding="utf-8")
        for forbidden in (
            "web_app",
            "services",
            "file_response",
            "binary_response",
            "output",
            "registry",
            "queue",
            "social",
            "tools",
        ):
            self.assertNotIn(forbidden, route_source)

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.video_stream", "register_video_stream_routes"), imported)
        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_video_stream_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(
            [arg.id for arg in register_calls[0].args if isinstance(arg, ast.Name)],
            ["WEB_ROUTER"],
        )
        self.assertEqual(
            [(keyword.arg, ast.unparse(keyword.value)) for keyword in register_calls[0].keywords],
            [
                ("videos_dir", "VIDEOS_DIR"),
                ("safe_filename", "safe_filename"),
                ("serve_video", "Handler.serve_video"),
            ],
        )

        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        get_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_GET")
        self.assertNotIn(
            "/video/",
            {node.value for node in ast.walk(get_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        self.assertEqual(
            sum(isinstance(node, ast.FunctionDef) and node.name == "serve_video" for node in handler.body),
            1,
        )
        proxy_get_method = next(
            node for node in handler.body
            if isinstance(node, ast.FunctionDef) and node.name == "handle_proxy_api_get"
        )
        proxy_calls = [
            node for node in ast.walk(proxy_get_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "serve_video"
        ]
        self.assertEqual(len(proxy_calls), 1)
        self.assertEqual(
            ast.unparse(proxy_calls[0]),
            "self.serve_video(tiktok_studio_publish.video_path(asset_id))",
        )

    def test_upload_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        self.assertEqual(MAX_UPLOAD_BYTES, 2 * 1024 * 1024 * 1024)
        upload_route = ast.parse((root / "routes" / "upload.py").read_text(encoding="utf-8"))
        post_paths = [
            node.args[0].value
            for node in ast.walk(upload_route)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "post"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(post_paths, ["/api/upload"])

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.upload", "register_upload_routes"), imported)
        self.assertIn(("services.upload", "UploadService"), imported)

        upload_service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "UploadService"
        ]
        self.assertEqual(len(upload_service_calls), 1)
        upload_keywords = {keyword.arg: keyword.value for keyword in upload_service_calls[0].keywords}
        expected_upload_injection = {
            "videos_dir": "VIDEOS_DIR",
            "safe_filename": "safe_filename",
            "ensure_analyzer_media_or_delete": "ensure_analyzer_media_or_delete",
            "register_video": "register_video",
            "video_source_hidden": "video_source_hidden",
            "make_web_manual_visible": "make_web_manual_visible",
            "start_social_context_job": "start_social_context_job",
        }
        self.assertEqual(
            {name: ast.unparse(upload_keywords[name]) for name in upload_keywords},
            expected_upload_injection,
        )

        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_upload_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        register_call = register_calls[0]
        self.assertEqual([arg.id for arg in register_call.args if isinstance(arg, ast.Name)], ["WEB_ROUTER", "upload_service"])
        self.assertEqual(
            [(keyword.arg, keyword.value.id if isinstance(keyword.value, ast.Name) else None) for keyword in register_call.keywords],
            [("normalize_video_source", "normalize_video_source"), ("default_source", "SOURCE_API_UPLOAD")],
        )

        self.assertFalse(
            any(
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == "MAX_UPLOAD_BYTES"
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                )
                for node in ast.walk(web_app)
            )
        )
        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "handle_upload" for node in handler.body))
        post_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        self.assertNotIn(
            "/api/upload",
            {node.value for node in ast.walk(post_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        registered_post = next(
            node for node in web_app.body
            if isinstance(node, ast.FunctionDef) and node.name == "is_registered_post_route"
        )
        self.assertNotIn(
            "/api/upload",
            {node.value for node in ast.walk(registered_post) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )

    def test_analyze_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        analyze_route = ast.parse((root / "routes" / "analyze.py").read_text(encoding="utf-8"))
        post_paths = [
            node.args[0].value
            for node in ast.walk(analyze_route)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "post"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(post_paths, ["/api/analyze"])

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.analyze", "register_analyze_routes"), imported)
        self.assertIn(("services.analyze", "AnalyzeService"), imported)

        service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AnalyzeService"
        ]
        self.assertEqual(len(service_calls), 1)
        service_keywords = {keyword.arg: keyword.value for keyword in service_calls[0].keywords}
        self.assertEqual(
            {name: ast.unparse(service_keywords[name]) for name in service_keywords},
            {
                "videos_dir": "VIDEOS_DIR",
                "output_dir_for_filename": "output_dir_for_filename",
                "safe_filename": "safe_filename",
                "queue_enqueue": "video_queue.enqueue",
            },
        )

        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_analyze_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(
            [arg.id for arg in register_calls[0].args if isinstance(arg, ast.Name)],
            ["WEB_ROUTER", "analyze_service"],
        )
        self.assertEqual(register_calls[0].keywords, [])

        route_register = next(
            node for node in analyze_route.body
            if isinstance(node, ast.FunctionDef) and node.name == "register_analyze_routes"
        )
        defaults = dict(
            zip(
                (argument.arg for argument in route_register.args.kwonlyargs),
                route_register.args.kw_defaults,
            )
        )
        getenv_default = defaults["getenv"]
        self.assertIsInstance(getenv_default, ast.Attribute)
        if isinstance(getenv_default, ast.Attribute):
            self.assertEqual(ast.unparse(getenv_default), "os.getenv")

        service_tree = ast.parse((root / "services" / "analyze.py").read_text(encoding="utf-8"))
        for module_tree in (analyze_route, service_tree):
            for node in ast.walk(module_tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name == "web_app" or alias.name.startswith("routes") for alias in node.names))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotEqual(node.module, "web_app")
                    if module_tree is service_tree:
                        self.assertFalse(node.module == "routes" or node.module.startswith("routes."))

        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "handle_analyze" for node in handler.body))
        post_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        self.assertNotIn(
            "/api/analyze",
            {node.value for node in ast.walk(post_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        registered_post = next(
            node for node in web_app.body
            if isinstance(node, ast.FunctionDef) and node.name == "is_registered_post_route"
        )
        self.assertNotIn(
            "/api/analyze",
            {node.value for node in ast.walk(registered_post) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )

    def test_translate_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        translate_route = ast.parse((root / "routes" / "translate.py").read_text(encoding="utf-8"))
        post_paths = [
            node.args[0].value
            for node in ast.walk(translate_route)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "post"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(post_paths, ["/api/translate"])

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.translate", "register_translate_routes"), imported)
        self.assertIn(("services.translate", "TranslateService"), imported)

        service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "TranslateService"
        ]
        self.assertEqual(len(service_calls), 1)
        service_keywords = {keyword.arg: keyword.value for keyword in service_calls[0].keywords}
        self.assertEqual(
            {name: ast.unparse(service_keywords[name]) for name in service_keywords},
            {
                "root": "ROOT",
                "scripts_dir": "SCRIPTS_DIR",
                "output_dir_for_filename": "output_dir_for_filename",
                "safe_filename": "safe_filename",
                "run_factory": "subprocess.run",
                "environ": "os.environ",
            },
        )

        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_translate_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(
            [arg.id for arg in register_calls[0].args if isinstance(arg, ast.Name)],
            ["WEB_ROUTER", "translate_service"],
        )
        self.assertEqual(register_calls[0].keywords, [])

        service_path = root / "services" / "translate.py"
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"))
        translate_service = next(
            node for node in service_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "TranslateService"
        )
        constructor = next(
            node for node in translate_service.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        self.assertEqual(
            [argument.arg for argument in constructor.args.args],
            ["self", "root", "scripts_dir", "output_dir_for_filename", "safe_filename", "run_factory", "environ"],
        )
        service_source = service_path.read_text(encoding="utf-8")
        for forbidden in ("video_queue", "postprocess", "threading", "JobRegistry", "run_job", "create_and_start"):
            self.assertNotIn(forbidden, service_source)

        for module_tree in (translate_route, service_tree):
            for node in ast.walk(module_tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name == "web_app" or alias.name.startswith("routes") for alias in node.names))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotEqual(node.module, "web_app")
                    if module_tree is service_tree:
                        self.assertFalse(node.module == "routes" or node.module.startswith("routes."))

        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "handle_translate" for node in handler.body))
        post_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        self.assertNotIn(
            "/api/translate",
            {node.value for node in ast.walk(post_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        registered_post = next(
            node for node in web_app.body
            if isinstance(node, ast.FunctionDef) and node.name == "is_registered_post_route"
        )
        self.assertNotIn(
            "/api/translate",
            {node.value for node in ast.walk(registered_post) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )

    def test_postprocess_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        postprocess_route = ast.parse((root / "routes" / "postprocess.py").read_text(encoding="utf-8"))
        post_paths = [
            node.args[0].value
            for node in ast.walk(postprocess_route)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "post"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(post_paths, ["/api/postprocess"])

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.postprocess", "register_postprocess_routes"), imported)
        self.assertIn(("services.postprocess", "PostprocessService"), imported)

        service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "PostprocessService"
        ]
        self.assertEqual(len(service_calls), 1)
        service_keywords = {keyword.arg: keyword.value for keyword in service_calls[0].keywords}
        self.assertEqual(
            {name: ast.unparse(service_keywords[name]) for name in service_keywords},
            {
                "output_dir_for_filename": "output_dir_for_filename",
                "safe_filename": "safe_filename",
                "queue_enqueue": "video_queue.enqueue",
            },
        )

        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_postprocess_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(
            [arg.id for arg in register_calls[0].args if isinstance(arg, ast.Name)],
            ["WEB_ROUTER", "postprocess_service"],
        )
        self.assertEqual(register_calls[0].keywords, [])

        service_path = root / "services" / "postprocess.py"
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"))
        postprocess_service = next(
            node for node in service_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "PostprocessService"
        )
        constructor = next(
            node for node in postprocess_service.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        self.assertEqual(
            [argument.arg for argument in constructor.args.args],
            ["self", "output_dir_for_filename", "safe_filename", "queue_enqueue"],
        )
        service_imports = {
            node.module
            for node in ast.walk(service_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        service_imports.update(
            alias.name
            for node in ast.walk(service_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertEqual(service_imports, {"__future__", "dataclasses", "pathlib", "typing"})
        service_source = service_path.read_text(encoding="utf-8")
        for forbidden in ("video_queue", "subprocess", "JobRegistry", "AnalyzeService", "TranslateService", "run_job", "create_and_start"):
            self.assertNotIn(forbidden, service_source)

        for module_tree in (postprocess_route, service_tree):
            for node in ast.walk(module_tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name == "web_app" or alias.name.startswith("routes") for alias in node.names))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotEqual(node.module, "web_app")
                    if module_tree is service_tree:
                        self.assertFalse(node.module == "routes" or node.module.startswith("routes."))

        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "handle_postprocess" for node in handler.body))
        post_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        self.assertNotIn(
            "/api/postprocess",
            {node.value for node in ast.walk(post_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        registered_post = next(
            node for node in web_app.body
            if isinstance(node, ast.FunctionDef) and node.name == "is_registered_post_route"
        )
        self.assertNotIn(
            "/api/postprocess",
            {node.value for node in ast.walk(registered_post) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )

    def test_video_result_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        route_path = root / "routes" / "video_result.py"
        route_tree = ast.parse(route_path.read_text(encoding="utf-8"))
        get_paths = [
            node.args[0].value
            for node in ast.walk(route_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "router"
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(get_paths, ["/api/result"])

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.video_result", "register_video_result_routes"), imported)
        self.assertIn(("services.video_result", "VideoResultService"), imported)

        service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "VideoResultService"
        ]
        self.assertEqual(len(service_calls), 1)
        service_keywords = {keyword.arg: keyword.value for keyword in service_calls[0].keywords}
        self.assertEqual(
            {name: ast.unparse(service_keywords[name]) for name in service_keywords},
            {
                "root": "ROOT",
                "output_dir_for_filename": "output_dir_for_filename",
                "read_json_file": "read_json",
            },
        )
        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_video_result_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(
            [arg.id for arg in register_calls[0].args if isinstance(arg, ast.Name)],
            ["WEB_ROUTER", "video_result_service"],
        )
        self.assertEqual(
            [(keyword.arg, ast.unparse(keyword.value)) for keyword in register_calls[0].keywords],
            [("safe_filename", "safe_filename")],
        )

        service_path = root / "services" / "video_result.py"
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"))
        service = next(
            node for node in service_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "VideoResultService"
        )
        constructor = next(
            node for node in service.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        self.assertEqual(
            [argument.arg for argument in constructor.args.args],
            ["self", "root", "output_dir_for_filename", "read_json_file"],
        )
        service_imports = {
            node.module
            for node in ast.walk(service_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        service_imports.update(
            alias.name
            for node in ast.walk(service_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertEqual(service_imports, {"__future__", "pathlib", "typing"})

        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        get_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_GET")
        self.assertNotIn(
            "/api/result",
            {node.value for node in ast.walk(get_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        self.assertNotIn("def mode_from_analysis(", (root / "web_app.py").read_text(encoding="utf-8"))

    def test_video_delete_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        route_path = root / "routes" / "video_delete.py"
        route_tree = ast.parse(route_path.read_text(encoding="utf-8"))
        post_paths = [
            node.args[0].value
            for node in ast.walk(route_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "router"
            and node.func.attr == "post"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(post_paths, ["/api/delete"])

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.video_delete", "register_video_delete_routes"), imported)
        self.assertIn(("services.video_delete", "VideoDeleteService"), imported)

        service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "VideoDeleteService"
        ]
        self.assertEqual(len(service_calls), 1)
        service_keywords = {keyword.arg: keyword.value for keyword in service_calls[0].keywords}
        self.assertEqual(
            {name: ast.unparse(service_keywords[name]) for name in service_keywords},
            {"videos_dir": "VIDEOS_DIR", "output_dir": "OUTPUT_DIR", "rmtree": "shutil.rmtree"},
        )
        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_video_delete_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(
            [arg.id for arg in register_calls[0].args if isinstance(arg, ast.Name)],
            ["WEB_ROUTER", "video_delete_service"],
        )
        self.assertEqual(
            [(keyword.arg, ast.unparse(keyword.value)) for keyword in register_calls[0].keywords],
            [("safe_filename", "safe_filename")],
        )

        service_path = root / "services" / "video_delete.py"
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"))
        service = next(
            node for node in service_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "VideoDeleteService"
        )
        constructor = next(
            node for node in service.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        self.assertEqual(
            [argument.arg for argument in constructor.args.args],
            ["self", "videos_dir", "output_dir", "rmtree"],
        )
        service_source = service_path.read_text(encoding="utf-8")
        for forbidden in (
            "output_dir_for_filename",
            "video_queue",
            "get_video_by_filename",
            "register_video",
            "start_social_context_job",
            "video_files",
            "video_result",
            "range",
        ):
            self.assertNotIn(forbidden, service_source)
        service_imports = {
            node.module
            for node in ast.walk(service_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        service_imports.update(
            alias.name
            for node in ast.walk(service_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertEqual(service_imports, {"__future__", "pathlib", "typing"})
        for module_tree in (route_tree, service_tree):
            for node in ast.walk(module_tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name == "web_app" or alias.name.startswith("routes") for alias in node.names))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotEqual(node.module, "web_app")
                    if module_tree is service_tree:
                        self.assertFalse(node.module == "routes" or node.module.startswith("routes."))

        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "handle_delete" for node in handler.body))
        post_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        self.assertNotIn(
            "/api/delete",
            {node.value for node in ast.walk(post_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )
        registered_post = next(
            node for node in web_app.body
            if isinstance(node, ast.FunctionDef) and node.name == "is_registered_post_route"
        )
        self.assertNotIn(
            "/api/delete",
            {node.value for node in ast.walk(registered_post) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )

    def test_video_files_route_and_composition_are_explicit(self) -> None:
        root = Path(__file__).resolve().parent
        route_path = root / "routes" / "video_files.py"
        route_tree = ast.parse(route_path.read_text(encoding="utf-8"))
        get_paths = [
            node.args[0].value
            for node in ast.walk(route_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        self.assertEqual(get_paths, ["/api/files"])

        web_app = ast.parse((root / "web_app.py").read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(web_app)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("routes.video_files", "register_video_files_routes"), imported)
        self.assertIn(("services.video_files", "VideoFilesService"), imported)

        service_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "VideoFilesService"
        ]
        self.assertEqual(len(service_calls), 1)
        service_keywords = {keyword.arg: keyword.value for keyword in service_calls[0].keywords}
        self.assertEqual(
            {name: ast.unparse(service_keywords[name]) for name in service_keywords},
            {
                "videos_dir": "VIDEOS_DIR",
                "suffixes": "ANALYZER_VIDEO_SUFFIXES",
                "media_validator": "analyzer_media_is_valid",
                "analyzer_visible_source": "analyzer_visible_source",
                "queue_status": "video_queue.get_status",
                "queue_status_meta": "video_queue.get_status_meta",
                "queue_title": "video_queue.get_title",
                "output_dir_for_filename": "output_dir_for_filename",
                "read_json_file": "read_json",
                "social_summary": "summarize_social_status",
            },
        )

        register_calls = [
            node for node in ast.walk(web_app)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_video_files_routes"
        ]
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(
            [arg.id for arg in register_calls[0].args if isinstance(arg, ast.Name)],
            ["WEB_ROUTER", "video_files_service"],
        )
        self.assertEqual(register_calls[0].keywords, [])

        service_path = root / "services" / "video_files.py"
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"))
        service = next(
            node for node in service_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "VideoFilesService"
        )
        constructor = next(
            node for node in service.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        self.assertEqual(
            [argument.arg for argument in constructor.args.args],
            [
                "self",
                "videos_dir",
                "suffixes",
                "media_validator",
                "analyzer_visible_source",
                "queue_status",
                "queue_status_meta",
                "queue_title",
                "output_dir_for_filename",
                "read_json_file",
                "social_summary",
            ],
        )
        service_imports = {
            node.module
            for node in ast.walk(service_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        service_imports.update(
            alias.name
            for node in ast.walk(service_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertEqual(service_imports, {"__future__", "pathlib", "typing"})
        service_source = service_path.read_text(encoding="utf-8")
        for forbidden in (
            "video_queue",
            "subprocess",
            "JobRegistry",
            "AnalyzeService",
            "TranslateService",
            "PostprocessService",
            "run_job",
            "create_and_start",
        ):
            self.assertNotIn(forbidden, service_source)

        for module_tree in (route_tree, service_tree):
            for node in ast.walk(module_tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name == "web_app" or alias.name.startswith("routes") for alias in node.names))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotEqual(node.module, "web_app")
                    if module_tree is service_tree:
                        self.assertFalse(node.module == "routes" or node.module.startswith("routes."))

        handler = next(node for node in web_app.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        get_method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_GET")
        self.assertNotIn(
            "/api/files",
            {node.value for node in ast.walk(get_method) if isinstance(node, ast.Constant) and isinstance(node.value, str)},
        )


if __name__ == "__main__":
    unittest.main()
