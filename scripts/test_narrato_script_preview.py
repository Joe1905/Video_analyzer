"""Real interval extraction, source mapping and click-to-load UI checks; no external APIs."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from app.services import narrato_script_preview as p

with TemporaryDirectory() as directory:
    root = Path(directory)
    paths = [str(root / (name + '.mp4')) for name in ('red', 'green')]
    for path, color in zip(paths, ('red', 'green')):
        subprocess.run([p._get_ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i',
            f'color={color}:s=160x120:r=25', '-t', '3', '-c:v', 'libx264', path], check=True)
    row = {'video_id': 2, 'video_name': 'green.mp4', 'timestamp': '00:00:00,500-00:00:01,300', 'narration': 'Preview'}
    source, start, end = p.resolve_preview(row, paths)
    assert source == Path(paths[1]) and (start, end) == (0.5, 1.3)
    clip = p.create_preview(source, start, end, root / 'previews')
    assert abs(p._probe_video(str(clip))['duration'] - 0.8) < 0.08
    pixel = subprocess.check_output([p._get_ffmpeg_binary(), '-v', 'error', '-i', str(clip),
        '-vf', 'scale=1:1', '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
    assert pixel[1] > pixel[0] + 30 and pixel[1] > pixel[2] + 30, pixel
    with patch.object(p.subprocess, 'run', side_effect=AssertionError('cache should avoid re-encoding')):
        assert p.create_preview(source, start, end, root / 'previews') == clip
    for bad in ('00:00:02,000-00:00:01,000', '00:00:00,000-00:00:20,000'):
        try:
            s, a, b = p.resolve_preview(dict(row, timestamp=bad), paths)
            p.create_preview(s, a, b, root / 'previews')
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid interval accepted')
    app = AppTest.from_string('from app.services.narrato_script_preview import render_preview\n'
        + f'render_preview({[row, dict(row, timestamp="00:00:01,000-00:00:02,000")]!r}, {paths!r})')
    with patch('streamlit.video') as video, patch.object(p, 'create_preview', return_value=clip) as create:
        app.run()
        assert not app.exception and not video.called and not create.called
        app.button[0].click().run()
        assert not app.exception and video.called and create.call_count == 1
        video.reset_mock()
        app.selectbox[0].select(1).run()
        assert not app.exception and not video.called, 'Changed interval displayed stale preview'
print('PASS: correct material and millisecond interval, cached MP4, invalid range rejection, explicit click and stale preview clearing')
