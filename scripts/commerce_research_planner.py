"""Provider-aware research task routing and capability planning helpers.

This module deliberately stops at task semantics, capability eligibility and
request-shape validation.  Tool evidence and report writing remain owned by the
existing chat pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


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


@dataclass(frozen=True)
class SellerSpriteRequestSpec:
    capability: str
    request_format: str
    required_fields: tuple[str, ...] = ()


def _seller_specs(
    capability: str,
    request_format: str,
    required_fields: tuple[str, ...],
    names: Iterable[str],
) -> dict[str, SellerSpriteRequestSpec]:
    return {
        name: SellerSpriteRequestSpec(capability, request_format, required_fields)
        for name in names
    }


SELLERSPRITE_TOOL_REQUEST_SPECS: dict[str, SellerSpriteRequestSpec] = {}
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "market_validation", "nested", ("marketplace", "nodeIdPath"), (
        "market_ebc_distribution", "market_price_distribution", "market_ratings_count_distribution",
        "market_listing_date_distribution", "market_product_demand_trend", "market_product_concentration",
        "market_brand_concentration", "market_listing_trend_distribution", "market_research_statistics",
        "market_rating_distribution", "market_seller_country_distribution",
        "market_seller_type_concentration", "market_seller_concentration",
    ),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_traffic", "flat", ("marketplace", "asin"),
    ("traffic_keyword_stat", "traffic_listing_stat"),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_traffic", "nested", ("asinList", "marketplace", "queryType"), ("traffic_extend",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_review", "flat", ("marketplace", "asin"), ("review",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "trademark", "nested", ("text",), ("trademark_list",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "trend_validation", "nested", ("marketplace",), ("google_trend",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_detail", "flat", ("marketplace", "asin"), (
        "asin_prediction", "asin_coupon_trend", "keepa_info", "asin_detail",
        "asin_sales_trend", "asin_detail_with_coupon_trend",
    ),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "keyword_discovery", "nested", ("marketplace",),
    ("aba_research_weekly", "aba_research_monthly", "keyword_research", "keyword_miner"),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_traffic", "nested", ("asins", "date", "marketplace", "reverseType"), ("keyword_order",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "trademark", "nested", ("office", "text"), ("trademark_stats",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "trademark", "flat", ("office", "brandId"), ("trademark_detail",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "trend_validation", "flat", ("marketplace", "keyword"),
    ("keyword_research_trends", "aba_research_trend"),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "category_resolution", "nested", ("marketplace",), ("product_node",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "product_discovery", "nested", ("marketplace",), ("product_research", "competitor_lookup"),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_traffic", "nested", ("marketplace", "q"), ("traffic_source",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_traffic", "nested", ("asin", "marketplace"), ("traffic_keyword",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "market_discovery", "nested", ("marketplace",), ("market_research",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_traffic", "nested", ("asinList", "marketplace", "relations"), ("traffic_listing",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS.update(_seller_specs(
    "asin_detail", "flat", ("marketplace", "bsr", "categoryId"), ("bsr_prediction",),
))
SELLERSPRITE_TOOL_REQUEST_SPECS["trademark_country_list"] = SellerSpriteRequestSpec("trademark", "none")


def normalize_sellersprite_registered_arguments(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Normalize the documented SellerSprite request envelope before runtime-schema validation."""
    spec = SELLERSPRITE_TOOL_REQUEST_SPECS.get(str(name or ""))
    if spec is None:
        return dict(args or {}), "unregistered SellerSprite tool; deferred to runtime schema"
    normalized = dict(args or {})
    action: str | None = None
    if spec.request_format == "nested":
        if not isinstance(normalized.get("request"), dict):
            normalized = {"request": normalized}
            action = "wrapped SellerSprite arguments in registered request object"
        request = normalized.get("request") if isinstance(normalized.get("request"), dict) else {}
    elif spec.request_format == "flat":
        if set(normalized) == {"request"} and isinstance(normalized.get("request"), dict):
            normalized = dict(normalized["request"])
            action = "unwrapped SellerSprite arguments for registered flat request"
        request = normalized
    else:
        request = normalized
    missing = [field for field in spec.required_fields if request.get(field) is None]
    if missing:
        raise ValueError(f"Invalid registered arguments for {name}: missing required field(s): {', '.join(missing)}")
    return normalized, action


def sellersprite_registry_diagnostics(runtime_tools: Iterable[dict[str, Any]]) -> dict[str, Any]:
    runtime = {
        str(tool.get("name") or ""): tool
        for tool in runtime_tools
        if isinstance(tool, dict) and tool.get("name")
    }
    registered = set(SELLERSPRITE_TOOL_REQUEST_SPECS)
    format_mismatches: list[str] = []
    required_mismatches: list[str] = []
    for name in sorted(registered & set(runtime)):
        schema = runtime[name].get("inputSchema") or runtime[name].get("parameters") or {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        runtime_format = "nested" if set(properties) == {"request"} else ("none" if not properties else "flat")
        spec = SELLERSPRITE_TOOL_REQUEST_SPECS[name]
        if runtime_format != spec.request_format:
            format_mismatches.append(name)
            continue
        required = set(schema.get("required") or [])
        if runtime_format == "nested":
            request_schema = properties.get("request") if isinstance(properties.get("request"), dict) else {}
            required = set(request_schema.get("required") or [])
        if set(spec.required_fields) != required:
            required_mismatches.append(name)
    return {
        "registered": len(registered),
        "runtime": len(runtime),
        "missing_registered": sorted(set(runtime) - registered),
        "missing_runtime": sorted(registered - set(runtime)),
        "format_mismatches": format_mismatches,
        "required_mismatches": required_mismatches,
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
        spec = SELLERSPRITE_TOOL_REQUEST_SPECS.get(name)
        return spec.capability if spec else "unknown"
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
        mapping = {name: spec.capability for name, spec in SELLERSPRITE_TOOL_REQUEST_SPECS.items()}
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
