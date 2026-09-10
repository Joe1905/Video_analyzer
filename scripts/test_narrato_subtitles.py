"""Verify real libass rendering and preservation of timed English subtitles."""
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import io
from PIL import Image
from app.services import generate_video as g, subtitle_merger, voice, script_subtitle
from app.models.schema import VideoClipParams
from streamlit.testing.v1 import AppTest
import ast

source = Path('/NarratoAI/webui/components/subtitle_settings.py').read_text(encoding='utf-8')
node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'render_font_settings')
app = AppTest.from_string('import streamlit as st\nimport os\nfrom types import SimpleNamespace\n'
    '__file__ = "/NarratoAI/webui/components/subtitle_settings.py"\n'
    'config = SimpleNamespace(ui={})\nget_fonts_cache = lambda _: ["test.otf"]\n'
    + ast.get_source_segment(source, node) + '\nrender_font_settings(lambda text: text)')
app.run()
assert not app.exception and app.checkbox[0].label == '自动换行' and app.checkbox[0].value
app.checkbox[0].uncheck().run()
assert not app.exception and app.session_state['subtitle_auto_wrap'] is False
assert VideoClipParams().subtitle_auto_wrap is True

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
    for sentence in ('Hold the center, give it a turn and watch the colors draw a bright ring around your hand.',
                     '握住中心，轻轻旋转，让彩色光轨在你的手中绽放。'):
        assert script_subtitle.split_narration(sentence, max_chars=0) == [sentence]
        maker = voice.new_sub_maker()
        for i, char in enumerate(sentence):
            voice.add_subtitle_event(maker, i * 1000000, (i + 1) * 1000000, char)
        voice.create_subtitle(maker, sentence, str(srt), subtitle_auto_wrap=True)
        assert srt.read_text().count('-->') == 1 and sentence in srt.read_text()
        for enabled in (True, False):
            vf = g._build_subtitle_filter(str(srt), './resource/fonts/SourceHanSansCN-Regular.otf',
                'SourceHanSansCN-Regular.otf', 60, '#FFFFFF', '#000000', 1, 540, 960, 'bottom', 70, 82,
                subtitle_auto_wrap=enabled)
            png = subprocess.check_output([g._get_ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i',
                'color=black:s=540x960:r=1', '-vf', vf, '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
            frame = Image.open(io.BytesIO(png)).convert('L').point(lambda value: 255 if value > 200 else 0)
            bands = []
            for y in range(frame.height):
                if frame.crop((0, y, frame.width, y + 1)).getbbox():
                    if not bands or y > bands[-1][-1] + 1:
                        bands.append([])
                    bands[-1].append(y)
            assert len(bands) >= 2 if enabled else len(bands) == 1, (enabled, bands)
            if enabled:
                for band in bands:
                    box = frame.crop((0, band[0], frame.width, band[-1] + 1)).getbbox()
                    assert abs((box[0] + box[2]) / 2 - frame.width / 2) < 12, box
                    assert box[0] > 15 and box[2] < frame.width - 15, box
        voice.create_subtitle(maker, sentence, str(srt), subtitle_auto_wrap=False)
        assert srt.read_text().count('-->') > 1
print('PASS: checkbox defaults on; complete timed sentences; real FFmpeg wraps and centers Chinese/English lines')
