"""Track SociaVault credit balance from API response metadata."""
import json
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
USAGE_FILE = ROOT / "data" / "sociavault_usage.json"
BALANCE_FILE = ROOT / "data" / "sociavault_credit_balance.json"
_BALANCE_LOCK = threading.Lock()


def _parse_number(value: Any) -> float | int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def _header_map(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in dict(headers or {}).items()}


def extract_remaining_credits(headers: Any, body: Any = None) -> float | int | None:
    normalized = _header_map(headers)
    preferred_names = (
        "x-credits-remaining",
        "x-credit-remaining",
        "x-remaining-credits",
        "x-remaining-credit",
        "x-credits-available",
        "x-credit-available",
        "x-available-credits",
        "x-available-credit",
        "x-sociavault-credits-remaining",
        "x-sociavault-credit-balance",
        "x-sociavault-credits-available",
        "x-credit-balance",
        "x-credits-balance",
    )
    for name in preferred_names:
        parsed = _parse_number(normalized.get(name))
        if parsed is not None:
            return parsed

    for key, value in normalized.items():
        if ("credit" in key or "balance" in key) and ("remaining" in key or "balance" in key or "available" in key):
            parsed = _parse_number(value)
            if parsed is not None:
                return parsed

    for key, value in normalized.items():
        if "credit" in key and not any(word in key for word in ("used", "cost", "required", "spent")):
            parsed = _parse_number(value)
            if parsed is not None:
                return parsed

    if isinstance(body, dict):
        parsed = _parse_number(body.get("available"))
        if parsed is not None:
            return parsed
        error = body.get("error")
        if isinstance(error, dict):
            parsed = _parse_number(error.get("available"))
            if parsed is not None:
                return parsed
    return None


def _body_credit_fields(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    result = {}
    for key, value in body.items():
        lowered = str(key).lower()
        if any(word in lowered for word in ("credit", "balance", "remaining", "available")):
            result[str(key)] = value
    error = body.get("error")
    if isinstance(error, dict):
        for key, value in error.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("credit", "balance", "remaining", "available")):
                result[f"error.{key}"] = value
    return result


def extract_credits_used(body: Any) -> float | int | None:
    """Return the explicit per-request credit charge from a SociaVault payload."""
    if isinstance(body, dict):
        for key in ("credits_used", "creditsUsed"):
            parsed = _parse_number(body.get(key))
            if parsed is not None:
                return parsed
        children = body.values()
    elif isinstance(body, list):
        children = body
    else:
        return None
    for value in children:
        parsed = extract_credits_used(value)
        if parsed is not None:
            return parsed
    return None


def _empty_balance() -> dict[str, Any]:
    return {
        "credits": None,
        "subscription_status": "",
        "updated_at": None,
        "last_refreshed_at": None,
        "last_credits_used": None,
        "last_source": "",
        "estimated": False,
        "observed": False,
    }


def _read_balance_unlocked() -> dict[str, Any]:
    if not BALANCE_FILE.is_file():
        return _empty_balance()
    try:
        stored = json.loads(BALANCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_balance()
    if not isinstance(stored, dict):
        return _empty_balance()
    return {
        "credits": _parse_number(stored.get("credits")),
        "subscription_status": str(stored.get("subscription_status") or ""),
        "updated_at": stored.get("updated_at"),
        "last_refreshed_at": stored.get("last_refreshed_at"),
        "last_credits_used": _parse_number(stored.get("last_credits_used")),
        "last_source": str(stored.get("last_source") or ""),
        "estimated": bool(stored.get("estimated")),
        "observed": bool(stored.get("observed")),
    }


def _write_balance_unlocked(payload: dict[str, Any]) -> None:
    BALANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = BALANCE_FILE.with_suffix(BALANCE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(BALANCE_FILE)


def read_sociavault_credit_balance() -> dict[str, Any]:
    """Read the sanitized local balance snapshot exposed to the UI."""
    with _BALANCE_LOCK:
        return _read_balance_unlocked()


def set_sociavault_credit_balance(credits: Any, subscription_status: str = "") -> dict[str, Any]:
    """Replace the estimate with an authoritative balance from check_credits."""
    parsed = _parse_number(credits)
    if parsed is None or parsed < 0:
        raise ValueError("Invalid SociaVault credit balance")
    now = time.time()
    payload = {
        "credits": parsed,
        "subscription_status": str(subscription_status or ""),
        "updated_at": now,
        "last_refreshed_at": now,
        "last_credits_used": None,
        "last_source": "check_credits",
        "estimated": False,
        "observed": True,
    }
    with _BALANCE_LOCK:
        _write_balance_unlocked(payload)
    return payload


def record_sociavault_credits_used(
    credits_used: Any,
    *,
    source: str = "",
    cache_hit: bool = False,
) -> dict[str, Any]:
    """Deduct a real request charge from an existing authoritative snapshot."""
    parsed = _parse_number(credits_used)
    if cache_hit or parsed is None or parsed <= 0:
        return read_sociavault_credit_balance()
    with _BALANCE_LOCK:
        payload = _read_balance_unlocked()
        current = _parse_number(payload.get("credits"))
        if current is None or not payload.get("observed"):
            return payload
        payload.update(
            {
                "credits": max(0, current - parsed),
                "updated_at": time.time(),
                "last_credits_used": parsed,
                "last_source": str(source or "sociavault_api"),
                "estimated": True,
            }
        )
        _write_balance_unlocked(payload)
        return payload


def _usage_payload(response: Any, body: Any = None) -> dict[str, Any]:
    headers = getattr(response, "headers", None)
    normalized_headers = _header_map(headers)
    credit_headers = {
        key: value
        for key, value in normalized_headers.items()
        if any(word in key for word in ("credit", "balance", "remaining", "available", "rate-limit", "ratelimit"))
    }
    remaining = extract_remaining_credits(headers, body)
    return {
        "remaining_credits": remaining,
        "updated_at": time.time(),
        "status_code": getattr(response, "status_code", None),
        "credit_headers": credit_headers,
        "body_credit_fields": _body_credit_fields(body),
        "observed": True,
        "has_remaining_header": remaining is not None,
    }


def update_sociavault_usage_from_response(response: Any, body: Any = None) -> None:
    payload = _usage_payload(response, body)
    write_error = None
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        write_error = str(exc)
    try:
        remaining = payload.get("remaining_credits")
        if remaining is not None:
            set_sociavault_credit_balance(remaining)
        else:
            record_sociavault_credits_used(
                extract_credits_used(body),
                source="sociavault_rest",
            )
    except OSError as exc:
        print(f"[SOCIAVAULT CREDITS] ledger_write_failed error_type={type(exc).__name__}", flush=True)
    print(
        "[SOCIAVAULT_USAGE] "
        + json.dumps(
            {
                "status_code": payload.get("status_code"),
                "remaining_credits": payload.get("remaining_credits"),
                "credit_headers": payload.get("credit_headers"),
                "body_credit_fields": payload.get("body_credit_fields"),
                "write_error": write_error,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def read_sociavault_usage() -> dict[str, Any]:
    if not USAGE_FILE.is_file():
        return {"remaining_credits": None, "updated_at": None, "observed": False}
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"remaining_credits": None, "updated_at": None, "observed": False}
    return {
        "remaining_credits": data.get("remaining_credits"),
        "updated_at": data.get("updated_at"),
        "status_code": data.get("status_code"),
        "credit_headers": data.get("credit_headers") or {},
        "body_credit_fields": data.get("body_credit_fields") or {},
        "observed": bool(data.get("observed")),
        "has_remaining_header": bool(data.get("has_remaining_header")),
    }
