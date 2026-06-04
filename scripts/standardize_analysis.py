#!/usr/bin/env python3
import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("response"), str):
        return value["response"]
    return json.dumps(value, ensure_ascii=False)


def usage_block(
    input_tokens: int = 0,
    output_tokens: int = 0,
    api_calls: int = 0,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    total_tokens = input_tokens + output_tokens
    input_price = float(os.getenv("VISION_INPUT_PRICE_PER_1M", "0") or 0)
    output_price = float(os.getenv("VISION_OUTPUT_PRICE_PER_1M", "0") or 0)
    estimated_cost = (input_tokens / 1_000_000 * input_price) + (
        output_tokens / 1_000_000 * output_price
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "api_calls": api_calls,
        "elapsed_seconds": elapsed_seconds,
        "estimated_cost_usd": round(estimated_cost, 8),
        "pricing": {
            "input_usd_per_1m_tokens": input_price,
            "output_usd_per_1m_tokens": output_price,
        },
    }


def standardize_analyzer(raw: dict[str, Any], output_dir: Path, elapsed_seconds: float | None) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    transcript = raw.get("transcript") if isinstance(raw.get("transcript"), dict) else {}
    frame_analyses = raw.get("frame_analyses") if isinstance(raw.get("frame_analyses"), list) else []
    video_description = raw.get("video_description")
    model = metadata.get("model") or os.getenv("VISION_MODEL", "")
    api_calls = len(frame_analyses) + (1 if video_description else 0)

    return {
        "schema_version": SCHEMA_VERSION,
        "processing_mode": "analyzer",
        "vision_model": model,
        "audio_mode": "whisper",
        "metadata": {
            **metadata,
            "output_dir": str(output_dir),
            "standardized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "summary": response_text(video_description),
        "transcript": {
            "text": transcript.get("text", ""),
            "segments": transcript.get("segments", []),
            "language": metadata.get("audio_language") or os.getenv("LANGUAGE", "zh"),
            "successful": bool(metadata.get("transcription_successful", bool(transcript.get("text")))),
        },
        "timeline": [
            {
                "index": index,
                "time_range": frame.get("time_range") or frame.get("timestamp") or "",
                "visual": response_text(frame),
            }
            for index, frame in enumerate(frame_analyses)
        ],
        "visual_evidence": [
            {
                "index": index,
                "description": response_text(frame),
            }
            for index, frame in enumerate(frame_analyses)
        ],
        "raw_model_output": raw,
        "usage": usage_block(api_calls=api_calls, elapsed_seconds=elapsed_seconds),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert analyzer output to the shared analysis schema.")
    parser.add_argument("output_dir", help="Output directory containing analysis.json.")
    parser.add_argument("--mode", default="analyzer", choices=["analyzer"])
    parser.add_argument("--elapsed-seconds", type=float, default=None)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    analysis_path = output_dir / "analysis.json"
    if not analysis_path.is_file():
        raise FileNotFoundError(f"analysis.json not found: {analysis_path}")

    raw = read_json(analysis_path)
    if isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION:
        return 0

    standardized = standardize_analyzer(raw, output_dir, args.elapsed_seconds)
    write_json(output_dir / "analysis_raw.json", raw)
    write_json(analysis_path, standardized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
