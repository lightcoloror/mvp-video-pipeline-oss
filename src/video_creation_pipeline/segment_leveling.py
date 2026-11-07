"""Speech-window level balancing on the existing PCM inspector and gain renderer.

No media executor or ASR is introduced here. Source-clock windows come from the
reviewed transcript/VAD. After measurements use the same windows on the cut clock.
"""
from __future__ import annotations

import math
import statistics
import copy

from .audio_edit_quality import inspect_pcm_levels, validate_clip_gains
from .contract_io import (artifact_reference, payload_sha256, read_json_object,
                          validate_artifact_reference)
from .editing_presets import POLICY, interval, reviewed, number
from .errors import ContractError

SCHEMA = "video_creation_pipeline.segment_speech_levels.v1"


def _bound(reference, label):
    if not isinstance(reference, dict):
        raise ContractError(f"{label} requires a bound artifact reference")
    return validate_artifact_reference(reference, label)


def _ranges(rows, label):
    if not isinstance(rows, list) or not rows:
        raise ContractError(f"{label} must contain explicit intervals")
    values = [dict(zip(("start", "end"), interval(r))) for r in rows]
    if any(a["end"] > b["start"] + 1e-7 for a, b in zip(values, values[1:])):
        raise ContractError(f"{label} must be ordered without overlaps")
    return values


def speech_groups(segments, speech_windows):
    """Intersect and merge words so overlapping ASR windows cannot double count."""
    keeps = _ranges(segments, "segments")
    if not isinstance(speech_windows, list) or not speech_windows:
        raise ContractError("actual speech windows required; whole-track loudness is insufficient")
    windows = sorted((interval(r) for r in speech_windows), key=lambda r: r[0])
    merged = []
    for a, b in windows:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    groups = []
    for keep in keeps:
        group = [dict(start=max(a, keep["start"]), end=min(b, keep["end"]))
                 for a, b in merged if min(b, keep["end"]) > max(a, keep["start"]) + 1e-7]
        if not group:
            raise ContractError("every spoken segment needs speech evidence; missing segment window")
        groups.append(group)
    return keeps, groups


def inspect_segments(audio, spec):
    """Measure a dialogue-only PCM. 'after' is concatenated, constant-speed audio."""
    if spec.get("audio_content") != "dialogue_only":
        raise ContractError("segment leveling requires dialogue_only audio, before BGM mixing")
    phase = spec.get("phase")
    if phase not in ("before", "after"):
        raise ContractError("segment level phase must be before or after")
    analysis_clock = spec.get("analysis_clock", "source_cuts")
    if analysis_clock not in ("source_cuts", "edited_timeline"):
        raise ContractError("unknown segment audio analysis clock")
    media = _bound(spec.get("source_media"), "leveling source media")
    subject = _bound(spec.get("measured_media"), "measured media")
    keeps, groups = speech_groups(spec.get("segments"), spec.get("speech_windows"))
    output_segments, cursor = [], 0.0
    for keep in keeps:
        end = cursor + keep["end"] - keep["start"]
        output_segments.append(dict(start=round(cursor, 9), end=round(end, 9)))
        cursor = end
    if spec.get("output_segments") is not None:
        output_segments = _ranges(spec["output_segments"], "output segments")
    if len(output_segments) != len(keeps) or any(
            abs((b['end']-b['start'])-(a['end']-a['start'])) > 1e-7
            for a,b in zip(keeps,output_segments)):
        raise ContractError("output segments must preserve each measured speech duration")
    windows, counts = [], []
    for keep, target, group in zip(keeps, output_segments, groups, strict=True):
        shift = 0 if phase == "before" else target["start"] - keep["start"]
        windows.extend(dict(start=round(w["start"] + shift, 9), end=round(w["end"] + shift, 9)) for w in group)
        counts.append(len(group))
    raw = inspect_pcm_levels(audio, windows)
    segments, offset = [], 0
    for keep, count in zip(keeps, counts, strict=True):
        rows = raw["windows"][offset:offset + count]
        offset += count
        if any(r["rms_dbfs"] is None for r in rows):
            raise ContractError("silent speech window: correct the transcript/VAD or restore missing speech")
        duration = sum(r["end"] - r["start"] for r in rows)
        power = sum((r["end"] - r["start"]) * 10 ** (r["rms_dbfs"] / 10) for r in rows) / duration
        segments.append(dict(**keep, speech_duration_s=duration, rms_dbfs=10 * math.log10(power),
                             sample_peak_dbfs=max(r["sample_peak_dbfs"] for r in rows),
                             full_scale_samples=sum(r["full_scale_samples"] for r in rows)))
    levels = [r["rms_dbfs"] for r in segments]
    policy = dict(POLICY["loudness"]["segment_leveling"])
    median = statistics.median(levels)
    gains = []
    for row in segments:
        delta = median - row["rms_dbfs"]
        gain = math.copysign(max(0, abs(delta) - policy["natural_band_db"]), delta)
        gain = max(-policy["max_gain_db"], min(policy["max_gain_db"], gain))
        gains.append(dict(start=row["start"], end=row["end"], gain_db=round(gain, 6),
                          audio_checked=False, expression_preserved=False, evidence=""))
    result = dict(schema=SCHEMA, phase=phase, analysis_clock=analysis_clock, audio_content="dialogue_only",
                  source_media=artifact_reference(media, "source"),
                  measured_media=artifact_reference(subject, "measured media"),
                  source_audio=raw["source_audio"], metric=raw["metric"], policy=policy,
                  segments=keeps, output_segments=output_segments,
                  speech_windows=[w for group in groups for w in group],
                  segment_measurements=segments, window_measurements=raw["windows"],
                  level_spread_db=max(levels) - min(levels),
                  max_adjacent_difference_db=max((abs(a-b) for a,b in zip(levels,levels[1:])), default=0),
                  channel_review_required=raw["channel_review_required"], candidate_clip_gains=gains,
                  automatic_gain_applied=False, final_mix_check_still_required=True)
    # Reuse the same PCM inspector at a smaller grain: a long segment's average
    # can hide its quiet opening. These are measurement windows, never edit cuts
    # or claims of automatic sentence recognition.
    local_windows = []
    local_ids = []
    for segment_index, (keep, target, group) in enumerate(zip(keeps, output_segments, groups, strict=True)):
        shift = 0 if phase == 'before' else target['start']-keep['start']
        local_index = 0
        for window in group:
            count = max(1, math.ceil((window['end']-window['start'])/policy['local_window_s']))
            size = (window['end']-window['start'])/count
            for index in range(count):
                start = round(window['start']+index*size+shift,9)
                end = round(window['start']+(index+1)*size+shift,9)
                local_windows.append(dict(start=start,end=end))
                local_ids.append(dict(id=f'segment-{segment_index:04}-window-{local_index:04}',segment_index=segment_index,
                    opening_speech_window=segment_index==0 and local_index==0))
                local_index += 1
    detail = inspect_pcm_levels(audio,local_windows)['windows']
    if any(row['rms_dbfs'] is None for row in detail):
        raise ContractError('silent local speech window; check missing or truncated speech')
    local_rows = [{**identity,**row} for identity,row in zip(local_ids,detail,strict=True)]
    stable = [row['rms_dbfs'] for row in local_rows if row['end']-row['start'] >= policy['local_window_s']/2]
    baseline = statistics.median_high(sorted(stable or levels))
    result['local_window_measurements'] = local_rows
    result['local_level_findings'] = [dict(id=row['id'],start=row['start'],end=row['end'],
        kind='quiet_opening_window' if row['opening_speech_window'] else 'quiet_local_speech_window',
        below_reference_db=round(baseline-row['rms_dbfs'],6))
        for row in local_rows if (row['opening_speech_window'] or row['end']-row['start'] >= policy['local_window_s']/2)
        and baseline-row['rms_dbfs'] > policy['local_spread_warning_db']]
    result["report_sha256"] = payload_sha256(result, "report_sha256")
    return result


def load_report(reference):
    path = _bound(reference, "segment levels report")
    report = read_json_object(path, "segment levels report")
    if report.get("schema") != SCHEMA or report.get("report_sha256") != payload_sha256(report, "report_sha256"):
        raise ContractError("segment levels report invalid; integrated LUFS cannot replace speech segments")
    # Recompute from the bound WAV, rather than trusting hand-entered summary numbers.
    audio = _bound(report.get("source_audio"), "measured dialogue PCM")
    current = inspect_segments(audio, report)
    if current != report:
        # Upgrade old measurement evidence by re-reading its original bound PCM;
        # do not rewrite it, discard approvals or invent local listening results.
        legacy = copy.deepcopy(current)
        legacy.pop('local_window_measurements',None)
        legacy.pop('local_level_findings',None)
        for key in ('local_window_s','local_spread_warning_db'):
            legacy['policy'].pop(key,None)
        legacy['report_sha256'] = payload_sha256(legacy,'report_sha256')
        if legacy == report:
            return current
        raise ContractError("segment level evidence or policy changed; remeasure")
    return report


def validate_leveling(review, *, keeps=None, source=None, gains=None, require_after=False,
                      measured_media=None, output_segments=None, speech_windows=None):
    if not isinstance(review, dict) or not review.get("before_ref"):
        raise ContractError("segment speech level evidence required; whole-track LUFS/checkbox is insufficient")
    before = load_report(review["before_ref"])
    if before["phase"] != "before":
        raise ContractError("before speech measurement required")
    if source is not None and before["source_media"] != source:
        raise ContractError("segment leveling source changed")
    if keeps is not None and before["segments"] != _ranges(keeps, "kept intervals"):
        raise ContractError("segment leveling is bound to a different cut")
    if output_segments is not None and before["output_segments"] != _ranges(output_segments, "output segments"):
        raise ContractError("segment leveling output timeline changed")
    if speech_windows is not None:
        _, groups = speech_groups(before['segments'], speech_windows)
        if before['speech_windows'] != [w for group in groups for w in group]:
            raise ContractError("segment speech coverage changed or incomplete")
    applied = validate_clip_gains(review.get("clip_gains"), before["segments"])
    if not applied:
        raise ContractError("explicit per-segment gains required, including zero when no adjustment is needed")
    if gains is not None and applied != gains:
        raise ContractError("reviewed segment gains must reach the edit plan unchanged")
    reviewed(review, ["expression_preserved"], "segment leveling")
    policy = before["policy"]
    if any(abs(number(r["gain_db"])) > policy["max_gain_db"] for r in applied):
        raise ContractError("segment correction exceeds bounded gain; inspect source instead of over-amplifying")
    if before["channel_review_required"] or any(r["full_scale_samples"] for r in before["segment_measurements"]):
        raise ContractError("clipping/channel issue requires source repair and new segment measurements")
    if any(r['sample_peak_dbfs'] + number(g['gain_db']) >= 0
           for r,g in zip(before['segment_measurements'],applied,strict=True)):
        raise ContractError("planned speech gain would clip; lower the target or repair dynamics")
    predicted = [r["rms_dbfs"] + number(g["gain_db"]) for r,g in zip(before["segment_measurements"],applied,strict=True)]
    exception = review.get("intentional_level_difference")
    if exception is not None:
        reviewed(exception, ["audio_checked", "expression_preserved"], "intentional level difference")
        if exception.get("segments") != before["segments"]:
            raise ContractError("intentional level exception must bind the current segments")
    if max(predicted)-min(predicted) > policy["max_spread_db"] and exception is None:
        raise ContractError("planned segment levels remain uneven; adjust gains or review intentional expression")
    if not review.get("after_ref"):
        if require_after:
            raise ContractError("post-application segment remeasurement required before rough-cut acceptance")
        return dict(status="planned_pending_application_and_remeasurement", rough_cut_audio_accepted=False)
    after = load_report(review["after_ref"])
    if after["phase"] != "after" or any(after[k] != before[k] for k in ("analysis_clock", "source_media", "segments", "output_segments", "speech_windows")):
        raise ContractError("after measurement must use the same source, cuts and speech windows")
    if measured_media is not None and after["measured_media"] != measured_media:
        raise ContractError("segment remeasurement belongs to a different edited media version")
    if after["channel_review_required"] or any(r["full_scale_samples"] for r in after["segment_measurements"]):
        raise ContractError("post-application clipping/channel check failed")
    actual = [r["rms_dbfs"] for r in after["segment_measurements"]]
    residual = [a-p for a,p in zip(actual,predicted,strict=True)]
    common_shift = statistics.median(residual)
    if max(abs(r-common_shift) for r in residual) > policy["application_tolerance_db"]:
        raise ContractError("rendered segment gains differ from plan; check missing/double applied gains")
    if (after["level_spread_db"] > policy["max_spread_db"] or
            after["max_adjacent_difference_db"] > policy["max_adjacent_difference_db"]) and exception is None:
        raise ContractError("post-application speech levels remain uneven")
    reviewed(review, ["joins_checked", "expression_preserved"], "post-application segment listening")
    findings = after['local_level_findings']
    resolutions = review.get('local_window_resolutions',[])
    if not isinstance(resolutions,list):
        raise ContractError('local speech window resolutions must be an explicit list')
    indexed = {row.get('finding_id'):row for row in resolutions if isinstance(row,dict)}
    if len(indexed) != len(resolutions) or any(key not in {f['id'] for f in findings} for key in indexed):
        raise ContractError('local speech window resolution has unknown/duplicate findings')
    for finding in findings:
        measured = next(row for row in after['local_window_measurements'] if row['id']==finding['id'])
        segment_level = after['segment_measurements'][measured['segment_index']]['rms_dbfs']
        if exception is not None and abs(measured['rms_dbfs']-segment_level) <= policy['max_spread_db']:
            # An already reviewed uniformly quiet segment remains accepted. A
            # quiet phrase hidden inside an otherwise loud paragraph does not.
            continue
        row = indexed.get(finding['id'])
        if (row is None or row.get('decision') != 'keep_intentional'
                or row.get('after_report_sha256') != after['report_sha256']
                or row.get('measured_media_sha256') != after['measured_media']['sha256']):
            raise ContractError('quiet opening/local speech window requires current specific listening or repair and remeasurement')
        reviewed(row,['audio_checked','expression_preserved'],'local speech window listening')
    return dict(status="accepted_with_intentional_expression" if exception else "accepted",
                rough_cut_audio_accepted=True, before_spread_db=before["level_spread_db"],
                after_spread_db=after["level_spread_db"], local_windows_checked=len(after['local_window_measurements']),
                local_findings_resolved=len(findings), whole_track_loudness_is_not_a_substitute=True)
