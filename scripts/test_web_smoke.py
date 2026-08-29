#!/usr/bin/env python3
"""Isolated HTTP smoke test for the V2 web server.

The test starts a disposable server process with all background work disabled.
It must not use the deployed 4004 data directory, MCP services, schedulers, or
external APIs.
"""
from __future__ import annotations

import ast
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PAGES = (
    "/",
    "/amazon",
    "/chuhaijiang",
    "/lan-chat",
    "/report",
    "/report/player",
    "/extract",
    "/shop",
    "/tool",
    "/metrics",
    "/taobao",
    "/harness",
)


def assert_unique_handler_methods() -> None:
    tree = ast.parse((ROOT / "scripts" / "web_app.py").read_text(encoding="utf-8"))
    handler = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Handler"
    )
    names = [
        node.name
        for node in handler.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"Handler has duplicate method definitions: {duplicates}"


def request(port: int, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, str], bytes]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def wait_for_port_file(process: subprocess.Popen[str], port_file: Path, log_file: Path) -> int:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if port_file.is_file():
            try:
                port = int(port_file.read_text(encoding="utf-8").strip())
            except ValueError:
                port = 0
            if port > 0:
                try:
                    status, _, _ = request(port, "GET", "/healthz")
                    if status == 200:
                        return port
                except OSError:
                    pass
        if process.poll() is not None:
            break
        time.sleep(0.05)
    log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
    raise AssertionError(f"smoke server did not become ready (exit={process.poll()}):\n{log}")


def assert_json_error(status: int, headers: dict[str, str], body: bytes, expected_status: int, expected_error: str) -> None:
    assert status == expected_status, (status, body.decode("utf-8", errors="replace"))
    assert headers.get("content-type", "").startswith("application/json")
    assert json.loads(body.decode("utf-8")) == {"error": expected_error}


def run_smoke(port: int) -> None:
    status, headers, body = request(port, "GET", "/healthz")
    assert status == 200
    assert headers.get("content-type", "").startswith("application/json")
    assert json.loads(body.decode("utf-8")) == {"status": "ok", "ui_test_mode": True}

    query_status, query_headers, query_body = request(port, "GET", "/healthz?probe=1")
    assert query_status == status
    assert query_body == body
    for key in ("content-type", "content-length", "cache-control", "allow"):
        assert query_headers.get(key) == headers.get(key), key

    status, headers, body = request(port, "GET", "/healthz/")
    assert_json_error(status, headers, body, 404, "Not found")
    for method in ("POST", "DELETE"):
        status, headers, body = request(port, method, "/healthz")
        assert_json_error(status, headers, body, 404, "Not found")
        assert "allow" not in headers, (method, headers)
    status, headers, body = request(port, "HEAD", "/healthz")
    assert status == 404
    assert body == b""
    assert "allow" not in headers

    for path in ("/report", "/report/player"):
        status, headers, body = request(port, "GET", path)
        assert status == 200, (path, status, body.decode("utf-8", errors="replace"))
        query_status, query_headers, query_body = request(port, "GET", f"{path}?probe=1")
        assert query_status == status
        assert query_body == body
        for key in ("content-type", "content-length", "cache-control", "location"):
            assert query_headers.get(key) == headers.get(key), (path, key)
        slash_status, slash_headers, slash_body = request(port, "GET", f"{path}/")
        assert_json_error(slash_status, slash_headers, slash_body, 404, "Not found")
        assert "location" not in slash_headers, path

    for path in ("/lan-chat", "/tool"):
        status, headers, body = request(port, "GET", path)
        assert status == 200, (path, status, body.decode("utf-8", errors="replace"))
        query_status, query_headers, query_body = request(port, "GET", f"{path}?probe=1")
        assert query_status == status
        assert query_body == body
        for key in ("content-type", "content-length", "cache-control", "location"):
            assert query_headers.get(key) == headers.get(key), (path, key)
        slash_status, slash_headers, slash_body = request(port, "GET", f"{path}/")
        assert_json_error(slash_status, slash_headers, slash_body, 404, "Not found")
        assert "location" not in slash_headers, path
        for method in ("POST", "DELETE"):
            method_status, method_headers, method_body = request(port, method, path, {} if method == "POST" else None)
            assert_json_error(method_status, method_headers, method_body, 404, "Not found")
            assert "allow" not in method_headers, (method, path)
        head_status, head_headers, head_body = request(port, "HEAD", path)
        assert head_status == 404
        assert head_body == b""
        assert "allow" not in head_headers

    status, headers, body = request(port, "GET", "/harness")
    assert status == 200, (status, body.decode("utf-8", errors="replace"))
    query_status, query_headers, query_body = request(port, "GET", "/harness?probe=1")
    assert query_status == status
    assert query_body == body
    for key in ("content-type", "content-length", "cache-control", "location"):
        assert query_headers.get(key) == headers.get(key), key
    slash_status, slash_headers, slash_body = request(port, "GET", "/harness/")
    assert_json_error(slash_status, slash_headers, slash_body, 404, "Not found")
    assert "location" not in slash_headers
    for method in ("POST", "DELETE"):
        method_status, method_headers, method_body = request(port, method, "/harness", {} if method == "POST" else None)
        assert_json_error(method_status, method_headers, method_body, 404, "Not found")
        assert "allow" not in method_headers
    head_status, head_headers, head_body = request(port, "HEAD", "/harness")
    assert head_status == 404
    assert head_body == b""
    assert "allow" not in head_headers

    for path in ("/shop", "/metrics", "/taobao"):
        status, headers, body = request(port, "GET", path)
        assert status == 200, (path, status)
        query_status, query_headers, query_body = request(port, "GET", f"{path}?probe=1")
        assert query_status == status and query_body == body
        for key in ("content-type", "content-length", "cache-control", "location"):
            assert query_headers.get(key) == headers.get(key), (path, key)
        slash_status, slash_headers, slash_body = request(port, "GET", f"{path}/")
        assert_json_error(slash_status, slash_headers, slash_body, 404, "Not found")
        assert "location" not in slash_headers
        for method in ("POST", "DELETE"):
            other_status, other_headers, other_body = request(port, method, path, {} if method == "POST" else None)
            assert_json_error(other_status, other_headers, other_body, 404, "Not found")
            assert "allow" not in other_headers
        head_status, head_headers, head_body = request(port, "HEAD", path)
        assert head_status == 404 and head_body == b"" and "allow" not in head_headers

    status, headers, body = request(port, "GET", "/extract")
    assert status == 200
    query_status, query_headers, query_body = request(port, "GET", "/extract?probe=1")
    assert query_status == status and query_body == body
    for key in ("content-type", "content-length", "cache-control", "location"):
        assert query_headers.get(key) == headers.get(key), key
    slash_status, slash_headers, slash_body = request(port, "GET", "/extract/")
    assert_json_error(slash_status, slash_headers, slash_body, 404, "Not found")
    assert "location" not in slash_headers
    for method in ("POST", "DELETE"):
        other_status, other_headers, other_body = request(port, method, "/extract", {} if method == "POST" else None)
        assert_json_error(other_status, other_headers, other_body, 404, "Not found")
        assert "allow" not in other_headers
    head_status, head_headers, head_body = request(port, "HEAD", "/extract")
    assert head_status == 404 and head_body == b"" and "allow" not in head_headers

    for path in ACTIVE_PAGES:
        status, headers, body = request(port, "GET", path)
        assert status == 200, (path, status, body.decode("utf-8", errors="replace"))
        assert headers.get("content-type", "").startswith("text/html"), path

    status, headers, body = request(port, "GET", "/__smoke_unknown__")
    assert_json_error(status, headers, body, 404, "Not found")
    assert "location" not in headers

    status, headers, body = request(port, "POST", "/__smoke_unknown__", {})
    assert_json_error(status, headers, body, 404, "Not found")
    assert "location" not in headers

    status, headers, body = request(port, "DELETE", "/__smoke_unknown__")
    assert_json_error(status, headers, body, 404, "Not found")
    assert "location" not in headers

    status, headers, body = request(port, "GET", "/api/chat/tool-catalog?provider=home")
    assert status == 200, (status, body.decode("utf-8", errors="replace"))
    assert headers.get("content-type", "").startswith("application/json")
    catalog = json.loads(body.decode("utf-8"))
    assert {domain["id"] for domain in catalog["domains"]} == {
        "system", "function", "sociavault", "sellersprite", "chuhaijiang",
    }

    for path in (
        "/api/chat/sessions?provider=unregistered-provider",
        "/api/chat/tool-catalog?provider=unregistered-provider",
    ):
        status, headers, body = request(port, "GET", path)
        assert_json_error(status, headers, body, 400, "Unknown chat provider")

    status, headers, body = request(port, "POST", "/api/report/run", {})
    assert_json_error(status, headers, body, 503, "日报功能已暂停")

    status, headers, body = request(port, "GET", "/proxy")
    assert status == 404
    assert headers.get("content-type", "").startswith("text/plain")
    assert body.decode("utf-8") == "Not found"

    status, headers, body = request(port, "GET", "/api/proxy/pools")
    assert_json_error(status, headers, body, 404, "Not found")

    status, headers, body = request(port, "POST", "/api/proxy/pools", {})
    assert_json_error(status, headers, body, 404, "Not found")


def main() -> int:
    assert_unique_handler_methods()
    with tempfile.TemporaryDirectory(prefix="video-analyzer-v2-smoke-") as temporary:
        test_root = Path(temporary)
        port_file = test_root / "web.port"
        log_file = test_root / "web.log"
        environment = os.environ.copy()
        environment.update(
            {
                "UI_TEST_MODE": "1",
                "APP_TEST_ROOT": str(test_root),
                "APP_TEST_PORT_FILE": str(port_file),
                "WEB_PORT": "0",
                "PROXY_POOL_ENABLED": "0",
                "HOT_VIDEO_REPORT_ENABLED": "0",
                "HOT_VIDEO_REPORT_SCHEDULER_ENABLED": "0",
                "SELLERSPRITE_REDIRECT_PORT": "0",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
                "http_proxy": "",
                "https_proxy": "",
                "all_proxy": "",
                "PYTHONUNBUFFERED": "1",
            }
        )
        with log_file.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                [sys.executable, "scripts/web_app.py"],
                cwd=ROOT,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                run_smoke(wait_for_port_file(process, port_file, log_file))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        if process.returncode not in {0, -15}:
            log = log_file.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"smoke server exited unexpectedly ({process.returncode}):\n{log}")
    print("web smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
