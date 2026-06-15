"""Small mihomo selector state machine for US-region proxy workflows."""
from __future__ import annotations

import os
import time
from typing import Any, Callable
from urllib.parse import quote

import requests
from urllib3.exceptions import InsecureRequestWarning


requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)


DEFAULT_MIHOMO_API = "http://127.0.0.1:9090"
DEFAULT_SELECTOR = "美国出口"
DEFAULT_US_KEYWORDS = ("美国", "US", "United States", "USA", "🇺🇸")


def _headers() -> dict[str, str]:
    secret = os.getenv("MIHOMO_SECRET", "").strip()
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def _api_url(path: str) -> str:
    return os.getenv("MIHOMO_API_URL", DEFAULT_MIHOMO_API).rstrip("/") + path


def _proxy_for(kind: str) -> str:
    if kind == "amazon":
        return os.getenv("AMAZON_PROXY", "").strip()
    if kind == "tiktok":
        return os.getenv("TIKTOK_PROXY_URL", "").strip()
    return os.getenv("TIKTOK_PROXY_URL", "").strip() or os.getenv("AMAZON_PROXY", "").strip()


def _test_url_for(kind: str) -> str:
    if kind == "amazon":
        return os.getenv("AMAZON_PROXY_TEST_URL", "https://www.amazon.com/")
    if kind == "tiktok":
        return os.getenv("TIKTOK_PROXY_TEST_URL", "https://www.tiktok.com/")
    return os.getenv("PROXY_TEST_URL", "https://www.tiktok.com/")


def _candidate_keywords() -> tuple[str, ...]:
    configured = os.getenv("MIHOMO_US_KEYWORDS", "").strip()
    if configured:
        return tuple(item.strip() for item in configured.split(",") if item.strip())
    return DEFAULT_US_KEYWORDS


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)
    else:
        print(message, flush=True)


def _matches_us_policy(name: str) -> bool:
    lowered = name.lower()
    return any(keyword.lower() in lowered for keyword in _candidate_keywords())


def _probe_proxy(proxy_url: str, test_url: str, timeout: float = 8.0) -> tuple[bool, str]:
    if not proxy_url:
        return False, "proxy url is empty"
    try:
        response = requests.get(
            test_url,
            proxies={"http": proxy_url, "https": proxy_url},
            headers={"User-Agent": "Mozilla/5.0 Chrome/122.0 Safari/537.36"},
            timeout=timeout,
            verify=False,
        )
        if 200 <= response.status_code < 500:
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}"
    except Exception as exc:
        return False, str(exc)


def _selector_state(selector: str) -> dict[str, Any]:
    response = requests.get(_api_url(f"/proxies/{quote(selector, safe='')}"), headers=_headers(), timeout=5)
    response.raise_for_status()
    return response.json()


def _switch_selector(selector: str, candidate: str) -> None:
    response = requests.put(
        _api_url(f"/proxies/{quote(selector, safe='')}"),
        headers={**_headers(), "Content-Type": "application/json"},
        json={"name": candidate},
        timeout=5,
    )
    response.raise_for_status()


def ensure_us_proxy(kind: str, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Ensure the configured mihomo selector is on a working US candidate.

    Returns a status dict and never raises for mihomo/probe failures; callers can
    proceed with the currently configured proxy if the controller is unavailable.
    """
    if os.getenv("MIHOMO_PROXY_AUTO_SWITCH", "1").strip().lower() in {"0", "false", "no"}:
        return {"enabled": False, "switched": False, "reason": "disabled"}

    proxy_url = _proxy_for(kind)
    selector = os.getenv("MIHOMO_US_SELECTOR", os.getenv("MIHOMO_SELECTOR", DEFAULT_SELECTOR)).strip() or DEFAULT_SELECTOR
    test_url = _test_url_for(kind)
    started = time.monotonic()
    try:
        state = _selector_state(selector)
    except Exception as exc:
        _log(log, f"[PROXY] mihomo selector check skipped: {exc}")
        return {"enabled": True, "switched": False, "reason": str(exc)}

    current = str(state.get("now") or "")
    ok, detail = _probe_proxy(proxy_url, test_url)
    if ok and _matches_us_policy(current):
        _log(log, f"[PROXY] {selector} current ok: {current} ({detail})")
        return {"enabled": True, "switched": False, "selector": selector, "current": current, "detail": detail}

    candidates = [
        str(name)
        for name in state.get("all", [])
        if isinstance(name, str) and name != current and _matches_us_policy(name)
    ]
    _log(log, f"[PROXY] {selector} current unavailable or non-US: {current or 'unknown'} ({detail}); candidates={len(candidates)}")
    for candidate in candidates:
        try:
            _switch_selector(selector, candidate)
            time.sleep(float(os.getenv("MIHOMO_SWITCH_SETTLE_SECONDS", "0.8")))
            ok, detail = _probe_proxy(proxy_url, test_url)
            if ok:
                _log(log, f"[PROXY] switched {selector} -> {candidate} ({detail})")
                return {
                    "enabled": True,
                    "switched": True,
                    "selector": selector,
                    "current": candidate,
                    "detail": detail,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            _log(log, f"[PROXY] candidate failed: {candidate} ({detail})")
        except Exception as exc:
            _log(log, f"[PROXY] candidate switch failed: {candidate} ({exc})")

    return {
        "enabled": True,
        "switched": False,
        "selector": selector,
        "current": current,
        "detail": detail,
        "reason": "no working US candidate",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
