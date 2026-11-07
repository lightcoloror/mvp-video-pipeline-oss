from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from video_creation_pipeline.color_processing import (
    analyze_color_source,
    build_color_processing_plan,
    color_processing_doctor,
    execute_color_processing,
    validate_color_analysis_report,
    validate_color_processing_plan,
    validate_color_processing_receipt,
    write_color_processing_contract,
)
from video_creation_pipeline.contract_io import (
    atomic_write_json,
    file_sha256,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt

ROOT = Path(__file__).resolve().parents[1]


class ColorProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self._synthetic_dim_video()
        self.analysis_path = self.root / "analysis-report.json"
        self.analysis = analyze_color_source(
            self.source,
            self.root / "analysis-work",
            self.analysis_path,
            sample_fps=4,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cautious_auto_double_run_is_deterministic_and_preserves_audio(
        self,
    ) -> None:
        first = self._run_candidate("first")
        second = self._run_candidate("second")

        self.assertEqual(
            file_sha256(self.root / "first.mp4"),
            file_sha256(self.root / "second.mp4"),
        )
        self.assertTrue(first["technical_qa"]["technically_valid"])
        self.assertTrue(first["technical_qa"]["audio_packet_sha256_matches"])
        self.assertEqual(
            first["audio_packet_qa"]["source"]["packet_sha256"],
            first["audio_packet_qa"]["processed"]["packet_sha256"],
        )
        self.assertEqual(first["review"]["status"], "needs_visual_review")
        self.assertFalse(first["authority_boundary"]["automatic_white_balance"])
        self.assertFalse(first["authority_boundary"]["automatic_publish"])
        validate_color_processing_receipt(first)
        validate_color_processing_receipt(second)

    def test_explicit_temperature_is_bounded_and_visible_in_filter_graph(self) -> None:
        plan = build_color_processing_plan(
            self.source,
            self.root / "explicit.mp4",
            self.analysis_path,
            source_role="talking_head_plate",
            mode="explicit_basic",
            brightness=0.02,
            contrast=1.04,
            gamma=1.01,
            saturation=1.08,
            temperature_kelvin=5600,
            temperature_mix=0.2,
            preserve_lightness=0.9,
        )

        self.assertIn("colortemperature=temperature=5600", plan["filter_graph"])
        self.assertEqual(plan["adjustments"]["policy"], "explicit_basic_v1")
        self.assertTrue(plan["adjustments"]["temperature"]["enabled"])
        validate_color_processing_plan(plan)

        plan_path = self.root / "explicit-plan.json"
        write_color_processing_contract(plan, plan_path)
        receipt = execute_color_processing(
            plan_path,
            self.root / "explicit-work",
            self.root / "explicit-receipt.json",
        )
        self.assertTrue(receipt["technical_qa"]["technically_valid"])
        self.assertTrue(receipt["technical_qa"]["audio_packet_sha256_matches"])

    def test_explicit_adjustment_outside_project_safety_bounds_fails(self) -> None:
        with self.assertRaisesRegex(ContractError, "brightness must be between"):
            build_color_processing_plan(
                self.source,
                self.root / "unsafe.mp4",
                self.analysis_path,
                source_role="talking_head_plate",
                mode="explicit_basic",
                brightness=0.3,
            )

    def test_flattened_graphics_composite_is_not_an_allowed_source_role(self) -> None:
        with self.assertRaisesRegex(ContractError, "unflattened camera"):
            build_color_processing_plan(
                self.source,
                self.root / "flattened.mp4",
                self.analysis_path,
                source_role="flattened_final_composite",
                mode="cautious_auto",
            )

    def test_analysis_metric_tampering_fails_even_after_rehash(self) -> None:
        tampered = dict(self.analysis)
        tampered["metrics"] = dict(self.analysis["metrics"])
        tampered["metrics"]["sampled_frame_count"] += 1
        tampered["report_sha256"] = payload_sha256(tampered, "report_sha256")

        with self.assertRaisesRegex(ContractError, "metrics drifted"):
            validate_color_analysis_report(tampered)

    def test_runtime_provenance_tampering_fails_even_after_rehash(self) -> None:
        tampered = dict(self.analysis)
        tampered["engine"] = dict(self.analysis["engine"])
        tampered["engine"]["runtime_sha256"] = "0" * 64
        tampered["report_sha256"] = payload_sha256(tampered, "report_sha256")

        with self.assertRaisesRegex(ContractError, "runtime provenance drifted"):
            validate_color_analysis_report(tampered)

    def test_source_change_after_plan_fails_closed(self) -> None:
        plan = build_color_processing_plan(
            self.source,
            self.root / "stale.mp4",
            self.analysis_path,
            source_role="camera_plate",
            mode="cautious_auto",
        )
        self.source.write_bytes(self.source.read_bytes() + b"drift")

        with self.assertRaisesRegex(ContractError, "changed after the contract"):
            validate_color_processing_plan(plan)

    def test_existing_final_output_is_not_overwritten(self) -> None:
        output = self.root / "occupied.mp4"
        plan = build_color_processing_plan(
            self.source,
            output,
            self.analysis_path,
            source_role="interview_plate",
            mode="cautious_auto",
        )
        plan_path = self.root / "occupied-plan.json"
        write_color_processing_contract(plan, plan_path)
        output.write_bytes(b"existing-user-output")

        with self.assertRaisesRegex(ContractError, "output already exists"):
            execute_color_processing(
                plan_path,
                self.root / "occupied-work",
                self.root / "occupied-receipt.json",
            )
        self.assertEqual(output.read_bytes(), b"existing-user-output")

    def test_doctor_reports_reviewed_filters_and_candidate_boundary(self) -> None:
        doctor = color_processing_doctor()

        self.assertEqual(doctor["status"], "ready")
        self.assertTrue(all(doctor["checks"].values()))
        self.assertFalse(doctor["authority_boundary"]["creative_grade_claimed"])
        self.assertTrue(doctor["authority_boundary"]["human_visual_review_required"])

    def test_installed_entry_point_targets_color_processing_cli(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            scripts = tomllib.load(handle)["project"]["scripts"]
        self.assertEqual(
            scripts["video-color-processing"],
            "video_creation_pipeline.color_processing_cli:main",
        )

    def _run_candidate(self, label: str) -> dict[str, object]:
        plan = build_color_processing_plan(
            self.source,
            self.root / f"{label}.mp4",
            self.analysis_path,
            source_role="synthetic_camera_plate",
            mode="cautious_auto",
        )
        plan_path = self.root / f"{label}-plan.json"
        write_color_processing_contract(plan, plan_path)
        receipt = execute_color_processing(
            plan_path,
            self.root / f"{label}-work",
            self.root / f"{label}-receipt.json",
        )
        self.assertEqual(
            receipt["processed_video"]["sha256"],
            receipt["staged_video"]["sha256"],
        )
        return receipt

    def _synthetic_dim_video(self) -> Path:
        spec = self.root / "synthetic-color-fixture.json"
        atomic_write_json(
            {
                "schema": "video_creation_pipeline.synthetic_color_fixture.v1",
                "duration_s": 1.25,
                "size": "320x180",
                "frame_rate": 24,
                "visual_transform": "brightness=-0.22:saturation=0.35",
                "audio": "440hz-aac",
            },
            spec,
        )
        output = self.root / "dim-source.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=1.25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.25",
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
            self.root / "synthetic-color-fixture-receipt.json",
        )
        return output


if __name__ == "__main__":
    unittest.main()
