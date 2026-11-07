from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .contract_io import read_json_object
from .errors import ContractError
from .stock_media import (
    build_licensed_media_asset,
    build_licensed_media_catalog,
    build_music_profile_from_licensed_asset,
    build_sound_effect_cue_sheet,
    build_sound_effect_edit_plan,
    build_sound_effect_profile,
    build_stock_source_registry,
    execute_sound_effect_mix,
    validate_stock_media_contract,
    write_stock_media_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest license-bound stock media and plan local BGM/SFX use."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor")

    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)

    sources = commands.add_parser("list-sources")
    sources.add_argument("--output")

    ingest = commands.add_parser("ingest")
    ingest.add_argument("--media", required=True)
    ingest.add_argument("--license-snapshot", required=True)
    ingest.add_argument("--spec", required=True)
    ingest.add_argument(
        "--purpose", choices=("personal", "commercial"), default="commercial"
    )
    ingest.add_argument("--platform", required=True)
    ingest.add_argument("--output", required=True)

    catalog = commands.add_parser("build-catalog")
    catalog.add_argument("--asset", action="append", required=True)
    catalog.add_argument(
        "--purpose", choices=("personal", "commercial"), default="commercial"
    )
    catalog.add_argument("--platform", required=True)
    catalog.add_argument("--output", required=True)

    music = commands.add_parser("build-music-profile")
    music.add_argument("--asset", required=True)
    music.add_argument("--analysis-spec", required=True)
    music.add_argument("--output", required=True)

    sfx_profile = commands.add_parser("build-sfx-profile")
    sfx_profile.add_argument("--asset", required=True)
    sfx_profile.add_argument("--spec", required=True)
    sfx_profile.add_argument("--output", required=True)

    cues = commands.add_parser("build-sfx-cues")
    cues.add_argument("--source", required=True)
    cues.add_argument("--spec", required=True)
    cues.add_argument(
        "--purpose", choices=("personal", "commercial"), default="commercial"
    )
    cues.add_argument("--platform", required=True)
    cues.add_argument("--output", required=True)

    plan = commands.add_parser("build-sfx-plan")
    plan.add_argument("--cues", required=True)
    plan.add_argument("--profile", action="append", required=True)
    plan.add_argument("--min-score", type=float, default=0.65)
    plan.add_argument("--min-margin", type=float, default=0.10)
    plan.add_argument("--output", required=True)

    mix = commands.add_parser("mix-sfx")
    mix.add_argument("--program", required=True)
    mix.add_argument("--plan", required=True)
    mix.add_argument("--output", required=True)
    mix.add_argument("--receipt", required=True)
    mix.add_argument("--timeout", type=float, default=120.0)
    mix.add_argument("--deterministic", action="store_true")
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
    if args.command == "doctor":
        registry = build_stock_source_registry()
        ffmpeg_available = shutil.which("ffmpeg") is not None
        return {
            "status": "ready" if ffmpeg_available else "partial",
            "source_count": len(registry["sources"]),
            "commercial_ingest": "local_file_with_license_provenance",
            "official_api_search": "not_implemented",
            "bgm_profile_binding": "available",
            "bgm_semantic_retrieval": (
                "available"
                if importlib.util.find_spec("msclap") is not None
                else "blocked_missing_optional_runtime"
            ),
            "sound_effect_tag_retrieval": "available",
            "sound_effect_ffmpeg_mix": (
                "available" if ffmpeg_available else "blocked_missing_ffmpeg"
            ),
            "automatic_download": False,
            "network_execution_performed": False,
            "publication_performed": False,
        }
    if args.command == "validate":
        payload = read_json_object(args.artifact, "stock media artifact")
        validate_stock_media_contract(payload)
        return {
            "valid": True,
            "schema": payload["schema"],
            "artifact": str(Path(args.artifact).expanduser().resolve()),
        }
    if args.command == "list-sources":
        payload = build_stock_source_registry()
        if args.output:
            path = write_stock_media_contract(payload, args.output)
            return _written(path, payload, "registry_sha256")
        return payload
    if args.command == "ingest":
        spec = read_json_object(args.spec, "stock media ingestion specification")
        payload = build_licensed_media_asset(
            args.media,
            args.license_snapshot,
            spec,
            purpose=args.purpose,
            platform=args.platform,
        )
        path = write_stock_media_contract(payload, args.output)
        return _written(path, payload, "asset_manifest_sha256")
    if args.command == "build-catalog":
        payload = build_licensed_media_catalog(
            args.asset, purpose=args.purpose, platform=args.platform
        )
        path = write_stock_media_contract(payload, args.output)
        return _written(path, payload, "catalog_sha256")
    if args.command == "build-music-profile":
        spec = read_json_object(args.analysis_spec, "music analysis specification")
        payload = build_music_profile_from_licensed_asset(args.asset, spec)
        path = write_stock_media_contract(payload, args.output)
        return _written(path, payload, "profile_sha256")
    if args.command == "build-sfx-profile":
        spec = read_json_object(args.spec, "sound effect profile specification")
        payload = build_sound_effect_profile(
            args.asset,
            duration_s=spec.get("duration_s"),
            category=str(spec.get("category") or ""),
            tags=spec.get("tags") or [],
            description=str(spec.get("description") or ""),
        )
        path = write_stock_media_contract(payload, args.output)
        return _written(path, payload, "profile_sha256")
    if args.command == "build-sfx-cues":
        spec = read_json_object(args.spec, "sound effect cue specification")
        payload = build_sound_effect_cue_sheet(
            args.source,
            spec.get("cues") or [],
            timeline_duration_s=spec.get("timeline_duration_s"),
            purpose=args.purpose,
            platform=args.platform,
            semantic_spotting=(
                spec.get("semantic_spotting") if "semantic_spotting" in spec else None
            ),
        )
        path = write_stock_media_contract(payload, args.output)
        return _written(path, payload, "cue_sheet_sha256")
    if args.command == "build-sfx-plan":
        payload = build_sound_effect_edit_plan(
            args.cues,
            args.profile,
            min_score=args.min_score,
            min_margin=args.min_margin,
        )
        path = write_stock_media_contract(payload, args.output)
        result = _written(path, payload, "sound_effect_edit_plan_sha256")
        result["render_eligible"] = payload["readiness"]["render_eligible"]
        return result
    receipt = execute_sound_effect_mix(
        args.program,
        args.plan,
        args.output,
        args.receipt,
        timeout_s=args.timeout,
        deterministic=args.deterministic,
    )
    return {
        "schema": receipt["schema"],
        "receipt": str(Path(args.receipt).expanduser().resolve()),
        "output": str(Path(args.output).expanduser().resolve()),
        "receipt_sha256": receipt["receipt_sha256"],
        "candidate_only": True,
        "publication_performed": False,
    }


def _written(path: Path, payload: dict[str, Any], hash_field: str) -> dict[str, object]:
    return {
        "schema": payload["schema"],
        "output": str(path),
        hash_field: payload[hash_field],
        "network_execution_performed": False,
        "automatic_download": False,
        "publication_performed": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
