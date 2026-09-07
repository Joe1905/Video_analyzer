"""Real decoder and HTTP checks; all media/state live in a temporary directory."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urlparse

import cv2
import numpy as np
from video_analyzer.frame import VideoProcessor
from storyboard import StoryboardService, extract_frames


def video(path, levels):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (96, 64))
    assert writer.isOpened()
    for level in levels:
        for _ in range(10):
            writer.write(np.full((64, 96, 3), level, dtype=np.uint8))
    writer.release()


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "videos").mkdir()
        self.source = self.root / "videos" / "sample.mp4"
        video(self.source, [0, 220, 0, 220])
        self.service = StoryboardService(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_threshold_is_local_and_static_fallback(self):
        before = VideoProcessor.FRAME_DIFFERENCE_THRESHOLD
        normal = extract_frames(self.source, self.root / "normal", 4)
        strict = extract_frames(self.source, self.root / "strict", 4, 255)
        self.assertGreater(len(normal["frames"]), 1)
        times = [item["timestamp"] for item in normal["frames"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(strict["fallback"])
        self.assertEqual(before, VideoProcessor.FRAME_DIFFERENCE_THRESHOLD)
        video(self.source, [60, 60])
        still = extract_frames(self.source, self.root / "still", 4)
        self.assertTrue(still["fallback"])
        self.assertEqual(still["frames"][0]["timestamp"], 0)
        with self.assertRaises(ValueError):
            extract_frames(self.source, self.root / "bad", 41)

    def test_http_jobs_exports_and_isolation(self):
        service = self.service
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                service.handle(self, urlparse(self.path), lambda page, path: page)
            do_POST = do_GET
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, body=None):
            with urllib.request.urlopen(base + path, data=body, timeout=20) as response:
                return response.read()
        def wait(job):
            for _ in range(100):
                result = json.loads(request('/api/storyboard/jobs?id=' + job["id"]))
                if result["status"] != "running":
                    return result
                time.sleep(.1)
            self.fail("job did not finish")
        original = self.source.read_bytes()
        old_output = self.root / "output" / "sample.mp4"
        old_output.mkdir(parents=True)
        (old_output / "analysis.json").write_text('original analysis')
        try:
            self.assertIn('分镜提取', request('/storyboard').decode())
            query = urlencode({"filename": "sample.mp4", "max_frames": 4})
            job = json.loads(request('/api/storyboard/jobs?' + query, original))
            result = wait(job)
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(result["difference_threshold"], 10)
            self.assertEqual(self.source.read_bytes(), original)
            self.assertEqual((old_output / "analysis.json").read_text(), 'original analysis')
            self.assertFalse(list(service.job_dir(job["id"]).glob('source.*')))
            png = request('/api/storyboard/export?id=' + job["id"])
            self.assertTrue(png.startswith(b'\x89PNG'))
            archive = request('/api/storyboard/export?id=' + job["id"] + '&format=zip')
            with zipfile.ZipFile(BytesIO(archive)) as zipped:
                self.assertEqual(len(zipped.namelist()), len(result["frames"]) + 1)
            self.assertEqual(StoryboardService(self.root).load(job["id"])["status"], "complete")
            frame = result["frames"][0]["name"]
            self.assertTrue(request('/api/storyboard/image?id=' + job["id"] + '&frame=' + frame).startswith(b'\xff\xd8'))
            service.slot.acquire()
            try:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/storyboard/jobs?' + query, original)
                self.assertEqual(error.exception.code, 409)
            finally:
                service.slot.release()
            for bad in ['&difference_threshold=50', '&max_frames=0']:
                invalid_query = urlencode({"filename": "sample.mp4"}) + bad
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/storyboard/jobs?' + invalid_query, original)
                self.assertEqual(error.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError):
                request('/api/storyboard/jobs?id=../../sample.mp4')
            existing = json.loads(request('/api/storyboard/jobs?' + query + '&source=existing', b''))
            self.assertEqual(wait(existing)["status"], 'complete')
            broken = json.loads(request('/api/storyboard/jobs?' + query, b'not a video'))
            self.assertEqual(wait(broken)["status"], 'failed')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
