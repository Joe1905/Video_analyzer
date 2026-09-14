"""Shared catalog + persistent product videos. Explicit updates always fetch fresh data."""
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from PIL import Image

import proxy_pool
from sociavault_tiktok_shop import SociaVaultClient, DEFAULT_API_BASE

ROOT = Path.cwd()
DIRECTORY = ROOT / "data" / "product_videos"
MEDIA = ROOT / "output" / "product_videos"
_lock = threading.RLock()
_images_lock = threading.Lock()
_audio_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="product-videos")
_owner = uuid.uuid4().hex
_initialized = False
_cleanup_started = False


def video_expiry(folder):
    target = folder / "video.mp4"
    if not target.is_file():
        return 0
    try:
        return float(json.loads((folder / "cache.json").read_text())["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        try:
            return target.stat().st_mtime + (7 if (folder / "audio.mp3").is_file() else 1) * 86400
        except FileNotFoundError:
            return 0


def cleanup_media():
    # ponytail: share the media lock so cleanup never deletes a video being extracted.
    if not _audio_lock.acquire(blocking=False):
        return
    try:
        for target in MEDIA.glob("*/video.mp4"):
            if target.parent.name.isascii() and target.parent.name.isdigit() and video_expiry(target.parent) <= time.time():
                target.unlink(missing_ok=True)
                (target.parent / "cache.json").unlink(missing_ok=True)
    finally:
        _audio_lock.release()


def cleanup_worker():
    while True:
        try:
            cleanup_media()
        except OSError:
            pass  # Retry transient filesystem failures on the next sweep.
        time.sleep(60)


@contextmanager
def database():
    global _initialized
    with _lock:
        conn = proxy_pool.connect()
        if not _initialized:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS product_video_items (
                    product_id TEXT NOT NULL REFERENCES tiktok_products(product_id) ON DELETE CASCADE,
                    video_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(product_id, video_id));
                CREATE TABLE IF NOT EXISTS product_video_jobs (
                    product_id TEXT PRIMARY KEY REFERENCES tiktok_products(product_id) ON DELETE CASCADE,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_video_items (
                    product_id TEXT NOT NULL, video_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(product_id, video_id));
                CREATE TABLE IF NOT EXISTS account_video_jobs (product_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_video_pages (
                    product_id TEXT PRIMARY KEY, cursor TEXT NOT NULL, has_more INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS shared_video_items (video_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)
            legacy = {}
            for table in ("product_video_items", "account_video_items"):
                for row in conn.execute(f"SELECT video_id,payload FROM {table} WHERE video_id NOT IN (SELECT video_id FROM shared_video_items)"):
                    legacy.setdefault(row[0], []).append(json.loads(row[1]))
            for vid, copies in legacy.items():
                merged = {"video_id": vid}
                for copy in sorted(copies, key=lambda v: max(v.get("updated_at") or 0, v.get("basic_updated_at") or 0)):
                    merged = merge_video(merged, copy)
                conn.execute("INSERT INTO shared_video_items VALUES (?,?)", (vid, json.dumps(merged, ensure_ascii=False)))
            conn.commit()
            _initialized = True
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def identifier(value):
    value = str(value or "")
    if not re.fullmatch(r"[0-9]{1,40}", value):
        raise ValueError("无效的商品或视频 ID")
    return value


def product(product_id):
    if str(product_id).startswith("account:"):
        identifier(product_id.removeprefix("account:"))
        for account in accounts():
            if account["product_id"] == product_id:
                return account
        raise ValueError("账号已从 IP 池移除，请刷新账号列表")
    product_id = identifier(product_id)
    with database() as conn:
        row = conn.execute("SELECT * FROM tiktok_products WHERE product_id=?", (product_id,)).fetchone()
    if not row:
        raise ValueError("商品已被删除，请刷新商品库")
    return dict(row)


def accounts():
    with database() as conn:
        records = conn.execute("SELECT id,username,display_name,feishu_user_name,status,profile_json FROM tiktok_accounts WHERE deleted_at='' ORDER BY id").fetchall()
    result = []
    for row in records:
        profile = json.loads(row["profile_json"] or "{}")
        if profile.get("platform_deletions", {}).get("tiktok"):
            continue
        handle = row["username"].lstrip("@")
        result.append({"product_id": f"account:{row['id']}", "product_name": row["display_name"] or handle,
                       "handle": handle, "price": "@" + handle, "stock": "", "status": row["status"],
                       "feishu_user_name": row["feishu_user_name"],
                       "product_url": "https://www.tiktok.com/@" + handle,
                       "image_url": f"/api/proxy/accounts/avatar/{row['id']}" if proxy_pool._account_avatar_path(row["id"]).is_file() else ""})
    return result


def video_tables(pid):
    prefix = "account" if str(pid).startswith("account:") else "product"
    identifier(str(pid).removeprefix("account:"))
    return prefix + "_video_items", prefix + "_video_jobs"


def snapshot(product_id):
    selected = product(product_id)
    items_table, jobs_table = video_tables(product_id)
    with database() as conn:
        videos = [json.loads(row[0]) for row in conn.execute(
            f"SELECT shared.payload FROM {items_table} AS links JOIN shared_video_items AS shared ON shared.video_id=links.video_id WHERE links.product_id=?", (product_id,))]
        row = conn.execute(f"SELECT payload FROM {jobs_table} WHERE product_id=?", (product_id,)).fetchone()
        page = conn.execute("SELECT cursor,has_more FROM account_video_pages WHERE product_id=?", (product_id,)).fetchone()
    job = json.loads(row[0]) if row else None
    if job and job.get("status") in {"queued", "running"} and job.get("owner") != _owner:
        job.update(status="failed", message="服务已重启，本次任务中断；已保存的数据仍可查看，请重新更新。")
    for video in videos:
        folder = MEDIA / video["video_id"]
        video["downloaded"] = video_expiry(folder) > time.time()
        video["audio_ready"] = (folder / "audio.mp3").is_file()
    return {"product": selected, "videos": videos, "job": job, "has_more": bool(page and page[1]), "cursor": page[0] if page else ""}


def item(product_id, video_id):
    identifier(video_id)
    for video in snapshot(product_id)["videos"]:
        if video["video_id"] == video_id:
            return video
    raise ValueError("该商品尚未收录此视频")


def merge_video(old, video):
    merged = dict(old)
    incoming_time = max(video.get("updated_at") or 0, video.get("basic_updated_at") or 0)
    current_time = max(old.get("updated_at") or 0, old.get("basic_updated_at") or 0)
    for key, value in video.items():
        if key != "error" and (value is None or value == "" or (key == "duration" and value == 0)):
            continue
        if incoming_time and incoming_time < current_time and key in old:
            continue
        merged[key] = value
    return merged


def save_video(product_id, video):
    items_table, _ = video_tables(product_id)
    vid = identifier(video["video_id"])
    with _lock, database() as conn:
        old = conn.execute("SELECT payload FROM shared_video_items WHERE video_id=?", (vid,)).fetchone()
        merged = merge_video(json.loads(old[0]) if old else {}, video)
        conn.execute("INSERT OR REPLACE INTO shared_video_items VALUES (?, ?)", (vid, json.dumps(merged, ensure_ascii=False)))
        # Existing tables retain source membership and legacy payloads for migration.
        conn.execute(f"INSERT OR IGNORE INTO {items_table} VALUES (?, ?, ?)", (product_id, vid, "{}"))


def client():
    key = os.getenv("SOCIAVAULT_API_KEY", "").strip()
    if not key:
        raise ValueError("服务器未配置 SociaVault，请先配置后重试")
    return SociaVaultClient(key, os.getenv("SOCIAVAULT_API_BASE", DEFAULT_API_BASE), 60)


def unwrap(payload):
    for _ in range(4):
        if not isinstance(payload, dict) or payload.get("success") is False or payload.get("error"):
            raise ValueError("SociaVault 未返回有效数据，请稍后重试")
        if "data" not in payload:
            return payload
        payload = payload["data"]
    raise ValueError("无法识别 SociaVault 返回结构")


def rows(value):
    return list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else []


def number(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def safe_url(value):
    value = str(value or "")
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password else ""


def first_url(node, *, image=False):
    if isinstance(node, str):
        if image and urlparse(node).path.lower().endswith((".heic", ".heif")):
            return ""
        return safe_url(node)
    for child in rows(node):
        result = first_url(child, image=image)
        if result:
            return result
    return ""


def normalize_related(raw):
    vid = identifier(raw.get("item_id"))
    author = str(raw.get("author_id") or "")
    result = {"video_id": vid, "title": str(raw.get("title") or ""),
              "url": f"https://www.tiktok.com/@{author if author.isdigit() else 'user'}/video/{vid}",
              "author": str(raw.get("author_name") or author), "author_id": author,
              "cover_url": safe_url(raw.get("cover_image_url")), "media_url": safe_url(raw.get("content_url")),
              "published_at": number(raw.get("upload_time")), "duration": (number(raw.get("duration")) or 0) / 1000,
              "discovered_at": time.time()}
    for key, source in (("views", "play_count"), ("likes", "like_count")):
        value = number(raw.get(source))
        if value is not None:
            result[key] = value
    result["basic_updated_at"] = time.time()
    return result


def refresh_video(api, video):
    data = unwrap(api.get("/v1/scrape/tiktok/video-info", {"url": video["url"], "region": "US"}, cache_policy="record_only"))
    detail = data.get("aweme_detail") or data.get("video") or data
    return video_detail(detail, video)


def video_detail(detail, video):
    if not isinstance(detail, dict) or str(detail.get("aweme_id") or detail.get("id") or "") != video["video_id"]:
        raise ValueError("视频详情 ID 不匹配或视频已不可用，已保留旧数据")
    stats = detail.get("statistics") or detail.get("stats") or {}
    if not isinstance(stats, dict) or not any(k in stats for k in ("play_count", "playCount")):
        raise ValueError("接口缺少视频指标，已保留旧数据")
    update = {"video_id": video["video_id"], "updated_at": time.time(), "error": ""}
    for name, snake, camel in (("views", "play_count", "playCount"), ("likes", "digg_count", "diggCount"),
                               ("comments", "comment_count", "commentCount"), ("shares", "share_count", "shareCount"),
                               ("saves", "collect_count", "collectCount")):
        val = number(stats.get(snake, stats.get(camel)))
        if val is not None:
            update[name] = val
    author = detail.get("author") or {}
    media = detail.get("video") or {}
    for key, val in {"title": detail.get("desc"), "author": author.get("nickname"),
                     "cover_url": first_url(media.get("cover"), image=True),
                     "media_url": first_url(media.get("play_addr"))}.items():
        if val:
            update[key] = val
    return update


def save_job(job):
    job["updated_at"] = time.time()
    _, jobs_table = video_tables(job["product_id"])
    with database() as conn:
        conn.execute(f"INSERT OR REPLACE INTO {jobs_table} VALUES (?, ?)",
                     (job["product_id"], json.dumps(job, ensure_ascii=False)))


def start(payload):
    pid = str(payload.get("product_id") or "")
    video_tables(pid)
    kind = payload.get("action", "refresh")
    vid = str(payload.get("video_id") or "")
    if kind not in {"refresh", "more", "play", "download", "audio"}:
        raise ValueError("不支持的任务类型")
    with _lock:
        state = snapshot(pid)
        if state["job"] and state["job"]["status"] in {"running", "queued"}:
            return state["job"]
        if vid:
            item(pid, vid)
        elif kind not in {"refresh", "more"}:
            raise ValueError("请先选择视频")
        if kind == "more" and (not pid.startswith("account:") or vid or not state["has_more"]):
            raise ValueError("没有可加载的下一页")
        if kind in {"refresh", "more"}:
            client()
        job = {"id": uuid.uuid4().hex, "owner": _owner, "product_id": pid, "video_id": vid,
               "action": kind, "status": "queued", "done": 0, "total": 0, "failures": 0,
               "message": "已加入队列", "started_at": time.time()}
        if kind == "more":
            job["cursor"] = state["cursor"]
        save_job(job)
        _pool.submit(run_job, job)
    return job


def run_job(job):
    pid, vid = job["product_id"], job["video_id"]
    try:
        job.update(status="running", message="正在读取商品关联视频…")
        save_job(job)
        if pid.startswith("account:") and not vid and job["action"] in {"refresh", "more"}:
            fetch_account_page(job)
        elif job["action"] != "refresh":
            prepare_media(job)
        else:
            api = client()
            discovered = 0
            discovery_error = ""
            if not vid:
                selected = product(pid)
                url = selected["product_url"] or f"https://www.tiktok.com/shop/pdp/{pid}"
                try:
                    data = unwrap(api.get("/v1/scrape/tiktok-shop/product-details",
                                          {"url": url, "region": "US", "get_related_videos": "true"}, cache_policy="record_only"))
                    if str(data.get("product_id") or "") != pid or "related_videos" not in data:
                        raise ValueError("商品 ID 不匹配或接口缺少关联视频字段")
                    related = data["related_videos"]
                    if not isinstance(related, (dict, list)):
                        raise ValueError("关联视频格式异常")
                    for raw in rows(related):
                        normalized = normalize_related(raw)
                        save_video(pid, normalized)
                        discovered += 1
                except Exception:
                    discovery_error = "关联视频查询失败；已保留原列表"
            videos = [item(pid, vid)] if vid else snapshot(pid)["videos"]
            job.update(total=len(videos), discovered=discovered)
            if discovery_error:
                job["failures"] += 1
            for video in videos:
                job["message"] = f"正在更新视频 {job['done'] + 1}/{len(videos)}"
                save_job(job)
                try:
                    save_video(pid, refresh_video(api, video))
                except Exception:
                    job["failures"] += 1
                    save_video(pid, {"video_id": video["video_id"], "error": "本次更新失败，保留上次数据", "attempted_at": time.time()})
                job["done"] += 1
                save_job(job)
            job["message"] = (f"{discovery_error}。" if discovery_error else "") + (
                f"已处理 {len(videos)} 条视频，{job['failures']} 项未成功。" if job["failures"] else
                f"已更新 {len(videos)} 条视频。" if videos else "本次未返回关联视频；不代表该商品没有带货视频。")
        job["status"] = "partial" if job["failures"] else "complete"
    except Exception as exc:
        job.update(status="failed", message=public_error(exc))
    finally:
        try:
            save_job(job)
        except Exception:
            pass  # Product may have been removed while a request was in flight.


def fetch_account_page(job):
    pid = job["product_id"]
    account = product(pid)
    params = {"handle": account["handle"], "sort_by": "latest"}
    if job.get("cursor"):
        params["max_cursor"] = job["cursor"]
    job["message"] = "正在查询账号视频（本页 1 credit）…"
    save_job(job)
    response = client().get("/v1/scrape/tiktok/videos", params, cache_policy="record_only")
    data = unwrap(response)
    if data.get("status_code", 0) != 0 or not isinstance(data.get("aweme_list"), (dict, list)):
        raise ValueError("账号视频列表查询失败，已保留原列表和翻页位置")
    videos = []
    for raw in rows(data["aweme_list"]):
        if raw.get("image_post_info"):
            continue
        author = raw.get("author") or {}
        if author.get("unique_id") and author["unique_id"].lower() != account["handle"].lower():
            raise ValueError("返回视频的账号不匹配，已保留原列表")
        base = normalize_related({"item_id": raw.get("aweme_id"), "title": raw.get("desc"),
                                  "author_id": author.get("uid"), "author_name": author.get("nickname"),
                                  "upload_time": raw.get("create_time"), "duration": (raw.get("video") or {}).get("duration")})
        base["url"] = account["product_url"] + "/video/" + base["video_id"]
        base.update(video_detail(raw, base))
        videos.append(base)
    for video in videos:
        save_video(pid, video)
    cursor = str(data.get("max_cursor") or "")
    more = data.get("has_more") in (1, True, "1") and bool(cursor) and cursor != job.get("cursor", "")
    with database() as conn:
        conn.execute("INSERT OR REPLACE INTO account_video_pages VALUES (?,?,?)", (pid, cursor, int(more)))
    job.update(done=len(videos), total=len(videos), credits_used=response.get("credits_used", 1),
               message=f"本页已更新 {len(videos)} 条视频 · 1 credit" + ("，可手动加载下一页。" if more else "，已到末页。"))


def public_error(exc):
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return str(exc)[:300]
    if isinstance(exc, subprocess.TimeoutExpired):
        return "处理超时，已保存的文件和转写保留，可重试。"
    return "处理失败，请检查网络与服务配置后重试；已有数据已保留。"


def validate_media_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = ("tiktokcdn.com", "tiktokcdn-us.com", "ttcdn-us.com", "tiktokcdn-eu.com", "byteoversea.com", "ibytedtos.com")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not any(host == d or host.endswith("." + d) for d in allowed):
        raise ValueError("仅支持 TikTok CDN 的 HTTPS 图片和视频链接")
    addresses = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("媒体地址不是公网地址")


class IPv4Adapter(requests.adapters.HTTPAdapter):
    """Bind only this downloader to IPv4; the server has unusable CDN IPv6 DNS answers."""
    def init_poolmanager(self, *args, **kwargs):
        return super().init_poolmanager(*args, source_address=("0.0.0.0", 0), **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        return super().proxy_manager_for(proxy, source_address=("0.0.0.0", 0), **kwargs)


def download_media(url, target, limit):
    validate_media_url(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    proxy = os.getenv("TIKTOK_PROXY_URL", "").strip()
    routes = [proxy, ""] if proxy else [""]
    for index, route in enumerate(routes):
        started = time.monotonic()
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.mount("https://", IPv4Adapter())
                with session.get(url, stream=True, allow_redirects=False, timeout=(8, 30),
                                 proxies={"http": route, "https": route} if route else {},
                                 headers={"User-Agent": "Mozilla/5.0"}) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ValueError("媒体地址已跳转，请更新视频数据后重试")
                    if int(response.headers.get("Content-Length") or 0) > limit:
                        raise ValueError("媒体文件超过大小限制")
                    size = 0
                    with temporary.open("wb") as output:
                        for chunk in response.iter_content(128 * 1024):
                            size += len(chunk)
                            if size > limit or time.monotonic() - started > 180:
                                raise ValueError("媒体文件过大或下载超时")
                            output.write(chunk)
                    if not size:
                        raise ValueError("媒体文件为空")
            temporary.replace(target)
            return
        except Exception:
            temporary.unlink(missing_ok=True)
            if index == len(routes) - 1:
                raise


def image_path(pid, vid=""):
    source = item(pid, vid).get("cover_url", "") if vid else product(pid)["image_url"]
    if not source:
        raise FileNotFoundError("暂无图片")
    # ponytail: serialize first image downloads; use per-URL locks if cold-load volume grows.
    with _images_lock:
        target = DIRECTORY / "images" / (hashlib.sha256(source.encode()).hexdigest() + ".jpg")
        if target.is_file():
            return target
        failure = target.with_suffix(".failed")
        if failure.exists() and time.time() - failure.stat().st_mtime < 300:
            raise FileNotFoundError("图片暂时不可用")
        temporary = target.with_suffix(".download")
        try:
            download_media(source, temporary, 12 * 1024 * 1024)
            with Image.open(temporary) as img:
                if img.width * img.height > 25_000_000:
                    raise ValueError("图片尺寸过大")
                img.thumbnail((900, 900))
                img.convert("RGB").save(target, "JPEG", quality=86)
        except Exception:
            target.parent.mkdir(parents=True, exist_ok=True)
            failure.touch()
            raise FileNotFoundError("图片暂时无法下载，可修改主图链接后重试")
        finally:
            temporary.unlink(missing_ok=True)
    return target


def media_state(pid, vid):
    item(pid, vid)
    folder = MEDIA / identifier(vid)
    expires_at = video_expiry(folder)
    result = {"video_id": vid, "downloaded": expires_at > time.time(), "expires_at": expires_at, "audio_ready": (folder / "audio.mp3").is_file()}
    for key in ("transcript", "translation"):
        file = folder / (key + ".json")
        result[key] = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else None
    return result


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def prepare_media(job):
    pid, vid = job["product_id"], job["video_id"]
    video = item(pid, vid)
    folder = MEDIA / vid
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "video.mp4"
    # ponytail: one media worker at a time limits Whisper RAM and shared-video writes.
    job["message"] = "等待下载 / 音频处理…"
    save_job(job)
    with _audio_lock:
        expires_at = video_expiry(folder)
        if expires_at <= time.time():
            job["message"] = "正在下载视频到本地…"
            save_job(job)
            api = client()
            fresh = refresh_video(api, video)
            save_video(pid, fresh)
            url = fresh.get("media_url") or video.get("media_url")
            if not url:
                raise ValueError("视频没有可用的下载地址")
            pending = folder / "video.pending.mp4"
            try:
                download_media(url, pending, 512 * 1024 * 1024)
                probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(pending)], capture_output=True, text=True, timeout=30, check=True)
                if "video" not in probe.stdout:
                    raise ValueError("下载结果没有有效视频流")
                pending.replace(target)
            finally:
                pending.unlink(missing_ok=True)
        write_json(folder / "cache.json", {"expires_at": max(expires_at, time.time() + (7 if job["action"] == "audio" else 1) * 86400)})
        if job["action"] in {"play", "download"}:
            job["message"] = "视频已就绪，可以播放。"
            return
        audio = folder / "audio.mp3"
        if not audio.is_file():
            job["message"] = "正在提取音频…"
            save_job(job)
            pending = folder / "audio.pending.mp3"
            try:
                subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(target), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(pending)], capture_output=True, timeout=120, check=True)
                pending.replace(audio)
            except subprocess.CalledProcessError:
                raise ValueError("无法提取音频，视频可能没有音轨")
            finally:
                pending.unlink(missing_ok=True)
        transcript_path = folder / "transcript.json"
        if not transcript_path.is_file():
            job["message"] = "音频已提取，正在识别语音（首次加载模型可能较慢）…"
            save_job(job)
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--transcribe", str(audio), str(transcript_path)], capture_output=True, timeout=900, check=True)
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        if not transcript.get("text", "").strip():
            job["message"] = "音频已提取，未识别到可翻译的语音。"
            return
        translation_path = folder / "translation.json"
        if not translation_path.is_file():
            job["message"] = "语音识别完成，正在翻译为简体中文…"
            save_job(job)
            from translate_analysis import translate_text_chunked, DEFAULT_API_URL, DEFAULT_MODEL
            key = os.getenv("DEEPSEEK_API_KEY", "")
            if not key:
                raise ValueError("原文已保存；翻译需要配置 DEEPSEEK_API_KEY")
            translated = translate_text_chunked(key, os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL), os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL), transcript["text"], 3000, 8192)
            if not translated.strip():
                raise ValueError("翻译返回为空，原文已保存，可重试")
            write_json(translation_path, {"text": translated, "updated_at": time.time()})
        job["message"] = "音频提取、语音转写和中文翻译已完成。"


def reply(handler, code, data, content_type="application/json; charset=utf-8", cache="no-store"):
    body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", cache)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def handle(handler, parsed, serve_file):
    if not parsed.path.startswith("/api/product-videos/"):
        return False
    global _cleanup_started
    with _lock:
        if not _cleanup_started:
            _cleanup_started = True
            threading.Thread(target=cleanup_worker, daemon=True, name="product-video-cleanup").start()
    query = parse_qs(parsed.query)
    pid = query.get("product_id", [""])[0]
    vid = query.get("video_id", [""])[0]
    endpoint = parsed.path.removeprefix("/api/product-videos/")
    try:
        if handler.command == "POST" and endpoint == "jobs":
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("请求大小无效")
            payload = json.loads(handler.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求必须为 JSON 对象")
            reply(handler, 202, start(payload))
        elif handler.command != "GET":
            reply(handler, 405, {"error": "不支持的操作"})
        elif endpoint == "accounts":
            reply(handler, 200, {"products": accounts()})
        elif endpoint == "list":
            reply(handler, 200, snapshot(pid))
        elif endpoint == "image":
            path = image_path(pid, vid)
            reply(handler, 200, path.read_bytes(), "image/jpeg", "public, max-age=86400")
        elif endpoint == "media":
            reply(handler, 200, media_state(pid, vid))
        elif endpoint == "file":
            item(pid, vid)
            kind = query.get("kind", ["video"])[0]
            if kind not in {"audio", "video"}:
                raise ValueError("无效的文件类型")
            path = MEDIA / vid / ("audio.mp3" if kind == "audio" else "video.mp4")
            if not path.is_file() or (kind == "video" and video_expiry(path.parent) <= time.time()):
                raise FileNotFoundError("视频缓存已过期，请点击在线播放或提取音频")
            serve_file(handler, path, "audio/mpeg" if kind == "audio" else "video/mp4", f"{vid}.{path.suffix[1:]}", path.stat().st_size, download=query.get("download", ["0"])[0] == "1")
        else:
            reply(handler, 404, {"error": "接口不存在"})
    except (ValueError, FileNotFoundError) as exc:
        reply(handler, 404 if isinstance(exc, FileNotFoundError) else 400, {"error": public_error(exc)})
    except Exception as exc:
        reply(handler, 500, {"error": public_error(exc)})
    return True


if __name__ == "__main__" and sys.argv[1:2] == ["--transcribe"]:
    from direct_video_analyze import transcribe_audio
    result = transcribe_audio(Path(sys.argv[2]), None, os.getenv("WHISPER_MODEL", "small"))
    write_json(Path(sys.argv[3]), result)
