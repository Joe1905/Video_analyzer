#!/usr/bin/env python3
"""SociaVault TikTok API — unified CLI for all 16 TikTok endpoints."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from api_cache import get_cached_or_call
from entity_registry import find_user, get_entity, videos_for_user
from sociavault_usage import update_sociavault_usage_from_response

ROOT = Path.cwd()
DEFAULT_API_BASE = "https://api.sociavault.com"
DEFAULT_REGION = "US"
VIDEO_INFO_TTL_SECONDS = 24 * 60 * 60

ENDPOINTS: dict[str, str] = {
    "profile":           "/v1/scrape/tiktok/profile",
    "videos":            "/v1/scrape/tiktok/videos",
    "videos-popular":    "/v1/scrape/tiktok/videos/popular",
    "followers":         "/v1/scrape/tiktok/followers",
    "following":         "/v1/scrape/tiktok/following",
    "demographics":      "/v1/scrape/tiktok/demographics",
    "video-info":        "/v1/scrape/tiktok/video-info",
    "comments":          "/v1/scrape/tiktok/comments",
    "comment-replies":   "/v1/scrape/tiktok/comment-replies",
    "transcript":        "/v1/scrape/tiktok/transcript",
    "live":              "/v1/scrape/tiktok/live",
    "search-users":      "/v1/scrape/tiktok/search/users",
    "search-hashtag":    "/v1/scrape/tiktok/search/hashtag",
    "search-keyword":    "/v1/scrape/tiktok/search/keyword",
    "search-music":      "/v1/scrape/tiktok/search/music",
    "search-top":        "/v1/scrape/tiktok/search/top",
    "trending":          "/v1/scrape/tiktok/trending",
    "creators-popular":  "/v1/scrape/tiktok/creators/popular",
    "hashtags-popular":  "/v1/scrape/tiktok/hashtags/popular",
    "music-popular":     "/v1/scrape/tiktok/music/popular",
    "music-info":        "/v1/scrape/tiktok/music/info",
    "music-videos":      "/v1/scrape/tiktok/music/videos",
}


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


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def first_present(data: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return None


def _video_id_from_url(url: str) -> str:
    import re
    match = re.search(r"/video/(\d+)", str(url or ""))
    return match.group(1) if match else ""


def registry_fallback(endpoint: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if endpoint == "videos":
        handle = str(first_present(params, ("handle", "user_id", "sec_uid")) or "").strip().lstrip("@")
        items = videos_for_user(handle, int(params.get("count") or 50))
        if items:
            return {
                "ok": True,
                "source": "entity_registry",
                "fallback_reason": "api_miss_or_empty",
                "videos": items,
                "data": {"videos": items},
                "_cache": {"hit": True, "provider": "entity_registry", "endpoint": "tiktok_user_videos", "label": "?????"},
            }
    if endpoint == "profile":
        handle = str(first_present(params, ("handle", "user_id", "sec_uid")) or "").strip().lstrip("@")
        user = find_user(handle)
        if user:
            snapshot = (user.get("extra") or {}).get("snapshot") if isinstance(user.get("extra"), dict) else None
            data = dict(snapshot) if isinstance(snapshot, dict) else {}
            data.setdefault("uid", user.get("entity_id"))
            data.setdefault("unique_id", user.get("author") or user.get("title"))
            data.setdefault("url", user.get("source_url"))
            data["_entity_registry"] = {"hit": True, "last_seen_at": user.get("last_seen_at")}
            return {"ok": True, "source": "entity_registry", "profile": data, "data": data, "_cache": {"hit": True, "provider": "entity_registry", "endpoint": "tiktok_user", "label": "?????"}}
    if endpoint == "video-info":
        video_id = _video_id_from_url(str(params.get("url") or "")) or str(first_present(params, ("video_id", "aweme_id", "item_id")) or "")
        entity = get_entity("tiktok_video", video_id) if video_id else None
        if entity:
            extra = entity.get("extra") or {}
            snapshot = extra.get("snapshot") if isinstance(extra.get("snapshot"), dict) else {}
            data = dict(snapshot) if snapshot else {}
            data.setdefault("aweme_id", entity.get("entity_id"))
            data.setdefault("video_id", entity.get("entity_id"))
            data.setdefault("desc", entity.get("title"))
            data.setdefault("url", entity.get("source_url"))
            data.setdefault("author", {"unique_id": entity.get("author")})
            if extra.get("stats"):
                data.setdefault("statistics", extra.get("stats"))
            data["_entity_registry"] = {"hit": True, "last_seen_at": entity.get("last_seen_at"), "source_url": entity.get("source_url")}
            return {"ok": True, "source": "entity_registry", "video": data, "data": data, "_cache": {"hit": True, "provider": "entity_registry", "endpoint": "tiktok_video", "label": "?????"}}
    return None


def is_empty_api_result(endpoint: str, payload: dict[str, Any]) -> bool:
    if endpoint == "videos":
        for key in ("videos", "items", "aweme_list", "data"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                return False
            if isinstance(value, dict):
                nested = value.get("videos") or value.get("items") or value.get("aweme_list")
                if isinstance(nested, list) and nested:
                    return False
        return True
    return False


def call_api(
    api_key: str,
    api_base: str,
    endpoint: str,
    params: dict[str, Any],
    timeout: float,
    cache_policy: str = "read_write",
) -> dict[str, Any]:
    path = ENDPOINTS[endpoint]
    cleaned = {k: v for k, v in params.items() if v not in (None, "")}
    request_key = {"api_base": api_base.rstrip("/"), "endpoint": endpoint, "params": cleaned}

    def fetch() -> dict[str, Any]:
        response = requests.get(
            api_base.rstrip("/") + path,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            params=cleaned,
            timeout=timeout,
        )
        response_body: Any | None = None
        try:
            response_body = response.json()
        except ValueError:
            response_body = None
        update_sociavault_usage_from_response(response, response_body)
        if response.status_code >= 400:
            raise requests.HTTPError(
                f"{response.status_code} {response.reason}: {response.text[:1000]}",
                response=response,
            )
        data = response_body
        if not isinstance(data, dict):
            raise ValueError("Unexpected SociaVault response shape")
        return data

    try:
        data = get_cached_or_call(
            "sociavault_tiktok",
            endpoint,
            request_key,
            fetch,
            ttl_seconds=VIDEO_INFO_TTL_SECONDS if endpoint == "video-info" else None,
            cache_policy=cache_policy,
            metadata_builder=lambda data: {
                "entity_type": "tiktok",
                "entity_id": str(first_present(cleaned, ("url", "handle", "query", "hashtag", "sound_id")) or endpoint),
                "source_url": str(cleaned.get("url") or ""),
            },
        )
    except Exception:
        fallback = registry_fallback(endpoint, cleaned)
        if fallback is not None:
            return fallback
        raise
    if is_empty_api_result(endpoint, data):
        fallback = registry_fallback(endpoint, cleaned)
        if fallback is not None:
            return fallback
    return data


def build_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.handle:
        params["handle"] = args.handle.lstrip("@")
    if args.url:
        params["url"] = args.url
    if args.query:
        params["query"] = args.query
    if args.hashtag:
        params["hashtag"] = args.hashtag.lstrip("#")
    if args.sound_id:
        params["sound_id"] = args.sound_id
    if args.count is not None:
        params["count"] = args.count
    if getattr(args, "days", None) is not None:
        params["days"] = args.days
    if getattr(args, "page", None) is not None:
        params["page"] = args.page
    if args.sort_by:
        params["sort_by"] = args.sort_by
    if args.cursor:
        params["max_cursor"] = args.cursor
    if args.region:
        params["region"] = args.region
    if args.trim:
        params["trim"] = "true"
    return params


def normalize_endpoint_params(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(params)
    if endpoint == "search-music" and normalized.get("query"):
        normalized["keyword"] = normalized.pop("query")
    return normalized


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="SociaVault TikTok API client")
    parser.add_argument("--endpoint", required=True, choices=list(ENDPOINTS.keys()),
                        help="TikTok API endpoint to call")
    parser.add_argument("--handle", default="", help="TikTok username/handle")
    parser.add_argument("--url", default="", help="TikTok video URL")
    parser.add_argument("--query", default="", help="Search query")
    parser.add_argument("--hashtag", default="", help="Hashtag (without #)")
    parser.add_argument("--sound-id", default="", help="Music/sound ID")
    parser.add_argument("--count", type=int, default=10, help="Number of results (default: 10)")
    parser.add_argument("--days", type=int, default=None, help="Lookback window in days for endpoints that support it")
    parser.add_argument("--page", type=int, default=None, help="Page number for endpoints that support it")
    parser.add_argument("--sort-by", default="", help="Sort order (e.g. most-liked, date-posted)")
    parser.add_argument("--cursor", default="", help="Pagination cursor")
    parser.add_argument("--trim", action="store_true", help="Request trimmed response")
    parser.add_argument("--region", default=os.getenv("SOCIAVAULT_REGION", "US"))
    parser.add_argument("--api-base", default=os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--api-key", default=os.getenv("SOCIAVAULT_API_KEY", ""))
    parser.add_argument("--output", default="", help="Output JSON file path")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("SOCIAVAULT_TIMEOUT", "180")))
    args = parser.parse_args()

    if not args.api_key:
        print("Missing required environment variable: SOCIAVAULT_API_KEY", file=sys.stderr)
        return 1

    try:
        params = normalize_endpoint_params(args.endpoint, build_params(args))
        started = time.monotonic()
        result = call_api(args.api_key, args.api_base, args.endpoint, params, args.timeout)
        result["_meta"] = {
            "endpoint": args.endpoint,
            "params": params,
            "api_base": args.api_base.rstrip("/"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        output_path = Path(args.output) if args.output else ROOT / "output" / "tiktok_api" / f"{args.endpoint}.json"
        write_json(output_path, result)
        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"SociaVault TikTok API call failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
