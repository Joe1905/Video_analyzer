"""Standalone keyframe jobs. No dependency on web_app or AI analysis state."""
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs


DEFAULT_DIFFERENCE_THRESHOLD = 10.0
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def extract_frames(video_path, output_dir, max_frames=20,
                   difference_threshold=DEFAULT_DIFFERENCE_THRESHOLD):
    """Reuse the installed analyzer; threshold is instance-local and not exposed in UI/API."""
    import cv2
    from video_analyzer.frame import VideoProcessor

    if not 1 <= max_frames <= 40:
        raise ValueError("提取数量须为 1–40")
    if not math.isfinite(difference_threshold) or not 0 <= difference_threshold <= 255:
        raise ValueError("Invalid difference threshold")
    cap = cv2.VideoCapture(str(video_path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        ok, first = cap.read()
    finally:
        cap.release()
    if not ok or not math.isfinite(fps) or fps <= 0 or not math.isfinite(count) or count <= 0:
        raise ValueError("无法读取有效的视频画面")
    duration = count / fps
    if duration > 300 or first.shape[0] * first.shape[1] > 3840 * 2160:
        raise ValueError("请使用 5 分钟以内、最高 4K 的视频")
    frames_dir = output_dir / "frames"
    processor = VideoProcessor(Path(video_path), frames_dir, model="")
    processor.FRAME_DIFFERENCE_THRESHOLD = difference_threshold
    frames = processor.extract_keyframes(
        frames_per_minute=math.ceil(max_frames * 60 / duration), max_frames=max_frames,
    )
    items = [{"name": frame.path.name, "timestamp": frame.timestamp, "score": frame.score}
             for frame in sorted(frames, key=lambda frame: frame.timestamp)]
    fallback = not items
    if fallback:
        frames_dir.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(frames_dir / "frame_0.jpg"), first):
            raise ValueError("保存视频画面失败")
        items = [{"name": "frame_0.jpg", "timestamp": 0.0, "score": 0.0}]
    if any(not (frames_dir / item["name"]).is_file() for item in items):
        raise ValueError("部分画面未能保存，请重试")
    result = {"duration": duration, "frames": items, "fallback": fallback,
              "difference_threshold": difference_threshold, "max_frames": max_frames}
    save_json(output_dir / "frames.json", result)
    return result


def export_sheet(directory, result):
    from PIL import Image, ImageDraw
    frames = result["frames"]
    columns = min(4, len(frames))
    width, height, gap = 320, 240, 12
    sheet = Image.new("RGB", (columns * (width + gap) + gap,
                              math.ceil(len(frames) / columns) * (height + 32 + gap) + gap), "#f1f4f8")
    draw = ImageDraw.Draw(sheet)
    for index, item in enumerate(frames):
        x = gap + index % columns * (width + gap)
        y = gap + index // columns * (height + 32 + gap)
        with Image.open(directory / "frames" / item["name"]) as source:
            thumbnail = source.convert("RGB")
            thumbnail.thumbnail((width, height))
            sheet.paste(thumbnail, (x + (width - thumbnail.width) // 2, y + (height - thumbnail.height) // 2))
        draw.text((x + 8, y + height + 8), f'{index + 1:02d}  /  {item["timestamp"]:.2f}s', fill="#233047")
    buffer = BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()


class StoryboardService:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / "output" / "storyboard"
        self.slot = threading.BoundedSemaphore(1)
        self.active = set()
        self.lock = threading.Lock()

    def job_dir(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("无效的任务编号")
        return self.directory / job_id

    def load(self, job_id):
        directory = self.job_dir(job_id)
        job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        with self.lock:
            active = job_id in self.active
        if job["status"] == "running" and not active:
            job.update(status="failed", error="服务重启中断了任务，请重新提取")
        if job["status"] == "complete":
            job.update(json.loads((directory / "frames.json").read_text(encoding="utf-8")))
        return job

    def run(self, job, source):
        directory = self.job_dir(job["id"])
        try:
            process = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--extract", str(source),
                 "--output", str(directory), "--max-frames", str(job["max_frames"])],
                capture_output=True, text=True, timeout=600,
            )
            if process.returncode:
                raise ValueError((process.stderr or "视频提取失败").strip()[-1200:])
            job["status"] = "complete"
        except subprocess.TimeoutExpired:
            job.update(status="failed", error="提取超过 10 分钟，请缩短视频后重试")
        except Exception as exc:
            job.update(status="failed", error=str(exc))
        finally:
            try:
                source.unlink(missing_ok=True)
                save_json(directory / "job.json", job)
            finally:
                with self.lock:
                    self.active.discard(job["id"])
                self.slot.release()

    def create(self, handler, query):
        name = query.get("filename", [""])[0]
        if not name or Path(name).name != name or "/" in name or "\\" in name or Path(name).suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError("请选择 MP4、MOV、M4V 或 WebM 视频")
        maximum = int(query.get("max_frames", ["20"])[0])
        if not 1 <= maximum <= 40:
            raise ValueError("提取数量须为 1–40")
        if "difference_threshold" in query:
            raise ValueError("当前版本未开放差异阈值调整")
        existing = query.get("source", ["upload"])[0] == "existing"
        size = int(handler.headers.get("Content-Length", "0"))
        source = (self.root / "videos" / name).resolve()
        if existing:
            if source.parent != (self.root / "videos").resolve() or not source.is_file():
                raise ValueError("视频不存在")
            if size != 0 or source.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValueError("视频不得超过 512 MB")
        elif size <= 0 or size > MAX_UPLOAD_BYTES:
            raise ValueError("视频大小须在 512 MB 以内")
        if not self.slot.acquire(blocking=False):
            self.reply(handler, 409, {"error": "已有分镜任务正在处理，请稍后重试"})
            return
        job = {"id": uuid.uuid4().hex, "filename": name, "max_frames": maximum,
               "status": "running", "created_at": time.time()}
        directory = self.job_dir(job["id"])
        try:
            directory.mkdir(parents=True)
            # Snapshot existing videos too: later uploads cannot change an in-flight job.
            target = directory / ("source" + Path(name).suffix.lower())
            if existing:
                shutil.copyfile(source, target)
            else:
                remaining = size
                with target.open("wb") as stream:
                    while remaining:
                        chunk = handler.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("视频上传中断，请重试")
                        stream.write(chunk)
                        remaining -= len(chunk)
            save_json(directory / "job.json", job)
            with self.lock:
                self.active.add(job["id"])
            threading.Thread(target=self.run, args=(job.copy(), target), daemon=True).start()
        except Exception:
            with self.lock:
                self.active.discard(job["id"])
            self.slot.release()
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self.reply(handler, 202, job)

    @staticmethod
    def reply(handler, status, data, content_type="application/json; charset=utf-8", download=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        if handler.command == "POST":
            handler.send_header("Connection", "close")
            handler.close_connection = True
        if download:
            handler.send_header("Content-Disposition", f'attachment; filename="{download}"')
        handler.end_headers()
        handler.wfile.write(data)

    def handle(self, handler, parsed, nav):
        path = parsed.path
        if path != "/storyboard" and not path.startswith("/api/storyboard/"):
            return False
        query = parse_qs(parsed.query)
        try:
            if handler.command == "POST" and path == "/api/storyboard/jobs":
                self.create(handler, query)
            elif handler.command != "GET":
                self.reply(handler, 405, {"error": "Method not allowed"})
            elif path == "/storyboard":
                page = (Path(__file__).parent / "static" / "storyboard.html").read_text(encoding="utf-8")
                self.reply(handler, 200, nav(page, path).encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/storyboard/videos":
                files = sorted(p.name for p in (self.root / "videos").glob("*")
                               if p.is_file() and not p.is_symlink() and p.suffix.lower() in VIDEO_SUFFIXES)
                self.reply(handler, 200, {"files": files})
            elif path == "/api/storyboard/jobs" and not query.get("id"):
                jobs = []
                paths = sorted(self.directory.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                for item in paths[:30]:
                    jobs.append(self.load(item.parent.name))
                self.reply(handler, 200, {"jobs": jobs})
            elif path in {"/api/storyboard/jobs", "/api/storyboard/image", "/api/storyboard/export"}:
                job_id = query.get("id", [""])[0]
                result = self.load(job_id)
                directory = self.job_dir(job_id)
                if path == "/api/storyboard/jobs":
                    self.reply(handler, 200, result)
                elif result["status"] != "complete":
                    self.reply(handler, 409, {"error": "任务尚未完成"})
                elif path == "/api/storyboard/image":
                    name = query.get("frame", [""])[0]
                    if name not in {item["name"] for item in result["frames"]}:
                        raise ValueError("无效的图片名称")
                    self.reply(handler, 200, (directory / "frames" / name).read_bytes(), "image/jpeg")
                elif query.get("format", ["png"])[0] == "zip":
                    buffer = BytesIO()
                    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.write(directory / "frames.json", "frames.json")
                        for item in result["frames"]:
                            archive.write(directory / "frames" / item["name"], item["name"])
                    self.reply(handler, 200, buffer.getvalue(), "application/zip", "storyboard.zip")
                else:
                    self.reply(handler, 200, export_sheet(directory, result), "image/png", "storyboard.png")
            else:
                self.reply(handler, 404, {"error": "Not found"})
        except (ValueError, OSError) as exc:
            self.reply(handler, 404 if isinstance(exc, FileNotFoundError) else 400, {"error": str(exc)})
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-frames", type=int, default=20)
    # Reserved for explicit future tuning; the web service always uses the default.
    parser.add_argument("--difference-threshold", type=float, default=DEFAULT_DIFFERENCE_THRESHOLD)
    args = parser.parse_args()
    try:
        extract_frames(Path(args.extract), Path(args.output), args.max_frames, args.difference_threshold)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
