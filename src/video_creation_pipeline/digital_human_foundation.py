from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

from .content_video_pipeline import validate_content_video_project_plan
from .creative_gates import APPROVAL_RECORD_SCHEMA, RELEASE_GATE_SCHEMA
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    atomic_write_text,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    execute_ffmpeg_with_receipt,
    validate_ffmpeg_execution_receipt,
)
from .generation_contracts import GENERATION_TASK_SCHEMA
from .reference_remix import normalize_reference_recipe
from .technical_qa import (
    build_technical_qa_report,
    validate_technical_qa_report,
    write_technical_qa_report,
)
from .tts_pipeline import (
    build_audio_stitch_qa,
    build_resume_plan,
    build_tts_job,
    normalize_tts_text,
    write_tts_contract,
)

SPOKEN_SCRIPT_SOURCE_SCHEMA = "video_creation_pipeline.spoken_script_source.v1"
SPOKEN_SCRIPT_REVIEW_SCHEMA = "video_creation_pipeline.spoken_script_review.v1"
DIGITAL_HUMAN_SOURCE_MANIFEST_SCHEMA = (
    "video_creation_pipeline.digital_human_source_manifest.v1"
)
DIGITAL_HUMAN_TIMELINE_SCHEMA = (
    "video_creation_pipeline.digital_human_candidate_timeline.v1"
)
DIGITAL_HUMAN_EDL_SCHEMA = "video_creation_pipeline.digital_human_candidate_edl.v1"
DIGITAL_HUMAN_RENDER_MANIFEST_SCHEMA = (
    "video_creation_pipeline.digital_human_render_manifest.v1"
)
DIGITAL_HUMAN_REVIEW_PACKET_SCHEMA = (
    "video_creation_pipeline.digital_human_review_packet.v1"
)
DIGITAL_HUMAN_RUN_RECEIPT_SCHEMA = (
    "video_creation_pipeline.digital_human_foundation_run_receipt.v1"
)

FOUNDATION_STATUS = "foundation_candidate_ready"
FEATURE_STATE = "paused"
_FILLERS = ("嗯，", "嗯", "呃，", "呃", "就是说，", "就是说", "其实，")
_REQUIRED_TRACKS = {
    "narration_audio",
    "avatar_placeholder",
    "slides",
    "interview",
    "captions",
    "music",
}
_PROHIBITED_ACTIONS = {"publish", "upload", "automatic_approval"}


def generate_digital_human_foundation_synthetic(
    plan_root: str | Path,
    *,
    dispatch_id: str = "DIGITAL-HUMAN-INDEPENDENT-FOUNDATION-20260812-01",
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Generate an entirely synthetic, route-unselected review candidate."""

    root = Path(plan_root).expanduser().resolve()
    plan_path = root / "project-plan.json"
    plan = read_json_object(plan_path, "content video project plan")
    validate_content_video_project_plan(plan)
    if plan.get("readiness", {}).get("synthetic_fixture_render_eligible") is not True:
        raise ContractError("content video plan blocks synthetic fixture rendering")
    _validate_plan_owner(root, plan)

    fixture_dir = root / "digital-human-foundation" / "fixture"
    media_dir = root / "digital-human-foundation" / "media"
    contract_dir = root / "digital-human-foundation" / "contracts"
    receipt_dir = root / "digital-human-foundation" / "ffmpeg-receipts"
    render_dir = root / "digital-human-foundation" / "render"
    qa_dir = root / "digital-human-foundation" / "qa"
    for directory in (
        fixture_dir,
        media_dir,
        contract_dir,
        receipt_dir,
        render_dir,
        qa_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    script_source_path = _write_script_source(plan_path, fixture_dir)
    script_review = build_spoken_script_review(plan_path, script_source_path)
    script_review_path = atomic_write_json(
        script_review, contract_dir / "spoken-script-review.json"
    )
    subtitles_path = render_dir / "candidate-subtitles.srt"
    atomic_write_text(_script_srt(script_review), subtitles_path)

    tts_paths, narration_path, narration_receipts = _build_synthetic_tts(
        root,
        plan,
        script_review_path,
        script_review,
        timeout_s=timeout_s,
    )
    music_path = media_dir / "synthetic-music-bed.wav"
    music_receipt = receipt_dir / "synthetic-music-bed.json"
    _render_wav(
        music_path,
        duration_s=15.0,
        frequency=180,
        volume=0.025,
        receipt_path=music_receipt,
        input_path=script_source_path,
        role="synthetic_music_bed",
        timeout_s=timeout_s,
    )

    deck_path = _write_synthetic_deck(fixture_dir)
    interview_authorizations = _write_synthetic_authorizations(fixture_dir)
    media_receipts: list[Path] = []
    slide_paths: list[Path] = []
    for page in range(1, 4):
        output = media_dir / f"slide-{page:03}.mp4"
        receipt = receipt_dir / f"slide-{page:03}.json"
        _render_color_clip(
            output,
            duration_s=2.0,
            color=("164e63", "166534", "7c2d12")[page - 1],
            label=f"SYNTHETIC PPT PAGE {page}",
            receipt_path=receipt,
            input_path=deck_path,
            role=f"slide_page_{page}",
            timeout_s=timeout_s,
        )
        slide_paths.append(output)
        media_receipts.append(receipt)

    interview_paths: list[Path] = []
    for index, authorization in enumerate(interview_authorizations, 1):
        output = media_dir / f"interview-{index:03}.mp4"
        receipt = receipt_dir / f"interview-{index:03}.json"
        _render_color_clip(
            output,
            duration_s=2.0,
            color=("4c1d95", "9f1239")[index - 1],
            label=f"SYNTHETIC INTERVIEW {index}",
            receipt_path=receipt,
            input_path=authorization,
            role=f"synthetic_interview_{index}",
            timeout_s=timeout_s,
        )
        interview_paths.append(output)
        media_receipts.append(receipt)

    placeholder_path = media_dir / "avatar-route-unselected.mp4"
    placeholder_receipt = receipt_dir / "avatar-route-unselected.json"
    _render_color_clip(
        placeholder_path,
        duration_s=3.0,
        color="334155",
        label="AVATAR ROUTE UNSELECTED",
        receipt_path=placeholder_receipt,
        input_path=script_source_path,
        role="avatar_placeholder",
        timeout_s=timeout_s,
    )
    media_receipts.append(placeholder_receipt)

    source_manifest = build_digital_human_source_manifest(
        plan_path=plan_path,
        script_review_path=script_review_path,
        deck_path=deck_path,
        slide_paths=slide_paths,
        interview_paths=interview_paths,
        interview_authorization_paths=interview_authorizations,
        placeholder_path=placeholder_path,
        narration_path=narration_path,
        music_path=music_path,
        subtitles_path=subtitles_path,
    )
    source_manifest_path = atomic_write_json(
        source_manifest, contract_dir / "source-manifest.json"
    )
    timeline = build_digital_human_candidate_timeline(
        source_manifest_path,
        script_review_path,
        tts_paths["job"],
        tts_paths["audio_qa"],
    )
    timeline_path = atomic_write_json(
        timeline, contract_dir / "candidate-timeline.json"
    )
    candidate_edl = build_digital_human_candidate_edl(timeline_path)
    candidate_edl_path = atomic_write_json(
        candidate_edl, contract_dir / "candidate-edl.json"
    )
    packaging_path = atomic_write_json(
        _foundation_packaging_plan(timeline),
        render_dir / "foundation-packaging-plan.json",
    )

    visual_program_path, timeline_receipts = _render_visual_timeline(
        source_manifest,
        timeline,
        render_dir,
        receipt_dir,
        timeline_path,
        timeout_s=timeout_s,
    )
    candidate_path = render_dir / "digital-human-foundation-candidate.mp4"
    mix_receipt_path = receipt_dir / "foundation-candidate-mix.json"
    mix_command = [
        "ffmpeg",
        "-y",
        "-i",
        str(visual_program_path),
        "-i",
        str(narration_path),
        "-i",
        str(music_path),
        "-filter_complex",
        "[1:a]volume=0.9[n];[2:a]volume=0.08[m];[n][m]amix=inputs=2:duration=first:normalize=0[a]",
        "-map",
        "0:v:0",
        "-map",
        "[a]",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-threads",
        "1",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        str(candidate_path),
    ]
    execute_ffmpeg_with_receipt(
        "music_mix",
        mix_command,
        [
            {"role": "visual_program", "path": visual_program_path},
            {"role": "synthetic_narration", "path": narration_path},
            {"role": "synthetic_music", "path": music_path},
            {"role": "candidate_timeline", "path": timeline_path},
        ],
        [{"role": "foundation_candidate", "path": candidate_path}],
        mix_receipt_path,
        timeout_s=timeout_s,
    )

    qa_report_path, qa_receipt_path = _run_technical_qa(
        candidate_path,
        subtitles_path,
        packaging_path,
        qa_dir,
        receipt_dir,
        expected_duration_s=15.0,
        deterministic_created_at=str(
            plan.get("created_at") or "2026-08-12T00:00:00+00:00"
        ),
        timeout_s=timeout_s,
    )
    all_receipts = sorted(
        {
            *narration_receipts,
            music_receipt,
            *media_receipts,
            *timeline_receipts,
            mix_receipt_path,
            qa_receipt_path,
        },
        key=lambda path: path.name,
    )
    render_manifest = build_digital_human_render_manifest(
        timeline_path,
        candidate_edl_path,
        candidate_path,
        subtitles_path,
        qa_report_path,
        all_receipts,
    )
    render_manifest_path = atomic_write_json(
        render_manifest, contract_dir / "render-manifest.json"
    )
    review_packet = build_digital_human_review_packet(
        script_review_path,
        source_manifest_path,
        timeline_path,
        candidate_edl_path,
        render_manifest_path,
        tts_paths,
    )
    review_packet_path = atomic_write_json(
        review_packet, contract_dir / "human-review-packet.json"
    )

    receipt: dict[str, Any] = {
        "schema": DIGITAL_HUMAN_RUN_RECEIPT_SCHEMA,
        "dispatch_id": str(dispatch_id),
        "status": FOUNDATION_STATUS,
        "feature_state": FEATURE_STATE,
        "artifacts": {
            "spoken_script_review": artifact_reference(
                script_review_path, "spoken script review"
            ),
            "source_manifest": artifact_reference(
                source_manifest_path, "source manifest"
            ),
            "candidate_timeline": artifact_reference(
                timeline_path, "candidate timeline"
            ),
            "candidate_edl": artifact_reference(candidate_edl_path, "candidate EDL"),
            "review_packet": artifact_reference(
                review_packet_path, "human review packet"
            ),
            "candidate_video": artifact_reference(
                candidate_path, "foundation candidate video"
            ),
            "render_manifest": artifact_reference(
                render_manifest_path, "render manifest"
            ),
            "technical_qa": artifact_reference(qa_report_path, "technical QA"),
        },
        "deterministic_identity": {
            "source_manifest_sha256": source_manifest["manifest_sha256"],
            "timeline_sha256": timeline["timeline_sha256"],
            "edl_sha256": candidate_edl["edl_sha256"],
            "candidate_video_sha256": file_sha256(candidate_path),
            "script_text_sha256": script_review["candidate_spoken_text_sha256"],
        },
        "digital_human_capability": {
            "avatar_route_selected": False,
            "actual_avatar_generated": False,
            "lip_sync_status": "not_tested_route_unselected",
            "face_anomaly_status": "not_applicable_placeholder",
            "voice_identity_status": "not_tested_personal_voice_unconfigured",
        },
        "authority_boundary": {
            "candidate_only": True,
            "manual_review_required": True,
            "approval_record_created": False,
            "release_eligible": False,
            "external_provider_called": False,
            "network_used": False,
            "upload_performed": False,
            "publish_performed": False,
            "personal_media_used": False,
            "customer_media_used": False,
            "second_ffmpeg_orchestrator_created": False,
            "second_tts_state_created": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    receipt_path = atomic_write_json(
        receipt, contract_dir / "foundation-run-receipt.json"
    )
    validation = validate_digital_human_foundation_bundle(root)
    return {
        **validation,
        "receipt": str(receipt_path),
        "candidate_video": str(candidate_path),
        "review_packet": str(review_packet_path),
    }


def build_spoken_script_review(
    plan_path: str | Path,
    script_source_path: str | Path,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    source_file = Path(script_source_path).expanduser().resolve()
    plan = read_json_object(plan_file, "content video project plan")
    validate_content_video_project_plan(plan)
    source = read_json_object(source_file, "spoken script source")
    _validate_spoken_script_source(source, plan_file)
    rows: list[dict[str, Any]] = []
    all_replacements: list[dict[str, Any]] = []
    for raw in source["segments"]:
        original = str(raw["original_text"])
        revised = original
        segment_changes: list[dict[str, Any]] = []
        for filler in _FILLERS:
            if filler in revised:
                count = revised.count(filler)
                revised = revised.replace(filler, "")
                change = {
                    "kind": "remove_filler",
                    "source": filler,
                    "target": "",
                    "count": count,
                }
                segment_changes.append(change)
                all_replacements.append(change)
        normalized, replacements = normalize_tts_text(revised, raw.get("term_rules"))
        for replacement in replacements:
            change = {**replacement, "count": 1}
            segment_changes.append(change)
            all_replacements.append(change)
        rows.append(
            {
                "segment_id": raw["segment_id"],
                "source_text_sha256": canonical_json_sha256(original),
                "original_text": original,
                "candidate_spoken_text": normalized,
                "candidate_text_sha256": canonical_json_sha256(normalized),
                "evidence_refs": [dict(row) for row in raw["evidence_refs"]],
                "changes": segment_changes,
                "pronunciation_notes": list(raw.get("pronunciation_notes") or []),
                "pause_and_emphasis": dict(raw.get("pause_and_emphasis") or {}),
                "subtitle_breaks": list(raw.get("subtitle_breaks") or [normalized]),
            }
        )
    review: dict[str, Any] = {
        "schema": SPOKEN_SCRIPT_REVIEW_SCHEMA,
        "source_project_plan": artifact_reference(plan_file, "project plan"),
        "source_script": artifact_reference(source_file, "spoken script source"),
        "source_script_sha256": source["source_sha256"],
        "segments": rows,
        "candidate_spoken_text_sha256": canonical_json_sha256(
            [row["candidate_spoken_text"] for row in rows]
        ),
        "change_summary": {
            "change_count": len(all_replacements),
            "meaning_preserved": True,
            "new_fact_count": 0,
            "free_rewrite_performed": False,
            "allowed_operations": ["remove_filler", "normalize_tts_text"],
        },
        "review_boundary": {
            "manual_review_required": True,
            "approval_status": "not_approved",
            "approval_record_created": False,
            "tts_execution_authorized": False,
            "new_fact_without_evidence_allowed": False,
        },
    }
    review["review_sha256"] = payload_sha256(review, "review_sha256")
    validate_spoken_script_review(review)
    return review


def validate_spoken_script_review(review: dict[str, Any]) -> None:
    if review.get("schema") != SPOKEN_SCRIPT_REVIEW_SCHEMA:
        raise ContractError("unsupported spoken script review schema")
    if review.get("review_sha256") != payload_sha256(review, "review_sha256"):
        raise ContractError("spoken script review integrity check failed")
    plan_path = validate_artifact_reference(
        _object(review.get("source_project_plan")), "project plan"
    )
    plan = read_json_object(plan_path, "content video project plan")
    validate_content_video_project_plan(plan)
    source_path = validate_artifact_reference(
        _object(review.get("source_script")), "spoken script source"
    )
    source = read_json_object(source_path, "spoken script source")
    _validate_spoken_script_source(source, plan_path)
    if review.get("source_script_sha256") != source.get("source_sha256"):
        raise ContractError("spoken script review binds a stale source")
    rows = review.get("segments")
    if not isinstance(rows, list) or len(rows) != len(source["segments"]):
        raise ContractError("spoken script review segment set is incomplete")
    for source_row, row_value in zip(source["segments"], rows, strict=True):
        row = _object(row_value)
        if (
            row.get("segment_id") != source_row.get("segment_id")
            or row.get("source_text_sha256")
            != canonical_json_sha256(source_row.get("original_text"))
            or row.get("evidence_refs") != source_row.get("evidence_refs")
            or not str(row.get("candidate_spoken_text") or "").strip()
            or row.get("candidate_text_sha256")
            != canonical_json_sha256(row.get("candidate_spoken_text"))
        ):
            raise ContractError("spoken script review segment drifted")
        if not row.get("evidence_refs"):
            raise ContractError("spoken script facts require evidence references")
        for change_value in row.get("changes") or []:
            change = _object(change_value)
            if change.get("kind") not in {
                "remove_filler",
                "term",
                "date",
                "percent",
                "phone",
                "decimal",
                "integer",
                "acronym",
            }:
                raise ContractError("spoken script contains an unaudited rewrite")
    if review.get("candidate_spoken_text_sha256") != canonical_json_sha256(
        [str(_object(row).get("candidate_spoken_text") or "") for row in rows]
    ):
        raise ContractError("spoken script candidate text hash drifted")
    summary = _object(review.get("change_summary"))
    boundary = _object(review.get("review_boundary"))
    if (
        summary.get("meaning_preserved") is not True
        or summary.get("new_fact_count") != 0
        or summary.get("free_rewrite_performed") is not False
        or boundary.get("manual_review_required") is not True
        or boundary.get("approval_status") != "not_approved"
        or boundary.get("approval_record_created") is not False
        or boundary.get("new_fact_without_evidence_allowed") is not False
    ):
        raise ContractError("spoken script review boundary is invalid")


def build_digital_human_source_manifest(
    *,
    plan_path: str | Path,
    script_review_path: str | Path,
    deck_path: str | Path,
    slide_paths: list[Path],
    interview_paths: list[Path],
    interview_authorization_paths: list[Path],
    placeholder_path: str | Path,
    narration_path: str | Path,
    music_path: str | Path,
    subtitles_path: str | Path,
) -> dict[str, Any]:
    if len(slide_paths) != 3 or len(interview_paths) != 2:
        raise ContractError(
            "synthetic foundation requires three slides and two interviews"
        )
    deck_ref = artifact_reference(deck_path, "synthetic PPT source")
    assets: list[dict[str, Any]] = []
    slide_ranges = ((3.0, 5.0), (7.0, 9.0), (11.0, 13.0))
    for page, (path, target_range) in enumerate(
        zip(slide_paths, slide_ranges, strict=True), 1
    ):
        assets.append(
            {
                "asset_id": f"slide-{page:03}",
                "source_type": "slide",
                "source_file": deck_ref,
                "primary_artifact": artifact_reference(path, f"slide page {page}"),
                "page_number": page,
                "page_count": 3,
                "original_order": page,
                "source_duration_s": 2.0,
                "target_ranges": [_time_range(*target_range)],
                "safe_crop": {"x": 0.03, "y": 0.03, "width": 0.94, "height": 0.82},
                "freshness_status": "synthetic_current",
                "authorization_status": "synthetic",
            }
        )
    interview_ranges = ((5.0, 7.0), (9.0, 11.0))
    for index, (path, auth, target_range) in enumerate(
        zip(
            interview_paths,
            interview_authorization_paths,
            interview_ranges,
            strict=True,
        ),
        1,
    ):
        assets.append(
            {
                "asset_id": f"interview-{index:03}",
                "source_type": "interview_clip",
                "source_file": artifact_reference(path, f"interview source {index}"),
                "primary_artifact": artifact_reference(path, f"interview clip {index}"),
                "authorization_artifact": artifact_reference(
                    auth, f"interview authorization {index}"
                ),
                "source_duration_s": 2.0,
                "source_in_s": 0.0,
                "source_out_s": 2.0,
                "target_ranges": [_time_range(*target_range)],
                "transcript_refs": [f"synthetic-vkp-transcript-{index:03}"],
                "evidence_refs": [f"synthetic-interview-evidence-{index:03}"],
                "authorization_status": "synthetic",
                "privacy_status": "synthetic_no_real_person",
                "fallback_asset_id": f"slide-{index:03}",
                "freshness_status": "synthetic_current",
            }
        )
    assets.extend(
        [
            {
                "asset_id": "avatar-placeholder-001",
                "source_type": "avatar_placeholder",
                "source_file": artifact_reference(
                    placeholder_path, "avatar placeholder source"
                ),
                "primary_artifact": artifact_reference(
                    placeholder_path, "avatar placeholder"
                ),
                "source_duration_s": 3.0,
                "target_ranges": [_time_range(0.0, 3.0), _time_range(13.0, 15.0)],
                "target_spoken_segment_ids": ["spoken-001", "spoken-002"],
                "avatar_route_selected": False,
                "actual_avatar_generated": False,
                "freshness_status": "synthetic_current",
                "authorization_status": "synthetic",
            },
            _simple_asset(
                "narration-audio-001",
                "narration_audio",
                narration_path,
                15.0,
                [_time_range(0.0, 15.0)],
            ),
            _simple_asset(
                "music-bed-001",
                "music",
                music_path,
                15.0,
                [_time_range(0.0, 15.0)],
            ),
            _simple_asset(
                "captions-001",
                "captions",
                subtitles_path,
                15.0,
                [_time_range(0.0, 15.0)],
            ),
        ]
    )
    manifest: dict[str, Any] = {
        "schema": DIGITAL_HUMAN_SOURCE_MANIFEST_SCHEMA,
        "feature_state": FEATURE_STATE,
        "source_project_plan": artifact_reference(plan_path, "project plan"),
        "spoken_script_review": artifact_reference(
            script_review_path, "spoken script review"
        ),
        "synthetic_only": True,
        "assets": assets,
        "interview_slots": _interview_slot_projection(assets),
        "source_order_policy": {
            "slides_preserve_page_order": True,
            "interviews_preserve_declared_ranges": True,
            "silent_reordering_allowed": False,
            "vkp_evidence_is_read_only": True,
        },
        "authority_boundary": {
            "avatar_route_selected": False,
            "actual_avatar_generated": False,
            "personal_media_used": False,
            "customer_media_used": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
    validate_digital_human_source_manifest(manifest)
    return manifest


def validate_digital_human_source_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != DIGITAL_HUMAN_SOURCE_MANIFEST_SCHEMA:
        raise ContractError("unsupported digital human source manifest schema")
    if manifest.get("manifest_sha256") != payload_sha256(manifest, "manifest_sha256"):
        raise ContractError("digital human source manifest integrity check failed")
    if manifest.get("feature_state") != FEATURE_STATE:
        raise ContractError("digital human foundation must remain paused")
    plan_path = validate_artifact_reference(
        _object(manifest.get("source_project_plan")), "project plan"
    )
    plan = read_json_object(plan_path, "content video project plan")
    validate_content_video_project_plan(plan)
    review_path = validate_artifact_reference(
        _object(manifest.get("spoken_script_review")), "spoken script review"
    )
    review = read_json_object(review_path, "spoken script review")
    validate_spoken_script_review(review)
    review_plan_path = validate_artifact_reference(
        _object(review.get("source_project_plan")), "spoken script project plan"
    )
    if review_plan_path != plan_path:
        raise ContractError("source manifest and spoken script use different plans")
    if manifest.get("synthetic_only") is not True:
        raise ContractError("this foundation route accepts synthetic sources only")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ContractError("digital human source manifest requires assets")
    ids: set[str] = set()
    slides: list[dict[str, Any]] = []
    interviews: list[dict[str, Any]] = []
    deck_paths: set[Path] = set()
    type_counts: dict[str, int] = {}
    for raw in assets:
        row = _object(raw)
        asset_id = str(row.get("asset_id") or "")
        source_type = str(row.get("source_type") or "")
        if not asset_id or asset_id in ids:
            raise ContractError("source manifest asset identities must be unique")
        ids.add(asset_id)
        type_counts[source_type] = type_counts.get(source_type, 0) + 1
        primary_path = validate_artifact_reference(
            _object(row.get("primary_artifact")), f"source asset {asset_id}"
        )
        if row.get("freshness_status") != "synthetic_current":
            raise ContractError(f"source asset is stale: {asset_id}")
        duration = _positive_number(row.get("source_duration_s"), "source duration")
        if source_type != "captions":
            measured_duration = _media_duration(primary_path)
            if abs(measured_duration - duration) > 0.05:
                raise ContractError(f"source asset duration drifted: {asset_id}")
        for target in row.get("target_ranges") or []:
            _validated_range(target, 15.0, f"{asset_id} target range")
        if source_type == "slide":
            slides.append(row)
            deck_paths.add(
                validate_artifact_reference(
                    _object(row.get("source_file")), f"slide source {asset_id}"
                )
            )
            if row.get("authorization_status") != "synthetic":
                raise ContractError("synthetic slide authorization is invalid")
        elif source_type == "interview_clip":
            interviews.append(row)
            source_path = validate_artifact_reference(
                _object(row.get("source_file")), f"interview source {asset_id}"
            )
            if source_path != primary_path:
                raise ContractError("interview projection changed its source identity")
            authorization_path = validate_artifact_reference(
                _object(row.get("authorization_artifact")),
                f"interview authorization {asset_id}",
            )
            authorization = read_json_object(
                authorization_path, f"interview authorization {asset_id}"
            )
            if (
                authorization.get("clip_id") != asset_id
                or authorization.get("authorization_status") != "synthetic"
                or authorization.get("privacy_status") != "synthetic_no_real_person"
                or authorization.get("real_person_depicted") is not False
                or authorization.get("customer_data_used") is not False
            ):
                raise ContractError("interview authorization sidecar is invalid")
            source_in = _nonnegative_number(row.get("source_in_s"), "interview in")
            source_out = _positive_number(row.get("source_out_s"), "interview out")
            if source_out <= source_in or source_out > duration + 0.001:
                raise ContractError("interview source range is out of bounds")
            if row.get("authorization_status") not in {"synthetic", "confirmed"}:
                raise ContractError("interview is not authorized")
            if row.get("privacy_status") != "synthetic_no_real_person":
                raise ContractError("interview privacy status is invalid")
            if not row.get("transcript_refs") or not row.get("evidence_refs"):
                raise ContractError("interview lacks VKP-style transcript evidence")
        elif source_type == "avatar_placeholder":
            if (
                row.get("avatar_route_selected") is not False
                or row.get("actual_avatar_generated") is not False
            ):
                raise ContractError("avatar placeholder cannot claim generated output")
        elif source_type not in {"narration_audio", "music", "captions"}:
            raise ContractError(
                f"unsupported source manifest asset type: {source_type}"
            )
    required_counts = {
        "slide": 3,
        "interview_clip": 2,
        "avatar_placeholder": 1,
        "narration_audio": 1,
        "music": 1,
        "captions": 1,
    }
    if any(type_counts.get(key) != count for key, count in required_counts.items()):
        raise ContractError("source manifest asset composition is incomplete")
    expected_slots = _interview_slot_projection(assets)
    if manifest.get("interview_slots") != expected_slots:
        raise ContractError("interview slots drifted from their source assets")
    ordered_pages = [row.get("page_number") for row in slides]
    ordered_identity = [row.get("original_order") for row in slides]
    if ordered_pages != [1, 2, 3] or ordered_identity != [1, 2, 3]:
        raise ContractError("PPT pages are missing, duplicated, or reordered")
    if len(deck_paths) != 1:
        raise ContractError("PPT pages must preserve one source deck identity")
    deck = read_json_object(next(iter(deck_paths)), "synthetic PPT source")
    if (
        deck.get("schema") != "video_creation_pipeline.synthetic_ppt_source.v1"
        or deck.get("page_count") != 3
        or [_object(row).get("page_number") for row in deck.get("pages") or []]
        != [1, 2, 3]
        or deck.get("contains_real_person_or_customer_data") is not False
    ):
        raise ContractError("PPT source deck page identity is invalid")
    assets_by_id = {
        str(_object(row).get("asset_id") or ""): _object(row) for row in assets
    }
    for interview in interviews:
        fallback = assets_by_id.get(str(interview.get("fallback_asset_id") or ""))
        if fallback is None or fallback.get("source_type") != "slide":
            raise ContractError("interview fallback must bind an existing slide")
    boundary = _object(manifest.get("authority_boundary"))
    if any(
        boundary.get(field) is not False
        for field in (
            "avatar_route_selected",
            "actual_avatar_generated",
            "personal_media_used",
            "customer_media_used",
            "external_provider_called",
            "upload_performed",
            "publish_performed",
        )
    ):
        raise ContractError("source manifest authority boundary is invalid")


def build_digital_human_candidate_timeline(
    source_manifest_path: str | Path,
    script_review_path: str | Path,
    tts_job_path: str | Path,
    audio_qa_path: str | Path,
) -> dict[str, Any]:
    manifest_path = Path(source_manifest_path).expanduser().resolve()
    manifest = read_json_object(manifest_path, "digital human source manifest")
    validate_digital_human_source_manifest(manifest)
    review_path = Path(script_review_path).expanduser().resolve()
    review = read_json_object(review_path, "spoken script review")
    validate_spoken_script_review(review)
    assets = {row["asset_id"]: row for row in manifest["assets"]}
    visual_rows = [
        ("avatar_placeholder", "avatar-placeholder-001", 0.0, 3.0, 0.0, 3.0),
        ("slides", "slide-001", 3.0, 5.0, 0.0, 2.0),
        ("interview", "interview-001", 5.0, 7.0, 0.0, 2.0),
        ("slides", "slide-002", 7.0, 9.0, 0.0, 2.0),
        ("interview", "interview-002", 9.0, 11.0, 0.0, 2.0),
        ("slides", "slide-003", 11.0, 13.0, 0.0, 2.0),
        ("avatar_placeholder", "avatar-placeholder-001", 13.0, 15.0, 0.0, 2.0),
    ]
    clips: list[dict[str, Any]] = []
    for index, (track, asset_id, start, end, source_in, source_out) in enumerate(
        visual_rows, 1
    ):
        clips.append(
            _timeline_clip(
                f"visual-{index:03}",
                track,
                assets[asset_id],
                start,
                end,
                source_in,
                source_out,
                transition="cut",
                audio_behavior=(
                    "source_audio_muted_real_interview_requires_ducking_review"
                    if track == "interview"
                    else "source_audio_muted"
                ),
            )
        )
    clips.extend(
        [
            _timeline_clip(
                "narration-001",
                "narration_audio",
                assets["narration-audio-001"],
                0.0,
                15.0,
                0.0,
                15.0,
                transition="none",
                audio_behavior="primary_narration",
            ),
            _timeline_clip(
                "captions-001",
                "captions",
                assets["captions-001"],
                0.0,
                15.0,
                0.0,
                15.0,
                transition="none",
                audio_behavior="not_applicable",
            ),
            _timeline_clip(
                "music-001",
                "music",
                assets["music-bed-001"],
                0.0,
                15.0,
                0.0,
                15.0,
                transition="none",
                audio_behavior="duck_under_narration_minus_18db",
            ),
        ]
    )
    timeline: dict[str, Any] = {
        "schema": DIGITAL_HUMAN_TIMELINE_SCHEMA,
        "feature_state": FEATURE_STATE,
        "source_manifest": artifact_reference(manifest_path, "source manifest"),
        "spoken_script_review": artifact_reference(review_path, "spoken script review"),
        "tts_job": artifact_reference(tts_job_path, "TTS job"),
        "audio_stitch_qa": artifact_reference(audio_qa_path, "audio stitch QA"),
        "total_duration_s": 15.0,
        "tracks": sorted(_REQUIRED_TRACKS),
        "clips": clips,
        "subtitle_binding": {
            "candidate_spoken_text_sha256": review["candidate_spoken_text_sha256"],
            "caption_asset_id": "captions-001",
        },
        "layout_safety": {
            "priority": ["evidence", "avatar", "captions", "semantic_cards"],
            "caption_safe_region": {
                "x": 0.05,
                "y": 0.78,
                "width": 0.90,
                "height": 0.17,
            },
            "decorations_removed_before_evidence_or_faces": True,
            "source_review_commit": "7065a5c1e84729ca8c41d6815d1490a039bc3fb0",
        },
        "avatar_adapter_interface": _avatar_adapter_interface(clips, assets, review),
        "readiness": {
            "status": FOUNDATION_STATUS,
            "production_render_eligible": False,
            "avatar_adapter_required": True,
            "avatar_slot_ids": ["avatar-placeholder-001"],
            "manual_review_required": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "avatar_route_selected": False,
            "actual_avatar_generated": False,
            "approval_record_created": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "mutates_vkp_timeline": False,
        },
    }
    timeline["timeline_sha256"] = payload_sha256(timeline, "timeline_sha256")
    validate_digital_human_candidate_timeline(timeline)
    return timeline


def validate_digital_human_candidate_timeline(timeline: dict[str, Any]) -> None:
    if timeline.get("schema") != DIGITAL_HUMAN_TIMELINE_SCHEMA:
        raise ContractError("unsupported digital human candidate timeline schema")
    if timeline.get("timeline_sha256") != payload_sha256(timeline, "timeline_sha256"):
        raise ContractError("digital human candidate timeline integrity check failed")
    manifest_path = validate_artifact_reference(
        _object(timeline.get("source_manifest")), "source manifest"
    )
    manifest = read_json_object(manifest_path, "source manifest")
    validate_digital_human_source_manifest(manifest)
    review_path = validate_artifact_reference(
        _object(timeline.get("spoken_script_review")), "spoken script review"
    )
    review = read_json_object(review_path, "spoken script review")
    validate_spoken_script_review(review)
    if (
        validate_artifact_reference(
            _object(manifest.get("spoken_script_review")), "source spoken script review"
        )
        != review_path
    ):
        raise ContractError("timeline and source manifest use different scripts")
    validate_artifact_reference(_object(timeline.get("tts_job")), "TTS job")
    qa_path = validate_artifact_reference(
        _object(timeline.get("audio_stitch_qa")), "audio stitch QA"
    )
    qa = read_json_object(qa_path, "audio stitch QA")
    if qa.get("readiness", {}).get("ready") is not True:
        raise ContractError("timeline requires passing existing audio stitch QA")
    if set(timeline.get("tracks") or []) != _REQUIRED_TRACKS:
        raise ContractError("candidate timeline track set is incomplete")
    total = _positive_number(timeline.get("total_duration_s"), "timeline duration")
    assets = {row["asset_id"]: row for row in manifest["assets"]}
    clips = timeline.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ContractError("candidate timeline requires clips")
    track_rows: dict[str, list[dict[str, Any]]] = {}
    ids: set[str] = set()
    for raw in clips:
        row = _object(raw)
        clip_id = str(row.get("clip_id") or "")
        track = str(row.get("track") or "")
        if not clip_id or clip_id in ids:
            raise ContractError("timeline clip identities must be unique")
        if track not in _REQUIRED_TRACKS:
            raise ContractError("timeline clip uses an unknown track")
        ids.add(clip_id)
        track_rows.setdefault(track, []).append(row)
        start, end = _validated_range(row, total, f"timeline clip {clip_id}")
        asset = assets.get(str(row.get("source_asset_id") or ""))
        if asset is None:
            raise ContractError("timeline clip source is missing from manifest")
        if row.get("source_sha256") != asset["primary_artifact"]["sha256"]:
            raise ContractError("timeline clip source hash drifted")
        source_in = _nonnegative_number(row.get("source_in_s"), "source in")
        source_out = _positive_number(row.get("source_out_s"), "source out")
        if (
            source_out <= source_in
            or source_out > float(asset["source_duration_s"]) + 0.001
            or abs((source_out - source_in) - (end - start)) > 0.001
        ):
            raise ContractError("timeline clip source range is invalid")
    for track, rows in track_rows.items():
        ordered = sorted(rows, key=lambda row: float(row["start_s"]))
        previous_end = -1.0
        for row in ordered:
            if float(row["start_s"]) < previous_end - 0.001:
                raise ContractError(f"timeline track overlaps: {track}")
            previous_end = float(row["end_s"])
    visual = sorted(
        [
            row
            for track in ("avatar_placeholder", "slides", "interview")
            for row in track_rows.get(track, [])
        ],
        key=lambda row: float(row["start_s"]),
    )
    cursor = 0.0
    for row in visual:
        if abs(float(row["start_s"]) - cursor) > 0.001:
            raise ContractError("visual timeline has a gap or overlap")
        cursor = float(row["end_s"])
    if abs(cursor - total) > 0.001:
        raise ContractError("visual timeline total duration drifted")
    captions = assets.get("captions-001")
    if captions is None:
        raise ContractError("caption asset is missing")
    caption_path = validate_artifact_reference(captions["primary_artifact"], "captions")
    caption_hash = canonical_json_sha256(_srt_text_rows(caption_path))
    binding = _object(timeline.get("subtitle_binding"))
    if binding.get("candidate_spoken_text_sha256") != review.get(
        "candidate_spoken_text_sha256"
    ) or caption_hash != review.get("candidate_spoken_text_sha256"):
        raise ContractError("subtitle text drifted from the spoken script")
    readiness = _object(timeline.get("readiness"))
    boundary = _object(timeline.get("authority_boundary"))
    expected_adapter = _avatar_adapter_interface(clips, assets, review)
    if (
        timeline.get("avatar_adapter_interface") != expected_adapter
        or timeline.get("feature_state") != FEATURE_STATE
        or expected_adapter.get("status") != "route_unselected"
        or readiness.get("status") != FOUNDATION_STATUS
        or readiness.get("production_render_eligible") is not False
        or readiness.get("avatar_adapter_required") is not True
        or boundary.get("candidate_only") is not True
        or boundary.get("avatar_route_selected") is not False
        or boundary.get("actual_avatar_generated") is not False
        or boundary.get("approval_record_created") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
    ):
        raise ContractError("candidate timeline authority boundary is invalid")


def build_digital_human_candidate_edl(timeline_path: str | Path) -> dict[str, Any]:
    source = Path(timeline_path).expanduser().resolve()
    timeline = read_json_object(source, "candidate timeline")
    validate_digital_human_candidate_timeline(timeline)
    visual_tracks = {"avatar_placeholder", "slides", "interview"}
    edl: dict[str, Any] = {
        "schema": DIGITAL_HUMAN_EDL_SCHEMA,
        "feature_state": FEATURE_STATE,
        "source_timeline": artifact_reference(source, "candidate timeline"),
        "source_timeline_sha256": timeline["timeline_sha256"],
        "total_duration_s": timeline["total_duration_s"],
        "ranges": [
            {
                "clip_id": row["clip_id"],
                "track": row["track"],
                "source_asset_id": row["source_asset_id"],
                "source_sha256": row["source_sha256"],
                "source_start_s": row["source_in_s"],
                "source_end_s": row["source_out_s"],
                "output_start_s": row["start_s"],
                "output_end_s": row["end_s"],
                "transition": row["transition"],
                "audio_behavior": row["audio_behavior"],
                "material_status": (
                    "avatar_placeholder_route_unselected"
                    if row["track"] == "avatar_placeholder"
                    else "synthetic_fixture"
                ),
            }
            for row in timeline["clips"]
            if row["track"] in visual_tracks
        ],
        "readiness": {
            "synthetic_candidate_render_ready": True,
            "production_execution_ready": False,
            "blocking_reason": "avatar_renderer_adapter_route_unselected",
        },
        "authority_boundary": {
            "candidate_only": True,
            "derived_projection_only": True,
            "independent_edit_allowed": False,
            "second_edit_state_created": False,
            "mutates_vkp_timeline": False,
            "approval_record_created": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    edl["edl_sha256"] = payload_sha256(edl, "edl_sha256")
    validate_digital_human_candidate_edl(edl)
    return edl


def validate_digital_human_candidate_edl(edl: dict[str, Any]) -> None:
    if edl.get("schema") != DIGITAL_HUMAN_EDL_SCHEMA:
        raise ContractError("unsupported digital human candidate EDL schema")
    if edl.get("edl_sha256") != payload_sha256(edl, "edl_sha256"):
        raise ContractError("digital human candidate EDL integrity check failed")
    timeline_path = validate_artifact_reference(
        _object(edl.get("source_timeline")), "candidate timeline"
    )
    timeline = read_json_object(timeline_path, "candidate timeline")
    validate_digital_human_candidate_timeline(timeline)
    expected = build_digital_human_candidate_edl_projection(timeline)
    if (
        edl.get("source_timeline_sha256") != timeline.get("timeline_sha256")
        or edl.get("total_duration_s") != timeline.get("total_duration_s")
        or edl.get("ranges") != expected
    ):
        raise ContractError("candidate EDL drifted from its source timeline")
    readiness = _object(edl.get("readiness"))
    boundary = _object(edl.get("authority_boundary"))
    if (
        edl.get("feature_state") != FEATURE_STATE
        or readiness.get("synthetic_candidate_render_ready") is not True
        or readiness.get("production_execution_ready") is not False
        or readiness.get("blocking_reason")
        != "avatar_renderer_adapter_route_unselected"
        or boundary.get("candidate_only") is not True
        or boundary.get("derived_projection_only") is not True
        or boundary.get("independent_edit_allowed") is not False
        or boundary.get("second_edit_state_created") is not False
        or boundary.get("mutates_vkp_timeline") is not False
        or boundary.get("approval_record_created") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
    ):
        raise ContractError("candidate EDL authority boundary is invalid")


def build_digital_human_candidate_edl_projection(
    timeline: dict[str, Any],
) -> list[dict[str, Any]]:
    visual_tracks = {"avatar_placeholder", "slides", "interview"}
    return [
        {
            "clip_id": row["clip_id"],
            "track": row["track"],
            "source_asset_id": row["source_asset_id"],
            "source_sha256": row["source_sha256"],
            "source_start_s": row["source_in_s"],
            "source_end_s": row["source_out_s"],
            "output_start_s": row["start_s"],
            "output_end_s": row["end_s"],
            "transition": row["transition"],
            "audio_behavior": row["audio_behavior"],
            "material_status": (
                "avatar_placeholder_route_unselected"
                if row["track"] == "avatar_placeholder"
                else "synthetic_fixture"
            ),
        }
        for row in timeline["clips"]
        if row["track"] in visual_tracks
    ]


def build_digital_human_render_manifest(
    timeline_path: str | Path,
    candidate_edl_path: str | Path,
    candidate_path: str | Path,
    subtitles_path: str | Path,
    qa_report_path: str | Path,
    receipt_paths: list[Path],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": DIGITAL_HUMAN_RENDER_MANIFEST_SCHEMA,
        "status": FOUNDATION_STATUS,
        "feature_state": FEATURE_STATE,
        "candidate_only": True,
        "candidate_timeline": artifact_reference(timeline_path, "candidate timeline"),
        "candidate_edl": artifact_reference(candidate_edl_path, "candidate EDL"),
        "candidate_video": artifact_reference(candidate_path, "candidate video"),
        "subtitles": artifact_reference(subtitles_path, "candidate subtitles"),
        "technical_qa": artifact_reference(qa_report_path, "technical QA"),
        "ffmpeg_receipts": [
            artifact_reference(path, f"FFmpeg receipt {index}")
            for index, path in enumerate(receipt_paths, 1)
        ],
        "digital_human_capability": {
            "avatar_route_selected": False,
            "actual_avatar_generated": False,
            "lip_sync_status": "not_tested_route_unselected",
            "face_anomaly_status": "not_applicable_placeholder",
            "voice_identity_status": "not_tested_personal_voice_unconfigured",
        },
        "authority_boundary": {
            "production_success_claimed": False,
            "release_eligible": False,
            "approval_record_created": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "single_ffmpeg_outlet_used": True,
        },
    }
    manifest["render_manifest_sha256"] = payload_sha256(
        manifest, "render_manifest_sha256"
    )
    validate_digital_human_render_manifest(manifest)
    return manifest


def validate_digital_human_render_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != DIGITAL_HUMAN_RENDER_MANIFEST_SCHEMA:
        raise ContractError("unsupported digital human render manifest schema")
    if manifest.get("render_manifest_sha256") != payload_sha256(
        manifest, "render_manifest_sha256"
    ):
        raise ContractError("digital human render manifest integrity check failed")
    timeline_path = validate_artifact_reference(
        _object(manifest.get("candidate_timeline")), "candidate timeline"
    )
    validate_digital_human_candidate_timeline(
        read_json_object(timeline_path, "candidate timeline")
    )
    edl_path = validate_artifact_reference(
        _object(manifest.get("candidate_edl")), "candidate EDL"
    )
    edl = read_json_object(edl_path, "candidate EDL")
    validate_digital_human_candidate_edl(edl)
    if (
        validate_artifact_reference(
            _object(edl.get("source_timeline")), "EDL source timeline"
        )
        != timeline_path
    ):
        raise ContractError("render manifest timeline and EDL drifted")
    validate_artifact_reference(
        _object(manifest.get("candidate_video")), "candidate video"
    )
    validate_artifact_reference(
        _object(manifest.get("subtitles")), "candidate subtitles"
    )
    qa_path = validate_artifact_reference(
        _object(manifest.get("technical_qa")), "technical QA"
    )
    qa = read_json_object(qa_path, "technical QA")
    validate_technical_qa_report(qa)
    if qa.get("readiness", {}).get("technical_ready") is not True:
        raise ContractError("foundation candidate failed technical QA")
    receipts = manifest.get("ffmpeg_receipts")
    if not isinstance(receipts, list) or not receipts:
        raise ContractError("render manifest requires FFmpeg receipts")
    for index, reference in enumerate(receipts, 1):
        receipt_path = validate_artifact_reference(
            _object(reference), f"FFmpeg receipt {index}"
        )
        validate_ffmpeg_execution_receipt(
            read_json_object(receipt_path, f"FFmpeg receipt {index}")
        )
    capability = _object(manifest.get("digital_human_capability"))
    expected_capability = {
        "avatar_route_selected": False,
        "actual_avatar_generated": False,
        "lip_sync_status": "not_tested_route_unselected",
        "face_anomaly_status": "not_applicable_placeholder",
        "voice_identity_status": "not_tested_personal_voice_unconfigured",
    }
    if capability != expected_capability:
        raise ContractError("unselected avatar capabilities cannot be marked as tested")
    boundary = _object(manifest.get("authority_boundary"))
    if (
        manifest.get("status") != FOUNDATION_STATUS
        or manifest.get("feature_state") != FEATURE_STATE
        or manifest.get("candidate_only") is not True
        or boundary.get("production_success_claimed") is not False
        or boundary.get("release_eligible") is not False
        or boundary.get("approval_record_created") is not False
        or boundary.get("external_provider_called") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
        or boundary.get("single_ffmpeg_outlet_used") is not True
    ):
        raise ContractError("render manifest authority boundary is invalid")


def build_digital_human_review_packet(
    script_review_path: str | Path,
    source_manifest_path: str | Path,
    timeline_path: str | Path,
    candidate_edl_path: str | Path,
    render_manifest_path: str | Path,
    tts_paths: dict[str, Path],
) -> dict[str, Any]:
    script = read_json_object(script_review_path, "spoken script review")
    validate_spoken_script_review(script)
    source = read_json_object(source_manifest_path, "source manifest")
    validate_digital_human_source_manifest(source)
    timeline = read_json_object(timeline_path, "candidate timeline")
    validate_digital_human_candidate_timeline(timeline)
    edl = read_json_object(candidate_edl_path, "candidate EDL")
    validate_digital_human_candidate_edl(edl)
    render = read_json_object(render_manifest_path, "render manifest")
    validate_digital_human_render_manifest(render)
    packet: dict[str, Any] = {
        "schema": DIGITAL_HUMAN_REVIEW_PACKET_SCHEMA,
        "status": "awaiting_human_review",
        "feature_state": FEATURE_STATE,
        "inputs": {
            "spoken_script_review": artifact_reference(
                script_review_path, "spoken script review"
            ),
            "source_manifest": artifact_reference(
                source_manifest_path, "source manifest"
            ),
            "candidate_timeline": artifact_reference(
                timeline_path, "candidate timeline"
            ),
            "candidate_edl": artifact_reference(candidate_edl_path, "candidate EDL"),
            "render_manifest": artifact_reference(
                render_manifest_path, "render manifest"
            ),
            "tts_job": artifact_reference(tts_paths["job"], "TTS job"),
            "tts_receipts": artifact_reference(tts_paths["receipts"], "TTS receipts"),
            "tts_resume_plan": artifact_reference(
                tts_paths["resume"], "TTS resume plan"
            ),
            "audio_stitch_qa": artifact_reference(
                tts_paths["audio_qa"], "audio stitch QA"
            ),
        },
        "review_sections": {
            "script_diff": [
                {
                    "segment_id": row["segment_id"],
                    "original_text": row["original_text"],
                    "candidate_spoken_text": row["candidate_spoken_text"],
                    "changes": list(row["changes"]),
                    "meaning_preserved_candidate": True,
                }
                for row in script["segments"]
            ],
            "script_subtitle_audio_consistency": {
                "script_text_sha256": script["candidate_spoken_text_sha256"],
                "subtitle_text_sha256": timeline["subtitle_binding"][
                    "candidate_spoken_text_sha256"
                ],
                "audio_stitch_qa_ready": True,
                "personal_voice_claimed": False,
            },
            "slides": {
                "page_order": [1, 2, 3],
                "source_integrity_checked": True,
                "manual_visual_review_required": True,
            },
            "interviews": {
                "clip_count": 2,
                "authorization": "synthetic",
                "privacy": "synthetic_no_real_person",
                "vkp_evidence_role": "read_only_fixture_projection",
                "manual_context_review_required": True,
            },
            "digital_human": {
                "avatar_route_selected": False,
                "actual_avatar_generated": False,
                "lip_sync_status": "not_tested_route_unselected",
                "face_anomaly_status": "not_applicable_placeholder",
                "voice_identity_status": "not_tested_personal_voice_unconfigured",
            },
        },
        "human_decisions_required": [
            "confirm_spoken_script_meaning",
            "confirm_slide_and_interview_timing",
            "select_avatar_renderer_adapter",
            "review_future_lip_sync_face_and_personal_voice_results",
            "record_separate_approval_records_before_release",
        ],
        "rejected_claims": [
            "avatar_generated",
            "lip_sync_passed",
            "personal_voice_verified",
            "face_quality_passed",
            "release_ready",
        ],
        "available_actions": ["inspect", "request_revision", "record_manual_review"],
        "existing_gate_handoff": _existing_gate_handoff(),
        "authority_boundary": {
            "review_packet_is_approval": False,
            "approval_record_created": False,
            "automatic_approval": False,
            "release_eligible": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    packet["review_packet_sha256"] = payload_sha256(packet, "review_packet_sha256")
    validate_digital_human_review_packet(packet)
    return packet


def validate_digital_human_review_packet(packet: dict[str, Any]) -> None:
    if packet.get("schema") != DIGITAL_HUMAN_REVIEW_PACKET_SCHEMA:
        raise ContractError("unsupported digital human review packet schema")
    if packet.get("review_packet_sha256") != payload_sha256(
        packet, "review_packet_sha256"
    ):
        raise ContractError("digital human review packet integrity check failed")
    inputs = _object(packet.get("inputs"))
    required_inputs = {
        "spoken_script_review",
        "source_manifest",
        "candidate_timeline",
        "candidate_edl",
        "render_manifest",
        "tts_job",
        "tts_receipts",
        "tts_resume_plan",
        "audio_stitch_qa",
    }
    if set(inputs) != required_inputs:
        raise ContractError("digital human review packet inputs are incomplete")
    resolved = {
        key: validate_artifact_reference(_object(inputs[key]), key)
        for key in required_inputs
    }
    script = read_json_object(resolved["spoken_script_review"], "spoken script review")
    validate_spoken_script_review(script)
    source = read_json_object(resolved["source_manifest"], "source manifest")
    validate_digital_human_source_manifest(source)
    timeline = read_json_object(resolved["candidate_timeline"], "candidate timeline")
    validate_digital_human_candidate_timeline(timeline)
    edl = read_json_object(resolved["candidate_edl"], "candidate EDL")
    validate_digital_human_candidate_edl(edl)
    render = read_json_object(resolved["render_manifest"], "render manifest")
    validate_digital_human_render_manifest(render)
    if (
        validate_artifact_reference(
            _object(source.get("spoken_script_review")), "source spoken script review"
        )
        != resolved["spoken_script_review"]
        or validate_artifact_reference(
            _object(timeline.get("source_manifest")), "timeline source manifest"
        )
        != resolved["source_manifest"]
        or validate_artifact_reference(
            _object(timeline.get("spoken_script_review")), "timeline spoken script"
        )
        != resolved["spoken_script_review"]
        or validate_artifact_reference(
            _object(render.get("candidate_timeline")), "render candidate timeline"
        )
        != resolved["candidate_timeline"]
        or validate_artifact_reference(
            _object(edl.get("source_timeline")), "EDL source timeline"
        )
        != resolved["candidate_timeline"]
        or validate_artifact_reference(
            _object(render.get("candidate_edl")), "render candidate EDL"
        )
        != resolved["candidate_edl"]
    ):
        raise ContractError("review packet inputs do not form one artifact generation")
    tts_job = read_json_object(resolved["tts_job"], "TTS job")
    tts_receipts = read_json_object(resolved["tts_receipts"], "TTS receipts")
    expected_resume = build_resume_plan(tts_job, tts_receipts)
    stored_resume = read_json_object(resolved["tts_resume_plan"], "TTS resume plan")
    if (
        stored_resume != expected_resume
        or expected_resume.get("summary", {}).get("status") != "complete"
    ):
        raise ContractError("TTS resume plan drifted from the existing TTS contracts")
    expected_audio_qa = build_audio_stitch_qa(tts_job, tts_receipts)
    stored_audio_qa = read_json_object(resolved["audio_stitch_qa"], "audio stitch QA")
    if (
        stored_audio_qa != expected_audio_qa
        or expected_audio_qa.get("readiness", {}).get("ready") is not True
    ):
        raise ContractError("audio stitch QA drifted from the existing TTS contracts")
    sections = _object(packet.get("review_sections"))
    consistency = _object(sections.get("script_subtitle_audio_consistency"))
    if (
        consistency.get("script_text_sha256")
        != script.get("candidate_spoken_text_sha256")
        or consistency.get("subtitle_text_sha256")
        != script.get("candidate_spoken_text_sha256")
        or consistency.get("audio_stitch_qa_ready") is not True
        or consistency.get("personal_voice_claimed") is not False
    ):
        raise ContractError("review packet script, subtitle, or audio binding drifted")
    capability = _object(sections.get("digital_human"))
    if capability != render.get("digital_human_capability"):
        raise ContractError("review packet digital human status drifted")
    actions = set(packet.get("available_actions") or [])
    if actions & _PROHIBITED_ACTIONS:
        raise ContractError("review packet contains a prohibited action")
    if packet.get("existing_gate_handoff") != _existing_gate_handoff():
        raise ContractError("review packet existing gate handoff drifted")
    boundary = _object(packet.get("authority_boundary"))
    if (
        packet.get("status") != "awaiting_human_review"
        or packet.get("feature_state") != FEATURE_STATE
        or boundary.get("review_packet_is_approval") is not False
        or boundary.get("approval_record_created") is not False
        or boundary.get("automatic_approval") is not False
        or boundary.get("release_eligible") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
    ):
        raise ContractError("digital human review packet authority boundary is invalid")


def validate_digital_human_foundation_bundle(
    plan_root: str | Path,
) -> dict[str, Any]:
    root = Path(plan_root).expanduser().resolve()
    contract_dir = root / "digital-human-foundation" / "contracts"
    source_path = contract_dir / "source-manifest.json"
    timeline_path = contract_dir / "candidate-timeline.json"
    edl_path = contract_dir / "candidate-edl.json"
    render_path = contract_dir / "render-manifest.json"
    review_path = contract_dir / "human-review-packet.json"
    receipt_path = contract_dir / "foundation-run-receipt.json"
    source = read_json_object(source_path, "source manifest")
    validate_digital_human_source_manifest(source)
    timeline = read_json_object(timeline_path, "candidate timeline")
    validate_digital_human_candidate_timeline(timeline)
    edl = read_json_object(edl_path, "candidate EDL")
    validate_digital_human_candidate_edl(edl)
    render = read_json_object(render_path, "render manifest")
    validate_digital_human_render_manifest(render)
    review = read_json_object(review_path, "review packet")
    validate_digital_human_review_packet(review)
    receipt = read_json_object(receipt_path, "foundation run receipt")
    if receipt.get("schema") != DIGITAL_HUMAN_RUN_RECEIPT_SCHEMA:
        raise ContractError("unsupported foundation run receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("foundation run receipt integrity check failed")
    if receipt.get("status") != FOUNDATION_STATUS:
        raise ContractError("foundation run receipt status is invalid")
    if receipt.get("feature_state") != FEATURE_STATE:
        raise ContractError("digital human foundation must remain paused")
    if not str(receipt.get("dispatch_id") or "").strip():
        raise ContractError("foundation run receipt dispatch_id is required")
    artifact_rows = _object(receipt.get("artifacts"))
    expected_artifacts = {
        "spoken_script_review": contract_dir / "spoken-script-review.json",
        "source_manifest": source_path,
        "candidate_timeline": timeline_path,
        "candidate_edl": edl_path,
        "review_packet": review_path,
        "candidate_video": root
        / "digital-human-foundation"
        / "render"
        / "digital-human-foundation-candidate.mp4",
        "render_manifest": render_path,
        "technical_qa": root
        / "digital-human-foundation"
        / "qa"
        / "technical-qa-report.json",
    }
    if set(artifact_rows) != set(expected_artifacts):
        raise ContractError("foundation run receipt artifact set is incomplete")
    for key, reference in artifact_rows.items():
        resolved = validate_artifact_reference(
            _object(reference), f"run artifact {key}"
        )
        if resolved != expected_artifacts[key].resolve():
            raise ContractError("foundation run receipt binds an unexpected artifact")
    identity = _object(receipt.get("deterministic_identity"))
    if (
        identity.get("source_manifest_sha256") != source["manifest_sha256"]
        or identity.get("timeline_sha256") != timeline["timeline_sha256"]
        or identity.get("edl_sha256") != edl["edl_sha256"]
        or identity.get("candidate_video_sha256") != render["candidate_video"]["sha256"]
        or identity.get("script_text_sha256")
        != read_json_object(
            contract_dir / "spoken-script-review.json", "spoken script review"
        ).get("candidate_spoken_text_sha256")
    ):
        raise ContractError("foundation run deterministic identity drifted")
    if receipt.get("digital_human_capability") != render.get(
        "digital_human_capability"
    ):
        raise ContractError("foundation run digital human capability drifted")
    boundary = _object(receipt.get("authority_boundary"))
    if (
        boundary.get("candidate_only") is not True
        or boundary.get("manual_review_required") is not True
        or boundary.get("approval_record_created") is not False
        or boundary.get("release_eligible") is not False
        or boundary.get("network_used") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
        or boundary.get("personal_media_used") is not False
        or boundary.get("customer_media_used") is not False
        or boundary.get("second_ffmpeg_orchestrator_created") is not False
        or boundary.get("second_tts_state_created") is not False
    ):
        raise ContractError("foundation run receipt authority boundary is invalid")
    return {
        "schema": "video_creation_pipeline.digital_human_foundation_validation.v1",
        "status": FOUNDATION_STATUS,
        "feature_state": FEATURE_STATE,
        "valid": True,
        "source_manifest_sha256": source["manifest_sha256"],
        "timeline_sha256": timeline["timeline_sha256"],
        "edl_sha256": edl["edl_sha256"],
        "candidate_video_sha256": render["candidate_video"]["sha256"],
        "review_packet_sha256": review["review_packet_sha256"],
        "candidate_only": True,
        "avatar_adapter_required": True,
    }


def _validate_spoken_script_source(source: dict[str, Any], plan_path: Path) -> None:
    if source.get("schema") != SPOKEN_SCRIPT_SOURCE_SCHEMA:
        raise ContractError("unsupported spoken script source schema")
    if source.get("source_sha256") != payload_sha256(source, "source_sha256"):
        raise ContractError("spoken script source integrity check failed")
    if (
        validate_artifact_reference(
            _object(source.get("source_project_plan")), "project plan"
        )
        != plan_path
    ):
        raise ContractError("spoken script source binds a different project plan")
    segments = source.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ContractError("spoken script source requires segments")
    ids: set[str] = set()
    for raw in segments:
        row = _object(raw)
        segment_id = str(row.get("segment_id") or "")
        if not segment_id or segment_id in ids:
            raise ContractError("spoken script segment ids must be unique")
        ids.add(segment_id)
        if not str(row.get("original_text") or "").strip() or not row.get(
            "evidence_refs"
        ):
            raise ContractError("spoken script segment lacks text or evidence")
    boundary = _object(source.get("authority_boundary"))
    if (
        source.get("synthetic_only") is not True
        or boundary.get("new_fact_without_evidence_allowed") is not False
        or boundary.get("free_rewrite_allowed") is not False
        or boundary.get("manual_review_required") is not True
    ):
        raise ContractError("spoken script source boundary is invalid")


def _write_script_source(plan_path: Path, fixture_dir: Path) -> Path:
    source: dict[str, Any] = {
        "schema": SPOKEN_SCRIPT_SOURCE_SCHEMA,
        "source_project_plan": artifact_reference(plan_path, "project plan"),
        "synthetic_only": True,
        "segments": [
            {
                "segment_id": "spoken-001",
                "original_text": "嗯，2026-08-12的合成QA进度为100%，API按字母读。",
                "evidence_refs": [
                    {"ref_type": "synthetic_fixture", "ref_id": "foundation-spec-001"}
                ],
                "term_rules": [],
                "pronunciation_notes": [
                    "日期完整朗读",
                    "百分比完整朗读",
                    "API逐字母朗读",
                ],
                "pause_and_emphasis": {"pause_after_ms": 360, "emphasis": ["100%"]},
                "subtitle_breaks": ["2026-08-12的合成QA进度为100%，", "API按字母读。"],
            },
            {
                "segment_id": "spoken-002",
                "original_text": "呃，统一输出只纳入已审核且非AI合成的支持证据。",
                "evidence_refs": [
                    {"ref_type": "claim", "ref_id": "claim_fixture_contract"},
                    {"ref_type": "evidence", "ref_id": "evidence_fixture_support"},
                ],
                "term_rules": [{"find": "AI", "replace": "A I"}],
                "pronunciation_notes": ["AI逐字母朗读"],
                "pause_and_emphasis": {"pause_after_ms": 0, "emphasis": ["已审核"]},
                "subtitle_breaks": ["统一输出只纳入已审核且非AI合成的支持证据。"],
            },
        ],
        "authority_boundary": {
            "new_fact_without_evidence_allowed": False,
            "free_rewrite_allowed": False,
            "manual_review_required": True,
        },
    }
    source["source_sha256"] = payload_sha256(source, "source_sha256")
    return atomic_write_json(source, fixture_dir / "spoken-script-source.json")


def _write_synthetic_deck(fixture_dir: Path) -> Path:
    payload = {
        "schema": "video_creation_pipeline.synthetic_ppt_source.v1",
        "deck_id": "synthetic-deck-001",
        "page_count": 3,
        "pages": [
            {"page_number": index, "title": f"Synthetic evidence page {index}"}
            for index in range(1, 4)
        ],
        "contains_real_person_or_customer_data": False,
    }
    return atomic_write_json(payload, fixture_dir / "synthetic-ppt-source.json")


def _write_synthetic_authorizations(fixture_dir: Path) -> list[Path]:
    paths = []
    for index in range(1, 3):
        payload = {
            "schema": "video_creation_pipeline.synthetic_interview_authorization.v1",
            "clip_id": f"interview-{index:03}",
            "authorization_status": "synthetic",
            "privacy_status": "synthetic_no_real_person",
            "real_person_depicted": False,
            "customer_data_used": False,
        }
        paths.append(
            atomic_write_json(
                payload, fixture_dir / f"interview-{index:03}-authorization.json"
            )
        )
    return paths


def _build_synthetic_tts(
    root: Path,
    plan: dict[str, Any],
    script_review_path: Path,
    script_review: dict[str, Any],
    *,
    timeout_s: float,
) -> tuple[dict[str, Path], Path, list[Path]]:
    contract_dir = root / "digital-human-foundation" / "contracts"
    media_dir = root / "digital-human-foundation" / "media"
    receipt_dir = root / "digital-human-foundation" / "ffmpeg-receipts"
    duration_per_segment = 15.0 / len(script_review["segments"])
    recipe = normalize_reference_recipe(
        {
            "schema": "video_creation_pipeline.reference_recipe.v1",
            "reference": {
                "kind": "spoken_script_review",
                "review_sha256": script_review["review_sha256"],
            },
            "shots": [
                {
                    "shot_id": row["segment_id"],
                    "start_s": round(index * duration_per_segment, 3),
                    "end_s": round((index + 1) * duration_per_segment, 3),
                    "script": row["candidate_spoken_text"],
                    "requirements": {
                        "scene": "route-unselected synthetic foundation",
                        "action": "spoken script timing candidate",
                        "shot_size": "not_applicable",
                        "avoid": ["personal voice claim"],
                    },
                }
                for index, row in enumerate(script_review["segments"])
            ],
        }
    )
    recipe_path = atomic_write_json(
        recipe, contract_dir / "spoken-reference-recipe.json"
    )
    config = read_json_object(root / "audio" / "tts-config.json", "TTS config")
    config["chunk_policy"] = {
        **dict(config.get("chunk_policy") or {}),
        "max_chars": 120,
        "max_tokens": 180,
        "max_estimated_duration_s": 30,
    }
    config_path = atomic_write_json(config, contract_dir / "spoken-tts-config.json")
    job = build_tts_job(recipe, config)
    job_path = write_tts_contract(job, contract_dir / "spoken-tts-job.json")
    chunk_paths: list[Path] = []
    ffmpeg_receipts: list[Path] = []
    receipt_rows: list[dict[str, Any]] = []
    for index, chunk in enumerate(job["chunks"], 1):
        output = media_dir / f"tts-{index:03}.wav"
        receipt_path = receipt_dir / f"tts-{index:03}.json"
        _render_wav(
            output,
            duration_s=duration_per_segment,
            frequency=360 + index * 40,
            volume=0.18,
            receipt_path=receipt_path,
            input_path=script_review_path,
            role=f"synthetic_tts_chunk_{index}",
            timeout_s=timeout_s,
        )
        chunk_paths.append(output)
        ffmpeg_receipts.append(receipt_path)
        receipt_rows.append(
            {
                "chunk_id": chunk["chunk_id"],
                "attempt": 1,
                "status": "completed",
                "provider_id": job["provider"]["id"],
                "voice_profile_sha256": job["voice_profile_sha256"],
                "input_sha256": chunk["input_sha256"],
                "idempotency_key": chunk["idempotency_key"],
                "output": {
                    "path": str(output.resolve()),
                    "sha256": file_sha256(output),
                    "duration_s": _wav_duration(output),
                },
            }
        )
    receipts = {
        "schema": "video_creation_pipeline.tts_chunk_receipts.v1",
        "tts_job_sha256": job["job_sha256"],
        "receipts": receipt_rows,
    }
    receipts_path = atomic_write_json(
        receipts, contract_dir / "spoken-tts-receipts.json"
    )
    resume = build_resume_plan(job, receipts)
    resume_path = write_tts_contract(resume, contract_dir / "spoken-tts-resume.json")
    audio_qa = build_audio_stitch_qa(job, receipts)
    if audio_qa["readiness"]["ready"] is not True:
        raise ContractError("synthetic spoken audio failed existing seam QA")
    audio_qa_path = write_tts_contract(
        audio_qa, contract_dir / "spoken-audio-stitch-qa.json"
    )
    concat_path = media_dir / "spoken-audio.concat.txt"
    atomic_write_text(
        "\n".join(f"file '{path.as_posix()}'" for path in chunk_paths) + "\n",
        concat_path,
    )
    narration_path = media_dir / "synthetic-narration.wav"
    concat_receipt = receipt_dir / "synthetic-narration-concat.json"
    execute_ffmpeg_with_receipt(
        "concat",
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-threads",
            "1",
            "-c:a",
            "pcm_s16le",
            str(narration_path),
        ],
        [
            {"role": f"tts_chunk_{index:03}", "path": path}
            for index, path in enumerate(chunk_paths, 1)
        ],
        [{"role": "stitched_synthetic_narration", "path": narration_path}],
        concat_receipt,
        timeout_s=timeout_s,
    )
    ffmpeg_receipts.append(concat_receipt)
    if abs(_wav_duration(narration_path) - 15.0) > 0.01:
        raise ContractError("synthetic narration duration drifted")
    return (
        {
            "recipe": recipe_path,
            "config": config_path,
            "job": job_path,
            "receipts": receipts_path,
            "resume": resume_path,
            "audio_qa": audio_qa_path,
        },
        narration_path,
        ffmpeg_receipts,
    )


def _render_color_clip(
    output: Path,
    *,
    duration_s: float,
    color: str,
    label: str,
    receipt_path: Path,
    input_path: Path,
    role: str,
    timeout_s: float,
) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x{color}:s=1280x720:r=30:d={duration_s:.3f}",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-vf",
        _drawtext_filter(label),
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-threads",
        "1",
        "-t",
        f"{duration_s:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(output),
    ]
    execute_ffmpeg_with_receipt(
        "render",
        command,
        [{"role": "synthetic_fixture_spec", "path": input_path}],
        [{"role": role, "path": output}],
        receipt_path,
        timeout_s=timeout_s,
    )


def _render_wav(
    output: Path,
    *,
    duration_s: float,
    frequency: int,
    volume: float,
    receipt_path: Path,
    input_path: Path,
    role: str,
    timeout_s: float,
) -> None:
    execute_ffmpeg_with_receipt(
        "render",
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration={duration_s:.3f}",
            "-af",
            f"volume={volume:.4f}",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-threads",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        [{"role": "synthetic_fixture_spec", "path": input_path}],
        [{"role": role, "path": output}],
        receipt_path,
        timeout_s=timeout_s,
    )


def _drawtext_filter(label: str) -> str:
    escaped_label = label.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    font_options = ""
    for candidate in (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ):
        if candidate.is_file():
            font_path = candidate.as_posix().replace(":", r"\:")
            font_options = f"fontfile='{font_path}':"
            break
    return (
        f"drawtext={font_options}text='{escaped_label}':fontcolor=white:"
        "fontsize=46:x=(w-text_w)/2:y=(h-text_h)/2"
    )


def _render_visual_timeline(
    source_manifest: dict[str, Any],
    timeline: dict[str, Any],
    render_dir: Path,
    receipt_dir: Path,
    timeline_path: Path,
    *,
    timeout_s: float,
) -> tuple[Path, list[Path]]:
    assets = {row["asset_id"]: row for row in source_manifest["assets"]}
    visual_tracks = {"avatar_placeholder", "slides", "interview"}
    visual = sorted(
        [row for row in timeline["clips"] if row["track"] in visual_tracks],
        key=lambda row: float(row["start_s"]),
    )
    segment_paths: list[Path] = []
    receipt_paths: list[Path] = []
    for index, clip in enumerate(visual, 1):
        source = Path(assets[clip["source_asset_id"]]["primary_artifact"]["path"])
        output = render_dir / f"visual-segment-{index:03}.mp4"
        receipt = receipt_dir / f"visual-segment-{index:03}.json"
        execute_ffmpeg_with_receipt(
            "trim",
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{float(clip['source_in_s']):.3f}",
                "-i",
                str(source),
                "-t",
                f"{float(clip['end_s']) - float(clip['start_s']):.3f}",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:v",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                "-threads",
                "1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(output),
            ],
            [
                {"role": "source_media", "path": source},
                {"role": "candidate_timeline", "path": timeline_path},
            ],
            [{"role": "visual_segment", "path": output}],
            receipt,
            timeout_s=timeout_s,
        )
        segment_paths.append(output)
        receipt_paths.append(receipt)
    concat_path = render_dir / "visual-segments.concat.txt"
    atomic_write_text(
        "\n".join(f"file '{path.as_posix()}'" for path in segment_paths) + "\n",
        concat_path,
    )
    output = render_dir / "visual-program.mp4"
    receipt = receipt_dir / "visual-program-concat.json"
    execute_ffmpeg_with_receipt(
        "concat",
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-threads",
            "1",
            "-c:v",
            "copy",
            str(output),
        ],
        [
            {"role": f"visual_segment_{index:03}", "path": path}
            for index, path in enumerate(segment_paths, 1)
        ],
        [{"role": "visual_program", "path": output}],
        receipt,
        timeout_s=timeout_s,
    )
    receipt_paths.append(receipt)
    return output, receipt_paths


def _run_technical_qa(
    candidate_path: Path,
    subtitles_path: Path,
    packaging_path: Path,
    qa_dir: Path,
    receipt_dir: Path,
    *,
    expected_duration_s: float,
    deterministic_created_at: str,
    timeout_s: float,
) -> tuple[Path, Path]:
    probe_path = qa_dir / "ffprobe.json"
    atomic_write_json(_ffprobe(candidate_path), probe_path)
    detector_log = qa_dir / "ffmpeg-detection.log"
    receipt_path = receipt_dir / "foundation-technical-qa.json"
    execute_ffmpeg_with_receipt(
        "technical_qa",
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-v",
            "info",
            "-i",
            str(candidate_path),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-50dB:d=1.5",
            "-af",
            "silencedetect=n=-50dB:d=1.0",
            "-f",
            "null",
            "-",
        ],
        [{"role": "media", "path": candidate_path}],
        [{"role": "detector_log", "path": detector_log}],
        receipt_path,
        capture_log_path=detector_log,
        timeout_s=timeout_s,
    )
    report = build_technical_qa_report(
        candidate_path,
        probe_path,
        receipt_path,
        subtitles_path=subtitles_path,
        packaging_plan_path=packaging_path,
        expected_duration_s=expected_duration_s,
    )
    report["created_at"] = deterministic_created_at
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    validate_technical_qa_report(report)
    report_path = qa_dir / "technical-qa-report.json"
    write_technical_qa_report(report, report_path)
    return report_path, receipt_path


def _foundation_packaging_plan(timeline: dict[str, Any]) -> dict[str, Any]:
    visual_tracks = {"avatar_placeholder", "slides", "interview"}
    return {
        "schema": "video_creation_pipeline.foundation_packaging_plan.v1",
        "source_timeline_sha256": timeline["timeline_sha256"],
        "output": {"width": 1280, "height": 720, "fps": 30},
        "scenes": [
            {
                "scene_id": row["clip_id"],
                "start": row["start_s"],
                "end": row["end_s"],
                "material_status": "synthetic_fixture",
            }
            for row in timeline["clips"]
            if row["track"] in visual_tracks
        ],
        "candidate_only": True,
    }


def _timeline_clip(
    clip_id: str,
    track: str,
    asset: dict[str, Any],
    start_s: float,
    end_s: float,
    source_in_s: float,
    source_out_s: float,
    *,
    transition: str,
    audio_behavior: str,
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "track": track,
        "start_s": start_s,
        "end_s": end_s,
        "source_asset_id": asset["asset_id"],
        "source_sha256": asset["primary_artifact"]["sha256"],
        "source_in_s": source_in_s,
        "source_out_s": source_out_s,
        "transition": transition,
        "audio_behavior": audio_behavior,
        "protected_regions": ["caption_safe_region"],
    }


def _simple_asset(
    asset_id: str,
    source_type: str,
    path: str | Path,
    duration_s: float,
    target_ranges: list[dict[str, float]],
) -> dict[str, Any]:
    reference = artifact_reference(path, f"{source_type} source")
    return {
        "asset_id": asset_id,
        "source_type": source_type,
        "source_file": reference,
        "primary_artifact": reference,
        "source_duration_s": duration_s,
        "target_ranges": target_ranges,
        "freshness_status": "synthetic_current",
        "authorization_status": "synthetic",
    }


def _interview_slot_projection(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    interviews = [row for row in assets if row.get("source_type") == "interview_clip"]
    return [
        {
            "slot_id": f"synthetic-interview-slot-{index:03}",
            "state": "synthetic_authorized",
            "story_segment_ref": f"synthetic-story-segment-{index:03}",
            "source_asset_id": row["asset_id"],
            "source_sha256": row["primary_artifact"]["sha256"],
            "source_in_s": row["source_in_s"],
            "source_out_s": row["source_out_s"],
            "transcript_refs": list(row["transcript_refs"]),
            "evidence_refs": list(row["evidence_refs"]),
            "authorization_sha256": row["authorization_artifact"]["sha256"],
            "fallback_asset_id": row["fallback_asset_id"],
            "projection_boundary": {
                "source_semantics": "case-video-script-handoff.v1.interview_slots",
                "state_machine_created": False,
                "vkp_evidence_is_read_only": True,
            },
        }
        for index, row in enumerate(interviews, 1)
    ]


def _avatar_adapter_interface(
    clips: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    review: dict[str, Any],
) -> dict[str, Any]:
    placeholder = assets["avatar-placeholder-001"]
    narration = assets["narration-audio-001"]
    targets = [
        {
            "clip_id": row["clip_id"],
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "duration_s": round(float(row["end_s"]) - float(row["start_s"]), 3),
        }
        for row in clips
        if row.get("track") == "avatar_placeholder"
    ]
    return {
        "schema": "video_creation_pipeline.avatar_renderer_adapter_interface.v1",
        "status": "route_unselected",
        "inputs": {
            "spoken_script_sha256": review["candidate_spoken_text_sha256"],
            "narration_asset_id": narration["asset_id"],
            "narration_sha256": narration["primary_artifact"]["sha256"],
            "placeholder_asset_id": placeholder["asset_id"],
            "placeholder_sha256": placeholder["primary_artifact"]["sha256"],
            "target_spoken_segment_ids": list(placeholder["target_spoken_segment_ids"]),
            "target_slots": targets,
        },
        "output_requirements": {
            "visual_only": True,
            "container": "mp4",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "exact_slot_duration_required": True,
        },
        "required_receipt_fields": [
            "adapter_id",
            "adapter_version",
            "input_artifact_hashes",
            "output_artifact",
            "consent_receipt_if_remote",
            "lip_sync_qa",
            "face_anomaly_qa",
            "voice_identity_status",
        ],
        "replacement_policy": {
            "replace_placeholder_track_only": True,
            "preserve_clip_ids_and_time_ranges": True,
            "preserve_non_avatar_track_hashes": True,
            "silent_fallback_allowed": False,
            "automatic_approval_allowed": False,
            "human_review_required": True,
        },
    }


def _existing_gate_handoff() -> dict[str, Any]:
    return {
        "approval_gate": {
            "schema": APPROVAL_RECORD_SCHEMA,
            "status": "not_created",
            "required_targets": [
                "script",
                "voice",
                "storyboard",
                "materials",
                "visuals",
            ],
            "automatic_approval": False,
            "next_action": "human_operator_may_create_separate_records_after_review",
        },
        "avatar_generation_gate": {
            "schema": GENERATION_TASK_SCHEMA,
            "status": "blocked",
            "blocking_reason": "avatar_renderer_adapter_route_unselected",
            "task_created": False,
            "silent_generator_fallback": False,
            "next_action": "select_and_preflight_avatar_renderer_adapter",
        },
        "release_gate": {
            "schema": RELEASE_GATE_SCHEMA,
            "status": "not_created",
            "release_ready": False,
            "automatic_publish": False,
            "next_action": "complete_existing_approval_and_release_gates",
        },
        "boundary": {
            "reuses_existing_gate_schemas": True,
            "second_approval_system_created": False,
            "review_packet_grants_execution_authority": False,
        },
    }


def _time_range(start_s: float, end_s: float) -> dict[str, float]:
    return {"start_s": start_s, "end_s": end_s}


def _validated_range(value: Any, total: float, label: str) -> tuple[float, float]:
    row = _object(value)
    start = _nonnegative_number(row.get("start_s"), f"{label} start")
    end = _positive_number(row.get("end_s"), f"{label} end")
    if end <= start or end > total + 0.001:
        raise ContractError(f"{label} is outside the timeline")
    return start, end


def _script_srt(review: dict[str, Any]) -> str:
    rows = review["segments"]
    duration = 15.0 / len(rows)
    blocks = []
    for index, row in enumerate(rows, 1):
        start = (index - 1) * duration
        end = index * duration
        blocks.append(
            f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n"
            f"{row['candidate_spoken_text']}\n"
        )
    return "\n".join(blocks)


def _srt_text_rows(path: Path) -> list[str]:
    rows: list[str] = []
    blocks = path.read_text(encoding="utf-8-sig").strip().split("\n\n")
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            raise ContractError("caption artifact is not valid SRT")
        rows.append(" ".join(lines[2:]))
    return rows


def _srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    whole_seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{millis:03}"


def _ffprobe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ContractError("ffprobe executable was not found")
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(f"ffprobe failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError("ffprobe returned an invalid document")
    return payload


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return round(handle.getnframes() / handle.getframerate(), 3)


def _media_duration(path: Path) -> float:
    if path.suffix.lower() == ".wav":
        return _wav_duration(path)
    probe = _ffprobe(path)
    try:
        return round(float(_object(probe.get("format"))["duration"]), 3)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"media duration is unavailable: {path}") from exc


def _validate_plan_owner(root: Path, plan: dict[str, Any]) -> None:
    owner = read_json_object(
        root / ".content-video-output-owner.json", "content video output owner"
    )
    if (
        owner.get("source_plan_sha256") != plan.get("plan_sha256")
        or owner.get("user_media_overwrite_allowed") is not False
    ):
        raise ContractError("content video output owner does not bind this plan")


def _positive_number(value: Any, label: str) -> float:
    number = _nonnegative_number(value, label)
    if number <= 0:
        raise ContractError(f"{label} must be positive")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"{label} must be non-negative")
    return number


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
