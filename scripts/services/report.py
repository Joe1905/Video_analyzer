"""Daily report orchestration without HTTP or report-core imports."""

from __future__ import annotations

import re
from typing import Any, Callable


class ReportDisabledError(RuntimeError):
    """Preserve the report-run feature-off response across route boundaries."""


def _coerce_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _compact_report_text(value: Any, max_len: int = 600) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, list):
        text = "；".join(_compact_report_text(item, max_len=max_len) for item in value if item)
    elif isinstance(value, dict):
        parts = []
        for key, item in value.items():
            item_text = _compact_report_text(item, max_len=max_len)
            if item_text:
                parts.append(f"{key}: {item_text}")
        text = "；".join(parts)
    else:
        text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len].rstrip() + ("..." if len(text) > max_len else "")


def _metric_from_video(video: dict[str, Any], key: str) -> int:
    return _coerce_int((video.get("metrics") or {}).get(key))


def _format_report_count(value: int) -> str:
    value = _coerce_int(value)
    if value >= 10000:
        rounded = round(value / 10000, 1)
        return f"{rounded:g}万"
    return str(value)


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

    def feishu_payload(
        self,
        report_date: str | None,
        *,
        limit: int,
        detail_origin: str,
    ) -> dict[str, Any]:
        report = self._get_report(report_date, include_raw=False, detail=True)
        videos = list(report.get("videos") or [])[: max(1, min(limit, 20))]
        report_body = report.get("report") or {}
        markdown = str(report.get("report_markdown") or "").strip()
        summary = (
            _compact_report_text(report_body.get("summary") if isinstance(report_body, dict) else "")
            or _compact_report_text(report_body.get("overall_conclusion") if isinstance(report_body, dict) else "")
            or _compact_report_text(markdown, max_len=800)
        )
        date = str(report.get("report_date") or report_date or "")
        title = f"{date} 爆款视频日报" if date else "爆款视频日报"
        compact_videos = []
        for index, video in enumerate(videos, start=1):
            compact_videos.append(
                {
                    "rank": _coerce_int(video.get("report_rank")) or index,
                    "platform": video.get("platform") or "",
                    "video_id": video.get("video_id") or "",
                    "title": video.get("title") or "无标题",
                    "author": video.get("author") or "",
                    "source_label": video.get("source_label") or "",
                    "source_endpoint": video.get("source_endpoint") or "",
                    "source_url": video.get("source_url") or "",
                    "cover_url": video.get("cover_url") or "",
                    "hot_score": _coerce_int(video.get("hot_score")),
                    "play_count": _metric_from_video(video, "play_count"),
                    "like_count": _metric_from_video(video, "like_count"),
                    "comment_count": _metric_from_video(video, "comment_count"),
                    "share_count": _metric_from_video(video, "share_count"),
                    "favorite_count": _metric_from_video(video, "favorite_count"),
                    "published_at": (video.get("metrics") or {}).get("published_at"),
                    "insight": video.get("insight") or {},
                }
            )
        lines = [
            f"**{title}**",
            f"状态：{report.get('status', 'missing')}｜视频：{report.get('video_count', len(compact_videos))}｜成功：{report.get('analysis_success_count', 0)}｜失败：{report.get('analysis_failed_count', 0)}",
        ]
        if summary:
            lines.extend(["", f"总体结论：{summary}"])
        if compact_videos:
            lines.extend(["", "Top 视频："])
            for item in compact_videos[:10]:
                title_text = _compact_report_text(item["title"], max_len=80) or "无标题"
                lines.append(
                    f"{item['rank']}. {title_text}｜播放 {_format_report_count(item['play_count'])}｜热度 {_format_report_count(item['hot_score'])}"
                )
        detail_url = f"{detail_origin}/report?date={date}" if date and detail_origin else (
            f"/report?date={date}" if date else "/report"
        )
        lines.extend(["", f"详情：{detail_url}"])
        return {
            "ok": bool(report.get("exists")),
            "exists": bool(report.get("exists")),
            "report_date": date,
            "status": report.get("status", "missing"),
            "title": title,
            "summary": summary,
            "url": detail_url,
            "generated_at": report.get("llm_generated_at") or report.get("updated_at") or "",
            "video_count": report.get("video_count", len(compact_videos)),
            "analysis_success_count": report.get("analysis_success_count", 0),
            "analysis_failed_count": report.get("analysis_failed_count", 0),
            "error": report.get("error") or "",
            "report": report_body,
            "report_markdown": markdown,
            "videos": compact_videos,
            "feishu_text": markdown or "\n".join(lines),
        }

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
