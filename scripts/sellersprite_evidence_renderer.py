#!/usr/bin/env python3
"""Deterministic Semantic rendering for SellerSprite MCP responses.

The runtime MCP schema remains the source of truth for request parameters.
This module describes response meaning only: tool capability, business shape,
entity type and a lossless Markdown representation for the report model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from fastmoss_evidence_renderer import (
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
SELLERSPRITE_RENDER_SPECS = {
    name: ToolRenderSpec(name, semantic.profile, semantic.entity_type)
    for name, semantic in SELLERSPRITE_TOOL_SEMANTICS.items()
}
SELLERSPRITE_CURRENT_TOOL_NAMES = frozenset(SELLERSPRITE_TOOL_SEMANTICS)


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
    renderer = SemanticToolRenderer(entry, render_specs=SELLERSPRITE_RENDER_SPECS)
    try:
        result = renderer.render()
    except Exception:
        tool_name = full_tool_name.split("__", 1)[-1]
        data = entry["business_data"]
        paths = business_leaf_paths(data)
        return RenderedToolEvidence(
            markdown=json_to_markdown(
                dict(entry),
                title=f"current-call · {full_tool_name}",
                include_paths=True,
            ).rstrip(),
            tool_name=tool_name,
            profile=SELLERSPRITE_RENDER_SPECS.get(
                tool_name, ToolRenderSpec(tool_name, PROFILE_GENERIC, "reference")
            ).profile,
            node_types=["GenericFallback"],
            business_leaf_paths=paths,
            unmapped_paths=paths,
            fallback=True,
        )
    return result


def render_sellersprite_evidence_document(
    dossier: Mapping[str, Any],
) -> RenderedEvidenceDocument:
    """Render a complete SellerSprite dossier while isolating per-call failures."""
    lines = ["# SellerSprite 调研证据"]
    context = {
        "report_date": dossier.get("report_date"),
        "research_task": dossier.get("research_task") or {},
        "quality_summary": dossier.get("quality_summary") or {},
    }
    lines.extend(["", json_to_markdown(context, title="调研上下文", include_paths=False).rstrip()])
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
            json_to_markdown(boundaries, title="硬事实边界", include_paths=False).rstrip(),
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
    "SellerSpriteToolSemantic",
    "render_sellersprite_current_evidence",
    "render_sellersprite_tool_evidence",
    "render_sellersprite_evidence_document",
    "sellersprite_business_payload",
    "sellersprite_semantic_registry_diagnostics",
]
