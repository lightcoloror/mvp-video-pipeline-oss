"""Optional progress packaging over the existing timeline, layout gate and outlet."""
from __future__ import annotations

import copy
import math
import re
from fractions import Fraction
from pathlib import Path

from .contract_io import (
    artifact_reference, atomic_write_json, atomic_write_text, file_sha256,
    read_json_object, validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import execute_ffmpeg_with_receipt, probe_media_metadata
from .final_layout_safety import validate_final_layout
from .remotion_project import build_remotion_timeline_preview

SCHEMA = 'video_creation_pipeline.progress_overlay.v1'
FONT_PRESETS_PATH = Path(__file__).with_name('policies') / 'progress-font-presets.v1.json'


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ContractError(f'{name} must be an integer >= {minimum}')
    return value


def _filter_path(path):
    text = Path(path).resolve().as_posix()
    if any(c in text for c in "'\n\r[];,\x00"):
        raise ContractError('unsupported filter path characters')
    return text.replace(':', '\\:')


def navigation_typography(spec, width, height):
    """Auxiliary navigation sizes deliberately do not use body/subtitle gates."""
    policy = read_json_object(FONT_PRESETS_PATH, 'progress font presets')
    if policy.get('schema') != 'video_creation_pipeline.progress-font-presets.v1':
        raise ContractError('unsupported progress font presets')
    preset_id = spec.get('font_preset', policy['default_preset'])
    preset = policy['presets'].get(preset_id)
    if not isinstance(preset, dict):
        raise ContractError('unknown progress font preset')
    reference = _integer(policy['reference_short_edge_px'], 'font reference short edge', 1)
    reference_size = _integer(preset['font_size_px'], 'navigation preset size', 1)
    size = spec.get('font_size_px', max(1, round(reference_size * min(width, height) / reference)))
    if type(size) is not int or size <= 0:
        raise ContractError('navigation font_size_px must be a positive integer')
    return {'schema': 'video_creation_pipeline.progress-navigation-type.v1',
            'policy': artifact_reference(FONT_PRESETS_PATH, 'progress font presets'),
            'preset_id': preset_id, 'font_size_px': size,
            'explicit_pixel_override': 'font_size_px' in spec,
            'body_and_subtitle_minima_applied': False,
            'viewport': {'width': width, 'height': height}}


def script_headings(reference, workspace_root):
    """Adapt the existing RWE Markdown H2 extraction, retaining author wording."""
    if not isinstance(reference, dict) or not reference.get('sha256'):
        raise ContractError('bound video script_ref with sha256 is required')
    # Accept the shared delivery {path,sha256} contract, then use the existing
    # full-artifact validator without weakening any supplied hash/size fields.
    current = artifact_reference(reference.get('path',''), 'video script')
    path = validate_artifact_reference({**current, **reference}, 'video script')
    path.resolve().relative_to(Path(workspace_root).resolve())
    headings, seen = [], set()
    for raw in re.findall(r'^##[ \t]+(.+?)[ \t]*$', path.read_text(encoding='utf-8-sig'), re.M):
        numbered = re.fullmatch(r'(\d{1,3})\s*[｜|.、:：-]\s*(.+)', raw)
        key, title = (str(int(numbered[1])), numbered[2]) if numbered else (str(len(headings)+1), raw)
        if key in seen:
            raise ContractError('duplicate script heading number')
        seen.add(key)
        headings.append({'chapter': key.zfill(2), 'title': title})
    if not headings:
        raise ContractError('video script H2 headings are required; provide an explicit manual override when unavailable')
    return artifact_reference(path, 'video script'), headings


def chapter_titles(timeline, spec, root, script_ref=None):
    """Resolve labels from one script; all playback times still come from scenes."""
    mode = spec.get('title_source', 'script')
    values = spec.get('chapters')
    if mode == 'manual':
        reason = spec.get('title_override_reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError('manual title override requires a reason')
        source = {'mode': 'manual', 'reason': reason.strip()}
    elif mode == 'script':
        refs = [r for r in (script_ref, spec.get('script_ref'), timeline.get('script_ref')) if r is not None]
        if not refs:
            raise ContractError('video script_ref is required for default script chapter titles')
        bound = [script_headings(r, root) for r in refs]
        if any(item[0] != bound[0][0] for item in bound[1:]):
            raise ContractError('conflicting video script references')
        reference, headings = bound[0]
        source = {'mode': 'script', 'script': reference, 'headings': headings}
        if values is None:
            values = [{'scene_id': row['scene_id']} for row in timeline['scenes']]
    else:
        raise ContractError('title_source must be script or manual')
    if not isinstance(values, list) or not 1 <= len(values) <= 30:
        raise ContractError('provide 1..30 chapters or one scene per script heading')
    if mode == 'script' and len(values) != len(headings) and not all(isinstance(r,dict) and 'script_heading' in r for r in values):
        raise ContractError('script/scene chapter count differs; map each chapter with scene_id and script_heading')
    resolved, selected = [], set()
    for i, row in enumerate(values):
        allowed = {'scene_id', 'title'} | ({'script_heading'} if mode == 'script' else set())
        if not isinstance(row, dict) or 'scene_id' not in row or set(row) - allowed:
            raise ContractError('chapter accepts scene_id/title/script_heading only; no independent timestamps')
        if mode == 'script':
            key = str(row.get('script_heading', headings[i]['chapter'] if i < len(headings) else ''))
            heading = next((h for h in headings if h['chapter'].lstrip('0') == key.lstrip('0')), None)
            if heading is None or heading['chapter'] in selected:
                raise ContractError('unknown or repeated script heading mapping')
            selected.add(heading['chapter'])
            title = heading['title']
            if 'title' in row and row['title'] != title:
                raise ContractError('chapter title differs from script; use an explicit manual override with a reason')
        else:
            title = row.get('title')
        resolved.append({'scene_id': row['scene_id'], 'title': title})
    return resolved, source


def _media_matches(path, width, height, fps, duration):
    meta = probe_media_metadata(path)
    videos = [s for s in meta['streams'] if s['codec_type'] == 'video']
    if len(videos) != 1:
        raise ContractError('progress overlay requires exactly one video stream')
    video = videos[0]
    if (video['width'], video['height']) != (width, height):
        raise ContractError('program canvas differs from locked packaging')
    if abs(float(Fraction(video['avg_frame_rate'])) - fps) > .001:
        raise ContractError('program frame rate differs from locked packaging')
    if abs(float(video.get('duration', meta['format']['duration'])) - duration) > .5 / fps:
        raise ContractError('program duration differs from locked packaging')
    if abs(float(video.get('start_time', 0))) > .5 / fps:
        raise ContractError('program must use a zero-based presentation clock')
    if video.get('sample_aspect_ratio', '1:1') not in ('1:1', 'N/A'):
        raise ContractError('program must use square pixels')
    if any(abs(float(s.get('rotation', 0))) > .001 for s in video.get('side_data_list', [])):
        raise ContractError('normalize display rotation before progress packaging')
    return meta


def build(program, packaging, layout, spec_path, workspace_root, *, script_ref=None):
    """Bind the locked program and derive chapter times only from scene IDs."""
    root = Path(workspace_root).resolve()
    source = Path(program).resolve()
    source.relative_to(root)
    timeline = read_json_object(packaging, 'packaging')
    preview = build_remotion_timeline_preview(packaging, composition_id='ProgressOverlay')
    width, height, fps = (timeline['output'][k] for k in ('width', 'height', 'fps'))
    for value, name in ((width, 'width'), (height, 'height'), (fps, 'fps')):
        _integer(value, name, 1)
    if width % 2 or height % 2 or fps > 60:
        raise ContractError('even output dimensions and fps <= 60 required')
    cursor, starts = 0, {}
    for raw, projected in zip(timeline['scenes'], preview['scenes'], strict=True):
        sid = raw['scene_id']
        a, b = raw['start_s'], raw['end_s']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (a, b)):
            raise ContractError('nonfinite scene time')
        if sid in starts or abs(a * fps - cursor) > 1e-6 or abs(b * fps - round(b * fps)) > 1e-6 or b <= a:
            raise ContractError('scene timeline has duplicate IDs, gaps, overlaps or non-frame boundaries')
        starts[sid] = projected['startFrame'] / fps
        cursor = round(b * fps)
    if cursor < 2:
        raise ContractError('progress overlay needs at least two frames')
    duration = cursor / fps
    _media_matches(source, width, height, fps, duration)
    spec = read_json_object(spec_path, 'progress spec')
    allowed = {'mode', 'position', 'margin_x_px', 'edge_offset_px', 'bar_height_px', 'bar_width_px',
               'track_color', 'fill_color', 'text_color', 'font_file', 'font_size_px',
               'typography_profile', 'typography_role', 'font_preset', 'chapters', 'color_reason',
               'title_source', 'title_override_reason', 'script_ref', 'safe_area_mode'}
    if set(spec) - allowed:
        raise ContractError('unknown progress spec fields')
    if spec.get('mode') not in ('plain', 'chapters') or spec.get('position') not in ('top', 'bottom'):
        raise ContractError('choose plain/chapters and top/bottom explicitly')
    if not isinstance(spec.get('color_reason'), str) or not spec['color_reason'].strip():
        raise ContractError('record why these overlay colors fit this scene')
    colors = {k: spec.get(k) for k in ('track_color', 'fill_color')}
    colors['text_color'] = spec.get('text_color', 'FFFFFF')
    if any(not isinstance(v, str) or not re.fullmatch('[0-9a-fA-F]{6}', v) for v in colors.values()):
        raise ContractError('colors must be six-digit RGB hex')
    margin = _integer(spec.get('margin_x_px'), 'margin_x_px', 1)
    edge = _integer(spec.get('edge_offset_px'), 'edge_offset_px', 1)
    bar_h = _integer(spec.get('bar_height_px'), 'bar_height_px', 2)
    bar_w = _integer(spec.get('bar_width_px',width-2*margin),'bar_width_px',16)
    if bar_w > width-2*margin or bar_w % 2 or bar_h % 2:
        raise ContractError('bar width/height must be positive even pixel counts')
    chapters, typography, font_ref, label_h, title_source = [], None, None, 0, None
    if spec['mode'] == 'chapters':
        typography = navigation_typography(spec, width, height)
        font_size = typography['font_size_px']
        font_ref = artifact_reference(spec.get('font_file', ''), 'chapter font')
        _filter_path(font_ref['path'])
        values, title_source = chapter_titles(timeline, spec, root, script_ref)
        previous = -1
        for i, row in enumerate(values):
            if not isinstance(row, dict) or set(row) != {'scene_id', 'title'}:
                raise ContractError('chapter accepts only scene_id and title; no independent timestamps')
            start = starts.get(row['scene_id'], -1)
            title = row['title']
            if start <= previous or (i == 0 and start != 0):
                raise ContractError('chapters must start at zero and follow scene order')
            if not isinstance(title, str) or not title.strip() or any(ord(c) < 32 for c in title):
                raise ContractError('chapter title must be one plain nonempty line')
            label = f'{i+1:02d}/{len(values):02d}  {title.strip()}'
            # Keep author headings; never silently shrink or truncate them.
            if len(label) * font_size > bar_w:
                raise ContractError('chapter title too long for the chosen navigation font/band; adjust font or layout explicitly')
            chapters.append({'scene_id': row['scene_id'], 'start_s': start, 'text': label})
            previous = start
        for i, row in enumerate(chapters):
            row['end_s'] = chapters[i+1]['start_s'] if i+1 < len(chapters) else duration
        label_h = math.ceil(font_size * 1.6)
    elif spec.get('chapters'):
        raise ContractError('plain mode must not silently ignore chapters')
    band_h = label_h + bar_h
    band_y = edge if spec['position'] == 'top' else height - edge - band_h
    if band_y < 0 or band_y + band_h > height:
        raise ContractError('progress band leaves output canvas')
    rect = {'x': margin/width, 'y': band_y/height, 'width': bar_w/width, 'height': band_h/height}
    base_layout = read_json_object(layout, 'final layout')
    if (root / base_layout['candidate']['path']).resolve() != source or base_layout['candidate']['expected_sha256'] != file_sha256(source):
        raise ContractError('layout must describe this exact base program')
    if abs(base_layout['duration_seconds'] - duration) > 1e-6:
        raise ContractError('layout duration differs from locked timeline')
    added_layout = copy.deepcopy(base_layout)
    safe_area_mode = spec.get('safe_area_mode', 'auxiliary')
    if safe_area_mode not in {'auxiliary', 'protected'}:
        raise ContractError('safe_area_mode must be auxiliary or protected')
    overlay = {'element_id': 'progress-overlay', 'role': 'information_motion',
               'start_seconds': 0, 'end_seconds': duration,
               'rect': rect, 'critical_content': safe_area_mode == 'protected'}
    if safe_area_mode == 'auxiliary':
        overlay['auxiliary_visibility'] = {
            'mode': 'fullscreen_or_controls_hidden',
            'reason': 'Nonessential playback navigation; visible in fullscreen or with platform controls hidden.'}
    added_layout['elements'].append(overlay)
    if 'information_motion' not in added_layout['required_roles']:
        added_layout['required_roles'].append('information_motion')
    layout_check = validate_final_layout(added_layout, root)
    return {'schema': SCHEMA, 'program': artifact_reference(source, 'program'),
            'packaging': artifact_reference(packaging, 'packaging'),
            'layout': artifact_reference(layout, 'layout'), 'spec': artifact_reference(spec_path, 'spec'),
            'workspace_root': str(root), 'font': font_ref, 'typography': typography,
            'output': {'width': width, 'height': height, 'fps': fps, 'frames': cursor, 'duration_s': duration},
            'geometry': {'x': margin, 'band_y': band_y, 'bar_y': band_y + label_h,
                         'bar_width': bar_w, 'bar_height': bar_h, 'rect': rect},
            'chapters': chapters, 'chapter_title_source': title_source, 'layout_with_progress': added_layout,
            'layout_check': layout_check, 'implementation_sha256': file_sha256(__file__),
            'candidate_only': True, 'human_review': 'pending'}


def validate(plan_path):
    plan = read_json_object(plan_path, 'progress plan')
    if plan.get('schema') != SCHEMA:
        raise ContractError('unsupported progress plan')
    refs = {k: validate_artifact_reference(plan[k], k) for k in ('program', 'packaging', 'layout', 'spec')}
    expected = build(refs['program'], refs['packaging'], refs['layout'], refs['spec'], plan['workspace_root'],
                     script_ref=(plan.get('chapter_title_source') or {}).get('script'))
    if expected != plan:
        raise ContractError('progress plan or dependency drift; rebuild from locked edit')
    return plan


def filtergraph(plan, folder, *, write_texts=True):
    spec = read_json_object(plan['spec']['path'], 'spec')
    g, out = plan['geometry'], plan['output']
    w, h, fps, duration = g['bar_width'], g['bar_height'], out['fps'], out['duration_s']
    # The nested overlay clips the moving fill to the track; no fill leaks into margins.
    lines = [f"color=c=0x{spec['track_color']}:s={w}x{h}:r={fps}:d={duration},format=yuv444p[track]",
             f"color=c=0x{spec['fill_color']}:s={w}x{h}:r={fps}:d={duration},format=yuv444p[fill]",
             f"[track][fill]overlay=x='-w+w*min(t/{(out['frames']-1)/fps},1)':y=0:format=yuv444:shortest=1[progress]"]
    dividers = []
    for chapter in plan['chapters'][1:]:
        x = max(0, min(w-4, round(w*chapter['start_s']/duration)-2))
        dividers.append(f"drawbox=x={x}:y=0:w=4:h={h}:c=0x{spec['track_color']}:t=fill")
    lines.append('[progress]' + (','.join(dividers) if dividers else 'null') + '[bar]')
    lines.append(f"[0:v:0][bar]overlay=x={g['x']}:y={g['bar_y']}:shortest=1[withbar]")
    labels = []
    for i, chapter in enumerate(plan['chapters']):
        text_file = folder / f'chapter-{i:02d}.txt'
        if write_texts: atomic_write_text(chapter['text'], text_file)
        labels.append(f"drawtext=fontfile='{_filter_path(plan['font']['path'])}':textfile='{_filter_path(text_file)}':expansion=none:fontsize={plan['typography']['font_size_px']}:fontcolor=0x{spec.get('text_color', 'FFFFFF')}:x={g['x']}:y={g['band_y']}:enable='gte(t,{chapter['start_s']})*lt(t,{chapter['end_s']})'")
    lines.append('[withbar]' + (','.join(labels) if labels else 'null') + '[vout]')
    return ';\n'.join(lines)


def render(plan_path, output_root, *, resource_policy=None, runner=None):
    plan = validate(plan_path)
    folder = Path(output_root).resolve()
    folder.relative_to(Path(plan['workspace_root']))
    Path(plan_path).resolve().relative_to(Path(plan['workspace_root']))
    if folder.exists() and any(folder.iterdir()):
        raise ContractError('progress output directory must be new or empty')
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / 'candidate.mp4'
    graph = atomic_write_text(filtergraph(plan, folder), folder / 'filtergraph.txt')
    inputs = [{'role': 'program', 'path': plan['program']['path']},
              {'role': 'progress_plan', 'path': Path(plan_path)}, {'role': 'filtergraph', 'path': graph}]
    if plan['font']:
        inputs.append({'role': 'font', 'path': plan['font']['path']})
    inputs.extend({'role': 'chapter_text', 'path': p} for p in sorted(folder.glob('chapter-*.txt')))
    threads = resource_policy.ffmpeg_threads if resource_policy is not None else None
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-n', '-filter_complex_threads','1','-i', plan['program']['path'],
               '-filter_complex_script', str(graph), '-map', '[vout]', '-map', '0:a?',
               '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
               '-c:a', 'copy', '-map_metadata', '-1', '-movflags', '+faststart', str(candidate)]
    if threads: command[-1:-1]=['-threads',str(threads)]
    execute_ffmpeg_with_receipt('render', command, inputs, [{'role': 'candidate', 'path': candidate}],
                                folder/'render-receipt.json', capture_log_path=folder/'render.log',
                                guard_resource=True, timeout_s=600,resource_policy=resource_policy,runner=runner)
    o = plan['output']
    _media_matches(candidate, o['width'], o['height'], o['fps'], o['duration_s'])
    decoded = folder / 'decoded.framemd5'
    execute_ffmpeg_with_receipt('technical_qa', ['ffmpeg', '-nostdin', '-v', 'error', '-n', '-i', str(candidate),
        '-map', '0:v:0', '-an', '-f', 'framemd5', str(decoded)], [{'role': 'candidate', 'path': candidate}],
        [{'role': 'decode', 'path': decoded}], folder/'decode-receipt.json', guard_resource=True, timeout_s=600,
        resource_policy=resource_policy,runner=runner)
    frames = sum(bool(line.strip()) and not line.startswith('#') for line in decoded.read_text().splitlines())
    if frames != o['frames']:
        raise ContractError('progress output decoded frame count differs from locked edit')
    final_layout = copy.deepcopy(plan['layout_with_progress'])
    final_layout['candidate'] = {'path': candidate.relative_to(Path(plan['workspace_root'])).as_posix(),
                                 'expected_sha256': file_sha256(candidate)}
    final_layout['layout_evidence'] = {'path': Path(plan_path).resolve().relative_to(Path(plan['workspace_root'])).as_posix(),
                                      'expected_sha256': file_sha256(plan_path)}
    layout_check = validate_final_layout(final_layout, Path(plan['workspace_root']))
    atomic_write_json(final_layout, folder/'final-layout.json')
    atomic_write_json(layout_check, folder/'layout-receipt.json')
    receipt = {'schema': SCHEMA + '.result', 'candidate': artifact_reference(candidate, 'candidate'),
               'plan': artifact_reference(plan_path, 'plan'), 'decoded_frames': frames,
               'render_receipt':artifact_reference(folder/'render-receipt.json','render receipt'),
               'decode_receipt':artifact_reference(folder/'decode-receipt.json','decode receipt'),
               'decoded_media':artifact_reference(decoded,'decoded frames'),
               'final_layout':artifact_reference(folder/'final-layout.json','final layout'),
               'layout_receipt':artifact_reference(folder/'layout-receipt.json','layout receipt'),
               'technical_status': 'passed', 'layout_status': layout_check['status'],
               'human_review': 'pending', 'privacy_rescan': 'required_for_final_candidate',
               'candidate_only': True, 'automatic_publish': False}
    atomic_write_json(receipt, folder/'result.json')
    return receipt
