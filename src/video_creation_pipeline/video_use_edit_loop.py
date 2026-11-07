"""Reversible, candidate-only suggestions and hash-approved incremental edits."""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from .contract_io import (artifact_reference as ref, atomic_write_json as write,
                          canonical_json_sha256 as digest, file_sha256, payload_sha256,
                          read_json_object as read, validate_artifact_reference as bound)
from .creative_gates import validate_approval_record
from .errors import ContractError
from .editing_presets import (POLICY as EDITING_POLICY, preferred_take, validate_take_review,
    validate_edit_boundaries, validate_boundary_review, splice_fades, fade_filter, boundary_windows)
from .timeline_dependencies import dependency_state
from .spoken_rough_cut import _nle_segments, _validate_segment_complement
from .spoken_pacing import (DEFAULT_NATURAL_PACING_POLICY, pause_cut_window,
                            pause_recommendation, validate_pause_compression)
from .video_review_executor import _retained_os_lock
from .video_use_review_handoff import build_video_use_review_handoff, validate_video_use_review_handoff
from .video_use_review_execution import (load_context, media_info, source_ranges,
                                         outlet, command_prefix, verified_cache, execute_windows)

SUGGESTIONS='video_creation_pipeline.video_use_suggestions.v1'
EDITS='video_creation_pipeline.video_use_edit_plan.v1'


def _seal(row):
    row['sha256']=payload_sha256(row,'sha256'); return row


def _read_sealed(path,schema):
    row=read(path,schema)
    if row.get('schema')!=schema or row.get('sha256')!=payload_sha256(row,'sha256'):
        raise ContractError('edit artifact integrity mismatch')
    return row


def build_suggestions(handoff_path,context_path):
    h=read(handoff_path,'handoff'); validate_video_use_review_handoff(h)
    info=media_info(bound(h['candidate_video'],'candidate'))
    ctx=load_context(context_path,h['candidate_video'],info['duration'])
    rows=[]; fps=info['fps']
    def add(kind,a,b,reason,uncertainty,**extra):
        # Snap inward: never expand a proposed deletion beyond evidence.
        import math
        a=math.ceil((a-1e-8)*fps)/fps; b=math.floor((b+1e-8)*fps)/fps
        if b<=a: return
        rows.append({'suggestion_id':f'suggestion-{len(rows)+1:03d}','kind':kind,
            'delete_start':a,'delete_end':b,'source_ranges':source_ranges(ctx,a,b),
            'reason':reason,'uncertainty':uncertainty,'status':'candidate_only',
            'default_action':'keep_pending_review',**extra})
    # No English filler list. Gaps are timing evidence, not silence or mistakes.
    for left,right in zip(ctx['words'],ctx['words'][1:]):
        same_utterance = any(u['start'] <= left['start'] and u['end'] >= right['end'] for u in ctx['utterances'])
        where = 'within_sentence' if same_utterance else 'between_sentences'
        recommendation = pause_recommendation(left['end'],right['start'],where)
        if recommendation['action'] == 'compress':
            preserve = recommendation['preserve_s']
            a,b = pause_cut_window(left['end'],right['start'],preserve,where)
            add('pause',a,b,
                '词时间存在较长间隔；按口播预设保留气口，帧边界向内取整',
                ['间隔可能是呼吸、强调、情绪或非语言声音；必须试听'],
                evidence={'left_word':left['id'],'right_word':right['id']},
                pause_start=left['end'],pause_end=right['start'],pause_where=where,
                preserve_s=preserve,requires_pause_review=True)
    def norm(text): return re.sub(r'[\s，。！？、,.!?；;：:]+','',text)
    seen={}
    for u in ctx['utterances']:
        text=norm(u['text'])
        if len(text)>=3: seen.setdefault(text,[]).append(u)
    for text,takes in seen.items():
        if len(takes)<2: continue
        preferred=preferred_take(takes)
        for alternative in takes:
            if alternative is preferred: continue
            add('repetition',alternative['start'],alternative['end'],'普通重录优先最后一遍候选；重复或强调仍需上下文复核',
                ['相同文字不证明可删；须核对完整性、表达与独有信息'],
                evidence={'retained_utterance':preferred['id'],'candidate_utterance':alternative['id'],
                          'preferred':preferred,'alternative':alternative,'text':text},requires_meaning_review=True)
    groups={}
    for u in ctx['utterances']:
        if u.get('take_group'): groups.setdefault(u['take_group'],[]).append(u)
    for group,takes in groups.items():
        if len(takes)<2: continue
        # Supplied explicit annotations only; no invented semantic/voice scores.
        preferred=preferred_take(takes)
        for alternative in takes:
            if alternative is preferred: continue
            if norm(preferred['text'])==norm(alternative['text']): continue
            add('alternative_take',alternative['start'],alternative['end'],
                '普通重录首选最后一遍；显式完整性或质量例外触发比较',
                ['质量标注未独立验证','两遍可能含独有信息、限定或转折；逐句人工比较后才能删'],
                evidence={'group':group,'preferred':preferred,'alternative':alternative},
                requires_meaning_review=True)
    return _seal({'schema':SUGGESTIONS,'handoff':ref(handoff_path,'handoff'),'context':ref(context_path,'context'),
        'media':h['candidate_video'],'duration':info['duration'],'fps':fps,'suggestions':rows,
        'pacing_policy':deepcopy(DEFAULT_NATURAL_PACING_POLICY),
        'editing_policy':deepcopy(EDITING_POLICY),
        'candidate_only':True,'source_modified':False,'human_review':'pending','synthetic_fixture_only':ctx.get('synthetic_fixture_only',False)})


def validate_suggestions(path):
    row=_read_sealed(path,SUGGESTIONS)
    hp=bound(row['handoff'],'handoff'); cp=bound(row['context'],'context'); bound(row['media'],'media')
    if build_suggestions(hp,cp)!=row: raise ContractError('suggestion inputs or derivation drifted')
    return row


def prepare_edit(suggestions_path,selection_path):
    from .audio_edit_quality import validate_rough_audio_review, validate_clip_gains
    s=validate_suggestions(suggestions_path); selection=read(selection_path,'selection')
    chosen=selection.get('suggestion_ids')
    if not isinstance(chosen,list) or len(chosen)!=len(set(chosen)): raise ContractError('unique selected suggestion ids required')
    by_id={r['suggestion_id']:r for r in s['suggestions']}
    if not set(chosen).issubset(by_id): raise ContractError('unknown selected suggestion')
    ctx=read(bound(s['context'],'context'),'context')
    h=read(bound(s['handoff'],'handoff'),'handoff')
    packaging=read(bound(h['source_packaging_plan'],'packaging'),'packaging')
    pause_reviews=selection.get('pause_review',{})
    if not isinstance(pause_reviews,dict): raise ContractError('pause_review must map suggestion ids to reviews')
    deletes=[]
    for ident in chosen:
        r=by_id[ident]; a,b=r['delete_start'],r['delete_end']
        if r['kind'] == 'pause':
            validate_pause_compression(r['pause_start'],r['pause_end'],r['preserve_s'],r['pause_where'],
                                       pause_reviews.get(ident))
        if r.get('requires_meaning_review') and not str(selection.get('meaning_review',{}).get(ident,'')).strip():
            raise ContractError('alternative take requires explicit complete-meaning review note')
        if r.get('requires_meaning_review'):
            validate_take_review(selection.get('take_review',{}).get(ident),{'start':a,'end':b},
                [{'start':by_id[k]['delete_start'],'end':by_id[k]['delete_end']} for k in chosen])
        for word in ctx['words']:
            if min(b,word['end'])>max(a,word['start'])+1e-7 and not (a<=word['start']+1e-7 and b>=word['end']-1e-7):
                raise ContractError('selection would cut through a word')
        deletes.append({'start':a,'end':b})
    deletes.sort(key=lambda r:r['start'])
    events=ctx.get('protected_events',[])
    validate_edit_boundaries(deletes,ctx['words'],events)
    if deletes:
        validate_boundary_review(selection.get('boundary_review'))
        validate_rough_audio_review(selection.get('rough_audio_review'))
    if any(a['end']>b['start'] for a,b in zip(deletes,deletes[1:])): raise ContractError('selected deletion ranges overlap')
    keeps=[]; cursor=0
    for d in deletes:
        if d['start']>cursor: keeps.append({'start':cursor,'end':d['start']})
        cursor=d['end']
    if cursor<s['duration']: keeps.append({'start':cursor,'end':s['duration']})
    _validate_segment_complement(deletes,keeps,s['duration'])
    from .segment_leveling import validate_leveling
    level_review = selection.get('rough_audio_review', {}).get('segment_leveling')
    audio_gate = validate_leveling(level_review, keeps=keeps, source=s['media'],
                                   gains=selection.get('clip_gains', []), speech_windows=ctx['words'])
    scenes=[]; out=0
    for scene in packaging['scenes']:
        ranges=[]
        for k in keeps:
            a,b=max(scene['start_s'],k['start']),min(scene['end_s'],k['end'])
            if a<b: ranges.append({'start':a,'end':b})
        if not ranges: raise ContractError('whole-scene removal requires a separate storyboard decision')
        length=sum(r['end']-r['start'] for r in ranges)
        scenes.append({'scene_id':scene['scene_id'],'start_s':out,'end_s':out+length,'keep':ranges,
            'changed':ranges!=[{'start':scene['start_s'],'end':scene['end_s']}]})
        out+=length
    result = {'schema':EDITS,'suggestions':ref(suggestions_path,'suggestions'),'selection':ref(selection_path,'selection'),
        'media':s['media'],'delete':deletes,'keep':keeps,'scenes':scenes,'duration':out,'fps':s['fps'],
        'audio_splices':splice_fades(keeps,ctx['words'],events),
        'clip_gains':validate_clip_gains(selection.get('clip_gains',[]),keeps),
        'segment_leveling':level_review, 'rough_cut_audio_gate':audio_gate,
        'boundary_review_windows':boundary_windows(keeps,out),
        'nle_segments':_nle_segments(Path(s['media']['path']),s['media'],keeps),
        'candidate_only':True,'synthetic_fixture_only':s['synthetic_fixture_only'],'approval_required':True}
    if selection.get('post_edit_speed_review') is not None:
        result['post_edit_speed_review'] = selection['post_edit_speed_review']
    from .timeline_dependencies import validate_speed
    projected = _project_context(ctx,result,Path(s['media']['path']))
    result['mapping_preview'] = projected['mapping']
    speed_changed = any(abs((m['source_end']-m['source_start'])/(m['output_end']-m['output_start'])-1)>1e-6 for m in projected['mapping'])
    result['speed_review_pending'] = speed_changed and projected.get('speed_review') is None
    if not result['speed_review_pending']:
        validate_speed(projected['mapping'],projected.get('speed_review'))
    return _seal(result)


def _project_context(ctx,edl,new_media):
    projected={k:v for k,v in ctx.items() if k not in ('words','subtitles','utterances','mapping','media')}
    projected.update(media=ref(new_media,'revised'),mapping=[])
    cursor=0
    for k in edl['keep']:
        for m in source_ranges(ctx,k['start'],k['end']):
            m=dict(m); m['output_start']=cursor+m['output_start']-k['start']; m['output_end']=cursor+m['output_end']-k['start']
            projected['mapping'].append(m)
        cursor+=k['end']-k['start']
    for kind in ('words','subtitles','utterances','protected_events'):
        rows=[]
        for r in ctx.get(kind,[]):
            pieces=[]; cursor=0
            for k in edl['keep']:
                a,b=max(r['start'],k['start']),min(r['end'],k['end'])
                if a<b: pieces.append((cursor+a-k['start'],cursor+b-k['start']))
                cursor+=k['end']-k['start']
            if pieces:
                # Keep same utterance text across a removed pause, not repeated captions.
                rows.append({**r,'start':pieces[0][0],'end':pieces[-1][1]})
        projected[kind]=rows
    projected['sync_dependencies']=dependency_state(projected['mapping'])
    projected.pop('speed_review',None)
    if edl.get('post_edit_speed_review') is not None:
        projected['speed_review'] = edl['post_edit_speed_review']
    # A previous speed check cannot survive a mapping change, even after trimming.
    if any(abs((m['source_end']-m['source_start'])/(m['output_end']-m['output_start'])-1)>1e-6 for m in projected['mapping']):
        projected['speed_review_required']=True
    return projected


def apply_edit(edit_path,approval_path,cache_dir,output_dir,font_path,*,runner=None,snapshot=None):
    edl=_read_sealed(edit_path,EDITS)
    sp=bound(edl['suggestions'],'suggestions'); selection=bound(edl['selection'],'selection')
    if prepare_edit(sp,selection)!=edl: raise ContractError('edit plan inputs drifted')
    approval=read(approval_path,'approval'); validate_approval_record(approval)
    if approval['target']!='storyboard' or approval['artifact']!=ref(edit_path,'edit plan'):
        raise ContractError('approval must bind this exact edit plan as storyboard')
    s=validate_suggestions(sp); h=read(bound(s['handoff'],'handoff'),'handoff'); ctx=read(bound(s['context'],'context'),'context')
    from .timeline_dependencies import validate_speed
    validate_speed(edl['mapping_preview'],edl.get('post_edit_speed_review'))
    media=bound(edl['media'],'source candidate'); info=media_info(media)
    out=Path(output_dir).resolve(); cache=Path(cache_dir).resolve(); cache.mkdir(parents=True,exist_ok=True)
    task_key=digest({'edl':ref(edit_path,'edl'),'approval':ref(approval_path,'approval'),'adapter':file_sha256(__file__),
                     'review_adapter':file_sha256(Path(__file__).with_name('video_use_review_execution.py')),'font':file_sha256(font_path)})
    complete=out/'edit-result.json'
    if complete.exists():
        result=read(complete,'existing edit result')
        if result['key']!=task_key: raise ContractError('output directory belongs to a different edit; use a fresh directory')
        for a in result['artifacts'].values(): bound(a,'existing edit artifact')
        for a in result['segments']: bound(a,'existing rendered segment')
        for row in result['review']['windows']:
            for a in row['artifacts'].values(): bound(a,'existing review artifact')
        return {**result,'invocation':'reused','rendered_segments':[],
                'reused_segments':[r['scene_id'] for r in edl['scenes']]}
    if out.exists() and any(out.iterdir()): raise ContractError('nonempty edit output; preserve partial evidence and use fresh directory')
    out.mkdir(parents=True,exist_ok=True)
    rendered=[]; reused=[]; segments=[]
    for scene in edl['scenes']:
        key=digest({'media':edl['media']['sha256'],'keep':scene['keep'],'fps':edl['fps'],
                    'audio_splices':[r for r in edl['audio_splices'] if r['end']>min(k['start'] for k in scene['keep']) and r['start']<max(k['end'] for k in scene['keep'])],
                    'clip_gains':[r for r in edl['clip_gains'] if r['end']>min(k['start'] for k in scene['keep']) and r['start']<max(k['end'] for k in scene['keep'])],
                    'geometry':[info['width'],info['height']],'adapter':file_sha256(__file__)})
        entry=cache/'segments'/key; cp=entry/'cache.json'
        with _retained_os_lock(cache/'.locks'/f'{key}.lock',timeout_s=5):
            hit=verified_cache(cp,key)
            if hit is None:
                entry.mkdir(parents=True,exist_ok=True); segment=entry/'segment.mkv'
                filters=[]; n=len(scene['keep'])
                # Each kept range uses input seeking so long prefixes are not decoded.
                cmd=command_prefix()
                for r in scene['keep']: cmd+=['-ss',f'{r["start"]:.9f}','-t',f'{r["end"]-r["start"]:.9f}','-threads','1','-i',str(media)]
                for i,r in enumerate(scene['keep']):
                    filters.append(f'[{i}:v]setpts=PTS-STARTPTS,fps={edl["fps"]},scale={info["width"]}:{info["height"]}[v{i}]')
                    if info['audio']:
                        fades={'start':r['start'],'end':r['end'],'fade_in_s':0.0,'fade_out_s':0.0}
                        for original in edl['audio_splices']:
                            if abs(r['start']-original['start'])<1e-7: fades['fade_in_s']=original['fade_in_s']
                            if abs(r['end']-original['end'])<1e-7: fades['fade_out_s']=original['fade_out_s']
                        fade=fade_filter(fades)
                        gains=[g for g in edl['clip_gains'] if g['start']<=r['start'] and g['end']>=r['end']]
                        gain=f",volume={gains[0]['gain_db']:.6f}dB" if gains else ''
                        filters.append(f'[{i}:a]asetpts=PTS-STARTPTS,aresample=48000'+gain+(','+fade if fade else '')+f'[a{i}]')
                labels=''.join(f'[v{i}]'+(f'[a{i}]' if info['audio'] else '') for i in range(n))
                filters.append(labels+f'concat=n={n}:v=1:a={int(info["audio"])}[v]'+('[a]' if info['audio'] else ''))
                cmd+=['-filter_complex',';'.join(filters),'-map','[v]']
                if info['audio']: cmd+=['-map','[a]','-c:a','pcm_s16le']
                cmd+=['-c:v','libx264','-preset','ultrafast','-crf','20','-pix_fmt','yuv420p','-threads','1',str(segment)]
                outlet(cmd,[media],[segment],entry/'render-receipt.json',runner=runner,snapshot=snapshot,operation='render')
                hit={'key':key,'artifacts':{'segment':ref(segment,'segment')}}; write(hit,cp); rendered.append(scene['scene_id'])
            else: reused.append(scene['scene_id'])
        segments.append(hit['artifacts']['segment'])
    concat=out/'concat.txt'
    # FFmpeg concat escaping, not shell escaping.
    concat.write_text('\n'.join("file '"+str(Path(a['path']).as_posix()).replace("'","'\\''")+"'" for a in segments),encoding='utf-8')
    revised=out/'revised.mp4'; cmd=command_prefix()+['-f','concat','-safe','0','-i',str(concat),'-map','0:v','-map','0:a?',
        '-c:v','copy','-c:a','aac','-b:a','128k','-threads','1','-movflags','+faststart',str(revised)]
    outlet(cmd,[Path(a['path']) for a in segments],[revised],out/'concat-receipt.json',runner=runner,snapshot=snapshot,operation='concat')
    # Full decode once for this revised candidate. This is technical evidence only.
    decode=out/'decode.framehash'
    outlet(command_prefix()+['-i',str(revised),'-map','0:v:0','-map','0:a:0?','-f','framehash','-threads','1',str(decode)],
           [revised],[decode],out/'decode-receipt.json',runner=runner,snapshot=snapshot)
    actual=media_info(revised)
    if abs(actual['duration']-edl['duration'])>1/edl['fps']+.01: raise ContractError('revised duration mismatch')
    projected=_project_context(ctx,edl,revised); context=write(projected,out/'context.json')
    packaging=write({'schema':'video_creation_pipeline.video_packaging_plan.v1','scenes':[
        {k:r[k] for k in ('scene_id','start_s','end_s')} for r in edl['scenes']]},out/'packaging.json')
    upstream=Path(h['video_use']['timeline_helper']['path']).parents[1]
    review=build_video_use_review_handoff(upstream,revised,packaging,window_radius_s=.6)
    changed={i for i,r in enumerate(edl['scenes']) if r['changed']}
    # Initial identity render checks all; edits check both sides of changed scenes.
    indices={i for c in changed for i in (c,c+1)} if changed else set(range(len(review['review_windows'])))
    selected=[w['window_id'] for i,w in enumerate(review['review_windows']) if i in indices]
    removed=0.0
    for deleted in edl['delete']:
        point=deleted['start']-removed
        removed+=deleted['end']-deleted['start']
        window_id=f'review-{len(review["review_windows"])+1:03d}'
        review['review_windows'].append({'window_id':window_id,'kind':'approved_internal_splice',
            'focus_s':point,'start_s':max(0,point-.6),'end_s':min(edl['duration'],point+.6)})
        selected.append(window_id)
    review['handoff_sha256']=payload_sha256(review,'handoff_sha256')
    validate_video_use_review_handoff(review)
    hp=write(review,out/'handoff.json')
    reviewed=execute_windows(hp,context,cache/'review',out/'review.json',font_path,only=selected,runner=runner,snapshot=snapshot)
    bound(edl['media'],'source candidate'); validate_approval_record(read(approval_path,'approval'))
    if prepare_edit(sp,selection)!=edl: raise ContractError('inputs changed during render')
    result={'schema':'video_creation_pipeline.video_use_edit_result.v1','key':task_key,'invocation':'rendered','candidate_only':True,
        'synthetic_fixture_only':edl['synthetic_fixture_only'],'approval':ref(approval_path,'approval'),'edl':ref(edit_path,'edl'),
        'rendered_segments':rendered,'reused_segments':reused,'segments':segments,'review':reviewed,
        'affected_scene_ids':[r['scene_id'] for r in edl['scenes'] if r['changed']],
        'checked_windows':selected,'human_review':'pending','real_acceptance':'pending',
        'artifacts':{p.name:ref(p,p.name) for p in [revised,decode,context,packaging,hp,out/'review.json']}}
    write(result,complete); return result
