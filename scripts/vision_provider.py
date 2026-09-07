"""Persistent, request-driven Qwen availability and frame-provider selection."""
import base64
import hashlib
import io
import json
import os
import sqlite3
import time
from pathlib import Path

import requests

CHECK_INTERVAL = 24 * 60 * 60
STATE_PATH = Path.cwd() / "data" / "qwen_status.sqlite"
DEFAULT_QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen3-vl-flash"
DEEPSEEK_VISION_MODEL = "deepseek-v4-flash-vision-exp"


def qwen_config():
    return {
        "api_url": os.getenv("VISION_API_URL", DEFAULT_QWEN_URL).rstrip("/"),
        "api_key": os.getenv("VISION_API_KEY", ""),
        "model": os.getenv("VISION_MODEL", DEFAULT_QWEN_MODEL),
        "direct_model": os.getenv("DIRECT_VIDEO_MODEL", DEFAULT_QWEN_MODEL),
    }


def _fingerprint():
    return hashlib.sha256(json.dumps(qwen_config(), sort_keys=True).encode()).hexdigest()


def completion_url(url):
    url = url.rstrip("/")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def _connect():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_PATH, timeout=90)
    conn.execute("CREATE TABLE IF NOT EXISTS qwen_status (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
    return conn


def _save(conn, available, code, reason):
    now = time.time()
    state = {"state": "available" if available else "unavailable", "checked_at": now,
             "next_check_at": now + CHECK_INTERVAL, "code": code, "reason": reason,
             "config_fingerprint": _fingerprint()}
    conn.execute("INSERT OR REPLACE INTO qwen_status VALUES (1, ?)", (json.dumps(state),))
    return state


def response_failure(response):
    """Return sanitized diagnostics; never persist raw upstream bodies or credentials."""
    try:
        error = response.json().get("error") or {}
        code = str(error.get("code") or error.get("type") or "") if isinstance(error, dict) else ""
    except (ValueError, AttributeError):
        code = ""
    if code.lower() in {"arrearage", "insufficient_quota", "insufficient_balance"}:
        return "Arrearage", "Qwen 账户欠费或额度不足"
    status = response.status_code
    if status in (401, 403):
        return "AuthenticationFailed", "Qwen 鉴权失败或账户无访问权限"
    if status == 429:
        return "RateLimited", "Qwen 请求受限"
    return "HTTP_" + str(status), "Qwen 接口返回 HTTP " + str(status)


def _probe():
    config = qwen_config()
    if not config["api_key"]:
        return False, "MissingApiKey", "未配置 Qwen API Key"
    # A tiny in-memory image verifies vision access, not just text completion access.
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    image = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    for model in dict.fromkeys([config["model"], config["direct_model"]]):
        try:
            response = requests.post(completion_url(config["api_url"]),
                headers={"Authorization": "Bearer " + config["api_key"]},
                json={"model": model, "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image}},
                    {"type": "text", "text": "Name the image color in one word."}]}], "max_tokens": 8},
                timeout=(5, 15))
            if not response.ok:
                return False, *response_failure(response)
            data = response.json()
            if data.get("error") or not data.get("choices") or not data["choices"][0].get("message", {}).get("content"):
                return False, "InvalidResponse", "Qwen 未返回有效的模型响应"
        except requests.RequestException:
            return False, "ConnectionFailed", "Qwen 连接失败或请求超时"
        except (ValueError, AttributeError, TypeError, KeyError, IndexError):
            return False, "InvalidResponse", "Qwen 返回格式异常"
    return True, "", ""


def get_status():
    # A database transaction serializes checks across web threads AND CLI processes.
    conn = _connect()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM qwen_status WHERE id = 1").fetchone()
            try:
                state = json.loads(row[0]) if row else {}
                fresh = (state.get("state") in {"available", "unavailable"}
                         and state.get("config_fingerprint") == _fingerprint()
                         and float(state.get("next_check_at", 0)) > time.time())
            except (ValueError, TypeError, AttributeError):
                fresh = False
            if not fresh:
                state = _save(conn, *_probe())
    finally:
        conn.close()
    return _public_status(state)


def _public_status(state):
    available = state["state"] == "available"
    return {**{k: v for k, v in state.items() if k != "config_fingerprint"},
            "frame_provider": "qwen" if available else "deepseek",
            "frame_model": qwen_config()["model"] if available else DEEPSEEK_VISION_MODEL,
            "frame_analysis_enabled": available or bool(os.getenv("DEEPSEEK_API_KEY")),
            "direct_video_enabled": available,
            "message": "当前使用 Qwen 帧分析，视频直连分析可用" if available else
                state["reason"] + "；已切换 DeepSeek Vision 帧分析，视频直连分析已禁用" +
                ("" if os.getenv("DEEPSEEK_API_KEY") else "；备用线路缺少 DeepSeek API Key，帧分析暂不可用")}


def mark_unavailable(code, reason):
    conn = _connect()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            state = _save(conn, False, code, reason)
    finally:
        conn.close()
    return _public_status(state)


def observe_response(response):
    code, reason = response_failure(response)
    if code == "Arrearage" or response.status_code in (401, 403, 429) or response.status_code >= 500:
        return mark_unavailable(code, reason)
    return None  # A bad video/image or malformed request is not an account outage.


def require_direct_video():
    status = get_status()
    if not status["direct_video_enabled"]:
        raise RuntimeError(status["message"])
    return status


def frame_config():
    status = get_status()
    if status["frame_provider"] == "qwen":
        return {**qwen_config(), "provider": "qwen"}
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError(status["message"])
    return {"api_key": key, "api_url": os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"),
            "model": DEEPSEEK_VISION_MODEL, "provider": "deepseek"}
