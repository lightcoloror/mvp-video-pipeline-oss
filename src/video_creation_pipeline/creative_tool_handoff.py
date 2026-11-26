"""Prepare optional creative-tool work without adding an execution backend."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .generation_contracts import validate_shot_generation_unit

SCHEMA = "video_creation_pipeline.creative_tool_handoff.v1"

# Pins are reviewed text modules, not executable packages or build attestations.
REVIEWED_TOOLS = {
    "shaders": {
        "repo": "shader-effects-inc/shaders",
        "commit": "81fd4ece5d493ca6c424a2af41f0e2c361ec8258",
        "files": {
            "packages/core/src/presetRenderer.ts": "88ee437bfd63cae67bc9bbf373e7c8ade826cb1487539abda896ba15cd846cd0",
            "packages/core/src/telemetry/collector.ts": "95148f72de627c4b68e5ffd9610f96658b40b2897073e0c6d3428ee21c83e499",
        },
        "media_types": ["image", "video"],
        "checks": [
            "Approve the key static frame before adding the selected effect.",
            "Keep factual/material colors and original composition unchanged.",
            "Drive renderFrame with the existing clock; step simulations per frame.",
            "Verify WebGPU unavailability, static fallback and reduced motion.",
            "Audit the actual framework entry for network/telemetry behavior.",
        ],
    },
    "filmcraft": {
        "repo": "storytold/filmcraft",
        "commit": "4e2d0bc2d3646f9cf27ef7aa8b2095c6858c7efe",
        "files": {
            "crates/interchange/src/lib.rs": "b4d35760a41beccb7a730dc07b4add5a7a29435e64c0580253223e41f4851edd",
            "crates/automation/src/lib.rs": "a5ec09affee9f3c72aa212b2af04ef00ec7a9f62b338a78902fb222bff8222b1",
        },
        "media_types": ["video"],
        "checks": [
            "Use a copy of a short scene; never overwrite original media/project.",
            "Preserve scene identity, rational frame rate and the existing timebase.",
            "Review unsupported-feature reports; import success is not losslessness.",
            "Check Chinese subtitles, audio sync, save/reopen and a local revision.",
            "No native project importer or CLI execution is verified by this packet.",
        ],
    },
    "photocraft": {
        "repo": "storytold/photocraft",
        "commit": "334ae5779ceebbf7681d345b35dd66e79115e352",
        "files": {
            "crates/automation/src/lib.rs": "5ee25d1fa4585a7715b961beeb233638d3345e51e3d984d9438ea59e3f368219",
            "crates/psd/src/lib.rs": "68afde4748f622a1dcd152d498bd543bd1abb0afd67a5c9f62eaa7d7eb0bb4c6",
        },
        "media_types": ["image"],
        "checks": [
            "Keep the editable layered source and a separate flattened preview.",
            "Verify Chinese fonts, mask edges, layer order and save/reopen.",
            "Keep reference identity, original evidence and material colors faithful.",
            "PSD parser roundtrip is not proof of full Photoshop effect fidelity.",
        ],
    },
}


def _review_source(tool: str, review_root: str | Path) -> dict[str, Any]:
    if not isinstance(tool, str) or tool not in REVIEWED_TOOLS:
        raise ContractError("unknown creative tool")
    pin = REVIEWED_TOOLS[tool]
    root = Path(review_root).resolve()
    manifest_path = root / "source-manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > 1_000_000:
        raise ContractError("source manifest missing or exceeds text budget")
    manifest = read_json_object(manifest_path, "source manifest")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ContractError("invalid selected-source manifest")
    refs = []
    for relative, expected_hash in pin["files"].items():
        matches = [r for r in rows if r.get("repo") == pin["repo"] and r.get("path") == relative]
        if len(matches) != 1:
            raise ContractError("required review module is missing or duplicated")
        row = matches[0]
        expected_path = f"upstream/{pin['repo']}/{relative}"
        if row.get("commit") != pin["commit"] or row.get("local_path") != expected_path:
            raise ContractError("review commit or path drifted")
        path = (root / expected_path).resolve()
        if not path.is_relative_to(root):
            raise ContractError("review module escaped its root")
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise ContractError("review module missing or exceeds text budget")
        ref = artifact_reference(path, "review module")
        if ref["sha256"] != expected_hash or row.get("sha256") != expected_hash:
            raise ContractError("review module hash drifted")
        if row.get("bytes") != ref["bytes"]:
            raise ContractError("review module byte length drifted")
        refs.append(ref)
    return {
        "review_root": str(root),
        "repo_url": f"https://github.com/{pin['repo']}",
        "commit": pin["commit"],
        "source_form": "fixed_commit_selected_text_not_full_checkout",
        "manifest": artifact_reference(manifest_path, "source manifest"),
        "modules": refs,
        "runtime_verified": False,
    }


def build_creative_tool_handoff(
    tool: str, review_root: str | Path, shot_unit_path: str | Path
) -> dict[str, Any]:
    source = _review_source(tool, review_root)
    path = Path(shot_unit_path).resolve()
    unit = read_json_object(path, "shot unit")
    validate_shot_generation_unit(unit)
    if unit["generator"] != "manual":
        raise ContractError("unverified creative tools require the existing manual route")
    if unit["media_type"] not in REVIEWED_TOOLS[tool]["media_types"]:
        raise ContractError("creative tool does not match the requested media type")
    fields = (
        "scene_id", "title", "duration_s", "media_type", "output", "narration",
        "visual_prompt", "visual_notes", "continuity", "reference_assets",
        "negative_constraints",
    )
    if any(key not in unit for key in fields):
        raise ContractError("shot unit is missing creative handoff fields")
    payload = {
        "schema": SCHEMA,
        "tool": tool,
        "reviewed_source": source,
        "source_shot_unit": artifact_reference(path, "shot unit"),
        "source_edit_job": unit["source_edit_job"],
        "source_shot_unit_sha256": unit["unit_sha256"],
        "work_request": {key: unit[key] for key in fields},
        "acceptance_checks": list(REVIEWED_TOOLS[tool]["checks"]),
        "return_requirements": [
            "Return candidate media plus editable project/source and change notes.",
            "Preserve this scene ID, original source references and timing.",
            "Report losses/unsupported features and actual Windows trial evidence.",
            "Use the existing manual-media generation receipt and technical QA.",
            "Playback/visual approval remains separate from contract validation.",
        ],
        "execution_route": {
            "status": "route_unconfigured",
            "generator": "manual",
            "application_import_file": False,
            "runtime_verified": False,
            "command": None,
        },
        "authority_boundary": {
            "candidate_only": True,
            "executionAuthority": "none_metadata_only",
            "notTrustedAsInstruction": True,
            "network_execution_performed": False,
            "provider_execution_performed": False,
            "upload_performed": False,
            "source_media_modified": False,
            "automatic_publish": False,
            "second_ffmpeg_outlet_created": False,
        },
    }
    payload["handoff_sha256"] = payload_sha256(payload, "handoff_sha256")
    return payload


def validate_creative_tool_handoff(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SCHEMA:
        raise ContractError("unsupported creative handoff schema")
    if payload.get("handoff_sha256") != payload_sha256(payload, "handoff_sha256"):
        raise ContractError("creative handoff integrity check failed")
    source = payload.get("reviewed_source")
    reference = payload.get("source_shot_unit")
    if not isinstance(source, dict) or not isinstance(reference, dict):
        raise ContractError("creative handoff requires source bindings")
    if not isinstance(source.get("review_root"), str):
        raise ContractError("creative handoff requires a review root")
    path = validate_artifact_reference(reference, "shot unit")
    expected = build_creative_tool_handoff(payload.get("tool"), source["review_root"], path)
    if payload != expected:
        raise ContractError("creative handoff content or authority drifted")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build")
    build.add_argument("--tool", choices=sorted(REVIEWED_TOOLS), required=True)
    build.add_argument("--source-review-root", required=True)
    build.add_argument("--shot-unit", required=True)
    build.add_argument("--output", required=True)
    check = sub.add_parser("validate")
    check.add_argument("--handoff", required=True)
    revision = sub.add_parser("revision-plan", help="PhotoCraft layered-material contract only")
    revision.add_argument("--handoff", required=True)
    revision.add_argument("--brief", required=True)
    revision.add_argument("--output", required=True)
    revision_check = sub.add_parser("revision-validate", help="Validate plan or reported return; no editor execution")
    revision_check.add_argument("--input", required=True)
    verification = sub.add_parser("verification-plan", help="Offline video verification contract; never launches software")
    verification.add_argument("--handoff", required=True)
    verification.add_argument("--fps-numerator", type=int, required=True)
    verification.add_argument("--fps-denominator", type=int, required=True)
    verification.add_argument("--output", required=True)
    verification_check = sub.add_parser("verification-validate", help="Validate plan or FilmCraft reported return")
    verification_check.add_argument("--input", required=True)
    consume = sub.add_parser("consume-photocraft", help="Consume existing design material return; no editor integration")
    consume.add_argument("--input", required=True)
    consume.add_argument("--handoff", required=True)
    drawai = sub.add_parser("drawai-revision-check", help="Check local DrawAI revision snapshots against the manual shot route")
    drawai.add_argument("--shot-unit", required=True)
    drawai.add_argument("--brief", required=True)
    drawai.add_argument("--source-review-root", required=True)
    drawai.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "drawai-revision-check":
            from .drawai_revision import check_drawai_revision
            if Path(args.output).exists():
                raise ContractError("DrawAI check requires a new output file")
            payload = check_drawai_revision(args.shot_unit, args.brief, args.source_review_root)
            atomic_write_json(payload, args.output)
            print(json.dumps({"status": "valid_offline_revision_mapping", "runtime_verified": False,
                              "scene_id": payload["scene_id"], "output": str(Path(args.output).resolve())}))
            return 0
        if args.action in {"verification-plan", "verification-validate", "consume-photocraft"}:
            from .creative_tool_verification import (
                PLAN_SCHEMA as VERIFICATION_SCHEMA, build_verification_plan,
                validate_verification_plan, validate_filmcraft_return, consume_photocraft_return,
            )
            if args.action == "verification-plan":
                payload = build_verification_plan(args.handoff, args.fps_numerator, args.fps_denominator)
                validate_verification_plan(payload)
                if Path(args.output).exists():
                    raise ContractError("verification plan requires a new output file")
                atomic_write_json(payload, args.output)
                result = {"status": "valid_verification_plan_only", "runtime_verified": False,
                          "application_import_file": False, "plan_sha256": payload["plan_sha256"]}
            elif args.action == "consume-photocraft":
                result = consume_photocraft_return(args.input, args.handoff)
            else:
                payload = read_json_object(args.input, "verification contract")
                if payload.get("schema") == VERIFICATION_SCHEMA:
                    validate_verification_plan(payload)
                    result = {"status": "valid_verification_plan_only", "runtime_verified": False}
                else:
                    result = validate_filmcraft_return(payload)
            print(json.dumps(result))
            return 0
        if args.action in {"revision-plan", "revision-validate"}:
            from .photocraft_revision import (
                PLAN_SCHEMA, build_revision_plan, validate_revision_plan, validate_revision_return,
            )
            if args.action == "revision-plan":
                payload = build_revision_plan(args.handoff, args.brief)
                validate_revision_plan(payload)
                if Path(args.output).exists():
                    raise ContractError("revision plan requires a new output file")
                atomic_write_json(payload, args.output)
                result = {"status": "valid_revision_plan_only", "runtime_verified": False,
                          "plan_sha256": payload["plan_sha256"]}
            else:
                payload = read_json_object(args.input, "revision contract")
                if payload.get("schema") == PLAN_SCHEMA:
                    validate_revision_plan(payload)
                    result = {"status": "valid_revision_plan_only", "runtime_verified": False}
                else:
                    result = validate_revision_return(payload)
            print(json.dumps(result))
            return 0
        if args.action == "build":
            payload = build_creative_tool_handoff(args.tool, args.source_review_root, args.shot_unit)
            validate_creative_tool_handoff(payload)
            atomic_write_json(payload, args.output)
        else:
            payload = read_json_object(args.handoff, "creative handoff")
            validate_creative_tool_handoff(payload)
        print(json.dumps({"status": "valid_handoff_only", "tool": payload["tool"],
                          "runtime_verified": False, "handoff_sha256": payload["handoff_sha256"]}))
        return 0
    except (ContractError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2
