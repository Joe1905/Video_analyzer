#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from api_cache import record_api_call


ROOT = Path.cwd()
DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"Input JSON not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def build_prompt(extracted: Any, user_prompt: str) -> str:
    base_prompt = (
        "你是 TikTok Shop 商品和短视频电商分析师。基于 SociaVault 提取出的 TikTok Shop JSON，"
        "输出一个可执行的中文分析报告。只返回严格可解析 JSON，不要 Markdown。"
        "使用这些固定键：summary, product_positioning, sales_signals, review_insights, "
        "content_opportunities, risk_flags, recommended_actions, next_questions。"
        "每个键的值要具体、简洁，并且只能使用输入 JSON 支持的信息；如果信息不足，明确写出缺口。"
    )
    if user_prompt.strip():
        base_prompt += f"\n\n用户补充分析要求：\n{user_prompt.strip()}"
    return f"{base_prompt}\n\nTikTok Shop extracted JSON:\n{json.dumps(extracted, ensure_ascii=False, indent=2)}"


def call_deepseek(api_key: str, api_url: str, model: str, prompt: str) -> dict[str, Any]:
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
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    record_api_call(
        "deepseek",
        "shop_analyze",
        {"api_url": api_url, "model": model, "prompt_sha256": __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest()},
        data,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return data


def extract_content(api_response: dict[str, Any]) -> str:
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


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Analyze TikTok Shop extraction JSON with DeepSeek.")
    parser.add_argument("input_json", help="Path to shop_extract.json.")
    parser.add_argument("--output", default="")
    parser.add_argument("--api-url", default=os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL))
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--prompt", default="")
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("Missing required environment variable: DEEPSEEK_API_KEY", file=sys.stderr)
        return 1

    try:
        input_path = Path(args.input_json)
        extracted = read_json(input_path)
        api_response = call_deepseek(
            api_key=api_key,
            api_url=args.api_url,
            model=args.model,
            prompt=build_prompt(extracted, args.prompt),
        )
        content = extract_content(api_response)
        try:
            analysis = parse_json_content(content)
        except json.JSONDecodeError:
            analysis = {"raw_result": content}
        output_path = Path(args.output) if args.output else input_path.parent / "shop_analysis.json"
        write_json(output_path, analysis)
        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"DeepSeek TikTok Shop analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
