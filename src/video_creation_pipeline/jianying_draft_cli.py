from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .jianying_draft_handoff import (
    JIANYING_DESKTOP_VALIDATION_SCHEMA,
    JIANYING_PROJECT_FOLDER_RECEIPT_SCHEMA,
    JIANYING_SPOKEN_DRAFT_JOB_SCHEMA,
    build_jianying_draft_job,
    build_jianying_spoken_draft_job,
    materialize_jianying_draft,
    materialize_jianying_project_folder,
    record_jianying_desktop_validation,
    validate_jianying_desktop_validation_receipt,
    validate_jianying_draft_job,
    validate_jianying_draft_receipt,
    validate_jianying_project_folder_receipt,
    validate_jianying_spoken_draft_job,
    vectcut_doctor,
    write_jianying_draft_job,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a candidate-only Jianying draft payload via fixed VectCut source."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--vectcut-source")

    build = commands.add_parser("build")
    build.add_argument("--edit-job", required=True)
    build.add_argument("--selection", required=True)
    build.add_argument("--media-map", required=True)
    build.add_argument("--vectcut-source", required=True)
    build.add_argument("--width", type=int, default=1080)
    build.add_argument("--height", type=int, default=1920)
    build.add_argument("--fps", type=int, default=30)
    build.add_argument("--output", required=True)

    build_spoken = commands.add_parser("build-spoken")
    build_spoken.add_argument("--spoken-edit-plan", required=True)
    build_spoken.add_argument("--delivery-request", required=True,
                              help="Explicit editable_project request with editor=jianying")
    build_spoken.add_argument("--vectcut-source", required=True)
    build_spoken.add_argument("--width", type=int, default=1080)
    build_spoken.add_argument("--height", type=int, default=1920)
    build_spoken.add_argument("--fps", type=int, default=30)
    build_spoken.add_argument("--output", required=True)

    materialize = commands.add_parser("materialize")
    materialize.add_argument("--job", required=True)
    materialize.add_argument("--output-dir", required=True)
    materialize.add_argument("--python")
    materialize.add_argument("--timeout", type=float, default=120.0)

    project = commands.add_parser("materialize-project")
    project.add_argument("--draft-receipt", required=True)
    project.add_argument("--project-dir", required=True)
    project.add_argument("--python")
    project.add_argument("--timeout", type=float, default=120.0)

    desktop = commands.add_parser("record-desktop-validation")
    desktop.add_argument("--project-receipt", required=True)
    desktop.add_argument("--output", required=True)
    desktop.add_argument("--jianying-version", required=True)
    desktop.add_argument("--validated-by", required=True)
    desktop.add_argument("--observed-segment-count", required=True, type=int)
    desktop.add_argument("--project-opened", action="store_true")
    desktop.add_argument("--editable-timeline-confirmed", action="store_true")
    desktop.add_argument("--original-audio-present", action="store_true")
    desktop.add_argument("--audio-sync-confirmed", action="store_true")
    desktop.add_argument("--project-saved-by-jianying", action="store_true")
    desktop.add_argument("--evidence", action="append", default=[])

    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _execute(args)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "doctor":
        return vectcut_doctor(args.vectcut_source)
    if args.command == "build":
        payload = build_jianying_draft_job(
            args.edit_job,
            args.selection,
            args.media_map,
            args.vectcut_source,
            width=args.width,
            height=args.height,
            fps=args.fps,
        )
        path = write_jianying_draft_job(payload, args.output)
        return {
            "schema": payload["schema"],
            "output": str(path),
            "job_sha256": payload["job_sha256"],
            "candidate_only": True,
            "nle_import_performed": False,
        }
    if args.command == "build-spoken":
        payload = build_jianying_spoken_draft_job(
            args.spoken_edit_plan,
            args.vectcut_source,
            delivery_request_path=args.delivery_request,
            width=args.width,
            height=args.height,
            fps=args.fps,
        )
        path = write_jianying_draft_job(payload, args.output)
        return {
            "schema": payload["schema"],
            "output": str(path),
            "job_sha256": payload["job_sha256"],
            "candidate_only": True,
            "source": "spoken_edit_plan",
        }
    if args.command == "materialize":
        receipt = materialize_jianying_draft(
            args.job,
            args.output_dir,
            python_executable=args.python,
            timeout_s=args.timeout,
        )
        return {
            "schema": receipt["schema"],
            "output": receipt["draft_content"]["path"],
            "receipt": str(
                Path(args.output_dir).expanduser().resolve()
                / "jianying-draft-receipt.json"
            ),
            "candidate_only": True,
            "jianying_runtime_validated": False,
        }
    if args.command == "materialize-project":
        receipt = materialize_jianying_project_folder(
            args.draft_receipt,
            args.project_dir,
            python_executable=args.python,
            timeout_s=args.timeout,
        )
        return {
            "schema": receipt["schema"],
            "project_dir": receipt["project_dir"],
            "full_project_folder_generated": True,
            "jianying_runtime_validated": False,
            "next_action": receipt["next_action"],
        }
    if args.command == "record-desktop-validation":
        receipt = record_jianying_desktop_validation(
            args.project_receipt,
            args.output,
            jianying_version=args.jianying_version,
            validated_by=args.validated_by,
            observed_segment_count=args.observed_segment_count,
            project_opened=args.project_opened,
            editable_timeline_confirmed=args.editable_timeline_confirmed,
            original_audio_present=args.original_audio_present,
            audio_sync_confirmed=args.audio_sync_confirmed,
            project_saved_by_jianying=args.project_saved_by_jianying,
            evidence_paths=args.evidence,
        )
        return {
            "schema": receipt["schema"],
            "output": str(Path(args.output).expanduser().resolve()),
            "status": receipt["status"],
            "candidate_only": True,
        }
    payload = read_json_object(args.artifact, "Jianying draft artifact")
    schema = payload.get("schema")
    if schema == JIANYING_SPOKEN_DRAFT_JOB_SCHEMA:
        validate_jianying_spoken_draft_job(payload)
    elif schema == JIANYING_PROJECT_FOLDER_RECEIPT_SCHEMA:
        validate_jianying_project_folder_receipt(payload)
    elif schema == JIANYING_DESKTOP_VALIDATION_SCHEMA:
        validate_jianying_desktop_validation_receipt(payload)
    elif str(schema).endswith("export_job.v1"):
        validate_jianying_draft_job(payload)
    else:
        validate_jianying_draft_receipt(payload)
    return {"valid": True, "schema": payload["schema"], "artifact": args.artifact}


if __name__ == "__main__":
    raise SystemExit(main())
