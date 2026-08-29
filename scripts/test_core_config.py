"""Regression coverage for the isolated import-time web configuration."""

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from core.config import AppConfig  # noqa: E402


class AppConfigTests(unittest.TestCase):
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
