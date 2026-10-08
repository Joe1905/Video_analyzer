#!/usr/bin/env python3
import argparse
import json
import os
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from api_cache import record_api_call
from standardize_analysis import EVIDENCE_VERSION, standardize_analyzer
from video_performance_context import (REPORT_INSTRUCTIONS, REPORT_VERSION, external_video_id,
                                       load_performance_context, validate_report)


DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_TOKENS = 32768


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
        "segments": [{k: item[k] for k in ("start", "end", "text") if k in item}
                     for item in transcript.get("segments", []) if isinstance(item, dict)],
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
    if (analysis.get("processing_mode") == "analyzer" and metadata.get("evidence_version") != EVIDENCE_VERSION
            and isinstance(analysis.get("raw_model_output"), dict)):
        analysis = standardize_analyzer(analysis["raw_model_output"], Path("."), None)
        analysis["metadata"]["duration_seconds"] = metadata.get("duration_seconds")
        metadata = analysis["metadata"]
    structured = metadata.get("evidence_version") == EVIDENCE_VERSION
    return {
        "schema_version": analysis.get("schema_version"),
        "processing_mode": analysis.get("processing_mode"),
        "vision_model": analysis.get("vision_model") or metadata.get("model"),
        "audio_mode": analysis.get("audio_mode"),
        "metadata": {
            "frames_processed": metadata.get("frames_processed") or metadata.get("frames_extracted"),
            "duration_processed": metadata.get("duration_processed"),
            "duration_seconds": metadata.get("duration_seconds"),
            "audio_language": metadata.get("audio_language"),
            "extraction_quality": metadata.get("extraction_quality"),
            "evidence_issues": metadata.get("evidence_issues", []),
            "coverage_note": metadata.get("coverage_note"),
            "sampling_coverage": metadata.get("sampling_coverage"),
        },
        "summary": truncate_text(analysis.get("summary", ""), 6000),
        "transcript": compact_transcript(analysis.get("transcript")),
        "timeline": analysis.get("timeline") if structured else compact_items(analysis.get("timeline")),
        "visual_evidence": [] if structured else compact_items(analysis.get("visual_evidence")),
    }


def video_duration(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True, timeout=15)
        value = float(result.stdout.strip())
        return value if math.isfinite(value) and value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def build_prompt(analysis: dict, user_prompt: str = "", performance: dict | None = None) -> str:
    analysis = compact_analysis(analysis)
    return (
        "用户补充重点（不能改变报告结构）：\n" + user_prompt.strip()[:12000] + "\n\n"
        + REPORT_INSTRUCTIONS + "\n\nanalysis.json:\n"
        + json.dumps(analysis, ensure_ascii=False, indent=2)
        + "\n\nProxy采集证据（available=false时不得补造指标）：\n"
        + json.dumps(performance or {"available": False, "limitations": ["未提供表现数据"]},
                     ensure_ascii=False, indent=2)
    )


def call_deepseek(
    api_key: str,
    prompt: str,
    api_url: str,
    model: str,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> dict:
    started = time.monotonic()
    api_url = normalize_chat_completions_url(api_url)
    payload: dict[str, Any] = {
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
    }
    if reasoning_effort and reasoning_effort != "disabled":
        payload["reasoning_effort"] = reasoning_effort
        payload["thinking"] = {"type": "enabled"}
    if reasoning_effort == "disabled":
        payload["thinking"] = {"type": "disabled"}

    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=600 if reasoning_effort and reasoning_effort != "disabled" else 120,
    )
    response.raise_for_status()
    data = response.json()
    record_api_call(
        "deepseek",
        "postprocess",
        {
            "api_url": api_url,
            "model": model,
            "prompt_sha256": __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest(),
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort if reasoning_effort != "disabled" else None,
            "thinking": "disabled" if reasoning_effort == "disabled" else "enabled" if reasoning_effort else "default",
        },
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
        help="Maximum combined reasoning and audit JSON output tokens.",
    )
    parser.add_argument("--reasoning-effort", choices=("disabled", "low", "high", "max"),
                        default="high", help="Report analysis thinking effort (default: high).")
    parser.add_argument("--video-id", default="", help="Exact external TikTok video ID for collected evidence.")
    parser.add_argument("--video-filename", default="", help="Original local media filename for duration probing.")
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
        metadata = dict(analysis.get("metadata") or {})
        media_name = Path(args.video_filename or analysis_path.parent.name).name
        metadata["duration_seconds"] = video_duration(Path.cwd() / "videos" / media_name)
        analysis["metadata"] = metadata
        video_id = external_video_id(args.video_id or analysis_path.parent.name)
        performance = load_performance_context(Path.cwd(), video_id)
        api_response = call_deepseek(
            api_key=api_key,
            prompt=build_prompt(analysis, args.prompt, performance),
            api_url=args.api_url,
            model=args.model,
            max_tokens=args.max_tokens,
            reasoning_effort=args.reasoning_effort,
        )
        content = extract_content(api_response)
        audit_result = parse_json_content(content)
        validate_report(audit_result)
        audit_result["report_version"] = REPORT_VERSION
        audit_result["采集数据来源"] = performance

        output_path = Path(args.output) if args.output else analysis_path.parent / "audit_result.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(audit_result, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary_path.replace(output_path)

        print(f"Wrote {output_path}")
        print(json.dumps({"model": api_response.get("model", args.model),
                          "reasoning_effort": args.reasoning_effort,
                          "max_tokens": args.max_tokens,
                          "finish_reason": api_response["choices"][0].get("finish_reason"),
                          "usage": api_response.get("usage", {})}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"DeepSeek postprocess failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
