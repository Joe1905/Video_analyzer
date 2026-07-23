#!/usr/bin/env python3
"""Regression for 98 provider-specific request/response Semantic contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastmoss_evidence_renderer import (
    FASTMOSS_CURRENT_TOOL_NAMES,
    render_fastmoss_evidence_document,
)
from sellersprite_evidence_renderer import (
    SELLERSPRITE_CURRENT_TOOL_NAMES,
    render_sellersprite_evidence_document,
)


ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = ROOT / "semantic_fixtures"
_TECHNICAL_PATTERNS = (
    re.compile(r"\b(?:fastmoss|sellersprite)__[a-z0-9_]+\b", re.IGNORECASE),
    re.compile(r"\bcall:\d+\b", re.IGNORECASE),
    re.compile(r"\$[.\[]"),
    re.compile(r"\b(?:null|true|false)\b", re.IGNORECASE),
    re.compile(r"\b\d{10}\b|\b\d{13}\b"),
    re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b", re.IGNORECASE),
)
_ALLOWED_ACRONYMS = {
    "ASIN", "BSR", "CPR", "GMV", "ID", "PPC", "ROAS", "SKU", "URL",
}


def _leaf_paths(value: Any, prefix: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        if not value and prefix:
            found.add(prefix)
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, (dict, list)):
                found.update(_leaf_paths(item, path))
            else:
                found.add(path)
    elif isinstance(value, list):
        if not value and prefix:
            found.add(prefix)
        for item in value:
            if isinstance(item, (dict, list)):
                found.update(_leaf_paths(item, f"{prefix}[]"))
            elif prefix:
                found.add(prefix)
    return found


def _schema_property_paths(schema: Any, prefix: str = "") -> set[str]:
    if not isinstance(schema, dict):
        return set()
    found: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, child in properties.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.add(path)
            found.update(_schema_property_paths(child, path))
    items = schema.get("items")
    if isinstance(items, dict):
        found.update(_schema_property_paths(items, f"{prefix}[]"))
    for keyword in ("oneOf", "anyOf", "allOf"):
        for branch in schema.get(keyword) or []:
            found.update(_schema_property_paths(branch, prefix))
    return found


def _remove_business_identity_values(markdown: str, fixture: dict[str, Any]) -> str:
    text = markdown
    identity_keys = {
        "asin", "product_id", "goods_id", "item_id", "category_id", "node_id",
        "shop_id", "seller_id", "creator_id", "creator_uid", "video_id", "live_id",
        "agency_id", "review_id", "registration_number", "title", "name", "keywords",
        "keyword", "category_name", "shop_name", "creator_name", "agency_name",
        "trademark_name", "review_content", "caption_text",
        "query", "search_term", "search_terms",
    }

    def visit(value: Any) -> None:
        nonlocal text
        def remove_identity(identity: Any) -> None:
            nonlocal text
            token = str(identity)
            if token:
                text = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
                    "",
                    text,
                )

        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in identity_keys and not isinstance(item, (dict, list)):
                    remove_identity(item)
                elif str(key).casefold() in identity_keys and isinstance(item, list):
                    for value in item:
                        if not isinstance(value, (dict, list)):
                            remove_identity(value)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(fixture.get("response_variants", {}).get("success"))
    for variant in fixture.get("request_variants") or []:
        visit(variant.get("arguments"))
    return text


def _technical_leaks(markdown: str, fixture: dict[str, Any]) -> list[str]:
    cleaned = _remove_business_identity_values(markdown, fixture)
    leaks: list[str] = []
    for pattern in _TECHNICAL_PATTERNS:
        leaks.extend(match.group(0) for match in pattern.finditer(cleaned))
    for word in re.findall(r"\b[A-Za-z][A-Za-z0-9.-]*\b", cleaned):
        if word.upper() in _ALLOWED_ACRONYMS:
            continue
        # Currency/region values must have been translated; Markdown punctuation
        # and the sample's business identities have already been removed.
        leaks.append(word)
    return sorted(set(leaks))


def _dossier(provider: str, fixture: dict[str, Any], variant: str) -> dict[str, Any]:
    response = fixture["response_variants"][variant]
    state = "error" if variant == "error" else "empty" if variant == "empty" else "data"
    entry = {
        "source_ref": "call:1",
        "tool_name": f"{provider}__{fixture['tool']}",
        "arguments": fixture["request_variants"][0]["arguments"],
        "evidence_fence": {"data_state": state},
        "business_data": response,
        **({"error": "样例调用失败"} if state == "error" else {}),
    }
    return {
        "type": f"{provider}_evidence_dossier",
        "provider": provider,
        "report_date": "2026-07-23",
        "tool_evidence": [entry],
        "hard_fact_boundaries": {"rules": ["只依据当前样例证据。"]},
    }


def run_contract_regression() -> dict[str, Any]:
    expected = {
        "fastmoss": set(FASTMOSS_CURRENT_TOOL_NAMES),
        "sellersprite": set(SELLERSPRITE_CURRENT_TOOL_NAMES),
    }
    report = {
        "tools": 0,
        "request_fields": 0,
        "request_fields_covered": 0,
        "response_fields": 0,
        "response_fields_covered": 0,
        "success_variants": 0,
        "empty_variants": 0,
        "error_variants": 0,
        "partial_variants": 0,
        "unmapped_fields": [],
        "audit_only_fields": [],
        "technical_leaks": [],
    }
    for provider, tool_names in expected.items():
        fixture_dir = FIXTURE_ROOT / provider
        files = sorted(fixture_dir.glob("*.json"))
        actual_names = {path.stem for path in files}
        assert actual_names == tool_names, (
            f"{provider} fixture mismatch missing={sorted(tool_names - actual_names)} "
            f"extra={sorted(actual_names - tool_names)}"
        )
        for path in files:
            fixture = json.loads(path.read_text(encoding="utf-8"))
            assert fixture["tool"] == path.stem
            assert fixture["provider"] == provider
            assert set(fixture["response_variants"]) == {
                "success", "empty", "error", "partial_identity",
            }
            schema_fields = _schema_property_paths(fixture["runtime_input_schema"])
            request_fields = set()
            for variant in fixture["request_variants"]:
                request_fields.update(_leaf_paths(variant["arguments"]))
            # Parent object fields count as covered when one of their children is
            # present in an explicit legal request example.
            covered_schema_fields = {
                field for field in schema_fields
                if field in request_fields
                or any(path.startswith(field + ".") or path.startswith(field + "[]") for path in request_fields)
            }
            assert covered_schema_fields == schema_fields, (
                f"{provider}/{path.stem} request fields not covered: "
                f"{sorted(schema_fields - covered_schema_fields)}"
            )
            report["request_fields"] += len(schema_fields)
            report["request_fields_covered"] += len(covered_schema_fields)

            renderer = (
                render_fastmoss_evidence_document
                if provider == "fastmoss"
                else render_sellersprite_evidence_document
            )
            success = renderer(_dossier(provider, fixture, "success"))
            assert fixture["semantic"]["title"] in success.markdown
            result = success.tool_results[0]
            assert not result.fallback, f"{provider}/{path.stem} used generic fallback"
            assert not result.unmapped_paths, f"{provider}/{path.stem} unmapped={result.unmapped_paths}"
            assert set(result.excluded_paths) == set(result.exclusion_reasons), (
                f"{provider}/{path.stem} audit-only fields must each have an explicit reason"
            )
            if fixture["semantic"].get("report_included", True):
                assert not result.excluded_paths, (
                    f"{provider}/{path.stem} business fields unexpectedly became audit-only: "
                    f"{sorted(result.excluded_paths)}"
                )
            leaks = _technical_leaks(success.markdown, fixture)
            assert not leaks, f"{provider}/{path.stem} technical leaks={leaks}"
            for forbidden in fixture["forbidden_semantic"]:
                assert forbidden.casefold() not in success.markdown.casefold()

            documented_fields = set(fixture["documented_response_fields"])
            consumed_names = {
                re.sub(r"\[\d+\]", "", leaf.rsplit(".", 1)[-1])
                for leaf in result.consumed_paths | result.excluded_paths
            }
            covered_response = {
                field for field in documented_fields
                if field in consumed_names
            }
            assert covered_response == documented_fields, (
                f"{provider}/{path.stem} response fields not covered: "
                f"{sorted(documented_fields - covered_response)}"
            )
            report["response_fields"] += len(documented_fields)
            report["response_fields_covered"] += len(covered_response)
            report["audit_only_fields"].extend(
                f"{provider}/{path.stem}:{field}" for field in result.excluded_paths
            )
            report["success_variants"] += 1

            empty = renderer(_dossier(provider, fixture, "empty"))
            assert empty.tool_results[0].empty
            error = renderer(_dossier(provider, fixture, "error"))
            if fixture["semantic"].get("report_included", True):
                assert any(phrase in error.markdown for phrase in ("调用失败", "查询失败", "样例调用失败")), (
                    f"{provider}/{path.stem} error variant was not rendered as failure"
                )
            partial = renderer(_dossier(provider, fixture, "partial_identity"))
            assert partial.tool_results and not partial.tool_results[0].fallback
            report["empty_variants"] += 1
            report["error_variants"] += 1
            report["partial_variants"] += 1
            report["tools"] += 1
    assert report["tools"] == 98
    return report


def main() -> None:
    report = run_contract_regression()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
