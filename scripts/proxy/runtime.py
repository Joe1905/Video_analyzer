"""External proxy-core and browser-process runtime primitives."""

from __future__ import annotations

import base64
import calendar
import ctypes
import hashlib
import http.client
import ipaddress
import json
import os
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from proxy import repository, settings
from proxy.nodes import _json_loads


_X_IDLE_LOCK = threading.Lock()


def _memory_available_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _hidden_slot_capacity_error(slot: int) -> str:
    if slot <= settings.visible_observation_slots():
        return ""
    minimum_memory_mb = max(0, int(os.getenv("TIKTOK_HIDDEN_SLOT_MIN_AVAILABLE_MB", "4096") or "4096"))
    available_memory_mb = _memory_available_mb()
    if available_memory_mb is not None and available_memory_mb < minimum_memory_mb:
        return f"后台浏览器槽位资源不足：可用内存 {available_memory_mb}MB，至少需要 {minimum_memory_mb}MB"
    max_load_per_cpu = max(0.1, float(os.getenv("TIKTOK_HIDDEN_SLOT_MAX_LOAD_PER_CPU", "0.75") or "0.75"))
    try:
        load_per_cpu = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except (AttributeError, OSError):
        return ""
    if load_per_cpu > max_load_per_cpu:
        return f"后台浏览器槽位资源不足：当前一分钟负载 {load_per_cpu:.2f}/核，上限 {max_load_per_cpu:.2f}/核"
    return ""


class ProxyConfigurationError(ValueError):
    """Raised when the local proxy core is not configured to load a parsed pool."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            parts = stat_path.read_text(encoding="utf-8", errors="ignore").split()
            if len(parts) > 2 and parts[2] == "Z":
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _pid_alive(pid):
            break
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(0.3)
    for _ in range(10):
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited:
                return
        except (ChildProcessError, OSError):
            return
        time.sleep(0.1)


def _terminate_session_processes(row: sqlite3.Row | dict[str, Any]) -> None:
    for key in ("pid", "websockify_pid", "x11vnc_pid", "xvfb_pid"):
        try:
            pid = int(row[key] or 0)
        except Exception:
            pid = 0
        if pid:
            _terminate_pid(pid)


def _iso_epoch(value: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("event_mask", ctypes.c_ulong),
    ]


def _display_last_activity_epoch(display_name: str) -> float:
    display_name = str(display_name or "").strip()
    if not display_name:
        return 0.0
    with _X_IDLE_LOCK:
        return _read_display_last_activity_epoch(display_name)


def _read_display_last_activity_epoch(display_name: str) -> float:
    display = None
    info = None
    try:
        x11 = ctypes.CDLL("libX11.so.6")
        xss = ctypes.CDLL("libXss.so.1")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XFree.restype = ctypes.c_int
        xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
        xss.XScreenSaverQueryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_XScreenSaverInfo),
        ]
        xss.XScreenSaverQueryInfo.restype = ctypes.c_int
        display = x11.XOpenDisplay(display_name.encode("utf-8"))
        if not display:
            return 0.0
        info = xss.XScreenSaverAllocInfo()
        if not info:
            return 0.0
        root = x11.XDefaultRootWindow(display)
        if not xss.XScreenSaverQueryInfo(display, root, info):
            return 0.0
        return time.time() - (float(info.contents.idle) / 1000.0)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0.0
    finally:
        if info:
            try:
                x11.XFree(ctypes.cast(info, ctypes.c_void_p))
            except (AttributeError, UnboundLocalError):
                pass
        if display:
            try:
                x11.XCloseDisplay(display)
            except (AttributeError, UnboundLocalError):
                pass


def _manual_session_idle_expired(row: sqlite3.Row, now_epoch: float) -> bool:
    if str(row["owner"] or "") not in {"manual", "manual_review"}:
        return False
    if str(row["current_job_id"] or "").strip():
        return False
    persisted_activity = _iso_epoch(str(row["last_activity_at"] or row["updated_at"] or row["created_at"] or ""))
    display_activity = _display_last_activity_epoch(str(row["display"] or ""))
    last_activity = max(persisted_activity, display_activity)
    return bool(last_activity and last_activity + settings.manual_observation_idle_seconds() <= now_epoch)


def _browser_binary() -> str:
    configured = os.getenv("TIKTOK_BROWSER_BIN", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(["google-chrome-stable", "google-chrome", "chromium-browser", "chromium"])
    for item in candidates:
        if not item:
            continue
        found = shutil.which(item)
        if found:
            return found
        if Path(item).exists():
            return item
    for root in (
        Path.home() / ".cache" / "ms-playwright",
        Path("/root/.cache/ms-playwright"),
        Path("/ms-playwright"),
    ):
        if root.exists():
            for pattern in ("chromium-*/chrome-linux64/chrome", "chromium-*/chrome-linux/chrome"):
                for chrome in root.glob(pattern):
                    return str(chrome)
    raise ValueError("服务器未找到 Chromium/Chrome；请配置 TIKTOK_BROWSER_BIN 或安装浏览器")


def _required_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    raise ValueError(f"服务器未找到 {name}；请重建镜像或安装 noVNC 隔离依赖")


def _novnc_web_dir() -> str:
    configured = os.getenv("NOVNC_WEB_DIR", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(["/usr/share/novnc", "/usr/local/share/novnc"])
    for item in candidates:
        if item and Path(item).exists():
            return item
    raise ValueError("服务器未找到 noVNC Web 目录；请安装 novnc")


def _slot_ports(slot: int) -> dict[str, Any]:
    # All new sessions share slots 1..browser_max_slots. Slot 0 is retained
    # only so a pre-existing legacy session can still be cleaned up safely.
    offset = slot if slot > 0 else settings.browser_max_slots() + max(1, settings.NOVNC_MANUAL_PORTS)
    return {
        "display": f":{settings.XVFB_DISPLAY_BASE + slot}",
        "vnc_port": settings.VNC_PORT + offset,
        "novnc_port": settings.NOVNC_PORT + offset,
        "debug_port": settings.CDP_PORT + slot,
    }


def _hidden_automation_slot(slot: int) -> bool:
    return slot > settings.visible_observation_slots()


def _display_socket_active(path: Path) -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.15)
    try:
        client.connect(str(path))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True
    finally:
        client.close()


def _slot_ports_available(slot: int) -> bool:
    ports = _slot_ports(slot)
    display_number = str(ports["display"]).lstrip(":")
    display_socket = Path(f"/tmp/.X11-unix/X{display_number}")
    if display_socket.exists():
        if _display_socket_active(display_socket):
            return False
        try:
            display_socket.unlink()
            Path(f"/tmp/.X{display_number}-lock").unlink(missing_ok=True)
        except OSError:
            return False
    checked_ports = ["debug_port"]
    if not _hidden_automation_slot(slot):
        checked_ports = ["vnc_port", "novnc_port", *checked_ports]
    return not any(_port_open("127.0.0.1", int(ports[key]), timeout=0.15) for key in checked_ports)


def _public_novnc_url(port: int) -> str:
    parsed = urlparse(settings.DEFAULT_NOVNC_PUBLIC_URL)
    host = parsed.hostname or "192.168.1.254"
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}:{port}/vnc.html?autoconnect=1&resize=scale"


def _wait_for_port(port: int, label: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open("127.0.0.1", port, timeout=0.4):
            return
        time.sleep(0.2)
    raise ValueError(f"{label} 端口 {port} 未在 {timeout:.0f}s 内启动")


def _open_process(
    log_dir: Path,
    name: str,
    args: list[str],
    env: dict[str, str] | None = None,
    user: int | None = None,
    group: int | None = None,
) -> subprocess.Popen:
    stdout = open(log_dir / f"{name}.log", "ab")
    stderr = open(log_dir / f"{name}.err.log", "ab")
    return subprocess.Popen(
        args,
        cwd=str(settings.ROOT),
        env=env or os.environ.copy(),
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        start_new_session=True,
        user=user,
        group=group,
    )


def _launch_observation_channel(slot: int, session_id: int, log_dir: Path) -> dict[str, Any]:
    ports = _slot_ports(slot)
    display = str(ports["display"])
    vnc_port = int(ports["vnc_port"])
    novnc_port = int(ports["novnc_port"])
    xvfb = _required_binary("Xvfb")

    if not _slot_ports_available(slot):
        raise ValueError(f"观测槽位 {slot} 的显示或端口已被其他服务占用")

    xvfb_proc = _open_process(log_dir, "xvfb", [xvfb, display, "-screen", "0", "1280x900x24", "-nolisten", "tcp"])
    time.sleep(0.8)
    if not _pid_alive(int(xvfb_proc.pid)):
        raise ValueError("独立 Xvfb 显示通道启动失败")

    if _hidden_automation_slot(slot):
        return {
            "display": display,
            "vnc_port": 0,
            "novnc_port": 0,
            "channel_url": "",
            "xvfb_pid": int(xvfb_proc.pid),
            "x11vnc_pid": 0,
            "websockify_pid": 0,
        }

    x11vnc = _required_binary("x11vnc")
    websockify = _required_binary("websockify")
    novnc_web = _novnc_web_dir()
    x11vnc_proc = _open_process(
        log_dir,
        "x11vnc",
        [x11vnc, "-display", display, "-rfbport", str(vnc_port), "-localhost", "-forever", "-shared", "-nopw", "-quiet"],
    )
    websockify_proc = None
    try:
        _wait_for_port(vnc_port, "VNC")
        time.sleep(0.2)
        if not _pid_alive(int(x11vnc_proc.pid)):
            raise ValueError(f"独立 VNC 进程未能监听端口 {vnc_port}")
        websockify_proc = _open_process(
            log_dir,
            "websockify",
            [websockify, "--web", novnc_web, str(novnc_port), f"127.0.0.1:{vnc_port}"],
        )
        _wait_for_port(novnc_port, "noVNC")
        time.sleep(0.2)
        if not _pid_alive(int(websockify_proc.pid)):
            raise ValueError(f"独立 noVNC 进程未能监听端口 {novnc_port}")
    except Exception:
        if websockify_proc is not None:
            _terminate_pid(int(websockify_proc.pid))
        _terminate_pid(int(x11vnc_proc.pid))
        _terminate_pid(int(xvfb_proc.pid))
        raise

    return {
        "display": display,
        "vnc_port": vnc_port,
        "novnc_port": novnc_port,
        "channel_url": _public_novnc_url(novnc_port),
        "xvfb_pid": int(xvfb_proc.pid),
        "x11vnc_pid": int(x11vnc_proc.pid),
        "websockify_pid": int(websockify_proc.pid),
    }


def _abs_workspace_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = settings.ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prepare_browser_profile_dir(user_data_dir: Path) -> dict[str, Path]:
    profiles_roots = (
        (settings.DATA_DIR / "tiktok_browser_profiles").resolve(),
    )
    profile_root = user_data_dir.parent.resolve()
    if not any(profile_root == root or root in profile_root.parents for root in profiles_roots):
        raise ValueError("浏览器 profile 必须位于受管的数据目录")

    paths = {
        "home": profile_root / "home",
        "config": profile_root / "config",
        "cache": profile_root / "cache",
        "downloads": profile_root / "downloads",
        "runtime": profile_root / "runtime",
        "user_data": user_data_dir,
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    def chown_path(path: Path | str) -> None:
        try:
            os.chown(path, settings.TIKTOK_BROWSER_UID, settings.TIKTOK_BROWSER_GID, follow_symlinks=False)
        except FileNotFoundError:
            pass

    for current_root, dirs, files in os.walk(profile_root, followlinks=False):
        chown_path(current_root)
        for name in dirs:
            chown_path(Path(current_root) / name)
        for name in files:
            chown_path(Path(current_root) / name)
    paths["runtime"].chmod(0o700)
    return paths


def _configure_browser_preferences(user_data_dir: Path) -> None:
    preferences_path = user_data_dir / "Default" / "Preferences"
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    preferences = _json_loads(preferences_path.read_text(encoding="utf-8") if preferences_path.is_file() else "", {})
    if not isinstance(preferences, dict):
        preferences = {}
    if not isinstance(preferences.get("intl"), dict):
        preferences["intl"] = {}
    if not isinstance(preferences.get("translate"), dict):
        preferences["translate"] = {}
    preferences["intl"]["accept_languages"] = settings.TIKTOK_BROWSER_ACCEPT_LANGUAGE
    preferences["translate"]["enabled"] = False
    preferences_path.write_text(json.dumps(preferences, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _decrypt_chrome_cookie(host: str, value: str, encrypted_value: bytes) -> str:
    if value:
        return value
    encrypted = bytes(encrypted_value or b"")
    if not encrypted.startswith((b"v10", b"v11")):
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
        padding = plain[-1]
        if not 1 <= padding <= 16:
            return ""
        plain = plain[:-padding]
        host_digest = hashlib.sha256(host.encode("utf-8")).digest()
        if plain.startswith(host_digest):
            plain = plain[len(host_digest):]
        return plain.decode("utf-8", errors="strict")
    except Exception:
        return ""


def _tiktok_profile_cookies(user_data_dir: str) -> dict[str, str]:
    root = Path(user_data_dir)
    candidates = (root / "Default" / "Cookies", root / "Default" / "Network" / "Cookies")
    for cookie_path in candidates:
        if not cookie_path.is_file():
            continue
        try:
            cookie_conn = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True, timeout=1)
            try:
                rows = cookie_conn.execute(
                    "SELECT host_key, name, value, encrypted_value FROM cookies WHERE host_key LIKE '%tiktok%'"
                ).fetchall()
            finally:
                cookie_conn.close()
        except sqlite3.Error:
            continue
        cookies: dict[str, str] = {}
        for host, name, value, encrypted_value in rows:
            decoded = _decrypt_chrome_cookie(str(host), str(value or ""), encrypted_value)
            if decoded:
                cookies[str(name)] = decoded
        return cookies
    return {}


def _proxy_json_with_cookies(url: str, proxy_port: int, cookies: dict[str, str], timeout: float = 10.0) -> tuple[bool, Any, str]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": settings.TIKTOK_BROWSER_ACCEPT_LANGUAGE,
            "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
            "Referer": "https://www.tiktok.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
        return True, json.loads(raw), ""
    except HTTPError as exc:
        return False, None, f"HTTP {exc.code}"
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return False, None, str(exc)


def _tiktok_avatar_url(item: dict[str, Any]) -> str:
    for key in (
        "avatar_url",
        "avatarUrl",
        "avatar_thumb",
        "avatarThumb",
        "avatar_larger",
        "avatarLarger",
        "avatar_medium",
    ):
        value = item.get(key)
        candidates: list[Any]
        if isinstance(value, dict):
            candidates = [value.get("url"), *(value.get("url_list") or [])]
        elif isinstance(value, list):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            url = str(candidate or "").strip()
            if url.startswith(("https://", "http://")):
                return url
    return ""


def _tiktok_identity(body: Any) -> dict[str, str]:
    if not isinstance(body, dict):
        return {}
    candidates: list[dict[str, Any]] = []
    for item in (body.get("data"), body.get("user"), body):
        if isinstance(item, dict):
            candidates.append(item)
            if isinstance(item.get("user"), dict):
                candidates.append(item["user"])
    for item in candidates:
        username = str(item.get("username") or item.get("unique_id") or item.get("uniqueId") or "").strip().lstrip("@")
        user_id = str(item.get("user_id") or item.get("user_id_str") or item.get("uid") or "").strip()
        display_name = str(item.get("screen_name") or item.get("nickname") or item.get("display_name") or "").strip()
        if username or user_id:
            return {
                "username": username or f"uid_{user_id}",
                "display_name": display_name,
                "user_id": user_id,
                "avatar_url": _tiktok_avatar_url(item),
            }
    return {}


def _launch_browser_for_session(profile: dict[str, Any], pool: sqlite3.Row, session_id: int, display: str, debug_port: int, start_url: str) -> tuple[int, str]:
    isolation = profile.get("isolation") if isinstance(profile.get("isolation"), dict) else {}
    browser_settings = profile.get("browser_settings") if isinstance(profile.get("browser_settings"), dict) else {}
    user_data_dir = _abs_workspace_path(str(isolation.get("user_data_dir") or f"data/tiktok_browser_profiles/session-{session_id}/user-data"))
    _configure_browser_preferences(user_data_dir)
    profile_paths = _prepare_browser_profile_dir(user_data_dir)
    log_dir = _abs_workspace_path(f"data/tiktok_browser_sessions/{session_id}")
    browser = _browser_binary()
    proxy_port = int(pool["local_port"] or 0)
    if not proxy_port:
        raise ValueError("代理没有专用本地端口，不能启动独立浏览器")
    window_size = browser_settings.get("window_size") or (1280, 900)
    try:
        window_width, window_height = (max(800, int(window_size[0])), max(600, int(window_size[1])))
    except (TypeError, ValueError, IndexError):
        window_width, window_height = 1280, 900
    locale = str(browser_settings.get("locale") or settings.TIKTOK_BROWSER_LOCALE).strip() or settings.TIKTOK_BROWSER_LOCALE
    args = [
        browser,
        f"--user-data-dir={user_data_dir}",
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        f"--lang={locale}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-translate",
        "--disable-background-networking",
        "--disable-default-apps",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-session-crashed-bubble",
        f"--window-size={window_width},{window_height}",
        "--new-window",
        start_url,
    ]
    user_agent = str(browser_settings.get("user_agent") or "").strip()
    if user_agent:
        args.insert(4, f"--user-agent={user_agent}")
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["HOME"] = str(profile_paths["home"])
    env["XDG_CONFIG_HOME"] = str(profile_paths["config"])
    env["XDG_CACHE_HOME"] = str(profile_paths["cache"])
    env["XDG_RUNTIME_DIR"] = str(profile_paths["runtime"])
    env["LANGUAGE"] = "en_US:en"
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    proc = _open_process(
        log_dir,
        "browser",
        args,
        env=env,
        user=settings.TIKTOK_BROWSER_UID,
        group=settings.TIKTOK_BROWSER_GID,
    )
    return int(proc.pid), str(user_data_dir)


def _browser_ip_check_urls() -> list[str]:
    configured = os.getenv("PROXY_BROWSER_IP_CHECK_URLS", "").strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]
    return _unique_urls([
        "https://ifconfig.co/json",
        "https://ipinfo.io/json",
        "https://httpbin.org/ip",
        "https://api.ipify.org?format=json",
        "https://icanhazip.com",
        *_proxy_ip_check_urls(),
    ])


def _unique_urls(urls: list[str]) -> list[str]:
    unique: list[str] = []
    for url in urls:
        value = str(url or "").strip()
        if value and value not in unique:
            unique.append(value)
    return unique


def _proxy_ip_check_urls() -> list[str]:
    configured = os.getenv("PROXY_IP_CHECK_URLS", "").strip()
    if configured:
        return _unique_urls([item.strip() for item in configured.split(",")])
    primary = os.getenv("PROXY_IP_CHECK_URL", "https://ifconfig.co/json").strip()
    return _unique_urls([
        primary,
        "https://ipinfo.io/json",
        "http://ip-api.com/json/?fields=status,country,regionName,city,query",
        "https://api.ipify.org?format=json",
    ])


def _browser_ip_from_response(raw: str) -> str:
    value = raw.strip()
    try:
        body = json.loads(value)
    except json.JSONDecodeError:
        body = value
    candidates: list[str] = []
    if isinstance(body, dict):
        candidates.extend(str(body.get(key) or "") for key in ("query", "ip", "origin"))
    elif isinstance(body, str):
        candidates.append(body)
    for candidate in candidates:
        for item in candidate.replace(",", " ").split():
            try:
                return str(ipaddress.ip_address(item.strip()))
            except ValueError:
                continue
    return ""


def _browser_account_avatar(debug_port: int) -> bytes:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            if not browser.contexts or not browser.contexts[0].pages:
                raise ValueError("Chrome 没有可用的 TikTok 页面")
            page = browser.contexts[0].pages[0]
            try:
                page.wait_for_function(
                    """() => [...document.images].some((image) => {
                        const rect = image.getBoundingClientRect();
                        const src = image.currentSrc || image.src || "";
                        return src.startsWith("https://") && src.includes("-avt-") &&
                            !image.alt && rect.width >= 24 && rect.width <= 44 &&
                            rect.height >= 24 && rect.height <= 44;
                    })""",
                    timeout=5000,
                )
            except Exception:
                pass
            result = page.evaluate(
                """async () => {
                    const rows = [...document.images].map((image) => {
                        const rect = image.getBoundingClientRect();
                        return {
                            src: image.currentSrc || image.src || "",
                            alt: image.alt || "",
                            width: rect.width,
                            height: rect.height,
                        };
                    });
                    const candidates = rows.filter((row) =>
                        row.src.startsWith("https://") &&
                        row.src.includes("-avt-") &&
                        !row.alt &&
                        row.width >= 24 && row.width <= 44 &&
                        row.height >= 24 && row.height <= 44
                    );
                    const counts = new Map();
                    for (const candidate of candidates) {
                        counts.set(candidate.src, (counts.get(candidate.src) || 0) + 1);
                    }
                    candidates.sort((left, right) =>
                        (counts.get(right.src) - counts.get(left.src)) ||
                        (Math.abs(left.width - 32) - Math.abs(right.width - 32))
                    );
                    if (!candidates.length) return {error: "当前 TikTok 页面未找到账号头像"};
                    try {
                        const response = await fetch(candidates[0].src, {cache: "force-cache"});
                        if (!response.ok) return {error: `头像请求返回 HTTP ${response.status}`};
                        const bytes = new Uint8Array(await response.arrayBuffer());
                        let binary = "";
                        for (let offset = 0; offset < bytes.length; offset += 8192) {
                            binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
                        }
                        return {data: btoa(binary)};
                    } catch (error) {
                        return {error: String(error)};
                    }
                }"""
            )
        if not isinstance(result, dict) or not result.get("data"):
            raise ValueError(str((result or {}).get("error") or "TikTok 头像读取失败"))
        return base64.b64decode(str(result["data"]), validate=True)
    except Exception as exc:
        raise ValueError(f"TikTok 头像读取失败：{exc}") from exc


def _detect_browser_exit_ip(debug_port: int) -> str:
    failures: list[str] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            if not browser.contexts:
                raise ValueError("Chrome 没有可用的浏览器上下文")
            for target in _browser_ip_check_urls():
                page = None
                try:
                    page = browser.contexts[0].new_page()
                    response = page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    if response is not None and not response.ok:
                        raise ValueError(f"HTTP {response.status}")
                    observed_ip = _browser_ip_from_response(page.locator("body").inner_text(timeout=5000))
                    if not observed_ip:
                        raise ValueError("没有返回合法出口 IP")
                    return observed_ip
                except Exception as exc:
                    failures.append(f"{urlparse(target).netloc or target}: {exc}")
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
        raise ValueError("；".join(failures) or "没有可用的 IP 查询接口")
    except Exception as exc:
        raise ValueError(f"浏览器出口 IP 校验失败：{exc}") from exc


def _sing_box_reality_enabled() -> bool:
    return os.getenv("PROXY_REALITY_CORE", "mihomo").strip().lower() == "sing-box"


def _sing_box_reality_pool(row: sqlite3.Row | dict[str, Any]) -> bool:
    if not _sing_box_reality_enabled() or str(row["source_type"] or "") != "vless":
        return False
    if isinstance(row, dict):
        raw_parsed = row.get("parsed_json", row.get("parsed", {}))
    else:
        raw_parsed = row["parsed_json"]
    parsed = raw_parsed if isinstance(raw_parsed, dict) else _json_loads(str(raw_parsed or ""), {})
    query = parsed.get("query") if isinstance(parsed.get("query"), dict) else {}
    return (
        str(parsed.get("network") or "tcp") == "tcp"
        and bool(query.get("pbk"))
        and bool(parsed.get("uuid"))
        and bool(parsed.get("server"))
        and int(parsed.get("port") or 0) > 0
        and int(row["local_port"] or 0) > 0
    )


def sing_box_export() -> dict[str, Any]:
    rows = repository.list_sing_box_pools()
    inbounds: list[dict[str, Any]] = []
    outbounds: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    pools: list[dict[str, Any]] = []
    default_fingerprint = os.getenv("PROXY_REALITY_DEFAULT_FINGERPRINT", "safari").strip() or "safari"
    for row in rows:
        if not _sing_box_reality_pool(row):
            continue
        parsed = _json_loads(str(row["parsed_json"] or ""), {})
        query = parsed.get("query") if isinstance(parsed.get("query"), dict) else {}
        inbound_tag = f"reality-in-{int(row['id'])}"
        outbound_tag = f"reality-out-{int(row['id'])}"
        tls = {
            "enabled": True,
            "server_name": str(query.get("sni") or query.get("peer") or parsed.get("server") or ""),
            "utls": {
                "enabled": True,
                "fingerprint": str(query.get("fp") or default_fingerprint),
            },
            "reality": {
                "enabled": True,
                "public_key": str(query.get("pbk") or ""),
                "short_id": str(query.get("sid") or ""),
            },
        }
        outbound: dict[str, Any] = {
            "type": "vless",
            "tag": outbound_tag,
            "server": str(parsed.get("server") or ""),
            "server_port": int(parsed.get("port") or 0),
            "uuid": str(parsed.get("uuid") or ""),
            "network": "tcp",
            "tls": tls,
        }
        if query.get("flow"):
            outbound["flow"] = str(query["flow"])
        inbounds.append(
            {
                "type": "mixed",
                "tag": inbound_tag,
                "listen": "127.0.0.1",
                "listen_port": int(row["local_port"] or 0),
            }
        )
        outbounds.append(outbound)
        rules.append({"inbound": [inbound_tag], "action": "route", "outbound": outbound_tag})
        pools.append(
            {
                "id": int(row["id"]),
                "name": str(row["name"] or ""),
                "local_port": int(row["local_port"] or 0),
                "fingerprint": tls["utls"]["fingerprint"],
            }
        )
    outbounds.append({"type": "direct", "tag": "direct"})
    return {
        "config": {
            "log": {"level": "info", "timestamp": True},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": {"rules": rules, "final": "direct"},
        },
        "pools": pools,
        "generated_at": settings.now_iso(),
    }


def _write_sing_box_config() -> dict[str, Any]:
    exported = sing_box_export()
    settings.SING_BOX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.SING_BOX_CONFIG_PATH.parent, 0o700)
    temporary = settings.SING_BOX_CONFIG_PATH.with_suffix(settings.SING_BOX_CONFIG_PATH.suffix + ".tmp")
    temporary.write_text(
        json.dumps(exported["config"], ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, settings.SING_BOX_CONFIG_PATH)
    return exported


def _restart_sing_box_container(required: bool = False) -> dict[str, Any]:
    lookup = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={settings.SING_BOX_COMPOSE_PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={settings.SING_BOX_COMPOSE_SERVICE}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if lookup.returncode != 0:
        raise ValueError(f"查询 sing-box 容器失败：{lookup.stderr.strip() or lookup.stdout.strip()}")
    container_ids = [item.strip() for item in lookup.stdout.splitlines() if item.strip()]
    if not container_ids:
        if required:
            raise ValueError("sing-box 代理核心容器未启动")
        return {"restarted": False, "containers": []}
    restarted = subprocess.run(
        ["docker", "restart", *container_ids],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if restarted.returncode != 0:
        raise ValueError(f"重启 sing-box 代理核心失败：{restarted.stderr.strip() or restarted.stdout.strip()}")
    return {"restarted": True, "containers": container_ids}


def ensure_proxy_cores(restart: bool = False, required: bool = False) -> dict[str, Any]:
    if not _sing_box_reality_enabled():
        return {"sing_box": {"enabled": False}}
    exported = _write_sing_box_config()
    runtime = _restart_sing_box_container(required=required) if restart else {"restarted": False, "containers": []}
    return {
        "sing_box": {
            "enabled": True,
            "config_path": str(settings.SING_BOX_CONFIG_PATH),
            "pools": exported["pools"],
            **runtime,
        }
    }


def _mihomo_headers() -> dict[str, str]:
    secret = os.getenv("MIHOMO_SECRET", "").strip()
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def _mihomo_request(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 5.0) -> tuple[bool, Any, str]:
    parsed = urlparse(settings.DEFAULT_MIHOMO_API.rstrip("/"))
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 9090, timeout=timeout)
    headers = _mihomo_headers()
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        text = response.read(1024 * 1024).decode("utf-8", errors="replace")
        if not (200 <= response.status < 300):
            return False, None, f"HTTP {response.status}: {text[:300]}"
        return True, json.loads(text) if text else {}, ""
    except Exception as exc:
        return False, None, str(exc)
    finally:
        conn.close()


def _atomic_write(path: Path, body: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(body)
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _reload_mihomo_config() -> None:
    reload_path = os.getenv("MIHOMO_RELOAD_PATH", "/etc/mihomo/config.yaml").strip() or "/etc/mihomo/config.yaml"
    ok, _body, error = _mihomo_request("PUT", "/configs?force=true", {"path": reload_path}, timeout=15)
    if not ok:
        raise ValueError(f"mihomo 配置重载失败：{error}")


def _pool_value(pool: sqlite3.Row | dict[str, Any], key: str, default: Any = "") -> Any:
    try:
        value = pool[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _mihomo_config_path() -> Path:
    configured = os.getenv("MIHOMO_CONFIG_PATH", "").strip()
    if not configured:
        raise ProxyConfigurationError("mihomo 配置未挂载，无法自动同步代理节点")
    path = Path(configured)
    if not path.is_file():
        raise ProxyConfigurationError(f"mihomo 配置文件不存在：{path}")
    return path


def _yaml_section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    start = next((index for index, line in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(?:#.*)?$", line.rstrip("\r\n"))), -1)
    if start < 0:
        return -1, -1
    end = next(
        (index for index in range(start + 1, len(lines)) if re.match(r"^[A-Za-z0-9_-]+:\s*", lines[index])),
        len(lines),
    )
    return start, end


def _yaml_list_item_indent(lines: list[str], start: int, end: int) -> int:
    indents = [
        len(match.group(1))
        for index in range(start + 1, end)
        if (match := re.match(r"^(\s*)-\s*", lines[index]))
    ]
    return min(indents) if indents else 2


def _yaml_list_item_ranges(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    item_indent = _yaml_list_item_indent(lines, start, end)
    starts = [
        index
        for index in range(start + 1, end)
        if (match := re.match(r"^(\s*)-\s*", lines[index])) and len(match.group(1)) == item_indent
    ]
    return [
        (item_start, starts[position + 1] if position + 1 < len(starts) else end)
        for position, item_start in enumerate(starts)
    ]


def _yaml_block_field_matches(block: list[str], key: str, value: Any) -> bool:
    expected = f"{key}: {_yaml_scalar(value)}"
    for line in block:
        candidate = line.strip()
        if candidate.startswith("- "):
            candidate = candidate[2:].strip()
        if candidate == expected:
            return True
    return False


def _managed_proxy_markers(pool_id: int) -> tuple[str, ...]:
    marker = f"proxy-pool-managed-{settings.PROXY_CONFIG_NAMESPACE}-id: {pool_id}"
    if settings.PROXY_CONFIG_NAMESPACE == "formal":
        return marker, f"proxy-pool-managed-id: {pool_id}"
    return (marker,)


def _managed_yaml_item(value: dict[str, Any], pool_id: int, item_indent: int) -> list[str]:
    value_indent = item_indent + 2
    lines = _yaml_lines(value, value_indent)
    if not lines:
        return []
    return [
        f"{' ' * item_indent}- {lines[0].lstrip()}\n",
        *(f"{line}\n" for line in lines[1:]),
        f"{' ' * value_indent}# {_managed_proxy_markers(pool_id)[0]}\n",
    ]


def _replace_mihomo_yaml_item(
    lines: list[str],
    section: str,
    pool_id: int,
    value: dict[str, Any] | None,
    *,
    match_name: str = "",
    match_port: int = 0,
) -> list[str]:
    start, end = _yaml_section_bounds(lines, section)
    if start < 0:
        block = _managed_yaml_item(value, pool_id, 2) if value is not None else []
        if not block:
            return lines
        preferred_anchor = "proxy-groups" if section == "proxies" else "rules"
        insert_at = next(
            (index for index, line in enumerate(lines) if re.match(rf"^{preferred_anchor}:\s*", line)),
            len(lines),
        )
        return [*lines[:insert_at], f"{section}:\n", *block, *lines[insert_at:]]

    item_indent = _yaml_list_item_indent(lines, start, end)
    block = _managed_yaml_item(value, pool_id, item_indent) if value is not None else []
    markers = _managed_proxy_markers(pool_id)
    matched_ranges: list[tuple[int, int]] = []
    for left, right in _yaml_list_item_ranges(lines, start, end):
        item = lines[left:right]
        if any(marker in line for marker in markers for line in item):
            matched_ranges.append((left, right))
            continue
        if (
            settings.PROXY_CONFIG_NAMESPACE == "formal"
            and match_name
            and _yaml_block_field_matches(item, "name", match_name)
        ):
            matched_ranges.append((left, right))
            continue
        if settings.PROXY_CONFIG_NAMESPACE == "formal" and match_port and _listener_port(item) == match_port:
            matched_ranges.append((left, right))

    remove_indexes = {index for left, right in matched_ranges for index in range(left, right)}
    insert_at = matched_ranges[0][0] if matched_ranges else end
    updated: list[str] = []
    for index in range(len(lines) + 1):
        if index == insert_at and block:
            updated.extend(block)
        if index < len(lines) and index not in remove_indexes:
            updated.append(lines[index])
    return updated


def _restore_mihomo_config(path: Path, original: bytes, mode: int) -> None:
    _atomic_write(path, original, mode)
    try:
        _reload_mihomo_config()
    except Exception:
        pass


def _runtime_mihomo_name(pool: sqlite3.Row | dict[str, Any]) -> str:
    if str(_pool_value(pool, "source_type") or "") == "direct":
        return settings.SYSTEM_PROXY_DIALER
    node_name = str(_pool_value(pool, "mihomo_name") or _pool_value(pool, "name") or "").strip()
    return f"{settings.PROXY_MIHOMO_NAME_PREFIX}{node_name}" if node_name else ""


def _runtime_mihomo_listener_name(pool: sqlite3.Row | dict[str, Any]) -> str:
    pool_name = str(_pool_value(pool, "name") or "").strip()
    pool_id = int(_pool_value(pool, "id", 0) or 0)
    namespace = "" if settings.PROXY_CONFIG_NAMESPACE == "formal" else f"{settings.PROXY_CONFIG_NAMESPACE}-"
    if pool_id:
        return f"tiktok-{namespace}{pool_id}-{pool_name}"
    return f"tiktok-{namespace}{pool_name}"


def _resolve_system_proxy_dialer(node_name: str) -> str:
    current = settings.SYSTEM_PROXY_DIALER
    visited: set[str] = set()
    for _attempt in range(8):
        if current == node_name:
            raise ProxyConfigurationError("系统代理当前指向该静态代理，无法建立代理链")
        if current.upper() in {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}:
            raise ProxyConfigurationError(f"系统代理当前未选择可用节点：{current}")
        if current in visited:
            raise ProxyConfigurationError("系统代理策略组存在循环引用")
        visited.add(current)
        ok, body, error = _mihomo_request("GET", f"/proxies/{quote_path(current)}", timeout=8)
        if not ok or not isinstance(body, dict):
            raise ProxyConfigurationError(f"无法读取系统代理当前节点 {current}：{error}")
        selected = str(body.get("now") or "").strip()
        if not selected or selected == current:
            return current
        current = selected
    raise ProxyConfigurationError("系统代理策略组嵌套过深")


def _sync_mihomo_pool_config(pool: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    source_type = str(_pool_value(pool, "source_type") or "")
    direct = source_type == "direct"
    proxy = _json_loads(_pool_value(pool, "mihomo_proxy_json", ""), {})
    if not proxy and isinstance(_pool_value(pool, "mihomo_proxy", {}), dict):
        proxy = dict(_pool_value(pool, "mihomo_proxy", {}))
    node_name = _runtime_mihomo_name(pool)
    local_port = int(_pool_value(pool, "local_port", 0) or 0)
    pool_id = int(_pool_value(pool, "id", 0) or 0)
    if not node_name or not local_port or not pool_id or (not direct and not proxy):
        raise ProxyConfigurationError("代理缺少可同步的 mihomo 节点、端口或记录 ID")
    if source_type == "static":
        proxy["dialer-proxy"] = _resolve_system_proxy_dialer(node_name)
    elif not direct:
        proxy.pop("dialer-proxy", None)
    if not direct:
        proxy["name"] = node_name
    listener = {
        "name": _runtime_mihomo_listener_name(pool),
        "type": "mixed",
        "port": local_port,
        "proxy": node_name,
    }


    # Mihomo does not reliably replace a listener when its name changes while
    # retaining the same port. Release a stale managed listener first, then add
    # the current one in a second reload.
    listener_restore: tuple[Path, bytes, int] | None = None
    if not _mihomo_listener_matches(pool):
        _cleanup, listener_restore = _remove_mihomo_listener_config(local_port)

    path = _mihomo_config_path()
    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    lines = original.decode("utf-8").splitlines(keepends=True)
    lines = _replace_mihomo_yaml_item(
        lines,
        "proxies",
        pool_id,
        None if direct else proxy,
        match_name="" if direct else node_name,
    )
    lines = _replace_mihomo_yaml_item(lines, "listeners", pool_id, listener, match_port=local_port)
    updated = "".join(lines).encode("utf-8")
    backup_path = path.with_name(path.name + ".proxy-pool.bak")
    _atomic_write(backup_path, original, mode)
    _atomic_write(path, updated, mode)
    try:
        _reload_mihomo_config()
        ok, body, error = _mihomo_request("GET", "/proxies", timeout=8)
        proxies = body.get("proxies") if ok and isinstance(body, dict) and isinstance(body.get("proxies"), dict) else {}
        if node_name not in proxies:
            raise ProxyConfigurationError(f"mihomo 重载后仍未发现节点 {node_name}：{error}")
        # mihomo may acknowledge a reload before its new listeners finish binding.
        for _attempt in range(100):
            if _port_open("127.0.0.1", local_port, timeout=0.2):
                break
            time.sleep(0.1)
        else:
            raise ProxyConfigurationError(f"mihomo 重载后端口 {local_port} 未监听")
    except Exception as exc:
        _restore_mihomo_config(path, original, mode)
        if listener_restore is not None:
            _restore_mihomo_listener_config(*listener_restore)
        if isinstance(exc, ProxyConfigurationError):
            raise
        raise ProxyConfigurationError(f"mihomo 自动同步失败：{exc}") from exc
    return {
        "configured": True,
        "node": node_name,
        "port": local_port,
        "dialer_proxy": str(proxy.get("dialer-proxy") or ""),
        "backup_path": str(backup_path),
    }


def ensure_static_proxy_configs() -> dict[str, Any]:
    pools = repository.list_static_runtime_pools()
    synced: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for pool in pools:
        try:
            synced.append(_sync_mihomo_pool_config(pool))
        except Exception as exc:
            errors.append({"id": int(pool["id"]), "name": str(pool["name"]), "error": str(exc)})
    return {"synced": synced, "errors": errors}


def reconcile_mihomo_pool_configs() -> dict[str, Any]:
    """Rebuild every managed Mihomo listener from the persisted pool binding.

    A listener can remain open while still pointing at a previous node.  In that
    case a reachability check alone cannot tell Mihomo to replace the mapping.
    """
    pools = repository.list_reconcilable_mihomo_pools()
    synced: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for pool in pools:
        pool_id = int(pool["id"])
        if _sing_box_reality_pool(pool):
            skipped.append({"id": pool_id, "name": str(pool["name"]), "reason": "sing-box"})
            continue
        try:
            synced.append(_sync_mihomo_pool_config(pool))
        except Exception as exc:
            errors.append({"id": pool_id, "name": str(pool["name"]), "error": str(exc)})
    return {"synced": synced, "skipped": skipped, "errors": errors}


def _remove_mihomo_pool_config(
    pool: sqlite3.Row | dict[str, Any],
) -> tuple[dict[str, Any], tuple[Path, bytes, int] | None]:
    path = _mihomo_config_path()
    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    pool_id = int(_pool_value(pool, "id", 0) or 0)
    node_name = _runtime_mihomo_name(pool)
    local_port = int(_pool_value(pool, "local_port", 0) or 0)
    lines = original.decode("utf-8").splitlines(keepends=True)
    if str(_pool_value(pool, "source_type") or "") != "direct":
        lines = _replace_mihomo_yaml_item(lines, "proxies", pool_id, None, match_name=node_name)
    lines = _replace_mihomo_yaml_item(lines, "listeners", pool_id, None, match_port=local_port)
    updated = "".join(lines).encode("utf-8")
    if updated == original:
        return {"configured": True, "removed": False, "node": node_name, "port": local_port}, None
    backup_path = path.with_name(path.name + ".proxy-pool.bak")
    _atomic_write(backup_path, original, mode)
    _atomic_write(path, updated, mode)
    try:
        _reload_mihomo_config()
    except Exception as exc:
        _restore_mihomo_config(path, original, mode)
        raise ProxyConfigurationError(f"mihomo 自动清理失败：{exc}") from exc
    return {
        "configured": True,
        "removed": True,
        "node": node_name,
        "port": local_port,
        "backup_path": str(backup_path),
    }, (path, original, mode)


def _restore_mihomo_listener_config(path: Path, original: bytes, mode: int) -> None:
    _atomic_write(path, original, mode)
    _reload_mihomo_config()


def _listener_port(block: list[str]) -> int:
    for line in block:
        match = re.match(r"^\s+port:\s*['\"]?(\d+)", line)
        if match:
            return int(match.group(1))
    return 0


def _mihomo_listener_matches(pool: sqlite3.Row | dict[str, Any]) -> bool:
    """Whether the persisted pool is the current owner of its local port."""
    local_port = int(_pool_value(pool, "local_port", 0) or 0)
    node_name = _runtime_mihomo_name(pool)
    listener_name = _runtime_mihomo_listener_name(pool)
    config_value = os.getenv("MIHOMO_CONFIG_PATH", "").strip()
    if not local_port or not node_name or not listener_name or not config_value:
        return True
    path = Path(config_value)
    if not path.is_file():
        return True
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        start, end = _yaml_section_bounds(lines, "listeners")
        if start < 0:
            return False
        for left, right in _yaml_list_item_ranges(lines, start, end):
            block = lines[left:right]
            if _listener_port(block) != local_port:
                continue
            return (
                _yaml_block_field_matches(block, "name", listener_name)
                and _yaml_block_field_matches(block, "proxy", node_name)
            )
    except OSError:
        return True
    return False


def _remove_mihomo_listener_config(
    required_port: int,
) -> tuple[dict[str, Any], tuple[Path, bytes, int] | None]:
    managed_ports = set(range(settings.PROXY_PORT_START, settings.PROXY_PORT_END + 1))
    if required_port not in managed_ports:
        return {"configured": False, "removed_ports": []}, None
    config_value = os.getenv("MIHOMO_CONFIG_PATH", "").strip()
    if not config_value:
        if _port_open("127.0.0.1", required_port, timeout=0.5):
            raise ValueError("mihomo 配置未挂载，无法同步删除正在监听的代理端口")
        return {"configured": False, "removed_ports": []}, None
    path = Path(config_value)
    if not path.is_file():
        raise ValueError(f"mihomo 配置文件不存在：{path}")

    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    lines = original.decode("utf-8").splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.startswith("listeners:")), -1)
    if start < 0:
        if _port_open("127.0.0.1", required_port, timeout=0.5):
            raise ValueError(f"mihomo 仍监听 {required_port}，但配置中找不到 listeners 段")
        return {"configured": True, "removed_ports": []}, None
    end = next(
        (index for index in range(start + 1, len(lines)) if re.match(r"^[A-Za-z0-9_-]+:", lines[index])),
        len(lines),
    )
    item_starts = [index for index in range(start + 1, end) if lines[index].startswith("-")]
    remove_ranges: list[tuple[int, int]] = []
    removed_ports: list[int] = []
    for position, item_start in enumerate(item_starts):
        item_end = item_starts[position + 1] if position + 1 < len(item_starts) else end
        port = _listener_port(lines[item_start:item_end])
        if port == required_port:
            remove_ranges.append((item_start, item_end))
            removed_ports.append(port)
    if not remove_ranges:
        if _port_open("127.0.0.1", required_port, timeout=0.5):
            raise ValueError(f"mihomo 仍监听 {required_port}，但没有找到可安全删除的配置块")
        return {"configured": True, "removed_ports": []}, None

    remove_indexes = {index for left, right in remove_ranges for index in range(left, right)}
    updated = "".join(line for index, line in enumerate(lines) if index not in remove_indexes).encode("utf-8")
    _atomic_write(path, updated, mode)
    try:
        _reload_mihomo_config()
    except Exception:
        _atomic_write(path, original, mode)
        try:
            _reload_mihomo_config()
        except Exception:
            pass
        raise
    return {"configured": True, "removed_ports": sorted(removed_ports)}, (path, original, mode)


def _switch_mihomo_node(node_name: str) -> dict[str, Any]:
    ok, body, error = _mihomo_request("GET", "/proxies")
    if not ok or not isinstance(body, dict):
        raise ProxyConfigurationError(f"无法读取服务器 mihomo 节点：{error}")
    proxies = body.get("proxies") if isinstance(body.get("proxies"), dict) else {}
    if node_name not in proxies:
        raise ProxyConfigurationError(f"节点 {node_name} 没有加载到服务器 mihomo")
    preferred = ["GLOBAL", "Proxy", "代理", "CoffeeCloud", "自动选择"]
    candidates = []
    for name, item in proxies.items():
        all_nodes = item.get("all") if isinstance(item, dict) else None
        if isinstance(all_nodes, list) and node_name in all_nodes:
            candidates.append(str(name))
    candidates.sort(key=lambda item: (0 if item in preferred else 1, preferred.index(item) if item in preferred else 999, item))
    switched = []
    for group in candidates[:3]:
        ok, _body, error = _mihomo_request("PUT", f"/proxies/{quote_path(group)}", {"name": node_name})
        if ok:
            switched.append(group)
    if candidates and not switched:
        raise ProxyConfigurationError(f"mihomo 找到节点 {node_name}，但切换策略组失败")
    return {"node": node_name, "groups": switched, "loaded": True}


def quote_path(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _proxy_get_json(url: str, proxy_port: int, timeout: float = 10.0) -> tuple[bool, Any, str]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    request = Request(url, headers={"User-Agent": "ShortVideoAnalyzer/1.0"})
    last_error = ""
    for _attempt in range(settings.PROXY_REQUEST_ATTEMPTS):
        try:
            with opener.open(request, timeout=timeout) as response:
                text = response.read(1024 * 1024).decode("utf-8", errors="replace")
            return True, json.loads(text), ""
        except HTTPError as exc:
            text = exc.read(300).decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {text}"
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
    return False, None, last_error


def _proxy_url_reachable(url: str, proxy_port: int, timeout: float = 10.0) -> tuple[bool, str]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": settings.TIKTOK_BROWSER_ACCEPT_LANGUAGE,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
        },
    )
    last_error = ""
    for attempt in range(settings.PROXY_REACHABILITY_ATTEMPTS):
        try:
            with opener.open(request, timeout=timeout) as response:
                response.read(1)
                return True, f"HTTP {response.status}"
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                return True, f"HTTP {exc.code}"
            last_error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        if attempt + 1 < settings.PROXY_REACHABILITY_ATTEMPTS:
            time.sleep(min(2.0, 0.5 * (attempt + 1)))
    return False, last_error


def _proxy_runtime_core(pool: sqlite3.Row) -> str:
    return "sing-box" if _sing_box_reality_pool(pool) else "mihomo"


def _repair_proxy_core_once(pool: sqlite3.Row) -> dict[str, Any]:
    core = _proxy_runtime_core(pool)
    try:
        if core == "sing-box":
            result = ensure_proxy_cores(restart=True, required=True)
            local_port = int(pool["local_port"] or 0)
            deadline = time.monotonic() + 10
            while local_port and time.monotonic() < deadline:
                if _port_open("127.0.0.1", local_port, timeout=0.2):
                    break
                time.sleep(0.2)
            else:
                raise ProxyConfigurationError(f"sing-box 重启后本地端口 {local_port} 未监听")
        else:
            result = _sync_mihomo_pool_config(pool)
    except Exception as exc:
        raise ProxyConfigurationError(f"{core} 自动修复失败：{exc}") from exc
    return {"attempted": True, "core": core, "result": result}


def _detect_exit_ip_with_single_repair(pool: sqlite3.Row) -> dict[str, Any]:
    try:
        return detect_exit_ip_for_pool(pool)
    except Exception as first_error:
        repair = _repair_proxy_core_once(pool)
        try:
            detected = detect_exit_ip_for_pool(pool)
        except Exception as retry_error:
            raise ProxyConfigurationError(
                f"{repair['core']} 已自动修复一次，但出口校验仍失败：{retry_error}"
            ) from retry_error
        detected["auto_repair"] = repair
        return detected


def detect_exit_ip_for_pool(pool: sqlite3.Row) -> dict[str, Any]:
    node_name = _runtime_mihomo_name(pool)
    if not node_name:
        raise ValueError("代理没有 mihomo 节点名")
    runtime_core = _proxy_runtime_core(pool)
    local_port = int(pool["local_port"] or 0)
    if str(pool["source_type"] or "") == "static":
        _sync_mihomo_pool_config(pool)
    elif (
        local_port
        and (
            not _port_open("127.0.0.1", local_port, timeout=1.0)
            or not _mihomo_listener_matches(pool)
        )
        and not _sing_box_reality_pool(pool)
    ):
        _sync_mihomo_pool_config(pool)
    if local_port and _port_open("127.0.0.1", local_port, timeout=1.0):
        proxy_port = local_port
        switch = {"node": node_name, "groups": [], "loaded": True, "listener_port": local_port}
    elif runtime_core == "sing-box":
        raise ProxyConfigurationError(f"sing-box 本地端口 {local_port} 未监听")
    else:
        switch = _switch_mihomo_node(node_name)
        proxy_port = int(os.getenv("MIHOMO_PROXY_PORT", "7890") or "7890")
    failures: list[str] = []
    detected: dict[str, Any] | None = None
    for target in _proxy_ip_check_urls():
        ok, body, error = _proxy_get_json(target, proxy_port)
        if not ok or not isinstance(body, dict):
            failures.append(f"{urlparse(target).netloc or target}: {error or '返回格式异常'}")
            continue
        ip = _browser_ip_from_response(json.dumps(body, ensure_ascii=False))
        if not ip:
            failures.append(f"{urlparse(target).netloc or target}: 没有返回合法出口 IP")
            continue
        country = str(body.get("country") or "")
        region = str(body.get("regionName") or body.get("region") or "")
        city = str(body.get("city") or "")
        address = " / ".join(item for item in (country, region, city) if item)
        detected = {
            "ip": ip,
            "geo": {"country": country, "region": region, "city": city, "address": address},
            "runtime_core": runtime_core,
            "mihomo": switch,
            "raw": body,
            "check_url": target,
            "fallback_failures": failures,
        }
        break
    if detected is None:
        raise ValueError(f"通过服务器 mihomo 查询出口 IP 失败：{'；'.join(failures) or '没有可用的 IP 查询接口'}")
    tiktok_url = os.getenv("PROXY_TIKTOK_CHECK_URL", "https://www.tiktok.com/").strip()
    if tiktok_url:
        reachable, reachability = _proxy_url_reachable(tiktok_url, proxy_port)
        if not reachable:
            raise ValueError(
                f"TikTok 连通性校验失败（出口 IP {detected['ip']}）："
                f"{urlparse(tiktok_url).netloc or tiktok_url}: {reachability}"
            )
        detected["tiktok_check"] = {"url": tiktok_url, "result": reachability}
    return detected


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or any(ch in text for ch in ":#{}[],-&*?!|>'\"%@`") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    space = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{space}{key}:")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{space}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{space}-")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{space}- {_yaml_scalar(item)}")
        return lines
    return [f"{space}{_yaml_scalar(value)}"]


def mihomo_export() -> dict[str, Any]:
    rows = repository.list_mihomo_export_pools()
    proxies = []
    skipped = []
    for row in rows:
        proxy = _json_loads(row["mihomo_proxy_json"], {})
        if proxy:
            proxy["name"] = _runtime_mihomo_name(row)
            proxies.append(proxy)
        else:
            skipped.append({"id": row["id"], "name": row["name"], "reason": row["parse_error"] or "no parsed mihomo proxy"})
    yaml = "proxies:\n"
    for proxy in proxies:
        lines = _yaml_lines(proxy, 4)
        if lines:
            first = lines[0].lstrip()
            yaml += f"  - {first}\n"
            yaml += "\n".join(lines[1:]) + ("\n" if len(lines) > 1 else "")
    listeners = [
        {
            "name": _runtime_mihomo_listener_name(row),
            "type": "mixed",
            "port": int(row["local_port"] or 0),
            "proxy": _runtime_mihomo_name(row),
        }
        for row in rows
        if int(row["local_port"] or 0) and not _sing_box_reality_pool(row)
    ]
    if listeners:
        yaml += "listeners:\n"
        for listener in listeners:
            lines = _yaml_lines(listener, 4)
            first = lines[0].lstrip()
            yaml += f"  - {first}\n"
            yaml += "\n".join(lines[1:]) + ("\n" if len(lines) > 1 else "")
    return {"proxies": proxies, "listeners": listeners, "skipped": skipped, "yaml": yaml, "port_range": f"{settings.PROXY_PORT_START}-{settings.PROXY_PORT_END}", "generated_at": settings.now_iso()}


def lookup_ip_geo(ip: str) -> dict[str, str]:
    ok, body, _error = _http_get_json(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,query", timeout=4)
    if not ok or not isinstance(body, dict) or body.get("status") != "success":
        return {"country": "", "region": "", "city": "", "address": ""}
    country = str(body.get("country") or "")
    region = str(body.get("regionName") or "")
    city = str(body.get("city") or "")
    address = " / ".join(item for item in (country, region, city) if item)
    return {"country": country, "region": region, "city": city, "address": address}


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _http_get_json(url: str, timeout: float = 3.0) -> tuple[bool, Any, str]:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=timeout)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    headers = {}
    secret = os.getenv("MIHOMO_SECRET", "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read(8192).decode("utf-8", errors="replace")
        if not (200 <= response.status < 300):
            return False, None, f"HTTP {response.status}: {body[:200]}"
        return True, json.loads(body), ""
    except Exception as exc:
        return False, None, str(exc)
    finally:
        conn.close()


def runtime_status() -> dict[str, Any]:
    mihomo_api = settings.DEFAULT_MIHOMO_API.rstrip("/")
    mihomo_ok, mihomo_body, mihomo_error = _http_get_json(mihomo_api + "/version")
    novnc_ports = settings.novnc_port_plan()
    novnc_checks = {str(port): _port_open("127.0.0.1", port) for port in novnc_ports["allowed_ports"]}
    return {
        "checked_at": settings.now_iso(),
        "novnc_url": settings.DEFAULT_NOVNC_PUBLIC_URL,
        "novnc_local_port": settings.NOVNC_PORT,
        "novnc_ports": novnc_ports,
        "vnc_port": int(os.getenv("VNC_PORT", "5900")),
        "mihomo_proxy_port": int(os.getenv("MIHOMO_PROXY_PORT", "7890")),
        "mihomo_api_url": mihomo_api,
        "checks": {
            "novnc_local": novnc_checks.get(str(settings.NOVNC_PORT), False),
            "novnc_ports": novnc_checks,
            "vnc_local": _port_open("127.0.0.1", int(os.getenv("VNC_PORT", "5900"))),
            "mihomo_proxy_local": _port_open("127.0.0.1", int(os.getenv("MIHOMO_PROXY_PORT", "7890"))),
            "mihomo_api_local": mihomo_ok,
        },
        "mihomo_version": (mihomo_body or {}).get("version") if isinstance(mihomo_body, dict) else "",
        "mihomo_error": mihomo_error,
        "port_range": f"{settings.PROXY_PORT_START}-{settings.PROXY_PORT_END}",
        "pending_login_ttl_seconds": settings.pending_login_ttl_seconds(),
        "manual_observation_idle_seconds": settings.manual_observation_idle_seconds(),
        "browser_locale": settings.TIKTOK_BROWSER_LOCALE,
        "browser_notice": (
            f"noVNC 放行端口按可见观测 {novnc_ports['visible_observation_slots']} + 手动 "
            f"{novnc_ports['manual_ports']} 计算：{novnc_ports['allowed_range']}；"
            f"另有 {novnc_ports['hidden_automation_slots']} 个后台自动槽位，服务器本机检测为准。"
        ),
    }
