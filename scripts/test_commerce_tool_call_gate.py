#!/usr/bin/env python3
"""Focused pre-call feasibility-gate regressions without external services."""

from __future__ import annotations

import json
import inspect
from types import SimpleNamespace

from commerce_tool_call_gate import (
    build_call_gate_candidates,
    validate_call_gate_response,
    validate_json_schema,
)
import web_app


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": self._content},
            }]
        }


class _FakeRequests:
    def __init__(self, content: str | None = None, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _FakeResponse(str(self.content))


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _model_tool(name: str, description: str = "研究工具") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "marketplace": {"type": "string", "enum": ["US", "UK"]},
                    "keyword": {"type": "string", "minLength": 2},
                },
                "required": ["marketplace"],
                "additionalProperties": False,
            },
        },
    }


def _apply(calls, content, cache=None, error=None):
    fake = _FakeRequests(content, error)
    message = SimpleNamespace(tool_calls=[], tool_results=[])
    original_record = web_app.record_api_call
    web_app.record_api_call = lambda *args, **kwargs: None
    try:
        outcome = web_app.apply_chat_tool_call_gate(
            calls,
            [_model_tool("sellersprite__keyword_research"), _model_tool("sellersprite__product_research")],
            "amazon",
            "分析便携风扇市场",
            {
                "intent": "amazon_analysis",
                "task_depth": "analysis",
                "research_task": {
                    "objective": "product_selection",
                    "entity": "portable fan",
                    "scope": "single_category",
                },
            },
            message,
            fake,
            "test-key",
            "https://example.invalid",
            "deepseek-v4-flash",
            cache if cache is not None else {},
        )
    finally:
        web_app.record_api_call = original_record
    return outcome, fake


def test_runtime_schema_validation_covers_types_enums_arrays_and_ranges() -> None:
    schema = {
        "type": "object",
        "properties": {
            "marketplace": {"type": "string", "enum": ["US", "UK"]},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "keywords": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 2},
            },
        },
        "required": ["marketplace", "keywords"],
        "additionalProperties": False,
    }
    assert validate_json_schema(schema, {
        "marketplace": "US", "page": 1, "keywords": ["fan"],
    }) == []
    errors = validate_json_schema(schema, {
        "marketplace": "CA", "page": 0, "keywords": [1], "extra": True,
    })
    assert any("enum" in error for error in errors)
    assert any("minimum" in error for error in errors)
    assert any("expected string" in error for error in errors)
    assert any("additional property" in error for error in errors)


def test_decision_contract_rejects_missing_extra_and_parameter_mutation() -> None:
    calls = [
        _call("call-1", "sellersprite__keyword_research", {"marketplace": "US"}),
        _call("call-2", "sellersprite__product_research", {"marketplace": "US"}),
    ]
    candidates = build_call_gate_candidates(calls, [
        _model_tool("sellersprite__keyword_research"),
        _model_tool("sellersprite__product_research"),
    ])
    valid = {
        "decisions": [
            {
                "call_key": "proposal-1", "decision": "approve",
                "reason": "补充需求证据", "unmet_preconditions": [],
            },
            {
                "call_key": "proposal-2", "decision": "reject",
                "reason": "对象不明确", "unmet_preconditions": ["类目节点"],
            },
        ]
    }
    assert validate_call_gate_response(candidates, valid) is not None
    assert validate_call_gate_response(candidates, {"decisions": valid["decisions"][:1]}) is None
    mutated = json.loads(json.dumps(valid, ensure_ascii=False))
    mutated["decisions"][0]["arguments"] = {"marketplace": "UK"}
    assert validate_call_gate_response(candidates, mutated) is None


def test_mixed_batch_executes_only_approved_subset_and_caches_rejection() -> None:
    calls = [
        _call("call-1", "sellersprite__keyword_research", {"marketplace": "US"}),
        _call("call-2", "sellersprite__product_research", {"marketplace": "US"}),
    ]
    content = json.dumps({
        "decisions": [
            {
                "call_key": "proposal-1", "decision": "approve",
                "reason": "补充需求证据", "unmet_preconditions": [],
            },
            {
                "call_key": "proposal-2", "decision": "reject",
                "reason": "缺少明确对象", "unmet_preconditions": ["关键词或类目"],
            },
        ]
    }, ensure_ascii=False)
    cache: dict[str, str] = {}
    outcome, fake = _apply(calls, content, cache)
    assert fake.calls == 1
    assert [item["id"] for item in outcome["approved"]] == ["call-1"]
    assert [item["tool_name"] for item in outcome["rejected"]] == [
        "sellersprite__product_research"
    ]
    assert len(cache) == 1

    cached_outcome, cached_fake = _apply([calls[1]], "not-used", cache)
    assert cached_fake.calls == 0
    assert cached_outcome["approved"] == []
    assert cached_outcome["rejected"][0]["source"] == "cache"


def test_gate_failure_approves_nothing() -> None:
    calls = [_call("call-1", "sellersprite__keyword_research", {"marketplace": "US"})]
    illegal, fake = _apply(calls, "not-json")
    assert fake.calls == 1
    assert illegal["failed"] is True
    assert illegal["approved"] == []

    timeout, _ = _apply(calls, None, error=TimeoutError("simulated timeout"))
    assert timeout["failed"] is True
    assert timeout["approved"] == []


def test_chat_execution_orders_hard_guards_and_gate_before_record_and_mcp() -> None:
    source = inspect.getsource(web_app.run_chat_deepseek)
    section = source[source.index("for tool_call in tool_calls:"):]
    hard_guard = section.index("guard_error =")
    llm_gate = section.index("gate_outcome = apply_chat_tool_call_gate")
    tool_record = section.index("assistant_msg.tool_calls =")
    mcp_execute = section.index("raw_result = execute_prefixed_tool")
    assert hard_guard < llm_gate < tool_record < mcp_execute
    assert "call_gate_no_approved_rounds += 1" in section
    assert "连续两轮没有获准调用" in section


def main() -> None:
    test_runtime_schema_validation_covers_types_enums_arrays_and_ranges()
    test_decision_contract_rejects_missing_extra_and_parameter_mutation()
    test_mixed_batch_executes_only_approved_subset_and_caches_rejection()
    test_gate_failure_approves_nothing()
    test_chat_execution_orders_hard_guards_and_gate_before_record_and_mcp()
    print("commerce tool call gate tests passed")


if __name__ == "__main__":
    main()
