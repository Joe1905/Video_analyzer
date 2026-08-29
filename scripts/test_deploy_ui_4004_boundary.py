#!/usr/bin/env python3
"""Static and fail-closed contracts for the isolated 4004 deployment."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_ui_4004.sh"
COMPOSE_FILE = ROOT / "docker-compose.yml"
EXPECTED_IMAGE = "short-video-analyzer-ui-4004:latest"
SHARED_IMAGE = "short-video-analyzer:latest"


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

    def test_every_service_uses_the_same_overridable_image_expression(self) -> None:
        text = COMPOSE_FILE.read_text(encoding="utf-8")
        image_expression = f"${{ANALYZER_IMAGE:-{SHARED_IMAGE}}}"

        self.assertEqual(text.count(f"image: {image_expression}"), 3)
        self.assertNotIn(f"image: {SHARED_IMAGE}", text)


if __name__ == "__main__":
    unittest.main()
