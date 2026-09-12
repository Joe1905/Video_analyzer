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
        response = Mock(status_code=200, headers={})
        response.json.return_value = payload
        script = [{"_id": 1, "OST": 2, "timestamp": "00:00:00,000-00:00:03,000", "narration": text}]
        import uuid
        test_voice = "testVoice_" + uuid.uuid4().hex[:8]
        with patch.object(eleven.requests, "post", return_value=response) as post, \
                patch.object(voice.utils, "task_dir", return_value=directory):
            results = voice.tts_multiple("test", script, test_voice, 1, 1, "elevenlabs")
            assert len(results) == 1 and results[0]["duration"] > 0
            assert Path(results[0]["audio_file"]).read_bytes() == audio.read_bytes()
            subtitles = Path(results[0]["subtitle_file"]).read_text()
            assert "指尖旋转" in subtitles and "彩色发光" in subtitles and "-->" in subtitles
            assert post.call_args.kwargs["headers"]["xi-api-key"] == "test-only-key"
            assert post.call_args.args[0].endswith(f"/{test_voice}/with-timestamps")
            assert post.call_args.kwargs["json"]["model_id"] == "eleven_multilingual_v2"
            response.status_code = 401
            assert voice.tts_multiple("test", script, "failedVoice", 1, 1, "elevenlabs") == []
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

        response.json.return_value = {"preview_url": "https://example.com/preview.mp3"}
        with patch.object(eleven.requests, "get", return_value=response) as get, \
                patch.object(eleven.requests, "post") as post:
            assert eleven.get_preview("testVoice", "test-only-key") == "https://example.com/preview.mp3"
            get.assert_called_once()
            post.assert_not_called()
            response.json.return_value = {"preview_url": None}
            assert eleven.get_preview("testVoice", "test-only-key") is None
        response.status_code = 400
        response.json.return_value = {"detail": {"status": "voice_not_found", "message": "test-only-key"}}
        error = eleven.response_error(response, "test", "test-only-key")
        assert "voice_not_found" in error and "400" in error and "test-only-key" not in error
        response.json.side_effect = None
        response.status_code = 200
        response.json.return_value = {"voices": [
            {"voice_id": "v1", "name": "Voice One", "preview_url": "https://example.com/v1.mp3", "labels": {"gender": "male"}},
            {"voice_id": "v2", "name": "Voice Two", "preview_url": "https://example.com/v2.mp3", "labels": {"gender": "female"}},
        ]}
        with patch.object(eleven.requests, "get", return_value=response) as get:
            voices = eleven.get_voices("test-only-key", force_refresh=True)
            assert len(voices) == 2 and voices[0]["voice_id"] == "v1" and voices[0]["name"] == "Voice One"
            assert voices[0]["preview_url"] == "https://example.com/v1.mp3"
            # Caching check
            voices_cached = eleven.get_voices("test-only-key")
            assert len(voices_cached) == 2
            assert get.call_count == 1
            # Empty key check
            assert eleven.get_voices("") == []

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
