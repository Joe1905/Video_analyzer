#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

import requests


DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-chat"


def load_analysis(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"analysis.json not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def build_prompt(analysis: dict) -> str:
    return (
        "你是短视频内容审核助手。请根据 video-analyzer 的 analysis.json 判断视频内容风险，"
        "输出严格 JSON，不要使用 Markdown。JSON 字段包括："
        "risk_level（low/medium/high）、summary、issues（数组）、recommended_action。"
        "\n\nanalysis.json:\n"
        f"{json.dumps(analysis, ensure_ascii=False, indent=2)}"
    )


def call_deepseek(api_key: str, prompt: str, api_url: str, model: str) -> dict:
    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "你只输出可解析的 JSON。",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0.2,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


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
    parser = argparse.ArgumentParser(
        description="Post-process video-analyzer analysis.json with DeepSeek."
    )
    parser.add_argument(
        "analysis_path",
        nargs="?",
        default=None,
        help="Path to analysis.json or an output subdirectory containing analysis.json.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path for audit_result.json. Defaults to the analysis.json directory.",
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
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("Missing required environment variable: DEEPSEEK_API_KEY", file=sys.stderr)
        return 1

    analysis_path = Path(args.analysis_path or "output/analysis.json")
    if analysis_path.is_dir():
        analysis_path = analysis_path / "analysis.json"

    try:
        analysis = load_analysis(analysis_path)
        api_response = call_deepseek(
            api_key=api_key,
            prompt=build_prompt(analysis),
            api_url=args.api_url,
            model=args.model,
        )
        content = extract_content(api_response)
        try:
            audit_result = parse_json_content(content)
        except json.JSONDecodeError:
            audit_result = {"raw_result": content}

        output_path = Path(args.output) if args.output else analysis_path.parent / "audit_result.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(audit_result, file, ensure_ascii=False, indent=2)
            file.write("\n")

        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"DeepSeek postprocess failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
