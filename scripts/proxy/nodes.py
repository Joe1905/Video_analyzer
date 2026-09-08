"""Pure proxy node URI parsing."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


def _clean_text(value: Any, max_len: int = 1000) -> str:
    return str(value or "").strip()[:max_len]


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
