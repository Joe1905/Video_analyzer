"""FastMoss Product Scout V2 evidence contract.

This module is deliberately independent from chat orchestration.  It turns the
already returned MCP envelopes into a bounded evidence contract, deterministic
user-visible fact blocks, and conservative interpretation checks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping


V2_MODE_VALUES = {"off", "shadow", "enforce"}
V2_CACHE_NAMESPACE = "fastmoss-product-scout-v2"
V2_CACHE_SCHEMA_VERSION = "2026-08-01"
NORMAL_EMPTY_TTL_SECONDS = 600


def product_scout_v2_mode(value: str | None) -> str:
    mode = str(value or "off").strip().lower()
    return mode if mode in V2_MODE_VALUES else "off"


def product_scout_v2_market_ambiguity_question(user_text: str) -> str | None:
    """Do not consume FastMoss calls when one request names multiple markets."""
    text = str(user_text or "")
    markets = set()
    if re.search(r"美国|美区|\bUS\b", text, re.I):
        markets.add("US")
    if re.search(r"英国|英区|\bUK\b", text, re.I):
        markets.add("UK")
    if len(markets) > 1:
        return "本次选品请求同时出现多个市场。请先确认要研究的单一市场，再开始 FastMoss 查询。"
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _nested_values(value: Any, keys: set[str]) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in keys and item not in (None, "", [], {}):
                yield item
            yield from _nested_values(item, keys)
    elif isinstance(value, list):
        for item in value:
            yield from _nested_values(item, keys)


def _first(value: Any, keys: set[str]) -> Any:
    return next(iter(_nested_values(value, keys)), None)


def _string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _result_data(result: Mapping[str, Any]) -> Any:
    for key in ("mcp_data", "summary", "data"):
        if result.get(key) is not None:
            return result[key]
    return {}


def _data_state(result: Mapping[str, Any]) -> str:
    state = str(result.get("data_state") or "").strip().lower()
    if state in {"data", "empty", "error"}:
        return "verified" if state == "data" else state
    if result.get("error") or result.get("ok") is False:
        return "error"
    data = _result_data(result)
    if data in (None, "", [], {}):
        return "empty"
    return "verified"


def _call_arguments(tool_calls: list[Mapping[str, Any]], index: int) -> dict[str, Any]:
    if index >= len(tool_calls):
        return {}
    function = _as_dict(_as_dict(tool_calls[index]).get("function"))
    raw = function.get("arguments")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        return json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}


def _tool_name(tool_result: Mapping[str, Any]) -> str:
    return str(tool_result.get("tool_name") or "").removeprefix("fastmoss__")


def _find_rows(value: Any) -> list[dict[str, Any]]:
    """Return the largest record-like list without inventing a response schema."""
    candidates: list[list[dict[str, Any]]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            rows = [dict(item) for item in node if isinstance(item, Mapping)]
            if rows:
                candidates.append(rows)
            for item in node:
                visit(item)
        elif isinstance(node, Mapping):
            for item in node.values():
                visit(item)

    visit(value)
    if not candidates:
        return []
    productish = [
        rows for rows in candidates
        if any(_first(row, {"productid", "productname", "title", "name"}) is not None for row in rows)
    ]
    return max(productish or candidates, key=len)


def _period(arguments: Mapping[str, Any], data: Any) -> dict[str, Any]:
    filters = _as_dict(arguments.get("filter"))
    requested = {
        key: filters.get(key)
        for key in ("date_type", "date_value", "listing_start_date", "listing_end_date", "time_range_days")
        if filters.get(key) not in (None, "")
    }
    actual = {
        "start": _string(_first(data, {"startdate", "periodstart", "starttime"})),
        "end": _string(_first(data, {"enddate", "periodend", "endtime"})),
        "value": _string(_first(data, {"datevalue", "statdate", "period"})),
    }
    return {"requested": requested, "actual": {key: value for key, value in actual.items() if value}}


def _market_scope(user_text: str, route: Mapping[str, Any], calls: list[dict[str, Any]]) -> dict[str, str]:
    direct = re.search(r"(?:美国|美区|\bUS\b|英国|英区|\bUK\b)", str(user_text or ""), re.I)
    region = str(route.get("region") or "").upper().strip()
    if not region:
        for arguments in calls:
            region = str(_first(arguments, {"region", "market", "marketplace", "country", "site"}) or "").upper().strip()
            if region:
                break
    if not region:
        region = "US"
    return {"value": region, "source": "user" if direct else "page_default"}


def _category_scope(calls: list[dict[str, Any]]) -> dict[str, Any]:
    category: dict[str, Any] = {"status": "not_attempted"}
    for arguments in calls:
        filters = _as_dict(arguments.get("filter"))
        path = filters.get("category_path")
        if isinstance(path, list) and path:
            category.update({"path": path, "status": "verified"})
        ids = {
            key: filters.get(key)
            for key in ("category_id", "category_l1_id", "category_l2_id", "category_l3_id")
            if filters.get(key) not in (None, "")
        }
        if ids:
            category.update({"ids": ids, "status": "verified"})
    return category


def _row_value(row: Mapping[str, Any], *keys: str) -> Any:
    normalized_keys = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in keys}
    # Ranking rows contain nested category IDs. Prefer a row's own product ID,
    # title and other fields before recursively inspecting nested metadata.
    for key, value in row.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized in normalized_keys and value not in (None, "", [], {}):
            return value
    return _first(row, normalized_keys)


def _product_row(
    row: Mapping[str, Any],
    source_ref: str,
    period: Mapping[str, Any],
    data_state: str,
) -> dict[str, Any]:
    data = {
        "product": _row_value(row, "product_name", "title", "name", "product_title"),
        "link": _row_value(row, "product_url", "url", "detail_url", "link"),
        "product_id": _row_value(row, "product_id", "id"),
        "price": _row_value(row, "price", "current_price", "sale_price"),
        "shop": _row_value(row, "shop_name", "store_name", "seller_name"),
        "period_units_sold": _row_value(row, "period_units_sold", "day28_units_sold", "units_sold", "sales"),
        "period_gmv": _row_value(row, "period_gmv", "day28_gmv", "gmv", "sales_amount"),
        "growth": _row_value(row, "growth_rate", "gmv_growth_rate", "units_sold_growth_rate"),
        "commission_rate": _row_value(row, "commission_rate", "commission"),
        "launch_date": _row_value(row, "launch_date", "listed_date", "create_time", "publish_date"),
        "channel": _row_value(row, "channel", "channel_type", "sales_channel"),
        "source_ref": source_ref,
        "period": dict(period),
        "data_state": data_state,
    }
    data = {key: value for key, value in data.items() if value not in (None, "", {}, [])}
    identity = str(data.get("product_id") or data.get("product") or "unknown")
    data["fact_id"] = f"{source_ref}/product:{identity}"
    return data


def _row_noise_reason(
    row: Mapping[str, Any],
    normalized: Mapping[str, Any],
    seen_identities: set[str],
    expected_market: str,
) -> str | None:
    """Keep only entities that can safely enter the deterministic ranking tables."""
    product = str(normalized.get("product") or "").strip().lower()
    link = str(normalized.get("link") or "").strip().lower()
    identity = str(normalized.get("product_id") or normalized.get("link") or normalized.get("product") or "").strip().lower()
    row_market = str(_row_value(row, "region", "market", "marketplace", "country", "site") or "").upper().strip()
    if any(marker in link for marker in ("example.test", "localhost", "/test", "test-link")):
        return "test_or_placeholder_link"
    if any(marker in product for marker in ("测试", "test product", "screen display", "shown on screen", "shown on the screen", "展示屏")):
        return "test_or_placeholder_product"
    if row_market and expected_market and row_market != expected_market:
        return "market_mismatch"
    if identity and identity in seen_identities:
        return "duplicate_entity"
    if identity:
        seen_identities.add(identity)
    return None


@dataclass(frozen=True)
class ProductScoutEvidenceContract:
    payload: dict[str, Any]

    @property
    def grade(self) -> str:
        return str(self.payload.get("coverage_grade") or "D")

    @property
    def status(self) -> str:
        return str(self.payload.get("status") or "unavailable")


def build_product_scout_evidence_contract(
    tool_calls: list[Mapping[str, Any]],
    tool_results: list[Mapping[str, Any]],
    user_text: str,
    route: Mapping[str, Any] | None = None,
) -> ProductScoutEvidenceContract:
    """Normalize returned FastMoss evidence into the V2 ProductScout contract."""
    route = _as_dict(route)
    calls = [_call_arguments(tool_calls, index) for index, _ in enumerate(tool_results)]
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(tool_results):
        if not isinstance(raw, Mapping) or not str(raw.get("tool_name") or "").startswith("fastmoss__"):
            continue
        result = _as_dict(raw.get("result"))
        data = _result_data(result)
        tool = _tool_name(raw)
        entries.append({
            "source_ref": f"call:{index + 1}/{tool}",
            "tool": tool,
            "arguments": calls[index],
            "data": data,
            "state": _data_state(result),
            "cache": _as_dict(result.get("cache")),
            "period": _period(calls[index], data),
        })

    market = _market_scope(user_text, route, calls)
    category = _category_scope(calls)
    market_entries = [entry for entry in entries if entry["tool"] == "market_category_analysis"]
    hot_entries = [entry for entry in entries if entry["tool"] == "product_rank_top_selling"]
    new_entries = [entry for entry in entries if entry["tool"] == "product_rank_new_listed"]
    trend_entries = [entry for entry in entries if entry["tool"] == "product_sales_trend"]
    leading_entries = [
        entry for entry in entries
        if entry["tool"] in {"product_creator_analysis", "product_video_list", "product_overview", "product_investment"}
    ]

    def ranking(entries_for_tool: list[dict[str, Any]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        seen_identities: set[str] = set()
        for entry in entries_for_tool:
            for row in _find_rows(entry["data"]):
                normalized = _product_row(row, entry["source_ref"], entry["period"], entry["state"])
                if not normalized.get("product_id") and not normalized.get("product"):
                    rejected.append({"source_ref": entry["source_ref"], "reason": "product_identity_missing"})
                elif reason := _row_noise_reason(row, normalized, seen_identities, market["value"]):
                    rejected.append({
                        "source_ref": entry["source_ref"],
                        "reason": reason,
                        "product_id": normalized.get("product_id"),
                    })
                else:
                    rows.append(normalized)
        states = [entry["state"] for entry in entries_for_tool]
        return {
            "status": "verified" if rows else "empty" if "empty" in states else "error" if "error" in states else "not_attempted",
            "actual_period": [entry["period"] for entry in entries_for_tool],
            "sort": [entry["arguments"].get("orderby") for entry in entries_for_tool if entry["arguments"].get("orderby")],
            "returned_count": len(rows) + len(rejected),
            "rows": rows[:10],
            "rejected_rows": rejected,
        }

    hot = ranking(hot_entries)
    new = ranking(new_entries)
    candidates: list[dict[str, Any]] = []
    trend_rows = [row for entry in trend_entries for row in _find_rows(entry["data"])]
    candidate_ids = []
    for row in hot["rows"] + new["rows"]:
        product_id = str(row.get("product_id") or "")
        if product_id and product_id not in candidate_ids:
            candidate_ids.append(product_id)
    for product_id in candidate_ids[:3]:
        trend = next((entry for entry in trend_entries if str(_first(entry["arguments"], {"productid"}) or "") == product_id), None)
        leading = [entry for entry in leading_entries if str(_first(entry["arguments"], {"productid"}) or "") == product_id]
        candidates.append({
            "product_identity": product_id,
            "selection_reason": "热销榜或新品榜有效样本",
            "sales_trend": trend["state"] if trend else "not_attempted",
            "creator_video_live_leading_signals": "verified" if any(item["state"] == "verified" for item in leading) else "not_attempted",
            "channel_attribution": "verified" if any(item["tool"] == "product_overview" and item["state"] == "verified" for item in leading) else "not_attempted",
            "competition_or_concentration": "not_attempted",
            "lifecycle_judgment": "not_attempted",
            "evidence_gaps": [
                label for label, value in (("sales_trend", trend), ("leading_signal", leading))
                if not value
            ],
            "status": "partial" if trend or leading else "not_attempted",
        })

    market_state = "verified" if any(entry["state"] == "verified" for entry in market_entries) else "empty" if any(entry["state"] == "empty" for entry in market_entries) else "error" if any(entry["state"] == "error" for entry in market_entries) else "not_attempted"
    market_metrics: list[dict[str, Any]] = []
    for entry in market_entries:
        metric_values = {
            "scale": _first(entry["data"], {"marketsize", "totalgmv", "periodgmv", "gmv", "salesamount", "categorygmv", "categoryunitssold"}),
            "growth": _first(entry["data"], {"growthrate", "gmvgrowthrate", "unitssoldgrowthrate", "categorygmvyoypercent", "categoryunitssoldyoypercent", "categorygmvmompercent"}),
            "channel_structure": _first(entry["data"], {"channelstructure", "channelratio", "channeldistribution"}),
            "competition_or_concentration": _first(entry["data"], {"concentration", "cr3", "cr5", "shopcount", "sellercount", "topproductsgmvshare", "topshopsgmvshare"}),
        }
        for metric, value in metric_values.items():
            if value not in (None, "", [], {}):
                market_metrics.append({
                    "fact_id": f"{entry['source_ref']}/market:{metric}",
                    "metric": metric,
                    "value": value,
                    "source_ref": entry["source_ref"],
                    "data_state": entry["state"],
                })
    market_category_ids = {
        str(_as_dict(entry["arguments"].get("filter")).get("category_id") or "")
        for entry in market_entries
    } - {""}
    top_category_ids = {
        str(_as_dict(entry["arguments"].get("filter")).get("category_id") or "")
        for entry in hot_entries
    } - {""}
    new_l3_ids = {
        str(_as_dict(entry["arguments"].get("filter")).get("category_l3_id") or "")
        for entry in new_entries
    } - {""}
    if market_category_ids and market_category_ids == top_category_ids and not new_l3_ids:
        market_grain = "target_L2"
    elif market_category_ids and new_l3_ids:
        market_grain = "upstream_L2_reference_for_L3"
    else:
        market_grain = "target_or_upstream_unverified"
    leading_count = sum(1 for item in candidates if item["creator_video_live_leading_signals"] == "verified")
    market_metric_names = {str(item.get("metric") or "") for item in market_metrics}
    has_market = market_state == "verified" and {"scale", "growth", "competition_or_concentration"}.issubset(market_metric_names)
    has_hot = len(hot["rows"]) >= 3
    has_new = len(new["rows"]) >= 1
    has_candidate_trends = sum(1 for item in candidates if item["sales_trend"] == "verified")
    target_level_market = market_grain == "target_L2"
    if target_level_market and has_market and has_hot and len(new["rows"]) >= 3 and has_candidate_trends >= 2 and leading_count >= 2:
        grade, status = "A", "sufficient"
    elif target_level_market and has_hot and has_new and has_market:
        grade, status = "B", "partial"
    elif any(entry["state"] == "verified" for entry in entries):
        grade, status = "C", "insufficient"
    else:
        grade, status = "D", "unavailable" if any(entry["state"] == "error" for entry in entries) else "insufficient"

    periods = [entry["period"] for entry in entries if entry["period"].get("requested") or entry["period"].get("actual")]
    currencies = sorted({str(_first(entry["data"], {"currency", "currencycode"}) or "") for entry in entries if _first(entry["data"], {"currency", "currencycode"})})
    payload = {
        "type": "fastmoss_product_scout_v2_contract",
        "source": "FastMoss",
        "scope": {
            "market": market,
            "category": category,
            "requested_period": [entry["period"]["requested"] for entry in entries if entry["period"]["requested"]],
            "actual_periods": periods,
            "currency": currencies,
        },
        "market_evidence": {
            "status": market_state,
            "source_refs": [entry["source_ref"] for entry in market_entries],
            "grain": market_grain,
            "metrics": market_metrics,
        },
        "hot_ranking": hot,
        "new_ranking": new,
        "candidate_validations": candidates,
        "claims": [],
        "coverage_grade": grade,
        "status": status,
        "evidence_gaps": [
            item for item, present in (
                ("market_evidence", target_level_market and has_market),
                ("hot_ranking", has_hot),
                ("new_ranking", has_new),
                ("candidate_sales_trend", has_candidate_trends >= 1),
                ("candidate_leading_signal", leading_count >= 1),
            ) if not present
        ],
        "tool_states": [
            {"source_ref": entry["source_ref"], "tool": entry["tool"], "data_state": entry["state"], "cache": entry["cache"]}
            for entry in entries
        ],
        "generated_at": datetime.now().astimezone().isoformat(),
        "schema_version": V2_CACHE_SCHEMA_VERSION,
    }
    return ProductScoutEvidenceContract(payload)


def contract_next_instruction(contract: ProductScoutEvidenceContract) -> str:
    payload = contract.payload
    if contract.status == "sufficient":
        return "V2 证据契约已达到 sufficient。停止调用工具，进入最终事实区块与解释阶段。"
    gaps = "、".join(payload.get("evidence_gaps") or []) or "当前事实覆盖"
    return (
        "V2 证据契约尚未完成。当前仅反馈能力缺口：" + gaps + "。"
        "请根据已取得的事实和当前暴露的 FastMoss 工具，自主选择下一项最能补足缺口的真实调用；"
        "不要重复同一语义调用，不要先输出最终选品结论。"
    )


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "暂无可展示记录。"
    stringify = lambda value: str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(stringify(value) for value in row) + " |" for row in rows],
    ])


def render_deterministic_fact_blocks(contract: ProductScoutEvidenceContract) -> str:
    """Render the V2 user-visible facts; this output is never sent back for model rewriting."""
    payload = contract.payload
    scope = _as_dict(payload.get("scope"))
    market = _as_dict(scope.get("market"))
    category = _as_dict(scope.get("category"))
    lines = ["## 数据口径卡", ""]
    lines.append(_table(["项目", "值"], [
        ["数据源", "FastMoss MCP"],
        ["覆盖等级", payload.get("coverage_grade")],
        ["市场", market.get("value") or "未确认"],
        ["市场来源", "用户指定" if market.get("source") == "user" else "页面默认"],
        ["类目路径 / ID", category.get("path") or category.get("ids") or "未确认"],
        ["实际周期", scope.get("actual_periods") or "接口未返回"],
        ["币种", ", ".join(scope.get("currency") or []) or "接口未返回"],
        ["数据状态", "; ".join(
            f"{item.get('tool')}：{'缓存命中' if item.get('cache', {}).get('hit') else '实时调用'}"
            + (f"（缓存 age {item.get('cache', {}).get('age_seconds')} 秒）" if item.get('cache', {}).get('age_seconds') is not None else "")
            for item in payload.get("tool_states") or []
        ) or "接口未返回"],
    ]))

    lines.extend(["", "## 市场与类目表", ""])
    market_evidence = _as_dict(payload.get("market_evidence"))
    market_rows = [
        [item.get("metric"), item.get("value"), market_evidence.get("grain") or "未确认", item.get("source_ref")]
        for item in market_evidence.get("metrics") or []
    ]
    if not market_rows:
        market_rows = [["规模、增长、渠道、竞争 / 集中度", market_evidence.get("status") or "not_attempted", market_evidence.get("grain") or "未确认", ", ".join(market_evidence.get("source_refs") or []) or "—"]]
    lines.append(_table(["能力", "接口值 / 状态", "粒度", "来源"], market_rows))

    def ranking_block(title: str, ranking: Mapping[str, Any], new_listing: bool = False) -> list[str]:
        rows = list(ranking.get("rows") or [])
        headers = ["商品 / 链接", "ID", "价格", "店铺", "周期销量", "周期 GMV", "增速", "上架日", "佣金", "来源"]
        table_rows = []
        for row in rows:
            product = str(row.get("product") or "—")
            if row.get("link"):
                product = f"[{product}]({row['link']})"
            table_rows.append([
                product, row.get("product_id"), row.get("price"), row.get("shop"),
                row.get("period_units_sold"), row.get("period_gmv"), row.get("growth"),
                row.get("launch_date") if new_listing else row.get("launch_date"),
                row.get("commission_rate"), row.get("source_ref"),
            ])
        return ["", f"## {title}", "", _table(headers, table_rows), "", (
            f"返回 {ranking.get('returned_count', 0)} 条；展示 {len(rows)} 条；"
            f"剔除 {len(ranking.get('rejected_rows') or [])} 条。"
        )]

    lines.extend(ranking_block("热销榜", _as_dict(payload.get("hot_ranking"))))
    lines.extend(ranking_block("新品榜", _as_dict(payload.get("new_ranking")), new_listing=True))

    lines.extend(["", "## 候选验证表", ""])
    candidate_rows = []
    for row in payload.get("candidate_validations") or []:
        candidate_rows.append([
            row.get("product_identity"), row.get("selection_reason"), row.get("sales_trend"),
            row.get("creator_video_live_leading_signals"), row.get("channel_attribution"),
            row.get("status"), ", ".join(row.get("evidence_gaps") or []),
        ])
    lines.append(_table(["候选", "选择原因", "销售趋势", "领先信号", "渠道", "状态", "缺口"], candidate_rows))

    lines.extend(["", "## 数据缺口与剔除项", ""])
    gaps = payload.get("evidence_gaps") or []
    lines.extend([f"- 未覆盖能力：{', '.join(gaps) if gaps else '无'}。"])
    for title, ranking in (("热销榜", _as_dict(payload.get("hot_ranking"))), ("新品榜", _as_dict(payload.get("new_ranking")))):
        for rejected in ranking.get("rejected_rows") or []:
            lines.append(f"- {title}剔除：{rejected.get('reason') or '未说明'}（{rejected.get('source_ref') or '来源未知'}）。")
    for state in payload.get("tool_states") or []:
        if state.get("data_state") in {"empty", "error"}:
            lines.append(f"- {state.get('tool')}：{state.get('data_state')}（{state.get('source_ref')}）。")
    return "\n".join(lines).strip()


_HIGH_RISK_TERMS = ("利润", "毛利", "低竞争", "最佳", "唯一", "爆发", "窗口开放", "备货")


def validate_interpretation(text: str, contract: ProductScoutEvidenceContract) -> tuple[str, list[str]]:
    """Keep interpretation within the contract; deterministic fact blocks carry all numbers."""
    payload = contract.payload
    grade = contract.grade
    market_metric_names = {
        str(item.get("metric") or "")
        for item in _as_dict(payload.get("market_evidence")).get("metrics") or []
        if isinstance(item, Mapping)
    }
    candidates = list(payload.get("candidate_validations") or [])
    has_growth_evidence = sum(1 for item in candidates if item.get("sales_trend") == "verified") >= 2
    has_leading_evidence = sum(
        1 for item in candidates if item.get("creator_video_live_leading_signals") == "verified"
    ) >= 2
    output: list[str] = []
    violations: list[str] = []
    for line in str(text or "").splitlines():
        if re.match(r"^#{1,6}\s*结论、风险与下一步\s*$", line.strip()):
            continue
        line_violations: list[str] = []
        if re.search(r"\d|[$￥€£]|\d\s*%", line):
            line_violations.append("interpretation_contains_unbound_number")
        if any(term in line for term in _HIGH_RISK_TERMS) and grade != "A":
            line_violations.append("unsupported_high_risk_claim")
        if any(term in line for term in ("利润", "毛利", "备货")):
            line_violations.append("unsupported_cost_or_inventory_claim")
        if "低竞争" in line and "competition_or_concentration" not in market_metric_names:
            line_violations.append("unsupported_competition_claim")
        if any(term in line for term in ("爆发", "窗口开放")) and not (has_growth_evidence and has_leading_evidence):
            line_violations.append("unsupported_growth_window_claim")
        if any(term in line for term in ("最佳", "唯一")):
            line_violations.append("unsupported_absolute_claim")
        if "直播" in line and "商品卡" in line and "占比" in line:
            line_violations.append("channel_denominator_mixing")
        if line_violations:
            violations.extend(line_violations)
            continue
        if line.strip():
            output.append(line)
    cleaned = "\n".join(output).strip()
    if not cleaned:
        cleaned = "当前证据仅支持上述事实展示；请按数据缺口继续验证后再做进入判断。"
    return cleaned, sorted(set(violations))


__all__ = [
    "NORMAL_EMPTY_TTL_SECONDS",
    "ProductScoutEvidenceContract",
    "V2_CACHE_NAMESPACE",
    "V2_CACHE_SCHEMA_VERSION",
    "build_product_scout_evidence_contract",
    "contract_next_instruction",
    "product_scout_v2_mode",
    "render_deterministic_fact_blocks",
    "validate_interpretation",
]
