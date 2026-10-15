"""Bounded text/PNG clipboard bridge for the dedicated browser's X display."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import subprocess
import threading
import time
from urllib.parse import urlsplit

from PIL import Image

API_PATH = "/api/remote-browser/clipboard"
MAX_BYTES = 16 * 1024 * 1024
LOCK = threading.Lock()


def validate(data, content_type):
    mime = content_type.split(";", 1)[0].strip().lower()
    if len(data) > MAX_BYTES:
        raise ValueError("剪贴板内容不能超过 16 MB")
    if mime == "text/plain":
        data.decode("utf-8")
        return "text/plain; charset=utf-8"
    if mime == "image/png":
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or image.width * image.height > 32_000_000:
                raise ValueError("仅支持不超过 3200 万像素的 PNG 图片")
            image.verify()
        return mime
    raise ValueError("剪贴板仅支持文字和 PNG 图片")


def xclip(*args, data=None):
    result = subprocess.run(["xclip", "-selection", "clipboard", *args], input=data,
                            stdout=subprocess.DEVNULL if data is not None else subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=5)
    if result.returncode:
        raise ValueError("远程剪贴板暂不可用，请重新复制后重试")
    output = result.stdout or b""
    if len(output) > MAX_BYTES:
        raise ValueError("远程剪贴板内容超过 16 MB")
    return output


def read_clipboard():
    try:
        targets = xclip("-o", "-t", "TARGETS").decode("ascii", errors="ignore").splitlines()
    except ValueError:
        return b"", "text/plain; charset=utf-8"
    if "image/png" in targets:
        data = xclip("-o", "-t", "image/png")
        return data, validate(data, "image/png")
    for target in ("UTF8_STRING", "text/plain;charset=utf-8", "STRING"):
        if target in targets:
            data = xclip("-o", "-t", target)
            if target == "STRING":
                data = data.decode("latin-1").encode("utf-8")
            return data, validate(data, "text/plain")
    return b"", "text/plain; charset=utf-8"


def write_clipboard(data, mime, paste=False):
    mime = validate(data, mime)
    xclip("-i", "-t", "image/png" if mime == "image/png" else "UTF8_STRING", data=data)
    if paste:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                       check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reply(handler, status, data, mime="application/json; charset=utf-8"):
    if not isinstance(data, bytes):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)


def request_body(handler):
    if handler.headers.get("X-Remote-Clipboard") != "1":
        raise PermissionError("请从远程浏览器页面操作剪贴板")
    length = int(handler.headers.get("Content-Length", "0"))
    if not 0 <= length <= MAX_BYTES:
        raise ValueError("剪贴板内容不能超过 16 MB")
    if handler.command == "POST" and handler.headers.get("Content-Type", "").split(";", 1)[0] not in {"text/plain", "image/png"}:
        raise ValueError("剪贴板仅支持文字和 PNG 图片")
    return handler.rfile.read(length) if handler.command == "POST" else b""


def forward(handler):
    """Called by the existing site; never forward site cookies or credentials."""
    parsed = urlsplit(handler.path)
    if parsed.path != API_PATH:
        return False
    connection = None
    try:
        data = request_body(handler)
        port = int(os.environ.get("WEB_PORT", "4002")) + 17000
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        query = parsed.query if parsed.query in {"paste=1", "copy=1", "cut=1"} else ""
        connection.request(handler.command, "/clipboard" + ("?" + query if query else ""),
                           body=data if handler.command == "POST" else None,
                           headers={"Content-Type": handler.headers.get("Content-Type", "text/plain"),
                                    "X-Remote-Clipboard": "1"})
        response = connection.getresponse()
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("远程剪贴板内容超过 16 MB")
        reply(handler, response.status, body, response.getheader("Content-Type", "text/plain"))
    except PermissionError as exc:
        reply(handler, 403, {"error": str(exc)})
    except ValueError as exc:
        reply(handler, 400, {"error": str(exc)})
    except (OSError, http.client.HTTPException):
        reply(handler, 502, {"error": "剪贴板服务暂不可用，请稍后重试"})
    finally:
        if connection:
            connection.close()
    return True


class ClipboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Clipboard content must not enter application logs.

    def do_GET(self):
        self.handle_clipboard()

    def do_POST(self):
        self.handle_clipboard()

    def handle_clipboard(self):
        parsed = urlsplit(self.path)
        if parsed.path != "/clipboard":
            return reply(self, 404, {"error": "Not found"})
        try:
            body = request_body(self)
            with LOCK:
                if self.command == "GET":
                    data, mime = read_clipboard()
                    return reply(self, 200, data, mime)
                if parsed.query in {"copy=1", "cut=1"}:
                    key = "ctrl+c" if parsed.query == "copy=1" else "ctrl+x"
                    subprocess.run(["xdotool", "key", "--clearmodifiers", key],
                                   check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(0.15)
                    data, mime = read_clipboard()
                    return reply(self, 200, data, mime)
                write_clipboard(body, self.headers.get("Content-Type", ""), parsed.query == "paste=1")
            reply(self, 200, {"ok": True})
        except PermissionError as exc:
            reply(self, 403, {"error": str(exc)})
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            reply(self, 400, {"error": str(exc)})
        except subprocess.SubprocessError:
            reply(self, 504, {"error": "远程剪贴板响应超时，请重试"})


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", "4002")) + 17000
    ThreadingHTTPServer(("127.0.0.1", port), ClipboardHandler).serve_forever()
