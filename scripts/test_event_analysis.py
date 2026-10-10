"""Regression checks for event provenance, corrections, caching and metric blindness."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from event_analysis import analyze_events, apply_checks, validate_events
from assisted_video_review import build_logic_evidence


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


if __name__ == '__main__':
    unittest.main()
