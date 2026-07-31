"""Load the pinned official SellerSprite Skills for the isolated chat path."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable


OFFICIAL_SELLERSPRITE_SKILL_VERSION = "0.1.17"
OFFICIAL_SELLERSPRITE_SKILL_COMMIT = "afea6ad232b3bcae38704b1e5a5953f82492bdf1"
OFFICIAL_SELLERSPRITE_SKILL_URL = (
    "https://codeload.github.com/opensellersprite/sellersprite-cli/tar.gz/"
    + OFFICIAL_SELLERSPRITE_SKILL_COMMIT
)
OFFICIAL_SELLERSPRITE_SKILL_SHA256 = (
    "05851877c80115fd9c2f91558b19b6622ab40523e2bc822c04fb6e6c3decb30d"
)
OFFICIAL_SELLERSPRITE_SKILL_ROOT = (
    f"sellersprite-cli-{OFFICIAL_SELLERSPRITE_SKILL_COMMIT}/"
    "src/sellersprite_cli/skills/"
)
OFFICIAL_SELLERSPRITE_PROMPT_FILES = (
    "SKILL.md",
    "README.md",
    "agent-instructions.md",
    "comprehensive/ad-optimizer.md",
    "comprehensive/competitor-analysis.md",
    "comprehensive/keyword-research.md",
    "comprehensive/listing-optimizer.md",
    "comprehensive/market-analysis.md",
    "comprehensive/opportunity-finder.md",
    "comprehensive/pricing-strategy.md",
    "comprehensive/product-research.md",
    "comprehensive/review-insights.md",
    "comprehensive/traffic-analysis.md",
    "tactical/aba-high-growth-trend.md",
    "tactical/fbm-intercept.md",
    "tactical/hidden-bestseller.md",
    "tactical/high-margin-lightweight.md",
    "tactical/high-new-product-ratio.md",
    "tactical/high-ticket-long-tail.md",
    "tactical/hot-low-rating.md",
    "tactical/local-premium-disruption.md",
    "tactical/low-brand-monopoly.md",
    "tactical/low-monopoly-keyword.md",
    "tactical/natural-traffic-audit.md",
    "tactical/new-product-burst.md",
    "tactical/poor-listing-winner.md",
    "tactical/review-sentiment.md",
    "tactical/seasonal-prepositioning.md",
    "tactical/title-density-gap.md",
    "tactical/variant-gap-analysis.md",
)

_LOAD_LOCK = threading.Lock()
_PROMPT_CACHE: dict[str, str] = {}


def official_sellersprite_skill_enabled() -> bool:
    return str(os.getenv("SELLERSPRITE_OFFICIAL_SKILL_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _skill_cache_dir() -> Path:
    configured = str(os.getenv("SELLERSPRITE_OFFICIAL_SKILL_CACHE_DIR", "")).strip()
    if configured:
        return Path(configured)
    return Path.cwd() / "data" / "sellersprite_official_skill"


def _verify_archive(
    payload: bytes,
    expected_sha256: str = OFFICIAL_SELLERSPRITE_SKILL_SHA256,
) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            "SellerSprite official Skills integrity verification failed: "
            f"expected sha256-{expected_sha256}, got sha256-{actual}"
        )


def _download_archive(
    url: str = OFFICIAL_SELLERSPRITE_SKILL_URL,
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
    proxy = str(os.getenv("SELLERSPRITE_OFFICIAL_SKILL_PROXY", "")).strip()
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    request_opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Video-analyzer SellerSprite Skills loader"},
    )
    with request_opener.open(request, timeout=timeout) as response:
        return response.read()


def _read_prompt_files_from_archive(payload: bytes) -> dict[str, str]:
    expected = set(OFFICIAL_SELLERSPRITE_PROMPT_FILES)
    found: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(
                    f"Unsafe path in SellerSprite official Skills archive: {member.name}"
                )
            if not member.name.startswith(OFFICIAL_SELLERSPRITE_SKILL_ROOT):
                continue
            relative_name = member.name[len(OFFICIAL_SELLERSPRITE_SKILL_ROOT):]
            if relative_name not in expected:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(
                    f"Could not read SellerSprite official Skills file: {relative_name}"
                )
            found[relative_name] = extracted.read().decode("utf-8")
    missing = sorted(expected - set(found))
    if missing:
        raise RuntimeError(
            "SellerSprite official Skills package is missing required files: "
            + ", ".join(missing)
        )
    return found


def _read_cached_prompt_files(
    version_dir: Path,
    expected_sha256: str,
) -> dict[str, str] | None:
    metadata_path = version_dir / "metadata.json"
    if not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        metadata.get("version") != OFFICIAL_SELLERSPRITE_SKILL_VERSION
        or metadata.get("commit") != OFFICIAL_SELLERSPRITE_SKILL_COMMIT
        or metadata.get("sha256") != expected_sha256
    ):
        return None
    found: dict[str, str] = {}
    for relative_name in OFFICIAL_SELLERSPRITE_PROMPT_FILES:
        path = version_dir / Path(relative_name)
        if not path.is_file():
            return None
        found[relative_name] = path.read_text(encoding="utf-8")
    return found


def _write_cached_prompt_files(
    version_dir: Path,
    files: dict[str, str],
    archive_sha256: str,
) -> None:
    for relative_name, content in files.items():
        path = version_dir / Path(relative_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    metadata = {
        "package": "opensellersprite/sellersprite-cli",
        "version": OFFICIAL_SELLERSPRITE_SKILL_VERSION,
        "commit": OFFICIAL_SELLERSPRITE_SKILL_COMMIT,
        "url": OFFICIAL_SELLERSPRITE_SKILL_URL,
        "sha256": archive_sha256,
        "files": list(OFFICIAL_SELLERSPRITE_PROMPT_FILES),
    }
    (version_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _compose_prompt(files: dict[str, str]) -> str:
    parts = [
        "以下内容来自 SellerSprite CLI 官方 Skills，"
        f"固定版本为 {OFFICIAL_SELLERSPRITE_SKILL_VERSION}，"
        f"提交为 {OFFICIAL_SELLERSPRITE_SKILL_COMMIT}。"
    ]
    for relative_name in OFFICIAL_SELLERSPRITE_PROMPT_FILES:
        parts.append(f"\n\n## 官方文件：{relative_name}\n\n{files[relative_name].strip()}")
    return "".join(parts)


def load_official_sellersprite_skill_prompt(
    *,
    cache_dir: Path | None = None,
    archive_payload: bytes | None = None,
    expected_sha256: str = OFFICIAL_SELLERSPRITE_SKILL_SHA256,
) -> str:
    """Return the exact official Skill Markdown as one pinned prompt."""
    root = cache_dir or _skill_cache_dir()
    version_dir = root / OFFICIAL_SELLERSPRITE_SKILL_COMMIT
    cache_key = f"{version_dir.resolve()}:{expected_sha256}"
    with _LOAD_LOCK:
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]
        files = _read_cached_prompt_files(version_dir, expected_sha256)
        if files is None:
            payload = archive_payload if archive_payload is not None else _download_archive()
            _verify_archive(payload, expected_sha256)
            files = _read_prompt_files_from_archive(payload)
            _write_cached_prompt_files(version_dir, files, expected_sha256)
        prompt = _compose_prompt(files)
        _PROMPT_CACHE[cache_key] = prompt
        return prompt


def select_official_sellersprite_skill_prompt(
    prompt: str,
    relative_name: str,
) -> str:
    """Return the pinned provenance header plus one exact official Skill document."""
    if relative_name not in OFFICIAL_SELLERSPRITE_PROMPT_FILES:
        raise ValueError(f"Unknown official SellerSprite Skill file: {relative_name}")
    section_marker = f"\n\n## 官方文件：{relative_name}\n\n"
    if section_marker not in prompt:
        raise RuntimeError(
            f"Official SellerSprite Skill prompt is missing file: {relative_name}"
        )
    header = prompt.split("\n\n## 官方文件：", 1)[0].strip()
    document = prompt.split(section_marker, 1)[1].split(
        "\n\n## 官方文件：", 1
    )[0].strip()
    return f"{header}{section_marker}{document}"


def clear_official_sellersprite_skill_memory_cache() -> None:
    """Test helper; disk cache is intentionally preserved."""
    with _LOAD_LOCK:
        _PROMPT_CACHE.clear()
