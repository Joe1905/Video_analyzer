"""Pure in-memory registry for jobs with shared lifecycle fields."""

from __future__ import annotations

from copy import deepcopy
import threading
import time
from typing import Any, Callable


class JobRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._jobs: dict[str, Any] = {}
        self._lock = threading.Lock()

    def register(self, job_id: str, job: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"job already registered: {job_id}")
            self._jobs[job_id] = deepcopy(job)

    def snapshot(self, job_id: str) -> Any | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job is not None else None

    def status(self, job_id: str) -> str | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.status if job is not None else None

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.log.append(line.rstrip())
            job.updated_at = self._clock()
