"""Run inside the pinned NarratoAI image; no model or TTS calls."""
import json
import subprocess
import uuid
from pathlib import Path

from app.models.schema import VideoClipParams
from app.services import task, voice


def main():
    job = "no-tts-smoke-" + uuid.uuid4().hex[:8]
    root = Path("/NarratoAI/storage") / job
    root.mkdir(parents=True)
    source = root / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=360x640:rate=25", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=44100", "-t", "4",
        "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(source),
    ], check=True, timeout=60)
    script = root / "script.json"
    script.write_text(json.dumps([
        {"_id": 1, "timestamp": "00:00:00,000-00:00:01,000", "narration": "", "OST": 1},
        {"_id": 2, "timestamp": "00:00:02,000-00:00:03,000", "narration": "", "OST": 1},
    ]), encoding="utf-8")

    def forbidden_tts(*args, **kwargs):
        raise AssertionError("No-voiceover workflow called TTS")

    voice.tts = forbidden_tts
    params = VideoClipParams(video_origin_path=str(source), video_clip_json_path=str(script),
                             subtitle_enabled=False, bgm_type="", bgm_name="", n_threads=2)
    task.start_subclip_unified(job, params)
    output = Path("/NarratoAI/storage/tasks") / job / "combined.mp4"
    info = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(output)
    ], timeout=30))
    assert 1.8 <= float(info["format"]["duration"]) <= 2.3, info["format"]
    assert {"video", "audio"} <= {s["codec_type"] for s in info["streams"]}
    print("PASS: original no-TTS pipeline, two clips, original audio:", output)

    # A selected but unavailable TTS engine must preserve both kinds of voiced clips.
    voice.tts = lambda **kwargs: None
    rows = json.loads(script.read_text(encoding="utf-8"))
    rows[0]["OST"], rows[1]["OST"] = 0, 2
    script.write_text(json.dumps(rows), encoding="utf-8")
    task.start_subclip_unified(job + "-fallback", params)
    output = Path("/NarratoAI/storage/tasks") / (job + "-fallback") / "combined.mp4"
    info = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(output)
    ], timeout=30))
    assert 1.8 <= float(info["format"]["duration"]) <= 2.3
    assert {"video", "audio"} <= {s["codec_type"] for s in info["streams"]}
    assert [row["OST"] for row in json.loads(script.read_text())] == [0, 2]
    print("PASS: unavailable TTS falls back to original audio without rewriting saved script:", output)


if __name__ == "__main__":
    main()
