"""HTTP response helpers shared by the web application.

This module intentionally contains only standard-library dependencies so it can be
tested without importing the application server.
"""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote


def json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: Any,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.end_headers()
    try:
        handler.wfile.write(encoded)
    except (BrokenPipeError, ConnectionResetError):
        pass


def binary_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
    filename: str | None = None,
    cache_control: str | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        quoted = filename.replace('"', "")
        handler.send_header("Content-Disposition", f'attachment; filename="{quoted}"')
    if cache_control:
        handler.send_header("Cache-Control", cache_control)
    handler.end_headers()
    try:
        if handler.command != "HEAD":
            handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def file_response(
    handler: BaseHTTPRequestHandler,
    path: Path,
    content_type: str,
    filename: str,
    size: int,
    download: bool = True,
) -> None:
    file_size = max(0, int(size))
    start = 0
    end = max(0, file_size - 1)
    status = HTTPStatus.OK
    range_header = handler.headers.get("Range", "").strip()
    if range_header:
        if not range_header.startswith("bytes=") or "," in range_header:
            handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.end_headers()
            return
        try:
            start_text, end_text = range_header[6:].split("-", 1)
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else end
            else:
                suffix = int(end_text)
                if suffix <= 0:
                    raise ValueError
                start = max(0, file_size - suffix)
            end = min(end, file_size - 1)
        except ValueError:
            handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.end_headers()
            return
        if file_size <= 0 or start < 0 or start >= file_size or start > end:
            handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.end_headers()
            return
        status = HTTPStatus.PARTIAL_CONTENT

    length = file_size if file_size else 0
    if status == HTTPStatus.PARTIAL_CONTENT:
        length = end - start + 1
    handler.send_response(status)
    handler.send_header("Content-Type", content_type or "application/octet-stream")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    if status == HTTPStatus.PARTIAL_CONTENT:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
    if download:
        encoded_name = quote(filename, safe="")
        handler.send_header(
            "Content-Disposition",
            f"attachment; filename=download; filename*=UTF-8''{encoded_name}",
        )
        handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if handler.command == "HEAD" or not length:
        return
    try:
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def write_sse_event(handler: BaseHTTPRequestHandler, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.wfile.write(b"data: ")
    handler.wfile.write(body)
    handler.wfile.write(b"\n\n")
    handler.wfile.flush()
