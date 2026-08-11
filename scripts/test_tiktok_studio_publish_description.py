#!/usr/bin/env python3
"""Offline simulation for TikTok Studio's prefilled description editor."""

import tiktok_studio_publish as publish


class FakeEditor:
    def __init__(self, text: str, clear_works: bool = True):
        self.text = text
        self.clear_works = clear_works
        self.selected = False
        self.actions: list[tuple[str, str]] = []

    def count(self) -> int:
        return 1

    def nth(self, _index: int):
        return self

    def is_visible(self) -> bool:
        return True

    def click(self) -> None:
        self.actions.append(("click", ""))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))
        if key == "Control+A":
            self.selected = True
        elif key == "Backspace" and self.selected:
            if self.clear_works:
                self.text = ""
            self.selected = False

    def type(self, value: str) -> None:
        self.actions.append(("type", value))
        self.text = value if self.selected else self.text + value
        self.selected = False

    def fill(self, value: str) -> None:
        self.actions.append(("fill", value))
        self.text = value if self.clear_works else self.text + value

    def input_value(self) -> str:
        raise RuntimeError("contenteditable has no input value")

    def inner_text(self) -> str:
        return self.text


class FakePage:
    def __init__(self, editor: FakeEditor):
        self.editor = editor

    def locator(self, _selector: str) -> FakeEditor:
        return self.editor

    def get_by_label(self, _label) -> FakeEditor:
        return self.editor


def main() -> None:
    editor = FakeEditor("TikTok 默认标题")
    publish._set_description(FakePage(editor), "用户输入的视频名称")
    assert editor.text == "用户输入的视频名称"
    assert editor.actions[:4] == [
        ("click", ""),
        ("press", "Control+A"),
        ("press", "Backspace"),
        ("type", "用户输入的视频名称"),
    ]

    broken_editor = FakeEditor("TikTok 默认标题", clear_works=False)
    try:
        publish._set_description(FakePage(broken_editor), "用户输入的视频名称")
    except RuntimeError as exc:
        assert "未能替换预填内容" in str(exc)
    else:
        raise AssertionError("未清空预填内容时必须让任务失败")

    print("PASS: TikTok Studio 标题预填内容替换模拟通过")


if __name__ == "__main__":
    main()
