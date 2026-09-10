"""Run in a disposable trial container; no external synthesis requests."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import subprocess
import uuid
from app.config import config
from app.services import voice

with TemporaryDirectory() as directory:
    sample = Path(directory) / "sample.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "2", str(sample)], check=True)
    def synthesize(text, voice_name, rate, pitch, output, engine):
        Path(output).write_bytes(sample.read_bytes())
        result = voice.new_sub_maker()
        voice.add_subtitle_event(result, 0, 20000000, text)
        return result
    text = "Cached speech " + uuid.uuid4().hex
    script = [{"_id": 1, "OST": 2, "timestamp": "00:00:00,000-00:00:02,000", "narration": text}]
    with patch.object(voice, "tts", side_effect=synthesize) as tts, patch.dict(config.ui):
        outputs = []
        for job in range(2):
            target = Path(directory) / str(job)
            target.mkdir()
            with patch.object(voice.utils, "task_dir", return_value=str(target)):
                config.ui["bgm_volume"] = job * .2
                outputs.append(voice.tts_multiple(str(job), script, "voiceA", 1, 1, "edge_tts"))
        assert tts.call_count == 1, "Unchanged speech synthesized twice"
        assert Path(outputs[0][0]["audio_file"]).read_bytes() == Path(outputs[1][0]["audio_file"]).read_bytes()
        assert Path(outputs[1][0]["subtitle_file"]).exists()
        with patch.object(voice.utils, "task_dir", return_value=directory):
            voice.tts_multiple("changed", script, "voiceB", 1, 1, "edge_tts")
            assert tts.call_count == 2
            script[0]["narration"] += " changed"
            voice.tts_multiple("changed", script, "voiceB", 1, 1, "edge_tts")
            assert tts.call_count == 3
            cache = Path("/NarratoAI/storage/tts_cache")
            for p in cache.glob("*/audio.mp3"):
                p.write_bytes(b"broken")
            voice.tts_multiple("corrupt", script, "voiceB", 1, 1, "edge_tts")
            assert tts.call_count == 4
print("PASS: cross-job reuse; subtitle recreation; voice/text invalidation; corrupt audio regeneration")
