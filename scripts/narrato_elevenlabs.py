"""ElevenLabs adapter and build-time hooks for the pinned NarratoAI image."""
import base64
import math
from pathlib import Path
from urllib.parse import quote

import requests
from loguru import logger


def synthesize(text, voice_id, voice_file):
    from app.config import config
    from app.services.voice import new_sub_maker, add_subtitle_event

    key = config.app.get("elevenlabs_api_key", "").strip()
    if not key or not voice_id.strip() or not text.strip():
        logger.warning("ElevenLabs：请填写 API Key、Voice ID 和配音文本")
        return None
    try:
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice_id.strip(), safe='')}/with-timestamps",
            headers={"xi-api-key": key},
            params={"output_format": "mp3_44100_128"},
            json={"text": text, "model_id": config.app.get("elevenlabs_model_id", "eleven_multilingual_v2")},
            timeout=(10, 120),
        )
        if response.status_code != 200:
            logger.error(f"ElevenLabs 配音失败：HTTP {response.status_code}，请检查密钥权限、额度、音色和模型")
            return None
        data = response.json()
        audio = base64.b64decode(data["audio_base64"], validate=True)
        alignment = data["alignment"]
        chars = alignment["characters"]
        starts = alignment["character_start_times_seconds"]
        ends = alignment["character_end_times_seconds"]
        if not audio or not chars or not len(chars) == len(starts) == len(ends):
            raise ValueError("Incomplete audio alignment")
        sub_maker = new_sub_maker()
        previous = 0
        for char, start, end in zip(chars, starts, ends):
            if not isinstance(char, str) or not math.isfinite(start) or not math.isfinite(end) or not 0 <= previous <= start <= end:
                raise ValueError("Invalid audio alignment")
            add_subtitle_event(sub_maker, round(start * 10000000), round(end * 10000000), char)
            previous = start
        Path(voice_file).write_bytes(audio)
        return sub_maker
    except (requests.RequestException, ValueError, KeyError, TypeError, OSError) as exc:
        # Do not log request headers or provider response bodies containing credentials.
        logger.error(f"ElevenLabs 配音失败：{type(exc).__name__}")
        return None


def render_settings(tr):
    import streamlit as st
    from app.config import config

    for name, label, default in (
        ("api_key", "ElevenLabs API Key", ""),
        ("voice_id", "ElevenLabs Voice ID", ""),
        ("model_id", "ElevenLabs 模型", "eleven_multilingual_v2"),
    ):
        key = f"elevenlabs_{name}"
        config.app[key] = st.text_input(label, value=config.app.get(key, default),
                                        type="password" if name == "api_key" else "default", key=key).strip()
    config.ui["voice_name"] = config.app["elevenlabs_voice_id"]
    st.session_state["voice_rate"] = 1.0
    st.session_state["voice_pitch"] = 1.0
    st.caption("在 ElevenLabs 音色库复制 Voice ID；填好后可点击下方试听。配音会同时生成时间戳字幕。")


def install():
    patches = {
        "/NarratoAI/app/services/voice.py": [
            ('    if tts_engine == "tencent_tts":',
             '    if tts_engine == "elevenlabs":\n        from app.services.narrato_elevenlabs import synthesize\n        return synthesize(text, voice_name, voice_file)\n\n    if tts_engine == "tencent_tts":'),
        ],
        "/NarratoAI/webui/components/audio_settings.py": [
            ('        "azure_speech": "Azure Speech Services"',
             '        "elevenlabs": "ElevenLabs",\n        "azure_speech": "Azure Speech Services"'),
            ('    if selected_engine == "edge_tts":\n        render_edge_tts_settings(tr)',
             '    if selected_engine == "elevenlabs":\n        from app.services.narrato_elevenlabs import render_settings\n        render_settings(tr)\n    elif selected_engine == "edge_tts":\n        render_edge_tts_settings(tr)'),
            ('        elif selected_engine == "azure_speech":',
             '        elif selected_engine == "elevenlabs":\n            voice_name = config.app.get("elevenlabs_voice_id", "")\n        elif selected_engine == "azure_speech":'),
        ],
    }
    for filename, replacements in patches.items():
        path = Path(filename)
        source = path.read_text(encoding="utf-8")
        for before, after in replacements:
            assert source.count(before) == 1, f"Pinned source changed: {filename}: {before}"
            source = source.replace(before, after, 1)
        compile(source, filename, "exec")
        path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    install()
