"""Clipboard validation and optional real X11/HTTP round trips (--live)."""
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from PIL import Image
import remote_clipboard as clipboard


def png():
    output = BytesIO()
    Image.new("RGB", (32, 24), "#2563eb").save(output, "PNG")
    return output.getvalue()


class ClipboardTests(unittest.TestCase):
    def test_unicode_and_png(self):
        self.assertEqual(clipboard.validate("中文 🌏".encode(), "text/plain;charset=utf-8"), "text/plain; charset=utf-8")
        self.assertEqual(clipboard.validate(png(), "image/png"), "image/png")

    def test_reject_html_fake_png_and_non_utf8(self):
        for body, mime in ((b"<script>", "text/html"), (b"fake", "image/png"), (b"\xff", "text/plain")):
            with self.subTest(mime=mime), self.assertRaises((ValueError, OSError)):
                clipboard.validate(body, mime)

    def test_request_requires_marker_and_bounded_length(self):
        handler = SimpleNamespace(command="POST", headers={}, rfile=BytesIO(b"private"))
        with self.assertRaises(PermissionError):
            clipboard.request_body(handler)
        handler.headers = {"X-Remote-Clipboard": "1", "Content-Length": str(clipboard.MAX_BYTES + 1)}
        with self.assertRaises(ValueError):
            clipboard.request_body(handler)
        self.assertEqual(handler.rfile.tell(), 0)


def live():
    opener = build_opener(ProxyHandler({}))
    url = f"http://127.0.0.1:{os.environ['WEB_PORT']}" + clipboard.API_PATH
    headers = {"X-Remote-Clipboard": "1"}
    for data, mime in (("Clipboard UTF-8 / 中文 / 🌏".encode(), "text/plain;charset=utf-8"), (png(), "image/png")):
        with opener.open(Request(url, data=data, headers={**headers, "Content-Type": mime}), timeout=15) as response:
            assert json.load(response)["ok"]
        with opener.open(Request(url, headers=headers), timeout=15) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert response.read() == data, f"Round trip failed: {mime}"
        print(f"X11 + site API round trip OK: {mime}")
    try:
        opener.open(Request(url, data=b"blocked", headers={"Content-Type": "text/plain"}), timeout=10)
        raise AssertionError("Missing CSRF header accepted")
    except HTTPError as error:
        assert error.code == 403
    import base64
    fixture = Path("/workspace/data/remote_browser/home/clipboard-fixture.html")
    fixture.write_text('''<!doctype html><meta charset="utf-8"><title>Clipboard QA</title>
<style>body{font:24px sans-serif;padding:24px}textarea{width:90%;height:100px;font:20px sans-serif}button{font:20px sans-serif;margin:12px}img{width:128px;image-rendering:pixelated}</style>
<h1>Clipboard QA</h1><textarea id="input" autofocus placeholder="Paste text or image here"></textarea>
<p id="result">Waiting for paste</p><img id="image"><br>
<button id="copy-text">Copy remote text</button><button id="copy-image">Copy remote PNG</button>
<script>
const fixture = "''' + base64.b64encode(png()).decode() + '''";
document.getElementById('input').addEventListener('paste', e => {
 e.preventDefault(); const file = Array.from(e.clipboardData.items).find(x=>x.type.startsWith('image/'));
 if(file){const image=document.getElementById('image');image.src=URL.createObjectURL(file.getAsFile());image.onload=()=>{document.getElementById('result').textContent='IMAGE PASTE OK: '+image.naturalWidth+' x '+image.naturalHeight};}
 else {document.getElementById('input').value=e.clipboardData.getData('text/plain');document.getElementById('result').textContent='TEXT PASTE OK';}
});
document.getElementById('copy-text').onclick=()=>navigator.clipboard.writeText('REMOTE COPY UTF-8 / 中文 / 🌏');
document.getElementById('copy-image').onclick=async()=>{const bytes=Uint8Array.from(atob(fixture),c=>c.charCodeAt(0));await navigator.clipboard.write([new ClipboardItem({'image/png':new Blob([bytes],{type:'image/png'})})]);};
</script>''', encoding="utf-8")
    os.chown(fixture, 10001, 10001)
    print(f"QA fixture: file://{fixture}")


if __name__ == "__main__":
    if "--live" in sys.argv:
        live()
    else:
        unittest.main()
