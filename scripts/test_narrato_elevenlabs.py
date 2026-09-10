"""Run inside the derived NarratoAI image; no live API calls or config writes."""
import base64
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.config import config
from app.services import voice
from app.services import narrato_elevenlabs as eleven


def check():
    with TemporaryDirectory() as directory, patch.dict(config.app, {
        "elevenlabs_api_key": "test-only-key", "elevenlabs_model_id": "eleven_multilingual_v2",
    }):
        audio = Path(directory) / "sample.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", "3", "-b:a", "128k", str(audio)], check=True)
        text = "指尖旋转，彩色发光。"
        payload = {"audio_base64": base64.b64encode(audio.read_bytes()).decode(), "alignment": {
            "characters": list(text), "character_start_times_seconds": [i * .2 for i in range(len(text))],
            "character_end_times_seconds": [(i + 1) * .2 for i in range(len(text))],
        }}
        response = Mock(status_code=200)
        response.json.return_value = payload
        script = [{"_id": 1, "OST": 2, "timestamp": "00:00:00,000-00:00:03,000", "narration": text}]
        with patch.object(eleven.requests, "post", return_value=response) as post, \
                patch.object(voice.utils, "task_dir", return_value=directory):
            results = voice.tts_multiple("test", script, "testVoice", 1, 1, "elevenlabs")
            assert len(results) == 1 and results[0]["duration"] > 0
            assert Path(results[0]["audio_file"]).read_bytes() == audio.read_bytes()
            subtitles = Path(results[0]["subtitle_file"]).read_text()
            assert "指尖旋转" in subtitles and "彩色发光" in subtitles and "-->" in subtitles
            assert post.call_args.kwargs["headers"]["xi-api-key"] == "test-only-key"
            assert post.call_args.args[0].endswith("/testVoice/with-timestamps")
            assert post.call_args.kwargs["json"]["model_id"] == "eleven_multilingual_v2"
            response.status_code = 401
            assert voice.tts_multiple("test", script, "testVoice", 1, 1, "elevenlabs") == []
            assert script[0]["OST"] == 1
            config.app["elevenlabs_api_key"] = ""
            post.reset_mock()
            assert eleven.synthesize(text, "testVoice", str(audio)) is None
            post.assert_not_called()
            config.app["elevenlabs_api_key"] = "test-only-key"
            response.status_code = 200
            payload["alignment"]["character_end_times_seconds"] = []
            assert eleven.synthesize(text, "testVoice", str(Path(directory) / "bad.mp3")) is None
            assert not (Path(directory) / "bad.mp3").exists()

    from streamlit.testing.v1 import AppTest
    with patch.dict(config.app), patch.dict(config.ui):
        app = AppTest.from_string("from app.services.narrato_elevenlabs import render_settings\nrender_settings(lambda x: x)").run()
        assert not app.exception
        assert [field.label for field in app.text_input] == ["ElevenLabs API Key", "ElevenLabs Voice ID", "ElevenLabs 模型"]
        app.text_input[1].input("chosenVoice").run()
        assert config.ui["voice_name"] == "chosenVoice"
        assert app.session_state["voice_rate"] == 1.0
    print("PASS: ElevenLabs dispatch, MP3, timed subtitles, failure fallback, settings UI")


if __name__ == "__main__":
    check()
