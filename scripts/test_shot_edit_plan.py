"""Checks for joint image evidence and executable edit plans, without paid calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shot_analysis import analyze_shots, validate_events
from video_performance_context import validate_edit_plan
import vision_provider


class ShotPlanTests(unittest.TestCase):
    def fixture(self):
        analysis = {"metadata": {"duration_seconds": 6}, "shot_evidence": {"shots": [
            {"id": "shot_0", "start": 0, "end": 6}]}}
        shots = [{"start": n, "end": n+2, "画面": "玩具", "动作": "手持", "景别机位": "近景",
            "台词原文": "Watch it glow.", "中文含义": "看它发光", "字幕原文": "Watch it glow.",
            "素材来源": "复用", "原片区间": [{"start": n, "end": n+2, "shot_id": "shot_0"}]}
            for n in (0, 2, 4)]
        report = {"原片改剪表": [{"start": 0, "end": 2, "操作": "前移", "具体改法": "前置效果", "理由": "测试开头"}],
            "新版分镜脚本": {"目标时长": 6, "主要验证变量": "开头画面", "验证指标": "首秒留存", "镜头列表": shots}}
        return analysis, report

    def test_valid_plan_and_reshoot(self):
        analysis, report = self.fixture()
        validate_edit_plan(report, analysis)
        shot = report["新版分镜脚本"]["镜头列表"][0]
        shot.update(素材来源="补拍", 原片区间=[])
        validate_edit_plan(report, analysis)

    def test_reject_impossible_english_voiceover(self):
        analysis, report = self.fixture()
        analysis["transcript"] = {"language": "en"}
        report["新版分镜脚本"]["镜头列表"][0]["台词原文"] = "word " * 20
        with self.assertRaisesRegex(ValueError, "口播过长"):
            validate_edit_plan(report, analysis)

    def test_composite_edit_and_millisecond_rounding(self):
        analysis, report = self.fixture()
        report["原片改剪表"][0]["操作"] = "前移/替换"
        analysis["shot_evidence"]["shots"][0]["start"] = .000333
        validate_edit_plan(report, analysis)
        report["新版分镜脚本"]["镜头列表"][0]["原片区间"][0]["start"] = -.1
        with self.assertRaises(ValueError):
            validate_edit_plan(report, analysis)

    def test_reject_gap_wrong_source_duration_and_missing_copy(self):
        analysis, base = self.fixture()
        for mutation in (lambda s: s.update(start=.5), lambda s: s["原片区间"][0].update(shot_id="invented"),
                         lambda s: s["原片区间"][0].update(end=7),
                         lambda s: s["原片区间"][0].update(end=1), lambda s: s.pop("台词原文")):
            report = copy.deepcopy(base)
            mutation(report["新版分镜脚本"]["镜头列表"][0])
            with self.assertRaises(ValueError):
                validate_edit_plan(report, analysis)

    def test_joint_frames_bounded_cache_reused_and_cut_ranges_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "frames").mkdir()
            video = root / "video.mp4"; video.write_bytes(b"test-video")
            rows = []
            for i in range(20):
                (root / "frames" / f"frame_{i}.jpg").write_bytes(b"image")
                rows.append({"evidence_id": f"frame_{i}", "time_source": "capture", "timestamp_seconds": i/2})
            analysis = {"metadata": {"duration_seconds": 10}, "timeline": rows}
            def model(prompt, images):
                self.assertLessEqual(len(images), 12)
                return {"shots": [{"id": "shot_0", "visual": "toy", "action": "uncertain",
                                  "camera": "close", "function": "demo", "uncertainties": [],
                                  "events": [{'kind':'object_visible','subject':'toy','frame_id':'frame_0','evidence':'visible'}]}]}
            callback = Mock(side_effect=model)
            result = analyze_shots(analysis, root, video, model=callback, cut_detector=lambda _: [])
            self.assertEqual(result["shots"][0]["end"], 10)
            self.assertEqual(len(result["shots"][0]["inspected_frame_ids"]), 12)
            self.assertEqual(len(result["shots"][0]["frame_ids"]), 20)
            analyze_shots(analysis, root, video, model=callback, cut_detector=lambda _: [])
            self.assertEqual(callback.call_count, 1)

    def test_event_timestamps_use_capture_and_different_kinds_remain_separate(self):
        frames=[{'evidence_id':'early','timestamp_seconds':4.8},{'evidence_id':'late','timestamp_seconds':15.2}]
        events=[{'kind':'object_visible','subject':'pink toy','frame_id':'early','evidence':'on bag','timestamp_seconds':15},
                {'kind':'close_up','subject':'blue toy','frame_id':'late','evidence':'close view'}]
        result=validate_events(events,frames)
        self.assertEqual([e['timestamp_seconds'] for e in result],[4.8,15.2])
        self.assertEqual([e['kind'] for e in result],['object_visible','close_up'])
        events[0]['frame_id']='invented'
        with self.assertRaises(ValueError):validate_events(events,frames)

    def test_multiple_images_use_same_route_and_disabled_thinking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.jpg"; path.write_bytes(b"image")
            response = Mock(ok=True)
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": '{}'}}]}
            cfg = {"provider": "deepseek", "model": "test", "api_key": "test", "api_url": "https://example.com"}
            with patch.object(vision_provider, "frame_config", return_value=cfg), \
                 patch.object(vision_provider.requests, "post", return_value=response) as request:
                vision_provider.recognize_image(prompt="test", image_paths=[("0秒", path), ("1秒", path)])
            body = request.call_args.kwargs["json"]
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual(sum(x["type"] == "image_url" for x in body["messages"][0]["content"]), 2)


if __name__ == "__main__":
    unittest.main()
