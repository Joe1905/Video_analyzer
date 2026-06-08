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
from html import escape as html_escape


ROOT = Path.cwd()
VIDEOS_DIR = ROOT / "videos"
OUTPUT_DIR = ROOT / "output"
SCRIPTS_DIR = ROOT / "scripts"
INDEX_HTML_PATH = SCRIPTS_DIR / "web_index.html"
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


@dataclass
class ShopJob:
    id: str
    url: str
    source_type: str
    region: str
    max_pages: int
    review_pages: int
    analyze: bool
    related_videos: bool
    prompt: str = ""
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


@dataclass
class MetricsJob:
    id: str
    url: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
download_jobs: dict[str, DownloadJob] = {}
download_jobs_lock = threading.Lock()
shop_jobs: dict[str, ShopJob] = {}
shop_jobs_lock = threading.Lock()
metrics_jobs: dict[str, MetricsJob] = {}
metrics_jobs_lock = threading.Lock()


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


def binary_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
    filename: str | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        quoted = filename.replace('"', "")
        handler.send_header("Content-Disposition", f'attachment; filename="{quoted}"')
    handler.end_headers()
    handler.wfile.write(body)


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


def clean_report_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("```"):
            stripped = stripped.removeprefix("```json").removeprefix("```").strip()
            stripped = stripped.removesuffix("```").strip()
        return stripped
    return json.dumps(value, ensure_ascii=False, indent=2)


def report_section(title: str, value: Any) -> str:
    text = clean_report_value(value)
    if not text:
        return ""
    return (
        '<section class="report-section">'
        f"<h3>{html_escape(title)}</h3>"
        f'<div class="content">{html_escape(text)}</div>'
        "</section>"
    )


def report_list(title: str, values: Any) -> str:
    if not isinstance(values, list) or not values:
        return ""
    lines: list[str] = []
    for item in values:
        if isinstance(item, dict):
            parts = []
            if item.get("time_range") or item.get("timestamp"):
                parts.append(str(item.get("time_range") or item.get("timestamp")))
            if item.get("visual") or item.get("description"):
                parts.append(f"画面：{item.get('visual') or item.get('description')}")
            if item.get("audio"):
                parts.append(f"音频：{item.get('audio')}")
            lines.append("\n".join(parts) or clean_report_value(item))
        else:
            lines.append(clean_report_value(item))
    return report_section(title, "\n\n".join(f"- {line}" for line in lines if line))


def metric_item(label: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    return (
        '<div class="metric">'
        f"<span>{html_escape(label)}</span>"
        f"<b>{html_escape(str(value))}</b>"
        "</div>"
    )


def build_report_html(filename: str, tab: str, payload: dict[str, Any]) -> str:
    is_audit = tab == "audit"
    title = "分析结果报告" if is_audit else "提取内容报告"
    eyebrow = "DeepSeek Audit" if is_audit else "Qwen Video Extraction"
    summary = clean_report_value(payload.get("summary")) or "暂无摘要。"

    if is_audit:
        metrics = "".join(
            [
                metric_item("风险等级", payload.get("risk_level")),
                metric_item("建议动作", payload.get("recommended_action")),
                metric_item("发布建议", payload.get("publish_suggestion")),
            ]
        )
        sections = "".join(
            [
                report_section("内容摘要", payload.get("summary")),
                report_section("内容概览", payload.get("content_overview")),
                report_section("转写要点", payload.get("transcript_notes")),
                report_section("画面要点", payload.get("visual_notes")),
                report_list("风险原因", payload.get("risk_reasons")),
                report_list("问题点", payload.get("issues")),
            ]
        )
    else:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        transcript = payload.get("transcript") if isinstance(payload.get("transcript"), dict) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        metrics = "".join(
            [
                metric_item("处理模式", payload.get("processing_mode")),
                metric_item("视觉模型", payload.get("vision_model") or metadata.get("model")),
                metric_item("音频模式", payload.get("audio_mode")),
                metric_item("处理帧数", metadata.get("frames_processed") or metadata.get("frames_extracted")),
                metric_item("音频语言", transcript.get("language") or metadata.get("audio_language")),
                metric_item("输入 Tokens", usage.get("input_tokens")),
                metric_item("输出 Tokens", usage.get("output_tokens")),
                metric_item("总 Tokens", usage.get("total_tokens")),
                metric_item("API 调用", usage.get("api_calls")),
                metric_item("总耗时", f"{usage.get('elapsed_seconds')}s" if usage.get("elapsed_seconds") is not None else None),
            ]
        )
        sections = "".join(
            [
                report_section("模型总结", payload.get("summary")),
                report_section(
                    "视频画面总述",
                    payload.get("video_description")
                    or payload.get("opening_description")
                    or payload.get("narrative_development"),
                ),
                report_list("时间线", payload.get("timeline")),
                report_list("视觉证据", payload.get("visual_evidence")),
                report_section("转写文本", transcript.get("text") or "无转写文本"),
            ]
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
body{{margin:0;background:#f6f8fb;color:#111827;font-family:"Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif}}
.page{{padding:34px}}.doc-head{{margin-bottom:18px;padding-bottom:14px;border-bottom:2px solid #1d4ed8}}
.doc-head h1{{margin:0;font-size:26px}}.doc-head p{{margin:8px 0 0;color:#64748b}}
.report{{display:flex;flex-direction:column;gap:14px}}.hero{{border:1px solid #d6deea;border-radius:12px;padding:18px;background:#fff}}
.eyebrow{{color:#64748b;font-size:12px;font-weight:800;text-transform:uppercase}}.hero h2{{margin:8px 0;font-size:24px}}
.hero p{{margin:0;line-height:1.75}}.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.metric,.report-section{{border:1px solid #d6deea;border-radius:10px;background:#fff}}.metric{{padding:11px}}
.metric span{{display:block;color:#64748b;font-size:12px;font-weight:700}}.metric b{{display:block;margin-top:5px}}
.report-section{{overflow:hidden;break-inside:avoid}}.report-section h3{{margin:0;padding:11px 13px;border-bottom:1px solid #d6deea;background:#f8fafc;font-size:15px}}
.report-section .content{{padding:12px 13px;line-height:1.8;white-space:pre-wrap}}
</style>
</head>
<body>
<main class="page">
<div class="doc-head"><h1>{html_escape(title)} - {html_escape(filename)}</h1><p>导出时间：{time.strftime("%Y-%m-%d %H:%M:%S")}</p></div>
<article class="report">
<div class="hero"><div class="eyebrow">{html_escape(eyebrow)}</div><h2>{html_escape(title)}</h2><p>{html_escape(summary)}</p></div>
<div class="metrics">{metrics}</div>
{sections}
</article>
</main>
</body>
</html>"""


def render_pdf_bytes(html: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1240, "height": 1754})
            page.set_content(html, wait_until="load")
            return page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "14mm", "right": "14mm", "bottom": "14mm", "left": "14mm"},
            )
        finally:
            browser.close()


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


def append_shop_log(job: ShopJob, line: str) -> None:
    with shop_jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_shop_command(job: ShopJob, command: list[str]) -> None:
    append_shop_log(job, f"$ {' '.join(command)}")
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
        append_shop_log(job, line)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def run_shop_job(job_id: str) -> None:
    with shop_jobs_lock:
        job = shop_jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    output_dir = OUTPUT_DIR / "tiktok_shop" / job_id
    extract_path = output_dir / "shop_extract.json"
    analysis_path = output_dir / "shop_analysis.json"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with shop_jobs_lock:
            job.output_dir = str(output_dir.relative_to(ROOT))
            job.updated_at = time.time()

        command = [
            "python",
            str(SCRIPTS_DIR / "sociavault_tiktok_shop.py"),
            job.url,
            "--source-type",
            job.source_type,
            "--region",
            job.region,
            "--max-pages",
            str(job.max_pages),
            "--review-pages",
            str(job.review_pages),
            "--output",
            str(extract_path),
        ]
        if job.related_videos:
            command.append("--related-videos")
        run_shop_command(job, command)

        if job.analyze:
            run_shop_command(
                job,
                [
                    "python",
                    str(SCRIPTS_DIR / "deepseek_shop_analyze.py"),
                    str(extract_path),
                    "--output",
                    str(analysis_path),
                    "--prompt",
                    job.prompt,
                ],
            )

        with shop_jobs_lock:
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        with shop_jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


def append_metrics_log(job: MetricsJob, line: str) -> None:
    with metrics_jobs_lock:
        job.log.append(line.rstrip())
        job.updated_at = time.time()


def run_metrics_command(job: MetricsJob, command: list[str]) -> None:
    append_metrics_log(job, f"$ {' '.join(command)}")
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
        append_metrics_log(job, line)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {' '.join(command)}")


def run_metrics_job(job_id: str) -> None:
    with metrics_jobs_lock:
        job = metrics_jobs[job_id]
        job.status = "running"
        job.updated_at = time.time()

    output_dir = OUTPUT_DIR / "social_metrics" / job_id
    metrics_path = output_dir / "metrics.json"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with metrics_jobs_lock:
            job.output_dir = str(output_dir.relative_to(ROOT))
            job.updated_at = time.time()

        run_metrics_command(
            job,
            [
                "python",
                str(SCRIPTS_DIR / "social_video_metrics.py"),
                job.url,
                "--output",
                str(metrics_path),
            ],
        )

        with metrics_jobs_lock:
            job.status = "complete"
            job.updated_at = time.time()
    except Exception as exc:
        with metrics_jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()
            job.log.append(str(exc))


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


def public_shop_job(job: ShopJob) -> dict[str, Any]:
    output_dir = OUTPUT_DIR / "tiktok_shop" / job.id
    return {
        "id": job.id,
        "url": job.url,
        "source_type": job.source_type,
        "region": job.region,
        "max_pages": job.max_pages,
        "review_pages": job.review_pages,
        "analyze": job.analyze,
        "related_videos": job.related_videos,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-120:],
        "extract": read_json(output_dir / "shop_extract.json"),
        "analysis": read_json(output_dir / "shop_analysis.json"),
    }


def public_metrics_job(job: MetricsJob) -> dict[str, Any]:
    output_dir = OUTPUT_DIR / "social_metrics" / job.id
    return {
        "id": job.id,
        "url": job.url,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "output_dir": job.output_dir,
        "error": job.error,
        "log": job.log[-120:],
        "metrics": read_json(output_dir / "metrics.json"),
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
            template = INDEX_HTML_PATH.read_text(encoding="utf-8") if INDEX_HTML_PATH.is_file() else INDEX_HTML
            html = template.replace(
                "__DEFAULT_ANALYSIS_MODE__",
                os.getenv("ANALYSIS_MODE", "analyzer"),
            )
            return text_response(self, HTTPStatus.OK, html, "text/html; charset=utf-8")
        if parsed.path == "/shop":
            return text_response(self, HTTPStatus.OK, SHOP_HTML, "text/html; charset=utf-8")
        if parsed.path == "/metrics":
            return text_response(self, HTTPStatus.OK, METRICS_HTML, "text/html; charset=utf-8")
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
        if parsed.path == "/api/shop-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with shop_jobs_lock:
                job = shop_jobs.get(job_id)
                payload = public_shop_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "TikTok Shop job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/shop-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_shop_events(job_id)
        if parsed.path == "/api/video-metrics-job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with metrics_jobs_lock:
                job = metrics_jobs.get(job_id)
                payload = public_metrics_job(job) if job else None
            if payload is None:
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Video metrics job not found"})
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path == "/api/video-metrics-events":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            return self.stream_metrics_events(job_id)
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
        if parsed.path == "/api/export-pdf":
            query = parse_qs(parsed.query)
            try:
                filename = safe_filename(query.get("filename", [""])[0])
            except ValueError as exc:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            tab = query.get("tab", ["audit"])[0]
            if tab not in {"audit", "content"}:
                return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid tab"})
            output_dir = OUTPUT_DIR / filename
            source = "audit_result_zh.json" if tab == "audit" else "analysis_zh.json"
            fallback = "audit_result.json" if tab == "audit" else "analysis.json"
            payload = read_json(output_dir / source) or read_json(output_dir / fallback)
            if not isinstance(payload, dict):
                return json_response(self, HTTPStatus.NOT_FOUND, {"error": f"Report not found for {filename}"})
            try:
                html = build_report_html(filename, tab, payload)
                pdf = render_pdf_bytes(html)
            except Exception as exc:
                return json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"PDF export failed: {exc}"})
            suffix = "audit" if tab == "audit" else "analysis"
            return binary_response(
                self,
                HTTPStatus.OK,
                pdf,
                "application/pdf",
                filename=f"{filename}.{suffix}.pdf",
            )
        return json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def stream_job_events(self, job_id: str) -> None:
        self.stream_events(job_id, jobs_lock, jobs, public_job, "Job not found")

    def stream_download_events(self, job_id: str) -> None:
        self.stream_events(job_id, download_jobs_lock, download_jobs, public_download_job, "Download job not found")

    def stream_shop_events(self, job_id: str) -> None:
        self.stream_events(job_id, shop_jobs_lock, shop_jobs, public_shop_job, "TikTok Shop job not found")

    def stream_metrics_events(self, job_id: str) -> None:
        self.stream_events(job_id, metrics_jobs_lock, metrics_jobs, public_metrics_job, "Video metrics job not found")

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
        if parsed.path == "/api/shop-extract":
            return self.handle_shop_extract()
        if parsed.path == "/api/video-metrics":
            return self.handle_video_metrics()
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
        attempted_url = ""
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            attempted_url = str(payload.get("url", ""))
            url = validate_short_video_url(attempted_url)
        except (json.JSONDecodeError, ValueError) as exc:
            job = DownloadJob(id=str(uuid.uuid4()), url=attempted_url, status="failed")
            job.error = str(exc)
            job.log.append(str(exc))
            with download_jobs_lock:
                download_jobs[job.id] = job
                write_download_job_log(job)
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = DownloadJob(id=str(uuid.uuid4()), url=url)
        with download_jobs_lock:
            download_jobs[job.id] = job
        thread = threading.Thread(target=run_download_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_download_job(job))

    def handle_shop_extract(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            url = str(payload.get("url", "")).strip()
            source_type = str(payload.get("source_type") or "product")
            region = str(payload.get("region") or os.getenv("SOCIAVAULT_REGION", "US")).strip().upper()
            max_pages = int(payload.get("max_pages") or os.getenv("SOCIAVAULT_MAX_PAGES", "1"))
            review_pages = int(payload.get("review_pages") or os.getenv("SOCIAVAULT_REVIEW_PAGES", "1"))
            prompt = str(payload.get("prompt") or "").strip()
            analyze = bool(payload.get("analyze", True))
            related_videos = bool(payload.get("related_videos", False))
            if source_type not in {"product", "details", "reviews", "shop", "search"}:
                raise ValueError("source_type must be product, details, reviews, shop, or search")
            if not url or len(url) > 2048:
                raise ValueError("A TikTok Shop URL is required")
            if max_pages < 1 or max_pages > 20:
                raise ValueError("max_pages must be between 1 and 20")
            if review_pages < 0 or review_pages > 20:
                raise ValueError("review_pages must be between 0 and 20")
            if len(prompt) > 6000:
                raise ValueError("prompt is too long")
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = ShopJob(
            id=str(uuid.uuid4()),
            url=url,
            source_type=source_type,
            region=region,
            max_pages=max_pages,
            review_pages=review_pages,
            analyze=analyze,
            related_videos=related_videos,
            prompt=prompt,
        )
        with shop_jobs_lock:
            shop_jobs[job.id] = job
        thread = threading.Thread(target=run_shop_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_shop_job(job))

    def handle_video_metrics(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            url = validate_short_video_url(str(payload.get("url", "")))
        except (json.JSONDecodeError, ValueError) as exc:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = MetricsJob(id=str(uuid.uuid4()), url=url)
        with metrics_jobs_lock:
            metrics_jobs[job.id] = job
        thread = threading.Thread(target=run_metrics_job, args=(job.id,), daemon=True)
        thread.start()
        return json_response(self, HTTPStatus.ACCEPTED, public_metrics_job(job))

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
        try:
            raw_file_items = form["video"]
        except KeyError:
            raw_file_items = []
        if not isinstance(raw_file_items, list):
            raw_file_items = [raw_file_items]
        file_items = [item for item in raw_file_items if getattr(item, "filename", None)]
        if not file_items:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Missing video file"})

        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        files = []
        errors = []
        for file_item in file_items:
            original_name = str(getattr(file_item, "filename", ""))
            try:
                filename = safe_filename(original_name)
                target = VIDEOS_DIR / filename
                with target.open("wb") as file:
                    shutil.copyfileobj(file_item.file, file)
                files.append({"filename": filename, "size": target.stat().st_size})
            except Exception as exc:
                errors.append({"filename": original_name, "error": str(exc)})

        status = HTTPStatus.OK if files else HTTPStatus.BAD_REQUEST
        return json_response(self, status, {"files": files, "errors": errors})

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


METRICS_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Video Metrics Extractor</title>
  <style>
    :root{--bg:#eef2f6;--card:#fff;--line:#d6deea;--text:#111827;--muted:#667589;--blue:#1d4ed8;--soft:#f6f8fb;--red:#b42318;--green:#047857;--dark:#101827}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Aptos,"Segoe UI",system-ui,sans-serif}header{height:64px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;background:#fff;border-bottom:1px solid var(--line)}main{display:grid;grid-template-columns:360px minmax(0,1fr);gap:16px;padding:18px;height:calc(100vh - 64px)}section{border:1px solid var(--line);border-radius:10px;background:var(--card);overflow:hidden}.side{padding:18px;display:flex;flex-direction:column;gap:14px}.workspace{display:grid;grid-template-rows:auto minmax(0,1fr);min-height:0}.head{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;align-items:center}.grid{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:16px;padding:16px;min-height:0}.card{display:grid;grid-template-rows:auto minmax(0,1fr);min-height:0;border:1px solid var(--line);border-radius:10px;background:#fff;overflow:hidden}.card h3{margin:0;padding:12px 14px;border-bottom:1px solid var(--line);background:var(--soft);font-size:15px}label{display:grid;gap:7px;color:#475569;font-weight:700;font-size:13px}input{width:100%;border:1px solid #c6d1df;border-radius:8px;padding:10px 12px}button{min-height:38px;border:1px solid var(--blue);border-radius:8px;background:var(--blue);color:#fff;padding:8px 13px;font-weight:800;cursor:pointer}button.secondary{background:#fff;color:var(--text);border-color:var(--line)}button:disabled{opacity:.55;cursor:not-allowed}.status{padding:11px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--muted);overflow-wrap:anywhere}.status.ok{background:#ecfdf5;color:var(--green)}.status.bad{background:#fff1f2;color:var(--red)}.muted{color:var(--muted);font-size:13px}pre{margin:0;min-height:0;overflow:auto;padding:14px;white-space:pre-wrap;word-break:break-word}.log{background:var(--dark);color:#dbeafe;font:12px/1.6 Consolas,monospace}.report{padding:16px;overflow:auto;line-height:1.65}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}.metric{border:1px solid var(--line);border-radius:8px;padding:10px;background:#fff}.metric span{display:block;color:var(--muted);font-size:12px}.metric b{display:block;margin-top:4px}.block{border:1px solid var(--line);border-radius:8px;padding:12px;background:#fbfcfe;margin-top:10px}.tabs{display:flex;gap:8px}.tab{background:#fff;color:var(--text);border-color:var(--line)}.tab.active{background:var(--blue);color:#fff;border-color:var(--blue)}@media(max-width:980px){main,.grid{grid-template-columns:1fr;height:auto}main{height:auto}.card{min-height:360px}}
  </style>
</head>
<body>
  <header><h1>短视频互动数据提取</h1><a class="muted" href="/">返回视频分析</a></header>
  <main>
    <section class="side">
      <label>公开视频链接<input id="url" placeholder="https://www.tiktok.com/@user/video/... 或 https://www.douyin.com/video/..."></label>
      <button id="run" type="button">提取数据</button>
      <div id="status" class="status">等待输入 TikTok / 抖音视频链接。</div>
      <p class="muted">使用 Scrapling MCP 按 get / fetch / stealthy_fetch 链路抓取页面公开数据；TikTok 会额外用 yt-dlp 补充点赞、评论、播放等字段。登录、风控或地区限制会导致部分字段为空。</p>
    </section>
    <section class="workspace">
      <div class="head">
        <div><b>结果</b><div id="output" class="muted">结果保存到 output/social_metrics/&lt;job-id&gt;/</div></div>
        <div class="tabs"><button class="tab active" data-tab="report">报告</button><button class="tab" data-tab="json">JSON</button></div>
      </div>
      <div class="grid">
        <article class="card"><h3>公开视频数据</h3><div id="result" class="report">暂无结果。</div></article>
        <article class="card"><h3>任务日志</h3><pre id="log" class="log">等待任务...</pre></article>
      </div>
    </section>
  </main>
  <script>
    const runBtn=document.querySelector("#run"),statusBox=document.querySelector("#status"),logBox=document.querySelector("#log"),resultBox=document.querySelector("#result"),outputBox=document.querySelector("#output");
    const state={job:null,events:null,tab:"report",lastLogLength:0};
    function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]))}
    function pretty(v){return JSON.stringify(v??{},null,2)}
    function setStatus(m,k=""){statusBox.className=`status ${k}`.trim();statusBox.textContent=m}
    function addLog(lines){const next=Array.isArray(lines)?lines:[lines];if(!next.length)return;const cur=logBox.textContent==="等待任务..."?"":logBox.textContent;logBox.textContent=`${cur}${cur?"\n":""}${next.join("\n")}`;logBox.scrollTop=logBox.scrollHeight}
    function metric(label,value){return value===undefined||value===null||value===""?"":`<div class="metric"><span>${esc(label)}</span><b>${esc(value)}</b></div>`}
    function block(title,value){if(!value||typeof value!=="object")return"";return `<div class="block"><b>${esc(title)}</b><pre>${esc(pretty(value))}</pre></div>`}
    function renderReport(data){if(!data)return"暂无结果。";const m=data.metrics||{},a=data.author||{},meta=data.page_meta||{},fetch=data.page_fetch||{};return `<div class="metrics">${metric("平台",data.platform)}${metric("点赞",m.like_count)}${metric("评论",m.comment_count)}${metric("分享/转发",m.share_count||m.repost_count)}${metric("播放",m.play_count||m.view_count)}${metric("收藏",m.favorite_count)}${metric("粉丝",a.follower_count||a.channel_follower_count)}${metric("作品数",a.video_count)}${metric("抓取器",fetch.fetcher)}</div>${block("作者",a)}${block("页面信息",meta)}${block("抓取诊断",fetch)}${data.yt_dlp_error?`<div class="block"><b>yt-dlp 提示</b><pre>${esc(data.yt_dlp_error)}</pre></div>`:""}`}
    function render(){const data=state.job&&state.job.metrics;if(state.tab==="json"){resultBox.innerHTML=`<pre>${esc(pretty(data))}</pre>`;return}resultBox.innerHTML=renderReport(data)}
    function closeEvents(){if(state.events){state.events.close();state.events=null}}
    function handleJob(job){state.job=job;if(job.output_dir)outputBox.textContent=`结果目录：${job.output_dir}`;if(Array.isArray(job.log)&&job.log.length>state.lastLogLength){addLog(job.log.slice(state.lastLogLength));state.lastLogLength=job.log.length}render();if(job.status==="queued"||job.status==="running"){setStatus(`任务运行中：${job.status}`);return}closeEvents();runBtn.disabled=false;setStatus(job.status==="complete"?"提取完成。":(job.error||"提取失败。"),job.status==="complete"?"ok":"bad")}
    async function start(){const url=document.querySelector("#url").value.trim();if(!url)return setStatus("请输入视频链接。","bad");closeEvents();state.job=null;state.lastLogLength=0;logBox.textContent="提交任务...";resultBox.textContent="暂无结果。";runBtn.disabled=true;setStatus("正在提交任务...");const r=await fetch("/api/video-metrics",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});const job=await r.json();if(!r.ok){runBtn.disabled=false;setStatus(job.error||"提交失败。","bad");addLog(job.error||"提交失败。");return}handleJob(job);state.events=new EventSource(`/api/video-metrics-events?id=${encodeURIComponent(job.id)}`);state.events.onmessage=e=>handleJob(JSON.parse(e.data));state.events.onerror=()=>{closeEvents();runBtn.disabled=false;setStatus("任务连接中断。","bad")}}
    runBtn.onclick=()=>start().catch(e=>{runBtn.disabled=false;setStatus(e.message,"bad");addLog(e.message)});
    document.querySelectorAll(".tab").forEach(btn=>btn.onclick=()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));btn.classList.add("active");state.tab=btn.dataset.tab;render()});
  </script>
</body>
</html>"""

INDEX_HTML = '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Short Video Analyzer</title>\n<style>\n:root{--bg:#eef3f8;--card:#fff;--soft:#f7f9fc;--line:#d7e0ec;--text:#142033;--muted:#607089;--blue:#2563eb;--blue2:#1d4ed8;--blueSoft:#eaf1ff;--red:#b42318;--green:#087443;--dark:#0d1628;--shadow:0 18px 45px rgba(15,23,42,.10)}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,rgba(37,99,235,.10),transparent 34%),var(--bg);color:var(--text);font-family:"Segoe UI",system-ui,sans-serif}header{height:66px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.92);position:sticky;top:0;z-index:5}h1{font-size:20px;margin:0}.page{display:none;min-height:calc(100vh - 66px);padding:18px}.page.active{display:block}.grid{display:grid;grid-template-columns:minmax(320px,430px) minmax(0,1fr);gap:18px}.detail-grid{display:grid;grid-template-columns:minmax(260px,360px) minmax(0,1fr);gap:18px;height:calc(100vh - 102px)}.card{border:1px solid var(--line);border-radius:12px;background:var(--card);box-shadow:var(--shadow);overflow:hidden}.stack{display:grid;gap:16px;padding:18px}.title{font-weight:800;margin:0 0 10px}label{display:block;margin-bottom:7px;color:var(--muted);font-size:13px;font-weight:650}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--text);outline:none}input,select{min-height:40px;padding:8px 11px}textarea{min-height:170px;padding:10px 12px;resize:vertical;font:13px/1.55 Consolas,monospace}button{min-height:40px;border:1px solid var(--blue);border-radius:9px;background:var(--blue);color:#fff;padding:8px 13px;font-weight:750;cursor:pointer;box-shadow:0 8px 18px rgba(37,99,235,.18)}button.secondary{background:#fff;color:var(--blue);box-shadow:none}button.danger{background:#fff;border-color:#fecaca;color:var(--red);box-shadow:none}button.small{min-height:32px;padding:5px 10px;font-size:13px}button:disabled{opacity:.55;cursor:not-allowed}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.muted{color:var(--muted)}.status{min-height:42px;border:1px solid var(--line);border-radius:9px;padding:10px 12px;background:var(--soft);color:var(--muted);font-size:13px;overflow-wrap:anywhere}.status.ok{background:#ecfdf3;color:var(--green)}.status.bad{background:#fff1f2;color:var(--red)}.check{display:flex;align-items:center;gap:9px;color:var(--text);font-size:14px;font-weight:650}.check input{width:auto;min-height:auto}.prompt{display:none}.prompt.active{display:block}.log-wrap{display:grid;grid-template-rows:auto minmax(360px,1fr);min-height:calc(100vh - 102px)}.head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 18px;border-bottom:1px solid var(--line);background:#fff}.head h2{margin:0;font-size:18px}.log{margin:0;overflow:auto;padding:18px;background:var(--dark);color:#e6edf7;font:13px/1.7 Consolas,monospace;white-space:pre-wrap;word-break:break-word}.files{display:grid;gap:8px;max-height:260px;overflow:auto}.detail-files{padding:14px;overflow:auto}.file{display:flex;justify-content:space-between;align-items:center;gap:12px;border:1px solid var(--line);border-radius:9px;padding:10px;background:#fff;cursor:pointer}.file.selected{border-color:var(--blue);background:var(--blueSoft)}.file-name{font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-meta{min-width:0;display:grid;gap:4px}.file-actions{display:flex;gap:6px}.tabs{display:flex;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line)}.tab{background:#fff;color:var(--text);border-color:var(--line);box-shadow:none}.tab.active{color:var(--blue);border-color:var(--blue);background:var(--blueSoft)}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line);background:var(--soft)}.out{min-height:0;overflow:auto;padding:22px 24px;border-left:4px solid rgba(37,99,235,.22);white-space:pre-wrap;word-break:break-word;line-height:1.75}.out.raw{background:var(--dark);color:#e6edf7;font-family:Consolas,monospace}.report{display:grid;gap:14px;max-width:1180px}.hero,.section,.metric{border:1px solid var(--line);border-radius:12px;background:#fff}.hero{padding:18px 20px;background:linear-gradient(135deg,rgba(37,99,235,.10),transparent 42%),#fff}.hero h2{margin:4px 0;font-size:22px}.hero p{margin:0;color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.metric{padding:10px 12px}.metric span{display:block;color:var(--muted);font-size:12px;font-weight:750}.metric strong{display:block;margin-top:5px}.section h3{margin:0;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--soft);font-size:15px}.section div{padding:14px 16px}.drop{position:fixed;inset:14px;z-index:20;display:none;align-items:center;justify-content:center;border:2px dashed rgba(37,99,235,.55);border-radius:18px;background:rgba(239,246,255,.86);color:var(--blue2);pointer-events:none}.drop.active{display:flex}.drop>div{padding:26px 30px;border-radius:14px;background:#fff;text-align:center}@media(max-width:900px){.grid,.detail-grid{grid-template-columns:1fr;height:auto}.log-wrap{min-height:520px}.card.result{height:72vh;min-height:520px}}\n</style>\n</head>\n<body>\n<div id="drop" class="drop"><div><strong>??????</strong><br><span class="muted">??????????</span></div></div>\n<header><h1>Short Video Analyzer</h1><div id="current" class="muted">??</div></header>\n<main id="home" class="page active"><div class="grid"><section class="card stack">\n<div><p class="title">TikTok / ??????</p><label>??????</label><input id="url" type="url" placeholder="https://www.tiktok.com/@user/video/... ? https://v.douyin.com/..."></div><div class="row"><button id="download">????</button><button id="network" class="secondary">??????</button></div>\n<div><p class="title">??????</p><label>??? videos/</label><input id="videoFile" type="file" accept="video/*" multiple></div><div class="row"><button id="upload">??</button><button id="refresh" class="secondary">????</button></div>\n<div><p class="title">?????</p><div id="homeFiles" class="files"></div></div>\n<div><p class="title">????</p><label>????</label><select id="mode"><option value="analyzer">????????video-analyzer?</option><option value="direct_video">?????????Qwen?</option></select></div><button id="promptBtn" class="secondary">???????</button><div id="promptPanel" class="prompt"><label>?????</label><textarea id="prompt"></textarea></div>\n<label class="check"><input id="autoPost" type="checkbox">???? DeepSeek ??</label><div class="row"><button id="analyze" disabled>????</button><button id="post" class="secondary" disabled>????????</button></div><div id="status" class="status">?????????????</div>\n</section><section class="card log-wrap"><div class="head"><div><h2>????</h2><div class="muted">?????????????????????</div></div><button id="clearLog" class="secondary small">????</button></div><pre id="log" class="log">????...</pre></section></div></main>\n<main id="detail" class="page"><div class="detail-grid"><section class="card" style="display:grid;grid-template-rows:auto 1fr"><div class="head" style="display:grid"><button id="back" class="secondary">????</button><div><h2>?????</h2><div class="muted">???????</div></div></div><div id="detailFiles" class="detail-files files"></div></section><section class="card result" style="display:grid;grid-template-rows:auto auto minmax(0,1fr)"><div class="tabs"><button class="tab active" data-tab="content">????????</button><button class="tab" data-tab="audit">????????</button></div><div class="toolbar"><b id="outTitle">Qwen ?????DeepSeek ??</b><div class="row"><button id="source" class="secondary small">????</button><button id="json" class="secondary small">???? JSON</button></div></div><div id="out" class="out">{}</div></section></div></main>\n<script>\nwindow.DEFAULT_ANALYSIS_MODE="__DEFAULT_ANALYSIS_MODE__";\nconst S={file:"",files:[],result:null,job:null,tab:"content",raw:false,has:false,logs:[]};\nconst $=id=>document.getElementById(id), home=$(\'home\'), detail=$(\'detail\'), current=$(\'current\'), status=$(\'status\'), log=$(\'log\'), out=$(\'out\'); let de=null, je=null, drag=0;\nfunction esc(v){return String(v??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]))} function pretty(v){return v==null?\'{}\':typeof v===\'string\'?v:JSON.stringify(v,null,2)} function clean(v){let s=typeof v===\'string\'?v:(v&&typeof v.response===\'string\'?v.response:pretty(v));return s.replace(/^```(?:json)?\\s*/i,\'\').replace(/\\s*```$/i,\'\').trim()} function bytes(n){return `${Math.round(Number(n||0)/1024/1024*10)/10} MB`} function setStatus(m,k=\'\'){status.className=\'status \'+k;status.textContent=m} function addLog(m){S.logs.push(`[${new Date().toLocaleTimeString()}] ${m}`);if(S.logs.length>500)S.logs.splice(0,S.logs.length-500);log.textContent=S.logs.join(\'\\n\')||\'????...\';log.scrollTop=log.scrollHeight}\nfunction metric(k,v){return v==null||v===\'\'?\'\':`<div class="metric"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`} function sec(t,b){b=clean(b);return b?`<section class="section"><h3>${esc(t)}</h3><div>${esc(b)}</div></section>`:\'\'} function list(t,a,map=x=>x){if(!Array.isArray(a)||!a.length)return\'\';return `<section class="section"><h3>${esc(t)}</h3><div>${a.map((x,i)=>`- ${esc(clean(map(x,i)))}`).join(\'\\n\')}</div></section>`} function has(r){return !!(r&&(r.analysis||r.analysis_zh||r.audit_result||r.audit_result_zh))}\nfunction extraction(v){if(!v||typeof v!==\'object\')return pretty(v);const md=v.metadata||{},tr=v.transcript||{},u=v.usage||{},tl=Array.isArray(v.timeline)?v.timeline:[],ve=Array.isArray(v.visual_evidence)?v.visual_evidence:[],fa=Array.isArray(v.frame_analyses)?v.frame_analyses:[];return `<article class="report"><div class="hero"><small>Qwen Video Extraction</small><h2>??????</h2><p>${esc(clean(v.summary)||\'?????????????????????\')}</p></div><div class="metrics">${metric(\'????\',v.processing_mode)}${metric(\'????\',v.vision_model||md.model)}${metric(\'????\',v.audio_mode)}${metric(\'????\',md.frames_processed||md.frames_extracted)}${metric(\'????\',tr.language||md.audio_language)}${metric(\'?? Tokens\',u.input_tokens)}${metric(\'?? Tokens\',u.output_tokens)}${metric(\'? Tokens\',u.total_tokens)}${metric(\'API ??\',u.api_calls)}${metric(\'???\',u.elapsed_seconds==null?\'\':u.elapsed_seconds+\'s\')}</div>${sec(\'????\',v.summary)}${sec(\'??????\',v.video_description)}${list(\'???\',tl,x=>typeof x===\'string\'?x:`${x.time_range||x.timestamp||\'\'}\\n${x.visual||\'\'}\\n${x.audio||\'\'}`)}${list(\'????\',ve,x=>typeof x===\'string\'?x:(x.description||x.visual||pretty(x)))}${list(\'??????\',fa,(x,i)=>`[? ${i+1}]\\n${clean(x)}`)}${sec(\'????\',tr.text||\'?????\')}</article>`}\nfunction audit(v){if(!v||typeof v!==\'object\')return pretty(v);return `<article class="report"><div class="hero"><small>DeepSeek Audit</small><h2>??????</h2><p>${esc(v.summary||\'?????????????????\')}</p></div><div class="metrics">${metric(\'????\',v.risk_level)}${metric(\'????\',v.recommended_action)}${metric(\'????\',v.publish_suggestion)}</div>${sec(\'????\',v.summary)}${sec(\'????\',v.content_overview)}${sec(\'????\',v.transcript_notes)}${sec(\'????\',v.visual_notes)}${list(\'????\',v.risk_reasons)}${list(\'???\',v.issues)}</article>`}\nfunction renderOut(r){S.result=r;out.className=S.raw?\'out raw\':\'out\';let v;if(S.tab===\'content\'){v=S.raw?r?.analysis:(r?.analysis_zh||r?.analysis);$(\'json\').style.display=\'inline-flex\';$(\'outTitle\').textContent=S.raw?\'Qwen ??????? JSON\':\'Qwen ?????DeepSeek ??\';S.raw?out.textContent=pretty(v):out.innerHTML=extraction(v)}else{v=S.raw?r?.audit_result:(r?.audit_result_zh||r?.audit_result);$(\'json\').style.display=\'none\';$(\'outTitle\').textContent=S.raw?\'DeepSeek ???????\':\'DeepSeek ???????\';S.raw?out.textContent=pretty(v):out.innerHTML=audit(v)}$(\'source\').textContent=S.raw?\'????\':\'????\'}\nfunction buttons(){ $(\'analyze\').textContent=S.has?\'????\':\'????\'; $(\'analyze\').disabled=!S.file; $(\'post\').disabled=!S.file||!S.has||!!S.job }\nfunction renderFiles(){for(const [id,detailMode] of [[\'homeFiles\',false],[\'detailFiles\',true]]){const box=$(id);box.innerHTML=\'\';if(!S.files.length){box.innerHTML=\'<div class="muted">videos/ ??????</div>\';continue}S.files.forEach(f=>{const el=document.createElement(\'div\');el.className=\'file\'+(f.name===S.file?\' selected\':\'\');el.innerHTML=`<span class="file-meta"><span class="file-name">${esc(f.name)}</span><span class="muted">${bytes(f.size)}</span></span>${detailMode?\'\':`<span class="file-actions"><button class="secondary small">??</button><button class="danger small">??</button></span>`}`;el.onclick=()=>toDetail(f.name);if(!detailMode){const b=el.querySelectorAll(\'button\');b[0].onclick=e=>{e.stopPropagation();open(\'/video/\'+encodeURIComponent(f.name),\'_blank\',\'noopener\')};b[1].onclick=e=>{e.stopPropagation();delFile(f.name)}}box.appendChild(el)})}buttons()}\nfunction view(v,f=\'\'){home.classList.toggle(\'active\',v===\'home\');detail.classList.toggle(\'active\',v===\'detail\');current.textContent=v===\'detail\'&&f?f:\'??\'} function toHome(){location.hash=\'\';view(\'home\')} function toDetail(f){location.hash=\'detail=\'+encodeURIComponent(f)} function route(){const h=location.hash.slice(1);if(h.startsWith(\'detail=\')){select(decodeURIComponent(h.slice(7)),false);view(\'detail\',S.file)}else{view(\'home\');renderFiles()}}\nasync function refresh(){const r=await fetch(\'/api/files\');S.files=await r.json();if(!Array.isArray(S.files))S.files=[];renderFiles()} async function loadResult(name){const r=await fetch(\'/api/result?filename=\'+encodeURIComponent(name)),j=await r.json();if(r.ok&&has(j)){S.result=j;S.has=true;const p=j.analysis&&j.analysis.metadata&&j.analysis.metadata.analysis_prompt;if(p)$(\'prompt\').value=p;renderOut(j);setStatus(name+\': ???????\',\'ok\')}else{S.result=null;S.has=false;out.textContent=\'{}\';setStatus(name+\': ????\')}buttons()} function select(name,openDetail=true){S.file=name;current.textContent=name||\'??\';S.has=false;renderFiles();if(name)loadResult(name).catch(e=>setStatus(e.message,\'bad\'));if(openDetail&&name)toDetail(name)}\nasync function upload(files=null){const input=$(\'videoFile\'),arr=Array.from(files||input.files||[]);if(!arr.length)return setStatus(\'????????????\',\'bad\');const bad=arr.filter(f=>!f.type.startsWith(\'video/\'));if(bad.length){addLog(\'??????????????\'+bad.map(f=>f.name).join(\', \'));return setStatus(\'????????\',\'bad\')}const form=new FormData();arr.forEach(f=>form.append(\'video\',f));addLog(`???? ${arr.length} ????`);setStatus(\'????...\');const r=await fetch(\'/api/upload\',{method:\'POST\',body:form}),p=await r.json(),ok=Array.isArray(p.files)?p.files:[],err=Array.isArray(p.errors)?p.errors:[];ok.forEach(f=>addLog(`?????${f.filename} (${bytes(f.size)})`));err.forEach(e=>addLog(`?????${e.filename||\'????\'} - ${e.error||\'????\'}`));if(!r.ok&&!ok.length)return setStatus(p.error||\'????\',\'bad\');setStatus(`??????? ${ok.length} ???? ${err.length} ?`,err.length?\'bad\':\'ok\');input.value=\'\';await refresh();if(ok.length)select(ok.at(-1).filename,false)}\nasync function delFile(name){if(!confirm(`?? ${name} ?????????`))return;const r=await fetch(\'/api/delete\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({filename:name})}),p=await r.json();if(!r.ok)return setStatus(p.error||\'????\',\'bad\');addLog(\'?????\'+name);if(S.file===name){S.file=\'\';S.result=null;S.has=false;toHome()}await refresh()}\nfunction closeD(){if(de){de.close();de=null}}function closeJ(){if(je){je.close();je=null}} function lastLog(j){return j&&Array.isArray(j.log)&&j.log.length?j.log.at(-1):\'\'}\nasync function startDownload(){const url=$(\'url\').value.trim();if(!url)return setStatus(\'??? TikTok ????????\',\'bad\');$(\'download\').disabled=true;addLog(\'???????\'+url);const r=await fetch(\'/api/download\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({url})}),j=await r.json();if(!r.ok){$(\'download\').disabled=false;return setStatus(j.error||\'????????\',\'bad\')}closeD();de=new EventSource(\'/api/download-events?id=\'+encodeURIComponent(j.id));de.onmessage=async e=>{const j=JSON.parse(e.data),l=lastLog(j);if(l)addLog(\'???\'+l);if(j.status===\'running\'||j.status===\'queued\')return setStatus(`??????${j.status}`);closeD();$(\'download\').disabled=false;if(j.status!==\'complete\')return setStatus(\'???????\'+(j.error||\'????\'),\'bad\');setStatus(j.filename+\': ????\',\'ok\');$(\'url\').value=\'\';await refresh();select(j.filename,false)};de.onerror=()=>{closeD();$(\'download\').disabled=false;setStatus(\'????????\',\'bad\')}}\nasync function checkNet(){$(\'network\').disabled=true;setStatus(\'??????????????...\');try{const r=await fetch(\'/api/network-check\'),p=await r.json();const fmt=x=>!x?\'???\':(!x.ok?\'???\'+(x.error||\'????\'):`${x.ip||\'?? IP\'} / ${x.country_name||x.country||\'????\'} / ${x.is_us?\'????\':\'?????\'}`);addLog(\'???\'+fmt(p.direct));addLog(\'???\'+fmt(p.proxy));setStatus(`???${fmt(p.direct)}????${fmt(p.proxy)}`,p.proxy&&p.proxy.ok&&p.proxy.is_us?\'ok\':\'bad\')}catch(e){setStatus(e.message,\'bad\')}finally{$(\'network\').disabled=false}}\nasync function analyze(){if(!S.file)return;$(\'analyze\').disabled=true;$(\'post\').disabled=true;const reset=S.has;addLog(`${S.file}: ${reset?\'??????????\':\'??????\'}`);const r=await fetch(\'/api/analyze\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({filename:S.file,analysis_mode:$(\'mode\').value,analysis_prompt:$(\'prompt\').value,postprocess:$(\'autoPost\').checked,reset_output:reset})}),j=await r.json();if(!r.ok){setStatus(j.error||\'????\',\'bad\');return buttons()}S.job=j.id;openJob(j.id)}\nasync function postprocess(){if(!S.file||!S.has)return;const r=await fetch(\'/api/postprocess\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({filename:S.file})}),j=await r.json();if(!r.ok)return setStatus(j.error||\'????\',\'bad\');S.tab=\'audit\';document.querySelectorAll(\'.tab\').forEach(x=>x.classList.toggle(\'active\',x.dataset.tab===\'audit\'));S.job=j.id;openJob(j.id)}\nfunction openJob(id){closeJ();je=new EventSource(\'/api/job-events?id=\'+encodeURIComponent(id));je.onmessage=e=>{const j=JSON.parse(e.data),l=lastLog(j);if(l)addLog(`${j.filename}: ${l}`);S.result=j;if(location.hash.startsWith(\'#detail=\'))renderOut(j);if(j.status===\'running\'||j.status===\'queued\')return setStatus(`${j.filename}: ${j.status}`);closeJ();S.job=null;S.has=j.status===\'complete\'||has(j);buttons();setStatus(j.status===\'complete\'?`${j.filename}: ??`:`${j.filename}: ${j.error||\'??\'}`,j.status===\'complete\'?\'ok\':\'bad\')};je.onerror=()=>{closeJ();buttons();setStatus(\'????????\',\'bad\')}}\nfunction downloadJson(){const a=S.result&&S.result.analysis;if(!a)return setStatus(\'??????? analysis.json?\',\'bad\');const name=`${S.file||\'video\'}.analysis.json`,blob=new Blob([JSON.stringify(a,null,2)],{type:\'application/json;charset=utf-8\'}),url=URL.createObjectURL(blob),link=document.createElement(\'a\');link.href=url;link.download=name;document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);addLog(\'???? JSON?\'+name)}\n$(\'download\').onclick=startDownload;$(\'network\').onclick=checkNet;$(\'upload\').onclick=()=>upload();$(\'refresh\').onclick=()=>refresh().then(()=>addLog(\'????????\'));$(\'analyze\').onclick=analyze;$(\'post\').onclick=postprocess;$(\'back\').onclick=toHome;$(\'clearLog\').onclick=()=>{S.logs=[];log.textContent=\'????...\'};$(\'source\').onclick=()=>{S.raw=!S.raw;renderOut(S.result)};$(\'json\').onclick=downloadJson;$(\'mode\').value=window.DEFAULT_ANALYSIS_MODE||\'analyzer\';$(\'promptBtn\').onclick=()=>{const p=$(\'promptPanel\');p.classList.toggle(\'active\');$(\'promptBtn\').textContent=p.classList.contains(\'active\')?\'???????\':\'???????\'};document.querySelectorAll(\'.tab\').forEach(t=>t.onclick=()=>{document.querySelectorAll(\'.tab\').forEach(x=>x.classList.remove(\'active\'));t.classList.add(\'active\');S.tab=t.dataset.tab;S.raw=false;renderOut(S.result)});addEventListener(\'hashchange\',route);addEventListener(\'dragenter\',e=>{e.preventDefault();drag++;$(\'drop\').classList.add(\'active\')});addEventListener(\'dragover\',e=>e.preventDefault());addEventListener(\'dragleave\',e=>{e.preventDefault();drag=Math.max(0,drag-1);if(!drag)$(\'drop\').classList.remove(\'active\')});addEventListener(\'drop\',e=>{e.preventDefault();drag=0;$(\'drop\').classList.remove(\'active\');if(e.dataTransfer.files.length)upload(e.dataTransfer.files)});fetch(\'/api/prompt\').then(r=>r.json()).then(p=>$(\'prompt\').value=p.prompt||\'\').catch(()=>{});refresh().then(route).catch(e=>setStatus(e.message,\'bad\'));\n</script>\n</body>\n</html>'



SHOP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TikTok Shop Extractor</title>
  <style>
    :root {
      color: #172033;
      background: #eef1f6;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; }
    button, input, select, textarea { font: inherit; }
    button {
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      color: #fff;
      background: #1f6feb;
      cursor: pointer;
    }
    button.secondary { color: #243044; background: #e6ebf2; }
    button:disabled { cursor: wait; opacity: 0.65; }
    input, select, textarea {
      width: 100%;
      border: 1px solid #c9d2df;
      border-radius: 6px;
      padding: 10px 11px;
      color: #172033;
      background: #fff;
    }
    textarea { min-height: 130px; resize: vertical; }
    label {
      display: grid;
      gap: 6px;
      color: #526173;
      font-size: 13px;
    }
    h1, h2, h3, p { margin: 0; }
    .app-shell {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      min-height: 100vh;
    }
    .side-panel {
      border-right: 1px solid #d5dce7;
      background: #f8fafc;
    }
    .panel-header, .form { display: grid; gap: 14px; padding: 18px; }
    .panel-header { border-bottom: 1px solid #dce3ec; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .check { display: flex; align-items: center; gap: 8px; color: #243044; }
    .check input { width: auto; }
    .workspace {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-width: 0;
      padding: 20px;
      gap: 16px;
    }
    .workspace-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .status {
      border: 1px solid #d9e1eb;
      border-radius: 8px;
      padding: 12px;
      color: #536273;
      background: #fff;
      overflow-wrap: anywhere;
    }
    .status.ok { color: #087443; background: #ecfdf3; }
    .status.bad { color: #b42318; background: #fff1f2; }
    .result-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(300px, 420px);
      gap: 16px;
      min-height: 0;
    }
    .card {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 520px;
      border: 1px solid #d9e1eb;
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }
    .card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid #dfe6ee;
    }
    .tabs { display: flex; gap: 8px; }
    .tab { color: #243044; background: #edf2f8; }
    .tab.active { color: #fff; background: #1f6feb; }
    pre {
      min-height: 0;
      margin: 0;
      padding: 16px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      color: #162033;
      font: 13px/1.55 Consolas, "Microsoft YaHei", monospace;
    }
    .report { padding: 16px; overflow: auto; line-height: 1.62; }
    .report-section {
      border: 1px solid #e0e6ef;
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 10px;
      background: #fbfcfe;
    }
    .report-section h3 { margin-bottom: 8px; font-size: 15px; }
    .log { color: #dbe7ff; background: #101827; }
    .muted { color: #667589; font-size: 13px; }
    @media (max-width: 960px) {
      .app-shell, .result-layout { grid-template-columns: 1fr; }
      .side-panel { border-right: 0; border-bottom: 1px solid #d5dce7; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="app-shell">
    <aside class="side-panel">
      <header class="panel-header">
        <h1>TikTok Shop 提取</h1>
        <p class="muted">使用 SociaVault 提取店铺或商品数据，可选择接 DeepSeek 生成中文分析。</p>
      </header>
      <section class="form">
        <label><span id="targetLabel">TikTok Shop 链接</span><input id="shopUrl" placeholder="https://shop.tiktok.com/..."></label>
        <div class="row">
          <label>类型<select id="sourceType"><option value="product">商品详情 + 评论</option><option value="details">商品详情</option><option value="reviews">商品评论</option><option value="shop">店铺商品列表</option><option value="search">商品搜索</option></select></label>
          <label>地区<input id="region" value="US" maxlength="8"></label>
        </div>
        <div class="row">
          <label>店铺页数<input id="maxPages" type="number" min="1" max="20" value="1"></label>
          <label>评论页数<input id="reviewPages" type="number" min="0" max="20" value="1"></label>
        </div>
        <label class="check"><input id="relatedVideos" type="checkbox">提取商品关联视频</label>
        <label class="check"><input id="analyze" type="checkbox" checked>使用 DeepSeek 生成中文分析</label>
        <label>分析补充要求<textarea id="prompt" placeholder="例如：重点看卖点、评论痛点、适合做短视频的脚本方向。"></textarea></label>
        <button id="runBtn" type="button">开始提取</button>
        <a class="muted" href="/">返回视频分析页</a>
        <div id="status" class="status">等待输入 TikTok Shop 链接。</div>
      </section>
    </aside>
    <section class="workspace">
      <header class="workspace-header">
        <div>
          <h2>提取结果</h2>
          <p class="muted" id="outputDir">结果会保存到 output/tiktok_shop/&lt;job-id&gt;/</p>
        </div>
      </header>
      <section class="result-layout">
        <article class="card">
          <div class="card-head">
            <div class="tabs">
              <button class="tab active" data-tab="analysis" type="button">中文分析</button>
              <button class="tab" data-tab="extract" type="button">原始提取</button>
            </div>
            <button id="rawToggle" class="secondary" type="button">显示 JSON</button>
          </div>
          <div id="result" class="report">暂无结果。</div>
        </article>
        <article class="card">
          <div class="card-head"><h3>任务日志</h3><button id="clearLog" class="secondary" type="button">清空</button></div>
          <pre id="log" class="log">等待任务...</pre>
        </article>
      </section>
    </section>
  </main>
  <script>
    const runBtn = document.querySelector("#runBtn");
    const statusBox = document.querySelector("#status");
    const logBox = document.querySelector("#log");
    const resultBox = document.querySelector("#result");
    const outputDir = document.querySelector("#outputDir");
    const rawToggle = document.querySelector("#rawToggle");
    const state = { job: null, events: null, tab: "analysis", raw: false, lastLogLength: 0 };

    function setStatus(message, kind = "") {
      statusBox.className = `status ${kind}`.trim();
      statusBox.textContent = message;
    }
    function pretty(value) { return JSON.stringify(value ?? {}, null, 2); }
    function escapeHtml(value) {
      return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    }
    function appendLog(lines) {
      const next = Array.isArray(lines) ? lines : [lines];
      if (!next.length) return;
      const current = logBox.textContent === "等待任务..." ? "" : logBox.textContent;
      logBox.textContent = `${current}${current ? "\n" : ""}${next.join("\n")}`;
      logBox.scrollTop = logBox.scrollHeight;
    }
    function section(title, value) {
      if (value === undefined || value === null || value === "") return "";
      const body = Array.isArray(value)
        ? `<ul>${value.map(item => `<li>${escapeHtml(typeof item === "string" ? item : pretty(item))}</li>`).join("")}</ul>`
        : `<p>${escapeHtml(typeof value === "string" ? value : pretty(value))}</p>`;
      return `<section class="report-section"><h3>${escapeHtml(title)}</h3>${body}</section>`;
    }
    function renderReport(value) {
      if (!value || typeof value !== "object") return `<pre>${escapeHtml(pretty(value))}</pre>`;
      return [
        section("概要", value.summary),
        section("产品定位", value.product_positioning),
        section("销售信号", value.sales_signals),
        section("评论洞察", value.review_insights),
        section("内容机会", value.content_opportunities),
        section("风险点", value.risk_flags),
        section("建议动作", value.recommended_actions),
        section("下一步问题", value.next_questions),
      ].join("") || `<pre>${escapeHtml(pretty(value))}</pre>`;
    }
    function currentValue() {
      if (!state.job) return null;
      return state.tab === "analysis" ? state.job.analysis || null : state.job.extract || null;
    }
    function renderResult() {
      const value = currentValue();
      rawToggle.textContent = state.raw ? "显示报告" : "显示 JSON";
      if (!value) {
        resultBox.textContent = "暂无结果。";
        return;
      }
      if (state.raw || state.tab === "extract") {
        resultBox.innerHTML = `<pre>${escapeHtml(pretty(value))}</pre>`;
        return;
      }
      resultBox.innerHTML = renderReport(value);
    }
    function closeEvents() {
      if (state.events) {
        state.events.close();
        state.events = null;
      }
    }
    function updateSourceMode() {
      const sourceType = document.querySelector("#sourceType").value;
      const target = document.querySelector("#shopUrl");
      const targetLabel = document.querySelector("#targetLabel");
      const maxPages = document.querySelector("#maxPages");
      const reviewPages = document.querySelector("#reviewPages");
      const relatedVideos = document.querySelector("#relatedVideos");
      targetLabel.textContent = sourceType === "search" ? "搜索关键词" : "TikTok Shop 链接 / 商品 ID";
      target.placeholder = sourceType === "search" ? "例如：cat toy" : "https://shop.tiktok.com/... 或商品 ID";
      maxPages.disabled = !["shop", "search"].includes(sourceType);
      reviewPages.disabled = !["product", "reviews"].includes(sourceType);
      relatedVideos.disabled = !["product", "details"].includes(sourceType);
    }
    function handleJob(job) {
      state.job = job;
      if (job.output_dir) outputDir.textContent = `结果目录：${job.output_dir}`;
      if (Array.isArray(job.log) && job.log.length > state.lastLogLength) {
        appendLog(job.log.slice(state.lastLogLength));
        state.lastLogLength = job.log.length;
      }
      renderResult();
      if (job.status === "queued" || job.status === "running") {
        setStatus(`任务运行中：${job.status}`);
        return;
      }
      closeEvents();
      runBtn.disabled = false;
      setStatus(job.status === "complete" ? "TikTok Shop 提取完成。" : (job.error || "TikTok Shop 提取失败。"), job.status === "complete" ? "ok" : "bad");
    }
    async function startJob() {
      const url = document.querySelector("#shopUrl").value.trim();
      if (!url) {
        setStatus("请输入 TikTok Shop 链接、商品 ID 或搜索关键词。", "bad");
        return;
      }
      closeEvents();
      state.job = null;
      state.lastLogLength = 0;
      logBox.textContent = "提交任务...";
      resultBox.textContent = "暂无结果。";
      runBtn.disabled = true;
      setStatus("正在提交任务...");
      const payload = {
        url,
        source_type: document.querySelector("#sourceType").value,
        region: document.querySelector("#region").value.trim() || "US",
        max_pages: Number(document.querySelector("#maxPages").value || 1),
        review_pages: Number(document.querySelector("#reviewPages").value || 0),
        related_videos: document.querySelector("#relatedVideos").checked,
        analyze: document.querySelector("#analyze").checked,
        prompt: document.querySelector("#prompt").value
      };
      const response = await fetch("/api/shop-extract", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const job = await response.json();
      if (!response.ok) {
        runBtn.disabled = false;
        setStatus(job.error || "任务提交失败。", "bad");
        appendLog(job.error || "任务提交失败。");
        return;
      }
      handleJob(job);
      state.events = new EventSource(`/api/shop-events?id=${encodeURIComponent(job.id)}`);
      state.events.onmessage = event => handleJob(JSON.parse(event.data));
      state.events.onerror = () => {
        closeEvents();
        runBtn.disabled = false;
        setStatus("任务连接中断，请刷新任务结果。", "bad");
      };
    }
    runBtn.onclick = () => startJob().catch(error => {
      runBtn.disabled = false;
      setStatus(error.message, "bad");
      appendLog(error.message);
    });
    rawToggle.onclick = () => {
      state.raw = !state.raw;
      renderResult();
    };
    document.querySelector("#clearLog").onclick = () => {
      state.lastLogLength = 0;
      logBox.textContent = "等待任务...";
    };
    document.querySelector("#sourceType").onchange = updateSourceMode;
    updateSourceMode();
    document.querySelectorAll(".tab").forEach(button => {
      button.onclick = () => {
        document.querySelectorAll(".tab").forEach(item => item.classList.remove("active"));
        button.classList.add("active");
        state.tab = button.dataset.tab;
        state.raw = false;
        renderResult();
      };
    });
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
