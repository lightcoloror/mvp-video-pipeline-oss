from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audio_mastering import execute_audio_mastering
from .contract_io import read_json_object
from .creative_alignment import (
    build_creative_alignment_plan,
    validate_creative_alignment_plan,
    write_creative_alignment_plan,
)
from .creative_gates import (
    bind_audio_mastering_render_manifest,
    build_approval_record,
    build_release_gate,
    write_audio_mastering_bound_render_manifest,
    write_creative_contract,
)
from .errors import ContractError
from .generation_contracts import (
    build_generation_task,
    build_shot_generation_unit,
    verify_generation_receipt_files,
    write_generation_contract,
)
from .nle_bridge import (
    build_premiere_import_job,
    validate_premiere_plugin_status,
    verify_premiere_import_files,
    write_premiere_import_job,
)
from .previs_contracts import (
    build_previs_scene,
    verify_previs_capture_files,
    write_previs_scene,
)
from .production_strategy import (
    build_audio_mastering_plan,
    build_visual_production_plan,
    write_production_strategy_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build auditable creative review, generation, previs, and NLE contracts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    unit = subparsers.add_parser("build-shot-unit")
    unit.add_argument("--edit-job", required=True)
    unit.add_argument("--scene-id", required=True)
    unit.add_argument("--generator", required=True)
    unit.add_argument("--media-type", choices=("image", "video"), default="video")
    unit.add_argument("--width", type=int, default=1080)
    unit.add_argument("--height", type=int, default=1920)
    unit.add_argument("--visual-prompt", default="")
    unit.add_argument("--output", required=True)

    visual = subparsers.add_parser("build-visual-strategy")
    visual.add_argument("--unit", required=True)
    visual.add_argument(
        "--argument-role",
        choices=("neutral", "system", "tension", "insight"),
        default="neutral",
    )
    visual.add_argument(
        "--effort-mode", choices=("efficient", "apex"), default="efficient"
    )
    visual.add_argument("--artifact-layer", action="store_true")
    visual.add_argument("--no-cinema-layer", action="store_false", dest="cinema_layer")
    visual.add_argument("--emphasis-phrase", action="append", default=[])
    visual.add_argument("--decomposition-adoption")
    visual.add_argument("--output", required=True)

    mastering = subparsers.add_parser("build-audio-mastering-plan")
    mastering.add_argument("--input-audio", required=True)
    mastering.add_argument("--output-audio", required=True)
    mastering.add_argument("--target-lufs", type=float)
    mastering.add_argument("--delivery-profile", choices=("web_spoken_candidate", "ebu_r128", "custom"))
    mastering.add_argument("--target-true-peak-db", type=float, default=-1.0)
    mastering.add_argument("--target-lra-lu", type=float, default=5.0)
    mastering.add_argument("--highpass-hz", type=float, default=80.0)
    mastering.add_argument("--limiter", type=float, default=0.7)
    mastering.add_argument("--sample-rate", type=int, default=48000)
    mastering.add_argument("--sample-width-bits", type=int, default=24)
    mastering.add_argument("--output", required=True)

    execute_mastering = subparsers.add_parser("execute-audio-mastering")
    execute_mastering.add_argument("--plan", required=True)
    execute_mastering.add_argument("--work-dir", required=True)
    execute_mastering.add_argument("--receipt", required=True)
    execute_mastering.add_argument("--timeout-s", type=float, default=180.0)

    alignment = subparsers.add_parser("build-creative-alignment")
    alignment.add_argument("--project-plan", required=True)
    alignment.add_argument("--cover-candidate", required=True)
    alignment.add_argument("--spec", required=True)
    alignment.add_argument("--output", required=True)

    validate_alignment = subparsers.add_parser("validate-creative-alignment")
    validate_alignment.add_argument("--plan", required=True)

    approve = subparsers.add_parser("approve")
    approve.add_argument("--target", required=True)
    approve.add_argument("--artifact", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--approved-at", required=True)
    approve.add_argument("--comment", default="")
    approve.add_argument("--attachment", action="append", default=[])
    approve.add_argument("--output", required=True)

    bind_mastering = subparsers.add_parser("bind-audio-mastering-render-manifest")
    bind_mastering.add_argument("--render-manifest", required=True)
    bind_mastering.add_argument("--audio-mastering-receipt", required=True)
    bind_mastering.add_argument("--voice-approval", required=True)
    bind_mastering.add_argument("--output", required=True)

    release = subparsers.add_parser("build-release-gate")
    release.add_argument("--approval", action="append", required=True)
    release.add_argument("--render-manifest", required=True)
    release.add_argument("--visual-plan", required=True)
    release.add_argument("--rough-cut-finalize")
    release.add_argument("--music-edit-plan")
    release.add_argument("--creative-alignment-plan")
    release.add_argument("--audio-mastering-receipt")
    release.add_argument("--technical-qa")
    release.add_argument(
        "--require-content-gate",
        action="append",
        choices=("insurance_content",),
        default=[],
    )
    release.add_argument("--insurance-content-scan")
    release.add_argument("--output", required=True)

    generation = subparsers.add_parser("build-generation-task")
    generation.add_argument("--unit", required=True)
    generation.add_argument("--storyboard-approval", required=True)
    generation.add_argument("--preflight", required=True)
    generation.add_argument("--output-dir", required=True)
    generation.add_argument("--design")
    generation.add_argument("--visual-strategy")
    generation.add_argument("--output", required=True)

    verify_generation = subparsers.add_parser("verify-generation-receipt")
    verify_generation.add_argument("--task", required=True)
    verify_generation.add_argument("--receipt", required=True)

    previs = subparsers.add_parser("build-previs-scene")
    previs.add_argument("--unit", required=True)
    previs.add_argument("--output", required=True)

    verify_previs = subparsers.add_parser("verify-previs-captures")
    verify_previs.add_argument("--scene", required=True)
    verify_previs.add_argument("--manifest", required=True)

    premiere = subparsers.add_parser("build-premiere-job")
    premiere.add_argument("--edit-job", required=True)
    premiere.add_argument("--selection", required=True)
    premiere.add_argument("--media-map", required=True)
    premiere.add_argument("--playhead", type=float, default=0.0)
    premiere.add_argument("--project-id", required=True)
    premiere.add_argument("--sequence-id", required=True)
    premiere.add_argument("--output", required=True)

    verify_premiere = subparsers.add_parser("verify-premiere-receipt")
    verify_premiere.add_argument("--task", required=True)
    verify_premiere.add_argument("--receipt", required=True)

    status = subparsers.add_parser("verify-premiere-status")
    status.add_argument("--status", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _execute(args)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "build-shot-unit":
        payload = build_shot_generation_unit(
            args.edit_job,
            args.scene_id,
            generator=args.generator,
            media_type=args.media_type,
            width=args.width,
            height=args.height,
            visual_prompt=args.visual_prompt,
        )
        output = write_generation_contract(payload, args.output)
        return _written(output, payload, "unit_sha256")
    if args.command == "build-visual-strategy":
        payload = build_visual_production_plan(
            args.unit,
            argument_role=args.argument_role,
            effort_mode=args.effort_mode,
            artifact_layer=args.artifact_layer,
            cinema_layer=args.cinema_layer,
            emphasis_phrases=args.emphasis_phrase,
            decomposition_adoption_path=args.decomposition_adoption,
        )
        output = write_production_strategy_contract(payload, args.output)
        return _written(output, payload, "plan_sha256")
    if args.command == "build-audio-mastering-plan":
        payload = build_audio_mastering_plan(
            args.input_audio,
            args.output_audio,
            target_lufs=args.target_lufs,
            delivery_profile=args.delivery_profile,
            target_true_peak_db=args.target_true_peak_db,
            target_lra_lu=args.target_lra_lu,
            highpass_hz=args.highpass_hz,
            limiter=args.limiter,
            sample_rate=args.sample_rate,
            sample_width_bits=args.sample_width_bits,
        )
        output = write_production_strategy_contract(payload, args.output)
        return _written(output, payload, "plan_sha256")
    if args.command == "execute-audio-mastering":
        return execute_audio_mastering(
            args.plan,
            args.work_dir,
            args.receipt,
            timeout_s=args.timeout_s,
        )
    if args.command == "build-creative-alignment":
        payload = build_creative_alignment_plan(
            args.project_plan,
            args.cover_candidate,
            args.spec,
        )
        output = write_creative_alignment_plan(payload, args.output)
        return _written(output, payload, "plan_sha256")
    if args.command == "validate-creative-alignment":
        payload = read_json_object(args.plan, "creative alignment plan")
        validate_creative_alignment_plan(payload)
        return {
            "schema": payload["schema"],
            "valid": True,
            "candidate_only": True,
            "plan_sha256": payload["plan_sha256"],
        }
    if args.command == "approve":
        payload = build_approval_record(
            args.target,
            args.artifact,
            approved_by=args.approved_by,
            approved_at=args.approved_at,
            comment=args.comment,
            attachments=_attachments(args.attachment),
        )
        output = write_creative_contract(payload, args.output)
        return _written(output, payload, "approval_record_sha256")
    if args.command == "bind-audio-mastering-render-manifest":
        payload = bind_audio_mastering_render_manifest(
            args.render_manifest,
            args.audio_mastering_receipt,
            args.voice_approval,
        )
        output = write_audio_mastering_bound_render_manifest(
            payload,
            args.output,
            source_manifest_path=args.render_manifest,
        )
        return {
            "output": str(output),
            "schema": (
                "video_creation_pipeline."
                "audio_mastering_render_manifest_binding_result.v1"
            ),
            "render_manifest_schema": payload.get("schema"),
            "render_manifest_sha256": payload["render_manifest_sha256"],
            "network_execution_performed": False,
            "render_execution_performed": False,
            "premiere_execution_performed": False,
        }
    if args.command == "build-release-gate":
        payload = build_release_gate(
            args.approval,
            render_manifest_path=args.render_manifest,
            visual_plan_path=args.visual_plan,
            rough_cut_finalize_path=args.rough_cut_finalize,
            music_edit_plan_path=args.music_edit_plan,
            creative_alignment_plan_path=args.creative_alignment_plan,
            audio_mastering_receipt_path=args.audio_mastering_receipt,
            technical_qa_path=args.technical_qa,
            required_content_gates=args.require_content_gate,
            insurance_content_scan_path=args.insurance_content_scan,
        )
        output = write_creative_contract(payload, args.output)
        return _written(output, payload, "release_gate_sha256")
    if args.command == "build-generation-task":
        payload = build_generation_task(
            args.unit,
            args.storyboard_approval,
            args.preflight,
            output_dir=args.output_dir,
            design_path=args.design,
            visual_strategy_path=args.visual_strategy,
        )
        output = write_generation_contract(payload, args.output)
        return _written(output, payload, "task_sha256")
    if args.command == "verify-generation-receipt":
        return verify_generation_receipt_files(args.task, args.receipt)
    if args.command == "build-previs-scene":
        payload = build_previs_scene(args.unit)
        output = write_previs_scene(payload, args.output)
        return _written(output, payload, "previs_scene_sha256")
    if args.command == "verify-previs-captures":
        return verify_previs_capture_files(args.scene, args.manifest)
    if args.command == "build-premiere-job":
        payload = build_premiere_import_job(
            args.edit_job,
            args.selection,
            args.media_map,
            playhead_s=args.playhead,
            project_id=args.project_id,
            sequence_id=args.sequence_id,
        )
        output = write_premiere_import_job(payload, args.output)
        return _written(output, payload, "task_sha256")
    if args.command == "verify-premiere-receipt":
        return verify_premiere_import_files(args.task, args.receipt)
    status = read_json_object(args.status, "Premiere plugin status")
    validate_premiere_plugin_status(status)
    return {
        "schema": status["schema"],
        "valid": True,
        "ready": True,
        "plugin_id": status.get("plugin_id"),
    }


def _attachments(values: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        role, separator, path = str(value).partition("=")
        if not separator or not role.strip() or not path.strip():
            raise ContractError("attachment must use role=path")
        result.append({"role": role.strip(), "path": path.strip()})
    return result


def _written(
    output: Path, payload: dict[str, object], digest_field: str
) -> dict[str, object]:
    return {
        "output": str(output),
        "schema": payload["schema"],
        digest_field: payload[digest_field],
        "network_execution_performed": False,
        "render_execution_performed": False,
        "premiere_execution_performed": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
