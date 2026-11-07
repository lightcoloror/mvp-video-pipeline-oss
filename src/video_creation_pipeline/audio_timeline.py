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
from .subtitle_readability import subtitle_layout, subtitle_font


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
    if any(type(v) is not int or v <= 0 for v in (width, height, fps)) or width % 2 or height % 2 or width > 1080 or height > 1920 or fps > 30:
        raise ContractError('invalid bounded output geometry')
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
        font_px,typography_hash=subtitle_font(width,height,media.get('subtitle_font_px'))
        cues, previous = [], start_s
        for cue in media['subtitles']:
            a, b = number(cue['start_s'], 'subtitle start'), number(cue['end_s'], 'subtitle end')
            text = cue['text']
            if not start_s <= a < b <= end_s or a < previous:
                raise ContractError('subtitle outside scene or overlapping')
            layout=subtitle_layout(text,semantic_breaks=cue.get('semantic_breaks',()),
                protected_terms=cue.get('protected_terms',()),duration_s=b-a,review=cue.get('readability_review'),
                max_line_units=math.floor(width*.86/font_px))
            previous = b
            cues.append({'start_s': a-start_s, 'end_s': b-start_s, 'text': text,'lines':layout['lines']})
        if not cues:
            raise ContractError('explicit subtitles required')
        chunk = pcm[start*channels*2:stop*channels*2]
        row = {'scene_id': sid, 'start_s': start_s, 'end_s': end_s, 'audio_sha256': hashlib.sha256(chunk).hexdigest(), 'audio_samples': stop-start, 'rate': rate, 'channels': channels, 'visual': media['visual'], 'provenance': media['provenance'], 'semantic_description': media['semantic_description'], 'visual_start_s': offset, 'subtitles': cues, 'output': output, 'renderer_projection': projected, 'implementation_sha256': file_hash(__file__)}
        row['subtitle_font_px'],row['typography_policy_sha256']=font_px,typography_hash
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
    rendered, reused, assets = [], [], []
    policy = ReviewResourcePolicy(ffmpeg_threads=1)

    def execute(operation, args, inputs, output, receipt):
        return execute_ffmpeg_with_receipt(operation, ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-filter_threads', '1', '-filter_complex_threads', '1', *args], inputs, [{'role': 'media', 'path': output}], receipt, guard_resource=True, resource_policy=policy, runner=runner, timeout_s=120)

    for row, pcm in rows:
        key = row['cache_key']
        folder = cache / key
        target, marker = folder / 'segment.mkv', folder / 'complete.json'
        if marker.exists():
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if saved['cache_key'] == key and target.is_file() and file_hash(target) == saved['sha256']:
                reused.append(row['scene_id'])
                assets.append(target)
                continue
            raise ContractError('cached segment integrity mismatch')
        folder.mkdir(exist_ok=True)
        wavpath, subtitle = folder / 'audio.wav', folder / 'subtitles.ass'
        with wave.open(str(wavpath), 'wb') as wav:
            wav.setnchannels(row['channels']); wav.setsampwidth(2); wav.setframerate(row['rate']); wav.writeframes(pcm)
        w, h, fps = (row['output'][k] for k in ('width', 'height', 'fps'))
        font = row['subtitle_font_px']
        header = f'[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\nStyle: Default,Arial,{font},&H00FFFFFF,&H00101010,3,2,0,2,{round(w*.07)},{round(w*.07)},{round(h*.09)}\n[Events]\nFormat: Layer, Start, End, Style, Text\n'
        subtitle.write_text(header + ''.join(f"Dialogue: 0,{ass_time(c['start_s'])},{ass_time(c['end_s'])},Default,"+'\\N'.join(c['lines'])+'\n' for c in row['subtitles']), encoding='utf-8')
        escaped = subtitle.as_posix().replace(':', '\\:')
        filters = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},subtitles=filename='{escaped}'"
        execute('render', ['-threads', '1', '-ss', str(row['visual_start_s']), '-i', str(row['visual']['path']), '-threads', '1', '-i', str(wavpath), '-t', str(row['end_s']-row['start_s']), '-map', '0:v:0', '-map', '1:a:0', '-vf', filters, '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-crf', '22', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', str(target)], [{'role': 'visual', 'path': row['visual']['path']}, {'role': 'audio', 'path': wavpath}, {'role': 'subtitle', 'path': subtitle}], target, folder/'render-receipt.json')
        meta = probe_media_metadata(target)
        if abs(float(meta['format']['duration'])-(row['end_s']-row['start_s'])) > 1/fps:
            raise ContractError('rendered segment duration mismatch')
        atomic_write_json({'cache_key': key, 'sha256': file_hash(target), 'row': row}, marker)
        rendered.append(row['scene_id']); assets.append(target)
    listing = out / 'concat.txt'
    if any("'" in p.as_posix() for p in assets):
        raise ContractError('unsupported apostrophe in concat path')
    listing.write_text(''.join(f"file '{p.as_posix()}'\n" for p in assets), encoding='utf-8')
    candidate = out / 'candidate.mp4'
    execute('concat', ['-f', 'concat', '-safe', '0', '-i', str(listing), '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-threads', '1', '-b:a', '128k', '-movflags', '+faststart', str(candidate)], [{'role': 'segment', 'path': p} for p in assets], candidate, out/'concat-receipt.json')
    meta = probe_media_metadata(candidate)
    expected = rows[-1][0]['end_s']
    audio = [s for s in meta['streams'] if s['codec_type'] == 'audio']
    video = [s for s in meta['streams'] if s['codec_type'] == 'video']
    if not audio or not video or abs(float(audio[0]['duration'])-expected) > .05 or abs(float(video[0]['duration'])-expected) > .05:
        raise ContractError('final audio/video duration mismatch')
    decode = out / 'decoded.framemd5'
    execute('technical_qa', ['-threads', '1', '-i', str(candidate), '-map', '0:v:0', '-an', '-threads', '1', '-f', 'framemd5', str(decode)], [{'role': 'candidate', 'path': candidate}], decode, out/'decode-receipt.json')
    decoded_frames = sum(1 for line in decode.read_text().splitlines() if line and not line.startswith('#'))
    if decoded_frames != round(expected * rows[0][0]['output']['fps']):
        raise ContractError('decoded frame count mismatch')
    result = {'schema': 'video_creation_pipeline.audio_timeline_render.v1', 'candidate_only': True, 'packaging': artifact_reference(packaging_path, 'packaging'), 'binding': artifact_reference(binding_path, 'binding'), 'audio': binding['audio'], 'candidate': artifact_reference(candidate, 'candidate'), 'rendered_segments': rendered, 'reused_segments': reused, 'segments': [r for r, _ in rows], 'duration_s': expected, 'decoded_frames': decoded_frames, 'real_narration_quality_verified': False}
    atomic_write_json(result, out/'manifest.json')
    return result
