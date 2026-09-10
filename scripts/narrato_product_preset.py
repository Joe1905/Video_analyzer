"""Apply the product preset to the pinned NarratoAI trial image at build time."""
from pathlib import Path
import ast


def patch(source):
    replacements = [
        ('        tr("Generation Prompt"),',
         '        "商品卖点" if product_preset else tr("Generation Prompt"),'),
        ('        tr("Auto Generate"): MODE_AUTO,',
         '        tr("Auto Generate"): MODE_AUTO,\n        "商品展示": MODE_AUTO,'),
        ("    video_theme = st.text_input(tr(\"Video Theme\"))\n    custom_prompt = st.text_area(",
         '    product_preset = st.session_state.get("script_mode_selection") == "商品展示"\n'
         '    video_theme = st.text_input(tr("Video Theme"))\n'
         '    if product_preset:\n'
         '        from app.services.narrato_multi_material import SCRIPT_LANGUAGES\n'
         '        st.selectbox("脚本语言", SCRIPT_LANGUAGES, key="product_script_language", help="生成的解说文案、字幕和配音使用此语言；商品描述和卖点仍可用中文填写。")\n'
         '        st.text_input("商品描述（必填）", key="product_description", placeholder="例如：指尖旋转发光玩具，可组合成光剑，用手指支撑中心旋转把玩。")\n'
         '    custom_prompt = st.text_area('),
        ("        value=st.session_state.get('video_plot', ''),\n        help=tr(\"Custom prompt for LLM, leave empty to use default prompt\"),",
         '        value="" if product_preset else st.session_state.get("video_plot", ""),\n'
         '        key="product_display_extra" if product_preset else "frame_analysis_prompt",\n'
         '        placeholder="例如：指尖旋转、解压、发光、光剑、炫酷" if product_preset else "",\n'
         '        help="填写想突出的特点或体验词，可用逗号或换行分隔。模型会结合画面证据选择镜头；抽象词用于表达方向，不作为已证实的功效。" if product_preset else tr("Custom prompt for LLM, leave empty to use default prompt"),'),
    ]
    for before, after in replacements:
        if source.count(before) != 1:
            raise ValueError("Pinned NarratoAI source changed; review preset patch")
        source = source.replace(before, after, 1)
    compile(source, "script_settings.py", "exec")
    return source


def check(source):
    from streamlit.testing.v1 import AppTest

    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "render_video_details")
    app = AppTest.from_string(
        "import streamlit as st\nfrom types import SimpleNamespace\n"
        "config = SimpleNamespace(frames={})\n"
        + ast.get_source_segment(source, function)
        + "\nrender_video_details(lambda text: text)\n"
    )
    app.session_state["script_mode_selection"] = "商品展示"
    app.run()
    assert not app.exception
    assert app.session_state["custom_prompt"] == ""
    assert app.text_area[0].label == "商品卖点"
    assert app.text_input[1].label == "商品描述（必填）"
    assert app.selectbox[0].value == "English"
    app.selectbox[0].select("Español").run()
    assert app.session_state["product_script_language"] == "Español"
    app.text_input[1].input("指尖旋转发光玩具").run()
    assert app.session_state["product_description"] == "指尖旋转发光玩具"
    app.text_area[0].input("指尖旋转、解压、发光、炫酷").run()
    assert app.session_state["custom_prompt"] == "指尖旋转、解压、发光、炫酷"
    app.session_state["script_mode_selection"] = "逐帧分析"
    app.run()
    assert not app.exception
    assert app.text_area[0].label == "Generation Prompt"
    assert len(app.text_input) == 1
    assert len(app.selectbox) == 0
    assert app.session_state["custom_prompt"] == ""


if __name__ == "__main__":
    path = Path("/NarratoAI/webui/components/script_settings.py")
    source = patch(path.read_text(encoding="utf-8"))
    check(source)
    path.write_text(source, encoding="utf-8")
    # Shared by video generation and Jianying export; retain clips when TTS is absent.
    service = Path("/NarratoAI/app/services/voice.py")
    source = service.read_text(encoding="utf-8")
    before = '''            if sub_maker is None:
                logger.error(f"无法为时间戳 {timestamp} 生成音频; "
                             f"如果您在中国，请使用VPN; "
                             f"或者使用其他 tts 引擎")
                continue'''
    after = '''            if sub_maker is None:
                item["OST"] = 1
                logger.warning(f"片段 {item['_id']} 配音不可用，跳过配音并保留原声")
                continue'''
    assert source.count(before) == 1, "Pinned TTS failure handling changed"
    source = source.replace(before, after, 1)
    compile(source, str(service), "exec")
    service.write_text(source, encoding="utf-8")
    generator = Path("/NarratoAI/webui/tools/generate_script_docu.py")
    source = generator.read_text(encoding="utf-8")
    before = "from app.services.documentary.frame_analysis_service import DocumentaryFrameAnalysisService"
    assert source.count(before) == 1
    source = source.replace(before, "from app.services.narrato_multi_material import MultiMaterialAnalysisService as DocumentaryFrameAnalysisService")
    before = "    progress_bar = st.progress(0)"
    assert source.count(before) == 1
    source = source.replace(before, '    if st.session_state.get("script_mode_selection") == "商品展示" and not st.session_state.get("product_description", "").strip():\n        st.error("请先填写商品描述，说明商品是什么及基本玩法")\n        return\n' + before)
    before = "                    video_path=params.video_origin_path,"
    assert source.count(before) == 1
    source = source.replace(before, before + '\n                    video_paths=st.session_state.get("video_origin_paths"),\n                    script_language=st.session_state.get("product_script_language", "English"),\n                    product_description=st.session_state.get("product_description", ""),\n                    product_mode=st.session_state.get("script_mode_selection") == "商品展示",')
    compile(source, str(generator), "exec")
    generator.write_text(source, encoding="utf-8")
    patches = {
        "/NarratoAI/webui.py": [
            ('        params = VideoClipParams(**all_params)',
             '        if st.session_state.get("script_mode_selection") == "商品展示":\n            all_params["original_volume"] = 0.0\n        params = VideoClipParams(**all_params)'),
        ],
        "/NarratoAI/app/services/task.py": [
            ('    if has_original_audio_segments:',
             '    if getattr(params, "original_volume", None) == 0:\n        final_original_volume = 0.0\n    elif has_original_audio_segments:'),
            ('    original_subtitle_paths = _get_original_subtitle_paths(params)',
             '    if list_script and all(item.get("subtitle") and path.isfile(item["subtitle"]) for item in list_script):\n        return subtitle_merger.merge_subtitle_files(list_script, path.join(utils.task_dir(task_id), "tts_subtitles.srt"))\n    original_subtitle_paths = _get_original_subtitle_paths(params)'),
        ],
        "/NarratoAI/app/services/generate_video.py": [
            ('            f"Fontname={font_family}",',
             '            f"PlayResX={video_width}",\n            f"PlayResY={video_height}",\n            f"Fontname={font_family}",'),
        ],
        "/NarratoAI/app/services/subtitle_merger.py": [
            ("map(int, start_time_str.split(':'))", "map(float, start_time_str.replace(',', '.').split(':'))"),
            ("map(int, end_time_str.split(':'))", "map(float, end_time_str.replace(',', '.').split(':'))"),
        ],
    }
    for filename, replacements in patches.items():
        path = Path(filename)
        source = path.read_text(encoding="utf-8")
        for before, after in replacements:
            count = 2 if before == '    if has_original_audio_segments:' else 1
            assert source.count(before) == count, f"Pinned source changed: {filename}: {before}"
            source = source.replace(before, after, count)
        compile(source, filename, "exec")
        path.write_text(source, encoding="utf-8")
