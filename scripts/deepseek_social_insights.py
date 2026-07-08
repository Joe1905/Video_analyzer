#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from api_cache import record_api_call


DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object JSON: {path}")
    return data


def resolve_context_path(path: Path) -> Path:
    if path.is_dir():
        return path / "social_context.json"
    return path


def compact_context(context: dict[str, Any]) -> dict[str, Any]:
    items = context.get("items") if isinstance(context.get("items"), dict) else {}
    compact: dict[str, Any] = {
        "status": context.get("status"),
        "source_url": context.get("source_url"),
        "updated_at": context.get("updated_at"),
        "video_info": items.get("video_info"),
        "comments": items.get("comments"),
        "creator_profile": items.get("creator_profile"),
    }
    comments = (((items.get("comments") or {}).get("data") or {}).get("items") or [])
    if isinstance(comments, list) and len(comments) > 40:
        compact["comments"] = dict(items.get("comments") or {})
        compact["comments"]["data"] = dict((items.get("comments") or {}).get("data") or {})
        compact["comments"]["data"]["items"] = comments[:40]
    return compact


def build_prompt(context: dict[str, Any]) -> str:
    return (
        "你是短视频运营分析师。基于 SociaVault 抓取到的视频数据、评论区数据和博主资料，"
        "输出严格可解析 JSON，不要 Markdown，不要代码块，不要额外解释。\n\n"
        "如果某类数据缺失，请在对应字段中说明 unavailable/failed，不要编造。\n\n"
        "JSON 结构必须符合：\n"
        "{\n"
        '  "summary": "整体一句话判断",\n'
        '  "data_insights": {\n'
        '    "performance_judgement": "播放、互动、收藏、评论等表现判断",\n'
        '    "strengths": ["数据表现上的优势"],\n'
        '    "weaknesses": ["数据表现上的问题"],\n'
        '    "next_actions": ["提升数据表现的建议"]\n'
        "  },\n"
        '  "comment_insights": {\n'
        '    "sentiment": "positive|neutral|negative|mixed|unavailable",\n'
        '    "audience_pain_points": ["评论暴露的痛点"],\n'
        '    "questions_or_objections": ["用户疑问或反对点"],\n'
        '    "content_opportunities": ["可用于下一条视频的选题或话术"]\n'
        "  },\n"
        '  "creator_insights": {\n'
        '    "creator_fit": "博主与该视频/产品/内容方向的匹配度",\n'
        '    "audience_signal": "粉丝与互动指标透露的受众信号",\n'
        '    "collaboration_notes": ["投放或合作建议"]\n'
        "  },\n"
        '  "recommended_actions": [\n'
        '    {"priority": "high|medium|low", "action": "具体动作", "reason": "依据"}\n'
        "  ]\n"
        "}\n\n"
        "social_context.json:\n"
        f"{json.dumps(compact_context(context), ensure_ascii=False, indent=2)}"
    )


def normalize_chat_completions_url(api_url: str) -> str:
    url = str(api_url or DEFAULT_API_URL).strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def call_deepseek(api_key: str, prompt: str, api_url: str, model: str, max_tokens: int) -> dict[str, Any]:
    started = time.monotonic()
    api_url = normalize_chat_completions_url(api_url)
    response = requests.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "Return strict parseable JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    record_api_call(
        "deepseek",
        "social_insights",
        {
            "api_url": api_url,
            "model": model,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "max_tokens": max_tokens,
        },
        data,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return data


def extract_content(api_response: dict[str, Any]) -> str:
    try:
        choice = api_response["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason in {"length", "max_tokens"}:
            raise ValueError(f"DeepSeek response was truncated by max_tokens: finish_reason={finish_reason}")
        return choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected DeepSeek API response shape") from exc


def parse_json_content(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("DeepSeek social insights must be a JSON object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate social/context insights with DeepSeek.")
    parser.add_argument("context_path", nargs="?", default="output/social_context.json")
    parser.add_argument("--output", default=None)
    parser.add_argument("--api-url", default=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL))
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-tokens", type=int, default=int(os.getenv("DEEPSEEK_SOCIAL_MAX_TOKENS", "4096")))
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("Missing required environment variable: DEEPSEEK_API_KEY", file=sys.stderr)
        return 1

    try:
        context_path = resolve_context_path(Path(args.context_path))
        context = read_json(context_path)
        response = call_deepseek(
            api_key=api_key,
            prompt=build_prompt(context),
            api_url=args.api_url,
            model=args.model,
            max_tokens=max(1024, min(args.max_tokens, 16000)),
        )
        result = parse_json_content(extract_content(response))
        output_path = Path(args.output) if args.output else context_path.parent / "social_insights.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)
            file.write("\n")
        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"DeepSeek social insights failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
