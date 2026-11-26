import copy

import pytest

from test_progress_overlay import case as base_case
from video_creation_pipeline.contract_io import artifact_reference, atomic_write_json
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.expressive_captions import SCHEMA, ass_document, build, validate


@pytest.fixture
def case(base_case, monkeypatch):
    from video_creation_pipeline import expressive_captions as module
    monkeypatch.setattr(module, '_font', lambda path, texts: 'Fixture Font')
    root = base_case['root']
    atomic_write_json(base_case['packaging'], root/'packaging.json')
    base_case['events'] = {'schema':SCHEMA+'.events',
        'program':artifact_reference(base_case['program'],'program'),
        'packaging':artifact_reference(root/'packaging.json','packaging'),
        'evidence':artifact_reference(root/'evidence.txt','evidence'), 'review_status':'synthetic_fixture',
        'events':[{'event_id':'laugh-1','scene_id':'scene-1','kind':'laughter','speaker_id':'father',
                   'start_frame':50,'end_frame':80,'text':'哈哈哈','fallback_text':'父亲笑'}]}
    base_case['spec'] = {'font_file':str(root/'font.ttf'),'font_size_px':60,
        'typography_profile':'semantic_motion_1080x1920',
        'speakers':{'father':{'label':'父亲','color':'65D8D0','identity_note':'父亲标签在同侧', 'color_reason':'示例角色色'}},
        'effects':{'laugh-1':{'mode':'float_fade','x_px':540,'y_px':650,'end_scale':1.15,'rise_px':60,'tail_frames':8}}}
    return base_case


def create(case):
    root = case['root']
    for k in ('events','spec','layout'):
        atomic_write_json(case[k], root/f'{k}.json')
    return build(case['program'],root/'packaging.json',root/'layout.json',root/'events.json',root/'spec.json',root)


def test_tail_and_fallback_preserve_measured_sound_interval(case):
    original = copy.deepcopy(case['layout'])
    plan = create(case)
    cue = plan['captions'][0]
    assert (cue['start_frame'],cue['end_frame'],cue['visual_end_frame']) == (50,80,88)
    assert case['layout'] == original
    assert plan['layout_check']['status'] == 'passed'
    path = atomic_write_json(plan,case['root']/'plan.json')
    assert validate(path) == plan
    ass = ass_document(plan)
    assert '\\1c&HD0D865&' in ass and '\\move(540,650,540,590' in ass
    assert '\\fscx115' in ass and '\\fad(0,' in ass
    assert '0:00:02.08,0:00:03.66' in ass  # centisecond quantization preserves sampled frames


@pytest.mark.parametrize('mode',['stable','grow'])
def test_stable_and_grow_do_not_move_or_extend(case, mode):
    case['spec']['effects']['laugh-1'].update(mode=mode,rise_px=0,tail_frames=0,end_scale=1 if mode=='stable' else 1.2)
    plan = create(case)
    ass = ass_document(plan)
    assert '\\pos(540,650)' in ass and '\\move' not in ass
    assert ('\\t(' in ass) == (mode == 'grow')
    assert plan['captions'][0]['visual_end_frame'] == 80


@pytest.mark.parametrize('change,match',[
    ('unverified','verification'),('no_speaker','unknown speaker'),('command','ASS commands'),
    ('too_small','typography minimum'),('overshoot','finite'),('offscreen','rect'),
    ('collision','collide'),('tail_scene','same scene'),('long_tail','0.5 s'),
    ('sound_scene','inside its bound scene'),('duplicate','duplicate'),('unknown_effect','unmatched'),
    ('ignored','silently ignore'),('foreign_edit','different locked edit'),('float_zero','positive'),
])
def test_invalid_inputs_fail_before_render(case, change, match):
    event, effect = case['events']['events'][0],case['spec']['effects']['laugh-1']
    if change == 'unverified': case['events']['review_status']='automatic_guess'
    elif change == 'no_speaker': event['speaker_id']='unknown'
    elif change == 'command': event['text']='{\\pos(1,2)}哈'
    elif change == 'too_small': case['spec']['font_size_px']=20
    elif change == 'overshoot': effect['end_scale']=2
    elif change == 'offscreen': effect['x_px']=10
    elif change == 'collision': effect['y_px']=1400
    elif change == 'tail_scene': event['end_frame']=95
    elif change == 'long_tail': effect['tail_frames']=13
    elif change == 'sound_scene': event['start_frame']=40
    elif change == 'duplicate': case['events']['events'].append(copy.deepcopy(event))
    elif change == 'unknown_effect': case['spec']['effects']['other']=copy.deepcopy(effect)
    elif change == 'ignored': effect['mode']='stable'
    elif change == 'foreign_edit': case['events']['program']=artifact_reference(case['root']/'evidence.txt','program')
    elif change == 'float_zero': effect['rise_px']=0
    with pytest.raises(ContractError, match=match): create(case)


@pytest.mark.parametrize('changed',['program','evidence','font','spec','plan'])
def test_dependencies_and_plan_are_hash_bound(case, changed):
    plan = create(case)
    path = atomic_write_json(plan,case['root']/'plan.json')
    if changed == 'plan':
        plan['captions'][0]['color']='FFFFFF'
        atomic_write_json(plan,path)
    else:
        from pathlib import Path
        Path(plan[changed]['path']).write_bytes(b'changed')
    with pytest.raises(ContractError): validate(path)


def test_embedded_subtitles_are_not_silently_dropped(case):
    case['metadata']['streams'].append({'codec_type':'subtitle'})
    with pytest.raises(ContractError,match='embedded stable subtitles'): create(case)
