"""Local, reviewable FastMoss Skill prompts for the 4004 chat runtime."""

from __future__ import annotations

import os
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent / "skills" / "fastmoss"
LIGHTWEIGHT_PRESET_FILES = {
    "fm-product-scout": "fm-product-scout.md",
}
PRODUCT_SCOUT_V2_FILE = "fm-product-scout-v2.md"


def fastmoss_skill_source() -> str:
    """Return the requested source; unsupported values safely use the local Skill."""
    source = str(os.getenv("FASTMOSS_SKILL_SOURCE", "local")).strip().lower()
    return source if source in {"local", "official"} else "local"


def uses_lightweight_fastmoss_skill(preset_id: str | None) -> bool:
    """Only opt the migrated preset into local guidance during the staged rollout."""
    return (
        fastmoss_skill_source() == "local"
        and str(preset_id or "").strip() in LIGHTWEIGHT_PRESET_FILES
    )


def load_lightweight_fastmoss_skill_prompt(preset_id: str) -> str:
    """Load the shared local principles plus the selected workflow document."""
    filename = LIGHTWEIGHT_PRESET_FILES.get(str(preset_id or "").strip())
    if not filename:
        raise ValueError(f"No lightweight FastMoss Skill for preset: {preset_id}")
    base_path = SKILL_ROOT / "BASE.md"
    workflow_path = SKILL_ROOT / filename
    try:
        base = base_path.read_text(encoding="utf-8").strip()
        workflow = workflow_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not load local FastMoss Skill: {exc}") from exc
    if not base or not workflow:
        raise RuntimeError("Local FastMoss Skill is empty")
    return base + "\n\n---\n\n" + workflow


def load_product_scout_v2_skill_prompt() -> str:
    """Load the V2 Product Scout instruction source, independent of V1 rollback."""
    base_path = SKILL_ROOT / "BASE.md"
    workflow_path = SKILL_ROOT / PRODUCT_SCOUT_V2_FILE
    try:
        base = base_path.read_text(encoding="utf-8").strip()
        workflow = workflow_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not load FastMoss Product Scout V2 Skill: {exc}") from exc
    if not base or not workflow:
        raise RuntimeError("FastMoss Product Scout V2 Skill is empty")
    return base + "\n\n---\n\n" + workflow
