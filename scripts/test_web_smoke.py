#!/usr/bin/env python3
"""Isolated HTTP smoke test for the V2 web server.

The test starts a disposable server process with all background work disabled.
It must not use the deployed 4004 data directory, MCP services, schedulers, or
external APIs.
"""
from __future__ import annotations

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

    for path in ACTIVE_PAGES:
        status, headers, body = request(port, "GET", path)
        assert status == 200, (path, status, body.decode("utf-8", errors="replace"))
        assert headers.get("content-type", "").startswith("text/html"), path

    status, headers, body = request(port, "GET", "/__smoke_unknown__")
    assert_json_error(status, headers, body, 404, "Not found")

    status, headers, body = request(port, "POST", "/__smoke_unknown__", {})
    assert_json_error(status, headers, body, 404, "Not found")

    status, headers, body = request(port, "DELETE", "/__smoke_unknown__")
    assert_json_error(status, headers, body, 404, "Not found")

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
