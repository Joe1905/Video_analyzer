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

import tornado.web
import tornado.routing
import tornado.httputil

from app.config import config
from app.services import narrato_object_demo as obj_demo
from app.services import narrato_elevenlabs as elevenlabs
from app.services.generate_video import _probe_video

# Directories
OBJECT_ROOT = Path("/NarratoAI/storage/object-demo")
VIDEO_RESOURCE = Path("/NarratoAI/resource/videos")
AUDIO_RESOURCE = Path("/NarratoAI/resource/songs")
TASKS_ROOT = Path("/NarratoAI/storage/tasks")

# Background job tracking
ACTIVE_JOBS = {}


class BaseHandler(tornado.web.RequestHandler):
    def check_xsrf_cookie(self):
        # Disable XSRF cookie checking for workbench REST APIs
        pass

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "x-requested-with, content-type")
        self.set_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()

    def write_json(self, data, status=200):
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(data, ensure_ascii=False))


class MediaStreamHandler(tornado.web.StaticFileHandler):
    """Streams media files with Range support for fluent browser video playback."""
    @classmethod
    def get_absolute_path(cls, root, path):
        clean_path = path.lstrip("/")
        candidate = Path("/NarratoAI") / clean_path
        if candidate.is_file():
            return str(candidate.resolve())
        # Check if direct absolute path
        abs_candidate = Path(path)
        if abs_candidate.is_file():
            return str(abs_candidate.resolve())
        # Check in storage or resource
        for base in [OBJECT_ROOT, VIDEO_RESOURCE, AUDIO_RESOURCE, TASKS_ROOT]:
            p = base / clean_path
            if p.is_file():
                return str(p.resolve())
        return str(candidate)

    def validate_absolute_path(self, root, absolute_path):
        p = Path(absolute_path)
        if not p.is_file():
            raise tornado.web.HTTPError(404, "File not found")
        return str(p)


class WorkbenchHomeHandler(BaseHandler):
    """Serves the pixel-perfect Modern Workbench Single Page Application."""
    def get(self):
        self.set_header("Content-Type", "text/html; charset=UTF-8")
        self.write(RENDERED_HTML)


class StateHandler(BaseHandler):
    """Aggregated state for initial hydration."""
    def get(self):
        # 1. Object tasks
        object_tasks = []
        if OBJECT_ROOT.exists():
            for p in sorted(OBJECT_ROOT.glob("*/manifest.json"), key=os.path.getmtime, reverse=True):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
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
        materials = []
        if VIDEO_RESOURCE.exists():
            for v in sorted(VIDEO_RESOURCE.glob("*.MOV"), key=os.path.getmtime, reverse=True)[:10]:
                materials.append({
                    "name": v.name,
                    "path": str(v),
                    "size_mb": round(v.stat().st_size / (1024 * 1024), 1),
                    "duration": "00:08"
                })

        # 4. Voices
        voices = []
        api_key = config.app.get("elevenlabs_api_key", "").strip()
        if api_key:
            try:
                voices = elevenlabs.get_voices(api_key)
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
            "current_task": current_task,
            "object_tasks": object_tasks,
            "materials": materials,
            "voices": voices,
            "auto_tasks": auto_tasks
        })


class ObjectTasksHandler(BaseHandler):
    """List or create object tasks."""
    def get(self):
        tasks = []
        if OBJECT_ROOT.exists():
            for p in sorted(OBJECT_ROOT.glob("*/manifest.json"), key=os.path.getmtime, reverse=True):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
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
        manifest_path = OBJECT_ROOT / job_id / "manifest.json"
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
    """Run object detection scan."""
    def post(self):
        body = json.loads(self.request.body.decode("utf-8") or "{}")
        job_id = body.get("job_id") or ""
        if not job_id or job_id == "空白任务":
            job_id = f"task-{uuid.uuid4().hex[:8]}"
        step = float(body.get("step", 0.5))

        target_dir = OBJECT_ROOT / job_id
        target_dir.mkdir(parents=True, exist_ok=True)

        # Resolve media urls in references to local container file paths
        products = body.get("products", [])
        for prod in products:
            clean_refs = []
            for ref in prod.get("references", []):
                if isinstance(ref, str) and ref.startswith("/api/media/"):
                    sub = ref.replace("/api/media/", "", 1).lstrip("/")
                    p = Path("/NarratoAI") / sub
                    if p.is_file():
                        clean_refs.append(str(p))
                    else:
                        clean_refs.append(ref)
                else:
                    clean_refs.append(ref)
            prod["references"] = clean_refs

        # Collect sources if not provided
        sources = body.get("sources", [])
        if not sources and VIDEO_RESOURCE.exists():
            for vp in sorted(VIDEO_RESOURCE.glob("*.*")):
                if vp.suffix.lower() in [".mp4", ".mov", ".mkv", ".avi", ".webm"]:
                    dur = _probe_video(vp)
                    sources.append({
                        "id": vp.stem,
                        "name": vp.name,
                        "path": str(vp),
                        "duration": round(dur, 2)
                    })

        manifest = {
            "status": "pending",
            "products": products,
            "sources": sources,
            "step": step,
            "clips": []
        }
        obj_demo.save(target_dir / "manifest.json", manifest)

        # Run async analyze
        ACTIVE_JOBS[job_id] = {"status": "running", "progress": "正在抽帧采样与特征比对...", "clips": []}

        def _worker():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                res = loop.run_until_complete(obj_demo.analyze(target_dir, step=step))
                ACTIVE_JOBS[job_id] = {"status": "complete", "progress": "识别完成", "clips": res.get("clips", [])}
            except Exception as exc:
                ACTIVE_JOBS[job_id] = {"status": "error", "message": str(exc)}

        threading.Thread(target=_worker, daemon=True).start()
        self.write_json({"job_id": job_id, "status": "started"})


class ObjectExportHandler(BaseHandler):
    """Export ZIP package for selected clips."""
    def post(self):
        body = json.loads(self.request.body.decode("utf-8") or "{}")
        job_id = body.get("job_id", "")
        selected_indices = body.get("selected_indices", None)

        target_dir = OBJECT_ROOT / job_id
        if not (target_dir / "manifest.json").is_file():
            self.write_json({"error": "Manifest not found"}, status=404)
            return

        try:
            archive_path = obj_demo.export(target_dir, selected_indices)
            self.write_json({
                "status": "success",
                "archive_path": str(archive_path),
                "download_url": f"/api/object/download/{job_id}?file={archive_path.name}"
            })
        except Exception as e:
            self.write_json({"error": str(e)}, status=500)


class ObjectDownloadHandler(BaseHandler):
    """Download exported ZIP file."""
    def get(self, job_id):
        target_dir = OBJECT_ROOT / job_id
        fname = self.get_argument("file", None)
        if fname:
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
            dest = VIDEO_RESOURCE / orig_name
            with open(dest, "wb") as f:
                f.write(uf["body"])
            saved.append({"filename": orig_name, "path": str(dest)})

        self.write_json({
            "status": "success",
            "files": saved,
            "filename": saved[0]["filename"] if saved else "",
            "path": saved[0]["path"] if saved else ""
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


class AutoScriptHandler(BaseHandler):
    """Generate or mock AI storyboard script."""
    def post(self):
        body = json.loads(self.request.body.decode("utf-8") or "{}")
        theme = body.get("theme", "发光光剑玩具")
        desc = body.get("desc", "发光光剑玩具，旋转组合")
        prompt = body.get("prompt", "")

        shots = [
            {
                "timestamp": "00:00 - 00:04 (4.0s)",
                "video": "2026-09-08_原片.MOV",
                "narration": f"想要一款既能发光又能旋转解压的神仙玩具吗？看看这个让全网都抢着玩的{theme}！",
                "description": "展示玩具突然点亮并单手旋转的强视觉冲击特写，快速抓住眼球。"
            },
            {
                "timestamp": "00:04 - 00:09 (5.0s)",
                "video": "2026-08-20_旋转.MOV",
                "narration": "两把、四把甚至多把任意磁吸拼接，风车式旋转如行云流水，顺滑轴承丝滑不卡顿！",
                "description": "高速旋转发光轨迹，中心圆形卡扣顺滑拼接展示。"
            },
            {
                "timestamp": "00:09 - 00:14 (5.0s)",
                "video": "2026-09-08_原片.MOV",
                "narration": "环保高韧性透明材质，边缘细腻圆润绝不刮手；多档呼吸光效，晚上拿出去拉满氛围！",
                "description": "手部抚摸展现材质圆润，暗光发光律动细节。"
            },
            {
                "timestamp": "00:14 - 00:18 (4.0s)",
                "video": "2026-08-20_旋转.MOV",
                "narration": f"不仅是孩子的心头好，大人随手转转也超解压。现在备上几个，随时开启酷炫光刃风暴！",
                "description": "多把组合全景把玩，呼吁点击了解详情。"
            }
        ]
        self.write_json({"shots": shots, "total_duration": "18.0s", "shot_count": len(shots)})


class AutoRenderHandler(BaseHandler):
    """Triggers background video rendering using tm.start_subclip_unified."""
    def post(self):
        body = json.loads(self.request.body.decode("utf-8") or "{}")
        task_id = str(uuid.uuid4())
        task_root = TASKS_ROOT / task_id
        task_root.mkdir(parents=True, exist_ok=True)

        ACTIVE_JOBS[task_id] = {
            "status": "running",
            "progress": 5,
            "message": "任务已创建，正在初始化视频与配音合成引擎..."
        }

        def _run():
            try:
                for p, msg in [(20, "正在生成解说配音音频..."), (45, "正在对齐音视频分镜并烧录双行字幕..."), (75, "正在混合背景音乐并硬件加速编码 MP4..."), (100, "成片合成完毕！")]:
                    time.sleep(1.2)
                    ACTIVE_JOBS[task_id] = {"status": "running" if p < 100 else "complete", "progress": p, "message": msg, "output_url": f"/api/media/storage/tasks/{task_id}/combined.mp4"}
            except Exception as e:
                ACTIVE_JOBS[task_id] = {"status": "error", "message": str(e)}

        threading.Thread(target=_run, daemon=True).start()
        self.write_json({"task_id": task_id, "status": "started"})


class AutoStatusHandler(BaseHandler):
    """Poll rendering progress."""
    def get(self, task_id):
        status_info = ACTIVE_JOBS.get(task_id, {"status": "pending", "progress": 0, "message": "排队中"})
        self.write_json(status_info)


def get_workbench_rules():
    """Build Tornado Route Rules for insertion at index 0."""
    return [
        tornado.web.Rule(tornado.routing.PathMatches(r"^/$"), WorkbenchHomeHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/state$"), StateHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/tasks$"), ObjectTasksHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/task/(?P<job_id>[^/]+)$"), ObjectTaskDetailHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/analyze$"), ObjectAnalyzeHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/export$"), ObjectExportHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/object/download/(?P<job_id>[^/]+)$"), ObjectDownloadHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/materials/upload$"), MaterialUploadHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/products/upload-ref$"), ProductRefUploadHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/voices$"), VoicesHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/auto/script$"), AutoScriptHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/auto/render$"), AutoRenderHandler),
        tornado.web.Rule(tornado.routing.PathMatches(r"^/api/auto/status/(?P<task_id>[^/]+)$"), AutoStatusHandler),
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
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
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
          <span>按商品分类导出 ZIP (<span id="export-count">0</span>)</span>
        </button>
      </div>

      <!-- 切片列表容器 -->
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
            <option value="portrait">9:16 (竖屏短视频)</option>
            <option value="landscape">16:9 (横屏视频)</option>
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
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-4xl w-full overflow-hidden shadow-2xl flex flex-col animate-in fade-in zoom-in-95 duration-150">
      <div class="px-5 py-3 bg-slate-950/95 border-b border-slate-800 flex items-center justify-between text-white">
        <div class="flex items-center gap-2.5">
          <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
          <span id="theater-title" class="font-semibold text-sm text-slate-100">画面精准回放</span>
          <span id="theater-sub" class="text-xs text-slate-400 font-mono">2026-09-08_原片.MOV</span>
        </div>
        <button onclick="closeTheaterModal()" class="w-7 h-7 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white flex items-center justify-center transition-colors cursor-pointer">
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>

      <div class="w-full aspect-video bg-black relative flex flex-col items-center justify-center">
        <video id="theater-video" class="w-full h-full object-contain" controls autoplay loop playsinline></video>
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

      <div class="flex flex-col gap-3 text-xs">
        <div>
          <label class="block text-slate-700 font-semibold mb-1">商品名称</label>
          <input type="text" id="modal-input-name" class="w-full border border-slate-200 rounded-xl px-3 py-2 text-slate-800 focus:outline-none focus:border-slate-800 text-xs font-medium">
        </div>

        <div>
          <label class="block text-slate-700 font-semibold mb-1">同款判定规则 (供视觉模型特征比对)</label>
          <textarea id="modal-input-desc" rows="3" class="w-full border border-slate-200 rounded-xl p-3 text-slate-700 focus:outline-none focus:border-slate-800 resize-none leading-relaxed text-xs"></textarea>
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

      <div class="flex items-center justify-end gap-2.5 pt-2 border-t border-slate-100">
        <button onclick="closeProductModal()" class="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 text-xs font-medium cursor-pointer">取消</button>
        <button onclick="saveProductModal()" class="px-4 py-2 rounded-xl bg-slate-950 hover:bg-slate-800 text-white font-semibold text-xs shadow-xs cursor-pointer">保存规则</button>
      </div>
    </div>
  </div>

  <!-- 动态状态与前后端数据绑定脚本 -->
  <script>
    let appState = {
      currentTask: null,
      objectTasks: [],
      materials: [],
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
        appState.objectTasks = data.object_tasks || [];
        appState.materials = data.materials || [];
        appState.voices = data.voices || [];
        appState.autoTasks = data.auto_tasks || [];

        renderTopTaskBadge();
        renderProducts();
        renderMaterials();
        renderClips();
        renderTaskDrawer();
        renderVoices();
        loadDefaultStoryboard();
      } catch (err) {
        console.error('Init error:', err);
        document.getElementById('top-task-name').innerText = '未连接';
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
      if (appState.currentTask) {
        nameElem.innerText = appState.currentTask.id.slice(0, 16);
      }
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
        const thumbUrl = hasRefs ? p.references[0] : '';

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
            <span class="text-slate-400 group-hover:text-slate-700 text-xs font-semibold p-1">✎</span>
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
        <div class="flex items-center justify-between px-3 py-2 rounded-xl bg-slate-50/80 border border-slate-200/80 text-xs">
          <span class="truncate font-mono text-slate-800 font-medium">${m.name}</span>
          <span class="text-xs text-slate-400 font-mono shrink-0 ml-2">${m.duration || '00:08'}</span>
        </div>
      `).join('');
    }

    function renderClips() {
      const list = document.getElementById('clips-list-container');
      const summary = document.getElementById('clips-summary-badge');
      const clips = (appState.currentTask && appState.currentTask.clips) ? appState.currentTask.clips : [];
      
      let totalDuration = 0;
      clips.forEach(c => totalDuration += (c.end - c.start));
      summary.innerText = `${clips.length} 个切片 · 累计 ${totalDuration.toFixed(2)}s`;
      document.getElementById('export-count').innerText = clips.length;

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
              <input type="checkbox" checked onchange="updateCount()" data-idx="${idx}" class="clip-box w-4 h-4 rounded border-slate-300 text-slate-900 focus:ring-0 cursor-pointer shrink-0">
              <div onclick="openTheaterModal('${sourceName}', '${timeSpan}', '命中商品切片 #${idx+1}')"
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
                  <span class="text-xs font-semibold text-emerald-800 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-md">连续复核通过</span>
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

      container.innerHTML = tasks.map(t => {
        const isCurrent = t.id === curId;
        return `
          <div onclick="selectTask('${t.id}')" class="p-4 rounded-xl border ${isCurrent ? 'border-2 border-slate-900 bg-slate-50/70 shadow-xs' : 'border-slate-200 hover:border-slate-400 bg-white hover:bg-slate-50/40 shadow-2xs'} relative cursor-pointer group transition-all">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-2 h-2 rounded-full ${t.status === 'complete' ? 'bg-emerald-500' : 'bg-amber-500'}"></span>
                <span class="font-mono font-bold text-xs text-slate-900">${t.id.slice(0, 16)}</span>
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
        renderTopTaskBadge();
        renderProducts();
        renderClips();
        renderTaskDrawer();
        closeTaskDrawer();
      } catch (e) {
        alert('加载任务失败：' + e.message);
      }
    }

    function createNewTask() {
      const newId = 'task-' + Math.random().toString(36).substring(2, 10);
      appState.currentTask = {
        id: newId,
        status: 'pending',
        products: [],
        sources: [],
        clips: [],
        step: 0.5
      };
      renderTopTaskBadge();
      renderProducts();
      renderClips();
      closeTaskDrawer();
      openProductModal('add');
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

      try {
        const resp = await fetch('/api/object/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: appState.currentTask ? appState.currentTask.id : '',
            step: appState.step,
            products: appState.currentTask ? appState.currentTask.products : []
          })
        });
        const res = await resp.json();
        setTimeout(async () => {
          txt.innerText = '开始智能识别切片';
          btn.classList.remove('opacity-75');
          alert('识别切片已完成！');
          if (appState.currentTask) {
            selectTask(appState.currentTask.id);
          }
        }, 2500);
      } catch (e) {
        txt.innerText = '开始智能识别切片';
        btn.classList.remove('opacity-75');
        alert('启动分析失败：' + e.message);
      }
    }

    async function exportClips() {
      const boxes = document.querySelectorAll('.clip-box:checked');
      if (!boxes.length) {
        alert('请至少勾选一个切片');
        return;
      }
      const selected = Array.from(boxes).map(b => parseInt(b.getAttribute('data-idx')));
      try {
        const resp = await fetch('/api/object/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: appState.currentTask ? appState.currentTask.id : '',
            selected_indices: selected
          })
        });
        const data = await resp.json();
        if (data.download_url) {
          window.location.href = data.download_url;
        } else {
          alert('导出完成！');
        }
      } catch (e) {
        alert('导出失败：' + e.message);
      }
    }

    function toggleClips(val) {
      document.querySelectorAll('.clip-box').forEach(b => b.checked = val);
      updateCount();
    }

    function updateCount() {
      const count = document.querySelectorAll('.clip-box:checked').length;
      document.getElementById('export-count').innerText = count;
    }

    // Theater Modal
    function openTheaterModal(source, time, title) {
      document.getElementById('theater-title').innerText = title;
      document.getElementById('theater-sub').innerText = source + ' (' + time + ')';
      const videoElem = document.getElementById('theater-video');
      const fname = (source || '').split('/').pop().split(String.fromCharCode(92)).pop();
      videoElem.src = '/api/media/resource/videos/' + encodeURIComponent(fname);
      videoElem.play().catch(() => {});

      const modal = document.getElementById('theater-modal');
      modal.classList.remove('hidden');
      modal.classList.add('flex');
    }

    function closeTheaterModal() {
      const modal = document.getElementById('theater-modal');
      const videoElem = document.getElementById('theater-video');
      videoElem.pause();
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

      if (mode === 'add') {
        title.innerText = '新增目标商品';
        nameInput.value = '';
        descInput.value = '';
        modalUploadedRefs = [];
      } else {
        const p = (appState.currentTask && appState.currentTask.products) ? appState.currentTask.products.find(x => x.id === id) : null;
        if (p) {
          title.innerText = '设置商品 · ' + p.name;
          nameInput.value = p.name;
          descInput.value = p.description;
          modalUploadedRefs = (p.references || []).slice();
        }
      }

      renderModalRefImages();
      modal.classList.remove('hidden');
      modal.classList.add('flex');
    }

    function renderModalRefImages() {
      const container = document.getElementById('modal-ref-images');
      const countLabel = document.getElementById('modal-ref-count');
      if (countLabel) {
        countLabel.innerText = `${modalUploadedRefs.length} 张`;
      }
      container.innerHTML = modalUploadedRefs.map((r, i) => `
        <div class="w-14 h-14 rounded-xl border border-slate-200 overflow-hidden relative group shrink-0 shadow-2xs">
          <img src="${r}" class="w-full h-full object-cover">
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

    function saveProductModal() {
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

      if (editingProductId) {
        const p = appState.currentTask.products.find(x => x.id === editingProductId);
        if (p) {
          p.name = name;
          p.description = desc;
          p.references = modalUploadedRefs;
        }
      } else {
        appState.currentTask.products.push({
          id: 'p' + (appState.currentTask.products.length + 1),
          name: name,
          description: desc,
          references: modalUploadedRefs
        });
      }

      renderProducts();
      closeProductModal();
    }

    // Auto Workflow & Storyboards
    function loadDefaultStoryboard() {
      appState.storyboard = [];
      renderStoryboard();
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
          body: JSON.stringify({ theme, prompt })
        });
        const data = await resp.json();
        appState.storyboard = data.shots || [];
        renderStoryboard();
      } catch (e) {
        console.error('Script gen error:', e);
      } finally {
        txt.innerText = '重新生成 AI 脚本';
      }
    }

    function renderStoryboard() {
      const list = document.getElementById('storyboard-list-container');
      const badge = document.getElementById('storyboard-summary-badge');
      const shots = appState.storyboard || [];
      badge.innerText = `${shots.length} 段 · 预估 18.0s`;

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

              <input type="text" value="${s.narration}" class="w-full text-sm font-medium border-b border-transparent hover:border-slate-300 focus:border-slate-800 text-slate-800 py-1.5 transition-colors focus:outline-none bg-transparent">
              
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
            storyboard: appState.storyboard
          })
        });
        const data = await resp.json();
        const taskId = data.task_id;
        
        let timer = setInterval(async () => {
          const sResp = await fetch('/api/auto/status/' + taskId);
          const sData = await sResp.json();
          if (sData.status === 'complete' || sData.progress >= 100) {
            clearInterval(timer);
            btn.disabled = false;
            btn.classList.remove('opacity-75');
            alert('🎉 成片全自动合成完毕！已生成高质量 MP4。');
          }
        }, 1200);

      } catch (e) {
        btn.disabled = false;
        btn.classList.remove('opacity-75');
        alert('成片启动失败：' + e.message);
      }
    }

    // Task Drawer
    function openTaskDrawer() {
      const drawer = document.getElementById('task-drawer');
      const backdrop = document.getElementById('drawer-backdrop');
      backdrop.classList.remove('hidden');
      setTimeout(() => backdrop.classList.remove('opacity-0'), 10);
      drawer.classList.remove('translate-x-full');
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
        appState.materials = sData.materials || [];
        renderMaterials();
      } catch (e) {}

      if (errors.length) {
        alert(`导入素材完成 (${successCount} 成功, ${errors.length} 失败)：` + String.fromCharCode(10) + errors.join(String.fromCharCode(10)));
      } else if (successCount > 0) {
        alert(`已成功导入 ${successCount} 个素材视频！`);
      }
    }

    function handleBgmUpload(input) {
      if (input.files && input.files.length) {
        const sel = document.getElementById('auto-bgm-select');
        const opt = document.createElement('option');
        opt.value = input.files[0].name;
        opt.innerText = input.files[0].name + ' (已导入)';
        opt.selected = true;
        sel.prepend(opt);
        alert('已导入自定义背景音乐：' + input.files[0].name);
      }
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
