"""Production preset regressions: original-media review and bounded treatments."""

import json
import struct
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from video_creation_pipeline import production_presets as presets
from video_creation_pipeline import production_delivery as production
from video_creation_pipeline import spoken_delivery as delivery
from video_creation_pipeline.audio_edit_quality import inspect_pcm_levels
from video_creation_pipeline.contract_io import (
    artifact_reference as ref,
    atomic_write_json as write,
    file_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.spoken_rough_cut_cli import main


def metadata(duration=8, color="bt709", audio=True):
    streams = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 320,
            "height": 240,
            "avg_frame_rate": "25/1",
            "r_frame_rate": "25/1",
            "pix_fmt": "yuv420p",
            "color_space": "bt709",
            "color_transfer": color,
            "color_primaries": "bt709",
            "color_range": "tv",
        }
    ]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "sample_rate": "48000",
                "channel_layout": "stereo",
            }
        )
    return {"streams": streams, "format": {"duration": str(duration)}}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"synthetic-source-only")
    return path


def edit_plan(source):
    return {
        "source_video": ref(source, "source"),
        "plan_sha256": "synthetic-plan-binding",
        "nle_segments": [
            {"in_s": 1.0, "out_s": 3.0, "duration_s": 2.0},
            {"in_s": 5.0, "out_s": 7.0, "duration_s": 2.0},
        ],
    }


def approval(*keys, **extra):
    return {
        **{key: True for key in keys},
        "evidence": "synthetic fixture review; not real-media acceptance",
        **extra,
    }


def test_source_audit_does_not_confirm_vfr_or_make_derivatives(source, tmp_path):
    info = metadata()
    info["streams"][0]["r_frame_rate"] = "30/1"
    info["streams"][0]["side_data_list"] = [{"rotation": 90}]
    before = set(tmp_path.iterdir())
    with patch.object(presets, "probe_media_metadata", return_value=info):
        audit = presets.inspect_source(
            source,
            project_type="tutorial",
            checks=[{"start": 2, "end": 4, "checks": ["technical", "composition"]}],
        )
    assert set(tmp_path.iterdir()) == before
    assert audit["facts"]["frame_cadence"] == "needs_timestamp_review"
    assert not audit["facts"]["vfr_confirmed"]
    assert audit["facts"]["rotation_degrees"] == 90
    assert "operation_area" in audit["profile"]["composition_checks"]
    assert audit["media_copies_created"] == 0
    presets.validate_audit(audit)
    source.write_bytes(b"changed")
    with pytest.raises(ContractError, match="changed"):
        presets.validate_audit(audit)


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [{"start": 0, "end": 20, "checks": ["audio"]}],
        [{"start": 0, "end": 1, "checks": ["render"]}],
        [{"start": 0, "end": float("nan"), "checks": ["audio"]}],
    ],
)
def test_source_review_rejects_bad_bounds_and_operations(source, bad):
    with (
        patch.object(presets, "probe_media_metadata", return_value=metadata()),
        pytest.raises(ContractError),
    ):
        presets.inspect_source(source, checks=bad)


def test_cadence_uses_presentation_order_and_never_proves_full_cfr(source):
    for points, expected in [
        ([0, 0.08, 0.04, 0.12], "no_variation_in_samples"),
        ([0, 0.04, 0.08, 0.20], "varying_pts_requires_review"),
    ]:
        packets = [{"pts_time": str(p)} for p in points]
        with patch.object(presets, "probe_packet_timestamps", return_value=packets):
            result = presets.sample_cadence(source, [{"start": 0, "end": 1}])
        assert result["status"] == expected
        assert not result["whole_source_cfr_proven"]


def test_cli_uses_canonical_vkp_page_without_generated_media(source, tmp_path):
    sys.path.insert(0, str(delivery.WORKSPACE / "video-knowledge-pipeline/src"))
    from video_knowledge_pipeline import targeted_review

    checks = write(
        {"ranges": [{"start": 2, "end": 4, "checks": ["audio", "color"]}]},
        tmp_path / "checks.json",
    )
    out = tmp_path / "audit"
    with (
        patch.object(presets, "probe_media_metadata", return_value=metadata()),
        patch.object(
            targeted_review,
            "probe_video",
            return_value=SimpleNamespace(
                sha256=file_sha256(source),
                duration_seconds=8,
                width=320,
                height=240,
                fps=25,
            ),
        ),
        patch.object(targeted_review, "_playback_duration_seconds", side_effect=lambda path, duration: duration),
        patch.object(delivery, "execute_ffmpeg_with_receipt") as execute,
    ):
        assert (
            main(
                [
                    "inspect-source",
                    "--source-video",
                    str(source),
                    "--checks",
                    str(checks),
                    "--output-dir",
                    str(out),
                ]
            )
            == 0
        )
    execute.assert_not_called()
    assert (out / "source-review/review/targeted-review.html").is_file()
    pack = json.loads(
        (out / "source-review/review/targeted-review-pack.json").read_text("utf-8")
    )
    assert pack["requests"][0]["review_start_seconds"] == 2
    assert pack["requests"][0]["review_end_seconds"] == 4
    assert not [
        p for p in out.rglob("*") if p.suffix in {".mp4", ".wav", ".jpg", ".png"}
    ]


def test_working_copy_needs_both_sparse_value_and_material_reduction(source):
    fake_ref = {**ref(source, "source"), "bytes": 2**30}
    assessment = approval(
        "full_source_checked",
        "useful_content_accounted",
        source=fake_ref,
        useful_ranges=[{"start": 10, "end": 20}],
        estimated_output_bytes=100 * 2**20,
    )
    with (
        patch.object(presets, "ref", return_value=fake_ref),
        patch.object(Path, "stat", return_value=SimpleNamespace(st_size=2**30)),
    ):
        result = presets.assess_working_copy(source, 100, assessment)
        assert result["eligible"] and result["immediate_reclaimed_bytes"] == 0
        assert (
            result["temporary_additional_bytes_with_original_retained"] == 100 * 2**20
        )
        assert not result["original_deletion_allowed"]
        for ranges, estimate in [
            ([{"start": 0, "end": 80}], 100 * 2**20),
            ([{"start": 0, "end": 10}], 800 * 2**20),
        ]:
            assert not presets.assess_working_copy(
                source,
                100,
                {
                    **assessment,
                    "useful_ranges": ranges,
                    "estimated_output_bytes": estimate,
                },
            )["eligible"]
        with pytest.raises(ContractError):
            presets.assess_working_copy(
                source, 100, {**assessment, "full_source_checked": False}
            )


def test_small_working_copy_blocked_in_actual_render_options(source, tmp_path):
    plan = edit_plan(source)
    assessment = approval(
        "full_source_checked",
        "useful_content_accounted",
        source=plan["source_video"],
        useful_ranges=[
            {"start": s["in_s"], "end": s["out_s"]} for s in plan["nle_segments"]
        ],
        estimated_output_bytes=1,
    )
    path = write(assessment, tmp_path / "copy-assessment.json")
    request = {
        "deliverable": "video",
        "production": {
            "output_purpose": "working_copy",
            "working_copy_assessment": ref(path, "assessment"),
        },
    }
    with pytest.raises(ContractError, match="not justified"):
        production.prepare_render_options(plan, request, metadata())
    with pytest.raises(ContractError, match="requires source-bound"):
        production.settings(
            {"deliverable": "video", "production": {"output_purpose": "working_copy"}}
        )


def test_actual_size_recheck_rejects_overoptimistic_copy_estimate(
    source, tmp_path, monkeypatch
):
    source.write_bytes(b"s" * 1000)
    plan = edit_plan(source)
    monkeypatch.setitem(
        presets.POLICY["working_copy"], "min_estimated_reduction_bytes", 100
    )
    assessment = approval(
        "full_source_checked",
        "useful_content_accounted",
        source=plan["source_video"],
        useful_ranges=[
            {"start": s["in_s"], "end": s["out_s"]} for s in plan["nle_segments"]
        ],
        estimated_output_bytes=10,
    )
    path = write(assessment, tmp_path / "optimistic.json")
    options = production.prepare_render_options(
        plan,
        {
            "deliverable": "video",
            "production": {
                "output_purpose": "working_copy",
                "working_copy_assessment": ref(path, "assessment"),
            },
        },
        metadata(duration=100),
    )
    candidate = tmp_path / "unexpectedly-large.mp4"
    candidate.write_bytes(b"c" * 900)
    with pytest.raises(ContractError, match="actual working-copy size"):
        production.verify_working_copy_result(plan, options, candidate)
    assert source.stat().st_size == 1000 and candidate.is_file()
    candidate.write_bytes(b"c" * 100)
    assert (
        production.verify_working_copy_result(plan, options, candidate)["size_basis"]
        == "actual_output_bytes"
    )


def test_delivery_review_generates_muted_task_without_rendering(source, tmp_path):
    with patch.object(presets, "probe_media_metadata", return_value=metadata()):
        audit = presets.inspect_source(
            source, checks=[{"start": 1, "end": 3, "checks": ["delivery"]}]
        )
    audit_path = write(audit, tmp_path / "final-audit.json")
    consumed = []

    def prepare(path, out, purpose):
        consumed.extend(json.loads(path.read_text("utf-8"))["requests"])
        return {"status": "prepared", "media_copies_created": 0}

    with patch.object(
        delivery, "_self_media_module", return_value=SimpleNamespace(prepare=prepare)
    ):
        result = presets.prepare_source_review(audit_path, tmp_path / "final-review")
    assert [r["playback_mode"] for r in consumed] == ["normal", "muted"]
    assert result["human_review_status"] == "pending"
    assert all(r["source_path"] == str(source.resolve()) for r in consumed)


@pytest.mark.parametrize(
    "transfer",
    ["smpte2084", "arib-std-b67", "log", "raw", "s-log3", "linear", "bt2020-10"],
)
def test_hdr_log_never_enters_sdr_correction_or_export_by_relabel(source, transfer):
    with pytest.raises(ContractError, match="conversion"):
        presets.assert_sdr_processing_safe(metadata(color=transfer))
    with pytest.raises(ContractError, match="conversion"):
        production.prepare_render_options(
            edit_plan(source), {"deliverable": "video"}, metadata(color=transfer)
        )


def test_unknown_color_is_not_labeled_bt709(source):
    info = metadata(color="unknown")
    options = production.prepare_render_options(
        edit_plan(source), {"deliverable": "video"}, info
    )
    assert options["color_disposition"]["status"] == "input_interpretation_pending"
    assert "-color_trc" not in production.export_arguments(options)
    with pytest.raises(ContractError, match="verified BT.709"):
        production.prepare_render_options(
            edit_plan(source),
            {"deliverable": "video", "production": {"export_profile": "web_sdr_1080"}},
            info,
        )


def restoration_review(source, candidate, **extra):
    return approval(
        "same_interval_compared",
        "normal_speed_checked",
        "eyes_checked",
        "teeth_checked",
        "text_checked",
        "temporal_consistency_checked",
        source=ref(source, "source"),
        candidate=ref(candidate, "candidate"),
        start=0,
        end=1,
        operation="enhancement",
        degradation_flags=[],
        **extra,
    )


def test_restoration_reverts_for_eye_flicker_and_excess_crop(source, tmp_path):
    candidate = tmp_path / "enhanced.mp4"
    candidate.write_bytes(b"synthetic candidate")
    review = restoration_review(source, candidate)
    assert presets.restoration_decision(source, candidate, review)["accepted"]
    review["degradation_flags"] = ["eye_change", "temporal_flicker"]
    result = presets.restoration_decision(source, candidate, review)
    assert not result["accepted"] and result["selected_path"] == str(source.resolve())
    review.update(
        operation="stabilization",
        degradation_flags=[],
        max_scale=1.2,
        intentional_camera_motion_preserved=True,
        critical_content_preserved=True,
    )
    assert (
        presets.restoration_decision(source, candidate, review)["fallback"]
        == "original"
    )
    candidate.write_bytes(b"later revision")
    with pytest.raises(ContractError, match="bind"):
        presets.restoration_decision(source, candidate, review)


def test_failed_restoration_blocks_actual_delivery(source, tmp_path):
    candidate = tmp_path / "bad-enhanced.mp4"
    candidate.write_bytes(b"candidate")
    review = restoration_review(source, candidate)
    review["degradation_flags"] = ["teeth_change"]
    path = write(review, tmp_path / "review.json")
    with pytest.raises(ContractError, match="rejected"):
        production.prepare_render_options(
            edit_plan(candidate),
            {
                "deliverable": "video",
                "production": {"restoration_review": ref(path, "review")},
            },
            metadata(),
        )


def test_restoration_sample_cannot_approve_unreviewed_timeline(source, tmp_path):
    candidate = tmp_path / "restored.mp4"
    candidate.write_bytes(b"synthetic restored")
    review = restoration_review(source, candidate)
    path = write(review, tmp_path / "sample-comparison.json")
    request = {
        "deliverable": "video",
        "production": {"restoration_review": ref(path, "review")},
    }
    with pytest.raises(ContractError, match="does not cover"):
        production.prepare_render_options(edit_plan(candidate), request, metadata())
    review.update(start=0, end=8)
    write(review, path)
    request["production"]["restoration_review"] = ref(path, "review")
    assert production.prepare_render_options(edit_plan(candidate), request, metadata())[
        "restoration_decision"
    ]["accepted"]


def test_project_specific_composition_cannot_omit_hands(source):
    review = approval(
        *[k + "_checked" for k in presets.profile("tutorial")["composition_checks"]],
        source=ref(source, "source"),
        start=0,
        end=2,
        unresolved_issues=[],
    )
    assert (
        presets.validate_composition_review(source, review, "tutorial")["status"]
        == "reviewed"
    )
    review["hands_checked"] = False
    with pytest.raises(ContractError, match="hands"):
        presets.validate_composition_review(source, review, "tutorial")


def test_existing_reframe_frontdoor_binds_project_composition(source, tmp_path):
    from video_creation_pipeline.smart_reframe import (
        build_smart_reframe_request,
        build_smart_reframe_plan,
    )

    review = approval(
        *[k + "_checked" for k in presets.profile("tutorial")["composition_checks"]],
        source=ref(source, "source"),
        start=0,
        end=8,
        unresolved_issues=[],
    )
    review_path = write(review, tmp_path / "composition.json")
    request = build_smart_reframe_request(
        profile_id="generic_vertical_9_16",
        media_role="screen_recording",
        production_project_type="tutorial",
        composition_review_path=review_path,
    )
    request_path = write(request, tmp_path / "reframe-request.json")
    with patch(
        "video_creation_pipeline.smart_reframe.source_media_dimensions",
        return_value={
            "width": 320,
            "height": 240,
            "duration_s": 8,
            "media_kind": "video",
            "fps": 25,
            "has_audio": True,
            "mime_type": "video/mp4",
        },
    ):
        result = build_smart_reframe_plan(source, request_path)
    assert result["inputs"]["source_media"] == ref(source, "source")
    review["hands_checked"] = False
    write(review, review_path)
    with pytest.raises(ContractError, match="changed"):
        build_smart_reframe_plan(source, request_path)


def audio_edit(plan):
    split = approval(
        "context_checked",
        "chronology_preserved",
        "reaction_not_fabricated",
        "visual_handles_checked",
        boundary_after_segment=0,
        video_cut_offset_s=0.25,
    )
    room = approval(
        "same_scene",
        "contains_no_speech_or_reaction",
        "target_non_dialogue_checked",
        "auditioned",
        start=1.5,
        end=2.5,
        source_range={"start": 3.5, "end": 4.5},
        gain_db=-24,
    )
    return {
        "source": plan["source_video"],
        "source_plan_sha256": plan["plan_sha256"],
        "split_edits": [split],
        "room_tone": [room],
    }


def test_j_l_edits_change_video_only_and_preserve_timeline(source, tmp_path):
    plan = edit_plan(source)
    for offset in [-0.25, 0.25]:
        edit = audio_edit(plan)
        edit["split_edits"][0]["video_cut_offset_s"] = offset
        value = production.validate_audio_edit(
            plan, presets.media_facts(metadata()), edit
        )
        assert value["video_ranges"] == [
            {"start": 1, "end": 3 + offset},
            {"start": 5 + offset, "end": 7},
        ]
        assert value["duration_s"] == 4
        path = write(edit, tmp_path / f"audio-{offset}.json")
        options = production.prepare_render_options(
            plan,
            {"deliverable": "video", "production": {"audio_edit": ref(path, "edit")}},
            metadata(),
        )
        command = delivery._render_command(plan, tmp_path / "out.mp4", True, options)
        graph = command[command.index("-filter_complex") + 1]
        assert "atrim=start=1.0:end=3.0" in graph
        assert "atrim=start=5.0:end=7.0" in graph
        assert "normalize=0" in graph and "adelay=1500.0" in graph
        assert "-r" not in command and "-ac" not in command


def test_treatment_rejects_missing_context_handles_and_fake_room_tone(source):
    plan, facts = edit_plan(source), presets.media_facts(metadata())
    for field in [
        "chronology_preserved",
        "reaction_not_fabricated",
        "visual_handles_checked",
    ]:
        edit = audio_edit(plan)
        edit["split_edits"][0][field] = False
        with pytest.raises(ContractError):
            production.validate_audio_edit(plan, facts, edit)
    edit = audio_edit(plan)
    edit["split_edits"][0]["video_cut_offset_s"] = 3
    with pytest.raises(ContractError):
        production.validate_audio_edit(plan, facts, edit)
    edit = audio_edit(plan)
    edit["room_tone"][0]["contains_no_speech_or_reaction"] = False
    with pytest.raises(ContractError):
        production.validate_audio_edit(plan, facts, edit)


def test_separate_audio_streams_require_explicit_speaker_accounting(source):
    info = metadata()
    info["streams"].append({**info["streams"][1], "index": 2})
    with pytest.raises(ContractError, match="audio stream selection"):
        production.prepare_render_options(
            edit_plan(source), {"deliverable": "video"}, info
        )


@pytest.mark.parametrize(
    "pair,mono_warning,imbalance",
    [
        ((1000, -1000), True, False),
        ((1000, 1000), False, False),
        ((1000, 0), False, True),
    ],
)
def test_pcm_channel_and_mono_checks_do_not_change_source(
    tmp_path, pair, mono_warning, imbalance
):
    path = tmp_path / "channels.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<" + "h" * 960, *(pair * 480)))
    before = file_sha256(path)
    report = inspect_pcm_levels(path, [{"start": 0, "end": 0.01}])
    assert report["windows"][0]["mono_cancellation_warning"] is mono_warning
    assert report["windows"][0]["channel_imbalance_warning"] is imbalance
    assert not report["automatic_downmix_applied"] and file_sha256(path) == before


def test_final_export_requires_actual_metadata_and_human_review_stays_pending(
    source, tmp_path
):
    options = production.prepare_render_options(
        edit_plan(source), {"deliverable": "video"}, metadata()
    )
    final = metadata(duration=4)
    assert production.final_metadata_check(final, 4, options)["status"] == "passed"
    final["streams"][0]["pix_fmt"] = "yuv444p"
    assert production.final_metadata_check(final, 4, options)["status"] == "failed"
    final["streams"][0].pop("pix_fmt")
    assert production.final_metadata_check(final, 4, options)["status"] == "unverified"
    log = tmp_path / "qa.log"
    log.write_text("black_start:0 black_end:0.2 freeze_start:2", encoding="utf-8")
    result = production.detection_findings(log)
    assert len(result["events"]) == 3 and not result["automatic_deletion"]
    assert all(e["disposition"].startswith("pending") for e in result["events"])


def test_final_export_detects_color_and_channel_drift(source):
    options = production.prepare_render_options(
        edit_plan(source), {"deliverable": "video"}, metadata()
    )
    final = metadata(duration=4)
    final["streams"][0]["color_transfer"] = "smpte2084"
    final["streams"][1]["channels"] = 1
    report = production.final_metadata_check(final, 4, options)
    assert report["status"] == "failed"
    assert {"color_transfer", "audio_channels"} <= set(report["errors"])
