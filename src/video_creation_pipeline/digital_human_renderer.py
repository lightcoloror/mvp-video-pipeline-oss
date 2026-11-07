from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .digital_human_foundation import (
    build_digital_human_candidate_edl_projection,
    validate_digital_human_candidate_timeline,
    validate_digital_human_foundation_bundle,
)
from .errors import ContractError
from .execution_receipts import (
    execute_ffmpeg_with_receipt,
    validate_ffmpeg_execution_receipt,
)

RENDERER_REQUEST_SCHEMA = "video_creation_pipeline.avatar_renderer_request.v1"
RENDERER_RECEIPT_SCHEMA = "video_creation_pipeline.avatar_renderer_receipt.v1"
RENDERER_TIMELINE_SCHEMA = (
    "video_creation_pipeline.digital_human_renderer_candidate_timeline.v1"
)
RENDERER_PROVENANCE_SCHEMA = (
    "video_creation_pipeline.digital_human_renderer_provenance.v1"
)
RENDERER_REVIEW_PACKET_SCHEMA = (
    "video_creation_pipeline.digital_human_renderer_review_packet.v1"
)
RENDERER_RECOVERY_SCHEMA = "video_creation_pipeline.avatar_renderer_recovery.v1"
RENDERER_RUN_RECEIPT_SCHEMA = (
    "video_creation_pipeline.digital_human_renderer_fixture_run_receipt.v1"
)

SYNTHETIC_ADAPTER_ID = "synthetic-avatar-renderer-stub"
SYNTHETIC_ADAPTER_VERSION = "1.0.0"
SYNTHETIC_PROVIDER_ID = "offline-synthetic-fixture"
SYNTHETIC_PROVIDER_REVISION = "1"
_SUCCESS_STATUS = "renderer_candidate_ready"
_FAILURE_STATUSES = {"blocked", "failed"}
_ERROR_CODES = {
    "renderer_provider_unavailable",
    "renderer_authorization_missing",
    "renderer_input_artifact_drift",
    "renderer_av_duration_drift",
    "renderer_lip_sync_failed",
    "tts_incomplete",
    "required_source_missing",
    "renderer_fallback_forbidden",
    "renderer_publication_escalation",
    "renderer_idempotency_conflict",
}


def build_avatar_renderer_request(
    plan_root: str | Path,
    *,
    capability_preflight_path: str | Path,
    avatar_asset_path: str | Path,
    voice_asset_path: str | Path,
    avatar_authorization_path: str | Path,
    voice_authorization_path: str | Path,
    provider_id: str = SYNTHETIC_PROVIDER_ID,
    provider_revision: str = SYNTHETIC_PROVIDER_REVISION,
    adapter_id: str = SYNTHETIC_ADAPTER_ID,
    adapter_version: str = SYNTHETIC_ADAPTER_VERSION,
) -> dict[str, Any]:
    root = Path(plan_root).expanduser().resolve()
    validate_digital_human_foundation_bundle(root)
    contract_dir = root / "digital-human-foundation" / "contracts"
    timeline_path = contract_dir / "candidate-timeline.json"
    timeline = read_json_object(timeline_path, "foundation candidate timeline")
    validate_digital_human_candidate_timeline(timeline)
    interface = _object(timeline.get("avatar_adapter_interface"))
    if interface.get("schema") != (
        "video_creation_pipeline.avatar_renderer_adapter_interface.v1"
    ):
        raise ContractError("renderer interface is unavailable")

    preflight_path = Path(capability_preflight_path).expanduser().resolve()
    preflight = read_json_object(preflight_path, "renderer capability preflight")
    _validate_renderer_preflight(preflight)
    avatar_auth_path = Path(avatar_authorization_path).expanduser().resolve()
    voice_auth_path = Path(voice_authorization_path).expanduser().resolve()
    _validate_synthetic_identity_authorization(
        read_json_object(avatar_auth_path, "avatar authorization"), "avatar"
    )
    _validate_synthetic_identity_authorization(
        read_json_object(voice_auth_path, "voice authorization"), "voice"
    )

    source_manifest_path = contract_dir / "source-manifest.json"
    source_manifest = read_json_object(source_manifest_path, "source manifest")
    assets = _manifest_asset_projection(source_manifest)
    tts_job_path = contract_dir / "spoken-tts-job.json"
    tts_receipts_path = contract_dir / "spoken-tts-receipts.json"
    tts_resume_path = contract_dir / "spoken-tts-resume.json"
    audio_qa_path = contract_dir / "spoken-audio-stitch-qa.json"
    _validate_tts_ready(tts_resume_path, audio_qa_path)

    inputs = {
        "reviewed_script": artifact_reference(
            contract_dir / "spoken-script-review.json", "reviewed script"
        ),
        "base_source_manifest": artifact_reference(
            source_manifest_path, "base source manifest"
        ),
        "base_candidate_timeline": artifact_reference(
            timeline_path, "base candidate timeline"
        ),
        "base_candidate_edl": artifact_reference(
            contract_dir / "candidate-edl.json", "base candidate EDL"
        ),
        "tts_job": artifact_reference(tts_job_path, "TTS job"),
        "tts_receipts": artifact_reference(tts_receipts_path, "TTS receipts"),
        "tts_resume_plan": artifact_reference(tts_resume_path, "TTS resume plan"),
        "audio_stitch_qa": artifact_reference(audio_qa_path, "audio stitch QA"),
        "narration_audio": _single_asset(assets, "narration_audio")["primary_artifact"],
        "avatar_asset": artifact_reference(avatar_asset_path, "avatar asset"),
        "voice_asset": artifact_reference(voice_asset_path, "voice asset"),
        "avatar_authorization": artifact_reference(
            avatar_auth_path, "avatar authorization"
        ),
        "voice_authorization": artifact_reference(
            voice_auth_path, "voice authorization"
        ),
        "slides": [row["primary_artifact"] for row in assets["slide"]],
        "interviews": [
            {
                "media": row["primary_artifact"],
                "authorization": row["authorization_artifact"],
            }
            for row in assets["interview_clip"]
        ],
        "b_roll": [],
    }
    request: dict[str, Any] = {
        "schema": RENDERER_REQUEST_SCHEMA,
        "mode": "synthetic_fixture_stub",
        "provider": {
            "id": str(provider_id),
            "revision": str(provider_revision),
            "available": True,
            "remote": False,
        },
        "adapter": {
            "id": str(adapter_id),
            "version": str(adapter_version),
            "interface_schema": interface["schema"],
            "code_license": "Apache-2.0",
            "model_weights_used": False,
        },
        "capability_preflight": artifact_reference(
            preflight_path, "renderer capability preflight"
        ),
        "inputs": inputs,
        "target_slots": _renderer_target_slots(interface),
        "correction_ranges": [],
        "rerecord_ranges": [],
        "output_requirements": dict(interface["output_requirements"]),
        "execution_policy": {
            "explicit_route_only": True,
            "silent_fallback_allowed": False,
            "automatic_retry_allowed": False,
            "upload_allowed": False,
            "network_allowed": False,
            "publish_allowed": False,
            "human_review_required": True,
        },
        "authority_boundary": {
            "synthetic_only": True,
            "personal_media_used": False,
            "customer_media_used": False,
            "provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "candidate_only": True,
        },
    }
    request["idempotency_key"] = _request_identity(request)
    request["request_sha256"] = payload_sha256(request, "request_sha256")
    validate_avatar_renderer_request(request)
    return request


def validate_avatar_renderer_request(request: dict[str, Any]) -> None:
    if request.get("schema") != RENDERER_REQUEST_SCHEMA:
        raise ContractError("unsupported avatar renderer request schema")
    if request.get("request_sha256") != payload_sha256(request, "request_sha256"):
        raise ContractError("avatar renderer request integrity check failed")
    if request.get("idempotency_key") != _request_identity(request):
        raise ContractError("renderer idempotency key is invalid")
    provider = _object(request.get("provider"))
    adapter = _object(request.get("adapter"))
    if (
        request.get("mode") != "synthetic_fixture_stub"
        or provider.get("id") != SYNTHETIC_PROVIDER_ID
        or provider.get("revision") != SYNTHETIC_PROVIDER_REVISION
        or provider.get("available") is not True
        or provider.get("remote") is not False
        or adapter.get("id") != SYNTHETIC_ADAPTER_ID
        or adapter.get("version") != SYNTHETIC_ADAPTER_VERSION
        or adapter.get("interface_schema")
        != "video_creation_pipeline.avatar_renderer_adapter_interface.v1"
        or adapter.get("model_weights_used") is not False
    ):
        raise ContractError("renderer provider unavailable or route changed")
    preflight_path = validate_artifact_reference(
        _object(request.get("capability_preflight")), "renderer capability preflight"
    )
    _validate_renderer_preflight(
        read_json_object(preflight_path, "renderer capability preflight")
    )
    inputs = _object(request.get("inputs"))
    expected_inputs = {
        "reviewed_script",
        "base_source_manifest",
        "base_candidate_timeline",
        "base_candidate_edl",
        "tts_job",
        "tts_receipts",
        "tts_resume_plan",
        "audio_stitch_qa",
        "narration_audio",
        "avatar_asset",
        "voice_asset",
        "avatar_authorization",
        "voice_authorization",
        "slides",
        "interviews",
        "b_roll",
    }
    if set(inputs) != expected_inputs:
        raise ContractError("renderer request input set is incomplete")
    for key in (
        "reviewed_script",
        "base_source_manifest",
        "base_candidate_timeline",
        "base_candidate_edl",
        "tts_job",
        "tts_receipts",
        "tts_resume_plan",
        "audio_stitch_qa",
        "narration_audio",
        "avatar_asset",
        "voice_asset",
        "avatar_authorization",
        "voice_authorization",
    ):
        validate_artifact_reference(_object(inputs[key]), key)
    slides = inputs.get("slides")
    interviews = inputs.get("interviews")
    b_roll = inputs.get("b_roll")
    if not isinstance(slides, list) or not slides:
        raise ContractError("required source missing: PPT")
    if not isinstance(interviews, list) or not interviews:
        raise ContractError("required source missing: interview")
    if not isinstance(b_roll, list):
        raise ContractError("renderer b_roll input must be a list")
    for index, row in enumerate(slides, 1):
        validate_artifact_reference(_object(row), f"PPT page {index}")
    for index, value in enumerate(interviews, 1):
        row = _object(value)
        validate_artifact_reference(_object(row.get("media")), f"interview {index}")
        validate_artifact_reference(
            _object(row.get("authorization")), f"interview authorization {index}"
        )
    for index, row in enumerate(b_roll, 1):
        validate_artifact_reference(_object(row), f"B-roll {index}")
    for kind in ("avatar", "voice"):
        path = validate_artifact_reference(
            _object(inputs[f"{kind}_authorization"]), f"{kind} authorization"
        )
        _validate_synthetic_identity_authorization(
            read_json_object(path, f"{kind} authorization"), kind
        )
    _validate_tts_ready(
        validate_artifact_reference(
            _object(inputs["tts_resume_plan"]), "TTS resume plan"
        ),
        validate_artifact_reference(
            _object(inputs["audio_stitch_qa"]), "audio stitch QA"
        ),
    )
    base_timeline_path = validate_artifact_reference(
        _object(inputs["base_candidate_timeline"]), "base candidate timeline"
    )
    base_timeline = read_json_object(base_timeline_path, "base candidate timeline")
    validate_digital_human_candidate_timeline(base_timeline)
    expected_interface = _object(base_timeline.get("avatar_adapter_interface"))
    if request.get("target_slots") != _renderer_target_slots(expected_interface):
        raise ContractError("renderer target slots drifted from the base timeline")
    output = _object(request.get("output_requirements"))
    if output != expected_interface.get("output_requirements"):
        raise ContractError("renderer output requirements drifted")
    for range_kind in ("correction_ranges", "rerecord_ranges"):
        ranges = request.get(range_kind)
        if not isinstance(ranges, list):
            raise ContractError(f"renderer {range_kind} must be a list")
        for index, row in enumerate(ranges, 1):
            _validate_range(
                _object(row),
                float(base_timeline["total_duration_s"]),
                index,
                range_kind,
            )
    policy = _object(request.get("execution_policy"))
    boundary = _object(request.get("authority_boundary"))
    if (
        policy.get("explicit_route_only") is not True
        or policy.get("silent_fallback_allowed") is not False
        or policy.get("automatic_retry_allowed") is not False
        or policy.get("upload_allowed") is not False
        or policy.get("network_allowed") is not False
        or policy.get("publish_allowed") is not False
        or policy.get("human_review_required") is not True
        or boundary.get("synthetic_only") is not True
        or boundary.get("personal_media_used") is not False
        or boundary.get("customer_media_used") is not False
        or boundary.get("provider_called") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
        or boundary.get("candidate_only") is not True
    ):
        raise ContractError("renderer request authority boundary is invalid")


def run_synthetic_avatar_renderer_stub(
    request_path: str | Path,
    output_dir: str | Path,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    request_file = Path(request_path).expanduser().resolve()
    request = read_json_object(request_file, "avatar renderer request")
    validate_avatar_renderer_request(request)
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    ffmpeg_receipt_root = output_root.parent / "ffmpeg-receipts"
    ffmpeg_receipt_root.mkdir(parents=True, exist_ok=True)
    receipt_path = output_root / "renderer-receipt.json"
    if receipt_path.is_file():
        existing = read_json_object(receipt_path, "avatar renderer receipt")
        validate_avatar_renderer_receipt(request, existing)
        if (
            existing.get("status") == "completed"
            and existing.get("idempotency_key") == request["idempotency_key"]
        ):
            return existing
        raise ContractError("renderer idempotency conflict")

    outputs: list[dict[str, Any]] = []
    expected = _object(request["output_requirements"])
    for slot in request["target_slots"]:
        slot_row = _object(slot)
        output_path = output_root / f"{slot_row['clip_id']}.mp4"
        ffmpeg_receipt_path = (
            ffmpeg_receipt_root / f"{slot_row['clip_id']}-synthetic-render.json"
        )
        _render_synthetic_avatar_slot(
            output_path,
            request_path=request_file,
            receipt_path=ffmpeg_receipt_path,
            duration_s=float(slot_row["duration_s"]),
            width=int(expected["width"]),
            height=int(expected["height"]),
            fps=int(expected["fps"]),
            timeout_s=timeout_s,
        )
        outputs.append(
            {
                "clip_id": slot_row["clip_id"],
                "target_start_s": slot_row["start_s"],
                "target_end_s": slot_row["end_s"],
                "expected_duration_s": slot_row["duration_s"],
                "output_artifact": artifact_reference(
                    output_path, f"renderer output {slot_row['clip_id']}"
                ),
                "ffmpeg_receipt": artifact_reference(
                    ffmpeg_receipt_path,
                    f"renderer FFmpeg receipt {slot_row['clip_id']}",
                ),
                "technical_probe": _renderer_probe(output_path),
                "lip_sync_qa": {
                    "status": "synthetic_fixture_passed",
                    "method": "deterministic_stub_marker",
                    "real_lip_sync_measured": False,
                },
                "face_anomaly_qa": {
                    "status": "synthetic_fixture_passed",
                    "method": "deterministic_stub_marker",
                    "real_face_measured": False,
                },
            }
        )
    receipt: dict[str, Any] = {
        "schema": RENDERER_RECEIPT_SCHEMA,
        "request_sha256": request["request_sha256"],
        "idempotency_key": request["idempotency_key"],
        "provider": dict(request["provider"]),
        "adapter": dict(request["adapter"]),
        "status": "completed",
        "outputs": outputs,
        "error": None,
        "execution": {
            "provider_called": False,
            "network_used": False,
            "upload_performed": False,
            "fallback_used": False,
            "retry_count": 0,
            "publish_performed": False,
        },
        "quality": {
            "lip_sync_status": "synthetic_fixture_passed",
            "face_anomaly_status": "synthetic_fixture_passed",
            "voice_identity_status": "synthetic_fixture_not_tested",
            "real_person_quality_claimed": False,
        },
        "candidate_only": True,
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_avatar_renderer_receipt(request, receipt)
    atomic_write_json(receipt, receipt_path)
    return receipt


def validate_avatar_renderer_receipt(
    request: dict[str, Any], receipt: dict[str, Any]
) -> None:
    validate_avatar_renderer_request(request)
    if receipt.get("schema") != RENDERER_RECEIPT_SCHEMA:
        raise ContractError("unsupported avatar renderer receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("avatar renderer receipt integrity check failed")
    if (
        receipt.get("request_sha256") != request.get("request_sha256")
        or receipt.get("idempotency_key") != request.get("idempotency_key")
        or receipt.get("provider") != request.get("provider")
        or receipt.get("adapter") != request.get("adapter")
    ):
        raise ContractError("renderer receipt does not bind the exact request")
    status = str(receipt.get("status") or "")
    if status not in {"completed", *_FAILURE_STATUSES}:
        raise ContractError("renderer receipt status is invalid")
    execution = _object(receipt.get("execution"))
    if (
        execution.get("provider_called") is not False
        or execution.get("network_used") is not False
        or execution.get("upload_performed") is not False
        or execution.get("fallback_used") is not False
        or execution.get("retry_count") != 0
        or execution.get("publish_performed") is not False
        or receipt.get("candidate_only") is not True
    ):
        raise ContractError("renderer fallback or publication boundary is invalid")
    if status in _FAILURE_STATUSES:
        _validate_renderer_error(receipt.get("error"))
        if receipt.get("outputs") not in (None, []):
            raise ContractError("failed renderer receipt cannot contain outputs")
        return
    if receipt.get("error") is not None:
        raise ContractError("completed renderer receipt cannot contain an error")
    outputs = receipt.get("outputs")
    slots = request["target_slots"]
    if not isinstance(outputs, list) or len(outputs) != len(slots):
        raise ContractError("renderer output slot set is incomplete")
    required = _object(request["output_requirements"])
    for slot, value in zip(slots, outputs, strict=True):
        expected_slot = _object(slot)
        output = _object(value)
        if (
            output.get("clip_id") != expected_slot.get("clip_id")
            or output.get("target_start_s") != expected_slot.get("start_s")
            or output.get("target_end_s") != expected_slot.get("end_s")
            or output.get("expected_duration_s") != expected_slot.get("duration_s")
        ):
            raise ContractError("renderer output slot identity drifted")
        path = validate_artifact_reference(
            _object(output.get("output_artifact")), "renderer output"
        )
        ffmpeg_receipt_path = validate_artifact_reference(
            _object(output.get("ffmpeg_receipt")), "renderer FFmpeg receipt"
        )
        ffmpeg_receipt = read_json_object(
            ffmpeg_receipt_path, "renderer FFmpeg execution receipt"
        )
        validate_ffmpeg_execution_receipt(ffmpeg_receipt)
        ffmpeg_outputs = ffmpeg_receipt.get("outputs")
        if (
            ffmpeg_receipt.get("operation") != "render"
            or not isinstance(ffmpeg_outputs, list)
            or len(ffmpeg_outputs) != 1
            or _object(ffmpeg_outputs[0]).get("artifact")
            != output.get("output_artifact")
        ):
            raise ContractError(
                "renderer output is not bound to the shared FFmpeg outlet"
            )
        probe = _renderer_probe(path)
        if output.get("technical_probe") != probe:
            raise ContractError("renderer technical probe drifted")
        if (
            probe["width"] != required["width"]
            or probe["height"] != required["height"]
            or abs(probe["fps"] - float(required["fps"])) > 0.01
            or probe["video_codec"] != required["video_codec"]
            or probe["pixel_format"] != required["pixel_format"]
            or probe["has_audio_stream"] is not False
            or abs(probe["video_duration_s"] - float(expected_slot["duration_s"]))
            > 0.05
        ):
            raise ContractError("renderer A/V or slot duration drifted")
        lip = _object(output.get("lip_sync_qa"))
        face = _object(output.get("face_anomaly_qa"))
        if (
            lip.get("status") != "synthetic_fixture_passed"
            or lip.get("real_lip_sync_measured") is not False
            or face.get("status") != "synthetic_fixture_passed"
            or face.get("real_face_measured") is not False
        ):
            raise ContractError("renderer lip sync or face QA failed")
    quality = _object(receipt.get("quality"))
    if (
        quality.get("lip_sync_status") != "synthetic_fixture_passed"
        or quality.get("face_anomaly_status") != "synthetic_fixture_passed"
        or quality.get("voice_identity_status") != "synthetic_fixture_not_tested"
        or quality.get("real_person_quality_claimed") is not False
    ):
        raise ContractError("renderer quality claims exceed synthetic evidence")


def build_renderer_candidate_timeline(
    request_path: str | Path, receipt_path: str | Path
) -> dict[str, Any]:
    request_file = Path(request_path).expanduser().resolve()
    receipt_file = Path(receipt_path).expanduser().resolve()
    request = read_json_object(request_file, "avatar renderer request")
    receipt = read_json_object(receipt_file, "avatar renderer receipt")
    validate_avatar_renderer_receipt(request, receipt)
    if receipt.get("status") != "completed":
        raise ContractError("renderer candidate requires a completed receipt")
    base_path = validate_artifact_reference(
        _object(request["inputs"]["base_candidate_timeline"]),
        "base candidate timeline",
    )
    base = read_json_object(base_path, "base candidate timeline")
    validate_digital_human_candidate_timeline(base)
    replacements = {row["clip_id"]: row for row in receipt["outputs"]}
    clips: list[dict[str, Any]] = []
    for value in base["clips"]:
        row = dict(_object(value))
        if row.get("track") == "avatar_placeholder":
            replacement = replacements[row["clip_id"]]
            row["track"] = "avatar_renderer_candidate"
            row["source_asset_id"] = f"renderer-{row['clip_id']}"
            row["source_sha256"] = replacement["output_artifact"]["sha256"]
            row["source_in_s"] = 0.0
            row["source_out_s"] = replacement["expected_duration_s"]
            row["audio_behavior"] = "source_audio_absent"
        clips.append(row)
    timeline: dict[str, Any] = {
        "schema": RENDERER_TIMELINE_SCHEMA,
        "base_timeline": artifact_reference(base_path, "base candidate timeline"),
        "renderer_request": artifact_reference(request_file, "renderer request"),
        "renderer_receipt": artifact_reference(receipt_file, "renderer receipt"),
        "total_duration_s": base["total_duration_s"],
        "clips": clips,
        "candidate_edl_projection": build_digital_human_candidate_edl_projection(base),
        "provenance": {
            "base_timeline_sha256": base["timeline_sha256"],
            "request_sha256": request["request_sha256"],
            "receipt_sha256": receipt["receipt_sha256"],
            "idempotency_key": request["idempotency_key"],
        },
        "replacement_policy": {
            "placeholder_track_only": True,
            "clip_ids_and_ranges_preserved": True,
            "non_avatar_clips_preserved_exactly": True,
            "silent_fallback_used": False,
        },
        "readiness": {
            "status": _SUCCESS_STATUS,
            "candidate_only": True,
            "manual_review_required": True,
            "release_eligible": False,
        },
    }
    timeline["timeline_sha256"] = payload_sha256(timeline, "timeline_sha256")
    validate_renderer_candidate_timeline(timeline)
    return timeline


def validate_renderer_candidate_timeline(timeline: dict[str, Any]) -> None:
    if timeline.get("schema") != RENDERER_TIMELINE_SCHEMA:
        raise ContractError("unsupported renderer candidate timeline schema")
    if timeline.get("timeline_sha256") != payload_sha256(timeline, "timeline_sha256"):
        raise ContractError("renderer candidate timeline integrity check failed")
    base_path = validate_artifact_reference(
        _object(timeline.get("base_timeline")), "base candidate timeline"
    )
    request_path = validate_artifact_reference(
        _object(timeline.get("renderer_request")), "renderer request"
    )
    receipt_path = validate_artifact_reference(
        _object(timeline.get("renderer_receipt")), "renderer receipt"
    )
    base = read_json_object(base_path, "base candidate timeline")
    request = read_json_object(request_path, "renderer request")
    receipt = read_json_object(receipt_path, "renderer receipt")
    validate_digital_human_candidate_timeline(base)
    validate_avatar_renderer_receipt(request, receipt)
    provenance = _object(timeline.get("provenance"))
    if provenance != {
        "base_timeline_sha256": base["timeline_sha256"],
        "request_sha256": request["request_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
        "idempotency_key": request["idempotency_key"],
    }:
        raise ContractError("renderer timeline provenance drifted")
    base_clips = {row["clip_id"]: row for row in base["clips"]}
    rows = timeline.get("clips")
    if not isinstance(rows, list) or [row.get("clip_id") for row in rows] != [
        row["clip_id"] for row in base["clips"]
    ]:
        raise ContractError("renderer candidate clip identities drifted")
    output_by_clip = {row["clip_id"]: row for row in receipt["outputs"]}
    for value in rows:
        row = _object(value)
        original = base_clips[row["clip_id"]]
        if original["track"] != "avatar_placeholder":
            if row != original:
                raise ContractError("renderer changed a non-avatar clip")
            continue
        output = output_by_clip.get(row["clip_id"])
        if output is None or (
            row.get("track") != "avatar_renderer_candidate"
            or row.get("start_s") != original.get("start_s")
            or row.get("end_s") != original.get("end_s")
            or row.get("clip_id") != original.get("clip_id")
            or row.get("source_sha256") != output["output_artifact"]["sha256"]
        ):
            raise ContractError("renderer avatar replacement drifted")
    if timeline.get("candidate_edl_projection") != (
        build_digital_human_candidate_edl_projection(base)
    ):
        raise ContractError("renderer candidate EDL drifted from base timeline")
    policy = _object(timeline.get("replacement_policy"))
    readiness = _object(timeline.get("readiness"))
    if (
        policy.get("placeholder_track_only") is not True
        or policy.get("clip_ids_and_ranges_preserved") is not True
        or policy.get("non_avatar_clips_preserved_exactly") is not True
        or policy.get("silent_fallback_used") is not False
        or readiness.get("status") != _SUCCESS_STATUS
        or readiness.get("candidate_only") is not True
        or readiness.get("manual_review_required") is not True
        or readiness.get("release_eligible") is not False
    ):
        raise ContractError("renderer candidate authority boundary is invalid")


def build_renderer_provenance(
    request_path: str | Path,
    receipt_path: str | Path,
    timeline_path: str | Path,
) -> dict[str, Any]:
    request_file = Path(request_path).expanduser().resolve()
    receipt_file = Path(receipt_path).expanduser().resolve()
    timeline_file = Path(timeline_path).expanduser().resolve()
    request = read_json_object(request_file, "renderer request")
    receipt = read_json_object(receipt_file, "renderer receipt")
    timeline = read_json_object(timeline_file, "renderer candidate timeline")
    validate_avatar_renderer_receipt(request, receipt)
    validate_renderer_candidate_timeline(timeline)
    payload: dict[str, Any] = {
        "schema": RENDERER_PROVENANCE_SCHEMA,
        "renderer_request": artifact_reference(request_file, "renderer request"),
        "renderer_receipt": artifact_reference(receipt_file, "renderer receipt"),
        "candidate_timeline": artifact_reference(
            timeline_file, "renderer candidate timeline"
        ),
        "identity": {
            "idempotency_key": request["idempotency_key"],
            "request_sha256": request["request_sha256"],
            "receipt_sha256": receipt["receipt_sha256"],
            "timeline_sha256": timeline["timeline_sha256"],
        },
        "route": {
            "provider": dict(request["provider"]),
            "adapter": dict(request["adapter"]),
            "synthetic_stub": True,
            "real_provider_execution": False,
        },
        "source_bindings": dict(request["inputs"]),
        "authority_boundary": {
            "candidate_only": True,
            "synthetic_only": True,
            "personal_media_used": False,
            "customer_media_used": False,
            "network_used": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    payload["provenance_sha256"] = payload_sha256(payload, "provenance_sha256")
    validate_renderer_provenance(payload)
    return payload


def validate_renderer_provenance(payload: dict[str, Any]) -> None:
    if payload.get("schema") != RENDERER_PROVENANCE_SCHEMA:
        raise ContractError("unsupported renderer provenance schema")
    if payload.get("provenance_sha256") != payload_sha256(payload, "provenance_sha256"):
        raise ContractError("renderer provenance integrity check failed")
    request_path = validate_artifact_reference(
        _object(payload.get("renderer_request")), "renderer request"
    )
    receipt_path = validate_artifact_reference(
        _object(payload.get("renderer_receipt")), "renderer receipt"
    )
    timeline_path = validate_artifact_reference(
        _object(payload.get("candidate_timeline")), "renderer candidate timeline"
    )
    request = read_json_object(request_path, "renderer request")
    receipt = read_json_object(receipt_path, "renderer receipt")
    timeline = read_json_object(timeline_path, "renderer candidate timeline")
    validate_avatar_renderer_receipt(request, receipt)
    validate_renderer_candidate_timeline(timeline)
    if _object(payload.get("identity")) != {
        "idempotency_key": request["idempotency_key"],
        "request_sha256": request["request_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
        "timeline_sha256": timeline["timeline_sha256"],
    }:
        raise ContractError("renderer provenance identity drifted")
    route = _object(payload.get("route"))
    if (
        route.get("provider") != request.get("provider")
        or route.get("adapter") != request.get("adapter")
        or route.get("synthetic_stub") is not True
        or route.get("real_provider_execution") is not False
        or payload.get("source_bindings") != request.get("inputs")
    ):
        raise ContractError("renderer provenance route or source binding drifted")
    boundary = _object(payload.get("authority_boundary"))
    if any(
        boundary.get(field) is not False
        for field in (
            "personal_media_used",
            "customer_media_used",
            "network_used",
            "upload_performed",
            "publish_performed",
        )
    ) or any(
        boundary.get(field) is not True
        for field in ("candidate_only", "synthetic_only")
    ):
        raise ContractError("renderer provenance authority boundary is invalid")


def build_renderer_recovery(
    request_path: str | Path, receipt_path: str | Path
) -> dict[str, Any]:
    request_file = Path(request_path).expanduser().resolve()
    receipt_file = Path(receipt_path).expanduser().resolve()
    request = read_json_object(request_file, "renderer request")
    receipt = read_json_object(receipt_file, "renderer receipt")
    validate_avatar_renderer_receipt(request, receipt)
    status = str(receipt["status"])
    payload: dict[str, Any] = {
        "schema": RENDERER_RECOVERY_SCHEMA,
        "renderer_request": artifact_reference(request_file, "renderer request"),
        "renderer_receipt": artifact_reference(receipt_file, "renderer receipt"),
        "idempotency_key": request["idempotency_key"],
        "status": "not_required" if status == "completed" else "blocked",
        "automatic_retry_allowed": False,
        "automatic_fallback_allowed": False,
        "next_action": (
            "human_review_renderer_candidate"
            if status == "completed"
            else _object(receipt["error"])["next_action"]
        ),
    }
    payload["recovery_sha256"] = payload_sha256(payload, "recovery_sha256")
    validate_renderer_recovery(payload)
    return payload


def validate_renderer_recovery(payload: dict[str, Any]) -> None:
    if payload.get("schema") != RENDERER_RECOVERY_SCHEMA:
        raise ContractError("unsupported renderer recovery schema")
    if payload.get("recovery_sha256") != payload_sha256(payload, "recovery_sha256"):
        raise ContractError("renderer recovery integrity check failed")
    request_path = validate_artifact_reference(
        _object(payload.get("renderer_request")), "renderer request"
    )
    receipt_path = validate_artifact_reference(
        _object(payload.get("renderer_receipt")), "renderer receipt"
    )
    request = read_json_object(request_path, "renderer request")
    receipt = read_json_object(receipt_path, "renderer receipt")
    validate_avatar_renderer_receipt(request, receipt)
    status = str(receipt["status"])
    if (
        payload.get("idempotency_key") != request.get("idempotency_key")
        or payload.get("status")
        != ("not_required" if status == "completed" else "blocked")
        or payload.get("automatic_retry_allowed") is not False
        or payload.get("automatic_fallback_allowed") is not False
        or payload.get("next_action")
        != (
            "human_review_renderer_candidate"
            if status == "completed"
            else _object(receipt["error"])["next_action"]
        )
    ):
        raise ContractError("renderer recovery drifted from request and receipt")


def generate_digital_human_renderer_synthetic_fixture(
    plan_root: str | Path,
    *,
    dispatch_id: str = "DIGITAL-HUMAN-PPT-INTERVIEW-PIPELINE-20260812-01",
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    root = Path(plan_root).expanduser().resolve()
    validate_digital_human_foundation_bundle(root)
    renderer_root = root / "digital-human-renderer"
    fixture_dir = renderer_root / "fixture"
    contract_dir = renderer_root / "contracts"
    output_dir = renderer_root / "provider-output"
    for directory in (fixture_dir, contract_dir, output_dir):
        directory.mkdir(parents=True, exist_ok=True)
    fixture_paths = _write_renderer_fixture_assets(fixture_dir)
    request = build_avatar_renderer_request(
        root,
        capability_preflight_path=fixture_paths["preflight"],
        avatar_asset_path=fixture_paths["avatar"],
        voice_asset_path=fixture_paths["voice"],
        avatar_authorization_path=fixture_paths["avatar_authorization"],
        voice_authorization_path=fixture_paths["voice_authorization"],
    )
    request_path = atomic_write_json(request, contract_dir / "renderer-request.json")
    run_synthetic_avatar_renderer_stub(request_path, output_dir, timeout_s=timeout_s)
    receipt_path = output_dir / "renderer-receipt.json"
    timeline = build_renderer_candidate_timeline(request_path, receipt_path)
    timeline_path = atomic_write_json(
        timeline, contract_dir / "renderer-candidate-timeline.json"
    )
    provenance = build_renderer_provenance(request_path, receipt_path, timeline_path)
    provenance_path = atomic_write_json(
        provenance, contract_dir / "renderer-provenance.json"
    )
    recovery = build_renderer_recovery(request_path, receipt_path)
    recovery_path = atomic_write_json(recovery, contract_dir / "renderer-recovery.json")
    review: dict[str, Any] = {
        "schema": RENDERER_REVIEW_PACKET_SCHEMA,
        "status": "awaiting_human_review",
        "inputs": {
            "foundation_review_packet": artifact_reference(
                root
                / "digital-human-foundation"
                / "contracts"
                / "human-review-packet.json",
                "foundation review packet",
            ),
            "renderer_request": artifact_reference(request_path, "renderer request"),
            "renderer_receipt": artifact_reference(receipt_path, "renderer receipt"),
            "candidate_timeline": artifact_reference(
                timeline_path, "renderer candidate timeline"
            ),
            "provenance": artifact_reference(provenance_path, "renderer provenance"),
            "recovery": artifact_reference(recovery_path, "renderer recovery"),
        },
        "quality_status": {
            "synthetic_fixture_only": True,
            "real_lip_sync_tested": False,
            "real_face_quality_tested": False,
            "personal_voice_tested": False,
        },
        "available_actions": ["inspect", "request_revision"],
        "authority_boundary": {
            "candidate_only": True,
            "review_packet_is_approval": False,
            "approval_record_created": False,
            "release_eligible": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    review["review_packet_sha256"] = payload_sha256(review, "review_packet_sha256")
    review_path = atomic_write_json(
        review, contract_dir / "renderer-review-packet.json"
    )
    validate_renderer_review_packet(review)
    receipt = read_json_object(receipt_path, "renderer receipt")
    run_receipt: dict[str, Any] = {
        "schema": RENDERER_RUN_RECEIPT_SCHEMA,
        "dispatch_id": dispatch_id,
        "status": _SUCCESS_STATUS,
        "artifacts": {
            "request": artifact_reference(request_path, "renderer request"),
            "provider_receipt": artifact_reference(
                receipt_path, "renderer provider receipt"
            ),
            "candidate_timeline": artifact_reference(
                timeline_path, "renderer candidate timeline"
            ),
            "provenance": artifact_reference(provenance_path, "renderer provenance"),
            "review_packet": artifact_reference(review_path, "renderer review packet"),
            "recovery": artifact_reference(recovery_path, "renderer recovery"),
        },
        "deterministic_identity": {
            "idempotency_key": request["idempotency_key"],
            "request_sha256": request["request_sha256"],
            "provider_receipt_sha256": receipt["receipt_sha256"],
            "candidate_timeline_sha256": timeline["timeline_sha256"],
            "provenance_sha256": provenance["provenance_sha256"],
        },
        "authority_boundary": {
            "candidate_only": True,
            "synthetic_only": True,
            "provider_called": False,
            "network_used": False,
            "upload_performed": False,
            "publish_performed": False,
            "personal_media_used": False,
            "customer_media_used": False,
            "second_timeline_orchestrator_created": False,
            "second_ffmpeg_orchestrator_created": False,
            "second_tts_state_created": False,
        },
    }
    run_receipt["receipt_sha256"] = payload_sha256(run_receipt, "receipt_sha256")
    run_receipt_path = atomic_write_json(
        run_receipt, contract_dir / "renderer-run-receipt.json"
    )
    validation = validate_digital_human_renderer_fixture_bundle(root)
    return {**validation, "run_receipt": str(run_receipt_path)}


def validate_renderer_review_packet(packet: dict[str, Any]) -> None:
    if packet.get("schema") != RENDERER_REVIEW_PACKET_SCHEMA:
        raise ContractError("unsupported renderer review packet schema")
    if packet.get("review_packet_sha256") != payload_sha256(
        packet, "review_packet_sha256"
    ):
        raise ContractError("renderer review packet integrity check failed")
    inputs = _object(packet.get("inputs"))
    if set(inputs) != {
        "foundation_review_packet",
        "renderer_request",
        "renderer_receipt",
        "candidate_timeline",
        "provenance",
        "recovery",
    }:
        raise ContractError("renderer review packet inputs are incomplete")
    request_path = validate_artifact_reference(
        _object(inputs["renderer_request"]), "renderer request"
    )
    receipt_path = validate_artifact_reference(
        _object(inputs["renderer_receipt"]), "renderer receipt"
    )
    timeline_path = validate_artifact_reference(
        _object(inputs["candidate_timeline"]), "renderer candidate timeline"
    )
    recovery_path = validate_artifact_reference(
        _object(inputs["recovery"]), "renderer recovery"
    )
    provenance_path = validate_artifact_reference(
        _object(inputs["provenance"]), "renderer provenance"
    )
    validate_artifact_reference(
        _object(inputs["foundation_review_packet"]), "foundation review packet"
    )
    request = read_json_object(request_path, "renderer request")
    receipt = read_json_object(receipt_path, "renderer receipt")
    timeline = read_json_object(timeline_path, "renderer candidate timeline")
    recovery = read_json_object(recovery_path, "renderer recovery")
    provenance = read_json_object(provenance_path, "renderer provenance")
    validate_avatar_renderer_receipt(request, receipt)
    validate_renderer_candidate_timeline(timeline)
    validate_renderer_recovery(recovery)
    validate_renderer_provenance(provenance)
    quality = _object(packet.get("quality_status"))
    boundary = _object(packet.get("authority_boundary"))
    if (
        packet.get("status") != "awaiting_human_review"
        or quality.get("synthetic_fixture_only") is not True
        or quality.get("real_lip_sync_tested") is not False
        or quality.get("real_face_quality_tested") is not False
        or quality.get("personal_voice_tested") is not False
        or set(packet.get("available_actions") or []) != {"inspect", "request_revision"}
        or boundary.get("candidate_only") is not True
        or boundary.get("review_packet_is_approval") is not False
        or boundary.get("approval_record_created") is not False
        or boundary.get("release_eligible") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
    ):
        raise ContractError("renderer review packet authority boundary is invalid")


def validate_digital_human_renderer_fixture_bundle(
    plan_root: str | Path,
) -> dict[str, Any]:
    root = Path(plan_root).expanduser().resolve()
    validate_digital_human_foundation_bundle(root)
    contract_dir = root / "digital-human-renderer" / "contracts"
    output_dir = root / "digital-human-renderer" / "provider-output"
    request_path = contract_dir / "renderer-request.json"
    receipt_path = output_dir / "renderer-receipt.json"
    timeline_path = contract_dir / "renderer-candidate-timeline.json"
    review_path = contract_dir / "renderer-review-packet.json"
    recovery_path = contract_dir / "renderer-recovery.json"
    provenance_path = contract_dir / "renderer-provenance.json"
    run_path = contract_dir / "renderer-run-receipt.json"
    request = read_json_object(request_path, "renderer request")
    receipt = read_json_object(receipt_path, "renderer receipt")
    timeline = read_json_object(timeline_path, "renderer candidate timeline")
    review = read_json_object(review_path, "renderer review packet")
    recovery = read_json_object(recovery_path, "renderer recovery")
    provenance = read_json_object(provenance_path, "renderer provenance")
    validate_avatar_renderer_receipt(request, receipt)
    validate_renderer_candidate_timeline(timeline)
    validate_renderer_review_packet(review)
    validate_renderer_recovery(recovery)
    validate_renderer_provenance(provenance)
    run = read_json_object(run_path, "renderer run receipt")
    if (
        run.get("schema") != RENDERER_RUN_RECEIPT_SCHEMA
        or run.get("receipt_sha256") != payload_sha256(run, "receipt_sha256")
        or run.get("status") != _SUCCESS_STATUS
    ):
        raise ContractError("renderer run receipt is invalid")
    for key, path in {
        "request": request_path,
        "provider_receipt": receipt_path,
        "candidate_timeline": timeline_path,
        "provenance": provenance_path,
        "review_packet": review_path,
        "recovery": recovery_path,
    }.items():
        if (
            validate_artifact_reference(
                _object(run["artifacts"][key]), f"renderer run artifact {key}"
            )
            != path.resolve()
        ):
            raise ContractError("renderer run receipt binds an unexpected artifact")
    if _object(run.get("deterministic_identity")) != {
        "idempotency_key": request["idempotency_key"],
        "request_sha256": request["request_sha256"],
        "provider_receipt_sha256": receipt["receipt_sha256"],
        "candidate_timeline_sha256": timeline["timeline_sha256"],
        "provenance_sha256": provenance["provenance_sha256"],
    }:
        raise ContractError("renderer run deterministic identity drifted")
    boundary = _object(run.get("authority_boundary"))
    if (
        boundary.get("candidate_only") is not True
        or boundary.get("synthetic_only") is not True
        or boundary.get("provider_called") is not False
        or boundary.get("network_used") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
        or boundary.get("personal_media_used") is not False
        or boundary.get("customer_media_used") is not False
        or boundary.get("second_timeline_orchestrator_created") is not False
        or boundary.get("second_ffmpeg_orchestrator_created") is not False
        or boundary.get("second_tts_state_created") is not False
    ):
        raise ContractError("renderer run authority boundary is invalid")
    return {
        "schema": "video_creation_pipeline.digital_human_renderer_validation.v1",
        "status": _SUCCESS_STATUS,
        "valid": True,
        "candidate_only": True,
        "synthetic_only": True,
        "idempotency_key": request["idempotency_key"],
        "request_sha256": request["request_sha256"],
        "provider_receipt_sha256": receipt["receipt_sha256"],
        "candidate_timeline_sha256": timeline["timeline_sha256"],
        "provenance_sha256": provenance["provenance_sha256"],
        "review_packet_sha256": review["review_packet_sha256"],
    }


def build_blocked_renderer_receipt(
    request: dict[str, Any], *, code: str, message: str, next_action: str
) -> dict[str, Any]:
    validate_avatar_renderer_request(request)
    if code not in _ERROR_CODES:
        raise ContractError("renderer failure code is not recognized")
    receipt: dict[str, Any] = {
        "schema": RENDERER_RECEIPT_SCHEMA,
        "request_sha256": request["request_sha256"],
        "idempotency_key": request["idempotency_key"],
        "provider": dict(request["provider"]),
        "adapter": dict(request["adapter"]),
        "status": "blocked",
        "outputs": None,
        "error": {
            "code": code,
            "message": str(message),
            "retryable": False,
            "next_action": str(next_action),
        },
        "execution": {
            "provider_called": False,
            "network_used": False,
            "upload_performed": False,
            "fallback_used": False,
            "retry_count": 0,
            "publish_performed": False,
        },
        "quality": None,
        "candidate_only": True,
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_avatar_renderer_receipt(request, receipt)
    return receipt


def _request_identity(request: dict[str, Any]) -> str:
    identity = {
        "mode": request.get("mode"),
        "provider": request.get("provider"),
        "adapter": request.get("adapter"),
        "capability_preflight": request.get("capability_preflight"),
        "inputs": request.get("inputs"),
        "target_slots": request.get("target_slots"),
        "correction_ranges": request.get("correction_ranges"),
        "rerecord_ranges": request.get("rerecord_ranges"),
        "output_requirements": request.get("output_requirements"),
        "execution_policy": request.get("execution_policy"),
        "authority_boundary": request.get("authority_boundary"),
    }
    return canonical_json_sha256(identity)


def _manifest_asset_projection(
    manifest: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for value in manifest.get("assets") or []:
        row = _object(value)
        result.setdefault(str(row.get("source_type") or ""), []).append(row)
    return result


def _single_asset(assets: dict[str, list[dict[str, Any]]], kind: str) -> dict[str, Any]:
    rows = assets.get(kind) or []
    if len(rows) != 1:
        raise ContractError(f"required source missing: {kind}")
    return rows[0]


def _validate_renderer_preflight(preflight: dict[str, Any]) -> None:
    if (
        preflight.get("schema")
        != "video_creation_pipeline.avatar_renderer_capability_preflight.v1"
        or preflight.get("provider") != SYNTHETIC_PROVIDER_ID
        or preflight.get("adapter") != SYNTHETIC_ADAPTER_ID
        or preflight.get("ready") is not True
        or preflight.get("probe_method") != "synthetic_fixture_static_probe"
        or preflight.get("installation_performed") is not False
        or preflight.get("network_used") is not False
    ):
        raise ContractError("renderer provider unavailable")


def _validate_synthetic_identity_authorization(
    authorization: dict[str, Any], kind: str
) -> None:
    if (
        authorization.get("schema")
        != "video_creation_pipeline.synthetic_identity_authorization.v1"
        or authorization.get("asset_kind") != kind
        or authorization.get("authorization_status") != "synthetic"
        or authorization.get("real_person_depicted") is not False
        or authorization.get("personal_biometric_data") is not False
        or authorization.get("remote_upload_allowed") is not False
        or authorization.get("commercial_use_claimed") is not False
    ):
        raise ContractError(f"renderer authorization missing: {kind}")


def _validate_tts_ready(resume_path: str | Path, qa_path: str | Path) -> None:
    resume = read_json_object(resume_path, "TTS resume plan")
    qa = read_json_object(qa_path, "audio stitch QA")
    if (
        _object(resume.get("summary")).get("status") != "complete"
        or _object(qa.get("readiness")).get("ready") is not True
    ):
        raise ContractError("TTS incomplete")


def _renderer_target_slots(interface: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _object(interface.get("inputs")).get("target_slots")
    if not isinstance(rows, list) or not rows:
        raise ContractError("renderer interface target slots are incomplete")
    result: list[dict[str, Any]] = []
    for value in rows:
        row = _object(value)
        result.append(
            {
                **dict(row),
                "narration_source_in_s": row["start_s"],
                "narration_source_out_s": row["end_s"],
            }
        )
    return result


def _validate_range(
    value: dict[str, Any], total: float, index: int, range_kind: str
) -> None:
    try:
        start = float(value.get("start_s"))
        end = float(value.get("end_s"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{range_kind} {index} is invalid") from exc
    if start < 0 or end <= start or end > total:
        raise ContractError(f"{range_kind} {index} is out of bounds")


def _validate_renderer_error(value: Any) -> None:
    error = _object(value)
    if (
        error.get("code") not in _ERROR_CODES
        or not str(error.get("message") or "").strip()
        or error.get("retryable") is not False
        or not str(error.get("next_action") or "").strip()
    ):
        raise ContractError("renderer failure requires a stable visible error")


def _write_renderer_fixture_assets(fixture_dir: Path) -> dict[str, Path]:
    preflight = {
        "schema": "video_creation_pipeline.avatar_renderer_capability_preflight.v1",
        "provider": SYNTHETIC_PROVIDER_ID,
        "adapter": SYNTHETIC_ADAPTER_ID,
        "ready": True,
        "probe_method": "synthetic_fixture_static_probe",
        "installation_performed": False,
        "network_used": False,
    }
    avatar = {
        "schema": "video_creation_pipeline.synthetic_avatar_asset.v1",
        "asset_id": "synthetic-avatar-001",
        "real_person_depicted": False,
        "personal_biometric_data": False,
    }
    voice = {
        "schema": "video_creation_pipeline.synthetic_voice_asset.v1",
        "asset_id": "synthetic-voice-001",
        "real_person_voice": False,
        "personal_biometric_data": False,
    }
    paths = {
        "preflight": atomic_write_json(preflight, fixture_dir / "preflight.json"),
        "avatar": atomic_write_json(avatar, fixture_dir / "avatar-asset.json"),
        "voice": atomic_write_json(voice, fixture_dir / "voice-asset.json"),
    }
    for kind in ("avatar", "voice"):
        authorization = {
            "schema": "video_creation_pipeline.synthetic_identity_authorization.v1",
            "asset_kind": kind,
            "authorization_status": "synthetic",
            "real_person_depicted": False,
            "personal_biometric_data": False,
            "remote_upload_allowed": False,
            "commercial_use_claimed": False,
        }
        paths[f"{kind}_authorization"] = atomic_write_json(
            authorization, fixture_dir / f"{kind}-authorization.json"
        )
    return paths


def _render_synthetic_avatar_slot(
    path: Path,
    *,
    request_path: Path,
    receipt_path: Path,
    duration_s: float,
    width: int,
    height: int,
    fps: int,
    timeout_s: float,
) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x1e3a8a:s={width}x{height}:r={fps}:d={duration_s:.3f}",
        "-an",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
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
        str(path),
    ]
    execute_ffmpeg_with_receipt(
        "render",
        command,
        [
            {
                "role": "avatar_renderer_request",
                "path": request_path,
            }
        ],
        [{"role": "synthetic_avatar_visual", "path": path}],
        receipt_path,
        timeout_s=timeout_s,
    )


def _renderer_probe(path: Path) -> dict[str, Any]:
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
        raise ContractError(f"renderer ffprobe failed: {result.stderr.strip()}")
    import json

    payload = json.loads(result.stdout)
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next(
        (
            row
            for row in streams
            if isinstance(row, dict) and row.get("codec_type") == "video"
        ),
        None,
    )
    audio = next(
        (
            row
            for row in streams
            if isinstance(row, dict) and row.get("codec_type") == "audio"
        ),
        None,
    )
    if not isinstance(video, dict):
        raise ContractError("renderer output lacks a video stream")
    duration_raw = video.get("duration") or _object(payload.get("format")).get(
        "duration"
    )
    try:
        duration = round(float(duration_raw), 3)
        numerator, denominator = str(video.get("avg_frame_rate") or "0/1").split("/", 1)
        fps = round(float(numerator) / float(denominator), 3)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ContractError("renderer output probe is incomplete") from exc
    return {
        "video_duration_s": duration,
        "audio_duration_s": None,
        "av_duration_delta_s": None,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "video_codec": str(video.get("codec_name") or ""),
        "pixel_format": str(video.get("pix_fmt") or ""),
        "has_audio_stream": audio is not None,
        "probe_method": "ffprobe_json",
    }


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("renderer contract requires an object")
    return value
