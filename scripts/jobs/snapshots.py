"""Pure, domain-specific job snapshot adapters."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def snapshot_download_job(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "url": job.url,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "filename": job.filename,
        "error": job.error,
        "log": list(job.log[-80:]),
        "result": deepcopy(job.result),
    }


def snapshot_shop_job(job: Any, *, extract: Any, analysis: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "url": job.url,
        "source_type": job.source_type,
        "region": job.region,
        "max_pages": job.max_pages,
        "review_pages": job.review_pages,
        "analyze": job.analyze,
        "related_videos": job.related_videos,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": list(job.log[-120:]),
        "extract": deepcopy(extract),
        "analysis": deepcopy(analysis),
    }


def snapshot_metrics_job(job: Any, *, result: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "target": job.target,
        "endpoint": job.endpoint,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": list(job.log[-120:]),
        "result": deepcopy(result),
    }


def snapshot_amazon_job(job: Any, *, result: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "target": job.target,
        "target_type": job.target_type,
        "url": job.url,
        "pages": job.pages,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": list(job.log[-120:]),
        "result": deepcopy(result),
    }
