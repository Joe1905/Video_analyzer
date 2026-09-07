"""Offline regression checks. Run in the analyzer Docker image."""
import copy
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from replication_workflow import ReplicationWorkflow, WorkflowError, QC_KEYS, VIDEO_QC_KEYS, validate_analysis, validate_blueprint


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'input.mp4').write_bytes(b'fixture')
        self.w = ReplicationWorkflow(self.root, self.root, self.root, model=lambda *args: {'prompt':'0–2秒：使用@图片1商品和@图片2分镜，保持动作连续。'})
        self.t = self.w.create(dict(filename='input.mp4', product='杯子', selling_points='保温', audience='通勤者', scene='办公室'))
        self.tid = self.t['id']
        self.upload('product')
        self.t = self.w.get(self.tid)
        self.t['analysis'] = {'shots':[dict(id='s1',start=0,end=2,visual='手持杯',action='打开',camera='近景',scene='桌前',function='展示')], 'copy':[], 'sounds':[]}
        self.t['media'] = {'duration':2}
        self.t['contract'] = {'details':'白色杯子'}
        cell = dict(id=1,source_ids=['s1'],source_start=0,source_end=2,start=0,end=2,event='打开',replacement='白杯',identity='正面',physics='手接触',decision='保留')
        self.t['blueprint'] = {'segments':[dict(id=1,duration=2,cells=[cell],copy='保温',image_prompt='一格竖图')]}
        self.w.save(self.t)

    def upload(self, kind, segment=0):
        b = io.BytesIO()
        Image.new('RGB',(90,160),'white').save(b,'PNG')
        raw = b.getvalue()
        return self.w.upload(self.tid,kind,segment,'test.png','正面',io.BytesIO(raw),len(raw))

    def review(self, kind='storyboard', passed=True):
        t = self.w.get(self.tid)
        a = self.w.latest_asset(t,kind,1)
        keys = VIDEO_QC_KEYS if kind=='video' else QC_KEYS
        cell = dict(id=1,note='' if passed else '标识错误',**{k:passed for k in keys})
        return self.w.review(self.tid,dict(kind=kind,segment=1,asset_id=a['id'],reviewer='测试审核',cells=[cell]))

    def test_validation(self):
        validate_analysis(self.t['analysis'],2)
        validate_blueprint(self.t['blueprint'],self.t['analysis'],2)
        bad=copy.deepcopy(self.t['analysis']);bad['shots'][0]['end']=1
        with self.assertRaises(WorkflowError):validate_analysis(bad,2)
        bad=copy.deepcopy(self.t['blueprint']);bad['segments'][0]['cells'][0]['source_ids']=['missing']
        with self.assertRaises(WorkflowError):validate_blueprint(bad,self.t['analysis'],2)

    def test_gate_and_replace(self):
        with self.assertRaises(WorkflowError):self.w.start(self.tid,'prompts')
        self.upload('storyboard',1)
        t=self.review();self.assertTrue(self.w.review_passed(t,'storyboard',1))
        self.w.video_prompts(t);self.w.save(t)
        self.assertTrue(t['prompts'])
        t=self.upload('storyboard',1)
        self.assertFalse(t['prompts']);self.assertFalse(self.w.review_passed(t,'storyboard',1))

    def test_product_edit_invalidates_and_persists(self):
        t=self.w.update(self.tid,{'brief':dict(self.t['brief'],product='新杯')})
        self.assertIsNone(t['blueprint']);self.assertIsNone(t['contract'])
        self.assertEqual(t['revision'],self.t['revision']+1)
        again=ReplicationWorkflow(self.root,self.root,self.root)
        self.assertEqual(again.get(self.tid)['brief']['product'],'新杯')

    def test_repair_limit(self):
        self.upload('storyboard',1)
        t=self.review(passed=False);self.assertTrue(t['reviews']['storyboard:1']['repairable'])
        self.upload('storyboard',1)
        t=self.review(passed=False);self.assertFalse(t['reviews']['storyboard:1']['repairable'])
        with self.assertRaises(WorkflowError):self.upload('storyboard',1)

    def test_new_prompts_invalidate_old_video(self):
        self.upload('storyboard',1);t=self.review()
        t['assets'].append(dict(t['assets'][-1],id='oldvideo',kind='video'))
        t['reviews']['video:1']={'passed':True}
        self.w.video_prompts(t)
        self.assertIsNone(self.w.latest_asset(t,'video',1));self.assertNotIn('video:1',t['reviews'])

    def test_export(self):
        with self.w.export(self.tid) as stream, zipfile.ZipFile(stream) as z:
            self.assertIn('outputs/image_prompts/segment_01.md',z.namelist())
            self.assertIn('product_fidelity.md',z.namelist())
            self.assertTrue(any(n.startswith('inputs/product_images/') for n in z.namelist()))

    def test_concurrency_and_restart(self):
        t=self.w.get(self.tid);t['status']='running';self.w.save(t)
        with self.assertRaises(WorkflowError):self.w.update(self.tid,{'brief':t['brief']})
        again=ReplicationWorkflow(self.root,self.root,self.root)
        self.assertEqual(again.get(self.tid)['status'],'interrupted')

    def test_path_and_incomplete_upload(self):
        with self.assertRaises(WorkflowError):self.w.get('../oops')
        with self.assertRaises(WorkflowError):self.w.upload(self.tid,'product',0,'x.png','正面',io.BytesIO(b''),100)


if __name__ == '__main__':unittest.main()
