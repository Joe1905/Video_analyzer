#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import requests
from api_cache import record_api_call


DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BATCH_CHARS = 8000
DEFAULT_TEXT_CHUNK_CHARS = 3000
DEFAULT_MAX_TOKENS = 8192
SKIP_STRING_KEYS = {
    "schema_version",
    "processing_mode",
    "vision_model",
    "audio_mode",
    "model",
    "language",
    "upload_mode",
    "video_file",
}


def normalize_chat_completions_url(api_url: str) -> str:
    url = str(api_url or DEFAULT_API_URL).strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"analysis.json not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def call_deepseek(api_key: str, api_url: str, model: str, items: list[dict[str, str]], max_tokens: int) -> dict:
    api_url = normalize_chat_completions_url(api_url)
    prompt = (
        "Translate each item's text into Simplified Chinese. Preserve line breaks, numbers, timestamps, "
        "frame labels, speaker meaning, and technical terms where appropriate. Return strict parseable JSON only "
        'with this shape: {"items":[{"id":"...","text":"translated text"}]}. Do not add commentary.\n\n'
        f"{json.dumps({'items': items}, ensure_ascii=False, indent=2)}"
    )
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
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        },
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    record_api_call(
        "deepseek",
        "translate_items",
        {"api_url": api_url, "model": model, "items_count": len(items), "items_sha256": __import__("hashlib").sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()},
        data,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return data


def call_deepseek_text(api_key: str, api_url: str, model: str, text: str, max_tokens: int) -> str:
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
                    "content": "You are a translation engine. Return only the Simplified Chinese translation, no JSON, no Markdown.",
                },
                {
                    "role": "user",
                    "content": "Translate this text into Simplified Chinese. Preserve timestamps, numbers, labels, and line breaks where useful.\n\n" + text,
                },
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    record_api_call(
        "deepseek",
        "translate_text",
        {"api_url": api_url, "model": model, "text_sha256": __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()},
        data,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return extract_content(data).strip()


def split_text_chunks(text: str, max_chars: int) -> list[str]:
    max_chars = max(500, int(max_chars or DEFAULT_TEXT_CHUNK_CHARS))
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        window = remaining[:max_chars]
        split_at = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind("。"),
            window.rfind("; "),
            window.rfind("；"),
        )
        if split_at < max_chars * 0.45:
            split_at = max_chars
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    return [chunk for chunk in chunks if chunk]


def translate_text_chunked(api_key: str, api_url: str, model: str, text: str, max_chars: int, max_tokens: int) -> str:
    chunks = split_text_chunks(text, max_chars)
    if len(chunks) == 1:
        return call_deepseek_text(api_key=api_key, api_url=api_url, model=model, text=text, max_tokens=max_tokens)
    translated_chunks = []
    for index, chunk in enumerate(chunks, start=1):
        translated = call_deepseek_text(api_key=api_key, api_url=api_url, model=model, text=chunk, max_tokens=max_tokens)
        translated_chunks.append(translated)
        print(f"Translated long text chunk {index}/{len(chunks)} ({len(chunk)} chars)", file=sys.stderr)
    return "\n".join(translated_chunks)


def extract_content(api_response: dict) -> str:
    try:
        choice = api_response["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason in {"length", "max_tokens"}:
            raise ValueError(f"DeepSeek translation output was truncated: finish_reason={finish_reason}")
        return choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Unexpected DeepSeek API response shape") from exc


def escape_control_chars_in_strings(value: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            result.append(char)
            continue
        if in_string and ord(char) < 32:
            result.append(f"\\u{ord(char):04x}")
            continue
        result.append(char)
    return "".join(result)


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
        return json.loads(escape_control_chars_in_strings(stripped))


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


def should_translate(path: tuple[Any, ...], value: str) -> bool:
    if not value.strip():
        return False
    key = str(path[-1]) if path else ""
    if key in SKIP_STRING_KEYS:
        return False
    if value.startswith("data:") or value.startswith("http://") or value.startswith("https://"):
        return False
    return any(("A" <= char <= "Z") or ("a" <= char <= "z") for char in value)


def collect_strings(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], str]]:
    if isinstance(value, dict):
        rows: list[tuple[tuple[Any, ...], str]] = []
        for key, item in value.items():
            rows.extend(collect_strings(item, (*path, key)))
        return rows
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            rows.extend(collect_strings(item, (*path, index)))
        return rows
    if isinstance(value, str) and should_translate(path, value):
        return [(path, value)]
    return []


def set_path(value: Any, path: tuple[Any, ...], text: str) -> None:
    cursor = value
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = text




def get_path(value: Any, path: tuple[Any, ...]) -> Any:
    cursor = value
    for key in path:
        cursor = cursor[key]
    return cursor


def looks_truncated_translation(original: str, translated: str) -> bool:
    source = str(original or "").strip()
    text = str(translated or "").strip()
    if len(source) < 120:
        return False
    if not text:
        return True
    if len(text) < max(30, int(len(source) * 0.18)):
        return True
    terminal_chars = ".!?\u3002\uff01\uff1f)]\uff09\"'\u201d\u2019"
    dangling_chars = "\u7684\u4e86\u5728\u5411\u4e0e\u548c\u53ca\u5e76\u800c\u4f46\u4e3a\u5bf9\u4ece\u5230\u4e2d\u4e0a\uff0c\u4e0b\u3001\uff1b\uff1a"
    if len(source) >= 220 and text[-1] not in terminal_chars:
        return text[-1] in dangling_chars or len(text) < int(len(source) * 0.35)
    return False


def suspicious_translation_paths(source_payload: Any, translated_payload: Any) -> list[tuple[Any, ...]]:
    suspicious: list[tuple[Any, ...]] = []
    for path, original in collect_strings(source_payload):
        try:
            translated = get_path(translated_payload, path)
        except (KeyError, IndexError, TypeError):
            suspicious.append(path)
            continue
        if isinstance(translated, str) and looks_truncated_translation(original, translated):
            suspicious.append(path)
    return suspicious


def has_suspicious_translation(source_payload: Any, translated_payload: Any) -> bool:
    return bool(suspicious_translation_paths(source_payload, translated_payload))


def batches(rows: list[tuple[tuple[Any, ...], str]], max_chars: int) -> list[list[tuple[tuple[Any, ...], str]]]:
    result: list[list[tuple[tuple[Any, ...], str]]] = []
    current: list[tuple[tuple[Any, ...], str]] = []
    size = 0
    for row in rows:
        row_size = len(row[1])
        if current and size + row_size > max_chars:
            result.append(current)
            current = []
            size = 0
        current.append(row)
        size += row_size
    if current:
        result.append(current)
    return result


def translate_in_batches(
    api_key: str,
    api_url: str,
    model: str,
    payload: Any,
    max_chars: int,
    max_tokens: int | None = None,
) -> Any:
    translated = deepcopy(payload)
    rows = collect_strings(payload)
    if not rows:
        return translated
    max_tokens = max(1024, int(max_tokens or os.getenv("TRANSLATION_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))))

    for batch_index, batch in enumerate(batches(rows, max_chars), start=1):
        items = [
            {
                "id": str(index),
                "text": text,
            }
            for index, (_path, text) in enumerate(batch)
        ]
        try:
            api_response = call_deepseek(api_key=api_key, api_url=api_url, model=model, items=items, max_tokens=max_tokens)
            content = extract_content(api_response)
            parsed = parse_json_content(content)
            translated_items = parsed.get("items", []) if isinstance(parsed, dict) else []
            by_id = {str(item.get("id")): item.get("text", "") for item in translated_items if isinstance(item, dict)}
            for index, (path, original) in enumerate(batch):
                text = by_id.get(str(index), original)
                if looks_truncated_translation(original, text):
                    text = translate_text_chunked(
                        api_key=api_key,
                        api_url=api_url,
                        model=model,
                        text=original,
                        max_chars=min(max_chars, int(os.getenv("TRANSLATION_TEXT_CHUNK_CHARS", str(DEFAULT_TEXT_CHUNK_CHARS)))),
                        max_tokens=max_tokens,
                    )
                set_path(translated, path, text)
            print(f"Translated batch {batch_index} ({len(batch)} strings)", file=sys.stderr)
        except Exception as exc:
            print(f"Batch {batch_index} JSON translation failed, falling back to single-text translation: {exc}", file=sys.stderr)
            for path, original in batch:
                set_path(
                    translated,
                    path,
                    translate_text_chunked(
                        api_key=api_key,
                        api_url=api_url,
                        model=model,
                        text=original,
                        max_chars=min(max_chars, int(os.getenv("TRANSLATION_TEXT_CHUNK_CHARS", str(DEFAULT_TEXT_CHUNK_CHARS)))),
                        max_tokens=max_tokens,
                    ),
                )
            print(f"Translated batch {batch_index} with fallback ({len(batch)} strings)", file=sys.stderr)

    return translated


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
    parser.add_argument(
        "--batch-chars",
        type=int,
        default=int(os.getenv("TRANSLATION_BATCH_CHARS", str(DEFAULT_BATCH_CHARS))),
        help=f"Approximate max characters per translation batch. Defaults to {DEFAULT_BATCH_CHARS}.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.getenv("TRANSLATION_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
        help=f"Max DeepSeek output tokens per translation request. Defaults to {DEFAULT_MAX_TOKENS}.",
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
        translated = translate_in_batches(
            api_key=api_key,
            api_url=args.api_url,
            model=args.model,
            payload=analysis,
            max_chars=args.batch_chars,
            max_tokens=args.max_tokens,
        )

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
