#!/usr/bin/env python3
import json
import mimetypes
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
from urllib.parse import unquote
import cgi


ROOT = Path.cwd()
VIDEOS_DIR = ROOT / "videos"
OUTPUT_DIR = ROOT / "output"
SCRIPTS_DIR = ROOT / "scripts"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
ALLOWED_SHORT_VIDEO_HOST_SUFFIXES = ("tiktok.com", "tiktokv.com", "douyin.com", "iesdouyin.com")
DEFAULT_ANALYSIS_PROMPT = (
    "Analyze this short video directly. Return strict JSON only, no Markdown. "
    "Use these exact keys: summary, timeline, visual_evidence. "
    "timeline must be an array of short chronological events with time_range, visual, audio fields. "
    "visual_evidence must be an array of concrete observations from the video frames. "
    "Be specific and do not invent unsupported facts."
)


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
    analysis_prompt: str = ""


@dataclass
class DownloadJob:
    id: str
    url: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    filename: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
download_jobs: dict[str, DownloadJob] = {}
download_jobs_lock = threading.Lock()


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


def validate_short_video_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https short-video URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_SHORT_VIDEO_HOST_SUFFIXES):
        raise ValueError("Only TikTok or Douyin URLs are supported")
    if len(cleaned) > 2048:
        raise ValueError("URL is too long")
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


def write_sse_event(handler: BaseHTTPRequestHandler, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.wfile.write(b"data: ")
    handler.wfile.write(body)
    handler.wfile.write(b"\n\n")
    handler.wfile.flush()


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


def run_command(job: Job, command: list[str], env_extra: dict[str, str] | None = None) -> None:
    append_log(job, f"$ {' '.join(command)}")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
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


def append_download_log(job: DownloadJob, line: str) -> None:
    with download_jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_download_command(job: DownloadJob, command: list[str]) -> None:
    append_download_log(job, f"$ {' '.join(command)}")
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
        append_download_log(job, line)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def run_download_job(job_id: str) -> None:
    with download_jobs_lock:
        job = download_jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    result_path = OUTPUT_DIR / "download_jobs" / f"{job_id}.json"
    try:
        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        run_download_command(
            job,
            [
                "python",
                str(SCRIPTS_DIR / "tiktok_download.py"),
                job.url,
                "--output-dir",
                str(VIDEOS_DIR),
                "--result-json",
                str(result_path),
            ],
        )
        result = read_json(result_path)
        if not isinstance(result, dict) or not result.get("filename"):
            raise RuntimeError("Downloader did not return a video filename")
        filename = safe_filename(str(result["filename"]))
        if not (VIDEOS_DIR / filename).is_file():
            raise FileNotFoundError(f"Downloaded file not found: {filename}")
        with download_jobs_lock:
            job.filename = filename
            job.result = result
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        useful_log = next(
            (
                line
                for line in reversed(job.log)
                if line and not line.startswith("$ ") and not line.startswith("Command failed with exit code")
            ),
            "",
        )
        with download_jobs_lock:
            job.status = "failed"
            job.error = useful_log or str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def run_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    try:
        output_dir = OUTPUT_DIR / job.filename
        job.output_dir = str(output_dir.relative_to(ROOT))
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt = job.analysis_prompt.strip() or DEFAULT_ANALYSIS_PROMPT
        prompt_file = output_dir / "analysis_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        if job.analysis_mode == "direct_video":
            run_command(
                job,
                [
                    "python",
                    str(SCRIPTS_DIR / "direct_video_analyze.py"),
                    job.filename,
                    "--output-dir",
                    str(output_dir),
                    "--prompt-file",
                    str(prompt_file),
                ],
            )
        else:
            run_command(
                job,
                ["bash", str(SCRIPTS_DIR / "analyze_one.sh"), job.filename],
                env_extra={"ANALYSIS_PROMPT_FILE": str(prompt_file)},
            )
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


def run_postprocess_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    try:
        output_dir = OUTPUT_DIR / job.filename
        job.output_dir = str(output_dir.relative_to(ROOT))
        if not (output_dir / "analysis.json").is_file():
            raise FileNotFoundError(f"analysis.json not found: {output_dir / 'analysis.json'}")

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


def public_download_job(job: DownloadJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "url": job.url,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "filename": job.filename,
        "error": job.error,
        "log": job.log[-80:],
        "result": job.result,
    }


def check_ip_route(name: str, proxy_url: str | None = None) -> dict[str, Any]:
    import httpx

    payload: dict[str, Any] = {
        "name": name,
        "proxy_url": proxy_url or "",
        "ok": False,
        "ip": "",
        "country": "",
        "country_name": "",
        "is_us": False,
        "error": "",
    }
    client_kwargs: dict[str, Any] = {
        "timeout": 12.0,
        "follow_redirects": True,
        "trust_env": False,
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    try:
        with httpx.Client(**client_kwargs) as client:
            response = client.get("https://ipapi.co/json/")
            response.raise_for_status()
            data = response.json()
        country = str(data.get("country_code") or data.get("country") or "").upper()
        payload.update(
            {
                "ok": True,
                "ip": str(data.get("ip") or ""),
                "country": country,
                "country_name": str(data.get("country_name") or ""),
                "is_us": country == "US",
            }
        )
    except Exception as exc:
        payload["error"] = str(exc) or repr(exc)
    return payload


def public_network_check() -> dict[str, Any]:
    tiktok_proxy = os.getenv("TIKTOK_PROXY_URL", "").strip()
    direct = check_ip_route("direct")
    proxy = check_ip_route("proxy", tiktok_proxy) if tiktok_proxy else None
    return {
        "tiktok_proxy_url": tiktok_proxy,
        "direct": direct,
        "proxy": proxy,
        "proxy_is_us": bool(proxy and proxy.get("is_us")),
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
        if parsed.path == "/api/prompt":
            return json_response(self, HTTPStatus.OK, {"prompt": DEFAULT_ANALYSIS_PROMPT})
        if parsed.path == "/api/network-check":
            return json_response(self, HTTPStatus.OK, public_network_check())
        if parsed.path.startswith("/video/"):
            try:
                filename = safe_filename(unquote(parsed.path.removeprefix("/video/")))
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return self.serve_video(VIDEOS_DIR / filename)
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
        if parsed.path == "/api/job-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_job_events(job_id)
        if parsed.path == "/api/download-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with download_jobs_lock:
                job = download_jobs.get(job_id)
                payload = public_download_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Download job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/download-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_download_events(job_id)
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

    def stream_job_events(self, job_id: str) -> None:
        self.stream_events(job_id, jobs_lock, jobs, public_job, "Job not found")

    def stream_download_events(self, job_id: str) -> None:
        self.stream_events(job_id, download_jobs_lock, download_jobs, public_download_job, "Download job not found")

    def stream_events(self, job_id: str, lock: threading.Lock, store: dict[str, Any], serializer: Any, missing_message: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_marker: tuple[Any, ...] | None = None
        while True:
            with lock:
                job = store.get(job_id)
                payload = serializer(job) if job else None

            if payload is None:
                try:
                    write_sse_event(self, {"status": "missing", "error": missing_message})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return

            marker = (
                payload.get("status"),
                payload.get("updated_at"),
                len(payload.get("log") or []),
                payload.get("error"),
            )
            try:
                if marker != last_marker:
                    write_sse_event(self, payload)
                    last_marker = marker
                if payload.get("status") not in {"queued", "running"}:
                    self.close_connection = True
                    return
                time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return

    def serve_video(self, path: Path) -> None:
        if not path.is_file():
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Video not found"})

        file_size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK

        if range_header and range_header.startswith("bytes="):
            status = HTTPStatus.PARTIAL_CONTENT
            range_value = range_header.removeprefix("bytes=").split(",", 1)[0]
            start_text, _, end_text = range_value.partition("-")
            if start_text:
                start = int(start_text)
            if end_text:
                end = int(end_text)
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with path.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            return self.handle_upload()
        if parsed.path == "/api/download":
            return self.handle_download()
        if parsed.path == "/api/analyze":
            return self.handle_analyze()
        if parsed.path == "/api/postprocess":
            return self.handle_postprocess()
        if parsed.path == "/api/delete":
            return self.handle_delete()
        return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def handle_download(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            url = validate_short_video_url(str(payload.get("url", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = DownloadJob(id=str(uuid.uuid4()), url=url)
        with download_jobs_lock:
            download_jobs[job.id] = job
        thread = threading.Thread(target=run_download_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_download_job(job))

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
            reset_output = bool(payload.get("reset_output", False))
            analysis_mode = str(payload.get("analysis_mode") or os.getenv("ANALYSIS_MODE", "analyzer"))
            analysis_prompt = str(payload.get("analysis_prompt") or "").strip()
            if analysis_mode not in {"analyzer", "direct_video"}:
                raise ValueError("analysis_mode must be analyzer or direct_video")
            if len(analysis_prompt) > 12000:
                raise ValueError("analysis_prompt is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        if not (VIDEOS_DIR / filename).is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"Video file not found: {filename}"})

        if reset_output:
            output_dir = OUTPUT_DIR / filename
            if output_dir.is_dir():
                shutil.rmtree(output_dir)

        job = Job(
            id=str(uuid.uuid4()),
            filename=filename,
            postprocess=postprocess,
            analysis_mode=analysis_mode,
            analysis_prompt=analysis_prompt,
        )
        with jobs_lock:
            jobs[job.id] = job
        thread = threading.Thread(target=run_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_job(job))

    def handle_postprocess(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        output_dir = OUTPUT_DIR / filename
        if not (output_dir / "analysis.json").is_file():
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"analysis.json not found for {filename}"})

        analysis = read_json(output_dir / "analysis.json")
        job = Job(
            id=str(uuid.uuid4()),
            filename=filename,
            postprocess=True,
            analysis_mode=mode_from_analysis(analysis) or "postprocess",
        )
        job.output_dir = str(output_dir.relative_to(ROOT))
        with jobs_lock:
            jobs[job.id] = job
        thread = threading.Thread(target=run_postprocess_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_job(job))

    def handle_delete(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            filename = safe_filename(str(payload.get("filename", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        video_path = VIDEOS_DIR / filename
        output_dir = OUTPUT_DIR / filename
        deleted_video = False
        deleted_output = False
        if video_path.is_file():
            video_path.unlink()
            deleted_video = True
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
            deleted_output = True

        with jobs_lock:
            for job_id in [job_id for job_id, job in jobs.items() if job.filename == filename]:
                del jobs[job_id]

        return json_response(
            self,
            HTTPStatus.OK,
            {
                "filename": filename,
                "deleted_video": deleted_video,
                "deleted_output": deleted_output,
            },
        )


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
    input[type="file"], input[type="url"], select {
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
    input[type="file"]:focus, input[type="url"]:focus, select:focus {
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
    .check {
      display: flex;
      gap: 8px;
      align-items: center;
      color: var(--text);
      font-size: 14px;
    }
    .check input {
      width: 16px;
      height: 16px;
      accent-color: var(--accent);
    }
    .prompt-panel {
      display: none;
      gap: 8px;
    }
    .prompt-panel.active {
      display: grid;
    }
    .prompt-panel textarea {
      width: 100%;
      min-height: 180px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 10px 12px;
      background: #fff;
      color: var(--text);
      font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      outline: none;
    }
    .prompt-panel textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
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
    .report-doc {
      display: grid;
      gap: 16px;
      max-width: 1180px;
    }
    .report-hero {
      display: grid;
      gap: 8px;
      padding: 18px 20px;
      border: 1px solid rgba(37, 99, 235, 0.14);
      border-radius: 12px;
      background:
        linear-gradient(135deg, rgba(37, 99, 235, 0.10), transparent 42%),
        #ffffff;
    }
    .report-kicker {
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .report-hero h2 {
      margin: 0;
      color: var(--text);
      font-size: 22px;
      line-height: 1.25;
    }
    .report-hero p {
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }
    .report-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 11px 12px;
      background: #fff;
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .metric strong {
      display: block;
      margin-top: 5px;
      color: var(--text);
      font-size: 14px;
      overflow-wrap: anywhere;
    }
    .report-section {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      overflow: hidden;
    }
    .report-section h3 {
      margin: 0;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-soft);
      color: var(--text);
      font-size: 15px;
    }
    .report-section .body {
      padding: 15px 16px;
      color: #243044;
      font-size: 14px;
      white-space: pre-wrap;
    }
    .report-list {
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 15px 16px;
      list-style: none;
    }
    .report-list li {
      border-left: 3px solid rgba(37, 99, 235, 0.28);
      padding: 8px 10px;
      border-radius: 8px;
      background: #f8fafc;
      white-space: pre-wrap;
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
      align-items: center;
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
    .file-meta {
      min-width: 0;
      display: grid;
      gap: 3px;
      flex: 1;
    }
    .file-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 700;
    }
    .file-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .play-button,
    .danger-button {
      min-height: 30px;
      padding: 4px 9px;
      background: #fff;
      box-shadow: none;
      font-size: 12px;
    }
    .play-button {
      border-color: #bfdbfe;
      color: var(--accent-strong);
    }
    .play-button:hover:not(:disabled) {
      border-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent-strong);
    }
    .danger-button {
      border-color: #fecaca;
      color: var(--danger);
    }
    .danger-button:hover:not(:disabled) {
      border-color: var(--danger);
      background: #fff1f2;
      color: var(--danger);
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
        <p class="section-title">TikTok / 抖音链接下载</p>
        <label for="tiktokUrl">公开视频链接</label>
        <input id="tiktokUrl" type="url" placeholder="https://www.tiktok.com/@user/video/... 或 https://v.douyin.com/...">
      </div>
      <div class="row">
        <button id="downloadBtn" type="button">下载视频</button>
        <button id="networkCheckBtn" class="secondary" type="button">检测代理出口</button>
      </div>
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
        <button id="promptToggle" class="secondary" type="button">显示当前提示词</button>
      </div>
      <div id="promptPanel" class="prompt-panel">
        <label for="analysisPrompt">分析提示词</label>
        <textarea id="analysisPrompt" spellcheck="false"></textarea>
      </div>
      <div>
        <label class="check">
          <input id="autoPostprocess" type="checkbox">
          自动生成 DeepSeek 分析
        </label>
      </div>
      <div>
        <button id="manualPostprocessBtn" class="secondary" type="button" disabled>
          手动生成分析报告
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
    const state = { selectedFile: "", currentJob: null, currentResult: null, currentTab: "content", showOriginal: false, hasOutput: false };
    const fileList = document.getElementById("fileList");
    const statusBox = document.getElementById("statusBox");
    const outputBox = document.getElementById("outputBox");
    const currentFile = document.getElementById("currentFile");
    const tiktokUrl = document.getElementById("tiktokUrl");
    const downloadBtn = document.getElementById("downloadBtn");
    const networkCheckBtn = document.getElementById("networkCheckBtn");
    const analyzeBtn = document.getElementById("analyzeBtn");
    const sourceToggle = document.getElementById("sourceToggle");
    const outputTitle = document.getElementById("outputTitle");
    const autoPostprocess = document.getElementById("autoPostprocess");
    const manualPostprocessBtn = document.getElementById("manualPostprocessBtn");
    const promptToggle = document.getElementById("promptToggle");
    const promptPanel = document.getElementById("promptPanel");
    const analysisPrompt = document.getElementById("analysisPrompt");
    const dropOverlay = document.getElementById("dropOverlay");
    let dragDepth = 0;
    let downloadEvents = null;
    let jobEvents = null;

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

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function metric(label, value) {
      if (value === null || value === undefined || value === "") return "";
      return `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
    }

    function section(title, body) {
      const text = cleanText(body);
      if (!text) return "";
      return `<section class="report-section"><h3>${escapeHtml(title)}</h3><div class="body">${escapeHtml(text)}</div></section>`;
    }

    function listHtml(title, items, mapper = item => item) {
      if (!Array.isArray(items) || !items.length) return "";
      const rows = items.map((item, index) => cleanText(mapper(item, index))).filter(Boolean);
      if (!rows.length) return "";
      return `<section class="report-section"><h3>${escapeHtml(title)}</h3><ul class="report-list">${rows.map(row => `<li>${escapeHtml(row)}</li>`).join("")}</ul></section>`;
    }

    function formatExtractionReport(value) {
      if (!value || typeof value !== "object") return pretty(value);
      const metadata = value.metadata || {};
      const transcript = value.transcript || {};
      const videoDescription = cleanText(value.video_description);
      const frameAnalyses = Array.isArray(value.frame_analyses) ? value.frame_analyses : [];
      const timeline = Array.isArray(value.timeline) ? value.timeline : [];
      const visualEvidence = Array.isArray(value.visual_evidence) ? value.visual_evidence : [];
      const audioLanguage = transcript.language || metadata.audio_language;
      const transcriptSuccessful = transcript.successful === undefined ? metadata.transcription_successful : transcript.successful;
      const usage = value.usage || {};
      const timelineRows = timeline.map((item, index) => {
        if (typeof item === "string") return item;
        const range = item.time_range || item.timestamp || `片段 ${index + 1}`;
        const visual = item.visual ? `画面：${item.visual}` : "";
        const audio = item.audio ? `音频：${item.audio}` : "";
        return `${range}\n${[visual, audio].filter(Boolean).join("\n")}`;
      });
      const segmentRows = Array.isArray(transcript.segments) ? transcript.segments.map(segment => {
          const start = Number.isFinite(segment.start) ? segment.start.toFixed(2) : "?";
          const end = Number.isFinite(segment.end) ? segment.end.toFixed(2) : "?";
          return `${start}s-${end}s  ${segment.text || ""}`;
      }) : [];
      return `
        <article class="report-doc">
          <div class="report-hero">
            <div class="report-kicker">Qwen Video Extraction</div>
            <h2>提取内容报告</h2>
            <p>${escapeHtml(value.summary ? cleanText(value.summary).slice(0, 180) : "视频内容、画面证据和音频转写的结构化整理。")}</p>
          </div>
          <div class="report-grid">
            ${metric("处理模式", value.processing_mode)}
            ${metric("视觉模型", value.vision_model || metadata.model)}
            ${metric("音频模式", value.audio_mode)}
            ${metric("处理帧数", metadata.frames_processed || metadata.frames_extracted)}
            ${metric("音频语言", audioLanguage)}
            ${metric("转写成功", transcriptSuccessful === undefined ? "" : (transcriptSuccessful ? "是" : "否"))}
            ${metric("输入 Tokens", usage.input_tokens)}
            ${metric("输出 Tokens", usage.output_tokens)}
            ${metric("总 Tokens", usage.total_tokens)}
            ${metric("API 调用", usage.api_calls)}
            ${metric("总耗时", usage.elapsed_seconds === null || usage.elapsed_seconds === undefined ? "" : `${usage.elapsed_seconds}s`)}
          </div>
          ${section("模型总结", value.summary)}
          ${section("视频画面总述", videoDescription)}
          ${listHtml("时间线", timelineRows)}
          ${listHtml("视觉证据", visualEvidence, item => typeof item === "string" ? item : (item.description || item.visual || pretty(item)))}
          ${listHtml("逐帧视觉分析", frameAnalyses, (frame, index) => `[帧 ${index + 1}]\n${cleanText(frame)}`)}
          ${section("转写文本", transcript.text || "无转写文本")}
          ${listHtml("分段转写", segmentRows)}
        </article>
      `;
    }

    function formatAuditReport(value) {
      if (!value || typeof value !== "object") return pretty(value);
      return `
        <article class="report-doc">
          <div class="report-hero">
            <div class="report-kicker">DeepSeek Audit</div>
            <h2>分析结果报告</h2>
            <p>${escapeHtml(value.summary || "基于提取内容生成的风险和发布建议。")}</p>
          </div>
          <div class="report-grid">
            ${metric("风险等级", value.risk_level)}
            ${metric("建议动作", value.recommended_action)}
            ${metric("发布建议", value.publish_suggestion)}
          </div>
          ${section("内容摘要", value.summary)}
          ${section("内容概览", value.content_overview)}
          ${section("转写要点", value.transcript_notes)}
          ${section("画面要点", value.visual_notes)}
          ${listHtml("风险原因", value.risk_reasons)}
          ${listHtml("问题点", value.issues)}
        </article>
      `;
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
        if (state.showOriginal) outputBox.textContent = pretty(value);
        else outputBox.innerHTML = formatExtractionReport(value);
      } else {
        outputTitle.textContent = state.showOriginal ? "DeepSeek 分析内容，原文" : "DeepSeek 分析内容，中文";
        value = state.showOriginal ? job.audit_result : (job.audit_result_zh || job.audit_result);
        if (state.showOriginal) outputBox.textContent = pretty(value);
        else outputBox.innerHTML = formatAuditReport(value);
      }
      sourceToggle.textContent = state.showOriginal ? "显示中文" : "显示原文";
    }

    function hasResultPayload(result) {
      return Boolean(result && (result.analysis || result.analysis_zh || result.audit_result || result.audit_result_zh));
    }

    function renderAnalyzeButton() {
      analyzeBtn.textContent = state.hasOutput ? "重新分析" : "开始分析";
      analyzeBtn.disabled = !state.selectedFile;
      manualPostprocessBtn.disabled = !state.selectedFile || !state.hasOutput || Boolean(state.currentJob);
    }

    async function loadDefaultPrompt() {
      const response = await fetch("/api/prompt");
      const payload = await response.json();
      analysisPrompt.value = payload.prompt || "";
    }

    function togglePromptPanel() {
      const active = !promptPanel.classList.contains("active");
      promptPanel.classList.toggle("active", active);
      promptToggle.textContent = active ? "隐藏当前提示词" : "显示当前提示词";
      if (active) analysisPrompt.focus();
    }

    function promptFromResult(result) {
      const prompt = result && result.analysis && result.analysis.metadata && result.analysis.metadata.analysis_prompt;
      return typeof prompt === "string" && prompt.trim() ? prompt : "";
    }

    async function loadSavedResult(name) {
      if (!name) return;
      const response = await fetch(`/api/result?filename=${encodeURIComponent(name)}`);
      const result = await response.json();
      if (hasResultPayload(result)) {
        state.currentJob = null;
        state.currentResult = result;
        state.hasOutput = true;
        const savedPrompt = promptFromResult(result);
        if (savedPrompt) analysisPrompt.value = savedPrompt;
        renderOutput(result);
        setStatus(`${name}: 已加载已有输出`, "ok");
      } else {
        state.currentResult = null;
        state.hasOutput = false;
        outputBox.className = "report-output";
        outputBox.textContent = "{}";
        setStatus(`${name}: 等待分析`);
      }
      renderAnalyzeButton();
    }

    function selectFile(name) {
      state.selectedFile = name;
      currentFile.textContent = name || "未选择视频";
      state.hasOutput = false;
      renderAnalyzeButton();
      [...fileList.children].forEach(item => item.classList.toggle("selected", item.dataset.name === name));
      loadSavedResult(name).catch(error => setStatus(error.message, "bad"));
    }

    async function deleteFile(name) {
      if (!name) return;
      if (!confirm(`删除 ${name} 及其所有分析输出？`)) return;
      const response = await fetch("/api/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: name })
      });
      const payload = await response.json();
      if (!response.ok) {
        setStatus(payload.error || "删除失败", "bad");
        return;
      }
      if (state.selectedFile === name) {
        state.selectedFile = "";
        state.currentResult = null;
        state.currentJob = null;
        state.hasOutput = false;
        outputBox.className = "report-output";
        outputBox.textContent = "{}";
      }
      setStatus(`${name}: 已删除`, "ok");
      await refreshFiles();
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
        const item = document.createElement("div");
        item.className = "file-item";
        item.tabIndex = 0;
        item.dataset.name = file.name;
        item.innerHTML = `
          <span class="file-meta">
            <span class="file-name">${escapeHtml(file.name)}</span>
            <span class="muted">${Math.round(file.size / 1024 / 1024 * 10) / 10} MB</span>
          </span>
          <span class="file-actions">
            <button class="play-button" type="button">播放</button>
            <button class="danger-button" type="button">删除</button>
          </span>
        `;
        item.onclick = () => selectFile(file.name);
        item.onkeydown = event => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectFile(file.name);
          }
        };
        item.querySelector(".danger-button").onclick = event => {
          event.stopPropagation();
          deleteFile(file.name).catch(error => setStatus(error.message, "bad"));
        };
        item.querySelector(".play-button").onclick = event => {
          event.stopPropagation();
          window.open(`/video/${encodeURIComponent(file.name)}`, "_blank", "noopener");
        };
        fileList.appendChild(item);
      });
      if (!state.selectedFile || !files.some(file => file.name === state.selectedFile)) {
        selectFile(files[0].name);
      } else {
        selectFile(state.selectedFile);
      }
    }

    async function startDownload() {
      const url = tiktokUrl.value.trim();
      if (!url) {
        setStatus("请输入 TikTok 或抖音视频链接。", "bad");
        return;
      }
      downloadBtn.disabled = true;
      setStatus("视频下载任务已提交...");
      const response = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url })
      });
      const job = await response.json();
      if (!response.ok) {
        setStatus(job.error || "下载任务提交失败", "bad");
        downloadBtn.disabled = false;
        return;
      }
      openDownloadEvents(job.id);
    }

    function formatRouteCheck(route) {
      if (!route) return "未配置";
      if (!route.ok) return `失败：${route.error || "未知错误"}`;
      const country = route.country_name ? `${route.country_name} (${route.country})` : route.country;
      return `${route.ip || "未知 IP"} / ${country || "未知地区"} / ${route.is_us ? "美国出口" : "非美国出口"}`;
    }

    async function checkNetwork() {
      networkCheckBtn.disabled = true;
      setStatus("正在检测服务器外网和代理出口...");
      try {
        const response = await fetch("/api/network-check");
        const payload = await response.json();
        if (!response.ok) {
          setStatus(payload.error || "代理出口检测失败", "bad");
          return;
        }
        const direct = formatRouteCheck(payload.direct);
        const proxy = formatRouteCheck(payload.proxy);
        const kind = payload.proxy && payload.proxy.ok && payload.proxy.is_us ? "ok" : "bad";
        setStatus(`直连：${direct}；代理：${proxy}`, kind);
      } catch (error) {
        setStatus(`代理出口检测失败：${error.message}`, "bad");
      } finally {
        networkCheckBtn.disabled = false;
      }
    }

    function latestJobLog(job) {
      if (!job || !Array.isArray(job.log) || !job.log.length) return "";
      const line = job.log[job.log.length - 1] || "";
      return line.length > 180 ? `${line.slice(0, 180)}...` : line;
    }

    function closeDownloadEvents() {
      if (downloadEvents) {
        downloadEvents.close();
        downloadEvents = null;
      }
    }

    function openDownloadEvents(id) {
      closeDownloadEvents();
      downloadEvents = new EventSource(`/api/download-events?id=${encodeURIComponent(id)}`);
      downloadEvents.onmessage = async event => {
        const job = JSON.parse(event.data);
        handleDownloadJob(job);
      };
      downloadEvents.onerror = () => {
        closeDownloadEvents();
        downloadBtn.disabled = false;
        setStatus("视频下载连接中断，请刷新后查看任务结果。", "bad");
      };
    }

    async function handleDownloadJob(job) {
      if (job.status === "missing") {
        closeDownloadEvents();
        setStatus(job.error || "下载任务不存在", "bad");
        downloadBtn.disabled = false;
        return;
      }
      if (job.status === "running" || job.status === "queued") {
        const log = latestJobLog(job);
        setStatus(`视频下载中：${job.status}${log ? ` - ${log}` : ""}`);
        return;
      }
      closeDownloadEvents();
      downloadBtn.disabled = false;
      if (job.status !== "complete") {
        setStatus(`视频下载失败：${job.error || "未知错误"}`, "bad");
        return;
      }
      setStatus(`${job.filename}: 下载完成`, "ok");
      tiktokUrl.value = "";
      await refreshFiles();
      selectFile(job.filename);
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
      manualPostprocessBtn.disabled = true;
      const resetOutput = state.hasOutput;
      setStatus(resetOutput ? "正在清空旧输出并重新分析..." : "分析任务已提交...");
      const response = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: state.selectedFile,
          analysis_mode: document.getElementById("analysisMode").value,
          analysis_prompt: analysisPrompt.value,
          postprocess: autoPostprocess.checked,
          reset_output: resetOutput
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
      state.hasOutput = false;
      openJobEvents(job.id);
    }

    async function startManualPostprocess() {
      if (!state.selectedFile || !state.hasOutput) return;
      manualPostprocessBtn.disabled = true;
      setStatus("DeepSeek 分析任务已提交...");
      const response = await fetch("/api/postprocess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: state.selectedFile })
      });
      const job = await response.json();
      if (!response.ok) {
        setStatus(job.error || "提交失败", "bad");
        renderAnalyzeButton();
        return;
      }
      state.currentJob = job.id;
      state.currentResult = job;
      state.currentTab = "audit";
      state.showOriginal = false;
      document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item.dataset.tab === "audit"));
      openJobEvents(job.id);
    }

    function closeJobEvents() {
      if (jobEvents) {
        jobEvents.close();
        jobEvents = null;
      }
    }

    function openJobEvents(id) {
      closeJobEvents();
      jobEvents = new EventSource(`/api/job-events?id=${encodeURIComponent(id)}`);
      jobEvents.onmessage = event => {
        const job = JSON.parse(event.data);
        handleJobUpdate(job);
      };
      jobEvents.onerror = () => {
        closeJobEvents();
        analyzeBtn.disabled = !state.selectedFile;
        renderAnalyzeButton();
        setStatus("分析任务连接中断，请刷新后查看任务结果。", "bad");
      };
    }

    function handleJobUpdate(job) {
      if (job.status === "missing") {
        closeJobEvents();
        state.currentJob = null;
        analyzeBtn.disabled = !state.selectedFile;
        renderAnalyzeButton();
        setStatus(job.error || "任务不存在", "bad");
        return;
      }
      state.currentResult = job;
      renderOutput(job);
      if (job.status === "running" || job.status === "queued") {
        const log = latestJobLog(job);
        setStatus(`${job.filename}: ${job.status}${log ? ` - ${log}` : ""}`);
        return;
      }
      closeJobEvents();
      state.currentJob = null;
      analyzeBtn.disabled = !state.selectedFile;
      state.hasOutput = job.status === "complete" || hasResultPayload(job);
      renderAnalyzeButton();
      setStatus(job.status === "complete" ? `${job.filename}: 完成` : `${job.filename}: ${job.error || "失败"}`, job.status === "complete" ? "ok" : "bad");
    }

    downloadBtn.onclick = startDownload;
    networkCheckBtn.onclick = checkNetwork;
    tiktokUrl.onkeydown = event => {
      if (event.key === "Enter") {
        event.preventDefault();
        startDownload();
      }
    };
    document.getElementById("uploadBtn").onclick = () => uploadVideo();
    document.getElementById("refreshBtn").onclick = refreshFiles;
    document.getElementById("analysisMode").value = window.DEFAULT_ANALYSIS_MODE || "analyzer";
    manualPostprocessBtn.onclick = startManualPostprocess;
    promptToggle.onclick = togglePromptPanel;
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
        renderOutput(state.currentResult);
      };
    });
    refreshFiles().catch(error => setStatus(error.message, "bad"));
    loadDefaultPrompt().catch(error => setStatus(error.message, "bad"));
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
