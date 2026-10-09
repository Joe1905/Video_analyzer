"""Isolated single-call vs staged video logic experiment; never replace live reviews."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from deepseek_postprocess import (compact_analysis, video_duration, call_deepseek,
                                 extract_content, parse_json_content, DEFAULT_MODEL, DEFAULT_API_URL)
from video_performance_context import build_report_facts

LOGIC_TASK = '''先还原视频的表达逻辑，不套固定带货模板，不先评价好坏。
仅依据给定证据，区分主推对象、剧情道具、口播宣称与实际证明。把各段的提问、信息增量、承接、揭示与兑现串起来。
视觉描述与ASR冲突时明确记录，不直接选一方；不同外观不能自行认定同款或独立SKU。
区分物体可见、互动、特写、功能演示；采样时间只代表最早已观察到，不证明精确首次。
只输出结构化结论和证据，不输出内部思考过程。logic对象包含：
core_message（核心表达字符串）；main_subject（主推对象及判断依据字符串）；
roles（数组，每项object、role、evidence）；claims（数组，每项claim、evidence、verification_status）；
beats（数组，每项start、end、event、purpose、connection_to_next、evidence）；
payoff（悬念及价值主张如何兑现字符串）；uncertainties（字符串数组）。'''

REPORT_TASK = '''结合视频逻辑与真实表现数据，输出电商内部复盘。
保留原片可能有效的表达机制，区分事实、合理解释、待验证假设。不因为没有特写、没有CTA或数据低就直接判差。
未知指标不写成0；没有留存、点击、订单不能归因流失或成交。不同视频或发布条件未知不得编造对照。
每条建议必须说明依据与可能破坏的原片机制；不要求完整拍摄脚本，最多提出两项主要调整实验。
仍核对原始timeline及ASR，不把logic当唯一事实来源。发现logic与原始证据矛盾，披露并纠正后再诊断。
report对象包含：summary（字符串）；strengths（数组，每项point、evidence）；
diagnoses（数组，最多3项，每项observation、evidence、hypothesis、alternative、confidence）；
experiments（数组，最多2项，每项change、reason、preserve、metric、limits）；limitations（字符串数组）。'''


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def evidence(snapshot):
    return json.dumps(snapshot['analysis'], ensure_ascii=False)


def prompts(snapshot, logic=None):
    raw = '\n原始视频提取证据：\n' + evidence(snapshot)
    metrics = '\n表现快照与脚本事实：\n' + json.dumps({k:snapshot[k] for k in ('performance','facts')}, ensure_ascii=False)
    return {
        'single': LOGIC_TASK + '\n在同一次调用中先形成logic，再完成下列分析。\n' + REPORT_TASK +
                  '\n只输出严格JSON {"logic":上述logic对象,"report":上述report对象}。' + raw + metrics,
        'logic': LOGIC_TASK + '\n只输出严格JSON logic对象。' + raw,
        'report': REPORT_TASK + '\n只输出严格JSON report对象。' + raw + metrics +
                  '\n独立逻辑梳理产物：\n' + json.dumps(logic, ensure_ascii=False),
    }


def prepare(folder, video_id):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/'input.json'
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    name = 'shortvideo_SociaVault_' + video_id + '.mp4'
    source = Path.cwd()/'output'/name
    analysis = json.loads((source/'analysis.json').read_text(encoding='utf-8'))
    analysis['metadata']['duration_seconds'] = video_duration(Path.cwd()/'videos'/name)
    analysis['shot_evidence'] = json.loads((source/'shot_evidence.json').read_text(encoding='utf-8'))
    performance = json.loads((source/'library_performance.json').read_text(encoding='utf-8'))
    snapshot = {'video_id':video_id,'analysis':compact_analysis(analysis),'performance':performance,
                'facts':build_report_facts(analysis,performance)}
    write(path,snapshot)
    digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    write(folder/'manifest.json',{'input_sha256':digest,'video_id':video_id,
          'deepseek_model':DEFAULT_MODEL,'reasoning_effort':'high','single_max_tokens':32768,
          'split_logic_max_tokens':8192,'split_report_max_tokens':24576,
          'note':'Luna runs through Codex agents; system context and token metering differ from DeepSeek API.'})
    for mode,prompt in prompts(snapshot).items():
        (folder/(mode+'_prompt.txt')).write_text(prompt,encoding='utf-8')
    return snapshot


def run(folder, mode, snapshot):
    target=folder/('deepseek_'+mode);target.mkdir(exist_ok=True)
    def invoke(stage,prompt,budget):
        (target/(stage+'_prompt.txt')).write_text(prompt,encoding='utf-8')
        started=time.monotonic()
        response=call_deepseek(os.environ['DEEPSEEK_API_KEY'],prompt,
            os.getenv('DEEPSEEK_API_URL',DEFAULT_API_URL),DEFAULT_MODEL,budget,'high')
        write(target/(stage+'_usage.json'),{'elapsed_seconds':round(time.monotonic()-started,2),
              'model':response.get('model'),'usage':response.get('usage'),
              'finish_reason':response['choices'][0].get('finish_reason')})
        content=extract_content(response)
        (target/(stage+'_raw.txt')).write_text(content,encoding='utf-8')
        result=parse_json_content(content);write(target/(stage+'.json'),result)
        print(mode,stage,'completed',flush=True)
        return result
    if mode=='single':
        result=invoke('result',prompts(snapshot)['single'],32768)
    else:
        logic=invoke('logic',prompts(snapshot)['logic'],8192)
        report=invoke('report',prompts(snapshot,logic)['report'],24576)
        result={'logic':logic,'report':report};write(target/'result.json',result)
    if not isinstance(result.get('logic'),dict) or not isinstance(result.get('report'),dict):
        raise ValueError('Experiment output missing logic or report; raw output retained.')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['prepare','single','split'],required=True)
    parser.add_argument('--folder',default='output/experiments/logic-ab-20261009')
    parser.add_argument('--video-id',default='7693858842249055501')
    args=parser.parse_args();folder=Path(args.folder)
    snapshot=prepare(folder,args.video_id)
    if args.mode!='prepare':run(folder,args.mode,snapshot)
