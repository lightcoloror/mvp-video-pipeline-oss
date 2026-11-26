from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .contract_io import read_json_object
from .errors import ContractError
from .motion_sfx import (
    VIDEO_SHOTCRAFT_COMMIT,
    WARM_PAPER_WOOD_PACK_ID,
    bootstrap_warm_paper_wood_pack,
    build_motion_sfx_batch_receipt,
    build_motion_sfx_edit_plan,
    build_motion_sfx_source_manifest,
    derive_motion_sfx_cue_sheet,
    execute_motion_sfx_mix,
    prepare_motion_sfx_output_root,
    profile_paths_from_pack_receipt,
    validate_motion_sfx_contract,
    write_motion_sfx_contract,
)
from .stock_media import validate_stock_media_contract, write_stock_media_contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Project script, semantic motion, Remotion and scene boundaries into "
            "the existing sound-effect cue, selection and FFmpeg contracts."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--video-shotcraft-root")

    bootstrap = commands.add_parser("bootstrap-pack")
    bootstrap.add_argument("--video-shotcraft-root", required=True)
    bootstrap.add_argument("--output-root", required=True)
    bootstrap.add_argument(
        "--purpose", choices=("personal", "commercial"), default="commercial"
    )
    bootstrap.add_argument("--platform", default="social_media")

    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)

    build = commands.add_parser("build")
    _add_source_args(build)

    plan = commands.add_parser("plan")
    _add_source_args(plan)
    _add_profile_args(plan)
    plan.add_argument("--min-score", type=float, default=0.65)
    plan.add_argument("--min-margin", type=float, default=0.10)

    run = commands.add_parser("run")
    _add_source_args(run)
    _add_profile_args(run)
    run.add_argument("--program", required=True)
    run.add_argument("--candidate")
    run.add_argument("--timeout", type=float, default=120.0)
    run.add_argument("--deterministic", action="store_true")
    run.add_argument("--min-score", type=float, default=0.65)
    run.add_argument("--min-margin", type=float, default=0.10)
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


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "doctor":
        source_status = "not_supplied"
        if args.video_shotcraft_root:
            source = Path(args.video_shotcraft_root).expanduser().resolve()
            source_status = (
                "present"
                if source.is_dir() and (source / ".git").exists()
                else "missing_or_not_git"
            )
        ffmpeg = shutil.which("ffmpeg") is not None
        ffprobe = shutil.which("ffprobe") is not None
        return {
            "schema": "video_creation_pipeline.motion_sfx_doctor.v1",
            "status": "ready" if ffmpeg and ffprobe else "partial",
            "cue_projection": "available",
            "local_pack_bootstrap": source_status,
            "expected_video_shotcraft_commit": VIDEO_SHOTCRAFT_COMMIT,
            "palette": WARM_PAPER_WOOD_PACK_ID,
            "ffmpeg": "available" if ffmpeg else "blocked_missing_ffmpeg",
            "ffprobe": "available" if ffprobe else "blocked_missing_ffprobe",
            "online_model": "not_used",
            "automatic_download": False,
            "upload_performed": False,
            "publish_performed": False,
        }
    if args.command == "bootstrap-pack":
        result = bootstrap_warm_paper_wood_pack(
            args.video_shotcraft_root,
            args.output_root,
            purpose=args.purpose,
            platform=args.platform,
        )
        return {
            "schema": result["payload"]["schema"],
            "pack_id": result["payload"]["pack_id"],
            "receipt": str(result["receipt"]),
            "profiles": [str(path) for path in result["profiles"]],
            "pack_sha256": result["payload"]["pack_sha256"],
            "automatic_download": False,
            "candidate_only": True,
        }
    if args.command == "validate":
        path = Path(args.artifact).expanduser().resolve()
        payload = read_json_object(path, "motion SFX artifact")
        try:
            validate_motion_sfx_contract(payload)
        except ContractError as motion_error:
            try:
                validate_stock_media_contract(payload)
            except ContractError:
                raise motion_error
        return {"valid": True, "schema": payload["schema"], "artifact": str(path)}

    manifest = build_motion_sfx_source_manifest(
        args.packaging_plan,
        semantic_motion_plan_path=args.semantic_motion_plan,
        remotion_project_data_path=args.remotion_project_data,
        visual_routing_plan_path=args.visual_routing_plan,
        script_anchor_path=args.script_anchor,
        script_path=args.script,
        subtitle_path=args.subtitles,
        word_timestamps_path=args.word_timestamps,
        video_talkcraft_recipe_plan_path=args.video_talkcraft_recipe_plan,
        external_motion_selection_plan_path=args.external_motion_selection_plan,
        density_profile=args.density,
    )
    root = prepare_motion_sfx_output_root(args.output_root, manifest)
    manifest_path = write_motion_sfx_contract(
        manifest, root / "motion-sfx-source-manifest.json"
    )
    cue_sheet = derive_motion_sfx_cue_sheet(
        manifest_path, purpose=args.purpose, platform=args.platform
    )
    cue_path = write_stock_media_contract(
        cue_sheet, root / "sound-effect-cue-sheet.json"
    )
    if args.command == "build":
        receipt = build_motion_sfx_batch_receipt(manifest_path, cue_path)
        receipt_path = write_motion_sfx_contract(
            receipt, root / "motion-sfx-batch-receipt.json"
        )
        return _result(root, manifest_path, cue_path, receipt_path)

    profiles, pack_receipt = _profile_paths(args)
    edit_plan = build_motion_sfx_edit_plan(
        cue_path,
        profiles,
        min_score=args.min_score,
        min_margin=args.min_margin,
    )
    edit_path = write_stock_media_contract(
        edit_plan, root / "sound-effect-edit-plan.json"
    )
    if args.command == "plan":
        receipt = build_motion_sfx_batch_receipt(
            manifest_path,
            cue_path,
            edit_plan_path=edit_path,
            pack_receipt_path=pack_receipt,
        )
        receipt_path = write_motion_sfx_contract(
            receipt, root / "motion-sfx-batch-receipt.json"
        )
        result = _result(root, manifest_path, cue_path, receipt_path)
        result.update(
            {
                "edit_plan": str(edit_path),
                "render_eligible": edit_plan["readiness"]["render_eligible"],
            }
        )
        return result

    candidate = (
        Path(args.candidate).expanduser().resolve()
        if args.candidate
        else root / "candidate-with-motion-sfx.mp4"
    )
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError(
            "motion SFX candidate must remain inside output root"
        ) from exc
    ffmpeg_receipt = root / "sound-effect-ffmpeg-receipt.json"
    execute_motion_sfx_mix(
        args.program,
        edit_path,
        candidate,
        ffmpeg_receipt,
        timeout_s=args.timeout,
        deterministic=args.deterministic,
    )
    receipt = build_motion_sfx_batch_receipt(
        manifest_path,
        cue_path,
        edit_plan_path=edit_path,
        ffmpeg_receipt_path=ffmpeg_receipt,
        pack_receipt_path=pack_receipt,
    )
    receipt_path = write_motion_sfx_contract(
        receipt, root / "motion-sfx-batch-receipt.json"
    )
    result = _result(root, manifest_path, cue_path, receipt_path)
    result.update(
        {
            "edit_plan": str(edit_path),
            "candidate": str(candidate),
            "ffmpeg_receipt": str(ffmpeg_receipt),
            "candidate_only": True,
            "publish_performed": False,
        }
    )
    return result


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--packaging-plan", required=True)
    parser.add_argument("--semantic-motion-plan")
    parser.add_argument("--remotion-project-data")
    parser.add_argument("--visual-routing-plan")
    parser.add_argument("--script-anchor")
    parser.add_argument("--script")
    parser.add_argument("--subtitles")
    parser.add_argument("--word-timestamps")
    parser.add_argument("--video-talkcraft-recipe-plan")
    parser.add_argument("--external-motion-selection-plan")
    parser.add_argument(
        "--density", choices=("sparse", "balanced", "dense"), default="balanced"
    )
    parser.add_argument(
        "--purpose", choices=("personal", "commercial"), default="commercial"
    )
    parser.add_argument("--platform", default="social_media")
    parser.add_argument("--output-root", required=True)


def _add_profile_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", action="append")
    group.add_argument("--pack-receipt")


def _profile_paths(args: argparse.Namespace) -> tuple[list[Path], Path | None]:
    if args.pack_receipt:
        receipt = Path(args.pack_receipt).expanduser().resolve()
        return profile_paths_from_pack_receipt(receipt), receipt
    return [Path(value).expanduser().resolve() for value in args.profile], None


def _result(
    root: Path, manifest: Path, cue_sheet: Path, receipt: Path
) -> dict[str, Any]:
    payload = read_json_object(receipt, "motion SFX batch receipt")
    return {
        "schema": payload["schema"],
        "status": payload["status"],
        "output_root": str(root),
        "source_manifest": str(manifest),
        "cue_sheet": str(cue_sheet),
        "receipt": str(receipt),
        "receipt_sha256": payload["receipt_sha256"],
        "candidate_only": True,
        "automatic_download": False,
        "publish_performed": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
