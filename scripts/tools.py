"""AI Chat Tool Registry — 32 tools wrapping existing API scripts."""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse, urlunparse

from api_cache import get_cached, get_cached_or_call, store_response
from proxy_state import ensure_us_proxy
from tiktok_download import video_cache_metadata, video_cache_request, with_download_cache_meta
from video_registry import get_video_by_filename, mark_extracted, register_from_payload, register_video, platform_for_url

ROOT = Path.cwd()
SCRIPTS_DIR = ROOT / "scripts"
OUTPUT_DIR = ROOT / "output" / "chat_tools"
VIDEOS_DIR = ROOT / "videos"
SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
DEFAULT_SOCIA_VAULT_API_BASE = "https://api.sociavault.com"
VIDEO_INFO_TTL_SECONDS = 24 * 60 * 60
VIDEO_MEDIA_TTL_SECONDS = int(os.getenv("VIDEO_MEDIA_TTL_SECONDS", "900"))
AUDIO_ONLY_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
VIDEO_SUFFIXES = {".m4v", ".mov", ".mp4", ".webm"}


def _video_output_dir(filename: str) -> Path:
    record = get_video_by_filename(filename)
    if record:
        return ROOT / "output" / str(record.get("extraction_dir") or filename)
    return ROOT / "output" / filename


def _is_tiktok_video_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    return ("tiktok.com" in host or "tiktokv.com" in host) and "/video/" in parsed.path

# ── Amazon Tools ──────────────────────────────────────────────────

def _amazon_scrape(target: str, target_type: str, pages: int = 1) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    url = target
    if target_type == "asin":
        url = f"https://www.amazon.com/dp/{target.upper()}"
    elif target_type == "keyword":
        url = f"https://www.amazon.com/s?k={quote_plus(target)}"

    def normalized_url(value: str) -> str:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        return urlunparse((parsed.scheme.lower() or "https", host, parsed.path, "", parsed.query, ""))

    def fetch() -> dict:
        ensure_us_proxy("amazon")
        cmd = [
            "docker", "run", "--rm", "--network", "host",
            "-e", "AMAZON_PROXY", "-e", "AMAZON_PROXIES",
            "amazon-scraper", "node", "assets/amazon_handler.js",
            url, "--pages", str(pages),
        ]
        env = os.environ.copy()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=ROOT, env=env)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or f"Exit code {result.returncode}")
        text = result.stdout.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            parsed = []
            for match in re.finditer(r"\{", text):
                try:
                    val, _ = decoder.raw_decode(text[match.start():])
                    parsed.append(val)
                except json.JSONDecodeError:
                    continue
            if not parsed:
                raise ValueError("amazon-scraper output did not contain JSON")
            parsed.sort(key=lambda v: len(json.dumps(v)), reverse=True)
            return parsed[0]

    data = get_cached_or_call(
        "amazon_scraper",
        target_type,
        {"url": normalized_url(url), "pages": int(pages)},
        fetch,
        metadata_builder=lambda payload: {
            "entity_type": "amazon",
            "entity_id": str((payload.get("products") or [{}])[0].get("asin") or normalized_url(url)) if isinstance(payload, dict) else normalized_url(url),
            "title": str((payload.get("products") or [{}])[0].get("title") or "") if isinstance(payload, dict) else "",
            "source_url": normalized_url(url),
        },
    )
    out_path = OUTPUT_DIR / f"amazon_{target_type}_{uuid.uuid4().hex[:8]}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"raw_ref": str(out_path.relative_to(ROOT)), "raw_bytes": out_path.stat().st_size, "data": data}


# ── TikTok API Tools ──────────────────────────────────────────────

TIKTOK_ENDPOINT_PARAMS: dict[str, list[str]] = {
    "profile": ["handle"], "videos": ["handle", "count"], "videos-popular": ["count"],
    "followers": ["handle"], "following": ["handle"], "demographics": ["handle"],
    "video-info": ["url"], "comments": ["url", "count"], "comment-replies": ["url", "count"],
    "transcript": ["url"], "live": ["handle"],
    "search-users": ["query", "count"], "search-hashtag": ["hashtag", "count"],
    "search-keyword": ["query", "count"], "search-music": ["query", "count"],
    "search-top": ["query", "count"], "trending": [],
    "creators-popular": ["count"], "hashtags-popular": ["count"],
    "music-popular": ["count"], "music-info": ["sound_id"], "music-videos": ["sound_id", "count"],
}


def _forward_child_output(result: subprocess.CompletedProcess[str]) -> None:
    for text in (result.stdout, result.stderr):
        if text:
            print(text.rstrip(), flush=True)


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    cleaned = "".join(ch for ch in name if ch in SAFE_CHARS)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Invalid filename")
    return cleaned


def _score_media_candidate(path: str, url: str) -> int:
    lowered = f"{path} {url}".lower()
    score = 0
    if ".video.play_addr.url_list" in lowered and ".bit_rate." not in lowered:
        score += 260
    if "download_no_watermark_addr.url_list" in lowered:
        score += 240
    if "download_addr.url_list" in lowered:
        score += 220
    if ".bit_rate." in lowered:
        score -= 120
    for word, points in (
        ("h264", 90),
        ("download", 100),
        ("no_watermark", 80),
        ("nowatermark", 80),
        ("play_addr", 70),
        ("playaddr", 70),
        ("video_url", 60),
        ("video", 40),
        (".mp4", 30),
        ("bytevc2", -160),
        ("bytevc1", -80),
        ("watermark", -40),
    ):
        if word in lowered:
            score += points
    return score


def _iter_media_url_candidates(value: Any, path: str = "") -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            candidates.extend(_iter_media_url_candidates(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            candidates.extend(_iter_media_url_candidates(child, f"{path}[{index}]"))
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        lowered = f"{path} {value}".lower()
        if any(word in lowered for word in ("cover", "avatar", "thumbnail", "image", "music", "audio", "subtitle")):
            return []
        if not any(word in lowered for word in ("video", "download", "play", ".mp4", "mime_type=video", "mime=video")):
            return []
        candidates.append((_score_media_candidate(path, value), path, value))
    return candidates


def _video_id_from_payload(payload: Any, fallback_url: str) -> str:
    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("id", "video_id", "aweme_id", "item_id"):
                raw = value.get(key)
                if raw not in (None, ""):
                    return str(raw)
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None

    found = walk(payload)
    if found:
        return re.sub(r"[^A-Za-z0-9_-]+", "_", found).strip("_")[:80] or "unknown"
    match = re.search(r"/video/(\d+)", fallback_url)
    if match:
        return match.group(1)
    return uuid.uuid4().hex[:12]


def _download_path_from_data(data: dict[str, Any]) -> Path | None:
    filename = str(data.get("filename") or "").strip()
    raw_path = str(data.get("path") or "").strip()
    if raw_path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = ROOT / path
        return path
    if filename:
        return VIDEOS_DIR / Path(filename).name
    return None


def _probe_media(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in AUDIO_ONLY_SUFFIXES:
        return {"ok": False, "reason": f"audio-only suffix {path.suffix.lower()}", "video_stream": None}
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"ok": path.suffix.lower() in VIDEO_SUFFIXES, "reason": "ffprobe unavailable", "video_stream": None}
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )
    if result.returncode != 0:
        return {"ok": False, "reason": result.stderr or result.stdout or f"ffprobe exit {result.returncode}", "video_stream": None}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"ok": False, "reason": f"ffprobe json error: {exc}", "video_stream": None}
    streams = payload.get("streams") if isinstance(payload, dict) else []
    video_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
    if not video_stream:
        return {"ok": False, "reason": "no video stream", "video_stream": None}
    return {"ok": True, "reason": "", "video_stream": video_stream}


def _needs_h264_transcode(path: Path, probe: dict[str, Any]) -> bool:
    stream = probe.get("video_stream") if isinstance(probe, dict) else None
    codec = str((stream or {}).get("codec_name") or "").lower()
    if codec not in {"h264", "avc1"}:
        return True
    return path.suffix.lower() not in {".mp4", ".m4v", ".mov"}


def _transcode_for_analyzer(path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg unavailable; cannot normalize video encoding")
    target = path.with_name(f"{path.stem}_h264.mp4")
    if target.is_file():
        target_probe = _probe_media(target)
        if target_probe.get("ok") and not _needs_h264_transcode(target, target_probe):
            return target
    temp_target = target.with_name(target.name + ".part.mp4")
    temp_target.unlink(missing_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temp_target),
        ],
        capture_output=True,
        text=True,
        timeout=int(os.getenv("VIDEO_TRANSCODE_TIMEOUT", "300")),
        cwd=ROOT,
    )
    if result.returncode != 0:
        temp_target.unlink(missing_ok=True)
        raise RuntimeError(result.stderr or result.stdout or f"ffmpeg exit {result.returncode}")
    temp_target.replace(target)
    return target


def _ensure_analyzer_video(data: dict[str, Any], source_url: str) -> dict[str, Any]:
    path = _download_path_from_data(data)
    if not path or not path.is_file():
        raise RuntimeError(f"downloaded file missing: {data.get('filename') or data.get('path')}")
    probe = _probe_media(path)
    if not probe.get("ok"):
        raise RuntimeError(f"downloaded file is not an analyzable video: {probe.get('reason')}")
    if _needs_h264_transcode(path, probe):
        original = path
        path = _transcode_for_analyzer(path)
        data["transcoded_from"] = original.name
        data["transcode"] = {"video_codec": "h264", "audio_codec": "aac", "source": original.name}
    data["filename"] = path.name
    data["path"] = str(path)
    data["size"] = path.stat().st_size
    data.setdefault("webpage_url", source_url)
    return data


def _download_direct_media(media_url: str, source_url: str, payload: Any) -> dict:
    import requests

    ensure_us_proxy("tiktok")
    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("SociaVault media URL is not http/https")
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    max_bytes = int(os.getenv("TIKTOK_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
    video_id = _video_id_from_payload(payload, source_url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".webm"}:
        suffix = ".mp4"
    target = VIDEOS_DIR / _safe_filename(f"shortvideo_SociaVault_{video_id}{suffix}")
    temp_target = target.with_suffix(target.suffix + ".part")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
        "Referer": source_url,
    }
    proxy = os.getenv("TIKTOK_PROXY_URL", "").strip()
    attempts: list[tuple[str, dict[str, str] | None]] = []
    if proxy:
        attempts.append((f"proxy={proxy}", {"http": proxy, "https": proxy}))
    attempts.append(("direct", None))
    try:
        errors = []
        for attempt_label, proxies in attempts:
            temp_target.unlink(missing_ok=True)
            try:
                print(f"[VIDEO_DOWNLOAD] direct media attempt={attempt_label}", flush=True)
                with requests.get(media_url, headers=headers, proxies=proxies, stream=True, timeout=(8, 60)) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("Content-Length") or 0)
                    if content_length > max_bytes:
                        raise RuntimeError(f"SociaVault media is too large: {content_length} bytes")
                    downloaded = 0
                    with temp_target.open("wb") as file:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise RuntimeError(f"SociaVault media exceeded max size: {downloaded} bytes")
                            file.write(chunk)
                break
            except Exception as exc:
                errors.append(f"{attempt_label}: {exc}")
        else:
            raise RuntimeError(" / ".join(errors))
        if temp_target.stat().st_size < 500 * 1024:
            raise RuntimeError(f"SociaVault media file is too small: {temp_target.stat().st_size} bytes")
        temp_target.replace(target)
    except Exception:
        temp_target.unlink(missing_ok=True)
        raise
    return _ensure_analyzer_video({
        "filename": target.name,
        "path": str(target),
        "size": target.stat().st_size,
        "id": video_id,
        "title": None,
        "uploader": None,
        "duration": None,
        "webpage_url": source_url,
        "downloader": "sociavault-video-info",
        "media_url": media_url,
    }, source_url)


def _sociavault_video_info_request(url: str) -> dict[str, Any]:
    api_base = os.getenv("SOCIAVAULT_API_BASE", DEFAULT_SOCIA_VAULT_API_BASE).rstrip("/")
    return {"api_base": api_base, "endpoint": "video-info", "params": {"url": url}}


def _write_download_result(result_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"raw_ref": str(result_path.relative_to(ROOT)), "raw_bytes": result_path.stat().st_size, "data": data}


def _cached_download_result(url: str, result_path: Path) -> dict | None:
    cached = get_cached("short_video_download", "download", video_cache_request(url))
    if not isinstance(cached, dict) or not cached.get("filename"):
        return None
    cached_path = VIDEOS_DIR / Path(str(cached["filename"])).name
    if not cached_path.is_file():
        print(f"[API_CACHE] miss provider=short_video_download endpoint=download reason=file_missing filename={cached.get('filename')}", flush=True)
        return None
    try:
        data = _ensure_analyzer_video(dict(cached), url)
    except Exception as exc:
        print(f"[API_CACHE] miss provider=short_video_download endpoint=download reason=invalid_video filename={cached.get('filename')} error={exc}", flush=True)
        return None
    store_response(
        "short_video_download",
        "download",
        video_cache_request(url),
        data,
        metadata=video_cache_metadata(data, url),
    )
    data = with_download_cache_meta(data, True)
    if data.get("id"):
        register_video(
            video_id=str(data.get("id")),
            platform=platform_for_url(url),
            source_url=str(data.get("webpage_url") or url),
            filename=str(data.get("filename") or ""),
            title=str(data.get("title") or ""),
            author=str(data.get("uploader") or ""),
        )
    return _write_download_result(result_path, data)


def _store_download_result(url: str, data: dict[str, Any]) -> dict[str, Any]:
    data = _ensure_analyzer_video(dict(data), url)
    if data.get("id"):
        register_video(
            video_id=str(data.get("id")),
            platform=platform_for_url(url),
            source_url=str(data.get("webpage_url") or url),
            filename=str(data.get("filename") or ""),
            title=str(data.get("title") or ""),
            author=str(data.get("uploader") or ""),
        )
    store_response(
        "short_video_download",
        "download",
        video_cache_request(url),
        data,
        metadata=video_cache_metadata(data, url),
    )
    return with_download_cache_meta(data, False)


def _media_cache_payload(url: str, payload: Any) -> dict[str, Any]:
    candidates = sorted(_iter_media_url_candidates(payload), key=lambda item: item[0], reverse=True)
    return {
        "source_url": url,
        "video_id": _video_id_from_payload(payload, url),
        "candidates": [
            {"score": score, "path": path, "url": media_url}
            for score, path, media_url in candidates[:12]
        ],
    }


def _try_media_cache_payload_download(url: str, payload: Any, result_path: Path, source_label: str) -> dict | None:
    if source_label.startswith("cached") and _media_cache_is_stale(payload):
        print(f"[VIDEO_DOWNLOAD] cached media ignored: stale signed URLs", flush=True)
        return None
    raw_candidates = payload.get("candidates") if isinstance(payload, dict) else []
    candidates = [item for item in raw_candidates if isinstance(item, dict) and item.get("url")]
    for item in candidates:
        item["score"] = _score_media_candidate(str(item.get("path") or ""), str(item.get("url") or ""))
    candidates.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    print(f"[VIDEO_DOWNLOAD] {source_label} media candidates={len(candidates)}", flush=True)
    errors = []
    for item in candidates[:12]:
        path = str(item.get("path") or "")
        media_url = str(item.get("url") or "")
        score = item.get("score")
        try:
            print(f"[VIDEO_DOWNLOAD] {source_label} candidate score={score} path={path}", flush=True)
            data = _download_direct_media(media_url, url, payload)
            data["video_info_source"] = source_label
            if isinstance(payload, dict) and isinstance(payload.get("_cache"), dict):
                data["media_cache"] = payload["_cache"]
            data = _store_download_result(url, data)
            return _write_download_result(result_path, data)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            print(f"[VIDEO_DOWNLOAD] {source_label} candidate failed: {errors[-1]}", flush=True)
    if errors:
        print("[VIDEO_DOWNLOAD] direct media failures: " + " | ".join(errors[:4]), flush=True)
    return None


def _media_cache_is_stale(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    cache_meta = payload.get("_cache")
    if not isinstance(cache_meta, dict):
        return False
    age = cache_meta.get("age_seconds")
    if age is None:
        return False
    return float(age) > VIDEO_MEDIA_TTL_SECONDS


def _try_video_info_payload_download(url: str, payload: Any, result_path: Path, source_label: str) -> dict | None:
    register_from_payload(payload, source_url=url)
    media_payload = _media_cache_payload(url, payload)
    if media_payload["candidates"]:
        store_response(
            "sociavault_tiktok_media",
            "video-info-media",
            _sociavault_video_info_request(url),
            media_payload,
            ttl_seconds=VIDEO_MEDIA_TTL_SECONDS,
            metadata={"entity_type": "tiktok_video_media", "entity_id": media_payload.get("video_id"), "source_url": url},
        )
    return _try_media_cache_payload_download(url, media_payload, result_path, source_label)


def _cached_video_info_download(url: str, result_path: Path) -> dict | None:
    payload = get_cached(
        "sociavault_tiktok_media",
        "video-info-media",
        _sociavault_video_info_request(url),
        ttl_seconds=VIDEO_MEDIA_TTL_SECONDS,
    )
    if not isinstance(payload, dict):
        return None
    return _try_media_cache_payload_download(url, payload, result_path, "cached-media")


def _api_video_info_download(url: str, result_path: Path, crawler_error: str = "") -> dict | None:
    info_path = OUTPUT_DIR / f"tiktok_video-info_{uuid.uuid4().hex[:8]}.json"
    api_cmd = [
        "python",
        str(SCRIPTS_DIR / "sociavault_tiktok.py"),
        "--endpoint",
        "video-info",
        "--url",
        url,
        "--output",
        str(info_path),
    ]
    api_result = subprocess.run(api_cmd, capture_output=True, text=True, timeout=180, cwd=ROOT)
    _forward_child_output(api_result)
    if api_result.returncode != 0:
        if crawler_error:
            raise RuntimeError(
                "Video download failed: original downloader failed, and SociaVault video-info failed too.\n"
                f"Original downloader: {crawler_error}\n"
                f"SociaVault: {api_result.stderr or api_result.stdout or f'Exit code {api_result.returncode}'}"
            )
        print(f"[VIDEO_DOWNLOAD] api video-info failed: {api_result.stderr or api_result.stdout or api_result.returncode}", flush=True)
        return None
    payload = json.loads(info_path.read_text(encoding="utf-8"))
    register_from_payload(payload, source_url=url)
    api_download = _try_video_info_payload_download(url, payload, result_path, "api")
    if api_download:
        data = api_download.get("data")
        if isinstance(data, dict):
            data["sociavault_video_info"] = str(info_path.relative_to(ROOT))
            result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            api_download["raw_bytes"] = result_path.stat().st_size
        return api_download
    if crawler_error:
        raise RuntimeError(
            "Video download failed: original downloader failed, and SociaVault video-info had no usable video media URL.\n"
            f"Original downloader: {crawler_error}\n"
        )
    return None


def _run_tiktok_api(endpoint: str, **kwargs) -> dict:
    cmd = ["python", str(SCRIPTS_DIR / "sociavault_tiktok.py"), "--endpoint", endpoint]
    for key, value in kwargs.items():
        if value is not None and value != "":
            cmd.extend(["--" + key.replace("_", "-"), str(value)])
    out_path = OUTPUT_DIR / f"tiktok_{endpoint}_{uuid.uuid4().hex[:8]}.json"
    cmd.extend(["--output", str(out_path)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=ROOT)
    _forward_child_output(result)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or f"Exit code {result.returncode}")
    if out_path.is_file():
        return {"raw_ref": str(out_path.relative_to(ROOT)), "raw_bytes": out_path.stat().st_size, "data": json.loads(out_path.read_text(encoding="utf-8"))}
    return {"output": result.stdout}


# ── TikTok Shop Tools ─────────────────────────────────────────────

def _run_shop(source_type: str, url: str, **kwargs) -> dict:
    cmd = ["python", str(SCRIPTS_DIR / "sociavault_tiktok_shop.py"), url, "--source-type", source_type]
    for key, value in kwargs.items():
        cmd.extend(["--" + key.replace("_", "-"), str(value)])
    out_path = OUTPUT_DIR / f"shop_{source_type}_{uuid.uuid4().hex[:8]}.json"
    cmd.extend(["--output", str(out_path)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=ROOT)
    _forward_child_output(result)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or f"Exit code {result.returncode}")
    if out_path.is_file():
        return {"raw_ref": str(out_path.relative_to(ROOT)), "raw_bytes": out_path.stat().st_size, "data": json.loads(out_path.read_text(encoding="utf-8"))}
    return {"output": result.stdout}


# ── Video Tools ───────────────────────────────────────────────────

def _run_video_download(url: str) -> dict:
    result_path = OUTPUT_DIR / f"video_download_{uuid.uuid4().hex[:8]}.json"
    downloaded_result = _cached_download_result(url, result_path)
    if downloaded_result:
        return downloaded_result

    cached_result = _cached_video_info_download(url, result_path)
    if cached_result:
        return cached_result

    api_result = _api_video_info_download(url, result_path)
    if api_result:
        return api_result

    cmd = [
        "python",
        str(SCRIPTS_DIR / "tiktok_download.py"),
        url,
        "--output-dir",
        str(VIDEOS_DIR),
        "--result-json",
        str(result_path),
    ]
    crawler_timeout = int(os.getenv("VIDEO_DOWNLOAD_CRAWLER_TIMEOUT", "90"))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=crawler_timeout, cwd=ROOT)
        _forward_child_output(result)
        if result.returncode == 0 and result_path.is_file():
            data = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                try:
                    data = _store_download_result(url, data)
                except Exception as exc:
                    crawler_error = f"original downloader returned unusable media: {exc}"
                    retry_result = _api_video_info_download(url, result_path, crawler_error)
                    if retry_result:
                        return retry_result
                    raise RuntimeError(crawler_error)
                result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return {"raw_ref": str(result_path.relative_to(ROOT)), "raw_bytes": result_path.stat().st_size, "data": data}
        crawler_error = result.stderr or result.stdout or f"Exit code {result.returncode}"
    except subprocess.TimeoutExpired as exc:
        crawler_error = f"原下载器超时 {crawler_timeout}s: {exc}"
    info_path = OUTPUT_DIR / f"tiktok_video-info_{uuid.uuid4().hex[:8]}.json"
    api_cmd = [
        "python",
        str(SCRIPTS_DIR / "sociavault_tiktok.py"),
        "--endpoint",
        "video-info",
        "--url",
        url,
        "--output",
        str(info_path),
    ]
    api_result = subprocess.run(api_cmd, capture_output=True, text=True, timeout=180, cwd=ROOT)
    _forward_child_output(api_result)
    if api_result.returncode != 0:
        raise RuntimeError(
            "原下载器失败，SociaVault video-info 也失败。\n"
            f"原下载器：{crawler_error}\n"
            f"SociaVault：{api_result.stderr or api_result.stdout or f'Exit code {api_result.returncode}'}"
        )
    payload = json.loads(info_path.read_text(encoding="utf-8"))
    register_from_payload(payload, source_url=url)
    api_download = _try_video_info_payload_download(url, payload, result_path, "api")
    if api_download:
        data = api_download.get("data")
        if isinstance(data, dict):
            data["sociavault_video_info"] = str(info_path.relative_to(ROOT))
            result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            api_download["raw_bytes"] = result_path.stat().st_size
        return api_download
    raise RuntimeError(
        "视频下载失败：缓存地址不可用，原下载器失败，SociaVault video-info 也没有可用下载地址。\n"
        f"原下载器：{crawler_error}\n"
    )


def _run_video_analyze(filename: str) -> dict:
    out_dir = _video_output_dir(filename)
    analysis = out_dir / "analysis.json"
    if analysis.is_file():
        data = json.loads(analysis.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["_cache"] = {"hit": True, "provider": "video_registry", "endpoint": "analysis"}
        return data
    cmd = ["bash", str(SCRIPTS_DIR / "analyze_one.sh"), filename]
    env = os.environ.copy()
    env["ANALYSIS_OUTPUT_DIR"] = str(out_dir)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or f"Exit code {result.returncode}")
    mark_extracted(filename, out_dir.name)
    if analysis.is_file():
        return json.loads(analysis.read_text(encoding="utf-8"))
    return {"output": result.stdout}


def _run_video_direct_analyze(filename: str) -> dict:
    out_dir = _video_output_dir(filename)
    cmd = ["python", str(SCRIPTS_DIR / "direct_video_analyze.py"), filename, "--output-dir", str(out_dir)]
    env = os.environ.copy()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or f"Exit code {result.returncode}")
    analysis = out_dir / "analysis.json"
    mark_extracted(filename, out_dir.name)
    if analysis.is_file():
        return json.loads(analysis.read_text(encoding="utf-8"))
    return {"output": result.stdout}


# ── Tool Registry ──────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    # Amazon (3)
    {"name": "amazon_scrape_url", "description": "抓取 Amazon 商品页面数据，输入完整的 Amazon URL，返回商品标题、价格、评分、评论数等结构化数据。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "完整的 Amazon 商品或搜索结果 URL"}, "pages": {"type": "integer", "description": "抓取页数，默认1", "default": 1}}, "required": ["url"]}},
    {"name": "amazon_scrape_asin", "description": "通过 ASIN 码查询 Amazon 商品详情。ASIN 是 Amazon 标准识别号，10位字母数字组合。",
     "parameters": {"type": "object", "properties": {"asin": {"type": "string", "description": "10位 Amazon ASIN 码"}, "pages": {"type": "integer", "description": "抓取页数，默认1", "default": 1}}, "required": ["asin"]}},
    {"name": "amazon_search_keyword", "description": "在 Amazon 上按关键词搜索商品，返回搜索结果列表。用于选品调研、竞品分析。",
     "parameters": {"type": "object", "properties": {"keyword": {"type": "string", "description": "搜索关键词"}, "pages": {"type": "integer", "description": "抓取页数，默认1", "default": 1}}, "required": ["keyword"]}},

    # TikTok Shop (4)
    {"name": "tiktok_shop_product", "description": "获取 TikTok Shop 商品详情和评论。输入商品链接，返回商品信息、价格、评价等。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok Shop 商品链接"}}, "required": ["url"]}},
    {"name": "tiktok_shop_details", "description": "获取 TikTok Shop 商品详情（不含评论）。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok Shop 商品链接"}}, "required": ["url"]}},
    {"name": "tiktok_shop_reviews", "description": "获取 TikTok Shop 商品评论列表。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok Shop 商品链接或商品ID"}}, "required": ["url"]}},
    {"name": "tiktok_shop_search", "description": "在 TikTok Shop 中搜索商品。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}}, "required": ["query"]}},

    # TikTok API — Profile & User (6)
    {"name": "tiktok_profile", "description": "获取 TikTok 用户主页信息：昵称、粉丝数、关注数、获赞数、视频数、认证状态、简介。",
     "parameters": {"type": "object", "properties": {"handle": {"type": "string", "description": "TikTok 用户名"}}, "required": ["handle"]}},
    {"name": "tiktok_videos", "description": "获取 TikTok 用户发布的视频列表，含播放量、点赞、评论等统计。",
     "parameters": {"type": "object", "properties": {"handle": {"type": "string", "description": "TikTok 用户名"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["handle"]}},
    {"name": "tiktok_videos_popular", "description": "获取 TikTok 当前热门/趋势视频列表。",
     "parameters": {"type": "object", "properties": {"count": {"type": "integer", "description": "返回数量，默认10"}}}},
    {"name": "tiktok_followers", "description": "获取 TikTok 用户的粉丝列表。",
     "parameters": {"type": "object", "properties": {"handle": {"type": "string", "description": "TikTok 用户名"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["handle"]}},
    {"name": "tiktok_following", "description": "获取 TikTok 用户关注的账号列表。",
     "parameters": {"type": "object", "properties": {"handle": {"type": "string", "description": "TikTok 用户名"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["handle"]}},
    {"name": "tiktok_demographics", "description": "获取 TikTok 用户的受众画像数据（年龄、性别、地区分布）。",
     "parameters": {"type": "object", "properties": {"handle": {"type": "string", "description": "TikTok 用户名"}}, "required": ["handle"]}},

    # TikTok API — Video & Content (5)
    {"name": "tiktok_video_info", "description": "获取单个 TikTok 视频的详细信息和统计数据：标题、播放量、点赞、评论、分享、发布时间、音乐、标签。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok 视频链接"}}, "required": ["url"]}},
    {"name": "tiktok_comments", "description": "获取 TikTok 视频的评论列表，包含评论文本、作者、点赞数。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok 视频链接"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["url"]}},
    {"name": "tiktok_comment_replies", "description": "获取 TikTok 视频评论的回复。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok 视频链接"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["url"]}},
    {"name": "tiktok_transcript", "description": "获取 TikTok 视频的字幕/转写文本。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok 视频链接"}}, "required": ["url"]}},
    {"name": "tiktok_live", "description": "获取 TikTok 用户的直播状态信息：是否在直播、观众数。",
     "parameters": {"type": "object", "properties": {"handle": {"type": "string", "description": "TikTok 用户名"}}, "required": ["handle"]}},

    # TikTok API — Search (5)
    {"name": "tiktok_search_users", "description": "在 TikTok 上搜索用户，用于找达人、竞品分析。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["query"]}},
    {"name": "tiktok_search_hashtag", "description": "搜索 TikTok 话题标签下的视频。",
     "parameters": {"type": "object", "properties": {"hashtag": {"type": "string", "description": "话题标签（不带#号）"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["hashtag"]}},
    {"name": "tiktok_search_keyword", "description": "在 TikTok 上按关键词搜索视频，用于内容调研。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["query"]}},
    {"name": "tiktok_search_music", "description": "在 TikTok 上搜索音乐/音频。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "音乐名称或关键词"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["query"]}},
    {"name": "tiktok_search_top", "description": "获取 TikTok 热门搜索趋势。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}}}},

    # TikTok API — Trending & Creators (3)
    {"name": "tiktok_trending", "description": "获取 TikTok 当前趋势视频 Feed。",
     "parameters": {"type": "object", "properties": {"_": {"type": "string", "description": "无需参数"}}}},
    {"name": "tiktok_creators_popular", "description": "获取 TikTok 热门创作者/达人列表。",
     "parameters": {"type": "object", "properties": {"count": {"type": "integer", "description": "返回数量，默认10"}}}},
    {"name": "tiktok_hashtags_popular", "description": "获取 TikTok 当前热门话题标签。",
     "parameters": {"type": "object", "properties": {"count": {"type": "integer", "description": "返回数量，默认10"}}}},

    # TikTok API — Music (3)
    {"name": "tiktok_music_popular", "description": "获取 TikTok 当前热门音乐/音频列表。",
     "parameters": {"type": "object", "properties": {"count": {"type": "integer", "description": "返回数量，默认10"}}}},
    {"name": "tiktok_music_info", "description": "获取指定 TikTok 音乐/音频的详细信息。",
     "parameters": {"type": "object", "properties": {"sound_id": {"type": "string", "description": "音乐/Sound ID"}}, "required": ["sound_id"]}},
    {"name": "tiktok_music_videos", "description": "获取使用指定音乐的 TikTok 视频列表。",
     "parameters": {"type": "object", "properties": {"sound_id": {"type": "string", "description": "音乐/Sound ID"}, "count": {"type": "integer", "description": "返回数量，默认10"}}, "required": ["sound_id"]}},

    # Video Tools (3)
    {"name": "video_download", "description": "下载 TikTok 或抖音的公开视频到服务器，返回视频文件名。下载后可进行分析。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "TikTok 或抖音视频链接"}}, "required": ["url"]}},
    {"name": "video_analyze", "description": "对已下载的视频进行关键帧提取分析（Qwen 视觉 + Whisper 转写）。需要先用 video_download 下载。",
     "parameters": {"type": "object", "properties": {"filename": {"type": "string", "description": "videos/ 目录下的视频文件名"}}, "required": ["filename"]}},
    {"name": "video_direct_analyze", "description": "对视频进行端到端分析（Qwen vision API），适用于7MB以下小视频。",
     "parameters": {"type": "object", "properties": {"filename": {"type": "string", "description": "videos/ 目录下的视频文件名"}}, "required": ["filename"]}},
]


def _override_tool_schema(name: str, description: str, properties: dict[str, dict[str, Any]]) -> None:
    for tool in TOOLS:
        if tool["name"] != name:
            continue
        tool["description"] = description
        tool["parameters"]["properties"].update(properties)
        return


_override_tool_schema(
    "tiktok_shop_product",
    "Get TikTok Shop product details plus reviews/comments. Use this after tiktok_shop_search when the user asks for product details, reviews, comments, complaints, or sentiment. Pass a TikTok Shop PDP URL, canonical_url/product_url, or numeric product_id from tiktok_shop_search. Do not pass normal TikTok /video/ URLs.",
    {"url": {"type": "string", "description": "TikTok Shop product URL containing /shop/pdp/ or a numeric product_id. Not a TikTok /video/ URL."}},
)
_override_tool_schema(
    "tiktok_shop_details",
    "Get TikTok Shop product details without reviews. Use only a TikTok Shop PDP URL like https://www.tiktok.com/shop/pdp/... or a product id from tiktok_shop_search. Do not pass normal TikTok /video/ URLs.",
    {"url": {"type": "string", "description": "TikTok Shop product URL containing /shop/pdp/ or a numeric product_id. Not a TikTok /video/ URL."}},
)
_override_tool_schema(
    "tiktok_shop_reviews",
    "Get TikTok Shop product reviews only. Prefer tiktok_shop_product when the user also needs product details or when starting from search results, because product detail context is usually needed. Use only a TikTok Shop PDP URL/canonical_url/product_url or numeric product_id. Do not pass normal TikTok /video/ URLs.",
    {"url": {"type": "string", "description": "TikTok Shop product URL containing /shop/pdp/ or numeric product_id. Not a TikTok /video/ URL."}},
)
_override_tool_schema(
    "tiktok_shop_search",
    "Search TikTok Shop products. The result includes product_id, title, price, sold_count, shop_name, labels, and product URL/canonical_url when available. For category/product-selection questions, answer from these results if enough_data=true. If the user asks for reviews/comments for specific products, use product_id or canonical_url from these results in tiktok_shop_product as the next tool call.",
    {"query": {"type": "string", "description": "Product search keywords, e.g. 'quiet book travel busy book'. Not a URL."}},
)
_override_tool_schema(
    "tiktok_video_info",
    "Get metadata and stats for a normal TikTok video URL. Use this for https://www.tiktok.com/@user/video/... links. Do not use TikTok Shop product URLs here.",
    {"url": {"type": "string", "description": "Normal TikTok video URL containing /video/."}},
)
_override_tool_schema(
    "tiktok_comments",
    "Get comments for a normal TikTok video URL. Use this with /video/ URLs, not TikTok Shop product URLs.",
    {"url": {"type": "string", "description": "Normal TikTok video URL containing /video/."}},
)
_override_tool_schema(
    "video_download",
    "Download a public TikTok/Douyin video to the server and return the local filename. Use only when actual video frame/audio analysis is needed; for metadata only, use tiktok_video_info.",
    {"url": {"type": "string", "description": "TikTok or Douyin video URL, usually containing /video/."}},
)
_override_tool_schema(
    "video_analyze",
    "Analyze a video file already downloaded by video_download. The filename must be exactly the filename returned by video_download.",
    {"filename": {"type": "string", "description": "Local videos/ filename returned by video_download, not a URL."}},
)


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool by name. Returns {"ok": True, "data": ...} or {"ok": False, "error": ...}."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        if name == "amazon_scrape_url":
            data = _amazon_scrape(str(args["url"]), "url", int(args.get("pages", 1)))
        elif name == "amazon_scrape_asin":
            data = _amazon_scrape(str(args["asin"]), "asin", int(args.get("pages", 1)))
        elif name == "amazon_search_keyword":
            data = _amazon_scrape(str(args["keyword"]), "keyword", int(args.get("pages", 1)))
        elif name.startswith("tiktok_shop_"):
            source_type = name.replace("tiktok_shop_", "")
            target = str(args.get("url") or args.get("query") or "")
            if source_type in {"product", "details", "reviews"} and _is_tiktok_video_url(target):
                return {
                    "ok": False,
                    "error": (
                        "tiktok_shop_* tools require a TikTok Shop product URL or product id; "
                        "a normal TikTok video URL was provided. Use tiktok_video_info/comments "
                        "for video URLs, or use product_id/canonical_url from tiktok_shop_search results."
                    ),
                    "elapsed": round(time.monotonic() - started, 2),
                }
            data = _run_shop(source_type, target)
        elif name.startswith("tiktok_") and name != "tiktok_shop_product":
            endpoint = name.replace("tiktok_", "").replace("_", "-")
            params = {}
            for p in TIKTOK_ENDPOINT_PARAMS.get(endpoint, []):
                val = args.get(p)
                if val is not None:
                    params[p] = val
            data = _run_tiktok_api(endpoint, **params)
        elif name == "video_download":
            data = _run_video_download(str(args["url"]))
        elif name == "video_analyze":
            data = _run_video_analyze(str(args["filename"]))
        elif name == "video_direct_analyze":
            data = _run_video_direct_analyze(str(args["filename"]))
        else:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        return {"ok": True, "data": data, "elapsed": round(time.monotonic() - started, 2)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed": round(time.monotonic() - started, 2)}


def get_tools_for_model(enabled: set[str] | None = None) -> list[dict]:
    """Return tool definitions in DeepSeek function-calling format, filtered by enabled set."""
    result = []
    for t in TOOLS:
        if enabled is None or t["name"] in enabled:
            result.append({"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}})
    return result


def list_tools() -> list[dict]:
    """List all tools grouped by category for the frontend selector."""
    categories = {
        "Amazon": ["amazon_scrape_url", "amazon_scrape_asin", "amazon_search_keyword"],
        "TikTok Shop": ["tiktok_shop_product", "tiktok_shop_details", "tiktok_shop_reviews", "tiktok_shop_search"],
        "TikTok 用户": ["tiktok_profile", "tiktok_videos", "tiktok_videos_popular", "tiktok_followers", "tiktok_following", "tiktok_demographics"],
        "TikTok 视频": ["tiktok_video_info", "tiktok_comments", "tiktok_comment_replies", "tiktok_transcript", "tiktok_live"],
        "TikTok 搜索": ["tiktok_search_users", "tiktok_search_hashtag", "tiktok_search_keyword", "tiktok_search_music", "tiktok_search_top"],
        "TikTok 趋势": ["tiktok_trending", "tiktok_creators_popular", "tiktok_hashtags_popular"],
        "TikTok 音乐": ["tiktok_music_popular", "tiktok_music_info", "tiktok_music_videos"],
        "视频分析": ["video_download", "video_analyze", "video_direct_analyze"],
    }
    result = []
    for cat, names in categories.items():
        items = [{"name": t["name"], "description": t["description"]} for t in TOOLS if t["name"] in names]
        result.append({"category": cat, "tools": items})
    return result
