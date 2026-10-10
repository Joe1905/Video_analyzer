import tempfile
import unittest
from pathlib import Path
import cv2
import numpy as np
from coverage_sampler import extract, coverage_indices


class CoverageTests(unittest.TestCase):
    def video(self, root, seconds, flashing=False):
        path = root / 'fixture.avi'
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 20, (160, 90))
        self.assertTrue(writer.isOpened())
        for i in range(seconds * 20):
            level = (220 if (i//5) % 2 else 20) if flashing and i < 60 else 100
            writer.write(np.full((90,160,3), level, dtype=np.uint8))
        writer.release()
        return path

    def test_linear_count_past_old_twenty_cap_and_short_video(self):
        self.assertGreater(len(coverage_indices(1200,20)), len(coverage_indices(600,20)))
        self.assertGreater(len(coverage_indices(600,20)),20)
        self.assertEqual(coverage_indices(1,20),[0])

    def test_static_video_keeps_endpoints_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); result=extract(self.video(root,12),root/'frames')
            self.assertTrue(result['coverage_ok'])
            self.assertEqual(result['first_timestamp'],0)
            self.assertAlmostEqual(result['last_timestamp'],11.95)
            self.assertLessEqual(result['max_gap_seconds'],1.05)
            self.assertEqual(result['selected_count'],result['baseline_count'])

    def test_flashing_opening_cannot_crowd_out_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); result=extract(self.video(root,12,True),root/'frames')
            self.assertTrue(result['coverage_ok'])
            self.assertGreater(result['selected_count'],result['baseline_count'])
            self.assertLessEqual(result['selected_count'],result['baseline_count']+result['extra_budget'])
            self.assertTrue(any(x['timestamp_seconds']>11 for x in result['frames']))

    def test_budget_refuses_silent_coverage_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaisesRegex(ValueError,'Coverage needs'):
                extract(self.video(root,12),root/'frames',hard_limit=3)

    def test_slow_cumulative_change_can_add_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'slow.avi'
            writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),20,(160,90))
            for i in range(160):
                writer.write(np.full((90,160,3),i,dtype=np.uint8))
            writer.release()
            result=extract(path,root/'frames')
            self.assertGreater(result['selected_count'],result['baseline_count'])
            bonus=[x for x in result['frames'] if x['reason']=='difference_bonus']
            self.assertTrue(all(x['adjacent_difference']<=10 for x in bonus))


if __name__=='__main__': unittest.main()
