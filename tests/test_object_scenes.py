"""Shared-clock and real source-inventory regression; fixtures are synthetic."""
from copy import deepcopy
from unittest.mock import patch

import pytest
from author_binding_fixture import approval_args, fixture_approval, make_case

from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.object_scene_routes import recommend_scene_route
from video_creation_pipeline.object_scenes import (
    SPEC_SCHEMA,
    build_object_scene_plan,
    component_sources,
    validate_object_scene_plan,
)
from video_creation_pipeline.remotion_project import (
    _project_data,
    _project_file_contents,
)
from video_creation_pipeline.remotion_skill_handoff import (
    REMOTION_SKILLS_COMMIT,
    build_remotion_skill_handoff,
)


def object_case(root):
    make_case(root)
    spec = read_json_object(root / 'binding-spec.json', 'fixture spec')
    spec['schema'] = SPEC_SCHEMA
    for name in ('asset', 'source_asset', 'cue_bindings'):
        spec.pop(name)
    spec['visual_tokens'] = {'background': '#F7F8F5', 'foreground': '#172D36', 'primary': '#245B5A', 'accent': '#AD5948', 'muted': '#626F6E', 'bodyFont': 'Microsoft YaHei, sans-serif'}
    content = read_json_object(root / 'content-plan.json', 'content')
    unit = content['shot_units'][0]
    quote = unit['narration']
    def window(offset, duration):
        return {'kind': 'at_for', 'anchor': {'word_id': 'word-001', 'boundary': 'start', 'offset': {'value': offset, 'unit': 'seconds'}}, 'duration': {'value': duration, 'unit': 'seconds'}}
    def scene(identity, component, offset):
        return {'id': identity, 'component_id': component, 'understanding_target': 'Objects retain identity across a staged change',
            'visual_role': 'illustration', 'relationship': 'Ordered stages', 'initial_state': 'Joined', 'final_state': 'Separated',
            'camera_purpose': 'Reveal the stage arrangement', 'headline': quote[:8],
            'objects': [{'id': 'stage-a', 'label': quote[:2], 'source_quote': quote, 'evidence_refs': unit['evidence_refs']},
                        {'id': 'stage-b', 'label': quote[-2:], 'source_quote': quote, 'evidence_refs': unit['evidence_refs']}],
            'temporal_expression': window(offset, 3), 'region': {'x': 0.1, 'y': 0.2, 'width': 0.67, 'height': 0.43},
            'events': [{'id': identity+'-connect', 'action': 'connect', 'target_id': 'stage-a', 'temporal_expression': window(offset, 1)},
                       {'id': identity+'-split', 'action': 'split', 'target_id': 'stage-b', 'temporal_expression': window(offset+1, 1)}]}
    spec['scenes'] = [scene('flow', 'relation_flow_2d', -0.5), scene('stages', 'stage_timeline_3d', 2.5)]
    spec['component_sources'] = component_sources([row['component_id'] for row in spec['scenes']])
    atomic_write_json(spec, root / 'object-spec.json')
    fixture_approval('storyboard', root / 'object-spec.json', root / 'storyboard-approval.json')
    return root


def build(root):
    return build_object_scene_plan(root / 'object-spec.json', **approval_args(root))


def test_two_components_resolve_using_existing_word_clock(tmp_path):
    root = object_case(tmp_path / 'case')
    plan = build(root)
    validate_object_scene_plan(plan)
    assert plan['synthetic_fixture_only']
    assert plan['scenes'][0]['startFrame'] == 15
    assert plan['scenes'][1]['startFrame'] == 105
    assert plan['scenes'][0]['events'][0]['payoffFrame'] == 30
    assert plan['source_recording'] == read_json_object(root / 'object-spec.json', 'spec')['source_recording']


@pytest.mark.parametrize('change', ['word', 'overflow', 'label', 'source', 'overlap', 'mode', 'amount'])
def test_invalid_or_unproven_scene_fails_closed(tmp_path, change):
    root = object_case(tmp_path / 'case')
    spec = read_json_object(root / 'object-spec.json', 'spec')
    row = spec['scenes'][0]
    if change == 'word': row['events'][0]['temporal_expression']['anchor']['word_id'] = 'unknown'
    elif change == 'overflow': row['events'][0]['temporal_expression']['duration']['value'] = 10
    elif change == 'label': row['objects'][0]['label'] = 'invented fact'
    elif change == 'source': spec['component_sources']['ObjectFlow2D.tsx']['sha256'] = '0'*64
    elif change == 'overlap': spec['scenes'][1]['temporal_expression'] = deepcopy(row['temporal_expression'])
    elif change == 'mode': spec['evidence_mode'] = 'measured_recording'
    else: row['objects'][0]['amount_label'] = 'unknown amount'
    atomic_write_json(spec, root / 'object-spec.json')
    fixture_approval('storyboard', root / 'object-spec.json', root / 'storyboard-approval.json')
    with pytest.raises(ContractError): build(root)


def test_rehashed_timing_and_stale_approval_cannot_bypass_sources(tmp_path):
    root = object_case(tmp_path / 'case')
    plan = build(root)
    plan['scenes'][0]['events'][0]['payoffFrame'] += 1
    plan['artifact_sha256'] = payload_sha256(plan, 'artifact_sha256')
    with pytest.raises(ContractError): validate_object_scene_plan(plan)
    spec = read_json_object(root / 'object-spec.json', 'spec')
    spec['scenes'][0]['camera_purpose'] = 'Changed camera intent'
    atomic_write_json(spec, root / 'object-spec.json')
    with pytest.raises(ContractError): build(root)


def test_projection_and_generated_component_sources_are_real(tmp_path):
    root = object_case(tmp_path / 'case')
    packaging = read_json_object(root / 'program-packaging.json', 'packaging')
    packaging['output'] = {'width': 1080, 'height': 1920, 'fps': 30}
    atomic_write_json(packaging, root / 'program-packaging.json')
    spec = read_json_object(root / 'object-spec.json', 'spec')
    spec['source_packaging_plan'] = artifact_reference(root / 'program-packaging.json', 'packaging')
    atomic_write_json(spec, root / 'object-spec.json')
    fixture_approval('storyboard', root / 'object-spec.json', root / 'storyboard-approval.json')
    plan = build(root)
    path = atomic_write_json(plan, root / 'objects.json')
    inputs = {'packaging_plan': artifact_reference(root / 'program-packaging.json', 'packaging'), 'object_scene_plan': artifact_reference(path, 'objects')}
    data = _project_data({'inputs': inputs, 'composition': {'id': 'ObjectTest'}})
    assert data['objectScenes'][0]['startFrame'] == 1365
    files = _project_file_contents(data, subtitle_text='')
    assert 'src/object-scenes/StageTimeline3D.tsx' in files
    assert '"@remotion/three": "4.0.477"' in files['package.json']
    assert 'data.objectScenes.map' in files['src/Composition.tsx']
    assert 'StageTimeline3D scene=' in files['src/object-scenes/ObjectScene.tsx']


def test_legacy_project_does_not_gain_object_fields_or_dependencies(tmp_path):
    packaging = {'schema': 'video_creation_pipeline.video_packaging_plan.v1', 'output': {'width': 1080, 'height': 1920, 'fps': 30},
        'scenes': [{'scene_id': 'legacy', 'start_s': 0, 'end_s': 2}]}
    path = atomic_write_json(packaging, tmp_path / 'packaging.json')
    data = _project_data({'inputs': {'packaging_plan': artifact_reference(path, 'packaging')}, 'composition': {'id': 'Legacy'}})
    assert 'objectScenes' not in data
    files = _project_file_contents(data, subtitle_text='')
    assert '@remotion/three' not in files['package.json']
    assert not any('object-scenes/' in name for name in files)


def test_recording_drift_and_payoff_on_exclusive_end_are_rejected(tmp_path):
    root = object_case(tmp_path / 'case')
    plan = build(root)
    with (root / 'synthetic-silence.wav').open('ab') as audio:
        audio.write(b'changed recording')
    with pytest.raises(ContractError): validate_object_scene_plan(plan)
    root = object_case(tmp_path / 'second-case')
    spec = read_json_object(root / 'object-spec.json', 'spec')
    spec['scenes'][0]['events'][1]['temporal_expression']['duration']['value'] = 2
    atomic_write_json(spec, root / 'object-spec.json')
    fixture_approval('storyboard', root / 'object-spec.json', root / 'storyboard-approval.json')
    with pytest.raises(ContractError, match='held frame'): build(root)


def test_native_materialization_rejects_rehashed_source_edits(tmp_path):
    from test_remotion_project import RemotionProjectTests

    from video_creation_pipeline.remotion_project import (
        REMOTION_RUNTIME_COMMIT,
        materialize_remotion_project,
        validate_remotion_project_manifest,
    )
    root = object_case(tmp_path / 'case')
    packaging = read_json_object(root / 'program-packaging.json', 'packaging')
    packaging['output'] = {'width': 1080, 'height': 1920, 'fps': 30}
    atomic_write_json(packaging, root / 'program-packaging.json')
    spec = read_json_object(root / 'object-spec.json', 'spec')
    spec['source_packaging_plan'] = artifact_reference(root / 'program-packaging.json', 'packaging')
    atomic_write_json(spec, root / 'object-spec.json')
    fixture_approval('storyboard', root / 'object-spec.json', root / 'storyboard-approval.json')
    plan_path = atomic_write_json(build(root), root / 'objects.json')
    fixture = RemotionProjectTests()
    fixture.skills, fixture.runtime = root / 'skills', root / 'runtime'
    fixture._write_skills_source()
    fixture._write_runtime_source()
    three = fixture.runtime / 'packages/three'
    (three / 'src').mkdir(parents=True)
    atomic_write_json({'name': '@remotion/three', 'version': '4.0.477'}, three / 'package.json')
    for name in ('index.ts','ThreeCanvas.tsx','SuspenseLoader.tsx'):
        (three / 'src' / name).write_text('// synthetic Three source only', encoding='utf-8')
    with patch('video_creation_pipeline.remotion_skill_handoff._git_head', return_value=REMOTION_SKILLS_COMMIT), patch('video_creation_pipeline.remotion_project._git_head', return_value=REMOTION_RUNTIME_COMMIT):
        handoff = build_remotion_skill_handoff(fixture.skills, root / 'program-packaging.json', composition_id='Objects', object_scene_plan_path=plan_path)
        handoff_path = atomic_write_json(handoff, root / 'handoff.json')
        paths = materialize_remotion_project(handoff_path, fixture.runtime, root / 'project', license_basis='individual')
        manifest = read_json_object(paths['manifest'], 'manifest')
        validate_remotion_project_manifest(manifest)
        assert manifest['object_scene_runtime_source']['dependency_pins']['three'] == '0.178.0'
        altered = root / 'project/src/object-scenes/ObjectFlow2D.tsx'
        altered.write_text(altered.read_text(encoding='utf-8')+'\n// manual override\n', encoding='utf-8')
        manifest['generated_files']['src/object-scenes/ObjectFlow2D.tsx'] = artifact_reference(altered, 'altered')
        manifest['manifest_sha256'] = payload_sha256(manifest, 'manifest_sha256')
        with pytest.raises(ContractError, match='generated source'):
            validate_remotion_project_manifest(manifest)


def test_scene_route_keeps_evidence_separate_and_adds_specialists_only_for_gaps():
    assert recommend_scene_route('stages', needs_depth=True)['component_id'] == 'stage_timeline_3d'
    assert recommend_scene_route('stages')['component_id'] == 'relation_flow_2d'
    assert recommend_scene_route('evidence', needs_depth=True)['visual_route'] == 'hyperframes_evidence_focus'
    assert recommend_scene_route('space', needs_depth=True)['component_id'] is None
    assert recommend_scene_route('space', capability_gap='complex_model_or_material')['specialist_handoff'] == 'blender'
    with pytest.raises(ContractError): recommend_scene_route('evidence', capability_gap='complex_model_or_material')
    with pytest.raises(ContractError): recommend_scene_route('unknown-topic')
