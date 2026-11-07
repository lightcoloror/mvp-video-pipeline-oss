from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .capability_truth import (
    build_capability_doctor,
    build_capability_truth,
    read_capability_truth_summary,
    validate_capability_truth,
    write_capability_doctor,
    write_capability_truth,
)
from .contract_io import read_json_object
from .errors import ContractError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a receipt-backed capability truth matrix for the video pipeline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="Write a local capability doctor artifact"
    )
    doctor.add_argument("--output", required=True)
    doctor.add_argument("--observed-at")
    doctor.add_argument("--repository-root")
    doctor.add_argument("--hyperframes-source-root")
    doctor.add_argument("--hyperframes-browser-path")
    doctor.add_argument("--hyperframes-ffmpeg-path")
    doctor.add_argument("--hyperframes-ffprobe-path")
    doctor.add_argument("--remotion-runtime-source")
    doctor.add_argument("--remotion-project-dir")
    doctor.add_argument("--remotion-browser-path")
    doctor.add_argument("--background-provider-source-root")
    doctor.add_argument("--background-model-path")
    doctor.add_argument("--background-runtime-dependencies-path")
    doctor.add_argument("--background-device", choices=("cpu", "cuda"), default="cpu")

    build = subparsers.add_parser(
        "build", help="Build a fail-closed capability truth matrix"
    )
    build.add_argument("--doctor", required=True)
    build.add_argument("--test-receipt")
    build.add_argument("--evidence", action="append", default=[])
    build.add_argument("--observed-at")
    build.add_argument("--test-freshness-hours", type=float, default=168.0)
    build.add_argument("--doctor-freshness-hours", type=float, default=1.0)
    build.add_argument("--output", required=True)

    validate = subparsers.add_parser(
        "validate", help="Revalidate a capability truth artifact"
    )
    validate.add_argument("--artifact", required=True)

    read = subparsers.add_parser(
        "read-artifact", help="Read a compact capability summary"
    )
    read.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            report = build_capability_doctor(
                observed_at=args.observed_at,
                repository_root=args.repository_root,
                hyperframes_source_root=args.hyperframes_source_root,
                hyperframes_browser_path=args.hyperframes_browser_path,
                hyperframes_ffmpeg_path=args.hyperframes_ffmpeg_path,
                hyperframes_ffprobe_path=args.hyperframes_ffprobe_path,
                remotion_runtime_source=args.remotion_runtime_source,
                remotion_project_dir=args.remotion_project_dir,
                remotion_browser_path=args.remotion_browser_path,
                background_provider_source_root=args.background_provider_source_root,
                background_model_path=args.background_model_path,
                background_runtime_dependencies_path=(
                    args.background_runtime_dependencies_path
                ),
                background_device=args.background_device,
            )
            output = write_capability_doctor(report, args.output)
            payload = {
                "status": "doctor_written",
                "artifact": str(output),
                "doctor_sha256": report["doctor_sha256"],
                "execution_performed": False,
            }
        elif args.command == "build":
            truth = build_capability_truth(
                args.doctor,
                evidence_paths=args.evidence,
                test_receipt_path=args.test_receipt,
                observed_at=args.observed_at,
                test_freshness_hours=args.test_freshness_hours,
                doctor_freshness_hours=args.doctor_freshness_hours,
            )
            output = write_capability_truth(truth, args.output)
            payload = {
                "status": "capability_truth_written",
                "artifact": str(output),
                "matrix_sha256": truth["matrix_sha256"],
                "summary": truth["summary"],
                "publish_authorized": False,
            }
        elif args.command == "validate":
            truth = read_json_object(args.artifact, "capability truth")
            validate_capability_truth(truth)
            payload = {
                "status": "valid",
                "artifact": str(Path(args.artifact).resolve()),
                "matrix_sha256": truth["matrix_sha256"],
                "publish_authorized": False,
            }
        else:
            payload = read_capability_truth_summary(args.artifact)
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
