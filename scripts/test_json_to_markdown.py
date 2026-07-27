#!/usr/bin/env python3
"""Ad-hoc tests for the lossless JSON-to-Markdown renderer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from json_to_markdown import json_text_to_markdown, json_to_markdown  # noqa: E402


def fastmoss_fixture() -> dict:
    return {
        "code": 0,
        "message": "",
        "timestamp": 1775714099,
        "request_id": "req-demo",
        "data": {
            "total": 2,
            "date_type": "day",
            "date_value": "2026-04-06",
            "list": [
                {
                    "product_id": "1729739021314527787",
                    "title": "Food Processor | 400W",
                    "sales": 0,
                    "is_ad": False,
                    "growth_rate": None,
                },
                {
                    "product_id": "1729844364239993614",
                    "title": "Mini Meat\nGrinder",
                    "sales": 2493,
                    "is_ad": True,
                    "growth_rate": "-15.57%",
                },
            ],
        },
    }


def test_fastmoss_envelope_and_flat_list_table() -> None:
    markdown = json_to_markdown(fastmoss_fixture(), title="FastMoss 响应")
    assert markdown.startswith("# FastMoss 响应\n")
    assert "## 响应元数据" in markdown
    assert "| code | 0 | $.code |" in markdown
    assert "| message | \"\" | $.message |" in markdown
    assert "## data (`$.data`)" in markdown
    assert "数组，共 2 项；以下完整展示全部 2 项。" in markdown
    assert "1729739021314527787" in markdown
    assert "Food Processor \\| 400W" in markdown
    assert "Mini Meat<br>Grinder" in markdown
    assert "| 0 | 1729739021314527787" in markdown
    assert "| false | null | $.data.list[0] |" in markdown


def test_nested_values_and_empty_states_are_explicit() -> None:
    source = {
        "data": {
            "shop": {
                "shop_id": "7494049503488000000",
                "metrics": {"gmv": 0, "enabled": False, "note": None},
            },
            "videos": [],
            "facets": {},
            "groups": [[1, 2], [3]],
        },
        "code": 0,
        "msg": "success",
    }
    markdown = json_to_markdown(source)
    assert "7494049503488000000" in markdown
    assert "本数组没有返回任何记录（JSON 路径：`$.data.videos`）。" in markdown
    assert "本对象没有返回任何字段（JSON 路径：`$.data.facets`）。" in markdown
    assert "### 项目 1 (`$.data.groups[0]`)" in markdown
    assert "| enabled | false | $.data.shop.metrics.enabled |" in markdown
    assert "| note | null | $.data.shop.metrics.note |" in markdown


def test_missing_field_is_distinct_from_null() -> None:
    markdown = json_to_markdown([{"id": "1", "score": None}, {"id": "2"}])
    assert "| 0 | 1 | null | $[0] |" in markdown
    assert "| 1 | 2 | （字段缺失） | $[1] |" in markdown


def test_optional_row_limit_is_never_silent() -> None:
    markdown = json_to_markdown(
        {"items": [{"id": str(index)} for index in range(5)]},
        max_table_rows=2,
    )
    assert "共 5 项，本次展示 2 项，省略 3 项" in markdown
    assert "数组，共 5 项；本次展示前 2 项。" in markdown
    assert "\n| 2 | 2 |" not in markdown


def test_path_narrative_override_replaces_empty_transport_shape() -> None:
    source = {
        "tool_evidence": [
            {
                "tool_name": "fastmoss__product_review_list",
                "arguments": {"filter": {"product_id": "1732249989673554739"}},
                "business_data": {"list": [], "total": 0},
            }
        ]
    }
    sentence = (
        "call:1（fastmoss__product_review_list）调用成功，但针对商品 "
        "1732249989673554739 没有返回评论记录。"
    )
    markdown = json_to_markdown(
        source,
        narrative_overrides={"$.tool_evidence[0]": sentence},
    )
    assert sentence in markdown
    assert "business_data" not in markdown
    assert "| total | 0 |" not in markdown


def test_default_rendering_is_complete_and_deterministic() -> None:
    source = {"items": [{"id": str(index)} for index in range(20)]}
    first = json_to_markdown(source)
    second = json_to_markdown(source)
    assert first == second
    assert "显式裁剪" not in first
    assert "$​.items" not in first  # Guard against invisible path characters.
    for index in range(20):
        assert f"| {index} | {index} | $.items[{index}] |" in first


def test_json_text_and_invalid_input() -> None:
    text = json.dumps(fastmoss_fixture(), ensure_ascii=False)
    assert json_text_to_markdown(text) == json_to_markdown(fastmoss_fixture())
    try:
        json_text_to_markdown('{"data":')
    except ValueError as exc:
        assert "line 1" in str(exc) and "column 9" in str(exc)
    else:
        raise AssertionError("invalid JSON should fail")


def test_rejects_non_json_python_values() -> None:
    try:
        json_to_markdown({"ids": {"1", "2"}})  # type: ignore[dict-item]
    except ValueError as exc:
        assert "non-JSON value" in str(exc)
    else:
        raise AssertionError("sets are not JSON-compatible")


def test_cli_reads_file_and_writes_stdout() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "response.json"
        source.write_text(json.dumps(fastmoss_fixture()), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "json_to_markdown.py"), str(source)],
            check=False,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stderr
    assert "## 响应元数据" in result.stdout
    assert "1729739021314527787" in result.stdout


if __name__ == "__main__":
    test_fastmoss_envelope_and_flat_list_table()
    test_nested_values_and_empty_states_are_explicit()
    test_missing_field_is_distinct_from_null()
    test_optional_row_limit_is_never_silent()
    test_path_narrative_override_replaces_empty_transport_shape()
    test_default_rendering_is_complete_and_deterministic()
    test_json_text_and_invalid_input()
    test_rejects_non_json_python_values()
    test_cli_reads_file_and_writes_stdout()
    print("json-to-markdown tests passed")
