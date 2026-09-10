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
    with st.expander("历史记录 · 成片 / 配音 / 字幕 / 脚本"):
        # Do not load media until the user explicitly opens history.
        if not st.checkbox("查看历史记录", key="show_narrato_history"):
            return
        st.button("刷新历史列表", key="refresh_narrato_history")
        category = st.radio("记录类型", ["视频任务", "已保存脚本"], horizontal=True, key="history_category")
        if category == "视频任务":
            tasks = history(Path("/NarratoAI/storage/tasks"))
            if not tasks:
                st.info("暂无已保存的任务文件")
                return
            def label(directory):
                files = task_files(directory)
                date = datetime.fromtimestamp(max(p.stat().st_mtime for p in files)).strftime("%Y-%m-%d %H:%M")
                state = "有成片" if any(p.suffix == ".mp4" for p in files) else "仅配音 / 字幕"
                return f"{date} · {state} · {directory.name}"
            selected = st.selectbox("历史任务（最新在前）", tasks, format_func=label, key="history_task")
            files = task_files(selected)
            st.caption("按已有文件展示；旧任务的运行状态未持久化，不能仅凭文件判断是否成功。修复版与原版分别保留。")
            target = st.selectbox("任务文件", files, format_func=lambda p: {
                "combined_fixed.mp4": "修复版成片", "combined.mp4": "原版成片",
                "merger_audio.mp3": "合并配音",
            }.get(p.name, p.name), key="history_asset")
            if target.suffix == ".mp4":
                st.video(str(target))
            elif target.suffix in {".mp3", ".wav"}:
                st.audio(str(target))
            else:
                st.code(target.read_text(encoding="utf-8-sig"), language="text")
        else:
            root = Path("/NarratoAI/resource/scripts")
            scripts = sorted((p for p in root.glob("*.json") if p.is_file() and not p.is_symlink()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            if not scripts:
                st.info("暂无已保存脚本；编辑后点击保存的脚本会出现在这里")
                return
            target = st.selectbox("已保存脚本（最新在前）", scripts, format_func=lambda p: p.name, key="history_script")
            try:
                st.json(json.loads(target.read_text(encoding="utf-8-sig")))
            except (ValueError, OSError):
                st.warning("该脚本无法解析，可下载原文件检查")
        with target.open("rb") as file:
            st.download_button("下载当前文件", file, file_name=target.name, key="history_download")


if __name__ == "__main__":
    path = Path("/NarratoAI/webui.py")
    source = path.read_text(encoding="utf-8")
    before = "    st.write(get_help_text())"
    assert source.count(before) == 1
    source = source.replace(before, before + "\n    from app.services.narrato_history import render_history\n    render_history()", 1)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
