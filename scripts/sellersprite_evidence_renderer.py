#!/usr/bin/env python3
"""Deterministic Semantic rendering for SellerSprite MCP responses.

The runtime MCP schema remains the source of truth for request parameters.
This module describes response meaning only: tool capability, business shape,
entity type and a lossless Markdown representation for the report model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from semantic_evidence_renderer import (
    PROFILE_DISTRIBUTION,
    PROFILE_ENTITY,
    PROFILE_GENERIC,
    PROFILE_RECORDS,
    PROFILE_REFERENCE,
    PROFILE_RELATIONSHIP,
    PROFILE_TREND,
    RenderedEvidenceDocument,
    RenderedToolEvidence,
    SemanticToolRenderer,
    ToolRenderSpec,
    business_leaf_paths,
    localize_semantic_value,
)
from json_to_markdown import json_to_markdown


@dataclass(frozen=True)
class SellerSpriteToolSemantic:
    capability: str
    profile: str
    entity_type: str


def _spec(capability: str, profile: str, entity_type: str) -> SellerSpriteToolSemantic:
    return SellerSpriteToolSemantic(capability, profile, entity_type)


SELLERSPRITE_TOOL_SEMANTICS: dict[str, SellerSpriteToolSemantic] = {
    # Keyword and cross-category discovery.
    "aba_research_weekly": _spec("keyword_discovery", PROFILE_RECORDS, "keyword"),
    "aba_research_monthly": _spec("keyword_discovery", PROFILE_RECORDS, "keyword"),
    "keyword_research": _spec("keyword_discovery", PROFILE_RECORDS, "keyword"),
    "keyword_miner": _spec("keyword_discovery", PROFILE_RECORDS, "keyword"),
    "market_research": _spec("market_discovery", PROFILE_RECORDS, "category"),
    "product_node": _spec("category_resolution", PROFILE_REFERENCE, "category"),
    "product_research": _spec("product_discovery", PROFILE_RECORDS, "asin"),
    "competitor_lookup": _spec("product_discovery", PROFILE_RECORDS, "asin"),

    # Keyword and demand trends.
    "keyword_research_trends": _spec("trend_validation", PROFILE_TREND, "keyword"),
    "aba_research_trend": _spec("trend_validation", PROFILE_TREND, "keyword"),
    "google_trend": _spec("trend_validation", PROFILE_TREND, "keyword"),

    # Node-level distributions and concentration structures.
    "market_ebc_distribution": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_price_distribution": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_ratings_count_distribution": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_listing_date_distribution": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_product_demand_trend": _spec("market_validation", PROFILE_TREND, "category"),
    "market_product_concentration": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_brand_concentration": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_listing_trend_distribution": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_research_statistics": _spec("market_validation", PROFILE_ENTITY, "category"),
    "market_rating_distribution": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_seller_country_distribution": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_seller_type_concentration": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),
    "market_seller_concentration": _spec("market_validation", PROFILE_DISTRIBUTION, "category"),

    # ASIN details and time series.
    "asin_detail": _spec("asin_detail", PROFILE_ENTITY, "asin"),
    "asin_detail_with_coupon_trend": _spec("asin_detail", PROFILE_TREND, "asin"),
    "asin_sales_trend": _spec("asin_detail", PROFILE_TREND, "asin"),
    "asin_prediction": _spec("asin_detail", PROFILE_TREND, "asin"),
    "asin_coupon_trend": _spec("asin_detail", PROFILE_TREND, "asin"),
    "keepa_info": _spec("asin_detail", PROFILE_TREND, "asin"),
    "bsr_prediction": _spec("asin_detail", PROFILE_TREND, "category"),
    "review": _spec("asin_review", PROFILE_RECORDS, "review"),

    # Traffic structures and ASIN relationships.
    "traffic_keyword_stat": _spec("asin_traffic", PROFILE_ENTITY, "asin"),
    "traffic_listing_stat": _spec("asin_traffic", PROFILE_DISTRIBUTION, "asin"),
    "traffic_extend": _spec("asin_traffic", PROFILE_RECORDS, "keyword"),
    "keyword_order": _spec("asin_traffic", PROFILE_RECORDS, "keyword"),
    "traffic_source": _spec("asin_traffic", PROFILE_RELATIONSHIP, "asin"),
    "traffic_keyword": _spec("asin_traffic", PROFILE_RECORDS, "keyword"),
    "traffic_listing": _spec("asin_traffic", PROFILE_RELATIONSHIP, "asin"),

    # Trademark reference data.
    "trademark_country_list": _spec("trademark", PROFILE_REFERENCE, "trademark"),
    "trademark_list": _spec("trademark", PROFILE_RECORDS, "trademark"),
    "trademark_stats": _spec("trademark", PROFILE_DISTRIBUTION, "trademark"),
    "trademark_detail": _spec("trademark", PROFILE_ENTITY, "trademark"),
}

SELLERSPRITE_TOOL_CAPABILITIES = {
    name: semantic.capability for name, semantic in SELLERSPRITE_TOOL_SEMANTICS.items()
}
SELLERSPRITE_TOOL_TITLES: dict[str, str] = {
    "aba_research_weekly": "亚马逊品牌分析周度关键词榜",
    "aba_research_monthly": "亚马逊品牌分析月度关键词榜",
    "keyword_research": "关键词研究结果",
    "keyword_miner": "关键词挖掘结果",
    "market_research": "市场研究结果",
    "product_node": "商品类目节点",
    "product_research": "商品研究样本",
    "competitor_lookup": "亚马逊商品竞品样本",
    "keyword_research_trends": "关键词趋势",
    "aba_research_trend": "亚马逊品牌分析关键词趋势",
    "google_trend": "谷歌搜索趋势",
    "market_ebc_distribution": "市场图文详情分布",
    "market_price_distribution": "市场价格分布",
    "market_ratings_count_distribution": "市场评分数量分布",
    "market_listing_date_distribution": "市场上架日期分布",
    "market_product_demand_trend": "市场商品需求趋势",
    "market_product_concentration": "市场商品集中度",
    "market_brand_concentration": "市场品牌集中度",
    "market_listing_trend_distribution": "市场上架趋势分布",
    "market_research_statistics": "市场研究统计",
    "market_rating_distribution": "市场评分分布",
    "market_seller_country_distribution": "市场卖家国家分布",
    "market_seller_type_concentration": "市场卖家类型集中度",
    "market_seller_concentration": "市场卖家集中度",
    "asin_detail": "亚马逊商品详情",
    "asin_detail_with_coupon_trend": "亚马逊商品详情与优惠趋势",
    "asin_sales_trend": "亚马逊商品销量趋势",
    "asin_prediction": "亚马逊商品销量预测",
    "asin_coupon_trend": "亚马逊商品优惠趋势",
    "keepa_info": "商品历史趋势",
    "bsr_prediction": "亚马逊类目销量排名预测",
    "review": "商品评论样本",
    "traffic_keyword_stat": "亚马逊商品关键词流量统计",
    "traffic_listing_stat": "亚马逊商品关联流量统计",
    "traffic_extend": "流量关键词扩展",
    "keyword_order": "关键词自然排名",
    "traffic_source": "亚马逊商品流量来源",
    "traffic_keyword": "亚马逊商品流量关键词",
    "traffic_listing": "亚马逊商品关联流量商品",
    "trademark_country_list": "商标国家参考",
    "trademark_list": "商标检索结果",
    "trademark_stats": "商标统计分布",
    "trademark_detail": "商标详情",
}
SELLERSPRITE_RENDER_SPECS = {
    name: ToolRenderSpec(
        name,
        semantic.profile,
        semantic.entity_type,
        evidence_title=SELLERSPRITE_TOOL_TITLES[name],
        contract_source="sellersprite_api",
    )
    for name, semantic in SELLERSPRITE_TOOL_SEMANTICS.items()
}
SELLERSPRITE_CURRENT_TOOL_NAMES = frozenset(SELLERSPRITE_TOOL_SEMANTICS)

_INTERNAL_CALL_RE = re.compile(r"\bcall:\d+\b")
_INTERNAL_TOOL_RE = re.compile(r"\bsellersprite__([A-Za-z0-9_]+)\b")


def _report_context_value(value: Any) -> Any:
    """Remove audit provenance from shared dossier metadata before report input."""
    if isinstance(value, Mapping):
        return {
            str(key): _report_context_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_report_context_value(item) for item in value]
    if isinstance(value, tuple):
        return [_report_context_value(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace_tool(match: re.Match[str]) -> str:
        tool_name = match.group(1)
        return SELLERSPRITE_TOOL_TITLES.get(tool_name, "内部数据源")

    text = _INTERNAL_CALL_RE.sub("本次证据", value)
    text = _INTERNAL_TOOL_RE.sub(replace_tool, text)
    text = text.replace("source_ref", "对应证据段").replace("arguments", "查询范围")
    for raw, label in {
        "Amazon": "亚马逊",

        "ASIN": "亚马逊商品编号",
        "SellerSprite": "亚马逊数据平台",
    }.items():
        text = re.sub(rf"\b{re.escape(raw)}\b", label, text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def sellersprite_business_payload(value: Any) -> Any:
    """Remove only the documented response envelope, preserving its business payload."""
    if isinstance(value, Mapping) and "data" in value and any(
        key in value for key in ("code", "message", "msg", "success", "status")
    ):
        return value.get("data")
    return value


def sellersprite_semantic_registry_diagnostics(
    runtime_tools: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    runtime_names = {
        str(tool.get("name") or "") for tool in runtime_tools if tool.get("name")
    }
    registered = set(SELLERSPRITE_TOOL_SEMANTICS)
    return {
        "registered": len(registered),
        "runtime": len(runtime_names),
        "missing_semantics": sorted(runtime_names - registered),
        "missing_runtime": sorted(registered - runtime_names),
    }


def _business_data(evidence: Mapping[str, Any]) -> Any:
    if "data" in evidence:
        return sellersprite_business_payload(evidence.get("data"))
    for key in ("products", "items", "results"):
        if key in evidence:
            return {key: evidence.get(key)}
    return None


def render_sellersprite_current_evidence(evidence: Mapping[str, Any]) -> RenderedToolEvidence:
    """Render one current-turn SellerSprite result without dropping fields."""
    full_tool_name = str(evidence.get("tool") or "sellersprite__unknown")
    entry = {
        "source_ref": "current-call",
        "tool_name": full_tool_name,
        "arguments": evidence.get("arguments") if isinstance(evidence.get("arguments"), dict) else {},
        "business_data": _business_data(evidence),
        "evidence_fence": {
            "data_state": evidence.get("data_state"),
            "ok": evidence.get("ok"),
            "enough_data": evidence.get("enough_data"),
        },
        "error": evidence.get("error"),
    }
    return render_sellersprite_tool_evidence(entry)


def render_sellersprite_tool_evidence(entry: Mapping[str, Any]) -> RenderedToolEvidence:
    """Render one normalized SellerSprite report-evidence entry."""
    full_tool_name = str(entry.get("tool_name") or "sellersprite__unknown")
    tool_name = full_tool_name.split("__", 1)[-1]
    if tool_name not in SELLERSPRITE_RENDER_SPECS:
        data = entry.get("business_data")
        paths = business_leaf_paths(data)
        reason = "运行时工具没有登记语义字段契约"
        return RenderedToolEvidence(
            markdown=(
                "## 未登记的业务证据\n\n"
                "该段返回仅保留在审计证据中，未交给报告模型推理；系统已记录缺失契约诊断。"
            ),
            tool_name=tool_name,
            profile=PROFILE_GENERIC,
            node_types=["ContractIsolation"],
            business_leaf_paths=paths,
            excluded_paths=paths,
            exclusion_reasons={path: reason for path in paths},
            diagnostics=[f"{tool_name}: {reason}"],
            fallback=True,
        )
    renderer = SemanticToolRenderer(
        entry,
        render_specs=SELLERSPRITE_RENDER_SPECS,
        strict_contract=True,
    )
    try:
        result = renderer.render()
    except Exception as exc:
        data = entry.get("business_data")
        paths = business_leaf_paths(data)
        spec = SELLERSPRITE_RENDER_SPECS[tool_name]
        reason = f"语义字段契约渲染失败：{type(exc).__name__}"
        return RenderedToolEvidence(
            markdown=(
                f"## {spec.evidence_title}\n\n"
                "该段业务返回未通过已登记的语义字段契约，因此仅保留在审计证据中，"
                "未交给报告模型推理。"
            ),
            tool_name=tool_name,
            profile=spec.profile,
            node_types=["ContractIsolation"],
            business_leaf_paths=paths,
            excluded_paths=paths,
            exclusion_reasons={path: reason for path in paths},
            diagnostics=[f"{tool_name}: {type(exc).__name__}: {exc}"],
            fallback=True,
        )
    return result


def render_sellersprite_evidence_document(
    dossier: Mapping[str, Any],
) -> RenderedEvidenceDocument:
    """Render a complete SellerSprite dossier while isolating per-call failures."""
    lines = ["# 亚马逊调研证据"]
    context = {
        "report_date": dossier.get("report_date"),
        "research_task": dossier.get("research_task") or {},
        "quality_summary": dossier.get("quality_summary") or {},
    }
    lines.extend([
        "",
        json_to_markdown(
            localize_semantic_value(_report_context_value(context)),
            title="调研上下文",
            include_paths=False,
        ).rstrip(),
    ])
    results: list[RenderedToolEvidence] = []
    for entry in dossier.get("tool_evidence") or []:
        if not isinstance(entry, Mapping):
            continue
        result = render_sellersprite_tool_evidence(entry)
        results.append(result)
        lines.extend(["", result.markdown])
    boundaries = dossier.get("hard_fact_boundaries")
    if boundaries:
        lines.extend([
            "",
            json_to_markdown(
                localize_semantic_value(_report_context_value(boundaries)),
                title="硬事实边界",
                include_paths=False,
            ).rstrip(),
        ])
    return RenderedEvidenceDocument(
        markdown="\n".join(lines).rstrip() + "\n",
        tool_results=results,
    )


__all__ = [
    "SELLERSPRITE_CURRENT_TOOL_NAMES",
    "SELLERSPRITE_RENDER_SPECS",
    "SELLERSPRITE_TOOL_CAPABILITIES",
    "SELLERSPRITE_TOOL_SEMANTICS",
    "SELLERSPRITE_TOOL_TITLES",
    "SellerSpriteToolSemantic",
    "render_sellersprite_current_evidence",
    "render_sellersprite_tool_evidence",
    "render_sellersprite_evidence_document",
    "sellersprite_business_payload",
    "sellersprite_semantic_registry_diagnostics",
]
