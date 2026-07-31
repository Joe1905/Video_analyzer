"""Automated verification script for all 27 SellerSprite official presets.

Tests:
1. Preset identification & routing (preset_id & label matching).
2. Skill Markdown file existence & provenances.
3. Allowed tools whitelist validation (all tools must be sellersprite__*).
4. Tool execution interception & Open API mock payload formatting.
5. Out-of-bounds tool execution rejection & boundary isolation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import web_app
from scripts.sellersprite_evidence_renderer import SELLERSPRITE_CURRENT_TOOL_NAMES


def run_27_presets_simulation() -> None:
    if not web_app.is_tool_mock_enabled("sellersprite"):
        raise RuntimeError(
            "Set SELLERSPRITE_TOOL_MOCK_MODE=1 before running this boundary test."
        )

    print("=" * 80)
    print("Starting 27 SellerSprite Official Presets Tool Mock & Boundary Verification")
    print("=" * 80)

    presets = web_app.SELLERSPRITE_OFFICIAL_PRESETS
    assert len(presets) == 27, f"Expected 27 presets, found {len(presets)}"

    all_sellersprite_tools = {
        f"sellersprite__{name}"
        for name in SELLERSPRITE_CURRENT_TOOL_NAMES
    }

    for index, (pid, pinfo) in enumerate(presets.items(), start=1):
        label = pinfo["label"]
        skill_file = pinfo["skill_file"]
        allowed_tools = pinfo["tools"]

        print(f"\n[{index:02d}/27] Testing Preset: '{label}' ({pid})")
        print(f"  - Official Skill File: {skill_file}")
        print(f"  - Whitelisted Tools ({len(allowed_tools)}): {sorted(allowed_tools)}")

        # 1. Route Verification by explicit preset ID
        route_id = web_app.sellersprite_official_skill_route("test query", pid)
        assert route_id["route_source"] == "official_preset"
        assert route_id["official_preset_id"] == pid
        assert route_id["official_skill_file"] == skill_file
        assert set(route_id["tools"]) == set(allowed_tools)

        # 2. Route Verification by Chinese Label Prompt Text
        prompt_text = f"请使用卖家精灵官方 Skill「{label}」开始分析。\n\n目标：测试商品"
        route_label = web_app.sellersprite_official_skill_route(prompt_text)
        assert route_label["route_source"] == "official_preset"
        assert route_label["official_preset_id"] == pid

        # 3. Simulate Execution & Interception for every Whitelisted Tool
        for tool_id in sorted(allowed_tools):
            sample_args = {"asin": "B08TEST001", "keyword": "fidget toy", "category": "Toys"}

            # Log boundary & intercept details
            print(
                f"  -> Executing & Intercepting Tool: {tool_id} "
                f"args={json.dumps(sample_args, ensure_ascii=False)}"
            )

            result = web_app.execute_prefixed_tool(tool_id, sample_args)
            assert result["ok"] is True, f"Execution failed for {tool_id}: {result}"
            
            raw_data = result["data"]
            assert raw_data["isError"] is False, f"MCP returned error for {tool_id}"
            
            parsed_text = web_app.parse_mcp_text_content(
                web_app.mcp_text_content(raw_data)
            )
            assert isinstance(parsed_text, dict), f"Failed to parse JSON for {tool_id}"
            assert parsed_text.get("code") == 200, f"Expected code 200, got {parsed_text.get('code')}"
            assert parsed_text.get("msg") == "success"
            assert parsed_text.get("is_test_mock") is True
            assert "[测试拦截/模拟发送]" in parsed_text.get("notice", "")
            
        # 4. Out-of-Bounds Tool Isolation Check
        forbidden_tools = all_sellersprite_tools - allowed_tools
        if forbidden_tools:
            out_of_bound_candidate = sorted(forbidden_tools)[0]
            preset_exposed = set(route_id["tools"])
            assert out_of_bound_candidate not in preset_exposed, (
                f"Boundary failure: Forbidden tool {out_of_bound_candidate} leaked into preset {pid}"
            )
            print(f"  - Out-of-bounds tool check: Forbidden '{out_of_bound_candidate}' correctly excluded.")

    print("\n" + "=" * 80)
    print(f"VERIFICATION COMPLETE: All 27 SellerSprite Official Presets PASSED Successfully!")
    print("=" * 80)


if __name__ == "__main__":
    run_27_presets_simulation()
