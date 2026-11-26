"""One synthetic attention/understanding/reaction recipe on the packaging clock.

Drawing is an editable Pillow asset recipe, not a new timeline or encoder.
FFmpeg execution, font coverage, artifact binding and cache validation reuse
the existing pipeline. Real people and source evidence are deliberately absent.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from .contract_io import (artifact_reference, atomic_write_json, canonical_json_sha256,
                          file_sha256, read_json_object, validate_artifact_reference)
from .errors import ContractError
from .execution_receipts import execute_ffmpeg_with_receipt, probe_media_metadata
from .remotion_project import build_remotion_timeline_preview
from .review_resource_guard import ReviewResourcePolicy
from .video_use_review_execution import verified_cache, verify_font

SCHEMA = 'video_creation_pipeline.performance_recipe.v1'
PHASES = ('attention', 'understanding', 'reaction')
MANIFEST_SHA = '3d01ded1dfda57c463738f8c8cf550601649dd048a63aa2e3a39cddf9abb44ae'
BASE_COMMIT = '0ac8bf2b31942376cb6b8c4074715595d512acd2'


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ContractError(f'finite numeric {label} required')
    return float(value)


def _label(value, limit=24):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(c) < 32 for c in value):
        raise ContractError('bounded plain-text label required')
    return value


def build_plan(packaging_path, spec_path):
    """Three authored scene cues; no inferred words, retiming, or second clock."""
    spec = read_json_object(spec_path, 'performance spec')
    packaging = read_json_object(packaging_path, 'packaging')
    projection = build_remotion_timeline_preview(packaging_path, composition_id='PerformanceRecipe')
    if spec.get('schema') != 'video_creation_pipeline.performance_spec.v1' or spec.get('synthetic_only') is not True:
        raise ContractError('explicit synthetic performance spec required')
    output = packaging['output']
    w, h, fps = (output.get(k) for k in ('width', 'height', 'fps'))
    if any(type(v) is not int for v in (w, h, fps)) or not (360 <= w <= 1280 and 360 <= h <= 720 and 12 <= fps <= 30) or w % 2 or h % 2:
        raise ContractError('bounded landscape/square recipe output required')
    # This single composition has a square design area; the existing composer
    # letterboxes it when a different aspect ratio is explicitly authored.
    if w != h:
        raise ContractError('this recipe currently needs a square design area')
    scenes = packaging.get('scenes', [])
    cues = spec.get('cues', [])
    if len(scenes) != 3 or len(cues) != 3:
        raise ContractError('recipe requires its three semantic scenes')
    source_root = Path(spec['source_review_root']).resolve()
    manifest_path = source_root/'source-manifest.json'
    if file_sha256(manifest_path) != MANIFEST_SHA:
        raise ContractError('reviewed source manifest drift')
    manifest = read_json_object(manifest_path, 'source manifest')
    sources = []
    for row in manifest['files']:
        if row['commit'] == BASE_COMMIT and row['upstream_path'] in ('src/core.js', 'src/clawd.js', 'src/timeline.js', 'LICENSE'):
            p = (source_root/row['local_path']).resolve()
            if not p.is_relative_to(source_root) or file_sha256(p) != row['sha256']:
                raise ContractError('reviewed source file drift')
            sources.append({'commit': row['commit'], 'upstream_path': row['upstream_path'],
                            'source': artifact_reference(p, 'source'), 'license': 'MIT'})
    if len(sources) != 4:
        raise ContractError('complete recipe source binding required')
    obj = spec['object']
    if set(obj) != {'id', 'label', 'color', 'color_reason'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', obj['id']):
        raise ContractError('stable object identity required')
    _label(obj['label'], 6)
    _label(obj['color_reason'], 80)
    if not re.fullmatch(r'#[0-9A-Fa-f]{6}', obj['color']):
        raise ContractError('explicit material hex color required')
    font = Path(spec['font_path']).resolve()
    verify_font(font, [obj['label'], '合成动作示例', '轻放区', *[c['label'] for c in cues]])
    rows, last = [], 0.0
    deps = {name: file_sha256(Path(__file__).with_name(name)) for name in
            ('performance_recipe.py', 'contract_io.py', 'execution_receipts.py',
             'review_resource_guard.py', 'video_use_review_execution.py', 'remotion_project.py')}
    for phase, scene, cue, projected in zip(PHASES, scenes, cues, projection['scenes'], strict=True):
        start, end = (_number(scene[k], k) for k in ('start_s', 'end_s'))
        a, b = (_number(cue[k], k) for k in ('start_s', 'end_s'))
        if cue.get('phase') != phase or cue.get('scene_id') != scene['scene_id']:
            raise ContractError('ordered semantic cue identity mismatch')
        if start != last or not start <= a < b <= end or end-start < 1 or end > 15:
            raise ContractError('cue must fit contiguous bounded packaging scenes')
        if any(abs(t*fps-round(t*fps)) > 1e-7 for t in (start, end, a, b)):
            raise ContractError('cue and scene boundaries must align with frames')
        if b-a < .4 or end-b < .4:
            raise ContractError('action and result reading hold need at least 0.4 seconds')
        label = _label(cue['label'], 24)
        row = {'scene_id': scene['scene_id'], 'phase': phase, 'start_s': start, 'end_s': end,
               'cue': {'start_s': a, 'end_s': b, 'label': label}, 'object': dict(obj),
               'output': dict(output), 'projection': projected, 'source_bindings': sources,
               'font': artifact_reference(font, 'local font'), 'implementation_dependencies': deps}
        row['regeneration_key'] = canonical_json_sha256(row)
        rows.append(row)
        last = end
    return {'schema': SCHEMA, 'candidate_only': True, 'synthetic_only': True,
            'packaging': artifact_reference(packaging_path, 'packaging'),
            'spec': artifact_reference(spec_path, 'performance spec'), 'scenes': rows,
            'clock_authority': 'existing video_packaging_plan.v1',
            'real_acceptance_pending': True}


def validate_plan(plan):
    p = validate_artifact_reference(plan['packaging'], 'packaging')
    s = validate_artifact_reference(plan['spec'], 'performance spec')
    if plan != build_plan(p, s):
        raise ContractError('performance plan or dependency drift')


def pose_at(row, global_time_s):
    """Pure sought pose: attention gaze, understanding nod, reaction arc+settle.

    Adapted principles: Base feel/emotions couples a face with body response;
    core.arcPt/spring derives motion from time. No character artwork is copied.
    Endpoints hold exactly, so adjacent scenes share the same parcel identity.
    """
    t = _number(global_time_s, 'frame time')
    a, b = row['cue']['start_s'], row['cue']['end_s']
    u = max(0., min(1., (t-a)/(b-a)))
    k = u*u*(3-2*u)
    phase = row['phase']
    attention = k if phase == 'attention' else 1.
    nod = math.sin(u*math.pi*2)*7 if phase == 'understanding' else 0.
    travel = k if phase == 'reaction' else 0.
    # A small, finite settle after contact; its envelope is zero at both ends.
    age = t-b
    settle = (3*math.exp(-7*age)*math.sin(20*age)*math.sin(math.pi*age/.4)
              if phase == 'reaction' and 0 < age < .4 else 0.)
    return {'object_id': row['object']['id'], 'parcel_x': 414+132*travel,
            'parcel_y': 452+25*travel-72*4*travel*(1-travel)+settle,
            'gaze_x': 9*attention+5*travel, 'gaze_y': 5*travel,
            'head_y': 363-7*attention+nod, 'hand_x': 349+132*travel,
            'hand_y': 452+25*travel-72*4*travel*(1-travel)+settle, 'travel': travel}


def draw_pose(row, global_time_s):
    """Bounded editable asset drawing, consumed by the existing FFmpeg outlet."""
    from PIL import Image, ImageDraw, ImageFont
    size = row['output']['width']
    im = Image.new('RGB', (720, 720), '#f3f5f7')
    d = ImageDraw.Draw(im)
    font_path = row['font']['path']
    title = ImageFont.truetype(font_path, 38)
    small = ImageFont.truetype(font_path, 28)
    tiny = ImageFont.truetype(font_path, 25)
    ink, muted = '#182b3a', '#586d7c'
    d.text((45, 38), '合成动作示例', font=small, fill=muted)
    text = row['cue']['label']
    # Wrap by measured glyph widths; never shrink a long Chinese label.
    lines, line = [], ''
    for c in text:
        if d.textlength(line+c, font=title) > 630:
            lines.append(line); line = c
        else:
            line += c
    lines.append(line)
    if len(lines) > 2:
        raise ContractError('label exceeds two readable lines')
    for i, text_line in enumerate(lines):
        d.text((45, 92+i*52), text_line, font=title, fill=ink)
    # Stable stage and material colors: grey robot, brown cardboard, blue tray.
    d.rounded_rectangle((35, 545, 685, 565), radius=8, fill='#d1d9de')
    d.rounded_rectangle((480, 520, 630, 547), radius=8, fill='#b6d1e0', outline='#416f89', width=3)
    d.text((499, 568), '轻放区', font=small, fill='#345d74')
    p = pose_at(row, global_time_s)
    cy = p['head_y']
    d.rounded_rectangle((136, 413, 293, 544), radius=27, fill='#bbc9d1', outline=ink, width=4)
    d.line((275, 448, p['hand_x'], p['hand_y']), fill=ink, width=13)
    d.ellipse((p['hand_x']-11,p['hand_y']-11,p['hand_x']+11,p['hand_y']+11), fill='#bbc9d1', outline=ink, width=3)
    d.rounded_rectangle((110, cy-66, 310, cy+58), radius=34, fill='#dbe4e9', outline=ink, width=4)
    for x in (168, 253):
        d.ellipse((x-24,cy-27,x+24,cy+20), fill='#ffffff', outline='#728793', width=2)
        px, py = x+p['gaze_x'], cy-2+p['gaze_y']
        d.ellipse((px-10,py-12,px+10,py+12), fill=ink)
        d.ellipse((px+1,py-8,px+6,py-3), fill='white')
    d.arc((194,cy+15,238,cy+39), start=5,end=175, fill=ink,width=3)
    if row['phase'] == 'attention':
        q = max(0., min(1., (global_time_s-row['cue']['start_s'])/.25))
        if q > 0:
            d.line((339, 306, 355, 287), fill='#a96c24', width=4)
            d.line((350, 328, 376, 328), fill='#a96c24', width=4)
    if row['phase'] == 'understanding' and global_time_s >= row['cue']['end_s']:
        d.line((325,271,339,285,363,256), fill='#266650', width=5)
    x, y = p['parcel_x'], p['parcel_y']
    d.rounded_rectangle((x-55,y-43,x+55,y+43), radius=7, fill=row['object']['color'], outline='#59462e', width=3)
    d.rectangle((x-9,y-41,x+9,y+41), fill='#e3c69b')
    d.rounded_rectangle((x-44,y-19,x+44,y+22),radius=4,fill='#fffdf7')
    label = row['object']['label']
    tw = d.textlength(label, font=tiny)
    if tw > 85:
        raise ContractError('parcel label does not fit at readable size')
    d.text((x-tw/2,y-17),label,font=tiny,fill=ink)
    # Reserve the lower band for the existing audio-timeline subtitle layer.
    return im if size == 720 else im.resize((size, size), Image.Resampling.LANCZOS)


def continuity_report(rows):
    """Check required boundary captures, not an average score over zero pairs."""
    if not rows or any(r.get('failed') or not r.get('left') or not r.get('right') for r in rows):
        return {'status': 'invalid', 'reason': 'missing_or_failed_capture', 'considered': 0}
    applicable = [r for r in rows if r.get('applicable') is True]
    if not applicable:
        return {'status': 'not_evaluated', 'considered': 0}
    checks = []
    for r in applicable:
        left, right = r['left'], r['right']
        try:
            delta = max(abs(_number(left[k], k)-_number(right[k], k)) for k in
                        ('parcel_x','parcel_y','head_y','gaze_x','gaze_y','hand_x','hand_y'))
            okay = left['object_id'] == right['object_id'] and delta <= 1.
        except (KeyError, ContractError):
            return {'status': 'invalid', 'reason': 'incomplete_pose', 'considered': 0}
        checks.append({'boundary_s': r['boundary_s'], 'max_delta_px': delta, 'passed': okay})
    return {'status': 'passed' if all(x['passed'] for x in checks) else 'failed',
            'considered': len(checks), 'checks': checks,
            'visual_acceptance': 'separate_from_numeric_pose_check'}


def _execute(operation, args, inputs, outputs, receipt, runner=None):
    return execute_ffmpeg_with_receipt(operation,
        ['ffmpeg','-nostdin','-hide_banner','-v','error','-n','-filter_threads','1',
         '-filter_complex_threads','1',*args], inputs, outputs, receipt,
        capture_log_path=Path(receipt).with_suffix('.log'), timeout_s=120,
        guard_resource=True, resource_policy=ReviewResourcePolicy(ffmpeg_threads=1), runner=runner)


def validate_encoded(path, expected_frames, output, folder, *, runner=None):
    metadata = probe_media_metadata(path)
    video = [s for s in metadata['streams'] if s['codec_type'] == 'video']
    if len(video) != 1 or any(video[0][k] != output[k] for k in ('width','height')):
        raise ContractError('encoded visual geometry mismatch')
    if abs(float(video[0].get('duration', metadata['format']['duration']))-expected_frames/output['fps']) > .01:
        raise ContractError('encoded visual duration mismatch')
    decoded = folder/'decoded.framemd5'
    _execute('technical_qa', ['-threads','1','-xerror','-err_detect','explode','-i',str(path),
             '-map','0:v:0','-an','-threads','1','-f','framemd5',str(decoded)],
             [{'role':'candidate','path':path}],[{'role':'decoded','path':decoded}],folder/'decode-receipt.json',runner)
    count = sum(bool(line) and not line.startswith('#') for line in decoded.read_text().splitlines())
    if count != expected_frames:
        raise ContractError('incomplete visual decode')
    return {'decoded_frames': count, 'metadata': metadata}


def materialize(plan_path, cache_root, output_root, *, runner=None):
    plan = read_json_object(plan_path, 'performance plan')
    validate_plan(plan)
    cache, out = Path(cache_root).resolve(), Path(output_root).resolve()
    if out.exists() and any(out.iterdir()):
        raise ContractError('new output directory required')
    out.mkdir(parents=True, exist_ok=True)
    rendered, reused, assets = [], [], []
    for row in plan['scenes']:
        key = row['regeneration_key']
        folder = cache/key
        marker = folder/'complete.json'
        saved = verified_cache(marker, key)
        if saved:
            reused.append(row['scene_id'])
        else:
            if folder.exists():
                raise ContractError('incomplete or corrupt recipe cache; choose a fresh cache root')
            folder.mkdir(parents=True)
            frames = folder/'frames'; frames.mkdir()
            n = round((row['end_s']-row['start_s'])*row['output']['fps'])
            references = []
            for frame in range(n):
                t = row['start_s']+frame/row['output']['fps']
                fp = frames/f'{frame:04d}.png'
                draw_pose(row,t).save(fp)
                references.append({'frame':frame, 'global_time_s':t,'sha256':file_sha256(fp)})
            provenance = atomic_write_json({'row':row,'frames':references},folder/'provenance.json')
            target = folder/'visual.mp4'
            _execute('render',['-threads','1','-framerate',str(row['output']['fps']),'-i',str(frames/'%04d.png'),
                     '-frames:v',str(n),'-an','-c:v','libx264','-preset','ultrafast','-crf','19',
                     '-pix_fmt','yuv420p','-threads','1',str(target)],
                     [{'role':'frame_manifest','path':provenance}],[{'role':'visual','path':target}],folder/'render-receipt.json',runner)
            qa = validate_encoded(target,n,row['output'],folder,runner=runner)
            saved = {'key':key,'artifacts':{'visual':artifact_reference(target,'visual'),
                     'provenance':artifact_reference(provenance,'provenance'),
                     'render_receipt':artifact_reference(folder/'render-receipt.json','render receipt'),
                     'decode_receipt':artifact_reference(folder/'decode-receipt.json','decode receipt')},'qa':qa}
            atomic_write_json(saved,marker)
            rendered.append(row['scene_id'])
        assets.append({'scene_id':row['scene_id'], 'visual':saved['artifacts']['visual'],
                       'provenance':saved['artifacts']['provenance'], 'visual_start_s':0,
                       'semantic_description':f"Synthetic parcel handling: {row['phase']}",
                       'regeneration_key':key})
    seams = continuity_report([{'boundary_s':right['start_s'],'applicable':True,
             'left':pose_at(left,left['end_s']), 'right':pose_at(right,right['start_s'])}
             for left,right in zip(plan['scenes'],plan['scenes'][1:])])
    result = {'plan':artifact_reference(plan_path,'plan'),'assets':assets,'rendered_scenes':rendered,
              'reused_scenes':reused,'continuity':seams,'normal_speed_human_review':'pending',
              'synthetic_only':True, 'candidate_only':True}
    atomic_write_json(result,out/'manifest.json')
    return result
