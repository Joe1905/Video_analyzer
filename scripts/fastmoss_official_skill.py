"""Load the pinned official FastMoss Agent Skill for the experimental chat path."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
import threading
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable


OFFICIAL_SKILL_VERSION = "0.1.17"
OFFICIAL_SKILL_URL = (
    "https://registry.npmjs.org/@fastmoss/skill/-/skill-0.1.17.tgz"
)
OFFICIAL_SKILL_SHA512 = (
    "PLLeTaeIAfad4CXrU+gvilDthjFvYrdPIFYOUPXFo0DrGnp1EKoeLwsRjcMO/"
    "l8Xz6zRZ7rctqI3QLAEVZt+Mg=="
)
OFFICIAL_SKILL_ROOT = "package/skills/fastmoss-cli/"
OFFICIAL_PROMPT_FILES = (
    "SKILL.md",
    "references/tool-call.md",
    "references/tools.md",
    "references/tools-advertising.md",
    "references/tools-agency.md",
    "references/tools-auxiliary-knowledge.md",
    "references/tools-creator.md",
    "references/tools-market.md",
    "references/tools-product.md",
    "references/tools-shop.md",
)

_LOAD_LOCK = threading.Lock()
_PROMPT_CACHE: dict[str, str] = {}


def official_fastmoss_skill_enabled() -> bool:
    return str(os.getenv("FASTMOSS_OFFICIAL_SKILL_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _skill_cache_dir() -> Path:
    configured = str(os.getenv("FASTMOSS_OFFICIAL_SKILL_CACHE_DIR", "")).strip()
    if configured:
        return Path(configured)
    return Path.cwd() / "data" / "fastmoss_official_skill"


def _verify_archive(payload: bytes, expected_sha512: str = OFFICIAL_SKILL_SHA512) -> None:
    actual = base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")
    if actual != expected_sha512:
        raise RuntimeError(
            "FastMoss official Skill integrity verification failed: "
            f"expected sha512-{expected_sha512}, got sha512-{actual}"
        )


def _download_archive(
    url: str = OFFICIAL_SKILL_URL,
    *,
    timeout: float = 20.0,
    opener: Callable[..., object] | None = None,
) -> bytes:
    if opener is not None:
        response = opener(url, timeout=timeout)
        try:
            return response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    proxy = str(os.getenv("FASTMOSS_OFFICIAL_SKILL_PROXY", "")).strip()
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    request_opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Video-analyzer FastMoss Skill loader"},
    )
    with request_opener.open(request, timeout=timeout) as response:
        return response.read()


def _read_prompt_files_from_archive(payload: bytes) -> dict[str, str]:
    expected = set(OFFICIAL_PROMPT_FILES)
    found: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"Unsafe path in FastMoss official Skill archive: {member.name}")
            if not member.name.startswith(OFFICIAL_SKILL_ROOT):
                continue
            relative_name = member.name[len(OFFICIAL_SKILL_ROOT):]
            if relative_name not in expected:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"Could not read FastMoss official Skill file: {relative_name}")
            found[relative_name] = extracted.read().decode("utf-8")
    missing = sorted(expected - set(found))
    if missing:
        raise RuntimeError(
            "FastMoss official Skill package is missing required files: "
            + ", ".join(missing)
        )
    return found


def _read_cached_prompt_files(version_dir: Path) -> dict[str, str] | None:
    metadata_path = version_dir / "metadata.json"
    if not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        metadata.get("version") != OFFICIAL_SKILL_VERSION
        or metadata.get("sha512") != OFFICIAL_SKILL_SHA512
    ):
        return None
    found: dict[str, str] = {}
    for relative_name in OFFICIAL_PROMPT_FILES:
        path = version_dir / Path(relative_name)
        if not path.is_file():
            return None
        found[relative_name] = path.read_text(encoding="utf-8")
    return found


def _write_cached_prompt_files(version_dir: Path, files: dict[str, str]) -> None:
    for relative_name, content in files.items():
        path = version_dir / Path(relative_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    metadata = {
        "package": "@fastmoss/skill",
        "version": OFFICIAL_SKILL_VERSION,
        "url": OFFICIAL_SKILL_URL,
        "sha512": OFFICIAL_SKILL_SHA512,
        "files": list(OFFICIAL_PROMPT_FILES),
    }
    (version_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _compose_prompt(files: dict[str, str]) -> str:
    parts = [
        "以下内容来自 FastMoss 官方 Agent Skill，"
        f"固定版本为 @fastmoss/skill@{OFFICIAL_SKILL_VERSION}。"
    ]
    for relative_name in OFFICIAL_PROMPT_FILES:
        parts.append(f"\n\n## 官方文件：{relative_name}\n\n{files[relative_name].strip()}")
    return "".join(parts)


def load_official_fastmoss_skill_prompt(
    *,
    cache_dir: Path | None = None,
    archive_payload: bytes | None = None,
    expected_sha512: str = OFFICIAL_SKILL_SHA512,
) -> str:
    """Return the exact official instruction/reference Markdown as one prompt."""
    root = cache_dir or _skill_cache_dir()
    version_dir = root / OFFICIAL_SKILL_VERSION
    cache_key = str(version_dir.resolve())
    with _LOAD_LOCK:
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]
        files = _read_cached_prompt_files(version_dir)
        if files is None:
            payload = archive_payload if archive_payload is not None else _download_archive()
            _verify_archive(payload, expected_sha512)
            files = _read_prompt_files_from_archive(payload)
            _write_cached_prompt_files(version_dir, files)
        prompt = _compose_prompt(files)
        _PROMPT_CACHE[cache_key] = prompt
        return prompt


def clear_official_fastmoss_skill_memory_cache() -> None:
    """Test helper; disk cache is intentionally preserved."""
    with _LOAD_LOCK:
        _PROMPT_CACHE.clear()

