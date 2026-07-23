#!/usr/bin/env python3
"""Pre-call feasibility checks for commerce MCP tools.

This module deliberately has no network or execution dependencies.  It validates
runtime JSON schemas, builds the metric-free V4 Flash review payload, and
strictly validates the model's approve/reject decisions.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any


CALL_GATE_DECISIONS = frozenset({"approve", "reject"})
CALL_GATE_DECISION_KEYS = frozenset({
    "call_key", "decision", "reason", "unmet_preconditions",
})


def _json_type_matches(expected: str, value: Any) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _resolve_local_ref(root: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    current: Any = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, dict) else None


def validate_json_schema(
    schema: dict[str, Any] | None,
    value: Any,
    *,
    root_schema: dict[str, Any] | None = None,
    path: str = "$",
) -> list[str]:
    """Validate the execution-relevant subset of JSON Schema used by MCP."""
    if not isinstance(schema, dict):
        return []
    root = root_schema or schema
    if isinstance(schema.get("$ref"), str):
        resolved = _resolve_local_ref(root, schema["$ref"])
        return (
            validate_json_schema(resolved, value, root_schema=root, path=path)
            if resolved is not None
            else [f"{path}: unresolved schema reference {schema['$ref']}"]
        )

    errors: list[str] = []
    for keyword in ("allOf",):
        alternatives = schema.get(keyword)
        if isinstance(alternatives, list):
            for item in alternatives:
                errors.extend(validate_json_schema(item, value, root_schema=root, path=path))
    for keyword in ("anyOf", "oneOf"):
        alternatives = schema.get(keyword)
        if isinstance(alternatives, list) and alternatives:
            matched = sum(
                not validate_json_schema(item, value, root_schema=root, path=path)
                for item in alternatives
            )
            if (keyword == "anyOf" and matched == 0) or (keyword == "oneOf" and matched != 1):
                errors.append(f"{path}: does not satisfy {keyword}")
                return errors

    expected = schema.get("type")
    allowed_types = [expected] if isinstance(expected, str) else expected if isinstance(expected, list) else []
    if allowed_types and not any(_json_type_matches(item, value) for item in allowed_types):
        errors.append(f"{path}: expected {'|'.join(str(item) for item in allowed_types)}")
        return errors
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: value does not match const")
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required field is missing")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                errors.extend(
                    validate_json_schema(child_schema, item, root_schema=root, path=f"{path}.{key}")
                )
            elif additional is False:
                errors.append(f"{path}.{key}: additional property is not allowed")
            elif isinstance(additional, dict):
                errors.extend(
                    validate_json_schema(additional, item, root_schema=root, path=f"{path}.{key}")
                )
        minimum = schema.get("minProperties")
        maximum = schema.get("maxProperties")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path}: fewer than {minimum} properties")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path}: more than {maximum} properties")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path}: fewer than {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path}: more than {maximum} items")
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(set(encoded)) != len(encoded):
                errors.append(f"{path}: items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    validate_json_schema(item_schema, item, root_schema=root, path=f"{path}[{index}]")
                )

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path}: shorter than {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path}: longer than {maximum} characters")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    errors.append(f"{path}: does not match required pattern")
            except re.error:
                errors.append(f"{path}: schema contains an invalid pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for keyword, comparator, label in (
            ("minimum", lambda actual, bound: actual >= bound, "minimum"),
            ("maximum", lambda actual, bound: actual <= bound, "maximum"),
            ("exclusiveMinimum", lambda actual, bound: actual > bound, "exclusive minimum"),
            ("exclusiveMaximum", lambda actual, bound: actual < bound, "exclusive maximum"),
        ):
            bound = schema.get(keyword)
            if isinstance(bound, (int, float)) and not comparator(value, bound):
                errors.append(f"{path}: violates {label} {bound}")
        multiple = schema.get("multipleOf")
        if isinstance(multiple, (int, float)) and multiple > 0:
            quotient = value / multiple
            if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                errors.append(f"{path}: is not a multiple of {multiple}")
    return errors


def build_call_gate_candidates(
    tool_calls: list[dict[str, Any]],
    model_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a compact, immutable view of proposed calls for the judge."""
    tool_index = {
        str((tool.get("function") or {}).get("name") or ""): tool.get("function") or {}
        for tool in model_tools
        if isinstance(tool, dict)
    }
    candidates: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls, 1):
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or "")
        try:
            arguments = json.loads(str(function.get("arguments") or "{}"))
        except (TypeError, ValueError):
            arguments = {}
        advertised = tool_index.get(name) or {}
        candidates.append({
            "call_key": f"proposal-{index}",
            "tool_name": name,
            "arguments": arguments,
            "purpose": str(advertised.get("description") or "")[:500],
            "schema": advertised.get("parameters") if isinstance(advertised.get("parameters"), dict) else {},
        })
    return candidates


def call_gate_payload(
    *,
    user_question: str,
    research_task: dict[str, Any],
    planner_state: dict[str, Any],
    confirmed_identities: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    return json.dumps({
        "用户问题": str(user_question or ""),
        "研究任务": research_task,
        "当前能力覆盖": {
            "已尝试": planner_state.get("attempted_capabilities") or [],
            "已有有效证据": planner_state.get("observed_capabilities") or [],
        },
        "已确认业务身份": confirmed_identities,
        "候选调用": candidates,
    }, ensure_ascii=False, separators=(",", ":"))


def validate_call_gate_response(
    candidates: list[dict[str, Any]],
    response: Any,
) -> list[dict[str, Any]] | None:
    """Require one immutable approve/reject decision for every candidate."""
    if not isinstance(response, dict) or set(response) != {"decisions"}:
        return None
    decisions = response.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(candidates):
        return None
    expected = {str(item.get("call_key") or "") for item in candidates}
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict) or set(item) != CALL_GATE_DECISION_KEYS:
            return None
        call_key = str(item.get("call_key") or "")
        decision = str(item.get("decision") or "")
        reason = item.get("reason")
        unmet = item.get("unmet_preconditions")
        if (
            call_key not in expected
            or call_key in seen
            or decision not in CALL_GATE_DECISIONS
            or not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(unmet, list)
            or any(not isinstance(value, str) for value in unmet)
        ):
            return None
        seen.add(call_key)
        parsed.append({
            "call_key": call_key,
            "decision": decision,
            "reason": reason.strip(),
            "unmet_preconditions": [value.strip() for value in unmet if value.strip()],
        })
    if seen != expected:
        return None
    order = {str(item["call_key"]): index for index, item in enumerate(candidates)}
    return sorted(parsed, key=lambda item: order[item["call_key"]])


__all__ = [
    "build_call_gate_candidates",
    "call_gate_payload",
    "validate_call_gate_response",
    "validate_json_schema",
]
