"""noVNC transport protected by the public site's authentik gateway.

The gateway must authenticate the entire prefix, including WebSocket upgrades.
The application upstream remains private, as for the existing /proxy page.
"""
import http.client
import re
import select
import socket
from urllib.parse import unquote, urlsplit

PREFIX = "/proxy/observation/"


def handle(handler, allowed_ports):
    parsed = urlsplit(handler.path)
    if not parsed.path.startswith(PREFIX):
        return False
    match = re.fullmatch(r"/proxy/observation/([0-9]+)/(.+)", parsed.path)
    if not match or int(match[1]) not in allowed_ports:
        handler.send_error(404, "Observation channel not found")
        return True
    path = unquote(match[2])
    if path.startswith("/") or "\\" in path or any(p in {".", ".."} for p in path.split("/")):
        handler.send_error(400, "Invalid observation path")
        return True
    port = int(match[1])
    target = "/" + match[2] + ("?" + parsed.query if parsed.query else "")
    upgrade = handler.headers.get("Upgrade", "").lower() == "websocket"
    if upgrade and path != "websockify":
        handler.send_error(404, "Unknown WebSocket endpoint")
        return True
    handler.close_connection = True
    started = False
    upstream = connection = None
    try:
        if upgrade:
            # Do not buffer any WebSocket bytes following the handshake.
            upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
            headers = [f"GET {target} HTTP/1.1", f"Host: 127.0.0.1:{port}",
                       "Upgrade: websocket", "Connection: Upgrade"]
            for name in ("Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Protocol"):
                value = handler.headers.get(name)
                if value:
                    headers.append(f"{name}: {value}")
            upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
            response = bytearray()
            while not response.endswith(b"\r\n\r\n"):
                chunk = upstream.recv(1)
                if not chunk or len(response) >= 16384:
                    raise OSError("Invalid noVNC handshake")
                response.extend(chunk)
            if response.split(b" ", 2)[1] != b"101":
                raise OSError("noVNC rejected WebSocket handshake")
            handler.wfile.write(response)
            handler.wfile.flush()
            started = True
            handler.connection.settimeout(30)
            upstream.settimeout(30)
            while True:
                readable, _, _ = select.select([handler.connection, upstream], [], [], 300)
                if not readable:
                    break
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return True
                    destination = upstream if source is handler.connection else handler.connection
                    destination.sendall(data)
        else:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            # Never forward authentik cookies or credentials to noVNC.
            connection.request("GET", target)
            response = connection.getresponse()
            handler.send_response(response.status)
            for name in ("Content-Type", "Content-Length", "Last-Modified"):
                value = response.getheader(name)
                if value is not None:
                    handler.send_header(name, value)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()
            started = True
            while chunk := response.read(65536):
                handler.wfile.write(chunk)
    except (OSError, http.client.HTTPException):
        if not started:
            handler.send_error(502, "Observation channel unavailable")
    finally:
        if upstream:
            upstream.close()
        if connection:
            connection.close()
    return True
