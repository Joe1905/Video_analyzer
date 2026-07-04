#!/usr/bin/env python3
"""Smoke tests for chat tool result normalization."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from web_app import normalize_tool_result, route_chat_intent  # noqa: E402
from tools import execute_tool, get_tools_for_model, list_tools, parse_duckduckgo_html  # noqa: E402


def test_tiktok_search_keeps_analysis_fields() -> None:
    raw = {
        "ok": True,
        "elapsed": 1.2,
        "data": {
            "raw_ref": "output/chat_tools/tiktok_search-keyword_test.json",
            "raw_bytes": 2_300_000,
            "data": {
                "success": True,
                "data": {
                    "search_item_list": {
                        "0": {
                            "aweme_info": {
                                "aweme_id": "video1",
                                "desc": "AI companion plush toy demo",
                                "author": {"nickname": "Toy Lab", "unique_id": "toylab"},
                                "statistics": {
                                    "play_count": 1200000,
                                    "digg_count": 54000,
                                    "comment_count": 900,
                                    "share_count": 2100,
                                },
                                "music": {"title": "demo sound"},
                                "text_extra": [{"hashtag_name": "aitoys"}],
                                "share_url": "https://www.tiktok.com/@toylab/video/video1",
                                "avatar_thumb": {
                                    "url_list": [
                                        "https://p16-common-sign.tiktokcdn-us.com/huge?x-signature=abc"
                                    ]
                                },
                            }
                        }
                    }
                },
            },
        },
    }

    result = normalize_tool_result("tiktok_search_keyword", raw)
    encoded = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert result["kind"] == "tiktok_items"
    assert result["enough_data"] is True
    assert result["raw_ref"] == "output/chat_tools/tiktok_search-keyword_test.json"
    assert result["items"][0]["description"] == "AI companion plush toy demo"
    assert result["items"][0]["author"] == "Toy Lab"
    assert result["items"][0]["play_count"] == 1200000
    assert "tiktokcdn" not in encoded
    assert len(encoded) < 3000


def test_amazon_keeps_product_fields() -> None:
    raw = {
        "ok": True,
        "elapsed": 0.8,
        "data": {
            "raw_ref": "output/chat_tools/amazon_keyword_test.json",
            "raw_bytes": 120000,
            "data": {
                "status": "SUCCESS",
                "type": "search",
                "category": "Toys",
                "products": [
                    {
                        "asin": "B000000001",
                        "title": "AI Plush Companion",
                        "priceStr": "$39.99",
                        "rating": 4.6,
                        "reviews": 321,
                        "boughtPastMonth": "1K+",
                        "bullets": ["Talks with kids", "Soft plush body"],
                        "url": "https://www.amazon.com/dp/B000000001",
                    }
                ],
            },
        },
    }

    result = normalize_tool_result("amazon_search_keyword", raw)
    assert result["ok"] is True
    assert result["kind"] == "amazon_products"
    assert result["enough_data"] is True
    assert result["products"][0]["asin"] == "B000000001"
    assert result["products"][0]["title"] == "AI Plush Companion"
    assert result["products"][0]["price"] == "$39.99"
    assert result["products"][0]["reviews"] == 321


def test_current_time_tool_is_available() -> None:
    result = execute_tool("current_time", {})
    assert result["ok"] is True
    assert result["data"]["date"]
    assert result["data"]["time"]
    assert result["data"]["utc_iso"]

    model_tool_names = {item["function"]["name"] for item in get_tools_for_model()}
    assert "current_time" in model_tool_names

    categories = {item["category"]: item["tools"] for item in list_tools()}
    assert any(tool["name"] == "current_time" for tool in categories["系统"])



def test_web_search_route_exposes_web_search_tool() -> None:
    route = route_chat_intent("\u98de\u98de\u5154\u4f60\u77e5\u9053\u5417")
    assert route["intent"] == "web_search"
    assert route["tools"] == {"web_search"}



def test_web_search_tool_is_registered_and_normalized() -> None:
    html = """
    <div class="result">
      <div>
        <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fnews">Example &amp; News</a>
        <a class="result__snippet">A concise &lt;b&gt;snippet&lt;/b&gt;.</a>
      </div>
    </div>
    """
    parsed = parse_duckduckgo_html(html, 3)
    assert parsed == [{"title": "Example & News", "snippet": "A concise snippet .", "url": "https://example.com/news"}]

    raw = {
        "ok": True,
        "elapsed": 0.4,
        "data": {
            "ok": True,
            "query": "latest AI news",
            "effective_query": "latest AI news",
            "retrieved_at": "2026-07-04T00:00:00+00:00",
            "attempts": [{"stage": "html_search", "status": "success", "path": "direct", "result_count": 1}],
            "errors": [],
            "results": parsed,
        },
    }
    result = normalize_tool_result("web_search", raw)
    assert result["ok"] is True
    assert result["kind"] == "web_search"
    assert result["search_ok"] is True
    assert result["enough_data"] is True
    assert result["results"][0]["url"] == "https://example.com/news"

    model_tool_names = {item["function"]["name"] for item in get_tools_for_model()}
    assert "web_search" in model_tool_names

    categories = {item["category"]: item["tools"] for item in list_tools()}
    assert any(tool["name"] == "web_search" for tools in categories.values() for tool in tools)

if __name__ == "__main__":
    test_tiktok_search_keeps_analysis_fields()
    test_amazon_keeps_product_fields()
    test_current_time_tool_is_available()
    test_web_search_route_exposes_web_search_tool()
    test_web_search_tool_is_registered_and_normalized()
    print("chat tool normalization tests passed")
