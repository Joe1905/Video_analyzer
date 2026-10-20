"""Small authenticated media ingress; public GET/HEAD supports byte ranges.

Run only behind the existing HTTPS proxy, bound to its Docker bridge gateway.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "/buffer-media/"
MAX_BYTES = 1024 ** 3
ROOT = Path(os.getenv("BUFFER_MEDIA_ROOT", "/var/lib/video-analyzer-media"))
TOKEN_FILE = Path(os.getenv("BUFFER_MEDIA_TOKEN_FILE", "/etc/video-analyzer-media/token"))
UPLOAD_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "Media"

    def setup(self):
        super().setup()
        self.connection.settimeout(180)

    def log_message(self, *_args):
        # Do not log headers, query strings or authentication data.
        pass

    def target(self):
        match = re.fullmatch(r"/buffer-media/([0-9a-f]{32}\.mp4)", urlsplit(self.path).path)
        return ROOT / match[1] if match else None

    def result(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        self.close_connection = True
        token = TOKEN_FILE.read_text().strip()
        if not token or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            return self.result(401, {"error": "Unauthorized"})
        target = self.target()
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not target or not 0 < size <= MAX_BYTES or self.headers.get("Content-Type") != "video/mp4":
            return self.result(400, {"error": "Invalid video"})
        if not UPLOAD_LOCK.acquire(blocking=False):
            return self.result(429, {"error": "Upload busy"})
        temporary = ROOT / (uuid.uuid4().hex + ".part")
        try:
            if shutil.disk_usage(ROOT).free < size + 2 * 1024 ** 3:
                return self.result(507, {"error": "Insufficient storage"})
            digest = hashlib.sha256()
            remaining = size
            header = b""
            with temporary.open("xb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("Interrupted upload")
                    if not header:
                        header = chunk[:32]
                    digest.update(chunk)
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if len(header) < 12 or header[4:8] != b"ftyp":
                return self.result(400, {"error": "Not MP4"})
            checksum = digest.hexdigest()
            if target.exists():
                old = hashlib.sha256()
                with target.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        old.update(chunk)
                if old.hexdigest() != checksum:
                    return self.result(409, {"error": "Existing asset differs"})
                return self.result(200, {"sha256": checksum, "size": size})
            os.replace(temporary, target)
            self.result(201, {"sha256": checksum, "size": size})
        except (OSError, TimeoutError):
            self.result(400, {"error": "Upload interrupted"})
        finally:
            temporary.unlink(missing_ok=True)
            UPLOAD_LOCK.release()

    def serve_file(self, head=False):
        target = self.target()
        if not target or not target.is_file():
            self.send_error(404)
            return
        with target.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            start, end = 0, size - 1
            partial = self.headers.get("Range")
            if partial:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", partial)
                try:
                    if not match or not any(match.groups()):
                        raise ValueError()
                    if not match[1]:
                        suffix = int(match[2])
                        if suffix < 1:
                            raise ValueError()
                        start = max(0, size - suffix)
                    else:
                        start = int(match[1])
                        end = min(int(match[2]), end) if match[2] else end
                    if start > end or start >= size:
                        raise ValueError()
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "public, max-age=86400, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            source.seek(start)
            remaining = end - start + 1
            try:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def do_GET(self):
        self.serve_file()

    def do_HEAD(self):
        self.serve_file(head=True)


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    # Incomplete files from an interrupted service are never publicly served.
    for part in ROOT.glob("*.part"):
        part.unlink()
    ThreadingHTTPServer((os.environ["BUFFER_MEDIA_BIND"], 8788), Handler).serve_forever()
