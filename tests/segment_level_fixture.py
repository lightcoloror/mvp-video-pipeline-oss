"""Tiny PCM fixtures. Review flags are synthetic, never real human approval."""
import array
import math
import wave
from pathlib import Path
from video_creation_pipeline.contract_io import artifact_reference as ref, atomic_write_json as write
from video_creation_pipeline.segment_leveling import inspect_segments


def pcm(path, samples, rate=8000):
    with wave.open(str(path), 'wb') as w:
        w.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        w.writeframes(array.array('h', samples).tobytes())


def make_review(root, media, keeps, windows, *, amplitudes=None, after_media=None, analysis_clock='source_cuts'):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rate = 8000
    amplitudes = amplitudes or [0.1] * len(keeps)
    data = [0] * round(max(k['end'] for k in keeps) * rate)
    for keep, amp in zip(keeps, amplitudes, strict=True):
        for i in range(round(keep['start']*rate), round(keep['end']*rate)):
            data[i] = round(32767*amp*math.sin(2*math.pi*400*i/rate))
    audio = root/'before.wav'
    pcm(audio, data)
    spec = dict(phase='before', analysis_clock=analysis_clock, audio_content='dialogue_only', source_media=ref(media,'source'),
                measured_media=ref(media,'source'), segments=keeps, speech_windows=windows)
    before = inspect_segments(audio, spec)
    before_path = write(before, root/'before.json')
    gains = [{**g, 'audio_checked':True, 'expression_preserved':True, 'evidence':'synthetic PCM fixture'}
             for g in before['candidate_clip_gains']]
    review = dict(before_ref=ref(before_path,'before'), after_ref=None, clip_gains=gains,
                  joins_checked=True, expression_preserved=True, evidence='synthetic fixture only')
    if after_media is not None:
        output = []
        for keep,g in zip(keeps,gains,strict=True):
            output += [round(x*10**(g['gain_db']/20)) for x in data[round(keep['start']*rate):round(keep['end']*rate)]]
        after_audio = root/'after.wav'
        pcm(after_audio, output)
        after = inspect_segments(after_audio, {**spec,'phase':'after','measured_media':ref(after_media,'edited')})
        review['after_ref'] = ref(write(after,root/'after.json'),'after')
    return review
