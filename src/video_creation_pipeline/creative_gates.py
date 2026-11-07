from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .insurance_content_gate import (
    validate_insurance_content_policy_pack,
    validate_insurance_content_scan_report,
)
from .music_pipeline import validate_music_edit_plan
from .rough_cut_workspace import validate_rough_cut_finalize_receipt
from .technical_qa import validate_technical_qa_report

APPROVAL_RECORD_SCHEMA = "video_creation_pipeline.approval_record.v1"
LEGACY_RELEASE_GATE_SCHEMA = "video_creation_pipeline.release_gate.v1"
RELEASE_GATE_SCHEMA = "video_creation_pipeline.release_gate.v2"
APPROVAL_TARGETS = {"script", "voice", "storyboard", "materials", "visuals"}
RELEASE_REQUIRED_TARGETS = {"script", "voice", "visuals"}
SUPPORTED_CONTENT_GATES = {"insurance_content"}
CONTENT_GATE_MAX_AGE_HOURS = 168.0


def build_approval_record(
    target: str,
    artifact_path: str | Path,
    *,
    approved_by: str,
    approved_at: str,
    comment: str = "",
    attachments: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    normalized_target = str(target).strip().lower()
    if normalized_target not in APPROVAL_TARGETS:
        raise ContractError(f"unsupported approval target: {target}")
    actor = str(approved_by).strip()
    if not actor:
        raise ContractError("approval requires an identified operator")
    confirmed_at = _parse_datetime(approved_at)
    if confirmed_at is None:
        raise ContractError("approval requires a timezone-aware confirmation time")

    normalized_attachments = _attachment_references(attachments)
    roles = {row["role"] for row in normalized_attachments}
    if normalized_target == "voice":
        if "voice_measurements" not in roles or not any(
            role == "audio" or role.startswith("audio:") for role in roles
        ):
            raise ContractError(
                "voice approval requires voice_measurements and at least one audio attachment"
            )
    if normalized_target == "visuals" and "director_review_sheet" not in roles:
        raise ContractError(
            "visuals approval requires a director_review_sheet attachment"
        )

    record: dict[str, Any] = {
        "schema": APPROVAL_RECORD_SCHEMA,
        "target": normalized_target,
        "artifact": artifact_reference(artifact_path, f"{normalized_target} artifact"),
        "attachments": normalized_attachments,
        "attestation": {
            "confirmed": True,
            "method": "visible_operator_confirmation",
            "approved_by": actor,
            "approved_at": confirmed_at.isoformat(),
            "comment": str(comment),
        },
        "security_boundary": {
            "cryptographic_identity_claimed": False,
            "content_integrity_bound": True,
            "automatic_approval": False,
        },
    }
    record["approval_record_sha256"] = payload_sha256(record, "approval_record_sha256")
    validate_approval_record(record)
    return record


def validate_approval_record(record: dict[str, Any]) -> None:
    if record.get("schema") != APPROVAL_RECORD_SCHEMA:
        raise ContractError("unsupported approval record schema")
    target = str(record.get("target") or "")
    if target not in APPROVAL_TARGETS:
        raise ContractError("approval record target is invalid")
    if record.get("approval_record_sha256") != payload_sha256(
        record, "approval_record_sha256"
    ):
        raise ContractError("approval record integrity check failed")
    attestation = (
        record.get("attestation") if isinstance(record.get("attestation"), dict) else {}
    )
    if (
        attestation.get("confirmed") is not True
        or attestation.get("method") != "visible_operator_confirmation"
        or not str(attestation.get("approved_by") or "").strip()
        or _parse_datetime(attestation.get("approved_at")) is None
    ):
        raise ContractError("approval record lacks a valid operator attestation")
    security = (
        record.get("security_boundary")
        if isinstance(record.get("security_boundary"), dict)
        else {}
    )
    if (
        security.get("cryptographic_identity_claimed") is not False
        or security.get("content_integrity_bound") is not True
        or security.get("automatic_approval") is not False
    ):
        raise ContractError("approval record security boundary is invalid")
    artifact = (
        record.get("artifact") if isinstance(record.get("artifact"), dict) else {}
    )
    validate_artifact_reference(artifact, f"{target} artifact")
    attachments = record.get("attachments")
    if not isinstance(attachments, list):
        raise ContractError("approval attachments must be a list")
    roles: set[str] = set()
    for row in attachments:
        if not isinstance(row, dict):
            raise ContractError("approval attachment must be an object")
        role = str(row.get("role") or "").strip()
        if not role or role in roles:
            raise ContractError(
                "approval attachment roles must be non-empty and unique"
            )
        roles.add(role)
        reference = row.get("artifact") if isinstance(row.get("artifact"), dict) else {}
        validate_artifact_reference(reference, f"approval attachment {role}")
    if target == "voice" and (
        "voice_measurements" not in roles
        or not any(role == "audio" or role.startswith("audio:") for role in roles)
    ):
        raise ContractError("voice approval attachments are incomplete")
    if target == "visuals" and "director_review_sheet" not in roles:
        raise ContractError("visuals approval attachments are incomplete")


def build_release_gate(
    approval_paths: Iterable[str | Path],
    *,
    render_manifest_path: str | Path,
    visual_plan_path: str | Path,
    rough_cut_finalize_path: str | Path | None = None,
    music_edit_plan_path: str | Path | None = None,
    creative_alignment_plan_path: str | Path | None = None,
    audio_mastering_receipt_path: str | Path | None = None,
    technical_qa_path: str | Path | None = None,
    required_content_gates: Iterable[str] = (),
    insurance_content_scan_path: str | Path | None = None,
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    content_gate_requirements = sorted(
        {str(value).strip() for value in required_content_gates if str(value).strip()}
    )
    unknown_content_gates = set(content_gate_requirements) - SUPPORTED_CONTENT_GATES
    if unknown_content_gates:
        raise ContractError(
            "unsupported content gates: " + ", ".join(sorted(unknown_content_gates))
        )
    insurance_required = "insurance_content" in content_gate_requirements
    if insurance_required != (insurance_content_scan_path is not None):
        raise ContractError(
            "insurance content gate requirement and scan receipt must be supplied together"
        )
    approvals = _load_approval_files(approval_paths)
    targets = {row["target"] for row in approvals}
    missing = sorted(RELEASE_REQUIRED_TARGETS - targets)
    if missing:
        raise ContractError(f"release gate is missing approvals: {', '.join(missing)}")

    visual_reference = artifact_reference(visual_plan_path, "visual plan")
    visuals_approval = next(row for row in approvals if row["target"] == "visuals")
    if visuals_approval["record"]["artifact"] != visual_reference:
        raise ContractError("visuals approval does not bind the release visual plan")
    manifest_reference, manifest = _validated_render_manifest(
        render_manifest_path, visual_reference
    )
    technical_qa_reference = None
    candidate_video_sha256 = None
    if technical_qa_path is not None:
        technical_qa_reference, candidate_video = _validated_technical_qa_handoff(
            technical_qa_path,
            manifest,
        )
        candidate_video_sha256 = candidate_video["sha256"]
    rough_cut_reference = None
    if rough_cut_finalize_path is not None:
        rough_cut_reference = artifact_reference(
            rough_cut_finalize_path, "rough-cut finalize receipt"
        )
        rough_cut = read_json_object(
            rough_cut_reference["path"], "rough-cut finalize receipt"
        )
        validate_rough_cut_finalize_receipt(rough_cut)
        if rough_cut.get("readiness", {}).get("render_eligible") is not True:
            raise ContractError("release gate requires a resolved rough-cut receipt")
        if (
            manifest.get("rough_cut_finalize_sha256")
            != rough_cut["finalize_receipt_sha256"]
        ):
            raise ContractError("render manifest is stale for the resolved rough cut")
    music_reference = None
    music_plan_sha256 = None
    if music_edit_plan_path is not None:
        music_reference = artifact_reference(music_edit_plan_path, "music edit plan")
        music_plan = read_json_object(music_reference["path"], "music edit plan")
        validate_music_edit_plan(music_plan)
        if music_plan.get("readiness", {}).get("render_eligible") is not True:
            raise ContractError("release gate requires a render-eligible music plan")
        music_plan_sha256 = music_plan["music_edit_plan_sha256"]
        if manifest.get("music_edit_plan_sha256") != music_plan_sha256:
            raise ContractError("render manifest is stale for the music edit plan")
    alignment_reference = None
    alignment_plan_sha256 = None
    final_voice_audio_sha256s = None
    if creative_alignment_plan_path is not None:
        (
            alignment_reference,
            alignment_plan_sha256,
            final_voice_audio_sha256s,
        ) = _validated_alignment_handoff(
            creative_alignment_plan_path,
            approvals,
            manifest,
        )
    audio_mastering_reference = None
    audio_mastering_receipt_sha256 = None
    mastered_audio_sha256 = None
    audio_mastering_status = "not_claimed"
    if audio_mastering_receipt_path is not None:
        (
            audio_mastering_reference,
            audio_mastering_receipt_sha256,
            mastered_audio_sha256,
        ) = _validated_audio_mastering_handoff(
            audio_mastering_receipt_path,
            approvals,
            manifest,
        )
        audio_mastering_status = "output_verified"
    elif _render_manifest_claims_audio_mastering(manifest):
        raise ContractError(
            "render manifest claims audio mastering without a v2 execution receipt"
        )
    insurance_content_reference = None
    insurance_content_report_sha256 = None
    if insurance_content_scan_path is not None:
        (
            insurance_content_reference,
            insurance_content_report_sha256,
        ) = _validated_insurance_content_handoff(
            insurance_content_scan_path,
            at=created_at,
        )
    gate: dict[str, Any] = {
        "schema": RELEASE_GATE_SCHEMA,
        "created_at": created_at.isoformat(),
        "approvals": [
            {
                "target": row["target"],
                "path": str(row["path"]),
                "sha256": row["file_reference"]["sha256"],
                "approval_record_sha256": row["record"]["approval_record_sha256"],
            }
            for row in approvals
        ],
        "visual_plan": visual_reference,
        "render_manifest": manifest_reference,
        "render_manifest_visual_plan_sha256": manifest["visual_plan_sha256"],
        "render_manifest_rough_cut_finalize_sha256": (
            manifest.get("rough_cut_finalize_sha256")
            if rough_cut_reference is not None
            else None
        ),
        "rough_cut_finalize": rough_cut_reference,
        "music_edit_plan": music_reference,
        "render_manifest_music_edit_plan_sha256": music_plan_sha256,
        "creative_alignment_plan": alignment_reference,
        "render_manifest_creative_alignment_plan_sha256": alignment_plan_sha256,
        "render_manifest_final_voice_audio_sha256s": final_voice_audio_sha256s,
        "audio_mastering_execution_receipt": audio_mastering_reference,
        "render_manifest_audio_mastering_receipt_sha256": (
            audio_mastering_receipt_sha256
        ),
        "render_manifest_mastered_audio_sha256": mastered_audio_sha256,
        "audio_mastering_status": audio_mastering_status,
        "technical_qa": technical_qa_reference,
        "render_manifest_candidate_video_sha256": candidate_video_sha256,
        "required_content_gates": content_gate_requirements,
        "insurance_content_scan": insurance_content_reference,
        "insurance_content_report_sha256": insurance_content_report_sha256,
        "postproduction_sequence": _postproduction_sequence(),
        "workflow_locks": _workflow_locks(
            rough_cut_bound=rough_cut_reference is not None,
            technical_qa_bound=technical_qa_reference is not None,
        ),
        "release_ready": technical_qa_reference is not None,
        "next_action": (
            "human_release_review"
            if technical_qa_reference is not None
            else "run_export_technical_qa"
        ),
        "automatic_publish": False,
    }
    gate["release_gate_sha256"] = payload_sha256(gate, "release_gate_sha256")
    validate_release_gate(gate)
    return gate


def validate_release_gate(gate: dict[str, Any]) -> None:
    schema = gate.get("schema")
    if schema not in {LEGACY_RELEASE_GATE_SCHEMA, RELEASE_GATE_SCHEMA}:
        raise ContractError("unsupported release gate schema")
    if gate.get("release_gate_sha256") != payload_sha256(gate, "release_gate_sha256"):
        raise ContractError("release gate integrity check failed")
    if gate.get("automatic_publish") is not False:
        raise ContractError("release gate boundary is invalid")
    if schema == LEGACY_RELEASE_GATE_SCHEMA:
        if (
            gate.get("release_ready") is not True
            or gate.get("next_action") != "human_release_review"
        ):
            raise ContractError("legacy release gate boundary is invalid")
    else:
        technical_qa_bound = gate.get("technical_qa") is not None
        expected_action = (
            "human_release_review" if technical_qa_bound else "run_export_technical_qa"
        )
        if (
            gate.get("release_ready") is not technical_qa_bound
            or gate.get("next_action") != expected_action
        ):
            raise ContractError("release gate technical QA boundary is invalid")
        _validate_postproduction_sequence(gate.get("postproduction_sequence"))

    required_content_gates = gate.get("required_content_gates", [])
    if not isinstance(required_content_gates, list) or len(
        required_content_gates
    ) != len(set(required_content_gates)):
        raise ContractError("release gate content gate requirements are invalid")
    if set(required_content_gates) - SUPPORTED_CONTENT_GATES:
        raise ContractError("release gate contains unsupported content gates")
    insurance_required = "insurance_content" in required_content_gates
    insurance_reference = gate.get("insurance_content_scan")
    if insurance_required != (insurance_reference is not None):
        raise ContractError("release gate insurance content binding is incomplete")
    if insurance_reference is not None:
        insurance_path = validate_artifact_reference(
            insurance_reference if isinstance(insurance_reference, dict) else {},
            "insurance content scan report",
        )
        _, report_sha256 = _validated_insurance_content_handoff(
            insurance_path,
            at=_parse_datetime(gate.get("created_at")),
        )
        if gate.get("insurance_content_report_sha256") != report_sha256:
            raise ContractError("release gate insurance content binding drifted")
    elif gate.get("insurance_content_report_sha256") is not None:
        raise ContractError(
            "release gate cannot claim insurance content without a scan report"
        )

    approvals = gate.get("approvals")
    if not isinstance(approvals, list):
        raise ContractError("release gate approvals must be a list")
    seen: set[str] = set()
    records: dict[str, dict[str, Any]] = {}
    for row in approvals:
        if not isinstance(row, dict):
            raise ContractError("release gate approval reference must be an object")
        target = str(row.get("target") or "")
        if target in seen:
            raise ContractError("release gate approval targets must be unique")
        seen.add(target)
        path = Path(str(row.get("path") or "")).expanduser().resolve()
        reference = artifact_reference(path, f"{target} approval record")
        if reference["sha256"] != row.get("sha256"):
            raise ContractError(f"{target} approval record changed after gate creation")
        record = read_json_object(path, f"{target} approval record")
        validate_approval_record(record)
        if record.get("target") != target or record.get(
            "approval_record_sha256"
        ) != row.get("approval_record_sha256"):
            raise ContractError(f"{target} approval reference is inconsistent")
        records[target] = record
    missing = sorted(RELEASE_REQUIRED_TARGETS - set(records))
    if missing:
        raise ContractError(f"release gate is missing approvals: {', '.join(missing)}")

    visual_reference = (
        gate.get("visual_plan") if isinstance(gate.get("visual_plan"), dict) else {}
    )
    validate_artifact_reference(visual_reference, "visual plan")
    if records["visuals"]["artifact"] != visual_reference:
        raise ContractError("visuals approval no longer matches the visual plan")
    manifest_reference = (
        gate.get("render_manifest")
        if isinstance(gate.get("render_manifest"), dict)
        else {}
    )
    manifest_path = validate_artifact_reference(manifest_reference, "render manifest")
    manifest = read_json_object(manifest_path, "render manifest")
    expected = visual_reference.get("canonical_sha256") or visual_reference.get(
        "sha256"
    )
    if (
        manifest.get("visual_plan_sha256") != expected
        or gate.get("render_manifest_visual_plan_sha256") != expected
    ):
        raise ContractError("render manifest is stale for the approved visual plan")
    technical_qa_reference = gate.get("technical_qa")
    if schema == RELEASE_GATE_SCHEMA and technical_qa_reference is not None:
        technical_qa_path = validate_artifact_reference(
            technical_qa_reference if isinstance(technical_qa_reference, dict) else {},
            "technical QA report",
        )
        _, candidate_video = _validated_technical_qa_handoff(
            technical_qa_path,
            manifest,
        )
        if (
            gate.get("render_manifest_candidate_video_sha256")
            != candidate_video["sha256"]
        ):
            raise ContractError("release gate candidate video binding drifted")
    elif (
        schema == RELEASE_GATE_SCHEMA
        and gate.get("render_manifest_candidate_video_sha256") is not None
    ):
        raise ContractError(
            "release gate cannot claim a candidate video without technical QA"
        )
    rough_cut_reference = gate.get("rough_cut_finalize")
    if rough_cut_reference is not None:
        rough_cut_path = validate_artifact_reference(
            rough_cut_reference if isinstance(rough_cut_reference, dict) else {},
            "rough-cut finalize receipt",
        )
        rough_cut = read_json_object(rough_cut_path, "rough-cut finalize receipt")
        validate_rough_cut_finalize_receipt(rough_cut)
        if rough_cut.get("readiness", {}).get("render_eligible") is not True:
            raise ContractError("release gate rough-cut receipt is unresolved")
        expected_rough_cut = rough_cut["finalize_receipt_sha256"]
        if (
            manifest.get("rough_cut_finalize_sha256") != expected_rough_cut
            or gate.get("render_manifest_rough_cut_finalize_sha256")
            != expected_rough_cut
        ):
            raise ContractError("render manifest is stale for the resolved rough cut")
    elif gate.get("render_manifest_rough_cut_finalize_sha256") is not None:
        raise ContractError(
            "release gate cannot claim a rough-cut binding without a receipt"
        )

    music_reference = gate.get("music_edit_plan")
    if music_reference is not None:
        music_path = validate_artifact_reference(
            music_reference if isinstance(music_reference, dict) else {},
            "music edit plan",
        )
        music_plan = read_json_object(music_path, "music edit plan")
        validate_music_edit_plan(music_plan)
        if music_plan.get("readiness", {}).get("render_eligible") is not True:
            raise ContractError("release gate music plan is unresolved")
        expected_music = music_plan["music_edit_plan_sha256"]
        if (
            manifest.get("music_edit_plan_sha256") != expected_music
            or gate.get("render_manifest_music_edit_plan_sha256") != expected_music
        ):
            raise ContractError("render manifest is stale for the music edit plan")
    elif gate.get("render_manifest_music_edit_plan_sha256") is not None:
        raise ContractError(
            "release gate cannot claim a music-plan binding without a plan"
        )
    alignment_reference = gate.get("creative_alignment_plan")
    if alignment_reference is not None:
        alignment_path = validate_artifact_reference(
            alignment_reference if isinstance(alignment_reference, dict) else {},
            "creative alignment plan",
        )
        (
            _expected_reference,
            expected_plan_sha256,
            expected_voice_sha256s,
        ) = _validated_alignment_handoff(alignment_path, records.values(), manifest)
        if (
            gate.get("render_manifest_creative_alignment_plan_sha256")
            != expected_plan_sha256
            or gate.get("render_manifest_final_voice_audio_sha256s")
            != expected_voice_sha256s
        ):
            raise ContractError("release gate creative alignment binding drifted")
    elif (
        gate.get("render_manifest_creative_alignment_plan_sha256") is not None
        or gate.get("render_manifest_final_voice_audio_sha256s") is not None
    ):
        raise ContractError(
            "release gate cannot claim creative alignment without a plan"
        )
    audio_mastering_reference = gate.get("audio_mastering_execution_receipt")
    if audio_mastering_reference is not None:
        audio_mastering_path = validate_artifact_reference(
            (
                audio_mastering_reference
                if isinstance(audio_mastering_reference, dict)
                else {}
            ),
            "audio mastering execution receipt",
        )
        (
            _expected_reference,
            expected_receipt_sha256,
            expected_mastered_audio_sha256,
        ) = _validated_audio_mastering_handoff(
            audio_mastering_path,
            records.values(),
            manifest,
        )
        if (
            gate.get("render_manifest_audio_mastering_receipt_sha256")
            != expected_receipt_sha256
            or gate.get("render_manifest_mastered_audio_sha256")
            != expected_mastered_audio_sha256
            or gate.get("audio_mastering_status") != "output_verified"
        ):
            raise ContractError("release gate audio mastering binding drifted")
    else:
        if _render_manifest_claims_audio_mastering(manifest):
            raise ContractError(
                "render manifest claims audio mastering without a v2 execution receipt"
            )
        if (
            gate.get("render_manifest_audio_mastering_receipt_sha256") is not None
            or gate.get("render_manifest_mastered_audio_sha256") is not None
            or gate.get("audio_mastering_status") not in {None, "not_claimed"}
        ):
            raise ContractError(
                "release gate cannot claim audio mastering without a v2 receipt"
            )
    if schema == RELEASE_GATE_SCHEMA:
        expected_locks = _workflow_locks(
            rough_cut_bound=rough_cut_reference is not None,
            technical_qa_bound=technical_qa_reference is not None,
        )
        if gate.get("workflow_locks") != expected_locks:
            raise ContractError("release gate workflow lock projection drifted")


def bind_audio_mastering_render_manifest(
    render_manifest_path: str | Path,
    audio_mastering_receipt_path: str | Path,
    voice_approval_path: str | Path,
) -> dict[str, Any]:
    manifest = read_json_object(render_manifest_path, "render manifest")
    _validate_optional_render_manifest_integrity(manifest)
    if not str(manifest.get("visual_plan_sha256") or "").strip():
        raise ContractError("render manifest lacks a visual plan binding")
    voice_approval = read_json_object(voice_approval_path, "voice approval")
    validate_approval_record(voice_approval)
    if voice_approval.get("target") != "voice":
        raise ContractError("audio mastering manifest binding requires voice approval")
    (
        _receipt_reference,
        receipt_sha256,
        mastered_audio_sha256,
    ) = _validated_audio_mastering_receipt(
        audio_mastering_receipt_path,
        [voice_approval],
    )
    if _render_manifest_claims_audio_mastering(manifest):
        _validate_manifest_audio_mastering_claim(
            manifest,
            receipt_sha256=receipt_sha256,
            mastered_audio_sha256=mastered_audio_sha256,
        )
    bound = dict(manifest)
    bound["audio_mastering_receipt_sha256"] = receipt_sha256
    bound["mastered_audio_sha256"] = mastered_audio_sha256
    bound["render_manifest_sha256"] = payload_sha256(bound, "render_manifest_sha256")
    validate_audio_mastering_bound_render_manifest(bound)
    return bound


def validate_audio_mastering_bound_render_manifest(manifest: dict[str, Any]) -> None:
    _validate_optional_render_manifest_integrity(manifest, required=True)
    if not str(manifest.get("visual_plan_sha256") or "").strip():
        raise ContractError("render manifest lacks a visual plan binding")
    for field in ("audio_mastering_receipt_sha256", "mastered_audio_sha256"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"render manifest {field} is invalid")
    _validate_manifest_audio_mastering_claim(
        manifest,
        receipt_sha256=str(manifest["audio_mastering_receipt_sha256"]),
        mastered_audio_sha256=str(manifest["mastered_audio_sha256"]),
    )


def write_audio_mastering_bound_render_manifest(
    manifest: dict[str, Any],
    output_path: str | Path,
    *,
    source_manifest_path: str | Path,
) -> Path:
    output = Path(output_path).expanduser().resolve()
    source = Path(source_manifest_path).expanduser().resolve()
    if output == source:
        raise ContractError("bound render manifest output must differ from its source")
    validate_audio_mastering_bound_render_manifest(manifest)
    return atomic_write_json(manifest, output)


def write_creative_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    schema = payload.get("schema")
    if schema == APPROVAL_RECORD_SCHEMA:
        validate_approval_record(payload)
    elif schema == RELEASE_GATE_SCHEMA:
        validate_release_gate(payload)
    else:
        raise ContractError("unsupported creative gate contract")
    return atomic_write_json(payload, output_path)


def _validated_insurance_content_handoff(
    report_path: str | Path,
    *,
    at: datetime | None,
) -> tuple[dict[str, Any], str]:
    if at is None:
        raise ContractError("release gate requires a timezone-aware creation time")
    reference = artifact_reference(report_path, "insurance content scan report")
    report = read_json_object(reference["path"], "insurance content scan report")
    validate_insurance_content_scan_report(report)
    created_at = _parse_datetime(report.get("created_at"))
    if created_at is None or created_at > at:
        raise ContractError("insurance content scan report time is invalid")
    age_hours = (at - created_at).total_seconds() / 3600.0
    if age_hours > CONTENT_GATE_MAX_AGE_HOURS:
        raise ContractError("insurance content scan report is stale")
    policy_reference = report.get("policy_pack")
    policy_path = validate_artifact_reference(
        policy_reference if isinstance(policy_reference, dict) else {},
        "insurance content policy pack",
    )
    policy = read_json_object(policy_path, "insurance content policy pack")
    validate_insurance_content_policy_pack(policy, at=at)
    readiness = (
        report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    )
    if (
        readiness.get("status") != "review_ready_candidate"
        or readiness.get("machine_gate_passed") is not True
        or readiness.get("render_candidate_eligible") is not True
        or readiness.get("publish_authorized") is not False
    ):
        raise ContractError("insurance content scan is not review-ready")
    return reference, str(report["report_sha256"])


def _attachment_references(
    attachments: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    roles: set[str] = set()
    for raw in attachments:
        if not isinstance(raw, dict):
            raise ContractError("approval attachment must be an object")
        role = str(raw.get("role") or "").strip()
        if not role or role in roles:
            raise ContractError(
                "approval attachment roles must be non-empty and unique"
            )
        roles.add(role)
        normalized.append(
            {
                "role": role,
                "artifact": artifact_reference(
                    str(raw.get("path") or ""), f"approval attachment {role}"
                ),
            }
        )
    return normalized


def _load_approval_files(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in paths:
        path = Path(source).expanduser().resolve()
        record = read_json_object(path, "approval record")
        validate_approval_record(record)
        target = str(record["target"])
        if target in seen:
            raise ContractError(f"duplicate approval target: {target}")
        seen.add(target)
        rows.append(
            {
                "target": target,
                "path": path,
                "file_reference": artifact_reference(path, f"{target} approval record"),
                "record": record,
            }
        )
    return rows


def _validated_render_manifest(
    render_manifest_path: str | Path,
    visual_reference: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    reference = artifact_reference(render_manifest_path, "render manifest")
    manifest = read_json_object(reference["path"], "render manifest")
    _validate_optional_render_manifest_integrity(manifest)
    expected = visual_reference.get("canonical_sha256") or visual_reference.get(
        "sha256"
    )
    if manifest.get("visual_plan_sha256") != expected:
        raise ContractError("render manifest is stale for the approved visual plan")
    return reference, manifest


def _validated_technical_qa_handoff(
    technical_qa_path: str | Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    reference = artifact_reference(technical_qa_path, "technical QA report")
    report = read_json_object(reference["path"], "technical QA report")
    validate_technical_qa_report(report)
    readiness = _object(report.get("readiness"))
    if (
        readiness.get("status") != "passed"
        or readiness.get("technical_ready") is not True
    ):
        raise ContractError("release gate requires passed export technical QA")
    media_reference = _object(_object(report.get("inputs")).get("media"))
    validate_artifact_reference(media_reference, "technical QA candidate video")
    manifest_candidate = _object(manifest.get("candidate_video"))
    if manifest_candidate != media_reference:
        raise ContractError(
            "technical QA report is not bound to the render manifest candidate video"
        )
    manifest_qa = _object(manifest.get("technical_qa"))
    if manifest_qa != reference:
        raise ContractError("render manifest is stale for the technical QA report")
    return reference, media_reference


def _postproduction_sequence() -> dict[str, Any]:
    return {
        "temporary_dialogue_cleanup_before_structure_lock": True,
        "temporary_subtitles_before_picture_lock": True,
        "final_audio_mix_after_picture_lock": True,
        "final_subtitles_after_picture_lock": True,
        "final_color_and_graphics_after_picture_lock": True,
        "export_technical_qa_before_release_review": True,
        "derived_policy_only": True,
        "second_pipeline_created": False,
    }


def _validate_postproduction_sequence(value: Any) -> None:
    if _object(value) != _postproduction_sequence():
        raise ContractError("release gate postproduction sequence is invalid")


def _workflow_locks(
    *,
    rough_cut_bound: bool,
    technical_qa_bound: bool,
) -> dict[str, Any]:
    return {
        "structure_lock": {
            "status": "bound" if rough_cut_bound else "not_bound",
            "basis": (
                "rough_cut_finalize_receipt"
                if rough_cut_bound
                else "external_or_not_supplied"
            ),
        },
        "picture_lock": {
            "status": "bound",
            "basis": "visuals_approval_plus_render_manifest",
        },
        "release_lock": {
            "status": "bound" if technical_qa_bound else "blocked_missing_export_qa",
            "basis": (
                "render_manifest_plus_passed_technical_qa"
                if technical_qa_bound
                else "technical_qa_required"
            ),
        },
        "derived_projection_only": True,
    }


def _validated_alignment_handoff(
    alignment_plan_path: str | Path,
    approvals: Iterable[dict[str, Any]],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str]]:
    from .creative_alignment import (
        temporary_voice_sha256s,
        validate_creative_alignment_plan,
    )

    reference = artifact_reference(alignment_plan_path, "creative alignment plan")
    plan = read_json_object(reference["path"], "creative alignment plan")
    validate_creative_alignment_plan(plan)
    records = []
    for row in approvals:
        if isinstance(row, dict) and isinstance(row.get("record"), dict):
            records.append(row["record"])
        elif isinstance(row, dict):
            records.append(row)
    voice = next((row for row in records if row.get("target") == "voice"), None)
    if voice is None:
        raise ContractError("creative alignment release requires voice approval")
    validate_approval_record(voice)
    final_audio_sha256s = sorted(
        str(_object(row.get("artifact")).get("sha256") or "")
        for row in voice["attachments"]
        if str(row.get("role") or "") == "audio"
        or str(row.get("role") or "").startswith("audio:")
    )
    if not final_audio_sha256s:
        raise ContractError("creative alignment release requires approved final audio")
    if temporary_voice_sha256s(plan).intersection(final_audio_sha256s):
        raise ContractError("temporary voice must be replaced before release")
    if manifest.get("creative_alignment_plan_sha256") != plan["plan_sha256"]:
        raise ContractError("render manifest is stale for creative alignment")
    if manifest.get("final_voice_audio_sha256s") != final_audio_sha256s:
        raise ContractError("render manifest is stale for the approved final voice")
    return reference, plan["plan_sha256"], final_audio_sha256s


def _validated_audio_mastering_handoff(
    receipt_path: str | Path,
    approvals: Iterable[dict[str, Any]],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    reference, receipt_sha256, mastered_audio_sha256 = (
        _validated_audio_mastering_receipt(receipt_path, approvals)
    )
    _validate_manifest_audio_mastering_claim(
        manifest,
        receipt_sha256=receipt_sha256,
        mastered_audio_sha256=mastered_audio_sha256,
    )
    return reference, receipt_sha256, mastered_audio_sha256


def _validated_audio_mastering_receipt(
    receipt_path: str | Path,
    approvals: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], str, str]:
    from .audio_mastering import (
        AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
        validate_audio_mastering_execution_receipt,
    )

    reference = artifact_reference(receipt_path, "audio mastering execution receipt")
    receipt = read_json_object(reference["path"], "audio mastering execution receipt")
    validate_audio_mastering_execution_receipt(receipt)
    if receipt.get("schema") != AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA:
        raise ContractError(
            "release audio mastering claim requires a v2 output-verified receipt"
        )
    output_quality = _object(receipt.get("output_quality"))
    authority = _object(receipt.get("authority_boundary"))
    if (
        output_quality.get("technical_ready") is not True
        or output_quality.get("blocking_failures") != []
        or authority.get("output_loudness_verified") is not True
        or authority.get("candidate_only") is not True
        or authority.get("creative_approval_claimed") is not False
    ):
        raise ContractError("audio mastering receipt is not output-verified")

    mastered_audio = _object(receipt.get("mastered_audio"))
    validate_artifact_reference(mastered_audio, "mastered audio")
    mastered_audio_sha256 = str(mastered_audio.get("sha256") or "")
    records = []
    for row in approvals:
        if isinstance(row, dict) and isinstance(row.get("record"), dict):
            records.append(row["record"])
        elif isinstance(row, dict):
            records.append(row)
    voice = next((row for row in records if row.get("target") == "voice"), None)
    if voice is None:
        raise ContractError("audio mastering release requires voice approval")
    validate_approval_record(voice)
    approved_audio_sha256s = {
        str(_object(row.get("artifact")).get("sha256") or "")
        for row in voice["attachments"]
        if str(row.get("role") or "") == "audio"
        or str(row.get("role") or "").startswith("audio:")
    }
    if mastered_audio_sha256 not in approved_audio_sha256s:
        raise ContractError("mastered audio is not bound to the voice approval")

    receipt_sha256 = str(receipt.get("receipt_sha256") or "")
    return reference, receipt_sha256, mastered_audio_sha256


def _validate_manifest_audio_mastering_claim(
    manifest: dict[str, Any],
    *,
    receipt_sha256: str,
    mastered_audio_sha256: str,
) -> None:
    if manifest.get("audio_mastering_receipt_sha256") != receipt_sha256:
        raise ContractError("render manifest is stale for audio mastering")
    if manifest.get("mastered_audio_sha256") != mastered_audio_sha256:
        raise ContractError("render manifest is stale for the mastered audio")
    manifest_final_audio = manifest.get("final_voice_audio_sha256s")
    if manifest_final_audio is not None:
        if not isinstance(manifest_final_audio, list) or not all(
            isinstance(value, str) and value for value in manifest_final_audio
        ):
            raise ContractError("render manifest final voice hashes are invalid")
        if mastered_audio_sha256 not in set(manifest_final_audio):
            raise ContractError(
                "render manifest final voice excludes the mastered audio"
            )


def _validate_optional_render_manifest_integrity(
    manifest: dict[str, Any],
    *,
    required: bool = False,
) -> None:
    integrity = manifest.get("render_manifest_sha256")
    if integrity is None:
        if required:
            raise ContractError("render manifest integrity hash is missing")
        return
    if not isinstance(integrity, str) or integrity != payload_sha256(
        manifest, "render_manifest_sha256"
    ):
        raise ContractError("render manifest integrity check failed")


def _render_manifest_claims_audio_mastering(manifest: dict[str, Any]) -> bool:
    return any(
        manifest.get(field) is not None
        for field in (
            "audio_mastering_receipt_sha256",
            "mastered_audio_sha256",
        )
    )


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
