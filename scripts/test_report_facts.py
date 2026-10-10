"""Regression cases from report review; no API calls."""
import unittest

from video_performance_context import build_report_facts
from deepseek_postprocess import build_prompt


class ReportFactsTests(unittest.TestCase):
    def facts(self, retention, **extra):
        return build_report_facts({}, {"available": True, "retention": retention, **extra})

    def test_actual_early_drop_and_decimal_precision(self):
        result = self.facts({"0:03": "24%", "0:00": "100%", "0:02": "32%", "0:01": "58%"})["retention"]
        self.assertEqual(result["first_three_seconds_drop_pp"], 76)
        self.assertEqual(result["largest_observed_drops"][0]["end_seconds"], 1)
        self.assertEqual(result["largest_observed_drops"][0]["drop_percentage_points"], 42)
        result = self.facts({"0:00": "16.3%", "0:01": "12.1%"})["retention"]
        self.assertEqual(result["adjacent_second_changes"][0]["drop_percentage_points"], 4.2)

    def test_gaps_invalid_units_and_duplicate_time_are_not_filled(self):
        result = self.facts({"0:00": "100%", "0:02": "32%", "0:03": "24%", "0:04": "NaN%",
                             "0:05": "101%", "0:06": 0.5, "0:60": "5%", "00:02": "30%"})["retention"]
        self.assertIsNone(result["first_three_seconds_drop_pp"])
        self.assertEqual(result["adjacent_second_changes"], [])
        self.assertEqual(result["duplicate_seconds"], [2])
        self.assertEqual(len(result["invalid_labels"]), 4)
        self.assertEqual(result["gaps"], [{"start_seconds": 0, "end_seconds": 3}])

    def test_zero_rise_and_tied_drops(self):
        result = self.facts({"0:00": "4%", "0:01": "2%", "0:02": "0%", "0:03": "1%"})["retention"]
        self.assertEqual(len(result["largest_observed_drops"]), 2)
        self.assertEqual(result["adjacent_second_changes"][-1]["drop_percentage_points"], -1)
        self.assertEqual(result["points"][2]["percent"], 0)
        self.assertIsNone(result["denominator"])

    def test_search_queries_do_not_establish_traffic_sources(self):
        result = self.facts({}, search_queries={"toy": "33%"}, traffic_sources_available=False)
        self.assertEqual(result["availability"]["traffic_sources"], "unavailable")
        self.assertEqual(result["availability"]["search_queries"], "available")
        self.assertEqual(result["availability"]["orders"], "unknown")
        result = build_report_facts({}, {"available": False, "retention": {"0:00": "100%"}})
        self.assertEqual(result["retention"]["points"], [])

    def test_capture_time_not_frame_index_and_unverified_excluded(self):
        frames = [{"evidence_id": "frame_2", "time_source": "capture", "timestamp_seconds": 1},
                  {"evidence_id": "frame_1", "time_source": "capture", "timestamp_seconds": 0.5},
                  {"evidence_id": "old", "time_source": "model_reported_unverified", "timestamp_seconds": 100},
                  {"evidence_id": "invalid", "time_source": "capture", "timestamp_seconds": float("nan")}]
        result = build_report_facts({"timeline": frames}, {})
        self.assertEqual(result["frame_time_map"][0], {"evidence_id": "frame_2", "seconds": 1})
        self.assertEqual(result["max_observed_frame_gap_seconds"], 0.5)
        self.assertEqual(result["unverified_frame_ids"], ["old", "invalid"])
        self.assertEqual(result["motion_from_sampling_interval"], "unknown")

    def test_facts_reach_actual_prompt(self):
        prompt = build_prompt({}, performance={"available": True, "retention": {"0:00": "100%", "0:01": "58%"}})
        self.assertIn('"drop_percentage_points": 42.0', prompt)
        self.assertIn('"traffic_sources": "unknown"', prompt)
        self.assertIn("搜索词占比不代表搜索流量占比", prompt)


if __name__ == "__main__":
    unittest.main()
