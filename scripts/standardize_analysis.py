#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
EVIDENCE_VERSION = 2


def parse_response(value: Any) -> dict:
    """Parse structured responses without repairing or inventing missing content."""
    if isinstance(value, dict) and "response" not in value:
        return value
    text = response_text(value).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].strip()
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        if text.startswith(("{", "[")):
            raise ValueError("incomplete_or_invalid_json") from None
        if not text or text.lower().startswith("error "):
            raise ValueError("missing_or_failed_response")
        return {"visual": text}
    if not isinstance(result, dict):
        raise ValueError("response_not_object")
    return result


def frame_evidence(frames: list) -> tuple[list, list]:
    timeline, issues = [], []
    for index, frame in enumerate(frames):
        try:
            parsed = parse_response(frame)
            items = parsed.get("timeline") or [parsed]
            if not isinstance(items, list):
                raise ValueError("invalid_timeline")
            frame_rows = []
            for item in items:
                visual = item.get("visual") or item.get("description")
                if not isinstance(visual, str) or not visual.strip():
                    raise ValueError("missing_visual")
                timestamp = frame.get("timestamp")
                captured = isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
                frame_rows.append({"index": index, "evidence_id": f"frame_{index}",
                    "time_range": str(timestamp) if captured else str(item.get("time_range") or frame.get("time_range") or ""),
                    "timestamp_seconds": timestamp if captured else None,
                    "time_source": "capture" if captured else "model_reported_unverified",
                    "evidence_type": "sampled_frame", "visual": visual,
                    "visible_text": item.get("visible_text", []),
                    "uncertainties": item.get("uncertainties", [])})
            timeline.extend(frame_rows)
        except (ValueError, TypeError, AttributeError) as exc:
            issues.append({"evidence_id": f"frame_{index}", "reason": str(exc)})
    return timeline, issues


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
    timeline, issues = frame_evidence(frame_analyses)
    try:
        description = parse_response(video_description)
        summary = description.get("summary") or description.get("visual") or ""
    except ValueError as exc:
        summary = ""
        issues.append({"evidence_id": "summary", "reason": str(exc)})
    if issues:
        # A narrative reconstructed from broken frame responses is not clean evidence.
        summary = ""
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

    return {
        "schema_version": SCHEMA_VERSION,
        "processing_mode": "analyzer",
        "vision_model": model,
        "audio_mode": "whisper",
        "metadata": {
            **metadata,
            "output_dir": str(output_dir),
            "standardized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "analysis_prompt": analysis_prompt,
            "evidence_version": EVIDENCE_VERSION,
            "extraction_quality": "complete" if timeline and not issues else "partial",
            "evidence_issues": issues,
            "coverage_note": "离散采样帧不能证明帧间动作或未采样区间；旧模型时间未经抽帧记录核实",
        },
        "summary": summary,
        "transcript": {
            "text": transcript.get("text", ""),
            "segments": transcript.get("segments", []),
            "language": transcript.get("language") or metadata.get("audio_language") or None,
            "successful": bool(metadata.get("transcription_successful", bool(transcript.get("text")))),
        },
        "timeline": timeline,
        "visual_evidence": [{"evidence_id": item["evidence_id"], "time_range": item["time_range"],
                             "description": item["visual"]} for item in timeline],
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
        if raw.get("processing_mode") == "analyzer" and raw.get("metadata", {}).get("evidence_version") != EVIDENCE_VERSION and isinstance(raw.get("raw_model_output"), dict):
            backup = output_dir / "analysis_before_evidence_v2.json"
            if not backup.exists():
                write_json(backup, raw)
            raw = raw["raw_model_output"]
        else:
            return 1 if raw.get("metadata", {}).get("extraction_quality") == "partial" else 0
    if not isinstance(raw, dict):
        raise ValueError("analysis must be an object")
    standardized = standardize_analyzer(raw, output_dir, args.elapsed_seconds)
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
    if metadata.get("extraction_quality") != "complete":
        log("提取证据不完整，已保存原始输出及问题清单，不能标记为成功")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
