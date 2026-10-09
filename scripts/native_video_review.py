"""One-click review orchestration for the video library; reuse the analyzer pipeline."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from video_performance_context import validate_report
from video_performance_context import load_performance_context, build_report_facts
from visual_analysis_cache import valid_analysis

_review_lock = threading.Lock()
REPORT_KEYS = ('report_version','summary', '内容拆解', '表现诊断', '优先修改', '原片镜头拆解', '数据限制', '采集数据来源',
               '证据时间轴','视频逻辑','逻辑合理性','留存分析','可尝试方向','人工补充')


def filename(video_id):
    return 'shortvideo_SociaVault_' + str(video_id) + '.mp4'


def needs_collection_refresh(report, current):
    if not report or not build_report_facts({'timeline':[]},current)['retention']['adjacent_second_changes']:return False
    previous=report.get('采集数据来源',{})
    return any(previous.get(k)!=current.get(k) for k in ('collection_id','collected_at','collection_source','retention'))


def saved_report(root, video_id):
    for name in ('assisted_review.json','audit_result.json'):
        path = Path(root) / 'output' / filename(video_id) / name
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
            validate_report(report)
            if str(report.get('采集数据来源', {}).get('video_id')) == str(video_id):
                return {k: report[k] for k in REPORT_KEYS if k in report}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
    return None


def library_context(video):
    overview = {'play_count': video.get('views')}
    engagement = {k: video.get(v) for k, v in
                  (('play', 'views'), ('likes', 'likes'), ('comments', 'comments'), ('shares', 'shares'), ('favorites', 'saves'))}
    collected = video.get('updated_at') or video.get('basic_updated_at')
    return {'video_id': str(video['video_id']), 'source': 'video_library',
            'available': any(v is not None for v in (*overview.values(), *engagement.values())),
            'title': video.get('title', ''),
            'collected_at': datetime.fromtimestamp(collected, timezone.utc).isoformat() if collected else None,
            'overview': {k: v for k, v in overview.items() if v is not None},
            'engagement': {k: v for k, v in engagement.items() if v is not None},
            'limitations': ['使用视频列表已保存的指标；未提供逐秒留存、平均观看、完播率、点击、订单和GMV，未知不等于0',
                            '商品关联或视频主题不证明实际挂车绑定；内容原因属于待验证假设']}


def run_stage(command, root, folder, env, stage):
    """Keep sanitized diagnostics instead of discarding a failed subprocess's error."""
    log = Path(folder) / (stage + '_failure.json')
    try:
        result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                text=True, timeout=1800, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = exc.stderr or ''
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        for key, value in env.items():
            if value and any(part in key.upper() for part in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
                detail = detail.replace(value, '[redacted]')
        detail = re.sub(r'https?://\S+', '[endpoint]', detail)
        detail = re.sub(r'(?i)Bearer\s+\S+|sk-[\w-]+', '[redacted]', detail)
        log.write_text(json.dumps({'stage': stage, 'error_type': type(exc).__name__,
            'returncode': getattr(exc, 'returncode', None), 'stderr': detail[-12000:],
            'recorded_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False), encoding='utf-8')
        if stage == 'extraction':
            message = '视频内容识别未完成，已下载的视频保留，可重试。'
        else:
            message = '视频内容已识别，复盘生成暂未完成；可重试，无需重新提取。'
        if isinstance(exc, subprocess.TimeoutExpired):
            message = '视频内容识别超时，可重试。' if stage == 'extraction' else '复盘生成超时；可重试，无需重新提取。'
        raise ValueError(message) from exc
    log.unlink(missing_ok=True)
    return result


def run_review(workspace, job):
    vid, pid = job['video_id'], job['product_id']
    root = Path(workspace.ROOT)
    job.update(message='等待生成复盘…', total=1)
    workspace.save_job(job)
    with _review_lock:
        cached=saved_report(root,vid)
        current=load_performance_context(root,vid)
        refresh=needs_collection_refresh(cached,current)
        if cached and not job.get('force') and not refresh:
            job.update(done=1, message='复盘已完成。')
            return
        media = root / 'videos' / filename(vid)
        if not media.is_file():
            workspace.prepare_media(job)
            media.parent.mkdir(parents=True, exist_ok=True)
            pending = media.with_suffix('.pending.mp4')
            shutil.copy2(workspace.MEDIA / vid / 'video.mp4', pending)
            pending.replace(media)
        folder = root / 'output' / media.name
        folder.mkdir(parents=True, exist_ok=True)
        scripts = Path(__file__).parent
        env = os.environ.copy()
        if not valid_analysis(folder / 'analysis.json'):
            job['message'] = '正在理解视频内容…'
            workspace.save_job(job)
            env.update(ANALYSIS_OUTPUT_DIR=str(folder), ANALYSIS_LANGUAGE_OVERRIDE='auto')
            run_stage(['bash', str(scripts / 'analyze_one.sh'), media.name], root, folder, env, 'extraction')
            if not valid_analysis(folder / 'analysis.json'):
                raise ValueError('视频内容未能完整识别，已保存的结果会保留，可重试。')
        job['message'] = '正在整理复盘结果…'
        workspace.save_job(job)
        context = folder / 'library_performance.json'
        workspace.write_json(context, library_context(workspace.item(pid, vid)))
        run_stage([sys.executable, str(scripts / 'deepseek_postprocess.py'),
                        str(folder / 'analysis.json'), '--video-id', vid,
                        '--video-filename', media.name, '--performance-context', str(context),
                        '--review-format','evidence','--output',str(folder/'assisted_review.json'),
                        '--logic-note',job.get('logic_note','')],
                       root, folder, env, 'review')
        if not saved_report(root, vid):
            raise ValueError('复盘结果未通过检查，请重试。')
        job.update(done=1, message='复盘已完成。')


def review_state(workspace, pid, vid):
    workspace.item(pid, vid)  # Membership check applies before accessing shared results.
    report = saved_report(workspace.ROOT, vid)
    job = workspace.snapshot(pid).get('job')
    related = job and job.get('action') == 'review' and job.get('video_id') == vid
    active = job and job['status'] in ('running', 'queued')
    current=load_performance_context(workspace.ROOT,vid)
    outdated=needs_collection_refresh(report,current)
    status = 'processing' if related and active else 'failed' if outdated and related and job['status']=='failed' else 'missing' if outdated else 'ready' if report else 'waiting' if active else 'failed' if related and job['status'] == 'failed' else 'missing'
    completed='复盘已完成。'
    if report and report.get('采集数据来源',{}).get('collected_at'):
        completed='复盘已完成 · 指标与留存来自 '+str(report['采集数据来源']['collected_at'])[:10]+' 的同一快照。'
    media = Path(workspace.ROOT) / 'videos' / filename(vid)
    return {'video_id': vid, 'status': status, 'report': report,
            'message': job.get('message') if related and active else '本次更新未完成，已保留上次复盘；可再次提交补充。' if report and related and job['status']=='failed' else '找到关联的采集留存，正在更新复盘。' if outdated else completed if report else job.get('message') if related else '正在等待当前任务完成…' if active else '点击生成内容复盘。',
            'video_url': '/video/' + media.name if media.is_file() else None}
