#!/usr/bin/env python3
"""Ad-hoc coverage tests for the FastMoss semantic evidence renderer."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fastmoss_evidence_renderer import (  # noqa: E402
    FASTMOSS_CURRENT_TOOL_NAMES,
    FASTMOSS_RENDER_SPECS,
    PROFILE_DISTRIBUTION,
    PROFILE_ENTITY,
    PROFILE_NARRATIVE,
    PROFILE_RECORDS,
    PROFILE_REFERENCE,
    PROFILE_RELATIONSHIP,
    PROFILE_TREND,
    business_leaf_paths,
    render_fastmoss_evidence_document,
    render_fastmoss_tool_evidence,
)


EXPECTED_FASTMOSS_TOOLS = frozenset({
    "ad_data_overview", "ad_search",
    "agency_creator_analysis", "agency_product_analysis", "agency_product_list",
    "agency_profile_overview", "agency_rank_top", "agency_search", "agency_shop_analysis",
    "creator_cargo_summary", "creator_data_trends", "creator_fans_distribution",
    "creator_product_list", "creator_profile_overview", "creator_rank_top_ecommerce",
    "creator_rank_top_growth", "creator_rank_top_potential", "creator_search",
    "creator_video_analysis", "fastmoss_detail_url_examples", "live_detail_analysis",
    "live_products_list", "live_search", "market_category_analysis",
    "market_category_author_sales_matrix", "market_category_ranking",
    "product_category_info", "product_creator_analysis", "product_detail_info",
    "product_investment", "product_overview", "product_rank_new_listed",
    "product_rank_top_selling", "product_review_list", "product_sales_trend",
    "product_search", "product_sku", "product_video_list", "search_category_by_words",
    "search_fastmoss_documents", "shop_base_info", "shop_creator_analysis",
    "shop_data_trends", "shop_investment_analysis", "shop_live_analysis",
    "shop_product_analysis", "shop_rank_top_selling", "shop_sale_analysis",
    "shop_search", "shop_video_analysis", "video_data_trends", "video_detail_analysis",
    "video_script_info", "video_search",
})


def _fixture(profile: str) -> dict:
    if profile == PROFILE_REFERENCE:
        return {
            "total": 1,
            "categories": [{"category_id": "935176", "name": "Food Processors", "score": 0.5023}],
            "schema_extension": "reference-extra",
        }
    if profile == PROFILE_RECORDS:
        return {
            "total": 1,
            "list": [{
                "product_id": "1730000000000000001",
                "title": "Mini Grinder",
                "day28_units_sold": 120,
                "day28_gmv": 3600,
                "schema_extension": "row-extra",
            }],
            "schema_extension": "records-extra",
        }
    if profile == PROFILE_ENTITY:
        return {
            "product": {
                "product_id": "1730000000000000001",
                "title": "Mini Grinder",
                "region": "US",
                "current_price": 34.99,
            },
            "schema_extension": "entity-extra",
        }
    if profile == PROFILE_TREND:
        return {
            "period_summary": {
                "product_id": "1730000000000000001",
                "time_range_days": 90,
                "period_gmv": 3600,
            },
            "daily_trend": [
                {"date": "2026-07-01", "daily_gmv": 100, "daily_units_sold": 4},
                {"date": "2026-07-02", "daily_gmv": 120, "daily_units_sold": 5},
            ],
            "schema_extension": "trend-extra",
        }
    if profile == PROFILE_DISTRIBUTION:
        return {
            "follower_tier_distribution": [
                {"follower_tier": "10k-50k", "creator_count": 8, "gmv_share_percent": 12.5},
            ],
            "schema_extension": "distribution-extra",
        }
    if profile == PROFILE_RELATIONSHIP:
        return {
            "product": {"product_id": "1730000000000000001", "title": "Mini Grinder"},
            "linked_creators": [{
                "creator_uid": "7000000000000000001",
                "nickname": "Creator A",
                "day28_gmv": 900,
            }],
            "schema_extension": "relationship-extra",
        }
    if profile == PROFILE_NARRATIVE:
        return {
            "video_id": "7400000000000000001",
            "subtitles": [
                {"start_time": 0, "end_time": 2.5, "text": "Easy prep in seconds."},
            ],
            "schema_extension": "script-extra",
        }
    raise AssertionError(profile)


def _entry(tool_name: str, data: dict, state: str = "data") -> dict:
    return {
        "source_ref": "call:1",
        "tool_name": f"fastmoss__{tool_name}",
        "arguments": {"filter": {"region": "US", "time_range_days": 28}},
        "evidence_fence": {
            "data_state": state,
            "region": "US",
            "period": "28d",
            "returned_count": 1,
        },
        "business_data": data,
    }


def test_catalog_covers_all_current_fastmoss_tools() -> None:
    assert EXPECTED_FASTMOSS_TOOLS == FASTMOSS_CURRENT_TOOL_NAMES
    assert EXPECTED_FASTMOSS_TOOLS == frozenset(FASTMOSS_RENDER_SPECS)
    assert len(FASTMOSS_RENDER_SPECS) == 54


def test_all_tools_render_success_without_silent_field_loss() -> None:
    for tool_name in sorted(EXPECTED_FASTMOSS_TOOLS):
        spec = FASTMOSS_RENDER_SPECS[tool_name]
        data = _fixture(spec.profile)
        result = render_fastmoss_tool_evidence(_entry(tool_name, data))
        assert not result.fallback, tool_name
        assert result.profile == spec.profile, tool_name
        assert result.business_leaf_paths == business_leaf_paths(data), tool_name
        assert result.business_leaf_paths == (
            result.consumed_paths | result.unmapped_paths | result.excluded_paths
        ), tool_name
        assert not (result.consumed_paths & result.unmapped_paths), tool_name
        assert "schema_extension" in result.markdown, tool_name
        assert "未映射业务字段" in result.markdown, tool_name
        assert "call:1" in result.markdown and tool_name in result.markdown, tool_name


def test_all_tools_render_empty_and_error_as_scoped_narrative() -> None:
    for tool_name in sorted(EXPECTED_FASTMOSS_TOOLS):
        empty = render_fastmoss_tool_evidence(
            _entry(tool_name, {"list": [], "total": 0}, state="empty")
        )
        assert empty.empty and not empty.fallback, tool_name
        assert "没有返回业务记录" in empty.markdown, tool_name
        assert "平台全局为零" in empty.markdown, tool_name
        assert "未映射业务字段" not in empty.markdown, tool_name
        assert "| 0 |" not in empty.markdown, tool_name
        assert "EmptyResult" in empty.node_types, tool_name

        error_entry = _entry(tool_name, {"request_id": "req-1"}, state="error")
        error_entry["error"] = "upstream timeout"
        failed = render_fastmoss_tool_evidence(error_entry)
        assert "失败范围仅限上述对象和参数" in failed.markdown, tool_name
        assert "upstream timeout" in failed.markdown, tool_name
        assert "ErrorResult" in failed.node_types, tool_name


def test_unknown_future_tool_uses_generic_shape_without_loss() -> None:
    data = {
        "items": [{"id": "x1", "name": "Future entity", "future_metric": 7}],
        "new_schema_object": {"opaque_value": "kept"},
    }
    result = render_fastmoss_tool_evidence(_entry("future_tool", data))
    assert result.profile == "generic"
    assert result.business_leaf_paths == result.consumed_paths | result.unmapped_paths
    assert "opaque_value" in result.markdown and "kept" in result.markdown


def test_document_keeps_call_order_boundaries_and_stats() -> None:
    dossier = {
        "workflow": "product",
        "report_date": "2026-07-19",
        "target_category_path": [13, 844168, 935176],
        "analysis_targets": [{"entity_type": "product", "entity_id": "1730000000000000001"}],
        "tool_evidence": [
            _entry("product_search", _fixture(PROFILE_RECORDS)),
            _entry("product_sales_trend", _fixture(PROFILE_TREND)),
        ],
        "hard_fact_boundaries": {"rules": ["跨实体或跨周期数据不得直接相除"]},
    }
    rendered = render_fastmoss_evidence_document(dossier)
    assert rendered.markdown.startswith("# FastMoss 调研证据")
    assert rendered.markdown.index("product_search") < rendered.markdown.index("product_sales_trend")
    assert "硬事实边界" in rendered.markdown
    assert rendered.stats["tool_count"] == 2
    assert rendered.stats["registered_tool_count"] == 2
    assert rendered.stats["fallback_tools"] == []
    assert rendered.stats["business_leaf_count"] == (
        rendered.stats["consumed_leaf_count"] + rendered.stats["unmapped_leaf_count"]
    )


if __name__ == "__main__":
    test_catalog_covers_all_current_fastmoss_tools()
    test_all_tools_render_success_without_silent_field_loss()
    test_all_tools_render_empty_and_error_as_scoped_narrative()
    test_unknown_future_tool_uses_generic_shape_without_loss()
    test_document_keeps_call_order_boundaries_and_stats()
    print("FastMoss evidence renderer tests passed")
