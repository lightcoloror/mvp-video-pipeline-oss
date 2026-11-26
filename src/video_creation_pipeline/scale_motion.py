"""Rhythm-aware pose transitions, adapted to FFmpeg's native frame expressions.

Uses the existing object-motion.ts cubic smoothstep. This module plans geometry;
it never changes the audio, subtitle clock, or source edit boundaries.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics

from .editing_presets import POLICY
from .errors import ContractError


def policy():
    return dict(POLICY['transitions']['scale_motion'])


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError('scale motion requires finite numbers')
    return float(value)


def _rect(value, aspect):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ContractError('scale motion rect must be [x, y, width, height]')
    rect = list(map(_number, value))
    if min(rect[2:]) <= 0 or abs(rect[2] / rect[3] / aspect - 1) > .015:
        raise ContractError('scale motion must preserve the prepared layer aspect ratio')
    return rect


def transition_duration(onsets, at, before, after, fps):
    """Native speech/action onset cadence is a proxy, not a detected music beat."""
    cfg = policy()
    nearby = sorted(set(t for t in onsets if abs(t - at) <= cfg['cadence_window_s']))
    gaps = [b-a for a, b in zip(nearby, nearby[1:]) if .08 <= b-a <= .8]
    cadence = statistics.median(gaps) if len(gaps) >= 3 else None
    base = cadence * cfg['onsets_per_transition'] if cadence is not None else cfg['fallback_duration_s']
    # Large changes need longer than small nudges; interpolation is in log scale.
    distance = abs(math.log(after[2] / before[2]))
    duration = max(base, distance * cfg['seconds_per_log_scale'])
    duration = max(cfg['min_duration_s'], min(cfg['max_duration_s'], duration))
    return math.ceil(duration * fps) / fps, {
        'kind': 'local_onset_cadence_proxy' if cadence is not None else 'explicit_policy_fallback',
        'median_onset_interval_s': cadence, 'sample_count': len(gaps),
        'scale_ratio': after[2] / before[2], 'manual_audition_required': True,
    }


def build_scale_motion(spec, width, height, duration_s, fps):
    """Resolve optional layer targets on a scene-local clock into continuous events.

    A target may anticipate its start (e.g. clear space for a demonstration).
    Adjacent targets retarget directly; short gaps at the same pose are bridged.
    Cuts are allowed only with an explicit reason, never inferred from scene cuts.
    """
    cfg = policy()
    spec = {} if spec is None else spec
    if not isinstance(spec, dict) or set(spec) - {'base_rect', 'targets', 'onsets_s'}:
        raise ContractError('unsupported scale motion settings')
    if fps <= 0 or duration_s <= 0:
        raise ContractError('invalid scale motion clock')
    base = _rect(spec.get('base_rect', [0, 0, width, height]), width / height)
    onsets = [_number(x) for x in spec.get('onsets_s', [])]
    if any(t < 0 or t > duration_s for t in onsets):
        raise ContractError('scale motion onset outside scene')
    groups = []
    for raw in spec.get('targets', []):
        if not isinstance(raw, dict) or set(raw) - {'start_s', 'end_s', 'rect', 'anticipate', 'mode', 'reason'}:
            raise ContractError('unsupported scale target')
        a, b = _number(raw['start_s']), _number(raw['end_s'])
        rect = _rect(raw['rect'], width / height)
        mode, reason = raw.get('mode', 'smooth'), str(raw.get('reason', '')).strip()
        if mode not in ('smooth', 'cut') or mode == 'cut' and not reason:
            raise ContractError('instant scale changes require an explicit editorial reason')
        if not 0 <= a < b <= duration_s or groups and a < groups[-1]['end_s'] - 1e-7:
            raise ContractError('scale targets overlap, are unordered or leave the scene clock')
        row = {'start_s': a, 'end_s': b, 'rect': rect, 'mode': mode,
               'reason': reason, 'anticipate': bool(raw.get('anticipate', False))}
        if groups and rect == groups[-1]['rect'] and mode == groups[-1]['mode'] == 'smooth' and a-groups[-1]['end_s'] <= cfg['same_pose_bridge_s']:
            groups[-1]['end_s'] = b
        else:
            groups.append(row)
    events, pose, free_at = [], base, 0.0

    def add(a, b, dest, basis, reason, mode='smooth'):
        nonlocal pose, free_at
        if dest != pose:
            events.append({'start_s': a, 'end_s': b, 'from_rect': pose, 'to_rect': dest,
                           'mode': mode, 'reason': reason, 'rhythm': basis})
        pose, free_at = dest, b

    for i, group in enumerate(groups):
        d, basis = transition_duration(onsets, group['start_s'], pose, group['rect'], fps)
        if group['mode'] == 'cut':
            d = 0
        a = max(free_at, group['start_s'] - d) if group['anticipate'] else max(free_at, group['start_s'])
        b = min(a + d, group['end_s'])
        if d and b-a < cfg['min_duration_s']:
            raise ContractError('scale target too short for a smooth transition; lengthen or review an explicit cut')
        add(a, b, group['rect'], basis, group['reason'], group['mode'])
        group['settled_s'] = b
        free_at = group['end_s']
        following = groups[i+1] if i+1 < len(groups) else None
        if following and following['start_s']-group['end_s'] <= cfg['same_pose_bridge_s']:
            continue
        d, basis = transition_duration(onsets, group['end_s'], pose, base, fps)
        available = (following['start_s'] if following else duration_s)-group['end_s']
        # Reserve time for the next entrance when its layout needs anticipation.
        if following and following['anticipate']:
            available /= 2
        b = min(group['end_s'] + d, group['end_s'] + available)
        if pose != base and b-group['end_s'] < cfg['min_duration_s']:
            raise ContractError('no room to restore scale smoothly before scene boundary')
        add(group['end_s'], b, base, basis, 'return to narration layout')
    return {'schema': 'video_creation_pipeline.scale_motion.v1', 'policy': cfg,
            'policy_sha256': hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest(),
            'base_rect': base, 'events': events, 'targets': groups,
            'duration_s': duration_s, 'fps': fps,
            'implementation_sha256': hashlib.sha256(__import__('pathlib').Path(__file__).read_bytes()).hexdigest()}


def sample_pose(plan, time_s):
    pose = plan['base_rect']
    for event in plan['events']:
        if time_s < event['start_s']:
            break
        if event['mode'] == 'cut' or time_s >= event['end_s']:
            pose = event['to_rect']
            continue
        u = (time_s-event['start_s'])/(event['end_s']-event['start_s'])
        u = u*u*(3-2*u)  # scene_components/object-motion.ts: smooth()
        before, after = event['from_rect'], event['to_rect']
        scale = math.exp(math.log(after[2]/before[2])*u)
        return [before[0]+(after[0]-before[0])*u, before[1]+(after[1]-before[1])*u,
                before[2]*scale, before[3]*scale]
    return list(pose)


def ffmpeg_pose_expressions(plan):
    """Single presenter/layer, variable scale dimensions; no duplicate-face dissolve."""
    values = [f'{x:.9f}' for x in plan['base_rect'][:3]]
    for event in plan['events']:
        a, b = event['start_s'], event['end_s']
        before, after = event['from_rect'], event['to_rect']
        if event['mode'] == 'cut':
            blend = [f'{x:.9f}' for x in after[:3]]
        else:
            u = f'clip((t-{a:.9f})/{b-a:.9f},0,1)'
            smooth = f'(({u})*({u})*(3-2*({u})))'
            blend = [f'({before[k]:.9f}+({after[k]-before[k]:.9f})*{smooth})' for k in (0, 1)]
            blend.append(f'({before[2]:.9f}*exp({math.log(after[2]/before[2]):.12f}*{smooth}))')
        values = [f'if(gte(t,{a:.9f}),{expr},{old})' for expr, old in zip(blend, values)]
    return dict(zip(('x', 'y', 'width'), values))


def ffmpeg_scale_overlay(plan, layer, background, output, *, prefix='motion'):
    e = ffmpeg_pose_expressions(plan)
    return [f"[{layer}]scale=w='max(2,2*round(({e['width']})/2))':h=-2:eval=frame:flags=bilinear,setsar=1[{prefix}Scaled]",
            f"[{background}][{prefix}Scaled]overlay=x='{e['x']}':y='{e['y']}':eval=frame:eof_action=pass[{output}]"]
