#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ALLOWED_HOST_SUFFIXES = (
    "tiktok.com",
    "tiktokv.com",
    "douyin.com",
    "iesdouyin.com",
)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def validate_short_video_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https short-video URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES):
        raise ValueError("Only TikTok or Douyin URLs are supported")
    return cleaned


def host_for_url(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_douyin_url(url: str) -> bool:
    host = host_for_url(url)
    return host == "douyin.com" or host.endswith(".douyin.com") or host == "iesdouyin.com" or host.endswith(".iesdouyin.com")


def media_id_from_url(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    if match:
        return match.group(1)
    compact = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path).strip("_")
    return compact[:80] or "unknown"


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


async def resolve_final_url(url: str) -> str:
    import httpx

    headers = {"User-Agent": DESKTOP_USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, verify=False, timeout=30.0, trust_env=False) as client:
        response = await client.get(url)
        return str(response.url)


def sanitize_douyin_url(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    if match:
        return f"https://www.douyin.com/video/{match.group(1)}"
    return url


async def download_douyin_with_playwright(url: str, output_dir: Path, max_bytes: int) -> dict[str, Any]:
    import httpx
    from playwright.async_api import async_playwright

    resolved_url = sanitize_douyin_url(await resolve_final_url(url))
    video_id = media_id_from_url(resolved_url)
    target = output_dir / f"shortvideo_Douyin_{video_id}.mp4"
    candidate_media: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=DESKTOP_USER_AGENT,
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = await context.new_page()

        async def handle_response(response):
            try:
                content_type = response.headers.get("content-type", "").lower()
                if "video" not in content_type and "audio" not in content_type:
                    return
                content_length = int(response.headers.get("content-length", "0") or 0)
                if content_length < 500 * 1024:
                    return
                if content_length > max_bytes:
                    return
                media_url = response.url
                if any(item["url"] == media_url for item in candidate_media):
                    return
                print(f"Captured media candidate: {content_length / 1024 / 1024:.2f} MB {media_url[:120]}")
                candidate_media.append({"url": media_url, "size": content_length})
            except Exception as exc:
                print(f"Skipping response: {exc}")

        try:
            page.on("response", handle_response)
            print(f"Opening Douyin page with Playwright: {resolved_url}")
            await page.goto(resolved_url, timeout=60000, wait_until="domcontentloaded")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

            started = asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - started < 10:
                if candidate_media and asyncio.get_running_loop().time() - started > 3:
                    break
                await asyncio.sleep(1)

            if not candidate_media:
                raise RuntimeError("No large Douyin media response was captured. The page may require fresh browser cookies.")

            best = max(candidate_media, key=lambda item: int(item["size"]))
            print(f"Selected media candidate: {best['size'] / 1024 / 1024:.2f} MB")
            headers = {"User-Agent": DESKTOP_USER_AGENT, "Referer": "https://www.douyin.com/"}
            async with httpx.AsyncClient(headers=headers, verify=False, timeout=120.0, trust_env=False) as client:
                response = await client.get(best["url"], follow_redirects=True)
                if response.status_code != 200:
                    raise RuntimeError(f"Media download failed: HTTP {response.status_code}")
                if len(response.content) > max_bytes:
                    raise RuntimeError(f"Downloaded media exceeds max size: {len(response.content)} bytes")
                target.write_bytes(response.content)

            title = await page.title()
            return {
                "filename": target.name,
                "path": str(target),
                "size": target.stat().st_size,
                "id": video_id,
                "title": title,
                "uploader": None,
                "duration": None,
                "webpage_url": resolved_url,
                "downloader": "playwright",
            }
        finally:
            await context.close()
            await browser.close()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a public TikTok or Douyin video into videos/.")
    parser.add_argument("url", help="TikTok or Douyin video URL")
    parser.add_argument("--output-dir", default="videos")
    parser.add_argument("--result-json", default="")
    parser.add_argument("--max-bytes", type=int, default=int(os.getenv("TIKTOK_MAX_BYTES", str(2 * 1024 * 1024 * 1024))))
    args = parser.parse_args()

    try:
        url = validate_short_video_url(args.url)
        from yt_dlp import YoutubeDL
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bv*+ba/b[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "shortvideo_%(extractor_key)s_%(id)s.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": args.max_bytes,
        "quiet": False,
        "no_warnings": False,
    }

    try:
        if is_douyin_url(url):
            result = asyncio.run(download_douyin_with_playwright(url, output_dir, args.max_bytes))
        else:
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
                "downloader": "yt-dlp",
            }
        if args.result_json:
            write_json(Path(args.result_json), result)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(f"Short-video download failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
