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


DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_TOKENS = 4096


def normalize_chat_completions_url(api_url: str) -> str:
    url = str(api_url or DEFAULT_API_URL).strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def load_analysis(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"analysis.json not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def truncate_text(value: Any, limit: int = 4000) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n...[truncated {len(value) - limit} chars]"


def compact_transcript(transcript: Any) -> dict:
    if not isinstance(transcript, dict):
        return {}
    return {
        "text": truncate_text(transcript.get("text", ""), 6000),
        "language": transcript.get("language"),
        "successful": transcript.get("successful", transcript.get("success")),
    }


def compact_items(value: Any, limit: int = 80) -> Any:
    if not isinstance(value, list):
        return value
    compacted = []
    for item in value[:limit]:
        if isinstance(item, str):
            compacted.append(truncate_text(item, 1200))
        elif isinstance(item, dict):
            compacted.append({k: truncate_text(v, 1200) for k, v in item.items() if k != "words"})
        else:
            compacted.append(item)
    return compacted


def compact_analysis(analysis: dict) -> dict:
    metadata = analysis.get("metadata") if isinstance(analysis.get("metadata"), dict) else {}
    return {
        "schema_version": analysis.get("schema_version"),
        "processing_mode": analysis.get("processing_mode"),
        "vision_model": analysis.get("vision_model") or metadata.get("model"),
        "audio_mode": analysis.get("audio_mode"),
        "metadata": {
            "frames_processed": metadata.get("frames_processed") or metadata.get("frames_extracted"),
            "duration_processed": metadata.get("duration_processed"),
            "audio_language": metadata.get("audio_language"),
        },
        "summary": truncate_text(analysis.get("summary", ""), 6000),
        "transcript": compact_transcript(analysis.get("transcript")),
        "timeline": compact_items(analysis.get("timeline")),
        "visual_evidence": compact_items(analysis.get("visual_evidence")),
    }


def build_prompt(analysis: dict, user_prompt: str = "") -> str:
    analysis = compact_analysis(analysis)
    if user_prompt.strip():
        return (
            f"{user_prompt.strip()}\n\n"
            "Return strict parseable JSON only, without Markdown.\n\n"
            "analysis.json:\n"
            f"{json.dumps(analysis, ensure_ascii=False, indent=2)}"
        )
    return (
        "You are a short-video content audit analyst. Review the provided standardized "
        "analysis.json and produce a practical Simplified Chinese audit report. "
        "The analysis may come from key-frame extraction or direct video understanding; "
        "use summary, transcript, timeline, and visual_evidence when available. "
        "Return strict parseable JSON only, without Markdown. Use these exact keys: "
        "risk_level (low/medium/high), summary, content_overview, transcript_notes, "
        "visual_notes, risk_reasons (array), issues (array), recommended_action, "
        "publish_suggestion. Keep the values concise but specific to this video; do not invent "
        "facts that are not supported by the transcript or frame analysis."
        "\n\nanalysis.json:\n"
        f"{json.dumps(analysis, ensure_ascii=False, indent=2)}"
    )


def call_deepseek(api_key: str, prompt: str, api_url: str, model: str, max_tokens: int) -> dict:
    started = time.monotonic()
    api_url = normalize_chat_completions_url(api_url)
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
                    "content": "Return strict parseable JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
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
        "postprocess",
        {"api_url": api_url, "model": model, "prompt_sha256": __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest()},
        data,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return data


def extract_content(api_response: dict) -> str:
    try:
        choice = api_response["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason in {"length", "max_tokens"}:
            raise ValueError(f"DeepSeek output was truncated: finish_reason={finish_reason}")
        return choice["message"]["content"]
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
    parser.add_argument(
        "--prompt",
        default="",
        help="User-defined analysis prompt. Overrides the default audit analyst prompt.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.getenv("DEEPSEEK_POSTPROCESS_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
        help="Maximum DeepSeek output tokens for audit JSON.",
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
            prompt=build_prompt(analysis, args.prompt),
            api_url=args.api_url,
            model=args.model,
            max_tokens=args.max_tokens,
        )
        content = extract_content(api_response)
        audit_result = parse_json_content(content)

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
