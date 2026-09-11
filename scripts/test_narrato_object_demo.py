"""Isolated tests; --live uses DeepSeek on labelled synthetic fixtures, never user originals."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch, AsyncMock

from PIL import Image, ImageDraw
from streamlit.testing.v1 import AppTest
from app.services import narrato_object_demo as d


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    for name, shape in [('cup', 'cup'), ('bottle', 'bottle'), ('both', 'both'), ('empty', '')]:
        image = Image.new('RGB', (640, 480), '#dddddd')
        draw = ImageDraw.Draw(image)
        if shape in ('cup', 'both'):
            draw.rounded_rectangle((70, 150, 220, 330), radius=15, fill='red')
            draw.ellipse((190, 190, 265, 285), outline='red', width=15)
            draw.ellipse((70, 137, 220, 170), fill='#770000')
        if shape in ('bottle', 'both'):
            draw.rounded_rectangle((380, 155, 475, 350), radius=20, fill='blue')
            draw.rectangle((403, 105, 452, 170), fill='blue')
            draw.rectangle((400, 90, 455, 113), fill='#111177')
        image.save(root / f'{name}.jpg')
    sources = []
    for i, sequence in enumerate([['empty', 'cup', 'both', 'bottle', 'empty'], ['empty', 'bottle', 'empty']]):
        directory = root / f'images{i}'
        directory.mkdir(exist_ok=True)
        for j, name in enumerate(sequence):
            (directory / f'{j:03d}.jpg').write_bytes((root / f'{name}.jpg').read_bytes())
        video = root / f'source{i}.mp4'
        subprocess.run([d._get_ffmpeg_binary(), '-v', 'error', '-y', '-framerate', '1', '-i', str(directory / '%03d.jpg'),
            '-c:v', 'libx264', '-r', '25', '-pix_fmt', 'yuv420p', str(video)], check=True)
        sources.append({'id': f'v{i+1}', 'name': video.name, 'path': str(video), 'duration': float(len(sequence))})
    products = [{'id': 'p1', 'name': '红色杯子', 'description': '红色圆柱杯，有右侧环形把手。', 'references': [str(root/'cup.jpg')]},
                {'id': 'p2', 'name': '蓝色瓶子', 'description': '蓝色长瓶身、细瓶颈，深蓝色瓶盖。', 'references': [str(root/'bottle.jpg')]}]
    manifest = {'status': 'pending', 'products': products, 'sources': sources, 'step': 0.5, 'clips': []}
    d.save(root / 'manifest.json', manifest)
    return manifest


def checks(root):
    manifest = fixture(root)
    frames = d.coarse_frames(manifest['sources'][0], .5, root/'samples')
    assert len(frames) >= 10 and frames[0]['time'] == 0 and frames[-1]['time'] > 4.9
    def objects(a, b='absent'):
        return [{'id': 'p1', 'status': a, 'reason': 'test'}, {'id': 'p2', 'status': b, 'reason': 'test'}]
    points = [dict(f, objects=objects('present' if 1 <= f['time'] < 3 else 'absent', 'present' if 2 <= f['time'] < 4 else 'absent')) for f in frames]
    result = {'frames': [{'frame_id': p['id'], 'objects': p['objects']} for p in points]}
    assert len(d.validate_result(json.dumps(result), frames, manifest['products'])) == len(frames)
    try:
        d.validate_result(json.dumps({'frames': result['frames'][:-1]}), frames, manifest['products'])
        raise AssertionError('Incomplete response accepted')
    except ValueError:
        pass
    refined = d.refinement_times(points, .5, 5)
    assert any(.8 < t < 1.04 for t in refined) and any(3.7 < t < 4.16 for t in refined)
    clips = d.intervals(points, manifest['products'], 5, 'v1')
    assert len(clips) == 2 and clips[0]['end'] > clips[1]['start'], clips
    uncertain = [dict(points[2], objects=objects('present')), dict(points[3], objects=objects('uncertain')), dict(points[4], objects=objects('present'))]
    assert len(d.intervals(uncertain, manifest['products'], 5, 'v1')) == 3
    manifest.update(status='review', clips=clips)
    d.save(root/'manifest.json', manifest)
    archive = d.export(root, [0, 1])
    assert archive.is_file()
    with d.zipfile.ZipFile(archive) as file:
        assert any(n.startswith('p1_') for n in file.namelist()) and any(n.startswith('p2_') for n in file.namelist())
    for file in archive.with_suffix('').glob('*/*.mp4'):
        assert d._probe_video(str(file))['width'] == 640
    app = AppTest.from_string('from app.services.narrato_object_demo import render\nrender()')
    with patch.object(d, 'ROOT', root/'no-jobs'):
        app.run()
        assert not app.exception
        app.button[0].click().run()
        assert not app.exception and app.error
    # Confirm references and every target frame share one request, and truncation stops the task.
    class Response:
        choices = [type('Choice', (), {'finish_reason': 'length', 'message': type('Message', (), {'content': '{}'})()})()]
        usage = None
    provider = d.OpenAICompatibleVisionProvider(api_key='test', model_name='deepseek-test-vision', base_url='https://api.deepseek.com/v1')
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create.return_value = Response()
    with patch.object(provider, '_build_client', return_value=client):
        try:
            asyncio.run(d.identify(provider, frames[:2], manifest['products'], root/'truncated.json'))
            raise AssertionError('Truncated response accepted')
        except ValueError:
            pass
    content = client.chat.completions.create.call_args.kwargs['messages'][0]['content']
    assert len(content) == 5 and 'reference_image_numbers' in content[0]['text']
    assert json.loads((root/'truncated.json').read_text())['status'] == 'failed'
    print('PASS: multiple targets, interval gaps/overlap, schema rejection, export, UI and reference payload')


if __name__ == '__main__':
    if '--live-existing' in sys.argv:
        root = d.ROOT / ('real-smoke-' + d.uuid.uuid4().hex[:12])
        manifest = fixture(root)
        resource = Path('/NarratoAI/resource/videos')
        ref = root/'toy.jpg'
        d.frame_at(str(resource/'2026-09-07_224115_20260910161454.MOV'), 1, ref)
        with Image.open(ref) as img:
            img.crop((0, round(img.height*.17), img.width, round(img.height*.73))).save(root/'toy-crop.jpg')
        sources = []
        for i, (filename, start) in enumerate([('2026-09-08_221416_20260910161454.MOV', 5), ('2026-08-20_223002_20260910161454.MOV', 1)]):
            dest = root / f'real-{i}.mp4'
            subprocess.run([d._get_ffmpeg_binary(), '-v', 'error', '-y', '-ss', str(start), '-i', str(resource/filename),
                '-t', '2', '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(dest)], check=True)
            sources.append({'id': f'v{i+1}', 'name': filename, 'path': str(dest), 'duration': 2.0})
        sources.append(dict(manifest['sources'][1], id='v3', name='无目标对照素材'))
        manifest.update(sources=sources, products=[{'id': 'p1', 'name': '组合发光光剑',
            'description': '彩色透明发光剑身，有圆形中心连接结构，可多把互相组合旋转。不同颜色、两把或多把组合均算同一商品；只看到类似光轨而看不清商品结构时不能确认。',
            'references': [str(root/'toy-crop.jpg')]}])
        d.save(root/'manifest.json', manifest)
        result = asyncio.run(d.analyze(root, lambda text: print(text, flush=True)))
        d.export(root, [i for i,c in enumerate(result['clips']) if c['status'] == 'present'])
        print('REAL_RESULT', root, json.dumps(result['clips'], ensure_ascii=False), flush=True)
    elif '--live' in sys.argv:
        root = d.ROOT / ('smoke-' + d.uuid.uuid4().hex[:12])
        fixture(root)
        result = asyncio.run(d.analyze(root, lambda text: print(text, flush=True)))
        d.export(root, [i for i,c in enumerate(result['clips']) if c['status'] == 'present'])
        print('LIVE_RESULT', root, json.dumps(result['clips'], ensure_ascii=False), flush=True)
    else:
        with TemporaryDirectory() as directory:
            checks(Path(directory))
