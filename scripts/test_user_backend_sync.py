#!/usr/bin/env python3
"""Regression checks for user-version backend features carried into UI 4004."""

from __future__ import annotations

import os
from pathlib import Path

import proxy_pool
import tiktok_studio_collect
import tiktok_studio_publish


ROOT = Path(__file__).resolve().parents[1]


def test_browser_slot_partition_and_legacy_three_slot_compatibility() -> None:
    previous_max = os.environ.get("TIKTOK_BROWSER_MAX_SLOTS")
    previous_hidden = os.environ.get("TIKTOK_BROWSER_HIDDEN_AUTOMATION_SLOTS")
    try:
        os.environ["TIKTOK_BROWSER_MAX_SLOTS"] = "4"
        os.environ["TIKTOK_BROWSER_HIDDEN_AUTOMATION_SLOTS"] = "1"
        assert proxy_pool.browser_max_slots() == 4
        assert proxy_pool.hidden_automation_slots() == 1
        assert proxy_pool.visible_observation_slots() == 3
        assert proxy_pool._hidden_automation_slot(4) is True
        assert proxy_pool._hidden_automation_slot(3) is False

        os.environ["TIKTOK_BROWSER_MAX_SLOTS"] = "3"
        assert proxy_pool.hidden_automation_slots() == 0
        assert proxy_pool.visible_observation_slots() == 3
    finally:
        if previous_max is None:
            os.environ.pop("TIKTOK_BROWSER_MAX_SLOTS", None)
        else:
            os.environ["TIKTOK_BROWSER_MAX_SLOTS"] = previous_max
        if previous_hidden is None:
            os.environ.pop("TIKTOK_BROWSER_HIDDEN_AUTOMATION_SLOTS", None)
        else:
            os.environ["TIKTOK_BROWSER_HIDDEN_AUTOMATION_SLOTS"] = previous_hidden


def test_direct_proxy_and_mihomo_reconcile_contracts_exist() -> None:
    source = (ROOT / "scripts" / "proxy_pool.py").read_text(encoding="utf-8")
    assert 'source_type not in {"vless", "vmess", "static", "direct", "demo"}' in source
    assert 'if source_type == "direct":' in source
    assert "def _direct_login_pool_id() -> int:" in source
    assert "def reconcile_mihomo_pool_configs() -> dict[str, Any]:" in source
    assert "def _mihomo_listener_matches(" in source


def test_collection_resume_and_rescan_contracts_exist() -> None:
    source = (ROOT / "scripts" / "tiktok_studio_collect.py").read_text(encoding="utf-8")
    assert "def start_discovery_rescans(" in source
    assert "def _write_discovery_snapshot(" in source
    assert "def _list_scroll(" in source
    assert '"rescan_discovery"' in source
    assert '"scroll_events": scroll_events' in source

    video_id = str(1_735_776_000 << 32)
    assert tiktok_studio_collect._video_id_published_date(video_id) == "2025-01-01"


def test_publish_file_input_state_is_resilient() -> None:
    class InputHandle:
        def evaluate(self, _script):
            return {"connected": True, "files": 1}

    class DetachedInput:
        def evaluate(self, _script):
            raise RuntimeError("detached")

    assert tiktok_studio_publish._file_input_selection_state(InputHandle()) == {
        "connected": True,
        "files": 1,
    }
    assert tiktok_studio_publish._file_input_selection_state(DetachedInput()) == {}

    source = (ROOT / "scripts" / "tiktok_studio_publish.py").read_text(encoding="utf-8")
    assert "def _append_file_input_trace(" in source
    assert "def _set_video_file_via_cdp(" in source
    assert '"DOM.setFileInputFiles"' in source
    assert "def _set_video_file_via_native_chooser(" in source


def test_web_and_compose_expose_only_backend_sync_contracts() -> None:
    web_source = (ROOT / "scripts" / "web_app.py").read_text(encoding="utf-8")
    assert '"/api/proxy/mihomo-reconcile"' in web_source
    assert '"/api/proxy/collect/jobs/rescan-discovery"' in web_source

    compose_path = ROOT / "docker-compose.yml"
    if compose_path.exists():
        compose_source = compose_path.read_text(encoding="utf-8")
        assert "TIKTOK_BROWSER_MAX_SLOTS: ${TIKTOK_BROWSER_MAX_SLOTS:-4}" in compose_source
        assert "TIKTOK_BROWSER_HIDDEN_AUTOMATION_SLOTS:" in compose_source
        assert "TIKTOK_HIDDEN_SLOT_MIN_AVAILABLE_MB:" in compose_source
        assert "TIKTOK_HIDDEN_SLOT_MAX_LOAD_PER_CPU:" in compose_source


if __name__ == "__main__":
    test_browser_slot_partition_and_legacy_three_slot_compatibility()
    test_direct_proxy_and_mihomo_reconcile_contracts_exist()
    test_collection_resume_and_rescan_contracts_exist()
    test_publish_file_input_state_is_resilient()
    test_web_and_compose_expose_only_backend_sync_contracts()
    print("user backend sync tests passed")
