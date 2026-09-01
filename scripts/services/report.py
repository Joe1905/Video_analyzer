"""Daily report orchestration without HTTP or report-core imports."""

from __future__ import annotations

from typing import Any, Callable


class ReportDisabledError(RuntimeError):
    """Preserve the report-run feature-off response across route boundaries."""


class ReportService:
    """Expose the existing report-core callbacks through a narrow public API."""

    def __init__(
        self,
        *,
        is_enabled: Callable[[], bool],
        get_report: Callable[..., dict[str, Any]],
        list_reports: Callable[[int], list[dict[str, Any]]],
        get_settings: Callable[[], dict[str, Any]],
        get_runtime_status: Callable[[], dict[str, Any]],
        get_progress: Callable[[str | None], dict[str, Any]],
        recover: Callable[[], None],
        enqueue: Callable[[], dict[str, Any]],
        delete: Callable[[str], dict[str, Any]],
        save: Callable[[dict[str, Any]], dict[str, Any]],
        translate: Callable[[str, str, str, bool], dict[str, Any]],
        backfill: Callable[[], dict[str, Any]],
    ) -> None:
        self._is_enabled = is_enabled
        self._get_report = get_report
        self._list_reports = list_reports
        self._get_settings = get_settings
        self._get_runtime_status = get_runtime_status
        self._get_progress = get_progress
        self._recover = recover
        self._enqueue = enqueue
        self._delete = delete
        self._save = save
        self._translate = translate
        self._backfill = backfill

    def is_enabled(self) -> bool:
        return self._is_enabled()

    def today(self, *, include_raw: bool) -> dict[str, Any]:
        return self._get_report(include_raw=include_raw, detail=include_raw)

    def dated_report(self, report_date: str | None, *, include_raw: bool) -> dict[str, Any]:
        return self._get_report(report_date, include_raw=include_raw, detail=True)

    def history(self, limit: int) -> list[dict[str, Any]]:
        return self._list_reports(limit)

    def settings(self) -> dict[str, Any]:
        return {**self._get_settings(), **self._get_runtime_status()}

    def progress(self, report_date: str | None) -> dict[str, Any]:
        return self._get_progress(report_date)

    def run(self) -> dict[str, Any]:
        if not self._is_enabled():
            raise ReportDisabledError("日报功能已暂停")
        self._recover()
        payload = self._enqueue()
        payload["report"] = self._get_report(include_raw=False, detail=False)
        return payload

    def delete(self, report_date: str) -> dict[str, Any]:
        return self._delete(report_date)

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._save(payload)

    def translate(self, report_date: str, platform: str, video_id: str, force: bool) -> dict[str, Any]:
        return self._translate(report_date, platform, video_id, force)

    def backfill(self) -> dict[str, Any]:
        return self._backfill()
