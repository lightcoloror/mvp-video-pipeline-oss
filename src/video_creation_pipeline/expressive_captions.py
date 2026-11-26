"""Small, cue-bound ASS laughter layer over the locked program and guarded outlet."""
from __future__ import annotations

import copy
import math
import re
import shutil
import unicodedata
from pathlib import Path

from .contract_io import (artifact_reference, atomic_write_json, atomic_write_text,
                          file_sha256, read_json_object, validate_artifact_reference)
from .errors import ContractError
from .execution_receipts import execute_ffmpeg_with_receipt
from .final_layout_safety import validate_final_layout
from .progress_overlay import _filter_path, _integer, _media_matches
from .remotion_project import build_remotion_timeline_preview
from .typography_gates import build_typography_gate
from . import variety_stickers

SCHEMA = 'video_creation_pipeline.expressive_captions.v1'


def _plain(value, name, limit=80):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(c in '{}\\\n\r' or unicodedata.category(c).startswith('C') for c in value)):
        raise ContractError(f'{name} must be short plain text without ASS commands')
    return value.strip()


def _number(value, name, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise ContractError(f'{name} must be finite in {low}..{high}')
    return value


def _font(path, texts):
    try:
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise ContractError('expressive captions need the optional expressive-fonts dependency') from exc
    with TTFont(path, fontNumber=0) as font:
        family = font['name'].getDebugName(1)
        if not family or any(c in family for c in ',{}\\\n\r'):
            raise ContractError('font family is unsafe for ASS')
        cmap = font.getBestCmap() or {}
        missing = sorted({c for text in texts for c in text if not c.isspace() and ord(c) not in cmap})
        if missing:
            raise ContractError('font lacks caption glyphs: ' + ''.join(missing))
        # Conservative envelope assumes <= 2 em per glyph; reject wider custom fonts.
        em = font['head'].unitsPerEm
        if any(font['hmtx'][cmap[ord(c)]][0] > 2*em for text in texts for c in text if not c.isspace()):
            raise ContractError('font advances exceed the conservative caption envelope')
        return family


def build(program, packaging, layout, events_path, spec_path, workspace_root):
    root, source = Path(workspace_root).resolve(), Path(program).resolve()
    source.relative_to(root)
    timeline = read_json_object(packaging, 'packaging')
    build_remotion_timeline_preview(packaging, composition_id='ExpressiveCaptions')
    width, height, fps = (timeline['output'][k] for k in ('width', 'height', 'fps'))
    for value, name in ((width, 'width'), (height, 'height'), (fps, 'fps')):
        _integer(value, name, 1)
    if width % 2 or height % 2 or fps > 60:
        raise ContractError('even canvas dimensions and fps <= 60 required')
    cursor, scenes = 0, {}
    for scene in timeline['scenes']:
        sid, a, b = scene['scene_id'], scene['start_s'], scene['end_s']
        _number(a, 'scene start', 0, 86400)
        _number(b, 'scene end', 0, 86400)
        if sid in scenes or abs(a*fps-cursor) > 1e-6 or abs(b*fps-round(b*fps)) > 1e-6 or b <= a:
            raise ContractError('locked timeline has gaps, overlaps or non-frame boundaries')
        scenes[sid] = (cursor, round(b*fps))
        cursor = round(b*fps)
    if not scenes:
        raise ContractError('locked timeline needs scenes')
    media = _media_matches(source, width, height, fps, cursor/fps)
    if any(s['codec_type'] == 'subtitle' for s in media['streams']):
        raise ContractError('render embedded stable subtitles into the base program before expressive packaging')
    events = read_json_object(events_path, 'sound events')
    if events.get('schema') != SCHEMA + '.events':
        raise ContractError('unsupported sound event schema')
    for key, expected in (('program', source), ('packaging', Path(packaging).resolve())):
        actual = validate_artifact_reference(events[key], key)
        if Path(actual).resolve() != expected:
            raise ContractError('events belong to a different locked edit')
    evidence = validate_artifact_reference(events['evidence'], 'event evidence')
    status = events.get('review_status')
    if status not in ('synthetic_fixture', 'human_audio_verified', 'timing_candidate'):
        raise ContractError('sound events require synthetic or human audio verification evidence')
    if status == 'timing_candidate':
        ack=events.get('timing_candidate_ack',{})
        if (set(ack) != {'event_confirmation','timing_method','requires_human_audio_review'}
                or ack.get('requires_human_audio_review') is not True):
            raise ContractError('timing candidate needs explicit unverified timing acknowledgement')
        _plain(ack['event_confirmation'],'event confirmation',500)
        _plain(ack['timing_method'],'candidate timing method',500)
    spec = read_json_object(spec_path, 'expressive spec')
    if set(spec) != {'font_file', 'font_size_px', 'typography_profile', 'speakers', 'effects'}:
        raise ContractError('unknown or missing expressive spec fields')
    typography = build_typography_gate(spec['typography_profile'], width=width, height=height)
    size = _integer(spec['font_size_px'], 'font_size_px', 1)
    if size < typography['roles'].get('motion_label', typography['roles'].get('structure', size+1)):
        raise ContractError('caption is below the existing typography minimum')
    speakers = spec['speakers']
    if not isinstance(speakers, dict) or not speakers:
        raise ContractError('explicit speaker identities are required')
    for sid, speaker in speakers.items():
        if set(speaker) != {'label', 'color', 'identity_note', 'color_reason'}:
            raise ContractError('speaker requires label, color and identity/color reasons')
        for key in ('label', 'identity_note', 'color_reason'):
            _plain(speaker[key], key, 300)
        if not re.fullmatch('[0-9A-Fa-f]{6}', speaker['color']):
            raise ContractError('speaker color must be six-digit RGB hex')
    rows = events.get('events')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ContractError('provide 1..100 explicit sound events')
    if not isinstance(spec['effects'], dict):
        raise ContractError('effects must map event IDs to visual choices')
    base_layout = read_json_object(layout, 'final layout')
    if (root/base_layout['candidate']['path']).resolve() != source or base_layout['candidate']['expected_sha256'] != file_sha256(source):
        raise ContractError('layout must describe this exact program')
    if abs(base_layout['duration_seconds']-cursor/fps) > 1e-6:
        raise ContractError('layout duration differs from locked edit')
    combined = copy.deepcopy(base_layout)
    normalized, seen = [], set()
    for event in rows:
        if set(event) - {'syllable_frames','syllable_strengths'} != {'event_id', 'scene_id', 'kind', 'speaker_id', 'start_frame', 'end_frame', 'text', 'fallback_text'}:
            raise ContractError('event fields must explicitly describe a measured sound interval')
        eid = _plain(event['event_id'], 'event_id')
        if eid in seen or event['kind'] != 'laughter' or event['speaker_id'] not in speakers:
            raise ContractError('duplicate event, unsupported kind or unknown speaker')
        seen.add(eid)
        start, end = (_integer(event[k], k) for k in ('start_frame', 'end_frame'))
        scene = scenes.get(event['scene_id'])
        if not scene or not scene[0] <= start < end <= scene[1]:
            raise ContractError('sound event must stay inside its bound scene')
        text = _plain(event['text'], 'laughter text', 24)
        fallback = _plain(event['fallback_text'], 'fallback text')
        effect = spec['effects'].get(eid)
        if not isinstance(effect, dict) or set(effect) != {'mode', 'x_px', 'y_px', 'end_scale', 'rise_px', 'tail_frames'}:
            raise ContractError('each event needs one complete effect; no independent sound timestamps')
        mode = effect['mode']
        if mode not in ('stable', 'grow', 'float_fade', *variety_stickers.MODES):
            raise ContractError('unsupported effect mode')
        x = _number(effect['x_px'], 'x_px', 0, width)
        y = _number(effect['y_px'], 'y_px', 0, height)
        scale = _number(effect['end_scale'], 'end_scale', 1, 1.35)
        rise = _number(effect['rise_px'], 'rise_px', 0, height*.1)
        tail = _integer(effect['tail_frames'], 'tail_frames')
        if tail > round(fps*.5) or end+tail > scene[1]:
            raise ContractError('visual tail must be <= 0.5 s and stay inside the same scene')
        if (mode == 'stable' and (scale != 1 or rise or tail)) or (mode == 'grow' and (rise or tail)):
            raise ContractError('stable/grow must not silently ignore motion parameters')
        if mode == 'float_fade' and (not rise or not tail):
            raise ContractError('float_fade needs a positive rise and visual tail')
        # Fixed an8 anchor: reserve the union of maximum scale and complete travel.
        # Deliberately generous; this is a planning bound, not pixel-level visual proof.
        pad = math.ceil(8*scale)
        half = len(text)*size*scale + pad
        rect = {'x': (x-half)/width, 'y': (y-rise-pad)/height,
                'width': 2*half/width, 'height': (rise+2*size*scale+2*pad)/height}
        if mode in variety_stickers.MODES:
            rect = variety_stickers.geometry(event,effect,size,width,height)
            if size*.62*min(event.get('syllable_strengths',[1.,1.,1.])) < typography['roles']['motion_label']:
                raise ContractError('variety entry is below the existing typography minimum')
        combined['elements'].append({'element_id': 'expressive-'+eid, 'role': 'information_motion',
            'start_seconds': start/fps, 'end_seconds': (end+tail)/fps, 'rect': rect, 'critical_content': True})
        normalized.append({**event, **effect, 'text': text, 'fallback_text': fallback,
                           'visual_end_frame': end+tail, 'color': speakers[event['speaker_id']]['color'],
                           'speaker_label': speakers[event['speaker_id']]['label'], 'rect': rect})
    if set(spec['effects']) != seen:
        raise ContractError('effects contain unmatched events')
    variety = any(row['mode'] in variety_stickers.MODES for row in normalized)
    if variety and not all(row['mode'] in variety_stickers.MODES for row in normalized):
        raise ContractError('render ASS and variety layers in separate bound packaging passes')
    if 'information_motion' not in combined['required_roles']:
        combined['required_roles'].append('information_motion')
    layout_check = validate_final_layout(combined, root)
    font = artifact_reference(spec['font_file'], 'caption font')
    family = _font(font['path'], [r['text'] for r in normalized])
    return {'schema': SCHEMA, 'workspace_root': str(root),
            **{k: artifact_reference(p, k) for k,p in [('program',source), ('packaging',packaging),
                ('layout',layout), ('events',events_path), ('spec',spec_path), ('evidence',evidence)]},
            'font': font, 'font_family': family, 'font_size_px': size, 'typography': typography,
            'output': {'width': width, 'height': height, 'fps': fps, 'frames': cursor, 'duration_s': cursor/fps},
            'captions': normalized, 'layout_with_captions': combined, 'layout_check': layout_check,
            'variety_sources':variety_stickers.source_refs() if variety else None,
            'implementation_sha256': file_sha256(__file__), 'review_status': status,
            'candidate_only': True, 'human_review': 'pending'}


def validate(plan_path):
    plan = read_json_object(plan_path, 'expressive plan')
    if plan.get('schema') != SCHEMA:
        raise ContractError('unsupported expressive plan')
    refs = {k: validate_artifact_reference(plan[k], k) for k in ('program','packaging','layout','events','spec')}
    expected = build(**{k: refs[k] for k in ('program','packaging','layout')},
                     events_path=refs['events'], spec_path=refs['spec'], workspace_root=plan['workspace_root'])
    if plan != expected:
        raise ContractError('expressive plan or dependency drift; rebuild from locked edit')
    return plan


def _ass_time(frame, fps):
    # libass uses centiseconds: floor both edges so no event starts a frame late.
    cs = frame*100//fps
    return f'{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}'


def ass_document(plan):
    o, size = plan['output'], plan['font_size_px']
    lines = ['[Script Info]', 'ScriptType: v4.00+', f"PlayResX: {o['width']}",
             f"PlayResY: {o['height']}", 'WrapStyle: 2', 'ScaledBorderAndShadow: yes',
             '[V4+ Styles]', 'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
             f"Style: Laugh,{plan['font_family']},{size},&H00FFFFFF,&H00FFFFFF,&H00202020,&H00000000,0,0,0,0,100,100,0,0,1,3,0,8,0,0,0,1",
             '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    for r in plan['captions']:
        x,y = r['x_px'],r['y_px']
        color = r['color'][4:6]+r['color'][2:4]+r['color'][0:2]
        sound_ms = round((r['end_frame']-r['start_frame'])*1000/o['fps'])
        total_ms = round((r['visual_end_frame']-r['start_frame'])*1000/o['fps'])
        tags = f'\\an8\\q2\\1c&H{color}&'
        if r['mode'] == 'float_fade':
            tags += f"\\move({x},{y},{x},{y-r['rise_px']},0,{total_ms})\\fad(0,{total_ms-sound_ms})"
        else:
            tags += f'\\pos({x},{y})'
        if r['mode'] != 'stable':
            scale = r['end_scale']*100
            tags += f'\\t(0,{sound_ms},1,\\fscx{scale:g}\\fscy{scale:g})'
        lines.append(f"Dialogue: 1,{_ass_time(r['start_frame'],o['fps'])},{_ass_time(r['visual_end_frame'],o['fps'])},Laugh,,0,0,0,,{{{tags}}}{r['text']}")
    return '\n'.join(lines)+'\n'


def render(plan_path, output_root):
    plan = validate(plan_path)
    root, folder = Path(plan['workspace_root']), Path(output_root).resolve()
    folder.relative_to(root)
    Path(plan_path).resolve().relative_to(root)
    if folder.exists() and any(folder.iterdir()):
        raise ContractError('expressive output directory must be new or empty')
    folder.mkdir(parents=True, exist_ok=True)
    fonts = folder/'fonts'
    fonts.mkdir()
    copied_font = fonts/Path(plan['font']['path']).name
    shutil.copyfile(plan['font']['path'], copied_font)
    fallback = atomic_write_json({'clock': 'locked_program_frames', 'fps': plan['output']['fps'],
        'events': [{k:r[k] for k in ('event_id','speaker_id','speaker_label','start_frame','end_frame','fallback_text')}
                   for r in plan['captions']]}, folder/'plain-events.json')
    candidate = folder/'candidate.mp4'
    sticker = None
    inputs = [('program',plan['program']['path']),('plan',plan_path),('font',copied_font)]
    if plan['variety_sources']:
        frames,manifest,sticker = variety_stickers.materialize(plan,plan_path,folder)
        filters = ['-framerate',str(plan['output']['fps']),'-i',str(frames/'%05d.png'),
                   '-filter_complex','[0:v][1:v]overlay=shortest=1:format=auto[v]','-map','[v]']
        inputs.append(('sticker_manifest',manifest))
    else:
        ass = atomic_write_text(ass_document(plan), folder/'captions.ass')
        graph = f"ass=filename='{_filter_path(ass)}':fontsdir='{_filter_path(fonts)}'"
        filters = ['-vf',graph,'-map','0:v:0']
        inputs.append(('ass',ass))
    execute_ffmpeg_with_receipt('render', ['ffmpeg','-nostdin','-v','error','-n','-i',plan['program']['path'],
        *filters,'-map','0:a?','-c:v','libx264','-preset','fast','-crf','18',
        '-pix_fmt','yuv420p','-c:a','copy','-map_metadata','-1','-movflags','+faststart',str(candidate)],
        [{'role':k,'path':p} for k,p in inputs],
        [{'role':'candidate','path':candidate}], folder/'render-receipt.json',
        capture_log_path=folder/'render.log', guard_resource=True, timeout_s=600)
    o = plan['output']
    _media_matches(candidate, o['width'],o['height'],o['fps'],o['duration_s'])
    decoded = folder/'decoded.framemd5'
    execute_ffmpeg_with_receipt('technical_qa', ['ffmpeg','-nostdin','-v','error','-n','-i',str(candidate),
        '-map','0:v:0','-an','-f','framemd5',str(decoded)], [{'role':'candidate','path':candidate}],
        [{'role':'decoded','path':decoded}], folder/'decode-receipt.json', guard_resource=True, timeout_s=600)
    frames = sum(bool(s.strip()) and not s.startswith('#') for s in decoded.read_text().splitlines())
    if frames != o['frames']:
        raise ContractError('expressive output frame count differs from locked edit')
    final_layout = copy.deepcopy(plan['layout_with_captions'])
    final_layout['candidate'] = {'path':candidate.relative_to(root).as_posix(),'expected_sha256':file_sha256(candidate)}
    final_layout['layout_evidence'] = {'path':Path(plan_path).resolve().relative_to(root).as_posix(), 'expected_sha256':file_sha256(plan_path)}
    check = validate_final_layout(final_layout, root)
    atomic_write_json(final_layout, folder/'final-layout.json')
    result = {'schema':SCHEMA+'.result','candidate':artifact_reference(candidate,'candidate'),
              'plan':artifact_reference(plan_path,'plan'),'plain_events':artifact_reference(fallback,'fallback'),
              'decoded_frames':frames,'technical_status':'passed','layout_status':check['status'],
              'human_review':'pending','candidate_only':True,'automatic_publish':False,
              'sound_timing_review':plan['review_status'],
              'privacy_rescan':'required_for_final_candidate','audio_policy':'copy_all_audio_streams'}
    if sticker:
        result['transparent_sticker']=artifact_reference(sticker,'transparent sticker')
        result['sticker_manifest']=artifact_reference(manifest,'frame manifest')
    atomic_write_json(result, folder/'result.json')
    return result
