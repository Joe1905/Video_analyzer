#!/usr/bin/env python3
"""Pure contracts for the zero-runtime-switch job snapshot adapters."""

from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from jobs.snapshots import (  # noqa: E402
    snapshot_amazon_job,
    snapshot_download_job,
    snapshot_metrics_job,
    snapshot_shop_job,
)


def assert_module_structure() -> None:
    source_path = SCRIPTS_DIR / "jobs" / "snapshots.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {"__future__", "copy", "typing"}
    assert not any(isinstance(node, ast.Import) for node in tree.body)
    assert not any(isinstance(node, ast.ClassDef) for node in tree.body)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert [node.name for node in functions] == [
        "snapshot_download_job",
        "snapshot_shop_job",
        "snapshot_metrics_job",
        "snapshot_amazon_job",
    ]
    assert all(not node.name.startswith("_") for node in functions)
    assert "web_app" not in source
    assert "routes" not in source
    assert "services" not in source
    assert "read_" not in source
    assert ".open(" not in source


def assert_download_snapshot() -> None:
    job = SimpleNamespace(
        id="download-1",
        url="https://example.invalid/download",
        status="complete",
        created_at=1.0,
        updated_at=2.0,
        filename="fixture.mp4",
        error=None,
        log=[f"download-{index}" for index in range(82)],
        result={"nested": {"value": 1}},
        source="excluded",
    )
    before = copy.deepcopy(job.__dict__)
    snapshot = snapshot_download_job(job)
    assert snapshot == {
        "id": "download-1",
        "url": "https://example.invalid/download",
        "status": "complete",
        "created_at": 1.0,
        "updated_at": 2.0,
        "filename": "fixture.mp4",
        "error": None,
        "log": [f"download-{index}" for index in range(2, 82)],
        "result": {"nested": {"value": 1}},
    }
    assert "source" not in snapshot
    assert snapshot["log"] is not job.log
    assert snapshot["result"] is not job.result
    assert job.__dict__ == before
    none_result_job = SimpleNamespace(**{**before, "result": None})
    assert snapshot_download_job(none_result_job)["result"] is None
    job.log.append("later")
    job.result["nested"]["value"] = 2
    assert snapshot["log"] == [f"download-{index}" for index in range(2, 82)]
    assert snapshot["result"] == {"nested": {"value": 1}}
    snapshot["result"]["nested"]["value"] = 3
    assert job.result == {"nested": {"value": 2}}


def assert_shop_snapshot() -> None:
    job = SimpleNamespace(
        id="shop-1",
        url="https://example.invalid/shop",
        source_type="product",
        region="US",
        max_pages=2,
        review_pages=1,
        analyze=True,
        related_videos=False,
        prompt="excluded",
        status="complete",
        created_at=3.0,
        updated_at=4.0,
        output_dir="output/shop-1",
        error=None,
        log=[f"shop-{index}" for index in range(122)],
    )
    extract: dict[str, Any] = {"nested": {"id": "extract"}}
    analysis: dict[str, Any] = {"nested": {"id": "analysis"}}
    before = copy.deepcopy((job.__dict__, extract, analysis))
    snapshot = snapshot_shop_job(job, extract=extract, analysis=analysis)
    assert snapshot == {
        "id": "shop-1",
        "url": "https://example.invalid/shop",
        "source_type": "product",
        "region": "US",
        "max_pages": 2,
        "review_pages": 1,
        "analyze": True,
        "related_videos": False,
        "status": "complete",
        "created_at": 3.0,
        "updated_at": 4.0,
        "output_dir": "output/shop-1",
        "error": None,
        "log": [f"shop-{index}" for index in range(2, 122)],
        "extract": {"nested": {"id": "extract"}},
        "analysis": {"nested": {"id": "analysis"}},
    }
    assert "prompt" not in snapshot
    assert snapshot["log"] is not job.log
    assert snapshot["extract"] is not extract
    assert snapshot["analysis"] is not analysis
    assert (job.__dict__, extract, analysis) == before
    extract["nested"]["id"] = "input"
    analysis["nested"]["id"] = "input"
    snapshot["extract"]["nested"]["id"] = "snapshot"
    snapshot["analysis"]["nested"]["id"] = "snapshot"
    assert extract == {"nested": {"id": "input"}}
    assert analysis == {"nested": {"id": "input"}}
    assert snapshot["extract"] == {"nested": {"id": "snapshot"}}
    assert snapshot["analysis"] == {"nested": {"id": "snapshot"}}
    assert job.__dict__ == before[0]
    assert before[1] == {"nested": {"id": "extract"}}
    assert before[2] == {"nested": {"id": "analysis"}}
    assert snapshot_shop_job(job, extract=None, analysis=None)["extract"] is None
    assert snapshot_shop_job(job, extract=None, analysis=None)["analysis"] is None


def assert_metrics_snapshot() -> None:
    job = SimpleNamespace(
        id="metrics-1",
        target="@fixture",
        endpoint="video-info",
        status="failed",
        created_at=5.0,
        updated_at=6.0,
        output_dir="output/metrics-1",
        error="fixture error",
        log=[f"metrics-{index}" for index in range(122)],
    )
    result: dict[str, Any] = {"nested": {"value": 1}}
    before = copy.deepcopy((job.__dict__, result))
    snapshot = snapshot_metrics_job(job, result=result)
    assert snapshot == {
        "id": "metrics-1",
        "target": "@fixture",
        "endpoint": "video-info",
        "status": "failed",
        "created_at": 5.0,
        "updated_at": 6.0,
        "output_dir": "output/metrics-1",
        "error": "fixture error",
        "log": [f"metrics-{index}" for index in range(2, 122)],
        "result": {"nested": {"value": 1}},
    }
    assert snapshot["log"] is not job.log
    assert snapshot["result"] is not result
    assert (job.__dict__, result) == before
    result["nested"]["value"] = 2
    snapshot["result"]["nested"]["value"] = 3
    assert result == {"nested": {"value": 2}}
    assert snapshot["result"] == {"nested": {"value": 3}}
    assert job.__dict__ == before[0]
    assert before[1] == {"nested": {"value": 1}}
    assert snapshot_metrics_job(job, result=None)["result"] is None


def assert_amazon_snapshot() -> None:
    job = SimpleNamespace(
        id="amazon-1",
        target="B000FIXTURE",
        target_type="asin",
        url="https://example.invalid/dp/B000FIXTURE",
        pages=2,
        status="complete",
        created_at=7.0,
        updated_at=8.0,
        output_dir="output/amazon-1",
        error=None,
        log=[f"amazon-{index}" for index in range(122)],
    )
    result: dict[str, Any] = {"products": [{"nested": {"asin": "B000FIXTURE"}}]}
    before = copy.deepcopy((job.__dict__, result))
    snapshot = snapshot_amazon_job(job, result=result)
    assert snapshot == {
        "id": "amazon-1",
        "target": "B000FIXTURE",
        "target_type": "asin",
        "url": "https://example.invalid/dp/B000FIXTURE",
        "pages": 2,
        "status": "complete",
        "created_at": 7.0,
        "updated_at": 8.0,
        "output_dir": "output/amazon-1",
        "error": None,
        "log": [f"amazon-{index}" for index in range(2, 122)],
        "result": {"products": [{"nested": {"asin": "B000FIXTURE"}}]},
    }
    assert snapshot["log"] is not job.log
    assert snapshot["result"] is not result
    assert (job.__dict__, result) == before
    result["products"][0]["nested"]["asin"] = "input"
    snapshot["result"]["products"][0]["nested"]["asin"] = "snapshot"
    assert result == {"products": [{"nested": {"asin": "input"}}]}
    assert snapshot["result"] == {"products": [{"nested": {"asin": "snapshot"}}]}
    assert job.__dict__ == before[0]
    assert before[1] == {"products": [{"nested": {"asin": "B000FIXTURE"}}]}
    assert snapshot_amazon_job(job, result=None)["result"] is None


def main() -> int:
    assert_module_structure()
    assert_download_snapshot()
    assert_shop_snapshot()
    assert_metrics_snapshot()
    assert_amazon_snapshot()
    print("job snapshot adapter tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
