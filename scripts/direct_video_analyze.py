#!/usr/bin/env python3
import argparse
import base64
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
from api_cache import record_api_call


ROOT = Path.cwd()
VIDEOS_DIR = ROOT / "videos"
OUTPUT_DIR = ROOT / "output"
SCHEMA_VERSION = "1.0"
MAX_BASE64_BYTES = 7 * 1024 * 1024
DEFAULT_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3-vl-flash"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_ANALYSIS_PROMPT = (
    "Analyze this short video directly. Return strict JSON only, no Markdown. "
    "Use these exact keys: summary, timeline, visual_evidence. "
    "timeline must be an array of short chronological events with time_range, visual, audio fields. "
    "visual_evidence must be an array of concrete observations from the video frames. "
    "Be specific and do not invent unsupported facts."
)


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


def _compress_for_base64(path: Path, target: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-an",
            "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "30", "-movflags", "+faststart", str(target),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "ffmpeg video compression failed")


def read_video_data_url(path: Path) -> str:
    source_path = path
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if path.stat().st_size > MAX_BASE64_BYTES:
        temp_dir = tempfile.TemporaryDirectory(prefix="direct_video_")
        source_path = Path(temp_dir.name) / "video.mp4"
        _compress_for_base64(path, source_path)
    size = source_path.stat().st_size
    if size > MAX_BASE64_BYTES:
        if temp_dir:
            temp_dir.cleanup()
        raise ValueError(
            f"Compressed direct video still exceeds the 7MB Base64 safety limit: "
            f"{path.name} is {size / 1024 / 1024:.2f}MB."
        )
    mime_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
    if temp_dir:
        temp_dir.cleanup()
    return f"data:{mime_type};base64,{encoded}"


def transcribe_audio(video_path: Path, language: str, whisper_model: str) -> dict[str, Any]:
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError("Whisper is not installed in this image") from exc

    with tempfile.TemporaryDirectory() as temp_dir:
        audio_path = Path(temp_dir) / "audio.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(audio_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        model = whisper.load_model(whisper_model)
        result = model.transcribe(str(audio_path), language=language)
    return {
        "text": result.get("text", "").strip(),
        "segments": result.get("segments", []),
        "language": result.get("language") or language,
        "successful": bool(result.get("text")),
    }


def build_prompt(transcript: dict[str, Any], analysis_prompt: str) -> str:
    prompt = analysis_prompt.strip() or DEFAULT_ANALYSIS_PROMPT
    if not transcript.get("available"):
        return (
            f"{prompt}\n\n"
            "No reliable ASR transcript is available. Do not invent, quote, or summarize "
            "spoken dialogue. Base the result on observable video content only."
        )
    return f"{prompt}\n\nWhisper transcript:\n{json.dumps(transcript, ensure_ascii=False, indent=2)}"


def call_vision_api(
    api_key: str,
    api_url: str,
    model: str,
    video_url: str,
    fps: float,
    transcript: dict[str, Any],
    analysis_prompt: str,
    max_tokens: int,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    response = requests.post(
        api_url.rstrip("/") + "/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {
                                "url": video_url,
                                "fps": fps,
                            },
                        },
                        {
                            "type": "text",
                            "text": build_prompt(transcript, analysis_prompt),
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        },
        timeout=300,
    )
    elapsed = time.monotonic() - started
    response.raise_for_status()
    data = response.json()
    record_api_call(
        "qwen_vision",
        "direct_video_analyze",
        {
            "api_url": api_url.rstrip("/") + "/chat/completions",
            "model": model,
            "fps": fps,
            "max_tokens": max_tokens,
            "video_url_sha256": __import__("hashlib").sha256(video_url.encode("utf-8")).hexdigest(),
            "transcript_sha256": __import__("hashlib").sha256(json.dumps(transcript, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            "prompt_sha256": __import__("hashlib").sha256(analysis_prompt.encode("utf-8")).hexdigest(),
        },
        data,
        elapsed_ms=int(elapsed * 1000),
    )
    return data, elapsed


def extract_content(api_response: dict[str, Any]) -> str:
    try:
        choice = api_response["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason in {"length", "max_tokens"}:
            raise ValueError(f"Vision API output was truncated: finish_reason={finish_reason}")
        return choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected vision API response shape") from exc


def parse_json_content(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def usage_from_response(api_response: dict[str, Any], elapsed_seconds: float) -> dict[str, Any]:
    usage = api_response.get("usage") if isinstance(api_response.get("usage"), dict) else {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    input_price = float(os.getenv("VISION_INPUT_PRICE_PER_1M", "0") or 0)
    output_price = float(os.getenv("VISION_OUTPUT_PRICE_PER_1M", "0") or 0)
    estimated_cost = (input_tokens / 1_000_000 * input_price) + (
        output_tokens / 1_000_000 * output_price
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "api_calls": 1,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "estimated_cost_usd": round(estimated_cost, 8),
        "pricing": {
            "input_usd_per_1m_tokens": input_price,
            "output_usd_per_1m_tokens": output_price,
        },
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Analyze a full video with an OpenAI-compatible Qwen API.")
    parser.add_argument("video_name", help="Video file name under videos/.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--api-url", default=os.getenv("VISION_API_URL", DEFAULT_API_URL))
    parser.add_argument("--api-key", default=os.getenv("VISION_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("DIRECT_VIDEO_MODEL", DEFAULT_MODEL))
    parser.add_argument("--fps", type=float, default=float(os.getenv("DIRECT_VIDEO_FPS", "2")))
    parser.add_argument("--audio-mode", default=os.getenv("DIRECT_VIDEO_AUDIO_MODE", "whisper"))
    parser.add_argument("--upload-mode", default=os.getenv("DIRECT_VIDEO_UPLOAD_MODE", "auto"))
    parser.add_argument("--public-url", default=os.getenv("DIRECT_VIDEO_PUBLIC_URL", ""))
    parser.add_argument("--prompt-file", default=os.getenv("ANALYSIS_PROMPT_FILE", ""))
    parser.add_argument("--prompt", default="")
    parser.add_argument("--language", default=os.getenv("LANGUAGE", "zh"))
    parser.add_argument("--whisper-model", default=os.getenv("WHISPER_MODEL", "small"))
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.getenv("DIRECT_VIDEO_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
        help="Maximum Qwen output tokens for direct video JSON.",
    )
    args = parser.parse_args()

    if not args.api_key:
        print("Missing required environment variable: VISION_API_KEY", file=sys.stderr)
        return 1

    video_path = VIDEOS_DIR / Path(args.video_name).name
    if not video_path.is_file():
        print(f"Video file not found: {video_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR / video_path.name
    output_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    if args.public_url:
        video_url = args.public_url
        upload_mode = "public_url"
    else:
        video_url = read_video_data_url(video_path)
        upload_mode = "base64"

    if args.audio_mode == "whisper":
        transcript = transcribe_audio(video_path, args.language, args.whisper_model)
        transcript["available"] = True
    elif args.audio_mode == "none":
        transcript = {"text": "", "segments": [], "language": args.language, "successful": False, "available": False}
    else:
        raise ValueError("DIRECT_VIDEO_AUDIO_MODE must be 'whisper' or 'none'")
    analysis_prompt = args.prompt.strip() or DEFAULT_ANALYSIS_PROMPT
    if not args.prompt.strip() and args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if prompt_path.is_file():
            analysis_prompt = prompt_path.read_text(encoding="utf-8").strip() or DEFAULT_ANALYSIS_PROMPT

    api_response, api_elapsed = call_vision_api(
        api_key=args.api_key,
        api_url=args.api_url,
        model=args.model,
        video_url=video_url,
        fps=args.fps,
        transcript=transcript,
        analysis_prompt=analysis_prompt,
        max_tokens=args.max_tokens,
    )
    content = extract_content(api_response)
    parse_error = ""
    try:
        parsed = parse_json_content(content)
    except json.JSONDecodeError as exc:
        parse_error = str(exc)
        parsed = {
            "summary": content.strip() or "Direct video model returned empty content.",
            "raw_text": content,
        }

    elapsed = time.monotonic() - started
    usage = usage_from_response(api_response, elapsed)
    summary = parsed.get("summary", "") if isinstance(parsed, dict) else ""
    timeline = parsed.get("timeline", []) if isinstance(parsed, dict) else []
    visual_evidence = parsed.get("visual_evidence", []) if isinstance(parsed, dict) else []
    analysis = {
        "schema_version": SCHEMA_VERSION,
        "processing_mode": "direct_video",
        "vision_model": args.model,
        "audio_mode": args.audio_mode,
        "metadata": {
            "video_file": video_path.name,
            "video_size_bytes": video_path.stat().st_size,
            "fps": args.fps,
            "upload_mode": upload_mode,
            "api_elapsed_seconds": round(api_elapsed, 3),
            "analysis_prompt": analysis_prompt,
        },
        "summary": summary,
        "transcript": transcript,
        "timeline": timeline,
        "visual_evidence": visual_evidence,
        "raw_model_output": api_response,
        "raw_model_text": content,
        "parse_error": parse_error,
        "usage": usage,
    }
    write_json(output_dir / "analysis.json", analysis)
    print(f"Wrote {output_dir / 'analysis.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
