"""Optional inputs and targeted Proxy jobs; isolated DB, no network or browser jobs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import product_video_workspace as workspace
import proxy_pool
import tiktok_studio_collect as collect
import video_review_inputs as inputs
from native_video_review import needs_collection_refresh
from video_performance_context import load_performance_context
from test_proxy_pool_lifecycle import isolated_proxy_db, create_manual_pool

VID='7655159328738954509'
PID='1732457689950426021'


class InputTests(unittest.TestCase):
    def setUp(self):
        scope=isolated_proxy_db();scope.__enter__();self.addCleanup(scope.__exit__,None,None,None)
        init=patch.object(workspace,'_initialized',False);init.start();self.addCleanup(init.stop)
        member=patch.object(workspace,'item',return_value={'video_id':VID});member.start();self.addCleanup(member.stop)
        self.root=proxy_pool.DATA_DIR.parent
        self.payload={'product_id':PID,'video_id':VID}

    def test_orders_zero_unknown_and_cross_membership(self):
        result=inputs.save_orders(workspace,dict(self.payload,count=0))
        self.assertEqual(result['manual_orders']['count'],0)
        self.assertEqual(result['manual_orders']['scope'],'video_cumulative')
        with workspace.database() as conn:
            self.assertEqual(json.loads(conn.execute('SELECT payload FROM video_manual_orders').fetchone()[0]),result['manual_orders'])
        inputs.save_orders(workspace,dict(self.payload,count=None))
        with workspace.database() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM video_manual_orders').fetchone()[0],0)
        with patch.object(workspace,'item',side_effect=ValueError('not a member')):
            with self.assertRaises(ValueError):inputs.save_orders(workspace,dict(self.payload,count=3))

    def test_reject_invalid_orders(self):
        for value in (-1,1.1,True,'2',1000000001):
            with self.assertRaises(ValueError):inputs.save_orders(workspace,dict(self.payload,count=value))

    def test_manual_orders_loaded_without_retention_and_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            with patch.object(proxy_pool,'DB_PATH',root/'data/proxy_pool.sqlite'),patch.object(proxy_pool,'DATA_DIR',root/'data'):
                value=inputs.save_orders(workspace,dict(self.payload,count=4))['manual_orders']
                with patch.dict('os.environ',{'REVIEW_COLLECTION_DB':''}):current=load_performance_context(root,VID)
                self.assertEqual(current['manual_orders'],value)
                self.assertTrue(needs_collection_refresh({'采集数据来源':{}},current))
                self.assertFalse(needs_collection_refresh({'采集数据来源':current},current))
                self.assertTrue(needs_collection_refresh({'采集数据来源':current},{'manual_orders':None}))

    def account(self,bound=True):
        pool=create_manual_pool('test','203.0.113.20')
        with proxy_pool.connect() as conn:
            now=proxy_pool.now_iso()
            return conn.execute('INSERT INTO tiktok_accounts(username,proxy_profile_id,proxy_bound,created_at,updated_at) VALUES (?,?,?,?,?)',('test',pool['id'],int(bound),now,now)).lastrowid

    def test_collection_uses_existing_creator_and_persists_target(self):
        account=self.account()
        target={'appToken':'a','tableId':'b'}
        with patch.object(collect,'_validate_feishu_target',return_value=target) as validate_target:
            result=inputs.start_collection(workspace,dict(self.payload,account_id=account,feishu_target=target,publish_date_start='2026-07-01',publish_date_end='2026-07-31'))
        validate_target.assert_called_once_with(target)
        self.assertEqual(result['job']['target_video_id'],VID)
        self.assertEqual(result['job']['status'],'queued')
        self.assertTrue(result['job']['auto_sync'])
        self.assertEqual(result['job']['account_id'],account)

    def test_unbound_account_cannot_queue(self):
        account=self.account(False)
        with patch.object(collect,'_validate_feishu_target') as target:
            with self.assertRaisesRegex(ValueError,'未绑定'):inputs.start_collection(workspace,dict(self.payload,account_id=account))
        target.assert_not_called()
        with proxy_pool.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM collect_jobs').fetchone()[0],0)

    def test_target_scope_never_collects_other_videos(self):
        links=[{'id':VID},{'id':'7655510284928290061'}]
        self.assertEqual(collect._target_video_links({'target_video_id':VID},links),links[:1])
        self.assertEqual(collect._target_video_links({},links),links)
        with self.assertRaisesRegex(ValueError,'不会采集其他视频'):collect._target_video_links({'target_video_id':'7655510284928290099'},links)

    def test_invalid_write_target_cannot_queue(self):
        account=self.account()
        with patch.object(collect,'list_feishu_targets',return_value={'targets':[]}):
            with self.assertRaisesRegex(ValueError,'白名单'):inputs.start_collection(workspace,dict(self.payload,account_id=account,feishu_target={'appToken':'a','tableId':'b'}))
        with proxy_pool.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM collect_jobs').fetchone()[0],0)

    def test_wrong_account_for_account_library_is_rejected(self):
        with patch.object(collect,'create_job') as creator:
            with self.assertRaises(ValueError):inputs.start_collection(workspace,dict(self.payload,product_id='account:4',account_id=5))
        creator.assert_not_called()


if __name__=='__main__':unittest.main()
