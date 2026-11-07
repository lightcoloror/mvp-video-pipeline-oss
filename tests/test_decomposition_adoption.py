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

from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.creative_cli import main as creative_cli_main
from video_creation_pipeline.decomposition_adoption import (
    DECOMPOSITION_ADOPTION_SCHEMA,
    VKP_DECOMPOSITION_REPORT_SCHEMA,
    build_decomposition_adoption,
    build_reference_recipe_from_decomposition,
    validate_decomposition_adoption,
    validate_vkp_decomposition_report,
    write_decomposition_adoption,
    write_decomposition_reference_recipe,
)
from video_creation_pipeline.decomposition_cli import main as decomposition_cli_main
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.generation_contracts import (
    build_shot_generation_unit,
    write_generation_contract,
)
from video_creation_pipeline.production_strategy import (
    build_visual_production_plan,
    validate_visual_production_plan,
    write_production_strategy_contract,
)
from video_creation_pipeline.reference_remix import normalize_reference_recipe
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

VKP_FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class DecompositionAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.timeline_path = self.root / "timeline-evidence.json"
        self.audio_path = self.root / "audio-evidence.json"
        atomic_write_json({"timeline": [{"index": 1}]}, self.timeline_path)
        atomic_write_json({"audio": {"available": True}}, self.audio_path)
        self.report_path = self.root / "decomposition-report.json"
        atomic_write_json(self.report(), self.report_path)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_builds_adoption_and_confirmed_only_reference_recipe(self) -> None:
        adoption = build_decomposition_adoption(self.report_path)
        self.assertEqual(adoption["schema"], DECOMPOSITION_ADOPTION_SCHEMA)
        self.assertEqual(adoption["status_counts"]["confirmed"], 3)
        self.assertEqual(adoption["status_counts"]["inferred"], 1)
        self.assertEqual(adoption["status_counts"]["unavailable"], 1)
        risk = adoption["creative_strategy"]["imitation_risks"][0]
        self.assertEqual(risk["planning_status"], "candidate_only")
        self.assertFalse(risk["automatic_instruction_allowed"])
        framework = adoption["creative_strategy"]["reusable_frameworks"][0]
        self.assertTrue(framework["automatic_instruction_allowed"])

        recipe = build_reference_recipe_from_decomposition(self.report_path)
        self.assertEqual(
            recipe["schema"], "video_creation_pipeline.reference_recipe.v1"
        )
        self.assertEqual(len(recipe["shots"]), 1)
        self.assertEqual(recipe["shots"][0]["script"], "展示明确结果并提出问题")
        self.assertEqual(recipe["reference"]["excluded_segment_ids"], ["body-inferred"])
        normalize_reference_recipe(recipe)

    def test_sensitive_identity_cannot_be_inferred(self) -> None:
        report = self.report()
        bgm = next(row for row in report["findings"] if row["finding_id"] == "bgm-gap")
        bgm.update(
            {
                "status": "inferred",
                "evidence_ids": ["audio-1", "timeline-1"],
                "missing_evidence": [],
                "direct_evidence_kind": "reliable_music_recognition",
            }
        )
        report["report_sha256"] = payload_sha256(report, "report_sha256")

        with self.assertRaisesRegex(ContractError, "cannot be inferred"):
            validate_vkp_decomposition_report(report)

    def test_non_confirmed_segment_cannot_emit_visual_instructions(self) -> None:
        report = self.report()
        segment = report["structure_segments"][1]
        segment["confirmed_requirements"]["scene"] = "猜测的场景"
        report["report_sha256"] = payload_sha256(report, "report_sha256")

        with self.assertRaisesRegex(ContractError, "cannot contain instructions"):
            validate_vkp_decomposition_report(report)

    def test_source_artifact_change_invalidates_report_adoption_and_recipe(
        self,
    ) -> None:
        adoption = build_decomposition_adoption(self.report_path)
        adoption_path = self.root / "adoption.json"
        write_decomposition_adoption(adoption, adoption_path)
        recipe = build_reference_recipe_from_decomposition(self.report_path)
        recipe_path = self.root / "reference-recipe.json"
        write_decomposition_reference_recipe(recipe, recipe_path)

        self.timeline_path.write_text('{"changed":true}', encoding="utf-8")
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_vkp_decomposition_report(report)
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_decomposition_adoption(adoption)
        with self.assertRaisesRegex(ContractError, "changed after"):
            normalize_reference_recipe(recipe)

    def test_visual_plan_binds_adoption_and_detects_stale_report_evidence(self) -> None:
        adoption = build_decomposition_adoption(self.report_path)
        adoption_path = self.root / "adoption.json"
        write_decomposition_adoption(adoption, adoption_path)
        unit_path = self.build_unit()
        plan = build_visual_production_plan(
            unit_path,
            decomposition_adoption_path=adoption_path,
        )
        plan_path = self.root / "visual-plan.json"
        write_production_strategy_contract(plan, plan_path)
        self.assertEqual(
            Path(plan["decomposition_adoption"]["path"]),
            adoption_path.resolve(),
        )

        self.timeline_path.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_visual_production_plan(plan)

    def test_cli_builds_adoption_recipe_and_visual_strategy(self) -> None:
        adoption_path = self.root / "adoption-cli.json"
        recipe_path = self.root / "recipe-cli.json"
        self.assertEqual(
            decomposition_cli_main(
                [
                    "build-adoption",
                    "--report",
                    str(self.report_path),
                    "--output",
                    str(adoption_path),
                ]
            ),
            0,
        )
        self.assertEqual(
            decomposition_cli_main(
                [
                    "build-reference-recipe",
                    "--report",
                    str(self.report_path),
                    "--output",
                    str(recipe_path),
                ]
            ),
            0,
        )
        visual_path = self.root / "visual-cli.json"
        self.assertEqual(
            creative_cli_main(
                [
                    "build-visual-strategy",
                    "--unit",
                    str(self.build_unit()),
                    "--decomposition-adoption",
                    str(adoption_path),
                    "--output",
                    str(visual_path),
                ]
            ),
            0,
        )
        visual = json.loads(visual_path.read_text(encoding="utf-8"))
        validate_visual_production_plan(visual)

    def build_unit(self) -> Path:
        bundle = self.root / "bundle"
        if not bundle.exists():
            shutil.copytree(VKP_FIXTURE, bundle)
        edit_job_path = self.root / "edit-job.json"
        if not edit_job_path.exists():
            write_edit_job(build_edit_job(bundle), edit_job_path)
        unit_path = self.root / "shot-unit.json"
        if not unit_path.exists():
            unit = build_shot_generation_unit(
                edit_job_path,
                "storyboard-0001",
                generator="remotion",
                visual_prompt="Render only evidence-supported visual structure.",
            )
            write_generation_contract(unit, unit_path)
        return unit_path

    def report(self) -> dict:
        report: dict = {
            "schema": VKP_DECOMPOSITION_REPORT_SCHEMA,
            "report_id": "decomposition-fixture-001",
            "title": "参考视频拆解测试",
            "source_artifacts": [
                {
                    "artifact_id": "timeline-1",
                    "role": "timeline",
                    **artifact_reference(self.timeline_path, "timeline evidence"),
                },
                {
                    "artifact_id": "audio-1",
                    "role": "audio_receipt",
                    **artifact_reference(self.audio_path, "audio evidence"),
                },
            ],
            "modality_coverage": {
                "video": {
                    "status": "available",
                    "evidence_ids": ["timeline-1"],
                    "missing_evidence": [],
                },
                "audio": {
                    "status": "available",
                    "evidence_ids": ["audio-1"],
                    "missing_evidence": [],
                },
                "transcript": {
                    "status": "available",
                    "evidence_ids": ["timeline-1"],
                    "missing_evidence": [],
                },
                "platform_metadata": {
                    "status": "unavailable",
                    "evidence_ids": [],
                    "missing_evidence": ["platform metadata was not supplied"],
                },
                "user_material": {
                    "status": "unavailable",
                    "evidence_ids": [],
                    "missing_evidence": ["no user-authored evidence supplied"],
                },
                "link_acquisition": {
                    "status": "unavailable",
                    "evidence_ids": [],
                    "missing_evidence": ["analysis used local media"],
                },
            },
            "findings": [
                self.finding(
                    "hook-confirmed",
                    "hook",
                    "confirmed",
                    "开头展示结果并提出问题",
                    ["video", "transcript"],
                    ["timeline-1"],
                    0,
                    3,
                ),
                self.finding(
                    "visual-confirmed",
                    "visual_language",
                    "confirmed",
                    "开头使用产品近景作为直接证据",
                    ["video"],
                    ["timeline-1"],
                    0,
                    3,
                ),
                self.finding(
                    "framework-confirmed",
                    "reusable_framework",
                    "confirmed",
                    "结果展示后解释步骤",
                    ["transcript"],
                    ["timeline-1"],
                    None,
                    None,
                ),
                self.finding(
                    "risk-inferred",
                    "imitation_risk",
                    "inferred",
                    "效果可能依赖原作者素材条件",
                    ["video", "transcript"],
                    ["timeline-1", "audio-1"],
                    3,
                    8,
                ),
                {
                    "finding_id": "bgm-gap",
                    "dimension": "bgm_identity",
                    "status": "unavailable",
                    "claim": "当前证据无法确认 BGM 曲名",
                    "time_range": {
                        "scope": "global",
                        "start_s": None,
                        "end_s": None,
                    },
                    "source_modalities": ["audio"],
                    "evidence_ids": [],
                    "evidence_paths": [],
                    "missing_evidence": ["reliable music recognition result"],
                    "direct_evidence_kind": "",
                },
            ],
            "structure_segments": [
                {
                    "segment_id": "hook-confirmed",
                    "label": "开头",
                    "start_s": 0,
                    "end_s": 3,
                    "summary": "展示明确结果并提出问题",
                    "status": "confirmed",
                    "finding_ids": ["hook-confirmed", "visual-confirmed"],
                    "confirmed_requirements": {
                        "scene": "产品近景",
                        "action": "展示结果",
                        "shot_size": "close",
                        "avoid": ["复制原视频品牌水印"],
                    },
                },
                {
                    "segment_id": "body-inferred",
                    "label": "正文",
                    "start_s": 3,
                    "end_s": 8,
                    "summary": "可能依赖作者素材条件",
                    "status": "inferred",
                    "finding_ids": ["risk-inferred"],
                    "confirmed_requirements": {
                        "scene": "",
                        "action": "",
                        "shot_size": "",
                        "avoid": [],
                    },
                },
            ],
            "creative_strategy": {
                "reusable_frameworks": ["framework-confirmed"],
                "techniques": ["visual-confirmed"],
                "non_copyable_factors": ["risk-inferred"],
                "imitation_risks": ["risk-inferred"],
                "differentiation": ["framework-confirmed"],
            },
        }
        report["report_sha256"] = payload_sha256(report, "report_sha256")
        return report

    @staticmethod
    def finding(
        finding_id: str,
        dimension: str,
        status: str,
        claim: str,
        source_modalities: list[str],
        evidence_ids: list[str],
        start_s: int | None,
        end_s: int | None,
    ) -> dict:
        return {
            "finding_id": finding_id,
            "dimension": dimension,
            "status": status,
            "claim": claim,
            "time_range": {
                "scope": "global" if start_s is None else "local",
                "start_s": start_s,
                "end_s": end_s,
            },
            "source_modalities": source_modalities,
            "evidence_ids": evidence_ids,
            "evidence_paths": [],
            "missing_evidence": [],
            "direct_evidence_kind": "",
        }


if __name__ == "__main__":
    unittest.main()
