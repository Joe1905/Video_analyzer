#!/usr/bin/env python3
"""Smoke tests for chat tool result normalization."""
from __future__ import annotations

import base64
import hashlib
import io
import inspect
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
# This is a V1/V2 mixed regression suite. Individual V2 cases opt in explicitly;
# legacy assertions must not inherit the deployed canary mode.

import web_app  # noqa: E402
from fastmoss_official_skill import (  # noqa: E402
    OFFICIAL_PROMPT_FILES,
    OFFICIAL_SKILL_ROOT,
    clear_official_fastmoss_skill_memory_cache,
    load_official_fastmoss_skill_prompt,
)
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
from web_app import build_chat_history_context, build_deepseek_tool_assistant_message, build_prefixed_model_tools, build_tool_limit_final_context, chat_markdown_to_html, chat_request_needs_tools, chat_routing_text, compact_chat_tool_evidence, deepseek_tool_protocol_present, estimate_chat_context_tokens, fastmoss_analysis_evidence_gaps, fastmoss_availability_search_arguments, fastmoss_defaults_to_us, fastmoss_empty_availability_answer, fastmoss_playbook_instruction, fastmoss_playbook_intent, fastmoss_product_evidence_required, fastmoss_required_capability_gaps, filter_locked_provider_tool_ids, forced_provider_domain_tool_available, is_chat_retry_request, manage_chat_context, normalize_mcp_tool_arguments, normalize_prefixed_tool_result, normalize_tool_result, parse_chat_intent_decision, provider_default_enabled_tool_ids, provider_forces_mcp_tools, resolve_chat_intent, route_chat_intent  # noqa: E402
from tools import _filter_relevant_search_results, execute_tool, parse_bing_html, parse_duckduckgo_html  # noqa: E402


def test_fastmoss_official_skill_chain_loads_exact_package_and_isolates_tools() -> None:
    previous_fastmoss_report_model = os.environ.get("FASTMOSS_REPORT_MODEL")
    os.environ["FASTMOSS_REPORT_MODEL"] = "deepseek-v4-flash"
    try:
        assert web_app.fastmoss_report_model() == "deepseek-v4-flash"
    finally:
        if previous_fastmoss_report_model is None:
            os.environ.pop("FASTMOSS_REPORT_MODEL", None)
        else:
            os.environ["FASTMOSS_REPORT_MODEL"] = previous_fastmoss_report_model

    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for index, relative_name in enumerate(OFFICIAL_PROMPT_FILES):
            content = (
                f"official-marker-{index}\n"
                if relative_name == "SKILL.md"
                else f"official-reference-{relative_name}\n"
            ).encode("utf-8")
            info = tarfile.TarInfo(OFFICIAL_SKILL_ROOT + relative_name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    payload = archive_buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    clear_official_fastmoss_skill_memory_cache()
    with tempfile.TemporaryDirectory() as temp_dir:
        prompt = load_official_fastmoss_skill_prompt(
            cache_dir=Path(temp_dir),
            archive_payload=payload,
            expected_sha256=digest,
        )
    assert "official-marker-0" in prompt
    for relative_name in OFFICIAL_PROMPT_FILES:
        assert f"官方文件：{relative_name}" in prompt

    route = web_app.fastmoss_official_skill_route()
    assert route["official_skill_chain"] is True
    assert route["route_source"] == "official_skill"
    assert route["dynamic_planner"] is False
    assert route["task_depth"] == "direct"
    assert web_app.chat_route_uses_report_model("fastmoss", route) is False
    assert web_app.official_skill_market_default_instruction("fastmoss") == (
        "应用执行默认值：用户未指定地区时，对支持 region 参数的 FastMoss 工具使用 US。"
    )
    assert web_app.fastmoss_official_skill_tool_ids({
        "fastmoss__product_search",
        "sellersprite__product_research",
        "system__current_time",
    }) == {"fastmoss__product_search"}
    tied_categories = {"mcp_data": {"categories": [
        {
            "category_id_level1": 9,
            "category_id_level2": 835592,
            "category_id_level3": 969224,
            "cn_name": "气枪",
            "score": 0.5109,
        },
        {
            "category_id_level1": 16,
            "category_id_level2": 846632,
            "category_id_level3": 848240,
            "cn_name": "喷枪及配件",
            "score": 0.4988,
        },
    ]}}
    assert web_app.fastmoss_category_ambiguity_question(
        "分析蜘蛛发射器的美区市场",
        tied_categories,
        route,
    ) is None

    instruction = web_app.fastmoss_official_skill_system_instruction(
        "2026-07-27",
        prompt,
    )
    assert instruction.startswith(prompt)
    assert "当前日期：2026-07-27" in instruction
    assert "Asia/Shanghai" in instruction

    evidence = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__product_search",
        "result": {
            "mcp_data": {
                "items": [
                    {"product_id": "1732262172485652893"},
                    {"product_id": "1732262172485652999"},
                ]
            }
        },
    }])
    assert web_app.fastmoss_official_skill_call_error(
        "fastmoss__product_overview",
        {"filter": {"product_id": "1732262172485652999"}},
        "分析蜘蛛发射器的美区市场",
        evidence,
    ) is None
    assert web_app.fastmoss_official_skill_call_error(
        "fastmoss__product_overview",
        {"filter": {"product_id": "1732262172485652000"}},
        "分析蜘蛛发射器的美区市场",
        evidence,
    )


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
    unknown_preset_route = web_app.sellersprite_official_skill_route(
        "解压玩具", "unknown-preset"
    )
    assert unknown_preset_route["tools"] is None
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
    assert low_confidence["route_source"] == "rules_fallback"
    assert parse_chat_intent_decision({"intent": "unknown", "task_depth": "lookup", "confidence": 1}, fallback, "fastmoss", "x")["intent"] == "product_research"
    assert parse_chat_intent_decision(None, fallback, "fastmoss", "x")["intent"] == "product_research"


def test_intent_router_uses_recent_context_and_falls_back_on_failure() -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": json.dumps({
                    "intent": "product_research",
                    "task_depth": "analysis",
                    "playbook": "product",
                    "entity": "厨房切碎机",
                    "region": "US",
                    "confidence": 0.96,
                    "research_task": {
                        "objective": "entity_analysis",
                        "scope": "keyword",
                        "entity_type": "keyword",
                        "entity": "厨房切碎机",
                        "entity_source": "explicit",
                        "region": "US",
                        "time_window": "最近一个月",
                    },
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
            SimpleNamespace(role="user", content="我在关注厨房切碎机这个方向"),
            SimpleNamespace(role="assistant", content="旧回答失败"),
            SimpleNamespace(role="user", content="分析一下它最近一个月的销售增长原因"),
        ]
        fake = FakeRequests()
        route = resolve_chat_intent(messages, "分析一下它最近一个月的销售增长原因", "fastmoss", "key", "https://example.test/v1", "model", fake)
        assert route["intent"] == "fastmoss_product"
        assert route["route_source"] == "llm"
        encoded_payload = json.dumps(fake.payload, ensure_ascii=False)
        assert "厨房切碎机" in encoded_payload
        assert "最近一个月的销售增长原因" in encoded_payload

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

    sellersprite_direct_empty = normalize_prefixed_tool_result(
        "sellersprite__product_node",
        result({"code": "OK", "message": "成功", "data": []}),
    )
    assert sellersprite_direct_empty["data_state"] == "empty"
    assert sellersprite_direct_empty["enough_data"] is False


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
            "research_task": {
                "objective": "entity_analysis",
                "scope": "keyword",
                "entity_type": "keyword",
                "entity": "electric food shredder",
                "entity_source": "explicit",
                "region": "US",
                "time_window": "",
            },
        },
        fallback,
        "fastmoss",
        "给我一份厨房切碎机的完整调研报告",
    )
    assert route["intent"] == "fastmoss_product"
    assert route["task_depth"] == "workflow"
    assert route["playbook"] == "product"
    assert route["max_rounds"] == web_app.FASTMOSS_PLAYBOOKS["product"]["max_rounds"]


def test_only_structured_direct_requests_bypass_intent_llm() -> None:
    research_text = "帮我做一份厨房类目的选品和定价报告"
    assert web_app.chat_intent_router_should_call(
        research_text, web_app.route_chat_intent(research_text, "fastmoss")
    ) is True
    assert web_app.chat_intent_router_should_call(
        "分析 B0ABCDEF12", web_app.route_chat_intent("分析 B0ABCDEF12", "amazon")
    ) is False
    assert web_app.chat_intent_router_should_call(
        "这个产品在 TK 有销售吗？", web_app.route_chat_intent("这个产品在 TK 有销售吗？", "fastmoss")
    ) is False
    assert web_app.chat_intent_router_should_call(
        "现在几点？", web_app.route_chat_intent("现在几点？", "fastmoss")
    ) is False


def test_disabling_intent_router_restores_legacy_rule_route() -> None:
    class NoCallRequests:
        def post(self, *_args, **_kwargs):
            raise AssertionError("intent LLM must not be called when the router is disabled")

    previous = os.environ.get("CHAT_INTENT_ROUTER_ENABLED")
    os.environ["CHAT_INTENT_ROUTER_ENABLED"] = "0"
    try:
        route = web_app.resolve_chat_intent(
            [], "帮我做一份厨房类目的选品报告", "fastmoss",
            "key", "https://example.test/v1", "model", NoCallRequests(),
        )
        assert route["route_source"] == "rules"
        assert route["dynamic_planner"] is True
        assert web_app.llm_orchestrated_route(route) is False
    finally:
        if previous is None:
            os.environ.pop("CHAT_INTENT_ROUTER_ENABLED", None)
        else:
            os.environ["CHAT_INTENT_ROUTER_ENABLED"] = previous


def test_llm_research_task_is_authoritative_for_ambiguous_trend_phrases() -> None:
    cases = (
        ("这个月有什么产品突然爆卖了？", "这个月"),
        ("帮我查找一下最近1-2个月热门趋势新品", "最近1-2个月"),
    )
    for text, time_window in cases:
        decision = {
            "intent": "product_research",
            "task_depth": "analysis",
            "entity": "",
            "region": "US",
            "confidence": 0.97,
            "playbook": "product",
            "research_task": {
                "objective": "trend_discovery",
                "scope": "cross_category",
                "entity_type": "none",
                "entity": "",
                "entity_source": "none",
                "region": "US",
                "time_window": time_window,
            },
        }
        route = web_app.parse_chat_intent_decision(
            decision, web_app.route_chat_intent(text, "fastmoss"), "fastmoss", text
        )
        assert route["route_source"] == "llm"
        assert route["research_task"]["scope"] == "cross_category"
        assert route["research_task"]["entity_type"] == "none"
        assert route["research_task"]["entity"] == ""
        assert "entity" not in route
        assert route["max_rounds"] != 12


def test_invalid_llm_research_task_uses_structured_only_fallback() -> None:
    text = "这个月有什么产品突然爆卖了？"
    invalid = {
        "intent": "product_research",
        "task_depth": "analysis",
        "entity": text,
        "region": "US",
        "confidence": 0.97,
        "playbook": "product",
        "research_task": {
            "objective": "trend_discovery",
            "scope": "cross_category",
            "entity_type": "none",
            "entity": text,
            "entity_source": "explicit",
            "region": "US",
            "time_window": "这个月",
        },
    }
    route = web_app.parse_chat_intent_decision(
        invalid, web_app.route_chat_intent(text, "fastmoss"), "fastmoss", text
    )
    assert route["route_source"] == "rules_fallback"
    assert route["research_task"]["entity"] == ""
    assert route["research_task"]["entity_type"] == "none"

    exact = web_app.research_task_from(
        "分析 B0ABCDEF12", "amazon",
        {"route_source": "rules_fallback", "task_depth": "analysis"},
    )
    assert exact["entity"] == "B0ABCDEF12"
    assert exact["entity_type"] == "asin"
    assert exact["scope"] == "entity"


def test_three_layer_research_task_rejects_goal_text_as_entity() -> None:
    text = "帮我查找一下最近1-2个月热门趋势新品"
    decision = {
        "intent": "product_research",
        "task_depth": "analysis",
        "entity": text,
        "region": "US",
        "confidence": 0.96,
        "playbook": "product",
        "research_task": {
            "objective": "trend_discovery",
            "scope": "cross_category",
            "entity_type": "none",
            "entity": text,
            "entity_source": "explicit",
            "region": "US",
            "time_window": "最近1-2个月",
        },
    }
    route = web_app.parse_chat_intent_decision(
        decision, web_app.route_chat_intent(text, "fastmoss"), "fastmoss", text
    )
    assert route["intent"] == "fastmoss_product"
    assert route["task_depth"] == "workflow"
    assert route["dynamic_planner"] is True
    assert "entity" not in route
    assert route["research_task"] == {
        "objective": "trend_discovery",
        "scope": "cross_category",
        "entity_type": "none",
        "entity": "",
        "entity_source": "none",
        "region": "US",
        "time_window": "最近1-2个月",
    }
    amazon = web_app.attach_research_task(
        {"intent": "product_research", "task_depth": "analysis"},
        "amazon",
        "美区帮我寻找需求大但卖家少的蓝海产品、潜力商品和热门新品",
    )
    assert amazon["research_task"]["scope"] == "cross_category"
    assert amazon["research_task"]["entity_type"] == "none"
    assert amazon["dynamic_planner"] is True
    explicit_cross = web_app.research_task_from(
        "帮我找美区亚马逊需求增长、竞争相对可验证的热门新品方向，先做跨品类发现",
        "amazon",
        {"intent": "product_research", "task_depth": "workflow"},
        {"research_task": {"entity": "需求增长与竞争可验证", "entity_type": "keyword"}},
    )
    assert explicit_cross["objective"] == "trend_discovery"
    assert explicit_cross["scope"] == "cross_category"
    assert explicit_cross["entity_type"] == "none"
    assert explicit_cross["entity"] == ""


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
    assert category_args == {"query": ["料理机"]}

    original_query_args = web_app.apply_fastmoss_business_defaults(
        "search_category_by_words",
        {"query": ["Electric Food Shredder", "Mini Meat Grinder", "Food Processor"], "top_k": 8},
        SimpleNamespace(tool_calls=[], tool_results=[]),
        fixed_today,
        user_text="给我一份 Electric Food Shredder, Mini Meat Grinder 这类产品的调研报告",
        route={"playbook": "product", "entity": "Electric Food Shredder, Mini Meat Grinder, Food Processor"},
    )
    assert original_query_args == {
        "query": ["Electric Food Shredder", "Mini Meat Grinder"], "top_k": 8,
    }


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
    exact_confirmation = [
        SimpleNamespace(role="user", content=text),
        SimpleNamespace(
            role="assistant",
            content="FastMoss 对这个关键词的类目匹配很接近，请直接回复要研究的类目名称。",
        ),
        SimpleNamespace(role="user", content="料理机"),
    ]
    exact_inherited = web_app.fastmoss_inherited_segment_keywords(
        exact_confirmation, exact_confirmation[-1].content
    )
    assert exact_inherited == ["Electric Food Shredder", "Mini Meat Grinder"]
    later_research = exact_confirmation + [
        SimpleNamespace(role="assistant", content="料理机调研报告已完成。"),
        SimpleNamespace(role="user", content="重新调研 air fryer"),
    ]
    assert web_app.fastmoss_inherited_segment_keywords(later_research, later_research[-1].content) == []
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
    assert web_app.fastmoss_category_ambiguity_question("Electric Food Shredder 调研", result)

    latest_result = {"mcp_data": {"result": {"categories": [
        {
            "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 934664,
            "cn_name": "面包机", "cn_full_name": "家电-厨房家电-面包机", "score": 0.5152,
            "matched_query": "Food Processor",
        },
        {
            "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 935176,
            "cn_name": "料理机", "cn_full_name": "家电-厨房家电-料理机", "score": 0.5023,
            "matched_query": "Electric Food Shredder",
        },
        {
            "category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 983944,
            "cn_name": "垃圾处理器", "cn_full_name": "家电-厨房家电-垃圾处理器", "score": 0.4891,
            "matched_query": "Electric Food Shredder",
        },
    ]}}}
    latest_question = web_app.fastmoss_category_ambiguity_question(
        "Electric Food Shredder, Mini Meat Grinder 调研", latest_result
    )
    assert latest_question and "料理机" in latest_question and "垃圾处理器" in latest_question
    assert "面包机" not in latest_question
    assert web_app.fastmoss_category_ambiguity_question("目标类目明确选择料理机", latest_result) is None


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


def test_sellersprite_semantic_report_and_pro_synthesis() -> None:
    marker = "SELLERSPRITE-REPORT-MARKER"
    message = SimpleNamespace(
        tool_calls=[{
            "id": "c1",
            "function": {
                "name": "sellersprite__keyword_research",
                "arguments": json.dumps({
                    "request": {"marketplace": "US", "keyword": "stroller fan"},
                }),
            },
        }],
        tool_results=[{
            "tool_name": "sellersprite__keyword_research",
            "result": {
                "ok": True,
                "data_state": "data",
                "mcp_data": {
                    "items": [{
                        "keyword": "stroller fan",
                        "searches": 12345,
                        "futureBusinessField": marker,
                    }],
                },
            },
        }],
    )
    route = {
        "intent": "product_research",
        "task_depth": "analysis",
        "research_task": {"objective": "opportunity_discovery", "region": "US"},
    }
    dossier = web_app.sellersprite_report_evidence_dossier(message, route)
    assert len(dossier["tool_evidence"]) == 1
    assert dossier["tool_evidence"][0]["source_ref"] == "call:1"
    assert marker in json.dumps(dossier, ensure_ascii=False)

    semantic, semantic_stats = web_app.sellersprite_render_report_evidence(dossier)
    assert "## stroller fan · 关键词研究结果" in semantic
    assert marker not in semantic
    assert "12345" in semantic
    assert "sellersprite__" not in semantic
    assert "call:1" not in semantic
    assert "source_ref" not in semantic
    assert "futureBusinessField" not in semantic
    assert semantic_stats["format"] == "semantic"

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "# SellerSprite 报告\n\n已由 V4 Pro 合成。"},
                }],
            }

    class Requests:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def post(self, _url: str, **kwargs):
            self.payloads.append(json.loads(kwargs["data"].decode("utf-8")))
            return Response()

    requests = Requests()
    report = web_app.synthesize_sellersprite_report_from_packet(
        message,
        "分析 stroller fan 市场",
        route,
        requests,
        "test-key",
        "https://example.invalid/v1",
        "deepseek-v4-pro-test",
    )
    assert report.startswith("# SellerSprite 报告")
    assert report.count(web_app.SELLERSPRITE_REPORT_NOTICE) == 1
    assert len(requests.payloads) == 1
    payload = requests.payloads[0]
    assert payload["model"] == "deepseek-v4-pro-test"
    assert payload["max_tokens"] == 12000
    assert "tools" not in payload
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    report_system = payload["messages"][0]["content"]
    report_date = re.search(r"当前日期（Asia/Shanghai）：(\d{4}-\d{2}-\d{2})", report_system).group(1)
    assert report_system == web_app.sellersprite_report_system_instruction(report_date)
    assert report_system != web_app.fastmoss_report_system_instruction(report_date)
    assert "每个有实质数据的业务证据段都必须在报告中得到使用" in report_system
    assert "不得补造采购成本、利润、FBA费用、广告花费、ACoS" in report_system
    assert "sellersprite__" not in report_system
    assert "fastmoss__" not in report_system
    assert marker not in report_system
    assert marker not in payload["messages"][1]["content"]
    assert "# 亚马逊调研证据" in payload["messages"][1]["content"]
    assert "--- Semantic 证据开始 ---" in payload["messages"][1]["content"]
    assert web_app.append_sellersprite_report_notice(report, route) == report

    official_answer = web_app.complete_sellersprite_answer(
        "美国站关键词 stroller fan 的月搜索量为 12345。",
        message,
        "查询 stroller fan 月搜索量",
        web_app.sellersprite_official_skill_route("查询 stroller fan 月搜索量"),
        requests,
        "test-key",
        "https://example.invalid/v1",
        "deepseek-v4-pro-test",
    )
    assert official_answer == "美国站关键词 stroller fan 的月搜索量为 12345。"

    class FailedRequests:
        def post(self, _url: str, **_kwargs):
            raise RuntimeError("report model unavailable")

    failed = web_app.synthesize_sellersprite_report_from_packet(
        message,
        "分析 stroller fan 市场",
        route,
        FailedRequests(),
        "test-key",
        "https://example.invalid/v1",
        "deepseek-v4-pro-test",
    )
    assert "没有使用 Flash 草稿替代 V4 Pro 报告" in failed

    run_source = inspect.getsource(web_app.run_chat_deepseek)
    assert "synthesize_sellersprite_report_from_packet(" not in run_source
    assert run_source.count("complete_sellersprite_answer(") >= 5


def test_dynamic_provider_capability_graph_uses_task_scope_and_evidence() -> None:
    fast_text = "帮我查找一下最近1-2个月热门趋势新品"
    fast_route = web_app.attach_research_task(
        {"intent": "product_research", "task_depth": "analysis"}, "fastmoss", fast_text
    )
    fast_enabled = {
        "system__current_time",
        "fastmoss__market_category_ranking",
        "fastmoss__search_category_by_words",
        "fastmoss__market_category_analysis",
        "fastmoss__product_rank_new_listed",
        "fastmoss__product_rank_top_selling",
        "fastmoss__product_detail_info",
    }
    message = SimpleNamespace(tool_calls=[], tool_results=[])
    selected = web_app.provider_profile_tool_ids("fastmoss", fast_route, fast_text, fast_enabled, message)
    assert selected == {"system__current_time", "fastmoss__market_category_ranking"}
    assert web_app.fastmoss_clarifying_question("fastmoss", fast_route, fast_text) is None
    message.tool_calls.append({
        "function": {"name": "fastmoss__market_category_ranking", "arguments": '{"filter":{"region":"US"}}'},
    })
    message.tool_results.append({
        "tool_name": "fastmoss__market_category_ranking",
        "result": {"ok": True, "data_state": "data", "evidence_observed": True, "mcp_data": {
            "ranked_categories": [{"category_id_level1": 13, "cn_name": "家电"}],
        }},
    })
    selected = web_app.provider_profile_tool_ids("fastmoss", fast_route, fast_text, fast_enabled, message)
    assert "fastmoss__search_category_by_words" in selected
    assert "fastmoss__product_rank_new_listed" not in selected
    assert "category_resolution" in web_app.fastmoss_analysis_evidence_gaps(fast_text, message, fast_route)
    allowed_query = {"query": ["家电"]}
    assert web_app.fastmoss_deep_dive_call_error(
        "fastmoss__search_category_by_words", allowed_query, fast_text, message, fast_route
    ) is None
    assert "未经当前类目榜" in web_app.fastmoss_deep_dive_call_error(
        "fastmoss__search_category_by_words", {"query": ["瑜伽裤"]}, fast_text, message, fast_route
    )
    category_args = web_app.apply_fastmoss_business_defaults(
        "search_category_by_words", allowed_query, message, user_text=fast_text, route=fast_route
    )
    assert category_args == allowed_query
    expanded_category_args = web_app.apply_fastmoss_business_defaults(
        "search_category_by_words",
        {"query": ["家电", "女装", "运动户外", "手机数码"]},
        message,
        user_text=fast_text,
        route=fast_route,
    )
    assert expanded_category_args["query"] == ["家电", "女装", "运动户外", "手机数码"]

    message.tool_calls.append({
        "function": {"name": "fastmoss__search_category_by_words", "arguments": '{"query":["家电"]}'},
    })
    message.tool_results.append({
        "tool_name": "fastmoss__search_category_by_words",
        "result": {"ok": True, "data_state": "data", "evidence_observed": True, "mcp_data": {
            "categories": [{
                "category_id_level1": 13,
                "category_id_level2": 844168,
                "category_id_level3": 935176,
                "cn_name": "料理机",
            }],
        }},
    })
    selected = web_app.provider_profile_tool_ids("fastmoss", fast_route, fast_text, fast_enabled, message)
    assert "fastmoss__market_category_analysis" in selected
    assert "fastmoss__product_rank_new_listed" in selected
    assert "fastmoss__product_detail_info" not in selected
    assert "category_resolution" not in web_app.fastmoss_analysis_evidence_gaps(fast_text, message, fast_route)

    amazon_text = "美区帮我寻找需求大但卖家少的蓝海产品、潜力商品和热门新品"
    amazon_route = web_app.attach_research_task(
        {"intent": "product_research", "task_depth": "analysis"}, "amazon", amazon_text
    )
    amazon_enabled = {
        "system__current_time",
        "sellersprite__keyword_research",
        "sellersprite__aba_research_monthly",
        "sellersprite__market_research",
        "sellersprite__keyword_research_trends",
        "sellersprite__product_research",
        "sellersprite__asin_detail",
        "sellersprite__review",
    }
    amazon_message = SimpleNamespace(tool_calls=[], tool_results=[])
    selected = web_app.provider_profile_tool_ids("amazon", amazon_route, amazon_text, amazon_enabled, amazon_message)
    assert "sellersprite__keyword_research" in selected
    assert "sellersprite__aba_research_monthly" in selected
    assert "sellersprite__market_research" in selected
    assert "sellersprite__product_research" not in selected
    assert "sellersprite__asin_detail" not in selected
    assert set(web_app.sellersprite_analysis_evidence_gaps(amazon_text, amazon_message, amazon_route)) == {
        "keyword_discovery", "market_discovery", "product_discovery",
    }

    discovered_asin_message = SimpleNamespace(
        tool_calls=[{
            "function": {"name": "sellersprite__keyword_research", "arguments": "{}"},
        }],
        tool_results=[{
            "tool_name": "sellersprite__keyword_research",
            "result": {
                "ok": True,
                "data_state": "data",
                "evidence_observed": True,
                "mcp_data": {"items": [{"keyword": "stroller fan", "asin": "B0ABCDEF12"}]},
            },
        }],
    )
    selected = web_app.provider_profile_tool_ids(
        "amazon", amazon_route, amazon_text, amazon_enabled, discovered_asin_message
    )
    assert "sellersprite__market_research" in selected
    assert "sellersprite__product_research" in selected
    assert "sellersprite__asin_detail" in selected
    assert "sellersprite__review" in selected
    assert set(web_app.sellersprite_analysis_evidence_gaps(
        amazon_text, discovered_asin_message, amazon_route
    )) == {"market_discovery", "product_discovery", "asin_detail", "asin_support"}

    amazon_message.tool_calls.extend([
        {"function": {"name": "sellersprite__aba_research_monthly", "arguments": "{}"}},
        {"function": {"name": "sellersprite__aba_research_monthly", "arguments": "{}"}},
        {"function": {"name": "sellersprite__aba_research_monthly", "arguments": "{}"}},
    ])
    amazon_message.tool_results.extend([
        {
            "tool_name": "sellersprite__aba_research_monthly",
            "result": {"ok": True, "data_state": "data", "evidence_observed": True, "mcp_data": {}},
        }
        for _ in range(3)
    ])
    selected = web_app.provider_profile_tool_ids("amazon", amazon_route, amazon_text, amazon_enabled, amazon_message)
    assert "sellersprite__aba_research_monthly" not in selected
    assert "sellersprite__keyword_research" not in selected
    assert "sellersprite__product_research" in selected
    assert "sellersprite__keyword_research_trends" in selected
    amazon_message.tool_calls.extend([
        {"function": {"name": "sellersprite__product_research", "arguments": "{}"}}
        for _ in range(9)
    ])
    repeated_selected = web_app.provider_profile_tool_ids(
        "amazon", amazon_route, amazon_text, amazon_enabled, amazon_message
    )
    assert "sellersprite__product_research" in repeated_selected
    assert "sellersprite__keyword_research_trends" in repeated_selected

    asin_route = web_app.attach_research_task(
        {"intent": "product_research", "task_depth": "analysis", "entity": "B0H3ZH8BF8"},
        "amazon", "分析 B0H3ZH8BF8",
    )
    asin_message = SimpleNamespace(tool_calls=[], tool_results=[])
    selected = web_app.provider_profile_tool_ids("amazon", asin_route, "分析 B0H3ZH8BF8", amazon_enabled, asin_message)
    assert "sellersprite__asin_detail" in selected
    assert "sellersprite__review" in selected
    assert "sellersprite__keyword_research" not in selected
    assert web_app.sellersprite_analysis_evidence_gaps("分析 B0H3ZH8BF8", asin_message, asin_route) == [
        "asin_detail", "asin_support",
    ]
    completed_asin_message = SimpleNamespace(
        tool_calls=[
            {"function": {"name": "sellersprite__asin_detail", "arguments": "{}"}},
            {"function": {"name": "sellersprite__review", "arguments": "{}"}},
        ],
        tool_results=[],
    )
    assert web_app.sellersprite_analysis_evidence_gaps(
        "分析 B0H3ZH8BF8", completed_asin_message, asin_route
    ) == []
    assert web_app.sellersprite_deep_dive_call_error(
        "sellersprite__asin_detail",
        {"marketplace": "US", "asin": "B0H3ZH8BF8"},
        "分析 B0H3ZH8BF8",
        asin_message,
    ) is None
    evidence_message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "sellersprite__product_research",
        "result": {"ok": True, "mcp_data": {"products": [{"asin": "B0ABCDEF12"}]}},
    }])
    assert web_app.sellersprite_deep_dive_call_error(
        "sellersprite__traffic_extend",
        {"request": {"marketplace": "US", "asinList": ["B0ABCDEF12"], "queryType": 2}},
        amazon_text,
        evidence_message,
    ) is None
    assert "未经用户输入" in web_app.sellersprite_deep_dive_call_error(
        "sellersprite__traffic_extend",
        {"request": {"marketplace": "US", "asinList": ["B0EXAMPLE1"], "queryType": 2}},
        amazon_text,
        evidence_message,
    )


def test_dynamic_provider_planner_does_not_cap_repeated_calls() -> None:
    task = {"objective": "opportunity_discovery", "scope": "cross_category"}
    state = {
        "attempted_capabilities": [],
        "observed_capabilities": [],
        "tool_counts": {"keyword_research": 99, "market_research": 99},
        "has_category": False,
        "has_product": False,
        "has_shop": False,
        "has_creator": False,
        "has_video": False,
        "has_asin": False,
        "has_node": False,
    }
    eligible = web_app.eligible_provider_tool_names("amazon", task, state)
    assert "keyword_research" in eligible
    assert "market_research" in eligible

    old_round_limit = os.environ.pop("CHAT_DYNAMIC_TOOL_ROUND_LIMIT", None)
    try:
        assert web_app.chat_max_tool_rounds(
            "amazon",
            {"intent": "product_research", "dynamic_planner": True, "max_rounds": 12},
            43,
        ) == 50
    finally:
        if old_round_limit is not None:
            os.environ["CHAT_DYNAMIC_TOOL_ROUND_LIMIT"] = old_round_limit

    queries = ["Kitchen", "Beauty", "Pet", "Sports", "Tools"]
    normalized = web_app.apply_fastmoss_business_defaults(
        "search_category_by_words",
        {"query": queries},
        SimpleNamespace(tool_calls=[], tool_results=[]),
        user_text="跨类目寻找机会",
        route={"dynamic_planner": True, "research_task": {"scope": "cross_category"}},
    )
    assert normalized["query"] == queries


def test_llm_orchestration_exposes_full_provider_tools_and_keeps_hard_guards() -> None:
    route = {
        "intent": "fastmoss_product",
        "task_depth": "workflow",
        "route_source": "llm",
        "dynamic_planner": True,
        "playbook": "product",
        "research_task": {
            "objective": "trend_discovery",
            "scope": "cross_category",
            "entity_type": "none",
            "entity": "",
            "entity_source": "none",
            "region": "US",
            "time_window": "这个月",
        },
    }
    enabled = {
        "system__current_time",
        "fastmoss__market_category_ranking",
        "fastmoss__search_category_by_words",
        "fastmoss__product_rank_new_listed",
        "fastmoss__product_detail_info",
    }
    empty = SimpleNamespace(tool_calls=[], tool_results=[])
    assert web_app.provider_profile_tool_ids("fastmoss", route, "本月爆卖产品", enabled, empty) == enabled
    instruction = web_app.research_planner_instruction("fastmoss", route, "本月爆卖产品", empty)
    assert "程序不会规定首个工具" in instruction
    assert "能力图仅供参考，不是工具门禁" in instruction
    assert "首个业务调用必须" not in instruction
    assert "不是固定工具顺序" in web_app.fastmoss_playbook_instruction("product", advisory=True)
    system_instruction = web_app.chat_system_instruction("fastmoss", "2026-07-21")
    assert "starts from platform/category ranking" not in system_instruction
    assert "do not impose a fixed first tool or sequence" in system_instruction

    assert web_app.fastmoss_deep_dive_call_error(
        "fastmoss__search_category_by_words", {"query": ["瑜伽裤"]}, "本月爆卖产品", empty, route
    ) is None
    assert web_app.fastmoss_deep_dive_call_error(
        "fastmoss__market_category_analysis", {"filter": {"category_id": 855944}}, "本月爆卖产品", empty, route
    ) is not None

    assert web_app.analysis_minimum_evidence_gaps("fastmoss", empty, route) == ["provider_tool_attempt"]
    failed_attempt = SimpleNamespace(
        tool_calls=[{"function": {"name": "fastmoss__product_search", "arguments": "{}"}}],
        tool_results=[{
            "tool_name": "fastmoss__product_search",
            "result": {"ok": False, "data_state": "error", "evidence_observed": False},
        }],
    )
    assert web_app.analysis_minimum_evidence_gaps("fastmoss", failed_attempt, route) == []

    fallback_route = dict(route, route_source="rules_fallback")
    assert web_app.provider_profile_tool_ids(
        "fastmoss", fallback_route, "本月爆卖产品", enabled, empty
    ) == enabled

    amazon_route = {
        "intent": "product_research",
        "task_depth": "workflow",
        "route_source": "llm",
        "dynamic_planner": True,
        "research_task": {
            "objective": "opportunity_discovery",
            "scope": "cross_category",
            "entity_type": "none",
            "entity": "",
            "entity_source": "none",
            "region": "US",
            "time_window": "最近两个月",
        },
    }
    amazon_enabled = {
        "system__current_time",
        "sellersprite__keyword_research",
        "sellersprite__market_research",
        "sellersprite__product_research",
        "sellersprite__asin_detail",
    }
    assert web_app.provider_profile_tool_ids(
        "amazon", amazon_route, "寻找亚马逊新品机会", amazon_enabled, empty
    ) == amazon_enabled
    assert web_app.analysis_minimum_evidence_gaps("amazon", empty, amazon_route) == ["provider_tool_attempt"]
    assert "未经用户输入或当前 SellerSprite 证据" in web_app.sellersprite_deep_dive_call_error(
        "sellersprite__asin_detail", {"asin": "B0ABCDEF12"}, "寻找亚马逊新品机会", empty
    )

    # Replay the two calls that were previously dropped after category ranking.
    # Capability stages may describe a useful next step, but cannot admit or deny
    # tools selected by an LLM-owned route.
    planner_state = {
        "attempted_capabilities": ["category_ranking"],
        "observed_capabilities": ["category_ranking"],
        "tool_counts": {"market_category_ranking": 1},
        "has_category": False,
        "has_product": False,
        "has_shop": False,
        "has_creator": False,
        "has_video": False,
        "has_asin": False,
        "has_node": False,
    }
    assert web_app.provider_tool_stage_error(
        "fastmoss", route, "fastmoss", "product_rank_top_selling", planner_state
    ) is None
    assert web_app.provider_tool_stage_error(
        "fastmoss", route, "fastmoss", "product_rank_new_listed", planner_state
    ) is None
    assert web_app.provider_tool_stage_error(
        "fastmoss", dict(route, route_source="rules"), "fastmoss", "not_in_capability_graph", planner_state
    ) == "legacy_capability_stage"

    tied_categories = {"mcp_data": {"result": {"categories": [
        {"category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 934664,
         "cn_name": "面包机", "score": 0.515},
        {"category_id_level1": 13, "category_id_level2": 844168, "category_id_level3": 935176,
         "cn_name": "料理机", "score": 0.502},
    ]}}}
    named_llm_route = dict(route, research_task={
        **route["research_task"],
        "scope": "keyword",
        "entity_type": "keyword",
        "entity": "food processor",
        "entity_source": "explicit",
    })
    assert web_app.fastmoss_category_ambiguity_question(
        "food processor 调研", tied_categories, named_llm_route
    ) is None

    category_evidence = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__search_category_by_words",
        "result": {"ok": True, "mcp_data": {"categories": [{
            "category_id_level1": 13,
            "category_id_level2": 844168,
            "category_id_level3": 935176,
        }]}},
    }])
    explicit_query = web_app.apply_fastmoss_business_defaults(
        "search_category_by_words",
        {"query": ["瑜伽裤"], "top_k": 7},
        category_evidence,
        user_text="food processor 调研",
        route=named_llm_route,
    )
    assert explicit_query == {"query": ["瑜伽裤"], "top_k": 7}
    explicit_ranking = web_app.apply_fastmoss_business_defaults(
        "market_category_ranking",
        {
            "filter": {"category_id": 935176, "date_type": "month", "date_value": "2026-06"},
            "orderby": [{"field": "category_gmv", "order": "desc"}],
            "page": 2,
            "pagesize": 20,
        },
        category_evidence,
        user_text="food processor 调研",
        route=named_llm_route,
    )
    assert explicit_ranking["filter"] == {
        "category_id": 935176, "date_type": "month", "date_value": "2026-06",
    }
    assert explicit_ranking["orderby"] == [{"field": "category_gmv", "order": "desc"}]
    assert explicit_ranking["page"] == 2 and explicit_ranking["pagesize"] == 20
    explicit_new_listed = web_app.apply_fastmoss_business_defaults(
        "product_rank_new_listed",
        {"filter": {"category_l1_id": 13, "listing_start_date": "2026-05-01"}},
        category_evidence,
        user_text="food processor 调研",
        route=named_llm_route,
    )
    assert explicit_new_listed["filter"]["category_l1_id"] == 13
    assert explicit_new_listed["filter"]["listing_start_date"] == "2026-05-01"
    assert "category_l2_id" not in explicit_new_listed["filter"]


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
        {
            "name": "keyword_research",
            "inputSchema": {
                "type": "object",
                "required": ["request"],
                "properties": {
                    "request": {
                        "type": "object",
                        "properties": {
                            "marketplace": {"type": "string"},
                            "keywords": {"type": "string"},
                        },
                    },
                },
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
        seller_default = web_app.apply_mcp_region_default(
            "sellersprite", "keyword_research", {"request": {"keywords": "flying toys"}}, "US"
        )
        assert seller_default == {
            "request": {"keywords": "flying toys", "marketplace": "US"}
        }
        seller_explicit = web_app.apply_mcp_region_default(
            "sellersprite",
            "keyword_research",
            {"request": {"marketplace": "DE", "keywords": "flying toys"}},
            "US",
        )
        assert seller_explicit["request"]["marketplace"] == "DE"
    finally:
        web_app.list_mcp_bridge_tools = original


def test_all_sites_disable_frontend_tool_selection() -> None:
    chat_html = (ROOT / "scripts" / "static" / "chat.html").read_text(encoding="utf-8")
    ui_system_js = (ROOT / "scripts" / "static" / "assets" / "ui-system.js").read_text(encoding="utf-8")
    assert "headerToolBtn" not in chat_html
    assert "toolModal" not in chat_html
    assert 'id="toolBtn"' not in chat_html
    assert "enhanceToolTree" not in ui_system_js
    assert "enabledToolMasks" not in chat_html
    assert "TOOL_SELECTION" not in chat_html


def test_fixed_full_site_tool_sets_include_all_sociavault_tools() -> None:
    sociavault_tools = [
        {
            "name": "check_credits" if index == 0 else f"social_tool_{index:03d}",
            "description": f"SociaVault tool {index}",
            "inputSchema": {"type": "object", "properties": {}},
        }
        for index in range(107)
    ]
    provider_tools = {
        "sociavault": sociavault_tools,
        "sellersprite": [{"name": "keyword_research", "inputSchema": {"type": "object"}}],
        "fastmoss": [{"name": "product_search", "inputSchema": {"type": "object"}}],
    }
    original = web_app.list_mcp_bridge_tools
    web_app.list_mcp_bridge_tools = lambda chat_type: provider_tools[chat_type]
    try:
        home_ids = web_app.provider_default_enabled_tool_ids("home")
        amazon_ids = web_app.provider_default_enabled_tool_ids("amazon")
        fastmoss_ids = web_app.provider_default_enabled_tool_ids("fastmoss")

        assert len([tool_id for tool_id in home_ids if tool_id.startswith("sociavault__")]) == 107
        assert not any(tool_id.startswith("function__tiktok_") for tool_id in home_ids)
        assert "sellersprite__keyword_research" not in home_ids
        assert "fastmoss__product_search" not in home_ids
        assert "sellersprite__keyword_research" in amazon_ids
        assert not any(tool_id.startswith("sociavault__") for tool_id in amazon_ids)
        assert "fastmoss__product_search" in fastmoss_ids
        assert not any(tool_id.startswith("sociavault__") for tool_id in fastmoss_ids)

        home_model_tools = web_app.build_prefixed_model_tools(home_ids)
        home_model_names = {
            str(tool.get("function", {}).get("name") or "")
            for tool in home_model_tools
        }
        assert len([name for name in home_model_names if name.startswith("sociavault__")]) == 107
        assert not any(name.startswith("function__tiktok_") for name in home_model_names)
        assert len(home_model_tools) <= 128

        catalog = web_app.build_tool_catalog("home")
        sociavault_domain = next(domain for domain in catalog["domains"] if domain["id"] == "sociavault")
        catalog_tools = [
            tool
            for category in sociavault_domain["categories"]
            for tool in category["tools"]
            if not tool.get("disabled")
        ]
        assert catalog["selectionEnabled"] is False
        assert len(catalog_tools) == 107
    finally:
        web_app.list_mcp_bridge_tools = original


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


def test_lightweight_fastmoss_skill_uses_runtime_dates_and_semantic_deduplication() -> None:
    route = {"lightweight_fastmoss_skill": True}
    today = __import__("datetime").date(2026, 7, 16)
    message = SimpleNamespace(tool_calls=[], tool_results=[])

    default_period = web_app.apply_fastmoss_business_defaults(
        "market_category_analysis",
        {"filter": {"category_id": 935176}},
        message,
        today,
        user_text="分析美国解压玩具最近表现",
        route=route,
    )
    assert default_period["filter"]["category_id"] == 935176
    assert default_period["filter"]["date_value"] == "2026-W28"

    explicit_period = web_app.apply_fastmoss_business_defaults(
        "market_category_analysis",
        {"filter": {"category_id": 935176, "date_type": "month", "date_value": "2026-06"}},
        message,
        today,
        user_text="分析美国解压玩具最近 30 天表现",
        route=route,
    )
    assert explicit_period["filter"]["date_type"] == "month"
    assert explicit_period["filter"]["date_value"] == "2026-06"

    first = web_app.chat_tool_call_signature(
        "fastmoss__market_category_analysis",
        {
            "analysis_type": "basic_metrics",
            "lang": "EN_US",
            "filter": {"category_id": 935176, "date_type": "week", "date_value": "2026-W28"},
        },
        route,
    )
    second = web_app.chat_tool_call_signature(
        "fastmoss__market_category_analysis",
        {
            "lang": "ZH_CN",
            "analysis_type": "basic_metrics",
            "filter": {"date_value": "2026-W28", "date_type": "week", "category_id": 935176},
        },
        route,
    )
    assert first == second
    different_window = web_app.chat_tool_call_signature(
        "fastmoss__market_category_analysis",
        {
            "analysis_type": "basic_metrics",
            "filter": {
                "category_id": 935176,
                "date_range": {"start_date": "2026-06-01", "end_date": "2026-06-30"},
            },
        },
        route,
    )
    assert first != different_window


def test_lightweight_fastmoss_skill_final_path_uses_semantic_report_without_draft_fallback() -> None:
    first_draft = "首稿证据与判断。" * 180
    final_answer = "最终结论基于本轮工具证据，建议先验证代表性样本和竞争风险。"

    class Response:
        status_code = 200
        text = ""

        def __init__(self, body: dict) -> None:
            self.body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.body

    class FakeRequests:
        def __init__(self, fail_synthesis: bool = False) -> None:
            self.fail_synthesis = fail_synthesis
            self.payloads: list[dict] = []

        def post(self, _url: str, **kwargs):
            self.payloads.append(json.loads(kwargs["data"].decode("utf-8")))
            if len(self.payloads) == 1:
                return Response({"choices": [{"message": {"content": "", "tool_calls": [{
                    "id": "tool-1",
                    "type": "function",
                    "function": {
                        "name": "fastmoss__search_category_by_words",
                        "arguments": json.dumps({"query": ["解压玩具"]}),
                    },
                }]}}]})
            if len(self.payloads) == 2:
                return Response({"choices": [{"message": {"content": first_draft}}]})
            if self.fail_synthesis:
                raise TimeoutError("synthesis timeout")
            return Response({"choices": [{"message": {"content": json.dumps({
                "overall_judgement": final_answer,
                "recommend": [{
                    "candidate": "解压玩具", "reason": "已取得类目解析证据", "counter_evidence": "尚待商品深挖",
                    "product_form": "", "price_band": "", "entry_conditions": "补齐商品样本",
                    "validation_actions": ["补查热销商品"], "evidence_refs": ["call:1"],
                }], "watch": [], "reject": [], "can_support": ["已完成类目解析"],
                "cannot_support": ["尚不能直接给出完整市场结论"], "next_actions": ["补齐商品深挖"],
            }, ensure_ascii=False)}}]})

    class Store:
        def update_message(self, _session, message, content: str, status: str = "done") -> None:
            message.content = content
            message.status = status

        def broadcast(self, _session_id: str, _event: str, _data: dict) -> None:
            return None

    def run_case(fail_synthesis: bool) -> tuple[web_app.Message, list[dict]]:
        fake_requests = FakeRequests(fail_synthesis)
        user = web_app.Message("user-1", "user", "请使用 FastMoss 官方 Skill「选品决策」开始分析：解压玩具")
        assistant = web_app.Message("assistant-1", "assistant", "", status="pending")
        session = SimpleNamespace(id="fastmoss-test", messages=[user, assistant])
        original_requests = sys.modules.get("requests")
        original_execute = web_app.execute_prefixed_tool
        original_record = web_app.record_api_call
        previous_key = os.environ.get("DEEPSEEK_API_KEY")
        previous_official = os.environ.get("FASTMOSS_OFFICIAL_SKILL_ENABLED")
        previous_source = os.environ.get("FASTMOSS_SKILL_SOURCE")
        sys.modules["requests"] = fake_requests
        web_app.execute_prefixed_tool = lambda *_args, **_kwargs: {
            "data": {"content": [{"type": "text", "text": '{"categories":[{"category_id_level1":13}]}' }]}
        }
        web_app.record_api_call = lambda *_args, **_kwargs: None
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
        os.environ["FASTMOSS_OFFICIAL_SKILL_ENABLED"] = "1"
        os.environ["FASTMOSS_SKILL_SOURCE"] = "local"
        try:
            web_app.run_chat_deepseek(Store(), session, assistant, user.content, "fastmoss", official_preset_id="fm-product-scout")
        finally:
            web_app.execute_prefixed_tool = original_execute
            web_app.record_api_call = original_record
            if original_requests is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = original_requests
            for name, previous in (
                ("DEEPSEEK_API_KEY", previous_key),
                ("FASTMOSS_OFFICIAL_SKILL_ENABLED", previous_official),
                ("FASTMOSS_SKILL_SOURCE", previous_source),
            ):
                if previous is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous
        return assistant, fake_requests.payloads

    assistant, payloads = run_case(False)
    assert assistant.status == "done"
    assert final_answer in assistant.content
    assert "## 完整数据表" in assistant.content
    assert "## 数据驱动判断" in assistant.content
    assert len(payloads) == 3
    synthesis_payload = payloads[-1]
    assert "tools" not in synthesis_payload
    synthesis_context = json.dumps(synthesis_payload["messages"], ensure_ascii=False)
    assert first_draft not in synthesis_context
    assert "解压玩具" in synthesis_context

    recovered, failed_payloads = run_case(True)
    assert recovered.status == "done"
    assert recovered.content
    assert first_draft not in recovered.content
    assert len(failed_payloads) == 3


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


def test_fastmoss_data_first_fallback_never_discards_usable_evidence() -> None:
    manifest = {
        "evidence_envelopes": [{"data_state": "data"}],
    }
    report = web_app.fastmoss_data_first_fallback(
        "| 商品编号 | 1729421229342823356 |\n| 佣金率 | 8% |\n| 首3日 GMV | 1234 |",
        manifest,
        "finish_reason=length",
    )
    assert "深度分析未完成" in report
    assert "1729421229342823356" in report
    assert "佣金率" in report and "首3日 GMV" in report


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
            }, {
                "source_tool": "fastmoss__product_overview", "dimension": "product_sample",
                "scope": "segment_head", "products": [{
                    "product_id": "1730898744848192092", "day28_units_sold": 424,
                }],
            }],
        },
    }])
    route = {"playbook": "product", "task_depth": "workflow", "entity": "mini grinder"}
    style = web_app.fastmoss_report_style_instruction(route)
    assert "标题、章节名称和顺序完全由你" in style
    assert "完整调研报告" in style and "可以自由提出执行建议" in style
    assert web_app.fastmoss_report_style_instruction({"task_depth": "lookup"}) == ""

    class FakeResponse:
        finish_reason = "stop"

        def __init__(self, decision=None):
            self.decision = decision or {"approved": True, "risk_level": "low", "edits": []}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(self.decision)}, "finish_reason": self.finish_reason}]}

    class FakeRequests:
        @staticmethod
        def post(*_args, **kwargs):
            payload = json.loads(kwargs["data"].decode("utf-8"))
            claims = json.loads(payload["messages"][1]["content"])["candidate_claims"]
            edits = []
            for claim in claims:
                if "低竞争市场" in claim["text"]:
                    edits.append({
                        "claim_id": claim["claim_id"],
                        "replacement": "本轮样本显示该细分仍需补充竞争强度证据。",
                        "reason": "当前样本不能证明低竞争",
                        "evidence_refs": ["call:1"],
                    })
                elif "首批500-1000件" in claim["text"]:
                    edits.append({
                        "claim_id": claim["claim_id"],
                        "replacement": "具体库存、预算、达人合作和经营目标需结合实际成本与测试数据确定。",
                        "reason": "执行数字没有工具证据或用户输入",
                        "evidence_refs": [],
                    })
            return FakeResponse({
                "approved": not edits, "risk_level": "high" if edits else "low", "edits": edits,
            })

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
        # Execution recommendations are writing judgments, not observed facts.
        # A verifier edit without an exact evidence reference must not erase them.
        for recommendation in (
            "500-1000", "$2000", "3-5", "3–5", "2周", "2.5", "MOQ建议不超过500",
            "$29.99", "$39.99", "$13–$16", "$14.99", "1–2个月", "月销1,000", "$5–$8",
        ):
            assert recommendation in edited, (recommendation, edited)

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
            draft, message, "调研", route, ApprovedRequests, "key", "https://example.test/v1", "model"
        )
        assert approved == draft

        detailed_report = (
            "# TikTok Shop 调研报告\n\n"
            "**结论先行：** 当前证据支持继续比较。\n\n"
            "## 4. 代表商品与趋势\n\n"
            "### 商品A\n\n- **定价**：$80.23–$85.58\n\n"
            "### 商品B\n\n- **定价**：$34.99\n\n"
            "## 7. 风险与验证顺序\n\n保留样本边界。"
        )
        preserved = web_app.verify_fastmoss_final_answer(
            detailed_report, message, "调研", route,
            ApprovedRequests, "key", "https://example.test/v1", "model",
        )
        assert preserved == detailed_report

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
            assert "500-1000" in kept and "$2000" in kept and "$29.99" in kept
            assert "$13–$16" in kept and "$14.99" in kept and "3–5" in kept
            assert "1–2个月" in kept and "$5–$8" in kept

        system_only = SimpleNamespace(tool_calls=[], tool_results=[{
            "tool_name": "system__current_time",
            "result": {"ok": True, "data_state": "data", "evidence_observed": True, "summary": "2026-07-17"},
        }])

        class MustNotCallRequests:
            @staticmethod
            def post(*_args, **_kwargs):
                raise AssertionError("verifier must not run without FastMoss evidence")

        no_evidence = web_app.verify_fastmoss_final_answer(
            draft, system_only, "调研", route, MustNotCallRequests, "key", "https://example.test/v1", "model"
        )
        assert "本轮没有取得可用于商业判断的 FastMoss 业务数据" in no_evidence
        assert "## 先说结论" not in no_evidence
        system_manifest = web_app.fastmoss_evidence_manifest(system_only, "调研", route)
        assert system_manifest["quality_states"]["data"] == []
        assert system_manifest["evidence_fact_count"] == 0
    finally:
        web_app.record_api_call = original_record


def test_fastmoss_all_workflow_tools_emit_supported_envelopes() -> None:
    assert len(web_app.FASTMOSS_SUPPORTED_EVIDENCE_TOOLS) == 54
    assert web_app.FASTMOSS_SUPPORTED_EVIDENCE_TOOLS == web_app.FASTMOSS_CURRENT_TOOL_NAMES
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


def test_fastmoss_verifier_batches_claims_and_isolates_failures() -> None:
    message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__market_category_ranking",
        "result": {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_envelope": {
                "source_tool": "fastmoss__market_category_ranking", "metric_grain": "market_category_ranking",
                "tool_family": "category", "parser_status": "supported", "data_state": "data",
                "entity_refs": [{"type": "category", "id": "844168"}],
            },
            "evidence_facts": [{
                "source_tool": "fastmoss__market_category_ranking", "data_state": "data",
                "dimension": "category_channel_ranking", "categories": [{
                    "category_id": 844168, "category_name": "厨房家电", "rank": 2,
                    "category_units_sold": 82995,
                }],
            }],
        },
    }])
    draft = "# 判断\n" + "\n".join(
        f"判断{index}：该市场已经进入成长期，是低竞争蓝海。" for index in range(1, 12)
    )

    class Response:
        def __init__(self, body: dict):
            self.body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.body

    class Requests:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def post(self, _url: str, **kwargs):
            payload = json.loads(kwargs["data"].decode("utf-8"))
            self.payloads.append(payload)
            batch = json.loads(payload["messages"][1]["content"])["candidate_claims"]
            if len(self.payloads) == 1:
                decision = {
                    "approved": False, "risk_level": "high", "edits": [{
                        "claim_id": batch[0]["claim_id"], "replacement": "判断1：竞争强度仍需验证。",
                        "reason": "样本不能证明低竞争", "evidence_refs": ["fm-c1-f1"],
                    }],
                }
                return Response({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision)}}]})
            if len(self.payloads) == 2:
                return Response({"choices": [{"finish_reason": "length", "message": {"content": ""}}]})
            return Response({"choices": [{"finish_reason": "stop", "message": {"content": "{invalid"}}]})

    requests = Requests()
    original_record = web_app.record_api_call
    web_app.record_api_call = lambda *_args, **_kwargs: None
    try:
        result = web_app.verify_fastmoss_final_answer(
            draft, message, "调研厨房家电", {"playbook": "product", "task_depth": "workflow"},
            requests, "key", "https://example.test/v1", "model",
        )
    finally:
        web_app.record_api_call = original_record
    assert len(requests.payloads) == 3
    assert all(len(json.loads(item["messages"][1]["content"])["candidate_claims"]) <= 5 for item in requests.payloads)
    assert all(item["max_tokens"] == 4800 for item in requests.payloads)
    assert all(len(json.dumps(item, ensure_ascii=False)) < 20000 for item in requests.payloads)
    assert "判断1：竞争强度仍需验证" in result
    for index in range(2, 12):
        assert f"判断{index}：" in result

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
        "evidence_fact_count": 2,
        "unsupported_parser_count": 0,
        "category_head": {"target_pages": 3, "completed_pages": [1], "reported_total": 20, "fetched_unique": 10, "coverage_complete": False},
        "segment_head": {"queries": {}, "fetched_unique": 0},
        "entity_bundles": [],
        "evidence_facts": [{
            "dimension": "category_trend", "data_state": "data",
            "source_tool": "fastmoss__market_category_analysis",
        }, {
            "fact_id": "fm-c2-f1", "dimension": "shop_data_trends", "data_state": "data",
            "source_call_index": 2, "source_tool": "fastmoss__shop_data_trends",
            "entity_type": "shop", "entity_id": "7495123456789012345",
            "payload": {"summary": {"period_gmv": 1200}},
        }],
        "derived_facts": [], "conflicts": [], "limitations": [],
    }
    packets = {
        playbook: web_app.fastmoss_report_packet(manifest, {"playbook": playbook})
        for playbook in ("product", "pricing", "competitor", "shop", "creator", "content_dissect", "content_strategy")
    }
    assert all(
        packet["available_dimensions"] == ["category_trend", "shop_data_trends"]
        for packet in packets.values()
    )
    assert all(
        any(item.get("dimension") == "shop_data_trends" for item in packet["supporting_evidence"])
        for packet in packets.values()
    )
    assert all("suggested_modules" not in packet and "claim_boundaries" not in packet for packet in packets.values())
    assert all(packet["numeric_policy"] == "only_tool_evidence_user_input_or_explicit_calculation" for packet in packets.values())
    integrity = web_app.fastmoss_report_integrity_stats("店铺趋势和类目市场均已覆盖。", manifest)
    assert integrity["used_dimensions"] == ["category_trend", "shop_data_trends"]


def test_fastmoss_list_truncation_is_explicit_and_invalid_entities_are_filtered() -> None:
    products = [{
        "product": {"product_id": str(1000 + index), "title": f"Product {index}"},
        "sales_summary": {"last_28d_units_sold": index},
    } for index in range(1, 11)]
    normalized = web_app.annotate_fastmoss_tool_result(
        "fastmoss__product_search",
        {
            "filter": {"category_id_level2": 844168, "category_id_level3": 935176},
            "page": 2, "pagesize": 10,
        },
        {"ok": True, "data_state": "data", "evidence_observed": True},
        {"data": {"content": [{"text": json.dumps({"total": 2000, "list": products})}]}},
    )
    fact = normalized["evidence_facts"][0]
    assert fact["returned_count"] == 10
    assert fact["included_count"] == 10
    assert fact["omitted_count"] == 0 and fact["truncated"] is False
    assert fact["returned_day28_units_sold_sum"] == 55

    envelope = dict(normalized["evidence_envelope"])
    envelope["source_call_index"] = 2
    envelope["scope"] = "category_head"
    manifest = {
        "quality_states": {"data": ["fastmoss__product_search"], "empty": [], "error": []},
        "evidence_envelope_count": 1, "evidence_fact_count": 1, "unsupported_parser_count": 0,
        "category_head": {"target_pages": 3, "completed_pages": [2], "reported_total": 2000, "fetched_unique": 10},
        "segment_head": {"queries": {}, "fetched_unique": 0},
        "entity_bundles": [], "analysis_targets": [], "evidence_envelopes": [envelope],
        "evidence_facts": [{**fact, "scope": "category_head", "page": 2}],
        "derived_facts": [], "conflicts": [], "limitations": [],
    }
    packet = web_app.fastmoss_report_packet(manifest, {"playbook": "product"})
    compact = packet["supporting_evidence"][0]
    assert compact["returned_count"] == 10
    assert compact["included_count"] == 2
    assert compact["omitted_count"] == 8 and compact["truncated"] is True
    assert [item["product_id"] for item in compact["products"]] == ["1001", "1010"]
    assert packet["source_catalog"][0]["arguments"]["page"] == 2
    assert packet["source_catalog"][0]["arguments"]["filter"] == {
        "category_id_level2": 844168, "category_id_level3": 935176,
    }

    segment_manifest = dict(manifest)
    segment_manifest["evidence_facts"] = [{
        **fact, "scope": "segment_head", "page": 1, "query": "Mini Meat Grinder",
    }]
    segment_packet = web_app.fastmoss_report_packet(segment_manifest, {"playbook": "product"})
    segment_compact = segment_packet["supporting_evidence"][0]
    assert segment_compact["included_count"] == 10
    assert segment_compact["omitted_count"] == 0 and segment_compact["truncated"] is False

    refs = web_app._fastmoss_entity_refs(
        {"filter": {"category_id_level2": 844168, "category_id_level3": 0, "product_id": "0"}},
        {"category_id": "0", "product_id": "1730898744848192092"},
    )
    assert {tuple(sorted(item.items())) for item in refs} == {
        tuple(sorted({"type": "category", "id": "844168"}.items())),
        tuple(sorted({"type": "product", "id": "1730898744848192092"}.items())),
    }


def test_fastmoss_22_call_semantic_registry_fixture() -> None:
    """Compact regression fixture derived from the 22-call product workflow."""
    calls: list[dict] = []
    results: list[dict] = []

    def add(name: str, arguments: dict, result: dict) -> None:
        calls.append({"function": {"name": f"fastmoss__{name}", "arguments": json.dumps(arguments)}})
        results.append({"tool_name": f"fastmoss__{name}", "result": result})

    def envelope(name: str, state: str, arguments: dict, returned: int | None = None, total: int | None = None) -> dict:
        item = {
            "source_tool": f"fastmoss__{name}", "metric_grain": name,
            "tool_family": web_app._fastmoss_tool_family(name), "parser_status": "supported",
            "data_state": state, "arguments": arguments,
            "entity_refs": web_app._fastmoss_entity_refs(arguments, None),
        }
        if returned is not None:
            item["returned_count"] = returned
        if total is not None:
            item["reported_total"] = total
        return item

    def product(product_id: str, units: int, title: str = "Sample") -> dict:
        return {
            "product_id": product_id, "title": title,
            "day28_units_sold": units, "day28_gmv": units * 20,
            "price_min": 18, "price_max": 22,
        }

    category_ids = [str(1730000000000000000 + index) for index in range(30)]
    segment_ids = category_ids[:2] + [str(1740000000000000000 + index) for index in range(18)]
    top_ids = category_ids[:1] + [str(1750000000000000000 + index) for index in range(9)]
    new_ids = [str(1760000000000000000 + index) for index in range(10)]

    arguments = {"keywords": "料理机"}
    add("search_category_by_words", arguments, {
        "ok": True, "data_state": "data", "evidence_observed": True,
        "evidence_envelope": envelope("search_category_by_words", "data", arguments, 2),
        "evidence_facts": [{
            "source_tool": "fastmoss__search_category_by_words", "data_state": "data",
            "dimension": "category_candidates", "categories": [
                {"category_id_level3": 935176, "cn_name": "料理机", "score": 0.5023},
                {"category_id_level3": 934920, "cn_name": "垃圾处理器", "score": 0.4891},
            ],
        }],
    })
    for analysis_type in ("basic_metrics", "sales_trends", "price_distribution"):
        arguments = {"filter": {"category_id": 844168}, "analysis_type": analysis_type}
        if analysis_type != "sales_trends":
            add("market_category_analysis", arguments, {
                "ok": True, "data_state": "empty", "evidence_observed": False,
                "evidence_metadata": {"scope": "supporting", "category_level": "L2", "category_id": 844168},
                "evidence_envelope": {
                    **envelope("market_category_analysis", "empty", arguments, 0),
                    "entity_refs": [{"type": "category", "id": "844168"}],
                },
            })
        else:
            add("market_category_analysis", arguments, {
                "ok": True, "data_state": "data", "evidence_observed": True,
                "evidence_envelope": envelope("market_category_analysis", "data", arguments, 16),
                "evidence_facts": [{
                    "source_tool": "fastmoss__market_category_analysis", "data_state": "data",
                    "dimension": "category_trend", "category_id": 844168, "entity_type": "category", "entity_id": "844168",
                    "summary_metrics": {"category_units_sold_total": 437785, "selling_video_count_total": 63822},
                }],
            })
    arguments = {"filter": {"category_id": 13}, "page": 1}
    add("market_category_ranking", arguments, {
        "ok": True, "data_state": "data", "evidence_observed": True,
        "evidence_envelope": envelope("market_category_ranking", "data", arguments, 4),
        "evidence_facts": [{
            "source_tool": "fastmoss__market_category_ranking", "data_state": "data",
            "dimension": "category_channel_ranking", "categories": [{
                "category_id": 844168, "category_name": "厨房家电", "rank": 2,
                "category_units_sold": 82995, "category_units_sold_yoy_percent": -15.57,
                "category_gmv_yoy_percent": -25.66,
                "channel_gmv_share": {"video_gmv_share_percent": 51.46},
            }],
        }],
    })

    for name, ids in (("product_rank_top_selling", top_ids), ("product_rank_new_listed", new_ids)):
        arguments = {"filter": {"category_id": 935176}, "page": 1, "pagesize": 10}
        dimension = "top_products" if name.endswith("top_selling") else "new_products"
        products = [product(product_id, 100 - index) for index, product_id in enumerate(ids)]
        add(name, arguments, {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_envelope": envelope(name, "data", arguments, 10),
            "evidence_facts": [{"source_tool": f"fastmoss__{name}", "data_state": "data", "dimension": dimension, "products": products, "returned_count": 10}],
            "evidence_product_records": products,
        })

    search_groups = [category_ids[:10], category_ids[10:20], category_ids[20:30], segment_ids[:10], segment_ids[10:20]]
    search_queries = ["", "", "", "Electric Food Shredder", "Mini Meat Grinder"]
    for page_index, (ids, query) in enumerate(zip(search_groups, search_queries), start=1):
        page = page_index if not query else 1
        arguments = {"filter": {"category_path": [13, 844168, 935176]}, "page": page, "pagesize": 10}
        if query:
            arguments["keywords"] = query
        products = [product(product_id, 30 - index, query or "Category product") for index, product_id in enumerate(ids)]
        scope = "segment_head" if query else "category_head"
        add("product_search", arguments, {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_metadata": {"scope": scope, "page": page, "query": query or None, "reported_total": 2000 if not query else 20, "fetched_records": 10, "data_state": "data"},
            "evidence_envelope": {**envelope("product_search", "data", arguments, 10, 2000 if not query else 20), "scope": scope},
            "evidence_facts": [{"source_tool": "fastmoss__product_search", "data_state": "data", "dimension": "product_sample", "scope": scope, "page": page, "query": query or None, "products": products, "returned_count": 10}],
            "evidence_product_records": products,
        })

    representatives = [(segment_ids[2], "Mini Meat Grinder"), (segment_ids[3], "Electric Food Shredder")]
    for index, (product_id, title) in enumerate(representatives):
        arguments = {"filter": {"product_id": product_id}}
        card_gmv, card_units = ((34, 33) if index == 0 else (91, 92))
        add("product_overview", arguments, {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_envelope": envelope("product_overview", "data", arguments, 28),
            "evidence_facts": [{
                "source_tool": "fastmoss__product_overview", "data_state": "data", "dimension": "product_overview",
                "product_id": product_id, "entity_type": "product", "entity_id": product_id,
                "channel_distribution": {"breakdown": [{
                    "sales_channel": "product_card", "gmv_share_percent": card_gmv,
                    "units_sold_share_percent": card_units,
                }]},
            }],
        })
    for product_id, _title in representatives:
        arguments = {"filter": {"product_id": product_id, "time_range_days": 90}}
        add("product_sales_trend", arguments, {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_envelope": envelope("product_sales_trend", "data", arguments, 90),
            "evidence_facts": [{
                "source_tool": "fastmoss__product_sales_trend", "data_state": "data", "dimension": "product_90d_trend",
                "product_id": product_id, "entity_type": "product", "entity_id": product_id,
                "trend_summary": {"days_returned": 90, "total_units_sold": 1716 if product_id == representatives[0][0] else 31},
            }],
        })
    for product_id, _title in representatives:
        arguments = {"filter": {"product_id": product_id}}
        add("product_review_list", arguments, {
            "ok": True, "data_state": "empty", "evidence_observed": False,
            "evidence_envelope": envelope("product_review_list", "empty", arguments, 0),
        })
    for index, (product_id, _title) in enumerate(representatives):
        arguments = {"filter": {"product_id": product_id}, "page": 1}
        creators = [{
            "creator_uid": str(1770000000000000000 + index * 10), "creator_name": "Apex",
            "creator_category": {"name": "Shopping & Retail"},
            "product_contribution": {"product_linked_video_count": 670},
        }, {
            "creator_uid": str(1770000000000000001 + index * 10), "creator_name": "T ao",
            "creator_category": {"name": "Other"},
            "product_contribution": {"product_linked_video_count": 14},
        }]
        add("product_creator_analysis", arguments, {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_envelope": envelope("product_creator_analysis", "data", arguments, 2, 3),
            "evidence_facts": [{
                "source_tool": "fastmoss__product_creator_analysis", "data_state": "data", "dimension": "product_creator_analysis",
                "product_id": product_id, "entity_type": "product", "entity_id": product_id,
                "reported_creator_total": 3, "returned_creators": 2,
                "creator_summary": {"creator_category_distribution": [{"creator_category": "Shopping & Retail", "creator_share_percent": 100}]},
                "top_creators": creators,
            }],
        })
    for product_id, _title in representatives:
        arguments = {"filter": {"product_id": product_id, "time_range_days": 90}, "page": 1}
        add("product_video_list", arguments, {
            "ok": True, "data_state": "data", "evidence_observed": True,
            "evidence_envelope": envelope("product_video_list", "data", arguments, 10, 146),
            "evidence_facts": [{
                "source_tool": "fastmoss__product_video_list", "data_state": "data", "dimension": "product_videos",
                "product_id": product_id, "entity_type": "product", "entity_id": product_id,
                "reported_video_total": 146, "returned_videos": 10, "time_range_days": 90,
                "top_videos": [],
            }],
        })

    assert len(calls) == len(results) == 22
    manifest = web_app.fastmoss_evidence_manifest(
        SimpleNamespace(tool_calls=calls, tool_results=results),
        "Electric Food Shredder 和 Mini Meat Grinder 调研",
        {"playbook": "product", "entity": "Electric Food Shredder / Mini Meat Grinder"},
    )
    coverage = manifest["coverage_summary"]
    registry = manifest["metric_registry"]
    assert manifest["evidence_envelope_count"] == 22
    assert manifest["evidence_fact_count"] == 18
    assert manifest["unsupported_parser_count"] == 0
    assert coverage["category_search"]["returned_rows"] == 30
    assert coverage["category_search"]["unique_products"] == 30
    assert coverage["all_product_search_calls"] == {"returned_rows": 50, "unique_products": 48}
    assert coverage["all_product_list_calls"] == {"returned_rows": 70, "unique_products": 67}
    assert all(item["entity_refs"] == [{"type": "category", "id": "844168"}] for item in coverage["exact_empty_results"][:2])

    def metric(metric_name: str, entity_id: str) -> float:
        return next(item["value"] for item in registry if item["metric"] == metric_name and item["entity_id"] == entity_id)

    assert metric("category_units_sold_yoy_percent", "844168") == -15.57
    assert metric("category_gmv_yoy_percent", "844168") == -25.66
    assert metric("channel_distribution.product_card.gmv_share_percent", representatives[1][0]) == 91
    assert metric("channel_distribution.product_card.units_sold_share_percent", representatives[1][0]) == 92
    assert metric("score", "935176") == 0.5023
    assert metric("score", "934920") == 0.4891
    assert metric("product_contribution.product_linked_video_count", "1770000000000000000") == 670
    assert metric("product_contribution.product_linked_video_count", "1770000000000000001") == 14
    assert any(item.get("conflict_type") == "summary_vs_returned_top_rows" for item in manifest["conflicts"])
    assert not any("11.7" in json.dumps(item, ensure_ascii=False) for item in manifest["derived_facts"])

    packet = web_app.fastmoss_report_packet(manifest, {"playbook": "product"})
    packet_facts = [
        item for item in packet["supporting_evidence"] if isinstance(item, dict)
    ] + [
        item for target in packet["target_evidence"] if isinstance(target, dict)
        for item in (target.get("facts") or []) if isinstance(item, dict)
    ]
    packet_dimensions = {item.get("dimension") for item in packet_facts}
    assert {
        "category_trend", "category_channel_ranking", "product_overview", "product_90d_trend",
    }.issubset(packet_dimensions)
    segment_included_by_fact: dict[str, int] = {}
    for item in packet_facts:
        if item.get("dimension") != "product_sample" or item.get("scope") != "segment_head":
            continue
        fact_id = str(item.get("fact_id") or "")
        segment_included_by_fact[fact_id] = (
            segment_included_by_fact.get(fact_id, 0) + int(item.get("included_count") or 0)
        )
    assert segment_included_by_fact and all(
        included == 10 for included in segment_included_by_fact.values()
    ), segment_included_by_fact
    target_ids = {
        target["entity_id"] for target in packet["target_evidence"] if isinstance(target, dict)
    }
    assert target_ids
    for target in packet["target_evidence"]:
        target_id = target["entity_id"]
        assert target["facts"], target
        for fact in target["facts"]:
            listed_ids = {
                str(product.get("product_id") or "")
                for product in (fact.get("products") or []) if isinstance(product, dict)
            }
            assert not listed_ids or listed_ids == {target_id}, (target_id, fact)
            if fact.get("product_id"):
                assert str(fact["product_id"]) == target_id, (target_id, fact)
            assert target_id in str(fact.get("entity_fact_ref") or "")
    assert all(
        not {
            str(product.get("product_id") or "")
            for product in (fact.get("products") or []) if isinstance(product, dict)
        }.intersection(target_ids)
        for fact in packet["supporting_evidence"] if isinstance(fact, dict)
    )

    # A category claim and a product claim may share one verifier HTTP batch,
    # but their evidence must remain isolated by claim_id.  The old batch-wide
    # entity union removed call 5 from the category claim and caused valid L2
    # metrics to be rewritten as "not returned".
    category_claim = {
        "claim_id": "line-category", "entity_ids": [],
        "reasons": ["metric_period", "cross_period_ratio"],
        "text": "上级类目厨房家电GMV同比下降25.66%，销量同比下降15.57%，视频GMV占比51.46%。",
    }
    product_claim = {
        "claim_id": "line-product", "entity_ids": [representatives[0][0]],
        "reasons": ["lifecycle"],
        "text": f"{representatives[0][0]} 已进入生命周期衰退期。",
    }
    isolated = web_app.fastmoss_candidate_evidence(
        manifest, [category_claim, product_claim], {"playbook": "product"}
    )
    slices = {item["claim_id"]: item for item in isolated["claim_evidence"]}
    category_slice = slices["line-category"]
    product_slice = slices["line-product"]
    assert any(
        item.get("dimension") == "category_channel_ranking"
        and int(item.get("source_call_index") or 0) == 5
        for item in category_slice["facts"]
    )
    assert not any(item.get("dimension") == "product_90d_trend" for item in category_slice["facts"])
    assert any(
        item.get("dimension") == "product_90d_trend"
        and item.get("product_id") == representatives[0][0]
        for item in product_slice["facts"]
    )
    original_category_text = category_claim["text"]
    guarded, applied = web_app.apply_fastmoss_verifier_edits(
        original_category_text,
        [{
            "claim_id": "line-category",
            "replacement": "厨房家电相关指标在证据中未返回。",
            "reason": "误判为缺少证据",
            "evidence_refs": ["call:5"],
        }],
        [category_claim, product_claim],
        isolated,
    )
    assert guarded == original_category_text and applied == 0

    lifecycle_claims = web_app.fastmoss_high_risk_claims(
        f"{representatives[1][0]} 已进入生命周期衰退期。"
    )
    lifecycle_evidence = web_app.fastmoss_candidate_evidence(
        manifest, lifecycle_claims, {"playbook": "product"}
    )
    assert any(
        item.get("dimension") == "product_90d_trend"
        and item.get("product_id") == representatives[1][0]
        for item in lifecycle_evidence["facts"]
    )
    assert any(
        str(item.get("metric") or "").startswith("trend_summary.")
        and item.get("entity_id") == representatives[1][0]
        for item in lifecycle_evidence["metric_registry"]
    )
    content_claims = web_app.fastmoss_high_risk_claims(
        f"{representatives[0][0]} 的达人视频是销量的核心驱动力，"
        "返回样本播放248次、26次点赞。"
    )
    assert content_claims and "content_causality" in content_claims[0]["reasons"]
    content_evidence = web_app.fastmoss_candidate_evidence(
        manifest, content_claims, {"playbook": "product"}
    )
    assert any(item.get("dimension") == "product_videos" for item in content_evidence["facts"])
    assert len(json.dumps(content_evidence, ensure_ascii=False)) < 40000

def test_fastmoss_dossier_synthesis_preserves_complete_tool_evidence() -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{
                    "finish_reason": "stop",
                "message": {"content": json.dumps({
                    "overall_judgement": "证据包已独立合成。",
                    "recommend": [{
                        "candidate": "Product 1", "reason": "样本与趋势证据齐备", "counter_evidence": "评论为空",
                        "product_form": "", "price_band": "", "entry_conditions": "补齐评论",
                        "validation_actions": ["核对详情"], "evidence_refs": ["call:1", "call:2"],
                    }], "watch": [], "reject": [], "can_support": [], "cannot_support": [], "next_actions": [],
                }, ensure_ascii=False)},
                }],
            }

    class Requests:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def post(self, _url: str, **kwargs):
            self.payloads.append(json.loads(kwargs["data"].decode("utf-8")))
            return Response()

    requests = Requests()
    trend_rows = [
        {"date": f"2026-06-{day:02d}", "units_sold": day}
        for day in range(1, 31)
    ] + [{"date": "2026-07-01", "units_sold": 31}]
    products = [{
        "product_id": str(1730000000000000000 + index),
        "title": f"Product {index}",
        "category": {"l3": {"id": 935176 if index < 9 else 934920, "name": "Food Processors" if index < 9 else "Other"}},
        "current_price": 10 + index,
    } for index in range(10)]
    calls = [
        {"id": "c1", "function": {"name": "fastmoss__product_sales_trend", "arguments": json.dumps({"filter": {"product_id": "1730000000000000001", "time_range_days": 90}})}},
        {"id": "c2", "function": {"name": "fastmoss__product_rank_new_listed", "arguments": json.dumps({"filter": {"category_l1_id": 13, "category_l2_id": 844168, "category_l3_id": 935176}, "page": 1, "pagesize": 10})}},
        {"id": "c3", "function": {"name": "fastmoss__product_review_list", "arguments": json.dumps({"filter": {"product_id": "1730000000000000001"}, "page": 1})}},
    ]
    results = [
        {"tool_name": "fastmoss__product_sales_trend", "result": {"ok": True, "data_state": "data", "mcp_data": {"trend_series": trend_rows}}},
        {"tool_name": "fastmoss__product_rank_new_listed", "result": {"ok": True, "data_state": "data", "mcp_data": {"list": products, "total": 10}}},
        {"tool_name": "fastmoss__product_review_list", "result": {"ok": True, "data_state": "empty", "mcp_data": {"list": [], "total": 0}}},
    ]
    original_verify = web_app.verify_fastmoss_final_answer
    web_app.verify_fastmoss_final_answer = lambda draft, *_args, **_kwargs: draft
    try:
        result = web_app.synthesize_fastmoss_report_from_packet(
            SimpleNamespace(tool_calls=calls, tool_results=results),
            "做一份完整产品调研",
            {"playbook": "product", "task_depth": "workflow"},
            requests,
            "test-key",
            "https://example.invalid/v1",
            "test-model",
        )
    finally:
        web_app.verify_fastmoss_final_answer = original_verify
    assert result.startswith("## 完整数据表")
    assert result.count(web_app.FASTMOSS_REPORT_NOTICE) == 1
    assert len(requests.payloads) == 1
    payload = requests.payloads[0]
    assert "tools" not in payload
    assert payload["max_tokens"] == 3600
    assert [message["role"] for message in payload["messages"]] == ["system", "system", "user"]
    base_system = payload["messages"][0]["content"]
    report_system = payload["messages"][1]["content"]
    semantic_content = payload["messages"][2]["content"]
    report_date = re.search(r"当前日期（Asia/Shanghai）：(\d{4}-\d{2}-\d{2})", base_system).group(1)
    assert base_system.startswith(web_app.fastmoss_report_system_instruction(report_date))
    assert base_system != web_app.sellersprite_report_system_instruction(report_date)
    assert "只负责做选品决策" in base_system
    assert "零关联或未返回不能推导私域、投流、刷单" in base_system
    assert "fastmoss__" not in base_system
    assert "sellersprite__" not in base_system
    assert report_system == web_app.fastmoss_report_prompt_instruction(
        {"playbook": "product", "task_depth": "workflow"}
    )
    assert "完整工具结果是报告的事实素材" in report_system
    assert "evidence_index：{" not in report_system
    assert "不得写成流量来源、因果、效率或生命周期结论" in report_system
    assert "事实边界优先于叙事完整性" in report_system
    assert "不得仅凭这些数据写成核心驱动力、决定性因素或直接原因" in report_system
    assert '"type":"fastmoss_decision_packet_v3"' in semantic_content
    assert '"reference_catalog"' in semantic_content
    assert "1730000000000000001" in semantic_content
    assert "关键词返回量不是市场容量" in semantic_content


def test_fastmoss_report_evidence_is_semantic() -> None:
    dossier = {
        "workflow": "product",
        "report_date": "2026-07-19",
        "tool_evidence": [{
            "source_ref": "call:1",
            "tool_name": "fastmoss__product_search",
            "arguments": {"keywords": "Mini Meat Grinder", "filter": {"region": "US"}},
            "evidence_fence": {"data_state": "data", "returned_count": 1},
            "business_data": {
                "total": 1,
                "list": [{"product_id": "1730000000000000001", "title": "Mini Grinder"}],
            },
        }],
        "hard_fact_boundaries": {"rules": ["空结果只适用于精确参数"]},
    }
    semantic, semantic_stats = web_app.fastmoss_render_report_evidence(dossier)
    assert "## Mini Meat Grinder · 商品搜索样本" in semantic
    assert "call:1" not in semantic
    assert "fastmoss__product_search" not in semantic
    assert "Mini Grinder" in semantic
    assert semantic_stats["format"] == "semantic"
    assert semantic_stats["registered_tool_count"] == 1


def test_fastmoss_v3_length_retries_without_losing_real_rows() -> None:
    class Response:
        def __init__(self, body: dict) -> None:
            self.body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.body

    decision = {
        "overall_judgement": "样本存在可继续验证的候选，但不能据此外推全市场。",
        "recommend": [{
            "candidate": "Stress Ball", "reason": "销量和价格字段均已返回", "counter_evidence": "仍缺少完整成本",
            "product_form": "解压球", "price_band": "$8-$12", "entry_conditions": "完成成本核验",
            "validation_actions": ["核对同形态竞品"], "evidence_refs": ["call:1"],
        }],
        "watch": [], "reject": [], "can_support": ["可继续验证"],
        "cannot_support": ["不能判断利润"], "next_actions": ["补齐成本和内容证据"],
    }

    class Requests:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def post(self, _url: str, **kwargs):
            self.payloads.append(json.loads(kwargs["data"].decode("utf-8")))
            if len(self.payloads) <= 2:
                return Response({"choices": [{"finish_reason": "length", "message": {"content": "{\"partial\":"}}]})
            return Response({"choices": [{"message": {"content": json.dumps(decision, ensure_ascii=False)}}]})

    calls = [{"id": "c1", "function": {"name": "fastmoss__product_search", "arguments": json.dumps({"keywords": "stress relief toys", "page": 1})}}]
    results = [{"tool_name": "fastmoss__product_search", "result": {"ok": True, "data_state": "data", "mcp_data": {
        "list": [{"product_id": "1729421229342823356", "title": "Stress Ball", "current_price": 9.99,
                  "commission_rate_percent": 8, "day28_units_sold": 17354, "first_3d_gmv": 1234}], "total": 1,
    }}}]
    requests = Requests()
    original_record = web_app.record_api_call
    web_app.record_api_call = lambda *_args, **_kwargs: None
    try:
        report = web_app.synthesize_fastmoss_report_from_packet(
            SimpleNamespace(tool_calls=calls, tool_results=results), "解压玩具选品", {"playbook": "product", "task_depth": "workflow"},
            requests, "test-key", "https://example.invalid/v1", "test-model",
        )
    finally:
        web_app.record_api_call = original_record
    assert len(requests.payloads) == 4
    assert "1729421229342823356" in report
    assert "佣金" in report and "上架后前3日成交金额" in report
    assert "深度分析未完成" not in report
    assert requests.payloads[0]["max_tokens"] == 3600
    assert requests.payloads[1]["max_tokens"] == 1800
    assert requests.payloads[2]["max_tokens"] == 1500


def test_fastmoss_v3_failure_falls_back_to_data_not_zero_products() -> None:
    class Requests:
        def post(self, _url: str, **_kwargs):
            raise TimeoutError("controlled high reasoning timeout")

    calls = [{"id": "c1", "function": {"name": "fastmoss__product_search", "arguments": json.dumps({"keywords": "stress relief toys"})}}]
    results = [{"tool_name": "fastmoss__product_search", "result": {"ok": True, "data_state": "data", "mcp_data": {
        "list": [{"product_id": "1729421229342823356", "title": "Stress Ball", "current_price": 9.99,
                  "commission_rate_percent": 8, "day28_units_sold": 17354}], "total": 1,
    }}}]
    report = web_app.synthesize_fastmoss_report_from_packet(
        SimpleNamespace(tool_calls=calls, tool_results=results), "解压玩具选品", {"playbook": "product", "task_depth": "workflow"},
        Requests(), "test-key", "https://example.invalid/v1", "test-model",
    )
    assert "深度分析未完成" in report
    assert "1729421229342823356" in report
    assert "0 商品" not in report


def test_fastmoss_analytical_answers_use_single_semantic_report_path() -> None:
    message = SimpleNamespace(tool_calls=[], tool_results=[{
        "tool_name": "fastmoss__market_category_ranking",
        "result": {"ok": True, "data_state": "data", "mcp_data": {"list": [{"id": "sample"}]}},
    }])
    calls: list[str] = []
    original_synthesize = web_app.synthesize_fastmoss_report_from_packet
    original_finalize = web_app.finalize_fastmoss_answer
    web_app.synthesize_fastmoss_report_from_packet = (
        lambda *_args, **_kwargs: calls.append("semantic") or "semantic report"
    )
    web_app.finalize_fastmoss_answer = (
        lambda draft, *_args, **_kwargs: calls.append("direct") or f"direct: {draft}"
    )
    try:
        analytical = web_app.complete_fastmoss_answer(
            "ignored draft", message, "调研最近热门新品",
            {"intent": "product_research", "task_depth": "analysis", "playbook": "product"},
            object(), "key", "https://example.invalid/v1", "deepseek-v4-pro-test",
        )
        assert analytical == "semantic report"
        assert calls == ["semantic"]

        calls.clear()
        direct = web_app.complete_fastmoss_answer(
            "direct answer", message, "这个商品是否存在",
            {"intent": "product_availability", "task_depth": "lookup"},
            object(), "key", "https://example.invalid/v1", "deepseek-v4-pro-test",
        )
        assert direct == "semantic report"
        assert calls == ["semantic"]

        calls.clear()
        official = web_app.complete_fastmoss_answer(
            "official answer", message, "分析美区美妆市场",
            web_app.fastmoss_official_skill_route(),
            object(), "key", "https://example.invalid/v1", "deepseek-v4-pro-test",
        )
        assert official == "semantic report"
        assert calls == ["semantic"]
    finally:
        web_app.synthesize_fastmoss_report_from_packet = original_synthesize
        web_app.finalize_fastmoss_answer = original_finalize

    chat_source = inspect.getsource(web_app.run_chat_deepseek)
    assert "synthesize_fastmoss_report_from_packet(" not in chat_source
    assert "finalize_fastmoss_answer(" not in chat_source
    assert chat_source.count("complete_fastmoss_answer(") >= 5


def test_fastmoss_claim_ids_and_verifier_edit_guards() -> None:
    claims = web_app.fastmoss_high_risk_claims("结论\n这个市场已经是低竞争蓝海。")
    assert claims and claims[0]["claim_id"] == "line-2"
    edited, applied = web_app.apply_fastmoss_verifier_edits(
        "结论\n这个市场已经是低竞争蓝海。",
        [{"claim_id": "line-2", "replacement": "竞争强度仍需验证。", "reason": "样本不足", "evidence_refs": []}],
        claims,
    )
    assert applied == 0 and "低竞争蓝海" in edited
    referenced_edit, referenced_count = web_app.apply_fastmoss_verifier_edits(
        "结论\n这个市场已经是低竞争蓝海。",
        [{"claim_id": "line-2", "replacement": "竞争强度仍需验证。", "reason": "样本不足", "evidence_refs": ["call:1"]}],
        claims,
        {"source_catalog": [{"source_ref": "call:1", "source_call_index": 1}]},
    )
    assert referenced_count == 1 and "竞争强度仍需验证" in referenced_edit
    malformed_ref_edit, malformed_ref_count = web_app.apply_fastmoss_verifier_edits(
        "结论\n这个市场已经是低竞争蓝海。",
        [{"claim_id": "line-2", "replacement": "竞争强度仍需验证。", "reason": "样本不足", "evidence_refs": [{"source_ref": "call:1"}]}],
        claims,
        {"source_catalog": [{"source_ref": "call:1", "source_call_index": 1}]},
    )
    assert malformed_ref_count == 0 and "低竞争蓝海" in malformed_ref_edit

    entity_claims = [{
        "claim_id": "line-1",
        "text": "代表商品ID 1732363961421369948。",
        "reasons": ["multiple_entity_ids"],
        "entity_ids": ["1732363961421369948"],
    }]
    unchanged_entity, entity_edits = web_app.apply_fastmoss_verifier_edits(
        "代表商品ID 1732363961421369948。",
        [{
            "claim_id": "line-1",
            "replacement": "代表商品ID 1730898744848192092。",
            "reason": "改用销量更高商品",
            "evidence_refs": ["fm-c12-f1"],
        }],
        entity_claims,
    )
    assert entity_edits == 0 and "1732363961421369948" in unchanged_entity

    restored_id, restored_count = web_app.normalize_fastmoss_entity_id_abbreviations(
        "代表商品ID 1732363961。",
        {
            "entity_bundles": [{"entity_type": "product", "entity_id": "1732363961421369948"}],
            "analysis_targets": [],
        },
    )
    assert restored_id == "代表商品ID 1732363961421369948。" and restored_count == 1

    table_claims = [{
        "claim_id": "line-1", "text": "| 达人链接数 | 382 | 766 | 853 |",
        "reasons": ["operational_number"], "entity_ids": [],
    }]
    removed_table, table_edits = web_app.apply_fastmoss_verifier_edits(
        table_claims[0]["text"],
        [{"claim_id": "line-1", "replacement": "", "reason": "证据不足", "evidence_refs": []}],
        table_claims,
    )
    assert table_edits == 0 and removed_table == table_claims[0]["text"]
    kept_numbers, number_edits = web_app.apply_fastmoss_verifier_edits(
        "本轮返回10件商品。",
        [{"original": "本轮返回10件商品。", "replacement": "本轮返回20件商品。", "reason": "修正", "evidence_refs": []}],
    )
    assert number_edits == 0 and "10件" in kept_numbers
    corrected_numbers, corrected_number_edits = web_app.apply_fastmoss_verifier_edits(
        "本轮返回10件商品。",
        [{"original": "本轮返回10件商品。", "replacement": "本轮返回20件商品。", "reason": "修正", "evidence_refs": ["f1"]}],
        evidence_slice={"facts": [{"fact_id": "f1", "returned_count": 20}]},
    )
    assert corrected_number_edits == 1 and "20件" in corrected_numbers
    unbound_numbers, unbound_number_edits = web_app.apply_fastmoss_verifier_edits(
        "本轮返回10件商品。",
        [{"original": "本轮返回10件商品。", "replacement": "本轮返回20件商品。", "reason": "修正", "evidence_refs": []}],
        evidence_slice={"facts": [{"fact_id": "f1", "returned_count": 20}]},
    )
    assert unbound_number_edits == 0 and "10件" in unbound_numbers

    semantic_claims = web_app.fastmoss_high_risk_claims(
        "关键词返回量证明该细分市场容量很小，商业化程度低，不具备规模投入条件。\n"
        "该商品依靠广告投放实现爆发，说明产品生命周期已自然衰退。"
    )
    semantic_reasons = {reason for claim in semantic_claims for reason in claim["reasons"]}
    assert {"market_scale", "channel_causality", "lifecycle"}.issubset(semantic_reasons)
    fenced_claims = web_app.fastmoss_high_risk_claims(
        "TikTok Shop尚无直接对应品类，也没有找到精确匹配的活跃商品。\n"
        "返回的987条视频几乎全是广告内容，且已有近百万美金广告投入。"
    )
    fenced_reasons = {reason for claim in fenced_claims for reason in claim["reasons"]}
    assert {"state_claim", "sample_extrapolation", "channel_causality"}.issubset(fenced_reasons)
    metric_claims = web_app.fastmoss_high_risk_claims(
        "## 4. 代表商品与趋势\n| 维度 | 商品A |\n| 30天销量 | 5710 |\n"
        "商品A近30天销量为5710件。\nMae贡献了该商品90%+的GMV。"
    )
    assert all(not claim["text"].startswith("##") for claim in metric_claims)
    metric_reasons = {reason for claim in metric_claims for reason in claim["reasons"]}
    assert {"metric_period", "cross_period_ratio"}.issubset(metric_reasons)

    cross_entity_claims = web_app.fastmoss_high_risk_claims(
        "两款商品都来自同一品牌 SPZTJK。"
    )
    assert cross_entity_claims and "cross_entity_attribute" in cross_entity_claims[0]["reasons"]

    evidence_manifest = {
        "quality_states": {"data": ["fastmoss__product_search"], "empty": [], "error": []},
        "evidence_envelope_count": 1, "evidence_fact_count": 1, "unsupported_parser_count": 0,
        "category_head": {"target_pages": 3, "completed_pages": [1], "reported_total": 30, "fetched_unique": 10},
        "segment_head": {"queries": {}, "fetched_unique": 0},
        "entity_bundles": [], "analysis_targets": [], "evidence_envelopes": [],
        "evidence_facts": [{
            "source_tool": "fastmoss__product_search", "source_call_index": 1,
            "data_state": "data", "dimension": "product_sample", "scope": "category_head", "page": 1,
            "returned_count": 10,
            "products": [
                {"product_id": str(1730000000000000000 + index), "title": f"Product {index}"}
                for index in range(10)
            ],
        }],
        "metric_registry": [], "derived_facts": [], "conflicts": [], "limitations": [],
    }
    operational_evidence = web_app.fastmoss_candidate_evidence(
        evidence_manifest,
        [{"claim_id": "line-1", "text": "达人链接数为382。", "reasons": ["operational_number"], "entity_ids": []}],
        {"playbook": "product"},
    )
    category_fact = next(item for item in operational_evidence["facts"] if item.get("scope") == "category_head")
    assert len(category_fact["products"]) == 3 and category_fact["page"] == 1

def test_analytical_routes_use_report_model_and_fastmoss_preserves_pro_draft() -> None:
    assert web_app.chat_route_uses_report_model(
        "home", {"intent": "product_research", "task_depth": "analysis"}
    )
    assert web_app.chat_route_uses_report_model(
        "amazon", {"intent": "product_research", "task_depth": "analysis"}
    )
    assert not web_app.chat_route_uses_report_model(
        "amazon", {"intent": "product_availability", "task_depth": "lookup"}
    )
    assert web_app.chat_route_uses_report_model(
        "fastmoss", {"intent": "fastmoss_product", "task_depth": "workflow", "playbook": "product"}
    )
    assert web_app.chat_route_uses_report_model(
        "home", {"intent": "video_analysis", "task_depth": "lookup"}
    )
    assert not web_app.chat_route_uses_report_model(
        "fastmoss", {"intent": "product_availability", "task_depth": "lookup"}
    )
    assert not web_app.chat_route_uses_report_model(
        "home", {"intent": "help", "task_depth": "direct"}
    )

    old_report_model = os.environ.get("DEEPSEEK_REPORT_MODEL")
    old_verifier = os.environ.get("FASTMOSS_LLM_VERIFIER_ENABLED")
    original_manifest = web_app.fastmoss_evidence_manifest
    calls = []

    class Requests:
        def post(self, *_args, **_kwargs):
            calls.append(True)
            raise AssertionError("disabled verifier must not make another LLM request")

    web_app.fastmoss_evidence_manifest = lambda *_args, **_kwargs: {
        "quality_states": {"data": ["fastmoss__product_search"], "empty": [], "error": []},
        "evidence_fact_count": 1,
        "category_head": {},
        "segment_head": {},
        "entity_bundles": [],
        "evidence_envelopes": [],
        "evidence_facts": [{"fact_id": "fm-c1-f1", "data_state": "data"}],
        "derived_facts": [],
        "conflicts": [],
        "limitations": [],
    }
    os.environ["DEEPSEEK_REPORT_MODEL"] = "deepseek-v4-pro-test"
    os.environ["FASTMOSS_LLM_VERIFIER_ENABLED"] = "0"
    draft = "# 完整调研报告\n\n## 市场判断\n\n保留原始结构、表格和分析。"
    try:
        assert web_app.chat_report_model() == "deepseek-v4-pro-test"
        result = web_app.finalize_fastmoss_answer(
            draft,
            SimpleNamespace(tool_calls=[], tool_results=[]),
            "调研这个类目",
            {"task_depth": "workflow", "playbook": "product"},
            Requests(),
            "key",
            "https://example.test/v1",
            "deepseek-v4-pro-test",
        )
    finally:
        web_app.fastmoss_evidence_manifest = original_manifest
        if old_report_model is None:
            os.environ.pop("DEEPSEEK_REPORT_MODEL", None)
        else:
            os.environ["DEEPSEEK_REPORT_MODEL"] = old_report_model
        if old_verifier is None:
            os.environ.pop("FASTMOSS_LLM_VERIFIER_ENABLED", None)
        else:
            os.environ["FASTMOSS_LLM_VERIFIER_ENABLED"] = old_verifier
    assert result.startswith(draft + "\n\n---\n\n## 注意事项")
    assert result.count(web_app.FASTMOSS_REPORT_NOTICE) == 1
    assert web_app.append_fastmoss_report_notice(
        result, {"task_depth": "workflow", "playbook": "product"}
    ) == result
    assert web_app.append_fastmoss_report_notice(
        draft, {"task_depth": "lookup"}
    ) == draft
    assert calls == []


if __name__ == "__main__":
    test_fastmoss_official_skill_chain_loads_exact_package_and_isolates_tools()
    test_sellersprite_official_skill_chain_loads_full_bundle_and_isolates_tools()
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
    test_only_structured_direct_requests_bypass_intent_llm()
    test_disabling_intent_router_restores_legacy_rule_route()
    test_llm_research_task_is_authoritative_for_ambiguous_trend_phrases()
    test_invalid_llm_research_task_uses_structured_only_fallback()
    test_three_layer_research_task_rejects_goal_text_as_entity()
    test_three_layer_research_task_keeps_real_product_entity()
    test_fastmoss_workflow_phases_accept_empty_and_error_attempts()
    test_fastmoss_product_phase_requires_complete_sample_coverage()
    test_fastmoss_business_defaults_use_verified_category_levels()
    test_fastmoss_product_workflow_keeps_its_round_budget_isolated()
    test_fastmoss_clarification_is_targeted_and_provider_isolated()
    test_fastmoss_close_cross_category_matches_request_confirmation()
    test_provider_profiles_use_aggregated_sellersprite_and_staged_fastmoss_tools()
    test_sellersprite_semantic_registry_is_complete_and_lossless()
    test_semantic_brace_residue_is_naturalized_or_preserved_and_logged()
    test_semantic_brace_residue_python_mapping_is_naturalized()
    test_sellersprite_semantic_report_and_pro_synthesis()
    test_dynamic_provider_capability_graph_uses_task_scope_and_evidence()
    test_dynamic_provider_planner_does_not_cap_repeated_calls()
    test_llm_orchestration_exposes_full_provider_tools_and_keeps_hard_guards()
    test_region_default_only_applies_when_schema_supports_it()
    test_all_sites_disable_frontend_tool_selection()
    test_fixed_full_site_tool_sets_include_all_sociavault_tools()
    test_social_platform_routes_use_sociavault_without_rest_fallback()
    test_social_router_inherits_platform_before_legacy_intent_model()
    test_social_router_model_failures_fall_back_to_full_catalog()
    test_social_router_does_not_hijack_commerce_intents()
    test_fastmoss_deep_dive_ids_must_come_from_current_task()
    test_tool_call_signature_deduplicates_argument_order()
    test_lightweight_fastmoss_skill_uses_runtime_dates_and_semantic_deduplication()
    test_lightweight_fastmoss_skill_final_path_uses_semantic_report_without_draft_fallback()
    test_fastmoss_dual_ranking_plan_uses_three_sorted_category_pages_then_segments()
    test_fastmoss_product_phase_waits_for_all_category_and_segment_searches()
    test_fastmoss_product_workflow_deterministically_advances_and_binds_two_targets()
    test_fastmoss_product_search_defaults_force_category_pages_and_short_segment_queries()
    test_fastmoss_evidence_manifest_separates_total_fetched_overlap_and_conflicts()
    test_fastmoss_data_first_fallback_never_discards_usable_evidence()
    test_fastmoss_l2_market_metrics_are_only_upstream_category_reference()
    test_fastmoss_failed_category_page_is_not_counted_as_coverage()
    test_fastmoss_metadata_detects_unsorted_page_and_string_product_ids()
    test_fastmoss_evidence_ledger_keeps_native_dimensions_and_late_trends()
    test_fastmoss_answer_verifier_applies_local_edits_and_keeps_draft_on_failure()
    test_fastmoss_all_workflow_tools_emit_supported_envelopes()
    test_fastmoss_verifier_batches_claims_and_isolates_failures()
    test_fastmoss_creator_video_facts_and_historical_rebuild()
    test_fastmoss_report_packets_are_workflow_native_and_numeric_policy_is_strict()
    test_fastmoss_list_truncation_is_explicit_and_invalid_entities_are_filtered()
    test_fastmoss_22_call_semantic_registry_fixture()
    test_fastmoss_dossier_synthesis_preserves_complete_tool_evidence()
    test_fastmoss_report_evidence_is_semantic()
    test_fastmoss_v3_length_retries_without_losing_real_rows()
    test_fastmoss_v3_failure_falls_back_to_data_not_zero_products()
    test_fastmoss_analytical_answers_use_single_semantic_report_path()
    test_fastmoss_claim_ids_and_verifier_edit_guards()
    test_analytical_routes_use_report_model_and_fastmoss_preserves_pro_draft()
    print("chat tool normalization tests passed")
