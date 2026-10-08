"""Native review orchestration checks; no paid API calls."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import native_video_review as review


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vid = '7683856958457253150'
        self.ws = SimpleNamespace(ROOT=self.root, MEDIA=self.root/'output/product_videos',
            item=Mock(return_value={'video_id':self.vid,'views':0}),
            snapshot=Mock(return_value={'job':None}), save_job=Mock(),
            prepare_media=Mock(), write_json=Mock())
        self.job = {'video_id':self.vid,'product_id':'account:24'}

    def test_zero_and_unknown_are_separate(self):
        context=review.library_context({'video_id':self.vid,'views':0})
        self.assertTrue(context['available'])
        self.assertEqual(context['overview']['play_count'],0)
        self.assertNotIn('likes',context['engagement'])
        self.assertNotIn('completion_rate',context['overview'])

    def test_membership_checked_before_report(self):
        self.ws.item.side_effect=ValueError('not associated')
        with patch.object(review,'saved_report') as saved:
            with self.assertRaises(ValueError):review.review_state(self.ws,'account:24',self.vid)
            saved.assert_not_called()

    def test_other_active_task_waits(self):
        self.ws.snapshot.return_value={'job':{'action':'audio','video_id':self.vid,'status':'running'}}
        with patch.object(review,'saved_report',return_value=None):
            self.assertEqual(review.review_state(self.ws,'account:24',self.vid)['status'],'waiting')

    def test_cached_report_does_not_start_pipeline(self):
        with patch.object(review,'saved_report',return_value={'summary':'cached'}),patch.object(review.subprocess,'run') as run:
            review.run_review(self.ws,self.job)
        run.assert_not_called();self.ws.prepare_media.assert_not_called()
        self.assertEqual(self.job['done'],1)

    def test_saved_extraction_reused_for_existing_pipeline(self):
        media=self.root/'videos'/review.filename(self.vid)
        media.parent.mkdir();media.write_bytes(b'video')
        with patch.object(review,'saved_report',side_effect=[None,{'summary':'done'}]), \
             patch.object(review,'valid_analysis',return_value=True),patch.object(review.subprocess,'run') as run:
            review.run_review(self.ws,self.job)
        self.assertEqual(run.call_count,1)
        args=run.call_args.args[0]
        self.assertTrue(args[1].endswith('deepseek_postprocess.py'))
        self.assertIn('--performance-context',args)
        self.ws.prepare_media.assert_not_called()
        self.assertEqual(self.job['done'],1)


if __name__=='__main__':unittest.main()
