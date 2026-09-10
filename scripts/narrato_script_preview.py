"""Preview a selected script interval without running analysis, TTS or final rendering."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from app.services.generate_video import _get_ffmpeg_binary, _probe_video
from app.services.script_subtitle import parse_time_range
from app.services.short_drama_narration_validation import normalize_script_video_sources


def resolve_preview(row, paths):
    row = normalize_script_video_sources([row], paths)[0]
    video_id = row.get('video_id')
    if not isinstance(video_id, int) or not 1 <= video_id <= len(paths):
        raise ValueError('请选择有效的素材编号或视频文件')
    source = Path(paths[video_id - 1]).resolve()
    if not source.is_file():
        raise ValueError('原素材不存在，请重新选择视频来源')
    start, end = parse_time_range(row.get('timestamp', ''))
    if not all(math.isfinite(t) for t in (start, end)) or start < 0:
        raise ValueError('请填写有效的起止时间')
    return source, start, end


def create_preview(source, start, end, root=Path('/NarratoAI/storage/temp/script-preview')):
    duration = float(_probe_video(str(source))['duration'])
    if start >= duration or end > duration + 0.05:
        raise ValueError(f'选中区间超出素材时长（{duration:.3f} 秒）')
    stat = source.stat()
    identity = json.dumps([str(source), stat.st_size, stat.st_mtime_ns, start, end])
    root.mkdir(parents=True, exist_ok=True)
    target = root / (hashlib.sha256(identity.encode()).hexdigest() + '.mp4')
    if target.is_file():
        return target
    with tempfile.NamedTemporaryFile(suffix='.mp4', dir=root, delete=False) as file:
        temporary = Path(file.name)
    try:
        subprocess.run([_get_ffmpeg_binary(), '-v', 'error', '-y', '-ss', str(start), '-i', str(source),
            '-t', str(min(end, duration) - start), '-map', '0:v:0', '-an',
            '-vf', 'scale=720:720:force_original_aspect_ratio=decrease:force_divisible_by=2',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-pix_fmt', 'yuv420p',
            '-threads', '2', '-movflags', '+faststart', str(temporary)],
            check=True, capture_output=True, timeout=180)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def render_preview(rows, paths):
    import streamlit as st
    if not rows:
        return
    st.markdown('**片段预览**')
    left, right = st.columns([1, 1])
    with left:
        index = st.selectbox('选择脚本片段', range(len(rows)),
            format_func=lambda i: f"第 {i + 1} 段 · {rows[i].get('timestamp', '')}")
        row = rows[index]
        st.caption(f"素材：{row.get('video_name') or row.get('video_id') or '未指定'}")
        st.write(row.get('narration', ''))
        try:
            source, start, end = resolve_preview(row, paths)
        except (ValueError, TypeError) as exc:
            st.warning(str(exc))
            return
        signature = (str(source), source.stat().st_mtime_ns, start, end)
        if st.button('预览片段', key='load_script_clip_preview', use_container_width=True):
            try:
                with st.spinner('正在准备片段预览…'):
                    target = create_preview(source, start, end)
                st.session_state['script_clip_preview'] = (signature, str(target))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                st.error(f'预览失败：{exc}')
        st.caption('按当前表格中的时间范围预览原画面；不含配音、BGM 和字幕，最终时长可能随配音调整。')
    with right:
        cached = st.session_state.get('script_clip_preview')
        if cached and cached[0] == signature and Path(cached[1]).is_file():
            with st.container(key='script_clip_player'):
                st.markdown('<style>.st-key-script_clip_player video{max-height:320px;object-fit:contain;background:#111}</style>', unsafe_allow_html=True)
                st.video(cached[1], muted=True)
        else:
            st.info('点击“预览片段”查看选中的画面。')


if __name__ == '__main__':
    path = Path('/NarratoAI/webui/components/script_settings.py')
    source = path.read_text(encoding='utf-8')
    before = '        video_clip_json_details = _script_table_to_json(edited_table)'
    assert source.count(before) == 1
    source = source.replace(before, before + '\n        from app.services.narrato_script_preview import render_preview\n        render_preview(json.loads(video_clip_json_details), _selected_video_paths())')
    before = '        video_script_dialog()'
    assert source.count(before) == 1
    source = source.replace(before, '        st.session_state.pop("script_clip_preview", None)\n' + before)
    compile(source, str(path), 'exec')
    path.write_text(source, encoding='utf-8')
