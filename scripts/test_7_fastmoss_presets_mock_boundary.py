"""Manual mock-boundary verification for all seven FastMoss official presets.

Run this only with ``FASTMOSS_TOOL_MOCK_MODE=1``.  It never contacts the
FastMoss MCP service: every whitelisted call must return the standard test
interception notice.  The checks mirror SellerSprite's preset-boundary test.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import web_app


def run_fastmoss_presets_simulation() -> None:
    if not web_app.is_tool_mock_enabled("fastmoss"):
        raise RuntimeError(
            "Set FASTMOSS_TOOL_MOCK_MODE=1 before running this boundary test."
        )

    presets = web_app.FASTMOSS_OFFICIAL_PRESETS
    assert len(presets) == 7, f"Expected 7 presets, found {len(presets)}"
    all_fastmoss_tools = set().union(*(info["tools"] for info in presets.values()))
    full_prompt = web_app.load_official_fastmoss_skill_prompt()

    print("=" * 80)
    print("Starting 7 FastMoss Official Presets Tool Mock & Boundary Verification")
    print("=" * 80)
    for index, (preset_id, preset) in enumerate(presets.items(), start=1):
        label = preset["label"]
        allowed_tools = set(preset["tools"])
        skill_file = preset["skill_file"]
        print(f"\n[{index:02d}/07] Testing preset: {label} ({preset_id})")

        route_by_id = web_app.fastmoss_official_skill_route(
            "测试目标", preset_id
        )
        assert route_by_id["route_source"] == "official_preset"
        assert route_by_id["official_preset_id"] == preset_id
        assert route_by_id["official_skill_file"] == skill_file
        assert set(route_by_id["tools"]) == allowed_tools

        route_by_label = web_app.fastmoss_official_skill_route(
            f"请使用 FastMoss 官方 Skill「{label}」开始分析。\n\n目标：测试商品"
        )
        assert route_by_label["official_preset_id"] == preset_id

        selected_prompt = web_app.select_official_fastmoss_skill_prompt(
            full_prompt, skill_file
        )
        assert f"## 官方文件：{skill_file}" in selected_prompt
        assert "## 官方文件：SKILL.md" in selected_prompt
        assert "## 官方文件：references/tool-call.md" in selected_prompt

        exposed_tools = web_app.fastmoss_official_skill_tool_ids(
            all_fastmoss_tools | {"system__current_time"}, route_by_id["tools"]
        )
        assert exposed_tools == allowed_tools

        for tool_id in sorted(allowed_tools):
            args = {
                "keyword": "fidget toy",
                "product_id": "test_product_001",
                "region": "US",
            }
            print(f"  -> mock intercepted: {tool_id} {json.dumps(args)}")
            result = web_app.execute_prefixed_tool(tool_id, args)
            assert result["ok"] is True, result
            raw_data = result["data"]
            assert raw_data["isError"] is False
            payload = web_app.parse_mcp_text_content(
                web_app.mcp_text_content(raw_data)
            )
            assert isinstance(payload, dict)
            assert payload.get("code") == 200
            assert payload.get("is_test_mock") is True
            assert "[测试拦截/模拟发送]" in str(payload.get("notice") or "")

        forbidden_tools = all_fastmoss_tools - allowed_tools
        if forbidden_tools:
            forbidden = sorted(forbidden_tools)[0]
            assert forbidden not in exposed_tools
            print(f"  -> boundary preserved: {forbidden} excluded")

    print("\nAll 7 FastMoss official presets passed mock boundary verification.")


if __name__ == "__main__":
    run_fastmoss_presets_simulation()
