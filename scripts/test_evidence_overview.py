"""Verify script reconstruction preserves evidence and bypasses the model."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import frame_vision_analyze as wrapper
from evidence_overview import build_video_description
from standardize_analysis import standardize_analyzer
from deepseek_postprocess import compact_analysis


class OverviewTests(unittest.TestCase):
    def frames(self):
        return [{"timestamp": t, "response": json.dumps({"visual": v, "visible_text": [],
                 "uncertainties": ["无法确认运动"]})} for t, v in [(0, "toy"), (0.5, "toy"), (1, "hand")]]

    def test_exact_grouping_and_asr_overlap_not_inference(self):
        result = json.loads(build_video_description(self.frames(), {"segments": [
            {"start": 0.4, "end": 0.8, "text": "hello"}, {"start": 2, "end": 3}]})["response"])
        overview = result["evidence_overview"]
        self.assertEqual(overview["frame_groups"][0]["frame_ids"], ["frame_0", "frame_1"])
        self.assertEqual(overview["frame_groups"][0]["last_sample_seconds"], 0.5)
        self.assertEqual(overview["transcript_segment_index"][0]["sampled_frame_ids"], ["frame_1"])
        self.assertEqual(overview["transcript_segment_index"][1]["sampled_frame_ids"], [])

    def test_invalid_frames_not_merged_across_gap(self):
        frames = self.frames()
        frames[1]["response"] = '{"visual":'
        frames[2]["response"] = frames[0]["response"]
        overview = json.loads(build_video_description(frames)["response"])["evidence_overview"]
        self.assertEqual(len(overview["frame_groups"]), 2)
        self.assertEqual(len(overview["evidence_issues"]), 1)

    def test_standardization_counts_only_frame_calls_and_keeps_transcript(self):
        raw = {"frame_analyses": self.frames(), "transcript": {"segments": [{"start": 0, "end": 1, "text": "hi"}]}}
        raw["video_description"] = build_video_description(raw["frame_analyses"], raw["transcript"])
        with tempfile.TemporaryDirectory() as directory:
            result = standardize_analyzer(raw, Path(directory), 1)
        self.assertEqual(result["usage"]["api_calls"], 3)
        self.assertEqual(result["metadata"]["summary_source"], "script")
        self.assertEqual(result["transcript"]["segments"], raw["transcript"]["segments"])
        self.assertEqual(compact_analysis(result)["evidence_overview"], result["evidence_overview"])

    def test_actual_wrapper_reconstruction_cannot_call_model(self):
        from video_analyzer import cli
        originals = {k: getattr(cli, k) for k in ("Config", "VideoProcessor", "VideoAnalyzer", "GenericOpenAIAPIClient")}
        def inspect_reconstruction():
            analyzer = cli.VideoAnalyzer.__new__(cli.VideoAnalyzer)
            with patch.object(wrapper, "recognize_image", side_effect=AssertionError("unexpected model call")):
                result = analyzer.reconstruct_video(self.frames(), [], argparse.Namespace(segments=[]))
            self.assertEqual(result["processing_source"], "script")
        try:
            with patch.object(wrapper, "frame_config", return_value={"provider": "deepseek", "model": "test",
                 "api_key": "test", "api_url": "http://unused"}), patch.object(wrapper.sys, "argv", ["script"]), \
                 patch.object(cli, "main", side_effect=inspect_reconstruction):
                wrapper.main()
        finally:
            for key, value in originals.items():
                setattr(cli, key, value)


if __name__ == "__main__":
    unittest.main()
