"""Regression coverage for the isolated import-time web configuration."""

import ast
import sys
import unittest
from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from core.config import AppConfig  # noqa: E402


WEB_APP_PATH = SCRIPTS_DIR / "web_app.py"
SHOP_ROUTE_PATH = SCRIPTS_DIR / "routes" / "shop.py"
METRICS_ROUTE_PATH = SCRIPTS_DIR / "routes" / "metrics.py"
METRICS_SERVICE_PATH = SCRIPTS_DIR / "services" / "metrics.py"
_MODULE_CONFIG_BINDINGS = {
    "ROOT": "root",
    "UI_TEST_MODE": "ui_test_mode",
    "APP_TEST_ROOT": "app_test_root",
    "RUNTIME_ROOT": "runtime_root",
    "DATA_DIR": "data_dir",
    "VIDEOS_DIR": "videos_dir",
    "OUTPUT_DIR": "output_dir",
    "SCRIPTS_DIR": "scripts_dir",
    "VIDEO_MEDIA_TTL_SECONDS": "video_media_ttl_seconds",
    "SOCIAL_COMMENT_COUNT": "social_comment_count",
    "SOCIAL_API_TIMEOUT": "social_api_timeout",
    "CHAT_IMAGE_MAX_BYTES": "chat_image_max_bytes",
    "CHAT_IMAGE_MAX_COUNT": "chat_image_max_count",
    "OCR_API_URL": "ocr_api_url",
    "OCR_SHARED_DIR": "ocr_shared_dir",
    "OCR_SERVER_SHARED_DIR": "ocr_server_shared_dir",
    "FEISHU_DIRECTORY_CACHE_SECONDS": "feishu_directory_cache_seconds",
    "PROXY_POOL_ENABLED": "proxy_pool_enabled",
    "UI_CHAT_SCROLL_TEST_SOURCE_SESSION": "ui_chat_scroll_test_source_session",
}
_DYNAMIC_GETENV_KEY_COUNTS = Counter(
    {
        "<dynamic>": 6,
        "AMAZON_MAX_PAGES": 1,
        "ANALYSIS_MODE": 3,
        "API_CACHE_TTL_SECONDS": 1,
        "APP_TEST_PORT_FILE": 1,
        "CHAT_INTENT_ROUTER_CONFIDENCE": 1,
        "CHAT_INTENT_ROUTER_ENABLED": 1,
        "CHAT_INTENT_ROUTER_TIMEOUT_SECONDS": 1,
        "CHAT_TOOL_MOCK_MODE": 1,
        "CHUHAIJIANG_DETAIL_CACHE_TTL_SECONDS": 1,
        "CHUHAIJIANG_MCP_API_KEY": 1,
        "CHUHAIJIANG_MCP_AUDIT_RETENTION": 1,
        "CHUHAIJIANG_MCP_URL": 1,
        "CHUHAIJIANG_QUERY_CACHE_TTL_SECONDS": 1,
        "DEEPSEEK_API_KEY": 3,
        "DEEPSEEK_API_URL": 2,
        "DEEPSEEK_CHAT_MODEL": 2,
        "DEEPSEEK_REPORT_MODEL": 1,
        "DEEPSEEK_V4_PRO_MODEL": 1,
        "DOWNLOAD_COMMAND_TIMEOUT": 1,
        "HOT_VIDEO_REPORT_SCHEDULER_ENABLED": 1,
        "REPORT_BOT_TOKEN": 1,
        "SELLERSPRITE_CACHE_TTL_SECONDS": 1,
        "SELLERSPRITE_MCP_URL": 1,
        "SELLERSPRITE_REDIRECT_PORT": 1,
        "SOCIAVAULT_API_BASE": 4,
        "SOCIAVAULT_API_KEY": 3,
        "SOCIAVAULT_BASE_URL": 1,
        "SOCIAVAULT_MAX_PAGES": 1,
        "SOCIAVAULT_MCP_COMMAND": 1,
        "SOCIAVAULT_REGION": 1,
        "SOCIAVAULT_REVIEW_PAGES": 1,
        "SOCIAVAULT_TIMEOUT": 1,
        "SOCIAVAULT_TOOL_ROUTER_CONFIDENCE": 1,
        "SOCIAVAULT_TOOL_ROUTER_MODE": 1,
        "TIKTOK_MAX_BYTES": 1,
        "TIKTOK_PROXY_URL": 2,
        "WEB_PORT": 2,
    }
)
_SHOP_ROUTE_GETENV_KEY_COUNTS = Counter(
    {
        "SOCIAVAULT_MAX_PAGES": 1,
        "SOCIAVAULT_REGION": 1,
        "SOCIAVAULT_REVIEW_PAGES": 1,
    }
)


def web_app_module_tree() -> ast.Module:
    return ast.parse(WEB_APP_PATH.read_text(encoding="utf-8"), filename=str(WEB_APP_PATH))


def shop_route_module_tree() -> ast.Module:
    return ast.parse(
        SHOP_ROUTE_PATH.read_text(encoding="utf-8"),
        filename=str(SHOP_ROUTE_PATH),
    )


def metrics_module_trees() -> tuple[ast.Module, ast.Module]:
    return (
        ast.parse(METRICS_ROUTE_PATH.read_text(encoding="utf-8"), filename=str(METRICS_ROUTE_PATH)),
        ast.parse(METRICS_SERVICE_PATH.read_text(encoding="utf-8"), filename=str(METRICS_SERVICE_PATH)),
    )


def top_level_assignments(tree: ast.Module) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}
    for statement in tree.body:
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            value = statement.value
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            value = statement.value
            targets = [statement.target]
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = value
    return assignments


def is_os_getenv_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "getenv"
    )


def is_app_config_from_env_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "AppConfig"
        and node.func.attr == "from_env"
    )


def getenv_key(node: ast.Call) -> str:
    if (
        node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return "<dynamic>"


def getenv_calls_by_scope(tree: ast.Module) -> tuple[list[ast.Call], list[ast.Call]]:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    module_calls: list[ast.Call] = []
    function_calls: list[ast.Call] = []
    function_scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    for node in ast.walk(tree):
        if not is_os_getenv_call(node):
            continue
        ancestor = parents.get(node)
        while ancestor is not None and not isinstance(ancestor, function_scopes):
            ancestor = parents.get(ancestor)
        (function_calls if ancestor is not None else module_calls).append(node)
    return module_calls, function_calls


class AppConfigTests(unittest.TestCase):
    def test_phase_1_2c_web_app_uses_one_explicit_app_config_factory(self) -> None:
        tree = web_app_module_tree()
        assignments = top_level_assignments(tree)
        factory_calls = [
            node for node in ast.walk(tree) if is_app_config_from_env_call(node)
        ]

        self.assertEqual(len(factory_calls), 1)
        self.assertIn("APP_CONFIG", assignments)
        self.assertTrue(is_app_config_from_env_call(assignments["APP_CONFIG"]))
        factory = factory_calls[0]
        self.assertEqual(len(factory.args), 1)
        self.assertIsInstance(factory.args[0], ast.Attribute)
        if isinstance(factory.args[0], ast.Attribute):
            self.assertIsInstance(factory.args[0].value, ast.Name)
            if isinstance(factory.args[0].value, ast.Name):
                self.assertEqual(factory.args[0].value.id, "os")
            self.assertEqual(factory.args[0].attr, "environ")
        self.assertEqual([keyword.arg for keyword in factory.keywords], ["root"])
        self.assertIsInstance(factory.keywords[0].value, ast.Name)
        if isinstance(factory.keywords[0].value, ast.Name):
            self.assertEqual(factory.keywords[0].value.id, "_BOOTSTRAP_ROOT")

    def test_phase_1_2c_web_app_module_globals_are_explicit_app_config_fields(self) -> None:
        assignments = top_level_assignments(web_app_module_tree())

        for name, field_name in _MODULE_CONFIG_BINDINGS.items():
            with self.subTest(name=name):
                value = assignments.get(name)
                self.assertIsInstance(value, ast.Attribute)
                if not isinstance(value, ast.Attribute):
                    continue
                self.assertIsInstance(value.value, ast.Name)
                if not isinstance(value.value, ast.Name):
                    continue
                self.assertEqual(value.value.id, "APP_CONFIG")
                self.assertEqual(value.attr, field_name)

    def test_phase_1_2c_only_functions_and_methods_keep_dynamic_getenv_reads(self) -> None:
        module_calls, function_calls = getenv_calls_by_scope(web_app_module_tree())
        shop_tree = shop_route_module_tree()
        shop_module_calls, shop_function_calls = getenv_calls_by_scope(shop_tree)
        shop_getenv_calls = [
            node
            for node in ast.walk(shop_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getenv"
        ]
        shop_parents = {
            child: parent
            for parent in ast.walk(shop_tree)
            for child in ast.iter_child_nodes(parent)
        }
        shop_getenv_scopes: list[str] = []
        for call in shop_getenv_calls:
            ancestor = shop_parents.get(call)
            while ancestor is not None and not isinstance(ancestor, ast.FunctionDef):
                ancestor = shop_parents.get(ancestor)
            shop_getenv_scopes.append(ancestor.name if isinstance(ancestor, ast.FunctionDef) else "")
        register = next(
            node
            for node in shop_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "register_shop_api_routes"
        )
        keyword_defaults = dict(
            zip(
                (argument.arg for argument in register.args.kwonlyargs),
                register.args.kw_defaults,
            )
        )
        getenv_default = keyword_defaults["getenv"]

        self.assertEqual(module_calls, [])
        self.assertEqual(shop_module_calls, [])
        self.assertEqual(shop_function_calls, [])
        for metrics_tree in metrics_module_trees():
            metrics_module_calls, metrics_function_calls = getenv_calls_by_scope(metrics_tree)
            self.assertEqual(metrics_module_calls, [])
            self.assertEqual(metrics_function_calls, [])
            self.assertFalse([
                node
                for node in ast.walk(metrics_tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getenv"
            ])
        self.assertEqual(len(function_calls), 53)
        self.assertEqual(len(shop_getenv_calls), 3)
        self.assertEqual(shop_getenv_scopes, ["shop_extract"] * 3)
        self.assertIsInstance(getenv_default, ast.Attribute)
        if isinstance(getenv_default, ast.Attribute):
            self.assertIsInstance(getenv_default.value, ast.Name)
            if isinstance(getenv_default.value, ast.Name):
                self.assertEqual(getenv_default.value.id, "os")
            self.assertEqual(getenv_default.attr, "getenv")
        self.assertEqual(
            Counter(getenv_key(call) for call in function_calls),
            _DYNAMIC_GETENV_KEY_COUNTS - _SHOP_ROUTE_GETENV_KEY_COUNTS,
        )
        self.assertEqual(
            Counter(getenv_key(call) for call in shop_getenv_calls),
            _SHOP_ROUTE_GETENV_KEY_COUNTS,
        )
        self.assertEqual(
            len(function_calls) + len(shop_getenv_calls),
            sum(_DYNAMIC_GETENV_KEY_COUNTS.values()),
        )

    def test_default_root_uses_current_working_directory(self) -> None:
        config = AppConfig.from_env({})

        self.assertEqual(config.root, Path.cwd())

    def test_defaults_use_supplied_root_and_match_current_values(self) -> None:
        root = Path("C:/workspace/video-analyzer")

        config = AppConfig.from_env({}, root=root)

        self.assertEqual(config.root, root)
        self.assertFalse(config.ui_test_mode)
        self.assertIsNone(config.app_test_root)
        self.assertEqual(config.runtime_root, root)
        self.assertEqual(config.data_dir, root / "data")
        self.assertEqual(config.videos_dir, root / "videos")
        self.assertEqual(config.output_dir, root / "output")
        self.assertEqual(config.scripts_dir, root / "scripts")
        self.assertEqual(config.video_media_ttl_seconds, 900)
        self.assertEqual(config.social_comment_count, 50)
        self.assertEqual(config.social_api_timeout, 45.0)
        self.assertEqual(config.chat_image_max_bytes, 6291456)
        self.assertEqual(config.chat_image_max_count, 6)
        self.assertEqual(config.ocr_api_url, "http://127.0.0.1:4000/v1/ocr/extract")
        self.assertEqual(config.ocr_shared_dir, Path("/home/openclaw/ocr-shared"))
        self.assertEqual(config.ocr_server_shared_dir, "/home/openclaw/ocr-shared")
        self.assertEqual(config.feishu_directory_cache_seconds, 60.0)
        self.assertTrue(config.proxy_pool_enabled)
        self.assertEqual(config.ui_chat_scroll_test_source_session, "B0GVZ3CWK1")
        with self.assertRaises(FrozenInstanceError):
            config.root = Path("C:/other")

    def test_explicit_relative_root_is_not_resolved(self) -> None:
        root = Path("relative-root")

        config = AppConfig.from_env({}, root=root)

        self.assertEqual(config.root, root)
        self.assertEqual(config.runtime_root, root)
        self.assertEqual(config.scripts_dir, Path("relative-root/scripts"))

    def test_two_environment_mappings_remain_independent(self) -> None:
        root = Path("C:/workspace/video-analyzer")
        first = AppConfig.from_env(
            {
                "UI_TEST_MODE": " YES ",
                "APP_TEST_ROOT": " C:/runtime-one ",
                "VIDEO_MEDIA_TTL_SECONDS": "1",
                "PROXY_POOL_ENABLED": "off",
            },
            root=root,
        )
        second = AppConfig.from_env(
            {
                "UI_TEST_MODE": "false",
                "VIDEO_MEDIA_TTL_SECONDS": "2",
                "PROXY_POOL_ENABLED": "on",
            },
            root=root,
        )

        self.assertEqual(first.runtime_root, Path("C:/runtime-one").resolve())
        self.assertEqual(first.data_dir, Path("C:/runtime-one").resolve() / "data")
        self.assertEqual(first.video_media_ttl_seconds, 1)
        self.assertFalse(first.proxy_pool_enabled)
        self.assertEqual(second.runtime_root, root)
        self.assertEqual(second.video_media_ttl_seconds, 2)
        self.assertTrue(second.proxy_pool_enabled)

    def test_read_only_mapping_input_is_not_mutated(self) -> None:
        values = {"VIDEO_MEDIA_TTL_SECONDS": "3"}
        env = MappingProxyType(values)

        config = AppConfig.from_env(env, root=Path("C:/workspace/video-analyzer"))

        self.assertEqual(config.video_media_ttl_seconds, 3)
        self.assertEqual(values, {"VIDEO_MEDIA_TTL_SECONDS": "3"})

    def test_app_test_root_requires_truthy_mode_and_nonempty_value(self) -> None:
        root = Path("C:/workspace/video-analyzer")
        disabled = AppConfig.from_env(
            {"UI_TEST_MODE": "no", "APP_TEST_ROOT": "C:/ignored"}, root=root
        )
        blank = AppConfig.from_env(
            {"UI_TEST_MODE": "true", "APP_TEST_ROOT": "  "}, root=root
        )

        self.assertIsNone(disabled.app_test_root)
        self.assertEqual(disabled.runtime_root, root)
        self.assertIsNone(blank.app_test_root)
        self.assertEqual(blank.runtime_root, root)

    def test_value_parsing_preserves_current_empty_and_normalization_rules(self) -> None:
        config = AppConfig.from_env(
            {
                "SOCIAL_COMMENT_COUNT": "7",
                "SOCIAL_API_TIMEOUT": "2.5",
                "CHAT_IMAGE_MAX_BYTES": "8",
                "CHAT_IMAGE_MAX_COUNT": "9",
                "OCR_API_URL": "",
                "OCR_SHARED_DIR": "relative-share",
                "OCR_SERVER_SHARED_DIR": "shared///",
                "FEISHU_DIRECTORY_CACHE_SECONDS": "0.5",
                "CHAT_SCROLL_TEST_SOURCE_SESSION": "  ",
            },
            root=Path("C:/workspace/video-analyzer"),
        )

        self.assertEqual(config.social_comment_count, 7)
        self.assertEqual(config.social_api_timeout, 2.5)
        self.assertEqual(config.chat_image_max_bytes, 8)
        self.assertEqual(config.chat_image_max_count, 9)
        self.assertEqual(config.ocr_api_url, "")
        self.assertEqual(config.ocr_shared_dir, Path("relative-share"))
        self.assertEqual(config.ocr_server_shared_dir, "shared")
        self.assertEqual(config.feishu_directory_cache_seconds, 1.0)
        self.assertEqual(config.ui_chat_scroll_test_source_session, "B0GVZ3CWK1")

    def test_truthy_synonyms_match_for_both_boolean_settings(self) -> None:
        root = Path("C:/workspace/video-analyzer")
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                config = AppConfig.from_env(
                    {
                        "UI_TEST_MODE": value,
                        "APP_TEST_ROOT": "C:/runtime",
                        "PROXY_POOL_ENABLED": value,
                    },
                    root=root,
                )
                self.assertTrue(config.ui_test_mode)
                self.assertIsNotNone(config.app_test_root)
                self.assertTrue(config.proxy_pool_enabled)

        for value in ("0", "false", "no", "off"):
            with self.subTest(value=value):
                config = AppConfig.from_env(
                    {"UI_TEST_MODE": value, "PROXY_POOL_ENABLED": value}, root=root
                )
                self.assertFalse(config.ui_test_mode)
                self.assertFalse(config.proxy_pool_enabled)

    def test_zero_and_negative_values_are_not_additionally_validated(self) -> None:
        config = AppConfig.from_env(
            {
                "VIDEO_MEDIA_TTL_SECONDS": "0",
                "SOCIAL_COMMENT_COUNT": "-2",
                "SOCIAL_API_TIMEOUT": "-1.25",
                "CHAT_IMAGE_MAX_BYTES": "0",
                "CHAT_IMAGE_MAX_COUNT": "-3",
                "FEISHU_DIRECTORY_CACHE_SECONDS": "-4.5",
            },
            root=Path("C:/workspace/video-analyzer"),
        )

        self.assertEqual(config.video_media_ttl_seconds, 0)
        self.assertEqual(config.social_comment_count, -2)
        self.assertEqual(config.social_api_timeout, -1.25)
        self.assertEqual(config.chat_image_max_bytes, 0)
        self.assertEqual(config.chat_image_max_count, -3)
        self.assertEqual(config.feishu_directory_cache_seconds, 1.0)

    def test_invalid_numeric_values_raise_like_the_current_imports(self) -> None:
        root = Path("C:/workspace/video-analyzer")
        cases = (
            ({"VIDEO_MEDIA_TTL_SECONDS": ""}, ValueError),
            ({"SOCIAL_API_TIMEOUT": "not-a-number"}, ValueError),
            ({"FEISHU_DIRECTORY_CACHE_SECONDS": ""}, ValueError),
        )

        for env, error in cases:
            with self.subTest(env=env):
                with self.assertRaises(error):
                    AppConfig.from_env(env, root=root)


if __name__ == "__main__":
    unittest.main()
