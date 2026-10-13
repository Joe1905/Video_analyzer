"""DuoPlus OpenAPI and UI gateway bridge for the development control page."""
import json
import os
import threading
import time
import uuid

import requests
import virphone

LOCK = threading.Lock()
LAST_CALL = {}
ACTIONS = {
    "OPEN_APP", "CLICK_ELEMENT", "LONG_ELEMENT", "CLICK_COORDINATE",
    "LONG_COORDINATE", "INPUT_CONTENT", "KEYBOARD_OPERATION", "SLIDE_PAGE",
    "GO_TO_HOME", "PAGE_BACK", "WAIT_TIME", "WAIT_FOR_SELECTOR",
    "GET_SINGLE_ELEMENT_TEXT",
}


def load_config(data_dir):
    path = data_dir / "duoplus.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"api_key": config.get("api_key") or os.getenv("DUOPLUS_API_KEY", ""),
            "image_id": config.get("image_id", ""), "region": config.get("region", "cn")}


def public_config(config):
    return {"configured": bool(config["api_key"]), "image_id": config["image_id"],
            "region": config["region"]}


def request(config, path, payload, phone=None):
    if not config["api_key"]:
        raise ValueError("请先保存 DuoPlus API Key。")
    headers = {"Content-Type": "application/json", "Lang": "zh"}
    if phone is None:
        host = "openapi.duoplus.cn" if config["region"] == "cn" else "openapi.duoplus.net"
        headers["DuoPlus-API-Key"] = config["api_key"]
    else:
        host = "agent-gateway.duoplus.cn"
        headers.update({"Authorization": "Bearer " + config["api_key"],
                        "Region": phone["region"], "CloudIP": phone["ip"]})
    # Serialize platform calls at <= 1 QPS. UI calls are also kept sequential.
    delay = 1.05 - (time.monotonic() - LAST_CALL.get(path, 0))
    if delay > 0:
        time.sleep(delay)
    LAST_CALL[path] = time.monotonic()
    with requests.Session() as session:
        session.trust_env = False
        response = session.post("https://" + host + path, headers=headers,
                                json=payload, timeout=(10, 310 if phone else 30))
    if response.status_code == 401:
        raise ValueError("DuoPlus 鉴权失败，请检查 API Key 和设备 HTTP 服务配置。")
    if not response.ok:
        raise ValueError("DuoPlus 请求失败：HTTP " + str(response.status_code))
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("DuoPlus 返回了无效数据。")
    if phone is None and result.get("code") != 200:
        raise ValueError("DuoPlus 平台请求失败，请检查设备及 API 权限。")
    return result


def list_phones(config, image_id=""):
    rows = []
    page = 1
    while True:
        payload = {"page": page, "pagesize": 100}
        if image_id:
            payload["image_id"] = [image_id]
        data = request(config, "/api/v1/cloudPhone/list", payload)["data"]
        rows.extend(data.get("list", []))
        if page >= int(data.get("total_page", 1)):
            return rows
        page += 1


def get_phone(config, image_id):
    if not isinstance(image_id, str) or not image_id.strip():
        raise ValueError("请先选择云手机。")
    phone = next((row for row in list_phones(config, image_id) if row.get("id") == image_id), None)
    if not phone:
        raise ValueError("未找到指定云手机。")
    if str(phone.get("status")) != "1" or str(phone.get("http_status")) != "1":
        raise ValueError("云手机需已开机并开启 HTTP 服务；请在 DuoPlus 控制台检查。")
    if not phone.get("ip") or not phone.get("region"):
        raise ValueError("云手机缺少 IP 或区域信息。")
    ready = request(config, "/agent-command", {"operation": "ready"}, phone)
    if ready.get("executor") != "ready" and ready.get("ready") is not True:
        raise ValueError("云手机执行器尚未就绪，请稍后刷新。")
    return phone


def submit(config, phone, action=None, params=None):
    task_id = uuid.uuid4().hex
    kind = "execute" if action else "get_ui_state"
    payload = {"task_type": "ai", "task_id": task_id, "action": kind, "lang": "zh"}
    if action:
        payload.update(action_name=action, params=params or {})
    outer = request(config, "/agent-command", {
        "operation": "submit", "command_id": uuid.uuid4().hex,
        "task_id": task_id, "action": kind, "payload": payload}, phone)
    result = json.loads(outer.get("result_json", "{}"))
    if outer.get("state") != "SUCCEEDED" or result.get("success") is not True:
        raise ValueError("云手机动作未成功，请刷新屏幕确认状态后再操作。")
    return result


def validate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError("脚本必须为包含 1–20 个动作的 JSON 数组。")
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in ACTIONS:
            raise ValueError("脚本包含不支持的动作。")
        params = step.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("动作 params 必须是 JSON 对象。")
        for key in ("wait_before", "wait_after", "wait_time", "duration"):
            if key in params and (type(params[key]) not in (int, float) or not 0 <= params[key] <= 10000):
                raise ValueError("等待及长按时间必须在 0–10000 毫秒之间。")
        if "timeout" in params and (type(params["timeout"]) not in (int, float) or not 0 < params["timeout"] <= 30):
            raise ValueError("元素等待 timeout 必须在 0–30 秒之间。")
        if "COORDINATE" in step["action"]:
            if any(type(params.get(k)) not in (int, float) or not 0 <= params[k] <= 1000 for k in ("x", "y")):
                raise ValueError("点击坐标必须在 0–1000 之间。")
    return steps


def handle(handler, parsed, data_dir, respond):
    if not parsed.path.startswith("/api/duoplus/"):
        return False
    if not virphone.allowed_request(handler, write=handler.command != "GET"):
        respond(handler, 403, {"error": "仅允许可信的 4003 局域网页面操作。"})
        return True
    if not LOCK.acquire(blocking=False):
        respond(handler, 409, {"error": "云手机操作正在执行，请等待完成。"})
        return True
    config = {}
    try:
        config = load_config(data_dir)
        path = parsed.path.removeprefix("/api/duoplus/")
        if handler.command == "GET" and path == "config":
            result = public_config(config)
        elif handler.command == "GET" and path == "phones":
            result = {"phones": [{k: row.get(k) for k in ("id", "name", "status", "http_status", "region")}
                                 for row in list_phones(config)]}
        elif handler.command == "POST" and path in {"config", "screen", "run"}:
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536 or handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请求必须为不超过 64KB 的 JSON。")
            body = json.loads(handler.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象。")
            if path == "config":
                for key in ("api_key", "image_id", "region"):
                    value = body.get(key, config[key])
                    if not isinstance(value, str) or len(value) > 500 or "\n" in value or "\r" in value:
                        raise ValueError("配置值无效。")
                    if key != "api_key" or value.strip():
                        config[key] = value.strip()
                if config["region"] not in {"cn", "global"}:
                    raise ValueError("请选择有效的接口区域。")
                data_dir.mkdir(parents=True, exist_ok=True)
                target = data_dir / "duoplus.json"
                fd = os.open(str(target) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(config, stream, ensure_ascii=False)
                os.replace(str(target) + ".tmp", target)
                result = public_config(config)
            else:
                steps = validate_steps(body.get("steps")) if path == "run" else []
                phone = get_phone(config, body.get("image_id", config["image_id"]))
                screen = submit(config, phone)
                logs = []
                for index, step in enumerate(steps, 1):
                    try:
                        action_result = submit(config, phone, step["action"], step.get("params", {}))
                        screen = submit(config, phone)
                        logs.append({"step": index, "action": step["action"], "success": True,
                                     "result": {key: value for key, value in action_result.items()
                                                if key != "screenshot"}})
                    except (ValueError, requests.RequestException):
                        # Never retry a mutating action: a timeout may hide successful execution.
                        respond(handler, 502, {"error": f"第 {index} 步未能确认成功，脚本已停止。请刷新屏幕确认后再操作。",
                                              "logs": logs, "screen": screen})
                        return True
                result = {"screen": screen, "logs": logs}
        else:
            respond(handler, 404, {"error": "接口不存在。"})
            return True
        respond(handler, 200, result)
    except requests.RequestException:
        respond(handler, 502, {"error": "连接 DuoPlus 失败或超时，请检查网络并刷新设备状态；操作未自动重试。"})
    except (ValueError, KeyError, TypeError, OSError) as exc:
        message = str(exc).replace(config.get("api_key") or "\0", "[已隐藏]")[:400]
        respond(handler, 400, {"error": message})
    finally:
        LOCK.release()
    return True
