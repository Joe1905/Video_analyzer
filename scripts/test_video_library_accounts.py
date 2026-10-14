"""Manual library accounts and late pool linking; isolated DB, no paid API calls."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

import product_video_workspace as workspace
import proxy_pool
import video_review_inputs as inputs
from test_product_video_workspace import Handler
from test_proxy_pool_lifecycle import isolated_proxy_db

VID = '7655159328738954509'


class ManualAccountTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_proxy_db()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        for name, value in (('_initialized', False), ('ROOT', proxy_pool.DATA_DIR),
                            ('MEDIA', proxy_pool.DATA_DIR / 'media')):
            scoped = patch.object(workspace, name, value)
            scoped.start()
            self.addCleanup(scoped.stop)

    def add(self, value='@Test.Creator'):
        return workspace.add_account({'handle': value})['account']

    def pool_account(self, handle='TEST.CREATOR', bound=False):
        with workspace.database() as conn:
            pool_id = conn.execute("INSERT INTO proxy_profiles(name,created_at,updated_at) VALUES ('test','','')").lastrowid
            return conn.execute(
                "INSERT INTO tiktok_accounts(username,display_name,proxy_profile_id,proxy_bound,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (handle, 'Creator', pool_id, int(bound), '', '')).lastrowid

    def test_inputs_deduplicate_without_creating_pool_accounts(self):
        account = self.add()
        for value in ('test.creator', '@TEST.CREATOR', 'https://www.tiktok.com/@test.creator/?lang=en',
                      'tiktok.com/@test.creator', 'https://m.tiktok.com/@test.creator#profile'):
            self.assertEqual(self.add(value)['product_id'], account['product_id'])
        self.assertEqual(len(workspace.accounts()), 1)
        self.assertFalse(account['pool_linked'])
        with workspace.database() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM tiktok_accounts').fetchone()[0], 0)
        for value in ('', '@', '昵称', 'a' * 25, 'test.', 'a/b', 'https://evil.test/@test',
                      'https://www.tiktok.com.evil.test/@test', 'https://www.tiktok.com/@test/video/123',
                      'https://user:pass@www.tiktok.com/@test', 'https://vm.tiktok.com/abc', '<script>'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.add(value)

    def test_existing_pool_account_is_reused(self):
        account_id = self.pool_account()
        self.assertEqual(self.add()['product_id'], f'account:{account_id}')
        with workspace.database() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM video_library_accounts').fetchone()[0], 0)

    def test_late_link_unlink_and_relink_preserve_library_and_enforce_collection(self):
        account = self.add()
        pid = account['product_id']
        workspace.save_video(pid, {'video_id': VID, 'url': f'https://www.tiktok.com/@test.creator/video/{VID}', 'views': 17})
        with workspace.database() as conn:
            conn.execute('INSERT INTO account_video_pages VALUES (?,?,?)', (pid, 'saved-cursor', 1))
        payload = {'product_id': pid, 'video_id': VID, 'account_id': 999}
        self.assertFalse(inputs.input_state(workspace, pid, VID)['collection_allowed'])
        with patch.object(inputs.collect, 'create_job') as create:
            with self.assertRaisesRegex(ValueError, '尚未关联'):
                inputs.start_collection(workspace, payload)
            create.assert_not_called()
        account_id = self.pool_account()
        self.assertEqual(workspace.product(pid)['account_id'], account_id)
        self.assertEqual(len(workspace.accounts()), 1)
        self.assertFalse(inputs.input_state(workspace, pid, VID)['collection_allowed'], 'Pool binding still required')
        with workspace.database() as conn:
            conn.execute('UPDATE tiktok_accounts SET proxy_bound=1 WHERE id=?', (account_id,))
        self.assertTrue(inputs.input_state(workspace, pid, VID)['collection_allowed'])
        with patch.object(inputs.collect, 'create_job', return_value={}) as create:
            inputs.start_collection(workspace, payload)
            self.assertEqual(create.call_args.args[0]['account_id'], account_id)
            # Product-source videos resolve to the real pool ID too, never the manual library ID.
            self.assertEqual(inputs.collection_account(workspace, '123', workspace.item(pid, VID)), account_id)
        with workspace.database() as conn:
            conn.execute('UPDATE tiktok_accounts SET deleted_at=? WHERE id=?', ('removed', account_id))
        self.assertFalse(inputs.input_state(workspace, pid, VID)['collection_allowed'])
        replacement = self.pool_account('test.creator', bound=True)
        self.assertEqual(workspace.product(pid)['account_id'], replacement)
        state = workspace.snapshot(pid)
        self.assertEqual(state['videos'][0]['views'], 17)
        self.assertEqual(state['cursor'], 'saved-cursor')
        self.assertTrue(state['has_more'])
        self.assertEqual(len(workspace.accounts()), 1)
        with workspace.database() as conn:
            conn.execute('UPDATE tiktok_accounts SET profile_json=? WHERE id=?',
                         (json.dumps({'platform_deletions': {'tiktok': True}}), replacement))
        self.assertFalse(inputs.input_state(workspace, pid, VID)['collection_allowed'])

    def test_rendered_add_and_late_link(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/metrics', wait_until='networkidle')
                self.assertEqual(page.title(), '视频列表')
                page.get_by_role('button', name='手动添加账号', exact=True).click()
                page.locator('#accountHandle').fill('https://evil.test/@test')
                page.locator('#saveAccount').click()
                expect(page.locator('#accountEditorError')).to_be_visible()
                page.locator('#accountHandle').fill('https://www.tiktok.com/@Test.Creator?lang=en')
                page.locator('#saveAccount').click()
                expect(page.locator('#selectedMeta')).to_contain_text('未关联账号池')
                self.assertEqual(page.locator('.product-row').count(), 1)
                pid = workspace.accounts()[0]['product_id']
                workspace.save_video(pid, {'video_id': VID, 'title': 'Test video',
                                          'url': f'https://www.tiktok.com/@test.creator/video/{VID}'})
                page.locator('#reloadProducts').click()
                page.locator('[data-review-inputs]').click()
                expect(page.locator('#reviewCollectionAccount')).to_contain_text('尚未关联')
                expect(page.locator('#collectReviewRetention')).to_be_disabled()
                page.locator('[data-close="reviewInputsDialog"]').click()
                self.pool_account(bound=True)
                page.locator('#reloadProducts').click()
                expect(page.locator('#selectedMeta')).to_contain_text('已关联账号池')
                self.assertEqual(page.locator('.product-row').count(), 1)
                page.locator('[data-review-inputs]').click()
                expect(page.locator('#collectReviewRetention')).to_be_enabled()
                with tempfile.TemporaryDirectory() as folder:
                    page.screenshot(path=str(Path(folder) / 'manual-account-desktop.png'))
                    page.set_viewport_size({'width': 390, 'height': 844})
                    expect(page.locator('#collectReviewRetention')).to_be_visible()
                    page.screenshot(path=str(Path(folder) / 'manual-account-mobile.png'))
                self.assertEqual(errors, [])
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
