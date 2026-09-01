"""Short-video download job orchestration without HTTP dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from jobs.registry import JobRegistry
from jobs.snapshots import snapshot_download_job


DEFAULT_DOWNLOAD_SOURCE = "api_upload"
ALLOWED_SHORT_VIDEO_HOST_SUFFIXES = ("tiktok.com", "tiktokv.com", "douyin.com", "iesdouyin.com")
AUDIO_ONLY_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}


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


@dataclass
class DownloadJob:
    id: str
    url: str
    source: str = DEFAULT_DOWNLOAD_SOURCE
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    filename: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class DownloadService:
    """Own the Download domain while delegating infrastructure through injection."""

    def __init__(
        self,
        registry: JobRegistry,
        root: Path,
        videos_dir: Path,
        output_dir: Path,
        scripts_dir: Path,
        read_json_file: Callable[[Path], Any],
        write_json_file: Callable[[Path, Any], None],
        run_factory: Callable[..., Any],
        thread_factory: Callable[..., Any],
        job_id_factory: Callable[[], str],
        fallback_video_id_factory: Callable[[], str],
        environ: Mapping[str, str],
        get_cached: Callable[..., Any],
        store_response: Callable[..., Any],
        video_cache_request: Callable[[str], Any],
        video_cache_metadata: Callable[[dict[str, Any], str], Any],
        with_download_cache_meta: Callable[[dict[str, Any], bool], dict[str, Any]],
        register_video: Callable[..., Any],
        register_from_payload: Callable[..., Any],
        platform_for_url: Callable[[str], str],
        video_source_hidden: Callable[[str], bool],
        make_web_manual_visible: Callable[[str, str, str], None],
        start_social_context_job: Callable[..., bool],
        safe_filename: Callable[[str], str],
        analyzer_media_is_valid: Callable[[Path], bool],
        ensure_analyzer_media_or_delete: Callable[[Path], None],
        ensure_us_proxy: Callable[..., Any],
        requests_get: Callable[..., Any],
        cache_log_label: Callable[[Any], str | None],
        normalize_video_source: Callable[[Any, str], str],
        default_source: str,
        video_media_ttl_seconds: int,
        default_sociavault_api_base: str,
    ) -> None:
        self._registry = registry
        self._root = root
        self._videos_dir = videos_dir
        self._output_dir = output_dir
        self._scripts_dir = scripts_dir
        self._read_json_file = read_json_file
        self._write_json_file = write_json_file
        self._run_factory = run_factory
        self._thread_factory = thread_factory
        self._job_id_factory = job_id_factory
        self._fallback_video_id_factory = fallback_video_id_factory
        self._environ = environ
        self._get_cached = get_cached
        self._store_response = store_response
        self._video_cache_request = video_cache_request
        self._video_cache_metadata = video_cache_metadata
        self._with_download_cache_meta = with_download_cache_meta
        self._register_video = register_video
        self._register_from_payload = register_from_payload
        self._platform_for_url = platform_for_url
        self._video_source_hidden = video_source_hidden
        self._make_web_manual_visible = make_web_manual_visible
        self._start_social_context_job = start_social_context_job
        self._safe_filename = safe_filename
        self._analyzer_media_is_valid = analyzer_media_is_valid
        self._ensure_analyzer_media_or_delete = ensure_analyzer_media_or_delete
        self._ensure_us_proxy = ensure_us_proxy
        self._requests_get = requests_get
        self._cache_log_label = cache_log_label
        self._normalize_video_source = normalize_video_source
        self._default_source = default_source
        self._video_media_ttl_seconds = video_media_ttl_seconds
        self._default_sociavault_api_base = default_sociavault_api_base

    def create_and_start(self, *, url: str, source: Any) -> dict[str, Any]:
        validated_url = validate_short_video_url(url)
        normalized_source = self._normalize_video_source(source, self._default_source)
        job = DownloadJob(id=self._job_id_factory(), url=validated_url, source=normalized_source)
        self._registry.register(job.id, job)
        thread = self._thread_factory(target=self.run_job, args=(job.id,), daemon=True)
        thread.start()
        payload = self.payload_for(job.id)
        if payload is None:
            raise RuntimeError("Download job disappeared after registration")
        return payload

    def register_failed_attempt(self, *, url: str, error: str) -> dict[str, Any]:
        job = DownloadJob(id=self._job_id_factory(), url=url, source=self._default_source, status="failed")
        job.error = error
        job.log.append(error)
        self._registry.register(job.id, job)
        payload = self.payload_for(job.id)
        if payload is None:
            raise RuntimeError("Download job disappeared after failure registration")
        return payload

    def append_log(self, job_id: str, line: str) -> None:
        self._registry.append_log(job_id, line)

    def run_command(self, job_id: str, command: list[str]) -> None:
        self.append_log(job_id, f"$ {' '.join(command)}")
        timeout = int(self._environ.get("DOWNLOAD_COMMAND_TIMEOUT", "210"))
        try:
            result = self._run_factory(
                command,
                cwd=self._root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            if exc.stdout:
                for line in str(exc.stdout).splitlines():
                    self.append_log(job_id, line)
            raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc
        for line in (result.stdout or "").splitlines():
            self.append_log(job_id, line)
        if result.returncode != 0:
            raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(command)}")

    @staticmethod
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

    @classmethod
    def _iter_media_url_candidates(cls, value: Any, path: str = "") -> list[tuple[int, str, str]]:
        candidates: list[tuple[int, str, str]] = []
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                candidates.extend(cls._iter_media_url_candidates(child, child_path))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                candidates.extend(cls._iter_media_url_candidates(child, f"{path}[{index}]"))
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            lowered = f"{path} {value}".lower()
            if any(word in lowered for word in ("cover", "avatar", "thumbnail", "image", "music", "audio", "subtitle")):
                return []
            if not any(word in lowered for word in ("video", "download", "play", ".mp4", "mime_type=video", "mime=video")):
                return []
            candidates.append((cls._score_media_candidate(path, value), path, value))
        return candidates

    def _sociavault_video_id(self, payload: Any, fallback_url: str) -> str:
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
        return self._fallback_video_id_factory()

    def _download_direct_media(self, job_id: str, media_url: str, source_url: str, payload: Any) -> dict[str, Any]:
        self._ensure_us_proxy("tiktok", log=lambda line: self.append_log(job_id, line))
        parsed = urlparse(media_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("SociaVault media URL is not http/https")
        max_bytes = int(self._environ.get("TIKTOK_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
        video_id = self._sociavault_video_id(payload, source_url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".webm"}:
            suffix = ".mp4"
        target = self._videos_dir / self._safe_filename(f"shortvideo_SociaVault_{video_id}{suffix}")
        temp_target = target.with_suffix(target.suffix + ".part")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Referer": source_url,
        }
        proxy = self._environ.get("TIKTOK_PROXY_URL", "").strip()
        attempts: list[tuple[str, dict[str, str] | None]] = []
        if proxy:
            attempts.append((f"proxy={proxy}", {"http": proxy, "https": proxy}))
        attempts.append(("direct", None))

        self.append_log(job_id, f"SociaVault 媒体直链下载：{media_url[:180]}")
        try:
            errors = []
            for attempt_label, proxies in attempts:
                temp_target.unlink(missing_ok=True)
                try:
                    self.append_log(job_id, f"SociaVault media direct attempt: {attempt_label}")
                    with self._requests_get(media_url, headers=headers, proxies=proxies, stream=True, timeout=(8, 60)) as response:
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
                size = temp_target.stat().st_size
                raise RuntimeError(f"SociaVault media file is too small: {size} bytes")
            temp_target.replace(target)
            self._ensure_analyzer_media_or_delete(target)
        except Exception:
            temp_target.unlink(missing_ok=True)
            raise
        return {
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
        }

    def _sociavault_video_info_request(self, url: str) -> dict[str, Any]:
        api_base = self._environ.get("SOCIAVAULT_API_BASE", self._default_sociavault_api_base).rstrip("/")
        return {"api_base": api_base, "endpoint": "video-info", "params": {"url": url}}

    def try_cached_download_result(self, job_id: str, url: str, source: str, result_path: Path) -> bool:
        cached = self._get_cached("short_video_download", "download", self._video_cache_request(url))
        if not isinstance(cached, dict) or not cached.get("filename"):
            return False
        filename = self._safe_filename(str(cached["filename"]))
        cached_path = self._videos_dir / filename
        if not cached_path.is_file():
            self.append_log(job_id, f"下载结果缓存文件不存在，继续重新下载：{filename}")
            return False
        if cached_path.suffix.lower() in AUDIO_ONLY_SUFFIXES:
            cached_path.unlink(missing_ok=True)
            self.append_log(job_id, f"删除缓存命中的无效音频文件，重新下载：{filename}")
            return False
        if not self._analyzer_media_is_valid(cached_path):
            cached_path.unlink(missing_ok=True)
            self.append_log(job_id, f"删除缓存命中的无效视频文件，重新下载：{filename}")
            return False
        result = self._with_download_cache_meta(dict(cached), True)
        result["path"] = str(cached_path)
        if result.get("id"):
            video_id = str(result.get("id"))
            platform = self._platform_for_url(url)
            self._register_video(
                video_id=video_id,
                platform=platform,
                source_url=str(result.get("webpage_url") or url),
                filename=filename,
                title=str(result.get("title") or ""),
                author=str(result.get("uploader") or ""),
                source=source,
                hidden_from_analyzer=self._video_source_hidden(source),
            )
            self._make_web_manual_visible(source, platform, video_id)
        self._write_json_file(result_path, result)
        self.append_log(job_id, "下载结果缓存命中，复用本地视频文件。")
        return True

    def _store_download_result(self, url: str, source: str, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("id"):
            video_id = str(result.get("id"))
            platform = self._platform_for_url(url)
            self._register_video(
                video_id=video_id,
                platform=platform,
                source_url=str(result.get("webpage_url") or url),
                filename=str(result.get("filename") or ""),
                title=str(result.get("title") or ""),
                author=str(result.get("uploader") or ""),
                source=source,
                hidden_from_analyzer=self._video_source_hidden(source),
            )
            self._make_web_manual_visible(source, platform, video_id)
        self._store_response(
            "short_video_download",
            "download",
            self._video_cache_request(url),
            result,
            metadata=self._video_cache_metadata(result, url),
        )
        return self._with_download_cache_meta(result, False)

    def _media_cache_payload(self, url: str, payload: Any) -> dict[str, Any]:
        candidates = sorted(self._iter_media_url_candidates(payload), key=lambda item: item[0], reverse=True)
        return {
            "source_url": url,
            "video_id": self._sociavault_video_id(payload, url),
            "candidates": [
                {"score": score, "path": path, "url": media_url}
                for score, path, media_url in candidates[:12]
            ],
        }

    def _media_cache_is_stale(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return True
        cache_meta = payload.get("_cache")
        if not isinstance(cache_meta, dict):
            return False
        age = cache_meta.get("age_seconds")
        if age is None:
            return False
        return float(age) > self._video_media_ttl_seconds

    def _try_media_cache_payload_download(
        self,
        job_id: str,
        url: str,
        source: str,
        payload: Any,
        result_path: Path,
        source_label: str,
    ) -> bool:
        if source_label.startswith("缓存") and self._media_cache_is_stale(payload):
            self.append_log(job_id, "媒体地址缓存已过期，刷新 SociaVault video-info。")
            return False
        raw_candidates = payload.get("candidates") if isinstance(payload, dict) else []
        candidates = [item for item in raw_candidates if isinstance(item, dict) and item.get("url")]
        for item in candidates:
            item["score"] = self._score_media_candidate(str(item.get("path") or ""), str(item.get("url") or ""))
        candidates.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
        self.append_log(job_id, f"{source_label} 媒体地址缓存返回 {len(candidates)} 个候选地址。")
        for item in candidates[:12]:
            path = str(item.get("path") or "")
            media_url = str(item.get("url") or "")
            score = item.get("score")
            try:
                self.append_log(job_id, f"{source_label} 尝试候选地址 score={score} path={path}")
                result = self._download_direct_media(job_id, media_url, url, payload)
                result["video_info_source"] = source_label
                if isinstance(payload, dict) and isinstance(payload.get("_cache"), dict):
                    result["media_cache"] = payload["_cache"]
                result = self._store_download_result(url, source, result)
                self._write_json_file(result_path, result)
                return True
            except Exception as exc:
                self.append_log(job_id, f"{source_label} 候选地址不可用：{exc}")
        return False

    def _try_video_info_payload_download(
        self,
        job_id: str,
        url: str,
        source: str,
        payload: Any,
        result_path: Path,
        source_label: str,
    ) -> bool:
        record = self._register_from_payload(
            payload,
            source_url=url,
            source=source,
            hidden_from_analyzer=self._video_source_hidden(source),
        )
        if record:
            self._make_web_manual_visible(
                source,
                str(record.get("platform") or self._platform_for_url(url)),
                str(record.get("video_id") or ""),
            )
        media_payload = self._media_cache_payload(url, payload)
        if media_payload["candidates"]:
            self._store_response(
                "sociavault_tiktok_media",
                "video-info-media",
                self._sociavault_video_info_request(url),
                media_payload,
                ttl_seconds=self._video_media_ttl_seconds,
                metadata={
                    "entity_type": "tiktok_video_media",
                    "entity_id": media_payload.get("video_id"),
                    "source_url": url,
                },
            )
        return self._try_media_cache_payload_download(job_id, url, source, media_payload, result_path, source_label)

    def try_cached_video_info_download(self, job_id: str, url: str, source: str, result_path: Path) -> bool:
        payload = self._get_cached(
            "sociavault_tiktok_media",
            "video-info-media",
            self._sociavault_video_info_request(url),
            ttl_seconds=self._video_media_ttl_seconds,
        )
        if not isinstance(payload, dict):
            self.append_log(job_id, "媒体地址缓存未命中。")
            return False
        return self._try_media_cache_payload_download(job_id, url, source, payload, result_path, "缓存")

    def try_sociavault_video_info_download(self, job_id: str, url: str, source: str, result_path: Path) -> bool:
        if not self._environ.get("SOCIAVAULT_API_KEY", "").strip():
            self.append_log(job_id, "未配置 SOCIAVAULT_API_KEY，跳过 SociaVault video-info。")
            return False
        output_path = result_path.with_suffix(".sociavault-video-info.json")
        try:
            self.run_command(
                job_id,
                [
                    "python",
                    str(self._scripts_dir / "sociavault_tiktok.py"),
                    "--endpoint",
                    "video-info",
                    "--url",
                    url,
                    "--output",
                    str(output_path),
                ],
            )
            payload = self._read_json_file(output_path)
            if self._try_video_info_payload_download(job_id, url, source, payload, result_path, "SociaVault API"):
                result = self._read_json_file(result_path)
                if isinstance(result, dict):
                    result["sociavault_video_info"] = str(output_path.relative_to(self._root))
                    self._write_json_file(result_path, result)
                return True
            return False
        except Exception as exc:
            self.append_log(job_id, f"SociaVault video-info 下载链路失败，回退原下载器：{exc}")
            return False

    def run_job(self, job_id: str) -> None:
        initial = self._registry.snapshot(job_id)
        if initial is None:
            return
        url = initial.url
        source = initial.source
        self._registry.update_fields(job_id, {"status": "running"})

        result_path = self._output_dir / "download_jobs" / f"{job_id}.json"
        try:
            self._videos_dir.mkdir(parents=True, exist_ok=True)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.try_cached_download_result(job_id, url, source, result_path) and not self.try_cached_video_info_download(
                job_id, url, source, result_path
            ):
                crawler_error: Exception | None = None
                try:
                    self.append_log(job_id, "缓存地址不可用，使用原下载器下载。")
                    self.run_command(
                        job_id,
                        [
                            "python",
                            str(self._scripts_dir / "tiktok_download.py"),
                            url,
                            "--output-dir",
                            str(self._videos_dir),
                            "--result-json",
                            str(result_path),
                        ],
                    )
                    result = self._read_json_file(result_path)
                    filename = self._safe_filename(str(result.get("filename") or "")) if isinstance(result, dict) else ""
                    if filename and Path(filename).suffix.lower() in AUDIO_ONLY_SUFFIXES:
                        audio_path = self._videos_dir / filename
                        audio_path.unlink(missing_ok=True)
                        crawler_error = RuntimeError(f"original downloader returned audio-only media: {filename}")
                        self.append_log(job_id, f"删除无效音频文件并降级到 SociaVault video-info：{filename}")
                        if not self.try_sociavault_video_info_download(job_id, url, source, result_path):
                            raise RuntimeError(
                                "视频下载失败：原下载器只返回音频文件，SociaVault video-info 也没有可用下载地址。"
                            ) from crawler_error
                    elif filename:
                        try:
                            self._ensure_analyzer_media_or_delete(self._videos_dir / filename)
                        except Exception as exc:
                            crawler_error = exc
                            self.append_log(job_id, f"删除无效视频文件并降级到 SociaVault video-info：{filename}，原因：{exc}")
                            if not self.try_sociavault_video_info_download(job_id, url, source, result_path):
                                raise RuntimeError(
                                    "视频下载失败：原下载器返回的文件不可分析，SociaVault video-info 也没有可用下载地址。"
                                ) from crawler_error
                except Exception as exc:
                    crawler_error = exc
                    self.append_log(job_id, f"原下载器失败，最后降级调用 SociaVault video-info：{exc}")
                    if not self.try_sociavault_video_info_download(job_id, url, source, result_path):
                        raise RuntimeError(
                            "视频下载失败：缓存地址不可用，原下载器失败，SociaVault video-info 也没有可用下载地址。"
                        ) from crawler_error
            result = self._read_json_file(result_path)
            if not isinstance(result, dict) or not result.get("filename"):
                raise RuntimeError("Downloader did not return a video filename")
            cache_label = self._cache_log_label(result)
            if cache_label:
                self.append_log(job_id, cache_label)
            filename = self._safe_filename(str(result["filename"]))
            if not (self._videos_dir / filename).is_file():
                raise FileNotFoundError(f"Downloaded file not found: {filename}")
            if result.get("id"):
                video_id = str(result.get("id"))
                platform = self._platform_for_url(url)
                self._register_video(
                    video_id=video_id,
                    platform=platform,
                    source_url=str(result.get("webpage_url") or url),
                    filename=filename,
                    title=str(result.get("title") or ""),
                    author=str(result.get("uploader") or ""),
                    source=source,
                    hidden_from_analyzer=self._video_source_hidden(source),
                )
                self._make_web_manual_visible(source, platform, video_id)
            self._registry.update_fields(job_id, {"filename": filename, "result": result, "status": "complete"})
            self._start_social_context_job(filename, generate_insights=True)
        except Exception as exc:
            latest = self._registry.snapshot(job_id)
            useful_log = next(
                (
                    line
                    for line in reversed(latest.log if latest is not None else [])
                    if line and not line.startswith("$ ") and not line.startswith("Command failed with exit code")
                ),
                "",
            )
            self._registry.update_fields(
                job_id,
                {"status": "failed", "error": useful_log or str(exc)},
                final_log=str(exc),
            )

    def payload_for(self, job_id: str) -> dict[str, Any] | None:
        job = self._registry.snapshot(job_id)
        if job is None:
            return None
        return snapshot_download_job(job)
