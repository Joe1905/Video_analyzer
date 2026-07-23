#!/usr/bin/env python3
"""Build per-tool Semantic contract fixtures from runtime MCP input schemas.

The generated files intentionally keep one file per tool. Runtime input schemas
are copied verbatim, while response examples are provider/tool-specific,
sanitized business samples used to exercise the natural-language renderer.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from fastmoss_evidence_renderer import FASTMOSS_RENDER_SPECS
from sellersprite_evidence_renderer import SELLERSPRITE_TOOL_SEMANTICS, SELLERSPRITE_TOOL_TITLES


ROOT = Path(__file__).resolve().parent


def _sample_scalar(name: str, schema: dict[str, Any]) -> Any:
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or isinstance(schema.get("properties"), dict):
        return _sample_object(schema)
    if kind == "array":
        return [_sample_scalar(name.rstrip("s") or "value", schema.get("items") or {"type": "string"})]
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    samples = {
        "asin": "B0TEST1234",
        "asins": "B0TEST1234",
        "marketplace": "US",
        "region": "US",
        "country": "US",
        "keyword": "portable desk fan",
        "keywords": "portable desk fan",
        "query": "portable desk fan",
        "month": "2026-06",
        "week": "2026-W29",
        "startdate": "2026-06-01",
        "enddate": "2026-06-30",
        "starttime": 1780272000,
        "endtime": 1782863999,
        "page": 1,
        "pagesize": 10,
        "pagesize": 10,
        "productid": "1734567890123456789",
        "categoryid": "123456",
        "shopid": "7234567890123456789",
        "creatorid": "7234567890123456790",
        "creatoruid": "7234567890123456790",
        "videoid": "7234567890123456791",
        "liveid": "7234567890123456792",
        "agencyid": "7234567890123456793",
        "returnfields": "asin",
    }
    if normalized in samples:
        sample = samples[normalized]
        if schema.get("type") == "array":
            return [sample]
        if schema.get("type") == "integer" and str(sample).isdigit():
            return int(sample)
        if schema.get("type") == "number" and str(sample).replace(".", "", 1).isdigit():
            return float(sample)
        return sample
    if kind == "integer":
        return max(int(schema.get("minimum") or 1), 1)
    if kind == "number":
        return float(schema.get("minimum") or 1)
    if kind == "boolean":
        return True
    return "样例值"


def _sample_object(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    selected: dict[str, Any] = {}
    for name, child in properties.items():
        selected[name] = _sample_scalar(name, child if isinstance(child, dict) else {})
    return selected


def _request_variants(schema: dict[str, Any]) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    branches = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(branches, list):
        for index, branch in enumerate(branches, 1):
            if isinstance(branch, dict):
                variants.append({"name": f"合法格式{index}", "arguments": _sample_object(branch)})
    if not variants:
        variants.append({"name": "运行时结构", "arguments": _sample_object(schema)})
    return variants


def _identity(entity: str, provider: str) -> dict[str, Any]:
    if entity in {"asin", "product"}:
        return (
            {"asin": "B0TEST1234", "title": "Portable Desk Fan"}
            if provider == "sellersprite"
            else {"product_id": "1734567890123456789", "title": "Portable Desk Fan"}
        )
    if entity == "keyword":
        return {"keywords": "portable desk fan"}
    if entity == "category":
        return {"category_id": "123456", "category_name": "Home and Kitchen"}
    if entity == "shop":
        return {"shop_id": "7234567890123456789", "shop_name": "Sample Store"}
    if entity == "creator":
        return {"creator_id": "7234567890123456790", "creator_name": "Sample Creator"}
    if entity == "video":
        return {"video_id": "7234567890123456791", "title": "Portable fan demonstration"}
    if entity == "live":
        return {"live_id": "7234567890123456792", "title": "Portable fan live show"}
    if entity == "agency":
        return {"agency_id": "7234567890123456793", "agency_name": "Sample Agency"}
    if entity == "review":
        return {"review_id": "R-1001", "review_content": "Quiet and convenient", "review_rating": 4}
    if entity == "trademark":
        return {"trademark_name": "Sample Mark", "registration_number": "TM-1001"}
    return {"message": "接口说明样本"}


def _success_response(tool: str, profile: str, entity: str, provider: str) -> dict[str, Any]:
    identity = _identity(entity, provider)
    base_scope = {"region": "US", "currency": "USD"}
    if profile == "trend" or "trend" in tool or "prediction" in tool:
        metric = (
            {"search_volume": 12500}
            if "keyword" in tool or "google" in tool or "aba_" in tool
            else {"units_sold": 320, "gmv": 7990}
        )
        return {
            "data": {
                **identity,
                **base_scope,
                "trend_series": [
                    {"date": "2026-06-01", **metric},
                    {"date": "2026-07-01", **{key: value + 100 for key, value in metric.items()}},
                ],
            }
        }
    if profile == "distribution":
        dimension = "price_range" if "price" in tool else "category_name"
        return {
            "data": {
                **identity,
                **base_scope,
                "breakdown": [
                    {dimension: "样本区间一", "product_count": 24, "share_percent": 40},
                    {dimension: "样本区间二", "product_count": 36, "share_percent": 60},
                ],
            }
        }
    if profile in {"records", "relationship"}:
        record = {
            **identity,
            "rank": 1,
            "price": 24.99,
            "currency": "USD",
            "period": "2026-06",
        }
        if "keyword" in tool or "aba_" in tool:
            record.update({"search_volume": 12500, "purchase_volume": 1800, "purchase_rate": 0.144})
        if "review" in tool:
            record.update({"review_rating": 4, "review_content": "Quiet and convenient"})
        if "creator" in tool:
            record.update({"linked_creator_count": 18})
        if "video" in tool:
            record.update({"play_count": 125000, "like_count": 7600})
        if "live" in tool:
            record.update({"live_gmv": 4800, "live_units_sold": 160})
        return {"data": {"total": 1, "list": [record]}}
    if profile == "narrative":
        return {"data": {**identity, "caption_text": "Quiet cooling for a small workspace."}}
    if profile == "reference":
        return {"data": {**identity, "region": "US", "period": "2026-06"}}
    return {
        "data": {
            **identity,
            **base_scope,
            "price": 24.99,
            "units_sold": 320,
            "gmv": 7990,
            "period": "2026-06",
        }
    }


def _leaf_keys(value: Any) -> list[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                keys.update(_leaf_keys(item))
            else:
                keys.add(str(key))
    elif isinstance(value, list):
        for item in value:
            keys.update(_leaf_keys(item))
    return sorted(keys)


def build(provider: str, schema_path: Path, output_dir: Path) -> int:
    payload = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    runtime_tools = {
        str(tool.get("name")): tool
        for tool in payload.get("result", {}).get("tools", [])
        if isinstance(tool, dict) and tool.get("name")
    }
    registry = FASTMOSS_RENDER_SPECS if provider == "fastmoss" else SELLERSPRITE_TOOL_SEMANTICS
    missing = set(registry) - set(runtime_tools)
    extra = set(runtime_tools) - set(registry)
    if missing or extra:
        raise RuntimeError(f"{provider} runtime/registry mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for tool_name, contract in sorted(registry.items()):
        runtime = runtime_tools[tool_name]
        profile = contract.profile
        entity = contract.entity_type
        title = (
            contract.evidence_title
            if provider == "fastmoss"
            else SELLERSPRITE_TOOL_TITLES[tool_name]
        )
        success = _success_response(tool_name, profile, entity, provider)
        partial = json.loads(json.dumps(success, ensure_ascii=False))
        collection = partial.get("data", {}).get("list")
        if isinstance(collection, list):
            collection.append({"price": 19.99, "currency": "USD"})
        else:
            partial = {"data": {"list": [{**_identity(entity, provider), "period": "2026-06"}, {"price": 19.99}]}}
        fixture = {
            "provider": provider,
            "tool": tool_name,
            "contract_source": {
                "request": "2026-07-23运行时MCP Schema",
                "response": "工具专属Semantic契约与脱敏业务样本",
            },
            "semantic": {
                "title": title,
                "profile": profile,
                "entity_type": entity,
                "report_included": bool(getattr(contract, "report_included", True)),
            },
            "runtime_input_schema": runtime.get("inputSchema") or {"type": "object", "properties": {}},
            "request_variants": _request_variants(runtime.get("inputSchema") or {}),
            "response_variants": {
                "success": success,
                "empty": {"data": {"total": 0, "list": []}},
                "error": {"success": False, "message": "样例调用失败"},
                "partial_identity": partial,
            },
            "documented_response_fields": _leaf_keys(success),
            "expected_semantic": [title],
            "forbidden_semantic": [
                f"{provider}__{tool_name}",
                "call:",
                "null",
                "true",
                "false",
            ],
        }
        target = output_dir / f"{tool_name}.json"
        target.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(registry)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fastmoss-schema", type=Path, required=True)
    parser.add_argument("--sellersprite-schema", type=Path, required=True)
    args = parser.parse_args()
    fast_count = build(
        "fastmoss", args.fastmoss_schema,
        ROOT / "semantic_fixtures" / "fastmoss",
    )
    seller_count = build(
        "sellersprite", args.sellersprite_schema,
        ROOT / "semantic_fixtures" / "sellersprite",
    )
    print(json.dumps({"fastmoss": fast_count, "sellersprite": seller_count, "total": fast_count + seller_count}))


if __name__ == "__main__":
    main()
