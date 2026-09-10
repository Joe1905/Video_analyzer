"""Browse durable NarratoAI outputs, including tasks created before this UI."""
from datetime import datetime
import json
from pathlib import Path


def task_files(directory):
    return sorted((p for p in directory.iterdir() if p.is_file() and not p.is_symlink()
                   and (p.name in {"combined.mp4", "combined_fixed.mp4"}
                        or p.suffix.lower() in {".srt", ".mp3", ".wav"})),
                  key=lambda p: (p.name != "combined_fixed.mp4", p.name != "combined.mp4", p.name))


def history(root):
    return sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and task_files(p)),
                  key=lambda p: max(f.stat().st_mtime for f in task_files(p)), reverse=True) if root.exists() else []


def render_history():
    import streamlit as st
    @st.dialog("历史记录", width="large")
    def open_history():
        render_browser()
    if st.button("历史记录", icon="🕘", key="open_narrato_history"):
        open_history()


def render_browser():
    import streamlit as st
    st.markdown('''<style>
    .st-key-history_view video {width:100%;max-height:48vh;object-fit:contain;background:#101820;border-radius:10px}
    .st-key-history_view [data-testid="stCodeBlock"], .st-key-history_view [data-testid="stJson"] {max-height:45vh;overflow:auto}
    .st-key-history_list button {text-align:left;justify-content:flex-start}
    </style>''', unsafe_allow_html=True)
    with st.container(key="history_view"):
        category = st.radio("内容", ["视频任务", "已保存脚本"], horizontal=True, key="history_category", label_visibility="collapsed")
        left, right = st.columns([1, 2], gap="large")
        if category == "视频任务":
            tasks = history(Path("/NarratoAI/storage/tasks"))
            if not tasks:
                st.info("暂无已保存的任务文件")
                return
            def label(directory):
                files = task_files(directory)
                date = datetime.fromtimestamp(max(p.stat().st_mtime for p in files)).strftime("%Y-%m-%d %H:%M")
                state = "有成片" if any(p.suffix == ".mp4" for p in files) else "仅配音 / 字幕"
                return f"{date} · {state}"
            with left:
                st.caption(f"{len(tasks)} 条记录 · 最新在前")
                st.button("刷新", key="refresh_narrato_history", use_container_width=True)
                if st.session_state.get("history_selected") not in [p.name for p in tasks]:
                    st.session_state["history_selected"] = tasks[0].name
                with st.container(height=390, key="history_list"):
                    for task in tasks:
                        active = task.name == st.session_state["history_selected"]
                        if st.button(label(task), key="history_pick_" + task.name,
                                     help="任务 " + task.name, type="primary" if active else "secondary",
                                     use_container_width=True):
                            st.session_state["history_selected"] = task.name
                            st.rerun(scope="fragment")
            selected = next(p for p in tasks if p.name == st.session_state["history_selected"])
            files = task_files(selected)
            with right:
                st.markdown(f"**{label(selected)}**")
                groups = {"成片": [p for p in files if p.suffix == ".mp4"],
                          "配音": [p for p in files if p.suffix in {".mp3", ".wav"}],
                          "字幕": [p for p in files if p.suffix == ".srt"]}
                kind = st.radio("文件类型", [k for k,v in groups.items() if v], horizontal=True,
                                key="history_kind_" + selected.name, label_visibility="collapsed")
                target = st.selectbox("版本 / 文件", groups[kind], format_func=lambda p: {
                    "combined_fixed.mp4": "修复版", "combined.mp4": "原版",
                    "merger_audio.mp3": "完整配音",
                }.get(p.name, p.name), key="history_asset_" + selected.name + kind)
                if target.suffix == ".mp4":
                    st.video(str(target))
                elif target.suffix in {".mp3", ".wav"}:
                    st.audio(str(target))
                else:
                    st.code(target.read_text(encoding="utf-8-sig"), language="text")
                download(target)
                with st.expander("任务详情"):
                    st.caption(f"任务编号：{selected.name}")
                    st.caption("展示已保存文件；旧任务没有完整的运行状态记录。")
        else:
            root = Path("/NarratoAI/resource/scripts")
            scripts = sorted((p for p in root.glob("*.json") if p.is_file() and not p.is_symlink()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            if not scripts:
                st.info("暂无已保存脚本；编辑后点击保存的脚本会出现在这里")
                return
            with left:
                st.caption(f"{len(scripts)} 份脚本 · 最新在前")
                target = st.selectbox("选择脚本", scripts, format_func=lambda p: p.name, key="history_script")
            with right:
                try:
                    st.json(json.loads(target.read_text(encoding="utf-8-sig")))
                except (ValueError, OSError):
                    st.warning("该脚本无法解析，可下载原文件检查")
                download(target)


def download(target):
    import streamlit as st
    with target.open("rb") as file:
        st.download_button("下载当前文件", file, file_name=target.name, key="history_download", use_container_width=True)


if __name__ == "__main__":
    path = Path("/NarratoAI/webui.py")
    source = path.read_text(encoding="utf-8")
    before = "    st.write(get_help_text())"
    assert source.count(before) == 1
    source = source.replace(before, before + "\n    from app.services.narrato_history import render_history\n    render_history()", 1)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
