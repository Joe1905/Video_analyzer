"""Report-cover artifact access without HTTP concerns."""

from __future__ import annotations

from pathlib import Path
from typing import Callable


class ReportCoverNotFoundError(Exception):
    """Raised when the requested report-cover artifact is absent."""


class ReportCoverService:
    def __init__(
        self,
        *,
        cover_dir: Path,
        guess_type: Callable[[str], tuple[str | None, str | None]],
    ) -> None:
        self._cover_dir = cover_dir
        self._guess_type = guess_type

    def load(self, filename: str) -> tuple[bytes, str]:
        path = self._cover_dir / filename
        if not path.is_file():
            raise ReportCoverNotFoundError
        return path.read_bytes(), self._guess_type(path.name)[0] or "image/jpeg"
