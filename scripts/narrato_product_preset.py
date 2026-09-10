"""Apply the product preset to the pinned NarratoAI trial image at build time."""
from pathlib import Path
import ast


PRODUCT_PROMPT = """创作一段真实、自然的商品展示视频脚本。
逐帧关注商品整体外观、颜色、可见细节、实际操作及使用场景；不确定的信息明确保留，不靠猜测补全。
优先选用能看清商品的画面，按素材支持的顺序组织：整体展示、细节特写、使用演示、整体回顾；缺少的环节直接略过。
文案使用简短自然的中文，以商品本身的可见特点吸引注意，不使用夸张悬念、剧情反转或“你绝对想不到”等套话。
不虚构品牌、材质、尺寸、价格、销量、功效、认证或优惠，不把外观推测写成事实。
画面描述和时间戳必须来自素材分析，保持原有 JSON 输出格式。"""


def patch(source):
    replacements = [
        ('        tr("Auto Generate"): MODE_AUTO,',
         '        tr("Auto Generate"): MODE_AUTO,\n        "商品展示": MODE_AUTO,'),
        ("    video_theme = st.text_input(tr(\"Video Theme\"))\n    custom_prompt = st.text_area(",
         '    product_preset = st.session_state.get("script_mode_selection") == "商品展示"\n'
         '    video_theme = st.text_input(tr("Video Theme"))\n'
         '    if product_preset:\n'
         '        st.caption("商品展示预设：复用逐帧分析，以下提示词可编辑；音频设置仍需在脚本中选择。")\n'
         '    custom_prompt = st.text_area('),
        ("        value=st.session_state.get('video_plot', ''),\n        help=tr(\"Custom prompt for LLM, leave empty to use default prompt\"),",
         f'        value={PRODUCT_PROMPT!r} if product_preset else st.session_state.get("video_plot", ""),\n'
         '        key="product_display_prompt" if product_preset else "frame_analysis_prompt",\n'
         '        help=tr("Custom prompt for LLM, leave empty to use default prompt"),'),
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
    assert app.session_state["custom_prompt"] == PRODUCT_PROMPT
    app.text_area[0].input("突出上包效果").run()
    assert app.session_state["custom_prompt"] == "突出上包效果"
    app.session_state["script_mode_selection"] = "逐帧分析"
    app.run()
    assert not app.exception
    assert app.session_state["custom_prompt"] == ""


if __name__ == "__main__":
    path = Path("/NarratoAI/webui/components/script_settings.py")
    source = patch(path.read_text(encoding="utf-8"))
    check(source)
    path.write_text(source, encoding="utf-8")
    # This trial deliberately has no TTS service; generated clips retain source audio.
    service = Path("/NarratoAI/app/services/documentary/frame_analysis_service.py")
    source = service.read_text(encoding="utf-8")
    before = 'final_script = [{**item, "OST": 2} for item in narration_items]'
    after = 'final_script = [{**item, "OST": 1} for item in narration_items]'
    assert source.count(before) == 1, "Pinned narration defaults changed"
    source = source.replace(before, after, 1)
    compile(source, str(service), "exec")
    service.write_text(source, encoding="utf-8")
