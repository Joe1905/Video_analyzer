"""TikTok Shop job orchestration without HTTP dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

from jobs.registry import JobRegistry
from jobs.snapshots import snapshot_shop_job


@dataclass
class ShopJob:
    id: str
    url: str
    source_type: str
    region: str
    max_pages: int
    review_pages: int
    analyze: bool
    related_videos: bool
    prompt: str = ""
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


def _shop_command_display(command: list[str]) -> str:
    display = list(command)
    for index, value in enumerate(display[:-1]):
        if value == "--prompt":
            display[index + 1] = "[redacted]"
    return " ".join(display)


def _shop_prompt_values(command: list[str]) -> list[str]:
    values = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--prompt" and command[index + 1]
    ]
    return sorted(dict.fromkeys(values), key=len, reverse=True)


class ShopService:
    def __init__(
        self,
        registry: JobRegistry,
        root: Path,
        output_dir: Path,
        scripts_dir: Path,
        read_json_file: Callable[[Path], Any],
        popen_factory: Callable[..., Any],
        thread_factory: Callable[..., Any],
        job_id_factory: Callable[[], str],
    ) -> None:
        self._registry = registry
        self._root = root
        self._output_dir = output_dir
        self._scripts_dir = scripts_dir
        self._read_json_file = read_json_file
        self._popen_factory = popen_factory
        self._thread_factory = thread_factory
        self._job_id_factory = job_id_factory

    def create_and_start(
        self,
        url: str,
        source_type: str,
        region: str,
        max_pages: int,
        review_pages: int,
        analyze: bool,
        related_videos: bool,
        prompt: str,
    ) -> dict[str, Any]:
        job = ShopJob(
            id=self._job_id_factory(),
            url=url,
            source_type=source_type,
            region=region,
            max_pages=max_pages,
            review_pages=review_pages,
            analyze=analyze,
            related_videos=related_videos,
            prompt=prompt,
        )
        self._registry.register(job.id, job)
        thread = self._thread_factory(target=self.run_job, args=(job.id,), daemon=True)
        thread.start()
        payload = self.payload_for(job.id)
        if payload is None:
            raise RuntimeError("Shop job disappeared after registration")
        return payload

    def append_log(self, job_id: str, line: str) -> None:
        self._registry.append_log(job_id, line)

    def run_command(self, job_id: str, command: list[str]) -> None:
        display_command = _shop_command_display(command)
        prompt_values = _shop_prompt_values(command)
        self.append_log(job_id, f"$ {display_command}")
        process = self._popen_factory(
            command,
            cwd=self._root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            for prompt in prompt_values:
                line = line.replace(prompt, "[redacted]")
            self.append_log(job_id, line)
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"Command failed with exit code {code}: {display_command}")

    def run_job(self, job_id: str) -> None:
        initial = self._registry.snapshot(job_id)
        if initial is None:
            return
        url = initial.url
        source_type = initial.source_type
        region = initial.region
        max_pages = initial.max_pages
        review_pages = initial.review_pages
        analyze = initial.analyze
        related_videos = initial.related_videos
        prompt = initial.prompt
        self._registry.update_fields(job_id, {"status": "running"})

        job_output_dir = self._output_dir / "tiktok_shop" / job_id
        extract_path = job_output_dir / "shop_extract.json"
        analysis_path = job_output_dir / "shop_analysis.json"
        try:
            job_output_dir.mkdir(parents=True, exist_ok=True)
            self._registry.update_fields(
                job_id,
                {"output_dir": str(job_output_dir.relative_to(self._root))},
            )
            command = [
                "python",
                str(self._scripts_dir / "sociavault_tiktok_shop.py"),
                url,
                "--source-type",
                source_type,
                "--region",
                region,
                "--max-pages",
                str(max_pages),
                "--review-pages",
                str(review_pages),
                "--output",
                str(extract_path),
            ]
            if related_videos:
                command.append("--related-videos")
            self.run_command(job_id, command)

            if analyze:
                self.run_command(
                    job_id,
                    [
                        "python",
                        str(self._scripts_dir / "deepseek_shop_analyze.py"),
                        str(extract_path),
                        "--output",
                        str(analysis_path),
                        "--prompt",
                        prompt,
                    ],
                )

            self._registry.update_fields(job_id, {"status": "complete"})
        except Exception as exc:
            self._registry.update_fields(
                job_id,
                {"status": "failed", "error": str(exc)},
                final_log=str(exc),
            )

    def payload_for(self, job_id: str) -> dict[str, Any] | None:
        job = self._registry.snapshot(job_id)
        if job is None:
            return None
        job_output_dir = self._output_dir / "tiktok_shop" / job.id
        extract = self._read_json_file(job_output_dir / "shop_extract.json")
        analysis = self._read_json_file(job_output_dir / "shop_analysis.json")
        return snapshot_shop_job(job, extract=extract, analysis=analysis)
