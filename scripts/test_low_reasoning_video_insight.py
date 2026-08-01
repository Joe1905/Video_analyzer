#!/usr/bin/env python3
"""Manual test script to compare single-video DeepSeek analysis under different reasoning_effort modes (default/high vs low)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from deepseek_postprocess import DEFAULT_API_URL, DEFAULT_MODEL, normalize_chat_completions_url
    from hot_video_report import _video_insight_prompt
except ImportError:
    DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
    DEFAULT_MODEL = "deepseek-v4-flash"

    def normalize_chat_completions_url(url: str) -> str:
        url = str(url or DEFAULT_API_URL).strip().rstrip("/")
        return url if url.endswith("/chat/completions") else f"{url}/chat/completions"

    def _video_insight_prompt(video: dict[str, Any], social_context: dict[str, Any]) -> str:
        payload = {
            "video": video,
            "full_video_extraction": video.get("analysis", {}),
            "social_context": social_context,
        }
        return (
            "你是资深短视频爆款拆解分析师。请基于输入中的完整单视频解析内容，为这一条视频生成深度中文拆解。\n"
            "只返回严格 JSON。JSON keys 必须包含：one_sentence, core_boom_reason, hook, content_structure, "
            "visual_language, audience_trigger, comment_signal, creator_context, engagement_driver, "
            "replicable_formula, adaptation_ideas, weakness_or_risk, evidence_quotes。\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
        )

# Sample hot video data for standalone testing
SAMPLE_VIDEO_DATA = {
    "report_rank": 1,
    "platform": "tiktok",
    "video_id": "7391823719283712938",
    "title": "爆款美妆新品使用前后对比！超强显色力测试",
    "author": "@beauty_trends_official",
    "source_url": "https://www.tiktok.com/@beauty_trends_official/video/7391823719283712938",
    "source_label": "TikTok 美妆榜",
    "metrics": {
        "play_count": 1250000,
        "like_count": 89000,
        "comment_count": 3400,
        "share_count": 12000,
    },
    "hot_score": 98,
    "analysis": {
        "summary": "视频展示了一款唇釉的实测效果。前 3 秒博主通过极具反差的素颜与完妆对比吸引注意力，随后展示防水防擦测试，配乐节奏明快。",
        "transcript": {
            "text": "姐妹们！今天带你们测这款风很大的缎光唇釉。看我左边嘴唇完全没涂，右边只叠加了一层！是不是超级显白？更绝的是我们直接用纸巾擦——完全不掉色！连喝水都不沾杯！",
            "language": "zh",
        },
        "timeline": [
            {"timestamp": "00:00", "description": "特写镜头：素颜与完妆对比，产生强烈视觉冲力"},
            {"timestamp": "00:03", "description": "涂抹过程演示：一刷成膜，展现质感"},
            {"timestamp": "00:07", "description": "纸巾擦拭实验：用力按压不掉色"},
            {"timestamp": "00:12", "description": "展示产品包装与口播引导关注"},
        ],
        "visual_evidence": [
            "高饱和度色彩反差对比",
            "纸巾擦拭实验强化品质信任",
            "引导手势指向评论区链接",
        ],
    },
}

SAMPLE_SOCIAL_CONTEXT = {
    "top_comments": [
        "好显白啊！求色号！",
        "真的不沾杯吗？我之前买的都沾杯",
        "已经下单 02 号色了，期待收到",
    ],
    "author_followers": 450000,
}


def call_deepseek_with_reasoning(
    api_key: str,
    prompt: str,
    api_url: str,
    model: str,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Execute DeepSeek API call with optional reasoning_effort setting."""
    started = time.monotonic()
    url = normalize_chat_completions_url(api_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return strict parseable JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    if reasoning_effort:
        # OpenAI reasoning_effort format
        payload["reasoning_effort"] = reasoning_effort
        # Optional Anthropic / DeepSeek native reasoning toggle
        if reasoning_effort == "none":
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["thinking"] = {"type": "enabled"}

    response = requests.post(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    data["_elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return data


def analyze_test_result(mode_name: str, response: dict[str, Any]) -> dict[str, Any]:
    """Extract metrics, token counts, and information density from API response."""
    choice = response.get("choices", [{}])[0]
    finish_reason = choice.get("finish_reason")
    message = choice.get("message", {})
    content = message.get("content", "") or ""
    reasoning_content = message.get("reasoning_content", "") or ""

    usage = response.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    completion_details = usage.get("completion_tokens_details", {})
    reasoning_tokens = completion_details.get("reasoning_tokens", 0) or len(reasoning_content) // 4

    parsed_json: dict[str, Any] = {}
    is_valid_json = False
    field_count = 0

    if content.strip():
        try:
            parsed = json.loads(content.strip().strip("`").removeprefix("json").strip())
            if isinstance(parsed, dict):
                parsed_json = parsed
                is_valid_json = True
                field_count = len([k for k, v in parsed.items() if v])
        except Exception:
            pass

    return {
        "mode": mode_name,
        "elapsed_ms": response.get("_elapsed_ms", 0),
        "finish_reason": finish_reason,
        "prompt_tokens": prompt_tokens,
        "reasoning_tokens": reasoning_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "is_valid_json": is_valid_json,
        "field_count": field_count,
        "content_length": len(content),
        "reasoning_length": len(reasoning_content),
        "content_preview": content[:200] + "..." if len(content) > 200 else content,
        "parsed_keys": list(parsed_json.keys()),
    }


def print_comparison_table(results: list[dict[str, Any]]) -> None:
    """Print formatted comparative table of test results."""
    print("\n" + "=" * 80)
    print(" 单视频分析：思考强度 (reasoning_effort) 效果对比报告")
    print("=" * 80)
    header = f"{'测试模式':<18} | {'耗时(s)':<8} | {'Reasoning Tokens':<16} | {'Completion Tokens':<18} | {'Finish Reason':<13} | {'有效字段数'}"
    print(header)
    print("-" * 80)

    for r in results:
        elapsed_sec = f"{r['elapsed_ms'] / 1000:.2f}s"
        line = (
            f"{r['mode']:<18} | "
            f"{elapsed_sec:<8} | "
            f"{r['reasoning_tokens']:<16} | "
            f"{r['completion_tokens']:<18} | "
            f"{r['finish_reason']:<13} | "
            f"{r['field_count']}"
        )
        print(line)

    print("=" * 80 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test single-video analysis under different reasoning_effort modes.")
    parser.add_argument("--api-key", default=os.getenv("DEEPSEEK_API_KEY", ""), help="DeepSeek API key")
    parser.add_argument("--api-url", default=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL), help="DeepSeek API URL")
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL), help="DeepSeek model name")
    parser.add_argument("--max-tokens", type=int, default=2200, help="Max output tokens (default: 2200)")

    args = parser.parse_args()

    api_key = args.api_key.strip()
    if not api_key:
        print("[ERROR] Missing DEEPSEEK_API_KEY. Please provide via --api-key or DEEPSEEK_API_KEY env var.", file=sys.stderr)
        return 1

    prompt = _video_insight_prompt(SAMPLE_VIDEO_DATA, SAMPLE_SOCIAL_CONTEXT)

    modes = [
        ("默认 (High强度)", None),
        ("思考强度: Low", "low"),
        ("关闭思考 (None)", "none"),
    ]

    results = []
    print(f"[INFO] 正在针对模型 {args.model} 测试单视频拆解 Prompt (max_tokens={args.max_tokens})...\n")

    for mode_label, effort_val in modes:
        print(f"--> 正在测试模式: {mode_label}...")
        try:
            resp = call_deepseek_with_reasoning(
                api_key=api_key,
                prompt=prompt,
                api_url=args.api_url,
                model=args.model,
                max_tokens=args.max_tokens,
                reasoning_effort=effort_val,
            )
            analysis = analyze_test_result(mode_label, resp)
            results.append(analysis)
            print(f"    完成! 耗时: {analysis['elapsed_ms']}ms, Finish Reason: {analysis['finish_reason']}, 正文长度: {analysis['content_length']} 字符")
        except Exception as exc:
            print(f"    失败: {exc}")
            results.append({
                "mode": mode_label,
                "elapsed_ms": 0,
                "finish_reason": f"Error: {type(exc).__name__}",
                "prompt_tokens": 0,
                "reasoning_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "is_valid_json": False,
                "field_count": 0,
                "content_length": 0,
                "reasoning_length": 0,
                "content_preview": str(exc),
                "parsed_keys": [],
            })

    print_comparison_table(results)

    # Display detailed density comparison
    print("【详细 JSON 字段与信息密度对比】")
    for r in results:
        print(f"\n▶ 模式: {r['mode']}")
        print(f"  - 是否合法 JSON: {r['is_valid_json']}")
        print(f"  - 解析包含 Key: {r['parsed_keys']}")
        print(f"  - 截取内容预览:\n{r['content_preview']}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
