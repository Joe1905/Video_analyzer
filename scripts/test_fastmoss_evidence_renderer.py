
#!/usr/bin/env python3
"""Ad-hoc coverage tests for the FastMoss semantic evidence renderer."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fastmoss_evidence_renderer import (  # noqa: E402
    FASTMOSS_CURRENT_TOOL_NAMES,
    FASTMOSS_RENDER_SPECS,
    FASTMOSS_TOOL_TITLES,
    PROFILE_DISTRIBUTION,
    PROFILE_ENTITY,
    PROFILE_NARRATIVE,
    PROFILE_RECORDS,
    PROFILE_REFERENCE,
    PROFILE_RELATIONSHIP,
    PROFILE_TREND,
    business_leaf_paths,
    fastmoss_semantic_registry_diagnostics,
    render_fastmoss_evidence_document,
    render_fastmoss_tool_evidence,
)
from fastmoss_evidence_renderer import _ENUM_VALUE_LABELS, _FIELD_LABELS  # noqa: E402


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
        "arguments": {
            "filter": {
                "region": "US",
                "date_type": "month",
                "date_value": "2026-06",
                "time_range_days": 28,
            },
            "lang": "ZH_CN",
        },
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
    diagnostics = fastmoss_semantic_registry_diagnostics(
        f"fastmoss__{name}" for name in EXPECTED_FASTMOSS_TOOLS
    )
    assert diagnostics["ok"] is True
    assert diagnostics["missing_contracts"] == []
    assert diagnostics["missing_runtime"] == []


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
        assert not result.unmapped_paths, tool_name
        assert result.excluded_paths == set(result.exclusion_reasons), tool_name
        assert any("schema_extension" in path for path in result.excluded_paths), tool_name
        assert "schema extension" not in result.markdown, tool_name
        assert "未映射业务字段" not in result.markdown, tool_name
        assert "JSON路径" not in result.markdown, tool_name
        assert "原字段" not in result.markdown, tool_name
        assert "$.business_data" not in result.markdown, tool_name
        assert "call:1" not in result.markdown and f"fastmoss__{tool_name}" not in result.markdown, tool_name



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
        assert "upstream timeout" not in failed.markdown, tool_name
        assert "ErrorResult" in failed.node_types, tool_name


def test_unknown_future_tool_is_isolated_without_raw_json_leak() -> None:
    data = {
        "items": [{"id": "x1", "name": "Future entity", "future_metric": 7}],
        "new_schema_object": {"opaque_value": "kept"},
    }
    result = render_fastmoss_tool_evidence(_entry("future_tool", data))
    assert result.profile == "generic"
    assert result.business_leaf_paths == result.excluded_paths
    assert not result.unmapped_paths
    assert result.fallback
    assert result.exclusion_reasons
    assert "opaque value" not in result.markdown and "kept" not in result.markdown
    assert "JSON路径" not in result.markdown


def test_official_fastmoss_video_fields_render_as_semantic_values() -> None:
    result = render_fastmoss_tool_evidence(_entry("product_video_list", {
        "list": [{
            "product_id": "1729421229342823356",
            "video_id": "7468186866076880170",
            "is_ad": 0,
            "create_time": 1765024218000,
            "units_sold": 28,
            "gmv": 700.0,
            "video": {
                "video_desc": "demo video",
                "duration": 12,
                "fastmoss_url": "https://www.fastmoss.com/example",
            },
        }],
        "total": 1,
    }))
    assert not result.fallback
    assert not result.unmapped_paths
    assert "否（非广告）" in result.markdown
    assert "2025年12月6日20时30分18秒（北京时间）" in result.markdown
    assert "1765024218000" not in result.markdown
    assert "视频时长（秒）" in result.markdown
    assert "JSON路径" not in result.markdown


def test_week_request_keeps_provider_week_without_invented_calendar_boundary() -> None:
    entry = _entry("market_category_ranking", {
        "ranked_categories": [{
            "category_name": "美妆个护",
            "category_units_sold": 3841949,
            "category_gmv_yoy_percent": 3.76,
        }],
    })
    entry["arguments"] = {
        "filter": {"region": "US", "date_type": "week", "date_value": "2026-W29"},
        "orderby": [{"field": "category_units_sold", "order": "desc"}],
        "lang": "ZH_CN",
    }
    result = render_fastmoss_tool_evidence(entry)
    assert "统计周期类型 | 周" in result.markdown
    assert "统计日期 | 2026年第29周" in result.markdown
    assert "2026-07-13" not in result.markdown
    assert "ISO" not in result.markdown
    assert "成交结构" in result.markdown
    assert "未取得时只能描述占比现象" in result.markdown
    assert "排序字段：类目销量；排序规则：降序" in result.markdown
    assert "类目成交金额同比增长率" in result.markdown
    assert "category GMV yoy percent" not in result.markdown
    assert "date type" not in result.markdown


def test_product_ranking_boundaries_prevent_period_and_causal_overreach() -> None:
    new_listed = render_fastmoss_tool_evidence(_entry("product_rank_new_listed", {
        "items": [{"product_id": "p1", "first_3d_gmv": 797940, "first_3d_units_sold": 8060}],
    }))
    assert "三日累计口径" in new_listed.markdown
    assert "不是单日或一天内的指标" in new_listed.markdown
    assert "需解释爆发原因时应先调用对应工具" in new_listed.markdown

    top_selling = render_fastmoss_tool_evidence(_entry("product_rank_top_selling", {
        "items": [{"product_id": "p2", "period_gmv": 500000, "period_units_sold": 12000}],
    }))
    assert "不能改写为实时趋势或单日表现" in top_selling.markdown
    assert "不得宣称某商品由单一爆款视频" in top_selling.markdown


def test_new_product_sample_naturalizes_dates_units_and_audit_fields() -> None:
    result = render_fastmoss_tool_evidence(_entry("product_rank_new_listed", {
        "items": [{
            "product_id": "1732452731783385673",
            "title": "Portable Fan",
            "launch_date": "2026-06-22",
            "launch_time": 1782105878,
            "commission_rate_percent": 12,
            "current_price": 12.96,
            "currency_code": "USD",
            "first_3d_units_sold": 14300,
            "first_3d_gmv": 1478565,
            "is_cross_border": 1,
            "is_fully_managed": 0,
            "cover_url": "https://example.test/cover.jpg",
        }],
        "total": 1,
    }))
    assert not result.fallback
    assert "2026年6月22日" in result.markdown
    assert "1782105878" not in result.markdown
    assert "12%" in result.markdown and "1200%" not in result.markdown
    assert "上架后前3日销量" in result.markdown
    assert "是否跨境" in result.markdown and "是" in result.markdown
    assert "https://example.test" not in result.markdown
    assert any("cover_url" in path for path in result.exclusion_reasons)


def test_document_keeps_call_order_boundaries_and_stats() -> None:
    dossier = {
        "workflow": "product",
        "report_date": "2026-07-19",
        "target_category_path": [13, 844168, 935176],
        "analysis_targets": [{"entity_type": "product", "entity_id": "1730000000000000001"}],
        "coverage_summary": {
            "call_count": 2,
            "completed_pages": [1, 2],
            "exact_empty_results": [],
        },
        "tool_evidence": [
            _entry("product_search", _fixture(PROFILE_RECORDS)),
            _entry("product_sales_trend", _fixture(PROFILE_TREND)),
        ],
        "hard_fact_boundaries": {"rules": ["跨实体或跨周期数据不得直接相除"]},
    }
    rendered = render_fastmoss_evidence_document(dossier)
    assert rendered.markdown.startswith("# 短视频电商调研证据")
    assert rendered.markdown.index("商品搜索样本") < rendered.markdown.index("商品销售趋势")
    assert "fastmoss__" not in rendered.markdown
    assert "call:1" not in rendered.markdown
    assert "硬事实边界" in rendered.markdown
    assert "商品研究" in rendered.markdown
    assert "分析目标" in rendered.markdown
    assert "研究对象类型" in rendered.markdown
    assert "调用总数" in rendered.markdown
    assert "已完成页码" in rendered.markdown
    assert "call count" not in rendered.markdown
    assert "completed pages" not in rendered.markdown
    assert "entity_type" not in rendered.markdown
    assert rendered.stats["tool_count"] == 2

    assert rendered.stats["registered_tool_count"] == 2
    assert rendered.stats["fallback_tools"] == []
    assert rendered.stats["business_leaf_count"] == (
        rendered.stats["consumed_leaf_count"]
        + rendered.stats["unmapped_leaf_count"]
        + rendered.stats["excluded_leaf_count"]
    )


def test_registered_semantic_labels_and_enums_use_plain_chinese() -> None:
    technical_letters = re.compile(r"[A-Za-z]")
    for key, label in _FIELD_LABELS.items():
        assert not technical_letters.search(label), (key, label)
    for field_name, values in _ENUM_VALUE_LABELS.items():
        for raw_value, label in values.items():
            assert not technical_letters.search(label), (field_name, raw_value, label)
    for tool_name, title in FASTMOSS_TOOL_TITLES.items():
        assert not technical_letters.search(title), (tool_name, title)

    sample = render_fastmoss_tool_evidence(_entry("product_rank_new_listed", {
        "items": [{
            "product_id": "1732452731783385673",
            "launch_date": "2026-06-22",
            "launch_time": "1782105878000",
            "currency": "USD",
            "fulfillment": "FBA",
            "gmv": 100,
            "sku_count": 2,
        }],
    }))
    assert "统计日期 | 2026年6月" in sample.markdown
    assert "2026年6月22日" in sample.markdown
    assert "美元" in sample.markdown
    assert "亚马逊物流配送" in sample.markdown
    assert "成交金额" in sample.markdown
    assert "商品规格数量" in sample.markdown
    for token in (
        "2026-06", "2026.06", "month", "USD", "FBA", "GMV", "SKU",
        "Semantic", "JSON", "null", "true", "false",
    ):
        assert token not in sample.markdown, token


if __name__ == "__main__":
    test_catalog_covers_all_current_fastmoss_tools()
    test_all_tools_render_success_without_silent_field_loss()
    test_all_tools_render_empty_and_error_as_scoped_narrative()
    test_unknown_future_tool_is_isolated_without_raw_json_leak()
    test_official_fastmoss_video_fields_render_as_semantic_values()
    test_week_request_keeps_provider_week_without_invented_calendar_boundary()
    test_product_ranking_boundaries_prevent_period_and_causal_overreach()
    test_new_product_sample_naturalizes_dates_units_and_audit_fields()
    test_document_keeps_call_order_boundaries_and_stats()
    test_registered_semantic_labels_and_enums_use_plain_chinese()
    print("FastMoss evidence renderer tests passed")
