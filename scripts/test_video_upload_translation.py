"""Docker: python scripts/test_video_upload_translation.py; isolated, no paid API calls."""
import json
import os
import subprocess
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import requests
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright
import product_video_workspace as app
from test_product_video_workspace import Handler

SPANISH = "Hola, buenos días. Este vídeo está en español. Gracias por escuchar."
CHINESE = "你好，早上好。这段视频是西班牙语的。感谢收听。"


def wait_job(base, upload_id):
    for _ in range(100):
        result = requests.get(base + "/api/product-videos/upload-state", params={"id": upload_id}).json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(.1)
    raise AssertionError("Upload job did not finish")


def main():
    with tempfile.TemporaryDirectory() as temp, patch.object(app, "MEDIA", Path(temp) / "media"):
        sample = Path(temp) / "西语测试.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=160x240:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest", "-pix_fmt", "yuv420p", str(sample)], check=True, capture_output=True)
        real_run = subprocess.run
        def command(args, **kwargs):
            if "--transcribe" in args:
                app.write_json(Path(args[-1]), {"text": SPANISH, "language": "es", "segments": []})
                return subprocess.CompletedProcess(args, 0)
            return real_run(args, **kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(app.subprocess, "run", side_effect=command), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch("translate_analysis.translate_text_chunked", return_value=CHINESE) as translate:
                # Real binary upload + ffprobe/ffmpeg; ASR/translation doubles keep this repeatable.
                response = requests.post(base + "/api/product-videos/upload", params={"filename": sample.name}, data=sample.read_bytes())
                assert response.status_code == 202, response.text
                upload_id = response.json()["id"]
                result = wait_job(base, upload_id)
                assert result["status"] == "complete", result
                assert result["transcript"]["text"] == SPANISH and result["transcript"]["language"] == "es"
                assert result["translation"]["text"] == CHINESE
                assert translate.call_args.args[3] == SPANISH, "Spanish must reach Chinese translator unchanged"
                assert requests.get(base + "/api/product-videos/upload-file", params={"id": upload_id}).content.startswith(b"ID3")
                assert requests.get(base + "/api/product-videos/upload-state", params={"id": "../escape"}).status_code == 400
                assert requests.post(base + "/api/product-videos/upload?filename=a.exe", data=b"bad").status_code == 400
                assert requests.post(base + "/api/product-videos/upload?filename=a.mp4", data=b"").status_code == 400
                folder = app.upload_folder(upload_id)
                (folder / "translation.json").unlink()
                failed = {key: result[key] for key in ("id", "filename", "owner")}
                failed.update(status="failed", message="Test translation failure")
                app.save_upload(failed)
                # Retry translation even after video cache expires; audio and transcript remain.
                with patch.object(app.time, "time", return_value=time.time() + 8 * 86400):
                    app.cleanup_media()
                assert not (folder / "video.mp4").exists()
                assert (folder / "transcript.json").is_file()
                requests.post(base + "/api/product-videos/upload-retry", params={"id": upload_id})
                assert wait_job(base, upload_id)["status"] == "complete"
                assert translate.call_count == 2
                saved = requests.get(base + "/api/product-videos/upload-list").json()["jobs"]
                assert saved[0]["id"] == upload_id and saved[0]["has_translation"]
                assert "transcript" not in saved[0], "History must not load every transcript"
                # A single upload worker processes accepted jobs in order; retries do not duplicate queued work.
                started, release = threading.Event(), threading.Event()
                def held_translation(*args):
                    started.set()
                    assert release.wait(10), "Queue test timed out"
                    return CHINESE
                with patch("translate_analysis.translate_text_chunked", side_effect=held_translation), patch.object(app._upload_pool, "submit", wraps=app._upload_pool.submit) as submit:
                    first = requests.post(base + "/api/product-videos/upload?filename=first.mp4", data=sample.read_bytes()).json()["id"]
                    assert started.wait(10)
                    second = requests.post(base + "/api/product-videos/upload?filename=second.mp4", data=sample.read_bytes()).json()["id"]
                    try:
                        assert app.upload_state(first)["status"] == "running"
                        assert app.upload_state(second)["status"] == "queued"
                        requests.post(base + "/api/product-videos/upload-retry", params={"id": second})
                        requests.post(base + "/api/product-videos/upload-retry", params={"id": second})
                        assert submit.call_count == 2
                    finally:
                        release.set()
                    assert wait_job(base, first)["status"] == wait_job(base, second)["status"] == "complete"
                with patch.object(app, "_owner", "restarted"):
                    interrupted = {**app.upload_state(second), "status": "running"}
                    app.save_upload(interrupted)
                    assert app.upload_state(second)["status"] == "failed"
                app.save_upload({**interrupted, "status": "complete"})
                damaged = app.upload_folder("a" * 32)
                damaged.mkdir()
                (damaged / "job.json").write_text("{broken", encoding="utf-8")
                assert len(app.upload_list()["jobs"]) == 3
                # A bad media file fails in the background with a useful user message.
                bad = requests.post(base + "/api/product-videos/upload?filename=bad.mp4", data=b"not a video").json()["id"]
                assert "有效" in wait_job(base, bad)["message"]
                silent = Path(temp) / "silent.mp4"
                real_run(["ffmpeg", "-y", "-i", str(sample), "-an", "-c:v", "copy", str(silent)], check=True, capture_output=True)
                bad = requests.post(base + "/api/product-videos/upload?filename=silent.mp4", data=silent.read_bytes()).json()["id"]
                assert "没有音轨" in wait_job(base, bad)["message"]
                # Size and interrupted-stream validation never leave a partial video.
                for size, stream in ((512 * 1024 * 1024 + 1, b""), (10, b"short")):
                    try:
                        app.receive_upload(SimpleNamespace(headers={"Content-Length": str(size)}, rfile=BytesIO(stream)), {"filename": ["test.mp4"]})
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Invalid upload accepted")
                assert not list(app.MEDIA.rglob("*.part"))
                print("PASS: upload API, real ffmpeg, Spanish original to Chinese, retry after expiry, invalid/silent/oversize/interrupted media", flush=True)
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.route("**/api/product-videos/accounts?*", lambda route: route.fulfill(json={"products": []}))
                    page.route("**/api/files", lambda route: route.fulfill(json=[]))
                    page.goto(base + "/metrics", wait_until="networkidle")
                    assert page.title() == "视频列表"
                    assert page.locator("#uploadVideo").is_visible(), "Upload must work with an empty account library"
                    page.locator("#uploadsTab").click()
                    assert page.locator("#uploadsTab").get_attribute("aria-pressed") == "true"
                    page.locator("#uploadTasks [data-open-upload]").first.wait_for()
                    before = len(app.upload_list()["jobs"])
                    with page.expect_file_chooser() as chooser:
                        page.locator("#uploadQueueVideo").click()
                    chooser.value.set_files([str(sample), str(sample)])
                    page.wait_for_function("document.querySelector('#uploadTransferText').textContent.startsWith('已加入队列 2 个视频')")
                    page.wait_for_function("document.querySelectorAll('#uploadTasks [data-open-upload]').length === " + str(before + 2))
                    page.wait_for_function("document.querySelector('#uploadTasks .upload-task:first-child .upload-task-status').textContent === '已完成'")
                    page.locator("#uploadTasks [data-open-upload]").first.click()
                    page.wait_for_function("document.querySelector('#translatedText').textContent.includes('感谢收听')")
                    assert page.locator("#originalText").inner_text() == SPANISH
                    assert "西班牙语" in page.locator("#audioLanguage").inner_text()
                    assert page.locator("#mediaOriginal").is_hidden() and page.locator("#mediaReviewButton").is_hidden()
                    assert page.locator("#audioPlayer audio").count() == 1
                    assert page.locator("#downloadLinks a").is_visible()
                    assert page.locator("#extractAudio").is_disabled()
                    page.screenshot(path="/tmp/video-upload-desktop.png")
                    page.keyboard.press("Escape")
                    page.reload(wait_until="networkidle")
                    assert page.locator("#uploadsTab").get_attribute("aria-pressed") == "true"
                    page.locator("#productFilter").fill("first.mp4")
                    assert page.locator("#uploadTasks .upload-task").count() == 1
                    page.locator("#uploadTasks [data-open-upload]").click()
                    page.wait_for_function("document.querySelector('#translatedText').textContent.includes('感谢收听')")
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert page.locator("#mediaUploadVideo").is_visible()
                    page.screenshot(path="/tmp/video-upload-mobile.png", full_page=True)
                    page.keyboard.press("Escape")
                    page.locator("#accountsTab").click()
                    assert page.locator("#uploadWorkspace").is_hidden()
                    page.locator("#uploadsTab").click()
                    page.locator("#uploadTasks [data-open-upload]").first.wait_for()
                    assert page.locator("#uploadWorkspace").is_visible()
                    assert not errors, errors
                    browser.close()
                    print("PASS: uploads tab, batch queue, persistent history, search/replay, Spanish/Chinese, desktop/mobile, tab isolation, no JS errors", flush=True)
        finally:
            server.shutdown()
            server.server_close()
    print("All video upload translation checks passed.", flush=True)


if __name__ == "__main__":
    main()
