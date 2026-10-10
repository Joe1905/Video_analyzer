"""Optional library inputs; collection remains owned by the Proxy worker."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import proxy_pool
import tiktok_studio_collect as collect


def load_orders(root, video_id):
    path = Path(root) / 'data' / 'proxy_pool.sqlite'
    if not path.is_file():
        return None
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='video_manual_orders'").fetchone()
        row = conn.execute('SELECT payload FROM video_manual_orders WHERE video_id=?', (video_id,)).fetchone() if exists else None
    return json.loads(row[0]) if row else None


def save_orders(workspace, payload):
    vid = workspace.identifier(payload.get('video_id'))
    workspace.item(payload.get('product_id'), vid)
    value = payload.get('count')
    if value is not None and (type(value) is not int or not 0 <= value <= 1000000000):
        raise ValueError('出单数量需为 0 至 10 亿的整数；未知请留空')
    with workspace.database() as conn:
        if value is None:
            conn.execute('DELETE FROM video_manual_orders WHERE video_id=?', (vid,))
            return {'manual_orders': None}
        result = {'count': value, 'source': 'manual', 'scope': 'video_cumulative',
                  'recorded_at': datetime.now(timezone.utc).isoformat()}
        conn.execute('INSERT OR REPLACE INTO video_manual_orders VALUES (?,?)', (vid, json.dumps(result)))
    return {'manual_orders': result}


def input_state(workspace, pid, vid):
    video = workspace.item(pid, vid)
    accounts = workspace.accounts()
    with workspace.database() as conn:
        row = conn.execute('SELECT payload FROM video_manual_orders WHERE video_id=?', (vid,)).fetchone()
        job = conn.execute("SELECT * FROM collect_jobs WHERE target_video_id=? AND platform='tiktok' ORDER BY created_at DESC LIMIT 1", (vid,)).fetchone()
        for account in accounts:
            raw = conn.execute('SELECT * FROM tiktok_accounts WHERE id=?', (int(account['product_id'].split(':')[1]),)).fetchone()
            try:
                proxy_pool.require_account_proxy_bound(raw)
                account['collection_allowed'] = True
            except ValueError as exc:
                account['collection_allowed'] = False
                account['collection_error'] = str(exc)
    return {'video_id': vid, 'manual_orders': json.loads(row[0]) if row else None,
            'collection_link': video.get('collection_link'), 'accounts': accounts,
            'collection_job': collect._job_row(job) if job else None}


def start_collection(workspace, payload):
    vid = workspace.identifier(payload.get('video_id'))
    workspace.item(payload.get('product_id'), vid)
    account_id = int(payload.get('account_id') or 0)
    if str(payload.get('product_id')).startswith('account:') and str(payload['product_id']) != f'account:{account_id}':
        raise ValueError('请选择当前视频所属账号')
    return collect.create_job({k: payload.get(k) for k in
        ('account_id', 'publish_date_start', 'publish_date_end', 'feishu_target', 'observation_session_id')}
        | {'platform': 'tiktok', 'target_video_id': vid})
