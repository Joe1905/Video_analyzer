"""Focused V2 Product Scout evidence-contract regression tests."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastmoss_product_scout_v2 import (
    build_product_scout_evidence_contract,
    contract_next_instruction,
    product_scout_v2_market_ambiguity_question,
    product_scout_v2_mode,
    render_deterministic_fact_blocks,
    validate_interpretation,
)


def _calls() -> list[dict]:
    return [
        {"function": {"arguments": json.dumps({"filter": {"region": "US", "category_id": 935176, "date_type": "week", "date_value": "2026-W30"}})}},
        {"function": {"arguments": json.dumps({"filter": {"region": "US", "category_id": 935176, "date_type": "week", "date_value": "2026-W30"}, "orderby": [{"field": "period_units_sold", "order": "desc"}]})}},
        {"function": {"arguments": json.dumps({"filter": {"region": "US", "category_id": 935176, "listing_start_date": "2026-07-01", "listing_end_date": "2026-07-14"}})}},
        {"function": {"arguments": json.dumps({"filter": {"product_id": "p1", "time_range_days": 28}})}},
        {"function": {"arguments": json.dumps({"filter": {"product_id": "p1", "time_range_days": 28}})}},
        {"function": {"arguments": json.dumps({"filter": {"product_id": "p2", "time_range_days": 28}})}},
        {"function": {"arguments": json.dumps({"filter": {"product_id": "p2", "time_range_days": 28}})}},
    ]


def _result(tool: str, data: dict, state: str = "data") -> dict:
    return {"tool_name": f"fastmoss__{tool}", "result": {"ok": state != "error", "data_state": state, "mcp_data": data}}


def _complete_results() -> list[dict]:
    hot = [{"product_id": f"p{index}", "product_name": f"Hot {index}", "current_price": index + 10, "period_units_sold": 100 - index, "period_gmv": 1000 - index, "product_url": f"https://shop.test/p{index}"} for index in range(1, 4)]
    new = [{"product_id": f"n{index}", "product_name": f"New {index}", "launch_date": f"2026-07-0{index}", "day28_units_sold": index * 10} for index in range(1, 4)]
    return [
        _result("market_category_analysis", {"market_size": 1000, "growth_rate": 0.1, "shop_count": 12, "currency": "USD", "date_value": "2026-W30"}),
        _result("product_rank_top_selling", {"items": hot}),
        _result("product_rank_new_listed", {"items": new}),
        _result("product_sales_trend", {"items": [{"product_id": "p1", "date": "2026-07-01", "units_sold": 10}]}),
        _result("product_video_list", {"items": [{"product_id": "p1", "video_id": "v1"}]}),
        _result("product_sales_trend", {"items": [{"product_id": "p2", "date": "2026-07-01", "units_sold": 9}]}),
        _result("product_creator_analysis", {"items": [{"product_id": "p2", "creator_id": "c1"}]}),
    ]


class ProductScoutV2Tests(unittest.TestCase):
    def test_redacted_golden_fixture_catalog_is_present(self):
        root = Path(__file__).resolve().parent / "fixtures" / "fastmoss_product_scout_v2"
        names = {path.name for path in root.glob("*.json")}
        self.assertEqual(names, {
            "official_19_tools.json", "v1_5_tools_degraded.json", "v1_8_tools_repaired.json",
        })
        for path in root.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("source", payload)
            self.assertIn("redaction", payload)
            self.assertIn("expected_contract", payload)

    def test_modes_are_closed(self):
        self.assertEqual(product_scout_v2_mode("shadow"), "shadow")
        self.assertEqual(product_scout_v2_mode("ENFORCE"), "enforce")
        self.assertEqual(product_scout_v2_mode("bad"), "off")
        self.assertIsNone(product_scout_v2_market_ambiguity_question("分析美国解压玩具"))
        self.assertIn("多个市场", product_scout_v2_market_ambiguity_question("比较美国和英国解压玩具") or "")

    def test_contract_grade_a_preserves_scope_rows_links_and_gaps(self):
        contract = build_product_scout_evidence_contract(
            _calls(), _complete_results(), "请分析美国解压玩具", {"region": "US"},
        )
        self.assertEqual(contract.grade, "A")
        self.assertEqual(contract.status, "sufficient")
        self.assertEqual(contract.payload["scope"]["market"]["source"], "user")
        self.assertEqual(len(contract.payload["hot_ranking"]["rows"]), 3)
        self.assertEqual(contract.payload["hot_ranking"]["rows"][0]["link"], "https://shop.test/p1")
        self.assertIn("热销榜", render_deterministic_fact_blocks(contract))
        self.assertIn("新品榜", render_deterministic_fact_blocks(contract))
        self.assertIn("候选验证表", render_deterministic_fact_blocks(contract))

    def test_contract_grades_b_c_d_are_conservative(self):
        results = _complete_results()
        partial = build_product_scout_evidence_contract(_calls()[:3], results[:3], "分析解压玩具", {})
        self.assertEqual(partial.grade, "B")
        snapshot = build_product_scout_evidence_contract(_calls()[:2], results[:2], "分析解压玩具", {})
        self.assertEqual(snapshot.grade, "C")
        unavailable = build_product_scout_evidence_contract([], [_result("product_rank_top_selling", {}, "error")], "分析解压玩具", {})
        self.assertEqual(unavailable.grade, "D")
        self.assertEqual(unavailable.status, "unavailable")
        self.assertIn("能力缺口", contract_next_instruction(snapshot))

    def test_l2_evidence_cannot_be_presented_as_target_l3(self):
        calls = _calls()
        calls[2] = {"function": {"arguments": json.dumps({
            "filter": {"region": "US", "category_l3_id": 935176,
                       "listing_start_date": "2026-07-01", "listing_end_date": "2026-07-14"}
        })}}
        contract = build_product_scout_evidence_contract(calls, _complete_results(), "分析美国解压玩具", {"region": "US"})
        self.assertEqual(contract.payload["market_evidence"]["grain"], "upstream_L2_reference_for_L3")
        self.assertEqual(contract.grade, "C")

    def test_interpretation_cannot_add_numbers_or_unsupported_claims(self):
        contract = build_product_scout_evidence_contract(_calls()[:2], _complete_results()[:2], "分析解压玩具", {})
        text, violations = validate_interpretation("建议备货 100 件，当前是低竞争最佳窗口。\n\n建议先继续验证渠道信号。", contract)
        self.assertIn("建议先继续验证渠道信号", text)
        self.assertIn("interpretation_contains_unbound_number", violations)
        self.assertIn("unsupported_high_risk_claim", violations)

    def test_ranking_removes_test_links_and_duplicate_entities_with_reasons(self):
        results = _complete_results()
        results[1] = _result("product_rank_top_selling", {"items": [
            {"product_id": "p1", "product_name": "Valid", "product_url": "https://shop.test/p1"},
            {"product_id": "p1", "product_name": "Duplicate", "product_url": "https://shop.test/p1-copy"},
            {"product_id": "test", "product_name": "测试商品", "product_url": "https://example.test/test"},
        ]})
        contract = build_product_scout_evidence_contract(_calls(), results, "分析美国解压玩具", {"region": "US"})
        ranking = contract.payload["hot_ranking"]
        self.assertEqual(len(ranking["rows"]), 1)
        self.assertEqual(ranking["returned_count"], 3)
        self.assertEqual({item["reason"] for item in ranking["rejected_rows"]}, {"duplicate_entity", "test_or_placeholder_link"})

    def test_runtime_mcp_shapes_keep_product_ids_and_candidate_evidence(self):
        calls = [
            {"function": {"arguments": json.dumps({"filter": {"category_id": 869640, "date_type": "day", "date_value": "2026-07-31", "region": "US"}})}},
            {"function": {"arguments": json.dumps({"filter": {"category_l3_id": 869640, "listing_start_date": "2026-06-29", "listing_end_date": "2026-07-28", "region": "US"}})}},
            *[{"function": {"arguments": json.dumps({"filter": {"product_id": product_id, "time_range_days": 28}})}} for product_id in ("p1", "p2")],
            *[{"function": {"arguments": json.dumps({"filter": {"product_id": product_id, "time_range_days": 28}})}} for product_id in ("p1", "p2")],
            {"function": {"arguments": json.dumps({"filter": {"category_id": 19, "date_type": "week", "date_value": "2026-W30", "region": "US"}})}},
        ]
        category = {"l1": {"id": 19, "name": "Toys & Hobbies"}, "l3": {"id": 869640, "name": "Stress Relief Toys"}}
        hot_rows = [{"category": category, "product_id": f"p{index}", "title": f"Hot {index}", "period_units_sold": 20 - index} for index in range(1, 4)]
        new_rows = [{"category": category, "product_id": f"n{index}", "title": f"New {index}", "first_3d_units_sold": index} for index in range(1, 4)]
        results = [
            _result("product_rank_top_selling", {"list": hot_rows}),
            _result("product_rank_new_listed", {"list": new_rows}),
            *[_result("product_sales_trend", {"daily_trend": [{"date": "2026-07-01", "daily_units_sold": 3}]}) for _ in range(2)],
            *[_result("product_video_list", {"videos": [{"video_id": "v1"}]}) for _ in range(2)],
            _result("market_category_analysis", {"scale_metrics": {"category_gmv": 1000}, "growth_metrics": {"category_gmv_yoy_percent": 2}, "concentration_metrics": {"top_shops_gmv_share": 0.2}}),
        ]
        contract = build_product_scout_evidence_contract(calls, results, "分析美国解压玩具", {"region": "US"})
        self.assertEqual(len(contract.payload["hot_ranking"]["rows"]), 3)
        self.assertEqual([row["product_id"] for row in contract.payload["hot_ranking"]["rows"]], ["p1", "p2", "p3"])
        self.assertEqual(len(contract.payload["new_ranking"]["rows"]), 3)
        self.assertEqual(sum(item["sales_trend"] == "verified" for item in contract.payload["candidate_validations"]), 2)
        self.assertEqual(sum(item["creator_video_live_leading_signals"] == "verified" for item in contract.payload["candidate_validations"]), 2)
        self.assertTrue({"scale", "growth", "competition_or_concentration"}.issubset({item["metric"] for item in contract.payload["market_evidence"]["metrics"]}))

    def test_interpretation_rejects_cost_absolute_and_unsupported_window_claims(self):
        contract = build_product_scout_evidence_contract(_calls()[:2], _complete_results()[:2], "分析解压玩具", {})
        _, violations = validate_interpretation("这是最佳低竞争窗口开放期，建议备货并确保毛利。", contract)
        self.assertIn("unsupported_cost_or_inventory_claim", violations)
        self.assertIn("unsupported_absolute_claim", violations)
        self.assertIn("unsupported_growth_window_claim", violations)


if __name__ == "__main__":
    unittest.main()
