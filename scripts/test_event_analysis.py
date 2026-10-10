"""Regression checks for event provenance, corrections, caching and metric blindness."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from event_analysis import analyze_events, apply_checks, validate_events, observed_events, supplement_frames
from assisted_video_review import build_logic_evidence, retention_prompt


class EventTests(unittest.TestCase):
    def setUp(self):
        self.frames = [{'evidence_id': f'frame_{i}', 'timestamp_seconds': i,
                        'time_source': 'capture', 'visual': '未经核验的插入动作'} for i in range(3)]
        self.event = {'id': 'paper', 'frame_ids': ['frame_2', 'frame_0'], 'subject': '白纸',
                      'observed': '插入设备', 'intended_meaning': '扫描纸张', 'uncertainties': []}

    def test_capture_times_and_foreign_frames(self):
        events = validate_events({'events': [self.event]}, self.frames)
        self.assertEqual((events[0]['start'], events[0]['end']), (0, 2))
        self.assertEqual(events[0]['frame_ids'], ['frame_0', 'frame_2'])
        bad = dict(self.event, frame_ids=['frame_99'])
        with self.assertRaises(ValueError): validate_events({'events': [bad]}, self.frames)

    def test_contradiction_replaces_observation_and_drops_inference(self):
        events = validate_events({'events': [self.event]}, self.frames)
        check = {'id': 'paper', 'status': 'contradicted', 'observed': '纸张向上移动后被取走', 'reason': '前后位置与手部变化'}
        result = apply_checks(events, {'checks': [check]})[0]
        self.assertEqual(result['observed'], check['observed'])
        self.assertEqual(result['intended_meaning'], '')
        self.assertEqual(result['proposed_observed'], '插入设备')
        revised = apply_checks(events, {'checks': [dict(check, uncertainties=['真实商品能力未核实'])]})[0]
        self.assertEqual(revised['uncertainties'], ['真实商品能力未核实'])
        self.assertNotIn('proposed_observed', observed_events({'events': [result]})[0])
        self.assertNotIn('intended_meaning', observed_events({'events': [dict(result, verification='supported', intended_meaning='假设的扫描功能')]})[0])
        for bad in ({'checks': []}, {'checks': [dict(check, id='other')]}, {'checks': [dict(check, status='sure')]}):
            with self.assertRaises(ValueError): apply_checks(events, bad)

    def test_cross_cut_context_cache_and_image_changes(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name); (folder/'frames').mkdir()
            for frame in self.frames: (folder/'frames'/(frame['evidence_id']+'.jpg')).write_bytes(b'image')
            checked = {'id': 'paper', 'status': 'uncertain', 'observed': '纸张先在插槽处，随后位于手中', 'reason': '缺少连续过程'}
            model = Mock(side_effect=[{'events': [self.event]}, {'checks': [checked]}]*2)
            analysis = {'metadata': {'duration_seconds': 3}, 'timeline': self.frames}
            result = analyze_events(analysis, folder, model)
            self.assertNotIn('插入设备',model.call_args_list[1].args[0])
            self.assertNotIn('扫描纸张',model.call_args_list[1].args[0])
            self.assertEqual(result['inspected_frame_ids'], ['frame_0', 'frame_1', 'frame_2'])
            self.assertEqual(model.call_count, 2)
            self.assertEqual(analyze_events(analysis, folder, model), result)
            self.assertEqual(model.call_count, 2)
            (folder/'frames/frame_1.jpg').write_bytes(b'changed')
            analyze_events(analysis, folder, model)
            self.assertEqual(model.call_count, 4)

    def test_logic_has_no_metrics_or_unverified_actions(self):
        evidence = build_logic_evidence({'metadata': {'duration_seconds': 3}, 'timeline': self.frames,
            'transcript': {'segments': []}, 'performance': {'views': 145000, 'retention': {'0:01': '85%'}}})
        serialized = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn('145000', serialized)
        self.assertNotIn('85%', serialized)
        self.assertNotIn('未经核验的插入动作', serialized)
        self.assertEqual(evidence['retention_points'], [])
        self.assertEqual(evidence['retention_windows'], [])

    def test_real_video_gap_frames_preserve_originals_and_actual_timestamps(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);(root/'videos').mkdir();folder=root/'output/clip.mp4';(folder/'frames').mkdir(parents=True)
            writer=cv2.VideoWriter(str(root/'videos/clip.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),10,(32,32))
            self.assertTrue(writer.isOpened())
            for i in range(20):writer.write(np.full((32,32,3),i*10,dtype=np.uint8))
            writer.release()
            frames=[dict(self.frames[i],timestamp_seconds=t) for i,t in enumerate([0,1,1.9])]
            for f in frames:(folder/'frames'/(f['evidence_id']+'.jpg')).write_bytes(b'original')
            extra=supplement_frames(frames,folder,limit=9)
            self.assertTrue(any(f['timestamp_seconds']==.5 for f in extra))
            self.assertTrue(any(f['timestamp_seconds']==.8 for f in extra))
            self.assertLessEqual(len(extra),6)
            self.assertTrue(all(Path(f['_image_path']).is_file() for f in extra))
            self.assertTrue(all((folder/'frames'/(f['evidence_id']+'.jpg')).read_bytes()==b'original' for f in frames))
            self.assertEqual(supplement_frames(frames,folder,limit=3),[])

    def test_later_event_not_exposed_to_earlier_retention_window(self):
        evidence = {'timeline': [], 'retention_windows': [{'id': 'r0', 'before': [], 'during': [], 'after': [], 'end_seconds': 2}]}
        prompt = retention_prompt(evidence, {'推进': []}, {}, [
            {'end': 1, 'observed': '前面的观察'}, {'end': 3, 'observed': '后续结果不能解释此前流失'}])
        self.assertIn('前面的观察', prompt)
        self.assertNotIn('后续结果不能解释此前流失', prompt)
        ongoing=retention_prompt(evidence, {'推进': []}, {}, [{'start':.5,'end':2.25,'observed':'跨窗擦拭'}])
        self.assertNotIn('跨窗擦拭',ongoing)

    def test_retention_with_verified_events_excludes_old_frame_and_logic_guesses(self):
        evidence={'timeline':[{'id':'t0','start':0,'end':1,'visuals':[
            {'frame_id':'frame_0','seconds':0,'text':'旧错误的共同绘画'}],'speech':[]}],
            'retention_windows':[{'id':'r0','before':[],'during':['t0'],'after':[],'end_seconds':1}]}
        prompt=retention_prompt(evidence,{'推进':[{'依据':['t0'],'逻辑作用':'错误的补救动机'}]}, {},
            [{'end':1,'observed':'成人擦拭画纸'}])
        self.assertIn('成人擦拭画纸',prompt)
        self.assertNotIn('旧错误的共同绘画',prompt)
        self.assertNotIn('错误的补救动机',prompt)
        prompt=retention_prompt(evidence,{'推进':[]},{},[{'start':0,'end':2,'observed':'未来按键'}],
            [{'frame_id':'frame_0','observed':'窗口内正在注视设备'}])
        self.assertIn('窗口内正在注视设备',prompt)
        self.assertNotIn('未来按键',prompt)


if __name__ == '__main__':
    unittest.main()
