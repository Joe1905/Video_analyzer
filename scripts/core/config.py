"""Immutable import-time configuration for the web application.

This module intentionally models only values currently read while
``web_app.py`` is imported.  Request-scoped settings and credentials stay at
their existing call sites.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AppConfig:
    root: Path
    ui_test_mode: bool
    app_test_root: Path | None
    runtime_root: Path
    data_dir: Path
    videos_dir: Path
    output_dir: Path
    scripts_dir: Path
    video_media_ttl_seconds: int
    social_comment_count: int
    social_api_timeout: float
    chat_image_max_bytes: int
    chat_image_max_count: int
    ocr_api_url: str
    ocr_shared_dir: Path
    ocr_server_shared_dir: str
    feishu_directory_cache_seconds: float
    proxy_pool_enabled: bool
    ui_chat_scroll_test_source_session: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        root: Path | None = None,
    ) -> "AppConfig":
        app_root = root if root is not None else Path.cwd()
        ui_test_mode = env.get("UI_TEST_MODE", "0").strip().lower() in _TRUTHY
        app_test_root_value = env.get("APP_TEST_ROOT", "").strip()
        app_test_root = (
            Path(app_test_root_value).expanduser().resolve()
            if ui_test_mode and app_test_root_value
            else None
        )
        runtime_root = app_test_root or app_root

        return cls(
            root=app_root,
            ui_test_mode=ui_test_mode,
            app_test_root=app_test_root,
            runtime_root=runtime_root,
            data_dir=runtime_root / "data",
            videos_dir=runtime_root / "videos",
            output_dir=runtime_root / "output",
            scripts_dir=app_root / "scripts",
            video_media_ttl_seconds=int(env.get("VIDEO_MEDIA_TTL_SECONDS", "900")),
            social_comment_count=int(env.get("SOCIAL_COMMENT_COUNT", "50")),
            social_api_timeout=float(env.get("SOCIAL_API_TIMEOUT", "45")),
            chat_image_max_bytes=int(env.get("CHAT_IMAGE_MAX_BYTES", "6291456")),
            chat_image_max_count=int(env.get("CHAT_IMAGE_MAX_COUNT", "6")),
            ocr_api_url=env.get("OCR_API_URL", "http://127.0.0.1:4000/v1/ocr/extract"),
            ocr_shared_dir=Path(env.get("OCR_SHARED_DIR", "/home/openclaw/ocr-shared")),
            ocr_server_shared_dir=env.get(
                "OCR_SERVER_SHARED_DIR", "/home/openclaw/ocr-shared"
            ).rstrip("/"),
            feishu_directory_cache_seconds=max(
                1.0, float(env.get("FEISHU_DIRECTORY_CACHE_SECONDS", "60"))
            ),
            proxy_pool_enabled=env.get("PROXY_POOL_ENABLED", "1").strip().lower()
            in _TRUTHY,
            ui_chat_scroll_test_source_session=env.get(
                "CHAT_SCROLL_TEST_SOURCE_SESSION", "B0GVZ3CWK1"
            ).strip()
            or "B0GVZ3CWK1",
        )
