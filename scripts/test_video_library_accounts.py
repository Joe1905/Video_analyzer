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

    def test_legacy_manual_accounts_migrate_without_loss(self):
        with proxy_pool.connect() as conn:
            conn.execute('CREATE TABLE video_library_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT NOT NULL UNIQUE)')
            conn.execute("INSERT INTO video_library_accounts(handle) VALUES ('yakuza0.3')")
        account = self.add('yakuza0.3')
        self.assertEqual(account['product_id'], 'account:manual:1')
        workspace.delete_account({'product_id': account['product_id']})
        self.assertEqual(workspace.accounts(), [])

    def test_delete_and_restore_preserve_saved_data(self):
        account = self.add('yakuza0.3')
        pid = account['product_id']
        workspace.save_video(pid, {'video_id': VID, 'views': 17})
        inputs.save_orders(workspace, {'product_id': pid, 'video_id': VID, 'count': 4})
        with workspace.database() as conn:
            conn.execute('INSERT INTO account_video_pages VALUES (?,?,?)', (pid, 'saved-cursor', 1))
        workspace.delete_account({'product_id': pid})
        workspace.delete_account({'product_id': pid})  # Idempotent repeated requests.
        self.assertEqual(workspace.accounts(), [])
        with self.assertRaises(ValueError):
            workspace.product(pid)
        self.assertEqual(self.add('@YAKUZA0.3')['product_id'], pid)
        state = workspace.snapshot(pid)
        self.assertEqual(state['videos'][0]['views'], 17)
        self.assertEqual(state['cursor'], 'saved-cursor')
        self.assertTrue(state['has_more'])
        self.assertEqual(inputs.input_state(workspace, pid, VID)['manual_orders']['count'], 4)
        self.assertEqual(len(workspace.accounts()), 1)

    def test_delete_linked_manual_account_preserves_pool(self):
        manual = self.add()
        pool_id = self.pool_account(bound=True)
        workspace.delete_account({'product_id': manual['product_id']})
        self.assertEqual(workspace.accounts()[0]['product_id'], f'account:{pool_id}')
        with workspace.database() as conn:
            row = conn.execute('SELECT deleted_at,proxy_bound FROM tiktok_accounts WHERE id=?', (pool_id,)).fetchone()
            self.assertEqual(tuple(row), ('', 1))
        self.assertEqual(self.add()['product_id'], manual['product_id'])
        self.assertEqual(workspace.product(manual['product_id'])['account_id'], pool_id)

    def test_delete_rejects_invalid_ids_pool_accounts_and_busy_jobs(self):
        self.pool_account()
        for pid in ('account:1', '123', 'account:manual:../../test', 'account:manual:999999'):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                workspace.delete_account({'product_id': pid})
        manual = self.add('yakuza0.3')
        pid = manual['product_id']
        workspace.save_job({'product_id': pid, 'status': 'running', 'owner': workspace._owner})
        with self.assertRaisesRegex(ValueError, '正在处理'):
            workspace.delete_account({'product_id': pid})
        self.assertEqual(workspace.product(pid)['handle'], 'yakuza0.3')
        workspace.save_job({'product_id': pid, 'status': 'running', 'owner': 'previous-process'})
        workspace.delete_account({'product_id': pid})
        self.assertEqual(len(workspace.accounts()), 1)

    def test_public_video_lookup_uses_normalized_handle_without_pool_account(self):
        account = self.add('https://www.tiktok.com/@Test.Creator')
        response = {'data': {'aweme_list': [{'aweme_id': VID, 'desc': 'Public video',
                    'author': {'unique_id': 'test.creator'}, 'video': {}, 'statistics': {'play_count': 3}}],
                    'max_cursor': 'next', 'has_more': 1}}
        with patch.object(workspace, 'client') as client:
            client.return_value.get.return_value = response
            job = {'id': 'test', 'owner': workspace._owner, 'product_id': account['product_id'],
                   'video_id': '', 'action': 'refresh', 'status': 'running', 'done': 0, 'total': 0, 'failures': 0}
            workspace.run_job(job)
            self.assertEqual(job['status'], 'complete', job)
            client.return_value.get.assert_called_once_with('/v1/scrape/tiktok/videos',
                {'handle': 'test.creator', 'sort_by': 'latest'}, cache_policy='record_only')
        self.assertEqual(workspace.item(account['product_id'], VID)['views'], 3)

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
                    page.locator('[data-close="reviewInputsDialog"]').click()
                    page.locator('#addProduct').click()
                    page.locator('#accountHandle').fill('yakuza0.3')
                    validity = page.locator('#accountHandle').evaluate('el => ({type:el.type, pattern:el.pattern, valid:el.validity.valid})')
                    self.assertEqual(validity, {'type': 'text', 'pattern': '', 'valid': True})
                    with page.expect_request('**/api/product-videos/accounts') as request:
                        page.locator('#saveAccount').click()
                    self.assertEqual(request.value.post_data_json, {'handle': 'yakuza0.3'})
                    expect(page.locator('#selectedName')).to_have_text('yakuza0.3')
                    manual_pid = next(a['product_id'] for a in workspace.accounts() if a['handle'] == 'yakuza0.3')
                    workspace.save_video(manual_pid, {'video_id': VID, 'title': 'Saved before deletion'})
                    page.locator('#deleteAccount').click()
                    expect(page.locator('#deleteAccountName')).to_have_text('@yakuza0.3')
                    page.screenshot(path=str(Path(folder) / 'delete-account-mobile.png'))
                    page.locator('#deleteAccountDialog [data-close="deleteAccountDialog"]').last.click()
                    expect(page.locator('#selectedName')).to_have_text('yakuza0.3')
                    page.locator('#deleteAccount').click()
                    page.locator('#confirmDeleteAccount').click()
                    expect(page.locator('#selectedName')).to_have_text('Creator')
                    self.assertFalse(any(a['handle'] == 'yakuza0.3' for a in workspace.accounts()))
                    page.reload(wait_until='networkidle')
                    self.assertEqual(page.locator('.product-row').count(), 1)
                    page.locator('#addProduct').click()
                    page.locator('#accountHandle').fill('@yakuza0.3')
                    page.locator('#saveAccount').click()
                    expect(page.locator('#selectedName')).to_have_text('yakuza0.3')
                    expect(page.locator('.video-card')).to_have_count(1)
                    self.assertEqual(workspace.product(manual_pid)['handle'], 'yakuza0.3')
                self.assertEqual(errors, [])
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
