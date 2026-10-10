"""Optional library inputs; collection remains owned by the Proxy worker."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import re

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


def collection_account(workspace, pid, video):
    if str(pid).startswith('account:'):
        return int(str(pid).split(':')[1])
    match = re.fullmatch(r'/@([^/]+)/video/' + re.escape(video['video_id']) + r'/?', urlparse(video.get('url') or '').path)
    accounts = workspace.accounts()
    if match:
        owners = [a for a in accounts if a['handle'].casefold() == match[1].casefold()]
    else:
        with workspace.database() as conn:
            ids = {row[0] for row in conn.execute('SELECT product_id FROM account_video_items WHERE video_id=?', (video['video_id'],))}
        owners = [a for a in accounts if a['product_id'] in ids]
    if len(owners) != 1:
        raise ValueError('未能确定视频所属账号，请先将该账号加入账号池并绑定 IP')
    return int(owners[0]['product_id'].split(':')[1])


def input_state(workspace, pid, vid):
    video = workspace.item(pid, vid)
    allowed, error, handle = False, '', ''
    try:
        account_id = collection_account(workspace, pid, video)
        with workspace.database() as conn:
            account = collect._account(conn, account_id)
            proxy_pool.require_account_proxy_bound(account)
            handle, allowed = account['username'], True
    except ValueError as exc:
        error = str(exc)
    with workspace.database() as conn:
        row = conn.execute('SELECT payload FROM video_manual_orders WHERE video_id=?', (vid,)).fetchone()
        job = conn.execute("SELECT * FROM collect_jobs WHERE target_video_id=? AND platform='tiktok' ORDER BY created_at DESC LIMIT 1", (vid,)).fetchone()
    return {'video_id': vid, 'manual_orders': json.loads(row[0]) if row else None,
            'collection_link': video.get('collection_link'), 'collection_allowed': allowed,
            'collection_error': error, 'collection_account': handle,
            'collection_job': collect._job_row(job) if job else None}


def start_collection(workspace, payload):
    vid = workspace.identifier(payload.get('video_id'))
    video = workspace.item(payload.get('product_id'), vid)
    account_id = collection_account(workspace, payload.get('product_id'), video)
    return collect.create_job({'account_id': account_id, 'platform': 'tiktok',
                               'target_video_id': vid, 'write_to_feishu': False})
