import unittest
from viral_feishu_sync import review_fields, script_fields

class PersonFieldsTest(unittest.TestCase):
    def test_empty_reviewer_is_empty_list(self):
        for fields, key in ((review_fields({}, '刘鹏飞', 'ou_test'), '负责人'),
                            (script_fields({}, owner='刘鹏飞', owner_id='ou_test'), '脚本负责人')):
            self.assertEqual(fields[key], [{'id':'ou_test'}])
            self.assertEqual(fields['审核人'], [])

    def test_missing_id_never_sends_name(self):
        self.assertEqual(review_fields({}, '刘鹏飞')['负责人'], [])

    def test_confirmed_reviewer_uses_id(self):
        self.assertEqual(review_fields({'reviewer':'刘鹏飞'}, '刘鹏飞', 'ou_test')['审核人'], [{'id':'ou_test'}])

if __name__ == '__main__': unittest.main()
