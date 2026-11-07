from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.color_processing import (
    analyze_color_source,
    build_color_processing_plan,
    execute_color_processing,
    write_color_processing_contract,
)
from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    file_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt


def run(output_dir: str | Path) -> dict[str, object]:
    root = Path(output_dir).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ContractError("synthetic color E2E output directory is not empty")
    root.mkdir(parents=True, exist_ok=True)
    source = _create_fixture(root)
    analysis_path = root / "analysis-report.json"
    analyze_color_source(
        source,
        root / "analysis-work",
        analysis_path,
        sample_fps=4,
    )
    receipts = []
    outputs = []
    for label in ("run-a", "run-b"):
        output = root / f"{label}.mp4"
        plan = build_color_processing_plan(
            source,
            output,
            analysis_path,
            source_role="synthetic_camera_plate",
            mode="cautious_auto",
        )
        plan_path = root / f"{label}-plan.json"
        write_color_processing_contract(plan, plan_path)
        receipt_path = root / f"{label}-receipt.json"
        receipt = execute_color_processing(
            plan_path,
            root / f"{label}-work",
            receipt_path,
        )
        receipts.append((receipt_path, receipt))
        outputs.append(output)
    byte_identical = file_sha256(outputs[0]) == file_sha256(outputs[1])
    audio_matches = all(
        bool(receipt["technical_qa"]["audio_packet_sha256_matches"])
        for _path, receipt in receipts
    )
    if not byte_identical or not audio_matches:
        raise ContractError("synthetic color E2E determinism or audio QA failed")
    attestation = {
        "schema": "video_creation_pipeline.color_processing_completion_receipt.v1",
        "status": "complete",
        "completed_at": datetime.now(UTC).isoformat(),
        "verification": {
            "synthetic_e2e": {
                "scope": "synthetic_color_fixture_only",
                "execution_receipt_payload_sha256": receipts[0][1]["receipt_sha256"],
                "double_run_byte_identical": True,
                "audio_packet_sha256_matches": True,
                "visual_review_status": "needs_visual_review",
                "publish_authorized": False,
            }
        },
        "boundary_check": {
            "human_visual_review_required": True,
            "real_person_or_customer_media_used": False,
            "online_provider_called": False,
            "media_uploaded": False,
            "source_media_modified": False,
            "automatic_white_balance_used": False,
            "creative_grade_quality_claimed": False,
            "skin_tone_quality_claimed": False,
            "release_eligible": False,
            "video_published": False,
            "automatic_publish_enabled": False,
            "second_ffmpeg_orchestration_added": False,
            "second_video_encoder_added": False,
            "second_state_machine_added": False,
        },
    }
    attestation_path = root / "completion-attestation.json"
    atomic_write_json(attestation, attestation_path)
    summary = {
        "schema": "video_creation_pipeline.color_processing_synthetic_e2e.v1",
        "status": "passed_candidate_only",
        "source": artifact_reference(source, "synthetic color source"),
        "analysis_report": artifact_reference(
            analysis_path, "synthetic color analysis"
        ),
        "run_receipts": [
            artifact_reference(path, "synthetic color run receipt")
            for path, _receipt in receipts
        ],
        "candidate_outputs": [
            artifact_reference(path, "synthetic color candidate") for path in outputs
        ],
        "completion_attestation": artifact_reference(
            attestation_path, "synthetic color completion attestation"
        ),
        "double_run_byte_identical": byte_identical,
        "audio_packet_sha256_matches": audio_matches,
        "candidate_only": True,
        "publish_authorized": False,
    }
    atomic_write_json(summary, root / "e2e-summary.json")
    return summary


def _create_fixture(root: Path) -> Path:
    spec = root / "synthetic-color-fixture.json"
    atomic_write_json(
        {
            "schema": "video_creation_pipeline.synthetic_color_fixture.v1",
            "duration_s": 2,
            "size": "640x360",
            "frame_rate": 24,
            "visual_transform": "brightness=-0.22:saturation=0.35",
            "audio": "440hz-aac",
        },
        spec,
    )
    output = root / "synthetic-dim-source.mp4"
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=24:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=2",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        "eq=brightness=-0.22:saturation=0.35,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-threads",
        "1",
        str(output),
    ]
    execute_ffmpeg_with_receipt(
        "render",
        command,
        [{"role": "synthetic_color_fixture_spec", "path": spec}],
        [{"role": "synthetic_dim_video", "path": output}],
        root / "synthetic-color-fixture-ffmpeg-receipt.json",
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic synthetic basic-color E2E."
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
