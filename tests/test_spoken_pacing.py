from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from video_creation_pipeline.contract_io import atomic_write_json, payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.spoken_pacing import (
    bound_snapped_pauses, pause_cut_window, pause_recommendation,
    validate_compiled_pause_bounds, validate_pause_compression, validate_pause_decision,
)
from video_creation_pipeline.spoken_rough_cut import (
    LEGACY_NATURAL_PACING_POLICY, _default_candidate_decision,
    build_spoken_edit_decision_template, build_spoken_edit_plan,
    prepare_spoken_edit_judge_packet, validate_spoken_edit_agent_decisions,
    validate_spoken_edit_judge_packet,
)
import test_spoken_rough_cut as fixtures


class SpokenPacingTests(unittest.TestCase):
    def _setup_fixture(self):
        self.fixture = fixtures.SpokenRoughCutTests('test_prepare_and_template_keep_short_pauses_and_flag_fillers')
        self.fixture._testMethodName = self._testMethodName
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def reviewed(self, role='ordinary'):
        return {'semantic_role': role, 'audio_checked': True, 'context_checked': True,
                'evidence': 'synthetic timing fixture, no real audio audition claimed'}

    def test_long_pause_is_a_review_candidate_not_a_high_confidence_cut(self):
        for where,duration,target in [('within_sentence',.5,.3),('between_sentences',.9,.55),('leading',.4,.2)]:
            with self.subTest(where=where):
                row=_default_candidate_decision(dict(id='p',kind='pause',start=0,end=duration,where=where))
                self.assertEqual(row['action'],'review_required')
                self.assertEqual(row['confidence'],0)
                self.assertNotIn('preserve_s',row)
                self.assertEqual(row['recommendation']['preserve_s'],target)

    def test_short_unknown_and_expressive_pauses_keep_original_length(self):
        self.assertEqual(pause_recommendation(0,.24,'within_sentence')['action'],'keep')
        self.assertEqual(pause_recommendation(0,4,'unknown')['action'],'keep')
        for role in ('emphasis','emotion','topic_change','breath','turn_taking'):
            self.assertEqual(pause_recommendation(0,4,'within_sentence',role)['action'],'keep')

    def test_tail_guard_is_not_a_whole_sentence_gap(self):
        with self.assertRaisesRegex(ContractError,'guards'):
            validate_pause_compression(1,2,.15,'within_sentence',self.reviewed())
        a,b=pause_cut_window(1,2,.25,'within_sentence')
        self.assertAlmostEqual(a-1,.15)
        self.assertAlmostEqual(2-b,.10)
        with self.assertRaisesRegex(ContractError,'override_reason'):
            validate_pause_compression(1,2,.3,'between_sentences',self.reviewed())
        review={**self.reviewed(),'override_reason':'Reviewed brisk sentence transition in fixture'}
        validate_pause_compression(1,2,.3,'between_sentences',review)

    def test_snapping_and_centisecond_rounding_cannot_expand_a_pause_cut(self):
        a,b=pause_cut_window(.803,1.607,.3,'within_sentence')
        rows=bound_snapped_pauses([dict(id='p',kind='pause',start=.8,end=1.61)],
                                 [dict(id='p',kind='pause',start=a,end=b)])
        self.assertGreaterEqual(rows[0]['start'],a)
        self.assertLessEqual(rows[0]['end'],b)
        self.assertGreaterEqual((rows[0]['start']-.803)+(1.607-rows[0]['end']),.3)
        row=dict(kind='pause',action='compress',start=.803,end=1.607,preserve_s=.3,where='within_sentence')
        validate_compiled_pause_bounds([row],rows)
        with self.assertRaisesRegex(ContractError,'protected pause'):
            validate_compiled_pause_bounds([row],[dict(start=.94,end=1.46)])
        row.update(action='keep',pause_review=self.reviewed('emotion'))
        with self.assertRaisesRegex(ContractError,'protected pause'):
            validate_compiled_pause_bounds([row],[dict(start=1.0,end=1.1)])

    def test_validator_rejects_delete_missing_review_and_candidate_kind_laundering(self):
        self._setup_fixture()
        packet_path=self.fixture._packet()
        packet=json.loads(packet_path.read_text('utf-8'))
        with patch('video_creation_pipeline.spoken_rough_cut._videocut_source_manifest',return_value=self.fixture.source_manifest):
            baseline=build_spoken_edit_decision_template(packet_path)
            with self.assertRaisesRegex(ContractError,'not ready'):
                validate_spoken_edit_agent_decisions(baseline,packet,require_complete=True)
            mutations=[{'action':'delete'}, {'action':'compress','preserve_s':.55},
                       {'action':'delete','kind':'filler'},
                       {'action':'compress','preserve_s':.55,'pause_review':self.reviewed('emphasis')},
                       {'action':'compress','preserve_s':.55,'pause_review':self.reviewed(),'start':2.7},
                       {'pause_review':{'semantic_role':[]}}]
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    value=copy.deepcopy(baseline)
                    value['decisions'][0].update(reason='fixture edit',**mutation)
                    value['decisions_sha256']=payload_sha256(value,'decisions_sha256')
                    with self.assertRaises(ContractError):
                        validate_spoken_edit_agent_decisions(value,packet)

    def test_old_policy_is_readable_but_not_silently_recompiled(self):
        self._setup_fixture()
        packet_path=self.fixture._packet()
        packet=json.loads(packet_path.read_text('utf-8'))
        packet['pacing_policy']=copy.deepcopy(LEGACY_NATURAL_PACING_POLICY)
        packet['agent_review_contract']['default_retry_resolution']=LEGACY_NATURAL_PACING_POLICY['retry_rule']
        packet['packet_sha256']=payload_sha256(packet,'packet_sha256')
        atomic_write_json(packet,packet_path)
        with patch('video_creation_pipeline.spoken_rough_cut._videocut_source_manifest',return_value=self.fixture.source_manifest):
            validate_spoken_edit_judge_packet(packet)
            with self.assertRaisesRegex(ContractError,'read-only'):
                build_spoken_edit_decision_template(packet_path)

    def test_v2_policy_is_read_only_with_original_retry_meaning(self):
        from video_creation_pipeline.spoken_pacing import LEGACY_PACING_V2
        self._setup_fixture()
        path = self.fixture._packet()
        packet = json.loads(path.read_text('utf-8'))
        packet['pacing_policy'] = copy.deepcopy(LEGACY_PACING_V2)
        packet['agent_review_contract']['default_retry_resolution'] = LEGACY_PACING_V2['retry_rule']
        packet['packet_sha256'] = payload_sha256(packet,'packet_sha256')
        atomic_write_json(packet,path)
        with patch('video_creation_pipeline.spoken_rough_cut._videocut_source_manifest',return_value=self.fixture.source_manifest):
            validate_spoken_edit_judge_packet(packet)
            with self.assertRaisesRegex(ContractError,'read-only'):
                build_spoken_edit_decision_template(path)

    def test_invalid_timing_and_spoken_overlap_fail_closed(self):
        for invalid in (None, [], True, float('nan'), -1):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                pause_cut_window(1,2,invalid,'within_sentence')
        row=dict(kind='pause',action='compress',start=1,end=2,preserve_s=.3,
                 where='within_sentence',pause_review=self.reviewed())
        with self.assertRaisesRegex(ContractError,'spoken token'):
            validate_pause_decision(row,row,[dict(isGap=False,start=1.4,end=1.5)])
        with self.assertRaisesRegex(ContractError,'spoken token'):
            validate_pause_decision(row,row,[dict(isGap=False,start='1.4',end='1.5')])
        with self.assertRaisesRegex(ContractError,'omitted'):
            bound_snapped_pauses([],[dict(id='p',kind='pause',start=1.15,end=1.85)])

    @unittest.skipUnless(os.environ.get('VIDEOCUT_KIT_SOURCE_ROOT'),'pinned local source required')
    def test_actual_upstream_scripts_compile_synthetic_pause_without_losing_guards(self):
        self._setup_fixture()
        packet=prepare_spoken_edit_judge_packet(self.fixture.video,self.fixture.tokens,
            self.root/'real-source-judge',os.environ['VIDEOCUT_KIT_SOURCE_ROOT'])
        packet_path=self.root/'real-source-judge/spoken-edit-judge-packet.json'
        decisions=build_spoken_edit_decision_template(packet_path)
        target=None
        for row in decisions['decisions']:
            row.update(action='keep',confidence=.9,reason='synthetic keep')
            if row['kind']=='pause' and abs(row['start']-.8)<.001:
                row.update(action='compress',preserve_s=.3,pause_review=self.reviewed(),reason='synthetic ordinary gap')
                target=row
        self.assertIsNotNone(target)
        decisions['boundary_review']=dict(opening_checked=True,ending_checked=True,cut_boundaries_checked=True,events_checked=True,evidence='synthetic boundary review')
        decisions['rough_audio_review']=dict(levels_checked=True,clipping_checked=True,evidence='synthetic level and clipping review fixture')
        decisions['status']='agent_review_complete'
        decisions['reviewed_fulltext_sha256']=packet['judge_fulltext']['sha256']
        decisions['review'].update(fulltext_read=True,semantic_retry_scan_completed=True)
        decisions['decisions_sha256']=payload_sha256(decisions,'decisions_sha256')
        path=atomic_write_json(decisions,self.root/'reviewed.json')
        with patch('video_creation_pipeline.spoken_rough_cut._media_duration',return_value=6.0):
            plan=build_spoken_edit_plan(packet_path,path,self.root/'compiled')
        cuts=json.loads(Path(plan['artifacts']['delete_segments']['path']).read_text('utf-8'))
        self.assertEqual(len(cuts),1)
        self.assertGreaterEqual(cuts[0]['start'],.95-1e-7)
        self.assertLessEqual(cuts[0]['end'],1.45+1e-7)
        self.assertFalse(plan['execution_receipt']['ffmpeg_executed'])
        self.assertEqual(self.fixture.video.read_bytes(),b'synthetic-video')
        print(json.dumps({'synthetic_upstream_integration':True,'deletes':cuts,
                          'pinned_commit':plan['videocut_kit']['commit'],
                          'execution_receipt':plan['execution_receipt']},sort_keys=True))


if __name__ == '__main__':
    unittest.main()
