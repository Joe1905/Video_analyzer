#!/usr/bin/env python3
"""Isolated Shop command-log redaction contract."""

from __future__ import annotations

import importlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

SHORT_SECRET = "private-shop"
SECRET = f"{SHORT_SECRET}-prompt-fixture"
REDACTED = "[redacted]"


class FakeProcess:
    def __init__(self, stdout: str, code: int) -> None:
        self.stdout = io.StringIO(stdout)
        self._code = code

    def wait(self) -> int:
        return self._code


def make_job(web_app: Any, job_id: str) -> Any:
    return web_app.ShopJob(
        id=job_id,
        url="https://shop.tiktok.com/view/product/fixture",
        source_type="product",
        region="US",
        max_pages=1,
        review_pages=1,
        analyze=True,
        related_videos=False,
        prompt=SECRET,
    )


def run_contract() -> None:
    temporary = Path(tempfile.mkdtemp(prefix=".test-shop-prompt-redaction-", dir=ROOT))
    original_environment = {key: os.environ.get(key) for key in ("UI_TEST_MODE", "APP_TEST_ROOT")}
    web_app: Any | None = None
    try:
        os.environ.update({"UI_TEST_MODE": "1", "APP_TEST_ROOT": str(temporary)})
        sys.modules.pop("web_app", None)
        web_app = importlib.import_module("web_app")

        with web_app.shop_jobs_lock:
            web_app.shop_jobs.clear()

        success_job = make_job(web_app, "shop-redaction-success")
        success_command = ["python", "worker.py", "--prompt", SHORT_SECRET, "--prompt", SECRET, "--region", "US"]
        received_commands: list[list[str]] = []

        def successful_popen(command: list[str], **_kwargs: Any) -> FakeProcess:
            received_commands.append(list(command))
            return FakeProcess(f"fixture stdout {SHORT_SECRET} and {SECRET}\nordinary stdout unchanged  \n", 0)

        with web_app.shop_jobs_lock:
            web_app.shop_jobs[success_job.id] = success_job
        with patch.object(web_app.subprocess, "Popen", side_effect=successful_popen):
            web_app.run_shop_command(success_job, success_command)

        assert received_commands == [success_command]
        assert success_command.count("--prompt") == 2
        assert success_command.count(SHORT_SECRET) == 1
        assert success_command.count(SECRET) == 1
        assert web_app._shop_prompt_values(success_command) == [SECRET, SHORT_SECRET]
        success_log = "\n".join(success_job.log)
        assert SECRET not in success_log
        assert SHORT_SECRET not in success_log
        assert "-prompt-fixture" not in success_log
        assert success_log.count(REDACTED) == 4
        assert success_job.log == [
            "$ python worker.py --prompt [redacted] --prompt [redacted] --region US",
            "fixture stdout [redacted] and [redacted]",
            "ordinary stdout unchanged",
        ]

        failure_job = make_job(web_app, "shop-redaction-failure")
        failure_command = ["python", "worker.py", "--prompt", SECRET]
        with web_app.shop_jobs_lock:
            web_app.shop_jobs[failure_job.id] = failure_job
        with patch.object(web_app.subprocess, "Popen", return_value=FakeProcess(f"failure stdout {SECRET}\n", 7)):
            try:
                web_app.run_shop_command(failure_job, failure_command)
            except RuntimeError as exc:
                error = str(exc)
            else:
                raise AssertionError("Expected a non-zero Shop command to raise RuntimeError")
        failure_log = "\n".join(failure_job.log)
        assert SECRET not in error
        assert SECRET not in failure_log
        assert REDACTED in error
        assert REDACTED in failure_log
        assert error == "Command failed with exit code 7: python worker.py --prompt [redacted]"
        assert failure_job.log == ["$ python worker.py --prompt [redacted]", "failure stdout [redacted]"]

        plain_job = make_job(web_app, "shop-redaction-plain")
        plain_command = ["python", "worker.py", "--region", "US"]
        with web_app.shop_jobs_lock:
            web_app.shop_jobs[plain_job.id] = plain_job
        with patch.object(web_app.subprocess, "Popen", return_value=FakeProcess("plain stdout\n", 0)):
            web_app.run_shop_command(plain_job, plain_command)
        assert plain_job.log == ["$ python worker.py --region US", "plain stdout"]
        assert web_app._shop_command_display(["python", "worker.py", "--prompt"]) == "python worker.py --prompt"
        assert web_app._shop_prompt_values(["python", "worker.py", "--prompt", ""]) == []
    finally:
        if web_app is not None:
            with web_app.shop_jobs_lock:
                web_app.shop_jobs.clear()
        sys.modules.pop("web_app", None)
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(temporary, ignore_errors=False)
        assert not temporary.exists()


def main() -> int:
    run_contract()
    print("shop prompt redaction tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
