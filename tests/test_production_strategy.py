from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import atomic_write_json, payload_sha256
from video_creation_pipeline.creative_cli import main as creative_cli_main
from video_creation_pipeline.creative_gates import (
    build_approval_record,
    write_creative_contract,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    build_ffmpeg_execution_receipt,
    validate_ffmpeg_execution_receipt,
)
from video_creation_pipeline.generation_contracts import (
    CAPABILITY_PREFLIGHT_SCHEMA,
    build_generation_task,
    build_shot_generation_unit,
    validate_generation_task,
    write_generation_contract,
)
from video_creation_pipeline.production_strategy import (
    AUDIO_MASTERING_PLAN_SCHEMA,
    FULL_VIDEO_PRODUCTION_COMMIT,
    VISUAL_PRODUCTION_PLAN_SCHEMA,
    build_audio_mastering_plan,
    build_visual_production_plan,
    validate_audio_mastering_plan,
    validate_visual_production_plan,
    write_production_strategy_contract,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class ProductionStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, bundle)
        self.edit_job_path = self.root / "edit-job.json"
        write_edit_job(build_edit_job(bundle), self.edit_job_path)
        self.unit_path = self.root / "shot-unit.json"
        unit = build_shot_generation_unit(
            self.edit_job_path,
            "storyboard-0001",
            generator="remotion",
            visual_prompt="Show the argument as an inspectable layered composition.",
        )
        write_generation_contract(unit, self.unit_path)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_visual_strategy_binds_the_generation_task(self) -> None:
        strategy = build_visual_production_plan(
            self.unit_path,
            argument_role="tension",
            effort_mode="apex",
            artifact_layer=True,
            emphasis_phrases=["关键矛盾", "证据"],
        )
        self.assertEqual(strategy["schema"], VISUAL_PRODUCTION_PLAN_SCHEMA)
        self.assertEqual(
            strategy["provenance"]["source_commit"], FULL_VIDEO_PRODUCTION_COMMIT
        )
        self.assertEqual(
            [layer["id"] for layer in strategy["layers"]],
            ["canvas", "artifact", "cinema"],
        )
        self.assertEqual(strategy["effort_protocol"]["divergent_concepts_required"], 3)
        self.assertFalse(
            strategy["execution_boundary"]["source_code_copied_from_upstream"]
        )
        self.assertEqual(
            strategy["reading_priority_contract"]["max_simultaneous_primary_targets"],
            1,
        )
        self.assertFalse(
            strategy["reading_priority_contract"][
                "information_overlay_full_sentence_restatement_allowed"
            ]
        )
        self.assertEqual(
            strategy["vertical_layout_contract"]["zones"]["subtitle"]["y"],
            0.6458,
        )
        self.assertEqual(
            strategy["composition_layout_policy"]["required_peer_layout_candidates"],
            ["vertical_list", "horizontal_row", "grid"],
        )
        self.assertEqual(
            strategy["composition_layout_policy"][
                "preferred_person_window_corner_candidates"
            ],
            ["top_left", "top_right"],
        )
        self.assertEqual(
            strategy["composition_layout_policy"]["lower_corner_use"],
            "exception_only_when_both_top_corners_unavailable",
        )
        self.assertFalse(
            strategy["composition_layout_policy"][
                "automatic_collision_check_is_sufficient"
            ]
        )
        strategy_path = self.root / "visual-strategy.json"
        write_production_strategy_contract(strategy, strategy_path)

        approval_path = self.root / "storyboard-approval.json"
        approval = build_approval_record(
            "storyboard",
            self.unit_path,
            approved_by="operator",
            approved_at="2026-07-28T12:00:00+08:00",
            comment="fixture approval",
        )
        write_creative_contract(approval, approval_path)
        preflight_path = self.root / "preflight.json"
        atomic_write_json(
            {
                "schema": CAPABILITY_PREFLIGHT_SCHEMA,
                "checked_at": "2026-07-28T12:01:00+08:00",
                "capabilities": [
                    {
                        "id": "remotion_render",
                        "ready": True,
                        "installation_performed": False,
                        "probe_method": "existing_runtime",
                    }
                ],
            },
            preflight_path,
        )
        task = build_generation_task(
            self.unit_path,
            approval_path,
            preflight_path,
            output_dir=self.root / "generated",
            visual_strategy_path=strategy_path,
        )
        validate_generation_task(task)
        self.assertEqual(
            Path(task["visual_strategy"]["path"]),
            strategy_path.resolve(),
        )
        self.assertEqual(task["visual_strategy"]["content_kind"], "json")

        strategy_path.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_generation_task(task)

    def test_visual_strategy_layout_and_reading_contracts_fail_closed(self) -> None:
        strategy = build_visual_production_plan(self.unit_path)
        strategy["reading_priority_contract"]["max_simultaneous_primary_targets"] = 2
        strategy["plan_sha256"] = payload_sha256(strategy, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "reading priority contract"):
            validate_visual_production_plan(strategy)

        strategy = build_visual_production_plan(self.unit_path)
        strategy["vertical_layout_contract"]["zones"]["subtitle"]["y"] = 0.6
        strategy["plan_sha256"] = payload_sha256(strategy, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "vertical layout contract"):
            validate_visual_production_plan(strategy)

        strategy = build_visual_production_plan(self.unit_path)
        strategy["composition_layout_policy"][
            "person_window_center_default_allowed"
        ] = True
        strategy["plan_sha256"] = payload_sha256(strategy, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "composition layout policy"):
            validate_visual_production_plan(strategy)

    def test_visual_strategy_cli_writes_a_nonexecuting_contract(self) -> None:
        output = self.root / "visual-strategy-cli.json"
        result = creative_cli_main(
            [
                "build-visual-strategy",
                "--unit",
                str(self.unit_path),
                "--argument-role",
                "insight",
                "--effort-mode",
                "efficient",
                "--emphasis-phrase",
                "解决方案",
                "--output",
                str(output),
            ]
        )
        self.assertEqual(result, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        validate_visual_production_plan(payload)
        self.assertFalse(payload["execution_boundary"]["render_execution_performed"])
        self.assertFalse(payload["execution_boundary"]["network_execution_performed"])

    def test_audio_mastering_cli_writes_a_plan_without_executing_ffmpeg(self) -> None:
        source = self.root / "voice.wav"
        source.write_bytes(b"fixture-wave")
        plan_path = self.root / "mastering-plan.json"
        result = creative_cli_main(
            [
                "build-audio-mastering-plan", "--delivery-profile", "web_spoken_candidate",
                "--input-audio",
                str(source),
                "--output-audio",
                str(self.root / "mastered.wav"),
                "--output",
                str(plan_path),
            ]
        )
        self.assertEqual(result, 0)
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        validate_audio_mastering_plan(payload)
        self.assertFalse(payload["ffmpeg_handoff"]["executed"])
        self.assertFalse(payload["execution_boundary"]["network_execution_performed"])

    def test_audio_mastering_plan_uses_the_single_ffmpeg_outlet(self) -> None:
        source = self.root / "voice.wav"
        source.write_bytes(b"fixture-wave")
        plan = build_audio_mastering_plan(source, self.root / "mastered.wav", target_lufs=-14.0)
        self.assertEqual(plan["schema"], AUDIO_MASTERING_PLAN_SCHEMA)
        self.assertEqual(plan["output"]["codec"], "pcm_s24le")
        self.assertEqual([row["id"] for row in plan["passes"]], ["measure", "apply"])
        self.assertEqual(plan["ffmpeg_handoff"]["operation"], "audio_mastering")
        self.assertFalse(plan["ffmpeg_handoff"]["command_generated"])
        self.assertFalse(plan["ffmpeg_handoff"]["second_ffmpeg_orchestrator_created"])
        validate_audio_mastering_plan(plan)

        source.write_bytes(b"changed-wave")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_audio_mastering_plan(plan)

    def test_audio_mastering_operation_is_accepted_by_existing_receipts(self) -> None:
        source = self.root / "source.wav"
        output = self.root / "mastered.wav"
        source.write_bytes(b"source")
        output.write_bytes(b"mastered")
        receipt = build_ffmpeg_execution_receipt(
            "audio_mastering",
            ["ffmpeg", "-i", str(source), str(output)],
            [{"role": "source_audio", "path": source}],
            [{"role": "mastered_audio", "path": output}],
        )
        validate_ffmpeg_execution_receipt(receipt)
        self.assertEqual(receipt["operation"], "audio_mastering")


if __name__ == "__main__":
    unittest.main()
