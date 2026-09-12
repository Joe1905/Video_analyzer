"""Tests for NarratoAI Modern Workbench Tornado server & router."""
import json
import asyncio
import time
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, AsyncMock
import tornado.web
import tornado.routing
import tornado.testing
from app.services import narrato_workbench_server as wb
from app.utils import utils


class WorkbenchServerTestCase(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir='/NarratoAI/storage')
        self.root = Path(self.temp.name)
        self.patches = [patch.object(wb, 'OBJECT_ROOT', self.root/'objects'),
                        patch.object(wb, 'VIDEO_RESOURCE', self.root/'videos'),
                        patch.object(wb, 'TASKS_ROOT', self.root/'tasks'),
                        patch.object(wb, 'GLOBAL_PRODUCTS_FILE', self.root/'products.json'),
                        patch.object(utils, 'task_dir', side_effect=lambda sub_dir='': str(self.root/'tasks'/sub_dir)),
                        patch.dict(wb.config.app, {'elevenlabs_api_key':'', 'vision_openai_api_key':'test', 'vision_openai_model_name':'deepseek-test-vision'})]
        for p in self.patches:
            p.start()
        for p in (wb.OBJECT_ROOT, wb.VIDEO_RESOURCE, wb.TASKS_ROOT):
            p.mkdir()
        wb.ACTIVE_JOBS.clear()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def post_json(self, url, body):
        return self.fetch(url, method='POST', body=json.dumps(body), headers={'Content-Type':'application/json'})

    def wait_job(self, endpoint, job):
        for _ in range(1200):
            data = json.loads(self.fetch(endpoint + job).body)
            if data['status'] in ('complete', 'error', 'failed'):
                self.assertEqual(data['status'], 'complete', data)
                return data
            time.sleep(.1)
        self.fail('Job did not finish')

    def get_app(self):
        # Create Tornado application with workbench rules
        rules = wb.get_workbench_rules()
        app = tornado.web.Application()
        for rule in reversed(rules):
            app.wildcard_router.rules.insert(0, rule)
        return app

    def test_root_serves_approved_html_without_legacy_header(self):
        response = self.fetch("/")
        self.assertEqual(response.code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("NarratoAI · 智能剪辑工作台", body)
        self.assertIn("物品识别剪辑", body)
        self.assertIn("全自动视频制作", body)
        self.assertIn("theater-modal", body)
        self.assertIn("product-modal", body)
        self.assertIn("task-drawer", body)
        # Verify legacy elements are completely absent
        self.assertNotIn("Narrato:blue[AI]:sunglasses:", body)
        self.assertNotIn("一站式 AI 影视解说+自动化剪辑工具", body)

    def test_api_state(self):
        response = self.fetch("/api/state")
        self.assertEqual(response.code, 200)
        data = json.loads(response.body.decode("utf-8"))
        self.assertIn("current_task", data)
        self.assertIn("object_tasks", data)
        self.assertIn("materials", data)
        self.assertIn("voices", data)
        self.assertIn("auto_tasks", data)

    def test_api_voices(self):
        response = self.fetch("/api/voices")
        self.assertEqual(response.code, 200)
        data = json.loads(response.body.decode("utf-8"))
        self.assertIn("voices", data)
        self.assertTrue(len(data["voices"]) > 0)
        first_voice = data["voices"][0]
        self.assertIn("voice_id", first_voice)
        self.assertIn("name", first_voice)

    def test_patch_streamlit_server(self):
        from streamlit.web.server.server import Server
        wb.patch_streamlit_server()
        self.assertTrue(getattr(Server, "_workbench_patched", False))

    def test_boundaries_duplicate_and_uncertain_export(self):
        self.assertEqual(self.fetch('/api/media/../../etc/hostname').code, 403)
        self.assertEqual(self.fetch('/api/media/config.toml').code, 403)
        self.assertEqual(self.post_json('/api/object/analyze', {}).code, 400)
        root = wb.OBJECT_ROOT/'busy'
        root.mkdir()
        wb.obj_demo.save(root/'manifest.json', {'status':'running','clips':[]})
        original = (root/'manifest.json').read_bytes()
        wb.ACTIVE_JOBS['busy'] = {'status':'running','progress':'active'}
        with patch.object(wb.threading, 'Thread') as thread:
            response = self.post_json('/api/object/analyze', {'job_id':'busy'})
            self.assertEqual(response.code, 200)
            thread.assert_not_called()
        self.assertEqual((root/'manifest.json').read_bytes(), original)
        self.assertEqual(wb.ACTIVE_JOBS['busy']['status'], 'running')
        wb.obj_demo.save(root/'manifest.json', {'status':'review','clips':[{'status':'uncertain'}]})
        response = self.post_json('/api/object/export', {'job_id':'busy','selected_indices':[0]})
        self.assertEqual(response.code, 400)
        response = self.fetch('/api/object/download/busy?file=../../config.toml')
        self.assertEqual(response.code, 400)

    def test_object_and_auto_pipeline(self):
        from PIL import Image
        video = wb.VIDEO_RESOURCE/'sample.mp4'
        subprocess.run([wb.obj_demo._get_ffmpeg_binary(), '-v','error','-f','lavfi','-i',
            'color=c=red:s=320x240:d=1', '-c:v','libx264','-pix_fmt','yuv420p',str(video)], check=True)
        ref = wb.OBJECT_ROOT/'ref.jpg'
        Image.new('RGB',(20,20),'red').save(ref)
        body = {'job_id':'sample','step':.5,'sources':[str(video)],
            'products':[{'id':'p1','name':'red','description':'red object','references':[str(ref)] * 5}]}
        async def identify(provider, frames, products, log, **kwargs):
            return [dict(f,objects=[{'id':'p1','status':'present','reason':'test'}]) for f in frames]
        with patch.object(wb.obj_demo, 'identify', side_effect=identify):
            response = self.post_json('/api/object/analyze', body)
            self.assertEqual(response.code, 200, response.body)
            result = self.wait_job('/api/object/status/', 'sample')
        self.assertEqual(len(result['clips']), 1)
        response = self.post_json('/api/object/export', {'job_id':'sample','selected_indices':[0]})
        self.assertEqual(response.code, 200, response.body)
        download = json.loads(response.body)['download_url']
        self.assertEqual(self.fetch(download).code, 200)
        from app.services.narrato_multi_material import MultiMaterialAnalysisService
        script = [{'_id':1,'video_id':1,'video_name':video.name,'video_path':str(video),
                   'timestamp':'00:00:00,000-00:00:01,000','picture':'red','narration':'Red.', 'OST':0}]
        with patch.object(MultiMaterialAnalysisService, 'generate_documentary_script', new_callable=AsyncMock, return_value=script) as generate:
            response = self.post_json('/api/auto/script', {'theme':'test','desc':'red object','sources':[str(video)]})
            self.assertEqual(response.code, 200, response.body)
            job = json.loads(response.body)['task_id']
            result = self.wait_job('/api/auto/status/', job)
            self.assertEqual(generate.call_args.kwargs['video_paths'], [str(video)])
        # Real FFmpeg renderer, no TTS API: verify success means a playable file exists.
        response = self.post_json('/api/auto/render', {'script_job_id':job,'storyboard':result['shots'],
            'aspect':'9:16','voice_id':'','subtitle_enabled':False})
        self.assertEqual(response.code, 200, response.body)
        rendered = self.wait_job('/api/auto/status/', json.loads(response.body)['task_id'])
        self.assertEqual(self.fetch(rendered['output_url'], headers={'Range':'bytes=0-31'}).code, 206)


if __name__ == "__main__":
    import unittest
    unittest.main()
