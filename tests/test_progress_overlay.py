import copy
from pathlib import Path

import pytest

from video_creation_pipeline.contract_io import atomic_write_json, file_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.final_layout_safety import validate_final_layout
from video_creation_pipeline.progress_overlay import build, filtergraph, validate


@pytest.fixture
def case(tmp_path, monkeypatch):
    from video_creation_pipeline import progress_overlay as module
    program = tmp_path/'base.mp4'
    program.write_bytes(b'synthetic-test-only')
    font = tmp_path/'font.ttf'
    font.write_bytes(b'font-fixture-not-for-rendering')
    packaging = {'schema': 'video_creation_pipeline.video_packaging_plan.v1',
                 'output': {'width': 1080, 'height': 1920, 'fps': 24},
                 'scenes': [{'scene_id': f'scene-{i}', 'start_s': i*2, 'end_s': (i+1)*2} for i in range(3)]}
    spec = {'mode': 'chapters', 'position': 'top', 'margin_x_px': 64, 'edge_offset_px': 160,
            'bar_height_px': 12, 'track_color': '303840', 'fill_color': '44DDBB',
            'text_color': 'FFFFFF', 'font_file': str(font), 'font_size_px': 54,
            'typography_profile': 'semantic_motion_1080x1920', 'typography_role': 'motion_label',
            'color_reason': 'neutral track and bright progress',
            'title_source': 'manual', 'title_override_reason': 'Explicit synthetic labels for legacy contract coverage',
            'safe_area_mode': 'protected',
            'chapters': [{'scene_id': f'scene-{i}', 'title': title} for i, title in enumerate(['开场', '说明', '结尾'])]}
    layout = {'schema_version': 'video-final-layout-safety.v1', 'layout_id': 'progress-test',
              'duration_seconds': 6, 'candidate': {'path': 'base.mp4', 'expected_sha256': file_sha256(program)},
              'layout_evidence': {'path': 'evidence.txt', 'expected_sha256': ''},
              'required_roles': ['subtitle', 'ai_label'], 'elements': [{'element_id': 'subtitles', 'role': 'subtitle',
              'start_seconds': 0, 'end_seconds': 6, 'critical_content': True,
              'rect': {'x': .1, 'y': .72, 'width': .8, 'height': .1}},
              {'element_id': 'ai-label', 'role': 'ai_label', 'text': 'AI 合成内容（测试夹具）',
              'start_seconds': 0, 'end_seconds': 6, 'critical_content': True,
              'rect': {'x': .2, 'y': .45, 'width': .5, 'height': .05}}],
              'reserved_regions': [{'region_id': f'corner-{i}', 'region_kind': 'rounded_corner',
              'rect': {'x': x, 'y': y, 'width': .02, 'height': .02}} for i, (x,y) in enumerate([(0,0),(.98,0),(0,.98),(.98,.98)])]}
    (tmp_path/'evidence.txt').write_text('synthetic manual layout')
    layout['layout_evidence']['expected_sha256'] = file_sha256(tmp_path/'evidence.txt')
    metadata = {'streams': [{'codec_type': 'video', 'width': 1080, 'height': 1920,
                             'avg_frame_rate': '24/1', 'duration': '6'}], 'format': {'duration': '6'}}
    monkeypatch.setattr(module, 'probe_media_metadata', lambda path: metadata)
    return {'root': tmp_path, 'program': program, 'packaging': packaging, 'spec': spec, 'layout': layout, 'metadata': metadata}


def create(case):
    root = case['root']
    for key in ('packaging', 'layout', 'spec'):
        atomic_write_json(case[key], root/f'{key}.json')
    return build(case['program'], root/'packaging.json', root/'layout.json', root/'spec.json', root)


def test_build_uses_scene_clock_and_preserves_layout(case):
    original = copy.deepcopy(case['layout'])
    plan = create(case)
    assert [(r['start_s'], r['end_s']) for r in plan['chapters']] == [(0,2),(2,4),(4,6)]
    assert case['layout'] == original
    assert plan['layout_check']['checked_elements'] == 3
    path = atomic_write_json(plan, case['root']/'plan.json')
    assert validate(path) == plan


def test_narrow_band_reserves_exact_width_without_shrinking_labels(case):
    case['spec'].update(bar_width_px=800)
    plan=create(case)
    assert plan['geometry']['bar_width']==800
    assert plan['geometry']['rect']['width']==800/1080
    case['spec']['bar_width_px']=1000
    with pytest.raises(ContractError,match='bar width'):
        create(case)


@pytest.mark.parametrize('position', ['top', 'bottom'])
def test_plain_top_bottom_and_no_overflow(case, position):
    case['spec'].update(mode='plain', position=position, chapters=[])
    plan = create(case)
    y = plan['geometry']['bar_y']
    assert y == (160 if position == 'top' else 1920-160-12)
    graph = filtergraph(plan, case['root'])
    assert '[track][fill]overlay' in graph
    assert 'drawtext' not in graph
    assert 'min(t/5.958333333333333,1)' in graph


@pytest.mark.parametrize('change,match', [
    ('order', 'scene order'), ('missing_first', 'start at zero'),
    ('time_field', 'no independent timestamps'), ('small_font', 'positive'),
    ('long_title', 'too long'), ('collision', 'collide'), ('platform', 'reserved region'),
    ('gap', 'gaps'), ('duration', 'duration differs'),
])
def test_bad_inputs_fail_before_render(case, change, match):
    if change == 'order':
        case['spec']['chapters'][1:] = list(reversed(case['spec']['chapters'][1:]))
    elif change == 'missing_first':
        case['spec']['chapters'].pop(0)
    elif change == 'time_field':
        case['spec']['chapters'][0]['start_s'] = 0
    elif change == 'small_font':
        case['spec']['font_size_px'] = 0
    elif change == 'long_title':
        case['spec']['chapters'][0]['title'] = '文字太长'*20
    elif change == 'collision':
        case['spec']['edge_offset_px'] = 1400
    elif change == 'platform':
        case['layout']['reserved_regions'].append({'region_id': 'platform-top', 'region_kind': 'platform_overlay',
                                                   'rect': {'x': .05, 'y': .05, 'width': .9, 'height': .1}})
    elif change == 'gap':
        case['packaging']['scenes'][1]['start_s'] = 2.5
    elif change == 'duration':
        case['metadata']['streams'][0]['duration'] = '7'
    with pytest.raises(ContractError, match=match):
        create(case)


def test_platform_reservation_passes_before_progress_overlay_insertion(case):
    # The base layout plus the platform-top reservation is valid on its own; only
    # build()'s critical progress overlay makes the 'platform' negative case fail.
    case['layout']['reserved_regions'].append({'region_id': 'platform-top', 'region_kind': 'platform_overlay',
                                               'rect': {'x': .05, 'y': .05, 'width': .9, 'height': .1}})
    assert validate_final_layout(case['layout'], case['root'])['status'] == 'passed'
    with pytest.raises(ContractError, match='reserved region'):
        create(case)


def test_edit_or_font_change_invalidates_plan(case):
    plan = create(case)
    path = atomic_write_json(plan, case['root']/'plan.json')
    Path(plan['font']['path']).write_bytes(b'changed font')
    with pytest.raises(ContractError, match='drift'):
        validate(path)
    case['program'].write_bytes(b'new edit')
    with pytest.raises(ContractError, match='changed'):
        validate(path)


def test_text_is_not_executed_as_filter_expression(case):
    case['spec']['chapters'][0]['title'] = '%{pts}'
    plan = create(case)
    graph = filtergraph(plan, case['root'])
    assert 'expansion=none' in graph
    assert '%{pts}' not in graph
    assert '%{pts}' in (case['root']/'chapter-00.txt').read_text(encoding='utf-8')


def script_defaults(case):
    script=case['root']/'video-script.md'
    script.write_text('# Synthetic script\n\n## 01｜Opening\nFirst paragraph.\n\n## 02｜Method\nSecond paragraph.\n\n## 03｜Outcome\nLast paragraph.\n',encoding='utf-8')
    from video_creation_pipeline.contract_io import artifact_reference
    case['packaging']['script_ref']=artifact_reference(script,'video script')
    for key in ('chapters','title_source','title_override_reason','font_size_px','typography_profile','typography_role'):
        case['spec'].pop(key,None)
    return script


def test_default_titles_and_font_come_from_script_and_navigation_preset(case):
    script_defaults(case)
    plan=create(case)
    assert [r['text'] for r in plan['chapters']]==['01/03  Opening','02/03  Method','03/03  Outcome']
    assert plan['chapter_title_source']['mode']=='script'
    assert plan['typography']['font_size_px']==40
    assert plan['typography']['preset_id']=='compact'
    assert 'fontsize=40:' in filtergraph(plan,case['root'])


def test_chapter_titles_do_not_silently_fall_back_to_independent_labels(case):
    case['spec'].pop('title_source');case['spec'].pop('title_override_reason')
    with pytest.raises(ContractError,match='script.*required'):
        create(case)
    script_defaults(case)
    case['spec']['chapters']=[{'scene_id':'scene-0','script_heading':'01','title':'Different summary'}]
    with pytest.raises(ContractError,match='differs from script'):
        create(case)


def test_manual_title_override_requires_a_reason(case):
    case['spec'].pop('title_override_reason')
    with pytest.raises(ContractError,match='override.*reason'):
        create(case)


@pytest.mark.parametrize('preset,expected',[('small',32),('compact',40),('comfortable',48)])
def test_navigation_font_presets_are_independent_of_body_and_subtitle_minima(case,preset,expected):
    script_defaults(case);case['spec']['font_preset']=preset
    plan=create(case)
    assert plan['typography']['font_size_px']==expected
    # A deliberate navigation override is permitted below the old motion/subtitle minima.
    case['spec'].update(font_size_px=16,typography_profile='not-a-body-profile',typography_role='subtitle')
    assert create(case)['typography']['font_size_px']==16


def test_script_edit_invalidates_old_plan(case):
    script=script_defaults(case);plan=create(case)
    path=atomic_write_json(plan,case['root']/'plan.json')
    script.write_text(script.read_text(encoding='utf-8').replace('Method','New method'),encoding='utf-8')
    with pytest.raises(ContractError,match='changed'):
        validate(path)


def test_numbered_script_headings_and_scene_mapping_must_be_unambiguous(case):
    script=script_defaults(case)
    script.write_text('## 01｜First\n## 01｜Repeated\n',encoding='utf-8')
    from video_creation_pipeline.contract_io import artifact_reference
    case['packaging']['script_ref']=artifact_reference(script,'video script')
    with pytest.raises(ContractError,match='duplicate script heading'):
        create(case)


def test_selected_script_heading_uses_only_scene_times(case):
    script_defaults(case)
    case['spec']['chapters']=[{'scene_id':'scene-0','script_heading':'01'},
                            {'scene_id':'scene-2','script_heading':'03'}]
    plan=create(case)
    assert [(r['start_s'],r['text']) for r in plan['chapters']]==[(0,'01/02  Opening'),(4,'02/02  Outcome')]


def test_navigation_preset_change_invalidates_bound_plan(case,monkeypatch):
    from video_creation_pipeline import progress_overlay as module
    import json
    script_defaults(case)
    policy=case['root']/'navigation-presets.json'
    policy.write_bytes(module.FONT_PRESETS_PATH.read_bytes())
    monkeypatch.setattr(module,'FONT_PRESETS_PATH',policy)
    plan=create(case);path=atomic_write_json(plan,case['root']/'plan.json')
    data=json.loads(policy.read_text());data['presets']['compact']['font_size_px']=42
    atomic_write_json(data,policy)
    with pytest.raises(ContractError,match='drift'):
        validate(path)


def test_shared_delivery_script_reference_preserves_hash_validation(case):
    script_defaults(case)
    reference=case['packaging']['script_ref']
    case['packaging']['script_ref']={k:reference[k] for k in ('path','sha256')}
    assert create(case)['chapter_title_source']['mode']=='script'
    case['packaging']['script_ref']['sha256']='0'*64
    with pytest.raises(ContractError,match='changed'):
        create(case)
