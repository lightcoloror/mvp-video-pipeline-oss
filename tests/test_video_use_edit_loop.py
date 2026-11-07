from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from video_creation_pipeline.contract_io import artifact_reference as ref, atomic_write_json as write
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.video_use_review_execution import (
    CONTEXT_SCHEMA, load_context, source_ranges, verified_cache, verify_font, outlet)
from video_creation_pipeline.video_use_edit_loop import (
    build_suggestions, prepare_edit, _project_context, apply_edit)
from video_creation_pipeline.creative_gates import build_approval_record
from video_creation_pipeline.video_use_review_handoff import build_video_use_review_handoff
from video_creation_pipeline.review_resource_guard import ResourcePressurePauseRequired

class EditLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.media=self.root/'synthetic.mock'
        self.media.write_bytes(b'metadata-only fixture; not a decodable video')
        self.upstream=self.root/'upstream'; (self.upstream/'helpers').mkdir(parents=True)
        (self.upstream/'helpers/timeline_view.py').write_text('# fixture',encoding='utf-8')
        (self.upstream/'SKILL.md').write_text('fixture',encoding='utf-8')
        self.patchers=[patch('video_creation_pipeline.video_use_review_handoff._git_head',return_value='92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66'),
            patch('video_creation_pipeline.video_use_edit_loop.media_info',return_value={'duration':12,'fps':20,'width':640,'height':360,'audio':True}),
            patch('video_creation_pipeline.video_use_review_execution.media_info',return_value={'duration':12,'fps':20,'width':640,'height':360,'audio':True})]
        for p in self.patchers: p.start(); self.addCleanup(p.stop)
        self.pack=write({'schema':'video_creation_pipeline.video_packaging_plan.v1','scenes':[
            {'scene_id':str(i),'start_s':i*4,'end_s':(i+1)*4} for i in range(3)]},self.root/'pack.json')
        self.hp=write(build_video_use_review_handoff(self.upstream,self.media,self.pack),self.root/'handoff.json')
        words=[{'id':'w1','start':.2,'end':1.,'text':'先保留限定'}, {'id':'w2','start':4.2,'end':4.8,'text':'唔好'},
            {'id':'w3','start':6.4,'end':7.2,'text':'剪走气口'}, {'id':'w4','start':8.2,'end':9.2,'text':'先保留限定'}]
        utt=[{'id':'u1','start':.2,'end':1.,'text':'先保留限定'}, {'id':'u2','start':4.2,'end':7.2,'text':'唔好剪走气口'},
             {'id':'u3','start':8.2,'end':9.2,'text':'先保留限定'}]
        self.ctx={'schema':CONTEXT_SCHEMA,'media':ref(self.media,'media'),'synthetic_fixture_only':True,
            'words':words,'utterances':utt,'subtitles':[dict(u,id='s'+u['id']) for u in utt],
            'mapping':[{'source':ref(self.media,'source'),'source_start':0,'source_end':12,'output_start':0,'output_end':12}]}
        self.cp=write(self.ctx,self.root/'context.json')

    def suggestion(self):
        return write(build_suggestions(self.hp,self.cp),self.root/'suggestions.json')

    def level_selection(self, path, keeps=None):
        from segment_level_fixture import make_review
        value=json.loads(Path(path).read_text())
        levels=make_review(self.root/'audio-levels',self.media,keeps or [dict(start=0,end=12)],self.ctx['words'])
        value.setdefault('rough_audio_review',dict(levels_checked=True,clipping_checked=True,evidence='synthetic'))['segment_leveling']=levels
        value['clip_gains']=levels['clip_gains']
        write(value,path)
        return path

    def test_pause_is_candidate_keeps_breathing_and_cantonese(self):
        s=build_suggestions(self.hp,self.cp)
        p=next(x for x in s['suggestions'] if x['kind']=='pause' and 4<x['delete_start']<6)
        self.assertAlmostEqual(p['delete_start'],4.95); self.assertAlmostEqual(p['delete_end'],6.25)
        self.assertEqual(p['preserve_s'],.3)
        self.assertEqual(s['pacing_policy']['name'],'natural_conversational_v3')
        self.assertEqual(p['default_action'],'keep_pending_review'); self.assertTrue(p['uncertainty'])
        self.assertEqual(self.media.read_bytes(),b'metadata-only fixture; not a decodable video')

    def test_ordinary_duplicate_prefers_last_candidate_with_review(self):
        r=next(x for x in build_suggestions(self.hp,self.cp)['suggestions'] if x['kind']=='repetition')
        self.assertEqual(r['evidence']['retained_utterance'],'u3')
        self.assertTrue(r['requires_meaning_review'])

    def test_take_ranking_preserves_meaning_review(self):
        c=copy.deepcopy(self.ctx)
        c['utterances'][0].update(take_group='a',complete=True,quality_score=.9)
        c['utterances'][1].update(take_group='a',complete=False,quality_score=1)
        write(c,self.cp)
        s=build_suggestions(self.hp,self.cp)
        r=next(x for x in s['suggestions'] if x['kind']=='alternative_take')
        self.assertEqual(r['evidence']['preferred']['id'],'u1')
        sp=write(s,self.root/'suggestions.json'); sel=write({'suggestion_ids':[r['suggestion_id']]},self.root/'selection.json')
        with self.assertRaisesRegex(ContractError,'meaning'): prepare_edit(sp,sel)

    def test_missing_word_and_subtitle_timing_rejected(self):
        for key in ['words','subtitles']:
            c=copy.deepcopy(self.ctx); c[key]=[]; write(c,self.cp)
            with self.assertRaisesRegex(ContractError,'missing'): load_context(self.cp,ref(self.media,'media'),12)

    def test_out_of_range_mapping_and_source_rejected(self):
        for key in ['output_end','source_end']:
            c=copy.deepcopy(self.ctx); c['mapping'][0][key]=13; write(c,self.cp)
            with self.assertRaises(ContractError): load_context(self.cp,ref(self.media,'media'),12)

    def test_uncovered_word_rejected(self):
        c=copy.deepcopy(self.ctx); c['subtitles'][0]['end']=.8; write(c,self.cp)
        with self.assertRaisesRegex(ContractError,'covered'): load_context(self.cp,ref(self.media,'media'),12)

    def test_piecewise_mapping(self):
        c=copy.deepcopy(self.ctx)
        c['mapping']=[{'source':ref(self.media,'s'),'output_start':0,'output_end':4,'source_start':1,'source_end':5},
                      {'source':ref(self.media,'s'),'output_start':4,'output_end':12,'source_start':4,'source_end':12}]
        rows=source_ranges(c,3,5)
        self.assertEqual([(r['source_start'],r['source_end']) for r in rows],[(4,5),(4,5)])

    def test_edl_projection_and_locality(self):
        sp=self.suggestion(); s=json.loads(sp.read_text(encoding='utf-8'))
        p=next(x for x in s['suggestions'] if x['kind']=='pause' and 4<x['delete_start']<6)
        sel=write({'suggestion_ids':[p['suggestion_id']], 'rough_audio_review':dict(levels_checked=True,clipping_checked=True,evidence='synthetic audio review'), 'boundary_review':dict(opening_checked=True,ending_checked=True,cut_boundaries_checked=True,events_checked=True,evidence='synthetic review'), 'pause_review':{p['suggestion_id']:{
            'semantic_role':'ordinary','audio_checked':True,'context_checked':True,'evidence':'synthetic timing fixture'}}},
            self.root/'selection.json')
        self.level_selection(sel,[dict(start=0,end=p['delete_start']),dict(start=p['delete_end'],end=12)])
        edl=prepare_edit(sp,sel)
        self.assertEqual([r['scene_id'] for r in edl['scenes'] if r['changed']],['1'])
        self.assertAlmostEqual(edl['duration'],10.7)
        revised=self.root/'revised.mock'; revised.write_bytes(b'revised')
        projected=_project_context(self.ctx,edl,revised)
        self.assertAlmostEqual(projected['words'][-1]['start'],6.9)
        self.assertAlmostEqual(projected['mapping'][-1]['source_start'],6.25)
        self.assertEqual(edl['nle_segments'][0]['source_sha256'],ref(self.media,'m')['sha256'])

    def test_pause_selection_requires_audio_review_and_keeps_emphasis(self):
        sp=self.suggestion(); s=json.loads(sp.read_text(encoding='utf-8'))
        p=next(x for x in s['suggestions'] if x['kind']=='pause')
        selection={'suggestion_ids':[p['suggestion_id']]}
        path=write(selection,self.root/'selection.json')
        with self.assertRaisesRegex(ContractError,'audio/context review'): prepare_edit(sp,path)
        selection['pause_review']={p['suggestion_id']:{'audio_checked':True,'context_checked':True,
            'semantic_role':'emotion','evidence':'synthetic emotional pause'}}
        write(selection,path)
        with self.assertRaisesRegex(ContractError,'ordinary gap'): prepare_edit(sp,path)

    def test_input_drift_invalidates_suggestions(self):
        sp=self.suggestion(); sel=write({'suggestion_ids':[]},self.root/'selection.json')
        self.media.write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError,'changed'): prepare_edit(sp,sel)

    def test_selection_drift_and_wrong_approval_block_before_render(self):
        sp=self.suggestion(); sel=write({'suggestion_ids':[]},self.root/'selection.json')
        self.level_selection(sel)
        ep=write(prepare_edit(sp,sel),self.root/'edit.json')
        ap=write(build_approval_record('script',ep,approved_by='fixture',approved_at='2026-10-08T15:00:00+08:00'),self.root/'approval.json')
        with patch('video_creation_pipeline.video_use_edit_loop.outlet') as runner:
            with self.assertRaisesRegex(ContractError,'approval'): apply_edit(ep,ap,self.root/'cache',self.root/'out','absent-font')
            runner.assert_not_called()
        write({'suggestion_ids':['changed']},sel)
        with self.assertRaisesRegex(ContractError,'changed'): apply_edit(ep,ap,self.root/'cache',self.root/'out','absent-font')

    def test_cache_corruption_rejected(self):
        output=self.root/'cache.mock'; output.write_bytes(b'good')
        cp=write({'key':'a','artifacts':{'one':ref(output,'output')}},self.root/'cache.json')
        self.assertIsNotNone(verified_cache(cp,'a'))
        output.write_bytes(b'bad'); self.assertIsNone(verified_cache(cp,'a'))

    def test_unsupported_character_rejected(self):
        with patch('fontTools.ttLib.TTFont') as tf:
            tf.return_value.getBestCmap.return_value={ord('a'):'a'}
            with self.assertRaisesRegex(ContractError,'unsupported'): verify_font('fixture-font',['a\U0010ffff'])

    def test_resource_pressure_rejects_before_runner(self):
        runner=Mock()
        snap={'cpu_percent':99,'available_memory_bytes':32*1024**3,'available_memory_percent':90,'disk_busy_percent':0}
        with patch('video_creation_pipeline.execution_receipts._resolve_windows_tool_wrapper',return_value='ffmpeg'):
            with self.assertRaises(ResourcePressurePauseRequired):
                outlet(['ffmpeg','-version'],[],[],self.root/'receipt.json',runner=runner,snapshot=lambda:snap)
        runner.assert_not_called()

    def test_protected_response_blocks_an_otherwise_reviewed_pause_cut(self):
        self.ctx['protected_events'] = [dict(event_id='laugh',kind='laughter',start=5,end=5.8)]
        write(self.ctx,self.cp)
        sp = self.suggestion()
        suggestion = json.loads(sp.read_text('utf-8'))
        pause = next(r for r in suggestion['suggestions'] if r['kind']=='pause' and 4<r['delete_start']<6)
        selection = {'suggestion_ids':[pause['suggestion_id']],
            'pause_review':{pause['suggestion_id']:dict(semantic_role='ordinary',audio_checked=True,context_checked=True,evidence='synthetic mistaken decision')},
            'boundary_review':dict(opening_checked=True,ending_checked=True,cut_boundaries_checked=True,events_checked=True,evidence='synthetic'),
            'rough_audio_review':dict(levels_checked=True,clipping_checked=True,evidence='synthetic')}
        path = write(selection,self.root/'selection.json')
        with self.assertRaisesRegex(ContractError,'protected response'):
            prepare_edit(sp,path)

    def test_retime_requires_fresh_review_before_any_render(self):
        from video_creation_pipeline.timeline_dependencies import fingerprint
        self.ctx['mapping'][0]['source_end'] = 6
        self.ctx['speed_review'] = dict(audio_checked=True,meaning_preserved=True,evidence='synthetic retime review',mapping_sha256=fingerprint(self.ctx['mapping']))
        write(self.ctx,self.cp)
        sp = self.suggestion()
        path = write({'suggestion_ids':[]},self.root/'selection.json')
        self.level_selection(path)
        preview = prepare_edit(sp,path)
        self.assertTrue(preview['speed_review_pending'])
        ep = write(preview,self.root/'pending-edit.json')
        approval = write(build_approval_record('storyboard',ep,approved_by='synthetic',approved_at='2026-10-26T10:00:00+08:00'),self.root/'approval.json')
        with patch('video_creation_pipeline.video_use_edit_loop.outlet') as runner:
            with self.assertRaisesRegex(ContractError,'speed change'):
                apply_edit(ep,approval,self.root/'cache',self.root/'render','not-used-font')
            runner.assert_not_called()
        review = {**self.ctx['speed_review'],'mapping_sha256':fingerprint(preview['mapping_preview'])}
        write({'suggestion_ids':[],'post_edit_speed_review':review},path)
        self.level_selection(path)
        edl = prepare_edit(sp,path)
        projected = _project_context(self.ctx,edl,self.media)
        self.assertEqual(set(projected['sync_dependencies']['roles'].values()),{'pending'})

    def test_old_sync_receipt_fails_at_context_consumer(self):
        from video_creation_pipeline.timeline_dependencies import dependency_state
        self.ctx['sync_dependencies'] = dependency_state(self.ctx['mapping'])
        self.ctx['sync_dependencies']['roles'] = {k:'verified' for k in self.ctx['sync_dependencies']['roles']}
        self.ctx['mapping'][0]['source_start'] = .1
        self.ctx['mapping'][0]['source_end'] = 11.9
        from video_creation_pipeline.timeline_dependencies import fingerprint
        self.ctx['speed_review'] = dict(audio_checked=True,meaning_preserved=True,evidence='synthetic retime',mapping_sha256=fingerprint(self.ctx['mapping']))
        write(self.ctx,self.cp)
        with self.assertRaisesRegex(ContractError,'stale'):
            load_context(self.cp,ref(self.media,'media'),12)

if __name__=='__main__': unittest.main()
