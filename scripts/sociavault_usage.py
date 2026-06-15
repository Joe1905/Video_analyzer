"""Track SociaVault credit balance from API response metadata."""
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
USAGE_FILE = ROOT / "data" / "sociavault_usage.json"


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
