"""Verified, complete local copy of the Chuhaijiang 1.2.6 official Skill."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


OFFICIAL_SKILL_VERSION = "1.2.6"
OFFICIAL_SKILL_SHA256 = "8c51c2d49f51ed0521eb2904b7a5300bef4c992be026bc64174a0db501684d4e"
OFFICIAL_TOOL_NAMES = frozenset({
    "search", "get_detail", "get_related", "amazon", "ai_generate", "check_task",
    "canvas", "canvas_tasks", "assets", "video_editor", "social_accounts",
    "social_publish", "social_comments", "social_analytics", "social_tools",
    "social_messages", "social_seller", "account_info", "upload_file",
})
OFFICIAL_PROMPT_FILES = (
    "SKILL.md", "_meta.json", "references/setup.md", "references/profit-model.md",
    "references/competitor-analysis.md", "references/prompt-templates.md",
    "references/canvas-operations.md", "references/product-selection.md",
    "references/creator-outreach.md", "references/content-generation.md",
    "references/social-media.md", "references/video-editor.md",
)


def skill_root() -> Path:
    return Path(__file__).resolve().parent / "skills" / "chuhaijiang"


def verify_official_skill() -> None:
    root = skill_root()
    required = [root / relative for relative in OFFICIAL_PROMPT_FILES]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Chuhaijiang official Skill missing files: " + ", ".join(missing))
    metadata = json.loads((root / "_meta.json").read_text(encoding="utf-8"))
    if str(metadata.get("version") or "") != OFFICIAL_SKILL_VERSION:
        raise RuntimeError("Chuhaijiang official Skill version mismatch")


def load_official_skill_prompt(selected_files: Iterable[str] | None = None) -> str:
    verify_official_skill()
    root = skill_root()
    prompt_files = OFFICIAL_PROMPT_FILES
    if selected_files is not None:
        requested = tuple(dict.fromkeys(str(item or "").strip() for item in selected_files))
        unknown = [item for item in requested if item not in OFFICIAL_PROMPT_FILES]
        if unknown:
            raise ValueError("Unknown Chuhaijiang official Skill file(s): " + ", ".join(unknown))
        prompt_files = tuple(dict.fromkeys(("SKILL.md", *requested)))
    sections = []
    for relative in prompt_files:
        sections.append(f"## 官方文件：{relative}\n\n{(root / relative).read_text(encoding='utf-8').strip()}")
    return "\n\n".join(sections)


def is_high_risk_tool(name: str, args: dict | None = None) -> bool:
    """Conservative backend confirmation gate for official side-effect operations."""
    args = args or {}
    if name in {"ai_generate", "upload_file", "social_publish"}:
        return True
    action = str(args.get("action") or "").lower()
    if name == "canvas":
        return action not in {"load", "list", "hydrate", "context"}
    if name == "video_editor":
        return action not in {"capabilities", "context", "renders", "action_status"}
    if name == "social_accounts":
        return action not in {"list"}
    if name == "social_comments":
        return action in {"reply", "delete", "hide", "unhide"}
    if name == "social_messages":
        return action in {"send", "remark", "upload"}
    if name == "social_seller":
        return action not in {"daily_report", "shops_overview", "products", "product_detail"}
    return False
