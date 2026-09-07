import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from visual_analysis_cache import best_analysis, valid_analysis, archive_invalid_analysis

class CacheTests(unittest.TestCase):
    def test_silent_video_does_not_need_whisper(self):
        from frame_vision_analyze import has_audio
        with patch('frame_vision_analyze.subprocess.run') as run:
            run.return_value.stdout='{"streams":[]}'
            self.assertFalse(has_audio('silent.mp4'))
            run.return_value.stdout='{"streams":[{"index":1}]}'
            self.assertTrue(has_audio('speech.mp4'))
            run.return_value.stdout='invalid'
            self.assertTrue(has_audio('unknown.mp4'))

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
    def put(self,name,payload):
        p=self.root/name;p.write_text(json.dumps(payload),encoding='utf-8');return p
    def test_bad_translation_does_not_mask_good_raw(self):
        self.put('analysis.json',{'visual_evidence':[{'description':'Person opening a box in a bedroom'}]})
        self.put('analysis_zh.json',{'visual_evidence':[{'description':'Error analyzing frame: Arrearage'}]})
        self.assertEqual(best_analysis(self.root)['source_channel'],'B')
    def test_archive_only_invalid_preserve_original(self):
        bad={'visual_evidence':[{'description':'Error analyzing frame: Bad Request'}]}
        self.put('analysis.json',bad)
        good=self.put('direct_analysis.json',{'timeline':[{'visual':'A shoe held in a hand'}]})
        self.assertEqual(archive_invalid_analysis(self.root),['analysis.json'])
        self.assertTrue(good.is_file())
        self.assertEqual(json.loads(next(self.root.glob('failed_visual_archive/*/analysis.json')).read_text()),bad)
        self.assertEqual(best_analysis(self.root)['source_channel'],'A')
    def test_empty_or_corrupt_is_not_cache(self):
        p=self.put('analysis.json',{'summary':'claimed success','visual_evidence':[]})
        self.assertIsNone(valid_analysis(p));p.write_text('{')
        self.assertIsNone(best_analysis(self.root))
    def test_failed_pipeline_enqueues_and_uses_fresh_evidence(self):
        import web_app as app
        from viral_elements import ELEMENT_DEFS
        source={'visual_evidence':[{'description':'A woman opening a shoe box in a bedroom'}]}
        job=app.ViralPipelineJob(id='cache-regression',filename='test.mp4')
        self.put('test.mp4',{})
        app.viral_pipeline_jobs[job.id]=job
        self.addCleanup(lambda:app.viral_pipeline_jobs.pop(job.id,None))
        with patch.object(app,'VIDEOS_DIR',self.root), patch.object(app,'_viral_pipeline_source',side_effect=[None,source]), patch.object(app,'video_queue') as queue, patch.object(app,'viral_element_store') as store, patch.object(app,'analyze_elements',return_value={'elements':[]}) as analyze, patch.object(app,'_sync_viral_review',return_value={'status':'disabled'}):
            store.get_review.return_value={'old':True}
            store.save_review.return_value={'new':True}
            queue.get_status.return_value='analyzed'
            app.run_viral_pipeline_job(job.id)
            queue.enqueue.assert_called_once_with('test.mp4','analyze')
            analyze.assert_called_once_with('test.mp4',source)
            self.assertEqual(job.status,'complete')

if __name__=='__main__':unittest.main()
