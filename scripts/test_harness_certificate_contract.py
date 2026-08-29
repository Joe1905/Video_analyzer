#!/usr/bin/env python3
"""Black-box contracts for the isolated Harness certificate endpoint."""

from __future__ import annotations

import ast
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
CERTIFICATE_PATH = "/harness-ca.crt"
CERTIFICATE_NAME = "harness-internal-ca.crt"
MISSING_MESSAGE = "Harness certificate is not available"


def request(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=b"{}" if method == "POST" else None)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def wait_for_port(process: subprocess.Popen[str], port_file: Path, log_file: Path) -> int:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if port_file.is_file():
            try:
                port = int(port_file.read_text(encoding="utf-8").strip())
            except ValueError:
                port = 0
            if port:
                try:
                    if request(port, "GET", "/healthz")[0] == 200:
                        return port
                except OSError:
                    pass
        if process.poll() is not None:
            break
        time.sleep(0.05)
    log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
    raise AssertionError(f"certificate test server did not become ready (exit={process.poll()}):\n{log}")


def write_import_stubs(test_root: Path) -> Path:
    """Provide only the unavailable import surface; this endpoint never calls it."""
    stub_dir = test_root / "import_stubs"
    stub_dir.mkdir()
    (stub_dir / "requests.py").write_text(
        """
class _Urllib3:
    def disable_warnings(self, *args, **kwargs):
        return None

class _Packages:
    urllib3 = _Urllib3()

packages = _Packages()

class Session:
    def request(self, *args, **kwargs):
        raise RuntimeError("requests is unavailable in the certificate contract test")

def request(*args, **kwargs):
    raise RuntimeError("requests is unavailable in the certificate contract test")

get = post = put = request
""".lstrip(),
        encoding="utf-8",
    )
    urllib3_dir = stub_dir / "urllib3"
    urllib3_dir.mkdir()
    (urllib3_dir / "__init__.py").write_text(
        "from .exceptions import InsecureRequestWarning\n\n"
        "def disable_warnings(*args, **kwargs):\n"
        "    return None\n",
        encoding="utf-8",
    )
    (urllib3_dir / "exceptions.py").write_text(
        "class InsecureRequestWarning(Warning):\n"
        "    pass\n",
        encoding="utf-8",
    )
    return stub_dir


@contextmanager
def isolated_server(test_root: Path) -> Iterator[int]:
    shutil.copytree(ROOT / "scripts", test_root / "scripts")
    port_file = test_root / "web.port"
    log_file = test_root / "web.log"
    # Avoid unrelated LAN-chat avatar generation (and Pillow) during startup.
    avatar = test_root / "data" / "lan_chat_avatars" / "public.png"
    avatar.parent.mkdir(parents=True, exist_ok=True)
    avatar.write_bytes(b"test-only-avatar")
    environment = os.environ.copy()
    stub_dir = write_import_stubs(test_root)
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
            "PYTHONPATH": str(stub_dir) + os.pathsep + environment.get("PYTHONPATH", ""),
        }
    )
    with log_file.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "scripts/web_app.py"],
            cwd=test_root,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            yield wait_for_port(process, port_file, log_file)
        finally:
            exited_before_cleanup = process.poll()
            if exited_before_cleanup is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if exited_before_cleanup not in {None, 0}:
            log = log_file.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"certificate test server exited unexpectedly ({exited_before_cleanup}):\n{log}")


class HarnessCertificateContractTests(unittest.TestCase):
    def assert_json_not_found(self, result: tuple[int, dict[str, str], bytes]) -> None:
        status, headers, body = result
        self.assertEqual(status, 404)
        self.assertTrue(headers.get("content-type", "").startswith("application/json"))
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "Not found"})
        self.assertNotIn("location", headers)

    def test_certificate_bytes_headers_query_and_legacy_methods(self) -> None:
        payload = b"test-only-harness-certificate\x00\xff"
        with tempfile.TemporaryDirectory(prefix="harness-certificate-") as temporary:
            test_root = Path(temporary)
            certificate = test_root / "data" / CERTIFICATE_NAME
            certificate.parent.mkdir(parents=True)
            certificate.write_bytes(payload)
            with isolated_server(test_root) as port:
                status, headers, body = request(port, "GET", CERTIFICATE_PATH)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-type"), "application/x-x509-ca-cert")
                self.assertEqual(
                    headers.get("content-disposition"),
                    f'attachment; filename="{CERTIFICATE_NAME}"',
                )
                self.assertEqual(headers.get("content-length"), str(len(payload)))
                self.assertEqual(headers.get("cache-control"), "no-store")
                self.assertEqual(body, payload)

                query_status, query_headers, query_body = request(port, "GET", f"{CERTIFICATE_PATH}?probe=1")
                self.assertEqual(query_status, status)
                self.assertEqual(query_body, body)
                for header in (
                    "content-type",
                    "content-disposition",
                    "content-length",
                    "cache-control",
                ):
                    self.assertEqual(query_headers.get(header), headers.get(header))

                self.assert_json_not_found(request(port, "GET", f"{CERTIFICATE_PATH}/"))
                self.assert_json_not_found(request(port, "POST", CERTIFICATE_PATH))
                self.assert_json_not_found(request(port, "DELETE", CERTIFICATE_PATH))
                head_status, head_headers, head_body = request(port, "HEAD", CERTIFICATE_PATH)
                self.assertEqual(head_status, 404)
                self.assertEqual(head_body, b"")
                self.assertNotIn("allow", head_headers)

    def test_missing_certificate_is_plaintext_not_found(self) -> None:
        with tempfile.TemporaryDirectory(prefix="harness-certificate-missing-") as temporary:
            test_root = Path(temporary)
            with isolated_server(test_root) as port:
                status, headers, body = request(port, "GET", CERTIFICATE_PATH)
                self.assertEqual(status, 404)
                self.assertEqual(headers.get("content-type"), "text/plain; charset=utf-8")
                self.assertEqual(headers.get("content-length"), str(len(MISSING_MESSAGE.encode("utf-8"))))
                self.assertEqual(headers.get("cache-control"), "no-cache, no-store, must-revalidate")
                self.assertEqual(body.decode("utf-8"), MISSING_MESSAGE)

    def test_router_wiring_replaces_the_handler_exact_branch(self) -> None:
        web_source = (ROOT / "scripts" / "web_app.py").read_text(encoding="utf-8")
        web_tree = ast.parse(web_source)
        route_imports = {
            alias.name
            for node in web_tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "routes.harness_certificate"
            for alias in node.names
        }
        self.assertEqual(route_imports, {"register_harness_certificate_route"})

        registrations = [
            node
            for node in ast.walk(web_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_harness_certificate_route"
        ]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(ast.unparse(registrations[0].args[0]), "WEB_ROUTER")
        self.assertEqual(
            {keyword.arg: ast.unparse(keyword.value) for keyword in registrations[0].keywords},
            {"certificate_path": "ROOT / 'data' / 'harness-internal-ca.crt'"},
        )

        handler_class = next(
            node
            for node in web_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Handler"
        )
        do_get = next(
            node
            for node in handler_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "do_GET"
        )
        self.assertNotIn(CERTIFICATE_PATH, ast.dump(do_get))

        route_path = ROOT / "scripts" / "routes" / "harness_certificate.py"
        route_tree = ast.parse(route_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(route_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("web_app", imported_modules)
        route_constants = {
            node.value
            for node in ast.walk(route_tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn(CERTIFICATE_PATH, route_constants)

if __name__ == "__main__":
    unittest.main()
