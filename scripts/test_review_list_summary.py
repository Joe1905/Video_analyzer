"""List summaries must reflect saved reports without leaking full evidence payloads."""
import json
import tempfile
import unittest
from pathlib import Path

from web_app import review_list_summary


class ReviewSummaryTests(unittest.TestCase):
    def test_saved_report_zero_metrics_and_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'audit_result.json'
            report = {'summary': 'First review', 'raw_result': {'secret': 'DO_NOT_RETURN'},
                      '采集数据来源': {'overview': {'play_count': '0', 'completion_rate': '0%'}}}
            path.write_text(json.dumps(report), encoding='utf-8')
            result = review_list_summary(root)
            self.assertTrue(result['review_available'])
            self.assertEqual(result['review_plays'], '0')
            self.assertEqual(result['review_completion'], '0%')
            self.assertNotIn('DO_NOT_RETURN', json.dumps(result))
            report['summary'] = 'Replacement review with new data'
            other = root / 'new.json'; other.write_text(json.dumps(report), encoding='utf-8'); other.replace(path)
            self.assertEqual(review_list_summary(root)['review_summary'], report['summary'])

    def test_missing_corrupt_report_and_direct_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(review_list_summary(root)['review_available'])
            (root / 'audit_result.json').write_text('{bad', encoding='utf-8')
            self.assertFalse(review_list_summary(root)['review_available'])
            (root / 'direct_audit_result.json').write_text(json.dumps({'summary': 'Direct review'}), encoding='utf-8')
            self.assertEqual(review_list_summary(root)['review_source'], 'direct')


if __name__ == '__main__':
    unittest.main()
