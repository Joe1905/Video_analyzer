"""Provider-aware research task routing and capability planning helpers.

This module deliberately stops at task semantics and capability eligibility.
Runtime MCP schemas own request validation; provider renderers own evidence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from sellersprite_evidence_renderer import SELLERSPRITE_TOOL_CAPABILITIES


DISCOVERY_BREADTH = 3
PROVIDER_CALL_BUDGET = 12

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
    "none", "category", "keyword", "product", "product_id", "shop",
    "creator", "video", "asin",
}
RESEARCH_ENTITY_SOURCES = {"none", "explicit", "inherited", "evidence"}


_ASIN_RE = re.compile(r"\b(?:B0[A-Z0-9]{8}|[0-9]{9}[0-9X])\b", re.IGNORECASE)
_FASTMOSS_PRODUCT_ID_RE = re.compile(r"\b\d{16,20}\b")
_TIME_WINDOW_RE = re.compile(
    r"(?:最近|近|过去)?\s*\d+\s*(?:[-—~到至]\s*\d+\s*)?(?:天|周|个月|月|年)",
    re.IGNORECASE,
)
_TASK_ONLY_PARTS = re.compile(
    r"(?i)fastmoss|seller\s*sprite|sellersprite|amazon|tiktok\s*shop|tiktok|"
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


def normalize_research_entity(text: str) -> str:
    """Return a real entity phrase, never a bare research objective."""
    value = re.sub(r"\s+", " ", str(text or "")).strip(" \t\r\n,，。;；:：")
    if not value:
        return ""
    asin = _ASIN_RE.search(value)
    if asin:
        return asin.group(0).upper()
    product_id = _FASTMOSS_PRODUCT_ID_RE.search(value)
    if product_id:
        return product_id.group(0)
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
    """Build one stable internal task profile from rules plus classifier hints."""
    route = route or {}
    decision = decision or {}
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
    entity = normalize_research_entity(candidate_entity)
    if not entity:
        entity = normalize_research_entity(text)
    if explicit_cross_category:
        entity = ""

    entity_type_hint = str(hint.get("entity_type") or "").strip().lower()
    if _ASIN_RE.fullmatch(entity):
        entity_type = "asin"
    elif _FASTMOSS_PRODUCT_ID_RE.fullmatch(entity):
        entity_type = "product_id"
    elif re.match(r"https?://", entity, re.IGNORECASE):
        entity_type = "product"
    elif entity and entity_type_hint in RESEARCH_ENTITY_TYPES - {"none"}:
        entity_type = entity_type_hint
    elif entity:
        entity_type = "keyword"
    else:
        entity_type = "none"

    scope_hint = str(hint.get("scope") or "").strip().lower()
    if entity_type in {"asin", "product_id", "product", "shop", "creator", "video"}:
        scope = "entity"
    elif entity_type == "category":
        scope = "category"
    elif entity_type == "keyword":
        scope = "keyword"
    elif explicit_cross_category or objective in {"trend_discovery", "opportunity_discovery"}:
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
        "discovery_breadth": DISCOVERY_BREADTH,
    }


FASTMOSS_TOOL_CAPABILITIES: dict[str, str] = {}


def _register_fastmoss(capability: str, names: Iterable[str]) -> None:
    FASTMOSS_TOOL_CAPABILITIES.update({name: capability for name in names})


_register_fastmoss("category_resolution", ("search_category_by_words", "product_category_info"))
_register_fastmoss("category_discovery", ("market_category_ranking",))
_register_fastmoss("category_context", ("market_category_analysis", "market_category_author_sales_matrix"))
_register_fastmoss("product_discovery", ("product_rank_new_listed", "product_rank_top_selling", "product_search"))
_register_fastmoss("product_detail", ("product_detail_info", "product_overview", "product_sku"))
_register_fastmoss("product_trend", ("product_investment", "product_sales_trend"))
_register_fastmoss("product_content", ("product_creator_analysis", "product_review_list", "product_video_list"))
_register_fastmoss("shop_discovery", ("shop_rank_top_selling", "shop_search"))
_register_fastmoss("shop_detail", ("shop_base_info", "shop_data_trends", "shop_investment_analysis"))
_register_fastmoss("shop_analysis", ("shop_creator_analysis", "shop_live_analysis", "shop_product_analysis", "shop_sale_analysis", "shop_video_analysis"))
_register_fastmoss("creator_discovery", ("creator_rank_top_ecommerce", "creator_rank_top_growth", "creator_rank_top_potential", "creator_search"))
_register_fastmoss("creator_detail", ("creator_profile_overview", "creator_data_trends", "creator_cargo_summary", "creator_fans_distribution"))
_register_fastmoss("creator_content", ("creator_product_list", "creator_video_analysis"))
_register_fastmoss("video_discovery", ("video_search", "ad_search"))
_register_fastmoss("video_detail", ("video_detail_analysis", "video_data_trends", "video_script_info", "ad_data_overview"))
_register_fastmoss("live_discovery", ("live_search",))
_register_fastmoss("live_detail", ("live_detail_analysis", "live_products_list"))
_register_fastmoss("agency_discovery", ("agency_rank_top", "agency_search"))
_register_fastmoss("agency_detail", ("agency_profile_overview", "agency_creator_analysis", "agency_product_analysis", "agency_product_list", "agency_shop_analysis"))
_register_fastmoss("reference", ("fastmoss_detail_url_examples", "search_fastmoss_documents"))


def provider_tool_capability(provider: str, tool_name: str) -> str:
    name = str(tool_name or "").split("__", 1)[-1]
    if provider == "amazon":
        return SELLERSPRITE_TOOL_CAPABILITIES.get(name, "unknown")
    if provider == "fastmoss":
        return FASTMOSS_TOOL_CAPABILITIES.get(name, "unknown")
    return "unknown"


def eligible_provider_capabilities(provider: str, task: dict[str, Any], state: dict[str, Any]) -> set[str]:
    """Return legal capability nodes; the LLM still chooses the next tool."""
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

    if provider == "fastmoss":
        if objective == "shop" or entity_type == "shop":
            return {"shop_discovery"} | ({"shop_detail", "shop_analysis"} if has_shop else set())
        if objective == "creator" or entity_type == "creator":
            return {"creator_discovery"} | ({"creator_detail", "creator_content"} if has_creator else set())
        if objective == "content" or entity_type == "video":
            capabilities = {"video_discovery", "product_discovery"}
            if has_product:
                capabilities.add("product_content")
            if has_video:
                capabilities.add("video_detail")
            return capabilities
        if entity_type in {"product", "product_id"} or has_product:
            return {"product_detail", "product_trend", "product_content"}
        if scope == "cross_category" and objective in {"trend_discovery", "opportunity_discovery"}:
            if "category_discovery" not in attempted:
                return {"category_discovery"}
            capabilities = {"category_discovery", "category_resolution"}
            if has_category:
                capabilities.update({"category_context", "product_discovery"})
            if has_product:
                capabilities.update({"product_detail", "product_trend", "product_content"})
            return capabilities
        capabilities = {"category_resolution"}
        if has_category:
            capabilities.update({"category_discovery", "category_context", "product_discovery"})
        if has_product:
            capabilities.update({"product_detail", "product_trend", "product_content"})
        return capabilities

    if provider == "amazon":
        if entity_type == "asin" or state.get("has_asin"):
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
    elif provider == "fastmoss":
        mapping = FASTMOSS_TOOL_CAPABILITIES
    else:
        return set()
    counts = state.get("tool_counts") if isinstance(state.get("tool_counts"), dict) else {}
    if sum(int(value or 0) for value in counts.values()) >= PROVIDER_CALL_BUDGET:
        return set()
    limits = {
        # Two broad ranking views plus up to three evidence-backed candidate
        # drill-downs fit the default cross-category discovery breadth.
        "market_category_ranking": DISCOVERY_BREADTH + 2,
        "search_category_by_words": DISCOVERY_BREADTH,
        "product_rank_new_listed": DISCOVERY_BREADTH,
        "product_rank_top_selling": DISCOVERY_BREADTH,
        "product_search": DISCOVERY_BREADTH,
        "keyword_research": DISCOVERY_BREADTH,
        "product_research": DISCOVERY_BREADTH,
    }
    return {
        name for name, capability in mapping.items()
        if capability in capabilities and int(counts.get(name) or 0) < limits.get(
            name,
            DISCOVERY_BREADTH
            if task.get("scope") == "cross_category" and capability in {
                "keyword_discovery", "market_discovery", "product_discovery", "category_resolution",
                "category_context", "product_detail", "product_trend", "product_content",
                "trend_validation", "market_validation", "asin_detail", "asin_traffic", "asin_review",
            }
            else 1,
        )
        and not (provider == "fastmoss" and name == "product_category_info" and not state.get("has_product"))
    }
