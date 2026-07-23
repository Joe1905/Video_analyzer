#!/usr/bin/env python3
"""Focused evidence-admission regression without external API calls."""

from __future__ import annotations

from types import SimpleNamespace

from commerce_evidence_gate import (
    admitted_business_payload,
    chunk_flash_judge_pending,
    deterministic_evidence_quality,
    evidence_quality_allows_entities,
    merge_flash_chunk_qualities,
    validate_flash_verdict,
)
import web_app


def _result(data):
    return {
        "ok": True,
        "data_state": "data",
        "enough_data": True,
        "mcp_data": data,
    }


def test_exact_identity_is_accepted() -> None:
    result = _result({"data": {"asin": "B0TEST1234", "title": "Sample"}})
    quality, pending = deterministic_evidence_quality(
        "sellersprite__asin_detail", {"asin": "B0TEST1234"}, result
    )
    assert quality["status"] == "accepted"
    assert pending is None


def test_mismatched_identity_is_off_topic_and_cannot_seed_entities() -> None:
    result = _result({"data": {"list": [{"asin": "B0WRONG999", "title": "Wrong"}]}})
    quality, pending = deterministic_evidence_quality(
        "sellersprite__product_research", {"asin": "B0TEST1234"}, result
    )
    result["evidence_quality"] = quality
    assert quality["status"] == "off_topic"
    assert quality["rejected_rows"] == [1]
    assert pending is None
    assert not evidence_quality_allows_entities(result)
    assert admitted_business_payload(result) == {}


def test_missing_identity_rows_are_partially_admitted() -> None:
    result = _result({
        "data": {
            "list": [
                {"keywords": "portable fan", "search_volume": 1000},
                {"search_volume": 900},
            ]
        }
    })
    quality, pending = deterministic_evidence_quality(
        "sellersprite__keyword_research", {"marketplace": "US"}, result
    )
    result["evidence_quality"] = quality
    assert quality["status"] == "partial"
    assert quality["accepted_rows"] == [1]
    assert quality["rejected_rows"] == [2]
    assert pending is None
    projected = admitted_business_payload(result)
    assert projected["data"]["list"] == [{"keywords": "portable fan", "search_volume": 1000}]


def test_explicit_scope_conflict_is_rejected() -> None:
    result = _result({
        "data": {
            "list": [
                {"asin": "B0TEST1234", "title": "Portable fan", "marketplace": "UK"}
            ]
        }
    })
    quality, pending = deterministic_evidence_quality(
        "sellersprite__product_research", {"marketplace": "US"}, result
    )
    assert quality["status"] == "off_topic"
    assert "地区" in quality["reason"]
    assert pending is None


def test_equivalent_month_formats_do_not_conflict() -> None:
    for returned_month in ("2026.06", "2026-06", "202606"):
        result = _result({
            "data": {
                "list": [{
                    "keywords": "portable fan",
                    "month": returned_month,
                    "searchVolume": 1000,
                }]
            }
        })
        quality, pending = deterministic_evidence_quality(
            "sellersprite__keyword_research",
            {"marketplace": "US", "month": "202606", "keyword": "portable fan"},
            result,
        )
        assert quality["status"] != "off_topic", (returned_month, quality)


def test_discovery_query_uses_validated_flash_rows() -> None:
    result = _result({
        "data": {
            "list": [
                {"asin": "B0TEST1234", "title": "Portable fan"},
                {"asin": "B0WRONG999", "title": "Dog supplement"},
            ]
        }
    })
    initial, pending = deterministic_evidence_quality(
        "sellersprite__product_research",
        {"keywords": ["portable fan"], "marketplace": "US"},
        result,
    )
    assert initial["status"] == "uncertain"
    assert pending is not None
    quality = validate_flash_verdict(pending, {
        "status": "partial",
        "accepted_rows": [1],
        "rejected_rows": [2],
        "reason": "第一行与查询对象匹配，第二行属于其他商品。",
    })
    assert quality is not None
    result["evidence_quality"] = quality
    assert admitted_business_payload(result)["data"]["list"][0]["asin"] == "B0TEST1234"


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


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


def _ambiguous_entry():
    return {
        "tool_name": "sellersprite__product_research",
        "arguments": {"keywords": ["portable fan"], "marketplace": "US"},
        "normalized_result": _result({
            "data": {
                "list": [
                    {"asin": "B0TEST1234", "title": "Portable fan"},
                    {"asin": "B0WRONG999", "title": "Dog supplement"},
                ]
            }
        }),
    }


def _apply_with_fake(fake: _FakeRequests):
    entry = _ambiguous_entry()
    original_record = web_app.record_api_call
    web_app.record_api_call = lambda *args, **kwargs: None
    try:
        web_app.apply_chat_evidence_quality_gate(
            [entry],
            "找便携风扇",
            {"research_task": {"objective": "product_selection", "entity": "portable fan"}},
            fake,
            "test-key",
            "https://example.invalid",
            "deepseek-v4-flash",
        )
    finally:
        web_app.record_api_call = original_record
    return entry["normalized_result"]["evidence_quality"]


def test_batched_flash_success_and_failure_fallbacks() -> None:
    success = _apply_with_fake(_FakeRequests(
        '{"calls":[{"call_key":"result-1-part-1","status":"partial",'
        '"accepted_rows":[1],"rejected_rows":[2],'
        '"reason":"第一行匹配，第二行偏题。"}]}'
    ))
    assert success["status"] == "partial"
    assert success["source"] == "v4_flash"

    illegal = _apply_with_fake(_FakeRequests("not-json"))
    assert illegal["status"] == "uncertain"
    assert illegal["source"] == "fallback"

    timeout = _apply_with_fake(_FakeRequests(error=TimeoutError("simulated timeout")))
    assert timeout["status"] == "uncertain"
    assert timeout["source"] == "fallback"


def test_off_topic_result_cannot_expand_planner_entities() -> None:
    result = _result({"data": {"list": [{"asin": "B0WRONG999", "title": "Wrong"}]}})
    quality, _ = deterministic_evidence_quality(
        "sellersprite__product_research", {"asin": "B0TEST1234"}, result
    )
    result["evidence_quality"] = quality
    message = SimpleNamespace(tool_results=[{
        "tool_name": "sellersprite__product_research",
        "result": result,
    }])
    assert web_app._planner_result_payloads(message) == []


def test_sellersprite_keyword_alias_and_time_series_identity_inheritance() -> None:
    keyword_result = _result({
        "data": {"list": [{"keywrod": "portable fan", "searchVolume": 1000}]}
    })
    quality, pending = deterministic_evidence_quality(
        "sellersprite__keyword_research",
        {"keyword": "portable fan", "marketplace": "US"},
        keyword_result,
    )
    assert quality["status"] == "uncertain"
    assert pending is not None
    assert pending["records"][0]["identity"]["keywrod"] == "portable fan"

    for tool_name in (
        "sellersprite__keyword_research_trends",
        "sellersprite__google_trend",
    ):
        trend_result = _result({
            "data": {
                "keywords": "portable fan",
                "trend_series": [
                    {"date": "2026-06-01", "searchVolume": 100},
                    {"date": "2026-07-01", "searchVolume": 120},
                ],
            }
        })
        quality, pending = deterministic_evidence_quality(
            tool_name,
            {"keyword": "portable fan", "marketplace": "US"},
            trend_result,
        )
        assert quality["status"] == "accepted"
        assert quality["accepted_rows"] == [1, 2]
        assert pending is None


def test_status_wrappers_and_empty_collections_are_empty() -> None:
    for payload in (
        {"code": 0, "message": "ok", "link": "trace"},
        {"code": 0, "data": []},
        {"success": True, "items": []},
        {"data": {"total": 0, "list": []}},
    ):
        quality, pending = deterministic_evidence_quality(
            "sellersprite__market_research",
            {"marketplace": "US"},
            _result(payload),
        )
        assert quality["status"] == "empty"
        assert pending is None


def test_large_pending_is_chunked_and_requires_full_merge() -> None:
    pending = {
        "call_key": "result-1",
        "tool_name": "sellersprite__product_node",
        "query_scope": {"terms": ["balsamic glaze"]},
        "record_path": ["data", "list"],
        "records": [
            {"row": index, "identity": {"title": "Balsamic vinegar " + ("x" * 500)}}
            for index in range(1, 31)
        ],
        "supported_dimensions": ["类目信息"],
    }
    batches = chunk_flash_judge_pending([pending], max_estimated_tokens=600)
    chunks = [item for batch in batches for item in batch]
    assert len(chunks) > 1
    qualities = []
    for chunk in chunks:
        quality = validate_flash_verdict(chunk, {
            "status": "accepted",
            "accepted_rows": [],
            "rejected_rows": [],
            "reason": "相关。",
        })
        assert quality is not None
        qualities.append(quality)
    merged = merge_flash_chunk_qualities(pending, qualities)
    assert merged is not None
    assert merged["status"] == "accepted"
    assert merged["accepted_rows"] == list(range(1, 31))
    assert merge_flash_chunk_qualities(pending, qualities[:-1]) is None


def main() -> None:
    test_exact_identity_is_accepted()
    test_mismatched_identity_is_off_topic_and_cannot_seed_entities()
    test_missing_identity_rows_are_partially_admitted()
    test_explicit_scope_conflict_is_rejected()
    test_equivalent_month_formats_do_not_conflict()
    test_discovery_query_uses_validated_flash_rows()
    test_batched_flash_success_and_failure_fallbacks()
    test_off_topic_result_cannot_expand_planner_entities()
    test_sellersprite_keyword_alias_and_time_series_identity_inheritance()
    test_status_wrappers_and_empty_collections_are_empty()
    test_large_pending_is_chunked_and_requires_full_merge()
    print("commerce evidence gate tests passed")


if __name__ == "__main__":
    main()
