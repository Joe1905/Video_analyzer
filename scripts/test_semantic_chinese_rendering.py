#!/usr/bin/env python3
"""双站点报告 Semantic 中文化回归。"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fastmoss_evidence_renderer import (  # noqa: E402
    FASTMOSS_CURRENT_TOOL_NAMES,
    FASTMOSS_TOOL_TITLES,
    render_fastmoss_tool_evidence,
)
from sellersprite_evidence_renderer import (  # noqa: E402
    SELLERSPRITE_CURRENT_TOOL_NAMES,
    SELLERSPRITE_TOOL_TITLES,
    render_sellersprite_evidence_document,
    render_sellersprite_tool_evidence,
)


TECHNICAL_TOKENS = (
    "fastmoss__",
    "sellersprite__",
    "call:",
    "$.business_data",
    "date_type",
    "date_value",
    "fba_fee",
    "main_seller",
    "rank_growth_rate",
    "brand_crn",
    "goods_crn",
    "fba_proportion",
    "null",
    "true",
    "false",
)


def _assert_report_clean(markdown: str) -> None:
    lowered = markdown.lower()
    for token in TECHNICAL_TOKENS:
        assert token not in lowered, token
    assert not re.search(r"(?<!\d)\d{10}(?:\d{3})?(?!\d)", markdown), markdown


def test_all_registered_tools_use_chinese_business_titles() -> None:
    assert len(FASTMOSS_CURRENT_TOOL_NAMES) == 54
    assert len(SELLERSPRITE_CURRENT_TOOL_NAMES) == 43
    technical_letters = re.compile(r"[A-Za-z_]")
    for title in [*FASTMOSS_TOOL_TITLES.values(), *SELLERSPRITE_TOOL_TITLES.values()]:
        assert not technical_letters.search(title), title

    for provider, names, renderer in (
        ("fastmoss", FASTMOSS_CURRENT_TOOL_NAMES, render_fastmoss_tool_evidence),
        ("sellersprite", SELLERSPRITE_CURRENT_TOOL_NAMES, render_sellersprite_tool_evidence),
    ):
        for tool_name in sorted(names):
            entry = {
                "source_ref": "call:1",
                "tool_name": f"{provider}__{tool_name}",
                "arguments": {
                    "request": {
                        "region": "US",
                        "date_type": "month",
                        "date_value": "2026-06",
                    }
                },
                "business_data": {
                    "items": [{
                        "product_id": "123456789",
                        "title": "测试商品",
                        "sales": 20,
                        "is_cross_border": True,
                    }]
                },
                "evidence_fence": {"data_state": "data"},
            }
            result = renderer(entry)
            _assert_report_clean(result.markdown)


def test_sellersprite_problem_fields_and_time_are_naturalized() -> None:
    entry = {
        "source_ref": "call:20",
        "tool_name": "sellersprite__product_research",
        "arguments": {
            "request": {
                "marketplace": "US",
                "date_type": "month",
                "date_value": "2026-06",
            }
        },
        "business_data": {
            "items": [{
                "asin": "B0CZT19JQ1",
                "title": "测试商品",
                "department": "家居",
                "fbaFee": 3.2,
                "mainSeller": "测试卖家",
                "isMainSeller": True,
                "rankGrowthRate": 12.5,
                "brands": 32,
                "brandCrn": 48.5,
                "goodsCrn": 36.2,
                "fbaProportion": 72.1,
                "availableDate": 1715084401000,
                "optionalUnknown": None,
            }]
        },
        "evidence_fence": {"data_state": "data"},
    }
    original = copy.deepcopy(entry)
    result = render_sellersprite_tool_evidence(entry)

    for expected in (
        "美国站",
        "统计周期类型",
        "2026年6月",
        "所属类目",
        "亚马逊物流费用",
        "主要卖家",
        "是否主要卖家",
        "排名增长率",
        "品牌数量",
        "头部品牌集中度",
        "头部商品集中度",
        "亚马逊物流商品占比",
        "2024年5月7日",
    ):
        assert expected in result.markdown, expected
    assert "optionalUnknown" not in result.markdown
    assert result.excluded_paths
    assert entry == original
    _assert_report_clean(result.markdown)


def test_fastmoss_live_category_fields_are_naturalized() -> None:
    category = render_fastmoss_tool_evidence({
        "source_ref": "call:1",
        "tool_name": "fastmoss__product_category_info",
        "arguments": {"filter": {"region": "US"}},
        "business_data": {
            "c_code": "10",
            "c_name": "家居用品",
            "sub": [{"c_code": "1001", "c_name": "节庆用品"}],
        },
        "evidence_fence": {"data_state": "data"},
    })
    market = render_fastmoss_tool_evidence({
        "source_ref": "call:2",
        "tool_name": "fastmoss__market_category_analysis",
        "arguments": {"filter": {"region": "US", "date_value": "2026-06"}},
        "business_data": {
            "category_gmv": 117935594.78,
            "category_gmv_mom_percent": 0,
            "category_units_sold_mom_percent": 0,
            "active_product_count_change": -3829,
            "product_count_change": 7212,
            "selling_creator_count_change": 0,
            "selling_live_count_change": 1829,
            "selling_video_count_change": -45123,
            "top_products_gmv_share": 0.25,
            "top_shops_gmv_share": 0.4,
            "top_products_summary": {
                "average_affiliate_count": 3060,
                "average_live_count": 944,
                "average_video_count": 2750,
            },
            "stat_date": "2026-06",
        },
        "evidence_fence": {"data_state": "data"},
    })
    for result in (category, market):
        assert not result.unmapped_paths
        assert not result.excluded_paths
        _assert_report_clean(result.markdown)
    for expected in ("类目编码", "类目名称", "下级类目"):
        assert expected in category.markdown
    for expected in (
        "类目成交金额",
        "类目成交金额环比增长率",
        "头部商品成交金额占比",
        "25%",
        "头部店铺成交金额占比",
        "40%",
        "平均关联达人数",
        "2026年6月",
    ):
        assert expected in market.markdown, expected


def test_report_document_hides_audit_provenance() -> None:
    dossier = {
        "report_date": "2026-07-27",
        "research_task": {
            "objective": "product_research",
            "entity_type": "asin",
            "entity": "B0CZT19JQ1",
            "time_window": "recent_1_2_months",
        },
        "quality_summary": {"data_call_count": 1, "error_call_count": 0},
        "tool_evidence": [{
            "source_ref": "call:1",
            "tool_name": "sellersprite__asin_detail",
            "arguments": {"request": {"marketplace": "US", "asin": "B0CZT19JQ1"}},
            "business_data": {
                "asin": "B0CZT19JQ1",
                "availableDate": 1715084401000,
                "isMainSeller": False,
            },
            "evidence_fence": {"data_state": "data"},
        }],
        "hard_fact_boundaries": {
            "rules": [
                "空结果只适用于 call:1 的 arguments，不能外推 Amazon 全站。",
            ]
        },
    }
    original = copy.deepcopy(dossier)
    rendered = render_sellersprite_evidence_document(dossier)
    assert dossier == original
    assert "亚马逊商品详情" in rendered.markdown
    assert "亚马逊全站" in rendered.markdown
    _assert_report_clean(rendered.markdown)


if __name__ == "__main__":
    test_all_registered_tools_use_chinese_business_titles()
    test_sellersprite_problem_fields_and_time_are_naturalized()
    test_fastmoss_live_category_fields_are_naturalized()
    test_report_document_hides_audit_provenance()
    print("双站点 Semantic 中文化测试通过")
