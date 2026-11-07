"""Bounded video-use adapter. Media I/O stays behind the existing FFmpeg outlet."""
from __future__ import annotations

import html
import importlib.util
import math
import wave
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from .contract_io import (artifact_reference as ref, atomic_write_json as write,
                          canonical_json_sha256 as digest, file_sha256,
                          read_json_object as read, validate_artifact_reference as bound)
from .errors import ContractError
from .editing_presets import validate_protected_events
from .timeline_dependencies import validate_speed, validate_dependencies
from .subtitle_readability import subtitle_layout
from .execution_receipts import execute_ffmpeg_with_receipt, probe_media_metadata
from .review_resource_guard import ReviewResourcePolicy
from .video_review_executor import _retained_os_lock
from .video_use_review_handoff import validate_video_use_review_handoff

HELPER_SHA = '4311fb57fa248ab6ab1032984c6075062ddff2fdef860938ca91aecac372f781'
CONTEXT_SCHEMA = 'video_creation_pipeline.video_use_context.v1'


def number(value, label):
    if isinstance(value, bool): raise ContractError(f'{label} must be numeric')
    try: n = float(value)
    except (TypeError, ValueError) as exc: raise ContractError(f'{label} must be numeric') from exc
    if not math.isfinite(n) or n < 0: raise ContractError(f'{label} must be finite and nonnegative')
    return n


def interval(row, duration, start='start', end='end'):
    a, b = number(row.get(start), start), number(row.get(end), end)
    if not 0 <= a < b <= duration + 1e-6: raise ContractError('interval outside media duration')
    return a, b


def media_info(path):
    info = probe_media_metadata(path)
    videos = [s for s in info['streams'] if s.get('codec_type') == 'video']
    if not videos: raise ContractError('video stream required')
    v = videos[0]
    duration = number(v.get('duration') or info['format']['duration'], 'duration')
    rate = str(v.get('avg_frame_rate', '0/1')).split('/')
    fps = float(rate[0]) / float(rate[1])
    if fps <= 0: raise ContractError('positive frame rate required')
    return {'duration': duration, 'fps': fps, 'width': int(v['width']), 'height': int(v['height']),
            'audio': any(s.get('codec_type') == 'audio' for s in info['streams'])}


def load_context(path, media_reference, duration):
    """Explicit adapter input; never infer word timings from sentence length."""
    ctx = read(path, 'timed review context')
    if ctx.get('schema') != CONTEXT_SCHEMA: raise ContractError('unsupported timed context schema')
    bound(ctx.get('media', {}), 'context media')
    if ctx['media'] != media_reference: raise ContractError('context belongs to different media')
    maps = ctx.get('mapping')
    if not isinstance(maps, list) or not maps: raise ContractError('explicit source mapping required')
    cursor = 0
    source_durations = {}
    for m in maps:
        a,b = interval(m, duration, 'output_start', 'output_end')
        if abs(a-cursor) > 1e-6: raise ContractError('mapping must cover candidate contiguously')
        sa,sb = number(m.get('source_start'), 'source_start'), number(m.get('source_end'), 'source_end')
        if sb <= sa: raise ContractError('invalid source mapping')
        source = bound(m.get('source', {}), 'mapped source')
        if str(source) not in source_durations:
            source_durations[str(source)] = media_info(source)['duration']
        if sb > source_durations[str(source)] + 1e-6: raise ContractError('mapped source interval exceeds source duration')
        cursor = b
    if abs(cursor-duration) > .05: raise ContractError('mapping duration differs from candidate')
    validate_speed(maps,ctx.get('speed_review'))
    validate_protected_events(ctx.get('protected_events',[]),duration)
    if 'sync_dependencies' in ctx: validate_dependencies(maps,ctx['sync_dependencies'])
    for kind in ('words', 'subtitles', 'utterances'):
        rows = ctx.get(kind)
        if not isinstance(rows, list) or not rows: raise ContractError(f'missing {kind} timing; no inferred fallback')
        last = -1
        ids = set()
        for r in rows:
            a,b = interval(r, duration)
            if a < last-1e-6: raise ContractError(f'{kind} timing overlaps or is unordered')
            last = b
            if not isinstance(r.get('text'), str) or not r['text'].strip(): raise ContractError(f'{kind} text required')
            if not isinstance(r.get('id'), str) or not r['id'] or r['id'] in ids: raise ContractError(f'{kind} unique ids required')
            ids.add(r['id'])
    for w in ctx['words']:
        if not any(s['start'] <= w['start'] and s['end'] >= w['end'] for s in ctx['subtitles']):
            raise ContractError('word is not covered by a subtitle cue')
        if not any(s['start'] <= w['start'] and s['end'] >= w['end'] for s in ctx['utterances']):
            raise ContractError('word is not covered by an utterance')
    for cue in ctx['subtitles']:
        subtitle_layout(cue['text'],semantic_breaks=cue.get('semantic_breaks',()),protected_terms=cue.get('protected_terms',()),
                        duration_s=cue['end']-cue['start'],review=cue.get('readability_review'))
    return ctx


def source_ranges(ctx, start, end):
    rows = []
    for m in ctx['mapping']:
        a,b = max(start,m['output_start']), min(end,m['output_end'])
        if a >= b: continue
        scale = (m['source_end']-m['source_start'])/(m['output_end']-m['output_start'])
        rows.append({'source':m['source'], 'output_start':a, 'output_end':b,
                     'source_start':m['source_start']+(a-m['output_start'])*scale,
                     'source_end':m['source_start']+(b-m['output_start'])*scale})
    return rows


def outlet(command, inputs, outputs, receipt, *, runner=None, snapshot=None, operation='technical_qa'):
    try:
        return execute_ffmpeg_with_receipt(operation, command, [{'role':f'input-{i}','path':p} for i,p in enumerate(inputs)],
            [{'role':f'output-{i}','path':p} for i,p in enumerate(outputs)], receipt,
            guard_resource=True, resource_policy=ReviewResourcePolicy(ffmpeg_threads=1),
            runner=runner, resource_snapshot_provider=snapshot, capture_log_path=Path(receipt).with_suffix('.log'))
    except ContractError as exc:
        if callable(getattr(exc,'as_dict',None)):
            write(exc.as_dict(),Path(receipt).with_suffix('.blocked.json'))
        raise


def command_prefix():
    return ['ffmpeg','-nostdin','-v','error','-y','-threads','1','-filter_threads','1','-filter_complex_threads','1']


def verify_font(font, texts):
    from fontTools.ttLib import TTFont
    try:
        f=TTFont(str(font),fontNumber=0); chars=set(f.getBestCmap()); f.close()
    except Exception as exc: raise ContractError('readable explicit font required') from exc
    missing=sorted({c for t in texts for c in t if not c.isspace() and ord(c) not in chars})
    if missing: raise ContractError('unsupported font characters: '+','.join(f'U+{ord(c):04X}' for c in missing))


def verified_cache(path, key):
    if not path.exists(): return None
    try:
        row=read(path,'cache')
        if row.get('key') != key: return None
        for a in row['artifacts'].values(): bound(a,'cached artifact')
        return row
    except (ContractError, KeyError, TypeError): return None


def _upstream(handoff):
    helper=bound(handoff['video_use']['timeline_helper'],'timeline helper')
    if file_sha256(helper) != HELPER_SHA: raise ContractError('pinned timeline helper hash drift')
    # HEAD alone does not detect local dirty source.
    import subprocess
    result=subprocess.run(['git','-C',str(helper.parents[1]),'status','--porcelain=v1'],capture_output=True,text=True,timeout=15)
    if result.returncode or result.stdout.strip(): raise ContractError('video-use source must be clean')
    spec=importlib.util.spec_from_file_location('_video_use_timeline_adapter',helper)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def execute_windows(handoff_path, context_path, cache_dir, output_path, font_path, *, only=None, runner=None, snapshot=None):
    h=read(handoff_path,'review handoff'); validate_video_use_review_handoff(h)
    media=bound(h['candidate_video'],'candidate')
    info=media_info(media); ctx=load_context(context_path,h['candidate_video'],info['duration'])
    upstream=_upstream(h)
    font=Path(font_path).resolve()
    verify_font(font,[r['text'] for k in ('words','subtitles','utterances') for r in ctx[k]])
    windows=h['review_windows']
    if only:
        requested=set(only)
        if not requested.issubset({w['window_id'] for w in windows}): raise ContractError('unknown requested window')
        windows=[w for w in windows if w['window_id'] in requested]
    if len(windows)>32 or sum(w['end_s']-w['start_s'] for w in windows)>120:
        raise ContractError('bounded review exceeds 32 windows / 120 seconds')
    cache=Path(cache_dir).resolve(); cache.mkdir(parents=True,exist_ok=True)
    report=[]
    for w in windows:
        a,b=interval(w,info['duration'],'start_s','end_s')
        if b-a>20: raise ContractError('review window exceeds 20 seconds')
        local={k:[r for r in ctx[k] if r['end']>a and r['start']<b] for k in ('words','subtitles','utterances')}
        mapped=source_ranges(ctx,a,b)
        key=digest({'media':h['candidate_video']['sha256'],'window':w,'text':local,'mapping':mapped,
                    'font':file_sha256(font),'helper':HELPER_SHA,'adapter':file_sha256(__file__),'info':info})
        entry=cache/key; receipt=entry/'cache.json'
        with _retained_os_lock(cache/'.locks'/f'{key}.lock',timeout_s=5):
            hit=verified_cache(receipt,key)
            if hit is None:
                entry.mkdir(exist_ok=True)
                clip=entry/'audition.mp4'
                cmd=command_prefix()+['-ss',f'{a:.9f}','-i',str(media),'-t',f'{b-a:.9f}',
                    '-map','0:v:0','-map','0:a:0?','-vf','scale=640:-2','-c:v','libx264','-preset','ultrafast','-crf','23',
                    '-threads','1','-c:a','aac','-b:a','96k','-movflags','+faststart',str(clip)]
                outlet(cmd,[media],[clip],entry/'clip-receipt.json',runner=runner,snapshot=snapshot,operation='trim')
                # Single bounded decode for all strip frames and PCM, from cached audition.
                frames=[entry/f'frame-{i}.png' for i in range(6)]
                times=[(i+.5)*(b-a)/6 for i in range(6)]
                filters=['[0:v]split=6'+''.join(f'[v{i}]' for i in range(6))]
                filters += [f'[v{i}]trim=start={t:.9f},setpts=PTS-STARTPTS,scale=320:-2[f{i}]' for i,t in enumerate(times)]
                cmd=command_prefix()+['-i',str(clip),'-filter_complex',';'.join(filters)]
                for i,f in enumerate(frames): cmd+=['-map',f'[f{i}]','-frames:v','1','-threads','1',str(f)]
                wav=entry/'wave.wav'
                outputs=list(frames)
                if info['audio']:
                    cmd+=['-map','0:a:0','-ac','1','-ar','16000','-c:a','pcm_s16le','-threads','1',str(wav)]; outputs.append(wav)
                outlet(cmd,[clip],outputs,entry/'strip-receipt.json',runner=runner,snapshot=snapshot)
                import numpy as np
                from PIL import Image, ImageDraw, ImageFont
                if info['audio']:
                    with wave.open(str(wav),'rb') as f: pcm=np.frombuffer(f.readframes(f.getnframes()),dtype='<i2').astype(float)/32768
                    def envelope(_v,_a,_b,samples):
                        edges=np.linspace(0,len(pcm),samples+1,dtype=int)
                        env=np.array([np.sqrt(np.mean(pcm[x:y]**2)) if y>x else 0 for x,y in zip(edges,edges[1:])])
                        return env/max(float(env.max()),1e-9)
                else:
                    def envelope(_v,_a,_b,samples): return np.zeros(samples)
                upstream.extract_frames=lambda *args: frames
                upstream.compute_envelope=envelope
                upstream.words_in_range=lambda *args:[dict(r,type='word') for r in local['words']]
                # Word gaps are not proven silence; no misleading silence shading.
                upstream.find_silences=lambda *args,**kwargs: []
                upstream.load_font=lambda size:ImageFont.truetype(str(font),max(size,18))
                upstream.tempfile=SimpleNamespace(TemporaryDirectory=lambda:nullcontext(entry))
                strip=entry/'timeline.png'
                upstream.render_timeline(Path('candidate'),a,b,strip,6,Path(context_path))
                if not info['audio']:
                    im=Image.open(strip).convert('RGB'); d=ImageDraw.Draw(im)
                    d.rectangle((40,270,im.width-40,460),fill=(28,28,34))
                    d.text((65,330),'NO AUDIO TRACK / 无音轨，不能判断静音或爆音',font=ImageFont.truetype(str(font),28),fill='white')
                    im.save(strip)
                annotations={'window':w,'frame_sample_times':[a+t for t in times],'source_ranges':mapped,**local,'audio_state':'present' if info['audio'] else 'no_audio',
                    'timing_basis':ctx.get('timing_basis','provided_unverified'),'human_review':'pending'}
                write(annotations,entry/'annotations.json')
                page='<meta charset="utf-8"><title>Bounded seam review</title><style>body{font:20px sans-serif;max-width:1100px;margin:auto}img,video{width:100%}</style><h1>候选接缝审阅 · 人工结论待定</h1><img src="timeline.png"><video controls src="audition.mp4"></video>'
                page+=f'<p>候选 {a:.3f}–{b:.3f}s；'+('有音轨' if info['audio'] else '无音轨')+'</p>'
                for r in local['subtitles']: page+=f'<p>{r["start"]:.3f}–{r["end"]:.3f}: {html.escape(r["text"])}</p>'
                page+='<p>波形是局部归一化 RMS，不能据此推断绝对响度；词间空隙不等于静音。源映射见 annotations.json。</p>'
                (entry/'review.html').write_text(page,encoding='utf-8')
                # Check mutable inputs before publishing a reusable cache receipt.
                validate_video_use_review_handoff(read(handoff_path,'review handoff'))
                if load_context(context_path,h['candidate_video'],info['duration']) != ctx: raise ContractError('context drift during review')
                hit={'key':key,'artifacts':{n:ref(entry/n,n) for n in ['audition.mp4','timeline.png','annotations.json','review.html']}}
                write(hit,receipt); disposition='rendered'
            else: disposition='reused'
        report.append({'window_id':w['window_id'],'disposition':disposition,'key':key,'artifacts':hit['artifacts']})
    result={'schema':'video_creation_pipeline.video_use_review_execution.v1','candidate_only':True,
            'media':h['candidate_video'],'context':ref(context_path,'context'),'handoff':ref(handoff_path,'handoff'),
            'windows':report,'technical_artifacts':'generated','human_review':'pending','real_acceptance':'pending'}
    write(result,output_path)
    return result
