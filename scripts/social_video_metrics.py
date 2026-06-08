#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path.cwd()
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


def validate_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https video URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES):
        raise ValueError("Only TikTok or Douyin URLs are supported")
    if len(cleaned) > 2048:
        raise ValueError("URL is too long")
    return cleaned


def host_for_url(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def platform_for_url(url: str) -> str:
    host = host_for_url(url)
    if host == "douyin.com" or host.endswith(".douyin.com") or host == "iesdouyin.com" or host.endswith(".iesdouyin.com"):
        return "douyin"
    return "tiktok"


def cookie_header_for(platform: str) -> str:
    if platform == "douyin":
        return os.getenv("DOUYIN_COOKIE", "").strip()
    return os.getenv("TIKTOK_COOKIE", "").strip()


def proxy_for(platform: str) -> str:
    if platform == "douyin":
        return os.getenv("DOUYIN_PROXY_URL", "").strip()
    return os.getenv("TIKTOK_PROXY_URL", "").strip()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def compact_text(value: str, limit: int = 5000) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[:limit]


def first_present(data: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return None


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        multiplier = 1
        suffixes = {
            "k": 1_000,
            "m": 1_000_000,
            "b": 1_000_000_000,
            "万": 10_000,
            "w": 10_000,
            "亿": 100_000_000,
        }
        if cleaned and cleaned[-1].lower() in suffixes:
            multiplier = suffixes[cleaned[-1].lower()]
            cleaned = cleaned[:-1]
        try:
            return int(float(cleaned) * multiplier)
        except ValueError:
            return None
    return None


def assign_first(target: dict[str, Any], key: str, value: Any) -> None:
    if target.get(key) in (None, ""):
        parsed = to_int(value)
        target[key] = parsed if parsed is not None else value


def collect_from_node(node: Any, metrics: dict[str, Any], author: dict[str, Any], samples: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        stats_keys = {
            "like_count": ("diggCount", "digg_count", "likeCount", "like_count", "likedCount"),
            "comment_count": ("commentCount", "comment_count", "comments", "comment_count_str"),
            "share_count": ("shareCount", "share_count", "forwardCount", "repostCount"),
            "play_count": ("playCount", "play_count", "viewCount", "view_count", "collectCount"),
            "favorite_count": ("collectCount", "collect_count", "favoriteCount", "favorite_count"),
        }
        author_keys = {
            "follower_count": ("followerCount", "follower_count", "fansCount", "fans_count"),
            "following_count": ("followingCount", "following_count"),
            "heart_count": ("heartCount", "heart_count", "totalFavorited"),
            "video_count": ("videoCount", "video_count"),
        }
        for output_key, names in stats_keys.items():
            value = first_present(node, names)
            if value is not None:
                assign_first(metrics, output_key, value)
        for output_key, names in author_keys.items():
            value = first_present(node, names)
            if value is not None:
                assign_first(author, output_key, value)
        if author.get("nickname") in (None, ""):
            value = first_present(node, ("nickname", "nickName", "authorName", "display_name"))
            if isinstance(value, str):
                author["nickname"] = value
        if author.get("unique_id") in (None, ""):
            value = first_present(node, ("uniqueId", "unique_id", "shortId", "authorId", "secUid"))
            if isinstance(value, str):
                author["unique_id"] = value
        interesting = set().union(*(set(names) for names in stats_keys.values()), *(set(names) for names in author_keys.values()))
        if interesting.intersection(node.keys()) and len(samples) < 20:
            samples.append({key: node.get(key) for key in sorted(interesting.intersection(node.keys()))})
        for value in node.values():
            collect_from_node(value, metrics, author, samples)
    elif isinstance(node, list):
        for item in node[:200]:
            collect_from_node(item, metrics, author, samples)


def json_candidates_from_html(html: str) -> list[Any]:
    candidates: list[Any] = []
    for pattern in (
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        r'<script[^>]+id="RENDER_DATA"[^>]*>(.*?)</script>',
    ):
        for match in re.finditer(pattern, html, flags=re.I | re.S):
            raw = match.group(1).strip()
            if "RENDER_DATA" in pattern:
                raw = unquote(raw)
            try:
                candidates.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    for match in re.finditer(r"window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>", html, flags=re.S):
        try:
            candidates.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return candidates


def extract_meta(html: str) -> dict[str, Any]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    title = compact_text(re.sub("<[^>]+>", "", title_match.group(1))) if title_match else ""
    description = ""
    desc_match = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']*)', html, flags=re.I)
    if desc_match:
        description = compact_text(desc_match.group(1))
    return {"title": title, "description": description}


def extract_from_html(html: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    author: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    candidates = json_candidates_from_html(html)
    for candidate in candidates:
        collect_from_node(candidate, metrics, author, samples)
    return {
        "page_meta": extract_meta(html),
        "metrics": metrics,
        "author": author,
        "json_candidate_count": len(candidates),
        "raw_stat_samples": samples,
    }


def response_text(response: Any) -> str:
    value = getattr(response, "text", "")
    if callable(value):
        value = value()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def fetch_with_scrapling(url: str, platform: str, timeout_ms: int, wait_ms: int) -> dict[str, Any]:
    from scrapling.fetchers import StealthyFetcher

    headers = {"User-Agent": DESKTOP_USER_AGENT}
    cookie_header = cookie_header_for(platform)
    if cookie_header:
        headers["Cookie"] = cookie_header

    fetch_kwargs: dict[str, Any] = {
        "headless": True,
        "humanize": True,
        "geoip": bool(proxy_for(platform)),
        "extra_headers": headers,
        "timeout": timeout_ms,
        "wait": wait_ms,
        "disable_resources": False,
        "network_idle": False,
        "google_search": False,
    }
    proxy = proxy_for(platform)
    if proxy:
        fetch_kwargs["proxy"] = proxy
    try:
        response = StealthyFetcher.fetch(url, **fetch_kwargs)
    except TypeError:
        response = StealthyFetcher().fetch(url, **fetch_kwargs)

    html = response_text(response)
    payload = extract_from_html(html)
    payload["fetcher"] = "scrapling_stealthy"
    payload["status"] = getattr(response, "status", None) or getattr(response, "status_code", None)
    payload["final_url"] = str(getattr(response, "url", "") or url)
    payload["html_length"] = len(html)
    return payload


def fetch_with_playwright(url: str, platform: str, timeout_ms: int, wait_ms: int) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        from playwright.async_api import async_playwright

        headers: dict[str, str] = {}
        cookie_header = cookie_header_for(platform)
        if cookie_header:
            headers["Cookie"] = cookie_header
        async with async_playwright() as p:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            }
            proxy = proxy_for(platform)
            if proxy:
                launch_options["proxy"] = {"server": proxy}
            browser = await p.chromium.launch(**launch_options)
            context = await browser.new_context(
                user_agent=DESKTOP_USER_AGENT,
                locale="zh-CN" if platform == "douyin" else "en-US",
                extra_http_headers=headers,
                viewport={"width": 1280, "height": 720},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = await context.new_page()
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                await page.wait_for_timeout(wait_ms)
                html = await page.content()
                payload = extract_from_html(html)
                payload["fetcher"] = "playwright_fallback"
                payload["status"] = None
                payload["final_url"] = page.url
                payload["html_length"] = len(html)
                return payload
            finally:
                await browser.close()

    return asyncio.run(run())


def fetch_page_data(url: str, platform: str, timeout_ms: int, wait_ms: int) -> dict[str, Any]:
    try:
        return fetch_with_scrapling(url, platform, timeout_ms, wait_ms)
    except Exception as exc:
        fallback = fetch_with_playwright(url, platform, timeout_ms, wait_ms)
        fallback["scrapling_error"] = str(exc)
        return fallback


def extract_tiktok_with_ytdlp(url: str) -> dict[str, Any]:
    from yt_dlp import YoutubeDL

    options = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 1,
        "extract_flat": False,
    }
    proxy = proxy_for("tiktok")
    if proxy:
        options["proxy"] = proxy
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "description": info.get("description"),
        "uploader": info.get("uploader") or info.get("uploader_id"),
        "uploader_id": info.get("uploader_id"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "metrics": {
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "repost_count": info.get("repost_count"),
            "view_count": info.get("view_count"),
            "availability": info.get("availability"),
        },
        "author": {
            "channel": info.get("channel"),
            "channel_id": info.get("channel_id"),
            "channel_follower_count": info.get("channel_follower_count"),
        },
        "extractor": info.get("extractor"),
    }


def merge_metrics(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, value in secondary.items():
        if merged.get(key) in (None, "") and value not in (None, ""):
            merged[key] = value
    return merged


def build_result(url: str, timeout_ms: int, wait_ms: int) -> dict[str, Any]:
    platform = platform_for_url(url)
    started = time.monotonic()
    page_data = fetch_page_data(url, platform, timeout_ms, wait_ms)
    ytdlp_data: dict[str, Any] | None = None
    ytdlp_error = ""
    if platform == "tiktok":
        try:
            ytdlp_data = extract_tiktok_with_ytdlp(url)
        except Exception as exc:
            ytdlp_error = str(exc)

    metrics = page_data.get("metrics") if isinstance(page_data.get("metrics"), dict) else {}
    author = page_data.get("author") if isinstance(page_data.get("author"), dict) else {}
    if isinstance(ytdlp_data, dict):
        metrics = merge_metrics(metrics, ytdlp_data.get("metrics") or {})
        author = merge_metrics(author, ytdlp_data.get("author") or {})

    return {
        "schema_version": 1,
        "url": url,
        "platform": platform,
        "metrics": metrics,
        "author": author,
        "page_meta": page_data.get("page_meta") or {},
        "page_fetch": {
            "fetcher": page_data.get("fetcher"),
            "status": page_data.get("status"),
            "final_url": page_data.get("final_url"),
            "html_length": page_data.get("html_length"),
            "json_candidate_count": page_data.get("json_candidate_count"),
            "scrapling_error": page_data.get("scrapling_error"),
        },
        "yt_dlp": ytdlp_data,
        "yt_dlp_error": ytdlp_error,
        "raw_stat_samples": page_data.get("raw_stat_samples") or [],
        "usage": {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "uses_scrapling": page_data.get("fetcher") == "scrapling_stealthy",
        },
    }


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Extract public TikTok/Douyin video metrics and author stats.")
    parser.add_argument("url", help="TikTok or Douyin public video URL")
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("SOCIAL_METRICS_TIMEOUT_MS", "60000")))
    parser.add_argument("--wait-ms", type=int, default=int(os.getenv("SOCIAL_METRICS_WAIT_MS", "6000")))
    args = parser.parse_args()

    try:
        url = validate_url(args.url)
        result = build_result(url, args.timeout_ms, args.wait_ms)
        output_path = Path(args.output) if args.output else ROOT / "output" / "social_metrics" / "metrics.json"
        write_json(output_path, result)
        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"Social video metrics extraction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
