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
    assert app.first_url({"url_list":{"0":"https://cdn.test/a.heic", "1":"https://cdn.test/a.jpeg"}}, image=True).endswith(".jpeg")
    with patch.object(app, "client", return_value=API()):
        first = job()
        app.run_job(first)
        video = app.item(PID, VID)
        assert first["status"] == "complete" and video["views"] == 0 and video["comments"] == 28
        app.run_job(job())
        assert len(app.snapshot(PID)["videos"]) == 1, "Refreshing a product again must not duplicate videos"
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
        with patch.object(app, "download_media", wraps=app.download_media) as download:
            app.run_job(job("play",VID))
            folder = app.MEDIA / VID
            assert 86390 < app.video_expiry(folder) - time.time() <= 86400
            audio_job=job("audio",VID)
            app.run_job(audio_job)
            assert audio_job["status"]=="complete", audio_job
            assert download.call_count == 1, "Play then audio must reuse one download"
            expiry = app.video_expiry(folder)
            assert 604790 < expiry - time.time() <= 604800
            app.run_job(job("play",VID))
            assert app.video_expiry(folder) == expiry, "Playback must not shorten audio retention"
            with patch.object(app.time, "time", return_value=expiry + 1):
                assert not app.media_state(PID, VID)["downloaded"]
                app.cleanup_media()
            assert not (folder / "video.mp4").exists()
            assert (folder / "audio.mp3").exists() and (folder / "translation.json").exists()
            app.run_job(job("audio", VID))
            assert download.call_count == 2, "Expired video is downloaded automatically for extraction"
        audio_job=job("audio",VID)
        app.run_job(audio_job)
        assert audio_job["status"]=="complete", audio_job
    media=app.media_state(PID,VID)
    assert media["downloaded"] and media["audio_ready"] and media["translation"]["text"]=="你好，世界"
    print("PASS: fresh updates, zero metrics, partial failure, empty discovery, duplicate jobs, safe IDs/URLs, local image cache, ffmpeg download/audio + translation persistence", flush=True)


def account_checks():
    with app.database() as conn:
        conn.execute("INSERT INTO proxy_profiles(id,name,created_at,updated_at) VALUES (1,'test','','')")
        conn.execute("INSERT INTO tiktok_accounts(id,username,display_name,proxy_profile_id,created_at,updated_at) VALUES (1,'test_creator','Test creator',1,'','')")
    account = app.accounts()[0]
    assert account["feishu_user_name"] == ""
    with app.database() as conn:
        conn.execute("UPDATE tiktok_accounts SET feishu_user_name='测试同事' WHERE id=1")
    assert app.accounts()[0]["feishu_user_name"] == "测试同事"
    assert account["product_id"] == "account:1" and account["handle"] == "test_creator"
    assert "profile" not in account and "proxy_profile_id" not in account
    calls = []
    class AccountAPI:
        def get(self, path, params, **kwargs):
            calls.append((path, params.copy()))
            assert path == "/v1/scrape/tiktok/videos" and kwargs["cache_policy"] == "record_only"
            second = bool(params.get("max_cursor"))
            return {"data":{"aweme_list":[{"aweme_id":str(int(VID) + int(second)), "desc":"Account video", "create_time":123,
                    "author":{"uid":"12345", "unique_id":"test_creator", "nickname":"Test creator"},
                    "video":{"duration":15000}, "statistics":{"play_count":0,"digg_count":3,"comment_count":2}}],
                    "has_more":0 if second else 1,"max_cursor":"page2" if second else "page1"},"credits_used":1}
    with patch.object(app, "client", return_value=AccountAPI()):
        first = {**job(), "product_id":"account:1"}
        app.run_job(first)
        assert first["status"] == "complete" and len(calls) == 1
        snapshot = app.snapshot("account:1")
        assert snapshot["has_more"] and snapshot["videos"][0]["views"] == 0
        assert app.media_state("account:1", VID) == app.media_state(PID, VID), "Video and audio files must be shared across sources"
        assert len(app.snapshot(PID)["videos"]) == 1, "Account results must not mix into product videos"
        with patch.object(app._pool, "submit"):
            more = app.start({"product_id":"account:1", "action":"more"})
        assert more["cursor"] == "page1"
        app.run_job(more)
        assert more["status"] == "complete" and len(calls) == 2
        assert calls[1][1]["max_cursor"] == "page1"
        snapshot = app.snapshot("account:1")
        assert not snapshot["has_more"] and len(snapshot["videos"]) == 2
        app.run_job(more)
        app.run_job({**job(), "product_id":"account:1"})
        ids = [v["video_id"] for v in app.snapshot("account:1")["videos"]]
        assert len(ids) == len(set(ids)) == 2, "Repeated pages and refreshes must not duplicate videos"
        more["message"] = "本页已更新 10 条视频 · 1 credit，可手动加载下一页。"
        app.save_job(more)
        assert "credit" not in app.snapshot("account:1")["job"]["message"]
        with patch.object(app, "client", side_effect=ValueError("test failure")):
            app.run_job({**job(), "product_id":"account:1"})
        assert len(app.snapshot("account:1")["videos"]) == 2
        assert app.snapshot("account:1")["cursor"] == "page1"
    print("PASS: shared accounts, one request per page, cursor, account/product isolation, failed refresh preserves videos", flush=True)


def shared_video_checks():
    detail = {"aweme_id":VID,"statistics":{"play_count":0},"video":{"cover":{"url_list":{"0":"https://cdn.test/cover.heic"}},"origin_cover":{"url_list":{"0":"https://cdn.test/origin.heic","1":"https://cdn.test/origin.jpeg"}}}}
    assert app.video_detail(detail, {"video_id":VID})["cover_url"] == "https://cdn.test/origin.jpeg"
    detail["video"]["cover"]["url_list"]["1"] = "https://cdn.test/cover.jpg"
    assert app.video_detail(detail, {"video_id":VID})["cover_url"] == "https://cdn.test/cover.jpg"
    vid = "991"
    old = {"video_id":vid,"title":"Shared title","views":100,"comments":7,"cover_url":"https://cdn.test/cover.jpg","updated_at":100}
    new = {"video_id":vid,"title":"Shared title","views":0,"cover_url":"","updated_at":200}
    with app.database() as conn:
        conn.execute("INSERT INTO product_video_items VALUES (?,?,?)", (PID,vid,json.dumps(old)))
        conn.execute("INSERT INTO account_video_items VALUES (?,?,?)", ("account:1",vid,json.dumps(new)))
    app._initialized = False
    migrated = app.item(PID,vid)
    assert migrated == app.item("account:1",vid)
    assert migrated["views"] == 0 and migrated["comments"] == 7 and migrated["cover_url"] == old["cover_url"]
    app.save_video("account:1", {"video_id":vid,"views":50,"updated_at":300})
    assert app.item(PID,vid)["views"] == 50
    app.save_video(PID, {"video_id":vid,"likes":9,"updated_at":400})
    assert app.item("account:1",vid)["likes"] == 9
    app.save_video(PID, old)
    assert app.item(PID,vid)["views"] == 50, "Older source data must not overwrite shared data"
    app.save_video(PID, {"video_id":"992","title":"Shared title","views":999})
    assert app.item("account:1",vid)["views"] == 50, "Same title with another ID must remain separate"
    with app.database() as conn:
        assert conn.execute("SELECT COUNT(*) FROM shared_video_items WHERE video_id=?",(vid,)).fetchone()[0] == 1
        conn.execute("DELETE FROM product_video_items WHERE product_id=? AND video_id=?",(PID,vid))
    assert app.item("account:1",vid)["views"] == 50
    app._initialized = False
    assert app.item("account:1",vid)["views"] == 50, "Restart must not restore stale legacy payloads"
    print("PASS: shared video ID, migration, bidirectional updates, stale/empty protection, independent memberships", flush=True)


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
            page.wait_for_function("document.querySelector('#player video').readyState >= 1")
            for width, height in ((1120, 624), (390, 844)):
                page.set_viewport_size({"width":width,"height":height})
                assert page.evaluate("""() => {
                    const player = document.querySelector('#player').getBoundingClientRect();
                    const video = document.querySelector('#player video').getBoundingClientRect();
                    return video.bottom <= player.bottom + 1 && video.top >= player.top - 1 && video.right <= player.right + 1;
                }"""), "Native video controls must stay inside the player"
                assert page.locator('#mediaDialog .icon-button').evaluate("el => getComputedStyle(el).borderTopWidth === '0px'")
            page.set_viewport_size({"width":1440,"height":1000})
            page.keyboard.press("Escape")
            (app.MEDIA / VID / "video.mp4").unlink()
            (app.MEDIA / VID / "translation.json").unlink()
            page.locator(".video-cover").click()
            page.wait_for_function("document.querySelector('#extractAudio').disabled === false")
            assert page.locator('#playVideo').is_visible()
            assert page.locator('#downloadVideo').count() == 0
            page.keyboard.press("Escape")
            page.locator("#addProduct").click()
            page.locator('[name="product_id"]').fill("123456789")
            page.locator('[name="product_name"]').fill("New product")
            page.locator("#saveProduct").click()
            page.wait_for_function("document.querySelector('#selectedName').textContent === 'New product'")
            page.locator("#deleteProduct").click();page.locator("#confirmDelete").click()
            page.wait_for_function("document.querySelector('#selectedName').textContent === 'Renamed product'")
            page.set_viewport_size({"width":390,"height":844})
            with patch.object(app, "client", side_effect=AssertionError("Switching tabs must not call SociaVault")):
                page.locator('#accountsTab').click()
                page.wait_for_function("document.querySelector('#selectedName').textContent === 'Test creator'")
                assert page.locator('.video-card').count() == 2
                assert page.locator('#videoSort').input_value() == 'published_at'
                assert page.locator('#products .row-id').first.inner_text() == '测试同事'
                assert not page.locator('#addProduct').is_visible()
                assert 'credit' not in page.locator('body').inner_text()
                page.locator('#productsTab').click()
                page.wait_for_function("document.querySelector('#selectedName').textContent === 'Renamed product'")
                assert page.locator('.video-card').count() == 1
                assert page.locator('#videoSort').input_value() == 'views'
                for i in range(21):
                    app.save_video(PID, {"video_id":str(9000+i),"title":f"Pagination {i}","author":"Tester","views":i,"published_at":i})
                page.locator('#reloadProducts').click()
                page.wait_for_function("document.querySelector('#pageInfo').textContent.includes('共 22 条')")
                seen = []
                for count in (10,10,2):
                    assert page.locator('.video-card').count() == count
                    seen += page.locator('.video-cover').evaluate_all("els => els.map(el => el.dataset.media)")
                    if count == 10:
                        page.locator('#nextPage').click()
                assert len(set(seen)) == 22
                assert page.locator('#nextPage').is_disabled()
                page.locator('#videoFilter').fill('Pagination 0')
                assert page.locator('.video-card').count() == 1
                assert page.locator('#previousPage').is_disabled()
                page.locator('#videoFilter').fill('')
                page.locator('#nextPage').click()
                page.locator('#videoSort').select_option('published_at')
                assert page.locator('#previousPage').is_disabled()
                page.locator('#nextPage').click()
                page.locator('#previousPage').click()
                assert page.locator('.video-card').count() == 10
                page.locator('#accountsTab').click()
                page.wait_for_function("document.querySelector('#visibleVideos').textContent === '2'")
                assert page.locator('#previousPage').is_disabled()
                assert page.locator('#nextPage').is_disabled()
            assert page.title() == '视频列表'
            assert page.locator('#addProduct').evaluate("el => getComputedStyle(el).borderTopWidth === '0px'")
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
            account_checks()
            browser_checks()
            shared_video_checks()
    print("All product video workspace checks passed.",flush=True)


if __name__=="__main__":main()
