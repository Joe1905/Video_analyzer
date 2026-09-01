"""Social video metrics job orchestration without HTTP dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

from jobs.registry import JobRegistry
from jobs.snapshots import snapshot_metrics_job


# Kept as a mapping to preserve the existing route validation contract and the
# endpoint list mirrored from sociavault_tiktok.py.
TIKTOK_ENDPOINTS: dict[str, str] = {
    "profile": "/v1/scrape/tiktok/profile",
    "videos": "/v1/scrape/tiktok/videos",
    "videos-popular": "/v1/scrape/tiktok/videos/popular",
    "followers": "/v1/scrape/tiktok/followers",
    "following": "/v1/scrape/tiktok/following",
    "video-info": "/v1/scrape/tiktok/video-info",
    "comments": "/v1/scrape/tiktok/comments",
    "comment-replies": "/v1/scrape/tiktok/comment-replies",
    "transcript": "/v1/scrape/tiktok/transcript",
    "demographics": "/v1/scrape/tiktok/demographics",
    "live": "/v1/scrape/tiktok/live",
    "search-users": "/v1/scrape/tiktok/search/users",
    "search-hashtag": "/v1/scrape/tiktok/search/hashtag",
    "search-keyword": "/v1/scrape/tiktok/search/keyword",
    "search-music": "/v1/scrape/tiktok/search/music",
    "search-top": "/v1/scrape/tiktok/search/top",
    "trending": "/v1/scrape/tiktok/trending",
    "creators-popular": "/v1/scrape/tiktok/creators/popular",
    "hashtags-popular": "/v1/scrape/tiktok/hashtags/popular",
    "music-popular": "/v1/scrape/tiktok/music/popular",
    "music-info": "/v1/scrape/tiktok/music/info",
    "music-videos": "/v1/scrape/tiktok/music/videos",
}


@dataclass
class MetricsJob:
    id: str
    target: str
    endpoint: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


class MetricsService:
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
        register_from_payload: Callable[..., Any],
    ) -> None:
        self._registry = registry
        self._root = root
        self._output_dir = output_dir
        self._scripts_dir = scripts_dir
        self._read_json_file = read_json_file
        self._popen_factory = popen_factory
        self._thread_factory = thread_factory
        self._job_id_factory = job_id_factory
        self._register_from_payload = register_from_payload

    def create_and_start(self, target: str, endpoint: str) -> dict[str, Any]:
        job = MetricsJob(id=self._job_id_factory(), target=target, endpoint=endpoint)
        self._registry.register(job.id, job)
        thread = self._thread_factory(target=self.run_job, args=(job.id,), daemon=True)
        thread.start()
        payload = self.payload_for(job.id)
        if payload is None:
            raise RuntimeError("Video metrics job disappeared after registration")
        return payload

    def append_log(self, job_id: str, line: str) -> None:
        self._registry.append_log(job_id, line)

    def run_command(self, job_id: str, command: list[str]) -> None:
        self.append_log(job_id, f"$ {' '.join(command)}")
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
            self.append_log(job_id, line)
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")

    def run_job(self, job_id: str) -> None:
        initial = self._registry.snapshot(job_id)
        if initial is None:
            return
        target = initial.target
        endpoint = initial.endpoint
        self._registry.update_fields(job_id, {"status": "running"})

        job_output_dir = self._output_dir / "tiktok_api" / job_id
        result_path = job_output_dir / "result.json"
        try:
            job_output_dir.mkdir(parents=True, exist_ok=True)
            self._registry.update_fields(
                job_id,
                {"output_dir": str(job_output_dir.relative_to(self._root))},
            )
            command = [
                "python",
                str(self._scripts_dir / "sociavault_tiktok.py"),
                "--endpoint",
                endpoint,
                "--output",
                str(result_path),
            ]
            if target:
                if target.startswith("http"):
                    command.extend(["--url", target])
                elif target.startswith("#"):
                    command.extend(["--hashtag", target.lstrip("#")])
                elif target.startswith("@"):
                    command.extend(["--handle", target.lstrip("@")])
                elif endpoint in ("music-info", "music-videos"):
                    command.extend(["--sound-id", target])
                elif endpoint.startswith("search-"):
                    command.extend(["--query", target])
                else:
                    command.extend(["--handle", target])

            self.run_command(job_id, command)
            if endpoint == "video-info" and result_path.is_file():
                self._register_from_payload(self._read_json_file(result_path), source_url=target)

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
        result = self._read_json_file(self._output_dir / "tiktok_api" / job.id / "result.json")
        return snapshot_metrics_job(job, result=result)
