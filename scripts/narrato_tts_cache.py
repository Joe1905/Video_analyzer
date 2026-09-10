"""Reuse completed TTS audio and timing across video export jobs."""
import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger

# ponytail: serialize synthesis in this single-process trial; use per-key locks if throughput matters.
_lock = threading.Lock()


def cached_tts(*, text, voice_name, voice_rate, voice_pitch, voice_file, tts_engine):
    from app.config import config
    from app.services import voice

    # Include provider configuration (hashed only), exclude editing/UI settings.
    settings = {name: value for name, value in vars(config).items()
                if not name.startswith("_") and isinstance(value, dict)
                and name not in {"ui", "frames", "proxy"}}
    identity = [text, voice_name, voice_rate, voice_pitch, tts_engine, settings]
    # Local cloning inputs can change without changing their configured filename.
    def files(value):
        if isinstance(value, dict):
            return [entry for item in value.values() for entry in files(item)]
        if isinstance(value, str) and len(value) < 1024:
            try:
                p = Path(value)
                if p.is_file():
                    return [(str(p), hashlib.sha256(p.read_bytes()).hexdigest())]
            except OSError:
                pass
        return []
    identity.append(files(settings))
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    root = Path("/NarratoAI/storage/tts_cache")
    root.mkdir(parents=True, exist_ok=True)
    entry = root / digest
    audio = entry / ("audio" + Path(voice_file).suffix)
    metadata = entry / "timing.json"
    with _lock:
        if metadata.exists() and audio.exists():
            try:
                timing = json.loads(metadata.read_text(encoding="utf-8"))
                if hashlib.sha256(audio.read_bytes()).hexdigest() != timing["sha256"]:
                    raise ValueError("Cached audio changed")
                duration = float(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(audio)
                ], timeout=30))
                if duration <= 0:
                    raise ValueError("Invalid cached audio")
                result = voice.new_sub_maker()
                for offset, sub in zip(timing["offset"], timing["subs"], strict=True):
                    voice.add_subtitle_event(result, *offset, sub)
                shutil.copyfile(audio, voice_file)
                logger.info(f"复用已有配音及字幕时间轴：{digest[:12]}，不请求 TTS")
                return result
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                logger.warning("配音缓存不完整或已损坏，将重新合成该片段")
        result = voice.tts(text, voice_name, voice_rate, voice_pitch, voice_file, tts_engine)
        if result is not None and Path(voice_file).is_file():
            with TemporaryDirectory(dir=root) as directory:
                temporary = Path(directory)
                shutil.copyfile(voice_file, temporary / audio.name)
                (temporary / metadata.name).write_text(json.dumps({
                    "sha256": hashlib.sha256(Path(voice_file).read_bytes()).hexdigest(),
                    "offset": result.offset, "subs": result.subs,
                }, ensure_ascii=False), encoding="utf-8")
                entry.mkdir(exist_ok=True)
                (temporary / audio.name).replace(audio)
                (temporary / metadata.name).replace(metadata)
        return result


if __name__ == "__main__":
    path = Path("/NarratoAI/app/services/voice.py")
    source = path.read_text(encoding="utf-8")
    before = "            sub_maker = tts(\n                text=text,"
    assert source.count(before) == 1
    source = source.replace(before, "            from app.services.narrato_tts_cache import cached_tts\n            sub_maker = cached_tts(\n                text=text,", 1)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
