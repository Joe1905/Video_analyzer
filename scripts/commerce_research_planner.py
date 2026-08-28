"""Provider-aware research task routing and capability planning helpers.

This module deliberately stops at task semantics and capability eligibility.
Runtime MCP schemas own request validation; provider renderers own evidence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from sellersprite_evidence_renderer import SELLERSPRITE_TOOL_CAPABILITIES


RESEARCH_OBJECTIVES = {
    "lookup",
    "entity_analysis",
    "compare",
    "opportunity_discovery",
    "trend_discovery",
    "pricing",
    "content",
    "creator",
    "shop",
}
RESEARCH_SCOPES = {"cross_category", "category", "keyword", "entity"}
RESEARCH_ENTITY_TYPES = {
    "none", "category", "keyword", "product", "shop",
    "creator", "video", "asin",
}
RESEARCH_ENTITY_SOURCES = {"none", "explicit", "inherited", "evidence"}


_ASIN_RE = re.compile(r"\b(?:B0[A-Z0-9]{8}|[0-9]{9}[0-9X])\b", re.IGNORECASE)
_TIME_WINDOW_RE = re.compile(
    r"(?:最近|近|过去)?\s*\d+\s*(?:[-—~到至]\s*\d+\s*)?(?:天|周|个月|月|年)",
    re.IGNORECASE,
)
_TASK_ONLY_PARTS = re.compile(
    r"(?i)seller\s*sprite|sellersprite|amazon|tiktok\s*shop|tiktok|"
    r"亚马逊|卖家精灵|美区|美国|帮我|给我|请|查找|寻找|找|搜索|查询|看看|一下|分析|调研|研究|报告|"
    r"最近|过去|近|热门|热销|趋势|新品|新产品|潜力|蓝海|机会|选品|产品|商品|品类|类目|"
    r"需求大|卖家少|竞争小|等等|情况|方向|推荐|这个|这款|该|但|的|和|与|、|，|。|；|：|\s+"
)


def _contains_any(text: str, words: Iterable[str]) -> bool:
    lowered = str(text or "").lower()
    return any(word in lowered for word in words)


def extract_time_window(text: str) -> str:
    matches = [re.sub(r"\s+", "", item) for item in _TIME_WINDOW_RE.findall(str(text or ""))]
    return matches[0] if matches else ""


def structured_research_entity(text: str) -> str:
    """Extract only identifiers whose meaning does not depend on language heuristics."""
    value = str(text or "")
    asin = _ASIN_RE.search(value)
    if asin:
        return asin.group(0).upper()
    urls = re.findall(r"https?://\S+", value, re.IGNORECASE)
    return urls[0] if urls else ""


def validate_research_task_hint(decision: dict[str, Any] | None) -> dict[str, str] | None:
    """Validate the classifier contract without reinterpreting its semantics."""
    if not isinstance(decision, dict) or not isinstance(decision.get("research_task"), dict):
        return None
    hint = decision["research_task"]
    required = {
        "objective", "scope", "entity_type", "entity", "entity_source", "region", "time_window",
    }
    if not required.issubset(hint):
        return None
    objective = str(hint.get("objective") or "").strip().lower()
    scope = str(hint.get("scope") or "").strip().lower()
    entity_type = str(hint.get("entity_type") or "").strip().lower()
    entity = re.sub(r"\s+", " ", str(hint.get("entity") or "")).strip()[:200]
    entity_source = str(hint.get("entity_source") or "").strip().lower()
    region = str(hint.get("region") or "").strip().upper()
    time_window = str(hint.get("time_window") or "").strip()[:80]
    if (
        objective not in RESEARCH_OBJECTIVES
        or scope not in RESEARCH_SCOPES
        or entity_type not in RESEARCH_ENTITY_TYPES
        or entity_source not in RESEARCH_ENTITY_SOURCES
        or (region and not re.fullmatch(r"[A-Z]{2}|GLOBAL", region))
    ):
        return None
    if entity_type == "none":
        if entity or entity_source != "none" or scope != "cross_category":
            return None
    else:
        if not entity or entity_source == "none":
            return None
        expected_scope = (
            "category" if entity_type == "category"
            else "keyword" if entity_type == "keyword"
            else "entity"
        )
        if scope != expected_scope:
            return None
    if entity_type == "asin" and not _ASIN_RE.fullmatch(entity):
        return None
    return {
        "objective": objective,
        "scope": scope,
        "entity_type": entity_type,
        "entity": entity,
        "entity_source": entity_source,
        "region": region,
        "time_window": time_window,
    }


def normalize_research_entity(text: str) -> str:
    """Return a real entity phrase, never a bare research objective."""
    value = re.sub(r"\s+", " ", str(text or "")).strip(" \t\r\n,，。;；:：")
    if not value:
        return ""
    asin = _ASIN_RE.search(value)
    if asin:
        return asin.group(0).upper()
    urls = re.findall(r"https?://\S+", value, re.IGNORECASE)
    if urls:
        return urls[0]
    without_time = _TIME_WINDOW_RE.sub(" ", value)
    remainder = _TASK_ONLY_PARTS.sub(" ", without_time)
    remainder = re.sub(r"\d+\s*(?:[-—~到至]\s*\d+\s*)?", " ", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip(" -_/|,，。;；:：")
    return remainder if len(re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", remainder)) >= 2 else ""


def research_task_from(
    user_text: str,
    provider: str,
    route: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one task profile while preserving a validated classifier decision verbatim."""
    route = route or {}
    decision = decision or {}
    route_source = str(route.get("route_source") or "")
    authoritative_hint = validate_research_task_hint(decision) if route_source == "llm" else None
    if authoritative_hint is not None:
        result = dict(authoritative_hint)
        result["region"] = result["region"] or str(route.get("region") or "").strip().upper()
        result["time_window"] = result["time_window"] or extract_time_window(user_text)
        return result

    hint = decision.get("research_task") if isinstance(decision.get("research_task"), dict) else decision
    text = str(user_text or "")
    lowered = text.lower()
    explicit_cross_category = _contains_any(
        lowered,
        ("跨品类", "跨类目", "全品类", "不限品类", "不限类目", "cross-category", "cross category"),
    )
    playbook = str(route.get("playbook") or "")

    objective_hint = str(hint.get("objective") or "").strip().lower()
    if objective_hint in RESEARCH_OBJECTIVES:
        objective = objective_hint
    elif _contains_any(lowered, ("趋势新品", "热门新品", "新品趋势", "trending product", "hot new product")) or (
        _contains_any(lowered, ("趋势", "热门", "trend", "trending", "hot"))
        and _contains_any(lowered, ("新品", "新产品", "new product", "newly listed"))
    ):
        objective = "trend_discovery"
    elif _contains_any(lowered, ("蓝海", "潜力", "机会", "值得做", "选品", "opportunity", "what to sell")):
        objective = "opportunity_discovery"
    elif playbook == "pricing" or _contains_any(lowered, ("定价", "价格带", "售价", "pricing", "price band")):
        objective = "pricing"
    elif playbook in {"content_dissect", "content_strategy"}:
        objective = "content"
    elif playbook == "creator":
        objective = "creator"
    elif playbook == "shop":
        objective = "shop"
    elif playbook == "competitor" or _contains_any(lowered, ("对比", "比较", "竞品", "competitor", "compare")):
        objective = "compare"
    elif str(route.get("task_depth") or "") in {"analysis", "workflow"}:
        objective = "entity_analysis"
    else:
        objective = "lookup"

    candidate_entity = str(hint.get("entity") or route.get("entity") or "")
    if route_source == "rules_fallback":
        entity = structured_research_entity(candidate_entity) or structured_research_entity(text)
    else:
        entity = normalize_research_entity(candidate_entity)
        if not entity:
            entity = normalize_research_entity(text)
    if explicit_cross_category:
        entity = ""

    entity_type_hint = str(hint.get("entity_type") or "").strip().lower()
    if _ASIN_RE.fullmatch(entity):
        entity_type = "asin"
    elif re.match(r"https?://", entity, re.IGNORECASE):
        entity_type = "product"
    elif entity and entity_type_hint in RESEARCH_ENTITY_TYPES - {"none"}:
        entity_type = entity_type_hint
    elif entity:
        entity_type = "keyword"
    else:
        entity_type = "none"

    scope_hint = str(hint.get("scope") or "").strip().lower()
    if entity_type in {"asin", "product", "shop", "creator", "video"}:
        scope = "entity"
    elif entity_type == "category":
        scope = "category"
    elif entity_type == "keyword":
        scope = "keyword"
    elif explicit_cross_category or objective in {"trend_discovery", "opportunity_discovery"}:
        scope = "cross_category"
    elif route_source == "rules_fallback":
        scope = "cross_category"
    elif scope_hint in RESEARCH_SCOPES:
        scope = scope_hint
    else:
        scope = "keyword"

    entity_source_hint = str(hint.get("entity_source") or "").strip().lower()
    entity_source = entity_source_hint if entity_source_hint in RESEARCH_ENTITY_SOURCES else ("explicit" if entity else "none")
    if not entity:
        entity_source = "none"

    region = str(hint.get("region") or route.get("region") or "").strip().upper()
    time_window = str(hint.get("time_window") or extract_time_window(text)).strip()[:80]
    return {
        "objective": objective,
        "scope": scope,
        "entity_type": entity_type,
        "entity": entity,
        "entity_source": entity_source,
        "region": region,
        "time_window": time_window,
    }


def provider_tool_capability(provider: str, tool_name: str) -> str:
    name = str(tool_name or "").split("__", 1)[-1]
    if provider == "amazon":
        return SELLERSPRITE_TOOL_CAPABILITIES.get(name, "unknown")
    return "unknown"


def eligible_provider_capabilities(provider: str, task: dict[str, Any], state: dict[str, Any]) -> set[str]:
    """Return advisory capability nodes; legacy rule routes may still use them for staging."""
    objective = str(task.get("objective") or "lookup")
    scope = str(task.get("scope") or "keyword")
    entity_type = str(task.get("entity_type") or "none")
    attempted = set(state.get("attempted_capabilities") or [])
    observed = set(state.get("observed_capabilities") or [])
    has_category = bool(state.get("has_category"))
    has_product = bool(state.get("has_product"))
    has_shop = bool(state.get("has_shop"))
    has_creator = bool(state.get("has_creator"))
    has_video = bool(state.get("has_video"))

    if provider == "amazon":
        if entity_type == "asin":
            return {"asin_detail", "asin_traffic", "asin_review"}
        if scope == "cross_category" and objective in {"trend_discovery", "opportunity_discovery"}:
            capabilities = set()
            if "keyword_discovery" not in observed:
                capabilities.add("keyword_discovery")
            if "market_discovery" not in observed:
                capabilities.add("market_discovery")
            if observed.intersection({"keyword_discovery", "market_discovery"}):
                capabilities.update({"trend_validation", "product_discovery", "category_resolution"})
            if state.get("has_node"):
                capabilities.add("market_validation")
            if state.get("has_asin"):
                capabilities.update({"asin_detail", "asin_traffic", "asin_review"})
            return capabilities
        capabilities = {"keyword_discovery", "product_discovery", "category_resolution"}
        if entity_type in {"keyword", "category"}:
            capabilities.add("trend_validation")
        if state.get("has_node") or "category_resolution" in observed:
            capabilities.add("market_validation")
        if state.get("has_asin"):
            capabilities.update({"asin_detail", "asin_traffic", "asin_review"})
        return capabilities
    return set()


def eligible_provider_tool_names(provider: str, task: dict[str, Any], state: dict[str, Any]) -> set[str]:
    capabilities = eligible_provider_capabilities(provider, task, state)
    if provider == "amazon":
        mapping = SELLERSPRITE_TOOL_CAPABILITIES
    else:
        return set()
    return {
        name for name, capability in mapping.items()
        if capability in capabilities
    }
