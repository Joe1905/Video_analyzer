#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-chat"


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"analysis.json not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def call_deepseek(api_key: str, api_url: str, model: str, analysis: Any) -> dict:
    prompt = (
        "Translate the following video analysis JSON into Simplified Chinese. "
        "Preserve the original JSON structure and keys exactly. Translate only human-readable string values. "
        "Do not add commentary. Return strict parseable JSON only.\n\n"
        f"{json.dumps(analysis, ensure_ascii=False, indent=2)}"
    )
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
                    "content": "You are a translation engine. Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0,
        },
        timeout=180,
    )
    response.raise_for_status()
    return response.json()


def extract_content(api_response: dict) -> str:
    try:
        return api_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected DeepSeek API response shape") from exc


def parse_json_content(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    return json.loads(stripped)


def compact_for_translation(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: compact_for_translation(item)
            for key, item in value.items()
            if key != "raw_model_output"
        }
    if isinstance(value, list):
        return [compact_for_translation(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate analysis.json to Simplified Chinese.")
    parser.add_argument(
        "analysis_path",
        nargs="?",
        default=None,
        help="Path to analysis.json or an output subdirectory containing analysis.json.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path for analysis_zh.json. Defaults to the analysis.json directory.",
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
        analysis = compact_for_translation(load_json(analysis_path))
        api_response = call_deepseek(
            api_key=api_key,
            api_url=args.api_url,
            model=args.model,
            analysis=analysis,
        )
        translated = parse_json_content(extract_content(api_response))

        output_path = Path(args.output) if args.output else analysis_path.parent / "analysis_zh.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(translated, file, ensure_ascii=False, indent=2)
            file.write("\n")

        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"Analysis translation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
