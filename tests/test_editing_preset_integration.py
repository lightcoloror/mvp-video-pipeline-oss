"""Synthetic regressions for the actual editing front doors, no real media."""

import copy
import json
import struct
import wave

import pytest

from video_creation_pipeline.audio_edit_quality import inspect_pcm_levels
from video_creation_pipeline.contract_io import atomic_write_json, file_sha256
from video_creation_pipeline.editing_presets import (
    preferred_take,
    splice_fades,
    fade_filter,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.production_strategy import build_audio_mastering_plan
from video_creation_pipeline.speech_cleanup import (
    build_speech_cleanup_plan,
    validate_speech_cleanup_plan,
)
from video_creation_pipeline.spoken_rough_cut_cli import main as spoken_main
from video_creation_pipeline.subtitle_readability import subtitle_layout, subtitle_font
from video_creation_pipeline.timeline_dependencies import (
    dependency_state,
    validate_dependencies,
)
from video_creation_pipeline.visual_edit_presets import visual_defaults


def pcm(path, values, rate=48000, channels=1):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(struct.pack("<" + "h" * len(values), *values))
    return path


def test_latest_take_preference_and_exception_flags():
    early = dict(start=0, end=1, complete=True, quality_score=0.7)
    last = dict(start=2, end=3, complete=True, quality_score=1)
    assert preferred_take([early, last]) is last
    for flag in ("misstatement", "quality_problem", "incomplete"):
        last["review_flags"] = [flag]
        assert preferred_take([early, last]) is early


def test_rough_audio_cli_reports_clipping_and_levels_without_applying_gain(tmp_path):
    audio = pcm(
        tmp_path / "levels.wav", [1000] * 48000 + [16000] * 48000 + [32767] * 48000
    )
    before = file_sha256(audio)
    windows = atomic_write_json(
        {"speech_windows": [dict(start=i, end=i + 1) for i in range(3)]},
        tmp_path / "windows.json",
    )
    out = tmp_path / "levels.json"
    assert (
        spoken_main(
            [
                "inspect-audio-levels",
                "--audio",
                str(audio),
                "--windows",
                str(windows),
                "--output",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads(out.read_text("utf-8"))
    assert report["review_required"] and report["level_spread_db"] > 25
    assert report["windows"][2]["full_scale_samples"] == 48000
    assert report["source_audio"]["sha256"] == before == file_sha256(audio)
    assert not report["automatic_gain_applied"] and not report["true_peak_measured"]
    stereo = pcm(tmp_path / "opposite.wav", [32767, -32768] * 480, channels=2)
    assert (
        inspect_pcm_levels(stereo, [dict(start=0, end=0.01)])["windows"][0][
            "full_scale_samples"
        ]
        == 960
    )


def test_mastering_requires_delivery_choice_and_cleanup_falls_back(tmp_path):
    source = pcm(tmp_path / "voice.wav", [1000] * 48000)
    with pytest.raises(ContractError, match="delivery loudness"):
        build_audio_mastering_plan(source, tmp_path / "out.wav")
    assert (
        build_audio_mastering_plan(
            source, tmp_path / "out.wav", delivery_profile="ebu_r128"
        )["policy"]["target_lufs"]
        == -23
    )
    with pytest.raises(ContractError, match="conflicts"):
        build_audio_mastering_plan(
            source, tmp_path / "out.wav", delivery_profile="ebu_r128", target_lufs=-16
        )
    gentle = build_speech_cleanup_plan(
        source, tmp_path / "gentle.wav", noise_kinds=["broadband_hiss"]
    )
    assert gentle["profile"] == "gentle"
    with pytest.raises(ContractError, match="explicit review"):
        build_speech_cleanup_plan(
            source,
            tmp_path / "strong.wav",
            profile="strong",
            noise_kinds=["broadband_hiss"],
        )
    feedback = dict(
        audio_checked=True,
        evidence="synthetic degradation decision",
        source_sha256=file_sha256(source),
        previous_profile="gentle",
        degradation_flags=["consonants_lost"],
    )
    original = build_speech_cleanup_plan(
        source,
        tmp_path / "original.wav",
        profile="original",
        noise_kinds=[],
        audition_feedback=feedback,
    )
    assert original["filter_chain"] == "anull"
    validate_speech_cleanup_plan(original)
    with pytest.raises(ContractError, match="must be reduced"):
        build_speech_cleanup_plan(
            source,
            tmp_path / "bad.wav",
            profile="gentle",
            noise_kinds=["broadband_hiss"],
            audition_feedback=feedback,
        )


def test_subtitles_preserve_numbers_negation_and_terms_without_shrinking():
    text = "等待期是90天，不是交费后立即生效"
    layout = subtitle_layout(
        text, protected_terms=["90天", "不是"], duration_s=3, max_line_units=12
    )
    assert len(layout["lines"]) == 2 and "".join(layout["lines"]) == text
    assert any("90天" in line for line in layout["lines"])
    assert not layout["font_shrunk"]
    with pytest.raises(ContractError, match="font will not shrink"):
        subtitle_layout(
            "这是完全没有断句位置的很长的一句中文说明需要先拆成有时间依据的两句"
        )
    with pytest.raises(ContractError, match="reading speed"):
        subtitle_layout("不是十万元", duration_s=0.1)
    with pytest.raises(ContractError, match="canonical"):
        subtitle_font(1080, 1920, 20)
    assert subtitle_font(1080, 1920)[0] >= 48


def test_semantic_cuts_and_single_primary_emphasis():
    assert visual_defaults({}, 0, 2)["transition"]["kind"] == "cut"
    for bad in ("fixed_interval", "keyword_only"):
        with pytest.raises(ContractError):
            visual_defaults({"cut_basis": bad}, 0, 2)
    with pytest.raises(ContractError, match="narrative reason"):
        visual_defaults({"transition": {"kind": "dissolve"}}, 0, 2)
    event = dict(
        start=0.1, end=0.8, semantic_trigger="reveal the amount", role="primary"
    )
    with pytest.raises(ContractError, match="one primary"):
        visual_defaults(
            {"emphasis_events": [event, {**event, "start": 0.3, "end": 1}]}, 0, 2
        )
    with pytest.raises(ContractError, match="whole-sentence"):
        visual_defaults({"whole_sentence_continuous_motion": True}, 0, 2)


def test_every_timeline_dependent_role_is_invalidated_on_reorder_trim_and_speed():
    source = [
        dict(source_start=0, source_end=2, output_start=0, output_end=2),
        dict(source_start=5, source_end=7, output_start=2, output_end=4),
    ]
    previous = dependency_state(source)
    previous["roles"] = {k: "verified" for k in previous["roles"]}
    validate_dependencies(source, previous, required=previous["roles"])
    variants = [list(reversed(source)), copy.deepcopy(source), copy.deepcopy(source)]
    variants[1][0]["source_start"] = 0.1
    variants[2][0]["output_end"] = 1
    for changed in variants:
        state = dependency_state(changed, previous)
        assert set(state["roles"]) == {
            "subtitles",
            "sound_effects",
            "chapters",
            "progress",
            "review",
        }
        assert set(state["roles"].values()) == {"pending"}
        with pytest.raises(ContractError, match="stale"):
            validate_dependencies(changed, previous)
        with pytest.raises(ContractError, match="fresh verification"):
            validate_dependencies(changed, state, required=state["roles"])


def test_rendered_splice_preserves_duration_and_reduces_discontinuity(tmp_path):
    from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
    from video_creation_pipeline.review_resource_guard import ReviewResourcePolicy

    source = pcm(
        tmp_path / "step.wav", [12000] * 48000 + [0] * 48000 + [-12000] * 48000
    )
    before = file_sha256(source)
    keeps = [dict(start=0, end=1), dict(start=2, end=3)]
    fades = splice_fades(keeps, [], [])
    graph = (
        ";".join(
            f"[0:a]atrim=start={k['start']}:end={k['end']},asetpts=PTS-STARTPTS,{fade_filter(f)}[a{i}]"
            for i, (k, f) in enumerate(zip(keeps, fades, strict=True))
        )
        + ";[a0][a1]concat=n=2:v=0:a=1[out]"
    )
    output = tmp_path / "splice.wav"
    receipt = execute_ffmpeg_with_receipt(
        "render",
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-threads",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        [{"role": "synthetic_source", "path": source}],
        [{"role": "synthetic_output", "path": output}],
        tmp_path / "splice-receipt.json",
        guard_resource=True,
        resource_policy=ReviewResourcePolicy(
            policy_id="synthetic-short-audio",
            min_available_memory_bytes=2 * 1024**3,
            min_available_memory_percent=5,
            ffmpeg_threads=1,
        ),
    )
    with wave.open(str(output), "rb") as stream:
        assert stream.getnframes() == 96000
        values = struct.unpack("<" + "h" * 96000, stream.readframes(96000))
    assert abs(values[48000] - values[47999]) < 100
    assert values[24000] == 12000 and values[72000] == -12000
    assert file_sha256(source) == before
    assert receipt["resource_guard"]["decision"] == "admitted"


def test_music_silence_window_reaches_actual_pcm_output(tmp_path):
    from video_creation_pipeline.music_pipeline import build_ducked_music_stem_command
    from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
    from video_creation_pipeline.review_resource_guard import ReviewResourcePolicy

    source = pcm(tmp_path / "music.wav", [6000] * 48000 * 3)
    speech = pcm(tmp_path / "speech.wav", [0] * 48000 * 3)
    selection = dict(
        status="auto_selected",
        timeline_interval=dict(start_s=0, end_s=3),
        source_interval=dict(start_s=0, end_s=3),
        loop=False,
        fade_in_s=0.01,
        fade_out_s=0.01,
        gain_db=0,
        ducking=dict(enabled=False),
        no_music_windows=[dict(start_s=1, end_s=2, reason="emotional original sound")],
    )
    output = tmp_path / "music-stem.wav"
    command = build_ducked_music_stem_command(speech, source, output, selection)
    execute_ffmpeg_with_receipt(
        "music_mix",
        command,
        [
            dict(role="synthetic_music", path=source),
            dict(role="synthetic_speech", path=speech),
        ],
        [dict(role="synthetic_stem", path=output)],
        tmp_path / "music-receipt.json",
        guard_resource=True,
        resource_policy=ReviewResourcePolicy(
            policy_id="synthetic-short-audio",
            min_available_memory_bytes=2 * 1024**3,
            min_available_memory_percent=5,
            ffmpeg_threads=1,
        ),
    )
    report = inspect_pcm_levels(
        output,
        [dict(start=0.2, end=0.8), dict(start=1.1, end=1.9), dict(start=2.2, end=2.8)],
    )
    assert report["windows"][0]["rms_dbfs"] is not None
    assert report["windows"][1]["rms_dbfs"] is None
    assert report["windows"][2]["rms_dbfs"] is not None
    with wave.open(str(output), "rb") as wav:
        assert wav.getnframes() == 144000


def test_reviewed_gain_and_fades_reach_existing_spoken_renderer():
    from video_creation_pipeline.spoken_delivery import _render_command
    from video_creation_pipeline.audio_edit_quality import validate_clip_gains

    keep = [dict(start=0, end=1), dict(start=2, end=3)]
    gains = [
        dict(
            **row,
            gain_db=g,
            audio_checked=True,
            expression_preserved=True,
            evidence="synthetic level balance",
        )
        for row, g in zip(keep, [3, -2], strict=True)
    ]
    validate_clip_gains(gains, keep)
    plan = dict(
        source_video={"path": "synthetic.mp4"},
        nle_segments=[dict(in_s=r["start"], out_s=r["end"]) for r in keep],
        audio_splices=splice_fades(keep, [], []),
        clip_gains=gains,
    )
    command = _render_command(plan, "synthetic-output.mp4", True)
    graph = command[command.index("-filter_complex") + 1]
    assert "volume=3dB" in graph and "volume=-2dB" in graph and "afade=" in graph
    gains[0]["end"] = 0.8
    with pytest.raises(ContractError, match="different cut"):
        validate_clip_gains(gains, keep)
