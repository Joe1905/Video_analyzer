"""Enable sentence-preserving, centered subtitle wrapping in the pinned trial."""
from pathlib import Path


def replace(filename, before, after, count=1):
    path = Path('/NarratoAI') / filename
    source = path.read_text(encoding='utf-8')
    assert source.count(before) == count, f'Pinned source changed: {filename}: {before}'
    source = source.replace(before, after)
    compile(source, str(path), 'exec')
    path.write_text(source, encoding='utf-8')


if __name__ == '__main__':
    replace('webui/components/subtitle_settings.py',
        "        st.session_state['font_size'] = font_size",
        "        st.session_state['font_size'] = font_size\n"
        '    config.ui["subtitle_auto_wrap"] = st.checkbox("自动换行",\n'
        '        value=config.ui.get("subtitle_auto_wrap", True), key="subtitle_auto_wrap",\n'
        '        help="保留完整句子，按画面宽度自动换行，每行居中；关闭后使用原分段方式，不自动折行。")')
    replace('webui/components/subtitle_settings.py',
        "        'font_size': st.session_state.get('font_size', 60),",
        "        'subtitle_auto_wrap': st.session_state.get('subtitle_auto_wrap', True),\n"
        "        'font_size': st.session_state.get('font_size', 60),")
    replace('app/models/schema.py', '    subtitle_enabled: bool = True',
        '    subtitle_enabled: bool = True\n    subtitle_auto_wrap: bool = True')
    replace('app/services/script_subtitle.py',
        '    max_chars = max(1, int(max_chars or DEFAULT_MAX_CHARS_PER_SUBTITLE))',
        '    if max_chars == 0:\n'
        '        # ponytail: punctuation-based sentences; use a tokenizer if abbreviations need finer handling.\n'
        '        return [part.strip() for part in re.split(r"(?<=[。！？!?])\\s*(?=[^。！？!?])|(?<=\\.)\\s+", text) if part.strip()]\n'
        '    max_chars = max(1, int(max_chars or DEFAULT_MAX_CHARS_PER_SUBTITLE))')
    replace('app/services/voice.py',
        'def create_subtitle(sub_maker: submaker.SubMaker, text: str, subtitle_file: str):',
        'def create_subtitle(sub_maker: submaker.SubMaker, text: str, subtitle_file: str, subtitle_auto_wrap: bool = True):')
    replace('app/services/voice.py', '    script_lines = utils.split_string_by_punctuations(text)',
        '    from app.services.script_subtitle import split_narration\n'
        '    script_lines = split_narration(text, max_chars=0) if subtitle_auto_wrap else utils.split_string_by_punctuations(text)')
    replace('app/services/voice.py', 'voice_pitch: float, tts_engine: str = "azure"):',
        'voice_pitch: float, tts_engine: str = "azure", subtitle_auto_wrap: bool = True):')
    replace('app/services/voice.py', 'create_subtitle(sub_maker=sub_maker, text=text, subtitle_file=subtitle_file)',
        'create_subtitle(sub_maker=sub_maker, text=text, subtitle_file=subtitle_file, subtitle_auto_wrap=subtitle_auto_wrap)')
    for filename, count in [('app/services/task.py', 2), ('app/services/jianying_task.py', 1)]:
        replace(filename, '        voice_pitch=params.voice_pitch,',
            '        voice_pitch=params.voice_pitch,\n        subtitle_auto_wrap=params.subtitle_auto_wrap,', count)
    replace('app/services/task.py', "        'subtitle_font_size': params.font_size,",
        "        'subtitle_auto_wrap': params.subtitle_auto_wrap,\n        'subtitle_font_size': params.font_size,", 3)
    replace('app/services/task.py', '    return script_subtitle.create_script_subtitle_file(\n',
        '    return script_subtitle.create_script_subtitle_file(\n'
        '        max_chars=0 if params.subtitle_auto_wrap else script_subtitle.DEFAULT_MAX_CHARS_PER_SUBTITLE,\n')
    filename = 'app/services/generate_video.py'
    replace(filename, '    orientation_subtitle_y_percent: Optional[float],\n) -> str:\n    font_family',
        '    orientation_subtitle_y_percent: Optional[float],\n    subtitle_auto_wrap: bool = True,\n) -> str:\n    font_family')
    replace(filename, '            f"Alignment={alignment}",',
        '            f"Alignment={alignment}",\n'
        '            f"WrapStyle={0 if subtitle_auto_wrap else 2}",\n'
        '            f"MarginL={round(video_width * 0.05)}",\n'
        '            f"MarginR={round(video_width * 0.05)}",')
    # libass centers every line; drawtext centers only its bounding box.
    replace(filename, '        if has_drawtext_filter:\n',
        '        if has_drawtext_filter and not options.get("subtitle_auto_wrap", True):\n')
    replace(filename, '                subtitle_font=subtitle_font,\n',
        '                subtitle_font=subtitle_font,\n                subtitle_auto_wrap=options.get("subtitle_auto_wrap", True),\n')
    replace(filename, '    video_width: int,\n) -> list[str]:',
        '    video_width: int,\n    subtitle_auto_wrap: bool = True,\n) -> list[str]:')
    replace(filename, '        if font_path:\n            wrapped_text, _ = wrap_text(',
        '        if font_path and subtitle_auto_wrap:\n            wrapped_text, _ = wrap_text(')
    replace(filename, '                video_width=video_width,\n            )\n            for index, drawtext_filter',
        '                video_width=video_width,\n                subtitle_auto_wrap=False,\n            )\n            for index, drawtext_filter')
    replace(filename, '    output_dir: str,\n) -> str:\n    font = ImageFont',
        '    output_dir: str,\n    subtitle_auto_wrap: bool = True,\n) -> str:\n    font = ImageFont')
    replace(filename, '        fontsize=subtitle_font_size,\n    )\n    stroke_width_px',
        '        fontsize=subtitle_font_size,\n    ) if subtitle_auto_wrap else (text, 0)\n    stroke_width_px')
    replace(filename, '                    output_dir=output_dir,\n                )\n                temp_files.append(png_path)',
        '                    output_dir=output_dir,\n                    subtitle_auto_wrap=options.get("subtitle_auto_wrap", True),\n                )\n                temp_files.append(png_path)')
    replace(filename, '        if font_path:\n            wrapped_txt, txt_height = wrap_text(',
        '        if font_path and options.get("subtitle_auto_wrap", True):\n            wrapped_txt, txt_height = wrap_text(')
    replace(filename, '                "text": wrapped_txt,',
        '                "text": wrapped_txt,\n                "text_align": "center",', 2)
