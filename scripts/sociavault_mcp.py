"""Bounded stdio client for the pinned official SociaVault MCP server."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import time


def request(method, params=None):
    if not os.getenv("SOCIAVAULT_API_KEY", "").strip():
        raise RuntimeError("SociaVault API Key 未配置")
    env = dict(os.environ)
    env["SOCIAVAULT_BASE_URL"] = os.getenv("SOCIAVAULT_API_BASE", "https://api.sociavault.com")
    entry = Path.cwd() / "data" / "sociavault-mcp" / "node_modules" / "sociavault-mcp" / "dist" / "index.js"
    if not entry.is_file():
        raise RuntimeError("请先运行 scripts/setup_sociavault_mcp.sh 安装官方 MCP")
    # Node's automatic address selection can stall TLS on the deployment network.
    # Scope the verified IPv4 path to this MCP child, not other Node services.
    process = subprocess.Popen(["node", "--no-network-family-autoselection",
                                "--dns-result-order=ipv4first", str(entry)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b""
    deadline = time.monotonic() + 150
    sequence = 0

    def send(payload):
        process.stdin.write(json.dumps(payload).encode() + b"\n")
        process.stdin.flush()

    def rpc(name, arguments):
        nonlocal sequence, buffer
        sequence += 1
        send({"jsonrpc": "2.0", "id": sequence, "method": name, "params": arguments})
        while time.monotonic() < deadline:
            if b"\n" not in buffer:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("SociaVault MCP 进程已退出")
                buffer += chunk
                if len(buffer) > 32 * 1024 * 1024:
                    raise RuntimeError("SociaVault MCP 响应超过大小限制")
                continue
            line, buffer = buffer.split(b"\n", 1)
            result = json.loads(line)
            if result.get("id") != sequence:
                continue
            if "error" in result:
                raise RuntimeError("SociaVault MCP 请求失败")
            return result.get("result", {})
        raise RuntimeError("SociaVault MCP 请求超时")

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "video-analyzer", "version": "1.0"}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = rpc(method, params or {})
        if method == "tools/list":
            tools = list(result.get("tools", []))
            seen = set()
            while result.get("nextCursor"):
                cursor = result["nextCursor"]
                if cursor in seen:
                    raise RuntimeError("SociaVault MCP 工具分页游标重复")
                seen.add(cursor)
                result = rpc(method, {"cursor": cursor})
                tools.extend(result.get("tools", []))
            return {"tools": sorted(tools, key=lambda tool: tool["name"])}
        return result
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
