"""NarratoAI Modern Workbench Tornado Server & API Backend.
Serves the approved v3 flat-UI single-page application at `/` and provides
REST APIs for object recognition, video materials, auto video rendering, and media streaming.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
import math
from urllib.parse import urlsplit

import tornado.web
import tornado.routing
import tornado.httputil
import tornado.ioloop
import logging

from app.config import config
from app.services import narrato_object_demo as obj_demo
from app.services import narrato_elevenlabs as elevenlabs
from app.services.generate_video import _probe_video

# Directories
OBJECT_ROOT = Path("/NarratoAI/storage/object-demo")
VIDEO_RESOURCE = Path("/NarratoAI/resource/videos")
AUDIO_RESOURCE = Path("/NarratoAI/resource/songs")
TASKS_ROOT = Path("/NarratoAI/storage/tasks")
GLOBAL_PRODUCTS_FILE = OBJECT_ROOT / "global_products.json"


def load_global_products():
    if GLOBAL_PRODUCTS_FILE.is_file():
        try:
            return json.loads(GLOBAL_PRODUCTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_global_products(products):
    OBJECT_ROOT.mkdir(parents=True, exist_ok=True)
    GLOBAL_PRODUCTS_FILE.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")


MATERIAL_DURATION_CACHE = {}


def format_duration(dur_val) -> str:
    try:
        if isinstance(dur_val, dict):
            sec = float(dur_val.get("duration", 0))
        else:
            sec = float(dur_val)
        secs = int(round(sec))
        m = secs // 60
        s = secs % 60
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "00:08"


def get_materials_list():
    materials = []
    if VIDEO_RESOURCE.exists():
        valid_exts = {".mov", ".mp4", ".mkv", ".avi", ".webm"}
        v_files = [f for f in VIDEO_RESOURCE.iterdir() if f.is_file() and f.suffix.lower() in valid_exts]
        for v in sorted(v_files, key=os.path.getmtime, reverse=True)[:50]:
            dur_str = MATERIAL_DURATION_CACHE.get(v.name)
            if not dur_str:
                try:
                    dur_val = _probe_video(v)
                    dur_str = format_duration(dur_val)
                except Exception:
                    dur_str = "00:08"
                MATERIAL_DURATION_CACHE[v.name] = dur_str
            materials.append({
                "name": v.name,
                "duration": dur_str,
                "path": str(v),
                "size": v.stat().st_size
            })
    return materials


# Background job tracking
ACTIVE_JOBS = {}
EXPORT_JOBS = {}
TASK_TTL = 7 * 24 * 3600


def export_state(root):
    path = root / 'export.json'
    state = json.loads(path.read_text()) if path.is_file() else {'status': 'idle'}
    if state['status'] == 'idle':
        manifest = json.loads((root / 'manifest.json').read_text())
        archive = Path(manifest.get('archive') or '/nonexistent')
        index = archive.with_suffix('') / 'index.json'
        if archive.is_file() and index.is_file():
            exported = json.loads(index.read_text()).get('clips', [])
            keys = ('source_id', 'product_id', 'start', 'end')
            selected = [i for i, clip in enumerate(manifest.get('clips', []))
                        if any(all(clip.get(k) == old.get(k) for k in keys) for old in exported)]
            state = dict(status='complete', message='导出完成', selected=selected, archive_path=str(archive),
                         download_url=f'/api/object/download/{root.name}?file={archive.name}')
    if state['status'] == 'running' and root.name not in EXPORT_JOBS:
        state.update(status='failed', message='服务重启中断了导出，请重新提交')
    return state


def cleanup_object_media():
    """Expire object-task media; shared videos survive while another task needs them."""
    now = time.time()
    protected, expired = set(), []
    for path in OBJECT_ROOT.glob('*/manifest.json'):
        try:
            data = json.loads(path.read_text())
            # Existing tasks receive a full week from rollout, not immediate deletion.
            if 'expires_at' not in data:
                data['expires_at'] = now + TASK_TTL
                obj_demo.save(path, data)
            busy = path.parent.name in EXPORT_JOBS or ACTIVE_JOBS.get(path.parent.name, {}).get('status') == 'running'
            if data['expires_at'] > now or busy:
                protected.update(s['path'] for s in data.get('sources', []))
            else:
                expired.append((path, data))
        except Exception:
            logging.exception('Task retention scan failed: %s', path)
            return  # Fail closed: do not delete when references cannot be read.
    # Automatic-production tasks also share this video directory.
    for path in TASKS_ROOT.rglob('*.json'):
        try:
            raw = path.read_text()
            protected.update(str(p) for p in VIDEO_RESOURCE.iterdir() if str(p) in raw)
        except Exception:
            logging.exception('Shared media reference scan failed: %s', path)
            return
    for path, data in expired:
        try:
            with obj_demo.job_lock(path.parent):
                for source in data.get('sources', []):
                    video = Path(source['path']).resolve()
                    if video.parent == VIDEO_RESOURCE.resolve() and str(video) not in protected:
                        video.unlink(missing_ok=True)
                if path.parent.resolve().parent != OBJECT_ROOT.resolve():
                    raise ValueError('Invalid task cleanup path')
                shutil.rmtree(path.parent)
        except Exception:
            logging.exception('Task retention cleanup failed: %s', path)
    for video in VIDEO_RESOURCE.glob('*'):
        if video.is_file() and not video.is_symlink() and video.suffix.lower() in {'.mp4', '.mov', '.mkv', '.avi', '.webm'} and str(video) not in protected and video.stat().st_mtime + TASK_TTL <= now:
            video.unlink()


async def run_retention_cleanup():
    await asyncio.to_thread(cleanup_object_media)
RENDER_LOCK = threading.Lock()  # ponytail: upstream renderer uses module globals; serialize renders.


def task_dir(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', job_id):
        raise tornado.web.HTTPError(400, '无效的任务编号')
    return OBJECT_ROOT / job_id


def media_path(value):
    if not isinstance(value, str):
        raise tornado.web.HTTPError(400, '无效媒体路径')
    value = value.removeprefix('/api/media/')
    candidate = Path(value) if value.startswith('/NarratoAI/') else Path('/NarratoAI') / value.lstrip('/')
    candidate = candidate.resolve()
    allowed = [OBJECT_ROOT, VIDEO_RESOURCE, AUDIO_RESOURCE, TASKS_ROOT, Path('/NarratoAI/storage/uploaded_bgms')]
    if not any(candidate.is_relative_to(p.resolve()) for p in allowed) or candidate.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.mkv', '.avi', '.webm', '.mp3', '.wav', '.m4a', '.srt', '.zip'}:
        raise tornado.web.HTTPError(403, '禁止访问此文件')
    if not candidate.is_file():
        raise tornado.web.HTTPError(404, '媒体不存在')
    return candidate


def selected_sources(body):
    values = body.get('sources', [])
    if not isinstance(values, list) or not values:
        raise tornado.web.HTTPError(400, '请先上传待处理素材')
    sources = []
    for value in values:
        p = media_path(value.get('path') if isinstance(value, dict) else value)
        if not p.is_relative_to(VIDEO_RESOURCE.resolve()) or p.suffix.lower() not in {'.mp4', '.mov', '.mkv', '.avi', '.webm'}:
            raise tornado.web.HTTPError(400, '请从素材库选择视频')
        if any(s['path'] == str(p) for s in sources):
            continue
        duration = float(_probe_video(str(p))['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise tornado.web.HTTPError(400, '视频时长无效')
        sources.append(dict(id=f'v{len(sources)+1}', name=p.name, path=str(p), duration=duration))
    return sources


class BaseHandler(tornado.web.RequestHandler):
    def check_xsrf_cookie(self):
        origin = self.request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.request.host:
            raise tornado.web.HTTPError(403, '不允许跨站写入')

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Headers", "x-requested-with, content-type")
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()

    def write_json(self, data, status=200):
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(data, ensure_ascii=False))

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get('exc_info', (None, None, None))[1]
        self.write_json({'error': (exc.log_message if isinstance(exc, tornado.web.HTTPError) else None) or '请求失败，请检查输入与服务器日志'}, status_code)


class MediaStreamHandler(tornado.web.StaticFileHandler):
    """Streams media files with Range support for fluent browser video playback."""
    @classmethod
    def get_absolute_path(cls, root, path):
        return str(media_path(path))

    def validate_absolute_path(self, root, absolute_path):
        return str(media_path(absolute_path))


class WorkbenchHomeHandler(BaseHandler):
    """Serves the pixel-perfect Modern Workbench Single Page Application."""
    def get(self):
        self.set_header("Content-Type", "text/html; charset=UTF-8")
        self.write(RENDERED_HTML)


class StateHandler(BaseHandler):
    """Aggregated state for initial hydration."""
    async def get(self):
        # 0. Global Products
        global_products = load_global_products()

        # 1. Object tasks
        object_tasks = []
        if OBJECT_ROOT.exists():
            for p in sorted(OBJECT_ROOT.glob("*/manifest.json"), key=os.path.getmtime, reverse=True):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if data.get('expires_at', float('inf')) <= time.time():
                        continue
                    task_id = p.parent.name
                    prod_names = [x.get("name", "") for x in data.get("products", [])]
                    clips_cnt = len(data.get("clips", []))
                    sources_cnt = len(data.get("sources", []))
                    status = data.get("status", "pending")
                    mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
                    has_zip = bool(data.get("archive") and Path(data["archive"]).is_file())
                    object_tasks.append({
                        "id": task_id,
                        "title": " / ".join(prod_names) if prod_names else task_id,
                        "status": status,
                        "clips_count": clips_cnt,
                        "sources_count": sources_cnt,
                        "time": mtime,
                        "has_zip": has_zip,
                        "archive": data.get("archive", ""),
                    })
                except Exception:
                    pass

        # 2. Pick current task or create clean empty task
        current_task = None
        if object_tasks:
            target_id = object_tasks[0]["id"]
            manifest_path = OBJECT_ROOT / target_id / "manifest.json"
            if manifest_path.is_file():
                try:
                    current_task = json.loads(manifest_path.read_text(encoding="utf-8"))
                    current_task["id"] = target_id
                except Exception:
                    pass

        if not current_task:
            current_task = {
                "id": "空白任务",
                "status": "pending",
                "products": [],
                "sources": [],
                "clips": [],
                "step": 0.5
            }


        # 3. Materials
        materials = await asyncio.to_thread(get_materials_list)

        # 4. Voices
        voices = []
        api_key = config.app.get("elevenlabs_api_key", "").strip()
        if api_key:
            try:
                voices = await asyncio.to_thread(elevenlabs.get_voices, api_key)
            except Exception:
                voices = []
        if not voices:
            voices = [
                {"voice_id": "pNInz6obpgDQGcFmaJgB", "name": "阳光活力带货男声 (Adam)", "labels": {"gender": "male", "accent": "american"}, "preview_url": "https://storage.googleapis.com/eleven-public-prod/previews/voices/pNInz6obpgDQGcFmaJgB/audio.mp3"},
                {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "知性温婉推荐女声 (Rachel)", "labels": {"gender": "female", "accent": "american"}, "preview_url": "https://storage.googleapis.com/eleven-public-prod/previews/voices/21m00Tcm4TlvDq8ikWAM/audio.mp3"},
            ]

        # 5. Auto Tasks
        auto_tasks = []
        if TASKS_ROOT.exists():
            for tp in sorted(TASKS_ROOT.glob("*/combined.mp4"), key=os.path.getmtime, reverse=True)[:10]:
                tid = tp.parent.name
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(tp.stat().st_mtime))
                auto_tasks.append({
                    "id": tid,
                    "title": f"成片合成-{tid[:8]}",
                    "time": mtime,
                    "path": str(tp)
                })

        self.write_json({
            "bgms": [{"path":str(p), "name":p.name} for base in [AUDIO_RESOURCE, Path("/NarratoAI/storage/uploaded_bgms")] if base.exists() for p in base.iterdir() if p.suffix.lower() in {".mp3",".wav",".m4a"}],
            "current_task": current_task,
            "global_products": global_products,
            "object_tasks": object_tasks,
            "materials": materials,
            "voices": voices,
            "auto_tasks": auto_tasks
        })


class GlobalProductsHandler(BaseHandler):
    """Manage global products repository for re-use across all tasks."""
    def get(self):
        self.write_json({"status": "success", "products": load_global_products()})

    def post(self):
        try:
            body = json.loads(self.request.body.decode("utf-8") or "{}")
        except Exception:
            self.write_json({"error": "无效的JSON数据"}, status=400)
            return
        action = body.get("action", "save")
        products = load_global_products()

        if action == "delete":
            prod_id = body.get("id")
            products = [p for p in products if p.get("id") != prod_id]
            save_global_products(products)
            self.write_json({"status": "success", "products": products})
            return

        prod_data = body.get("product") or body
        prod_id = prod_data.get("id") or f"p_{uuid.uuid4().hex[:8]}"
        prod_name = prod_data.get("name", "").strip()
        prod_desc = prod_data.get("description", "").strip()
        prod_refs = prod_data.get("references", [])

        if not prod_name:
            self.write_json({"error": "商品名称不能为空"}, status=400)
            return

        # Update if exists, else append
        existing = False
        for p in products:
            if p.get("id") == prod_id or p.get("name") == prod_name:
                p["id"] = prod_id
                p["name"] = prod_name
                p["description"] = prod_desc
                p["references"] = prod_refs
                p["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                existing = True
                break
        if not existing:
            products.append({
                "id": prod_id,
                "name": prod_name,
                "description": prod_desc,
                "references": prod_refs,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            })

        save_global_products(products)
        self.write_json({
            "status": "success",
            "product": {"id": prod_id, "name": prod_name, "description": prod_desc, "references": prod_refs},
            "products": products
        })


class ObjectTasksHandler(BaseHandler):
    """List or create object tasks."""
    def get(self):
        tasks = []
        if OBJECT_ROOT.exists():
            for p in sorted(OBJECT_ROOT.glob("*/manifest.json"), key=os.path.getmtime, reverse=True):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if data.get('expires_at', float('inf')) <= time.time():
                        continue
                    tasks.append({
                        "id": p.parent.name,
                        "title": " / ".join(x.get("name", "") for x in data.get("products", [])),
                        "status": data.get("status", "pending"),
                        "clips_count": len(data.get("clips", [])),
                        "sources_count": len(data.get("sources", [])),
                        "has_zip": bool(data.get("archive") and Path(data["archive"]).is_file()),
                        "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
                    })
                except Exception:
                    pass
        self.write_json({"tasks": tasks})


class ObjectTaskDetailHandler(BaseHandler):
    """Get single task details."""
    def get(self, job_id):
        manifest_path = task_dir(job_id) / "manifest.json"
        if not manifest_path.is_file():
            self.write_json({"error": "Task not found"}, status=404)
            return
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["id"] = job_id
            self.write_json(data)
        except Exception as e:
            self.write_json({"error": str(e)}, status=500)


class ObjectAnalyzeHandler(BaseHandler):
    """Start once; never rewrite a running task or reuse changed-input caches."""
    async def post(self):
        body = json.loads(self.request.body or b'{}')
        job_id = body.get('job_id') or f'task-{uuid.uuid4().hex[:12]}'
        if job_id == '空白任务':
            job_id = f'task-{uuid.uuid4().hex[:12]}'
        root = task_dir(job_id)
        current = ACTIVE_JOBS.get(job_id)
        if current and current['status'] == 'running':
            self.write_json({'job_id': job_id, 'status': 'running'})
            return
        if root.exists():
            raise tornado.web.HTTPError(409, '此任务已有记录，请新建任务后识别')
        step = body.get('step', 0.5)
        if step not in (0.25, 0.5, 1):
            raise tornado.web.HTTPError(400, '无效采样间隔')
        products = body.get('products', [])
        if not isinstance(products, list) or not 1 <= len(products) <= 3:
            raise tornado.web.HTTPError(400, '请选择 1～3 个商品')
        ids = set()
        for p in products:
            if not isinstance(p, dict) or not p.get('name', '').strip() or not p.get('id') or p['id'] in ids:
                raise tornado.web.HTTPError(400, '商品名称和唯一编号必填')
            if not isinstance(p.get('description', ''), str):
                raise tornado.web.HTTPError(400, '同款判定规则应为文本')
            ids.add(p['id'])
            refs = p.get('references', [])
            if not isinstance(refs, list) or not refs:
                raise tornado.web.HTTPError(400, '每个商品至少需要一张参考图')
            p['references'] = [str(media_path(ref)) for ref in refs]
            from PIL import Image
            for ref in p['references']:
                with Image.open(ref) as image:
                    image.verify()
        # Reserve before yielding to probes; a second request cannot replace this job.
        record = {'status': 'running', 'progress': '正在检查素材', 'clips': []}
        ACTIVE_JOBS[job_id] = record
        try:
            sources = await asyncio.to_thread(selected_sources, body)
            root.mkdir(parents=True)
            obj_demo.save(root / 'manifest.json', dict(status='pending', products=products, sources=sources, step=step, clips=[], expires_at=time.time() + TASK_TTL))
        except Exception:
            ACTIVE_JOBS.pop(job_id, None)
            raise

        def worker():
            try:
                result = asyncio.run(obj_demo.analyze(root, progress=lambda msg: record.update(progress=msg)))
                record.update(status='complete', progress='识别完成', clips=result['clips'])
            except Exception as exc:
                record.update(status='error', message=str(exc))
        threading.Thread(target=worker, daemon=True).start()
        self.write_json({'job_id': job_id, 'status': 'started'})


class ObjectStatusHandler(BaseHandler):
    """Poll object analyze progress."""
    def get(self, job_id):
        task_dir(job_id)
        status_info = ACTIVE_JOBS.get(job_id)
        if not status_info:
            manifest_path = OBJECT_ROOT / job_id / "manifest.json"
            if manifest_path.is_file():
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    status_info = {
                        "status": data.get("status", "complete"),
                        "progress": "识别完成" if data.get("status") in ["complete", "review"] else data.get("status", ""),
                        "clips": data.get("clips", [])
                    }
                    if status_info['status'] in ('running', 'pending'):
                        status_info.update(status='failed', message='服务重启中断了任务，请新建任务后运行')
                except Exception:
                    status_info = {"status": "unknown", "progress": ""}
            else:
                status_info = {"status": "not_found", "progress": ""}
        self.write_json(status_info)


class ObjectExportHandler(BaseHandler):
    """Export ZIP package for selected clips."""
    async def post(self):
        body = json.loads(self.request.body.decode("utf-8") or "{}")
        job_id = body.get("job_id", "")
        selected_indices = body.get("selected_indices", None)

        target_dir = task_dir(job_id)
        if not (target_dir / "manifest.json").is_file():
            self.write_json({"error": "Manifest not found"}, status=404)
            return

        try:
            manifest = json.loads((target_dir / 'manifest.json').read_text())
            if not isinstance(selected_indices, list) or not selected_indices or any(type(i) is not int or i < 0 or i >= len(manifest['clips']) or manifest['clips'][i]['status'] != 'present' for i in selected_indices):
                self.write_json({'error': '仅可导出确认出现的片段'}, 400)
                return
            if manifest.get('expires_at', float('inf')) <= time.time():
                raise ValueError('任务已过期，请新建任务')
            selected = sorted(set(selected_indices))
            previous = export_state(target_dir)
            if job_id in EXPORT_JOBS:
                self.write_json(previous)
                return
            if previous.get('selected') == selected and previous['status'] == 'complete' and Path(previous.get('archive_path', '')).is_file():
                self.write_json(previous)
                return
            record = dict(status='running', message='等待剪辑', selected=selected)
            EXPORT_JOBS[job_id] = record
            obj_demo.save(target_dir / 'export.json', record)

            def worker():
                def progress(message):
                    record.update(message=message)
                    obj_demo.save(target_dir / 'export.json', record)
                try:
                    archive = obj_demo.export(target_dir, selected, progress=progress)
                    # Replace only after the new archive is complete; failure keeps the old result.
                    for old in target_dir.glob('export-*'):
                        if old in (archive, archive.with_suffix('')) or not re.fullmatch(r'export-[a-f0-9]{8}(?:\.zip)?', old.name):
                            continue
                        if old.is_symlink():
                            old.unlink()
                        elif old.is_dir():
                            shutil.rmtree(old)
                        else:
                            old.unlink()
                    record.update(status='complete', message='导出完成', archive_path=str(archive),
                                  download_url=f'/api/object/download/{job_id}?file={archive.name}')
                except Exception as exc:
                    record.update(status='failed', message=str(exc))
                    logging.exception('Export failed: %s', job_id)
                finally:
                    obj_demo.save(target_dir / 'export.json', record)
                    EXPORT_JOBS.pop(job_id, None)
            threading.Thread(target=worker, daemon=True).start()
            self.write_json(record, status=202)
        except Exception as e:
            self.write_json({"error": str(e)}, status=500)


class ObjectExportStatusHandler(BaseHandler):
    def get(self, job_id):
        root = task_dir(job_id)
        if not (root / 'manifest.json').is_file():
            raise tornado.web.HTTPError(404, '任务不存在或已过期')
        self.write_json(export_state(root))


class ObjectDownloadHandler(BaseHandler):
    """Download exported ZIP file."""
    def get(self, job_id):
        target_dir = task_dir(job_id)
        fname = self.get_argument("file", None)
        if fname:
            if Path(fname).name != fname or not re.fullmatch(r'export-[a-zA-Z0-9_-]+\.zip', fname):
                raise tornado.web.HTTPError(400, '无效下载文件名')
            zip_file = target_dir / fname
        else:
            zips = list(target_dir.glob("*.zip"))
            zip_file = zips[0] if zips else None

        if not zip_file or not zip_file.is_file():
            self.write_json({"error": "Zip file not found"}, status=404)
            return

        self.set_header("Content-Type", "application/zip")
        self.set_header("Content-Disposition", f"attachment; filename={zip_file.name}")
        with open(zip_file, "rb") as f:
            while chunk := f.read(65536):
                self.write(chunk)


class MaterialUploadHandler(BaseHandler):
    """Save uploaded video material to /NarratoAI/resource/videos."""
    def post(self):
        uploaded_files = []
        for key in ["files", "file"]:
            if key in self.request.files:
                uploaded_files.extend(self.request.files[key])

        seen = set()
        unique_files = []
        for uf in uploaded_files:
            sig = (uf.get("filename", ""), len(uf.get("body", b"")))
            if sig not in seen:
                seen.add(sig)
                unique_files.append(uf)

        if not unique_files:
            self.write_json({"error": "No file uploaded"}, status=400)
            return

        VIDEO_RESOURCE.mkdir(parents=True, exist_ok=True)
        saved = []
        for uf in unique_files:
            orig_name = re.sub(r"[^\w\.-]", "_", uf.get("filename", "video.mp4"))
            if Path(orig_name).suffix.lower() not in {'.mp4', '.mov', '.mkv', '.avi', '.webm'}:
                raise tornado.web.HTTPError(400, '不支持的视频格式')
            dest = VIDEO_RESOURCE / orig_name
            if dest.exists():
                dest = VIDEO_RESOURCE / f'{uuid.uuid4().hex[:8]}_{orig_name}'
            with open(dest, "wb") as f:
                f.write(uf["body"])
            MATERIAL_DURATION_CACHE.pop(orig_name, None)
            saved.append({"filename": orig_name, "path": str(dest)})

        self.write_json({
            "status": "success",
            "files": saved,
            "filename": saved[0]["filename"] if saved else "",
            "path": saved[0]["path"] if saved else "",
            "materials": get_materials_list()
        })


class MaterialDeleteHandler(BaseHandler):
    """Delete an uploaded material video."""
    def post(self):
        try:
            body = json.loads(self.request.body.decode("utf-8") or "{}")
        except Exception:
            self.write_json({"error": "无效的JSON数据"}, status=400)
            return

        filename = body.get("name") or body.get("filename")
        if not filename:
            self.write_json({"error": "缺少素材名称"}, status=400)
            return

        safe_name = Path(filename).name
        target = VIDEO_RESOURCE / safe_name
        for manifest_path in OBJECT_ROOT.glob('*/manifest.json'):
            manifest = json.loads(manifest_path.read_text())
            if any(s.get('path') == str(target) for s in manifest.get('sources', [])):
                raise tornado.web.HTTPError(409, '此素材被任务引用，请保留以便预览和导出')
        deleted = False
        if target.is_file():
            try:
                target.unlink()
                deleted = True
            except Exception as e:
                self.write_json({"error": f"删除素材失败: {str(e)}"}, status=500)
                return

        MATERIAL_DURATION_CACHE.pop(safe_name, None)
        self.write_json({
            "status": "success",
            "message": f"素材 {safe_name} 已删除" if deleted else f"未找到素材 {safe_name}",
            "materials": get_materials_list()
        })


class ProductRefUploadHandler(BaseHandler):
    """Save uploaded product reference images."""
    def post(self):
        uploaded_files = []
        for key in ["files", "file"]:
            if key in self.request.files:
                uploaded_files.extend(self.request.files[key])

        seen = set()
        unique_files = []
        for uf in uploaded_files:
            sig = (uf.get("filename", ""), len(uf.get("body", b"")))
            if sig not in seen:
                seen.add(sig)
                unique_files.append(uf)

        if not unique_files:
            self.write_json({"error": "No file uploaded"}, status=400)
            return

        ref_dir = OBJECT_ROOT / "references"
        ref_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for uf in unique_files:
            orig_name = re.sub(r"[^\w\.-]", "_", uf.get("filename", "ref.png"))
            from PIL import Image
            import io
            image = Image.open(io.BytesIO(uf['body']))
            image.verify()
            fname = f"ref_{uuid.uuid4().hex[:8]}_{orig_name}"
            dest = ref_dir / fname
            with open(dest, "wb") as f:
                f.write(uf["body"])
            url = f"/api/media/storage/object-demo/references/{fname}"
            saved.append({"filename": fname, "path": str(dest), "url": url})

        self.write_json({
            "status": "success",
            "files": saved,
            "urls": [s["url"] for s in saved],
            "url": saved[0]["url"] if saved else ""
        })


class VoicesHandler(BaseHandler):

    """List ElevenLabs voices."""
    def get(self):
        api_key = config.app.get("elevenlabs_api_key", "").strip()
        voices = []
        if api_key:
            try:
                voices = elevenlabs.get_voices(api_key)
            except Exception:
                pass
        if not voices:
            voices = [
                {"voice_id": "pNInz6obpgDQGcFmaJgB", "name": "阳光活力带货男声 (Adam)", "labels": {"gender": "male", "accent": "american"}, "preview_url": "https://storage.googleapis.com/eleven-public-prod/previews/voices/pNInz6obpgDQGcFmaJgB/audio.mp3"},
                {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "知性温婉推荐女声 (Rachel)", "labels": {"gender": "female", "accent": "american"}, "preview_url": "https://storage.googleapis.com/eleven-public-prod/previews/voices/21m00Tcm4TlvDq8ikWAM/audio.mp3"},
                {"voice_id": "AZnzlk1XvdvUeBnXmlld", "name": "亲切知性解说女声 (Domi)", "labels": {"gender": "female", "accent": "american"}, "preview_url": "https://storage.googleapis.com/eleven-public-prod/previews/voices/AZnzlk1XvdvUeBnXmlld/audio.mp3"}
            ]
        self.write_json({"voices": voices})


def launch_auto_job(work):
    job_id = str(uuid.uuid4())
    root = TASKS_ROOT / job_id
    root.mkdir(parents=True)
    record = {'status': 'running', 'progress': 0, 'message': '正在处理'}
    ACTIVE_JOBS[job_id] = record
    obj_demo.save(root / 'workbench.json', record)
    def worker():
        try:
            result = work(job_id, root, record)
            record.update(result, status='complete', progress=100, message='处理完成')
        except Exception as exc:
            record.update(status='error', message=str(exc))
        finally:
            obj_demo.save(root / 'workbench.json', record)
    threading.Thread(target=worker, daemon=True).start()
    return job_id


class AutoScriptHandler(BaseHandler):
    async def post(self):
        from app.services.narrato_multi_material import MultiMaterialAnalysisService, SCRIPT_LANGUAGES
        body = json.loads(self.request.body or b'{}')
        if not body.get('theme', '').strip() or not body.get('desc', '').strip():
            raise tornado.web.HTTPError(400, '请填写主题和商品描述')
        language = body.get('language', 'English')
        if language not in SCRIPT_LANGUAGES:
            raise tornado.web.HTTPError(400, '不支持的脚本语言')
        sources = await asyncio.to_thread(selected_sources, body)
        def work(job_id, root, record):
            paths = [s['path'] for s in sources]
            script = asyncio.run(MultiMaterialAnalysisService().generate_documentary_script(
                video_paths=paths, video_path=paths[0], product_mode=True,
                product_description=body['desc'], script_language=language,
                video_theme=body['theme'], custom_prompt=body.get('prompt', ''),
                frame_interval_input=3, vision_batch_size=4, vision_llm_provider='openai',
                vision_api_key=config.app.get('vision_openai_api_key'),
                vision_model_name=config.app.get('vision_openai_model_name'),
                vision_base_url=config.app.get('vision_openai_base_url'), max_concurrency=1,
                progress_callback=lambda p, msg: record.update(progress=p, message=msg)))
            obj_demo.save(root / 'script.json', script)
            obj_demo.save(root / 'sources.json', sources)
            shots = [dict(s, video=sources[int(s['video_id'])-1]['name'], description=s.get('picture','')) for s in script]
            return {'shots': shots, 'script_job_id': job_id}
        self.write_json({'task_id': launch_auto_job(work)})


class AutoRenderHandler(BaseHandler):
    def post(self):
        from app.models.schema import VideoClipParams
        from app.services import task as tm
        body = json.loads(self.request.body or b'{}')
        script_id = body.get('script_job_id', '')
        task_dir(script_id)  # validate identifier, even though auto jobs live in TASKS_ROOT
        source_root = TASKS_ROOT / script_id
        if not (source_root / 'script.json').is_file():
            raise tornado.web.HTTPError(400, '请先生成真实脚本')
        script = json.loads((source_root / 'script.json').read_text())
        sources = json.loads((source_root / 'sources.json').read_text())
        edits = body.get('storyboard', [])
        if len(edits) != len(script):
            raise tornado.web.HTTPError(400, '脚本段数不一致，请重新加载')
        for row, edit in zip(script, edits):
            if not isinstance(edit.get('narration'), str):
                raise tornado.web.HTTPError(400, '台词格式错误')
            row['narration'] = edit['narration']
        bgm = str(media_path(body['bgm'])) if body.get('bgm') else ''
        if bgm and Path(bgm).suffix.lower() not in {'.mp3','.wav','.m4a'}:
            raise tornado.web.HTTPError(400, 'BGM 必须是音频')
        voice = body.get('voice_id', '')
        has_tts = bool(voice and config.app.get('elevenlabs_api_key'))
        for row in script:
            row['OST'] = 0 if has_tts else 1
        params = VideoClipParams(video_origin_paths=[s['path'] for s in sources],
            video_origin_path=sources[0]['path'], video_aspect=body.get('aspect','9:16'),
            voice_name=voice, tts_engine='elevenlabs' if has_tts else '',
            bgm_type='custom' if bgm else '', bgm_file=bgm,
            subtitle_enabled=bool(body.get('subtitle_enabled', True)), original_volume=0,
            n_threads=2, subtitle_auto_wrap=True)
        if not RENDER_LOCK.acquire(blocking=False):
            raise tornado.web.HTTPError(409, '已有成片任务运行，请等它完成')
        def work(job_id, root, record):
            try:
                obj_demo.save(root / 'script.json', script)
                params.video_clip_json_path = str(root / 'script.json')
                record.update(message='正在生成配音并合成视频')
                result = tm.start_subclip_unified(job_id, params)
                videos = result.get('videos', []) if result else []
                if not videos or not Path(videos[0]).is_file():
                    raise ValueError('合成未生成成片，请查看任务日志')
                _probe_video(videos[0])
                return {'output_url': '/api/media/' + str(Path(videos[0]).relative_to('/NarratoAI'))}
            finally:
                RENDER_LOCK.release()
        try:
            job_id = launch_auto_job(work)
        except Exception:
            RENDER_LOCK.release()
            raise
        self.write_json({'task_id': job_id})


class AutoStatusHandler(BaseHandler):
    def get(self, task_id):
        task_dir(task_id)
        record = ACTIVE_JOBS.get(task_id)
        saved = TASKS_ROOT / task_id / 'workbench.json'
        if record is None and saved.is_file():
            record = json.loads(saved.read_text())
            if record['status'] == 'running':
                record.update(status='error', message='服务重启导致任务中断，请重新发起')
        self.write_json(record or {'status':'error', 'message':'任务不存在'})


class BgmUploadHandler(BaseHandler):
    def post(self):
        files = self.request.files.get('file', [])
        if len(files) != 1 or Path(files[0]['filename']).suffix.lower() not in {'.mp3','.wav','.m4a'}:
            raise tornado.web.HTTPError(400, '请选择音频文件')
        dest = AUDIO_RESOURCE / (uuid.uuid4().hex[:12] + Path(files[0]['filename']).suffix.lower())
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(files[0]['body'])
        self.write_json({'path':str(dest), 'name':files[0]['filename']})


def get_workbench_rules():
    """Build Tornado Route Rules for insertion at index 0."""
    return [
        tornado.web.Rule(tornado.routing.PathMatches(r"^/$"), WorkbenchHomeHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/state$"), StateHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/bgm/upload$"), BgmUploadHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/tasks$"), ObjectTasksHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/task/(?P<job_id>[^/]+)$"), ObjectTaskDetailHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/analyze$"), ObjectAnalyzeHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/status/(?P<job_id>[^/]+)$"), ObjectStatusHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/export$"), ObjectExportHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/export-status/(?P<job_id>[^/]+)$"), ObjectExportStatusHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/download/(?P<job_id>[^/]+)$"), ObjectDownloadHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/materials/upload$"), MaterialUploadHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/materials/delete$"), MaterialDeleteHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/products$"), GlobalProductsHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/products/upload-ref$"), ProductRefUploadHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/voices$"), VoicesHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/auto/script$"), AutoScriptHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/auto/render$"), AutoRenderHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/auto/status/(?P<task_id>[^/]+)$"), AutoStatusHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/workbench-assets/(?P<path>.*)$"), tornado.web.StaticFileHandler, {"path": "/NarratoAI/workbench-assets"}),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/media/(?P<path>.*)$"), MediaStreamHandler, {"path": "/NarratoAI"}),
    ]


def patch_streamlit_server():
    """Hook Streamlit's Server._create_app to prepend our workbench rules and enlarge upload limit."""
    from streamlit import config as st_config
    try:
        st_config.set_option("server.maxUploadSize", 2048)
    except Exception:
        pass

    from streamlit.web.server.server import Server
    if getattr(Server, "_workbench_patched", False):
        return

    orig_create_app = Server._create_app

    def patched_create_app(self):
        app = orig_create_app(self)
        tornado.ioloop.IOLoop.current().spawn_callback(run_retention_cleanup)
        app._retention_cleanup = tornado.ioloop.PeriodicCallback(run_retention_cleanup, 3600 * 1000)
        app._retention_cleanup.start()
        rules = get_workbench_rules()
        for rule in reversed(rules):
            app.wildcard_router.rules.insert(0, rule)
        return app

    Server._create_app = patched_create_app
    Server._workbench_patched = True


# ==============================================================================
# Approved Workbench Single-Page HTML with responsive data-binding JS
# ==============================================================================
RENDERED_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NarratoAI · 智能剪辑工作台</title>
  <script src="/api/workbench-assets/tailwindcss.min.js?v=1"></script>
  <style>
    /* Same native player as storyboard.html; bound portrait media to the viewport. */
    #theater-modal > .theater-panel { width:min(960px,100%); height:min(760px,calc(100dvh - 48px)); min-height:0; display:flex; flex-direction:column; }
    #theater-modal .theater-header { flex:0 0 auto; min-width:0; }
    #theater-modal .theater-stage { flex:1 1 0; min-height:0; position:relative; background:#000; }
    #theater-video { position:absolute; inset:0; display:block; width:100%; height:100%; object-fit:contain; }
    #theater-sub { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    @media (min-width:1024px) {
      body > header { flex-shrink:0; }
      #page-object { height:calc(100dvh - 56px); min-height:0; flex:none; overflow:hidden; }
      #page-object > section:first-child { max-height:100%; overflow-y:auto; }
      #page-object > section:last-child { height:100%; min-height:0; }
      #page-object > section:last-child > div:first-child { flex-shrink:0; }
      #clips-list-container { flex:1; min-height:0; overflow-y:auto; overscroll-behavior:contain; padding:3px; }
      #clips-list-container > * { flex-shrink:0; }
    }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
    
    .spring-hover {
      transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.22s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.15s ease;
    }
    .spring-hover:hover {
      transform: translateY(-2px);
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05), 0 8px 10px -6px rgba(0, 0, 0, 0.02);
      border-color: #94a3b8;
    }

    @keyframes pulse-ring {
      0% { transform: scale(0.95); opacity: 0.8; }
      50% { transform: scale(1.15); opacity: 0.3; }
      100% { transform: scale(0.95); opacity: 0.8; }
    }
    .play-glow:hover .play-ring {
      animation: pulse-ring 2s infinite ease-in-out;
    }
  </style>
</head>
<body class="bg-[#f8fafc] text-slate-800 antialiased min-h-screen flex flex-col font-sans selection:bg-slate-900 selection:text-white text-sm overflow-x-hidden">

  <!-- 顶栏：洗练通透的站点头部 (56px) -->
  <header class="h-14 border-b border-slate-200/80 bg-white/95 backdrop-blur-md px-6 flex items-center justify-between sticky top-0 z-30 shadow-xs">
    <div class="flex items-center gap-7">
      <div class="flex items-center gap-2.5 cursor-pointer select-none" onclick="location.reload()">
        <div class="w-7 h-7 rounded-lg bg-slate-950 flex items-center justify-center text-white font-bold text-xs tracking-wider shadow-xs">
          N
        </div>
        <div class="flex items-baseline gap-1">
          <span class="font-bold text-sm tracking-tight text-slate-900">Narrato<span class="text-slate-500 font-normal">AI</span></span>
          <span class="text-[11px] text-slate-400 font-mono">Studio</span>
        </div>
      </div>

      <!-- 左上角 Switch 胶囊滑块 -->
      <div class="inline-flex p-1 bg-slate-100 rounded-full border border-slate-200/90 relative">
        <button id="switch-object-btn" onclick="switchMode('object')" class="px-4 py-1.5 rounded-full text-xs font-semibold bg-white text-slate-900 shadow-sm transition-all duration-200 flex items-center gap-1.5 z-10 cursor-pointer">
          <svg class="w-3.5 h-3.5 text-slate-700" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
          <span>物品识别剪辑</span>
        </button>
        <button id="switch-auto-btn" onclick="switchMode('auto')" class="px-4 py-1.5 rounded-full text-xs font-medium text-slate-500 hover:text-slate-900 transition-all duration-200 flex items-center gap-1.5 z-10 cursor-pointer">
          <svg class="w-3.5 h-3.5 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
          <span>全自动视频制作</span>
        </button>
      </div>
    </div>

    <!-- 右侧：侧边悬浮任务抽屉触发器 -->
    <div class="flex items-center gap-4">
      <button onclick="openTaskDrawer()" class="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-slate-200/90 bg-slate-50 hover:bg-slate-100 text-xs font-medium text-slate-800 transition-all cursor-pointer shadow-2xs group">
        <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
        <span class="text-slate-500 font-normal">任务:</span>
        <span id="top-task-name" class="font-mono font-semibold text-slate-900">加载中...</span>
        <svg class="w-3.5 h-3.5 text-slate-400 group-hover:text-slate-700 transition-colors ml-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16m-7 6h7"/>
        </svg>
      </button>

      <div class="h-4 w-px bg-slate-200"></div>

      <div class="flex items-center gap-2 text-xs text-slate-600 font-medium">
        <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
        <span class="font-mono text-xs text-slate-700">DeepSeek Vision</span>
      </div>
    </div>
  </header>

  <!-- 侧边悬浮任务栏 (Slide-Over Task Drawer) -->
  <div id="drawer-backdrop" onclick="closeTaskDrawer()" class="fixed inset-0 bg-slate-950/20 backdrop-blur-xs z-50 hidden opacity-0 transition-opacity duration-300"></div>

  <aside id="task-drawer" class="fixed inset-y-0 right-0 z-50 w-full sm:w-[380px] bg-white border-l border-slate-200/90 shadow-2xl flex flex-col transform translate-x-full transition-transform duration-300 ease-out">
    <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
      <div class="flex items-center gap-2">
        <div class="w-6 h-6 rounded-md bg-slate-100 flex items-center justify-center text-slate-700">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        </div>
        <div>
          <h2 class="font-bold text-sm text-slate-900 tracking-tight">任务历史记录</h2>
          <p class="text-[11px] text-slate-400">选择任务加载切片或恢复断点</p>
        </div>
      </div>
      
      <div class="flex items-center gap-2">
        <button onclick="createNewTask()" title="新建空白识别任务" class="w-7 h-7 rounded-lg border border-slate-200 hover:border-slate-400 hover:bg-slate-50 flex items-center justify-center text-slate-700 transition-colors cursor-pointer shadow-2xs">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M12 4v16m8-8H4"/></svg>
        </button>
        <button onclick="closeTaskDrawer()" class="w-7 h-7 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 flex items-center justify-center transition-colors cursor-pointer">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>
    </div>

    <!-- 任务卡片流容器 -->
    <div id="drawer-task-list" class="flex-1 p-4 overflow-y-auto flex flex-col gap-3">
      <!-- Dynamically filled -->
    </div>

    <div class="p-4 border-t border-slate-100 bg-slate-50/50 flex items-center justify-between text-xs text-slate-500">
      <span class="font-mono text-[11px]">任务存储隔离: 状态已同步</span>
      <span class="text-[11px]">支持断点增量续跑</span>
    </div>
  </aside>

  <!-- ============================================== -->
  <!-- 页面 A：按物品识别剪辑 -->
  <!-- ============================================== -->
  <main id="page-object" class="flex-1 max-w-[1520px] w-full mx-auto p-5 md:p-6 flex flex-col lg:flex-row gap-6 items-start">

    <!-- 左栏：控制面板 -->
    <section class="w-full lg:w-[340px] shrink-0 bg-white border border-slate-200/90 rounded-2xl p-5 shadow-sm flex flex-col gap-5">
      <div>
        <div class="flex items-center justify-between pb-2 border-b border-slate-100">
          <div class="flex items-center gap-2">
            <span class="font-bold text-slate-900 text-sm tracking-tight">目标商品</span>
            <span id="product-count-badge" class="text-xs text-slate-400 font-mono bg-slate-100 px-1.5 py-0.2 rounded">0 个</span>
          </div>
          <button onclick="openProductModal('add')" title="添加商品" class="w-7 h-7 rounded-lg border border-slate-200 hover:border-slate-400 hover:bg-slate-50 flex items-center justify-center text-slate-700 transition-all cursor-pointer shadow-2xs">
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M12 4v16m8-8H4"/></svg>
          </button>
        </div>

        <div id="product-list-container" class="flex flex-col gap-2.5 mt-3">
          <!-- Dynamically filled -->
        </div>
      </div>

      <div>
        <div class="flex items-center justify-between pb-2 border-b border-slate-100">
          <div class="flex items-center gap-2">
            <span class="font-bold text-slate-900 text-sm tracking-tight">待检视频素材</span>
            <span id="material-count-badge" class="text-xs text-slate-400 font-mono bg-slate-100 px-1.5 py-0.2 rounded">0 个</span>
          </div>
          <label title="导入新素材" class="w-7 h-7 rounded-lg border border-slate-200 hover:border-slate-400 hover:bg-slate-50 flex items-center justify-center text-slate-700 cursor-pointer transition-all shadow-2xs">
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M12 4v16m8-8H4"/></svg>
            <input type="file" class="hidden" multiple accept="video/*" onchange="handleMaterialUpload(this)">
          </label>
        </div>

        <div id="material-list-container" class="flex flex-col gap-2 mt-3 max-h-48 overflow-y-auto">
          <!-- Dynamically filled -->
        </div>
      </div>

      <div class="pt-3 border-t border-slate-100 flex flex-col gap-3">
        <div class="flex items-center justify-between">
          <span class="text-slate-600 text-xs font-medium">采样检查间隔</span>
          <div class="inline-flex rounded-lg border border-slate-200 p-0.5 bg-slate-50 text-xs font-mono" id="interval-group">
            <button onclick="setIntervalStep(1.0, this)" class="px-2.5 py-1 text-slate-500 hover:text-slate-900 rounded font-medium">1.0s</button>
            <button onclick="setIntervalStep(0.5, this)" class="px-2.5 py-1 bg-white text-slate-900 font-bold rounded shadow-2xs">0.5s (推荐)</button>
            <button onclick="setIntervalStep(0.25, this)" class="px-2.5 py-1 text-slate-500 hover:text-slate-900 rounded font-medium">0.25s</button>
          </div>
        </div>

        <button onclick="startAnalysis()" id="start-analysis-btn" class="w-full py-2.5 rounded-xl bg-slate-950 hover:bg-slate-800 text-white font-semibold text-xs tracking-wide transition-all shadow-sm flex items-center justify-center gap-2 cursor-pointer active:scale-98">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          <span id="start-analysis-text">开始智能识别切片</span>
        </button>
      </div>
    </section>

    <!-- 右栏：命中检视台 -->
    <section class="flex-1 w-full flex flex-col gap-4 min-w-0">
      <div class="bg-white border border-slate-200/90 rounded-2xl px-5 py-3.5 flex items-center justify-between shadow-xs">
        <div class="flex items-center gap-3">
          <span class="font-bold text-slate-900 text-sm">命中片段检视</span>
          <span id="clips-summary-badge" class="text-xs font-mono font-semibold text-slate-600 bg-slate-100 px-2 py-0.5 rounded-full">0 个切片 · 累计 00.00s</span>
          <div class="flex items-center gap-2 text-xs text-slate-400 ml-3">
            <button onclick="toggleClips(true)" class="hover:text-slate-900 font-medium cursor-pointer">全选</button>
            <span>/</span>
            <button onclick="toggleClips(false)" class="hover:text-slate-900 font-medium cursor-pointer">全不选</button>
          </div>
        </div>

        <button onclick="exportClips()" id="export-clips-btn" class="px-4 py-2 rounded-xl bg-slate-950 hover:bg-slate-800 text-white font-semibold text-xs transition-all shadow-xs flex items-center gap-1.5 cursor-pointer active:scale-98">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
          <span><span id="export-label">导出 ZIP</span> (<span id="export-count">0</span>)</span>
        </button>
      </div>

      <!-- 切片列表容器 -->
      <p id="export-status" role="status" aria-live="polite" class="hidden text-xs text-slate-600 px-2 shrink-0"></p>
      <div id="clips-list-container" class="flex flex-col gap-3.5">
        <!-- Dynamically filled -->
      </div>
    </section>

  </main>

  <!-- ============================================== -->
  <!-- 页面 B：全自动视频制作 -->
  <!-- ============================================== -->
  <main id="page-auto" class="hidden flex-1 max-w-[1520px] w-full mx-auto p-5 md:p-6 flex flex-col lg:flex-row gap-6 items-start">
    
    <!-- 左栏：卖点与合成核心控制台 -->
    <section class="w-full lg:w-[360px] shrink-0 bg-white border border-slate-200/90 rounded-2xl p-5 shadow-sm flex flex-col gap-4">
      <div>
        <span class="font-bold text-slate-900 text-sm tracking-tight block mb-2">商品卖点与口播诉求</span>
        <input type="text" id="auto-theme-input" value="" placeholder="输入视频主题，如：便携挂脖风扇" class="w-full text-xs font-medium border border-slate-200 rounded-xl px-3 py-2 text-slate-800 focus:outline-none focus:border-slate-800">
        <textarea id="auto-prompt-input" rows="3" placeholder="输入商品卖点与解说文案方向，例如：&#10;1. 双涡轮静音大风力，三档随心调节&#10;2. 挂脖免手持设计，运动通勤夏日降温神器" class="w-full text-xs border border-slate-200 rounded-xl p-3 text-slate-700 resize-none leading-relaxed mt-2 focus:outline-none focus:border-slate-800"></textarea>
      </div>

      <!-- 合成音视频关键参数区 -->
      <div class="pt-3 border-t border-slate-100 flex flex-col gap-3 text-xs">
        
        <!-- 1. 成片画幅 -->
        <div class="flex items-center justify-between">
          <span class="text-slate-600 font-medium">画幅比例</span>
          <select id="auto-aspect-select" class="border border-slate-200 rounded-lg px-2.5 py-1 bg-white text-slate-800 font-medium text-xs">
            <option value="9:16">9:16 (竖屏短视频)</option>
            <option value="16:9">16:9 (横屏视频)</option>
          </select>
        </div>

        <!-- 2. TTS 配音音色 + 语音试听 (动态加载真实 ElevenLabs 库) -->
        <div class="flex items-center justify-between">
          <span class="text-slate-600 font-medium">TTS 配音音色</span>
          <div class="flex items-center gap-1.5">
            <select id="auto-voice-select" onchange="onVoiceChange()" class="border border-slate-200 rounded-lg px-2.5 py-1 bg-white text-slate-800 font-medium text-xs w-36 truncate">
              <!-- Dynamically populated from /api/voices -->
            </select>
            <button onclick="toggleVoiceAudition(this)" id="audition-btn" title="点击试听当前音色" class="px-2 py-1 rounded-lg border border-slate-200 hover:border-slate-400 bg-slate-50 hover:bg-slate-100 text-slate-700 font-medium text-xs flex items-center gap-1 transition-colors cursor-pointer">
              <span id="audition-icon">🔊</span>
              <span id="audition-text">试听</span>
            </button>
          </div>
        </div>

        <label class="block">商品描述（必填）<input id="auto-desc-input" class="w-full border rounded p-2" placeholder="说明商品是什么及基本玩法"></label>
        <label>脚本语言 <select id="auto-language"><option>English</option><option>简体中文</option><option>Español</option><option>日本語</option><option>Deutsch</option><option>Français</option></select></label>
        <p class="text-xs text-slate-500">使用当前待检素材列表。没有配音配置时跳过配音。</p>
        <!-- 3. 背景音乐 (BGM) -->
        <div class="flex items-center justify-between">
          <span class="text-slate-600 font-medium">背景音乐 (BGM)</span>
          <div class="flex items-center gap-1.5">
            <select id="auto-bgm-select" class="border border-slate-200 rounded-lg px-2.5 py-1 bg-white text-slate-800 font-medium text-xs w-36 truncate">
              <option>轻快带货节奏.mp3</option>
              <option>欢快活力聚会.mp3</option>
              <option>无背景音 (纯人声)</option>
            </select>
            <label title="上传本地 BGM 文件" class="px-2 py-1 rounded-lg border border-slate-200 hover:border-slate-400 bg-slate-50 hover:bg-slate-100 text-slate-700 font-medium text-xs flex items-center gap-0.5 cursor-pointer transition-colors">
              <span>+ 上传</span>
              <input type="file" accept="audio/*" class="hidden" onchange="handleBgmUpload(this)">
            </label>
          </div>
        </div>

        <!-- 4. 智能双行字幕开启勾选框 -->
        <div class="pt-1 flex items-center justify-between">
          <span class="text-slate-600 font-medium">智能双行字幕</span>
          <label class="inline-flex items-center gap-2 cursor-pointer select-none">
            <input type="checkbox" id="auto-subtitle-check" checked class="w-4 h-4 rounded border-slate-300 text-slate-900 focus:ring-0 cursor-pointer">
            <span class="text-xs text-slate-800 font-medium">开启字幕烧录</span>
          </label>
        </div>

        <button onclick="regenerateScript()" id="regen-script-btn" class="w-full py-2 rounded-xl border border-slate-200 hover:bg-slate-50 text-slate-800 font-semibold text-xs mt-1 transition-colors flex items-center justify-center gap-1.5 cursor-pointer">
          <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
          <span id="regen-script-text">重新生成 AI 脚本</span>
        </button>
      </div>
    </section>

    <!-- 右栏：分镜故事板 -->
    <section class="flex-1 w-full flex flex-col gap-4 min-w-0">
      <div class="bg-white border border-slate-200/90 rounded-2xl px-5 py-3.5 flex items-center justify-between shadow-xs">
        <div class="flex items-center gap-2.5">
          <span class="font-bold text-slate-900 text-sm">分镜脚本</span>
          <span id="storyboard-summary-badge" class="text-xs font-mono text-slate-500 bg-slate-100 px-2 py-0.5 rounded-full">4 段 · 预估 18.0s</span>
        </div>

        <button onclick="startAutoRender()" id="start-render-btn" class="px-5 py-2 rounded-xl bg-slate-950 hover:bg-slate-800 text-white font-semibold text-xs transition-all shadow-xs cursor-pointer active:scale-98 flex items-center gap-2">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
          <span>一键全自动成片 (MP4)</span>
        </button>
      </div>

      <!-- 分镜列表容器 -->
      <div id="storyboard-list-container" class="flex flex-col gap-3.5">
        <!-- Dynamically filled -->
      </div>
    </section>

  </main>

  <!-- ============================================== -->
  <!-- 浮窗全屏影院播放器 (Theater Modal) -->
  <!-- ============================================== -->
  <div id="theater-modal" class="fixed inset-0 bg-slate-950/75 backdrop-blur-md z-50 hidden items-center justify-center p-4 sm:p-6 transition-all">
    <div class="theater-panel bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="theater-title">
      <div class="theater-header px-5 py-3 bg-slate-950/95 border-b border-slate-800 flex items-center justify-between text-white">
        <div class="flex items-center gap-2.5 min-w-0">
          <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
          <span id="theater-title" class="font-semibold text-sm text-slate-100">画面精准回放</span>
          <span id="theater-sub" class="text-xs text-slate-400 font-mono">2026-09-08_原片.MOV</span>
        </div>
        <button aria-label="关闭播放器" onclick="closeTheaterModal()" class="w-7 h-7 shrink-0 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white flex items-center justify-center transition-colors cursor-pointer">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>

      <div class="theater-stage">
        <video id="theater-video" aria-label="视频播放器" controls autoplay playsinline preload="metadata"></video>
      </div>
    </div>
  </div>

  <!-- ============================================== -->
  <!-- 弹窗：商品规则配置 -->
  <!-- ============================================== -->
  <div id="product-modal" class="fixed inset-0 bg-slate-950/45 backdrop-blur-sm z-50 hidden items-center justify-center p-4">
    <div class="bg-white border border-slate-200 rounded-2xl max-w-md w-full p-5 shadow-2xl flex flex-col gap-4 animate-in fade-in zoom-in-95 duration-150">
      <div class="flex items-center justify-between pb-2 border-b border-slate-100">
        <h3 id="modal-product-title" class="font-bold text-slate-900 text-sm">商品规则配置</h3>
        <button onclick="closeProductModal()" class="text-slate-400 hover:text-slate-600 cursor-pointer">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>

      <!-- 全局商品库一键复用栏 -->
      <div id="modal-library-bar" class="hidden flex-col gap-1.5 p-2.5 bg-slate-50 border border-slate-200/80 rounded-xl">
        <div class="flex items-center justify-between text-[11px] text-slate-500 font-medium">
          <span class="flex items-center gap-1">
            <svg class="w-3.5 h-3.5 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
            从已存商品库复用 (点击填入)
          </span>
          <span class="text-[10px] text-slate-400">跨任务长期可用</span>
        </div>
        <div id="modal-library-chips" class="flex flex-wrap gap-1.5"></div>
      </div>

      <div class="flex flex-col gap-3 text-xs">
        <div>
          <label class="block text-slate-700 font-semibold mb-1">商品名称</label>
          <input type="text" id="modal-input-name" class="w-full border border-slate-200 rounded-xl px-3 py-2 text-slate-800 focus:outline-none focus:border-slate-800 text-xs font-medium">
        </div>

        <div>
          <label class="block text-slate-700 font-semibold mb-1">同款判定规则（选填）</label>
          <textarea id="modal-input-desc" rows="3" placeholder="不填则按参考图判断；可补充不同颜色、组合状态是否算同款。" class="w-full border border-slate-200 rounded-xl p-3 text-slate-700 focus:outline-none focus:border-slate-800 resize-none leading-relaxed text-xs"></textarea>
        </div>

        <div>
          <div class="flex items-center justify-between mb-1.5">
            <label class="block text-slate-700 font-semibold">实拍参考图 (支持多选)</label>
            <span id="modal-ref-count" class="text-[11px] text-slate-400 font-mono">0 张</span>
          </div>
          <div class="flex items-center gap-2.5 flex-wrap" id="modal-ref-images">
            <label id="ref-upload-btn" class="w-14 h-14 rounded-xl border-2 border-dashed border-slate-300 hover:border-slate-800 flex flex-col items-center justify-center text-slate-400 hover:text-slate-800 cursor-pointer shrink-0 transition-all hover:bg-slate-50">
              <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
              <span class="text-[9px] font-medium mt-0.5">上传</span>
              <input type="file" accept="image/*" multiple class="hidden" onchange="handleProductRefUpload(this)">
            </label>
          </div>
        </div>

      </div>

      <div class="flex items-center justify-between pt-2 border-t border-slate-100">
        <button type="button" id="modal-delete-btn" onclick="deleteCurrentProduct()" class="hidden px-3 py-2 rounded-xl text-rose-600 hover:bg-rose-50 text-xs font-medium cursor-pointer transition-colors">
          删除商品
        </button>
        <div class="flex items-center gap-2.5 ml-auto">
          <button type="button" onclick="closeProductModal()" class="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 text-xs font-medium cursor-pointer">取消</button>
          <button type="button" onclick="saveProductModal()" class="px-4 py-2 rounded-xl bg-slate-950 hover:bg-slate-800 text-white font-semibold text-xs shadow-xs cursor-pointer">保存并同步商品库</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 动态状态与前后端数据绑定脚本 -->
  <script>
    let appState = {
      currentTask: null,
      globalProducts: [],
      objectTasks: [],
      materials: [],
      scriptJobId: '',
      voices: [],
      autoTasks: [],
      step: 0.5,
      storyboard: []
    };

    let audioPlayer = new Audio();
    let isAuditionPlaying = false;

    // 初始化数据载入
    async function initApp() {
      try {
        const resp = await fetch('/api/state');
        if (!resp.ok) throw new Error('Failed to load state');
        const data = await resp.json();
        appState.currentTask = data.current_task;
        appState.globalProducts = data.global_products || [];
        appState.objectTasks = data.object_tasks || [];
        restoreTaskMaterials();
        appState.voices = data.voices || [];
        appState.autoTasks = data.auto_tasks || [];
        const bgmSelect = document.getElementById('auto-bgm-select');
        bgmSelect.replaceChildren(new Option('无背景音乐', ''));
        (data.bgms || []).forEach(b => bgmSelect.add(new Option(b.name, b.path)));


        renderTopTaskBadge();
        renderProducts();
        renderMaterials();
        renderClips();
        renderTaskDrawer();
        refreshExportStatus();
        renderVoices();
        loadDefaultStoryboard();
      } catch (err) {
        console.error('Init error:', err);
        document.getElementById('top-task-name').innerText = '未连接：' + err.message;
      }
    }

    function switchMode(mode) {
      const pageObj = document.getElementById('page-object');
      const pageAuto = document.getElementById('page-auto');
      const btnObj = document.getElementById('switch-object-btn');
      const btnAuto = document.getElementById('switch-auto-btn');

      if (mode === 'object') {
        pageObj.classList.remove('hidden');
        pageAuto.classList.add('hidden');
        btnObj.className = "px-4 py-1.5 rounded-full text-xs font-semibold bg-white text-slate-900 shadow-sm transition-all duration-200 flex items-center gap-1.5 z-10 cursor-pointer";
        btnAuto.className = "px-4 py-1.5 rounded-full text-xs font-medium text-slate-500 hover:text-slate-900 transition-all duration-200 flex items-center gap-1.5 z-10 cursor-pointer";
      } else {
        pageObj.classList.add('hidden');
        pageAuto.classList.remove('hidden');
        btnAuto.className = "px-4 py-1.5 rounded-full text-xs font-semibold bg-white text-slate-900 shadow-sm transition-all duration-200 flex items-center gap-1.5 z-10 cursor-pointer";
        btnObj.className = "px-4 py-1.5 rounded-full text-xs font-medium text-slate-500 hover:text-slate-900 transition-all duration-200 flex items-center gap-1.5 z-10 cursor-pointer";
      }
    }

    function renderTopTaskBadge() {
      const nameElem = document.getElementById('top-task-name');
      const task = appState.currentTask;
      nameElem.innerText = task && task.status !== 'pending'
        ? (task.products || []).map(p => p.name).filter(Boolean).join(' / ') || '识别任务'
        : '新建任务';
    }

    function restoreTaskMaterials() {
      appState.materials = ((appState.currentTask || {}).sources || []).map(s => ({
        ...s, duration: typeof s.duration === 'number'
          ? Math.floor(s.duration / 60).toString().padStart(2, '0') + ':' + Math.round(s.duration % 60).toString().padStart(2, '0')
          : s.duration || ''
      }));
    }

    function renderProducts() {
      const list = document.getElementById('product-list-container');
      const countBadge = document.getElementById('product-count-badge');
      const products = (appState.currentTask && appState.currentTask.products) ? appState.currentTask.products : [];
      countBadge.innerText = products.length + ' 个';

      if (!products.length) {
        list.innerHTML = `
          <div onclick="openProductModal('add')" class="p-4 rounded-xl border border-dashed border-slate-200 hover:border-slate-400 bg-slate-50/50 hover:bg-slate-50 flex flex-col items-center justify-center text-center cursor-pointer transition-colors group">
            <span class="text-xs text-slate-500 group-hover:text-slate-800 font-medium">+ 点击添加目标商品</span>
          </div>
        `;
        return;
      }

      list.innerHTML = products.map((p, idx) => {
        const hasRefs = p.references && p.references.length > 0;
        const refCount = hasRefs ? p.references.length : 0;
        const thumbUrl = hasRefs ? mediaUrl(p.references[0]) : '';

        return `
          <div onclick="openProductModal('edit', '${p.id}')" class="p-3 rounded-xl border border-slate-200/80 hover:border-slate-400 hover:bg-slate-50/60 bg-slate-50/30 cursor-pointer group transition-all flex items-center justify-between shadow-2xs spring-hover">
            <div class="flex items-center gap-3 min-w-0">
              ${hasRefs ? `
                <div class="w-12 h-12 rounded-lg border border-slate-200 overflow-hidden relative shrink-0 bg-slate-100 shadow-xs">
                  <img src="${thumbUrl}" class="w-full h-full object-cover">
                  <span class="absolute bottom-0 inset-x-0 bg-black/60 text-[9px] text-center text-white font-mono py-0.5">${refCount} 张实拍</span>
                </div>
              ` : `
                <div class="w-12 h-12 rounded-lg bg-slate-900 text-slate-100 flex items-center justify-center font-bold text-xs shrink-0 relative overflow-hidden shadow-xs">
                  ${p.name.slice(0, 2)}
                  <span class="absolute bottom-0 inset-x-0 bg-black/60 text-[9px] text-center text-slate-300 font-mono py-0.2">无图</span>
                </div>
              `}
              <div class="min-w-0">
                <div class="font-bold text-slate-900 text-sm truncate">${p.name}</div>
                <div class="text-xs text-slate-500 truncate mt-0.5">${p.description || '暂无规则描述'}</div>
              </div>
            </div>
            <div class="flex items-center gap-1 shrink-0">
              <button type="button" onclick="removeProductFromTask(event, '${p.id}')" title="从当前任务移除" class="opacity-0 group-hover:opacity-100 w-6 h-6 rounded-lg text-slate-400 hover:text-rose-600 hover:bg-rose-50 flex items-center justify-center text-xs transition-opacity cursor-pointer">✕</button>
              <span class="text-slate-400 group-hover:text-slate-700 text-xs font-semibold p-1">✎</span>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderMaterials() {
      const list = document.getElementById('material-list-container');
      const countBadge = document.getElementById('material-count-badge');
      const mats = appState.materials || [];
      countBadge.innerText = mats.length + ' 个';

      if (!mats.length) {
        list.innerHTML = `
          <label class="p-4 rounded-xl border border-dashed border-slate-200 hover:border-slate-400 bg-slate-50/50 hover:bg-slate-50 flex flex-col items-center justify-center text-center cursor-pointer transition-colors group">
            <span class="text-xs text-slate-500 group-hover:text-slate-800 font-medium">+ 导入本地素材视频</span>
            <input type="file" class="hidden" multiple accept="video/*" onchange="handleMaterialUpload(this)">
          </label>
        `;
        return;
      }

      list.innerHTML = mats.map(m => `
        <div class="group flex items-center justify-between px-3 py-2 rounded-xl bg-slate-50/80 hover:bg-slate-100/90 border border-slate-200/80 hover:border-slate-300 transition-all text-xs">
          <div onclick="openTheaterModal('${m.name}', '${m.duration || ''}', '素材视频预览 · ${m.name}')" class="flex items-center gap-2 min-w-0 flex-1 cursor-pointer" title="点击播放预览素材视频">
            <div class="w-6 h-6 rounded-lg bg-slate-200/80 group-hover:bg-slate-900 group-hover:text-white flex items-center justify-center text-slate-600 transition-colors shrink-0">
              <svg class="w-3 h-3 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            </div>
            <span class="truncate font-mono text-slate-800 font-medium group-hover:text-slate-950">${m.name}</span>
          </div>
          <div class="flex items-center gap-2 shrink-0 ml-2">
            <span class="text-xs text-slate-400 font-mono">${m.duration || '00:08'}</span>
            <button type="button" onclick="deleteMaterial(event, '${m.name}')" title="删除素材" class="opacity-0 group-hover:opacity-100 w-6 h-6 rounded-lg text-slate-400 hover:text-rose-600 hover:bg-rose-50 flex items-center justify-center text-xs transition-opacity cursor-pointer">
              <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
            </button>
          </div>
        </div>
      `).join('');
    }

    function mediaUrl(path) {
      if (path.startsWith('/NarratoAI/')) return '/api/media/' + path.slice('/NarratoAI/'.length).split('/').map(encodeURIComponent).join('/');
      return path;
    }

    async function deleteMaterial(e, name) {
      if (e) e.stopPropagation();
      if (!name) return;
      if (!confirm('确定要删除视频素材“' + name + '”吗？')) return;

      try {
        const resp = await fetch('/api/materials/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name })
        });
        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.error || ('HTTP ' + resp.status));
        }
        const data = await resp.json();
        if (data.materials) {
          const paths = new Set(appState.materials.map(m => m.path));
          appState.materials = data.materials.filter(m => paths.has(m.path));
        } else {
          appState.materials = (appState.materials || []).filter(m => m.name !== name);
        }
        if (appState.currentTask && appState.currentTask.sources) {
          appState.currentTask.sources = appState.currentTask.sources.filter(s => s !== name && (typeof s !== 'object' || s.name !== name));
        }
        renderMaterials();
      } catch (err) {
        alert('删除素材失败: ' + err.message);
      }
    }

    function renderClips() {
      const list = document.getElementById('clips-list-container');
      const summary = document.getElementById('clips-summary-badge');
      const clips = (appState.currentTask && appState.currentTask.clips) ? appState.currentTask.clips : [];
      
      let totalDuration = 0;
      clips.forEach(c => totalDuration += (c.end - c.start));
      summary.innerText = `${clips.length} 个切片 · 累计 ${totalDuration.toFixed(2)}s`;
      document.getElementById('export-count').innerText = clips.filter((c, i) => c.status === 'present' && (!appState.currentTask.export_selected || appState.currentTask.export_selected.includes(i))).length;

      if (!clips.length) {
        list.innerHTML = `
          <div class="bg-white border border-slate-200/90 rounded-2xl p-12 flex flex-col items-center justify-center text-center shadow-xs">
            <div class="w-12 h-12 rounded-full bg-slate-100 flex items-center justify-center text-slate-400 mb-3">
              <svg class="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
            </div>
            <h3 class="text-sm font-semibold text-slate-800">暂无命中切片</h3>
            <p class="text-xs text-slate-400 mt-1 max-w-sm">请在左侧添加目标商品规则与待检视频素材，设置采样间隔后点击【开始智能识别切片】。</p>
          </div>
        `;
        return;
      }


      list.innerHTML = clips.map((c, idx) => {
        const timeSpan = `${c.start.toFixed(2)}s — ${c.end.toFixed(2)}s`;
        const dur = (c.end - c.start).toFixed(2);
        const sourceName = c.source_id || '素材';
        const evidenceStr = (c.evidence && c.evidence[0] && c.evidence[0].reason) ? c.evidence[0].reason : '画面中商品轮廓与实拍参考图结构吻合。';
        const mediaPath = (c.evidence && c.evidence[0] && c.evidence[0].frame) ? c.evidence[0].frame : '';
        const cleanPath = mediaPath ? mediaPath.replace('/NarratoAI/', '').replace('NarratoAI/', '') : '';
        const streamUrl = cleanPath ? ('/api/media/' + (cleanPath.startsWith('/') ? cleanPath.slice(1) : cleanPath)) : '';

        return `
          <div class="bg-white border border-slate-200/90 rounded-2xl p-4 flex items-center justify-between gap-5 spring-hover shadow-xs">
            <div class="flex items-center gap-4 min-w-0 flex-1">
              <input type="checkbox" ${c.status !== 'present' ? 'disabled' : (!appState.currentTask.export_selected || appState.currentTask.export_selected.includes(idx) ? 'checked' : '')} onchange="updateCount()" data-idx="${idx}" class="clip-box w-4 h-4 rounded border-slate-300 text-slate-900 focus:ring-0 cursor-pointer shrink-0">
              <div onclick="previewClip(${idx})"
                   class="w-[120px] h-[68px] rounded-xl bg-slate-950 overflow-hidden relative group cursor-pointer shrink-0 border border-slate-200 shadow-xs flex items-center justify-center play-glow">
                ${mediaPath ? `<img src="${streamUrl}" class="w-full h-full object-cover opacity-85 group-hover:opacity-100 transition-opacity">` : `
                  <div class="w-full h-full bg-gradient-to-tr from-slate-900 via-slate-850 to-slate-800 flex flex-col justify-between p-1.5">
                    <span class="text-[9px] font-mono text-slate-300 bg-black/60 px-1 py-0.2 rounded w-max">#${idx+1}</span>
                    <span class="text-[10px] text-slate-300 font-mono text-right">${dur}s</span>
                  </div>
                `}
                <div class="play-ring absolute w-9 h-9 rounded-full bg-white/20 pointer-events-none"></div>
                <div class="absolute inset-0 bg-black/15 group-hover:bg-black/0 flex items-center justify-center transition-colors">
                  <div class="w-7 h-7 rounded-full bg-white/95 text-slate-950 flex items-center justify-center shadow-md group-hover:scale-110 transition-transform">
                    <svg class="w-3.5 h-3.5 fill-current ml-0.5" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                  </div>
                </div>
              </div>

              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2.5 flex-wrap">
                  <span class="font-mono font-bold text-slate-900 text-sm">${timeSpan}</span>
                  <span class="text-xs font-semibold text-emerald-800 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-md">${c.status === 'present' ? '确认出现' : '不确定，不导出'}</span>
                  <span class="text-xs font-semibold text-slate-800 bg-slate-100 border border-slate-200 px-2 py-0.5 rounded-md font-mono">${c.product_id ? '#' + c.product_id.toUpperCase() : '#P1'}</span>
                  <span class="text-xs text-slate-400 font-mono truncate">${sourceName}</span>
                </div>
                <p class="text-xs text-slate-600 mt-2 leading-relaxed">
                  <strong class="text-slate-800 font-medium">依据：</strong>${evidenceStr}
                </p>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderTaskDrawer() {
      const container = document.getElementById('drawer-task-list');
      const tasks = appState.objectTasks || [];
      const curId = appState.currentTask ? appState.currentTask.id : '';

      if (!tasks.length) {
        container.textContent = '暂无任务记录，开始识别后会自动保存。';
        return;
      }

      container.innerHTML = tasks.map(t => {
        const isCurrent = t.id === curId;
        return `
          <div onclick="selectTask('${t.id}')" class="p-4 rounded-xl border ${isCurrent ? 'border-2 border-slate-900 bg-slate-50/70 shadow-xs' : 'border-slate-200 hover:border-slate-400 bg-white hover:bg-slate-50/40 shadow-2xs'} relative cursor-pointer group transition-all">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-2 h-2 rounded-full ${t.status === 'complete' ? 'bg-emerald-500' : 'bg-amber-500'}"></span>
                <span class="font-bold text-xs text-slate-900">${String(t.title || '识别任务').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}</span>
              </div>
              <span class="text-[10px] font-semibold text-emerald-800 bg-emerald-100/80 px-2 py-0.5 rounded-full">${t.clips_count} 切片</span>
            </div>
            <div class="mt-2.5 flex flex-col gap-1">
              <div class="text-xs font-semibold text-slate-900">目标：${t.title}</div>
              <div class="text-[11px] text-slate-400 font-mono mt-1">${t.time}</div>
            </div>
            <div class="mt-3 pt-2.5 border-t border-slate-200/80 flex items-center justify-between text-xs">
              <span class="${isCurrent ? 'text-slate-900 font-semibold' : 'text-slate-500 group-hover:text-slate-900'}">${isCurrent ? '✓ 当前正在查看' : '点击加载此任务 ❯'}</span>
              ${t.has_zip ? '<span class="text-[11px] font-mono text-slate-400">已打包 ZIP</span>' : ''}
            </div>
          </div>
        `;
      }).join('');
    }

    function renderVoices() {
      const select = document.getElementById('auto-voice-select');
      const voices = appState.voices || [];
      if (!voices.length) return;

      select.innerHTML = voices.map(v => {
        const labelStr = v.labels ? ` (${v.labels.gender || ''} ${v.labels.accent || ''})`.trim() : '';
        return `<option value="${v.voice_id}">${v.name}${labelStr}</option>`;
      }).join('');
    }

    function onVoiceChange() {
      if (isAuditionPlaying) {
        audioPlayer.pause();
        isAuditionPlaying = false;
        resetAuditionBtn();
      }
    }

    function toggleVoiceAudition(btn) {
      const voiceId = document.getElementById('auto-voice-select').value;
      const voiceObj = appState.voices.find(v => v.voice_id === voiceId);

      if (!isAuditionPlaying) {
        const previewUrl = (voiceObj && voiceObj.preview_url) ? voiceObj.preview_url : 'https://storage.googleapis.com/eleven-public-prod/previews/voices/pNInz6obpgDQGcFmaJgB/audio.mp3';
        audioPlayer.src = previewUrl;
        audioPlayer.play().then(() => {
          isAuditionPlaying = true;
          btn.classList.add('bg-blue-50', 'border-blue-300', 'text-blue-700');
          document.getElementById('audition-icon').innerText = '⏸';
          document.getElementById('audition-text').innerText = '播放中';
        }).catch(err => {
          console.warn('Audio play failed:', err);
        });

        audioPlayer.onended = () => {
          isAuditionPlaying = false;
          resetAuditionBtn();
        };
      } else {
        audioPlayer.pause();
        isAuditionPlaying = false;
        resetAuditionBtn();
      }
    }

    function resetAuditionBtn() {
      const btn = document.getElementById('audition-btn');
      btn.classList.remove('bg-blue-50', 'border-blue-300', 'text-blue-700');
      document.getElementById('audition-icon').innerText = '🔊';
      document.getElementById('audition-text').innerText = '试听';
    }

    async function selectTask(taskId) {
      try {
        const resp = await fetch('/api/object/task/' + taskId);
        if (!resp.ok) throw new Error('Failed to load task');
        appState.currentTask = await resp.json();
        restoreTaskMaterials();
        renderMaterials();
        renderTopTaskBadge();
        renderProducts();
        renderClips();
        renderTaskDrawer();
        refreshExportStatus();
        closeTaskDrawer();
      } catch (e) {
        alert('加载任务失败：' + e.message);
      }
    }

    function createNewTask() {
      const newId = 'task-' + Math.random().toString(36).substring(2, 10);
      appState.materials = [];
      clearTimeout(exportPoll);
      showExportState({status: 'idle'});
      appState.currentTask = {
        id: newId,
        status: 'pending',
        products: [],
        sources: [],
        clips: [],
        step: 0.5
      };
      renderTopTaskBadge();
      renderMaterials();
      renderProducts();
      renderClips();
      closeTaskDrawer();
    }

    function setIntervalStep(step, btn) {
      appState.step = step;
      const group = document.getElementById('interval-group');
      group.querySelectorAll('button').forEach(b => {
        b.className = 'px-2.5 py-1 text-slate-500 hover:text-slate-900 rounded font-medium';
      });
      btn.className = 'px-2.5 py-1 bg-white text-slate-900 font-bold rounded shadow-2xs';
    }

    async function startAnalysis() {
      const btn = document.getElementById('start-analysis-btn');
      const txt = document.getElementById('start-analysis-text');
      txt.innerText = 'AI 扫描比对中...';
      btn.classList.add('opacity-75');
      btn.style.pointerEvents = 'none';

      try {
        const resp = await fetch('/api/object/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: appState.currentTask ? appState.currentTask.id : '',
            step: appState.step,
            sources: appState.materials.map(m => m.path),
            products: appState.currentTask ? appState.currentTask.products : []
          })
        });
        if (!resp.ok) {
          const errText = await resp.text();
          let msg = '服务响应异常 (' + resp.status + ')';
          try {
            const errObj = JSON.parse(errText);
            if (errObj.error) msg = errObj.error;
          } catch (_) {}
          throw new Error(msg);
        }
        const res = await resp.json();
        const activeJobId = res.job_id || (appState.currentTask ? appState.currentTask.id : '');
        if (appState.currentTask && res.job_id) {
          appState.currentTask.id = res.job_id;
          appState.currentTask.status = 'running';
          renderTopTaskBadge();
        }

        const pollInterval = setInterval(async () => {
          try {
            const sResp = await fetch('/api/object/status/' + encodeURIComponent(activeJobId));
            if (!sResp.ok) return;
            const sData = await sResp.json();
            if (sData.progress) {
              txt.innerText = sData.progress;
            }
            if (sData.status === 'complete' || sData.status === 'review') {
              clearInterval(pollInterval);
              txt.innerText = '开始智能识别切片';
              btn.classList.remove('opacity-75');
              btn.style.pointerEvents = 'auto';
              await selectTask(activeJobId);
              const clipsCount = (sData.clips && sData.clips.length) ? sData.clips.length : 0;
              alert('识别切片已完成！共发现 ' + clipsCount + ' 个目标命中片段');
            } else if (sData.status === 'error' || sData.status === 'failed') {
              clearInterval(pollInterval);
              txt.innerText = '开始智能识别切片';
              btn.classList.remove('opacity-75');
              btn.style.pointerEvents = 'auto';
              alert('识别分析失败：' + (sData.message || sData.error || '未知错误'));
            }
          } catch (_) {}
        }, 1500);
      } catch (e) {
        txt.innerText = '开始智能识别切片';
        btn.classList.remove('opacity-75');
        btn.style.pointerEvents = 'auto';
        alert('启动分析失败：' + e.message);
      }
    }

    let exportPoll;
    let lastExportState = {status:'idle'};
    function showExportState(data) {
      lastExportState = data;
      const btn = document.getElementById('export-clips-btn');
      const status = document.getElementById('export-status');
      btn.disabled = data.status === 'running';
      document.getElementById('export-label').textContent = btn.disabled ? '后台导出中' : '导出 ZIP';
      status.classList.remove('hidden');
      status.textContent = data.status === 'idle' ? '任务、视频素材及导出结果保留 7 天，商品和参考图长期保留。' : (data.message || '') + (btn.disabled ? ' · 可刷新或切换任务，后台继续处理。' : '');
      const selected = Array.from(document.querySelectorAll('.clip-box:checked')).map(b => Number(b.getAttribute('data-idx'))).sort((a,b) => a-b);
      const matches = JSON.stringify(selected) === JSON.stringify(data.selected || []);
      if (data.status === 'complete' && !matches) status.textContent = '勾选已变化，请重新导出；成功后将替换旧压缩包。';
      if (data.download_url && matches) {
        const link = document.createElement('a');
        link.href = data.download_url;
        link.textContent = '下载 ZIP';
        link.className = 'inline-block rounded-lg bg-slate-900 text-white px-3 py-2 ml-2';
        status.appendChild(link);
      }
    }

    async function refreshExportStatus() {
      clearTimeout(exportPoll);
      const id = appState.currentTask && appState.currentTask.id;
      if (!id || appState.currentTask.status === 'pending') return showExportState({status:'idle'});
      try {
        const resp = await fetch('/api/object/export-status/' + encodeURIComponent(id));
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (id !== appState.currentTask.id) return;
        showExportState(data);
        if (data.status === 'running') exportPoll = setTimeout(refreshExportStatus, 2000);
      } catch (e) {
        if (id !== appState.currentTask.id) return;
        showExportState({status:'failed', message:'暂时无法获取导出进度，正在重试：' + e.message});
        exportPoll = setTimeout(refreshExportStatus, 5000);
      }
    }

    async function exportClips() {
      const btn = document.getElementById('export-clips-btn');
      if (btn.disabled) return;
      const boxes = document.querySelectorAll('.clip-box:checked');
      if (!boxes.length) {
        alert('请至少勾选一个切片');
        return;
      }
      const selected = Array.from(boxes).map(b => parseInt(b.getAttribute('data-idx')));
      const id = appState.currentTask.id;
      showExportState({status:'running', message:'正在提交后台导出'});
      try {
        const resp = await fetch('/api/object/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: id,
            selected_indices: selected
          })
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || '导出提交失败');
        if (id !== appState.currentTask.id) return;
        showExportState(data);
        if (data.status === 'running') exportPoll = setTimeout(refreshExportStatus, 2000);
      } catch (e) {
        if (id === appState.currentTask.id) showExportState({status:'failed', message:'导出失败：' + e.message});
      }
    }

    function toggleClips(val) {
      document.querySelectorAll('.clip-box:not(:disabled)').forEach(b => b.checked = val);
      updateCount();
    }

    function updateCount() {
      const count = document.querySelectorAll('.clip-box:checked').length;
      document.getElementById('export-count').innerText = count;
      showExportState(lastExportState);
    }

    // Theater Modal
    function previewClip(index) {
      const c = appState.currentTask.clips[index];
      const source = appState.currentTask.sources.find(s => s.id === c.source_id);
      if (!source) return alert('找不到片段素材');
      openTheaterModal('/api/media/' + source.path.replace('/NarratoAI/', ''), '', '片段预览', c.start, c.end);
    }

    function openTheaterModal(source, time, title, start = 0, end = null) {
      document.getElementById('theater-title').innerText = title || '画面精准回放';
      document.getElementById('theater-sub').innerText = source + (time ? ' (' + time + ')' : '');
      const videoElem = document.getElementById('theater-video');
      let videoSrc = '';
      if (source && (source.startsWith('/api/media/') || source.startsWith('http'))) {
        videoSrc = source;
      } else {
        const fname = (source || '').split('/').pop().split(String.fromCharCode(92)).pop();
        videoSrc = '/api/media/resource/videos/' + encodeURIComponent(fname);
      }
      videoElem.onloadedmetadata = () => { videoElem.currentTime = start; };
      videoElem.ontimeupdate = () => { if (end !== null && videoElem.currentTime >= end) videoElem.pause(); };
      videoElem.src = videoSrc;
      videoElem.play().catch(() => {});

      const modal = document.getElementById('theater-modal');
      modal.classList.remove('hidden');
      modal.classList.add('flex');
    }

    function closeTheaterModal() {
      const modal = document.getElementById('theater-modal');
      const videoElem = document.getElementById('theater-video');
      videoElem.pause();
      videoElem.removeAttribute('src');
      videoElem.load();
      modal.classList.add('hidden');
      modal.classList.remove('flex');
    }

    // Product Modal
    let editingProductId = null;
    let modalUploadedRefs = [];

    function openProductModal(mode, id) {
      editingProductId = id;
      const modal = document.getElementById('product-modal');
      const title = document.getElementById('modal-product-title');
      const nameInput = document.getElementById('modal-input-name');
      const descInput = document.getElementById('modal-input-desc');
      const libBar = document.getElementById('modal-library-bar');
      const delBtn = document.getElementById('modal-delete-btn');

      if (mode === 'add') {
        title.innerText = '新增目标商品';
        nameInput.value = '';
        descInput.value = '';
        modalUploadedRefs = [];
        if (delBtn) delBtn.classList.add('hidden');
        renderLibraryBar();
      } else {
        if (libBar) libBar.classList.add('hidden');
        if (delBtn) delBtn.classList.remove('hidden');
        let p = (appState.currentTask && appState.currentTask.products) ? appState.currentTask.products.find(x => x.id === id) : null;
        if (!p && appState.globalProducts) {
          p = appState.globalProducts.find(x => x.id === id);
        }
        if (p) {
          title.innerText = '设置商品 · ' + p.name;
          nameInput.value = p.name;
          descInput.value = p.description || '';
          modalUploadedRefs = (p.references || []).slice();
        }
      }

      renderModalRefImages();
      modal.classList.remove('hidden');
      modal.classList.add('flex');
    }

    function renderLibraryBar() {
      const libBar = document.getElementById('modal-library-bar');
      const chipsContainer = document.getElementById('modal-library-chips');
      if (!libBar || !chipsContainer) return;
      const prods = appState.globalProducts || [];
      if (!prods.length) {
        libBar.classList.add('hidden');
        return;
      }
      libBar.classList.remove('hidden');
      chipsContainer.innerHTML = prods.map((gp, idx) => {
        const count = (gp.references && gp.references.length) ? gp.references.length : 0;
        return `
          <button type="button" onclick="autofillFromGlobal(${idx})" class="px-2.5 py-1 rounded-lg bg-white border border-slate-200 hover:border-slate-800 hover:text-slate-900 text-slate-700 text-[11px] font-medium transition-all shadow-2xs cursor-pointer flex items-center gap-1">
            <span class="text-indigo-500">✨</span>
            <span>${gp.name}</span>
            <span class="text-[10px] text-slate-400">(${count}图)</span>
          </button>
        `;
      }).join('');
    }

    function autofillFromGlobal(idx) {
      const gp = appState.globalProducts ? appState.globalProducts[idx] : null;
      if (!gp) return;
      document.getElementById('modal-input-name').value = gp.name || '';
      document.getElementById('modal-input-desc').value = gp.description || '';
      modalUploadedRefs = (gp.references || []).slice();
      renderModalRefImages();
    }

    function removeProductFromTask(e, id) {
      if (e) e.stopPropagation();
      if (!appState.currentTask || !appState.currentTask.products) return;
      appState.currentTask.products = appState.currentTask.products.filter(p => p.id !== id);
      renderProducts();
    }

    async function deleteCurrentProduct() {
      if (!editingProductId) return;
      const name = document.getElementById('modal-input-name').value.trim();
      if (!confirm('确定要从全局商品库中彻底删除商品“' + (name || '此商品') + '”吗？')) return;
      try {
        const resp = await fetch('/api/products', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'delete', id: editingProductId })
        });
        if (resp.ok) {
          const data = await resp.json();
          appState.globalProducts = data.products || [];
        }
      } catch (err) {
        console.error('Delete product error:', err);
      }
      if (appState.currentTask && appState.currentTask.products) {
        appState.currentTask.products = appState.currentTask.products.filter(p => p.id !== editingProductId);
      }
      closeProductModal();
      renderProducts();
    }

    function renderModalRefImages() {
      const container = document.getElementById('modal-ref-images');
      const countLabel = document.getElementById('modal-ref-count');
      if (countLabel) {
        countLabel.innerText = `${modalUploadedRefs.length} 张`;
      }
      container.innerHTML = modalUploadedRefs.map((r, i) => `
        <div class="w-14 h-14 rounded-xl border border-slate-200 overflow-hidden relative group shrink-0 shadow-2xs">
          <img src="${mediaUrl(r)}" class="w-full h-full object-cover">
          <button type="button" onclick="modalUploadedRefs.splice(${i}, 1); renderModalRefImages();" class="absolute inset-0 bg-black/60 text-white opacity-0 group-hover:opacity-100 flex items-center justify-center text-xs font-bold transition-opacity cursor-pointer">✕</button>
        </div>
      `).join('') + `
        <label id="ref-upload-btn" class="w-14 h-14 rounded-xl border-2 border-dashed border-slate-300 hover:border-slate-800 flex flex-col items-center justify-center text-slate-400 hover:text-slate-800 cursor-pointer shrink-0 transition-all hover:bg-slate-50">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
          <span class="text-[9px] font-medium mt-0.5">上传</span>
          <input type="file" accept="image/*" multiple class="hidden" onchange="handleProductRefUpload(this)">
        </label>
      `;
    }

    async function handleProductRefUpload(input) {
      if (!input.files || !input.files.length) return;
      const files = Array.from(input.files);
      const formData = new FormData();
      for (const f of files) {
        formData.append('files', f);
      }
      input.value = '';

      const btn = document.getElementById('ref-upload-btn');
      if (btn) {
        btn.innerHTML = `<span class="w-4 h-4 border-2 border-slate-400 border-t-slate-800 rounded-full animate-spin"></span>`;
        btn.style.pointerEvents = 'none';
      }

      try {
        const resp = await fetch('/api/products/upload-ref', {
          method: 'POST',
          body: formData
        });
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(`HTTP ${resp.status}: ${text.slice(0, 80)}`);
        }
        const res = await resp.json();
        if (res.urls && res.urls.length) {
          modalUploadedRefs.push(...res.urls);
        } else if (res.url) {
          modalUploadedRefs.push(res.url);
        } else if (res.error) {
          alert('上传失败: ' + res.error);
        }
      } catch (e) {
        alert('上传参考图失败: ' + e.message);
      } finally {
        renderModalRefImages();
      }
    }

    function closeProductModal() {
      const modal = document.getElementById('product-modal');
      modal.classList.add('hidden');
      modal.classList.remove('flex');
    }

    async function saveProductModal() {
      const name = document.getElementById('modal-input-name').value.trim();
      const desc = document.getElementById('modal-input-desc').value.trim();
      if (!name) {
        alert('请输入商品名称');
        return;
      }

      if (!appState.currentTask) {
        appState.currentTask = { id: '空白任务', status: 'pending', products: [], sources: [], clips: [], step: 0.5 };
      }
      if (!appState.currentTask.products) appState.currentTask.products = [];

      const targetId = editingProductId || ('p_' + Math.random().toString(36).substring(2, 10));
      const productPayload = {
        id: targetId,
        name: name,
        description: desc,
        references: modalUploadedRefs
      };

      if (editingProductId) {
        const p = appState.currentTask.products.find(x => x.id === editingProductId);
        if (p) {
          p.name = name;
          p.description = desc;
          p.references = modalUploadedRefs;
        } else {
          appState.currentTask.products.push(productPayload);
        }
      } else {
        appState.currentTask.products.push(productPayload);
      }

      // Persist to global library
      try {
        const resp = await fetch('/api/products', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'save', product: productPayload })
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data.products) {
            appState.globalProducts = data.products;
          }
        }
      } catch (err) {
        console.error('Save global product error:', err);
      }

      renderProducts();
      closeProductModal();
    }

    // Auto Workflow & Storyboards
    function loadDefaultStoryboard() {
      appState.storyboard = [];
      renderStoryboard();
    }

    async function pollAuto(taskId, onProgress) {
      while (true) {
        const resp = await fetch('/api/auto/status/' + encodeURIComponent(taskId));
        const data = await resp.json();
        if (!resp.ok || data.status === 'error') throw new Error(data.error || data.message || '处理失败');
        onProgress(data.message || '正在处理');
        if (data.status === 'complete') return data;
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
    }

    async function regenerateScript() {
      const theme = document.getElementById('auto-theme-input').value.trim();
      const prompt = document.getElementById('auto-prompt-input').value.trim();
      if (!theme) {
        alert('请先填写视频主题（如：便携挂脖风扇）');
        return;
      }
      const txt = document.getElementById('regen-script-text');
      txt.innerText = 'AI 分镜编排中...';

      try {
        const resp = await fetch('/api/auto/script', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ theme, prompt, desc: document.getElementById('auto-desc-input').value, language: document.getElementById('auto-language').value, sources: appState.materials.map(m => m.path) })
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || '生成失败');
        const result = await pollAuto(data.task_id, msg => txt.innerText = msg);
        appState.scriptJobId = result.script_job_id;
        appState.storyboard = result.shots || [];
        renderStoryboard();
      } catch (e) {
        alert('脚本生成失败：' + e.message);
      } finally {
        txt.innerText = '重新生成 AI 脚本';
      }
    }

    function renderStoryboard() {
      const list = document.getElementById('storyboard-list-container');
      const badge = document.getElementById('storyboard-summary-badge');
      const shots = appState.storyboard || [];
      badge.innerText = `${shots.length} 段 · 按实际配音对齐`;

      if (!shots.length) {
        list.innerHTML = `
          <div class="bg-white border border-slate-200/90 rounded-2xl p-12 flex flex-col items-center justify-center text-center shadow-xs">
            <div class="w-12 h-12 rounded-full bg-slate-100 flex items-center justify-center text-slate-400 mb-3">
              <svg class="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
            </div>
            <h3 class="text-sm font-semibold text-slate-800">暂无分镜脚本</h3>
            <p class="text-xs text-slate-400 mt-1 max-w-sm">请在左侧填写商品主题与卖点诉求，点击【重新生成 AI 脚本】编排分镜故事板。</p>
          </div>
        `;
        return;
      }

      list.innerHTML = shots.map((s, idx) => `
        <div class="bg-white border border-slate-200/90 rounded-2xl p-4 flex items-center justify-between gap-5 spring-hover shadow-xs">
          <div class="flex items-start gap-3.5 min-w-0 flex-1">
            <span class="font-mono text-slate-400 font-bold text-sm mt-0.5">${(idx+1).toString().padStart(2, '0')}</span>

            <div class="flex flex-col gap-1.5 min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="font-mono font-bold text-slate-900 text-xs">${s.timestamp}</span>
                <span class="text-xs text-slate-400 font-mono truncate">素材: ${s.video}</span>
              </div>

              <input type="text" onchange="appState.storyboard[${idx}].narration = this.value" value="${s.narration.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;')}" class="w-full text-sm font-medium border-b border-transparent hover:border-slate-300 focus:border-slate-800 text-slate-800 py-1.5 transition-colors focus:outline-none bg-transparent">
              
              <div class="text-xs text-slate-400 truncate">
                场景：${s.description}
              </div>
            </div>
          </div>

          <div onclick="openTheaterModal('${s.video}', '${s.timestamp}', '分镜 ${(idx+1).toString().padStart(2, '0')}')"
               class="w-[120px] h-[68px] rounded-xl bg-slate-950 overflow-hidden relative group cursor-pointer shrink-0 border border-slate-200 shadow-xs flex items-center justify-center play-glow">
            
            <div class="w-full h-full bg-gradient-to-tr from-slate-900 via-slate-850 to-slate-800 flex items-center justify-center text-[10px] text-slate-400 font-mono">
              ${s.timestamp.slice(0, 5)}
            </div>
            
            <div class="play-ring absolute w-9 h-9 rounded-full bg-white/20 pointer-events-none"></div>
            <div class="absolute inset-0 bg-black/15 group-hover:bg-black/0 flex items-center justify-center transition-colors">
              <div class="w-7 h-7 rounded-full bg-white/95 text-slate-950 flex items-center justify-center shadow-md group-hover:scale-110 transition-transform">
                <svg class="w-3.5 h-3.5 fill-current ml-0.5" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
              </div>
            </div>
          </div>
        </div>
      `).join('');
    }

    async function startAutoRender() {
      const btn = document.getElementById('start-render-btn');
      btn.disabled = true;
      btn.classList.add('opacity-75');

      try {
        const resp = await fetch('/api/auto/render', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            aspect: document.getElementById('auto-aspect-select').value,
            voice_id: document.getElementById('auto-voice-select').value,
            subtitle_enabled: document.getElementById('auto-subtitle-check').checked,
            storyboard: appState.storyboard,
            script_job_id: appState.scriptJobId,
            bgm: document.getElementById('auto-bgm-select').value
          })
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || '启动失败');
        const result = await pollAuto(data.task_id, msg => btn.innerText = msg);
        openTheaterModal(result.output_url, '', '生成的成片');
        btn.innerText = '一键全自动成片 (MP4)';
        btn.disabled = false;
        btn.classList.remove('opacity-75');

      } catch (e) {
        btn.disabled = false;
        btn.classList.remove('opacity-75');
        alert('成片启动失败：' + e.message);
      }
    }

    // Task Drawer
    async function openTaskDrawer() {
      const drawer = document.getElementById('task-drawer');
      const backdrop = document.getElementById('drawer-backdrop');
      backdrop.classList.remove('hidden');
      setTimeout(() => backdrop.classList.remove('opacity-0'), 10);
      drawer.classList.remove('translate-x-full');
      const list = document.getElementById('drawer-task-list');
      list.textContent = '正在加载任务记录...';
      try {
        const resp = await fetch('/api/state', { cache: 'no-store' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        appState.objectTasks = data.object_tasks || [];
        renderTaskDrawer();
      } catch (err) {
        list.textContent = '任务记录加载失败，请关闭后重试：' + err.message;
      }
    }

    function closeTaskDrawer() {
      const drawer = document.getElementById('task-drawer');
      const backdrop = document.getElementById('drawer-backdrop');
      backdrop.classList.add('opacity-0');
      drawer.classList.add('translate-x-full');
      setTimeout(() => backdrop.classList.add('hidden'), 300);
    }

    async function handleMaterialUpload(input) {
      if (!input.files || !input.files.length) return;
      const files = Array.from(input.files);
      input.value = '';

      const badge = document.getElementById('material-count-badge');
      const origText = badge ? badge.innerText : '';

      let successCount = 0;
      let errors = [];

      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        if (badge) {
          badge.innerText = `上传中 (${i + 1}/${files.length})...`;
        }

        const formData = new FormData();
        formData.append('file', file);

        try {
          const resp = await fetch('/api/materials/upload', {
            method: 'POST',
            body: formData
          });
          if (!resp.ok) {
            const text = await resp.text();
            throw new Error(`HTTP ${resp.status}: ${text.slice(0, 60)}`);
          }
          const data = await resp.json();
          if (data.status === 'success') {
            appState.materials.push({path: data.path, name: data.path.split('/').pop(), duration: ''});
            successCount++;
          } else {
            throw new Error(data.error || '上传异常');
          }
        } catch (e) {
          console.error('Upload error for', file.name, e);
          errors.push(`${file.name}: ${e.message}`);
        }
      }

      if (badge) {
        badge.innerText = origText;
      }

      try {
        const sResp = await fetch('/api/state');
        const sData = await sResp.json();
        const paths = new Set(appState.materials.map(m => m.path));
        appState.materials = (sData.materials || []).filter(m => paths.has(m.path));
        renderMaterials();
      } catch (e) {}

      if (errors.length) {
        alert(`导入素材完成 (${successCount} 成功, ${errors.length} 失败)：` + String.fromCharCode(10) + errors.join(String.fromCharCode(10)));
      } else if (successCount > 0) {
        alert(`已成功导入 ${successCount} 个素材视频！`);
      }
    }

    async function handleBgmUpload(input) {
      if (!input.files.length) return;
      try {
        const form = new FormData(); form.append('file', input.files[0]);
        const resp = await fetch('/api/bgm/upload', {method:'POST', body:form});
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || '上传失败');
        const option = new Option(data.name, data.path, true, true);
        document.getElementById('auto-bgm-select').add(option);
      } catch (e) { alert(e.message); }
    }


    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeTheaterModal();
        closeProductModal();
        closeTaskDrawer();
      }
    });

    document.getElementById('theater-modal').addEventListener('click', (e) => {
      if (e.target.id === 'theater-modal') {
        closeTheaterModal();
      }
    });

    // Boot
    window.addEventListener('DOMContentLoaded', initApp);
  </script>
</body>
</html>
'''
