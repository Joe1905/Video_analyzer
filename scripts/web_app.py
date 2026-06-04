#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import cgi


ROOT = Path.cwd()
VIDEOS_DIR = ROOT / "videos"
OUTPUT_DIR = ROOT / "output"
SCRIPTS_DIR = ROOT / "scripts"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


@dataclass
class Job:
    id: str
    filename: str
    postprocess: bool
    analysis_mode: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    if not name:
        raise ValueError("Missing filename")
    cleaned = "".join(ch for ch in name if ch in SAFE_CHARS)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Invalid filename")
    return cleaned


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def mode_from_analysis(analysis: Any) -> str | None:
    if isinstance(analysis, dict):
        return analysis.get("processing_mode")
    return None


def append_log(job: Job, line: str) -> None:
    with jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_command(job: Job, command: list[str]) -> None:
    append_log(job, f"$ {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        append_log(job, line)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def run_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    try:
        output_dir = OUTPUT_DIR / job.filename
        job.output_dir = str(output_dir.relative_to(ROOT))
        if job.analysis_mode == "direct_video":
            run_command(
                job,
                [
                    "python",
                    str(SCRIPTS_DIR / "direct_video_analyze.py"),
                    job.filename,
                    "--output-dir",
                    str(output_dir),
                ],
            )
        else:
            run_command(job, ["bash", str(SCRIPTS_DIR / "analyze_one.sh"), job.filename])
        if os.getenv("DEEPSEEK_API_KEY"):
            try:
                run_command(job, ["python", str(SCRIPTS_DIR / "translate_analysis.py"), str(output_dir)])
            except Exception as exc:
                append_log(job, f"Translation skipped: {exc}")
        if job.postprocess:
            run_command(job, ["python", str(SCRIPTS_DIR / "deepseek_postprocess.py"), str(output_dir)])
            if os.getenv("DEEPSEEK_API_KEY"):
                try:
                    run_command(
                        job,
                        [
                            "python",
                            str(SCRIPTS_DIR / "translate_analysis.py"),
                            str(output_dir / "audit_result.json"),
                            "--output",
                            str(output_dir / "audit_result_zh.json"),
                        ],
                    )
                except Exception as exc:
                    append_log(job, f"Audit translation skipped: {exc}")
        with jobs_lock:
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        with jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def public_job(job: Job) -> dict[str, Any]:
    output_dir = OUTPUT_DIR / job.filename
    return {
        "id": job.id,
        "filename": job.filename,
        "postprocess": job.postprocess,
        "analysis_mode": job.analysis_mode,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-200:],
        "analysis": read_json(output_dir / "analysis.json"),
        "analysis_zh": read_json(output_dir / "analysis_zh.json"),
        "audit_result": read_json(output_dir / "audit_result.json"),
        "audit_result_zh": read_json(output_dir / "audit_result_zh.json"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ShortVideoAnalyzer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = INDEX_HTML.replace(
                "__DEFAULT_ANALYSIS_MODE__",
                os.getenv("ANALYSIS_MODE", "analyzer"),
            )
            return text_response(self, HTTPStatus.OK, html, "text/html; charset=utf-8")
        if parsed.path == "/api/jobs":
            with jobs_lock:
                payload = [public_job(job) for job in sorted(jobs.values(), key=lambda item: item.created_at, reverse=True)]
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with jobs_lock:
                job = jobs.get(job_id)
                payload = public_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/files":
            files = []
            for path in sorted(VIDEOS_DIR.glob("*")):
                if path.is_file():
                    files.append({"name": path.name, "size": path.stat().st_size})
            return json_response(self, HTTPStatus.OK, files)
        if parsed.path == "/api/result":
            try:
                filename = safe_filename(parse_qs(parsed.query).get("filename", [""])[0])
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            output_dir = OUTPUT_DIR / filename
            analysis = read_json(output_dir / "analysis.json")
            return json_response(
                self,
                HTTPStatus.OK,
                {
                    "filename": filename,
                    "status": "saved",
                    "output_dir": str(output_dir.relative_to(ROOT)),
                    "analysis_mode": mode_from_analysis(analysis),
                    "analysis": analysis,
                    "analysis_zh": read_json(output_dir / "analysis_zh.json"),
                    "audit_result": read_json(output_dir / "audit_result.json"),
                    "audit_result_zh": read_json(output_dir / "audit_result_zh.json"),
                    "log": [],
                },
            )
        return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            return self.handle_upload()
        if parsed.path == "/api/analyze":
            return self.handle_analyze()
        return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def handle_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid upload size"})

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )
        file_item = form["video"] if "video" in form else None
        if file_item is None or not getattr(file_item, "filename", None):
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Missing video file"})

        try:
            filename = safe_filename(file_item.filename)
        except ValueError as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        target = VIDEOS_DIR / filename
        with target.open("wb") as file:
            shutil.copyfileobj(file_item.file, file)

        return json_response(self, HTTPStatus.OK, {"filename": filename, "size": target.stat().st_size})

    def handle_analyze(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
            postprocess = bool(payload.get("postprocess", False))
            analysis_mode = str(payload.get("analysis_mode") or os.getenv("ANALYSIS_MODE", "analyzer"))
            if analysis_mode not in {"analyzer", "direct_video"}:
                raise ValueError("analysis_mode must be analyzer or direct_video")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        if not (VIDEOS_DIR / filename).is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"Video file not found: {filename}"})

        job = Job(
            id=str(uuid.uuid4()),
            filename=filename,
            postprocess=postprocess,
            analysis_mode=analysis_mode,
        )
        with jobs_lock:
            jobs[job.id] = job
        thread = threading.Thread(target=run_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_job(job))


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Short Video Analyzer</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef2f7;
      --panel: #ffffff;
      --panel-soft: #f8fafc;
      --line: #d6deea;
      --line-strong: #b8c5d8;
      --text: #142033;
      --muted: #607089;
      --accent: #2563eb;
      --accent-strong: #1d4ed8;
      --accent-soft: #eaf1ff;
      --danger: #b42318;
      --ok: #087443;
      --code: #0d1628;
      --shadow: 0 18px 45px rgba(15, 23, 42, 0.10);
      --shadow-soft: 0 8px 24px rgba(15, 23, 42, 0.07);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      overflow: hidden;
      background:
        linear-gradient(135deg, rgba(37, 99, 235, 0.10), transparent 34%),
        linear-gradient(315deg, rgba(14, 165, 233, 0.08), transparent 38%),
        var(--bg);
      color: var(--text);
      font-family: Inter, "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }
    header {
      height: 66px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 28px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(12px);
    }
    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 750;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 430px) minmax(0, 1fr);
      gap: 18px;
      padding: 18px;
      height: calc(100vh - 66px);
      min-height: 0;
      overflow: hidden;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      min-width: 0;
      box-shadow: var(--shadow-soft);
    }
    .controls {
      padding: 18px;
      display: grid;
      gap: 16px;
      align-content: start;
      min-height: 0;
      overflow: auto;
    }
    .output {
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
      min-height: 0;
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    .section-title {
      font-size: 14px;
      font-weight: 760;
      margin: 0 0 10px;
    }
    label {
      display: block;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 6px;
    }
    input[type="file"], select {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 9px 10px;
      background: #fff;
      color: var(--text);
      outline: none;
      transition: border-color 160ms ease, box-shadow 160ms ease;
    }
    input[type="file"]:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
    }
    input[type="file"]::file-selector-button {
      margin-right: 10px;
      border: 0;
      border-radius: 7px;
      background: var(--accent-soft);
      color: var(--accent-strong);
      padding: 7px 10px;
      font-weight: 700;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    button {
      min-height: 40px;
      border: 1px solid var(--accent);
      border-radius: 9px;
      background: var(--accent);
      color: white;
      padding: 8px 12px;
      font-weight: 750;
      cursor: pointer;
      transition: transform 120ms ease, background 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
      box-shadow: 0 8px 18px rgba(37, 99, 235, 0.18);
    }
    button:hover:not(:disabled) {
      background: var(--accent-strong);
      border-color: var(--accent-strong);
      transform: translateY(-1px);
    }
    button.secondary {
      background: #fff;
      color: var(--accent);
      box-shadow: none;
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .status {
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 10px 12px;
      color: var(--muted);
      background: var(--panel-soft);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .tabs {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      overflow-x: auto;
      background: #ffffff;
    }
    .tab {
      min-height: 34px;
      padding: 6px 12px;
      border-color: var(--line);
      background: #fff;
      color: var(--text);
      white-space: nowrap;
      box-shadow: none;
    }
    .tab.active {
      border-color: var(--accent);
      color: var(--accent);
      background: var(--accent-soft);
    }
    .output-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-soft);
    }
    .output-title {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }
    .small-button {
      min-height: 32px;
      padding: 5px 10px;
      font-size: 13px;
    }
    .toggle-button {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border-color: var(--line);
      background: #fff;
      color: var(--text);
      box-shadow: none;
      text-align: left;
    }
    .toggle-button::after {
      content: "关闭";
      min-width: 46px;
      border-radius: 999px;
      padding: 3px 9px;
      background: #edf2f7;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }
    .toggle-button.active {
      border-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent-strong);
    }
    .toggle-button.active::after {
      content: "开启";
      background: var(--accent);
      color: #fff;
    }
    .report-output {
      margin: 0;
      height: 100%;
      min-height: 0;
      padding: 22px 24px;
      overflow: auto;
      border-radius: 0 0 12px 12px;
      background:
        linear-gradient(180deg, rgba(248, 250, 252, 0.92), rgba(255, 255, 255, 0.98)),
        #fff;
      color: var(--text);
      font-size: 13px;
      line-height: 1.78;
      white-space: pre-wrap;
      word-break: break-word;
      border-left: 4px solid rgba(37, 99, 235, 0.22);
      scrollbar-color: #9aa8bd #eef2f7;
      scrollbar-width: thin;
    }
    .report-output.raw-output {
      background: var(--code);
      color: #e6edf7;
      border-left-color: rgba(96, 165, 250, 0.45);
      font-family: "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
      line-height: 1.68;
      scrollbar-color: #607089 #111827;
    }
    .report-output::-webkit-scrollbar, .controls::-webkit-scrollbar, .file-list::-webkit-scrollbar {
      width: 10px;
      height: 10px;
    }
    .report-output::-webkit-scrollbar-track, .controls::-webkit-scrollbar-track, .file-list::-webkit-scrollbar-track {
      background: rgba(148, 163, 184, 0.16);
    }
    .report-output::-webkit-scrollbar-thumb, .controls::-webkit-scrollbar-thumb, .file-list::-webkit-scrollbar-thumb {
      background: rgba(96, 112, 137, 0.7);
      border-radius: 999px;
      border: 2px solid transparent;
      background-clip: padding-box;
    }
    .file-list {
      display: grid;
      gap: 8px;
      max-height: 180px;
      overflow: auto;
    }
    .file-item {
      width: 100%;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 9px 10px;
      background: #fff;
      color: var(--text);
      text-align: left;
      box-shadow: none;
    }
    .file-item.selected {
      border-color: var(--accent);
      background: var(--accent-soft);
    }
    .muted { color: var(--muted); }
    .ok { color: var(--ok); }
    .bad { color: var(--danger); }
    .drop-overlay {
      position: fixed;
      inset: 14px;
      z-index: 20;
      display: none;
      align-items: center;
      justify-content: center;
      border: 2px dashed rgba(37, 99, 235, 0.55);
      border-radius: 18px;
      background: rgba(239, 246, 255, 0.86);
      color: var(--accent-strong);
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow);
      pointer-events: none;
    }
    .drop-overlay.active {
      display: flex;
    }
    .drop-card {
      display: grid;
      gap: 8px;
      min-width: min(420px, 86vw);
      padding: 26px 30px;
      border: 1px solid rgba(37, 99, 235, 0.18);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.92);
      text-align: center;
    }
    .drop-card strong {
      font-size: 18px;
    }
    .drop-card span {
      color: var(--muted);
      font-size: 13px;
    }
    @media (max-width: 860px) {
      header { padding: 0 14px; }
      body { overflow: auto; }
      main {
        grid-template-columns: 1fr;
        height: auto;
        min-height: calc(100vh - 66px);
        overflow: visible;
        padding: 12px;
      }
      .output { height: 70vh; min-height: 460px; }
    }
  </style>
</head>
<body>
  <div class="drop-overlay" id="dropOverlay">
    <div class="drop-card">
      <strong>松手上传视频</strong>
      <span>文件会保存到 videos/，上传后自动选中。</span>
    </div>
  </div>
  <header>
    <h1>Short Video Analyzer</h1>
    <div class="muted" id="currentFile">未选择视频</div>
  </header>
  <main>
    <section class="controls">
      <div>
        <p class="section-title">输入视频</p>
        <label for="videoFile">上传到 videos/</label>
        <input id="videoFile" type="file" accept="video/*">
      </div>
      <div class="row">
        <button id="uploadBtn">上传</button>
        <button id="refreshBtn" class="secondary">刷新列表</button>
      </div>
      <div>
        <p class="section-title">已上传视频</p>
        <div class="file-list" id="fileList"></div>
      </div>
      <div>
        <p class="section-title">分析选项</p>
        <label for="analysisMode">处理方式</label>
        <select id="analysisMode">
          <option value="analyzer">关键帧提取模式（video-analyzer）</option>
          <option value="direct_video">直接视频理解模式（Qwen）</option>
        </select>
      </div>
      <div>
        <button id="postprocessToggle" class="toggle-button" type="button" aria-pressed="false">
          生成 DeepSeek 分析报告
        </button>
      </div>
      <button id="analyzeBtn" disabled>开始分析</button>
      <div class="status" id="statusBox">等待上传或选择视频。</div>
    </section>
    <section class="output">
      <div class="tabs">
        <button class="tab active" data-tab="content">提取内容（中文）</button>
        <button class="tab" data-tab="audit">分析结果（中文）</button>
      </div>
      <div class="output-toolbar">
        <p class="output-title" id="outputTitle">Qwen 输出内容，DeepSeek 翻译</p>
        <button id="sourceToggle" class="secondary small-button" type="button">显示原文</button>
      </div>
      <div id="outputBox" class="report-output">{}</div>
    </section>
  </main>
  <script>
    window.DEFAULT_ANALYSIS_MODE = "__DEFAULT_ANALYSIS_MODE__";
    const state = { selectedFile: "", currentJob: null, currentResult: null, currentTab: "content", showOriginal: false, postprocess: false, timer: null };
    const fileList = document.getElementById("fileList");
    const statusBox = document.getElementById("statusBox");
    const outputBox = document.getElementById("outputBox");
    const currentFile = document.getElementById("currentFile");
    const analyzeBtn = document.getElementById("analyzeBtn");
    const sourceToggle = document.getElementById("sourceToggle");
    const outputTitle = document.getElementById("outputTitle");
    const postprocessToggle = document.getElementById("postprocessToggle");
    const dropOverlay = document.getElementById("dropOverlay");
    let dragDepth = 0;

    function setStatus(message, kind = "") {
      statusBox.className = "status " + kind;
      statusBox.textContent = message;
    }

    function pretty(value) {
      if (value === null || value === undefined) return "{}";
      if (typeof value === "string") return value;
      return JSON.stringify(value, null, 2);
    }

    function labelValue(label, value) {
      if (value === null || value === undefined || value === "") return "";
      if (Array.isArray(value) && !value.length) return "";
      return `${label}：${Array.isArray(value) ? value.join("、") : value}\n`;
    }

    function listSection(title, items) {
      if (!Array.isArray(items) || !items.length) return `${title}：无\n`;
      return `${title}：\n${items.map(item => `- ${typeof item === "string" ? item : pretty(item)}`).join("\n")}\n`;
    }

    function usageSummary(usage) {
      if (!usage || typeof usage !== "object") return "";
      const parts = [];
      parts.push(labelValue("输入 Tokens", usage.input_tokens).trim());
      parts.push(labelValue("输出 Tokens", usage.output_tokens).trim());
      parts.push(labelValue("总 Tokens", usage.total_tokens).trim());
      parts.push(labelValue("API 调用次数", usage.api_calls).trim());
      parts.push(labelValue("总耗时", usage.elapsed_seconds === null || usage.elapsed_seconds === undefined ? "" : `${usage.elapsed_seconds}s`).trim());
      parts.push(labelValue("估算费用", usage.estimated_cost_usd === null || usage.estimated_cost_usd === undefined ? "" : `$${usage.estimated_cost_usd}`).trim());
      return parts.filter(item => item).join("\n");
    }

    function responseText(value) {
      if (value === null || value === undefined) return "";
      if (typeof value === "string") return value;
      if (typeof value.response === "string") return value.response;
      return pretty(value);
    }

    function cleanText(value) {
      return responseText(value).replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/i, "").trim();
    }

    function formatExtractionReport(value) {
      if (!value || typeof value !== "object") return pretty(value);
      const metadata = value.metadata || {};
      const transcript = value.transcript || {};
      const videoDescription = cleanText(value.video_description);
      const frameAnalyses = Array.isArray(value.frame_analyses) ? value.frame_analyses : [];
      const timeline = Array.isArray(value.timeline) ? value.timeline : [];
      const visualEvidence = Array.isArray(value.visual_evidence) ? value.visual_evidence : [];
      const lines = [];
      lines.push("提取内容");
      lines.push("");
      lines.push(labelValue("处理模式", value.processing_mode).trim());
      lines.push(labelValue("模型", value.vision_model || metadata.model).trim());
      lines.push(labelValue("音频模式", value.audio_mode).trim());
      lines.push(labelValue("处理帧数", metadata.frames_processed || metadata.frames_extracted).trim());
      const audioLanguage = transcript.language || metadata.audio_language;
      const transcriptSuccessful = transcript.successful === undefined ? metadata.transcription_successful : transcript.successful;
      lines.push(labelValue("音频语言", audioLanguage).trim());
      lines.push(labelValue("转写成功", transcriptSuccessful === undefined ? "" : (transcriptSuccessful ? "是" : "否")).trim());
      const usageText = usageSummary(value.usage);
      if (usageText) {
        lines.push("");
        lines.push("Token 与耗时：");
        lines.push(usageText);
      }
      if (value.summary) {
        lines.push("");
        lines.push("模型总结：");
        lines.push(cleanText(value.summary));
      }
      if (videoDescription) {
        lines.push("");
        lines.push("视频画面总述：");
        lines.push(videoDescription);
      }
      if (timeline.length) {
        lines.push("");
        lines.push("时间线：");
        timeline.forEach((item, index) => {
          if (typeof item === "string") {
            lines.push(`- ${item}`);
          } else {
            const range = item.time_range || item.timestamp || `片段 ${index + 1}`;
            const visual = item.visual ? `画面：${item.visual}` : "";
            const audio = item.audio ? `音频：${item.audio}` : "";
            lines.push(`- ${range} ${[visual, audio].filter(Boolean).join("；")}`);
          }
        });
      }
      if (visualEvidence.length) {
        lines.push("");
        lines.push("视觉证据：");
        visualEvidence.forEach((item, index) => {
          lines.push(`- ${typeof item === "string" ? item : (item.description || item.visual || pretty(item))}`);
        });
      }
      if (frameAnalyses.length) {
        lines.push("");
        lines.push("逐帧视觉分析：");
        frameAnalyses.forEach((frame, index) => {
          const text = cleanText(frame);
          if (text) lines.push(`\n[帧 ${index + 1}]\n${text}`);
        });
      }
      lines.push("");
      lines.push("转写文本：");
      lines.push(transcript.text || "无转写文本");
      if (Array.isArray(transcript.segments) && transcript.segments.length) {
        lines.push("");
        lines.push("分段转写：");
        transcript.segments.forEach(segment => {
          const start = Number.isFinite(segment.start) ? segment.start.toFixed(2) : "?";
          const end = Number.isFinite(segment.end) ? segment.end.toFixed(2) : "?";
          lines.push(`- ${start}s-${end}s  ${segment.text || ""}`);
        });
      }
      return lines.filter(line => line !== "").join("\n");
    }

    function formatAuditReport(value) {
      if (!value || typeof value !== "object") return pretty(value);
      const lines = [];
      lines.push("分析结果");
      lines.push("");
      lines.push(labelValue("风险等级", value.risk_level).trim());
      lines.push(labelValue("内容摘要", value.summary).trim());
      lines.push(labelValue("内容概览", value.content_overview).trim());
      lines.push(labelValue("转写要点", value.transcript_notes).trim());
      lines.push(labelValue("画面要点", value.visual_notes).trim());
      lines.push(listSection("风险原因", value.risk_reasons).trim());
      lines.push(listSection("问题点", value.issues).trim());
      lines.push(labelValue("建议动作", value.recommended_action).trim());
      lines.push(labelValue("发布建议", value.publish_suggestion).trim());
      return lines.filter(line => line !== "").join("\n\n");
    }

    function renderOutput(job) {
      if (!job) {
        outputBox.className = "report-output";
        outputBox.textContent = "{}";
        return;
      }
      state.currentResult = job;
      outputBox.className = state.showOriginal ? "report-output raw-output" : "report-output";
      let value = null;
      if (state.currentTab === "content") {
        outputTitle.textContent = state.showOriginal ? "Qwen 输出内容，原文" : "Qwen 输出内容，DeepSeek 翻译";
        value = state.showOriginal ? job.analysis : (job.analysis_zh || job.analysis);
        outputBox.textContent = state.showOriginal ? pretty(value) : formatExtractionReport(value);
      } else {
        outputTitle.textContent = state.showOriginal ? "DeepSeek 分析内容，原文" : "DeepSeek 分析内容，中文";
        value = state.showOriginal ? job.audit_result : (job.audit_result_zh || job.audit_result);
        outputBox.textContent = state.showOriginal ? pretty(value) : formatAuditReport(value);
      }
      sourceToggle.textContent = state.showOriginal ? "显示中文" : "显示原文";
    }

    function renderPostprocessToggle() {
      postprocessToggle.classList.toggle("active", state.postprocess);
      postprocessToggle.setAttribute("aria-pressed", state.postprocess ? "true" : "false");
    }

    async function loadSavedResult(name) {
      if (!name) return;
      const response = await fetch(`/api/result?filename=${encodeURIComponent(name)}`);
      const result = await response.json();
      if (result.analysis || result.analysis_zh || result.audit_result || result.audit_result_zh) {
        state.currentJob = null;
        state.currentResult = result;
        renderOutput(result);
        setStatus(`${name}: 已加载已有输出`, "ok");
      } else {
        state.currentResult = null;
        outputBox.className = "report-output";
        outputBox.textContent = "{}";
        setStatus(`${name}: 等待分析`);
      }
    }

    function selectFile(name) {
      state.selectedFile = name;
      currentFile.textContent = name || "未选择视频";
      analyzeBtn.disabled = !name;
      [...fileList.children].forEach(item => item.classList.toggle("selected", item.dataset.name === name));
      loadSavedResult(name).catch(error => setStatus(error.message, "bad"));
    }

    async function refreshFiles() {
      const response = await fetch("/api/files");
      const files = await response.json();
      fileList.innerHTML = "";
      if (!files.length) {
        fileList.innerHTML = '<div class="muted">videos/ 目录暂无视频</div>';
        selectFile("");
        return;
      }
      files.forEach(file => {
        const item = document.createElement("button");
        item.className = "file-item";
        item.dataset.name = file.name;
        item.innerHTML = `<span>${file.name}</span><span class="muted">${Math.round(file.size / 1024 / 1024 * 10) / 10} MB</span>`;
        item.onclick = () => selectFile(file.name);
        fileList.appendChild(item);
      });
      if (!state.selectedFile || !files.some(file => file.name === state.selectedFile)) {
        selectFile(files[0].name);
      } else {
        selectFile(state.selectedFile);
      }
    }

    async function uploadVideo(file = null) {
      const input = document.getElementById("videoFile");
      const videoFile = file || input.files[0];
      if (!videoFile) {
        setStatus("请选择一个视频文件。", "bad");
        return;
      }
      if (!videoFile.type.startsWith("video/")) {
        setStatus("请拖入视频文件。", "bad");
        return;
      }
      const form = new FormData();
      form.append("video", videoFile);
      setStatus("正在上传...");
      const response = await fetch("/api/upload", { method: "POST", body: form });
      const payload = await response.json();
      if (!response.ok) {
        setStatus(payload.error || "上传失败", "bad");
        return;
      }
      setStatus(`已上传 ${payload.filename}`, "ok");
      await refreshFiles();
      selectFile(payload.filename);
    }

    async function startAnalyze() {
      if (!state.selectedFile) return;
      analyzeBtn.disabled = true;
      setStatus("分析任务已提交...");
      const response = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: state.selectedFile,
          analysis_mode: document.getElementById("analysisMode").value,
          postprocess: state.postprocess
        })
      });
      const job = await response.json();
      if (!response.ok) {
        setStatus(job.error || "提交失败", "bad");
        analyzeBtn.disabled = false;
        return;
      }
      state.currentJob = job.id;
      state.currentResult = job;
      state.showOriginal = false;
      pollJob();
      if (state.timer) clearInterval(state.timer);
      state.timer = setInterval(pollJob, 2500);
    }

    async function pollJob() {
      if (!state.currentJob) return;
      const response = await fetch(`/api/job?id=${encodeURIComponent(state.currentJob)}`);
      const job = await response.json();
      state.currentResult = job;
      renderOutput(job);
      if (job.status === "running" || job.status === "queued") {
        setStatus(`${job.filename}: ${job.status}`);
        return;
      }
      if (state.timer) clearInterval(state.timer);
      state.timer = null;
      analyzeBtn.disabled = !state.selectedFile;
      setStatus(job.status === "complete" ? `${job.filename}: 完成` : `${job.filename}: ${job.error || "失败"}`, job.status === "complete" ? "ok" : "bad");
    }

    document.getElementById("uploadBtn").onclick = () => uploadVideo();
    document.getElementById("refreshBtn").onclick = refreshFiles;
    document.getElementById("analysisMode").value = window.DEFAULT_ANALYSIS_MODE || "analyzer";
    postprocessToggle.onclick = () => {
      state.postprocess = !state.postprocess;
      renderPostprocessToggle();
    };
    renderPostprocessToggle();
    analyzeBtn.onclick = startAnalyze;
    window.addEventListener("dragenter", event => {
      event.preventDefault();
      dragDepth += 1;
      dropOverlay.classList.add("active");
    });
    window.addEventListener("dragover", event => {
      event.preventDefault();
    });
    window.addEventListener("dragleave", event => {
      event.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) dropOverlay.classList.remove("active");
    });
    window.addEventListener("drop", event => {
      event.preventDefault();
      dragDepth = 0;
      dropOverlay.classList.remove("active");
      const file = event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) uploadVideo(file).catch(error => setStatus(error.message, "bad"));
    });
    sourceToggle.onclick = () => {
      state.showOriginal = !state.showOriginal;
      renderOutput(state.currentResult);
    };
    document.querySelectorAll(".tab").forEach(tab => {
      tab.onclick = () => {
        document.querySelectorAll(".tab").forEach(item => item.classList.remove("active"));
        tab.classList.add("active");
        state.currentTab = tab.dataset.tab;
        state.showOriginal = false;
        if (state.currentJob) {
          pollJob();
        } else {
          renderOutput(state.currentResult);
        }
      };
    });
    refreshFiles().catch(error => setStatus(error.message, "bad"));
  </script>
</body>
</html>
"""


def main() -> int:
    load_env_file()
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("WEB_PORT", "4000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Web UI listening on http://0.0.0.0:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
