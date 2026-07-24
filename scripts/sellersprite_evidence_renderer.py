#!/usr/bin/env python3
"""Deterministic Semantic rendering for SellerSprite MCP responses.

The runtime MCP schema remains the source of truth for request parameters.
This module describes response meaning only: tool capability, business shape,
entity type and a lossless Markdown representation for the report model.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
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
    localize_semantic_value,
)
from json_to_markdown import json_to_markdown
from commerce_research_ledger import CAPABILITY_FACTS


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


@dataclass(frozen=True)
class SellerSpriteEvidenceContract:
    """One tool's facts, projection dependencies and verified filter support."""

    capability: str
    entity_type: str
    facts: tuple[str, ...]
    identity_facts: tuple[str, ...]
    return_fields_location: str
    verified_return_fields: Mapping[str, tuple[str, ...]]
    field_source: str


_FIXTURE_DIR = Path(__file__).with_name("semantic_fixtures") / "sellersprite"

_IDENTITY_FACTS_BY_ENTITY: dict[str, tuple[str, ...]] = {
    "keyword": ("关键词身份",),
    "asin": ("亚马逊商品编号",),
    "category": ("类目身份", "类目编号"),
    "review": ("亚马逊商品编号", "评论身份"),
    "trademark": ("商标身份",),
}

# These are business facts, not response field allowlists.  Aliases are used
# only for local projection and fact observation after the full response has
# already been retained in the audit layer.
_FACT_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "关键词身份": frozenset({"keyword", "keywords", "keywrod", "keywrod_cn", "keywrod_jp", "query"}),
    "亚马逊商品编号": frozenset({"asin", "parent_asin", "child_asin"}),
    "商品标题": frozenset({"title", "product_title", "listing_title"}),
    "类目身份": frozenset({"category", "category_name", "department", "node_name", "node_path"}),
    "类目编号": frozenset({"category_id", "node_id", "node_id_path", "nodeidpath"}),
    "统计周期": frozenset({"period", "market_period", "month", "week", "date_value", "billing_period"}),
    "统计月份": frozenset({"month", "period", "date_value"}),
    "统计时间": frozenset({"date", "time", "timestamp", "month", "week", "period", "date_value"}),
    "趋势值": frozenset({"value", "trend", "searches", "search_volume", "rank", "units", "revenue"}),
    "搜索量": frozenset({"searches", "search_volume", "search_volume_value", "search_count"}),
    "购买量": frozenset({"purchases", "purchase_volume", "purchase_count"}),
    "购买率": frozenset({"purchase_rate", "purchaserate", "conversion_rate"}),
    "商品供给": frozenset({"products", "product_count", "goods_count", "listings"}),
    "市场规模": frozenset({"volume", "market_volume", "revenue", "units", "product_count", "goods_count"}),
    "竞争程度": frozenset({"competition", "supply_demand_ratio", "products", "sellers", "brands"}),
    "集中度": frozenset({"concentration", "cr", "crn", "brand_crn", "goods_crn", "seller_crn"}),
    "价格": frozenset({"price", "avg_price", "new_price", "buy_box_price"}),
    "价格分布": frozenset({"price", "price_range", "avg_price", "distribution"}),
    "币种": frozenset({"currency", "currency_code"}),
    "销量": frozenset({"units", "units_sold", "sales", "monthly_sales"}),
    "销售额": frozenset({"revenue", "gmv", "sales_amount"}),
    "评分": frozenset({"rating", "stars", "review_rating"}),
    "类目排名": frozenset({"rank", "bsr", "bsr_rank", "sub_bsr_rank"}),
    "评论身份": frozenset({"review_id", "id"}),
    "评论时间": frozenset({"review_date", "date", "time", "timestamp", "create_time"}),
    "评论星级": frozenset({"review_rating", "rating", "star", "stars"}),
    "评论内容": frozenset({"review_content", "content", "text", "body"}),
    "流量来源": frozenset({"traffic_source", "source", "traffic", "share"}),
    "自然排名": frozenset({"organic_rank", "natural_rank", "rank", "position"}),
    "卖家结构": frozenset({"seller", "sellers", "seller_type", "seller_country", "distribution"}),
    "商标身份": frozenset({"trademark", "trademark_name", "name", "serial_number"}),
    "商标状态": frozenset({"status", "trademark_status"}),
    "商标国家": frozenset({"country", "country_code"}),
}

_STRUCTURAL_FIELDS = frozenset({
    "data", "items", "list", "records", "results", "rows", "content",
    "children", "trend", "series", "points", "total",
})
_SCOPE_FIELDS = frozenset({
    "marketplace", "region", "country", "currency", "currency_code", "period",
    "month", "week", "date", "time", "timestamp", "date_value", "rank", "order",
})

# Only fields explicitly established by the existing official alias correction
# are safe for server-side filtering today.  A tool is filtered only if *all*
# requested facts and their dependencies can be expressed by this allowlist.
_VERIFIED_RETURN_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "keyword_research": {
        "关键词身份": ("keywords",),
        "搜索量": ("searches",),
        "统计月份": ("month",),
        "统计周期": ("month",),
    },
}


def _normalized_contract_field(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _return_fields_location(tool_name: str) -> str:
    """Read only the checked-in runtime schema shape, never infer field names."""
    path = _FIXTURE_DIR / f"{tool_name}.json"
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        schema = fixture.get("runtime_input_schema") or {}
        properties = schema.get("properties") or {}
        if "returnFields" in properties:
            return "top_level"
        request = properties.get("request") or {}
        if "returnFields" in (request.get("properties") or {}):
            return "request"
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return "unsupported"


SELLERSPRITE_EVIDENCE_CONTRACTS: dict[str, SellerSpriteEvidenceContract] = {
    name: SellerSpriteEvidenceContract(
        capability=semantic.capability,
        entity_type=semantic.entity_type,
        facts=tuple(CAPABILITY_FACTS.get(semantic.capability) or ("业务事实",)),
        identity_facts=_IDENTITY_FACTS_BY_ENTITY.get(semantic.entity_type, ("对象身份",)),
        return_fields_location=_return_fields_location(name),
        verified_return_fields=_VERIFIED_RETURN_FIELDS.get(name, {}),
        field_source=(
            "官方字段别名与脱敏真实响应"
            if name in _VERIFIED_RETURN_FIELDS
            else "运行时MCP Schema与工具专属脱敏响应；未核实服务端字段名"
        ),
    )
    for name, semantic in SELLERSPRITE_TOOL_SEMANTICS.items()
}


def sellersprite_contract_diagnostics() -> dict[str, Any]:
    contracts = set(SELLERSPRITE_EVIDENCE_CONTRACTS)
    semantics = set(SELLERSPRITE_TOOL_SEMANTICS)
    return {
        "contracts": len(contracts),
        "semantics": len(semantics),
        "missing_contracts": sorted(semantics - contracts),
        "unexpected_contracts": sorted(contracts - semantics),
        "return_fields_supported": sum(
            1 for contract in SELLERSPRITE_EVIDENCE_CONTRACTS.values()
            if contract.return_fields_location != "unsupported"
        ),
        "server_filter_tools": sorted(
            name for name, contract in SELLERSPRITE_EVIDENCE_CONTRACTS.items()
            if contract.verified_return_fields
        ),
    }


def _fact_fields(facts: Iterable[str]) -> set[str]:
    fields: set[str] = set()
    for fact in facts:
        fields.update(_FACT_FIELD_ALIASES.get(str(fact), ()))
    return fields


def compile_sellersprite_return_fields(
    tool_name: str,
    required_facts: Iterable[str],
) -> dict[str, Any]:
    """Compile a safe field plan; never guess an upstream field name."""
    contract = SELLERSPRITE_EVIDENCE_CONTRACTS.get(str(tool_name))
    requested = [str(item) for item in required_facts if str(item).strip()]
    if contract is None:
        return {
            "mode": "local_projection",
            "fields": [],
            "reason": "工具没有证据契约",
        }
    facts = list(dict.fromkeys([*contract.identity_facts, *requested]))
    verified = contract.verified_return_fields
    missing = [fact for fact in facts if fact not in verified]
    if (
        contract.return_fields_location == "unsupported"
        or not verified
        or missing
    ):
        return {
            "mode": "local_projection",
            "fields": [],
            "location": contract.return_fields_location,
            "required_facts": facts,
            "unverified_facts": missing,
            "reason": "所需事实没有完整、可追溯的官方返回字段白名单",
        }
    fields: list[str] = []
    for fact in facts:
        for field_name in verified[fact]:
            if field_name not in fields:
                fields.append(field_name)
    return {
        "mode": "server_filter",
        "fields": fields,
        "location": contract.return_fields_location,
        "required_facts": facts,
        "unverified_facts": [],
        "reason": "全部字段来自已核实白名单",
    }


def apply_sellersprite_return_field_plan(
    tool_name: str,
    arguments: Mapping[str, Any],
    required_facts: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Override model-supplied returnFields with the contract plan."""
    normalized = copy.deepcopy(dict(arguments or {}))
    plan = compile_sellersprite_return_fields(tool_name, required_facts)
    target = normalized
    if isinstance(normalized.get("request"), dict):
        target = normalized["request"]
    target.pop("returnFields", None)
    if plan.get("mode") == "server_filter":
        location = plan.get("location")
        if location == "request":
            request = normalized.get("request")
            if not isinstance(request, dict):
                request = {}
                normalized["request"] = request
            request["returnFields"] = ",".join(plan["fields"])
        elif location == "top_level":
            normalized["returnFields"] = ",".join(plan["fields"])
    return normalized, plan


def _business_value_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, bool):
        return True
    return True


def _project_value(
    value: Any,
    *,
    selected_fields: set[str],
    path: str,
    kept_paths: list[str],
    audit_paths: list[str],
) -> Any:
    if isinstance(value, list):
        projected_rows = [
            _project_value(
                item,
                selected_fields=selected_fields,
                path=f"{path}[{index}]",
                kept_paths=kept_paths,
                audit_paths=audit_paths,
            )
            for index, item in enumerate(value)
        ]
        return [item for item in projected_rows if item not in (None, {}, [])]
    if not isinstance(value, Mapping):
        kept_paths.append(path)
        return copy.deepcopy(value)
    projected: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        normalized = _normalized_contract_field(key)
        child_path = f"{path}.{key}"
        structural = normalized in _STRUCTURAL_FIELDS
        selected = normalized in selected_fields or normalized in _SCOPE_FIELDS
        if isinstance(item, (Mapping, list)):
            kept_before = len(kept_paths)
            child = _project_value(
                item,
                selected_fields=selected_fields,
                path=child_path,
                kept_paths=kept_paths,
                audit_paths=audit_paths,
            )
            child_has_selected = len(kept_paths) > kept_before
            if child not in (None, {}, []) and (structural or selected or child_has_selected):
                projected[key] = child
            elif child in ({}, []) and structural:
                projected[key] = child
            elif not child_has_selected:
                audit_paths.append(child_path)
            continue
        if selected:
            projected[key] = copy.deepcopy(item)
            kept_paths.append(child_path)
        else:
            audit_paths.append(child_path)
    return projected


def project_sellersprite_business_data(
    tool_name: str,
    business_data: Any,
    required_facts: Iterable[str],
) -> dict[str, Any]:
    """Create the slot/report projection while preserving the raw audit value."""
    contract = SELLERSPRITE_EVIDENCE_CONTRACTS.get(str(tool_name))
    requested = list(dict.fromkeys(str(item) for item in required_facts if str(item).strip()))
    if contract is None:
        return {
            "projected_data": None,
            "observed_facts": [],
            "selected_facts": requested,
            "kept_paths": [],
            "audit_only_paths": ["$"],
            "unmapped_fields": ["未登记工具"],
            "raw_chars": len(json.dumps(business_data, ensure_ascii=False, default=str)),
            "projected_chars": 0,
        }
    selected_facts = list(dict.fromkeys([*contract.identity_facts, *requested]))
    selected_fields = _fact_fields(selected_facts)
    # Keep currency and scope whenever numerical evidence survives.
    selected_fields.update({"currency", "currency_code", "marketplace", "region", "period", "month", "date"})
    kept_paths: list[str] = []
    audit_paths: list[str] = []
    projected = _project_value(
        business_data,
        selected_fields=selected_fields,
        path="$",
        kept_paths=kept_paths,
        audit_paths=audit_paths,
    )
    observed: list[str] = []
    seen_keys: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = _normalized_contract_field(key)
                if _business_value_present(item):
                    seen_keys.add(normalized)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(projected)
    for fact in selected_facts:
        if seen_keys.intersection(_FACT_FIELD_ALIASES.get(fact, ())):
            observed.append(fact)
    raw_chars = len(json.dumps(business_data, ensure_ascii=False, default=str))
    projected_chars = len(json.dumps(projected, ensure_ascii=False, default=str))
    return {
        "projected_data": projected,
        "observed_facts": observed,
        "selected_facts": selected_facts,
        "kept_paths": kept_paths,
        "audit_only_paths": audit_paths,
        "unmapped_fields": [],
        "raw_chars": raw_chars,
        "projected_chars": projected_chars,
        "compression_ratio": (
            round(projected_chars / raw_chars, 4) if raw_chars else 0.0
        ),
    }

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
        "FastMoss": "短视频电商数据平台",
        "SellerSprite": "亚马逊数据平台",
    }.items():
        text = re.sub(rf"\b{re.escape(raw)}\b", label, text)
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
    "SELLERSPRITE_EVIDENCE_CONTRACTS",
    "SELLERSPRITE_RENDER_SPECS",
    "SELLERSPRITE_TOOL_CAPABILITIES",
    "SELLERSPRITE_TOOL_SEMANTICS",
    "SELLERSPRITE_TOOL_TITLES",
    "SellerSpriteToolSemantic",
    "SellerSpriteEvidenceContract",
    "apply_sellersprite_return_field_plan",
    "compile_sellersprite_return_fields",
    "project_sellersprite_business_data",
    "render_sellersprite_current_evidence",
    "render_sellersprite_tool_evidence",
    "render_sellersprite_evidence_document",
    "sellersprite_business_payload",
    "sellersprite_contract_diagnostics",
    "sellersprite_semantic_registry_diagnostics",
]
