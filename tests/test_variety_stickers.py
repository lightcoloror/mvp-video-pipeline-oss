import copy

import pytest

from test_expressive_captions import base_case, case, create
from video_creation_pipeline import variety_stickers as stickers
from video_creation_pipeline.contract_io import atomic_write_json, read_json_object
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.expressive_captions import validate


def setup_variety(case, mode='variety'):
    case['spec']['font_size_px'] = 110
    case['spec']['effects']['laugh-1'].update(mode=mode, end_scale=1)
    case['events']['events'][0]['syllable_frames'] = [50, 58, 66]
    return create(case)


def test_separate_reveals_and_visual_tail_preserve_sound_clock(case):
    plan = setup_variety(case)
    cue = plan['captions'][0]
    recipe = read_json_object(stickers.RECIPE, 'recipe')
    state = lambda i, frame: stickers.letter_state(cue, i, frame, 24, recipe)
    assert state(0, 49) is None and state(1, 57) is None and state(2, 65) is None
    assert state(0, 54)['opacity'] > 0 and state(1, 62)['opacity'] > 0
    assert state(2, 70)['opacity'] > 0
    assert cue['end_frame'] == 80 and cue['visual_end_frame'] == 88
    assert 0 < state(2, 84)['opacity'] < 1
    assert all(state(i, 88) is None for i in range(3))
    assert state(2, 72) == state(2, 72)  # random-access rendering has no prior-frame dependency


@pytest.mark.parametrize('onsets', [None, [], [50, 58], [49, 58, 66], [50, 58, 80], [50, 58, 58], [50, 58.0, 66], [False, 58, 66]])
def test_unmeasured_or_invalid_onsets_fail_before_render(case, onsets):
    setup_variety(case)
    case['events']['events'][0]['syllable_frames'] = onsets
    with pytest.raises(ContractError, match='syllable_frames'):
        create(case)


def test_reduced_motion_keeps_shape_without_bounce_or_drift(case):
    plan = setup_variety(case, 'variety_reduced')
    cue = plan['captions'][0]
    recipe = read_json_object(stickers.RECIPE, 'recipe')
    first = stickers.letter_state(cue, 0, 50, 24, recipe)
    last = stickers.letter_state(cue, 0, 84, 24, recipe)
    for key in ('x_em', 'y_em', 'y_px', 'scale', 'squash', 'angle'):
        assert first[key] == last[key]
    assert first['opacity'] == 1 and last['opacity'] < 1


def test_large_moving_shape_is_checked_against_existing_subtitle(case):
    setup_variety(case)
    case['spec']['effects']['laugh-1']['y_px'] = 1400
    with pytest.raises(ContractError, match='collide'):
        create(case)


def test_recipe_change_invalidates_saved_plan(case, monkeypatch):
    plan = setup_variety(case)
    path = atomic_write_json(plan, case['root']/'plan.json')
    assert validate(path) == plan
    refs = copy.deepcopy(plan['variety_sources'])
    refs['recipe']['sha256'] = '0'*64
    monkeypatch.setattr(stickers, 'source_refs', lambda: refs)
    with pytest.raises(ContractError, match='dependency drift'):
        validate(path)


def test_no_silent_mix_of_ass_and_sticker_renderers(case):
    setup_variety(case)
    second = copy.deepcopy(case['events']['events'][0])
    second.update(event_id='laugh-2', start_frame=81, end_frame=84)
    second.pop('syllable_frames')
    case['events']['events'].append(second)
    case['spec']['effects']['laugh-2'] = {
        'mode':'stable', 'x_px':540, 'y_px':650, 'end_scale':1, 'rise_px':0, 'tail_frames':0}
    with pytest.raises(ContractError, match='separate bound packaging passes'):
        create(case)


def test_later_pulse_moves_existing_letter_and_exit_is_staggered(case):
    plan=setup_variety(case); cue=plan['captions'][0]
    recipe=read_json_object(stickers.RECIPE,'recipe')
    settled=stickers.letter_state(cue,0,58,24,recipe)
    response=stickers.letter_state(cue,0,61,24,recipe)
    assert response['scale'] > settled['scale']
    assert response['y_em'] < settled['y_em']
    assert stickers.letter_state(cue,2,84,24,recipe)['opacity'] > stickers.letter_state(cue,0,84,24,recipe)['opacity']


@pytest.mark.parametrize('strengths', [[1,0,1],[1,1.5,1],[True,1,1],[1,float('nan'),1],[1,1],None])
def test_invalid_voice_strength_candidates_are_rejected(case,strengths):
    setup_variety(case)
    case['events']['events'][0]['syllable_strengths']=strengths
    with pytest.raises(ContractError,match='syllable_strengths'):
        create(case)


def test_timing_candidate_cannot_impersonate_human_verification(case):
    setup_variety(case)
    case['events']['review_status']='timing_candidate'
    with pytest.raises(ContractError,match='acknowledgement'):
        create(case)
    case['events']['timing_candidate_ack']={
        'event_confirmation':'User identified laughter in this source tail.',
        'timing_method':'Energy peaks only; not direct listening or emotion detection.',
        'requires_human_audio_review':True}
    plan=create(case)
    assert plan['review_status']=='timing_candidate'
    assert plan['human_review']=='pending' and plan['candidate_only'] is True
