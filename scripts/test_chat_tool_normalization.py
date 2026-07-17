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
from web_app import build_chat_history_context, build_deepseek_tool_assistant_message, build_prefixed_model_tools, build_tool_limit_final_context, chat_markdown_to_html, chat_request_needs_tools, chat_routing_text, compact_chat_tool_evidence, deepseek_tool_protocol_present, estimate_chat_context_tokens, fastmoss_analysis_evidence_gaps, fastmoss_availability_search_arguments, fastmoss_defaults_to_us, fastmoss_empty_availability_answer, fastmoss_playbook_instruction, fastmoss_playbook_intent, fastmoss_product_evidence_required, fastmoss_required_capability_gaps, filter_locked_provider_tool_ids, forced_provider_domain_tool_available, is_chat_retry_request, manage_chat_context, normalize_mcp_tool_arguments, normalize_prefixed_tool_result, normalize_tool_result, parse_chat_intent_decision, provider_default_enabled_tool_ids, provider_forces_mcp_tools, resolve_chat_intent, route_chat_intent  # noqa: E402
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
    assert "建议上市价" in instruction
    assert "月度销量=月流量×转化率" in instruction
    assert "缺少输入时只列公式和待补参数" in instruction
    assert "不得自行设定库存、预算、达人数量、周期或经营目标" in instruction
    assert "保守/基准/激进三套" not in instruction
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


def test_current_tool_evidence_is_lossless_until_budget_pressure() -> None:
    marker = "CURRENT_EVIDENCE_AFTER_12000_CHARS"
    payload = {
        "code": "OK",
        "message": "成功",
        "data": {
            "total": 30,
            "items": [
                {
                    "keyword": f"keyword-{index}",
                    "searches": index * 100,
                    "description": ("x" * 700) + (marker if index == 20 else ""),
                }
                for index in range(30)
            ],
        },
    }
    raw_result = {
        "ok": True,
        "data": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]},
    }
    normalized = normalize_prefixed_tool_result(
        "sellersprite__keyword_research_trends",
        raw_result,
    )
    assert isinstance(normalized["mcp_data"], str)
    assert marker not in normalized["mcp_data"]

    evidence = web_app.current_chat_tool_evidence(
        "sellersprite__keyword_research_trends",
        normalized,
        {"marketplace": "US", "keyword": "air pump"},
        raw_result,
    )
    assert marker in evidence
    assert '"marketplace":"US"' in evidence
    assert "mcp_text_preview" not in evidence

    messages = [
        {
            "role": "assistant",
            "content": "old-" + ("h" * 30000),
            "_context_scope": "history",
            "_context_priority": "normal",
        },
        {
            "role": "tool",
            "tool_call_id": "call_current",
            "content": evidence,
            "_context_scope": "current",
        },
    ]
    request_messages, _, stats = manage_chat_context(messages, [], max_tokens=12000)
    retained = next(message["content"] for message in request_messages if message.get("role") == "tool")
    assert retained == evidence
    assert stats["current_evidence_compressed"] == 0
    assert stats["compressed"] is True
    history_content = next(message["content"] for message in request_messages if message.get("role") == "assistant")
    assert len(history_content) <= 3000


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

    sellersprite_empty = normalize_prefixed_tool_result(
        "sellersprite__market_research",
        result({
            "code": "OK",
            "message": "成功",
            "data": {
                "pages": 1,
                "page": 1,
                "size": 5,
                "total": 0,
                "order": {"field": "", "desc": True},
                "items": [],
                "guestVisited": False,
            },
        }),
    )
    assert sellersprite_empty["data_state"] == "empty"
    assert sellersprite_empty["enough_data"] is False


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
    assert stats["current_evidence_compressed"] == 1
    assert stats["current_evidence_chars_after"] < stats["current_evidence_chars_before"]
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
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({
                "trend": "up",
                "series": ["x" * 3000, "DEEP_EVIDENCE_MARKER"],
            }),
            "_context_scope": "current",
        },
    ]
    final_context = build_tool_limit_final_context(messages, "分析产品")
    assert all(message.get("role") != "tool" for message in final_context)
    assert all(not message.get("tool_calls") for message in final_context)
    assert all("DSML" not in str(message.get("content") or "") for message in final_context)
    assert all("感谢认可" not in str(message.get("content") or "") for message in final_context)
    assert any("completed_tool_collection" in str(message.get("content") or "") for message in final_context)
    assert any("completed_tool_evidence" in str(message.get("content") or "") for message in final_context)
    assert any("original_user_request" in str(message.get("content") or "") for message in final_context)
    assert any("DEEP_EVIDENCE_MARKER" in str(message.get("content") or "") for message in final_context)
    request_messages, _, stats = manage_chat_context(final_context, [], max_tokens=100000)
    assert stats["current_evidence_compressed"] == 0
    assert any("DEEP_EVIDENCE_MARKER" in str(message.get("content") or "") for message in request_messages)


def test_tool_limit_keeps_large_current_collection_when_capacity_allows() -> None:
    messages = [{"role": "user", "content": "分析 Air Pump", "_context_scope": "history"}]
    markers = []
    for index in range(21):
        marker = f"CURRENT_TOOL_MARKER_{index:02d}"
        markers.append(marker)
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"call_{index}",
                "function": {"name": "sellersprite__keyword_research_trends", "arguments": "{}"},
            }],
            "_context_scope": "current",
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"call_{index}",
            "content": web_app.current_chat_tool_evidence(
                "sellersprite__keyword_research_trends",
                {"ok": True, "mcp_data": {"series": ["x" * 8200, marker]}},
                {"marketplace": "US", "keyword": f"air pump {index}"},
            ),
            "_context_scope": "current",
        })

    final_context = build_tool_limit_final_context(messages, "分析 Air Pump")
    request_messages, request_tools, stats = manage_chat_context(final_context, [], max_tokens=120000)
    encoded = json.dumps(request_messages, ensure_ascii=False)
    assert request_tools == []
    assert stats["over_budget"] is False
    assert stats["current_evidence_compressed"] == 0
    assert stats["current_evidence_chars_before"] > 170000
    assert stats["current_evidence_chars_after"] == stats["current_evidence_chars_before"]
    assert all(marker in encoded for marker in markers)


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
    assert second and second[0].startswith("获取类目规模与趋势")
    message.tool_calls.append({
        "function": {"name": "fastmoss__market_category_analysis", "arguments": json.dumps({"analysis_type": "basic_metrics"})},
    })
    message.tool_results.append({
        "tool_name": "fastmoss__market_category_analysis",
        "result": {"ok": False, "data_state": "error", "evidence_observed": False},
    })
    alternative = web_app.fastmoss_workflow_phase("product", message, available)
    assert alternative and alternative[0].startswith("获取类目规模与趋势")
    assert alternative[1] == {"fastmoss__market_category_analysis"}
    assert "sales_trends" in alternative[0]
    for analysis_type in ("sales_trends", "price_distribution"):
        message.tool_calls.append({
            "function": {"name": "fastmoss__market_category_analysis", "arguments": json.dumps({"analysis_type": analysis_type})},
        })
        message.tool_results.append({
            "tool_name": "fastmoss__market_category_analysis",
            "result": {"ok": False, "data_state": "error", "evidence_observed": False},
        })
    ranking = web_app.fastmoss_workflow_phase("product", message, available)
    assert ranking and ranking[1] == {"fastmoss__market_category_ranking"}
    message.tool_results.append({
        "tool_name": "fastmoss__market_category_ranking",
        "result": {"ok": False, "data_state": "error", "evidence_observed": False},
    })
    third = web_app.fastmoss_workflow_phase("product", message, available)
    assert third and third[0] == "获取热销样本"
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
        tool_calls=[{
            "function": {
                "name": "fastmoss__market_category_analysis",
                "arguments": json.dumps({"analysis_type": analysis_type}),
            },
        } for analysis_type in web_app.FASTMOSS_PRODUCT_MARKET_ANALYSIS_TYPES],
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
    instruction = web_app.fastmoss_workflow_instruction(phase)
    assert "新品成功率" in instruction
    assert "统一截止日" in instruction


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
    assert top["orderby"] == [{"field": "period_units_sold", "order": "desc"}]
    assert new["orderby"] == [{"field": "day3_units_sold", "order": "desc"}]
    search = web_app.apply_fastmoss_business_defaults("product_search", {"keywords": "mini chopper"}, message, fixed_today)
    assert search["filter"]["category_path"] == [13, 844168, 935176]
    planned = web_app.fastmoss_planned_product_search_arguments(
        message,
        "Electric Food Shredder, Mini Meat Grinder 调研报告",
        {"playbook": "product", "entity": "Electric Food Shredder, Mini Meat Grinder"},
        "US",
    )
    assert planned == {
        "filter": {"category_path": [13, 844168, 935176], "region": "US"},
        "page": 1,
        "pagesize": 10,
        "orderby": [{"field": "day28_units_sold", "order": "desc"}],
    }

    named_category_message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__search_category_by_words",
        "result": {"ok": True, "mcp_data": {"result": {"categories": [
            {
                "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 934792,
                "cn_name": "搅拌机", "score": 0.6977,
            },
            {
                "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 935176,
                "cn_name": "料理机", "score": 0.5023,
            },
        ]}}},
    }])
    assert web_app.fastmoss_current_category_path(named_category_message) == {
        "level1": 13, "level2": 844168, "level3": 934792,
    }
    assert web_app.fastmoss_current_category_path(named_category_message, "第二个，料理机") == {
        "level1": 13, "level2": 844168, "level3": 935176,
    }
    named_search = web_app.apply_fastmoss_business_defaults(
        "product_search", {}, named_category_message, fixed_today,
        user_text="目标类目明确选择料理机", route={"playbook": "product", "entity": "料理机"},
    )
    assert named_search["filter"]["category_path"] == [13, 844168, 935176]
    category_args = web_app.apply_fastmoss_business_defaults(
        "search_category_by_words", {"query": ["food processor"], "desc": "瑜伽裤", "unexpected": 1},
        named_category_message, fixed_today, user_text="料理机",
    )
    assert category_args == {"query": ["food processor"]}


def test_fastmoss_product_workflow_keeps_its_round_budget_isolated() -> None:
    assert web_app.chat_max_tool_rounds("fastmoss", {"playbook": "product"}, 2) == 24
    assert web_app.chat_max_tool_rounds("fastmoss", {"playbook": "product", "max_rounds": 14}, 2) == 24
    assert web_app.chat_max_tool_rounds(
        "fastmoss", {"playbook": "product", "max_rounds": 14, "full_ranking": True}, 2
    ) == 27
    assert web_app.chat_max_tool_rounds("amazon", {"max_rounds": 14}, 20) == 10


def test_fastmoss_research_report_uses_product_playbook_on_rule_fallback() -> None:
    text = "给我一份 Electric Food Shredder, Mini Meat Grinder 这类产品的调研报告"
    assert web_app.fastmoss_playbook_intent(text) == "product"
    assert web_app.fastmoss_segment_keywords(text, {"playbook": "product"}) == [
        "Electric Food Shredder", "Mini Meat Grinder",
    ]
    explicit = "目标类目明确选择料理机。请给我一份 Electric Food Shredder 和 Mini Meat Grinder 这类产品的完整调研报告。"
    assert web_app.fastmoss_segment_keywords(explicit, {"entity": explicit}) == [
        "Electric Food Shredder", "Mini Meat Grinder",
    ]
    route = web_app.route_chat_intent(text, "fastmoss")
    assert route["intent"] == "fastmoss_product"
    assert route["playbook"] == "product"
    messages = [
        SimpleNamespace(role="user", content=text),
        SimpleNamespace(role="assistant", content="请选择第二个类目"),
        SimpleNamespace(role="user", content="第二个，料理机。请继续生成完整调研报告。"),
    ]
    inherited = web_app.fastmoss_inherited_segment_keywords(messages, messages[-1].content)
    assert inherited == ["Electric Food Shredder", "Mini Meat Grinder"]
    assert web_app.fastmoss_segment_keywords(messages[-1].content, {"segment_keywords": inherited}) == inherited
    assert web_app.fastmoss_inherited_segment_keywords(messages, "重新调研 air fryer") == []


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


def _fastmoss_search_message(calls: list[dict], results: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        tool_calls=[{
            "id": f"call_{index}",
            "function": {"name": "fastmoss__product_search", "arguments": json.dumps(args)},
        } for index, args in enumerate(calls)],
        tool_results=results or [],
    )


def test_fastmoss_dual_ranking_plan_uses_three_sorted_category_pages_then_segments() -> None:
    route = {"playbook": "product", "entity": "Electric Food Shredder / Mini Meat Grinder"}
    message = _fastmoss_search_message([])
    plan = web_app.fastmoss_product_search_plan(message, "给我一份调研报告", route)
    assert plan["category_pages"] == 3
    assert plan["segment_keywords"] == ["Electric Food Shredder", "Mini Meat Grinder"]
    assert plan["next_call"] == {"scope": "category_head", "page": 1, "pagesize": 10}

    message = _fastmoss_search_message([
        {"page": 1, "pagesize": 10},
        {"page": 2, "pagesize": 10},
        {"page": 3, "pagesize": 10},
    ])
    plan = web_app.fastmoss_product_search_plan(message, "给我一份调研报告", route)
    assert plan["next_call"]["scope"] == "segment_head"
    assert plan["next_call"]["keywords"] == "Electric Food Shredder"

    message.tool_calls.extend([
        {"id": "segment_1", "function": {"name": "fastmoss__product_search", "arguments": json.dumps({"keywords": "Electric Food Shredder", "page": 1})}},
        {"id": "segment_2", "function": {"name": "fastmoss__product_search", "arguments": json.dumps({"keywords": "Mini Meat Grinder", "page": 1})}},
    ])
    assert web_app.fastmoss_product_search_plan(message, "给我一份调研报告", route)["complete"] is True
    assert web_app.fastmoss_product_search_plan(message, "给我完整类目榜单", route)["category_pages"] == 6


def test_fastmoss_product_phase_waits_for_all_category_and_segment_searches() -> None:
    available = {tool_id for phases in web_app.FASTMOSS_WORKFLOW_PHASES.values() for _, tools in phases for tool_id in tools}
    completed_before_search = (
        "fastmoss__search_category_by_words",
        "fastmoss__market_category_analysis",
        "fastmoss__market_category_ranking",
        "fastmoss__product_rank_top_selling",
        "fastmoss__product_rank_new_listed",
    )
    message = _fastmoss_search_message([{"page": 1}, {"page": 2}], [
        *[{
            "tool_name": tool_name,
            "result": {"ok": True, "data_state": "data", "evidence_observed": True},
        } for tool_name in completed_before_search],
        {"tool_name": "fastmoss__product_search", "result": {"ok": True, "data_state": "data", "evidence_observed": True}},
        {"tool_name": "fastmoss__product_search", "result": {"ok": True, "data_state": "empty", "evidence_observed": True}},
    ])
    message.tool_calls.extend({
        "function": {
            "name": "fastmoss__market_category_analysis",
            "arguments": json.dumps({"analysis_type": analysis_type}),
        },
    } for analysis_type in web_app.FASTMOSS_PRODUCT_MARKET_ANALYSIS_TYPES)
    route = {"playbook": "product", "entity": "Electric Food Shredder / Mini Meat Grinder"}
    phase = web_app.fastmoss_workflow_phase("product", message, available, "调研", route)
    assert phase and phase[0] == "获取类目销量头部（第 3/3 页）"
    message.tool_calls.append({
        "function": {"name": "fastmoss__product_search", "arguments": json.dumps({"page": 3})},
    })
    phase = web_app.fastmoss_workflow_phase("product", message, available, "调研", route)
    assert phase and phase[0] == "补充细分匹配样本"
    assert phase[1] == {"fastmoss__product_search"}


def test_fastmoss_product_workflow_deterministically_advances_and_binds_two_targets() -> None:
    available = provider_default_enabled_tool_ids("fastmoss")
    route = {"playbook": "product", "entity": "Electric Food Shredder / Mini Meat Grinder"}
    category_result = {
        "tool_name": "fastmoss__search_category_by_words",
        "result": {
            "ok": True,
            "data_state": "data",
            "evidence_observed": True,
            "mcp_data": {"items": [{
                "category_id_level1": 13,
                "category_id_level2": 844168,
                "category_id_level3": 935176,
            }]},
        },
    }
    message = SimpleNamespace(tool_calls=[], tool_results=[category_result])
    first = web_app.fastmoss_planned_product_workflow_call(message, "调研", route, available, "US")
    assert first and first[0] == "fastmoss__market_category_analysis"
    assert first[1]["analysis_type"] == "basic_metrics"
    assert first[1]["filter"]["region"] == "US"
    message.tool_calls.append({
        "function": {"name": first[0], "arguments": json.dumps(first[1])},
    })
    message.tool_results.append({
        "tool_name": first[0],
        "result": {"ok": True, "data_state": "empty", "evidence_observed": False},
    })
    second = web_app.fastmoss_planned_product_workflow_call(message, "调研", route, available, "US")
    assert second and second[0] == "fastmoss__market_category_analysis"
    assert second[1]["analysis_type"] == "sales_trends"

    message.tool_calls.extend({
        "function": {
            "name": "fastmoss__market_category_analysis",
            "arguments": json.dumps({"analysis_type": analysis_type}),
        },
    } for analysis_type in ("sales_trends", "price_distribution"))
    for tool_name in (
        "fastmoss__market_category_analysis",
        "fastmoss__market_category_analysis",
        "fastmoss__market_category_ranking",
        "fastmoss__product_rank_top_selling",
        "fastmoss__product_rank_new_listed",
    ):
        message.tool_results.append({
            "tool_name": tool_name,
            "result": {"ok": True, "data_state": "data", "evidence_observed": True},
        })
    message.tool_calls.extend([
        {"function": {"name": "fastmoss__market_category_ranking", "arguments": "{}"}},
        {"function": {"name": "fastmoss__product_rank_top_selling", "arguments": "{}"}},
        {"function": {"name": "fastmoss__product_rank_new_listed", "arguments": "{}"}},
    ])
    search_calls = [
        {"page": 1}, {"page": 2}, {"page": 3},
        {"keywords": "Electric Food Shredder", "page": 1},
        {"keywords": "Mini Meat Grinder", "page": 1},
    ]
    message.tool_calls.extend({
        "function": {"name": "fastmoss__product_search", "arguments": json.dumps(arguments)},
    } for arguments in search_calls)
    for query, product in (
        ("Electric Food Shredder", {
            "product_id": "172900000000000001", "day28_units_sold": 100,
            "day28_gmv": 1000, "price_min": 10, "price_max": 10,
        }),
        ("Mini Meat Grinder", {
            "product_id": "172900000000000002", "day28_units_sold": 80,
            "day28_gmv": 1600, "price_min": 20, "price_max": 20,
        }),
    ):
        message.tool_results.append({
            "tool_name": "fastmoss__product_search",
            "result": {
                "ok": True,
                "data_state": "data",
                "evidence_observed": True,
                "evidence_metadata": {"scope": "segment_head", "query": query},
                "evidence_product_records": [product],
            },
        })
    # The three category pages need stored results as well, even though target
    # selection comes from the two segment calls above.
    message.tool_results.extend({
        "tool_name": "fastmoss__product_search",
        "result": {
            "ok": True, "data_state": "empty", "evidence_observed": False,
            "evidence_metadata": {"scope": "category_head", "page": page},
            "evidence_product_records": [],
        },
    } for page in (1, 2, 3))

    overview = web_app.fastmoss_planned_product_workflow_call(message, "调研", route, available, "US")
    assert overview and overview[0] == "fastmoss__product_overview"
    assert overview[1]["filter"]["product_id"] == "172900000000000001"
    for product_id in ("172900000000000001", "172900000000000002"):
        message.tool_calls.append({
            "function": {
                "name": "fastmoss__product_overview",
                "arguments": json.dumps({"filter": {"product_id": product_id, "time_range_days": 28}}),
            },
        })
    trend = web_app.fastmoss_planned_product_workflow_call(message, "调研", route, available, "US")
    assert trend and trend[0] == "fastmoss__product_sales_trend"
    assert trend[1]["filter"] == {"product_id": "172900000000000001", "time_range_days": 90}


def test_fastmoss_product_search_defaults_force_category_pages_and_short_segment_queries() -> None:
    category_result = {
        "tool_name": "fastmoss__search_category_by_words",
        "result": {"ok": True, "mcp_data": {"items": [{
            "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 935176,
        }]}},
    }
    route = {"playbook": "product", "entity": "Electric Food Shredder / Mini Meat Grinder"}
    message = SimpleNamespace(tool_calls=[], tool_results=[category_result])
    first = web_app.apply_fastmoss_business_defaults(
        "product_search", {
            "keywords": "wrong long keyword", "page": 99,
            "filter": {"day28_units_sold_range": {"min": 0}, "region": "US"},
        }, message,
        user_text="调研", route=route,
    )
    assert "keywords" not in first
    assert first["page"] == 1 and first["pagesize"] == 10
    assert first["orderby"] == [{"field": "day28_units_sold", "order": "desc"}]
    assert first["filter"]["category_path"] == [13, 844168, 935176]
    assert first["filter"] == {"category_path": [13, 844168, 935176], "region": "US"}

    message.tool_calls = [
        {"function": {"name": "fastmoss__product_search", "arguments": json.dumps({"page": page})}}
        for page in (1, 2, 3)
    ]
    segment = web_app.apply_fastmoss_business_defaults(
        "product_search", {"keywords": "ignored"}, message,
        user_text="调研", route=route,
    )
    assert segment["keywords"] == "Electric Food Shredder"
    assert segment["page"] == 1


def test_fastmoss_evidence_manifest_separates_total_fetched_overlap_and_conflicts() -> None:
    def result(scope: str, page: int, query: str, total: int, records: list[dict]) -> dict:
        return {
            "tool_name": "fastmoss__product_search",
            "result": {
                "ok": True,
                "data_state": "data",
                "evidence_observed": True,
                "evidence_metadata": {
                    "scope": scope, "page": page, "query": query or None,
                    "reported_total": total, "fetched_records": len(records), "sort_verified": True,
                    "category_level": "L3",
                },
                "evidence_product_records": records,
            },
        }
    category_records = [
        {"product_id": "1732183167826498507", "title": "Head", "day7_units_sold": 120, "day28_units_sold": 100, "day28_gmv": 1000, "price_min": 9, "price_max": 11},
        {"product_id": "1732183167826498508", "title": "Second", "day28_units_sold": 80},
    ]
    segment_records = [dict(category_records[0])]
    tool_results = [
        result("category_head", 1, "", 60, category_records),
        result("category_head", 2, "", 60, []),
        result("category_head", 3, "", 60, []),
        result("segment_head", 1, "mini grinder", 12, segment_records),
    ]
    calls = [{"page": 1}, {"page": 2}, {"page": 3}, {"page": 1, "keywords": "mini grinder"}]
    message = _fastmoss_search_message(calls, tool_results)
    manifest = web_app.fastmoss_evidence_manifest(message, "普通调研", {"playbook": "product", "entity": "mini grinder"})
    assert manifest["category_head"]["reported_total"] == 60
    assert manifest["category_head"]["fetched_unique"] == 2
    assert manifest["category_head"]["completed_pages"] == [1, 2, 3]
    assert manifest["overlap_product_ids"] == ["1732183167826498507"]
    assert any("近7天销量高于近28天销量" in item["issue"] for item in manifest["conflicts"])
    assert sum("近7天销量高于近28天销量" in item["issue"] for item in manifest["conflicts"]) == 1
    assert any("接口报告匹配总数 60" in item for item in manifest["limitations"])
    signals = manifest["derived_signals"]
    assert signals["category_sample_units_total"] == 180
    assert signals["category_top1_share"] == 100 / 180
    assert signals["overlap_rate_of_segment_sample"] == 1
    assert signals["price_midpoint_median"] == 10
    assert signals["segment_queries"][0]["sample_units_total"] == 100


def test_fastmoss_deterministic_fallback_contains_analysis_and_prioritized_advice() -> None:
    category_products = [
        {"product_id": "1", "title": "Leader", "day28_units_sold": 400, "price_min": 30, "price_max": 40},
        {"product_id": "2", "title": "Second", "day28_units_sold": 300, "price_min": 35, "price_max": 45},
        {"product_id": "3", "title": "Third", "day28_units_sold": 200, "price_min": 40, "price_max": 50},
        {"product_id": "4", "title": "Tail", "day28_units_sold": 100, "price_min": 45, "price_max": 55},
    ]
    segment_products = [
        {"product_id": "1", "title": "Mini grinder leader", "query": "Mini Meat Grinder", "day28_units_sold": 300},
        {"product_id": "5", "title": "Mini grinder tail", "query": "Mini Meat Grinder", "day28_units_sold": 100},
        {"product_id": "6", "title": "Shredder", "query": "Electric Food Shredder", "day28_units_sold": 20},
    ]
    manifest = {
        "category_head": {
            "products": category_products, "fetched_unique": 4, "target_pages": 3,
            "completed_pages": [1, 2, 3], "reported_total": 20,
        },
        "segment_head": {"products": segment_products, "fetched_unique": 3},
        "overlap_product_ids": ["1"],
        "target_category_path": {"level1": 13, "level2": 844168, "level3": 935176},
        "derived_signals": {
            "category_top3_share": 0.9,
            "overlap_rate_of_segment_sample": 1 / 3,
            "price_midpoint_q1": 38.75,
            "price_midpoint_median": 42.5,
            "price_midpoint_q3": 46.25,
            "segment_queries": [
                {"query": "Mini Meat Grinder", "fetched_unique": 2, "sample_units_total": 400, "top_product_units": 300},
                {"query": "Electric Food Shredder", "fetched_unique": 2, "sample_units_total": 20, "top_product_units": 20},
            ],
        },
        "limitations": [],
        "conflicts": [],
    }
    report = web_app.fastmoss_deterministic_quality_fallback(manifest)
    assert "样本销量明显向少数商品集中" in report
    assert "Mini Meat Grinder 的样本销量合计为 400" in report
    assert "把 Mini Meat Grinder 放到更高的验证优先级" in report
    assert "中间 50%" in report and "38.75–46.25" in report and "中位数约 42.5" in report
    assert "**先定方向：** 优先围绕 Mini Meat Grinder" in report
    assert "不能把配件、不同规格和不同使用场景的商品混成一个价格带" in report


def test_fastmoss_l2_market_metrics_are_only_upstream_category_reference() -> None:
    message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__search_category_by_words",
        "result": {"ok": True, "data_state": "data", "evidence_observed": True, "mcp_data": {"items": [{
            "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 935176,
        }]}},
    }, {
        "tool_name": "fastmoss__market_category_analysis",
        "result": {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_metadata": {"source_tool": "fastmoss__market_category_analysis", "category_level": "L2", "scope": "supporting"},
        },
    }])
    manifest = web_app.fastmoss_evidence_manifest(message, "调研", {"playbook": "product"})
    assert manifest["target_category_path"] == {"level1": 13, "level2": 844168, "level3": 935176}
    assert manifest["market_category_levels"] == ["L2"]
    assert any("不能直接作为目标 L3 类目规模" in item for item in manifest["limitations"])


def test_fastmoss_failed_category_page_is_not_counted_as_coverage() -> None:
    message = _fastmoss_search_message([{"page": 1}, {"page": 2}, {"page": 3}], [{
        "tool_name": "fastmoss__product_search",
        "result": {
            "ok": False, "data_state": "error", "evidence_observed": False,
            "evidence_metadata": {"scope": "category_head", "page": 1, "data_state": "error"},
        },
    }, *[{
        "tool_name": "fastmoss__product_search",
        "result": {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_metadata": {"scope": "category_head", "page": page, "data_state": "data"},
        },
    } for page in (2, 3)]])
    manifest = web_app.fastmoss_evidence_manifest(message, "调研", {"playbook": "product"})
    assert manifest["category_head"]["attempted_pages"] == [1, 2, 3]
    assert manifest["category_head"]["completed_pages"] == [2, 3]
    assert manifest["category_head"]["coverage_complete"] is False
    assert any("页码 [1] 调用失败" in item for item in manifest["limitations"])


def test_fastmoss_metadata_detects_unsorted_page_and_string_product_ids() -> None:
    payload = {"total": 60, "list": [
        {"product": {"product_id": "1732183167826498507", "title": "Low", "floor_price": 9, "ceiling_price": 12}, "sales_summary": {"last_28d_units_sold": 10, "last_28d_gmv": 100}},
        {"product": {"product_id": "1732183167826498508", "title": "High", "floor_price": 10, "ceiling_price": 15}, "sales_summary": {"last_28d_units_sold": 20, "last_28d_gmv": 250}},
    ]}
    raw = {"data": {"content": [{"text": json.dumps(payload)}]}}
    normalized = {"ok": True, "data_state": "data", "evidence_observed": True}
    annotated = web_app.annotate_fastmoss_tool_result(
        "fastmoss__product_search",
        {"page": 1, "pagesize": 10, "orderby": [{"field": "day28_units_sold", "order": "desc"}]},
        normalized,
        raw,
    )
    assert annotated["evidence_metadata"]["reported_total"] == 60
    assert annotated["evidence_metadata"]["sort_verified"] is False
    assert annotated["evidence_product_records"][0]["day28_units_sold"] == 10
    assert annotated["evidence_product_records"][0]["price_max"] == 12
    assert web_app._collect_named_ids(json.dumps(payload), {"productid", "goodsid", "itemid"}) == {
        "1732183167826498507", "1732183167826498508",
    }

    dated_payload = {"trend_series": [{"date": "2026-01-16", "period_units_sold": 10}]}
    dated_raw = {"data": {"content": [{"text": json.dumps(dated_payload)}]}}
    dated = web_app.annotate_fastmoss_tool_result(
        "fastmoss__market_category_analysis",
        {"filter": {"date_type": "week", "date_value": "2026-W28"}},
        {"ok": True, "data_state": "data", "evidence_observed": True},
        dated_raw,
    )
    dated_message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__market_category_analysis", "result": dated,
    }])
    dated_manifest = web_app.fastmoss_evidence_manifest(dated_message, "调研", {"playbook": "product"})
    assert any("两者不重叠" in item["issue"] for item in dated_manifest["conflicts"])


def test_fastmoss_evidence_ledger_keeps_native_dimensions_and_late_trends() -> None:
    def annotate(name: str, arguments: dict, payload: dict) -> dict:
        return web_app.annotate_fastmoss_tool_result(
            f"fastmoss__{name}",
            arguments,
            {"ok": True, "data_state": "data", "evidence_observed": True},
            {"data": {"content": [{"text": json.dumps(payload)}]}},
        )

    category_trend = annotate("market_category_analysis", {
        "filter": {"category_id": 844168, "region": "US", "date_type": "week", "date_value": "2026-W28"},
    }, {
        "analysis_type": "sales_trends",
        "category": {"category_id": 844168, "category_name": "厨房家电", "region": "US"},
        "summary_metrics": {"category_units_sold_total": 473150, "selling_video_count_total": 70612},
        "trend_series": [{"period_label": "2026-06-01 ~ 2026-06-07", "category_units_sold": 71374}],
    })
    ranking = annotate("market_category_ranking", {"filter": {"category_id": 13, "region": "US"}}, {
        "ranking_scope": {"parent_category_id": 13, "ranked_category_level": 2, "region": "US"},
        "ranked_categories": [{
            "category_id": 844168, "category_name": "厨房家电", "category_units_sold": 425507,
            "channel_gmv_share": {"video_gmv_share_percent": 57.15, "live_gmv_share_percent": 11.87},
        }],
    })
    new_products = annotate("product_rank_new_listed", {"filter": {"category_id": 935176, "region": "US"}}, {
        "list": [{
            "product_id": "1732461324214113003", "title": "Mini Chopper", "current_price": 13.57,
            "commission_rate_percent": 14.5, "first_3d_units_sold": 18, "first_3d_gmv": 210.42,
        }],
    })
    product_sample = annotate("product_search", {"keywords": "Mini Meat Grinder", "page": 1}, {
        "list": [{
            "distribution_summary": {"linked_creator_count": 406, "linked_video_count": 119},
            "product": {"product_id": "1730898744848192092", "title": "Mini Meat Grinder", "floor_price": 36.05, "ceiling_price": 53},
            "sales_summary": {"last_28d_units_sold": 424, "last_28d_gmv": 29727},
        }],
    })
    overview = annotate("product_overview", {"filter": {"product_id": "1730898744848192092"}}, {
        "ads_distribution": {"breakdown": [{"traffic_source": "ad_traffic", "gmv_share_percent": 59}]},
        "channel_distribution": {"breakdown": [{"sales_channel": "affiliate", "units_sold_share_percent": 67}]},
        "content_distribution": {"breakdown": [{"content_type": "video", "gmv_share_percent": 66}]},
        "daily_trend": [{"date": "2026-06-18", "daily_units_sold": 4, "daily_gmv": 304}],
    })
    trend_17 = annotate("product_sales_trend", {"filter": {"product_id": "1730898744848192092"}}, {
        "daily_trend": [
            {"date": f"2026-04-{day:02d}", "daily_units_sold": day, "daily_gmv": day * 10}
            for day in range(1, 31)
        ] + [{"date": "2026-05-01", "daily_units_sold": 1, "daily_gmv": 10}],
    })
    trend_18 = annotate("product_sales_trend", {"filter": {"product_id": "1731973841209955212"}}, {
        "daily_trend": [
            {"date": f"2026-05-{day:02d}", "daily_units_sold": 1 if day <= 10 else 0, "daily_gmv": 10 if day <= 10 else 0}
            for day in range(1, 31)
        ],
    })
    reviews = annotate("product_review_list", {"filter": {"product_id": "1730898744848192092"}}, {
        "reviews": [], "total_review_count": 0,
    })

    tool_results = [{"tool_name": name, "result": result} for name, result in (
        ("fastmoss__market_category_analysis", category_trend),
        ("fastmoss__market_category_ranking", ranking),
        ("fastmoss__product_rank_new_listed", new_products),
        ("fastmoss__product_search", product_sample),
        ("fastmoss__product_overview", overview),
        ("fastmoss__product_sales_trend", trend_17),
        ("fastmoss__product_sales_trend", trend_18),
        ("fastmoss__product_review_list", reviews),
    )]
    manifest = web_app.fastmoss_evidence_manifest(
        SimpleNamespace(tool_calls=[], tool_results=tool_results),
        "调研",
        {"playbook": "product", "entity": "Mini Meat Grinder"},
    )
    facts = manifest["evidence_facts"]
    assert manifest["evidence_fact_count"] == 8
    assert "evidence_excerpts" not in manifest
    assert {fact["dimension"] for fact in facts} == {
        "category_trend", "category_channel_ranking", "new_products", "product_sample",
        "product_overview", "product_90d_trend", "review_status",
    }
    assert next(fact for fact in facts if fact["dimension"] == "category_trend")["summary_metrics"]["category_units_sold_total"] == 473150
    assert next(fact for fact in facts if fact["dimension"] == "category_channel_ranking")["categories"][0]["channel_gmv_share"]["video_gmv_share_percent"] == 57.15
    assert next(fact for fact in facts if fact["dimension"] == "new_products")["products"][0]["first_3d_units_sold"] == 18
    assert next(fact for fact in facts if fact["dimension"] == "product_sample")["products"][0]["linked_creator_count"] == 406
    assert next(fact for fact in facts if fact["dimension"] == "product_overview")["ads_distribution"]["breakdown"][0]["gmv_share_percent"] == 59
    late_trends = [fact for fact in facts if fact["dimension"] == "product_90d_trend"]
    assert [fact["product_id"] for fact in late_trends] == ["1730898744848192092", "1731973841209955212"]
    assert late_trends[0]["trend_summary"]["first_30d_units_sold"] == 465
    assert late_trends[0]["trend_summary"]["last_30d_units_sold"] == 465
    assert next(fact for fact in facts if fact["dimension"] == "review_status")["state"] == "empty"


def test_fastmoss_answer_verifier_applies_local_edits_and_keeps_draft_on_failure() -> None:
    message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__product_overview",
        "result": {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_metadata": {"source_tool": "fastmoss__product_overview", "scope": "supporting"},
            "evidence_facts": [{
                "source_tool": "fastmoss__product_overview", "dimension": "product_overview",
                "product_id": "1730898744848192092",
                "ads_distribution": {"breakdown": [{"traffic_source": "ad_traffic", "gmv_share_percent": 59}]},
            }],
        },
    }])
    route = {"playbook": "product", "task_depth": "workflow", "entity": "mini grinder"}
    style = web_app.fastmoss_report_style_instruction(route)
    assert "不要求出现固定短语" in style
    assert "完整调研报告" in style and "精确上市价" in style
    assert web_app.fastmoss_report_style_instruction({"task_depth": "lookup"}) == ""

    native_report = (
        "## 核心判断\n类目渠道结构显示视频成交更重要。\n"
        "## 研究口径\n这里说明上级类目与样本边界。\n"
        "## 平台成交结构\n广告和联盟数据用于核对代表商品。\n"
        "## 验证建议\n优先验证同形态商品。"
    )
    native_manifest = {
        "evidence_facts": [{"dimension": "category_channel_ranking"}, {"dimension": "product_overview"}],
    }
    assert web_app.fastmoss_rewrite_preserves_report_detail("", native_report, native_manifest, route)

    class FakeResponse:
        finish_reason = "stop"

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "approved": False,
                "risk_level": "high",
                "edits": [{
                    "original": "这个细分已经被证明是低竞争市场。",
                    "replacement": "本轮样本显示该细分仍需补充竞争强度证据。",
                    "reason": "当前样本不能证明低竞争",
                    "evidence_refs": ["fastmoss__product_overview"],
                }],
            })}, "finish_reason": self.finish_reason}]}

    class FakeRequests:
        @staticmethod
        def post(*_args, **_kwargs):
            return FakeResponse()

    original_record = web_app.record_api_call
    web_app.record_api_call = lambda *_args, **_kwargs: None
    try:
        draft = (
            "## 核心判断\n这个细分已经被证明是低竞争市场。\n\n"
            "## 代表商品\n| 商品 | 近28天销量 | 观测价格 |\n|---|---:|---:|\n"
            "| Mini Grinder | 424 | $36.05–$53 |\n\n"
            "## 渠道证据\n该商品广告GMV占比为59%，关联达人406名。\n\n"
            "## 执行建议\n首批500-1000件，测试预算$2000，配合3-5个达人，观察2周，"
            "目标ROI达到2.5，MOQ建议不超过500，建议定价$29.99-$39.99。"
            "建议售价 **$13–$16**；以$14.99在TikTok Shop上架；邀请3–5个达人；"
            "预计1–2个月做到月销1,000件；如果按$5–$8广告成本计算ROI。"
        )
        edited = web_app.verify_fastmoss_final_answer(
            draft, message, "调研", route, FakeRequests, "key", "https://example.test/v1", "model"
        )
        assert "本轮样本显示该细分仍需补充竞争强度证据" in edited
        assert "## 核心判断" in edited and "## 代表商品" in edited and "## 渠道证据" in edited
        assert "| Mini Grinder | 424 | $36.05–$53 |" in edited
        assert "广告GMV占比为59%" in edited and "关联达人406名" in edited
        for unsupported in (
            "500-1000", "$2000", "3-5", "3–5", "2周", "2.5", "MOQ建议不超过500",
            "$29.99", "$39.99", "$13–$16", "$14.99", "1–2个月", "月销1,000", "$5–$8",
        ):
            assert unsupported not in edited

        downgraded = web_app.polish_fastmoss_report_tone(
            "该细分不存在独立市场，但它是真实细分机会。"
        )
        assert "尚未显示出足以确认独立市场的强信号" in downgraded
        assert "是否构成可进入机会仍需验证" in downgraded

        softened = web_app.polish_fastmoss_report_tone(
            "首批500-1000件，配合3-5个达人，筛选10k-50k粉的创作者，观察2周，建议定价$29.99-$39.99。"
        )
        assert "500" not in softened and "3-5" not in softened
        assert "10k" not in softened and "2周" not in softened
        assert "$29.99" not in softened and "$39.99" not in softened
        assert "小批量" in softened and "少量匹配达人" in softened
        assert "受众匹配" in softened and "完整测试周期" in softened

        class ApprovedResponse(FakeResponse):
            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "approved": True, "risk_level": "low", "edits": [],
                })}, "finish_reason": "stop"}]}

        class ApprovedRequests:
            @staticmethod
            def post(*_args, **_kwargs):
                return ApprovedResponse()

        approved = web_app.verify_fastmoss_final_answer(
            native_report, message, "调研", route, ApprovedRequests, "key", "https://example.test/v1", "model"
        )
        assert approved == native_report

        class InvalidJsonResponse(FakeResponse):
            def json(self):
                return {"choices": [{"message": {"content": "{invalid"}, "finish_reason": "stop"}]}

        class InvalidJsonRequests:
            @staticmethod
            def post(*_args, **_kwargs):
                return InvalidJsonResponse()

        class LengthResponse(FakeResponse):
            finish_reason = "length"

        class LengthRequests:
            @staticmethod
            def post(*_args, **_kwargs):
                return LengthResponse()

        class MissingTargetResponse(FakeResponse):
            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "approved": False, "risk_level": "high", "edits": [{
                        "original": "草稿里不存在的句子", "replacement": "", "reason": "风险",
                        "evidence_refs": ["fastmoss__product_overview"],
                    }],
                })}, "finish_reason": "stop"}]}

        class MissingTargetRequests:
            @staticmethod
            def post(*_args, **_kwargs):
                return MissingTargetResponse()

        class TimeoutRequests:
            @staticmethod
            def post(*_args, **_kwargs):
                raise TimeoutError("verifier timeout")

        for failing_requests in (InvalidJsonRequests, LengthRequests, MissingTargetRequests, TimeoutRequests):
            kept = web_app.verify_fastmoss_final_answer(
                draft, message, "调研", route, failing_requests, "key", "https://example.test/v1", "model"
            )
            assert "## 核心判断" in kept and "## 代表商品" in kept and "## 渠道证据" in kept
            assert "| Mini Grinder | 424 | $36.05–$53 |" in kept
            assert "这个细分已经被证明是低竞争市场" in kept
            assert "## 先说结论" not in kept
            assert "500-1000" not in kept and "$2000" not in kept and "$29.99" not in kept
            assert "$13–$16" not in kept and "$14.99" not in kept and "3–5" not in kept
            assert "1–2个月" not in kept and "$5–$8" not in kept

        system_only = SimpleNamespace(tool_calls=[], tool_results=[{
            "tool_name": "system__current_time",
            "result": {"ok": True, "data_state": "data", "evidence_observed": True, "summary": "2026-07-17"},
        }])

        class MustNotCallRequests:
            @staticmethod
            def post(*_args, **_kwargs):
                raise AssertionError("verifier must not run without FastMoss evidence")

        no_evidence = web_app.verify_fastmoss_final_answer(
            native_report, system_only, "调研", route, MustNotCallRequests, "key", "https://example.test/v1", "model"
        )
        assert "## 先说结论" in no_evidence
        system_manifest = web_app.fastmoss_evidence_manifest(system_only, "调研", route)
        assert system_manifest["quality_states"]["data"] == []
        assert system_manifest["evidence_fact_count"] == 0
    finally:
        web_app.record_api_call = original_record


def test_fastmoss_all_workflow_tools_emit_supported_envelopes() -> None:
    generic_payload = {
        "summary_metrics": {"gmv": 100, "units_sold": 5},
        "list": [{"entity_id": "sample", "gmv": 100}],
        "total": 1,
    }
    for tool_name in sorted(web_app.FASTMOSS_SUPPORTED_EVIDENCE_TOOLS):
        arguments = {
            "filter": {
                "category_id": 935176,
                "product_id": "1730898744848192092",
                "shop_id": "7496115528088652380",
                "creator_uid": "7025465585010885637",
                "video_id": "7661483798802009357",
            }
        }
        payload = generic_payload
        if tool_name == "search_category_by_words":
            payload = {"categories": [{"category_id_level3": 935176, "cn_name": "料理机"}]}
        elif tool_name == "product_creator_analysis":
            payload = {
                "creator_summary": {"follower_tier_distribution": [{"follower_tier": "10k-50k", "creator_count": 7}]},
                "linked_creators": {"list": [], "total": 7},
            }
        elif tool_name == "product_video_list":
            payload = {"product_id": "1730898744848192092", "time_range_days": 28, "videos": [], "total": 16}
        normalized = {
            "ok": True, "enough_data": True, "data_state": "data", "mcp_data": payload,
        }
        annotated = web_app.annotate_fastmoss_tool_result(
            f"fastmoss__{tool_name}", arguments, normalized
        )
        envelope = annotated["evidence_envelope"]
        assert envelope["parser_status"] == "supported", tool_name
        assert envelope["tool_family"] in web_app.FASTMOSS_EVIDENCE_TOOL_FAMILIES, tool_name
        assert annotated.get("evidence_facts"), tool_name

    empty = web_app.annotate_fastmoss_tool_result(
        "fastmoss__product_search", {"keywords": "empty"},
        {"ok": True, "enough_data": False, "data_state": "empty", "mcp_data": {"list": [], "total": 0}},
    )
    assert empty["evidence_envelope"]["data_state"] == "empty"
    assert "evidence_facts" not in empty

    unknown = web_app.annotate_fastmoss_tool_result(
        "fastmoss__future_metric", {},
        {"ok": True, "enough_data": True, "data_state": "data", "mcp_data": {"value": 1}},
    )
    assert unknown["evidence_envelope"]["parser_status"] == "unsupported_parser"
    assert "evidence_facts" not in unknown


def test_fastmoss_creator_video_facts_and_historical_rebuild() -> None:
    creator_payload = {
        "creator_summary": {
            "follower_tier_distribution": [
                {"follower_tier": "1k-5k", "creator_count": 3},
                {"follower_tier": "10k-50k", "creator_count": 7},
            ],
        },
        "linked_creators": {
            "total": 10,
            "list": [{
                "creator": {"creator_uid": "7025465585010885637", "creator_name": "A", "follower_count": 12000},
                "product_contribution": {"product_gmv": 500, "product_units_sold": 10},
            }],
        },
    }
    video_payload = {
        "product_id": "1730898744848192092", "time_range_days": 28, "total": 16,
        "videos": [{
            "video_id": "7661483798802009357",
            "creator": {"creator_uid": "7025465585010885637"},
            "engagement_metrics": {"play_count": 1124},
            "product_contribution": {"window_gmv": 304, "window_units_sold": 4},
            "traffic_flags": {"is_ad": True},
        }],
    }
    calls = [
        {"function": {"name": "fastmoss__product_creator_analysis", "arguments": json.dumps({"filter": {"product_id": "1730898744848192092"}})}},
        {"function": {"name": "fastmoss__product_video_list", "arguments": json.dumps({"filter": {"product_id": "1730898744848192092", "time_range_days": 28}})}},
    ]
    results = [
        {"tool_name": "fastmoss__product_creator_analysis", "result": {"ok": True, "enough_data": True, "data_state": "data", "mcp_data": creator_payload}},
        {"tool_name": "fastmoss__product_video_list", "result": {"ok": True, "enough_data": True, "data_state": "data", "mcp_data": video_payload}},
    ]
    manifest = web_app.fastmoss_evidence_manifest(
        SimpleNamespace(tool_calls=calls, tool_results=results), "调研", {"playbook": "product"}
    )
    assert manifest["evidence_envelope_count"] == 2
    assert manifest["unsupported_parser_count"] == 0
    by_dimension = {fact["dimension"]: fact for fact in manifest["evidence_facts"]}
    assert by_dimension["product_creator_analysis"]["reported_creator_total"] == 10
    assert "not_gmv_contribution" in by_dimension["product_creator_analysis"]["metric_semantics"]["follower_tier_distribution"]
    assert by_dimension["product_videos"]["reported_video_total"] == 16
    assert "not_category_content_preference" in by_dimension["product_videos"]["metric_semantics"]
    product_bundle = next(
        item for item in manifest["entity_bundles"]
        if item["entity_type"] == "product" and item["entity_id"] == "1730898744848192092"
    )
    assert {"product_creator_analysis", "product_videos"}.issubset(set(product_bundle["dimensions"]))


def test_fastmoss_report_packets_are_workflow_native_and_numeric_policy_is_strict() -> None:
    manifest = {
        "quality_states": {"data": ["fastmoss__product_search"], "empty": [], "error": []},
        "evidence_envelope_count": 1,
        "evidence_fact_count": 1,
        "unsupported_parser_count": 0,
        "category_head": {"target_pages": 3, "completed_pages": [1], "reported_total": 20, "fetched_unique": 10, "coverage_complete": False},
        "segment_head": {"queries": {}, "fetched_unique": 0},
        "entity_bundles": [], "evidence_facts": [], "derived_facts": [], "conflicts": [], "limitations": [],
    }
    packets = {
        playbook: web_app.fastmoss_report_packet(manifest, {"playbook": playbook})
        for playbook in ("product", "pricing", "competitor", "shop", "creator", "content_dissect", "content_strategy")
    }
    assert packets["shop"]["suggested_modules"] != packets["product"]["suggested_modules"]
    assert packets["creator"]["suggested_modules"] != packets["content_dissect"]["suggested_modules"]
    assert all(packet["numeric_policy"] == "only_tool_evidence_user_input_or_explicit_calculation" for packet in packets.values())


def test_fastmoss_packet_synthesis_isolated_from_tool_protocol_history() -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "# 结论\n\n证据包已独立合成。"},
                }],
            }

    class Requests:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def post(self, _url: str, **kwargs):
            self.payloads.append(json.loads(kwargs["data"].decode("utf-8")))
            return Response()

    requests = Requests()
    original_verify = web_app.verify_fastmoss_final_answer
    web_app.verify_fastmoss_final_answer = lambda draft, *_args, **_kwargs: draft
    try:
        result = web_app.synthesize_fastmoss_report_from_packet(
            SimpleNamespace(tool_calls=[], tool_results=[]),
            "做一份完整产品调研",
            {"playbook": "product", "task_depth": "workflow"},
            requests,
            "test-key",
            "https://example.invalid/v1",
            "test-model",
        )
    finally:
        web_app.verify_fastmoss_final_answer = original_verify
    assert result.startswith("# 结论")
    assert len(requests.payloads) == 1
    payload = requests.payloads[0]
    assert "tools" not in payload
    assert len(payload["messages"]) == 2
    assert "report_packet" in payload["messages"][0]["content"]
    assert "无法执行的工具协议" not in payload["messages"][0]["content"]


def test_fastmoss_claim_ids_and_extended_mechanical_cleanup() -> None:
    draft = (
        "## 建议\n"
        "建议月度广告预算 $3K–$5K，首批联系20位达人，每周发布5–7条视频，"
        "CPO控制在$15–$18，测试周期2周，建议功率300–500W，售价定在$39.99。\n"
        "其余1970个商品合计不足25%，所以广告ROI稳定。"
    )
    cleaned, count = web_app.sanitize_fastmoss_unsupported_recommendations(draft)
    cleaned = web_app.downgrade_fastmoss_absolute_market_claims(cleaned)
    assert count >= 6
    for unsupported in ("$3K", "$5K", "20位", "5–7", "$15", "$18", "2周", "300–500W", "$39.99", "1970", "25%", "ROI稳定"):
        assert unsupported not in cleaned

    claims = web_app.fastmoss_high_risk_claims("结论\n这个市场已经是低竞争蓝海。")
    assert claims and claims[0]["claim_id"] == "line-2"
    edited, applied = web_app.apply_fastmoss_verifier_edits(
        "结论\n这个市场已经是低竞争蓝海。",
        [{"claim_id": "line-2", "replacement": "竞争强度仍需验证。", "reason": "样本不足", "evidence_refs": []}],
        claims,
    )
    assert applied == 1 and "竞争强度仍需验证" in edited


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
    test_current_tool_evidence_is_lossless_until_budget_pressure()
    test_product_availability_is_a_shallow_lookup()
    test_intent_decision_validation_and_fallback()
    test_intent_router_uses_recent_context_and_falls_back_on_failure()
    test_empty_mcp_collections_are_not_enough_data()
    test_mcp_content_error_rules_are_provider_specific()
    test_fastmoss_zero_analysis_metadata_is_empty_without_affecting_sellersprite()
    test_mcp_sql_error_text_is_not_evidence()
    test_deepseek_tool_turn_preserves_reasoning_content()
    test_dynamic_chat_context_compresses_to_budget()
    test_tool_limit_final_context_removes_protocol_and_detects_dsml()
    test_tool_limit_keeps_large_current_collection_when_capacity_allows()
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
    test_fastmoss_dual_ranking_plan_uses_three_sorted_category_pages_then_segments()
    test_fastmoss_product_phase_waits_for_all_category_and_segment_searches()
    test_fastmoss_product_workflow_deterministically_advances_and_binds_two_targets()
    test_fastmoss_product_search_defaults_force_category_pages_and_short_segment_queries()
    test_fastmoss_evidence_manifest_separates_total_fetched_overlap_and_conflicts()
    test_fastmoss_deterministic_fallback_contains_analysis_and_prioritized_advice()
    test_fastmoss_l2_market_metrics_are_only_upstream_category_reference()
    test_fastmoss_failed_category_page_is_not_counted_as_coverage()
    test_fastmoss_metadata_detects_unsorted_page_and_string_product_ids()
    test_fastmoss_evidence_ledger_keeps_native_dimensions_and_late_trends()
    test_fastmoss_answer_verifier_applies_local_edits_and_keeps_draft_on_failure()
    test_fastmoss_all_workflow_tools_emit_supported_envelopes()
    test_fastmoss_creator_video_facts_and_historical_rebuild()
    test_fastmoss_report_packets_are_workflow_native_and_numeric_policy_is_strict()
    test_fastmoss_packet_synthesis_isolated_from_tool_protocol_history()
    test_fastmoss_claim_ids_and_extended_mechanical_cleanup()
    print("chat tool normalization tests passed")
