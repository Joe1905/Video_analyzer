#!/usr/bin/env python3
"""Post-call evidence admission for SellerSprite and FastMoss.

The gate is deliberately narrower than the research planner: it only decides
whether returned records belong to the requested object and scope. It never
scores commercial attractiveness or selects the next tool.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Iterable


EVIDENCE_QUALITY_STATUSES = frozenset({
    "accepted",
    "partial",
    "off_topic",
    "identity_missing",
    "scope_uncertain",
    "empty",
    "error",
    "uncertain",
})
EVIDENCE_QUALITY_OBSERVED_STATUSES = frozenset({"accepted", "partial"})
EVIDENCE_QUALITY_ENTITY_STATUSES = frozenset({"accepted", "partial"})

_WRAPPER_KEYS = frozenset({
    "data", "result", "results", "items", "list", "records", "rows", "products",
    "keywords", "categories", "shops", "stores", "creators", "videos", "lives",
})
_IDENTITY_KEYS = frozenset({
    "asin", "asins", "productid", "productids", "goodsid", "goodsids", "itemid",
    "categoryid", "categoryids", "nodeid", "nodeidpath", "shopid", "sellerid",
    "creatorid", "creatoruid", "authorid", "uid", "videoid", "liveid",
    "keyword", "keywords", "title", "name", "categoryname", "shopname", "sellername",
    "creatorname", "date", "day", "week", "month", "period", "statdate", "datetime",
    "timestamp", "ccode", "cname",
    "categoryidlevel1", "categoryidlevel2", "categoryidlevel3", "cnname",
    "cnfullname", "matchedquery",
})
_SCOPE_KEYS = frozenset({
    "region", "country", "marketplace", "market", "site", "currency", "currencycode",
    "date", "day", "week", "month", "period", "statdate", "startdate", "enddate",
    "starttime", "endtime", "datetype", "datevalue",
})
_EXACT_ID_KEYS = frozenset({
    "asin", "productid", "goodsid", "itemid", "categoryid", "nodeid", "shopid",
    "sellerid", "creatorid", "creatoruid", "authorid", "uid", "videoid", "liveid",
    "categoryidlevel1", "categoryidlevel2", "categoryidlevel3",
})
_LIST_HINTS = (
    "search", "rank", "ranking", "list", "research", "distribution", "trend",
    "top_", "market_", "keyword_", "product_",
)
_DETAIL_HINTS = (
    "detail", "overview", "profile", "analysis", "summary", "info", "traffic",
    "coupon", "prediction",
)


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip() and value.strip().casefold() not in {"null", "none", "[]", "{}"})
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _walk_named_values(value: Any, keys: frozenset[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _norm_key(key)
            if normalized in keys:
                if isinstance(item, list):
                    found.extend(str(entry).strip() for entry in item if _nonempty(entry))
                elif not isinstance(item, dict) and _nonempty(item):
                    found.append(str(item).strip())
            found.extend(_walk_named_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_named_values(item, keys))
    return found


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        marker = normalized.casefold()
        if normalized and marker not in seen:
            seen.add(marker)
            result.append(normalized)
    return result


def _business_payload(result: dict[str, Any]) -> Any:
    for key in ("mcp_data", "summary", "products", "items", "results"):
        if result.get(key) is not None:
            value = result.get(key)
            if isinstance(value, str) and value.strip().startswith(("{", "[")):
                try:
                    return json.loads(value)
                except ValueError:
                    pass
            return value
    return {}


def _find_record_collection(value: Any) -> tuple[list[Any] | None, tuple[Any, ...]]:
    """Return the first business-record list and its path.

    Scalar time series are intentionally included because each dated point is a
    record whose identity is its date/period.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, list) and _norm_key(key) in {_norm_key(name) for name in _WRAPPER_KEYS}:
                return item, (key,)
        for key, item in value.items():
            records, child_path = _find_record_collection(item)
            if records is not None:
                return records, (key, *child_path)
    elif isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            return value, ()
        for index, item in enumerate(value):
            records, child_path = _find_record_collection(item)
            if records is not None:
                return records, (index, *child_path)
    return None, ()


def _row_identity(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    identity: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if _norm_key(key) in _IDENTITY_KEYS and _nonempty(item) and not isinstance(item, (dict, list)):
                    identity.setdefault(str(key), item)
                if isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(row)
    return identity


def _query_terms(arguments: dict[str, Any]) -> list[str]:
    keys = frozenset({
        "keyword", "keywords", "query", "searchterm", "searchterms", "word", "words",
        "title", "name", "categoryname",
    })
    return _unique(_walk_named_values(arguments, keys))


def _exact_ids(arguments: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = _norm_key(key)
                if normalized in _EXACT_ID_KEYS and _nonempty(item) and not isinstance(item, dict):
                    values = item if isinstance(item, list) else [item]
                    found.setdefault(normalized, []).extend(str(entry).strip() for entry in values if _nonempty(entry))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(arguments)
    return {key: _unique(values) for key, values in found.items()}


def _dimensions(tool_name: str) -> list[str]:
    name = str(tool_name or "").casefold()
    dimensions: list[str] = []
    for needle, label in (
        ("keyword", "关键词需求"),
        ("product", "商品样本"),
        ("market", "市场范围"),
        ("category", "类目信息"),
        ("trend", "时间趋势"),
        ("price", "价格分布"),
        ("shop", "店铺信息"),
        ("seller", "卖家信息"),
        ("creator", "达人信息"),
        ("video", "视频信息"),
        ("live", "直播信息"),
        ("ad_", "广告表现"),
        ("rank", "排名"),
        ("review", "评论"),
        ("traffic", "流量"),
    ):
        if needle in name and label not in dimensions:
            dimensions.append(label)
    return dimensions or ["业务数据"]


def _quality(
    status: str,
    *,
    accepted_rows: Iterable[int] = (),
    rejected_rows: Iterable[int] = (),
    supported_dimensions: Iterable[str] = (),
    unsupported_claims: Iterable[str] = (),
    reason: str,
    source: str,
) -> dict[str, Any]:
    if status not in EVIDENCE_QUALITY_STATUSES:
        raise ValueError(f"Unsupported evidence quality status: {status}")
    return {
        "status": status,
        "accepted_rows": sorted({int(index) for index in accepted_rows if int(index) > 0}),
        "rejected_rows": sorted({int(index) for index in rejected_rows if int(index) > 0}),
        "supported_dimensions": _unique(str(item) for item in supported_dimensions),
        "unsupported_claims": _unique(str(item) for item in unsupported_claims),
        "reason": str(reason or "").strip(),
        "source": source,
    }


def deterministic_evidence_quality(
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return a deterministic verdict and optional compact Flash judge input."""
    dimensions = _dimensions(tool_name)
    state = str(result.get("data_state") or "").strip().casefold()
    if result.get("ok") is not True or state == "error":
        return _quality(
            "error", reason="工具调用失败，不能形成业务证据。", source="deterministic",
            unsupported_claims=dimensions,
        ), None
    if state == "empty" or result.get("enough_data") is False:
        return _quality(
            "empty", reason="调用成功，但在本次精确查询范围内没有返回记录。", source="deterministic",
            unsupported_claims=dimensions,
        ), None

    payload = _business_payload(result)
    rows, row_path = _find_record_collection(payload)
    exact_ids = _exact_ids(arguments)
    tool_key = str(tool_name or "").casefold()
    filter_scoped_ids = any(
        marker in tool_key
        for marker in ("search", "rank", "ranking", "list", "distribution", "matrix", "research")
    )
    identity_ids = {
        key: values
        for key, values in exact_ids.items()
        if not (filter_scoped_ids and key.startswith("categoryid"))
    }
    returned_ids = {
        key: _unique(_walk_named_values(payload, frozenset({key})))
        for key in identity_ids
    }
    mismatched: list[str] = []
    for key, expected_values in identity_ids.items():
        actual_values = returned_ids.get(key) or []
        if not actual_values:
            continue
        expected = {value.casefold() for value in expected_values}
        actual = {value.casefold() for value in actual_values}
        if not expected & actual:
            mismatched.append(key)
    if mismatched:
        row_count = len(rows or [])
        return _quality(
            "off_topic",
            rejected_rows=range(1, row_count + 1),
            reason="返回记录中的精确对象编号与请求对象不一致。",
            source="deterministic",
            unsupported_claims=dimensions,
        ), None

    scope_conflicts: list[str] = []
    for scope_family, scope_keys in (
        ("地区", frozenset({"region", "country", "marketplace", "market", "site"})),
        ("统计周期", frozenset({"date", "week", "month", "period", "statdate", "datevalue"})),
        ("币种", frozenset({"currency", "currencycode"})),
    ):
        requested_scope = {
            value.casefold() for value in _walk_named_values(arguments, scope_keys)
        }
        returned_scope = {
            value.casefold() for value in _walk_named_values(payload, scope_keys)
        }
        compatible = bool(requested_scope & returned_scope)
        if scope_family == "统计周期" and not compatible:
            compatible = any(
                requested.startswith(returned) or returned.startswith(requested)
                for requested in requested_scope
                for returned in returned_scope
            )
        if requested_scope and returned_scope and not compatible:
            scope_conflicts.append(scope_family)
    if scope_conflicts:
        row_count = len(rows or [])
        return _quality(
            "off_topic",
            rejected_rows=range(1, row_count + 1),
            reason="返回内容的" + "、".join(scope_conflicts) + "与请求范围不一致。",
            source="deterministic",
            unsupported_claims=dimensions,
        ), None

    if identity_ids:
        return _quality(
            "accepted",
            accepted_rows=range(1, len(rows or []) + 1),
            reason=(
                "精确对象接口未返回与请求冲突的对象或范围；"
                "返回中的汇总、趋势或关联记录归属于该次精确调用。"
            ),
            source="deterministic",
            supported_dimensions=dimensions,
        ), None

    identities = [_row_identity(row) for row in (rows or [])]
    accepted_rows = [index for index, identity in enumerate(identities, 1) if identity]
    rejected_rows = [index for index, identity in enumerate(identities, 1) if not identity]
    if rows is not None and rows and not accepted_rows:
        return _quality(
            "identity_missing",
            rejected_rows=range(1, len(rows) + 1),
            reason="返回记录缺少关键词、标题、编号或日期等必要身份，不能确定指标归属。",
            source="deterministic",
            unsupported_claims=dimensions,
        ), None
    if rejected_rows:
        return _quality(
            "partial",
            accepted_rows=accepted_rows,
            rejected_rows=rejected_rows,
            reason="部分记录具有明确身份，缺少身份的记录不进入正面证据。",
            source="deterministic",
            supported_dimensions=dimensions,
            unsupported_claims=["缺少身份记录对应的业务指标"],
        ), None

    query_terms = _query_terms(arguments)
    is_discovery = any(hint in tool_key for hint in _LIST_HINTS)
    is_detail = any(hint in tool_key for hint in _DETAIL_HINTS)
    if rows is None and (is_detail or not is_discovery):
        return _quality(
            "accepted",
            reason="返回为有数据的汇总或实体分析结果，未发现对象冲突。",
            source="deterministic",
            supported_dimensions=dimensions,
        ), None

    if rows is not None and not query_terms and not identity_ids:
        # Rankings and broad discovery are scoped by their request filters; they
        # do not need a semantic similarity judgment.
        return _quality(
            "accepted",
            accepted_rows=range(1, len(rows) + 1),
            reason="榜单或发现请求未指定文本对象，返回记录均具有可追溯身份。",
            source="deterministic",
            supported_dimensions=dimensions,
        ), None

    compact_rows = [
        {"row": index, "identity": identity}
        for index, identity in enumerate(identities, 1)
        if identity
    ]
    pending = {
        "tool_name": tool_name,
        "query_scope": {
            "terms": query_terms,
            "exact_ids": exact_ids,
            "scope": {
                key: values
                for key in _SCOPE_KEYS
                if (values := _walk_named_values(arguments, frozenset({key})))
            },
        },
        "record_path": list(row_path),
        "records": compact_rows,
        "supported_dimensions": dimensions,
    }
    return _quality(
        "uncertain",
        reason="搜索或发现结果与自然语言查询的相关性需要模型判定。",
        source="fallback",
        unsupported_claims=dimensions,
    ), pending


def validate_flash_verdict(pending: dict[str, Any], verdict: Any) -> dict[str, Any] | None:
    if not isinstance(verdict, dict):
        return None
    status = str(verdict.get("status") or "").strip()
    if status not in {"accepted", "partial", "off_topic", "identity_missing", "scope_uncertain"}:
        return None
    valid_rows = {
        int(record["row"])
        for record in pending.get("records") or []
        if isinstance(record, dict) and str(record.get("row") or "").isdigit()
    }

    def rows(name: str) -> list[int]:
        values = verdict.get(name)
        if not isinstance(values, list):
            return []
        return sorted({
            int(value) for value in values
            if str(value).isdigit() and int(value) in valid_rows
        })

    accepted = rows("accepted_rows")
    rejected = rows("rejected_rows")
    if status == "accepted":
        accepted = sorted(valid_rows)
        rejected = []
    elif status in {"off_topic", "identity_missing", "scope_uncertain"}:
        accepted = []
        rejected = sorted(valid_rows)
    elif status == "partial":
        if not accepted or not rejected or set(accepted) & set(rejected):
            return None
        if set(accepted) | set(rejected) != valid_rows:
            return None
    return _quality(
        status,
        accepted_rows=accepted,
        rejected_rows=rejected,
        supported_dimensions=pending.get("supported_dimensions") if status in {"accepted", "partial"} else (),
        unsupported_claims=verdict.get("unsupported_claims") or (
            [] if status == "accepted" else pending.get("supported_dimensions") or []
        ),
        reason=str(verdict.get("reason") or "相关性判定未提供说明。"),
        source="v4_flash",
    )


def uncertain_evidence_quality(pending: dict[str, Any], reason: str) -> dict[str, Any]:
    return _quality(
        "uncertain",
        reason=reason,
        source="fallback",
        unsupported_claims=pending.get("supported_dimensions") or ["业务数据"],
    )


def evidence_quality_observed(result: Any) -> bool:
    quality = result.get("evidence_quality") if isinstance(result, dict) else None
    return isinstance(quality, dict) and quality.get("status") in EVIDENCE_QUALITY_OBSERVED_STATUSES


def evidence_quality_allows_entities(result: Any) -> bool:
    quality = result.get("evidence_quality") if isinstance(result, dict) else None
    return isinstance(quality, dict) and quality.get("status") in EVIDENCE_QUALITY_ENTITY_STATUSES


def _replace_path(value: Any, path: tuple[Any, ...], replacement: Any) -> Any:
    if not path:
        return replacement
    output = copy.deepcopy(value)
    cursor = output
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = replacement
    return output


def admitted_business_payload(result: dict[str, Any]) -> Any:
    """Return the report/planner view while leaving the stored result untouched."""
    payload = _business_payload(result)
    quality = result.get("evidence_quality") if isinstance(result, dict) else None
    if not isinstance(quality, dict):
        return payload
    status = quality.get("status")
    if status == "accepted":
        return payload
    if status == "partial":
        rows, path = _find_record_collection(payload)
        if rows is None:
            return {}
        accepted = {
            int(index) for index in quality.get("accepted_rows") or []
            if str(index).isdigit()
        }
        filtered = [row for index, row in enumerate(rows, 1) if index in accepted]
        return _replace_path(payload, path, filtered)
    return {}


def evidence_quality_prompt(quality: dict[str, Any] | None) -> str:
    if not isinstance(quality, dict):
        return ""
    status = str(quality.get("status") or "uncertain")
    labels = {
        "accepted": "有效",
        "partial": "部分有效",
        "off_topic": "返回偏题",
        "identity_missing": "记录身份缺失",
        "scope_uncertain": "范围无法确认",
        "empty": "精确范围内为空",
        "error": "调用失败",
        "uncertain": "相关性未确认",
    }
    accepted = quality.get("accepted_rows") or []
    rejected = quality.get("rejected_rows") or []
    suffix = []
    if accepted:
        suffix.append("可用记录：" + "、".join(str(item) for item in accepted))
    if rejected:
        suffix.append("不可用记录：" + "、".join(str(item) for item in rejected))
    detail = "；".join(suffix)
    return (
        f"证据准入状态：{labels.get(status, '相关性未确认')}。"
        f"{quality.get('reason') or ''}"
        + (f" {detail}。" if detail else "")
        + (
            " 只有可用记录可以支撑业务事实或提供后续深挖编号。"
            if status == "partial"
            else " 本结果不得作为正面业务证据，也不得提供新的深挖编号。"
            if status not in {"accepted", "partial"}
            else ""
        )
    )


def flash_judge_payload(pending_items: list[dict[str, Any]]) -> str:
    """Serialize the intentionally metric-free batch used by V4 Flash."""
    return json.dumps({"calls": pending_items}, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "EVIDENCE_QUALITY_STATUSES",
    "admitted_business_payload",
    "deterministic_evidence_quality",
    "evidence_quality_allows_entities",
    "evidence_quality_observed",
    "evidence_quality_prompt",
    "flash_judge_payload",
    "uncertain_evidence_quality",
    "validate_flash_verdict",
]
