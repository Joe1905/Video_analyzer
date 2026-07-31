"""Load the pinned official FastMoss Agent Skill for the experimental chat path."""
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


OFFICIAL_SKILL_COMMIT = "a0248dbf8bf66ae2f8865d8584552715ee421324"
OFFICIAL_SKILL_URL = (
    "https://codeload.github.com/FastMoss/fastmoss-skills/tar.gz/"
    + OFFICIAL_SKILL_COMMIT
)
OFFICIAL_SKILL_SHA256 = "48b28a724e37bd284a60321c411d95dabb5c7995879532ef49b8afd4dd9c0851"
OFFICIAL_SKILL_ROOT = f"fastmoss-skills-{OFFICIAL_SKILL_COMMIT}/"
OFFICIAL_PROMPT_FILES = (
    "SKILL.md",
    "references/PRINCIPLES.md",
    "references/GLOSSARY.md",
    "references/fm-product-scout.md",
    "references/fm-creator-outreach.md",
    "references/fm-competitor-batch.md",
    "references/fm-store-diagnosis.md",
    "references/fm-video-brief.md",
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


def _verify_archive(payload: bytes, expected_sha256: str = OFFICIAL_SKILL_SHA256) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            "FastMoss official Skill integrity verification failed: "
            f"expected sha256-{expected_sha256}, got sha256-{actual}"
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
        metadata.get("commit") != OFFICIAL_SKILL_COMMIT
        or metadata.get("sha256") != OFFICIAL_SKILL_SHA256
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
        "package": "FastMoss/fastmoss-skills",
        "commit": OFFICIAL_SKILL_COMMIT,
        "url": OFFICIAL_SKILL_URL,
        "sha256": OFFICIAL_SKILL_SHA256,
        "files": list(OFFICIAL_PROMPT_FILES),
    }
    (version_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _compose_prompt(files: dict[str, str]) -> str:
    parts = [
        "以下内容来自 FastMoss 官方 Agent Skill，"
        "固定来源为 FastMoss/fastmoss-skills，"
        f"提交为 {OFFICIAL_SKILL_COMMIT}。"
    ]
    for relative_name in OFFICIAL_PROMPT_FILES:
        parts.append(f"\n\n## 官方文件：{relative_name}\n\n{files[relative_name].strip()}")
    return "".join(parts)


def load_official_fastmoss_skill_prompt(
    *,
    cache_dir: Path | None = None,
    archive_payload: bytes | None = None,
    expected_sha256: str = OFFICIAL_SKILL_SHA256,
) -> str:
    """Return the exact official instruction/reference Markdown as one prompt."""
    root = cache_dir or _skill_cache_dir()
    version_dir = root / OFFICIAL_SKILL_COMMIT
    cache_key = f"{version_dir.resolve()}:{expected_sha256}"
    with _LOAD_LOCK:
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]
        files = _read_cached_prompt_files(version_dir)
        if files is None:
            payload = archive_payload if archive_payload is not None else _download_archive()
            _verify_archive(payload, expected_sha256)
            files = _read_prompt_files_from_archive(payload)
            _write_cached_prompt_files(version_dir, files)
        prompt = _compose_prompt(files)
        _PROMPT_CACHE[cache_key] = prompt
        return prompt


def select_official_fastmoss_skill_prompt(
    prompt: str,
    relative_name: str,
) -> str:
    """Keep the official entrypoint, shared rules, and one workflow Skill."""
    if relative_name not in OFFICIAL_PROMPT_FILES:
        raise ValueError(f"Unknown official FastMoss Skill file: {relative_name}")

    header = prompt.split("\n\n## 官方文件：", 1)[0].strip()
    selected_names = tuple(dict.fromkeys((
        "SKILL.md",
        "references/PRINCIPLES.md",
        "references/GLOSSARY.md",
        relative_name,
    )))
    sections: list[str] = []
    for name in selected_names:
        marker = f"\n\n## 官方文件：{name}\n\n"
        if marker not in prompt:
            raise RuntimeError(
                f"Official FastMoss Skill prompt is missing file: {name}"
            )
        document = prompt.split(marker, 1)[1].split(
            "\n\n## 官方文件：", 1
        )[0].strip()
        sections.append(f"{marker}{document}")
    return header + "".join(sections)


def clear_official_fastmoss_skill_memory_cache() -> None:
    """Test helper; disk cache is intentionally preserved."""
    with _LOAD_LOCK:
        _PROMPT_CACHE.clear()

