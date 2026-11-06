"""Serve the page editor through its private HTTP Unix socket."""

import http.client
import re
import socket


SOCKET_PATH = "/run/neurobuddie-editor/editor.sock"
BUFFER_SIZE = 64 * 1024
REQUEST_HEADERS = {
    "accept", "accept-encoding", "accept-language", "content-encoding",
    "content-type", "if-match", "if-range", "origin", "range", "referer",
    "sec-fetch-site", "user-agent",
}
RESPONSE_EXCLUSIONS = {
    "age", "cache-control", "connection", "date", "expires", "keep-alive",
    "pragma", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "server", "set-cookie", "te", "trailer", "transfer-encoding", "upgrade",
}


def _connection_headers(headers):
    return {
        item.strip().lower()
        for value in headers.get_all("Connection", [])
        for item in value.split(",")
    }


def _error(handler, status, message):
    body = message.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def proxy_page_editor(handler):
    handler.close_connection = True
    target = handler.path.removeprefix("/page-editor")
    if not target.startswith("/") or target.startswith("//"):
        return _error(handler, 400, "Invalid page editor path")

    hosts = handler.headers.get_all("Host", [])
    if len(hosts) != 1 or not hosts[0]:
        return _error(handler, 400, "A single Host header is required")
    if "Transfer-Encoding" in handler.headers:
        return _error(handler, 400, "Use Content-Length for editor uploads")
    lengths = handler.headers.get_all("Content-Length", [])
    if lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,19}", lengths[0])):
        return _error(handler, 400, "Invalid Content-Length")

    conn = http.client.HTTPConnection("localhost", timeout=120)
    started = False
    try:
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.settimeout(conn.timeout)
        conn.sock.connect(SOCKET_PATH)
        conn.putrequest(handler.command, target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", hosts[0])
        allowed = REQUEST_HEADERS - _connection_headers(handler.headers)
        for key, value in handler.headers.items():
            if key.lower() in allowed:
                conn.putheader(key, value)
        if lengths:
            conn.putheader("Content-Length", lengths[0])
        conn.putheader("Connection", "close")
        conn.endheaders()
        remaining = int(lengths[0]) if lengths else 0
        while remaining:
            data = handler.rfile.read1(min(remaining, BUFFER_SIZE))
            if not data:
                return _error(handler, 400, "Incomplete request body")
            try:
                conn.send(data)
            except (BrokenPipeError, ConnectionResetError):
                # The editor may reject an upload before reading its whole body.
                break
            remaining -= len(data)

        with conn.getresponse() as response:
            excluded = RESPONSE_EXCLUSIONS | _connection_headers(response.headers)
            if response.chunked:
                excluded.add("content-length")
            handler.send_response(response.status)
            for key, value in response.getheaders():
                name = key.lower()
                if name in excluded:
                    continue
                if (name == "location" and value.startswith("/") and not value.startswith("//")
                        and not re.match(r"^/page-editor(?:[/#?]|$)", value)):
                    value = "/page-editor" + value
                handler.send_header(key, value)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            started = True
            handler.end_headers()
            while data := response.read1(BUFFER_SIZE):
                handler.wfile.write(data)
    except (OSError, http.client.HTTPException, ValueError):
        if not started:
            return _error(handler, 502, "Page editor unavailable")
    finally:
        conn.close()
