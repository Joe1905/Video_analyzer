#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_HOST_SUFFIXES = (
    "tiktok.com",
    "tiktokv.com",
)


def validate_tiktok_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https TikTok URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES):
        raise ValueError("Only TikTok URLs are supported")
    return cleaned


def pick_downloaded_path(info: dict[str, Any], fallback: Path) -> Path:
    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, dict) and item.get("filepath"):
                path = Path(item["filepath"])
                if path.is_file():
                    return path
    if fallback.is_file():
        return fallback
    mp4 = fallback.with_suffix(".mp4")
    if mp4.is_file():
        return mp4
    raise FileNotFoundError("Downloaded file path could not be resolved")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a public TikTok video into videos/.")
    parser.add_argument("url", help="TikTok video URL")
    parser.add_argument("--output-dir", default="videos")
    parser.add_argument("--result-json", default="")
    parser.add_argument("--max-bytes", type=int, default=int(os.getenv("TIKTOK_MAX_BYTES", str(2 * 1024 * 1024 * 1024))))
    args = parser.parse_args()

    try:
        url = validate_tiktok_url(args.url)
        from yt_dlp import YoutubeDL
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bv*+ba/b[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "tiktok_%(id)s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": args.max_bytes,
        "quiet": False,
        "no_warnings": False,
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            fallback = Path(ydl.prepare_filename(info))
        video_path = pick_downloaded_path(info, fallback)
        result = {
            "filename": video_path.name,
            "path": str(video_path),
            "size": video_path.stat().st_size,
            "id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("uploader_id"),
            "duration": info.get("duration"),
            "webpage_url": info.get("webpage_url") or url,
        }
        if args.result_json:
            write_json(Path(args.result_json), result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(f"TikTok download failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
