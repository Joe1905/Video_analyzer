#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


ROOT = Path.cwd()
DEFAULT_API_BASE = "https://api.sociavault.com"
DEFAULT_REGION = "US"
SHOP_PRODUCTS_PATH = "/v1/scrape/tiktok-shop/products"
PRODUCT_DETAILS_PATH = "/v1/scrape/tiktok-shop/product-details"
PRODUCT_REVIEWS_PATH = "/v1/scrape/tiktok-shop/product-reviews"


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


def validate_tiktok_shop_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https TikTok Shop URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        raise ValueError("Only tiktok.com URLs are supported")
    if len(cleaned) > 2048:
        raise ValueError("URL is too long")
    return cleaned


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def first_present(data: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return None


def response_items(data: Any) -> list[Any]:
    if not isinstance(data, dict):
        return []
    for key in ("products", "items", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = response_items(value)
            if nested:
                return nested
    return []


class SociaVaultClient:
    def __init__(self, api_key: str, api_base: str, timeout: float) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        cleaned_params = {key: value for key, value in params.items() if value not in (None, "")}
        response = requests.get(
            self.api_base + path,
            headers={
                "X-API-Key": self.api_key,
                "Accept": "application/json",
            },
            params=cleaned_params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected SociaVault response shape")
        return data


def collect_shop_products(client: SociaVaultClient, url: str, region: str, max_pages: int) -> dict[str, Any]:
    pages = []
    products = []
    cursor = ""
    for page_index in range(max_pages):
        data = client.get(
            SHOP_PRODUCTS_PATH,
            {
                "url": url,
                "cursor": cursor,
                "region": region,
            },
        )
        pages.append(data)
        products.extend(response_items(data))
        next_cursor = first_present(data, ("next_cursor", "nextCursor", "cursor"))
        if not next_cursor or str(next_cursor) == str(cursor):
            break
        cursor = str(next_cursor)
        print(f"Fetched shop products page {page_index + 1}; next cursor={cursor}")
    return {
        "source_type": "shop",
        "shop_url": url,
        "region": region,
        "pages_requested": max_pages,
        "pages_fetched": len(pages),
        "product_count": len(products),
        "products": products,
        "raw_pages": pages,
    }


def collect_product(client: SociaVaultClient, url: str, region: str, review_pages: int, related_videos: bool) -> dict[str, Any]:
    details = client.get(
        PRODUCT_DETAILS_PATH,
        {
            "url": url,
            "region": region,
            "get_related_videos": str(related_videos).lower(),
        },
    )
    product_id = str(first_present(details, ("product_id", "productId", "id")) or "")
    reviews = []
    raw_review_pages = []
    for page in range(1, review_pages + 1):
        params: dict[str, Any] = {"page": page, "region": region}
        if product_id:
            params["product_id"] = product_id
        else:
            params["url"] = url
        data = client.get(PRODUCT_REVIEWS_PATH, params)
        raw_review_pages.append(data)
        page_reviews = response_items(data)
        reviews.extend(page_reviews)
        if not page_reviews:
            break
        print(f"Fetched product reviews page {page}; reviews={len(page_reviews)}")
    return {
        "source_type": "product",
        "product_url": url,
        "region": region,
        "product_id": product_id,
        "details": details,
        "review_pages_requested": review_pages,
        "review_pages_fetched": len(raw_review_pages),
        "review_count": len(reviews),
        "reviews": reviews,
        "raw_review_pages": raw_review_pages,
    }


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="Extract TikTok Shop data with SociaVault.")
    parser.add_argument("url", help="TikTok Shop or TikTok Shop product URL")
    parser.add_argument("--source-type", choices=("shop", "product"), default="product")
    parser.add_argument("--region", default=os.getenv("SOCIAVAULT_REGION", DEFAULT_REGION))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("SOCIAVAULT_MAX_PAGES", "1")))
    parser.add_argument("--review-pages", type=int, default=int(os.getenv("SOCIAVAULT_REVIEW_PAGES", "1")))
    parser.add_argument("--related-videos", action="store_true")
    parser.add_argument("--api-base", default=os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--api-key", default=os.getenv("SOCIAVAULT_API_KEY", ""))
    parser.add_argument("--output", default="")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("SOCIAVAULT_TIMEOUT", "180")))
    args = parser.parse_args()

    if not args.api_key:
        print("Missing required environment variable: SOCIAVAULT_API_KEY", file=sys.stderr)
        return 1
    if args.max_pages < 1 or args.max_pages > 20:
        print("--max-pages must be between 1 and 20", file=sys.stderr)
        return 1
    if args.review_pages < 0 or args.review_pages > 20:
        print("--review-pages must be between 0 and 20", file=sys.stderr)
        return 1

    try:
        url = validate_tiktok_shop_url(args.url)
        client = SociaVaultClient(args.api_key, args.api_base, args.timeout)
        started = time.monotonic()
        if args.source_type == "shop":
            result = collect_shop_products(client, url, args.region, args.max_pages)
        else:
            result = collect_product(client, url, args.region, args.review_pages, args.related_videos)
        result["usage"] = {
            "api_provider": "sociavault",
            "api_base": args.api_base.rstrip("/"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        output_path = Path(args.output) if args.output else ROOT / "output" / "tiktok_shop" / "shop_extract.json"
        write_json(output_path, result)
        print(f"Wrote {output_path}")
        return 0
    except Exception as exc:
        print(f"TikTok Shop extraction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
