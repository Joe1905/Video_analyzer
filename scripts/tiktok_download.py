#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
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


def douyin_cookie_header() -> str:
    return os.getenv("DOUYIN_COOKIE", "").strip()


def proxy_for_tiktok() -> str:
    return os.getenv("TIKTOK_PROXY_URL", "").strip()


def proxy_for_douyin() -> str:
    return os.getenv("DOUYIN_PROXY_URL", "").strip()


def playwright_cookies_from_header(cookie_header: str) -> list[dict[str, Any]]:
    cookies = []
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".douyin.com",
                "path": "/",
            }
        )
    return cookies


def media_id_from_url(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    if match:
        return match.group(1)
    compact = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path).strip("_")
    return compact[:80] or "unknown"


def has_douyin_media_keywords(url: str) -> bool:
    return any(
        keyword in url
        for keyword in (
            "douyinvod.com",
            "douyinstatic.com",
            "v1-",
            "v3-",
            "v5-",
            "v6-",
            "v9-",
            "playwm",
            "play/",
        )
    )


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
    cookie_header = douyin_cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    client_kwargs = {
        "headers": headers,
        "follow_redirects": True,
        "verify": False,
        "timeout": 30.0,
        "trust_env": False,
    }
    douyin_proxy = proxy_for_douyin()
    if douyin_proxy:
        client_kwargs["proxy"] = douyin_proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
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
        launch_options: dict[str, Any] = {
            "headless": os.getenv("DOUYIN_HEADLESS", "true").lower() != "false",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        }
        douyin_proxy = proxy_for_douyin()
        if douyin_proxy:
            launch_options["proxy"] = {"server": douyin_proxy}

        def add_candidate(media_url: str, size: int, source: str) -> None:
            if not media_url.startswith("http"):
                return
            if any(blocked in media_url for blocked in ("ads", "pre-roll", "commercial")):
                return
            if size > max_bytes:
                return
            if any(item["url"] == media_url for item in candidate_media):
                return
            candidate_media.append({"url": media_url, "size": size, "source": source})
            size_text = f"{size / 1024 / 1024:.2f} MB" if size else "unknown size"
            print(f"Captured media candidate ({source}): {size_text} {media_url[:140]}")

        with tempfile.TemporaryDirectory(prefix="douyin_browser_") as profile_dir:
            context = await p.chromium.launch_persistent_context(
                profile_dir,
                user_agent=DESKTOP_USER_AGENT,
                viewport={"width": 1280, "height": 720},
                locale="zh-CN",
                **launch_options,
            )
            cookie_header = douyin_cookie_header()
            if cookie_header:
                await context.add_cookies(playwright_cookies_from_header(cookie_header))
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = await context.new_page()

            async def handle_request(request):
                try:
                    request_url = request.url
                    if has_douyin_media_keywords(request_url):
                        add_candidate(request_url, 0, "request")
                except Exception as exc:
                    print(f"Skipping request: {exc}")

            async def handle_response(response):
                try:
                    response_url = response.url
                    content_type = response.headers.get("content-type", "").lower()
                    looks_like_media = "video" in content_type or "audio" in content_type or has_douyin_media_keywords(response_url)
                    if not looks_like_media:
                        return
                    content_length = int(response.headers.get("content-length", "0") or 0)
                    if content_length and content_length < 500 * 1024:
                        return
                    add_candidate(response_url, content_length, "response")
                except Exception as exc:
                    print(f"Skipping response: {exc}")

            try:
                page.on("request", handle_request)
                page.on("response", handle_response)
                print("Opening Douyin home page to warm anonymous browser state")
                await page.goto("https://www.douyin.com/", timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
                print(f"Opening Douyin page with Playwright: {resolved_url}")
                await page.goto(resolved_url, timeout=60000, wait_until="domcontentloaded")
                try:
                    await page.keyboard.press("Escape")
                    close_button = await page.query_selector(".dy-account-close, [data-e2e='modal-close-inner-button']")
                    if close_button:
                        await close_button.click(timeout=1000)
                except Exception:
                    pass

                try:
                    video = await page.query_selector("video")
                    if video:
                        await video.click(timeout=1000)
                        src = await video.evaluate("(node) => node.currentSrc || node.src || ''")
                        if isinstance(src, str) and src.startswith("http"):
                            add_candidate(src, 0, "dom-video")
                except Exception as exc:
                    print(f"DOM video probe failed: {exc}")

                try:
                    await page.keyboard.press("Space")
                except Exception:
                    pass

                started = asyncio.get_running_loop().time()
                while asyncio.get_running_loop().time() - started < 14:
                    if candidate_media and asyncio.get_running_loop().time() - started > 5:
                        break
                    await asyncio.sleep(1)

                if not candidate_media:
                    title = await page.title()
                    current_url = page.url
                    raise RuntimeError(
                        "No Douyin media response was captured after browser warm-up. "
                        f"title={title!r} current_url={current_url!r}. "
                        "The server browser may be seeing a login, risk-control, or blank page."
                    )

                best = max(candidate_media, key=lambda item: int(item["size"]))
                best_url = best["url"]
                if best["size"]:
                    print(f"Selected media candidate: {best['size'] / 1024 / 1024:.2f} MB from {best.get('source')}")
                else:
                    print(f"Selected media candidate with unknown size from {best.get('source')}")
                headers = {"User-Agent": DESKTOP_USER_AGENT, "Referer": "https://www.douyin.com/"}
                if cookie_header:
                    headers["Cookie"] = cookie_header
                client_kwargs = {
                    "headers": headers,
                    "verify": False,
                    "timeout": 120.0,
                    "trust_env": False,
                }
                if douyin_proxy:
                    client_kwargs["proxy"] = douyin_proxy
                async with httpx.AsyncClient(**client_kwargs) as client:
                    response = await client.get(best_url, follow_redirects=True)
                    if response.status_code != 200:
                        raise RuntimeError(f"Media download failed: HTTP {response.status_code}")
                    if len(response.content) < 500 * 1024:
                        raise RuntimeError(f"Downloaded media is too small: {len(response.content)} bytes")
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
                    "candidate_count": len(candidate_media),
                }
            finally:
                await context.close()


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
    tiktok_proxy = proxy_for_tiktok()
    if tiktok_proxy:
        options["proxy"] = tiktok_proxy
        print(f"Using TikTok proxy: {tiktok_proxy}")

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
