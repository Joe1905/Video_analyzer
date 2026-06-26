#!/usr/bin/env python3
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


LISTEN_PORT = int(os.getenv("SELLERSPRITE_REDIRECT_PORT", "3001") or "3001")
TARGET_PORT = int(os.getenv("WEB_PORT", "4002") or "4002")


class RedirectHandler(BaseHTTPRequestHandler):
    server_version = "SellerSpriteRedirect/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def target_location(self) -> str:
        parsed = urlparse(self.path)
        host = self.headers.get("Host", "localhost")
        if host.startswith("["):
            hostname = host.split("]", 1)[0] + "]"
        else:
            hostname = host.split(":", 1)[0]

        if parsed.path in {"", "/"}:
            target_path = "/amazon"
        elif parsed.path.startswith("/amazon"):
            target_path = parsed.path
        elif parsed.path == "/api" or parsed.path.startswith("/api/"):
            target_path = "/amazon" + parsed.path
        else:
            target_path = "/amazon" + parsed.path

        query = f"?{parsed.query}" if parsed.query else ""
        return f"http://{hostname}:{TARGET_PORT}{target_path}{query}"

    def redirect(self) -> None:
        status = HTTPStatus.FOUND if self.command in {"GET", "HEAD"} else HTTPStatus.TEMPORARY_REDIRECT
        self.send_response(status)
        self.send_header("Location", self.target_location())
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        self.redirect()

    def do_HEAD(self) -> None:
        self.redirect()

    def do_POST(self) -> None:
        self.redirect()

    def do_DELETE(self) -> None:
        self.redirect()


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), RedirectHandler)
    print(f"SellerSprite redirect listening on http://0.0.0.0:{LISTEN_PORT} -> :{TARGET_PORT}/amazon", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
