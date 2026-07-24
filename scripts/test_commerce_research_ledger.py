#!/usr/bin/env python3
"""Focused regression checks for the SellerSprite research ledger."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from chat_session import ChatStore, Message, load_sessions_from_disk, save_sessions_to_disk
from commerce_research_ledger import (
    active_slot,
    apply_progress_delta,
    core_slots_terminal,
    create_research_ledger,
    fallback_slots,
    finish_no_progress_batch,
    validate_generated_slots,
)
from sellersprite_evidence_renderer import (
    SELLERSPRITE_EVIDENCE_CONTRACTS,
    SELLERSPRITE_TOOL_SEMANTICS,
    apply_sellersprite_return_field_plan,
    project_sellersprite_business_data,
    sellersprite_contract_diagnostics,
)
import web_app


def test_contract_registry() -> None:
    diagnostics = sellersprite_contract_diagnostics()
    assert len(SELLERSPRITE_TOOL_SEMANTICS) == 43
    assert len(SELLERSPRITE_EVIDENCE_CONTRACTS) == 43
    assert diagnostics["missing_contracts"] == []
    assert diagnostics["unexpected_contracts"] == []
    assert diagnostics["return_fields_supported"] == 42


def test_all_43_tool_specific_fixtures_project_independently() -> None:
    fixture_dir = Path(__file__).with_name("semantic_fixtures") / "sellersprite"
    covered: set[str] = set()
    for tool_name, contract in SELLERSPRITE_EVIDENCE_CONTRACTS.items():
        fixture = json.loads(
            (fixture_dir / f"{tool_name}.json").read_text(encoding="utf-8")
        )
        variants = fixture.get("response_variants") or {}
        assert set(variants) >= {"success", "empty", "error", "partial_identity"}
        success = variants["success"]
        business_data = success.get("data") if isinstance(success, dict) else success
        projection = project_sellersprite_business_data(
            tool_name, business_data, contract.facts
        )
        assert projection["projected_data"] is not None
        assert projection["raw_chars"] >= projection["projected_chars"]
        assert contract.field_source
        assert contract.return_fields_location in {
            "request", "top_level", "unsupported",
        }
        covered.add(tool_name)
    assert covered == set(SELLERSPRITE_TOOL_SEMANTICS)


def test_slot_validation_rejects_invented_fact() -> None:
    valid = validate_generated_slots({
        "slots": [{
            "id": "slot-1",
            "topic": "关键词需求",
            "priority": "core",
            "entity_scope": {"entity": "tent fan", "region": "US", "period": "最近两个月"},
            "required_facts": ["关键词身份", "搜索量"],
            "acceptable_capabilities": ["keyword_discovery"],
            "boundaries": [],
        }],
    }, allowed_capabilities={"keyword_discovery"})
    assert valid and valid[0]["state"] == "pending"
    invalid = validate_generated_slots({
        "slots": [{
            "id": "slot-1",
            "topic": "臆造利润",
            "priority": "core",
            "entity_scope": {},
            "required_facts": ["供应商利润"],
            "acceptable_capabilities": ["keyword_discovery"],
            "boundaries": [],
        }],
    }, allowed_capabilities={"keyword_discovery"})
    assert invalid is None


def test_progress_and_no_progress_are_batch_scoped() -> None:
    slots = fallback_slots(
        {"objective": "product_research", "entity_type": "keyword", "entity": "tent fan"},
        allowed_capabilities={"keyword_discovery"},
    )
    ledger = create_research_ledger({"entity": "tent fan"}, slots)
    slot = active_slot(ledger)
    assert slot and slot["id"] == "slot-1"
    before = ledger["progress_hash"]
    apply_progress_delta(
        ledger,
        source_ref="message-1:1",
        tool_name="keyword_research",
        capability="keyword_discovery",
        arguments={"request": {"keywords": "tent fan", "marketplace": "US"}},
        quality_status="accepted",
        observed_facts=["关键词身份", "统计周期", "搜索量"],
        projected_data={"items": [{"keywords": "tent fan", "month": "2026-06", "searches": 1000}]},
        entity_scope={"entity": "tent fan", "region": "US", "period": "2026-06"},
        slot_id=slot["id"],
    )
    finish_no_progress_batch(
        ledger,
        attempted_capabilities={"keyword_discovery"},
        before_hash=before,
    )
    assert ledger["no_progress_rounds"] == 0
    assert active_slot(ledger)["state"] == "partial"

    for expected in (1, 0):
        before = ledger["progress_hash"]
        finish_no_progress_batch(
            ledger,
            attempted_capabilities={"keyword_discovery"},
            before_hash=before,
        )
        assert ledger["no_progress_rounds"] == expected
    assert core_slots_terminal(ledger)
    assert ledger["slots"][0]["state"] == "unavailable"


def test_projection_keeps_fact_dependencies_and_removes_noise() -> None:
    raw = {
        "items": [{
            "keywords": "tent fan",
            "month": "2026-06",
            "searches": 61950,
            "purchases": 9385,
            "purchaseRate": 15.15,
            "currency": "USD",
            "imageUrl": "https://example.invalid/noise.jpg",
            "internalTrace": "do-not-send",
        }],
        "requestId": "audit-only",
    }
    projected = project_sellersprite_business_data(
        "keyword_research",
        raw,
        ["关键词身份", "统计月份", "搜索量", "购买量", "购买率"],
    )
    text = json.dumps(projected["projected_data"], ensure_ascii=False)
    assert "tent fan" in text
    assert "61950" in text and "9385" in text and "15.15" in text
    assert "imageUrl" not in text
    assert "internalTrace" not in text
    assert set(projected["observed_facts"]) >= {
        "关键词身份", "统计月份", "搜索量", "购买量", "购买率",
    }
    assert projected["projected_chars"] < projected["raw_chars"]


def test_return_fields_are_program_owned_and_conservative() -> None:
    filtered, plan = apply_sellersprite_return_field_plan(
        "keyword_research",
        {"request": {
            "keywords": "tent fan",
            "marketplace": "US",
            "returnFields": "wrongField",
        }},
        ["关键词身份", "统计月份", "搜索量"],
    )
    assert plan["mode"] == "server_filter"
    assert filtered["request"]["returnFields"] == "keywords,month,searches"

    full_fetch, fallback_plan = apply_sellersprite_return_field_plan(
        "keyword_research",
        {"request": {
            "keywords": "tent fan",
            "marketplace": "US",
            "returnFields": "wrongField",
        }},
        ["关键词身份", "统计月份", "搜索量", "购买量"],
    )
    assert fallback_plan["mode"] == "local_projection"
    assert "returnFields" not in full_fetch["request"]


def test_research_state_persists_without_message_payload_changes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sessions.json"
        store = ChatStore(path)
        session = store.create_session("session-1")
        message = Message(id="message-1", role="assistant", content="")
        store.add_message(session, message)
        ledger = create_research_ledger(
            {"entity": "tent fan"},
            fallback_slots(
                {"entity_type": "keyword", "entity": "tent fan"},
                allowed_capabilities={"keyword_discovery"},
            ),
        )
        store.commit_research_batch(
            session,
            message,
            [{"tool_name": "sellersprite__keyword_research", "result": {"ok": True}}],
            ledger,
        )
        save_sessions_to_disk(store)

        restored = ChatStore(path)
        load_sessions_from_disk(restored)
        loaded = restored.get_session("session-1")
        assert loaded is not None
        assert loaded.research_state["task_id"] == ledger["task_id"]
        assert len(loaded.messages[0].tool_results or []) == 1
        serialized = json.loads(path.read_text(encoding="utf-8"))
        assert "research_state" in serialized[0]
        assert "research_state" not in serialized[0]["messages"][0]


def test_report_dossier_reads_only_terminal_ledger_projection() -> None:
    slots = [{
        "id": "slot-1",
        "topic": "关键词需求",
        "priority": "core",
        "state": "pending",
        "entity_scope": {"entity": "tent fan", "region": "US", "period": "2026-06"},
        "required_facts": ["关键词身份", "统计周期", "搜索量"],
        "acceptable_capabilities": ["keyword_discovery"],
        "observed_facts": [],
        "missing_facts": ["关键词身份", "统计周期", "搜索量"],
        "evidence_refs": [],
        "exhausted_capabilities": [],
        "boundaries": [],
        "created_order": 1,
    }]
    ledger = create_research_ledger({"entity": "tent fan"}, slots)
    apply_progress_delta(
        ledger,
        source_ref="message-1:1",
        tool_name="keyword_research",
        capability="keyword_discovery",
        arguments={"request": {"keywords": "tent fan", "marketplace": "US"}},
        quality_status="accepted",
        observed_facts=["关键词身份", "统计周期", "搜索量"],
        projected_data={"items": [{"keywords": "tent fan", "month": "2026-06", "searches": 61950}]},
        entity_scope={"entity": "tent fan", "region": "US", "period": "2026-06"},
        slot_id="slot-1",
    )
    finish_no_progress_batch(
        ledger,
        attempted_capabilities={"keyword_discovery"},
        before_hash="not-the-current-hash",
    )
    ledger["status"] = "ready"
    assistant = Message(id="message-1", role="assistant", content="")
    dossier = web_app.sellersprite_report_evidence_dossier(
        assistant, {"_research_ledger": ledger}
    )
    assert len(dossier["tool_evidence"]) == 1
    markdown, stats = web_app.sellersprite_render_report_evidence(dossier)
    assert "tent fan" in markdown and "61950" in markdown
    assert "sellersprite__" not in markdown
    assert stats["tool_count"] == 1


def main() -> None:
    tests = [
        test_contract_registry,
        test_all_43_tool_specific_fixtures_project_independently,
        test_slot_validation_rejects_invented_fact,
        test_progress_and_no_progress_are_batch_scoped,
        test_projection_keeps_fact_dependencies_and_removes_noise,
        test_return_fields_are_program_owned_and_conservative,
        test_research_state_persists_without_message_payload_changes,
        test_report_dossier_reads_only_terminal_ledger_projection,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS {len(tests)} SellerSprite ledger tests")


if __name__ == "__main__":
    main()
