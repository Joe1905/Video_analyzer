#!/usr/bin/env python3
"""Black-box contracts for the GET-only /assets/ file boundary."""

from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
CACHE_CONTROL = "no-cache, no-store, must-revalidate"


def request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        body = b"{}" if method == "POST" else None
        connection.request(method, path, body=body, headers=headers or {})
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
                if request(port, "GET", "/healthz")[0] == 200:
                    return port
            except (OSError, ValueError):
                pass
        if process.poll() is not None:
            break
        time.sleep(0.1)
    output = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
    raise AssertionError(f"isolated web server did not start (exit={process.poll()}):\n{output}")


def write_import_stubs(test_root: Path) -> Path:
    """Supply only the optional HTTP packages needed for test-mode server startup."""
    stub_root = test_root / "import_stubs"
    urllib3_dir = stub_root / "urllib3"
    urllib3_dir.mkdir(parents=True)
    (stub_root / "requests.py").write_text(
        """
class _Urllib3:
    @staticmethod
    def disable_warnings(*args, **kwargs):
        return None


class _Packages:
    urllib3 = _Urllib3()


packages = _Packages()


class Session:
    def request(self, *args, **kwargs):
        raise RuntimeError(\"network access is disabled in this contract test\")


def request(*args, **kwargs):
    raise RuntimeError(\"network access is disabled in this contract test\")


get = post = put = request
""".lstrip(),
        encoding="utf-8",
    )
    (urllib3_dir / "__init__.py").write_text(
        """
def disable_warnings(*args, **kwargs):
    return None
""".lstrip(),
        encoding="utf-8",
    )
    (urllib3_dir / "exceptions.py").write_text(
        """
class InsecureRequestWarning(Warning):
    pass
""".lstrip(),
        encoding="utf-8",
    )
    return stub_root


def prepare_workspace(test_root: Path) -> tuple[Path, Path]:
    shutil.copytree(ROOT / "scripts", test_root / "scripts")
    assets_dir = test_root / "scripts" / "static" / "assets"
    shutil.rmtree(assets_dir)
    assets_dir.mkdir(parents=True)
    (assets_dir / "nested").mkdir()
    (assets_dir / "folder").mkdir()
    (assets_dir / "plain.css").write_bytes(b"body{color:#123}")
    (assets_dir / "nested" / "app.js").write_bytes(b"console.log('nested');")
    (assets_dir / "unknown.fixture").write_bytes(b"\x00fixture\xff")
    outside = test_root / "outside.txt"
    outside.write_bytes(b"outside")
    (test_root / "data" / "lan_chat_avatars").mkdir(parents=True)
    (test_root / "data" / "lan_chat_avatars" / "public.png").write_bytes(b"fixture")
    return assets_dir, outside


@contextmanager
def isolated_server(test_root: Path) -> Iterator[int]:
    port_file = test_root / "web-port.txt"
    log_file = test_root / "web.log"
    stub_root = write_import_stubs(test_root)
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
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(stub_root) + os.pathsep + environment.get("PYTHONPATH", ""),
        }
    )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(key, None)
    with log_file.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [sys.executable, "scripts/web_app.py"],
            cwd=test_root,
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            yield wait_for_port(process, port_file, log_file)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class StaticAssetContractTests(unittest.TestCase):
    def assert_asset(self, response: tuple[int, dict[str, str], bytes], expected: bytes, name: str) -> None:
        status, headers, body = response
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.assertEqual(headers.get("content-length"), str(len(expected)))
        self.assertEqual(headers.get("cache-control"), CACHE_CONTROL)
        self.assertNotIn("content-disposition", headers)
        self.assertNotIn("accept-ranges", headers)
        self.assertNotIn("content-range", headers)
        self.assertEqual(body, expected)

    def assert_json_error(
        self, response: tuple[int, dict[str, str], bytes], status: int, message: str
    ) -> None:
        actual_status, headers, body = response
        expected = ('{\n  "error": "' + message + '"\n}').encode("utf-8")
        self.assertEqual(actual_status, status)
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assertEqual(headers.get("content-length"), str(len(expected)))
        self.assertNotIn("cache-control", headers)
        self.assertEqual(body, expected)

    @contextmanager
    def workspace_server(self) -> Iterator[tuple[int, Path, Path]]:
        with tempfile.TemporaryDirectory(prefix="static-assets-contract-") as temporary:
            test_root = Path(temporary)
            assets_dir, outside = prepare_workspace(test_root)
            with isolated_server(test_root) as port:
                yield port, assets_dir, outside

    def test_serves_known_nested_unknown_and_query_assets(self) -> None:
        cases = (
            ("/assets/plain.css", b"body{color:#123}", "plain.css"),
            ("/assets/nested/app.js", b"console.log('nested');", "app.js"),
            ("/assets/unknown.fixture", b"\x00fixture\xff", "unknown.fixture"),
        )
        with self.workspace_server() as (port, _, _):
            for path, expected, name in cases:
                with self.subTest(path=path):
                    self.assert_asset(request(port, "GET", path), expected, name)
            self.assert_asset(request(port, "GET", "/assets/plain.css?version=1"), b"body{color:#123}", "plain.css")

    def test_decodes_once_normalizes_inside_root_and_keeps_file_tail_slash(self) -> None:
        expected = b"body{color:#123}"
        with self.workspace_server() as (port, _, _):
            for path in (
                "/assets/nested/../plain.css",
                "/assets/nested%2F..%2Fplain.css",
                "/assets/plain.css/",
            ):
                with self.subTest(path=path):
                    self.assert_asset(request(port, "GET", path), expected, "plain.css")
            self.assert_json_error(request(port, "GET", "/assets/nested%252F..%252Fplain.css"), 404, "Asset not found")

    def test_rejects_external_and_encoded_traversal(self) -> None:
        with self.workspace_server() as (port, _, _):
            for path in ("/assets/../outside.txt", "/assets/%2e%2e%2foutside.txt", "/assets/%2Fetc/passwd"):
                with self.subTest(path=path):
                    self.assert_json_error(request(port, "GET", path), 400, "Invalid asset path")

    def test_root_directories_and_missing_assets_are_not_files(self) -> None:
        with self.workspace_server() as (port, _, _):
            for path in ("/assets/", "/assets/folder", "/assets/folder/", "/assets/missing.css"):
                with self.subTest(path=path):
                    self.assert_json_error(request(port, "GET", path), 404, "Asset not found")
            self.assert_json_error(request(port, "GET", "/assets"), 404, "Not found")

    def test_get_range_is_ignored_and_returns_the_full_asset(self) -> None:
        with self.workspace_server() as (port, _, _):
            self.assert_asset(
                request(port, "GET", "/assets/plain.css", headers={"Range": "bytes=1-3"}),
                b"body{color:#123}",
                "plain.css",
            )

    def test_symlink_to_outside_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="static-assets-contract-") as temporary:
            test_root = Path(temporary)
            assets_dir, outside = prepare_workspace(test_root)
            try:
                (assets_dir / "outside-link.txt").symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with isolated_server(test_root) as port:
                self.assert_json_error(request(port, "GET", "/assets/outside-link.txt"), 400, "Invalid asset path")

    def test_method_boundary_is_http_observable(self) -> None:
        with self.workspace_server() as (port, _, _):
            for method in ("POST", "DELETE"):
                with self.subTest(method=method):
                    response = request(port, method, "/assets/plain.css")
                    self.assert_json_error(response, 404, "Not found")
                    self.assertNotIn("allow", response[1])

            status, headers, body = request(port, "HEAD", "/assets/plain.css")
            self.assertEqual(status, 404)
            self.assertEqual(body, b"")
            self.assertNotIn("allow", headers)
            self.assertNotIn("content-type", headers)
            self.assertNotIn("content-length", headers)
            self.assertNotIn("cache-control", headers)


if __name__ == "__main__":
    unittest.main()
