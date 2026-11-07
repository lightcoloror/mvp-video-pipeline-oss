import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from segment_level_fixture import make_review, pcm
from video_creation_pipeline.contract_io import artifact_reference as ref, atomic_write_json as write, payload_sha256
from video_creation_pipeline.segment_leveling import inspect_segments, load_report, validate_leveling
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.spoken_rough_cut_cli import main


def test_quiet_opening_inside_one_long_segment_is_not_hidden_by_its_average(tmp_path):
    import math
    media=tmp_path/'synthetic-source';media.write_bytes(b'Synthetic media identity only')
    audio=tmp_path/'opening.wav';rate=8000
    samples=[round(32767*(.025 if i < 3*rate else .15)*math.sin(2*math.pi*400*i/rate)) for i in range(12*rate)]
    pcm(audio,samples,rate)
    spec=dict(phase='before',audio_content='dialogue_only',source_media=ref(media,'source'),measured_media=ref(media,'measured'),
        segments=[dict(start=0,end=12)],speech_windows=[dict(start=0,end=12)])
    before=inspect_segments(audio,spec)
    after=inspect_segments(audio,{**spec,'phase':'after'})
    assert before['level_spread_db']==0  # the old whole-segment check cannot detect it
    assert before['local_level_findings'][0]['kind']=='quiet_opening_window'
    review=dict(before_ref=ref(write(before,tmp_path/'before.json'),'before'),after_ref=ref(write(after,tmp_path/'after.json'),'after'),
        clip_gains=[{**row,'audio_checked':True,'expression_preserved':True,'evidence':'synthetic only'} for row in before['candidate_clip_gains']],
        joins_checked=True,expression_preserved=True,evidence='Synthetic test, not actual listening')
    with pytest.raises(ContractError,match='quiet opening/local'):
        validate_leveling(review,require_after=True)
    review['intentional_level_difference']=dict(segments=before['segments'],audio_checked=True,
        expression_preserved=True,evidence='Synthetic whole-segment exception cannot approve a hidden quiet opening')
    with pytest.raises(ContractError,match='quiet opening/local'):
        validate_leveling(review,require_after=True)
    review['local_window_resolutions']=[dict(finding_id=f['id'],decision='keep_intentional',
        after_report_sha256=after['report_sha256'],measured_media_sha256=after['measured_media']['sha256'],
        audio_checked=True,expression_preserved=True,evidence='Synthetic intentional whisper decision, not a real approval')
        for f in after['local_level_findings']]
    assert validate_leveling(review,require_after=True)['local_findings_resolved']==len(after['local_level_findings'])
    review['local_window_resolutions'][0]['measured_media_sha256']='f'*64
    with pytest.raises(ContractError,match='current specific'):
        validate_leveling(review,require_after=True)


def test_old_bound_pcm_report_is_upgraded_without_rewriting_or_forging_listening(case):
    *_,review=case
    current=load_report(review['before_ref'])
    legacy=copy.deepcopy(current)
    legacy.pop('local_window_measurements');legacy.pop('local_level_findings')
    for key in ('local_window_s','local_spread_warning_db'):legacy['policy'].pop(key)
    legacy['report_sha256']=payload_sha256(legacy,'report_sha256')
    path=Path(review['before_ref']['path']).with_name('legacy-before.json')
    write(legacy,path)
    original=path.read_bytes()
    upgraded=load_report(ref(path,'legacy'))
    assert 'local_window_measurements' in upgraded
    assert path.read_bytes()==original
    assert 'local_window_resolutions' not in upgraded


@pytest.fixture
def case(tmp_path):
    media = tmp_path/'source.mp4'; media.write_bytes(b'synthetic source')
    edited = tmp_path/'edited.mp4'; edited.write_bytes(b'synthetic edited binding')
    keeps = [dict(start=0,end=2),dict(start=3,end=4)]
    windows = [dict(start=.1,end=.5),dict(start=1.5,end=1.9),dict(start=3.1,end=3.9)]
    review = make_review(tmp_path/'audio',media,keeps,windows,amplitudes=[.04,.12],after_media=edited)
    return media,edited,keeps,windows,review


def test_real_pcm_segment_alignment_with_natural_band_and_post_measurement(case):
    media,edited,keeps,windows,review = case
    result = validate_leveling(review,source=ref(media,'source'),keeps=keeps,
        speech_windows=windows,gains=review['clip_gains'],require_after=True,measured_media=ref(edited,'edited'))
    assert result['rough_cut_audio_accepted'] is True
    assert result['before_spread_db'] > 9
    assert 1.8 < result['after_spread_db'] < 2.2
    assert load_report(review['before_ref'])['segment_measurements'][0]['speech_duration_s'] == pytest.approx(.8)


def test_whole_track_only_checkbox_and_empty_gain_table_are_rejected(case):
    *_,review = case
    for value in (None, {}, {'levels_checked':True,'integrated_lufs':-16}):
        with pytest.raises(ContractError,match='whole-track'): validate_leveling(value)
    bad = {**review,'clip_gains':[]}
    with pytest.raises(ContractError,match='explicit per-segment'): validate_leveling(bad)


def test_zero_gains_for_already_consistent_speech_are_explicit_and_valid(tmp_path):
    media=tmp_path/'source'; media.write_bytes(b'source')
    keeps=[dict(start=0,end=1),dict(start=1,end=2)]
    review=make_review(tmp_path,media,keeps,keeps,after_media=media)
    assert all(g['gain_db']==0 for g in review['clip_gains'])
    assert validate_leveling(review,require_after=True)['rough_cut_audio_accepted']


def test_missing_post_measurement_is_pending_never_accepted(case):
    *_,review=case
    review={**review,'after_ref':None}
    assert not validate_leveling(review)['rough_cut_audio_accepted']
    with pytest.raises(ContractError,match='remeasurement'): validate_leveling(review,require_after=True)


def test_stale_cut_source_window_gain_or_output_bindings_block(case):
    media,edited,keeps,windows,review=case
    badg=copy.deepcopy(review['clip_gains']); badg[0]['gain_db']+=1
    for kwargs in (dict(keeps=[dict(start=0,end=1),keeps[1]]), dict(gains=badg),
                   dict(speech_windows=[windows[0],windows[-1]]),
                   dict(measured_media=ref(media,'wrong')),dict(output_segments=keeps)):
        with pytest.raises(ContractError): validate_leveling(review,require_after=True,**kwargs)
    media.write_bytes(b'changed')
    with pytest.raises(ContractError,match='changed'): validate_leveling(review)


def test_measurement_cannot_omit_a_segment_or_use_bgm_mix(case,tmp_path):
    *_,review=case
    report=load_report(review['before_ref']); audio=report['source_audio']['path']
    with pytest.raises(ContractError,match='missing segment'): inspect_segments(audio,{**report,'speech_windows':report['speech_windows'][:2]})
    with pytest.raises(ContractError,match='dialogue_only'): inspect_segments(audio,{**report,'audio_content':'mixed_with_bgm'})


def test_summary_forgery_and_changed_pcm_are_rejected(case):
    *_,review=case
    path=Path(review['before_ref']['path']); report=json.loads(path.read_text())
    report['level_spread_db']=0
    report['report_sha256']=payload_sha256(report,'report_sha256')
    write(report,path)
    with pytest.raises(ContractError,match='remeasure'): load_report(ref(path,'forged'))


def test_missing_gain_application_fails_even_if_integrated_level_was_normalized(case):
    *_,review=case
    before=load_report(review['before_ref']); after=load_report(review['after_ref'])
    import wave,array
    with wave.open(before['source_audio']['path'],'rb') as w:
        data=array.array('h',w.readframes(w.getnframes()))
    # Concatenate the original unequal voice, simulating an editor dropping gains.
    pcm(after['source_audio']['path'],list(data[:16000])+list(data[24000:32000]))
    revised=inspect_segments(after['source_audio']['path'],after)
    review['after_ref']=ref(write(revised,review['after_ref']['path']),'after')
    with pytest.raises(ContractError,match='gains differ'): validate_leveling(review,require_after=True)


def test_clipping_and_silent_speech_are_not_repaired_by_loudness_labels(case):
    *_,review=case
    report=load_report(review['before_ref']); audio=report['source_audio']['path']
    pcm(audio,[0]*32000)
    with pytest.raises(ContractError,match='silent speech'): inspect_segments(audio,report)
    pcm(audio,[32767]*32000)
    clipped=inspect_segments(audio,report)
    review['before_ref']=ref(write(clipped,review['before_ref']['path']),'clipped')
    with pytest.raises(ContractError,match='clipping'): validate_leveling(review)


def test_cli_generates_unapproved_template_and_acceptance_requires_current_media(case,tmp_path):
    media,edited,keeps,windows,review=case
    template=tmp_path/'template.json'
    assert main(['segment-leveling-template','--report',review['before_ref']['path'],'--output',str(template)])==0
    draft=json.loads(template.read_text())
    assert draft['expression_preserved'] is False and draft['after_ref'] is None
    assert all(g['audio_checked'] is False for g in draft['clip_gains'])
    rp=write(review,tmp_path/'review.json')
    assert main(['check-segment-leveling','--review',str(rp),'--measured-media',str(edited),'--output',str(tmp_path/'accepted.json')])==0
    assert main(['check-segment-leveling','--review',str(rp),'--measured-media',str(media),'--output',str(tmp_path/'bad.json')])==2
    assert not (tmp_path/'bad.json').exists()


def test_intentional_expression_exception_is_bound_and_cannot_skip_post_check(case):
    *_,review=case
    before=load_report(review['before_ref']); after=load_report(review['after_ref'])
    import wave,array
    with wave.open(before['source_audio']['path'],'rb') as w:
        data=array.array('h',w.readframes(w.getnframes()))
    pcm(after['source_audio']['path'],list(data[:16000])+list(data[24000:32000]))
    review['after_ref']=ref(write(inspect_segments(after['source_audio']['path'],after),review['after_ref']['path']),'after')
    for g in review['clip_gains']: g['gain_db']=0
    with pytest.raises(ContractError,match='remain uneven'): validate_leveling(review,require_after=True)
    review['intentional_level_difference']=dict(segments=before['segments'],audio_checked=True,
        expression_preserved=True,evidence='synthetic whisper-to-emphasis narrative fixture')
    assert validate_leveling(review,require_after=True)['status']=='accepted_with_intentional_expression'
    review['after_ref']=None
    with pytest.raises(ContractError,match='remeasurement'): validate_leveling(review,require_after=True)


def test_speech_with_non_speech_insert_keeps_actual_output_positions(case):
    *_,review=case
    report=load_report(review['before_ref'])
    spec={**report,'output_segments':[dict(start=0,end=2),dict(start=5,end=6)]}
    before=inspect_segments(report['source_audio']['path'],spec)
    assert before['output_segments'][1]['start']==5
    assert before['level_spread_db']==report['level_spread_db']


def test_existing_ffmpeg_gain_renderer_really_aligns_output(case,tmp_path):
    from video_creation_pipeline.spoken_delivery import _render_command
    from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
    from video_creation_pipeline.review_resource_guard import ResourcePressurePauseRequired
    _,_,keeps,windows,review=case
    before=load_report(review['before_ref'])
    source=tmp_path/'ffmpeg-source.mp4'; output=tmp_path/'ffmpeg-output.mp4'; decoded=tmp_path/'decoded.wav'
    def run(name,command,inputs,outputs):
        try:
            execute_ffmpeg_with_receipt('render',command,
                [dict(role='synthetic_input',path=p) for p in inputs],
                [dict(role='synthetic_output',path=p) for p in outputs],
                tmp_path/(name+'-receipt.json'),timeout_s=40,guard_resource=True)
        except ResourcePressurePauseRequired as e:
            pytest.skip('existing resource guard retained: '+str(e))
    run('source',['ffmpeg','-v','error','-n','-f','lavfi','-i','color=blue:s=96x128:r=10',
        '-i',before['source_audio']['path'],'-t','4','-c:v','libx264','-preset','ultrafast','-threads','1',
        '-c:a','aac',str(source)], [before['source_audio']['path']], [source])
    spec={**before,'source_media':ref(source,'source'),'measured_media':ref(source,'source')}
    report=inspect_segments(before['source_audio']['path'],spec)
    review['before_ref']=ref(write(report,tmp_path/'render-before.json'),'before')
    plan=dict(source_video=ref(source,'source'),nle_segments=[dict(in_s=k['start'],out_s=k['end']) for k in keeps],clip_gains=review['clip_gains'])
    run('cut',_render_command(plan,output,True),[source],[output])
    run('decode',['ffmpeg','-v','error','-n','-i',str(output),'-vn','-c:a','pcm_s16le','-ar','8000',str(decoded)],[output],[decoded])
    after=inspect_segments(decoded,{**spec,'phase':'after','measured_media':ref(output,'edited')})
    review['after_ref']=ref(write(after,tmp_path/'render-after.json'),'after')
    result=validate_leveling(review,require_after=True,measured_media=ref(output,'edited'))
    assert result['before_spread_db']>9 and result['after_spread_db']<3
