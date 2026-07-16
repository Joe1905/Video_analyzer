#!/usr/bin/env python3
"""Smoke tests for chat tool result normalization."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import web_app  # noqa: E402
from web_app import build_chat_history_context, build_deepseek_tool_assistant_message, build_prefixed_model_tools, build_tool_limit_final_context, chat_markdown_to_html, chat_request_needs_tools, chat_routing_text, compact_chat_tool_evidence, deepseek_tool_protocol_present, estimate_chat_context_tokens, fastmoss_analysis_evidence_gaps, fastmoss_availability_search_arguments, fastmoss_defaults_to_us, fastmoss_empty_availability_answer, fastmoss_playbook_instruction, fastmoss_playbook_intent, fastmoss_product_evidence_required, fastmoss_required_capability_gaps, filter_locked_provider_tool_ids, forced_provider_domain_tool_available, is_chat_retry_request, manage_chat_context, normalize_mcp_tool_arguments, normalize_prefixed_tool_result, normalize_tool_result, parse_chat_intent_decision, provider_default_enabled_tool_ids, provider_forces_mcp_tools, provider_scope_short_circuit, resolve_chat_intent, route_chat_intent, run_chat_deepseek  # noqa: E402
from tools import _filter_relevant_search_results, execute_tool, get_tools_for_model, list_tools, parse_bing_html, parse_duckduckgo_html  # noqa: E402


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



def test_locked_amazon_provider_filters_system_web_search() -> None:
    selected = filter_locked_provider_tool_ids("amazon", provider_default_enabled_tool_ids("amazon"))
    assert "system__web_search" not in selected
    assert "system__current_time" in selected
    assert any(tool_id.startswith("sellersprite__") for tool_id in selected)

def test_locked_amazon_product_route_keeps_sellersprite_tools() -> None:
    provider = "amazon"
    route = route_chat_intent("分析一下这个产品在亚马逊类目情况Bark Collars")
    route_intent = str(route.get("intent") or "general")
    force_mcp_tools = provider_forces_mcp_tools(provider) and route_intent not in {"web_search", "mcp_interface"}
    assert force_mcp_tools is True
    effective = filter_locked_provider_tool_ids(provider, provider_default_enabled_tool_ids(provider))
    tools = build_prefixed_model_tools(effective) if chat_request_needs_tools("Bark Collars", route) or force_mcp_tools else []
    names = {item["function"]["name"] for item in tools}
    assert "system__web_search" not in names
    assert any(name.startswith("sellersprite__") for name in names)


def test_amazon_url_query_api_fragment_does_not_disable_tools() -> None:
    text = "https://www.amazon.com/example/dp/B0FKLZF4BM/ref=sr_1_3?dib=api-token 分析一下美区亚马逊这个产品的市场情况"
    route = route_chat_intent(text)
    assert route["intent"] != "mcp_interface"
    force_mcp_tools = provider_forces_mcp_tools("amazon") and str(route.get("intent") or "general") not in {"web_search", "mcp_interface"}
    assert force_mcp_tools is True


def test_ocr_metadata_does_not_change_chat_route() -> None:
    enriched = (
        "User question:\n这些产品的销量你分析了吗"
        "\n\nImage OCR result:\nllmStatus: skipped_api_key_missing\nAPI fallback"
    )
    routing_text = chat_routing_text(enriched)
    assert routing_text == "这些产品的销量你分析了吗"
    assert route_chat_intent(routing_text)["intent"] != "mcp_interface"


def test_fastmoss_defaults_to_us_unless_another_region_is_named() -> None:
    assert fastmoss_defaults_to_us("分析 Hidden Camera Detector 在 TK 的销售数据") is True
    assert fastmoss_defaults_to_us("分析美区 Hidden Camera Detector") is True
    assert fastmoss_defaults_to_us("分析 product id 1732424427368190285") is True
    assert fastmoss_defaults_to_us("分析日本和墨西哥的 Hidden Camera Detector") is False
    assert fastmoss_defaults_to_us("Compare US and JP markets") is False


def test_fastmoss_playbook_intent_routes_official_workflows() -> None:
    cases = {
        "帮我做防偷拍探测器选品，并给出定价建议": "product",
        "拆解这个竞品店铺的打法": "competitor",
        "给这个店铺做一次店铺诊断": "shop",
        "拆解这条爆款视频为什么能卖": "content_dissect",
        "为我的产品制定内容策略和拍摄 brief": "content_strategy",
        "用 28 天数据做价格带和月度 GMV 测算": "pricing",
        "按带货力帮我找达人并写建联文案": "creator",
    }
    for text, expected in cases.items():
        assert fastmoss_playbook_intent(text) == expected
        route = route_chat_intent(text, "fastmoss")
        assert route["intent"] == f"fastmoss_{expected}"
        assert route["playbook"] == expected

    assert "playbook" not in route_chat_intent("给这个商品定价", "home")


def test_fastmoss_selection_playbook_includes_pricing_model() -> None:
    instruction = fastmoss_playbook_instruction("product")
    assert "最近 7 天" in instruction
    assert "最近 28 天" in instruction
    assert "建议上市价" in instruction
    assert "保守/基准/激进" in instruction
    assert "月度销量与 GMV" in instruction
    assert "不得把 GMV 当利润" in instruction


def test_fastmoss_product_evidence_is_scoped_by_playbook() -> None:
    assert fastmoss_product_evidence_required("帮我做防偷拍探测器选品") is True
    assert fastmoss_product_evidence_required("给这个品类做价格测算") is True
    assert fastmoss_product_evidence_required("拆解这个竞品商品") is True
    assert fastmoss_product_evidence_required("拆解这个竞品店铺") is False
    assert fastmoss_product_evidence_required("给这个店铺做店铺诊断") is False
    assert fastmoss_product_evidence_required("为我的产品制定内容策略") is False
    empty_message = SimpleNamespace(tool_calls=[], tool_results=[])
    assert fastmoss_analysis_evidence_gaps("给这个店铺做店铺诊断", empty_message) == []
    assert fastmoss_analysis_evidence_gaps("给这个品类做价格测算", empty_message) == [
        "category_lookup", "market_ranking", "us_region", "product_reviews"
    ]


def _model_tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def test_fastmoss_analysis_requires_domain_and_evidence_capabilities() -> None:
    query = "酒店防偷拍探测器在 TK 的销售怎样？"
    complete_tools = [
        _model_tool("system__current_time"),
        _model_tool("fastmoss__search_category_by_words"),
        _model_tool("fastmoss__product_rank_top_selling"),
        _model_tool("fastmoss__product_review_list"),
    ]
    assert forced_provider_domain_tool_available("fastmoss", complete_tools) is True
    assert forced_provider_domain_tool_available("fastmoss", [_model_tool("system__current_time")]) is False
    assert fastmoss_required_capability_gaps(query, complete_tools) == []
    assert fastmoss_required_capability_gaps(query, complete_tools[:-1]) == ["product_reviews"]


def test_fastmoss_analysis_requires_us_ranking_and_reviews() -> None:
    query = "分析 Hidden Camera Detector 在 TK 的销售数据"
    empty_message = SimpleNamespace(tool_calls=[], tool_results=[])
    assert fastmoss_analysis_evidence_gaps(query, empty_message) == [
        "category_lookup", "market_ranking", "us_region", "product_reviews"
    ]

    message = SimpleNamespace(
        tool_calls=[
            {"function": {"name": "fastmoss__search_category_by_words", "arguments": '{"keywords":"camera detector"}'}},
            {"function": {"name": "fastmoss__product_rank_top_selling", "arguments": '{"region":"US","category_id":"911752"}'}},
            {"function": {"name": "fastmoss__product_review_list", "arguments": '{"product_id":"1732424427368190285","region":"US"}'}},
        ],
        tool_results=[
            {"tool_name": "fastmoss__search_category_by_words", "result": {"ok": True, "enough_data": True}},
            {"tool_name": "fastmoss__product_rank_top_selling", "result": {"ok": True, "enough_data": True}},
            {"tool_name": "fastmoss__product_review_list", "result": {"ok": True, "enough_data": False}},
        ],
    )
    assert fastmoss_analysis_evidence_gaps(query, message) == []

    message.tool_calls.insert(
        2,
        {"function": {"name": "fastmoss__product_search", "arguments": '{"keywords":"camera detector"}'}},
    )
    message.tool_results.insert(
        2,
        {"tool_name": "fastmoss__product_search", "result": {"ok": True, "enough_data": True}},
    )
    message.tool_calls.append(
        {"function": {"name": "fastmoss__shop_search", "arguments": '{"seller_id":"7495582349874924375"}'}},
    )
    message.tool_results.append(
        {"tool_name": "fastmoss__shop_search", "result": {"ok": True, "enough_data": True}},
    )
    assert fastmoss_analysis_evidence_gaps(query, message) == []

    message.tool_results[1]["result"]["enough_data"] = False
    message.tool_results[1]["result"].update({"data_state": "empty", "evidence_observed": True})
    assert fastmoss_analysis_evidence_gaps(query, message) == []


def test_fastmoss_explicit_other_region_does_not_require_us() -> None:
    query = "分析日本 Hidden Camera Detector 在 TK 的销售数据"
    message = SimpleNamespace(
        tool_calls=[
            {"function": {"name": "fastmoss__search_category_by_words", "arguments": '{"keywords":"camera detector"}'}},
            {"function": {"name": "fastmoss__market_category_ranking", "arguments": '{"region":"JP","category_id":"911752"}'}},
            {"function": {"name": "fastmoss__product_review_list", "arguments": '{"product_id":"1732424427368190285","region":"JP"}'}},
        ],
        tool_results=[
            {"tool_name": "fastmoss__search_category_by_words", "result": {"ok": True, "enough_data": True}},
            {"tool_name": "fastmoss__market_category_ranking", "result": {"ok": True, "enough_data": True}},
            {"tool_name": "fastmoss__product_review_list", "result": {"ok": True, "enough_data": True}},
        ],
    )
    assert fastmoss_analysis_evidence_gaps(query, message) == []


def test_short_cjk_web_search_filters_irrelevant_results() -> None:
    results = [
        {"title": "知乎 - 有问题，就会有答案", "snippet": "中文问答社区", "url": "https://www.zhihu.com/"},
        {"title": "又一新款AI产品：AI飞飞兔惊喜来袭", "snippet": "开启智能陪伴新时代", "url": "https://www.sunfuntoys.com/"},
    ]
    filtered = _filter_relevant_search_results("飞飞兔", results)
    assert filtered == [results[1]]

def test_pdf_markdown_export_matches_frontend_quote_heading() -> None:
    rendered = chat_markdown_to_html(
        ">#### Target summary\n>\n>Case **bold**.\n\n| Item | Detail |\n|---|---|\n| A | B |"
    )
    assert "<h4>Target summary</h4>" in rendered
    assert "&gt;####" not in rendered
    assert "<strong>bold</strong>" in rendered
    assert '<table class="md-table">' in rendered

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

    bing_html = """
    <li class="b_algo"><h2><a href="https://example.com/story">Example Story</a></h2>
    <div class="b_caption"><p>Useful search snippet.</p></div></li>
    """
    assert parse_bing_html(bing_html, 3) == [{"title": "Example Story", "snippet": "Useful search snippet.", "url": "https://example.com/story"}]
    unsafe_html = '<li class="b_algo"><h2><a href="https://example.com/porn">Bad</a></h2><p>adult porn result</p></li>'
    assert parse_bing_html(unsafe_html, 3) == []

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


def _chat_message(
    message_id: str,
    role: str,
    content: str,
    *,
    status: str = "done",
    tool_calls: list[dict] | None = None,
    tool_results: list[dict] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        role=role,
        content=content,
        status=status,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        attachments=[],
    )


def test_chat_history_archives_done_tools_and_recovers_failed_results() -> None:
    done_call = {
        "id": "call_done",
        "function": {"name": "sellersprite__keyword_research", "arguments": '{"keyword":"camera"}'},
    }
    error_calls = [
        {"id": "call_1", "function": {"name": "sellersprite__keyword_research", "arguments": '{"keyword":"detector"}'}},
        {"id": "call_2", "function": {"name": "sellersprite__product_search", "arguments": '{"keyword":"detector"}'}},
    ]
    messages = [
        _chat_message("u1", "user", "分析 camera"),
        _chat_message(
            "a1",
            "assistant",
            "历史报告",
            tool_calls=[done_call],
            tool_results=[{
                "tool_name": "sellersprite__keyword_research",
                "result": {"ok": True, "mcp_text_preview": "ARCHIVED_RAW_PAYLOAD" * 1000},
            }],
        ),
        _chat_message("u2", "user", "继续分析 detector"),
        _chat_message(
            "a2",
            "assistant",
            "Request failed: 402 Payment Required",
            status="error",
            tool_calls=error_calls,
            tool_results=[
                {"tool_name": "sellersprite__keyword_research", "result": {"ok": True, "mcp_data": {"search_volume": 12000}}},
                {"tool_name": "sellersprite__product_search", "result": {"ok": True, "mcp_data": {"products_total": 4321}}},
            ],
        ),
        _chat_message("u3", "user", "继续"),
        _chat_message("a3", "assistant", "", status="pending"),
    ]

    history, recovery = build_chat_history_context(messages, "a3")
    encoded = json.dumps(history, ensure_ascii=False)
    assert "Historical tool evidence archived" in encoded
    assert "ARCHIVED_RAW_PAYLOAD" not in encoded
    assert "previous_tool_collection" in encoded
    assert "402 Payment Required" in encoded
    assert "sellersprite__product_search" in encoded
    assert recovery == {"complete": True, "tool_count": 2, "message_id": "a2"}
    assert is_chat_retry_request("继续") is True
    assert is_chat_retry_request("请重新分析这个新产品并给出完整报告") is False


def test_tool_evidence_is_compact_but_keeps_business_fields() -> None:
    evidence = compact_chat_tool_evidence(
        "sellersprite__keyword_research",
        {
            "ok": True,
            "mcp_text_preview": "RAW_SHOULD_BE_DROPPED" * 1000,
            "mcp_data": {
                "keyword": "camera detector",
                "search_volume": 12000,
                "items": [{"title": f"Product {index}", "description": "x" * 2000} for index in range(30)],
            },
        },
        max_chars=1200,
    )
    assert len(evidence) <= 1200
    assert "camera detector" in evidence
    assert "12000" in evidence
    assert "RAW_SHOULD_BE_DROPPED" not in evidence


def test_product_availability_is_a_shallow_lookup() -> None:
    cases = (
        "贪吃蛇小车这款玩具在TK上有销售吗",
        "这款产品TK是否有销售？",
        "Hidden Camera Detector 在 TikTok Shop 有没有卖？",
        "这个同款是否上架？",
    )
    empty_message = SimpleNamespace(tool_calls=[], tool_results=[])
    for text in cases:
        route = route_chat_intent(text, "fastmoss")
        assert route["intent"] == "product_availability"
        assert route["task_depth"] == "lookup"
        assert route["max_rounds"] == 2
        assert fastmoss_product_evidence_required(text, route) is False
        assert fastmoss_analysis_evidence_gaps(text, empty_message, route) == []

    analysis_text = "分析这款产品在TK的销量、市场和竞争机会"
    analysis_route = route_chat_intent(analysis_text, "fastmoss")
    assert analysis_route["intent"] == "product_research"
    assert fastmoss_product_evidence_required(analysis_text, analysis_route) is True


def test_intent_decision_validation_and_fallback() -> None:
    fallback = route_chat_intent("帮我看看这个产品", "fastmoss")
    valid = parse_chat_intent_decision(
        {
            "intent": "product_availability",
            "task_depth": "lookup",
            "entity": "磁力贪吃蛇小车",
            "region": "CN",
            "confidence": 0.94,
        },
        fallback,
        "fastmoss",
        "这款产品TK是否有销售？",
    )
    assert valid["intent"] == "product_availability"
    assert valid["route_source"] == "llm"
    assert valid["entity"] == "磁力贪吃蛇小车"
    assert valid["region"] == "US"
    assert fastmoss_availability_search_arguments(valid, "这款产品TK是否有销售？") == {
        "keywords": "磁力贪吃蛇小车",
        "region": "US",
        "pagesize": 10,
    }
    empty_answer = fastmoss_empty_availability_answer({"keywords": "磁力贪吃蛇小车", "region": "US"})
    assert "本次" in empty_answer
    assert "未检索到" in empty_answer
    assert "不表示平台上绝对没有销售" in empty_answer

    low_confidence = parse_chat_intent_decision(
        {"intent": "product_availability", "task_depth": "lookup", "confidence": 0.4},
        fallback,
        "fastmoss",
        "帮我看看这个产品",
    )
    assert low_confidence["intent"] == "product_research"
    assert low_confidence["route_source"] == "rules"
    assert parse_chat_intent_decision({"intent": "unknown", "task_depth": "lookup", "confidence": 1}, fallback, "fastmoss", "x")["intent"] == "product_research"
    assert parse_chat_intent_decision(None, fallback, "fastmoss", "x")["intent"] == "product_research"


def test_intent_router_uses_recent_context_and_falls_back_on_failure() -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": json.dumps({
                    "intent": "product_availability",
                    "task_depth": "lookup",
                    "entity": "磁力贪吃蛇小车",
                    "region": "US",
                    "confidence": 0.96,
                }, ensure_ascii=False)}}],
            }

    class FakeRequests:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.payload = None

        def post(self, _url: str, **kwargs):
            self.payload = kwargs.get("json")
            if self.fail:
                raise TimeoutError("router timeout")
            return FakeResponse()

    previous_enabled = os.environ.get("CHAT_INTENT_ROUTER_ENABLED")
    os.environ["CHAT_INTENT_ROUTER_ENABLED"] = "1"
    try:
        messages = [
            SimpleNamespace(role="user", content="贪吃蛇小车这款玩具在TK上有销售吗"),
            SimpleNamespace(role="assistant", content="旧回答失败"),
            SimpleNamespace(role="user", content="这款产品TK是否有销售？"),
        ]
        fake = FakeRequests()
        route = resolve_chat_intent(messages, "这款产品TK是否有销售？", "fastmoss", "key", "https://example.test/v1", "model", fake)
        assert route["intent"] == "product_availability"
        encoded_payload = json.dumps(fake.payload, ensure_ascii=False)
        assert "贪吃蛇小车" in encoded_payload
        assert "这款产品TK是否有销售" in encoded_payload

        fallback = resolve_chat_intent(messages, "帮我看看这个产品", "fastmoss", "key", "https://example.test/v1", "model", FakeRequests(fail=True))
        assert fallback["intent"] == "product_research"
        assert fallback["route_source"] == "rules_fallback"
    finally:
        if previous_enabled is None:
            os.environ.pop("CHAT_INTENT_ROUTER_ENABLED", None)
        else:
            os.environ["CHAT_INTENT_ROUTER_ENABLED"] = previous_enabled


def test_empty_mcp_collections_are_not_enough_data() -> None:
    def result(payload: dict) -> dict:
        return {
            "ok": True,
            "data": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            },
        }

    empty = normalize_prefixed_tool_result(
        "fastmoss__product_search",
        result({"code": 0, "message": "success", "data": {"list": [], "total": 0}}),
    )
    assert empty["enough_data"] is False
    assert empty["data_state"] == "empty"
    assert empty["evidence_observed"] is True
    assert empty["suggested_next_action"] == "answer_with_limitation"

    populated = normalize_prefixed_tool_result(
        "fastmoss__product_search",
        result({"code": 0, "data": {"list": [{"product_id": "123", "title": "Magnetic snake toy"}], "total": 1}}),
    )
    assert populated["enough_data"] is True
    assert populated["data_state"] == "data"
    assert populated["evidence_observed"] is True
    assert populated["suggested_next_action"] == "answer_from_results"

    for payload in (
        {"reviews": [], "total_review_count": 0},
        {"ranked_categories": [], "total": 0},
        {"category": {"category_id": 0, "name": "", "region": ""}, "top_products_summary": []},
    ):
        normalized = normalize_prefixed_tool_result("fastmoss__market_category_analysis", result(payload))
        assert normalized["enough_data"] is False
        assert normalized["data_state"] == "empty"

    for key in ("videos", "shops", "creators", "skus", "rows"):
        normalized = normalize_prefixed_tool_result("fastmoss__product_search", result({key: [], "total": 0}))
        assert normalized["data_state"] == "empty"
        assert normalized["evidence_observed"] is True

    partial = normalize_prefixed_tool_result(
        "fastmoss__market_category_analysis",
        result({"category": {"category_id": 935176, "name": "Food Processors"}, "gmv": 12345, "top_products_summary": []}),
    )
    assert partial["data_state"] == "data"
    assert partial["enough_data"] is True

    evidence = compact_chat_tool_evidence("fastmoss__product_review_list", empty)
    assert "接口调用成功但本轮返回空结果" in evidence
    assert "不得推断为平台绝对不存在" in evidence


def test_mcp_content_error_rules_are_provider_specific() -> None:
    def result(payload: dict) -> dict:
        return {
            "ok": True,
            "data": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": False,
            },
        }

    sellersprite = normalize_prefixed_tool_result(
        "sellersprite__asin_detail",
        result({
            "code": "OK",
            "message": "成功",
            "data": {"asin": "B0FBVL7SG7", "price": 26.99, "rating": 4.3, "ratings": 344},
        }),
    )
    assert sellersprite["ok"] is True
    assert sellersprite["enough_data"] is True
    assert sellersprite["data_state"] == "data"
    assert sellersprite["mcp_data"]["data"]["asin"] == "B0FBVL7SG7"
    assert "error" not in sellersprite

    fastmoss = normalize_prefixed_tool_result(
        "fastmoss__product_search",
        result({"code": "ERROR_QUERY", "message": "query failed", "data": None}),
    )
    assert fastmoss["ok"] is False
    assert fastmoss["error"] == "query failed"
    assert fastmoss["data_state"] == "error"


def test_fastmoss_zero_analysis_metadata_is_empty_without_affecting_sellersprite() -> None:
    payload = {
        "code": 0,
        "message": "success",
        "data": {
            "analysis_type": "basic_metrics",
            "category_id": 935176,
            "category_name": "Food Processors",
            "region": "US",
            "stat_date": "2026-W28",
            "currency": {"code": "USD", "symbol": "$"},
            "product_count": 0,
            "gmv": 0,
            "units_sold": 0,
            "creator_count": 0,
            "video_count": 0,
            "trend_series": [],
        },
    }
    raw = {"ok": True, "data": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
    fastmoss = normalize_prefixed_tool_result("fastmoss__market_category_analysis", raw)
    sellersprite = normalize_prefixed_tool_result("sellersprite__market_research", raw)
    assert fastmoss["data_state"] == "empty"
    assert fastmoss["evidence_observed"] is True
    assert sellersprite["data_state"] == "data"


def test_mcp_sql_error_text_is_not_evidence() -> None:
    raw = {
        "ok": True,
        "data": {
            "content": [{"type": "text", "text": "SQLSTATE[42S22]: Unknown column 'format_price' in 'field list'"}],
        },
    }
    normalized = normalize_prefixed_tool_result("fastmoss__product_detail_info", raw)
    assert normalized["ok"] is False
    assert normalized["enough_data"] is False
    assert normalized["data_state"] == "error"
    assert normalized["evidence_observed"] is False
    assert normalized["suggested_next_action"] == "answer_with_limitation"


def test_deepseek_tool_turn_preserves_reasoning_content() -> None:
    tool_calls = [{"id": "call_1", "function": {"name": "fastmoss__product_search", "arguments": "{}"}}]
    turn = build_deepseek_tool_assistant_message(
        {"content": "", "reasoning_content": "internal reasoning", "tool_calls": tool_calls},
        tool_calls,
        True,
    )
    assert turn["role"] == "assistant"
    assert turn["tool_calls"] == tool_calls
    assert turn["reasoning_content"] == "internal reasoning"


def test_fastmoss_amazon_request_short_circuits_without_model_or_tools() -> None:
    query = "美区亚马逊帮我找需求大但卖家少的蓝海产品"
    assert provider_scope_short_circuit("fastmoss", query, {"fastmoss__product_search"}) is not None
    assert provider_scope_short_circuit("fastmoss", query, {"sellersprite__keyword_mining"}) is None
    assert provider_scope_short_circuit("amazon", query, {"sellersprite__keyword_mining"}) is None

    class FakeStore:
        def __init__(self) -> None:
            self.updated = None
            self.broadcasted = None

        def update_message(self, _session, message, content: str, status: str = "done") -> None:
            message.content = content
            message.status = status
            self.updated = (content, status)

        def broadcast(self, session_id: str, event: str, data: dict) -> None:
            self.broadcasted = (session_id, event, data)

    store = FakeStore()
    session = SimpleNamespace(id="session_1", messages=[])
    assistant = SimpleNamespace(id="assistant_1", content="", status="pending", tool_calls=None, tool_results=None)
    run_chat_deepseek(store, session, assistant, query, "fastmoss", {"fastmoss__product_search"})
    assert assistant.status == "done"
    assert "未启用 Amazon 数据能力" in assistant.content
    assert "Amazon" in assistant.content
    assert assistant.tool_calls is None
    assert assistant.tool_results is None
    assert store.broadcasted[1] == "done"


def test_dynamic_chat_context_compresses_to_budget() -> None:
    messages = [{"role": "system", "content": "system rules", "_context_scope": "system"}]
    messages.extend(
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"history-{index}-" + ("x" * 12000),
            "_context_scope": "history",
            "_context_priority": "normal",
        }
        for index in range(8)
    )
    messages.append({
        "role": "user",
        "content": "请根据已有数据生成报告",
        "_context_scope": "history",
        "_context_priority": "keep",
    })
    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "function": {"name": "tool__one", "arguments": "{}"}}],
        "_context_scope": "current",
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "evidence-" + ("y" * 20000),
        "_context_scope": "current",
    })
    tools = [{
        "type": "function",
        "function": {"name": "tool__one", "description": "z" * 8000, "parameters": {"type": "object"}},
    }]

    request_messages, request_tools, stats = manage_chat_context(messages, tools, max_tokens=3000)
    assert stats["initial_tokens"] > stats["max_tokens"]
    assert stats["final_tokens"] <= stats["max_tokens"]
    assert stats["compressed"] is True
    assert stats["dropped_history"] > 0
    assert estimate_chat_context_tokens(request_messages, request_tools) <= 3000
    assert any(message.get("role") == "tool" for message in request_messages)
    assert all(not any(key.startswith("_context_") for key in message) for message in request_messages)


def test_tool_limit_final_context_removes_protocol_and_detects_dsml() -> None:
    dsml = (
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="sellersprite__google_trend">'
        '<｜｜DSML｜｜parameter name="request">{}</｜｜DSML｜｜parameter>'
        '</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'
    )
    assert deepseek_tool_protocol_present({"content": dsml}) is True
    assert deepseek_tool_protocol_present({"content": "这是最终的中文分析报告。"}) is False
    assert deepseek_tool_protocol_present({"tool_calls": [{"id": "call_1"}]}) is True

    messages = [
        {"role": "user", "content": "分析产品", "_context_scope": "history"},
        {"role": "assistant", "content": "感谢认可，继续做1688比价", "_context_scope": "current"},
        {
            "role": "assistant",
            "content": dsml,
            "tool_calls": [{"id": "call_1", "function": {"name": "sellersprite__google_trend"}}],
            "_context_scope": "current",
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"trend":"up"}', "_context_scope": "current"},
    ]
    final_context = build_tool_limit_final_context(messages, "分析产品")
    assert all(message.get("role") != "tool" for message in final_context)
    assert all(not message.get("tool_calls") for message in final_context)
    assert all("DSML" not in str(message.get("content") or "") for message in final_context)
    assert all("感谢认可" not in str(message.get("content") or "") for message in final_context)
    assert any("completed_tool_collection" in str(message.get("content") or "") for message in final_context)
    assert any("original_user_request" in str(message.get("content") or "") for message in final_context)


def test_sellersprite_schema_argument_normalization() -> None:
    schemas = [
        {
            "name": "keyword_research_trends",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "marketplace": {"type": "string"},
                    "keyword": {"type": "string"},
                    "month": {"type": "string"},
                },
                "required": ["marketplace", "keyword"],
                "additionalProperties": False,
            },
        },
        {
            "name": "product_research",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "object",
                        "properties": {"marketplace": {"type": "string"}, "keyword": {"type": "string"}},
                        "required": ["marketplace"],
                    },
                },
                "required": ["request"],
                "additionalProperties": False,
            },
        },
    ]
    original = web_app.list_mcp_bridge_tools
    web_app.list_mcp_bridge_tools = lambda chat_type: schemas
    try:
        unwrapped, action = normalize_mcp_tool_arguments(
            "sellersprite",
            "keyword_research_trends",
            {"request": {"marketplace": "US", "keyword": "electric food chopper", "month": "202606"}},
        )
        assert unwrapped == {"marketplace": "US", "keyword": "electric food chopper", "month": "202606"}
        assert action and action.startswith("unwrapped")

        wrapped, action = normalize_mcp_tool_arguments(
            "sellersprite", "product_research", {"marketplace": "US", "keyword": "mini chopper"}
        )
        assert wrapped == {"request": {"marketplace": "US", "keyword": "mini chopper"}}
        assert action and action.startswith("wrapped")

        try:
            normalize_mcp_tool_arguments("sellersprite", "keyword_research_trends", {"month": "202606"})
        except ValueError as exc:
            assert "marketplace" in str(exc) and "keyword" in str(exc)
        else:
            raise AssertionError("missing required fields should fail before the MCP call")
    finally:
        web_app.list_mcp_bridge_tools = original


def test_llm_router_can_select_fastmoss_playbook() -> None:
    fallback = route_chat_intent("给我一份厨房切碎机的完整调研报告", "fastmoss")
    route = parse_chat_intent_decision(
        {
            "intent": "product_research",
            "task_depth": "analysis",
            "playbook": "product",
            "entity": "electric food shredder",
            "region": "US",
            "confidence": 0.96,
        },
        fallback,
        "fastmoss",
        "给我一份厨房切碎机的完整调研报告",
    )
    assert route["intent"] == "fastmoss_product"
    assert route["task_depth"] == "workflow"
    assert route["playbook"] == "product"
    assert route["max_rounds"] == web_app.FASTMOSS_PLAYBOOKS["product"]["max_rounds"]


def test_fastmoss_workflow_phases_accept_empty_and_error_attempts() -> None:
    available = {tool_id for phases in web_app.FASTMOSS_WORKFLOW_PHASES.values() for _, tools in phases for tool_id in tools}
    for playbook_id, phases in web_app.FASTMOSS_WORKFLOW_PHASES.items():
        message = SimpleNamespace(tool_calls=[], tool_results=[])
        phase = web_app.fastmoss_workflow_phase(playbook_id, message, available)
        assert phase is not None
        assert phase[0] == phases[0][0]
        assert phase[1] == set(phases[0][1])

    message = SimpleNamespace(tool_calls=[], tool_results=[])
    first = web_app.fastmoss_workflow_phase("product", message, available)
    assert first and first[1] == {"fastmoss__search_category_by_words"}
    message.tool_results.append({
        "tool_name": "fastmoss__search_category_by_words",
        "result": {"ok": True, "enough_data": False, "data_state": "empty", "evidence_observed": True},
    })
    second = web_app.fastmoss_workflow_phase("product", message, available)
    assert second and second[0] == "获取类目规模与趋势"
    message.tool_results.append({
        "tool_name": "fastmoss__market_category_analysis",
        "result": {"ok": False, "data_state": "error", "evidence_observed": False},
    })
    alternative = web_app.fastmoss_workflow_phase("product", message, available)
    assert alternative and alternative[0] == "获取类目规模与趋势"
    assert alternative[1] == {"fastmoss__market_category_ranking"}
    message.tool_results.append({
        "tool_name": "fastmoss__market_category_ranking",
        "result": {"ok": False, "data_state": "error", "evidence_observed": False},
    })
    third = web_app.fastmoss_workflow_phase("product", message, available)
    assert third and third[0] == "获取热销与新品样本"
    assert third[1] == {"fastmoss__product_rank_top_selling"}


def test_fastmoss_product_phase_requires_complete_sample_coverage() -> None:
    available = {tool_id for phases in web_app.FASTMOSS_WORKFLOW_PHASES.values() for _, tools in phases for tool_id in tools}
    observed_tools = (
        "fastmoss__search_category_by_words",
        "fastmoss__market_category_analysis",
        "fastmoss__market_category_ranking",
        "fastmoss__product_search",
    )
    message = SimpleNamespace(
        tool_calls=[],
        tool_results=[{
            "tool_name": tool_name,
            "result": {"ok": True, "data_state": "data", "evidence_observed": True},
        } for tool_name in observed_tools],
    )
    phase = web_app.fastmoss_workflow_phase("product", message, available)
    assert phase and phase[1] == {"fastmoss__product_rank_top_selling"}
    message.tool_results.append({
        "tool_name": "fastmoss__product_rank_top_selling",
        "result": {"ok": True, "data_state": "empty", "evidence_observed": True},
    })
    phase = web_app.fastmoss_workflow_phase("product", message, available)
    assert phase and phase[1] == {"fastmoss__product_rank_new_listed"}


def test_fastmoss_business_defaults_use_verified_category_levels() -> None:
    message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__search_category_by_words",
        "result": {
            "ok": True,
            "mcp_data": {"data": {"list": [{
                "category_id_level1": 13,
                "category_id_level2": 844168,
                "category_id_level3": 935176,
            }]}},
        },
    }])
    fixed_today = __import__("datetime").date(2026, 7, 16)
    assert web_app.fastmoss_current_category_path(message) == {
        "level1": 13, "level2": 844168, "level3": 935176,
    }
    market = web_app.apply_fastmoss_business_defaults(
        "market_category_analysis", {"analysis_type": "sales_trends", "filter": {"category_id": 935176}}, message, fixed_today
    )
    assert market["filter"]["category_id"] == 844168
    assert market["filter"]["date_value"] == "2026-W28"
    ranking = web_app.apply_fastmoss_business_defaults("market_category_ranking", {}, message, fixed_today)
    assert ranking["filter"]["category_id"] == 13
    top = web_app.apply_fastmoss_business_defaults("product_rank_top_selling", {}, message, fixed_today)
    assert top["filter"]["category_id"] == 844168
    new = web_app.apply_fastmoss_business_defaults("product_rank_new_listed", {}, message, fixed_today)
    assert new["filter"]["category_l1_id"] == 13
    assert new["filter"]["category_l3_id"] == 935176
    assert new["filter"]["listing_start_date"] == "2026-06-13"
    assert new["filter"]["listing_end_date"] == "2026-07-12"
    assert "lang" not in new
    search = web_app.apply_fastmoss_business_defaults("product_search", {"keywords": "mini chopper"}, message, fixed_today)
    assert search["filter"]["category_path"] == [13, 844168, 935176]


def test_fastmoss_product_workflow_keeps_its_round_budget_isolated() -> None:
    assert web_app.chat_max_tool_rounds("fastmoss", {"playbook": "product", "max_rounds": 14}, 2) == 14
    assert web_app.chat_max_tool_rounds("amazon", {"max_rounds": 14}, 20) == 10


def test_fastmoss_clarification_is_targeted_and_provider_isolated() -> None:
    route = {"intent": "fastmoss_product", "task_depth": "workflow", "playbook": "product", "entity": ""}
    question = web_app.fastmoss_clarifying_question("fastmoss", route, "帮我做一份选品报告")
    assert question and "具体商品或品类关键词" in question
    assert "美国区" in question and "最近已完成周期" in question
    assert web_app.fastmoss_clarifying_question("fastmoss", route, "给我一份 electric food shredder 调研报告") is None
    assert web_app.fastmoss_clarifying_question("fastmoss", route, "继续分析这款产品") is None
    assert web_app.fastmoss_clarifying_question("amazon", route, "帮我做一份选品报告") is None


def test_fastmoss_close_cross_category_matches_request_confirmation() -> None:
    result = {"mcp_data": {"result": {"categories": [
        {
            "category_id_level1": 11, "category_id_level2": 858632, "category_id_level3": 861576,
            "cn_full_name": "厨房用品-刀具-厨房剪刀", "score": 0.52,
        },
        {
            "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 935176,
            "cn_full_name": "家电-厨房家电-料理机", "score": 0.502,
        },
    ]}}}
    question = web_app.fastmoss_category_ambiguity_question("Electric Food Shredder, Mini Meat Grinder 调研", result)
    assert question and "厨房剪刀" in question and "料理机" in question
    assert "不会继续消耗" in question
    result["mcp_data"]["result"]["categories"][0]["category_id_level2"] = 844168
    assert web_app.fastmoss_category_ambiguity_question("Electric Food Shredder 调研", result) is None


def test_provider_profiles_use_aggregated_sellersprite_and_staged_fastmoss_tools() -> None:
    selected = {
        "system__current_time",
        "sellersprite__keyword_research",
        "sellersprite__market_research",
        "sellersprite__product_research",
        "sellersprite__asin_detail",
        "sellersprite__asin_sales_trend",
        "sellersprite__review",
    }
    message = SimpleNamespace(tool_calls=[], tool_results=[])
    research = web_app.provider_profile_tool_ids("amazon", {"intent": "product_research"}, "electric chopper", selected, message)
    assert "sellersprite__keyword_research" in research
    assert "sellersprite__market_research" in research
    assert "sellersprite__asin_detail" not in research
    asin = web_app.provider_profile_tool_ids("amazon", {"intent": "product_research"}, "分析 B0ABCDEFGH", selected, message)
    assert "sellersprite__asin_detail" in asin
    assert "sellersprite__keyword_research" not in asin

    fastmoss_selected = {"system__current_time", "fastmoss__search_category_by_words", "fastmoss__market_category_analysis", "fastmoss__product_detail_info"}
    staged = web_app.provider_profile_tool_ids("fastmoss", {"playbook": "product"}, "electric chopper", fastmoss_selected, message)
    assert staged == {"system__current_time", "fastmoss__search_category_by_words"}


def test_region_default_only_applies_when_schema_supports_it() -> None:
    schemas = [
        {
            "name": "market_category_analysis",
            "inputSchema": {
                "type": "object",
                "properties": {"filter": {"type": "object", "properties": {"category_id": {"type": "integer"}, "region": {"type": "string"}}}},
            },
        },
        {
            "name": "product_detail_info",
            "inputSchema": {
                "type": "object",
                "properties": {"filter": {"type": "object", "properties": {"product_id": {"type": "string"}}}},
            },
        },
    ]
    original = web_app.list_mcp_bridge_tools
    web_app.list_mcp_bridge_tools = lambda _chat_type: schemas
    try:
        regional = web_app.apply_mcp_region_default("fastmoss", "market_category_analysis", {"filter": {"category_id": 935176}}, "US")
        assert regional == {"filter": {"category_id": 935176, "region": "US"}}
        no_region = web_app.apply_mcp_region_default("fastmoss", "product_detail_info", {"filter": {"product_id": "1732183167826498507"}}, "US")
        assert no_region == {"filter": {"product_id": "1732183167826498507"}}
    finally:
        web_app.list_mcp_bridge_tools = original


def test_fastmoss_deep_dive_ids_must_come_from_current_task() -> None:
    message = SimpleNamespace(
        tool_calls=[],
        tool_results=[{
            "tool_name": "fastmoss__product_search",
            "result": {"ok": True, "mcp_data": {"products": [{"product_id": "1732183167826498507"}]}},
        }, {
            "tool_name": "fastmoss__search_category_by_words",
            "result": {"ok": True, "mcp_data": {"items": [{"category_id": 935176, "category_id_level2": 844168}]}},
        }],
    )
    assert web_app.fastmoss_deep_dive_call_error(
        "fastmoss__product_detail_info", {"filter": {"product_id": "1732183167826498507"}}, "electric chopper", message
    ) is None
    assert "未经当前任务" in web_app.fastmoss_deep_dive_call_error(
        "fastmoss__product_detail_info", {"filter": {"product_id": "1730819059386716431"}}, "electric chopper", message
    )
    assert web_app.fastmoss_deep_dive_call_error(
        "fastmoss__market_category_analysis", {"filter": {"category_id": 935176}}, "electric chopper", message
    ) is None
    assert web_app.fastmoss_deep_dive_call_error(
        "fastmoss__market_category_analysis", {"filter": {"category_id": 844168}}, "electric chopper", message
    ) is None
    assert "类目 ID" in web_app.fastmoss_deep_dive_call_error(
        "fastmoss__market_category_analysis", {"filter": {"category_id": 855944}}, "electric chopper", message
    )


def test_tool_call_signature_deduplicates_argument_order() -> None:
    left = web_app.tool_call_signature("fastmoss__product_search", {"keywords": "chopper", "page": 1})
    right = web_app.tool_call_signature("fastmoss__product_search", {"page": 1, "keywords": "chopper"})
    assert left == right


if __name__ == "__main__":
    test_tiktok_search_keeps_analysis_fields()
    test_amazon_keeps_product_fields()
    test_current_time_tool_is_available()
    test_web_search_route_exposes_web_search_tool()
    test_locked_amazon_provider_filters_system_web_search()
    test_locked_amazon_product_route_keeps_sellersprite_tools()
    test_amazon_url_query_api_fragment_does_not_disable_tools()
    test_ocr_metadata_does_not_change_chat_route()
    test_fastmoss_defaults_to_us_unless_another_region_is_named()
    test_fastmoss_playbook_intent_routes_official_workflows()
    test_fastmoss_selection_playbook_includes_pricing_model()
    test_fastmoss_product_evidence_is_scoped_by_playbook()
    test_fastmoss_analysis_requires_domain_and_evidence_capabilities()
    test_fastmoss_analysis_requires_us_ranking_and_reviews()
    test_fastmoss_explicit_other_region_does_not_require_us()
    test_short_cjk_web_search_filters_irrelevant_results()
    test_pdf_markdown_export_matches_frontend_quote_heading()
    test_web_search_tool_is_registered_and_normalized()
    test_chat_history_archives_done_tools_and_recovers_failed_results()
    test_tool_evidence_is_compact_but_keeps_business_fields()
    test_product_availability_is_a_shallow_lookup()
    test_intent_decision_validation_and_fallback()
    test_intent_router_uses_recent_context_and_falls_back_on_failure()
    test_empty_mcp_collections_are_not_enough_data()
    test_mcp_content_error_rules_are_provider_specific()
    test_fastmoss_zero_analysis_metadata_is_empty_without_affecting_sellersprite()
    test_mcp_sql_error_text_is_not_evidence()
    test_deepseek_tool_turn_preserves_reasoning_content()
    test_fastmoss_amazon_request_short_circuits_without_model_or_tools()
    test_dynamic_chat_context_compresses_to_budget()
    test_tool_limit_final_context_removes_protocol_and_detects_dsml()
    test_sellersprite_schema_argument_normalization()
    test_llm_router_can_select_fastmoss_playbook()
    test_fastmoss_workflow_phases_accept_empty_and_error_attempts()
    test_fastmoss_product_phase_requires_complete_sample_coverage()
    test_fastmoss_business_defaults_use_verified_category_levels()
    test_fastmoss_product_workflow_keeps_its_round_budget_isolated()
    test_fastmoss_clarification_is_targeted_and_provider_isolated()
    test_fastmoss_close_cross_category_matches_request_confirmation()
    test_provider_profiles_use_aggregated_sellersprite_and_staged_fastmoss_tools()
    test_region_default_only_applies_when_schema_supports_it()
    test_fastmoss_deep_dive_ids_must_come_from_current_task()
    test_tool_call_signature_deduplicates_argument_order()
    print("chat tool normalization tests passed")

