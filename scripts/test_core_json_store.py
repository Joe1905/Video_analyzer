#!/usr/bin/env python3
"""Focused regression tests for dependency-free JSON file primitives."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import json_store


class JsonStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "nested" / "state.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @unittest.skipIf(os.name == "nt", "Windows file sharing can reject replace while readers are open")
    def test_concurrent_reads_only_observe_complete_json(self) -> None:
        json_store.atomic_write_json(self.path, {"value": -1, "padding": "x" * 4096})
        errors: list[BaseException] = []
        done = threading.Event()

        def writer() -> None:
            try:
                for value in range(100):
                    json_store.atomic_write_json(self.path, {"value": value, "padding": "x" * 4096})
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)
            finally:
                done.set()

        def reader() -> None:
            try:
                while not done.is_set():
                    payload = json.loads(self.path.read_bytes().decode("utf-8"))
                    self.assertIsInstance(payload, dict)
                    self.assertIsInstance(payload["value"], int)
                    self.assertEqual(payload["padding"], "x" * 4096)
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        writer_thread = threading.Thread(target=writer)
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for thread in readers:
            thread.start()
        writer_thread.start()
        writer_thread.join()
        for thread in readers:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(json_store.read_json(self.path)["value"], 99)

    def test_replace_failure_preserves_old_target_and_cleans_temp(self) -> None:
        json_store.atomic_write_json(self.path, {"version": "old"})
        with mock.patch.object(json_store.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                json_store.atomic_write_json(self.path, {"version": "new"})

        self.assertEqual(json_store.read_json(self.path), {"version": "old"})
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_serialization_failure_does_not_create_target(self) -> None:
        with self.assertRaises(TypeError):
            json_store.atomic_write_json(self.path, {"not_json": {1, 2}})

        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_serialization_failure_preserves_existing_target(self) -> None:
        json_store.atomic_write_json(self.path, {"version": "old"})
        original = self.path.read_bytes()

        with self.assertRaises(TypeError):
            json_store.atomic_write_json(self.path, {"not_json": {1, 2}})

        self.assertEqual(self.path.read_bytes(), original)

    def test_fsync_failure_preserves_old_target_and_cleans_temp(self) -> None:
        json_store.atomic_write_json(self.path, {"version": "old"})
        with mock.patch.object(json_store.os, "fsync", side_effect=OSError("fsync failed")):
            with self.assertRaisesRegex(OSError, "fsync failed"):
                json_store.atomic_write_json(self.path, {"version": "new"})

        self.assertEqual(json_store.read_json(self.path), {"version": "old"})
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_fdopen_failure_closes_descriptor_and_cleans_temp(self) -> None:
        self.path.parent.mkdir(parents=True)
        real_close = json_store.os.close
        with (
            mock.patch.object(json_store.os, "fdopen", side_effect=OSError("fdopen failed")),
            mock.patch.object(json_store.os, "close", side_effect=real_close) as close,
        ):
            with self.assertRaisesRegex(OSError, "fdopen failed"):
                json_store.atomic_write_json(self.path, {"version": "new"})

        close.assert_called_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_corrupt_json_raises_decode_error(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            json_store.read_json(self.path)

    def test_lock_is_released_after_write_error(self) -> None:
        json_store.atomic_write_json(self.path, {"version": "old"})
        with mock.patch.object(json_store.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                json_store.atomic_write_json(self.path, {"version": "new"})

        completed = threading.Event()

        def retry() -> None:
            json_store.atomic_write_json(self.path, {"version": "recovered"})
            completed.set()

        thread = threading.Thread(target=retry)
        thread.start()
        thread.join(timeout=2)
        self.assertTrue(completed.is_set(), "write lock remained held after an exception")
        self.assertEqual(json_store.read_json(self.path), {"version": "recovered"})

    def test_normalized_paths_share_the_same_lock(self) -> None:
        self.path.parent.mkdir(parents=True)
        (self.path.parent / "child").mkdir()
        canonical = json_store._normalized_path(self.path)
        equivalent_path = self.path.parent / "child" / ".." / self.path.name
        equivalent = json_store._normalized_path(equivalent_path)
        self.assertEqual(canonical, equivalent)

        active_replaces = 0
        maximum_active = 0
        counter_lock = threading.Lock()
        start = threading.Barrier(2)
        real_replace = json_store.os.replace
        errors: list[BaseException] = []

        def observed_replace(source: Path, target: Path) -> None:
            nonlocal active_replaces, maximum_active
            with counter_lock:
                active_replaces += 1
                maximum_active = max(maximum_active, active_replaces)
            time.sleep(0.05)
            try:
                real_replace(source, target)
            finally:
                with counter_lock:
                    active_replaces -= 1

        def writer(path: Path, value: int) -> None:
            try:
                start.wait()
                json_store.atomic_write_json(path, {"value": value})
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        with mock.patch.object(json_store.os, "replace", side_effect=observed_replace):
            threads = [
                threading.Thread(target=writer, args=(self.path, 1)),
                threading.Thread(target=writer, args=(equivalent_path, 2)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(maximum_active, 1)
        self.assertIn(json_store.read_json(self.path)["value"], {1, 2})

    def test_unused_path_lock_entry_is_removed(self) -> None:
        normalized = json_store._normalized_path(self.path)
        key = str(normalized)

        json_store.atomic_write_json(self.path, {"value": 1})

        self.assertNotIn(key, json_store._path_locks)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(json_store.read_json(self.path))

    def test_output_format_is_utf8_indented_and_newline_terminated(self) -> None:
        json_store.atomic_write_json(self.path, {"中文": "值", "items": [1, 2]})
        self.assertEqual(
            self.path.read_bytes(),
            '{\n  "中文": "值",\n  "items": [\n    1,\n    2\n  ]\n}\n'.encode("utf-8"),
        )

    def test_web_app_json_store_wiring_contract(self) -> None:
        source_path = Path(__file__).with_name("web_app.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

        imports = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "core.json_store"
        ]
        self.assertEqual(len(imports), 1)
        self.assertEqual(
            [(alias.name, alias.asname) for alias in imports[0].names],
            [("atomic_write_json", None), ("read_json", None)],
        )

        definitions = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"read_json", "write_json"}
        ]
        self.assertEqual(definitions, [])

        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        # Phase 4.1 moves Shop reads into its service. Phase 4.2 moves four
        # Metrics call sites behind two service-owned reads (worker + payload).
        # Phase 4.3 moves Amazon artifact reads behind its service payload;
        # Phase 4.4A moves four Download reads and three atomic writes;
        # Phase 4.4F moves the files endpoint social-context read into its service.
        self.assertEqual(
            sum(isinstance(node.func, ast.Name) and node.func.id == "read_json" for node in calls),
            37,
        )
        video_files_source_path = source_path.with_name("services") / "video_files.py"
        video_files_tree = ast.parse(
            video_files_source_path.read_text(encoding="utf-8"), filename=str(video_files_source_path)
        )
        self.assertEqual(
            sum(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == "_read_json_file"
                for node in ast.walk(video_files_tree)
                if isinstance(node, ast.Call)
            ),
            1,
        )
        shop_source_path = source_path.with_name("services") / "shop.py"
        shop_tree = ast.parse(shop_source_path.read_text(encoding="utf-8"), filename=str(shop_source_path))
        payload_for = next(
            node for node in ast.walk(shop_tree)
            if isinstance(node, ast.FunctionDef) and node.name == "payload_for"
        )
        self.assertEqual(
            sum(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == "_read_json_file"
                for node in ast.walk(payload_for)
                if isinstance(node, ast.Call)
            ),
            2,
        )
        metrics_source_path = source_path.with_name("services") / "metrics.py"
        metrics_tree = ast.parse(
            metrics_source_path.read_text(encoding="utf-8"),
            filename=str(metrics_source_path),
        )
        self.assertEqual(
            sum(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == "_read_json_file"
                for node in ast.walk(metrics_tree)
                if isinstance(node, ast.Call)
            ),
            2,
        )
        self.assertEqual(
            sum(isinstance(node.func, ast.Name) and node.func.id == "write_json" for node in calls),
            0,
        )
        self.assertEqual(
            sum(isinstance(node.func, ast.Name) and node.func.id == "atomic_write_json" for node in calls),
            7,
        )
        self.assertEqual(
            sum(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "json"
                and node.func.attr == "dump"
                for node in calls
            ),
            0,
        )
        self.assertEqual(
            sum(
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_text"
                and any(
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "json"
                    and child.func.attr == "dumps"
                    for argument in node.args
                    for child in ast.walk(argument)
                )
                for node in calls
            ),
            0,
        )

        amazon_source_path = source_path.with_name("services") / "amazon.py"
        amazon_tree = ast.parse(
            amazon_source_path.read_text(encoding="utf-8"),
            filename=str(amazon_source_path),
        )
        self.assertEqual(
            sum(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == "_read_json_file"
                for node in ast.walk(amazon_tree)
                if isinstance(node, ast.Call)
            ),
            1,
        )
        amazon_atomic_writes = [
            node
            for node in ast.walk(amazon_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_write_json_file"
        ]
        self.assertEqual(len(amazon_atomic_writes), 1)
        self.assertEqual(
            [
                ast.unparse(argument)
                for argument in amazon_atomic_writes[0].args
            ],
            ["result_path", "result"],
        )
        amazon_service_assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "amazon_service" for target in node.targets)
        )
        self.assertIsInstance(amazon_service_assignment.value, ast.Call)
        if isinstance(amazon_service_assignment.value, ast.Call):
            keywords = {keyword.arg: keyword.value for keyword in amazon_service_assignment.value.keywords}
            self.assertIsInstance(keywords.get("read_json_file"), ast.Name)
            self.assertEqual(getattr(keywords.get("read_json_file"), "id", None), "read_json")
            self.assertIsInstance(keywords.get("write_json_file"), ast.Name)
            self.assertEqual(getattr(keywords.get("write_json_file"), "id", None), "atomic_write_json")

        download_source_path = source_path.with_name("services") / "downloads.py"
        download_tree = ast.parse(
            download_source_path.read_text(encoding="utf-8"),
            filename=str(download_source_path),
        )
        download_reads = [
            node for node in ast.walk(download_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_read_json_file"
        ]
        download_writes = [
            node for node in ast.walk(download_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_write_json_file"
        ]
        self.assertEqual(len(download_reads), 4)
        self.assertEqual(len(download_writes), 3)
        download_service_assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "download_service" for target in node.targets)
        )
        self.assertIsInstance(download_service_assignment.value, ast.Call)
        if isinstance(download_service_assignment.value, ast.Call):
            self.assertTrue(all(keyword.arg is not None for keyword in download_service_assignment.value.keywords))
            keywords = {keyword.arg: keyword.value for keyword in download_service_assignment.value.keywords}
            expected_download_injection = {
                "registry": "download_job_registry",
                "root": "ROOT",
                "videos_dir": "VIDEOS_DIR",
                "output_dir": "OUTPUT_DIR",
                "scripts_dir": "SCRIPTS_DIR",
                "read_json_file": "read_json",
                "write_json_file": "atomic_write_json",
                "run_factory": "subprocess.run",
                "thread_factory": "threading.Thread",
                "job_id_factory": "lambda: str(uuid.uuid4())",
                "fallback_video_id_factory": "lambda: uuid.uuid4().hex[:12]",
                "environ": "os.environ",
                "get_cached": "get_cached",
                "store_response": "store_response",
                "video_cache_request": "video_cache_request",
                "video_cache_metadata": "video_cache_metadata",
                "with_download_cache_meta": "with_download_cache_meta",
                "register_video": "register_video",
                "register_from_payload": "register_from_payload",
                "platform_for_url": "platform_for_url",
                "video_source_hidden": "video_source_hidden",
                "make_web_manual_visible": "make_web_manual_visible",
                "start_social_context_job": "start_social_context_job",
                "safe_filename": "safe_filename",
                "analyzer_media_is_valid": "analyzer_media_is_valid",
                "ensure_analyzer_media_or_delete": "ensure_analyzer_media_or_delete",
                "ensure_us_proxy": "ensure_us_proxy",
                "requests_get": "lambda *args, **kwargs: __import__('requests').get(*args, **kwargs)",
                "cache_log_label": "cache_log_label",
                "normalize_video_source": "normalize_video_source",
                "default_source": "SOURCE_API_UPLOAD",
                "video_media_ttl_seconds": "APP_CONFIG.video_media_ttl_seconds",
                "default_sociavault_api_base": "DEFAULT_SOCIA_VAULT_API_BASE",
            }
            self.assertEqual(set(keywords), set(expected_download_injection))
            self.assertEqual(
                {name: ast.unparse(keywords[name]) for name in expected_download_injection},
                expected_download_injection,
            )


if __name__ == "__main__":
    unittest.main()
