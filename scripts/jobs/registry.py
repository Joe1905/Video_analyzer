"""Pure in-memory registry for jobs with shared lifecycle fields."""

from __future__ import annotations

from collections.abc import Mapping
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
        normalized = line.rstrip()
        with self._lock:
            job = self._jobs[job_id]
            timestamp = self._clock()
            job.log.append(normalized)
            job.updated_at = timestamp

    def update_fields(
        self,
        job_id: str,
        fields: Mapping[str, Any],
        *,
        final_log: str | None = None,
    ) -> None:
        if not isinstance(fields, Mapping):
            raise TypeError("fields must be a mapping")
        if final_log is not None and not isinstance(final_log, str):
            raise TypeError("final_log must be a string or None")
        if not fields and final_log is None:
            raise ValueError("fields or final_log is required")
        with self._lock:
            job = self._jobs[job_id]
            items = list(fields.items())
            for name, _value in items:
                if not isinstance(name, str):
                    raise TypeError("field names must be strings")
                if name.startswith("_") or name in {"log", "updated_at"}:
                    raise ValueError(f"field is not writable: {name}")
                if not hasattr(job, name):
                    raise ValueError(f"unknown job field: {name}")
            candidate = deepcopy(job)
            for name, value in items:
                setattr(candidate, name, deepcopy(value))
            if final_log is not None:
                candidate.log.append(final_log)
            candidate.updated_at = self._clock()
            self._jobs[job_id] = candidate
