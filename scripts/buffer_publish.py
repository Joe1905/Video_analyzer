"""Persistent Buffer publishing for the development LAN, independent of Studio."""
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import requests

ROOT = Path.cwd()
DATA = ROOT / "data" / "buffer_publish"
VIDEOS = ROOT / "videos" / "buffer_publish"
MAX_BYTES = 1024 ** 3
MEDIA_URL = "https://ruitech.hk/buffer-media"
ACTIVE = {"queued", "uploading", "submitting", "scheduled", "sending", "cancelling"}
POST_FIELDS = "id status dueAt sentAt externalLink error { message } allowedActions"
CREATE = "mutation Publish($input: CreatePostInput!) { createPost(input: $input) { ... on PostActionSuccess { post { " + POST_FIELDS + " } } ... on MutationError { message } } }"
_started = False
_lock = threading.RLock()


class PublishError(ValueError):
    pass


class Uncertain(PublishError):
    pass


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA / "jobs.sqlite", timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE, state TEXT, payload TEXT, updated REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS api_calls (at REAL)")
    return conn


def config():
    path = DATA / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_config(values):
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        values = {**config(), **values}
        target = DATA / "config.json"
        temporary = target.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(values, stream, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)


def clock():
    name = os.getenv("TZ", "America/Los_Angeles")
    try:
        zone = ZoneInfo(name)
    except (ValueError, KeyError):
        name, zone = "America/Los_Angeles", ZoneInfo("America/Los_Angeles")
    return {"timezone": name, "now": datetime.now(zone).isoformat()}


def due_at(value, zone_name):
    try:
        naive = datetime.fromisoformat(str(value))
        if naive.tzinfo is not None:
            raise ValueError()
        zone = ZoneInfo(zone_name)
        candidates = set()
        for fold in (0, 1):
            utc = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == naive:
                candidates.add(utc)
        if len(candidates) != 1:
            raise PublishError("该时间处于夏令时切换，请选择其他时间")
        result = candidates.pop()
        if result.timestamp() < time.time() + 120:
            raise PublishError("定时时间至少在两分钟后")
        return result.isoformat().replace("+00:00", "Z")
    except (ValueError, KeyError) as exc:
        if isinstance(exc, PublishError):
            raise
        raise PublishError("请选择有效的日期和时间") from exc


def safe_error(value):
    text = str(value)[:1500]
    for secret in config().values():
        if isinstance(secret, str) and len(secret) > 15:
            text = text.replace(secret, "[已隐藏]")
    return text


def graphql(query, variables=None, mutation=False):
    key = config().get("buffer_key", "")
    if not key:
        raise PublishError("请先配置 Buffer API Key")
    now = time.time()
    with _lock, db() as conn:
        # Leave headroom under the Free plan's 250/day and 3000/month limits.
        if conn.execute("SELECT count(*) FROM api_calls WHERE at>?", (now - 86400,)).fetchone()[0] >= 200:
            raise PublishError("今日 API 预算已用完，稍后自动继续")
        if conn.execute("SELECT count(*) FROM api_calls WHERE at>?", (now - 30 * 86400,)).fetchone()[0] >= 2800:
            raise PublishError("本月 API 预算已用完，请稍后重试")
        conn.execute("INSERT INTO api_calls VALUES (?)", (now,))
        conn.execute("DELETE FROM api_calls WHERE at<?", (now - 31 * 86400,))
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.post("https://api.buffer.com", headers={"Authorization": "Bearer " + key},
                                json={"query": query, "variables": variables or {}}, timeout=(10, 50), allow_redirects=False)
        if response.status_code != 200:
            error = (Uncertain if mutation and response.status_code >= 500 else PublishError)
            raise error("Buffer HTTP " + str(response.status_code))
        try:
            payload = response.json()
        except ValueError as exc:
            raise (Uncertain if mutation else PublishError)("Buffer 返回无法解析，需确认结果") from exc
        if not isinstance(payload, dict):
            raise (Uncertain if mutation else PublishError)("Buffer 返回无效结果")
        if payload.get("errors"):
            message = safe_error("; ".join(str(e.get("message", "API error")) for e in payload["errors"]))
            # Validation errors cannot execute a mutation; other errors may follow a write.
            validation = all(e.get("extensions", {}).get("code") in {"GRAPHQL_VALIDATION_FAILED", "GRAPHQL_PARSE_FAILED", "UNAUTHENTICATED", "FORBIDDEN"} for e in payload["errors"])
            raise (Uncertain if mutation and not validation else PublishError)(message)
        if not isinstance(payload.get("data"), dict):
            raise (Uncertain if mutation else PublishError)("Buffer 缺少结果数据")
        return payload["data"]
    except requests.RequestException as exc:
        # Never retry createPost following an ambiguous transport error.
        raise (Uncertain if mutation else PublishError)("Buffer 连接失败，请检查网络或代理；未自动重发") from exc
    finally:
        session.close()


def channels():
    organizations = graphql("{ account { organizations { id name } } }")["account"]["organizations"]
    result = []
    for org in organizations:
        data = graphql("query Channels($org: OrganizationId!) { channels(input: {organizationId: $org}) { id name displayName service isDisconnected isLocked isQueuePaused allowedActions timezone } }", {"org": org["id"]})
        for item in data["channels"]:
            if str(item["service"]).lower() == "tiktok":
                result.append({**item, "organization": org["name"], "organizationId": org["id"]})
    return result


def get_job(job_id):
    with db() as conn:
        row = conn.execute("SELECT payload FROM jobs WHERE id=?", (str(job_id),)).fetchone()
    if not row:
        raise PublishError("找不到任务")
    return json.loads(row[0])


def save(job, state=None, message=None):
    with _lock, db() as conn:
        if state:
            job["state"] = state
        if message:
            job.setdefault("logs", []).append({"at": datetime.now(timezone.utc).isoformat(), "state": job["state"], "message": safe_error(message)})
            job["logs"] = job["logs"][-150:]
        job["updated"] = time.time()
        conn.execute("UPDATE jobs SET state=?, payload=?, updated=? WHERE id=?", (job["state"], json.dumps(job, ensure_ascii=False), job["updated"], job["id"]))
    return job


def jobs():
    with db() as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT payload FROM jobs ORDER BY updated DESC LIMIT 100")]


def asset(asset_id):
    if not isinstance(asset_id, str) or not re.fullmatch(r"[0-9a-f]{32}", asset_id):
        raise PublishError("视频编号无效")
    path = VIDEOS / (asset_id + ".mp4")
    meta = path.with_suffix(".json")
    if not path.is_file() or not meta.is_file():
        raise PublishError("请重新上传视频")
    return path, json.loads(meta.read_text(encoding="utf-8"))


def inspect_video(path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, timeout=30, check=True)
        probe = json.loads(result.stdout)
        video = next(s for s in probe["streams"] if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"))
        duration = float(probe["format"]["duration"])
        if not 3 <= duration <= 600 or min(video["width"], video["height"]) < 360:
            raise PublishError("视频需为 3 秒至 10 分钟，宽高至少 360 像素")
        if "mp4" not in probe["format"].get("format_name", "").split(","):
            raise PublishError("请上传 MP4 视频")
        return {"duration": round(duration, 2), "width": video["width"], "height": video["height"], "size": path.stat().st_size}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as exc:
        if isinstance(exc, PublishError):
            raise
        raise PublishError("视频解析失败，请上传有效的 MP4 文件") from exc


def create_job(payload):
    request_id = str(payload.get("request_id", ""))
    if not re.fullmatch(r"[0-9a-f-]{36}", request_id):
        raise PublishError("请求编号无效，请刷新重试")
    with _lock:
        with db() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if row:
            return json.loads(row[0])
        asset(payload.get("asset_id"))
        mode = payload.get("mode")
        if mode not in {"draft", "now", "schedule"}:
            raise PublishError("请选择发布方式")
        text = str(payload.get("text", "")).strip()
        if not text or len(text) > 2200:
            raise PublishError("文案需为 1 至 2200 个字符")
        if payload.get("product_id"):
            raise PublishError("Buffer 不支持挂车，请使用 TikTok Shop 发布链路")
        if not config().get("media_token"):
            raise PublishError("远程视频存储尚未配置")
        channel = next((c for c in channels() if c["id"] == payload.get("channel_id")), None)
        if not channel:
            raise PublishError("请选择已连接的 TikTok 频道")
        if channel["isDisconnected"] or channel["isLocked"] or "scheduleUpdates" not in channel["allowedActions"]:
            raise PublishError("频道未连接或没有发布权限")
        if mode != "draft" and channel["isQueuePaused"]:
            raise PublishError("该频道已暂停发布，请先在 Buffer 恢复")
        zone = clock()["timezone"]
        due = due_at(payload.get("local_time"), zone) if mode == "schedule" else None
        job = {"id": uuid.uuid4().hex, "request_id": request_id, "asset_id": payload["asset_id"], "text": text,
               "channel_id": channel["id"], "channel_name": channel.get("displayName") or channel["name"], "mode": mode,
               "timezone": zone, "due_at": due, "ai_generated": payload.get("ai_generated") is True,
               "state": "queued", "created": time.time(), "updated": time.time(), "post_id": None, "logs": []}
        with db() as conn:
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (job["id"], request_id, job["state"], json.dumps(job, ensure_ascii=False), job["updated"]))
        return save(job, message="任务已保存，等待上传视频")


def post_input(job):
    value = {"channelId": job["channel_id"], "text": job["text"], "schedulingType": "automatic",
             "mode": "customScheduled" if job["mode"] == "schedule" else "shareNow" if job["mode"] == "now" else "addToQueue",
             "saveToDraft": job["mode"] == "draft", "needsApproval": False,
             "assets": [{"video": {"url": job["media_url"]}}], "metadata": {"tiktok": {"isAiGenerated": job["ai_generated"]}}}
    if job["mode"] == "schedule":
        value["dueAt"] = job["due_at"]
    return value


def upload_media(job):
    path, meta = asset(job["asset_id"])
    target = MEDIA_URL + "/" + job["asset_id"] + ".mp4"
    session = requests.Session()
    session.trust_env = False
    try:
        with path.open("rb") as stream:
            response = session.put(target, data=stream, headers={"Authorization": "Bearer " + config()["media_token"], "Content-Type": "video/mp4"}, timeout=(10, 180), allow_redirects=False)
        if response.status_code not in {200, 201}:
            raise PublishError("远程存储上传失败 HTTP " + str(response.status_code))
        receipt = response.json()
        if receipt.get("sha256") != meta["sha256"] or receipt.get("size") != meta["size"]:
            raise PublishError("远程视频校验失败")
        check = session.get(target, headers={"Range": "bytes=0-31"}, timeout=(10, 20), allow_redirects=False)
        if check.status_code != 206 or len(check.content) != 32 or check.headers.get("Content-Type", "").split(";")[0] != "video/mp4":
            raise PublishError("远程视频未通过公开读取验证")
        return target
    except requests.RequestException as exc:
        raise PublishError("远程存储连接失败，视频未提交 Buffer") from exc
    finally:
        session.close()


def apply_post(job, post):
    if not post or not post.get("id"):
        raise Uncertain("Buffer 未返回 Post ID，请在 Buffer 核对")
    status = post.get("status")
    state = {"sent": "sent", "error": "failed", "scheduled": "scheduled", "sending": "sending", "draft": "draft", "needs_approval": "needs_approval"}.get(status, "uncertain")
    previous = job.get("buffer_status")
    job.update(post_id=post["id"], buffer_status=status, external_link=post.get("externalLink"), sent_at=post.get("sentAt"))
    if post.get("dueAt"):
        job["due_at"] = post["dueAt"]
    if post.get("error"):
        job["error"] = safe_error(post["error"].get("message", "发布失败"))
    job["next_check"] = time.time() + 600
    return save(job, state, ("Buffer 状态：" + str(status) + (" · " + job["error"] if job.get("error") else "")) if status != previous else None)


def reconcile(job):
    post = graphql("query Status($id: PostId!) { post(input: {id: $id}) { " + POST_FIELDS + " } }", {"id": job["post_id"]})["post"]
    if post is None:
        raise Uncertain("Buffer 中找不到该任务，请核对是否已在外部删除")
    return apply_post(job, post)


def submit(job):
    try:
        save(job, "uploading", "上传视频到 Ubuntu")
        job["media_url"] = upload_media(job)
        if job["mode"] == "schedule" and datetime.fromisoformat(job["due_at"].replace("Z", "+00:00")).timestamp() <= time.time() + 30:
            raise PublishError("定时时间已过近或已错过，请新建任务选择未来时间")
        # Durable boundary: a process interruption after this line must not replay createPost.
        save(job, "submitting", "已校验视频，提交 Buffer")
        response = graphql(CREATE, {"input": post_input(job)}, mutation=True)["createPost"]
        if response.get("message"):
            raise PublishError(safe_error(response["message"]))
        apply_post(job, response.get("post"))
    except Uncertain as exc:
        save(job, "uncertain", str(exc))
    except Exception as exc:
        state = "uncertain" if job["state"] == "submitting" and not isinstance(exc, PublishError) else "failed"
        job["error"] = safe_error(exc)
        save(job, state, job["error"])


def cancel(job_id):
    with _lock:
        job = get_job(job_id)
        if job["state"] == "queued":
            return save(job, "cancelled", "已取消，未提交 Buffer")
        if not job.get("post_id") or job["state"] not in {"draft", "scheduled", "needs_approval"}:
            raise PublishError("当前状态不能取消，请先刷新状态；结果待确认时请到 Buffer 核对")
        post = graphql("query Status($id: PostId!) { post(input: {id: $id}) { " + POST_FIELDS + " } }", {"id": job["post_id"]})["post"]
        if not post or post["status"] not in {"scheduled", "draft", "needs_approval"} or "deletePost" not in post["allowedActions"]:
            raise PublishError("任务可能已开始发布，无法取消，请刷新状态")
        save(job, "cancelling", "正在从 Buffer 移除待发布任务")
        try:
            result = graphql("mutation Cancel($id: PostId!) { deletePost(input: {id: $id}) { ... on DeletePostSuccess { id } ... on MutationError { message } } }", {"id": job["post_id"]}, mutation=True)["deletePost"]
            if result.get("id") != job["post_id"]:
                raise PublishError(result.get("message") or "取消失败")
            return save(job, "cancelled", "已从 Buffer 移除待发布任务")
        except Uncertain as exc:
            save(job, "uncertain", str(exc))
            raise
        except PublishError as exc:
            save(job, post["status"], str(exc))
            raise


def worker_tick():
    # One worker owns submissions. API cancellation and worker claims share the lock.
    for item in jobs():
        if item["state"] == "queued":
            with _lock:
                item = get_job(item["id"])
                if item["state"] != "queued":
                    continue
                save(item, "uploading", "开始处理")
            submit(item)
        elif item["state"] in {"scheduled", "sending"} and item.get("next_check", 0) <= time.time():
            due = item.get("due_at")
            if item["state"] == "scheduled" and due and datetime.fromisoformat(due.replace("Z", "+00:00")).timestamp() > time.time() + 120:
                continue
            try:
                with _lock:
                    fresh = get_job(item["id"])
                    if fresh["state"] not in {"scheduled", "sending"}:
                        continue
                    reconcile(fresh)
            except (PublishError, OSError, ValueError) as exc:
                # A read failure does not mean the publishing operation failed.
                fresh = get_job(item["id"])
                fresh["next_check"] = time.time() + 1800
                save(fresh, message="状态查询暂缓：" + safe_error(exc))


def start_worker():
    global _started
    if _started:
        return
    _started = True
    for item in jobs():
        if item["state"] in {"submitting", "cancelling"}:
            save(item, "uncertain", "服务重启时请求结果未知，未自动重发，请到 Buffer 核对")
        elif item["state"] == "uploading":
            save(item, "queued", "恢复未提交的上传任务")
    def loop():
        while True:
            try:
                worker_tick()
            except Exception as exc:
                print("Buffer worker: " + safe_error(type(exc).__name__), flush=True)
            time.sleep(20)
    threading.Thread(target=loop, daemon=True, name="buffer-publish").start()


def allowed(handler, write=False):
    try:
        peer = ipaddress.ip_address(handler.client_address[0])
        if not (peer.is_loopback or peer in ipaddress.ip_network("192.168.0.0/23")):
            return False
    except ValueError:
        return False
    hosts = {"192.168.1.254:4003", "tymy-beta.local:4003", "127.0.0.1:4003", "localhost:4003"}
    host = handler.headers.get("Host")
    return host in hosts and (not write or handler.headers.get("Origin") == "http://" + host)


def handle(handler, parsed):
    if not parsed.path.startswith("/api/publish/"):
        return False
    def reply(code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    write = handler.command == "POST"
    if not allowed(handler, write):
        reply(403, {"error": "仅允许可信局域网页面访问"})
        return True
    try:
        if not write:
            if parsed.path == "/api/publish/status":
                cfg = config()
                reply(200, {**clock(), "buffer_configured": bool(cfg.get("buffer_key")), "media_configured": bool(cfg.get("media_token")), "media_url": MEDIA_URL})
            elif parsed.path == "/api/publish/channels":
                reply(200, {"channels": channels()})
            elif parsed.path == "/api/publish/jobs":
                reply(200, {"jobs": jobs()})
            else:
                reply(404, {"error": "接口不存在"})
            return True
        length = int(handler.headers.get("Content-Length", "0"))
        if parsed.path == "/api/publish/upload":
            if not 0 < length <= MAX_BYTES or handler.headers.get("Content-Type", "").split(";")[0] != "video/mp4":
                handler.close_connection = True
                raise PublishError("请上传 1 GB 以内的 MP4 视频")
            name = parse_qs(parsed.query).get("name", ["video.mp4"])[0][:200]
            handler.connection.settimeout(180)
            VIDEOS.mkdir(parents=True, exist_ok=True)
            video_id = uuid.uuid4().hex
            path = VIDEOS / (video_id + ".mp4")
            digest = hashlib.sha256()
            try:
                remaining = length
                with path.open("xb") as target:
                    while remaining:
                        chunk = handler.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise PublishError("视频上传中断")
                        target.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                info = {**inspect_video(path), "id": video_id, "name": name, "sha256": digest.hexdigest()}
                path.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            except Exception:
                path.unlink(missing_ok=True)
                raise
            reply(201, info)
            return True
        if not 0 < length <= 32768 or handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
            handler.close_connection = True
            raise PublishError("无效请求")
        payload = json.loads(handler.rfile.read(length))
        if not isinstance(payload, dict):
            raise PublishError("无效请求")
        if parsed.path == "/api/publish/config":
            key = payload.get("buffer_key")
            if not isinstance(key, str) or not 20 <= len(key.strip()) <= 4096 or any(c.isspace() for c in key.strip()):
                raise PublishError("请输入有效的 Buffer API Key")
            save_config({"buffer_key": key.strip()})
            reply(200, {"ok": True})
        elif parsed.path == "/api/publish/jobs":
            reply(202, create_job(payload))
        elif parsed.path == "/api/publish/cancel":
            reply(200, cancel(payload.get("id")))
        elif parsed.path == "/api/publish/refresh":
            with _lock:
                item = get_job(payload.get("id"))
                if not item.get("post_id") or item["state"] in {"submitting", "cancelling", "cancelled"}:
                    raise PublishError("暂无可查询的 Buffer 任务，请在 Buffer 核对")
                reply(200, reconcile(item))
        else:
            reply(404, {"error": "接口不存在"})
    except (PublishError, ValueError, TypeError, KeyError, OSError) as exc:
        reply(400, {"error": safe_error(exc)})
    return True
