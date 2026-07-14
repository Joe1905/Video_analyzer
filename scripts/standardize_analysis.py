#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.1"


def log(message: str) -> None:
    print(f"[standardize_analysis] {message}", file=sys.stderr)


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


def numeric_timestamp(frame: Any, fallback_text: str = "") -> float | None:
    if isinstance(frame, dict):
        for key in ("timestamp_seconds", "timestamp", "time"):
            value = frame.get(key)
            if isinstance(value, (int, float)) and float(value) >= 0:
                return round(float(value), 3)
    text = fallback_text or response_text(frame)
    patterns = (
        r"(?:Frame|帧|第\s*\d+\s*帧|画面)\s*\d*[^\d]{0,24}(\d+(?:\.\d+)?)\s*(?:s|seconds?|秒)",
        r"(?:at|在)\s*(\d+(?:\.\d+)?)\s*(?:s|seconds?|秒)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return round(float(match.group(1)), 3)
    return None


def probe_duration(video_path: Path | None) -> float | None:
    if not video_path or not video_path.is_file():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        duration = float(result.stdout.strip())
        return round(duration, 3) if duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def timeline_rows(frame_analyses: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for index, frame in enumerate(frame_analyses):
        visual = response_text(frame)
        timestamp = numeric_timestamp(frame, visual)
        frame_number = frame.get("frame_number") if isinstance(frame, dict) else None
        if not isinstance(frame_number, int):
            frame_number = index
        row: dict[str, Any] = {
            "index": index,
            "frame_number": frame_number,
            "time_range": f"{timestamp:.3f}s" if timestamp is not None else "",
            "visual": visual,
        }
        if timestamp is not None:
            row["timestamp_seconds"] = timestamp
        rows.append(row)
    return rows


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


def standardize_analyzer(
    raw: dict[str, Any],
    output_dir: Path,
    elapsed_seconds: float | None,
    video_path: Path | None = None,
) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    transcript = raw.get("transcript") if isinstance(raw.get("transcript"), dict) else {}
    frame_analyses = raw.get("frame_analyses") if isinstance(raw.get("frame_analyses"), list) else []
    video_description = raw.get("video_description")
    model = metadata.get("model") or os.getenv("VISION_MODEL", "")
    api_calls = len(frame_analyses) + (1 if video_description else 0)
    prompt_path = output_dir / "analysis_prompt.txt"
    analysis_prompt = prompt_path.read_text(encoding="utf-8").strip() if prompt_path.is_file() else ""
    frames_dir = output_dir / "frames"
    frames_on_disk = len([path for path in frames_dir.rglob("*") if path.is_file()]) if frames_dir.is_dir() else 0
    log(
        "raw metadata "
        f"frames_extracted={metadata.get('frames_extracted')} "
        f"frames_processed={metadata.get('frames_processed')} "
        f"frame_analyses={len(frame_analyses)} "
        f"has_video_description={bool(video_description)} "
        f"transcription_successful={metadata.get('transcription_successful')} "
        f"frames_on_disk={frames_on_disk}"
    )

    timeline = timeline_rows(frame_analyses)
    duration_seconds = probe_duration(video_path)
    normalized_metadata = {**metadata}
    if duration_seconds is not None:
        normalized_metadata["duration_seconds"] = duration_seconds
    return {
        "schema_version": SCHEMA_VERSION,
        "processing_mode": "analyzer",
        "vision_model": model,
        "audio_mode": "whisper",
        "metadata": {
            **normalized_metadata,
            "output_dir": str(output_dir),
            "standardized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "analysis_prompt": analysis_prompt,
        },
        "summary": response_text(video_description),
        "transcript": {
            "text": transcript.get("text", ""),
            "segments": transcript.get("segments", []),
            "language": metadata.get("audio_language") or os.getenv("LANGUAGE", "zh"),
            "successful": bool(metadata.get("transcription_successful", bool(transcript.get("text")))),
        },
        "timeline": timeline,
        "visual_evidence": [
            {
                "index": row["index"],
                "frame_number": row["frame_number"],
                **({"timestamp_seconds": row["timestamp_seconds"]} if "timestamp_seconds" in row else {}),
                "description": row["visual"],
            }
            for row in timeline
        ],
        "raw_model_output": raw,
        "usage": usage_block(api_calls=api_calls, elapsed_seconds=elapsed_seconds),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert analyzer output to the shared analysis schema.")
    parser.add_argument("output_dir", help="Output directory containing analysis.json.")
    parser.add_argument("--mode", default="analyzer", choices=["analyzer"])
    parser.add_argument("--elapsed-seconds", type=float, default=None)
    parser.add_argument("--video-path", default="", help="Source video path used for duration probing.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    analysis_path = output_dir / "analysis.json"
    if not analysis_path.is_file():
        raise FileNotFoundError(f"analysis.json not found: {analysis_path}")

    raw = read_json(analysis_path)
    if isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION:
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        log(
            "analysis already standardized "
            f"frames_extracted={metadata.get('frames_extracted')} "
            f"frames_processed={metadata.get('frames_processed')} "
            f"timeline={len(raw.get('timeline') or [])}"
        )
        return 0

    standardized = standardize_analyzer(
        raw,
        output_dir,
        args.elapsed_seconds,
        Path(args.video_path) if args.video_path else None,
    )
    write_json(output_dir / "analysis_raw.json", raw)
    write_json(analysis_path, standardized)
    metadata = standardized.get("metadata", {})
    log(
        "wrote standardized analysis "
        f"frames_extracted={metadata.get('frames_extracted')} "
        f"frames_processed={metadata.get('frames_processed')} "
        f"timeline={len(standardized.get('timeline') or [])} "
        f"summary_chars={len(standardized.get('summary') or '')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
