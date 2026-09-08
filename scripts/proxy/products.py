"""Proxy-managed product catalog workflows."""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlparse

from proxy.nodes import _clean_text
from proxy.repository import connect
from proxy.settings import now_iso


def _row_to_product(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "product_id": row["product_id"],
        "product_name": row["product_name"],
        "product_url": row["product_url"],
        "image_url": row["image_url"],
        "price": row["price"],
        "stock": row["stock"],
        "status": row["status"],
        "source": row["source"],
        "sort_order": int(row["sort_order"]),
        "updated_at": row["updated_at"],
    }


def list_products() -> dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM tiktok_products ORDER BY source, sort_order, product_name").fetchall()
    finally:
        conn.close()
    return {"products": [_row_to_product(row) for row in rows]}


def _product_payload(raw: dict[str, Any], *, source: str = "universal") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("商品数据格式不正确")
    product_id = _clean_text(raw.get("product_id"), 120)
    product_name = _clean_text(raw.get("product_name") or raw.get("title"), 2000)
    if not product_id or not product_name:
        raise ValueError("商品必须包含 product_id 和 product_name")
    if not product_id.isdigit():
        raise ValueError("TikTok Shop 商品 ID 必须是纯数字")
    product_url = _clean_text(raw.get("product_url") or raw.get("url"), 4000)
    if product_url:
        parsed = urlparse(product_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            raise ValueError("商品链接必须是 TikTok Shop 的 http/https 链接")
    return {
        "product_id": product_id,
        "product_name": product_name,
        "product_url": product_url,
        "image_url": _clean_text(raw.get("image_url"), 4000),
        "price": _clean_text(raw.get("price"), 80),
        "stock": _clean_text(raw.get("stock"), 80),
        "status": _clean_text(raw.get("status"), 80) or "Active",
        "source": _clean_text(source, 80) or "universal",
        "sort_order": int(raw.get("sort_order") or 0),
    }


def create_product(raw: dict[str, Any]) -> dict[str, Any]:
    product = _product_payload(raw)
    now = now_iso()
    with connect() as conn:
        duplicate = conn.execute(
            "SELECT product_id FROM tiktok_products WHERE product_id = ?",
            (product["product_id"],),
        ).fetchone()
        if duplicate:
            raise ValueError(f"商品 ID {product['product_id']} 已存在于通用商品库")
        conn.execute(
            """
            INSERT INTO tiktok_products (
                product_id, product_name, product_url, image_url, price, stock,
                status, source, sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product["product_id"], product["product_name"], product["product_url"],
                product["image_url"], product["price"], product["stock"], product["status"],
                product["source"], product["sort_order"], now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tiktok_products WHERE product_id = ?", (product["product_id"],)).fetchone()
    return {"product": _row_to_product(row), **list_products()}


def update_product(raw: dict[str, Any]) -> dict[str, Any]:
    product = _product_payload(raw)
    now = now_iso()
    with connect() as conn:
        existing = conn.execute("SELECT * FROM tiktok_products WHERE product_id = ?", (product["product_id"],)).fetchone()
        if not existing:
            raise ValueError("通用商品不存在或已被删除")
        conn.execute(
            """
            UPDATE tiktok_products
            SET product_name = ?, product_url = ?, image_url = ?, price = ?, stock = ?,
                status = ?, sort_order = ?, updated_at = ?
            WHERE product_id = ?
            """,
            (
                product["product_name"], product["product_url"], product["image_url"],
                product["price"], product["stock"], product["status"], product["sort_order"],
                now, product["product_id"],
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tiktok_products WHERE product_id = ?", (product["product_id"],)).fetchone()
    return {"product": _row_to_product(row), **list_products()}


def delete_product(product_id: str) -> dict[str, Any]:
    cleaned_id = _clean_text(product_id, 120)
    if not cleaned_id:
        raise ValueError("product_id is required")
    with connect() as conn:
        existing = conn.execute("SELECT product_id FROM tiktok_products WHERE product_id = ?", (cleaned_id,)).fetchone()
        if not existing:
            raise ValueError("通用商品不存在或已被删除")
        pending = conn.execute(
            """
            SELECT id FROM publish_jobs
            WHERE product_link = ? AND deleted_at = ''
              AND status NOT IN ('published','scheduled_on_tiktok','cancelled','dry_run')
            LIMIT 1
            """,
            (cleaned_id,),
        ).fetchone()
        if pending:
            raise ValueError("商品仍被待处理或可重试的发布任务使用，请先移除任务中的商品")
        conn.execute("DELETE FROM tiktok_products WHERE product_id = ?", (cleaned_id,))
        conn.commit()
    return {"deleted_product_id": cleaned_id, **list_products()}
