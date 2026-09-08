"""Proxy node parsing, port rules, and row serialization."""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from proxy import settings


def _clean_text(value: Any, max_len: int = 1000) -> str:
    return str(value or "").strip()[:max_len]


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _clean_status(value: Any, default: str = settings.STATUS_ACTIVE) -> str:
    raw = _clean_text(value, 40)
    if not raw:
        return default
    return settings.STATUS_MAP.get(
        raw.lower(),
        settings.STATUS_MAP.get(
            raw,
            raw if raw in {settings.STATUS_ACTIVE, settings.STATUS_PAUSED, settings.STATUS_ERROR, settings.STATUS_DUPLICATE} else default,
        ),
    )


def _clean_account_status(value: Any, default: str = settings.ACCOUNT_STATUS_ACTIVE) -> str:
    raw = _clean_text(value, 40)
    if not raw:
        return default
    return settings.ACCOUNT_STATUS_MAP.get(
        raw.lower(),
        settings.ACCOUNT_STATUS_MAP.get(
            raw,
            raw if raw in {settings.ACCOUNT_STATUS_ACTIVE, settings.ACCOUNT_STATUS_PAUSED, settings.ACCOUNT_STATUS_ERROR} else default,
        ),
    )


def _clean_port_scope(value: Any) -> str:
    return settings.PORT_SCOPE_DEFAULT


def _port_range(port_scope: str = settings.PORT_SCOPE_DEFAULT) -> tuple[int, int]:
    return settings.PROXY_PORT_START, settings.PROXY_PORT_END


def _validate_port_ranges() -> None:
    if settings.PROXY_PORT_START < 1024 or settings.PROXY_PORT_END < settings.PROXY_PORT_START or settings.PROXY_PORT_END > 65535:
        raise ValueError(f"IP 池代理端口范围无效：{settings.PROXY_PORT_START}-{settings.PROXY_PORT_END}")


def _row_to_pool(
    row: sqlite3.Row,
    account_count: int = 0,
    account_names: list[str] | None = None,
    pending_job_count: int = 0,
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "source_type": row["source_type"],
        "source_uri": row["source_uri"],
        "dialer_proxy": row["dialer_proxy"],
        "expected_exit_ip": row["expected_exit_ip"],
        "region": row["region"],
        "local_port": row["local_port"],
        "detected_exit_ip": row["detected_exit_ip"],
        "detected_country": row["detected_country"],
        "detected_region": row["detected_region"],
        "detected_city": row["detected_city"],
        "detected_address": row["detected_address"],
        "detected_at": row["detected_at"],
        "auto_check_failures": int(row["auto_check_failures"] or 0),
        "next_auto_check_at": row["next_auto_check_at"],
        "last_auto_check_at": row["last_auto_check_at"],
        "status": _clean_status(row["status"]),
        "notes": row["notes"],
        "parse_status": row["parse_status"],
        "parse_error": row["parse_error"],
        "mihomo_name": row["mihomo_name"],
        "parsed": _json_loads(row["parsed_json"], {}),
        "mihomo_proxy": _json_loads(row["mihomo_proxy_json"], {}),
        "account_count": account_count,
        "account_names": account_names or [],
        "pending_job_count": pending_job_count,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def parse_vless_uri(uri: str, fallback_name: str = "") -> dict[str, Any]:
    uri = _clean_text(uri, 10000)
    if not uri:
        return {"parse_status": "manual", "parsed": {}, "mihomo_proxy": {}, "mihomo_name": fallback_name}
    if not uri.startswith("vless://"):
        raise ValueError("Only vless:// URI is supported")

    parsed = urlparse(uri)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    if not parsed.username and not parsed.port:
        encoded = (parsed.netloc + parsed.path).strip("/")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
            if "@" in decoded:
                userinfo, hostinfo = decoded.rsplit("@", 1)
                uuid_part = userinfo.split(":", 1)[-1]
                rebuilt = f"vless://{uuid_part}@{hostinfo}"
                parsed = urlparse(rebuilt)
        except (binascii.Error, UnicodeError, ValueError):
            pass
    uuid = unquote(parsed.username or "")
    server = parsed.hostname or ""
    port = parsed.port
    name = unquote(parsed.fragment or "") or unquote(query.get("remarks") or query.get("remark") or "") or fallback_name or server or "vless-node"
    if not uuid or not server or not port:
        raise ValueError("VLESS URI must include uuid, server and port")

    network = query.get("type") or query.get("network") or "tcp"
    security = query.get("security", "")
    tls_enabled = security in {"tls", "reality"} or query.get("tls") in {"1", "true", "tls"}
    reality_enabled = security == "reality" or bool(query.get("pbk"))
    mihomo: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": server,
        "port": int(port),
        "uuid": uuid,
        "network": network,
        "udp": True,
    }
    if query.get("flow"):
        mihomo["flow"] = query["flow"]
    if tls_enabled or reality_enabled:
        mihomo["tls"] = True
    if reality_enabled:
        mihomo["reality-opts"] = {}
        if query.get("pbk"):
            mihomo["reality-opts"]["public-key"] = query["pbk"]
        if query.get("sid"):
            mihomo["reality-opts"]["short-id"] = query["sid"]
    if query.get("sni") or query.get("peer"):
        mihomo["servername"] = query.get("sni") or query.get("peer")
    if query.get("fp"):
        mihomo["client-fingerprint"] = query["fp"]
    elif reality_enabled:
        mihomo["client-fingerprint"] = "chrome"
    if network == "ws":
        ws_opts: dict[str, Any] = {}
        if query.get("path"):
            ws_opts["path"] = query["path"]
        if query.get("host"):
            ws_opts["headers"] = {"Host": query["host"]}
        if ws_opts:
            mihomo["ws-opts"] = ws_opts
    if network == "grpc" and query.get("serviceName"):
        mihomo["grpc-opts"] = {"grpc-service-name": query["serviceName"]}

    return {
        "parse_status": "ok",
        "mihomo_name": name,
        "parsed": {
            "uuid": uuid,
            "server": server,
            "port": int(port),
            "network": network,
            "security": security,
            "query": query,
            "name": name,
        },
        "mihomo_proxy": mihomo,
    }


def parse_vmess_uri(uri: str, fallback_name: str = "") -> dict[str, Any]:
    uri = _clean_text(uri, 10000)
    if not uri:
        return {"parse_status": "manual", "parsed": {}, "mihomo_proxy": {}, "mihomo_name": fallback_name}
    if not uri.startswith("vmess://"):
        raise ValueError("Only vmess:// URI is supported")

    parsed_uri = urlparse(uri)
    query = {key: values[-1] for key, values in parse_qs(parsed_uri.query).items() if values}
    encoded = unquote((parsed_uri.netloc + parsed_uri.path).strip("/"))
    if not encoded:
        raise ValueError("VMess URI must include a Base64 payload")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise ValueError("VMess URI payload is not valid Base64") from exc

    config: dict[str, Any]
    try:
        value = json.loads(decoded)
        config = value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        config = {}

    if config:
        uuid = _clean_text(config.get("id"), 200)
        server = _clean_text(config.get("add"), 500)
        port_value = config.get("port")
        name = _clean_text(config.get("ps"), 500) or fallback_name or server or "vmess-node"
        cipher = _clean_text(config.get("scy"), 80) or "auto"
        alter_id_value = config.get("aid", 0)
        network = _clean_text(config.get("net"), 80) or "tcp"
        security = _clean_text(config.get("tls"), 80)
        host = _clean_text(config.get("host"), 1000)
        path = _clean_text(config.get("path"), 2000)
        servername = _clean_text(config.get("sni"), 500)
        fingerprint = _clean_text(config.get("fp"), 80)
    else:
        authority = urlparse(f"vmess://{decoded.strip()}")
        uuid = unquote(authority.password or authority.username or "")
        server = authority.hostname or ""
        port_value = authority.port
        name = unquote(query.get("remarks") or query.get("remark") or "") or fallback_name or server or "vmess-node"
        cipher = unquote(authority.username or "") if authority.password else "auto"
        alter_id_value = query.get("alterId", query.get("aid", 0))
        network = query.get("type") or query.get("network") or "tcp"
        security = query.get("security") or query.get("tls") or ""
        host = query.get("host") or ""
        path = query.get("path") or ""
        servername = query.get("sni") or query.get("peer") or ""
        fingerprint = query.get("fp") or ""

    try:
        port = int(port_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("VMess URI port is invalid") from exc
    try:
        alter_id = int(alter_id_value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("VMess URI alterId is invalid") from exc
    if not uuid or not server or not port:
        raise ValueError("VMess URI must include uuid, server and port")

    tls_enabled = str(security).lower() in {"1", "true", "tls"}
    mihomo: dict[str, Any] = {
        "name": name,
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": uuid,
        "alterId": alter_id,
        "cipher": cipher,
        "network": network,
        "udp": str(query.get("udp", "1")).lower() not in {"0", "false", "no", "off"},
    }
    if tls_enabled:
        mihomo["tls"] = True
    if servername:
        mihomo["servername"] = servername
    if fingerprint:
        mihomo["client-fingerprint"] = fingerprint
    if network == "ws":
        ws_opts: dict[str, Any] = {}
        if path:
            ws_opts["path"] = path
        if host:
            ws_opts["headers"] = {"Host": host}
        if ws_opts:
            mihomo["ws-opts"] = ws_opts
    if network == "grpc" and (config.get("path") if config else query.get("serviceName")):
        mihomo["grpc-opts"] = {"grpc-service-name": config.get("path") if config else query["serviceName"]}

    return {
        "parse_status": "ok",
        "mihomo_name": name,
        "parsed": {
            "uuid": uuid,
            "server": server,
            "port": port,
            "alter_id": alter_id,
            "cipher": cipher,
            "network": network,
            "security": security,
            "query": query,
            "name": name,
        },
        "mihomo_proxy": mihomo,
    }


def parse_static_proxy_uri(uri: str, fallback_name: str = "") -> dict[str, Any]:
    uri = _clean_text(uri, 10000)
    if not uri:
        return {"parse_status": "manual", "parsed": {}, "mihomo_proxy": {}, "mihomo_name": fallback_name}

    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks", "socks5", "socks5h"}:
        raise ValueError("静态代理仅支持 socks://、socks5://、http:// 或 https://")

    if scheme == "socks" and not parsed.username and "@" not in parsed.netloc and ":" not in parsed.netloc:
        encoded = (parsed.netloc + parsed.path).strip("/")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            authority = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            parsed = urlparse(f"socks5://{authority}")
            scheme = "socks5"
        except (binascii.Error, UnicodeError, ValueError) as exc:
            raise ValueError("socks:// 订阅内容不是有效的 Base64 代理地址") from exc

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("静态代理端口无效") from exc
    server = parsed.hostname or ""
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not server or not port:
        raise ValueError("静态代理必须包含服务器和端口")

    name = unquote(parsed.fragment or "") or fallback_name or server or "static-proxy"
    mihomo_type = "socks5" if scheme in {"socks", "socks5", "socks5h"} else "http"
    mihomo: dict[str, Any] = {
        "name": name,
        "type": mihomo_type,
        "server": server,
        "port": int(port),
    }
    if username:
        mihomo["username"] = username
    if password:
        mihomo["password"] = password
    if mihomo_type == "socks5":
        mihomo["udp"] = True
    if scheme == "https":
        mihomo["tls"] = True

    return {
        "parse_status": "ok",
        "mihomo_name": name,
        "parsed": {
            "scheme": scheme,
            "server": server,
            "port": int(port),
            "username": username,
            "has_password": bool(password),
            "name": name,
        },
        "mihomo_proxy": mihomo,
    }
