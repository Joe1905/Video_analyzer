#!/usr/bin/env python3
"""Evidence-sufficiency contracts for iterative commerce research.

This module intentionally contains no tool selection logic.  It validates the
business-level coverage decision that sits between tool orchestration and final
report writing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable


SUFFICIENCY_STATUSES = frozenset({"continue", "ready", "blocked"})
COVERAGE_PRIORITIES = frozenset({"core", "supporting"})
COVERAGE_STATES = frozenset({"supported", "missing", "unavailable"})
REPORT_CONTRACT_KEYS = frozenset({
    "must_cover", "must_compare", "must_state_as_limit", "forbidden_claims",
})
SUFFICIENCY_KEYS = frozenset({
    "status", "reason", "coverage_items", "missing_capabilities",
    "next_capabilities", "unsupported_claims", "report_contract",
})
COVERAGE_ITEM_KEYS = frozenset({
    "id", "topic", "priority", "state", "boundaries",
})
REPORT_REWRITE_KEYS = frozenset({
    "coverage", "removed_unsupported_claims", "report",
})
REPORT_REWRITE_COVERAGE_KEYS = frozenset({"id", "status", "reason"})
REPORT_REWRITE_COVERAGE_STATUSES = frozenset({"covered", "limitation"})


CAPABILITY_LABELS: dict[str, str] = {
    "keyword_discovery": "关键词需求发现",
    "market_discovery": "市场机会发现",
    "trend_validation": "趋势验证",
    "product_discovery": "商品样本发现",
    "category_resolution": "类目身份解析",
    "market_validation": "市场规模与竞争验证",
    "asin_detail": "亚马逊商品详情",
    "asin_traffic": "亚马逊商品流量",
    "asin_review": "亚马逊商品评论",
    "trademark": "商标信息",
    "category_discovery": "类目趋势发现",
    "category_context": "类目市场背景",
    "product_detail": "商品详情",
    "product_trend": "商品销售趋势",
    "product_content": "商品内容与达人结构",
    "shop_discovery": "店铺发现",
    "shop_detail": "店铺详情",
    "shop_analysis": "店铺经营分析",
    "creator_discovery": "达人发现",
    "creator_detail": "达人详情",
    "creator_content": "达人内容分析",
    "video_discovery": "视频发现",
    "video_detail": "视频详情",
    "live_discovery": "直播发现",
    "live_detail": "直播详情",
    "agency_discovery": "机构发现",
    "agency_detail": "机构详情",
    "reference": "业务参考信息",
}
CAPABILITY_CODES_BY_LABEL = {label: code for code, label in CAPABILITY_LABELS.items()}


def capability_label(code: Any) -> str:
    return CAPABILITY_LABELS.get(str(code or "").strip(), "其他业务证据")


def capability_labels(codes: Iterable[Any]) -> list[str]:
    labels: list[str] = []
    for code in codes:
        label = capability_label(code)
        if label not in labels:
            labels.append(label)
    return labels


def capability_codes(labels: Iterable[Any], allowed_codes: Iterable[str]) -> list[str]:
    allowed = {str(code) for code in allowed_codes}
    result: list[str] = []
    for label in labels:
        code = CAPABILITY_CODES_BY_LABEL.get(str(label or "").strip())
        if code in allowed and code not in result:
            result.append(code)
    return result


def _string_list(value: Any, *, allow_empty: bool = True) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    values = [item.strip() for item in value if item.strip()]
    if not allow_empty and not values:
        return None
    return values


def validate_sufficiency_response(
    payload: Any,
    *,
    allowed_capability_codes: Iterable[str],
) -> dict[str, Any] | None:
    """Validate one strict business-coverage decision and attach hidden codes."""
    if not isinstance(payload, dict) or set(payload) != SUFFICIENCY_KEYS:
        return None
    status = str(payload.get("status") or "").strip()
    reason = payload.get("reason")
    if status not in SUFFICIENCY_STATUSES or not isinstance(reason, str) or not reason.strip():
        return None

    raw_items = payload.get("coverage_items")
    if not isinstance(raw_items, list) or not raw_items:
        return None
    coverage_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != COVERAGE_ITEM_KEYS:
            return None
        item_id = str(raw.get("id") or "").strip()
        topic = raw.get("topic")
        priority = str(raw.get("priority") or "").strip()
        state = str(raw.get("state") or "").strip()
        boundaries = _string_list(raw.get("boundaries"))
        if (
            not re.fullmatch(r"coverage-[1-9]\d*", item_id)
            or item_id in seen_ids
            or not isinstance(topic, str)
            or not topic.strip()
            or priority not in COVERAGE_PRIORITIES
            or state not in COVERAGE_STATES
            or boundaries is None
        ):
            return None
        seen_ids.add(item_id)
        coverage_items.append({
            "id": item_id,
            "topic": topic.strip(),
            "priority": priority,
            "state": state,
            "boundaries": boundaries,
        })

    missing = _string_list(payload.get("missing_capabilities"))
    next_labels = _string_list(payload.get("next_capabilities"))
    unsupported = _string_list(payload.get("unsupported_claims"))
    report_contract = payload.get("report_contract")
    if (
        missing is None
        or next_labels is None
        or unsupported is None
        or not isinstance(report_contract, dict)
        or set(report_contract) != REPORT_CONTRACT_KEYS
    ):
        return None
    normalized_contract: dict[str, list[str]] = {}
    for key in REPORT_CONTRACT_KEYS:
        values = _string_list(report_contract.get(key))
        if values is None:
            return None
        normalized_contract[key] = values

    allowed_codes = set(str(code) for code in allowed_capability_codes)
    next_codes = capability_codes(next_labels, allowed_codes)
    missing_codes = capability_codes(missing, allowed_codes)
    if any(label not in CAPABILITY_CODES_BY_LABEL for label in next_labels + missing):
        return None
    core_missing = any(
        item["priority"] == "core" and item["state"] == "missing"
        for item in coverage_items
    )
    if status == "continue" and (not core_missing or not next_codes):
        return None
    if status == "ready" and core_missing:
        return None

    return {
        "status": status,
        "reason": reason.strip(),
        "coverage_items": coverage_items,
        "missing_capabilities": missing,
        "next_capabilities": next_labels,
        "unsupported_claims": unsupported,
        "report_contract": normalized_contract,
        "_missing_capability_codes": missing_codes,
        "_next_capability_codes": next_codes,
        "source": "v4_flash",
    }


def fallback_sufficiency(
    *,
    has_business_attempt: bool,
    allowed_capability_codes: Iterable[str],
    reason: str,
) -> dict[str, Any]:
    """Conservative fallback: require one real attempt, then report limitations."""
    allowed_codes = [str(code) for code in allowed_capability_codes]
    if not has_business_attempt and allowed_codes:
        next_codes = allowed_codes[:1]
        status = "continue"
        state = "missing"
        next_labels = capability_labels(next_codes)
    else:
        next_codes = []
        status = "blocked"
        state = "unavailable"
        next_labels = []
    return {
        "status": status,
        "reason": reason,
        "coverage_items": [{
            "id": "coverage-1",
            "topic": "使用当前站点真实业务证据回答用户问题",
            "priority": "core",
            "state": state,
            "boundaries": ["满足性模型不可用，按保守确定性底线处理"],
        }],
        "missing_capabilities": capability_labels(next_codes),
        "next_capabilities": next_labels,
        "unsupported_claims": ["缺少真实证据的商业结论"],
        "report_contract": {
            "must_cover": ["用户问题和已经取得的有效业务证据"],
            "must_compare": [],
            "must_state_as_limit": ["未取得或无法确认的证据维度"],
            "forbidden_claims": ["证据中不存在的数字、因果关系和市场外推"],
        },
        "_missing_capability_codes": next_codes,
        "_next_capability_codes": next_codes,
        "source": "fallback",
    }


def public_sufficiency_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item for key, item in value.items()
        if not str(key).startswith("_") and key != "source"
    }


def sufficiency_prompt_payload(
    *,
    user_question: str,
    research_task: dict[str, Any],
    evidence_inventory: list[dict[str, Any]],
    attempted_capability_codes: Iterable[str],
    observed_capability_codes: Iterable[str],
    available_capability_codes: Iterable[str],
) -> str:
    return json.dumps({
        "用户问题": str(user_question or ""),
        "研究任务": research_task,
        "已尝试业务能力": capability_labels(attempted_capability_codes),
        "已有有效证据能力": capability_labels(observed_capability_codes),
        "仍可使用的业务能力": capability_labels(available_capability_codes),
        "证据目录": evidence_inventory,
    }, ensure_ascii=False, separators=(",", ":"))


def validate_report_rewrite_response(
    payload: Any,
    *,
    required_coverage_ids: Iterable[str],
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or set(payload) != REPORT_REWRITE_KEYS:
        return None
    coverage = payload.get("coverage")
    removed = _string_list(payload.get("removed_unsupported_claims"))
    report = payload.get("report")
    if not isinstance(coverage, list) or removed is None or not isinstance(report, str) or not report.strip():
        return None
    required = {str(item) for item in required_coverage_ids}
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for item in coverage:
        if not isinstance(item, dict) or set(item) != REPORT_REWRITE_COVERAGE_KEYS:
            return None
        item_id = str(item.get("id") or "").strip()
        status = str(item.get("status") or "").strip()
        reason = item.get("reason")
        if (
            item_id not in required
            or item_id in seen
            or status not in REPORT_REWRITE_COVERAGE_STATUSES
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            return None
        seen.add(item_id)
        normalized.append({"id": item_id, "status": status, "reason": reason.strip()})
    if seen != required:
        return None
    return {
        "coverage": normalized,
        "removed_unsupported_claims": removed,
        "report": report.strip(),
    }


_INTERNAL_REPORT_RE = re.compile(
    r"(?:fastmoss|sellersprite|system|function)__[A-Za-z0-9_]+|"
    r"\bcall:\d+\b|<\|(?:DSML|tool_calls?)\b|"
    r"\b(?:tool_calls?|function_calls?)\s*[:=]",
    re.IGNORECASE,
)


def report_contains_internal_protocol(text: Any) -> bool:
    return bool(_INTERNAL_REPORT_RE.search(str(text or "")))


__all__ = [
    "CAPABILITY_LABELS",
    "capability_codes",
    "capability_label",
    "capability_labels",
    "fallback_sufficiency",
    "public_sufficiency_contract",
    "report_contains_internal_protocol",
    "sufficiency_prompt_payload",
    "validate_report_rewrite_response",
    "validate_sufficiency_response",
]
