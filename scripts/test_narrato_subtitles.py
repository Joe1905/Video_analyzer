"""Verify real libass rendering and preservation of timed English subtitles."""
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import io
from PIL import Image
from app.services import generate_video as g, subtitle_merger

with TemporaryDirectory() as directory:
    p = Path(directory)
    srt = p / "speech.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nFour glowing blades combine.\n", encoding="utf-8")
    merged = subtitle_merger.merge_subtitle_files([
        {"_id": 1, "subtitle": str(srt), "editedTimeRange": "00:00:00,000-00:00:02,000"},
        {"_id": 2, "subtitle": str(srt), "editedTimeRange": "00:00:02,500-00:00:04,500"},
    ], str(p / "merged.srt"))
    text = Path(merged).read_text()
    assert "00:00:02,500 --> 00:00:04,500" in text and "Four glowing blades combine." in text
    vf = g._build_subtitle_filter(str(srt), "./resource/fonts/SourceHanSansCN-Regular.otf",
        "SourceHanSansCN-Regular.otf", 60, "#FFFFFF", "#000000", 1, 1080, 1920, "bottom", 70, 82)
    png = subprocess.check_output([g._get_ffmpeg_binary(), "-v", "error", "-f", "lavfi", "-i",
        "color=black:s=1080x1920:r=1", "-vf", vf, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"])
    frame = Image.open(io.BytesIO(png)).convert("L")
    assert frame.crop((0, 1200, 1080, 1900)).getextrema()[1] > 200, "Subtitles invisible/off-screen"
print("PASS: real FFmpeg subtitles visible; English text and fractional timing preserved")
