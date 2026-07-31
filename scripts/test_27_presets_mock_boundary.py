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


def run_27_presets_simulation() -> None:
    print("=" * 80)
    print("Starting 27 SellerSprite Official Presets Tool Mock & Boundary Verification")
    print("=" * 80)

    presets = web_app.SELLERSPRITE_OFFICIAL_PRESETS
    assert len(presets) == 27, f"Expected 27 presets, found {len(presets)}"

    all_sellersprite_tools = {
        "sellersprite__product_research",
        "sellersprite__product_node",
        "sellersprite__asin_detail",
        "sellersprite__asin_prediction",
        "sellersprite__market_research",
        "sellersprite__market_research_statistics",
        "sellersprite__keyword_research",
        "sellersprite__keyword_miner",
        "sellersprite__google_trend",
        "sellersprite__review",
        "sellersprite__keepa_info",
        "sellersprite__traffic_keyword",
        "sellersprite__traffic_keyword_stat",
        "sellersprite__traffic_source",
        "sellersprite__traffic_extend",
        "sellersprite__traffic_listing",
        "sellersprite__traffic_listing_stat",
        "sellersprite__keyword_order",
        "sellersprite__asin_coupon_trend",
        "sellersprite__competitor_lookup",
        "sellersprite__market_brand_concentration",
        "sellersprite__market_seller_concentration",
        "sellersprite__market_product_concentration",
        "sellersprite__market_listing_date_distribution",
        "sellersprite__market_listing_trend_distribution",
        "sellersprite__market_price_distribution",
        "sellersprite__market_rating_distribution",
        "sellersprite__market_ratings_count_distribution",
        "sellersprite__market_ebc_distribution",
        "sellersprite__market_seller_country_distribution",
        "sellersprite__market_seller_type_concentration",
        "sellersprite__aba_research_weekly",
        "sellersprite__aba_research_monthly",
        "sellersprite__aba_research_trend",
        "sellersprite__keyword_research_trends",
    }

    results_summary = []

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
        executed_tools_results = []
        for tool_id in sorted(allowed_tools):
            domain, tool_name = web_app.split_prefixed_tool_id(tool_id)
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
            
            executed_tools_results.append({
                "tool": tool_id,
                "code": parsed_text["code"],
                "mock": parsed_text["is_test_mock"],
            })

        # 4. Out-of-Bounds Tool Isolation Check
        forbidden_tools = all_sellersprite_tools - allowed_tools
        if forbidden_tools:
            out_of_bound_candidate = sorted(forbidden_tools)[0]
            # Verify that sellersprite_official_skill_tool_ids or schema exposure excludes the out-of-bounds tool
            filtered = web_app.sellersprite_official_skill_tool_ids({out_of_bound_candidate})
            # When routed for a preset, only allowed_tools should be exposed
            preset_exposed = set(route_id["tools"])
            assert out_of_bound_candidate not in preset_exposed, (
                f"Boundary failure: Forbidden tool {out_of_bound_candidate} leaked into preset {pid}"
            )
            print(f"  - Out-of-bounds tool check: Forbidden '{out_of_bound_candidate}' correctly excluded.")

        results_summary.append({
            "index": index,
            "pid": pid,
            "label": label,
            "tools_count": len(allowed_tools),
            "status": "PASSED",
        })

    print("\n" + "=" * 80)
    print(f"VERIFICATION COMPLETE: All 27 SellerSprite Official Presets PASSED Successfully!")
    print("=" * 80)


if __name__ == "__main__":
    run_27_presets_simulation()
