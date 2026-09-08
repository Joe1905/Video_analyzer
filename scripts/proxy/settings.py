from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path.cwd()
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "proxy_pool.sqlite"
DEFAULT_NOVNC_PUBLIC_URL = os.getenv("NOVNC_PUBLIC_URL", "http://192.168.1.254:6080/vnc.html?autoconnect=1&resize=scale")
DEFAULT_MIHOMO_API = os.getenv("MIHOMO_API_URL", "http://127.0.0.1:9090")
SYSTEM_PROXY_DIALER = "GLOBAL"
PROXY_CONFIG_NAMESPACE = os.getenv("PROXY_POOL_CONFIG_NAMESPACE", "v2").strip() or "v2"
PROXY_MIHOMO_NAME_PREFIX = os.getenv("PROXY_POOL_MIHOMO_PREFIX", "v2-").strip()
SING_BOX_CONFIG_PATH = Path(os.getenv("SING_BOX_CONFIG_PATH", str(DATA_DIR / "sing-box" / "config.json")))
SING_BOX_COMPOSE_PROJECT = os.getenv("SING_BOX_COMPOSE_PROJECT", "short-video-analyzer-ui-4004").strip() or "short-video-analyzer-ui-4004"
SING_BOX_COMPOSE_SERVICE = os.getenv("SING_BOX_COMPOSE_SERVICE", "sing-box").strip() or "sing-box"
PROXY_PORT_START = int(os.getenv("PROXY_POOL_PORT_START", "19300") or "19300")
PROXY_PORT_END = int(os.getenv("PROXY_POOL_PORT_END", "19399") or "19399")
PORT_SCOPE_DEFAULT = "default"
PROXY_REQUEST_ATTEMPTS = max(1, int(os.getenv("PROXY_REQUEST_ATTEMPTS", "3") or "3"))
PROXY_REACHABILITY_ATTEMPTS = max(1, int(os.getenv("PROXY_REACHABILITY_ATTEMPTS", "10") or "10"))
PROXY_RECHECK_DELAYS_SECONDS = (5 * 60, 10 * 60, 30 * 60)
PROXY_QUEUE_RECHECK_SECONDS = 5 * 60
PROXY_RETRYABLE_ERROR_MARKERS = (
    "通过服务器 mihomo 查询出口 ip 失败",
    "无法读取服务器 mihomo 节点",
    "ip 查询接口没有返回出口 ip",
    "服务器未能自动查询到出口 ip",
    "浏览器出口 ip 校验失败",
    "浏览器出口 ip",
    "当前出口 ip",
    "代理 ip 校验未通过",
    "tiktok 连通性校验失败",
    "代理状态为 不可用",
    "代理状态为 异常",
)
NOVNC_PORT = int(os.getenv("NOVNC_PORT", "6080") or "6080")
NOVNC_MANUAL_PORTS = int(os.getenv("NOVNC_MANUAL_PORTS", "1") or "1")
VNC_PORT = int(os.getenv("VNC_PORT", "5900") or "5900")
CDP_PORT = int(os.getenv("TIKTOK_CDP_PORT_START", "19220") or "19220")
XVFB_DISPLAY_BASE = int(os.getenv("TIKTOK_XVFB_DISPLAY_BASE", "90") or "90")
TIKTOK_BROWSER_UID = int(os.getenv("TIKTOK_BROWSER_UID", "10001") or "10001")
TIKTOK_BROWSER_GID = int(os.getenv("TIKTOK_BROWSER_GID", "10001") or "10001")
TIKTOK_BROWSER_LOCALE = os.getenv("TIKTOK_BROWSER_LOCALE", "en-US").strip() or "en-US"
TIKTOK_BROWSER_ACCEPT_LANGUAGE = os.getenv("TIKTOK_BROWSER_ACCEPT_LANGUAGE", "en-US,en").strip() or "en-US,en"
RUNTIME_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
PLATFORM_START_URLS = {
    "tiktok": "https://www.tiktok.com/?lang=en",
    "instagram": "https://www.instagram.com/",
}
STATUS_ACTIVE = "启用"
STATUS_PAUSED = "禁用"
STATUS_ERROR = "不可用"
STATUS_DUPLICATE = "IP重复"
STATUS_MAP = {
    "active": STATUS_ACTIVE,
    "enabled": STATUS_ACTIVE,
    "ok": STATUS_ACTIVE,
    "bound": STATUS_ACTIVE,
    "可用": STATUS_ACTIVE,
    "已绑定": STATUS_ACTIVE,
    "未绑定": STATUS_ACTIVE,
    "启用": STATUS_ACTIVE,
    "paused": STATUS_PAUSED,
    "disabled": STATUS_PAUSED,
    "disable": STATUS_PAUSED,
    "暂停": STATUS_PAUSED,
    "禁用": STATUS_PAUSED,
    "blocked": STATUS_ERROR,
    "error": STATUS_ERROR,
    "异常": STATUS_ERROR,
    "不可用": STATUS_ERROR,
    "duplicate": STATUS_DUPLICATE,
    "duplicate_ip": STATUS_DUPLICATE,
    "ip重复": STATUS_DUPLICATE,
    "IP重复": STATUS_DUPLICATE,
}
ACCOUNT_STATUS_ACTIVE = "可用"
ACCOUNT_STATUS_PAUSED = "暂停"
ACCOUNT_STATUS_ERROR = "异常"
ACCOUNT_STATUS_MAP = {
    "active": ACCOUNT_STATUS_ACTIVE,
    "enabled": ACCOUNT_STATUS_ACTIVE,
    "ok": ACCOUNT_STATUS_ACTIVE,
    "可用": ACCOUNT_STATUS_ACTIVE,
    "paused": ACCOUNT_STATUS_PAUSED,
    "disabled": ACCOUNT_STATUS_PAUSED,
    "暂停": ACCOUNT_STATUS_PAUSED,
    "blocked": ACCOUNT_STATUS_ERROR,
    "error": ACCOUNT_STATUS_ERROR,
    "异常": ACCOUNT_STATUS_ERROR,
}


def browser_max_slots() -> int:
    return max(1, int(os.getenv("TIKTOK_BROWSER_MAX_SLOTS", "4") or "4"))


def hidden_automation_slots() -> int:
    configured = max(0, int(os.getenv("TIKTOK_BROWSER_HIDDEN_AUTOMATION_SLOTS", "1") or "1"))
    # Keep all three existing observation slots available if an older
    # deployment still explicitly caps total browser slots at three.
    return min(configured, max(0, browser_max_slots() - 3))


def visible_observation_slots() -> int:
    return browser_max_slots() - hidden_automation_slots()


def pending_login_ttl_seconds() -> int:
    return max(60, int(os.getenv("TIKTOK_PENDING_LOGIN_TTL_SECONDS", "900") or "900"))


def manual_observation_idle_seconds() -> int:
    return max(60, int(os.getenv("TIKTOK_MANUAL_OBSERVATION_IDLE_SECONDS", "300") or "300"))


def novnc_port_plan() -> dict[str, Any]:
    max_slots = browser_max_slots()
    hidden_slots = hidden_automation_slots()
    visible_slots = visible_observation_slots()
    manual_ports = max(1, NOVNC_MANUAL_PORTS)
    total_ports = visible_slots + manual_ports
    # NOVNC_PORT is reserved for the existing server-level desktop. Account
    # sessions use the following ports so they cannot attach to another app.
    session_base = NOVNC_PORT + 1
    end_port = session_base + total_ports - 1
    return {
        "base_port": session_base,
        "reserved_port": NOVNC_PORT,
        "manual_ports": manual_ports,
        "max_slots": max_slots,
        "hidden_automation_slots": hidden_slots,
        "visible_observation_slots": visible_slots,
        "total_ports": total_ports,
        "allowed_ports": list(range(session_base, end_port + 1)),
        "allowed_range": f"{session_base}-{end_port}" if end_port != session_base else str(session_base),
    }


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_retryable_proxy_error(error: Exception | str) -> bool:
    message = str(error or "").strip().lower()
    return bool(message and any(marker in message for marker in PROXY_RETRYABLE_ERROR_MARKERS))
