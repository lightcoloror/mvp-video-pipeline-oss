"""Offline fixtures only. The named attestations are NOT real human approval."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import wave

from video_creation_pipeline.author_bindings import AUTHOR_BINDING_SPEC_SCHEMA, WORD_ALIGNMENT_EVIDENCE_SCHEMA
from video_creation_pipeline.content_video_pipeline import build_content_video_project_plan, validate_content_video_inputs
from video_creation_pipeline.contract_io import artifact_reference, atomic_write_json
from video_creation_pipeline.creative_gates import build_approval_record
from video_creation_pipeline.production_strategy import build_content_visual_production_plan

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def fixture_approval(target, source, output, **kwargs):
    return atomic_write_json(build_approval_record(target, source,
        approved_by='synthetic-test-fixture-not-a-person', approved_at='2026-09-19T20:00:00+08:00',
        comment='OFFLINE FIXTURE ONLY; no real script, voice, or publication approval.', **kwargs), output)


def make_case(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES / 'content-video-brief-v1/upstream-canonical', root / 'inputs')
    shutil.copytree(FIXTURES / 'semantic-motion', root / 'motion')
    inputs = root / 'inputs'
    verified = validate_content_video_inputs(
        inputs / 'content-video-brief.v1.json', inputs / 'content-video-brief.v1.schema.json',
        inputs / 'research-output.v1.json', inputs / 'research-output.v1.schema.json', max_age_hours=168,
    )
    content = build_content_video_project_plan(verified, width=1080, height=1920)
    content_path = atomic_write_json(content, root / 'content-plan.json')
    scene_id = content['shot_units'][0]['scene_id']
    quote = content['shot_units'][0]['narration']
    visual = build_content_visual_production_plan(content_path, scene_id)
    visual_path = atomic_write_json(visual, root / 'visual-plan.json')
    recording = root / 'synthetic-silence.wav'
    with wave.open(str(recording), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x00\x00' * 16000 * 12)
    raw_path = atomic_write_json({'synthetic_fixture_only': True, 'observed_speech': False,
                                 'note': 'Timing is supplied, not measured from this silent WAV.'}, root / 'raw-alignment.json')
    alignment = {
        'schema': WORD_ALIGNMENT_EVIDENCE_SCHEMA, 'evidence_mode': 'synthetic_fixture',
        'time_domain': 'recording_ms', 'scene_id': scene_id, 'quote': quote,
        'source_script': content['inputs']['content_video_brief'],
        'source_recording': artifact_reference(recording, 'recording'),
        'source_raw_alignment': artifact_reference(raw_path, 'raw alignment'),
        'engine': 'offline-supplied-test-data',
        'words': [{'word_id': 'word-001', 'text': quote[:2], 'text_start': 0, 'text_end': 2,
                   'start_ms': 3000, 'end_ms': 3500, 'timing_origin': 'synthetic_supplied_word'}],
    }
    alignment_path = atomic_write_json(alignment, root / 'alignment.json')
    packaging = {'schema': 'video_creation_pipeline.video_packaging_plan.v1', 'status': 'candidate_only',
                 'scenes': [{'scene_id': scene_id, 'story_segment_ref': scene_id, 'start_s': 45.0, 'end_s': 53.0}]}
    packaging_path = atomic_write_json(packaging, root / 'program-packaging.json')
    native = json.loads((root / 'motion/build-spec.json').read_text(encoding='utf-8'))
    cue = deepcopy(native['cues'][0])
    cue['story_segment_ref'] = scene_id
    cue['timing'].pop('timeline_start_s')
    spec = {
        'schema': AUTHOR_BINDING_SPEC_SCHEMA, 'evidence_mode': 'synthetic_fixture',
        'source_media_role': 'creator_narration', 'playback_rate': 1, 'scene_id': scene_id,
        'source_visual_production_plan': artifact_reference(visual_path, 'visual'),
        'source_recording': artifact_reference(recording, 'recording'),
        'source_word_alignment': artifact_reference(alignment_path, 'alignment'),
        'source_packaging_plan': artifact_reference(packaging_path, 'packaging'),
        'source_asset': artifact_reference(root / 'motion/source-evidence.svg', 'image'),
        'asset': {key: native[key] for key in ('asset_id', 'media_type', 'evidence_status', 'authorization_status', 'deidentified')},
        'output': {key: native[key] for key in ('width', 'height', 'fps')},
        'recording_start_s': 2.0, 'recording_end_s': 10.0,
        'cue_bindings': [{'word_id': 'word-001', 'boundary': 'start', 'offset_ms': 100, 'cue': cue}],
    }
    atomic_write_json(spec, root / 'binding-spec.json')
    refresh_fixture_approvals(root)
    return root


def refresh_fixture_approvals(root):
    root = Path(root)
    atomic_write_json({'schema': 'video_creation_pipeline.voice_measurements.v1',
        'synthetic_fixture_only': True, 'measured': False,
        'source_recording': artifact_reference(root / 'synthetic-silence.wav', 'fixture audio')},
        root / 'fixture-voice-measurements.json')
    fixture_approval('script', root / 'inputs/content-video-brief.v1.json', root / 'script-approval.json')
    fixture_approval('voice', root / 'synthetic-silence.wav', root / 'voice-approval.json', attachments=[
        {'role': 'audio', 'path': str(root / 'synthetic-silence.wav')},
        {'role': 'voice_measurements', 'path': str(root / 'fixture-voice-measurements.json')},
        {'role': 'word_alignment', 'path': str(root / 'alignment.json')},
    ])
    fixture_approval('storyboard', root / 'binding-spec.json', root / 'storyboard-approval.json')


def approval_args(root):
    return {name + '_approval_path': Path(root) / (name + '-approval.json') for name in ('script', 'voice', 'storyboard')}
