#!/usr/bin/env python3
"""Offline checks for TikTok Studio publish-success detection."""

import tiktok_studio_publish as publish


class FakeLocator:
    def __init__(self, visible: bool):
        self.visible = visible

    def count(self) -> int:
        return 1 if self.visible else 0

    def nth(self, _index: int):
        return self

    def is_visible(self) -> bool:
        return self.visible


class FakePage:
    def __init__(self, url: str, texts: list[str] | None = None):
        self.url = url
        self.texts = texts or []

    def get_by_text(self, pattern, exact: bool = False) -> FakeLocator:
        visible = any(pattern.search(text) for text in self.texts)
        return FakeLocator(visible)


def main() -> None:
    assert publish._submission_succeeded(
        FakePage("https://www.tiktok.com/tiktokstudio/content")
    )
    assert publish._submission_succeeded(
        FakePage("https://www.tiktok.com/tiktokstudio/upload", ["Video published"])
    )
    assert publish._submission_succeeded(
        FakePage("https://www.tiktok.com/tiktokstudio/upload", ["Your video has been published successfully."])
    )
    assert publish._submission_succeeded(
        FakePage("https://www.tiktok.com/tiktokstudio/upload", ["视频已定时"])
    )
    assert not publish._submission_succeeded(
        FakePage("https://www.tiktok.com/tiktokstudio/upload", ["Schedule", "Uploaded videos"])
    )

    print("PASS: TikTok Studio 发布成功信号判定模拟通过")


if __name__ == "__main__":
    main()
