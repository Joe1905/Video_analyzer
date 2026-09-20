#!/usr/bin/env python3
"""Replay publish failures offline in Chromium; no TikTok/API traffic or real posts."""
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import tiktok_studio_publish as publish


def expect_error(kind, action, message):
    try:
        action()
    except kind as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected {kind.__name__}: {message}")


def main():
    with TemporaryDirectory() as directory, sync_playwright() as pw:
        folder = Path(directory)
        video = folder / "sample.mp4"
        video.write_bytes(b"offline fixture: input selection only")
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        # Every browser request is fulfilled locally, including the synthetic TikTok URL.
        context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html><body></body></html>"))
        page = context.new_page()
        page.goto("https://www.tiktok.com/tiktokstudio/upload")
        try:
            page.set_content('''<input type="file" accept="video/*"><button aria-label="Select video">Select video</button>
                <script>document.querySelector('input').onchange = event => {
                  event.target.value = '';
                  document.body.insertAdjacentHTML('beforeend','<div class="upload-text-container">Uploading</div>');
                  setTimeout(() => {document.body.innerHTML = '<div data-e2e="caption"><div contenteditable="true">Description</div></div>';}, 6100);
                };</script>''')
            started = time.monotonic()
            with patch.object(publish, "_set_video_file_via_native_chooser") as fallback:
                publish._set_video_file(page, video, log_dir=folder)
                assert publish._video_upload_state(page) == "uploading"
                with patch.object(publish, "UPLOAD_TIMEOUT_SECONDS", 12), patch("browser_page_state._ocr_page", return_value=("", "")):
                    publish._wait_for_upload_editor(page, folder)
                assert publish._video_upload_state(page) == "editor"
                fallback.assert_not_called()
            assert time.monotonic() - started > 5
            print("PASS: input cleared while uploading >5s; wait for editor without duplicate file selection", flush=True)

            page.set_content('<input type="file" accept="video/*" onchange="this.remove()">')
            with patch.object(page, "wait_for_timeout"):
                expect_error(RuntimeError, lambda: publish._set_video_file_via_cdp(page, video, page.locator('input'), folder), "未检测到")
            assert publish._video_upload_state(page) == "unknown"
            for markup, state in [
                ('<input type="file"><p>Upload failed</p><div class="upload-text-container">Uploading</div>', "failed"),
                ('<input type="file"><button aria-label="Select video">Select video</button>', "idle"),
                ('<input type="file"><div class="upload-text-container">Uploading</div>', "uploading"),
                ('<input type="file">', "unknown"),
            ]:
                page.set_content(markup)
                assert publish._video_upload_state(page) == state
                with patch.object(publish, "_set_video_file_via_cdp", side_effect=RuntimeError("CDP fixture")), patch.object(publish, "_set_video_file_via_native_chooser") as fallback:
                    if state in {"failed", "unknown"}:
                        expect_error(publish.ManualReviewRequired, lambda: publish._set_video_file(page, video, log_dir=folder), state)
                    else:
                        publish._set_video_file(page, video, log_dir=folder)
                    assert fallback.call_count == (1 if state == "idle" else 0)
            page.set_content('<p>Upload failed</p><div data-e2e="caption"><div contenteditable="true">Description</div></div>')
            expect_error(Exception, lambda: publish._wait_for_upload_editor(page, folder), "上传或处理失败")
            print("PASS: detached input alone is not success; explicit failure wins; fallback only from idle", flush=True)

            page.set_content('''<input id="schedule" value="2026-09-25 10:25"><div role="dialog"><h2>Continue to post?</h2>
                <button onclick="window.posts=(window.posts||0)+1">Post now</button></div>''')
            expect_error(publish.ResultUncertain, lambda: publish._wait_for_submission_result(page, .5), "二次确认")
            assert page.evaluate("window.posts || 0") == 0
            assert page.locator('#schedule').input_value() == "2026-09-25 10:25"
            page.set_content('<p>Uploaded</p>')
            expect_error(publish.ResultUncertain, lambda: publish._wait_for_submission_result(page, .1), "未收到明确成功信号")
            page.set_content('<p id="status">Uploading</p><script>setTimeout(()=>document.querySelector("#status").textContent="Video scheduled successfully", 300)</script>')
            publish._wait_for_submission_result(page, 2)
            print("PASS: confirmation is explicit, no Post now click; upload alone is not a publish receipt", flush=True)

            # Exercise the actual browser job wrapper twice; no live account or database is used.
            class BrowserHandle:
                contexts = [context]
            class PlaywrightHandle:
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                class chromium:
                    @staticmethod
                    def connect_over_cdp(_url):
                        return BrowserHandle()
            page.set_content('<p>Offline replay</p>')
            def fail_with_chain(*_):
                try:
                    raise RuntimeError("first upload error")
                except RuntimeError as original:
                    raise RuntimeError("later screenshot error") from original
            with patch.object(publish, "LOG_ROOT", folder), patch.object(publish, "video_path", return_value=video), patch("playwright.sync_api.sync_playwright", return_value=PlaywrightHandle()), patch.object(publish, "_ensure_studio_page", side_effect=fail_with_chain), patch.object(page, "wait_for_timeout"):
                for _ in range(2):
                    expect_error(RuntimeError, lambda: publish._execute_browser({"id":"fixture","asset_id":"fixture","attempt_count":1}, {"debug_port":1}), "later screenshot error")
            attempts = list((folder / 'fixture').glob('attempt-*'))
            assert len(attempts) == 2
            for attempt in attempts:
                chain = json.loads((attempt / 'error.json').read_text())
                assert [e['message'] for e in chain] == ['later screenshot error', 'first upload error']
                assert (attempt / 'last-state.png').exists()
            print("PASS: independent attempt directories and original exception chain preserved", flush=True)
        finally:
            browser.close()
    print("All publish reliability replays passed.", flush=True)


if __name__ == "__main__":
    main()
