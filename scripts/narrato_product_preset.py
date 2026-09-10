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
    app.text_area[0].input("指尖旋转、解压、发光、炫酷").run()
    assert app.session_state["custom_prompt"] == "指尖旋转、解压、发光、炫酷"
    app.session_state["script_mode_selection"] = "逐帧分析"
    app.run()
    assert not app.exception
    assert app.text_area[0].label == "Generation Prompt"
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
    before = "                    video_path=params.video_origin_path,"
    assert source.count(before) == 1
    source = source.replace(before, before + '\n                    video_paths=st.session_state.get("video_origin_paths"),\n                    product_mode=st.session_state.get("script_mode_selection") == "商品展示",')
    compile(source, str(generator), "exec")
    generator.write_text(source, encoding="utf-8")
