"""Evidence-first review checks, without model API calls."""
import copy
import unittest
from assisted_video_review import build_evidence, validate, ground_logic, retention_prompt
from video_performance_context import build_report_facts, validate_report


class EvidenceReviewTests(unittest.TestCase):
    def fixture(self,retention=True):
        analysis={'metadata':{'duration_seconds':5.3},'timeline':[
            {'evidence_id':f'frame_{n}','time_source':'capture','timestamp_seconds':n,'visual':'人物' if n else '台面'} for n in range(6)],
            'transcript':{'segments':[{'start':.5,'end':2.5,'text':' hello world',
                'words':[{'start':.5,'end':.9,'word':' hello'},{'start':2.1,'end':2.5,'word':' world'}]}]}}
        perf={'available':True,'retention':{'0:00':'100%','0:01':'60%','0:02':'50%','0:03':'50%','0:04':'50%','0:05':'50%'}} if retention else {}
        e=build_evidence(analysis,build_report_facts(analysis,perf))
        r={'summary':'辅助判断','视频逻辑':{'核心表达':'主体','主体与道具':'未核实','推进':[
            {'start':0,'end':5.3,'事件':'剧情','逻辑作用':'示意','承接':'结束','依据':['t0','t2']}],'待核实':[]},
            '逻辑合理性':[{'判断':'成立','要点':'提问','依据':['t0'],'解释':'画面与声音'}],
            '留存分析':[{'区间ID':w['id'],'解释候选':'待验证','其他解释':'分发','证据强度':'低','依据':w['during']} for w in e['retention_windows']],
            '可尝试方向':[],'数据限制':['缺少归因数据']}
        return e,r

    def test_script_values_words_and_partial_last_second(self):
        e,r=self.fixture()
        self.assertEqual(e['timeline'][0]['speech'][0]['text'],'hello')
        self.assertEqual(e['timeline'][2]['speech'][0]['text'],'world')
        self.assertFalse(e['timeline'][1]['speech'])
        self.assertEqual(e['timeline'][0]['retention']['drop_percentage_points'],40)
        self.assertEqual(e['timeline'][-1]['end'],5.3)
        self.assertIsNone(e['timeline'][-1]['retention'])
        validate(r,e)
        validate_report(dict(r,report_version='commerce-review-v3-evidence',证据时间轴=e))

    def test_unknown_retention_stays_empty(self):
        e,r=self.fixture(False);validate(r,e)
        self.assertFalse(e['retention_windows']);self.assertTrue(all(row['retention'] is None for row in e['timeline']))
        r['留存分析']=[{'区间ID':'r0','依据':['t0']}]
        with self.assertRaises(ValueError):validate(r,e)

    def test_adjacent_opening_losses_are_merged_and_other_runs_retained(self):
        for values,expected in (([100,85,76,73,69,64,62,59,57,56,55,53,51,49,48,47],24),
                                ([100,82,67,62,58,55,51,47,44,42,41,39,36,33,32,29],33)):
            analysis={'metadata':{'duration_seconds':15.1},'timeline':[],'transcript':{}}
            performance={'available':True,'retention':{f'0:{i:02d}':str(v)+'%' for i,v in enumerate(values)}}
            evidence=build_evidence(analysis,build_report_facts(analysis,performance))
            main=evidence['retention_windows'][0]
            self.assertEqual((main['start_seconds'],main['end_seconds'],main['drop_percentage_points']),(0,2,expected))
            self.assertEqual(main['during'],['t0','t1'])
            self.assertTrue(any(w['start_seconds']==5 and w['end_seconds']==7 for w in evidence['retention_windows']))
            self.assertTrue(all('后段' not in w['label'] for w in evidence['retention_windows']))
            if expected==33:
                self.assertTrue(any(w['start_seconds']==11 and w['end_seconds']==13 for w in evidence['retention_windows']))
            else:
                self.assertTrue(any(w['label']=='相对平稳' for w in evidence['retention_windows']))

    def test_retention_merge_does_not_bridge_missing_seconds(self):
        analysis={'metadata':{'duration_seconds':5},'timeline':[],'transcript':{}}
        performance={'available':True,'retention':{'0:00':'100%','0:01':'80%','0:03':'50%','0:04':'35%'}}
        evidence=build_evidence(analysis,build_report_facts(analysis,performance))
        self.assertEqual(evidence['retention_windows'][0]['end_seconds'],1)
        self.assertFalse(any(w['start_seconds']<3 and w['end_seconds']>1 for w in evidence['retention_windows']))

    def test_event_review_requires_verified_provenance_and_blind_logic(self):
        e,r=self.fixture()
        r.update(report_version='commerce-review-v4-events',证据时间轴=e,
                 拆解流程={'logic_blinded_to_performance':True},
                 事件拆解={'available':True,'version':1,'inspected_frame_ids':['frame_0','frame_1'],
                     'events':[{'id':'e0','frame_ids':['frame_0','frame_1'],'subject':'人物',
                                'observed':'人物进入画面','intended_meaning':'','uncertainties':[],
                                'verification':'supported','verification_reason':'前后采样可见'}]})
        validate_report(r)
        for mutate in (lambda v:v.pop('事件拆解'),
                       lambda v:v['事件拆解']['events'][0].update(frame_ids=['frame_99']),
                       lambda v:v['事件拆解']['events'][0].pop('verification'),
                       lambda v:v['拆解流程'].update(logic_blinded_to_performance=False)):
            bad=copy.deepcopy(r);mutate(bad)
            with self.assertRaises(ValueError):validate_report(bad)

    def test_reject_invented_or_misaligned_references(self):
        e,base=self.fixture()
        for mutate in (lambda r:r['视频逻辑']['推进'][0].update(end=6),
                       lambda r:r['视频逻辑']['推进'][0].update(start=3),
                       lambda r:r['逻辑合理性'][0].update(依据=['imaginary']),
                       lambda r:r['留存分析'][0].update(区间ID='r100')):
            r=copy.deepcopy(base);mutate(r)
            with self.assertRaises(ValueError):validate(r,e)

    def test_later_utterance_cannot_explain_earlier_drop(self):
        e,r=self.fixture()
        e['timeline'][2]['speech'][0]['text']='this is later'
        r['留存分析'][0]['解释候选']='“this is later”可能影响此处留存'
        with self.assertRaisesRegex(ValueError,'区间前/区间内'):validate(r,e)
        r['留存分析'][0]['解释候选']='“hello world here”可能影响此处留存'
        e['timeline'][0]['speech'][0]['text']='hello world here'
        validate(r,e)

    def test_logic_time_is_derived_from_references(self):
        e,r=self.fixture();r['视频逻辑']['推进'][0].update(start=15,end=18)
        ground_logic(r,e)
        self.assertEqual(r['视频逻辑']['推进'][0]['start'],0)
        self.assertEqual(r['视频逻辑']['推进'][0]['end'],3)
        validate(r,e)

    def test_focused_cause_prompt_excludes_future_full_utterances(self):
        import json
        e,r=self.fixture()
        e['timeline'][2]['speech'][0].update(text='future phrase',full_text='future phrase')
        prompt=retention_prompt(e,r['视频逻辑'],{})
        contexts=json.loads(prompt.split('窗口证据：')[1].split('\n已知表现快照：')[0])
        first=json.dumps(contexts[0],ensure_ascii=False)
        self.assertNotIn('future phrase',first)
        self.assertNotIn('full_text',first)

    def test_denominator_guard_does_not_reject_a_warning(self):
        e,r=self.fixture();r['留存分析'][0]['其他解释']='分母未知，不能认为分母变小。'
        validate(r,e)
        r['留存分析'][0]['其他解释']='可能因为分母变小，压缩百分比。'
        with self.assertRaisesRegex(ValueError,'分母未知'):validate(r,e)


if __name__=='__main__':unittest.main()
