"""Amazon scraper job orchestration without HTTP dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import subprocess
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote_plus, urlparse

from jobs.registry import JobRegistry
from jobs.snapshots import snapshot_amazon_job


ALLOWED_AMAZON_HOST_SUFFIXES = ("amazon.com",)
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)


@dataclass
class AmazonJob:
    id: str
    target: str
    target_type: str
    url: str
    pages: int
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    output_dir: str | None = None
    error: str | None = None


def validate_amazon_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https Amazon URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_AMAZON_HOST_SUFFIXES):
        raise ValueError("Only amazon.com URLs are supported")
    if len(cleaned) > 2048:
        raise ValueError("URL is too long")
    return cleaned


def amazon_url_for_target(target: str, target_type: str) -> str:
    cleaned = target.strip()
    if not cleaned:
        raise ValueError("Amazon URL, ASIN, or keyword is required")
    if target_type == "url":
        return validate_amazon_url(cleaned)
    if target_type == "asin":
        asin = cleaned.upper()
        if not ASIN_RE.match(asin):
            raise ValueError("ASIN must be 10 letters or digits")
        return f"https://www.amazon.com/dp/{asin}"
    if target_type == "keyword":
        if len(cleaned) > 200:
            raise ValueError("Keyword is too long")
        return f"https://www.amazon.com/s?k={quote_plus(cleaned)}"
    raise ValueError("target_type must be url, asin, or keyword")


def parse_json_from_process_output(output: str) -> Any:
    text = output.strip()
    if not text:
        raise ValueError("amazon-scraper returned no output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed_values = []
        for match in re.finditer(r"{", text):
            try:
                value, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            parsed_values.append(value)
        if not parsed_values:
            raise ValueError("amazon-scraper output did not contain JSON")
        parsed_values.sort(key=lambda value: len(json.dumps(value)), reverse=True)
        return parsed_values[0]


class AmazonService:
    def __init__(
        self,
        registry: JobRegistry,
        root: Path,
        output_dir: Path,
        read_json_file: Callable[[Path], Any],
        write_json_file: Callable[[Path, Any], None],
        popen_factory: Callable[..., Any],
        thread_factory: Callable[..., Any],
        job_id_factory: Callable[[], str],
        environ: Mapping[str, str],
        ensure_us_proxy: Callable[..., Any],
        get_cached_or_call: Callable[..., Any],
        cache_log_label: Callable[[Any], str | None],
    ) -> None:
        self._registry = registry
        self._root = root
        self._output_dir = output_dir
        self._read_json_file = read_json_file
        self._write_json_file = write_json_file
        self._popen_factory = popen_factory
        self._thread_factory = thread_factory
        self._job_id_factory = job_id_factory
        self._environ = environ
        self._ensure_us_proxy = ensure_us_proxy
        self._get_cached_or_call = get_cached_or_call
        self._cache_log_label = cache_log_label

    def create_and_start(self, *, target: str, target_type: str, url: str, pages: int) -> dict[str, Any]:
        job = AmazonJob(
            id=self._job_id_factory(),
            target=target,
            target_type=target_type,
            url=url,
            pages=pages,
        )
        self._registry.register(job.id, job)
        thread = self._thread_factory(target=self.run_job, args=(job.id,), daemon=True)
        thread.start()
        payload = self.payload_for(job.id)
        if payload is None:
            raise RuntimeError("Amazon job disappeared after registration")
        return payload

    def append_log(self, job_id: str, line: str) -> None:
        self._registry.append_log(job_id, line)

    def run_command(self, job_id: str, command: list[str]) -> tuple[str, int]:
        self.append_log(job_id, f"$ {' '.join(command)}")
        process = self._popen_factory(
            command,
            cwd=self._root,
            env=dict(self._environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            self.append_log(job_id, line)
        code = process.wait()
        output = "".join(output_lines)
        if code != 0:
            self.append_log(job_id, f"Command exited with code {code}")
        return output, code

    def run_job(self, job_id: str) -> None:
        initial = self._registry.snapshot(job_id)
        if initial is None:
            return
        url = initial.url
        pages = initial.pages
        self._registry.update_fields(job_id, {"status": "running"})

        job_output_dir = self._output_dir / "amazon" / job_id
        result_path = job_output_dir / "result.json"
        try:
            job_output_dir.mkdir(parents=True, exist_ok=True)
            self._registry.update_fields(
                job_id,
                {"output_dir": str(job_output_dir.relative_to(self._root))},
            )

            def normalized_amazon_url(value: str) -> str:
                parsed = urlparse(value.strip())
                host = (parsed.hostname or "").lower()
                return parsed._replace(
                    scheme=(parsed.scheme or "https").lower(),
                    netloc=host,
                    fragment="",
                ).geturl()

            def fetch_amazon() -> dict[str, Any]:
                self._ensure_us_proxy("amazon", log=lambda line: self.append_log(job_id, line))
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "host",
                    "-e",
                    "AMAZON_PROXY",
                    "-e",
                    "AMAZON_PROXIES",
                    "amazon-scraper",
                    "node",
                    "assets/amazon_handler.js",
                    url,
                    "--pages",
                    str(pages),
                ]
                output, code = self.run_command(job_id, command)
                parsed = parse_json_from_process_output(output)
                if code != 0 and not (isinstance(parsed, dict) and parsed.get("status") == "ERROR"):
                    raise RuntimeError(f"amazon-scraper exited with code {code}")
                return parsed

            result = self._get_cached_or_call(
                "amazon_scraper",
                "web",
                {"url": normalized_amazon_url(url), "pages": int(pages)},
                fetch_amazon,
                metadata_builder=lambda payload: {
                    "entity_type": "amazon",
                    "entity_id": str((payload.get("products") or [{}])[0].get("asin") or normalized_amazon_url(url))
                    if isinstance(payload, dict)
                    else normalized_amazon_url(url),
                    "title": str((payload.get("products") or [{}])[0].get("title") or "")
                    if isinstance(payload, dict)
                    else "",
                    "source_url": normalized_amazon_url(url),
                },
            )
            cache_label = self._cache_log_label(result)
            if cache_label:
                self.append_log(job_id, cache_label)
            self._write_json_file(result_path, result)

            if not (isinstance(result, dict) and result.get("status") == "ERROR"):
                self._registry.update_fields(job_id, {"status": "complete"})
            else:
                self._registry.update_fields(
                    job_id,
                    {
                        "status": "failed",
                        "error": str(result.get("message") or "amazon-scraper failed"),
                    },
                )
        except FileNotFoundError:
            message = "Docker CLI is not available in the web container"
            self._registry.update_fields(
                job_id,
                {"status": "failed", "error": message},
                final_log=message,
            )
        except Exception as exc:
            self._registry.update_fields(
                job_id,
                {"status": "failed", "error": str(exc)},
                final_log=str(exc),
            )

    def payload_for(self, job_id: str) -> dict[str, Any] | None:
        job = self._registry.snapshot(job_id)
        if job is None:
            return None
        result = self._read_json_file(self._output_dir / "amazon" / job.id / "result.json")
        return snapshot_amazon_job(job, result=result)
