"""ElevenLabs adapter and build-time hooks for the pinned NarratoAI image."""
import base64
import math
import re
from pathlib import Path
from urllib.parse import quote

import requests
from loguru import logger


def report_error(message, key=""):
    message = str(message).replace(key, "[REDACTED]") if key else str(message)
    message = re.sub(r"sk_[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = " ".join(message.split())[:1500]
    logger.error(message)
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True):
        import streamlit as st
        st.error(message)
    return message


def response_error(response, operation, key):
    try:
        data = response.json()
        detail = data.get("detail", data.get("error", "未提供错误详情"))
    except ValueError:
        detail = response.text
    return report_error(
        f"ElevenLabs {operation}失败：HTTP {response.status_code}; "
        f"request-id={response.headers.get('request-id', response.headers.get('x-request-id', '未提供'))}; "
        f"content-type={response.headers.get('content-type', '未提供')}; {detail}", key)


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
            response_error(response, "配音", key)
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
        report_error(f"ElevenLabs 配音失败：{type(exc).__name__}: {exc}", key)
        return None


def get_preview(voice_id, key):
    if not voice_id.strip() or not key.strip():
        report_error("请先填写 ElevenLabs API Key 和 Voice ID")
        return None
    try:
        response = requests.get(
            f"https://api.elevenlabs.io/v1/voices/{quote(voice_id.strip(), safe='')}",
            headers={"xi-api-key": key.strip()}, timeout=(10, 30))
        if response.status_code != 200:
            response_error(response, "获取音色预览", key)
            return None
        url = response.json().get("preview_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            report_error("该音色没有可用的预设音频；可更换音色或使用合成试听")
            return None
        return url
    except (requests.RequestException, ValueError, TypeError) as exc:
        report_error(f"ElevenLabs 音色预览失败：{type(exc).__name__}: {exc}", key)
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
    if st.button("保存 ElevenLabs 配置"):
        config.save_config()
        st.success("ElevenLabs 配置已保存")
    st.caption("音色预览播放已有示例，不生成新语音、不消耗合成积分；示例不代表当前模型和文案的效果。")
    if st.button("播放音色预设音频（不合成）"):
        url = get_preview(config.app["elevenlabs_voice_id"], config.app["elevenlabs_api_key"])
        if url:
            st.audio(url, format="audio/mp3")
    st.caption("下方合成试听会调用 TTS API 并消耗积分；正式配音同时生成时间戳字幕。")


def install():
    patches = {
        "/NarratoAI/app/services/voice.py": [
            ('    if tts_engine == "tencent_tts":',
             '    if tts_engine == "elevenlabs":\n        from app.services.narrato_elevenlabs import synthesize\n        return synthesize(text, voice_name, voice_file)\n\n    if tts_engine == "tencent_tts":'),
        ],
        "/NarratoAI/webui/components/audio_settings.py": [
            ('    if st.button(tr("Preview Voice Synthesis"), use_container_width=True):',
             '    if st.button("合成试听（消耗积分）" if selected_engine == "elevenlabs" else tr("Preview Voice Synthesis"), use_container_width=True):'),
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
