from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.audio_mastering import execute_audio_mastering
from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
    read_json_object,
)
from video_creation_pipeline.creative_gates import (
    bind_audio_mastering_render_manifest,
    build_approval_record,
    build_release_gate,
    validate_release_gate,
    write_audio_mastering_bound_render_manifest,
    write_creative_contract,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
from video_creation_pipeline.production_strategy import (
    build_audio_mastering_plan,
    write_production_strategy_contract,
)


def run(output_dir: str | Path) -> dict[str, object]:
    root = Path(output_dir).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ContractError("audio mastering E2E output directory is not empty")
    root.mkdir(parents=True, exist_ok=True)
    source = _create_fixture(root)
    outputs: list[Path] = []
    receipts: list[Path] = []
    verifications: list[dict[str, object]] = []
    for label in ("run-a", "run-b"):
        output = root / f"{label}-mastered.wav"
        plan_path = root / f"{label}-plan.json"
        receipt_path = root / f"{label}-receipt.json"
        write_production_strategy_contract(
            build_audio_mastering_plan(
                source,
                output,
                target_lufs=-16.0,
                target_true_peak_db=-1.0,
                target_lra_lu=5.0,
            ),
            plan_path,
        )
        receipt = execute_audio_mastering(
            plan_path,
            root / f"{label}-work",
            receipt_path,
        )
        verification_path = Path(receipt["output_verification"]["path"])
        verification = read_json_object(
            verification_path, "audio mastering output verification"
        )
        if verification["readiness"]["technical_ready"] is not True:
            raise ContractError("synthetic audio mastering output QA did not pass")
        outputs.append(output)
        receipts.append(receipt_path)
        verifications.append(verification)
    byte_identical = file_sha256(outputs[0]) == file_sha256(outputs[1])
    measurement_identical = (
        verifications[0]["measurements"] == verifications[1]["measurements"]
    )
    if not byte_identical or not measurement_identical:
        raise ContractError("synthetic audio mastering E2E is not deterministic")
    (
        release_gate_path,
        release_gate,
        render_manifest_binding_idempotent,
    ) = _build_release_gate_fixture(
        root,
        mastered_audio_path=outputs[0],
        mastering_receipt_path=receipts[0],
    )
    summary = {
        "schema": "video_creation_pipeline.audio_mastering_output_qa_synthetic_e2e.v1",
        "status": "passed_candidate_only",
        "source": artifact_reference(source, "synthetic audio source"),
        "receipts": [
            artifact_reference(path, "audio mastering execution receipt")
            for path in receipts
        ],
        "outputs": [
            artifact_reference(path, "mastered audio candidate") for path in outputs
        ],
        "output_measurements": dict(verifications[0]["measurements"]),
        "output_checks": list(verifications[0]["checks"]),
        "double_run_byte_identical": byte_identical,
        "double_run_measurements_identical": measurement_identical,
        "release_gate": artifact_reference(
            release_gate_path, "synthetic audio mastering release gate"
        ),
        "release_gate_audio_mastering_status": release_gate["audio_mastering_status"],
        "release_gate_release_ready": release_gate["release_ready"],
        "release_gate_next_action": release_gate["next_action"],
        "render_manifest_binding_idempotent": render_manifest_binding_idempotent,
        "synthetic_approval_fixture_only": True,
        "real_or_customer_audio_used": False,
        "provider_called": False,
        "media_uploaded": False,
        "publish_authorized": False,
        "candidate_only": True,
    }
    atomic_write_json(summary, root / "e2e-summary.json")
    return summary


def _build_release_gate_fixture(
    root: Path,
    *,
    mastered_audio_path: Path,
    mastering_receipt_path: Path,
) -> tuple[Path, dict[str, object], bool]:
    fixture_dir = root / "release-gate-fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    script_path = atomic_write_json(
        {"text": "synthetic audio mastering release binding fixture"},
        fixture_dir / "script.json",
    )
    voice_path = atomic_write_json(
        {"segments": [{"id": "synthetic-voice-001"}]},
        fixture_dir / "voice.json",
    )
    measurements_path = atomic_write_json(
        {"duration_s": 2.0, "synthetic_fixture": True},
        fixture_dir / "voice-measurements.json",
    )
    visual_path = atomic_write_json(
        {"shots": [{"id": "synthetic-shot-001"}]},
        fixture_dir / "visual-plan.json",
    )
    director_review = fixture_dir / "director-review.md"
    director_review.write_text(
        "Synthetic contract fixture only; not a production approval.\n",
        encoding="utf-8",
    )
    approval_specs = [
        build_approval_record(
            "script",
            script_path,
            approved_by="synthetic-fixture-operator",
            approved_at="2026-08-21T06:45:00+08:00",
            comment="non-production synthetic contract fixture",
        ),
        build_approval_record(
            "voice",
            voice_path,
            approved_by="synthetic-fixture-operator",
            approved_at="2026-08-21T06:45:01+08:00",
            comment="non-production synthetic contract fixture",
            attachments=[
                {"role": "voice_measurements", "path": measurements_path},
                {"role": "audio", "path": mastered_audio_path},
            ],
        ),
        build_approval_record(
            "visuals",
            visual_path,
            approved_by="synthetic-fixture-operator",
            approved_at="2026-08-21T06:45:02+08:00",
            comment="non-production synthetic contract fixture",
            attachments=[{"role": "director_review_sheet", "path": director_review}],
        ),
    ]
    approval_paths: list[Path] = []
    voice_approval_path: Path | None = None
    for approval in approval_specs:
        approval_path = fixture_dir / f"approval-{approval['target']}.json"
        write_creative_contract(approval, approval_path)
        approval_paths.append(approval_path)
        if approval["target"] == "voice":
            voice_approval_path = approval_path
    if voice_approval_path is None:
        raise ContractError("synthetic release fixture lacks voice approval")

    mastering_receipt = read_json_object(
        mastering_receipt_path, "audio mastering execution receipt"
    )
    visual_plan = read_json_object(visual_path, "visual plan fixture")
    base_render_manifest_path = atomic_write_json(
        {
            "visual_plan_sha256": canonical_json_sha256(visual_plan),
            "final_voice_audio_sha256s": [
                mastering_receipt["mastered_audio"]["sha256"]
            ],
            "synthetic_fixture_only": True,
        },
        fixture_dir / "base-render-manifest.json",
    )
    bound_manifest = bind_audio_mastering_render_manifest(
        base_render_manifest_path,
        mastering_receipt_path,
        voice_approval_path,
    )
    render_manifest_path = write_audio_mastering_bound_render_manifest(
        bound_manifest,
        fixture_dir / "render-manifest.json",
        source_manifest_path=base_render_manifest_path,
    )
    rebound_manifest = bind_audio_mastering_render_manifest(
        render_manifest_path,
        mastering_receipt_path,
        voice_approval_path,
    )
    render_manifest_binding_idempotent = bound_manifest == rebound_manifest
    if not render_manifest_binding_idempotent:
        raise ContractError("audio mastering render manifest binding is not idempotent")
    write_audio_mastering_bound_render_manifest(
        rebound_manifest,
        fixture_dir / "render-manifest-rerun.json",
        source_manifest_path=render_manifest_path,
    )
    try:
        build_release_gate(
            approval_paths,
            render_manifest_path=render_manifest_path,
            visual_plan_path=visual_path,
        )
    except ContractError as exc:
        if "without a v2 execution receipt" not in str(exc):
            raise
    else:
        raise ContractError("audio mastering release claim did not fail closed")

    release_gate = build_release_gate(
        approval_paths,
        render_manifest_path=render_manifest_path,
        visual_plan_path=visual_path,
        audio_mastering_receipt_path=mastering_receipt_path,
    )
    validate_release_gate(release_gate)
    if (
        release_gate["audio_mastering_status"] != "output_verified"
        or release_gate["release_ready"] is not False
        or release_gate["next_action"] != "run_export_technical_qa"
    ):
        raise ContractError("synthetic audio mastering release binding is invalid")
    release_gate_path = fixture_dir / "release-gate.json"
    write_creative_contract(release_gate, release_gate_path)
    return release_gate_path, release_gate, render_manifest_binding_idempotent


def _create_fixture(root: Path) -> Path:
    spec = root / "synthetic-audio-fixture.json"
    atomic_write_json(
        {
            "schema": "video_creation_pipeline.synthetic_audio_fixture.v1",
            "frequency_hz": 440,
            "duration_s": 2.0,
            "sample_rate": 48000,
            "volume": 0.02,
        },
        spec,
    )
    source = root / "synthetic-low-volume-source.wav"
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=2",
        "-af",
        "volume=0.02",
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
        str(source),
    ]
    execute_ffmpeg_with_receipt(
        "render",
        command,
        [{"role": "synthetic_audio_fixture_spec", "path": spec}],
        [{"role": "synthetic_audio", "path": source}],
        root / "synthetic-audio-fixture-ffmpeg-receipt.json",
    )
    return source


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic synthetic audio mastering output QA."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = run(args.output_dir)
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
