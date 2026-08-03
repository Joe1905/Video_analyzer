"""Local, reviewable FastMoss Skill prompts for the 4004 chat runtime."""

from __future__ import annotations

from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent / "skills" / "fastmoss-product-scout"


def fastmoss_skill_source() -> str:
    """Product Scout is intentionally local-only in the 4004 development build."""
    return "local"


def uses_lightweight_fastmoss_skill(preset_id: str | None) -> bool:
    return str(preset_id or "").strip() == "fm-product-scout"


def load_lightweight_fastmoss_skill_prompt(preset_id: str) -> str:
    """Load the sole authoritative local Product Scout Skill."""
    if str(preset_id or "").strip() != "fm-product-scout":
        raise ValueError(f"No local FastMoss Skill for preset: {preset_id}")
    workflow_path = SKILL_ROOT / "SKILL.md"
    try:
        workflow = workflow_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not load local FastMoss Skill: {exc}") from exc
    if not workflow:
        raise RuntimeError("Local FastMoss Skill is empty")
    return workflow
