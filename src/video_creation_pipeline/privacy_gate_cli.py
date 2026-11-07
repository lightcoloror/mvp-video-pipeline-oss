from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object, validate_artifact_reference
from .errors import ContractError
from .privacy_exclusion_zones import (
    build_privacy_exclusion_zones,
    build_privacy_layout_collision_report,
    media_clock_from_redaction_receipt,
    project_identity_privacy_exclusion_zones,
    project_privacy_exclusion_zones,
    read_detection_capture,
    validate_privacy_exclusion_zones,
    write_privacy_exclusion_zones,
    write_privacy_layout_collision_report,
)
from .privacy_gate import (
    PRIVACY_REVIEW_DECISION_SCHEMA,
    PRIVACY_REVIEW_PLAN_SCHEMA,
    PRIVACY_SCAN_REPORT_SCHEMA,
    assert_privacy_gate_passed,
    build_privacy_review_decision,
    build_privacy_review_plan,
    privacy_gate_doctor,
    run_privacy_scan,
    validate_privacy_review_decision,
    validate_privacy_review_plan,
    validate_privacy_scan_report,
    write_privacy_review_decision,
    write_privacy_review_plan,
)
from .privacy_redaction import (
    adopt_redaction,
    build_cloakframe_handoff,
    build_redaction_job,
    preview_redaction,
    run_redaction,
    validate_redaction_job,
    validate_redaction_receipt,
    write_redaction_job,
)
from .semantic_motion import materialize_hyperframes_project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hash-bound privacy candidate scan and mandatory human review gate."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="Check local privacy gate dependencies"
    )
    doctor.add_argument("--sentryblur-executable")
    doctor.add_argument("--sentryblur-model-root")
    doctor.add_argument("--cloakframe-executable")

    build = subparsers.add_parser("build", help="Build a privacy review plan")
    build.add_argument("--final-artifact", required=True)
    build.add_argument("--terms", required=True)
    build.add_argument("--authorization-evidence")
    build.add_argument("--spec", required=True)
    build.add_argument("--output", required=True)

    scan = subparsers.add_parser("scan", help="Scan the actual final artifact")
    scan.add_argument("--plan", required=True)
    scan.add_argument("--output-dir", required=True)
    scan.add_argument("--report")
    scan.add_argument("--timeout", type=float, default=300.0)

    review = subparsers.add_parser("review", help="Record human dispositions")
    review.add_argument("--report", required=True)
    review.add_argument("--spec", required=True)
    review.add_argument("--output", required=True)

    validate = subparsers.add_parser("validate", help="Validate any privacy contract")
    validate.add_argument("--artifact", required=True)

    release = subparsers.add_parser(
        "check-release", help="Fail unless privacy review passed"
    )
    release.add_argument("--decision", required=True)
    redact_plan = subparsers.add_parser(
        "redact-plan", help="Build a third-party redaction job"
    )
    redact_plan.add_argument("--spec", required=True)
    redact_plan.add_argument("--output", required=True)
    redact_preview = subparsers.add_parser(
        "redact-preview", help="Preview automatic redaction or create GUI handoff"
    )
    redact_preview.add_argument("--job", required=True)
    redact_preview.add_argument("--output", required=True)
    redact_run = subparsers.add_parser(
        "redact-run", help="Run explicit SentryBlur stages"
    )
    redact_run.add_argument("--job", required=True)
    redact_run.add_argument("--receipt", required=True)
    redact_run.add_argument("--timeout", type=float, default=3600.0)
    redact_run.add_argument("--capture-zones", action="store_true")
    redact_handoff = subparsers.add_parser(
        "redact-handoff", help="Write manual CloakFrame handoff"
    )
    redact_handoff.add_argument("--job", required=True)
    redact_handoff.add_argument("--output", required=True)
    redact_adopt = subparsers.add_parser(
        "redact-adopt", help="Adopt an externally reviewed candidate"
    )
    redact_adopt.add_argument("--job", required=True)
    redact_adopt.add_argument("--candidate", required=True)
    redact_adopt.add_argument("--spec", required=True)
    redact_adopt.add_argument("--receipt", required=True)
    redact_validate = subparsers.add_parser(
        "redact-validate", help="Validate redaction job or receipt"
    )
    redact_validate.add_argument("--artifact", required=True)
    redact_zones = subparsers.add_parser(
        "redact-zones", help="Build hash-bound dynamic privacy exclusion zones"
    )
    redact_zones.add_argument("--receipt", required=True)
    redact_zones.add_argument("--output", required=True)
    redact_zones.add_argument("--media-clock")
    redact_zones.add_argument("--scene-cuts")
    redact_zones.add_argument("--reframe-plan")
    redact_zones.add_argument(
        "--identity-final-canvas",
        action="store_true",
        help="Declare that the redaction input already matches the final canvas",
    )
    redact_zones_validate = subparsers.add_parser(
        "redact-zones-validate", help="Validate privacy exclusion zones"
    )
    redact_zones_validate.add_argument("--artifact", required=True)
    layout_check = subparsers.add_parser(
        "redact-layout-check", help="Build a privacy layout collision report"
    )
    layout_check.add_argument("--semantic-motion", required=True)
    layout_check.add_argument("--zones", required=True)
    layout_check.add_argument("--output", required=True)
    layout_check.add_argument(
        "--redaction-order",
        choices=("redaction_before_graphics", "redaction_after_graphics"),
        default="redaction_before_graphics",
    )
    layout_debug = subparsers.add_parser(
        "redact-layout-debug", help="Materialize a candidate-only privacy debug HTML"
    )
    layout_debug.add_argument("--semantic-motion", required=True)
    layout_debug.add_argument("--project-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = privacy_gate_doctor(
                sentryblur_executable=args.sentryblur_executable,
                sentryblur_model_root=args.sentryblur_model_root,
                cloakframe_executable=args.cloakframe_executable,
            )
        elif args.command == "build":
            spec = read_json_object(args.spec, "privacy build spec")
            plan = build_privacy_review_plan(
                args.final_artifact,
                args.terms,
                authorization_evidence_path=args.authorization_evidence,
                evidence=list(spec.get("evidence") or []),
                subject_policy=dict(spec.get("subject_policy") or {}),
                required_lanes=spec.get("required_lanes"),
                visual_sample_interval_s=float(
                    spec.get("visual_sample_interval_s") or 1.0
                ),
                max_visual_frames=int(spec.get("max_visual_frames") or 300),
                ocr_language=str(spec.get("ocr_language") or "chi_sim"),
            )
            path = write_privacy_review_plan(plan, args.output)
            payload = {
                "status": "ready_for_privacy_scan",
                "plan": str(path),
                "plan_sha256": plan["plan_sha256"],
                "release_blocked": True,
            }
        elif args.command == "scan":
            report = run_privacy_scan(
                args.plan,
                args.output_dir,
                report_path=args.report,
                timeout_s=args.timeout,
            )
            payload = {
                "status": "human_review_required",
                "candidate_count": report["readiness"]["candidate_count"],
                "machine_scan_complete": report["readiness"]["machine_scan_complete"],
                "incomplete_required_lanes": report["readiness"][
                    "incomplete_required_lanes"
                ],
                "release_blocked": True,
            }
        elif args.command == "review":
            spec = read_json_object(args.spec, "privacy review spec")
            decision = build_privacy_review_decision(args.report, spec)
            path = write_privacy_review_decision(decision, args.output)
            payload = {
                "status": decision["outcome"]["status"],
                "decision": str(path),
                "privacy_gate_passed": decision["outcome"]["privacy_gate_passed"],
                "automatic_publish": False,
            }
        elif args.command == "validate":
            artifact = read_json_object(args.artifact, "privacy contract")
            schema = artifact.get("schema")
            if schema == PRIVACY_REVIEW_PLAN_SCHEMA:
                validate_privacy_review_plan(artifact)
            elif schema == PRIVACY_SCAN_REPORT_SCHEMA:
                validate_privacy_scan_report(artifact)
            elif schema == PRIVACY_REVIEW_DECISION_SCHEMA:
                validate_privacy_review_decision(artifact)
            else:
                raise ContractError("unsupported privacy contract schema")
            payload = {"status": "valid", "schema": schema}
        elif args.command == "redact-plan":
            job = build_redaction_job(read_json_object(args.spec, "redaction spec"))
            path = write_redaction_job(job, args.output)
            payload = {"status": "ready", "job": str(path), "publish_allowed": False}
        elif args.command == "redact-preview":
            result = preview_redaction(args.job, args.output)
            payload = result
        elif args.command == "redact-handoff":
            path = build_cloakframe_handoff(args.job, args.output)
            payload = {
                "status": "manual_review_required",
                "handoff": str(path),
                "publish_allowed": False,
            }
        elif args.command == "redact-run":
            receipt = run_redaction(
                args.job,
                args.receipt,
                timeout_s=args.timeout,
                capture_zones=args.capture_zones,
            )
            payload = {
                "status": receipt["status"],
                "receipt": str(Path(args.receipt).resolve()),
                "publish_allowed": False,
            }
        elif args.command == "redact-adopt":
            receipt = adopt_redaction(
                args.job,
                args.candidate,
                read_json_object(args.spec, "adoption spec"),
                args.receipt,
            )
            payload = {
                "status": receipt["status"],
                "receipt": str(Path(args.receipt).resolve()),
                "publish_allowed": False,
            }
        elif args.command == "redact-validate":
            artifact = read_json_object(args.artifact, "redaction contract")
            if (
                artifact.get("schema")
                == "video_creation_pipeline.privacy_redaction_job.v1"
            ):
                validate_redaction_job(artifact)
            elif (
                artifact.get("schema")
                == "video_creation_pipeline.privacy_redaction_receipt.v1"
            ):
                validate_redaction_receipt(artifact)
            else:
                raise ContractError("unsupported redaction contract schema")
            payload = {"status": "valid", "schema": artifact["schema"]}
        elif args.command == "redact-zones":
            if args.reframe_plan and args.identity_final_canvas:
                raise ContractError(
                    "redact-zones accepts either reframe projection or identity projection"
                )
            receipt_path = Path(args.receipt).expanduser().resolve()
            receipt = read_json_object(receipt_path, "redaction receipt")
            validate_redaction_receipt(receipt)
            job_path = validate_artifact_reference(receipt["job"], "redaction job")
            captures = [
                validate_artifact_reference(
                    stage["coordinate_capture"], "redaction coordinate capture"
                )
                for stage in receipt["stages"]
                if isinstance(stage, dict)
                and stage.get("coordinate_capture") is not None
            ]
            if not captures:
                raise ContractError(
                    "redaction receipt has no same-invocation coordinate captures"
                )
            clock = (
                _clock_spec(args.media_clock)
                if args.media_clock
                else media_clock_from_redaction_receipt(receipt)
            )
            scene_cuts = _scene_cut_spec(args.scene_cuts) if args.scene_cuts else []
            source_zones = build_privacy_exclusion_zones(
                receipt["input"]["path"],
                receipt["output"]["path"],
                job_path,
                receipt_path,
                read_detection_capture(captures),
                media_clock=clock,
                scene_cuts=scene_cuts,
            )
            output = Path(args.output).expanduser().resolve()
            if args.reframe_plan or args.identity_final_canvas:
                source_output = output.with_name(f"{output.stem}.source{output.suffix}")
                write_privacy_exclusion_zones(source_zones, source_output)
                if args.reframe_plan:
                    zones = project_privacy_exclusion_zones(
                        source_output, args.reframe_plan
                    )
                else:
                    zones = project_identity_privacy_exclusion_zones(
                        source_output,
                        target_width=int(clock["width"]),
                        target_height=int(clock["height"]),
                    )
                path = write_privacy_exclusion_zones(zones, output)
                payload = {
                    "status": zones["coverage"]["status"],
                    "source_zones": str(source_output),
                    "projected_zones": str(path),
                    "zones_sha256": zones["zones_sha256"],
                    "publish_allowed": False,
                }
            else:
                path = write_privacy_exclusion_zones(source_zones, output)
                payload = {
                    "status": source_zones["coverage"]["status"],
                    "zones": str(path),
                    "zones_sha256": source_zones["zones_sha256"],
                    "publish_allowed": False,
                }
        elif args.command == "redact-zones-validate":
            zones = read_json_object(args.artifact, "privacy exclusion zones")
            validate_privacy_exclusion_zones(zones)
            payload = {"status": "valid", "schema": zones["schema"]}
        elif args.command == "redact-layout-check":
            report = build_privacy_layout_collision_report(
                args.semantic_motion,
                args.zones,
                redaction_order=args.redaction_order,
            )
            path = write_privacy_layout_collision_report(report, args.output)
            payload = {
                "status": report["summary"]["status"],
                "report": str(path),
                "collision_count": report["summary"]["collision_count"],
                "geometry_gate_passed": report["summary"]["geometry_gate_passed"],
                "publish_allowed": False,
            }
        elif args.command == "redact-layout-debug":
            paths = materialize_hyperframes_project(
                args.semantic_motion, args.project_dir, privacy_debug=True
            )
            payload = {
                "status": "candidate_only_debug_ready",
                "project": {key: str(value) for key, value in paths.items()},
                "publish_allowed": False,
            }
        else:
            decision = read_json_object(args.decision, "privacy review decision")
            assert_privacy_gate_passed(decision)
            payload = {
                "status": "privacy_gate_passed",
                "decision": str(Path(args.decision).resolve()),
                "automatic_publish": False,
            }
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _clock_spec(path: str | Path) -> dict[str, object]:
    value = read_json_object(path, "privacy media clock")
    nested = value.get("media_clock")
    return dict(nested) if isinstance(nested, dict) else value


def _scene_cut_spec(path: str | Path) -> list[int]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"privacy scene cuts are invalid: {exc}") from exc
    if isinstance(value, dict):
        value = value.get("scene_cuts")
    if not isinstance(value, list):
        raise ContractError("privacy scene cuts must be a JSON array")
    return [int(item) for item in value]


if __name__ == "__main__":
    raise SystemExit(main())
