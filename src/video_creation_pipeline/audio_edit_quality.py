"""Small PCM diagnostics and delivery choices; no second media executor."""

from __future__ import annotations

import math
import statistics
import wave
from .contract_io import artifact_reference

from .editing_presets import POLICY, number, interval, reviewed
from .errors import ContractError

LOUDNESS_PROFILES = {
    "web_spoken_candidate": -16.0,  # local starting choice, not a platform mandate
    "ebu_r128": -23.0,
}


def resolve_loudness(profile, target):
    if profile in LOUDNESS_PROFILES:
        expected = LOUDNESS_PROFILES[profile]
        if target is not None and number(target) != expected:
            raise ContractError(
                "named loudness profile conflicts with target; use custom"
            )
        return profile, expected
    if profile not in (None, "custom") or target is None:
        raise ContractError(
            "choose a delivery loudness profile or an explicit custom target"
        )
    return "custom", number(target, "target LUFS")


def inspect_pcm_levels(path, speech_intervals):
    """Measure supplied speech windows. RMS dBFS is deliberately not labelled LUFS."""
    if not speech_intervals:
        raise ContractError(
            "explicit speech windows required for rough-cut level inspection"
        )
    source = artifact_reference(path, "inspection audio")
    with wave.open(str(path), "rb") as wav:
        width, channels, rate, frames = (
            wav.getsampwidth(),
            wav.getnchannels(),
            wav.getframerate(),
            wav.getnframes(),
        )
        if (
            wav.getcomptype() != "NONE"
            or width not in (2, 3, 4)
            or channels not in (1, 2)
        ):
            raise ContractError(
                "level inspection requires signed 16/24/32-bit PCM mono/stereo"
            )
        scale = float(1 << (8 * width - 1))
        rows = []
        for window in speech_intervals:
            a, b = interval(window)
            if b > frames / rate + 1e-7:
                raise ContractError("speech inspection window outside audio")
            first, last = round(a * rate), round(b * rate)
            wav.setpos(first)
            remaining = last - first
            count, square_sum, peak, clipped = 0, 0, 0, 0
            channel_squares = [0.0] * channels
            mono_squares, frame_count = 0.0, 0
            while remaining > 0:
                data = wav.readframes(min(8192, remaining))
                if not data:
                    break
                remaining -= len(data) // (width * channels)
                for i in range(0, len(data), width * channels):
                    samples = [int.from_bytes(data[i+c*width:i+(c+1)*width], "little", signed=True) for c in range(channels)]
                    frame_count += 1
                    mono_squares += (sum(samples) / channels) ** 2
                    for c, sample in enumerate(samples):
                        count += 1
                        square_sum += sample * sample
                        channel_squares[c] += sample * sample
                        peak = max(peak, abs(sample))
                        clipped += abs(sample) >= scale - 1
            if not count or remaining:
                raise ContractError("empty speech inspection window")
            # Per-channel peaks, not summed mono: opposite phase must not hide clipping.
            rms = math.sqrt(square_sum / count) / scale
            channel_rms = [math.sqrt(s / frame_count) / scale for s in channel_squares]
            mono_rms = math.sqrt(mono_squares / frame_count) / scale
            mono_loss = 20 * math.log10(mono_rms / rms) if mono_rms and rms else None
            from .production_presets import POLICY as PRODUCTION_POLICY
            mono_warning = channels == 2 and rms > 0 and (mono_rms == 0 or mono_loss < PRODUCTION_POLICY["audio"]["mono_cancellation_warning_db"])
            imbalance = 20 * math.log10(max(channel_rms) / min(channel_rms)) if min(channel_rms) > 0 else None
            rows.append(
                {
                    "start": a,
                    "end": b,
                    "rms_dbfs": 20 * math.log10(rms) if rms else None,
                    "sample_peak_dbfs": 20 * math.log10(peak / scale) if peak else None,
                    "full_scale_samples": clipped,
                    "channel_rms_dbfs": [20 * math.log10(v) if v else None for v in channel_rms],
                    "mono_rms_dbfs": 20 * math.log10(mono_rms) if mono_rms else None,
                    "mono_loss_db_relative_to_channels": mono_loss,
                    "mono_cancellation_warning": mono_warning,
                    "channel_imbalance_db": imbalance,
                    "channel_imbalance_warning": channels == 2 and max(channel_rms) > 0 and (min(channel_rms) == 0 or imbalance > PRODUCTION_POLICY["audio"]["channel_imbalance_warning_db"]),
                }
            )
    if source != artifact_reference(path, "inspection audio"):
        raise ContractError("inspection source changed while measuring")
    audible = [r["rms_dbfs"] for r in rows if r["rms_dbfs"] is not None]
    median = statistics.median(audible) if audible else None
    threshold = POLICY["loudness"]["rough_cut_spread_warning_db"]
    return {
        "source_audio": source,
        "metric": "PCM_RMS_dBFS_not_LUFS",
        "windows": rows,
        "level_spread_db": max(audible) - min(audible) if audible else None,
        "review_required": not audible
        or max(audible) - min(audible) > threshold
        or any(r["full_scale_samples"] for r in rows),
        "channel_review_required": any(r["mono_cancellation_warning"] or r["channel_imbalance_warning"] for r in rows),
        "automatic_downmix_applied": False,
        "candidate_gain_db": [
            max(-6, min(6, median - r["rms_dbfs"]))
            if median is not None and r["rms_dbfs"] is not None
            else None
            for r in rows
        ],
        "automatic_gain_applied": False,
        "true_peak_measured": False,
        "final_mix_remeasurement_required": True,
    }


def audio_review_template():
    return {"levels_checked": False, "clipping_checked": False, "evidence": "",
            "segment_leveling": {"before_ref": None, "after_ref": None,
                                 "clip_gains": [], "joins_checked": False,
                                 "expression_preserved": False, "evidence": ""}}


def validate_rough_audio_review(value):
    reviewed(value, ["levels_checked", "clipping_checked"], "rough-cut audio")


def validate_clip_gains(values, keeps):
    if values is None or values == []:
        return []
    if not isinstance(values, list) or len(values) != len(keeps):
        raise ContractError("clip gains must bind every kept interval")
    result = []
    for value, keep in zip(values, keeps, strict=True):
        if interval(value) != interval(keep):
            raise ContractError("clip gain is bound to a different cut")
        gain = number(value.get("gain_db"), "clip gain")
        if not -12 <= gain <= 12:
            raise ContractError("clip gain outside bounded correction range")
        reviewed(value, ["audio_checked", "expression_preserved"], "clip gain")
        result.append(dict(value))
    return result


def cleanup_fallback(profile, flags):
    if not isinstance(flags, list) or any(
        flag not in POLICY["speech_cleanup"]["degradation_flags"] for flag in flags
    ):
        raise ContractError("unknown speech degradation feedback")
    return ("gentle" if profile == "strong" else "original") if flags else profile
