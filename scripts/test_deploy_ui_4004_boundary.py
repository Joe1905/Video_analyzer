#!/usr/bin/env python3
"""Static and fail-closed contracts for the isolated 4004 deployment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_ui_4004.sh"
COMPOSE_FILE = ROOT / "docker-compose.yml"
EXPECTED_IMAGE = "short-video-analyzer-ui-4004:latest"
SHARED_IMAGE = "short-video-analyzer:latest"
SCRIPT_LIFECYCLE = ROOT / "scripts" / "script_lifecycle.json"
TEMPORARY_SCRIPTS_DIR = ROOT / "scripts" / "temporary"
HOT_REPORT_REPAIR_SCRIPT = ROOT / "scripts" / "repair_hot_report_checkpoint.py"
LIFECYCLE_README = "scripts/temporary/README.md"
PLAYWRIGHT_TESTS = {
    "test_chat_scroll_playwright.py",
    "test_lan_chat_upload_queue_playwright.py",
}
NODE_GATES = {
    ROOT / "scripts" / "test_mcp_bridge_cache.js",
    ROOT / "sellersprite_mcp_chat" / "test_stdio_mcp_client.js",
}


def script_lifecycle_errors(manifest: dict, on_disk_paths: set[str], today: date) -> list[str]:
    errors = []
    expected_root = {"schema_version", "max_ttl_days", "active_phase", "scripts"}
    if set(manifest) != expected_root:
        return ["manifest root fields do not match the schema"]
    if manifest["schema_version"] != 1 or manifest["max_ttl_days"] != 14:
        errors.append("unsupported lifecycle policy")
    entries = manifest["scripts"]
    if not isinstance(entries, list):
        return errors + ["scripts must be a list"]
    active_phase = manifest["active_phase"]
    if entries and not (isinstance(active_phase, str) and active_phase.strip()):
        errors.append("active_phase must exist exactly while temporary scripts exist")
    if not entries and active_phase is not None:
        errors.append("active_phase must be null when no temporary scripts exist")

    required = {
        "path", "phase", "owner", "purpose", "created_on", "expires_on",
        "delete_condition", "promotion_condition",
    }
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if len(entries) != len(paths) or len(paths) != len(set(paths)):
        errors.append("entries must be objects with unique paths")
    if set(paths) != on_disk_paths:
        errors.append("manifest and temporary directory differ")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required:
            errors.append("entry fields do not match the schema")
            continue
        path = entry["path"]
        if not isinstance(path, str) or not path.startswith("scripts/temporary/"):
            errors.append("temporary path is outside scripts/temporary")
        if Path(path).name.startswith("test_") and Path(path).suffix == ".py":
            errors.append("temporary test_*.py would leak into test discovery")
        if entry["phase"] != active_phase:
            errors.append("entry phase differs from active_phase")
        for field in ("phase", "owner", "purpose", "delete_condition", "promotion_condition"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                errors.append(f"{field} is empty")
        try:
            created_on = date.fromisoformat(entry["created_on"])
            expires_on = date.fromisoformat(entry["expires_on"])
        except (TypeError, ValueError):
            errors.append("lifecycle dates must use ISO format")
            continue
        if created_on > today:
            errors.append("created_on is in the future")
        if expires_on < created_on or (expires_on - created_on).days > manifest["max_ttl_days"]:
            errors.append("TTL is negative or exceeds the maximum")
        if today > expires_on:
            errors.append("temporary script has expired")
    return errors


class DeployUi4004BoundaryTest(unittest.TestCase):
    def test_deploy_defaults_to_and_requires_the_dedicated_image(self) -> None:
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(f'expected_image="{EXPECTED_IMAGE}"', text)
        self.assertIn('image_name="${ANALYZER_IMAGE:-$expected_image}"', text)
        self.assertIn('if [[ "$image_name" != "$expected_image" ]]; then', text)
        self.assertLess(
            text.index('if [[ "$image_name" != "$expected_image" ]]; then'),
            text.index('if command -v docker >/dev/null 2>&1'),
        )
        self.assertIn('-t "$image_name" .', text)
        self.assertGreaterEqual(text.count('ANALYZER_IMAGE="$image_name"'), 2)

    @unittest.skipIf(os.name == "nt", "requires a native POSIX shell; covered by the Linux server gate")
    def test_invalid_image_is_rejected_before_docker_detection(self) -> None:
        bash_path = shutil.which("bash")
        self.assertIsNotNone(bash_path, "bash is required to test the deployment script")
        environment = os.environ.copy()
        environment.update({"ANALYZER_IMAGE": SHARED_IMAGE, "PATH": ""})
        result = subprocess.run(
            [str(bash_path), str(DEPLOY_SCRIPT)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Refusing non-4004 analyzer image", result.stderr)
        self.assertNotIn("Docker Compose is required", result.stderr)

    @unittest.skipUnless(COMPOSE_FILE.is_file(), "requires docker-compose.yml in a source checkout")
    def test_every_service_uses_the_same_overridable_image_expression(self) -> None:
        text = COMPOSE_FILE.read_text(encoding="utf-8")
        image_expression = f"${{ANALYZER_IMAGE:-{SHARED_IMAGE}}}"

        self.assertEqual(text.count(f"image: {image_expression}"), 3)
        self.assertNotIn(f"image: {SHARED_IMAGE}", text)


class ScriptLifecycleBoundaryTest(unittest.TestCase):
    def test_permanent_recovery_example_stays_in_the_4004_project(self) -> None:
        text = HOT_REPORT_REPAIR_SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(text.count("docker-compose -p short-video-analyzer-ui-4004 run"), 2)
        self.assertNotIn("docker compose -p short-video-analyzer run", text)
        self.assertNotIn("docker-compose -p short-video-analyzer run", text)

    def test_temporary_scripts_have_a_live_bounded_ttl(self) -> None:
        manifest = json.loads(SCRIPT_LIFECYCLE.read_text(encoding="utf-8"))
        on_disk_paths = {
            path.relative_to(ROOT).as_posix()
            for path in TEMPORARY_SCRIPTS_DIR.rglob("*")
            if path.is_file() and path.relative_to(ROOT).as_posix() != LIFECYCLE_README
        }
        today = datetime.now(timezone.utc).date()
        self.assertEqual(script_lifecycle_errors(manifest, on_disk_paths, today), [])
        for entry in manifest["scripts"]:
            self.assertTrue((ROOT / entry["path"]).is_file())

    def test_lifecycle_policy_rejects_common_bypasses(self) -> None:
        entry = {
            "path": "scripts/temporary/probe.py", "phase": "phase-x", "owner": "owner",
            "purpose": "probe", "created_on": "2026-08-01", "expires_on": "2026-08-14",
            "delete_condition": "phase exit", "promotion_condition": "documented stable use",
        }
        manifest = {
            "schema_version": 1, "max_ttl_days": 14,
            "active_phase": "phase-x", "scripts": [entry],
        }
        cases = [
            ("expired", manifest, {entry["path"]}, date(2026, 8, 15)),
            ("unknown extension", manifest, {entry["path"], "scripts/temporary/extra.cmd"}, date(2026, 8, 10)),
            ("future creation", {**manifest, "scripts": [{**entry, "created_on": "2026-08-11"}]}, {entry["path"]}, date(2026, 8, 10)),
            ("temporary test", {**manifest, "scripts": [{**entry, "path": "scripts/temporary/test_probe.py"}]}, {"scripts/temporary/test_probe.py"}, date(2026, 8, 10)),
            ("completed phase", {**manifest, "active_phase": None}, {entry["path"]}, date(2026, 8, 10)),
        ]
        for label, candidate, files, today in cases:
            with self.subTest(label=label):
                self.assertTrue(script_lifecycle_errors(candidate, files, today))

    def test_full_gate_asset_count_stays_explicit(self) -> None:
        python_tests = {path.name for path in (ROOT / "scripts").glob("test_*.py")}
        self.assertEqual(len(python_tests), 54)
        self.assertTrue(PLAYWRIGHT_TESTS <= python_tests)
        self.assertEqual(len(python_tests - PLAYWRIGHT_TESTS), 52)
        self.assertTrue(all(path.is_file() for path in NODE_GATES))
        self.assertEqual(52 + len(NODE_GATES) + len(PLAYWRIGHT_TESTS), 56)


if __name__ == "__main__":
    unittest.main()
