"""Audio-clocked incremental composition over the existing packaging/FFmpeg outlet.

Only explicit local PCM audio and hash-bound rendered visual assets are accepted.
This candidate composer does not generate narration, approve content or publish.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import wave
from pathlib import Path

from .contract_io import artifact_reference, atomic_write_json, validate_artifact_reference
from .errors import ContractError
from .execution_receipts import execute_ffmpeg_with_receipt, probe_media_metadata
from .remotion_project import build_remotion_timeline_preview
from .review_resource_guard import ReviewResourcePolicy
from .subtitle_readability import subtitle_layout, subtitle_font, subtitle_render_preset
from .subtitle_sequence import (
    SEQUENCE_SCHEMA,
    build_reviewed_subtitle_sequence,
    export_whole_srt,
    extract_cues_from_pack,
    slice_sequence_for_scene,
)
from .subtitle_styles import (
    build_ass_document,
    build_ass_ffmpeg_filter,
    check_font_glyph_coverage,
    materialize_font_bundle,
    parse_libass_font_log,
    resolve_subtitle_style,
    verify_libass_font_selection,
)
from .scale_motion import build_scale_motion, ffmpeg_scale_overlay


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ContractError(f'invalid {label}')
    return value


def prepare(packaging_path, binding_path):
    packaging = json.loads(Path(packaging_path).read_text(encoding='utf-8'))
    binding = json.loads(Path(binding_path).read_text(encoding='utf-8'))
    if binding.get('schema') != 'video_creation_pipeline.audio_timeline_binding.v1':
        raise ContractError('unsupported audio timeline binding')
    if binding['packaging'] != artifact_reference(packaging_path, 'packaging'):
        raise ContractError('packaging binding drift')
    audio = validate_artifact_reference(binding['audio'], 'audio')
    with wave.open(str(audio), 'rb') as wav:
        if wav.getcomptype() != 'NONE' or wav.getsampwidth() != 2 or wav.getnchannels() not in (1, 2):
            raise ContractError('16-bit PCM mono/stereo audio required')
        rate, channels, frames = wav.getframerate(), wav.getnchannels(), wav.getnframes()
        pcm = wav.readframes(frames)
    if not frames or len(pcm) != frames * channels * 2:
        raise ContractError('empty or incomplete audio')
    output = packaging['output']
    width, height, fps = (output[k] for k in ('width', 'height', 'fps'))
    if any(type(v) is not int or v <= 0 for v in (width, height, fps)) or width % 2 or height % 2 or max(width, height) > 1920 or min(width, height) > 1080 or fps > 30:
        raise ContractError('invalid bounded output geometry')
    binding_style = binding.get('subtitle_style') or {}
    if not isinstance(binding_style, dict):
        raise ContractError('subtitle_style must be an object')
    pack_cues_seq = None
    if binding.get('subtitle_pack') is not None:
        pack_path = validate_artifact_reference(binding['subtitle_pack'], 'subtitle pack')
        pack_doc = json.loads(pack_path.read_text(encoding='utf-8-sig'))
        pack_raw_cues = extract_cues_from_pack(pack_doc)
        total_dur_s = frames / rate
        base_style = resolve_subtitle_style(
            width,
            height,
            preset_id=binding_style.get('preset_id'),
            font_id=binding_style.get('font_id'),
            requested_font_px=binding_style.get('font_px'),
            background_opacity=binding_style.get('background_opacity'),
            background_color=binding_style.get('background_color'),
        )
        pack_cues_seq = build_reviewed_subtitle_sequence(
            pack_raw_cues,
            max_line_units=base_style['max_line_units'],
            total_duration_s=total_dur_s,
        )
    preview = build_remotion_timeline_preview(packaging_path, composition_id='AudioTimeline')
    rows, end, ids = [], 0, set()
    if len(binding['segments']) != len(packaging['scenes']):
        raise ContractError('every scene requires an explicit visual binding')
    metadata = {}
    for scene, media, projected in zip(packaging['scenes'], binding['segments'], preview['scenes'], strict=True):
        sid = scene['scene_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', sid) or sid in ids or sid != media['scene_id']:
            raise ContractError('invalid, duplicate or mismatched scene identity')
        ids.add(sid)
        start_s, end_s = number(scene['start_s'], 'scene start'), number(scene['end_s'], 'scene end')
        start, stop = round(start_s * rate), round(end_s * rate)
        if start != end or stop <= start or stop > frames:
            raise ContractError('audio timeline gap, overlap or out of range')
        if abs(start_s * fps - round(start_s * fps)) > 1e-6 or abs(end_s * fps - round(end_s * fps)) > 1e-6:
            raise ContractError('scene boundaries must align with output frames')
        end = stop
        asset = validate_artifact_reference(media['visual'], 'visual')
        validate_artifact_reference(media['provenance'], 'visual provenance')
        if not media.get('semantic_description', '').strip():
            raise ContractError('meaningful visual description required; no placeholder fallback')
        key = str(asset)
        if key not in metadata:
            metadata[key] = probe_media_metadata(asset)
        streams = [s for s in metadata[key]['streams'] if s['codec_type'] == 'video']
        offset = number(media['visual_start_s'], 'visual offset')
        if not streams or offset + end_s - start_s > float(streams[0].get('duration', metadata[key]['format']['duration'])) + 1/fps:
            raise ContractError('missing visual stream or visual interval out of range')
        seg_style = media.get('subtitle_style') or {}
        if not isinstance(seg_style, dict):
            raise ContractError('segment subtitle_style must be an object')
        requested_font_px = seg_style.get('font_px', media.get('subtitle_font_px', binding_style.get('font_px')))
        style_spec = resolve_subtitle_style(
            width,
            height,
            preset_id=seg_style.get('preset_id', binding_style.get('preset_id')),
            font_id=seg_style.get('font_id', binding_style.get('font_id')),
            requested_font_px=requested_font_px,
            background_opacity=seg_style.get('background_opacity', binding_style.get('background_opacity')),
            background_color=seg_style.get('background_color', binding_style.get('background_color')),
        )
        font_px, typography_hash = style_spec['font_px'], style_spec['typography_policy_sha256']
        if media.get('subtitles') is not None:
            raw_seg_cues = list(media['subtitles'])
            for raw_c in raw_seg_cues:
                cs = number(raw_c.get('start_seconds', raw_c.get('start_s')), 'subtitle start')
                ce = number(raw_c.get('end_seconds', raw_c.get('end_s')), 'subtitle end')
                if not start_s <= cs < ce <= end_s:
                    raise ContractError('subtitle outside scene or overlapping')
            seg_seq = build_reviewed_subtitle_sequence(
                raw_seg_cues,
                max_line_units=style_spec['max_line_units'],
            )
            cues = slice_sequence_for_scene(seg_seq, scene_start_s=start_s, scene_end_s=end_s)
        elif pack_cues_seq is not None:
            cues = slice_sequence_for_scene(pack_cues_seq, scene_start_s=start_s, scene_end_s=end_s)
        else:
            raise ContractError('explicit subtitles required')
        check_font_glyph_coverage(
            style_spec['font']['source_path'],
            [c['display_text'] for c in cues],
        )
        chunk = pcm[start*channels*2:stop*channels*2]
        row = {
            'scene_id': sid,
            'start_s': start_s,
            'end_s': end_s,
            'audio_sha256': hashlib.sha256(chunk).hexdigest(),
            'audio_samples': stop-start,
            'rate': rate,
            'channels': channels,
            'visual': media['visual'],
            'provenance': media['provenance'],
            'semantic_description': media['semantic_description'],
            'visual_start_s': offset,
            'subtitles': cues,
            'output': output,
            'renderer_projection': projected,
            'implementation_sha256': file_hash(__file__),
            'subtitle_font_px': font_px,
            'typography_policy_sha256': typography_hash,
            'subtitle_preset_id': style_spec['preset_id'],
            'subtitle_style_resolved': style_spec,
            'subtitle_style_policy_sha256': style_spec['policy_sha256'],
            'subtitle_font_sha256': style_spec['font']['sha256'],
            'subtitle_implementation_sha256': style_spec['implementation_sha256'],
        }
        row['scale_motion'] = build_scale_motion(media.get('scale_motion'), width, height, end_s-start_s, fps)
        # Global recording hash is intentionally excluded: local PCM changes invalidate
        # only their affected segments. Final manifest still binds the whole recording.
        row['cache_key'] = digest(row)
        rows.append((row, chunk))
    if end != frames:
        raise ContractError('audio tail not covered by timeline')
    return binding, rows


def ass_time(seconds):
    ticks = round(seconds * 100)
    return f'{ticks//360000}:{ticks//6000%60:02}:{ticks//100%60:02}.{ticks%100:02}'


def compose(packaging_path, binding_path, cache_root, output_root, *, runner=None):
    binding, rows = prepare(packaging_path, binding_path)
    cache, out = Path(cache_root).resolve(), Path(output_root).resolve()
    if out.exists() and any(out.iterdir()):
        raise ContractError('output directory must be new or empty')
    cache.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    rendered, reused, assets, segment_receipts = [], [], [], []
    policy = ReviewResourcePolicy(ffmpeg_threads=1)

    def execute(operation, args, inputs, output, receipt, *, verbosity='error', capture_log_path=None):
        return execute_ffmpeg_with_receipt(
            operation,
            ['ffmpeg', '-nostdin', '-v', verbosity, '-y', '-filter_threads', '1', '-filter_complex_threads', '1', *args],
            inputs,
            [{'role': 'media', 'path': output}],
            receipt,
            capture_log_path=capture_log_path,
            guard_resource=True,
            resource_policy=policy,
            runner=runner,
            timeout_s=120,
        )

    for row, pcm in rows:
        key = row['cache_key']
        folder = cache / key
        target, marker = folder / 'segment.mkv', folder / 'complete.json'
        if marker.exists():
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if saved['cache_key'] == key and target.is_file() and file_hash(target) == saved['sha256']:
                reused.append(row['scene_id'])
                assets.append(target)
                segment_receipts.append({
                    'scene_id': row['scene_id'],
                    'cache_key': key,
                    'reused': True,
                    'render_receipt': saved.get('render_receipt'),
                    'subtitle_ass': saved.get('subtitle_ass'),
                    'font_bundle_manifest': saved.get('font_bundle_manifest'),
                    'font_load_evidence': saved.get('font_load_evidence'),
                })
                continue
            raise ContractError('cached segment integrity mismatch')
        folder.mkdir(exist_ok=True)
        wavpath, subtitle = folder / 'audio.wav', folder / 'subtitles.ass'
        with wave.open(str(wavpath), 'wb') as wav:
            wav.setnchannels(row['channels']); wav.setsampwidth(2); wav.setframerate(row['rate']); wav.writeframes(pcm)
        w, h, fps = (row['output'][k] for k in ('width', 'height', 'fps'))
        style_spec = row['subtitle_style_resolved']
        fonts_dir = folder / 'fonts'
        bundle = materialize_font_bundle(
            style_spec['font'],
            fonts_dir,
            manifest_path=folder / 'font-bundle-manifest.json',
        )
        subtitle.write_text(build_ass_document(row['subtitles'], style_spec), encoding='utf-8')
        sub_filter = build_ass_ffmpeg_filter(subtitle, fonts_dir)
        filters = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},{sub_filter}"
        transform_args = ['-map', '0:v:0', '-map', '1:a:0', '-vf', filters]
        if row['scale_motion']['events']:
            nodes = [f'[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},setpts=PTS-STARTPTS[layer]',
                     f'color=c=0xf6f5ef:s={w}x{h}:r={fps}:d={row["end_s"]-row["start_s"]}[canvas]']
            nodes += ffmpeg_scale_overlay(row['scale_motion'], 'layer', 'canvas', 'posed')
            nodes += [f"[posed]{sub_filter}[captioned]"]
            transform_args = ['-filter_complex', ';'.join(nodes), '-map', '[captioned]', '-map', '1:a:0']
        render_log = folder / 'render.log'
        render_receipt_path = folder / 'render-receipt.json'
        execute(
            'render',
            ['-threads', '1', '-ss', str(row['visual_start_s']), '-i', str(row['visual']['path']), '-threads', '1', '-i', str(wavpath), '-t', str(row['end_s']-row['start_s']), *transform_args, '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-crf', '22', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', str(target)],
            [
                {'role': 'visual', 'path': row['visual']['path']},
                {'role': 'audio', 'path': wavpath},
                {'role': 'subtitle', 'path': subtitle},
                {'role': 'subtitle_font', 'path': bundle['font_file']['path']},
                {'role': 'subtitle_font_bundle', 'path': bundle['bundle_manifest']['path']},
            ],
            target,
            render_receipt_path,
            verbosity='verbose',
            capture_log_path=render_log,
        )
        font_evidence_path = folder / 'font-load-evidence.json'
        log_text = render_log.read_text(encoding='utf-8', errors='replace') if render_log.is_file() else ''
        if runner is None:
            if 'fontselect:' in log_text:
                font_evidence = parse_libass_font_log(log_text, expected_font=style_spec['font'], fonts_dir=fonts_dir)
            else:
                font_evidence = verify_libass_font_selection(subtitle, fonts_dir, style_spec['font'], width=w, height=h)
        else:
            font_evidence = (
                parse_libass_font_log(log_text, expected_font=style_spec['font'], fonts_dir=fonts_dir)
                if 'fontselect:' in log_text
                else {
                    'verified': 'mocked_runner',
                    'expected_family': style_spec['font']['family'],
                    'expected_postscript_name': style_spec['font']['postscript_name'],
                    'expected_filename': style_spec['font']['filename'],
                }
            )
        atomic_write_json(font_evidence, font_evidence_path)
        meta = probe_media_metadata(target)
        if abs(float(meta['format']['duration'])-(row['end_s']-row['start_s'])) > 1/fps:
            raise ContractError('rendered segment duration mismatch')
        render_receipt_ref = artifact_reference(render_receipt_path, 'segment render receipt')
        subtitle_ass_ref = artifact_reference(subtitle, 'segment subtitle ass')
        bundle_ref = bundle['bundle_manifest']
        font_ev_ref = artifact_reference(font_evidence_path, 'segment font load evidence')
        atomic_write_json(
            {
                'cache_key': key,
                'sha256': file_hash(target),
                'row': row,
                'render_receipt': render_receipt_ref,
                'subtitle_ass': subtitle_ass_ref,
                'font_bundle_manifest': bundle_ref,
                'font_load_evidence': font_ev_ref,
            },
            marker,
        )
        rendered.append(row['scene_id'])
        assets.append(target)
        segment_receipts.append({
            'scene_id': row['scene_id'],
            'cache_key': key,
            'reused': False,
            'render_receipt': render_receipt_ref,
            'subtitle_ass': subtitle_ass_ref,
            'font_bundle_manifest': bundle_ref,
            'font_load_evidence': font_ev_ref,
        })
    listing = out / 'concat.txt'
    if any("'" in p.as_posix() for p in assets):
        raise ContractError('unsupported apostrophe in concat path')
    listing.write_text(''.join(f"file '{p.as_posix()}'\n" for p in assets), encoding='utf-8')
    candidate = out / 'candidate.mp4'
    concat_receipt_path = out / 'concat-receipt.json'
    execute('concat', ['-f', 'concat', '-safe', '0', '-i', str(listing), '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-threads', '1', '-b:a', '128k', '-movflags', '+faststart', str(candidate)], [{'role': 'segment', 'path': p} for p in assets], candidate, concat_receipt_path)
    meta = probe_media_metadata(candidate)
    expected = rows[-1][0]['end_s']
    audio = [s for s in meta['streams'] if s['codec_type'] == 'audio']
    video = [s for s in meta['streams'] if s['codec_type'] == 'video']
    if not audio or not video or abs(float(audio[0]['duration'])-expected) > .05 or abs(float(video[0]['duration'])-expected) > .05:
        raise ContractError('final audio/video duration mismatch')
    decode = out / 'decoded.framemd5'
    decode_receipt_path = out / 'decode-receipt.json'
    execute('technical_qa', ['-threads', '1', '-i', str(candidate), '-map', '0:v:0', '-an', '-threads', '1', '-f', 'framemd5', str(decode)], [{'role': 'candidate', 'path': candidate}], decode, decode_receipt_path)
    decoded_frames = sum(1 for line in decode.read_text().splitlines() if line and not line.startswith('#'))
    if decoded_frames != round(expected * rows[0][0]['output']['fps']):
        raise ContractError('decoded frame count mismatch')
    global_cues = [
        {
            **c,
            'start_s': c.get('global_start_s', round(row['start_s'] + c['start_s'], 6)),
            'end_s': c.get('global_end_s', round(row['start_s'] + c['end_s'], 6)),
        }
        for row, _ in rows
        for c in row['subtitles']
    ]
    whole_sequence = {
        'schema': SEQUENCE_SCHEMA,
        'cue_count': len(global_cues),
        'total_duration_s': expected,
        'cues': global_cues,
    }
    srt_path = out / 'subtitles.srt'
    srt_path.write_text(export_whole_srt(whole_sequence), encoding='utf-8')
    seq_path = out / 'subtitle-sequence.json'
    atomic_write_json(whole_sequence, seq_path)
    result = {
        'schema': 'video_creation_pipeline.audio_timeline_render.v1',
        'candidate_only': True,
        'subtitle_source_route': 'audio_timeline_segmented',
        'packaging': artifact_reference(packaging_path, 'packaging'),
        'binding': artifact_reference(binding_path, 'binding'),
        'audio': binding['audio'],
        'candidate': artifact_reference(candidate, 'candidate'),
        'sidecar_srt': artifact_reference(srt_path, 'sidecar srt'),
        'subtitle_sequence': artifact_reference(seq_path, 'reviewed subtitle sequence'),
        'concat_receipt': artifact_reference(concat_receipt_path, 'concat receipt'),
        'decode_receipt': artifact_reference(decode_receipt_path, 'decode receipt'),
        'segment_receipts': segment_receipts,
        'rendered_segments': rendered,
        'reused_segments': reused,
        'segments': [r for r, _ in rows],
        'duration_s': expected,
        'decoded_frames': decoded_frames,
        'real_narration_quality_verified': False,
    }
    atomic_write_json(result, out/'manifest.json')
    return result
