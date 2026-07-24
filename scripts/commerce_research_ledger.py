#!/usr/bin/env python3
"""Durable SellerSprite research control state.

The ledger is deliberately independent from HTTP, MCP and model clients.  It
owns stable answer slots and accepts small, validated progress deltas produced
after a real tool response has passed the evidence contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from typing import Any, Iterable, Mapping


LEDGER_VERSION = 1
SLOT_STATES = frozenset({
    "pending", "active", "partial", "supported", "conflicted",
    "unavailable", "not_applicable", "blocked",
})
TERMINAL_SLOT_STATES = frozenset({
    "supported", "conflicted", "unavailable", "not_applicable", "blocked",
})
SLOT_PRIORITIES = frozenset({"core", "supporting"})

CAPABILITY_FACTS: dict[str, tuple[str, ...]] = {
    "keyword_discovery": (
        "关键词身份", "统计周期", "搜索量", "购买量", "购买率",
        "商品供给", "竞争程度", "价格",
    ),
    "market_discovery": (
        "类目身份", "类目编号", "统计周期", "市场规模",
        "竞争程度", "价格", "卖家结构",
    ),
    "trend_validation": ("对象身份", "统计时间", "趋势值"),
    "product_discovery": (
        "亚马逊商品编号", "商品标题", "统计周期", "价格",
        "销量", "销售额", "评分", "类目排名",
    ),
    "category_resolution": ("类目身份", "类目编号"),
    "market_validation": (
        "类目身份", "类目编号", "统计周期", "统计时间", "趋势值",
        "市场规模", "集中度", "价格分布", "卖家结构",
    ),
    "asin_detail": (
        "亚马逊商品编号", "商品标题", "统计周期", "统计时间",
        "趋势值", "价格", "销量", "销售额", "评分", "类目排名",
    ),
    "asin_review": ("亚马逊商品编号", "评论身份", "评论时间", "评论星级", "评论内容"),
    "asin_traffic": ("亚马逊商品编号", "关键词身份", "流量来源", "自然排名"),
    "trademark": ("商标身份", "商标状态", "商标国家"),
}

ENTITY_FACTS = frozenset({
    "对象身份", "关键词身份", "亚马逊商品编号", "类目身份", "类目编号",
    "评论身份", "商标身份",
})


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _normalized_scope(value: Mapping[str, Any] | None) -> dict[str, str]:
    value = value or {}
    return {
        "entity": _text(value.get("entity")),
        "region": _text(value.get("region")).upper(),
        "period": _text(value.get("period")),
    }


def validate_generated_slots(
    payload: Any,
    *,
    allowed_capabilities: Iterable[str],
) -> list[dict[str, Any]] | None:
    """Validate model-created slots without letting it invent capabilities."""
    raw_slots = payload.get("slots") if isinstance(payload, Mapping) else None
    if not isinstance(raw_slots, list) or not raw_slots or len(raw_slots) > 16:
        return None
    allowed = {str(item) for item in allowed_capabilities}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_slots, 1):
        if not isinstance(raw, Mapping):
            return None
        slot_id = _text(raw.get("id")) or f"slot-{index}"
        if not re.fullmatch(r"slot-[1-9]\d*", slot_id) or slot_id in seen:
            return None
        seen.add(slot_id)
        topic = _text(raw.get("topic"))
        priority = _text(raw.get("priority"))
        required_facts = _string_list(raw.get("required_facts"))
        capabilities = [
            item for item in _string_list(raw.get("acceptable_capabilities"))
            if item in allowed
        ]
        allowed_facts = {
            fact for capability in capabilities
            for fact in CAPABILITY_FACTS.get(capability, ())
        }
        if (
            not topic
            or priority not in SLOT_PRIORITIES
            or not required_facts
            or not capabilities
            or not set(required_facts).issubset(allowed_facts)
        ):
            return None
        result.append({
            "id": slot_id,
            "topic": topic,
            "priority": priority,
            "state": "pending",
            "entity_scope": _normalized_scope(
                raw.get("entity_scope") if isinstance(raw.get("entity_scope"), Mapping) else {}
            ),
            "required_facts": required_facts,
            "acceptable_capabilities": capabilities,
            "observed_facts": [],
            "missing_facts": list(required_facts),
            "evidence_refs": [],
            "exhausted_capabilities": [],
            "boundaries": _string_list(raw.get("boundaries")),
            "created_order": index,
        })
    if not any(slot["priority"] == "core" for slot in result):
        return None
    return result


def fallback_slots(
    research_task: Mapping[str, Any] | None,
    *,
    allowed_capabilities: Iterable[str],
) -> list[dict[str, Any]]:
    """Small deterministic fallback used only when slot generation fails."""
    task = dict(research_task or {})
    objective = _text(task.get("objective")) or "research"
    entity_type = _text(task.get("entity_type")) or "none"
    scope = _normalized_scope({
        "entity": task.get("entity"),
        "region": task.get("region"),
        "period": task.get("time_range") or task.get("period"),
    })
    allowed = {str(item) for item in allowed_capabilities}
    capability_order: list[str]
    if entity_type == "asin":
        capability_order = ["asin_detail", "asin_review", "asin_traffic"]
    elif entity_type in {"category", "node"}:
        capability_order = ["category_resolution", "market_validation", "product_discovery"]
    elif entity_type == "keyword":
        capability_order = ["keyword_discovery", "trend_validation", "product_discovery"]
    elif objective in {"competitor_analysis", "pricing_analysis"}:
        capability_order = ["product_discovery", "asin_detail", "asin_review"]
    else:
        capability_order = [
            "keyword_discovery", "market_discovery", "product_discovery",
            "trend_validation", "market_validation",
        ]
    capabilities = [item for item in capability_order if item in allowed]
    if not capabilities:
        capabilities = sorted(allowed)[:1]
    slots: list[dict[str, Any]] = []
    for index, capability in enumerate(capabilities, 1):
        facts = list(CAPABILITY_FACTS.get(capability) or ("业务事实",))
        slots.append({
            "id": f"slot-{index}",
            "topic": {
                "keyword_discovery": "目标需求与购买转化",
                "market_discovery": "相关类目与市场范围",
                "product_discovery": "代表商品与竞争样本",
                "category_resolution": "准确类目身份",
                "market_validation": "市场规模与竞争结构",
                "trend_validation": "近期趋势变化",
                "asin_detail": "商品经营表现",
                "asin_review": "商品评论反馈",
                "asin_traffic": "商品流量结构",
                "trademark": "商标状态",
            }.get(capability, "必要业务证据"),
            "priority": "core" if index <= 3 or len(capabilities) <= 3 else "supporting",
            "state": "pending",
            "entity_scope": dict(scope),
            "required_facts": facts,
            "acceptable_capabilities": [capability],
            "observed_facts": [],
            "missing_facts": list(facts),
            "evidence_refs": [],
            "exhausted_capabilities": [],
            "boundaries": [],
            "created_order": index,
        })
    return slots


def _compatible_scope(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    a = _normalized_scope(left)
    b = _normalized_scope(right)
    for key in ("entity", "region", "period"):
        if a[key] and b[key] and a[key].casefold() != b[key].casefold():
            return False
    return True


def compatible_inherited_evidence(
    previous_state: Mapping[str, Any] | None,
    new_task: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(previous_state, Mapping):
        return []
    new_scope = {
        "entity": (new_task or {}).get("entity"),
        "region": (new_task or {}).get("region"),
        "period": (new_task or {}).get("time_range") or (new_task or {}).get("period"),
    }
    inherited: list[dict[str, Any]] = []
    for ref in previous_state.get("evidence_refs") or []:
        if (
            isinstance(ref, Mapping)
            and str(ref.get("quality_status")) in {"accepted", "partial"}
            and _compatible_scope(
                ref.get("entity_scope") if isinstance(ref.get("entity_scope"), Mapping) else {},
                new_scope,
            )
        ):
            inherited.append(copy.deepcopy(dict(ref)))
    return inherited


def create_research_ledger(
    research_task: Mapping[str, Any] | None,
    slots: list[dict[str, Any]],
    *,
    previous_state: Mapping[str, Any] | None = None,
    inherit_compatible: bool = False,
) -> dict[str, Any]:
    inherited = (
        compatible_inherited_evidence(previous_state, research_task)
        if inherit_compatible else []
    )
    ledger = {
        "version": LEDGER_VERSION,
        "task_id": "research-" + uuid.uuid4().hex,
        "inherited_from": (
            str(previous_state.get("task_id"))
            if inherited and isinstance(previous_state, Mapping)
            else None
        ),
        "research_task": copy.deepcopy(dict(research_task or {})),
        "status": "researching",
        "active_slot_id": None,
        "slots": copy.deepcopy(slots),
        "attempts": [],
        "evidence_refs": inherited,
        "entities": {},
        "progress_hash": "",
        "no_progress_rounds": 0,
        "report_contract": {},
        "completion_review_count": 0,
    }
    _apply_inherited_evidence(ledger)
    select_active_slot(ledger)
    ledger["progress_hash"] = progress_hash(ledger)
    return ledger


def _apply_inherited_evidence(ledger: dict[str, Any]) -> None:
    for ref in ledger.get("evidence_refs") or []:
        facts = set(_string_list(ref.get("observed_facts")))
        for slot in ledger.get("slots") or []:
            if not _compatible_scope(
                slot.get("entity_scope") or {}, ref.get("entity_scope") or {}
            ):
                continue
            relevant = facts.intersection(slot.get("required_facts") or [])
            if relevant:
                slot["observed_facts"] = sorted(set(slot.get("observed_facts") or []).union(relevant))
                slot["evidence_refs"] = sorted(set(slot.get("evidence_refs") or []).union({
                    str(ref.get("source_ref"))
                }))
                _refresh_slot_state(slot)


def active_slot(ledger: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(ledger, Mapping):
        return None
    active_id = str(ledger.get("active_slot_id") or "")
    for slot in ledger.get("slots") or []:
        if isinstance(slot, dict) and str(slot.get("id")) == active_id:
            return slot
    return None


def select_active_slot(ledger: dict[str, Any]) -> dict[str, Any] | None:
    current = active_slot(ledger)
    if current and current.get("state") not in TERMINAL_SLOT_STATES:
        current["state"] = "active" if not current.get("observed_facts") else "partial"
        return current
    candidates = [
        slot for slot in ledger.get("slots") or []
        if isinstance(slot, dict) and slot.get("state") not in TERMINAL_SLOT_STATES
    ]
    candidates.sort(key=lambda item: (
        0 if item.get("priority") == "core" else 1,
        int(item.get("created_order") or 0),
    ))
    if not candidates:
        ledger["active_slot_id"] = None
        ledger["status"] = "candidate_complete" if core_slots_terminal(ledger) else "blocked"
        return None
    chosen = candidates[0]
    chosen["state"] = "active" if not chosen.get("observed_facts") else "partial"
    ledger["active_slot_id"] = chosen["id"]
    # Re-projecting a previously retained full response is represented by
    # reusing its fact-bearing evidence reference.  No MCP call is needed.
    for ref in ledger.get("evidence_refs") or []:
        if not isinstance(ref, Mapping):
            continue
        if str(ref.get("capability") or "") not in set(chosen.get("acceptable_capabilities") or []):
            continue
        if not _compatible_scope(
            chosen.get("entity_scope") or {}, ref.get("entity_scope") or {}
        ):
            continue
        relevant = set(ref.get("observed_facts") or []).intersection(
            chosen.get("required_facts") or []
        )
        if relevant:
            chosen["observed_facts"] = sorted(
                set(chosen.get("observed_facts") or []).union(relevant)
            )
            source_ref = str(ref.get("source_ref") or "")
            if source_ref and source_ref not in chosen.setdefault("evidence_refs", []):
                chosen["evidence_refs"].append(source_ref)
    _refresh_slot_state(chosen)
    if chosen.get("state") in TERMINAL_SLOT_STATES:
        return select_active_slot(ledger)
    return chosen


def active_capabilities(ledger: Mapping[str, Any] | None) -> set[str]:
    slot = active_slot(ledger)
    if not slot:
        return set()
    exhausted = set(slot.get("exhausted_capabilities") or [])
    return {
        str(item) for item in slot.get("acceptable_capabilities") or []
        if str(item) and str(item) not in exhausted
    }


def active_required_facts(ledger: Mapping[str, Any] | None) -> list[str]:
    slot = active_slot(ledger)
    if not slot:
        return []
    observed = set(slot.get("observed_facts") or [])
    return [item for item in slot.get("required_facts") or [] if item not in observed]


def planner_instruction(ledger: Mapping[str, Any]) -> str:
    slot = active_slot(ledger)
    if not slot:
        return (
            "SellerSprite研究台账中已没有可激活的核心答案槽位。"
            "不要再提出工具调用，等待系统进入候选完成审核和报告阶段。"
        )
    return (
        "SellerSprite研究由持久化台账总控。你只推进当前答案槽位，不得创建新缺口或跳到其他槽位。"
        f"当前槽位编号：{slot['id']}；主题：{slot['topic']}；"
        f"对象范围：{json.dumps(slot.get('entity_scope') or {}, ensure_ascii=False)}；"
        f"仍缺事实：{'、'.join(active_required_facts(ledger)) or '无'}；"
        f"可用业务能力：{'、'.join(sorted(active_capabilities(ledger))) or '无'}。"
        "请从当前开放工具中选择能补充这些事实的具体调用。"
        "模型不得填写或修改returnFields，返回字段由台账按事实契约编译。"
        "如果当前工具无法补充事实，可以停止提议；不得自行写最终报告。"
    )


def _refresh_slot_state(slot: dict[str, Any]) -> None:
    required = set(slot.get("required_facts") or [])
    observed = set(slot.get("observed_facts") or [])
    slot["missing_facts"] = [item for item in slot.get("required_facts") or [] if item not in observed]
    if required and required.issubset(observed):
        slot["state"] = "supported"
    elif set(slot.get("acceptable_capabilities") or []).issubset(
        set(slot.get("exhausted_capabilities") or [])
    ):
        slot["state"] = "unavailable"
    elif observed:
        slot["state"] = "partial"
    elif slot.get("state") not in TERMINAL_SLOT_STATES:
        slot["state"] = "active"


def progress_hash(ledger: Mapping[str, Any]) -> str:
    stable = {
        "slots": [{
            "id": slot.get("id"),
            "state": slot.get("state"),
            "observed_facts": sorted(slot.get("observed_facts") or []),
            "evidence_refs": sorted(slot.get("evidence_refs") or []),
        } for slot in ledger.get("slots") or [] if isinstance(slot, Mapping)],
        "entities": ledger.get("entities") or {},
        "evidence_refs": sorted(
            str(ref.get("source_ref"))
            for ref in ledger.get("evidence_refs") or []
            if isinstance(ref, Mapping) and ref.get("source_ref")
        ),
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def apply_progress_delta(
    ledger: dict[str, Any],
    *,
    source_ref: str,
    tool_name: str,
    capability: str,
    arguments: Mapping[str, Any],
    quality_status: str,
    observed_facts: Iterable[str],
    projected_data: Any,
    entity_scope: Mapping[str, Any] | None = None,
    boundaries: Iterable[str] = (),
    projection_diagnostics: Mapping[str, Any] | None = None,
    slot_id: str | None = None,
) -> bool:
    """Atomically record one real attempt and reduce it into the active slot."""
    before = progress_hash(ledger)
    slot = next((
        item for item in ledger.get("slots") or []
        if isinstance(item, dict) and str(item.get("id")) == str(slot_id or "")
    ), None) if slot_id else active_slot(ledger)
    slot_id = str(slot.get("id")) if slot else ""
    quality = str(quality_status or "uncertain")
    facts = sorted(set(_string_list(list(observed_facts))))
    attempt = {
        "source_ref": source_ref,
        "slot_id": slot_id,
        "tool_name": str(tool_name),
        "capability": str(capability),
        "arguments": copy.deepcopy(dict(arguments)),
        "quality_status": quality,
    }
    ledger.setdefault("attempts", []).append(attempt)
    if slot is not None:
        if quality in {"accepted", "partial"} and facts:
            slot["observed_facts"] = sorted(set(slot.get("observed_facts") or []).union(facts))
            if source_ref not in slot.setdefault("evidence_refs", []):
                slot["evidence_refs"].append(source_ref)
            ref = {
                "source_ref": source_ref,
                "slot_id": slot_id,
                "tool_name": str(tool_name),
                "capability": str(capability),
                "quality_status": quality,
                "observed_facts": facts,
                "entity_scope": _normalized_scope(entity_scope),
                "arguments": copy.deepcopy(dict(arguments)),
                "projected_data": copy.deepcopy(projected_data),
                "boundaries": _string_list(list(boundaries)),
                "projection": copy.deepcopy(dict(projection_diagnostics or {})),
            }
            ledger.setdefault("evidence_refs", []).append(ref)
        elif quality in {"empty", "error"}:
            slot.setdefault("boundaries", []).append(
                f"{tool_name} 在本次精确对象和参数范围内"
                + ("返回为空。" if quality == "empty" else "调用失败。")
            )
        elif quality in {"off_topic", "identity_missing", "scope_uncertain", "uncertain"}:
            slot.setdefault("boundaries", []).append(
                f"{tool_name} 返回未通过当前槽位的身份或范围校验。"
            )
        _refresh_slot_state(slot)
    after = progress_hash(ledger)
    changed = before != after
    ledger["progress_hash"] = after
    return changed


def finish_no_progress_batch(
    ledger: dict[str, Any],
    *,
    attempted_capabilities: Iterable[str],
    before_hash: str,
) -> None:
    """Exhaust a stalled capability path after two approved no-progress batches."""
    current_hash = progress_hash(ledger)
    if current_hash != str(before_hash or ""):
        ledger["no_progress_rounds"] = 0
        ledger["progress_hash"] = current_hash
        select_active_slot(ledger)
        return
    ledger["no_progress_rounds"] = int(ledger.get("no_progress_rounds") or 0) + 1
    ledger["progress_hash"] = current_hash
    if int(ledger.get("no_progress_rounds") or 0) < 2:
        select_active_slot(ledger)
        return
    slot = active_slot(ledger)
    if slot is None:
        ledger["status"] = "blocked"
        return
    exhausted = slot.setdefault("exhausted_capabilities", [])
    for capability in attempted_capabilities:
        value = str(capability)
        if value and value not in exhausted:
            exhausted.append(value)
    _refresh_slot_state(slot)
    if slot.get("state") == "unavailable":
        ledger["no_progress_rounds"] = 0
        select_active_slot(ledger)
    if active_slot(ledger) is None and not core_slots_terminal(ledger):
        ledger["status"] = "blocked"
    else:
        select_active_slot(ledger)


def core_slots_terminal(ledger: Mapping[str, Any]) -> bool:
    core = [
        slot for slot in ledger.get("slots") or []
        if isinstance(slot, Mapping) and slot.get("priority") == "core"
    ]
    return bool(core) and all(slot.get("state") in TERMINAL_SLOT_STATES for slot in core)


def candidate_complete(ledger: dict[str, Any]) -> bool:
    if not core_slots_terminal(ledger):
        return False
    ledger["status"] = "candidate_complete"
    ledger["active_slot_id"] = None
    ledger["report_contract"] = {
        "must_cover": [
            str(slot.get("topic")) for slot in ledger.get("slots") or []
            if isinstance(slot, Mapping) and slot.get("priority") == "core"
        ],
        "must_compare": [],
        "must_state_as_limit": [
            str(slot.get("topic")) for slot in ledger.get("slots") or []
            if isinstance(slot, Mapping)
            and slot.get("state") in {"unavailable", "blocked", "conflicted"}
        ],
        "forbidden_claims": ["证据中不存在的数字、因果关系和市场外推"],
    }
    return True


def reject_candidate_completion(
    ledger: dict[str, Any],
    *,
    slot_id: str,
    reason: str,
) -> bool:
    if int(ledger.get("completion_review_count") or 0) >= 2:
        ledger["status"] = "blocked"
        return False
    target = next((
        slot for slot in ledger.get("slots") or []
        if isinstance(slot, dict) and str(slot.get("id")) == str(slot_id)
    ), None)
    if target is None:
        return False
    ledger["completion_review_count"] = int(ledger.get("completion_review_count") or 0) + 1
    rejected_refs = set(target.get("evidence_refs") or [])
    for ref in ledger.get("evidence_refs") or []:
        if isinstance(ref, dict) and str(ref.get("source_ref") or "") in rejected_refs:
            ref["candidate_rejected"] = True
            ref.setdefault("boundaries", []).append(_text(reason))
    target["observed_facts"] = []
    target["missing_facts"] = list(target.get("required_facts") or [])
    target["evidence_refs"] = []
    target["state"] = "pending"
    target.setdefault("boundaries", []).append(_text(reason))
    ledger["status"] = "researching"
    ledger["active_slot_id"] = target["id"]
    ledger["progress_hash"] = progress_hash(ledger)
    return True


def mark_ready(ledger: dict[str, Any]) -> None:
    ledger["status"] = "ready"
    ledger["active_slot_id"] = None


__all__ = [
    "CAPABILITY_FACTS",
    "LEDGER_VERSION",
    "SLOT_STATES",
    "TERMINAL_SLOT_STATES",
    "active_capabilities",
    "active_required_facts",
    "active_slot",
    "apply_progress_delta",
    "candidate_complete",
    "compatible_inherited_evidence",
    "core_slots_terminal",
    "create_research_ledger",
    "fallback_slots",
    "finish_no_progress_batch",
    "mark_ready",
    "planner_instruction",
    "progress_hash",
    "reject_candidate_completion",
    "select_active_slot",
    "validate_generated_slots",
]
