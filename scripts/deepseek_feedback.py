#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from api_cache import record_api_call


DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_FEEDBACK_PROMPT = (
    "基于视频提取内容和分析结果，给出可执行的视频改进反馈。"
    "只返回严格可解析 JSON，不要 Markdown。"
    "建议包含这些键：summary、strengths、issues、improvement_suggestions、script_feedback、visual_feedback、audio_feedback、priority_actions。"
)


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object JSON: {path}")
    return data


def resolve_paths(path: Path) -> tuple[Path, Path]:
    if path.is_dir():
        return path / "analysis.json", path / "audit_result.json"
    if path.name == "analysis.json":
        return path, path.parent / "audit_result.json"
    if path.name == "direct_analysis.json":
        return path, path.parent / "direct_audit_result.json"
    return path / "analysis.json", path / "audit_result.json"


def build_prompt(
    analysis: dict,
    audit_result: dict,
    user_prompt: str,
    social_context: dict | None = None,
    social_insights: dict | None = None,
) -> str:
    prompt = user_prompt.strip() or DEFAULT_FEEDBACK_PROMPT
    body = (
        f"{prompt}\n\n"
        "Return strict parseable JSON only, without Markdown.\n\n"
        "analysis.json:\n"
        f"{json.dumps(analysis, ensure_ascii=False, indent=2)}\n\n"
        "audit_result.json:\n"
        f"{json.dumps(audit_result, ensure_ascii=False, indent=2)}"
    )
    if social_context:
        body += "\n\nsocial_context.json:\n" + json.dumps(social_context, ensure_ascii=False, indent=2)
    if social_insights:
        body += "\n\nsocial_insights.json:\n" + json.dumps(social_insights, ensure_ascii=False, indent=2)
    return body


def call_deepseek(api_key: str, prompt: str, api_url: str, model: str) -> dict:
    started = time.monotonic()
    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "Return strict parseable JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    record_api_call(
        "deepseek",
        "feedback",
        {
            "api_url": api_url,
            "model": model,
            "prompt_sha256": __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest(),
        },
        data,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return data


def extract_content(api_response: dict) -> str:
    try:
        return api_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected DeepSeek API response shape") from exc


def parse_json_content(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    return json.loads(stripped)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate video improvement feedback with DeepSeek.")
    parser.add_argument(
        "analysis_path",
        nargs="?",
        default=None,
        help="Path to an output directory containing analysis.json and audit_result.json.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path for feedback_result.json. Defaults to the analysis.json directory.",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL),
        help=f"DeepSeek chat completions URL. Defaults to {DEFAULT_API_URL}.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        help=f"DeepSeek model name. Defaults to {DEFAULT_MODEL}.",
    )
    parser.add_argument("--prompt", default="", help="User-defined feedback prompt.")
    parser.add_argument("--audit", default="", help="Explicit audit result JSON path.")
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("Missing required environment variable: DEEPSEEK_API_KEY", file=sys.stderr)
        return 1

    base_path = Path(args.analysis_path or "output/analysis.json")
    try:
        analysis_path, audit_path = resolve_paths(base_path)
        if args.audit:
            audit_path = Path(args.audit)
        analysis = read_json(analysis_path)
        audit_result = read_json(audit_path)
        social_context_path = analysis_path.parent / "social_context.json"
        social_insights_path = analysis_path.parent / "social_insights.json"
        social_context = read_json(social_context_path) if social_context_path.is_file() else None
        social_insights = read_json(social_insights_path) if social_insights_path.is_file() else None
        api_response = call_deepseek(
            api_key=api_key,
            prompt=build_prompt(analysis, audit_result, args.prompt, social_context, social_insights),
            api_url=args.api_url,
            model=args.model,
        )
        content = extract_content(api_response)
        try:
            feedback_result = parse_json_content(content)
        except json.JSONDecodeError:
            feedback_result = {"raw_result": content}

        output_path = Path(args.output) if args.output else analysis_path.parent / "feedback_result.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(feedback_result, file, ensure_ascii=False, indent=2)
            file.write("\n")

        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"DeepSeek feedback failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
