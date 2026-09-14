"""Docker: python scripts/test_product_video_workspace.py (isolated DB, no paid calls)."""
import json
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import sync_playwright
import product_video_workspace as app
import proxy_pool

PID, VID = "1732457689950426021", "7655159328738954509"
CATALOG = {"product_id": PID, "product_name": "NeuroBuddie 测试商品", "price": "$29.99", "stock": "0", "status": "Active", "image_url": "https://p16-oec-general-useast5.ttcdn-us.com/sample.jpg"}
RELATED = {"item_id": VID, "title": "A product video <script>bad()</script>", "author_id": "7654493188364108813", "author_name": "LittleDriverClub", "play_count": "150120", "like_count": "1004", "duration": 15117, "upload_time": "1782355699"}


class API:
    def get(self, path, params, **kwargs):
        assert kwargs["cache_policy"] == "record_only", "Updates must bypass API cache"
        if path.endswith("product-details"):
            return {"success": True, "data": {"product_id": PID, "related_videos": {"0": RELATED}}}
        return {"data": {"aweme_detail": {"aweme_id": VID, "statistics": {"play_count": 0, "digg_count": 0, "comment_count": 28, "share_count": 51, "collect_count": 225}}}}


def job(action="refresh", vid=""):
    return {"id": "test", "owner": app._owner, "product_id": PID, "video_id": vid, "action": action, "status": "running", "done": 0, "total": 0, "failures": 0}


def backend_checks(directory):
    proxy_pool.create_product(CATALOG)
    normalized = app.normalize_related(RELATED)
    assert normalized["video_id"] == VID and normalized["duration"] == 15.117
    with patch.object(app, "client", return_value=API()):
        first = job()
        app.run_job(first)
        video = app.item(PID, VID)
        assert first["status"] == "complete" and video["views"] == 0 and video["comments"] == 28
        with patch.object(app, "refresh_video", side_effect=ValueError("test error")):
            failed = job(vid=VID)
            app.run_job(failed)
        assert failed["status"] == "partial" and app.item(PID, VID)["comments"] == 28
        assert app.item(PID, VID)["error"]
    class EmptyAPI(API):
        def get(self, path, params, **kwargs):
            if path.endswith("product-details"):
                return {"data": {"product_id": PID, "related_videos": {}}}
            return super().get(path, params, **kwargs)
    with patch.object(app, "client", return_value=EmptyAPI()):
        app.run_job(job())
    assert len(app.snapshot(PID)["videos"]) == 1, "Empty discovery must preserve collected videos"
    with patch.object(app._pool, "submit") as submit, patch.object(app, "client", return_value=API()):
        one = app.start({"product_id":PID})
        two = app.start({"product_id":PID})
        assert one["id"] == two["id"] and submit.call_count == 1
    app.save_job({**one, "status":"complete"})
    for bad in ("../../etc/passwd", "1' OR 1=1", "１２３"):
        try: app.identifier(bad)
        except ValueError: pass
        else: raise AssertionError("Unsafe ID accepted")
    for bad in ("http://127.0.0.1/a", "https://example.com/a", "https://tiktokcdn.com.evil.test/a", "https://user:pass@p16.tiktokcdn.com/a"):
        try: app.validate_media_url(bad)
        except ValueError: pass
        else: raise AssertionError("Unsafe media URL accepted")
    def make_image(source, target, limit):
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (30,30), "#c6d6b3").save(target, "PNG")
    with patch.object(app, "download_media", side_effect=make_image) as download:
        assert app.image_path(PID) == app.image_path(PID)
        assert download.call_count == 1, "Local cache must avoid second remote request"
    sample = directory / "sample.mp4"
    real_run = subprocess.run
    real_run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=160x240:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest", "-pix_fmt", "yuv420p", str(sample)], check=True, capture_output=True)
    def run_command(command, **kwargs):
        if "--transcribe" in command:
            app.write_json(Path(command[-1]), {"text":"Hello world", "language":"en", "segments":[]})
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, **kwargs)
    with patch.object(app, "client", return_value=API()), patch.object(app, "refresh_video", return_value={"video_id":VID,"media_url":"https://v16.tiktokcdn.com/sample.mp4"}), patch.object(app,"download_media",side_effect=lambda source,target,limit:shutil.copyfile(sample,target)), patch.object(app.subprocess,"run",side_effect=run_command), patch.dict(app.os.environ, {"DEEPSEEK_API_KEY":"test"}), patch("translate_analysis.translate_text_chunked", return_value="你好，世界"):
        app.run_job(job("download",VID))
        audio_job=job("audio",VID)
        app.run_job(audio_job)
        assert audio_job["status"]=="complete", audio_job
    media=app.media_state(PID,VID)
    assert media["downloaded"] and media["audio_ready"] and media["translation"]["text"]=="你好，世界"
    print("PASS: fresh updates, zero metrics, partial failure, empty discovery, duplicate jobs, safe IDs/URLs, local image cache, ffmpeg download/audio + translation persistence", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        parsed=urlparse(self.path)
        if app.handle(self, parsed, self.file):return
        if parsed.path=="/api/proxy/products":return app.reply(self,200,proxy_pool.list_products())
        if parsed.path=="/metrics":
            page=(Path(__file__).parent/"static"/"metrics.html").read_bytes()
            return app.reply(self,200,page,"text/html; charset=utf-8")
        if parsed.path.startswith("/assets/"):
            path=Path(__file__).parent/"static"/"assets"/Path(parsed.path).name
            return app.reply(self,200,path.read_bytes(),"text/css" if path.suffix==".css" else "text/javascript")
        app.reply(self,404,{})
    def do_POST(self):
        parsed=urlparse(self.path)
        if app.handle(self,parsed,self.file):return
        payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if parsed.path=="/api/proxy/products":
            return app.reply(self,200,proxy_pool.update_product(payload) if payload.get("action")=="update" else proxy_pool.create_product(payload))
        if parsed.path=="/api/proxy/products/delete":return app.reply(self,200,proxy_pool.delete_product(payload["product_id"]))
        app.reply(self,404,{})
    @staticmethod
    def file(handler,path,content_type,*args,**kwargs):app.reply(handler,200,path.read_bytes(),content_type)


def browser_checks():
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,args=["--no-sandbox"])
            page=browser.new_page(viewport={"width":1440,"height":1000})
            errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/metrics",wait_until="networkidle")
            assert page.locator(".video-card").count()==1
            assert page.locator("#videos script").count()==0
            page.locator("#productFilter").fill("not found")
            assert page.locator(".product-row").count()==0
            page.locator("#productFilter").fill("")
            page.locator("#editProduct").click()
            page.locator('[name="product_name"]').fill("Renamed product")
            page.locator("#saveProduct").click()
            page.wait_for_function("document.querySelector('#selectedName').textContent === 'Renamed product'")
            page.locator(".video-cover").click()
            page.wait_for_function("document.querySelector('#translatedText').textContent === '你好，世界'")
            assert page.locator("#mediaDialog audio").count()==1 and page.locator("#mediaDialog video").count()==1
            page.keyboard.press("Escape")
            page.locator("#addProduct").click()
            page.locator('[name="product_id"]').fill("123456789")
            page.locator('[name="product_name"]').fill("New product")
            page.locator("#saveProduct").click()
            page.wait_for_function("document.querySelector('#selectedName').textContent === 'New product'")
            page.locator("#deleteProduct").click();page.locator("#confirmDelete").click()
            page.wait_for_function("document.querySelector('#selectedName').textContent === 'Renamed product'")
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile overflow"
            assert not errors, errors
            browser.close()
            print("PASS: browser CRUD/search, escaped video titles, media dialog, mobile layout, no JS errors",flush=True)
    finally:server.shutdown();server.server_close()


def main():
    with tempfile.TemporaryDirectory() as temp:
        directory=Path(temp)
        with patch.object(proxy_pool,"DATA_DIR",directory),patch.object(proxy_pool,"DB_PATH",directory/"test.sqlite"),patch.object(app,"DIRECTORY",directory/"cache"),patch.object(app,"MEDIA",directory/"media"):
            app._initialized=False
            backend_checks(directory)
            browser_checks()
    print("All product video workspace checks passed.",flush=True)


if __name__=="__main__":main()
