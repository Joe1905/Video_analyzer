#!/usr/bin/env python3
"""Isolated Shop command-log redaction contract."""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from jobs.registry import JobRegistry
from services import shop as shop_service
from services.shop import ShopJob, ShopService


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


def make_job(job_id: str) -> ShopJob:
    return ShopJob(
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
    try:
        registry = JobRegistry()
        current_popen: Any = None

        def popen_factory(command: list[str], **kwargs: Any) -> FakeProcess:
            assert current_popen is not None
            return current_popen(command, **kwargs)

        service = ShopService(
            registry,
            ROOT,
            temporary / "output",
            SCRIPTS_DIR,
            lambda _path: None,
            popen_factory,
            threading.Thread,
            lambda: "unused",
        )

        success_job = make_job("shop-redaction-success")
        success_command = ["python", "worker.py", "--prompt", SHORT_SECRET, "--prompt", SECRET, "--region", "US"]
        received_commands: list[list[str]] = []

        def successful_popen(command: list[str], **_kwargs: Any) -> FakeProcess:
            received_commands.append(list(command))
            return FakeProcess(f"fixture stdout {SHORT_SECRET} and {SECRET}\nordinary stdout unchanged  \n", 0)

        registry.register(success_job.id, success_job)
        current_popen = successful_popen
        service.run_command(success_job.id, success_command)

        assert received_commands == [success_command]
        assert success_command.count("--prompt") == 2
        assert success_command.count(SHORT_SECRET) == 1
        assert success_command.count(SECRET) == 1
        assert shop_service._shop_prompt_values(success_command) == [SECRET, SHORT_SECRET]
        success = registry.snapshot(success_job.id)
        assert success is not None
        success_log = "\n".join(success.log)
        assert SECRET not in success_log
        assert SHORT_SECRET not in success_log
        assert "-prompt-fixture" not in success_log
        assert success_log.count(REDACTED) == 4
        assert success.log == [
            "$ python worker.py --prompt [redacted] --prompt [redacted] --region US",
            "fixture stdout [redacted] and [redacted]",
            "ordinary stdout unchanged",
        ]

        failure_job = make_job("shop-redaction-failure")
        failure_command = ["python", "worker.py", "--prompt", SECRET]
        registry.register(failure_job.id, failure_job)
        current_popen = lambda _command, **_kwargs: FakeProcess(f"failure stdout {SECRET}\n", 7)
        try:
            service.run_command(failure_job.id, failure_command)
        except RuntimeError as exc:
            error = str(exc)
        else:
            raise AssertionError("Expected a non-zero Shop command to raise RuntimeError")
        failure = registry.snapshot(failure_job.id)
        assert failure is not None
        failure_log = "\n".join(failure.log)
        assert SECRET not in error
        assert SECRET not in failure_log
        assert REDACTED in error
        assert REDACTED in failure_log
        assert error == "Command failed with exit code 7: python worker.py --prompt [redacted]"
        assert failure.log == ["$ python worker.py --prompt [redacted]", "failure stdout [redacted]"]

        plain_job = make_job("shop-redaction-plain")
        plain_command = ["python", "worker.py", "--region", "US"]
        registry.register(plain_job.id, plain_job)
        current_popen = lambda _command, **_kwargs: FakeProcess("plain stdout\n", 0)
        service.run_command(plain_job.id, plain_command)
        plain = registry.snapshot(plain_job.id)
        assert plain is not None
        assert plain.log == ["$ python worker.py --region US", "plain stdout"]
        assert shop_service._shop_command_display(["python", "worker.py", "--prompt"]) == "python worker.py --prompt"
        assert shop_service._shop_prompt_values(["python", "worker.py", "--prompt", ""]) == []
    finally:
        shutil.rmtree(temporary, ignore_errors=False)
        assert not temporary.exists()


def main() -> int:
    run_contract()
    print("shop prompt redaction tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
