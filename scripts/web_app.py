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
        run_command(job, ["bash", str(SCRIPTS_DIR / "analyze_one.sh"), job.filename])
        if job.postprocess:
            run_command(job, ["python", str(SCRIPTS_DIR / "deepseek_postprocess.py"), str(output_dir)])
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
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-200:],
        "analysis": read_json(output_dir / "analysis.json"),
        "audit_result": read_json(output_dir / "audit_result.json"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ShortVideoAnalyzer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return text_response(self, HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
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
            return json_response(
                self,
                HTTPStatus.OK,
                {
                    "filename": filename,
                    "status": "saved",
                    "output_dir": str(output_dir.relative_to(ROOT)),
                    "analysis": read_json(output_dir / "analysis.json"),
                    "audit_result": read_json(output_dir / "audit_result.json"),
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
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        if not (VIDEOS_DIR / filename).is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"Video file not found: {filename}"})

        job = Job(id=str(uuid.uuid4()), filename=filename, postprocess=postprocess)
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
      --bg: #f7f8fa;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #18202f;
      --muted: #657083;
      --accent: #1967d2;
      --accent-strong: #0f4da0;
      --danger: #b42318;
      --ok: #087443;
      --code: #111827;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }
    header {
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 19px;
      font-weight: 650;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: minmax(300px, 420px) minmax(0, 1fr);
      gap: 18px;
      padding: 18px;
      min-height: calc(100vh - 64px);
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-width: 0;
    }
    .controls {
      padding: 18px;
      display: grid;
      gap: 16px;
      align-content: start;
    }
    .output {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 560px;
    }
    .section-title {
      font-size: 14px;
      font-weight: 650;
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
      border-radius: 6px;
      padding: 8px;
      background: #fff;
      color: var(--text);
    }
    .row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .check {
      display: flex;
      gap: 8px;
      align-items: center;
      color: var(--text);
      font-size: 14px;
    }
    button {
      min-height: 40px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--accent);
      color: white;
      padding: 8px 12px;
      font-weight: 600;
      cursor: pointer;
    }
    button.secondary {
      background: #fff;
      color: var(--accent);
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .status {
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      color: var(--muted);
      background: #fbfcfe;
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .tabs {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      overflow-x: auto;
    }
    .tab {
      min-height: 34px;
      padding: 6px 10px;
      border-color: var(--line);
      background: #fff;
      color: var(--text);
      white-space: nowrap;
    }
    .tab.active {
      border-color: var(--accent);
      color: var(--accent);
    }
    pre {
      margin: 0;
      padding: 16px;
      overflow: auto;
      min-height: 0;
      background: #0f172a;
      color: #e6edf7;
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
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
      border-radius: 6px;
      padding: 8px;
      background: #fff;
      color: var(--text);
      text-align: left;
    }
    .file-item.selected {
      border-color: var(--accent);
    }
    .muted { color: var(--muted); }
    .ok { color: var(--ok); }
    .bad { color: var(--danger); }
    @media (max-width: 860px) {
      header { padding: 0 14px; }
      main {
        grid-template-columns: 1fr;
        padding: 12px;
      }
      .output { min-height: 460px; }
    }
  </style>
</head>
<body>
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
        <label class="check">
          <input id="postprocess" type="checkbox">
          DeepSeek 后处理
        </label>
      </div>
      <button id="analyzeBtn" disabled>开始分析</button>
      <div class="status" id="statusBox">等待上传或选择视频。</div>
    </section>
    <section class="output">
      <div class="tabs">
        <button class="tab active" data-tab="analysis">analysis.json</button>
        <button class="tab" data-tab="audit">audit_result.json</button>
        <button class="tab" data-tab="log">运行日志</button>
      </div>
      <pre id="outputBox">{}</pre>
    </section>
  </main>
  <script>
    const state = { selectedFile: "", currentJob: null, currentTab: "analysis", timer: null };
    const fileList = document.getElementById("fileList");
    const statusBox = document.getElementById("statusBox");
    const outputBox = document.getElementById("outputBox");
    const currentFile = document.getElementById("currentFile");
    const analyzeBtn = document.getElementById("analyzeBtn");

    function setStatus(message, kind = "") {
      statusBox.className = "status " + kind;
      statusBox.textContent = message;
    }

    function pretty(value) {
      if (value === null || value === undefined) return "{}";
      if (typeof value === "string") return value;
      return JSON.stringify(value, null, 2);
    }

    function renderOutput(job) {
      if (!job) {
        outputBox.textContent = "{}";
        return;
      }
      if (state.currentTab === "analysis") outputBox.textContent = pretty(job.analysis);
      if (state.currentTab === "audit") outputBox.textContent = pretty(job.audit_result);
      if (state.currentTab === "log") outputBox.textContent = (job.log || []).join("\n");
    }

    async function loadSavedResult(name) {
      if (!name) return;
      const response = await fetch(`/api/result?filename=${encodeURIComponent(name)}`);
      const result = await response.json();
      if (result.analysis || result.audit_result) {
        state.currentJob = null;
        renderOutput(result);
        setStatus(`${name}: 已加载已有输出`, "ok");
      } else {
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

    async function uploadVideo() {
      const input = document.getElementById("videoFile");
      if (!input.files.length) {
        setStatus("请选择一个视频文件。", "bad");
        return;
      }
      const form = new FormData();
      form.append("video", input.files[0]);
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
          postprocess: document.getElementById("postprocess").checked
        })
      });
      const job = await response.json();
      if (!response.ok) {
        setStatus(job.error || "提交失败", "bad");
        analyzeBtn.disabled = false;
        return;
      }
      state.currentJob = job.id;
      pollJob();
      if (state.timer) clearInterval(state.timer);
      state.timer = setInterval(pollJob, 2500);
    }

    async function pollJob() {
      if (!state.currentJob) return;
      const response = await fetch(`/api/job?id=${encodeURIComponent(state.currentJob)}`);
      const job = await response.json();
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

    document.getElementById("uploadBtn").onclick = uploadVideo;
    document.getElementById("refreshBtn").onclick = refreshFiles;
    analyzeBtn.onclick = startAnalyze;
    document.querySelectorAll(".tab").forEach(tab => {
      tab.onclick = () => {
        document.querySelectorAll(".tab").forEach(item => item.classList.remove("active"));
        tab.classList.add("active");
        state.currentTab = tab.dataset.tab;
        if (state.currentJob) pollJob();
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
