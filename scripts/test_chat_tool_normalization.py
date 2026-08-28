#!/usr/bin/env python3
"""Smoke tests for chat tool result normalization."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
# Active v2 chat contracts: Home/SociaVault, SellerSprite and Chuhaijiang.

import web_app  # noqa: E402
from sellersprite_official_skill import (  # noqa: E402
    OFFICIAL_SELLERSPRITE_PROMPT_FILES,
    OFFICIAL_SELLERSPRITE_SKILL_ROOT,
    clear_official_sellersprite_skill_memory_cache,
    load_official_sellersprite_skill_prompt,
    select_official_sellersprite_skill_prompt,
)
from sellersprite_evidence_renderer import (  # noqa: E402
    SELLERSPRITE_RENDER_SPECS,
    SELLERSPRITE_TOOL_SEMANTICS,
    SELLERSPRITE_TOOL_TITLES,
    render_sellersprite_current_evidence,
    sellersprite_semantic_registry_diagnostics,
)
from social_tool_router import SOCIAVAULT_OFFICIAL_TOOL_NAMES  # noqa: E402
from web_app import build_chat_history_context, build_deepseek_tool_assistant_message, build_prefixed_model_tools, build_tool_limit_final_context, chat_markdown_to_html, chat_request_needs_tools, chat_routing_text, compact_chat_tool_evidence, deepseek_tool_protocol_present, estimate_chat_context_tokens, filter_locked_provider_tool_ids, is_chat_retry_request, manage_chat_context, normalize_mcp_tool_arguments, normalize_prefixed_tool_result, normalize_tool_result, parse_chat_intent_decision, provider_default_enabled_tool_ids, provider_forces_mcp_tools, resolve_chat_intent, route_chat_intent  # noqa: E402
from tools import _filter_relevant_search_results, execute_tool, parse_bing_html, parse_duckduckgo_html  # noqa: E402




def test_sellersprite_official_skill_chain_loads_full_bundle_and_isolates_tools() -> None:
    assert len(OFFICIAL_SELLERSPRITE_PROMPT_FILES) == 30
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for index, relative_name in enumerate(OFFICIAL_SELLERSPRITE_PROMPT_FILES):
            content = f"sellersprite-official-{index}-{relative_name}\n".encode("utf-8")
            info = tarfile.TarInfo(OFFICIAL_SELLERSPRITE_SKILL_ROOT + relative_name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    payload = archive_buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    clear_official_sellersprite_skill_memory_cache()
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_dir = Path(temp_dir)
        prompt = load_official_sellersprite_skill_prompt(
            cache_dir=cache_dir,
            archive_payload=payload,
            expected_sha256=digest,
        )
        clear_official_sellersprite_skill_memory_cache()
        cached_prompt = load_official_sellersprite_skill_prompt(
            cache_dir=cache_dir,
            expected_sha256=digest,
        )
    assert prompt == cached_prompt
    for relative_name in OFFICIAL_SELLERSPRITE_PROMPT_FILES:
        assert f"官方文件：{relative_name}" in prompt
    product_research_prompt = select_official_sellersprite_skill_prompt(
        prompt,
        "comprehensive/product-research.md",
    )
    assert "官方文件：comprehensive/product-research.md" in product_research_prompt
    assert "官方文件：comprehensive/market-analysis.md" not in product_research_prompt
    assert len(product_research_prompt) < len(prompt)

    route = web_app.sellersprite_official_skill_route("/keyword-research flying toys")
    assert route["official_skill_chain"] is True
    assert route["official_skill_provider"] == "sellersprite"
    assert route["route_source"] == "official_skill"
    assert route["dynamic_planner"] is False
    assert route["intent"] == "sellersprite_official_skill"
    assert route["task_depth"] == "direct"
    assert web_app.chat_route_uses_report_model("amazon", route) is False
    assert len(web_app.SELLERSPRITE_OFFICIAL_PRESETS) == 27
    for pid, pinfo in web_app.SELLERSPRITE_OFFICIAL_PRESETS.items():
        assert pinfo["skill_file"] in OFFICIAL_SELLERSPRITE_PROMPT_FILES
        assert len(pinfo["tools"]) > 0
        assert all(t.startswith("sellersprite__") for t in pinfo["tools"])
        r_by_id = web_app.sellersprite_official_skill_route("test query", pid)
        assert r_by_id["route_source"] == "official_preset"
        assert r_by_id["official_preset_id"] == pid
        assert r_by_id["official_skill_file"] == pinfo["skill_file"]
        assert set(r_by_id["tools"]) == set(pinfo["tools"])

        text_prompt = f"请使用卖家精灵官方 Skill「{pinfo['label']}」开始分析。\n\n目标：测试商品"
        r_by_label = web_app.sellersprite_official_skill_route(text_prompt)
        assert r_by_label["route_source"] == "official_preset"
        assert r_by_label["official_preset_id"] == pid

    preset_route = web_app.sellersprite_official_skill_route(
        "请使用卖家精灵官方 Skill「智能选品助手」开始分析。\n\n目标：解压玩具"
    )
    assert preset_route["route_source"] == "official_preset"
    assert preset_route["official_preset_id"] == "comprehensive/product-research"
    assert preset_route["official_skill_file"] == "comprehensive/product-research.md"
    assert set(preset_route["tools"]) == set(
        web_app.SELLERSPRITE_PRODUCT_RESEARCH_TOOL_IDS
    )
    assert {
        web_app.split_prefixed_tool_id(tool_id)[1]
        for tool_id in preset_route["tools"]
    } <= set(SELLERSPRITE_TOOL_SEMANTICS)
    explicit_preset_route = web_app.sellersprite_official_skill_route(
        "解压玩具",
        web_app.SELLERSPRITE_PRODUCT_RESEARCH_PRESET_ID,
    )
    assert explicit_preset_route == preset_route
    allowlisted_tool_id = next(iter(preset_route["tools"]))
    allowlisted_tool_name = web_app.split_prefixed_tool_id(allowlisted_tool_id)[1]
    bridge_tools = [{
        "name": allowlisted_tool_name,
        "description": "isolated SellerSprite test tool",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    # 静态官方 Skill 契约不应访问真实 MCP bridge 或依赖本机凭据。
    original_list_tools = web_app.list_mcp_bridge_tools
    web_app.list_mcp_bridge_tools = lambda chat_type: (
        bridge_tools if chat_type == "sellersprite" else []
    )
    try:
        default_tool_ids = web_app.provider_default_enabled_tool_ids("amazon")
    finally:
        web_app.list_mcp_bridge_tools = original_list_tools
    assert allowlisted_tool_id in default_tool_ids
    assert web_app.sellersprite_official_skill_tool_ids(
        {allowlisted_tool_id, "unregistered__tool"},
        preset_route["tools"],
    ) == {allowlisted_tool_id}
    unknown_preset_route = web_app.sellersprite_official_skill_route(
        "解压玩具", "unknown-preset"
    )
    assert unknown_preset_route["route_source"] == "invalid_preset"
    assert unknown_preset_route["tools"] == []
    assert unknown_preset_route["invalid_preset"] == "unknown-preset"
    assert "official_preset_id" not in unknown_preset_route
    assert web_app.chat_route_uses_report_model("amazon", route) is False

    # Test generic mock payload generation and interception
    mock_payload = web_app.generate_generic_mock_tool_payload("sellersprite", "product_research", {"asin": "B08TEST001"})
    assert mock_payload["isError"] is False
    parsed_mock = json.loads(mock_payload["content"][0]["text"])
    assert parsed_mock["code"] == 200
    assert parsed_mock["msg"] == "success"
    assert parsed_mock["is_test_mock"] is True
    assert "[测试拦截/模拟发送]" in parsed_mock["notice"]
    assert parsed_mock["data"]["items"][0]["asin"] == "B08TEST001"

    previous_mock_mode = os.environ.get("SELLERSPRITE_TOOL_MOCK_MODE")
    os.environ["SELLERSPRITE_TOOL_MOCK_MODE"] = "1"
    try:
        intercepted_exec = web_app.execute_prefixed_tool(
            "sellersprite__product_research", {"asin": "B08TEST001"}
        )
    finally:
        if previous_mock_mode is None:
            os.environ.pop("SELLERSPRITE_TOOL_MOCK_MODE", None)
        else:
            os.environ["SELLERSPRITE_TOOL_MOCK_MODE"] = previous_mock_mode
    assert intercepted_exec["ok"] is True
    assert "data" in intercepted_exec

    clear_official_sellersprite_skill_memory_cache()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            load_official_sellersprite_skill_prompt(
                cache_dir=Path(temp_dir),
                archive_payload=payload,
                expected_sha256="0" * 64,
            )
    except RuntimeError as exc:
        assert "integrity verification failed" in str(exc)
    else:
        raise AssertionError("invalid official SellerSprite archive digest must fail")

    incomplete_buffer = io.BytesIO()
    with tarfile.open(fileobj=incomplete_buffer, mode="w:gz") as archive:
        content = b"only-one-file\n"
        info = tarfile.TarInfo(OFFICIAL_SELLERSPRITE_SKILL_ROOT + "SKILL.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    incomplete_payload = incomplete_buffer.getvalue()
    clear_official_sellersprite_skill_memory_cache()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            load_official_sellersprite_skill_prompt(
                cache_dir=Path(temp_dir),
                archive_payload=incomplete_payload,
                expected_sha256=hashlib.sha256(incomplete_payload).hexdigest(),
            )
    except RuntimeError as exc:
        assert "missing required files" in str(exc)
    else:
        raise AssertionError("incomplete official SellerSprite bundle must fail")

    unsafe_buffer = io.BytesIO()
    with tarfile.open(fileobj=unsafe_buffer, mode="w:gz") as archive:
        content = b"unsafe\n"
        info = tarfile.TarInfo("../unsafe.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    unsafe_payload = unsafe_buffer.getvalue()
    clear_official_sellersprite_skill_memory_cache()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            load_official_sellersprite_skill_prompt(
                cache_dir=Path(temp_dir),
                archive_payload=unsafe_payload,
                expected_sha256=hashlib.sha256(unsafe_payload).hexdigest(),
            )
    except RuntimeError as exc:
        assert "Unsafe path" in str(exc)
    else:
        raise AssertionError("unsafe official SellerSprite archive path must fail")


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

    model_tool_names = {
        item["function"]["name"]
        for item in web_app.build_prefixed_model_tools({"system__current_time"})
    }
    assert "system__current_time" in model_tool_names

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


def test_active_provider_domains_and_unknown_tool_domain_are_fail_closed() -> None:
    assert web_app.CHAT_PROVIDERS == {"home", "amazon", "chuhaijiang"}
    assert set(web_app.CHAT_TOOL_DOMAINS) == {
        "system", "function", "sociavault", "sellersprite", "chuhaijiang",
    }
    unknown = web_app.execute_prefixed_tool("unregistered__tool", {})
    assert unknown["ok"] is False
    assert "Unknown tool domain" in unknown["error"]


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










def _model_tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}








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

    model_tool_names = {
        item["function"]["name"]
        for item in web_app.build_prefixed_model_tools({"system__web_search"})
    }
    assert "system__web_search" in model_tool_names

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
    assert isinstance(normalized["mcp_data"], dict)
    assert marker in json.dumps(normalized["mcp_data"], ensure_ascii=False)
    assert len(normalized["mcp_text_preview"]) == 4000
    assert marker not in normalized["mcp_text_preview"]

    evidence = web_app.current_chat_tool_evidence(
        "sellersprite__keyword_research_trends",
        normalized,
        {"marketplace": "US", "keyword": "air pump"},
        raw_result,
    )
    assert marker not in evidence
    assert "站点" in evidence and "美国站" in evidence
    assert "marketplace" not in evidence
    assert "本次实际返回 30 条记录" in evidence
    assert "keyword-20" in evidence
    assert "2000" in evidence
    assert "description" not in evidence
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
    assert stats["compressed"] is False
    history_content = next(message["content"] for message in request_messages if message.get("role") == "assistant")
    assert len(history_content) == 30004

















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
                {
                    "ok": True,
                    "mcp_data": {
                        "trend_series": [{
                            "date": f"2026-06-{index + 1:02d}",
                            "value": ("x" * 8200) + marker,
                        }]
                    },
                },
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














def test_three_layer_research_task_keeps_real_product_entity() -> None:
    route = web_app.attach_research_task(
        {"intent": "product_research", "task_depth": "analysis", "entity": "Electric Baby Nail Trimmer"},
        "amazon",
        "Electric Baby Nail Trimmer 分析一下亚马逊这个产品情况",
    )
    task = route["research_task"]
    assert task["objective"] == "entity_analysis"
    assert task["scope"] == "keyword"
    assert task["entity_type"] == "keyword"
    assert task["entity"] == "Electric Baby Nail Trimmer"
    asin_route = web_app.attach_research_task(
        {"intent": "product_research", "task_depth": "analysis", "entity": "B0H3ZH8BF8"},
        "amazon",
        "B0H3ZH8BF8 分析一下",
    )
    assert asin_route["research_task"]["entity_type"] == "asin"
    assert asin_route["research_task"]["scope"] == "entity"


















def test_sellersprite_semantic_registry_is_complete_and_lossless() -> None:
    assert len(SELLERSPRITE_TOOL_SEMANTICS) == 43
    assert set(SELLERSPRITE_TOOL_TITLES) == set(SELLERSPRITE_TOOL_SEMANTICS)
    assert set(SELLERSPRITE_RENDER_SPECS) == set(SELLERSPRITE_TOOL_SEMANTICS)
    diagnostics = sellersprite_semantic_registry_diagnostics(
        [{"name": name} for name in SELLERSPRITE_TOOL_SEMANTICS]
    )
    assert diagnostics == {
        "registered": 43,
        "runtime": 43,
        "missing_semantics": [],
        "missing_runtime": [],
    }
    for name, semantic in SELLERSPRITE_TOOL_SEMANTICS.items():
        assert not re.search(r"[A-Za-z]", SELLERSPRITE_TOOL_TITLES[name])
        sample = {
            "asin": "B0ABCDEF12",
            "keyword": "stroller fan",
            "keyword_jp": "ベビーカーファン",
            "date": "202606",
            "availableDate": 1773187200000,
            "currency": "USD",
            "fulfillment": "FBA",
            "searches": 12345,
            "futureBusinessField": f"kept-{name}",
        }
        business_data = sample if name == "traffic_listing_stat" else {"items": [sample]}
        result = render_sellersprite_current_evidence({
            "tool": f"sellersprite__{name}",
            "arguments": {
                "marketplace": "US",
                "date_type": "month",
                "date_value": "2026-06",
            },
            "ok": True,
            "data_state": "data",
            "data": business_data,
        })
        assert result.profile == semantic.profile
        assert result.fallback is False
        assert result.business_leaf_paths == (
            result.consumed_paths | result.unmapped_paths | result.excluded_paths
        )
        assert not (result.consumed_paths & result.unmapped_paths)
        assert not result.unmapped_paths
        assert f"kept-{name}" not in result.markdown
        future_paths = [
            path for path in result.excluded_paths
            if "futureBusinessField" in path
        ]
        assert future_paths
        assert all(
            "自然语言字段契约" in result.exclusion_reasons[path]
            for path in future_paths
        )
        assert "未映射业务字段" not in result.markdown
        assert "JSON路径" not in result.markdown
        assert "原字段" not in result.markdown
        assert "$.business_data" not in result.markdown
        assert f"sellersprite__{name}" not in result.markdown
        assert "current-call" not in result.markdown
        assert "ベビーカーファン" not in result.markdown
        assert "2026年6月" in result.markdown
        assert "2026年3月11日" in result.markdown
        assert "美元" in result.markdown
        assert "亚马逊物流配送" in result.markdown
        for token in ("2026-06", "202606", "month", "USD", "FBA", "Semantic"):
            assert token not in result.markdown, (name, token)

    empty = render_sellersprite_current_evidence({
        "tool": "sellersprite__review",
        "arguments": {"marketplace": "US", "asin": "B0ABCDEF12"},
        "ok": True,
        "data_state": "empty",
        "data": {"items": [], "total": 0},
    })
    assert empty.empty is True
    assert "本次调用成功" in empty.markdown
    assert "没有返回业务记录" in empty.markdown

    wrapped_empty = render_sellersprite_current_evidence({
        "tool": "sellersprite__product_node",
        "arguments": {"request": {"marketplace": "US", "keyword": "wifi extender"}},
        "ok": True,
        "data_state": "empty",
        "data": {"code": "OK", "message": "成功", "data": []},
    })
    assert wrapped_empty.empty is True
    assert wrapped_empty.business_leaf_paths == set()
    assert "没有返回业务记录" in wrapped_empty.markdown

    unknown = render_sellersprite_current_evidence({
        "tool": "sellersprite__future_tool",
        "arguments": {"marketplace": "US"},
        "ok": True,
        "data_state": "data",
        "data": {"items": [{"secretFutureField": "AUDIT-ONLY-MARKER"}]},
    })
    assert unknown.fallback is True
    assert unknown.business_leaf_paths == unknown.excluded_paths
    assert "AUDIT-ONLY-MARKER" not in unknown.markdown
    assert "future_tool" not in unknown.markdown
    assert "仅保留在审计证据" in unknown.markdown


def test_semantic_brace_residue_is_naturalized_or_preserved_and_logged() -> None:
    import contextlib
    import io

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        naturalized, success_count, failure_count = (
            web_app._naturalize_and_log_semantic_braces(
                "sellersprite",
                '正常 {"createdTime": 1715084401000, "nested": {"value": true}} '
                "异常 {not valid} 结尾",
            )
        )
    logged = output.getvalue()
    assert success_count == 1
    assert failure_count == 1
    assert "{创建时间：2024年5月7日" in naturalized
    assert "值：是" in naturalized
    assert "{not valid}" in naturalized
    assert logged.count("[CHAT SEMANTIC BRACE RESIDUE]") == 2
    assert "provider=sellersprite" in logged
    assert "status=naturalized" in logged
    assert "status=unchanged" in logged
    assert '\\"createdTime\\": 1715084401000' in logged
    assert "after=" in logged
    assert 'content="not valid"' in logged


def test_semantic_brace_residue_python_mapping_is_naturalized() -> None:
    import contextlib
    import io

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        naturalized, success_count, failure_count = (
            web_app._naturalize_and_log_semantic_braces(
                "sellersprite",
                "标题 {'asin': 'B0CZT19JQ1', 'createdTime': 1715084401000}",
            )
        )
    assert success_count == 1
    assert failure_count == 0
    assert "{亚马逊商品编号：B0CZT19JQ1；创建时间：2024年5月7日" in naturalized
    assert "1715084401000" not in naturalized












def test_all_sites_disable_frontend_tool_selection() -> None:
    chat_html = (ROOT / "scripts" / "static" / "chat.html").read_text(encoding="utf-8")
    ui_system_js = (ROOT / "scripts" / "static" / "assets" / "ui-system.js").read_text(encoding="utf-8")
    assert "headerToolBtn" not in chat_html
    assert "toolModal" not in chat_html
    assert 'id="toolBtn"' not in chat_html
    assert "enhanceToolTree" not in ui_system_js
    assert "enabledToolMasks" not in chat_html
    assert "TOOL_SELECTION" not in chat_html




def test_social_platform_routes_use_sociavault_without_rest_fallback() -> None:
    instagram = web_app.route_chat_intent("分析 instagram.com/creator 的最新帖子", "home")
    assert instagram["intent"] == "sociavault_social"
    assert instagram["tool_domain"] == "sociavault"
    assert instagram["tool_prefixes"] == ("instagram_",)

    tiktok = web_app.route_chat_intent("看看 TikTok 最近的热门趋势", "home")
    assert tiktok["intent"] == "sociavault_social"
    assert tiktok["tool_prefixes"] == ("tiktok_",)

    generic = web_app.route_chat_intent("比较几个主流社交媒体平台的热度", "home")
    assert generic["intent"] == "sociavault_social"
    assert generic["tool_domain"] == "sociavault"
    assert "tool_prefixes" not in generic

    handler_source = (ROOT / "scripts" / "web_app.py").read_text(encoding="utf-8")
    assert 'enabled_tool_ids = None' in handler_source
    assert "ignored legacy tool masks" in handler_source
    assert "decode_tool_masks(enabled_masks)" not in handler_source


def test_social_router_inherits_platform_before_legacy_intent_model() -> None:
    messages = [
        SimpleNamespace(id="u1", role="user", content="看看 YouTube 频道", tool_calls=[]),
        SimpleNamespace(
            id="a1",
            role="assistant",
            content="",
            tool_calls=[{"function": {"name": "sociavault__youtube_channel"}}],
        ),
        SimpleNamespace(id="u2", role="user", content="再看看评论", tool_calls=[]),
    ]

    class Requests:
        def post(self, *_args, **_kwargs):
            raise AssertionError("confirmed follow-up must not call an intent model")

    intent = web_app.resolve_chat_intent(
        messages,
        "再看看评论",
        "home",
        "key",
        "https://example.test/v1",
        "model",
        Requests(),
    )
    assert intent["intent"] == "sociavault_social"

    route = web_app.resolve_sociavault_tool_route(
        messages,
        "",
        "再看看评论",
        list(SOCIAVAULT_OFFICIAL_TOOL_NAMES),
        "key",
        "https://example.test/v1",
        "model",
        Requests(),
    )
    assert route.source == "rules"
    assert route.platforms == ("youtube",)
    assert "youtube_video_comments" in route.candidate_tools
    assert not any(name.startswith("tiktok_") for name in route.candidate_tools)


def test_social_router_model_failures_fall_back_to_full_catalog() -> None:
    class TimeoutRequests:
        def post(self, *_args, **kwargs):
            assert kwargs["timeout"] == 3
            raise TimeoutError("controlled timeout")

    timeout_route = web_app.resolve_sociavault_tool_route(
        [],
        "",
        "比较几个主流社交媒体平台的热度",
        list(SOCIAVAULT_OFFICIAL_TOOL_NAMES),
        "key",
        "https://example.test/v1",
        "model",
        TimeoutRequests(),
    )
    assert timeout_route.source == "fallback_all"
    assert timeout_route.fallback_reason == "model_TimeoutError"
    assert len(timeout_route.candidate_tools) == 107

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "not-json"}}]}

    class InvalidRequests:
        def post(self, *_args, **kwargs):
            assert kwargs["timeout"] == 3
            return Response()

    original_record = web_app.record_api_call
    web_app.record_api_call = lambda *_args, **_kwargs: None
    try:
        invalid_route = web_app.resolve_sociavault_tool_route(
            [],
            "",
            "比较几个主流社交媒体平台的热度",
            list(SOCIAVAULT_OFFICIAL_TOOL_NAMES),
            "key",
            "https://example.test/v1",
            "model",
            InvalidRequests(),
        )
    finally:
        web_app.record_api_call = original_record
    assert invalid_route.source == "fallback_all"
    assert invalid_route.fallback_reason == "invalid_model_output"
    assert len(invalid_route.candidate_tools) == 107


def test_social_router_does_not_hijack_commerce_intents() -> None:
    for text in (
        "ASIN B0ABCDEF12 的关键词排名",
        "这个商品和竞品相比怎么样",
        "分析商品趋势和评论",
    ):
        assert web_app.route_chat_intent(text, "home")["intent"] != "sociavault_social"


def test_intent_decision_validation_and_fallback() -> None:
    fallback = route_chat_intent("research wireless earbuds", "amazon")
    valid = parse_chat_intent_decision(
        {
            "intent": "product_research",
            "task_depth": "analysis",
            "entity": "wireless earbuds",
            "region": "US",
            "confidence": 0.94,
            "research_task": {
                "objective": "entity_analysis",
                "scope": "keyword",
                "entity_type": "keyword",
                "entity": "wireless earbuds",
                "entity_source": "explicit",
                "region": "US",
                "time_window": "recent month",
            },
        },
        fallback, "amazon", "research wireless earbuds",
    )
    assert valid["intent"] == "product_research"
    assert valid["route_source"] == "llm"
    invalid = parse_chat_intent_decision(
        {"intent": "unknown", "task_depth": "lookup", "confidence": 1},
        fallback, "amazon", "research wireless earbuds",
    )
    assert invalid["route_source"] == "rules_fallback"


def test_intent_router_uses_recent_context_and_falls_back_on_failure() -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": json.dumps({
                "intent": "product_research", "task_depth": "analysis", "entity": "stroller fan", "region": "US", "confidence": 0.96,
                "research_task": {"objective": "entity_analysis", "scope": "keyword", "entity_type": "keyword", "entity": "stroller fan", "entity_source": "explicit", "region": "US", "time_window": "recent month"},
            })}}]}

    class Requests:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.payload = None

        def post(self, _url: str, **kwargs):
            self.payload = kwargs["json"]
            if self.fail:
                raise TimeoutError("router timeout")
            return Response()

    previous = os.environ.get("CHAT_INTENT_ROUTER_ENABLED")
    os.environ["CHAT_INTENT_ROUTER_ENABLED"] = "1"
    try:
        messages = [SimpleNamespace(role="user", content="stroller fan"), SimpleNamespace(role="user", content="research its sales trend")]
        requests = Requests()
        route = resolve_chat_intent(messages, "research its sales trend", "amazon", "key", "https://example.test/v1", "model", requests)
        assert route["route_source"] == "llm"
        assert "stroller fan" in json.dumps(requests.payload)
        fallback = resolve_chat_intent(messages, "research product", "amazon", "key", "https://example.test/v1", "model", Requests(True))
        assert fallback["route_source"] == "rules_fallback"
    finally:
        if previous is None:
            os.environ.pop("CHAT_INTENT_ROUTER_ENABLED", None)
        else:
            os.environ["CHAT_INTENT_ROUTER_ENABLED"] = previous


def test_empty_mcp_collections_are_not_enough_data() -> None:
    def result(payload: dict) -> dict:
        return {"ok": True, "data": {"content": [{"type": "text", "text": json.dumps(payload)}]}}

    empty = normalize_prefixed_tool_result("sellersprite__market_research", result({"code": "OK", "data": {"items": [], "total": 0}}))
    assert empty["data_state"] == "empty"
    assert empty["enough_data"] is False
    populated = normalize_prefixed_tool_result("sellersprite__market_research", result({"code": "OK", "data": {"items": [{"asin": "B0ABCDEF12"}], "total": 1}}))
    assert populated["data_state"] == "data"
    assert populated["enough_data"] is True
    direct_empty = normalize_prefixed_tool_result("sellersprite__product_node", result({"code": "OK", "data": []}))
    assert direct_empty["data_state"] == "empty"


def test_mcp_content_error_rules_are_provider_specific() -> None:
    def result(payload: dict) -> dict:
        return {"ok": True, "data": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}}

    success = normalize_prefixed_tool_result("sellersprite__asin_detail", result({"code": "OK", "data": {"asin": "B0FBVL7SG7", "price": 26.99}}))
    assert success["ok"] is True
    failure = normalize_prefixed_tool_result("sellersprite__product_research", result({"code": "ERROR_QUERY", "message": "query failed", "data": None}))
    assert failure["ok"] is False
    assert failure["data_state"] == "error"


def test_mcp_sql_error_text_is_not_evidence() -> None:
    raw = {"ok": True, "data": {"content": [{"type": "text", "text": "SQLSTATE[42S22]: Unknown column 'format_price'"}]}}
    normalized = normalize_prefixed_tool_result("sellersprite__asin_detail", raw)
    assert normalized["ok"] is False
    assert normalized["enough_data"] is False
    assert normalized["evidence_observed"] is False


def test_deepseek_tool_turn_preserves_reasoning_content() -> None:
    tool_calls = [{"id": "call_1", "function": {"name": "sellersprite__product_research", "arguments": "{}"}}]
    turn = build_deepseek_tool_assistant_message({"content": "", "reasoning_content": "internal reasoning", "tool_calls": tool_calls}, tool_calls, True)
    assert turn["role"] == "assistant"
    assert turn["tool_calls"] == tool_calls
    assert turn["reasoning_content"] == "internal reasoning"


def test_only_structured_direct_requests_bypass_intent_llm() -> None:
    assert web_app.chat_intent_router_should_call("research wireless earbuds", web_app.route_chat_intent("research wireless earbuds", "amazon")) is True
    assert web_app.chat_intent_router_should_call("analyse B0ABCDEF12", web_app.route_chat_intent("analyse B0ABCDEF12", "amazon")) is False
    assert web_app.chat_intent_router_should_call("what time is it", web_app.route_chat_intent("what time is it", "home")) is False


def test_disabling_intent_router_restores_legacy_rule_route() -> None:
    class NoCallRequests:
        def post(self, *_args, **_kwargs):
            raise AssertionError("intent LLM must not be called when disabled")

    previous = os.environ.get("CHAT_INTENT_ROUTER_ENABLED")
    os.environ["CHAT_INTENT_ROUTER_ENABLED"] = "0"
    try:
        route = web_app.resolve_chat_intent([], "research wireless earbuds", "amazon", "key", "https://example.test/v1", "model", NoCallRequests())
        assert route["route_source"] == "rules"
        assert web_app.llm_orchestrated_route(route) is False
    finally:
        if previous is None:
            os.environ.pop("CHAT_INTENT_ROUTER_ENABLED", None)
        else:
            os.environ["CHAT_INTENT_ROUTER_ENABLED"] = previous


def test_llm_research_task_is_authoritative_for_ambiguous_trend_phrases() -> None:
    text = "find recent trending products"
    decision = {"intent": "product_research", "task_depth": "analysis", "entity": "", "region": "US", "confidence": 0.97, "research_task": {"objective": "trend_discovery", "scope": "cross_category", "entity_type": "none", "entity": "", "entity_source": "none", "region": "US", "time_window": "recent month"}}
    route = web_app.parse_chat_intent_decision(decision, web_app.route_chat_intent(text, "amazon"), "amazon", text)
    assert route["route_source"] == "llm"
    assert route["research_task"]["entity_type"] == "none"
    assert "entity" not in route


def test_invalid_llm_research_task_uses_structured_only_fallback() -> None:
    text = "find recent trending products"
    invalid = {"intent": "product_research", "task_depth": "analysis", "entity": text, "region": "US", "confidence": 0.97, "research_task": {"objective": "trend_discovery", "scope": "cross_category", "entity_type": "none", "entity": text, "entity_source": "explicit", "region": "US", "time_window": "recent month"}}
    route = web_app.parse_chat_intent_decision(invalid, web_app.route_chat_intent(text, "amazon"), "amazon", text)
    assert route["route_source"] == "rules_fallback"
    assert route["research_task"]["entity"] == ""
    exact = web_app.research_task_from("analyse B0ABCDEF12", "amazon", {"route_source": "rules_fallback", "task_depth": "analysis"})
    assert exact["entity_type"] == "asin"


def test_three_layer_research_task_rejects_goal_text_as_entity() -> None:
    text = "find recent trending products"
    decision = {"intent": "product_research", "task_depth": "analysis", "entity": text, "region": "US", "confidence": 0.96, "research_task": {"objective": "trend_discovery", "scope": "cross_category", "entity_type": "none", "entity": text, "entity_source": "explicit", "region": "US", "time_window": "recent month"}}
    route = web_app.parse_chat_intent_decision(decision, web_app.route_chat_intent(text, "amazon"), "amazon", text)
    assert "entity" not in route
    assert route["research_task"]["entity"] == ""


def test_sellersprite_semantic_report_and_pro_synthesis() -> None:
    marker = "SELLERSPRITE-REPORT-MARKER"
    message = SimpleNamespace(tool_calls=[{"id": "c1", "function": {"name": "sellersprite__keyword_research", "arguments": json.dumps({"request": {"marketplace": "US", "keyword": "stroller fan"}})}}], tool_results=[{"tool_name": "sellersprite__keyword_research", "result": {"ok": True, "data_state": "data", "mcp_data": {"items": [{"keyword": "stroller fan", "searches": 12345, "futureBusinessField": marker}]}}}])
    dossier = web_app.sellersprite_report_evidence_dossier(message, {"intent": "product_research", "research_task": {"objective": "opportunity_discovery", "region": "US"}})
    semantic, stats = web_app.sellersprite_render_report_evidence(dossier)
    assert marker in json.dumps(dossier)
    assert marker not in semantic
    assert stats["format"] == "semantic"


def test_dynamic_provider_capability_graph_uses_task_scope_and_evidence() -> None:
    route = web_app.attach_research_task({"intent": "product_research", "task_depth": "analysis"}, "amazon", "find cross category opportunities")
    enabled = {"sellersprite__keyword_research", "sellersprite__market_research", "sellersprite__product_research", "sellersprite__asin_detail"}
    selected = web_app.provider_profile_tool_ids("amazon", route, "find cross category opportunities", enabled, SimpleNamespace(tool_calls=[], tool_results=[]))
    assert selected <= enabled
    assert "sellersprite__keyword_research" in selected


def test_dynamic_provider_planner_does_not_cap_repeated_calls() -> None:
    state = {"attempted_capabilities": [], "observed_capabilities": [], "tool_counts": {"keyword_research": 99, "market_research": 99}, "has_category": False, "has_product": False, "has_shop": False, "has_creator": False, "has_video": False, "has_asin": False, "has_node": False}
    eligible = web_app.eligible_provider_tool_names("amazon", {"objective": "opportunity_discovery", "scope": "cross_category"}, state)
    assert {"keyword_research", "market_research"} <= eligible
    assert web_app.chat_max_tool_rounds("amazon", {"intent": "product_research", "dynamic_planner": True, "max_rounds": 12}, 43) == 50


def test_llm_orchestration_exposes_full_provider_tools_and_keeps_hard_guards() -> None:
    route = {"intent": "product_research", "task_depth": "workflow", "route_source": "llm", "dynamic_planner": True, "research_task": {"objective": "opportunity_discovery", "scope": "cross_category", "entity_type": "none", "entity": "", "entity_source": "none", "region": "US", "time_window": "recent month"}}
    enabled = {"system__current_time", "sellersprite__keyword_research", "sellersprite__market_research", "sellersprite__product_research", "sellersprite__asin_detail"}
    empty = SimpleNamespace(tool_calls=[], tool_results=[])
    assert web_app.provider_profile_tool_ids("amazon", route, "find opportunities", enabled, empty) == enabled
    assert web_app.analysis_minimum_evidence_gaps("amazon", empty, route) == ["provider_tool_attempt"]
    assert "未经用户输入" in web_app.sellersprite_deep_dive_call_error("sellersprite__asin_detail", {"asin": "B0ABCDEF12"}, "find opportunities", empty)


def test_region_default_only_applies_when_schema_supports_it() -> None:
    schemas = [{"name": "keyword_research", "inputSchema": {"type": "object", "required": ["request"], "properties": {"request": {"type": "object", "properties": {"marketplace": {"type": "string"}, "keywords": {"type": "string"}}}}}}]
    original = web_app.list_mcp_bridge_tools
    web_app.list_mcp_bridge_tools = lambda _chat_type: schemas
    try:
        default = web_app.apply_mcp_region_default("sellersprite", "keyword_research", {"request": {"keywords": "flying toys"}}, "US")
        assert default == {"request": {"keywords": "flying toys", "marketplace": "US"}}
        explicit = web_app.apply_mcp_region_default("sellersprite", "keyword_research", {"request": {"marketplace": "DE", "keywords": "flying toys"}}, "US")
        assert explicit["request"]["marketplace"] == "DE"
    finally:
        web_app.list_mcp_bridge_tools = original


def test_fixed_full_site_tool_sets_include_all_sociavault_tools() -> None:
    sociavault_tools = [{"name": "check_credits" if index == 0 else f"social_tool_{index:03d}", "inputSchema": {"type": "object", "properties": {}}} for index in range(107)]
    provider_tools = {"sociavault": sociavault_tools, "sellersprite": [{"name": "keyword_research", "inputSchema": {"type": "object"}}], "chuhaijiang": [{"name": "search", "inputSchema": {"type": "object"}}]}
    original = web_app.list_mcp_bridge_tools
    web_app.list_mcp_bridge_tools = lambda chat_type: provider_tools[chat_type]
    try:
        home_ids = web_app.provider_default_enabled_tool_ids("home")
        amazon_ids = web_app.provider_default_enabled_tool_ids("amazon")
        chuhaijiang_ids = web_app.provider_default_enabled_tool_ids("chuhaijiang")
        assert len([tool_id for tool_id in home_ids if tool_id.startswith("sociavault__")]) == 107
        assert "sellersprite__keyword_research" in amazon_ids
        assert "chuhaijiang__search" in chuhaijiang_ids
    finally:
        web_app.list_mcp_bridge_tools = original


def test_tool_call_signature_deduplicates_argument_order() -> None:
    left = web_app.tool_call_signature("sellersprite__product_research", {"asin": "B0ABCDEF12", "page": 1})
    right = web_app.tool_call_signature("sellersprite__product_research", {"page": 1, "asin": "B0ABCDEF12"})
    assert left == right























if __name__ == "__main__":
    test_sellersprite_official_skill_chain_loads_full_bundle_and_isolates_tools()
    test_tiktok_search_keeps_analysis_fields()
    test_amazon_keeps_product_fields()
    test_current_time_tool_is_available()
    test_web_search_route_exposes_web_search_tool()
    test_locked_amazon_provider_filters_system_web_search()
    test_locked_amazon_product_route_keeps_sellersprite_tools()
    test_active_provider_domains_and_unknown_tool_domain_are_fail_closed()
    test_amazon_url_query_api_fragment_does_not_disable_tools()
    test_ocr_metadata_does_not_change_chat_route()
    test_short_cjk_web_search_filters_irrelevant_results()
    test_pdf_markdown_export_matches_frontend_quote_heading()
    test_web_search_tool_is_registered_and_normalized()
    test_chat_history_archives_done_tools_and_recovers_failed_results()
    test_tool_evidence_is_compact_but_keeps_business_fields()
    test_current_tool_evidence_is_lossless_until_budget_pressure()
    test_dynamic_chat_context_compresses_to_budget()
    test_tool_limit_final_context_removes_protocol_and_detects_dsml()
    test_tool_limit_keeps_large_current_collection_when_capacity_allows()
    test_sellersprite_schema_argument_normalization()
    test_three_layer_research_task_keeps_real_product_entity()
    test_sellersprite_semantic_registry_is_complete_and_lossless()
    test_semantic_brace_residue_is_naturalized_or_preserved_and_logged()
    test_semantic_brace_residue_python_mapping_is_naturalized()
    test_all_sites_disable_frontend_tool_selection()
    test_social_platform_routes_use_sociavault_without_rest_fallback()
    test_social_router_inherits_platform_before_legacy_intent_model()
    test_social_router_model_failures_fall_back_to_full_catalog()
    test_social_router_does_not_hijack_commerce_intents()
    test_intent_decision_validation_and_fallback()
    test_intent_router_uses_recent_context_and_falls_back_on_failure()
    test_empty_mcp_collections_are_not_enough_data()
    test_mcp_content_error_rules_are_provider_specific()
    test_mcp_sql_error_text_is_not_evidence()
    test_deepseek_tool_turn_preserves_reasoning_content()
    test_only_structured_direct_requests_bypass_intent_llm()
    test_disabling_intent_router_restores_legacy_rule_route()
    test_llm_research_task_is_authoritative_for_ambiguous_trend_phrases()
    test_invalid_llm_research_task_uses_structured_only_fallback()
    test_three_layer_research_task_rejects_goal_text_as_entity()
    test_sellersprite_semantic_report_and_pro_synthesis()
    test_dynamic_provider_capability_graph_uses_task_scope_and_evidence()
    test_dynamic_provider_planner_does_not_cap_repeated_calls()
    test_llm_orchestration_exposes_full_provider_tools_and_keeps_hard_guards()
    test_region_default_only_applies_when_schema_supports_it()
    test_fixed_full_site_tool_sets_include_all_sociavault_tools()
    test_tool_call_signature_deduplicates_argument_order()
    print("chat tool normalization tests passed")
