"""Isolated browser contract test; run in the 4004 Compose validation environment."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    state = {"direct_video_enabled": False, "message": "Qwen 欠费；DeepSeek 帧分析；视频直连分析已禁用",
             "next_check_at": 1788835043}
    html = """<!doctype html><html><body>
    <label><input id="directMode" type="checkbox" checked>直接调用 LLM</label>
    <div id="existingDirectResult">已有直接提取内容</div>
    <script>
    window.DEFAULT_ANALYSIS_MODE = 'direct_video';
    async function refresh() { window.filesRefreshed = true; }
    async function submitAnalysis() { throw new Error('DIRECT_VIDEO_DISABLED'); }
    </script></body></html>"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.route("http://vision.test/**", lambda route: route.fulfill(
            content_type="application/json" if route.request.url.endswith("/api/vision-status") else "text/html",
            body=json.dumps(state) if route.request.url.endswith("/api/vision-status") else html))
        page.goto("http://vision.test/")
        page.add_script_tag(path=str(Path(__file__).parent / "static/assets/vision-status.js"))
        page.wait_for_function("document.querySelector('#visionStatus').textContent.includes('下次检查')")
        assert page.locator("#directMode").is_disabled()
        assert not page.locator("#directMode").is_checked()
        assert "已禁用" in page.locator("#visionStatus").inner_text()
        assert page.locator("#existingDirectResult").inner_text() == "已有直接提取内容"
        assert page.evaluate("window.DEFAULT_ANALYSIS_MODE") == "analyzer"
        state.update(direct_video_enabled=True, message="Qwen 已恢复")
        page.evaluate("refresh()")
        assert page.locator("#directMode").is_enabled()
        assert "Qwen 已恢复" in page.locator("#visionStatus").inner_text()
        assert page.evaluate("window.filesRefreshed")
        state.update(direct_video_enabled=False, message="Qwen 欠费；视频直连分析已禁用")
        page.evaluate("submitAnalysis().catch(() => {})")
        assert page.locator("#directMode").is_disabled()
        browser.close()
    print("Vision UI: disabled/recovery/submission refresh/history preservation OK")


if __name__ == "__main__":
    main()
