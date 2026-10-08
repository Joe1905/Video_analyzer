"""Regression checks for truncated responses, timestamps and evidence preservation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import deepseek_postprocess as post
import frame_vision_analyze as frames
import vision_provider as provider
from standardize_analysis import frame_evidence, standardize_analyzer
from visual_analysis_cache import valid_analysis


class EvidenceTests(unittest.TestCase):
    def test_capture_time_wins_over_model_time_including_zero(self):
        timeline, issues = frame_evidence([{"timestamp": 0.0, "response": json.dumps({
            "timeline": [{"time_range": "99-100", "visual": "Red toy"}]})}])
        self.assertEqual(timeline[0]["timestamp_seconds"], 0.0)
        self.assertEqual(timeline[0]["time_range"], "0.0")
        self.assertEqual(timeline[0]["time_source"], "capture")
        self.assertFalse(issues)

    def test_partial_json_is_excluded_not_repaired(self):
        rows, issues = frame_evidence([
            {"response": '```json\n{"timeline":[{"visual":"toy","time_range":"1.5"}]}\n```'},
            {"response": '{"visual":"invented tail'},
            {"response": 'Error analyzing frame: request failed'}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["time_source"], "model_reported_unverified")
        self.assertEqual(len(issues), 2)

    def test_legacy_repack_preserves_segments_and_full_visual(self):
        raw = {"metadata": {"audio_language": "en"}, "video_description": {"response": '{"summary":"Toy"}'},
               "frame_analyses": [{"timestamp": 2.5, "response": json.dumps({"visual": "v" * 1400})}],
               "transcript": {"text": "Look", "segments": [{"start": 2, "end": 3, "text": "Look", "words": [1]}]}}
        source = {"processing_mode": "analyzer", "metadata": {"duration_seconds": 10}, "raw_model_output": raw}
        compact = post.compact_analysis(source)
        self.assertEqual(compact["transcript"]["segments"], [{"start": 2, "end": 3, "text": "Look"}])
        self.assertEqual(len(compact["timeline"][0]["visual"]), 1400)
        self.assertEqual(compact["summary"], "Toy")
        self.assertEqual(compact["metadata"]["duration_seconds"], 10)
        self.assertEqual(compact["visual_evidence"], [])
        self.assertNotIn("evidence_version", source["metadata"])

    def test_partial_extraction_is_not_cache_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = standardize_analyzer({"frame_analyses": [{"response": '{"visual":"ok"}'},
                {"response": '{"visual":'}], "video_description": {"response": '{"summary":"ok"}'}}, root, 1)
            self.assertEqual(result["metadata"]["extraction_quality"], "partial")
            self.assertEqual(result["summary"], "")
            path = root / "analysis.json"
            path.write_text(json.dumps(result))
            self.assertIsNone(valid_analysis(path))
            self.assertEqual(len(result["raw_model_output"]["frame_analyses"]), 2)

    def test_frame_budget_and_invalid_response_abort(self):
        with patch.object(frames, "recognize_image", return_value='{"visual":"toy"}') as recognize:
            frames.generate("frame", "image.png", num_predict=300)
            self.assertGreaterEqual(recognize.call_args.kwargs["max_tokens"], 2048)
        with patch.object(frames, "recognize_image", return_value='{"visual":'):
            with self.assertRaises(SystemExit):
                frames.generate("frame", "image.png")

    def test_finish_length_rejected_even_if_json_parses(self):
        response = Mock(ok=True)
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": '{"visual":"toy"}'}}]}
        config = {"provider": "deepseek", "model": "test", "api_key": "test", "api_url": "http://unused"}
        with patch.object(provider, "frame_config", return_value=config), patch.object(provider.requests, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "length"):
                provider.recognize_image(prompt="test")


if __name__ == "__main__":
    unittest.main()
