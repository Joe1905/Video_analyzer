#!/usr/bin/env python3
"""Focused three-layer commerce-control regressions without external APIs."""

from __future__ import annotations

from commerce_evidence_sufficiency import (
    report_contains_internal_protocol,
    validate_report_rewrite_response,
    validate_sufficiency_response,
)


def _decision(status: str = "continue") -> dict:
    state = "missing" if status == "continue" else "supported"
    return {
        "status": status,
        "reason": "仍需趋势证据。" if status == "continue" else "核心证据已经覆盖。",
        "coverage_items": [{
            "id": "coverage-1",
            "topic": "判断需求趋势",
            "priority": "core",
            "state": state,
            "boundaries": ["仅适用于美国站和证据实际周期"],
        }],
        "missing_capabilities": ["趋势验证"] if status == "continue" else [],
        "next_capabilities": ["趋势验证"] if status == "continue" else [],
        "unsupported_claims": ["广告导致销量增长"],
        "report_contract": {
            "must_cover": ["需求趋势"],
            "must_compare": ["不同周期"],
            "must_state_as_limit": ["未取得广告数据"],
            "forbidden_claims": ["广告导致销量增长"],
        },
    }


def test_sufficiency_contract_controls_continue_and_ready() -> None:
    continued = validate_sufficiency_response(
        _decision("continue"),
        allowed_capability_codes={"trend_validation", "product_discovery"},
    )
    assert continued is not None
    assert continued["_next_capability_codes"] == ["trend_validation"]

    ready = validate_sufficiency_response(
        _decision("ready"),
        allowed_capability_codes={"trend_validation"},
    )
    assert ready is not None
    assert ready["status"] == "ready"

    invalid_ready = _decision("ready")
    invalid_ready["coverage_items"][0]["state"] = "missing"
    assert validate_sufficiency_response(
        invalid_ready,
        allowed_capability_codes={"trend_validation"},
    ) is None


def test_sufficiency_rejects_unknown_capabilities() -> None:
    invalid = _decision("continue")
    invalid["next_capabilities"] = ["不存在的能力"]
    assert validate_sufficiency_response(
        invalid,
        allowed_capability_codes={"trend_validation"},
    ) is None


def test_report_rewrite_requires_every_coverage_item() -> None:
    valid = validate_report_rewrite_response({
        "coverage": [
            {"id": "coverage-1", "status": "covered", "reason": "正文已比较需求。"},
            {"id": "coverage-2", "status": "limitation", "reason": "证据未返回成本。"},
        ],
        "removed_unsupported_claims": ["采购成本"],
        "report": "# 报告\n\n正文。",
    }, required_coverage_ids={"coverage-1", "coverage-2"})
    assert valid is not None

    assert validate_report_rewrite_response({
        "coverage": [
            {"id": "coverage-1", "status": "covered", "reason": "已覆盖。"},
        ],
        "removed_unsupported_claims": [],
        "report": "# 报告",
    }, required_coverage_ids={"coverage-1", "coverage-2"}) is None


def test_internal_report_protocol_detection() -> None:
    assert report_contains_internal_protocol("使用 sellersprite__keyword_research 取得数据")
    assert report_contains_internal_protocol("依据 call:12 的结果")
    assert report_contains_internal_protocol("<|DSML|tool_calls>")
    assert not report_contains_internal_protocol("# 市场报告\n\n根据现有业务证据判断。")


def main() -> None:
    test_sufficiency_contract_controls_continue_and_ready()
    test_sufficiency_rejects_unknown_capabilities()
    test_report_rewrite_requires_every_coverage_item()
    test_internal_report_protocol_detection()
    print("commerce evidence sufficiency tests passed")


if __name__ == "__main__":
    main()
