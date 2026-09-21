"""Offline session ownership races and account status regressions."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import proxy_pool
import tiktok_studio_publish as publish


def main():
    with TemporaryDirectory() as temp:
        db = Path(temp) / "sessions.sqlite"
        def connect():
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            return conn
        with connect() as conn:
            conn.execute("CREATE TABLE browser_sessions (id INTEGER PRIMARY KEY, account_id INTEGER, status TEXT, current_job_id TEXT, last_activity_at TEXT, updated_at TEXT, owner TEXT)")
            conn.execute("INSERT INTO browser_sessions VALUES (1,15,'observing','','','','manual')")
            conn.execute("INSERT INTO browser_sessions VALUES (2,16,'observing','','','','manual')")
            conn.execute("INSERT INTO browser_sessions VALUES (3,15,'stopped','','','','manual')")
        with patch.object(proxy_pool, "connect", connect), patch.object(proxy_pool, "_active_sessions"), patch.object(proxy_pool, "_row_to_session", side_effect=dict):
            def claim(job):
                try:
                    return proxy_pool.claim_observation_session_for_job(15, 0, job, reuse_idle=True)
                except ValueError as exc:
                    assert '正在执行其他任务' in str(exc)
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                winners = [row for row in pool.map(claim, ['one', 'two']) if row]
            assert len(winners) == 1, winners
            winner = winners[0]
            assert winner['id'] == 1 and winner['owner'] == 'manual'
            proxy_pool.release_observation_session_job(1, winner['current_job_id'])
            assert proxy_pool.claim_observation_session_for_job(15, 3, 'next', reuse_idle=True)['id'] == 1
            proxy_pool.release_observation_session_job(1, 'next')
            try:
                proxy_pool.claim_observation_session_for_job(15, 2, 'wrong', reuse_idle=True)
            except ValueError as exc:
                assert '不属于' in str(exc)
            else:
                raise AssertionError('Cross-account session accepted')
            assert proxy_pool.claim_observation_session_for_job(99, 0, 'new', reuse_idle=True) is None
            assert proxy_pool.claim_observation_session_for_job(15, 0, 'legacy') is None
            with connect() as conn:
                conn.execute("UPDATE browser_sessions SET status='starting' WHERE id=1")
            try:
                proxy_pool.claim_observation_session_for_job(15, 0, 'starting', reuse_idle=True)
            except ValueError as exc:
                assert '正在唤醒' in str(exc)
            else:
                raise AssertionError('Starting session claimed')
        with patch.object(publish, '_job_query', return_value=[{'account_id':15}]), patch.object(proxy_pool, 'claim_observation_session_for_job', side_effect=ValueError('观测通道正在执行其他任务')), patch.object(publish, '_set_job') as update, patch.object(proxy_pool, 'start_automation_session') as start:
            publish._run_job('busy')
            assert update.call_args.args[1:3] == ('delayed', 'waiting_slot')
            start.assert_not_called()
    print('PASS: concurrent claims, stale session fallback, account isolation, ownership and busy queue')


if __name__ == '__main__':
    main()
