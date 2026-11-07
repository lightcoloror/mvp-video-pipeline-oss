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

from video_creation_pipeline.contract_io import payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.rough_cut_workspace import (
    LEGACY_ROUGH_CUT_WORKSPACE_SCHEMA,
    MATERIAL_RETRY_PLAN_SCHEMA,
    ROUGH_CUT_FINALIZE_RECEIPT_SCHEMA,
    ROUGH_CUT_SELECTION_SCHEMA,
    build_material_retry_plan,
    build_rough_cut_finalize_receipt,
    build_rough_cut_workspace,
    validate_rough_cut_finalize_receipt,
    validate_rough_cut_workspace,
    write_rough_cut_contract,
)


class RoughCutWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.storyboard = self._json("storyboard.json", self._storyboard())
        self.round_one_candidates = self._json(
            "round-one-candidates.json", self._round_one_candidates()
        )
        self.workspace_path = self.root / "workspace.json"
        workspace = build_rough_cut_workspace(
            self.storyboard, self.round_one_candidates
        )
        write_rough_cut_contract(workspace, self.workspace_path)
        self.workspace = workspace

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_review_delivery_defaults_to_script_aligned_independent_clips(
        self,
    ) -> None:
        policy = self.workspace["review_delivery_policy"]

        self.assertEqual(
            policy["default_delivery_mode"],
            "script_aligned_independent_clips",
        )
        self.assertFalse(policy["cross_segment_batch_browse_allowed"])
        self.assertFalse(policy["continuous_review_reel_generated_by_default"])
        self.assertEqual(
            [row["script_segment_id"] for row in self.workspace["review_units"]],
            ["shot-001", "shot-002"],
        )
        self.assertTrue(
            all(
                row["review_mode"] == "single_mapped_clip"
                for row in self.workspace["review_units"]
            )
        )

    def test_review_delivery_rejects_cross_segment_continuous_reel(self) -> None:
        workspace = json.loads(json.dumps(self.workspace))
        workspace["review_delivery_policy"][
            "continuous_review_reel_generated_by_default"
        ] = True
        workspace["workspace_sha256"] = payload_sha256(workspace, "workspace_sha256")

        with self.assertRaisesRegex(ContractError, "script-aligned independent clips"):
            validate_rough_cut_workspace(workspace)

    def test_legacy_workspace_remains_readable_without_new_review_fields(self) -> None:
        workspace = json.loads(json.dumps(self.workspace))
        workspace["schema"] = LEGACY_ROUGH_CUT_WORKSPACE_SCHEMA
        workspace.pop("review_delivery_policy")
        workspace.pop("review_units")
        workspace["workspace_sha256"] = payload_sha256(workspace, "workspace_sha256")

        validate_rough_cut_workspace(workspace)

    def test_multiple_candidates_are_compared_only_within_the_script_segment(
        self,
    ) -> None:
        candidates = self._round_one_candidates()
        candidates["shots"][1]["candidates"].append(
            self._candidate("asset-b-alt", 0.79)
        )
        candidates_path = self._json("multiple-candidates.json", candidates)

        workspace = build_rough_cut_workspace(self.storyboard, candidates_path)

        self.assertEqual(
            workspace["review_units"][1]["review_mode"],
            "compare_candidates_within_segment",
        )
        self.assertEqual(
            workspace["review_units"][1]["candidate_ids"],
            ["asset-b", "asset-b-alt"],
        )
        self.assertFalse(
            workspace["review_delivery_policy"]["cross_segment_batch_browse_allowed"]
        )

    def test_second_round_only_contains_rejected_shots_and_locks_first_round(
        self,
    ) -> None:
        first_selection = self._selection(
            "round-one-selection.json",
            1,
            [
                {
                    "shot_id": "shot-001",
                    "status": "accepted",
                    "candidate_id": "asset-a",
                    "reason": "matched",
                },
                {
                    "shot_id": "shot-002",
                    "status": "rejected",
                    "candidate_id": "asset-b",
                    "reason": "action mismatch",
                },
            ],
        )

        retry = build_material_retry_plan(self.workspace_path, first_selection)

        self.assertEqual(retry["schema"], MATERIAL_RETRY_PLAN_SCHEMA)
        self.assertEqual([row["shot_id"] for row in retry["retry_shots"]], ["shot-002"])
        self.assertEqual(
            retry["locked_round_one_decisions"][0]["candidate_id"], "asset-a"
        )
        self.assertFalse(retry["execution_boundary"]["database_created"])

    def test_finalize_binds_both_rounds_and_becomes_stale_on_input_change(self) -> None:
        first_selection = self._selection(
            "round-one-selection.json",
            1,
            [
                {
                    "shot_id": "shot-001",
                    "status": "accepted",
                    "candidate_id": "asset-a",
                    "reason": "matched",
                },
                {
                    "shot_id": "shot-002",
                    "status": "rejected",
                    "candidate_id": "asset-b",
                    "reason": "action mismatch",
                },
            ],
        )
        retry_path = self.root / "retry-plan.json"
        write_rough_cut_contract(
            build_material_retry_plan(self.workspace_path, first_selection),
            retry_path,
        )
        round_two_candidates = self._json(
            "round-two-candidates.json",
            {
                "schema": "video_creation_pipeline.material_candidates.v1",
                "shots": [
                    {
                        "shot_id": "shot-002",
                        "candidates": [self._candidate("asset-b-alt", 0.91)],
                    }
                ],
            },
        )
        second_selection = self._selection(
            "round-two-selection.json",
            2,
            [
                {
                    "shot_id": "shot-002",
                    "status": "accepted",
                    "candidate_id": "asset-b-alt",
                    "reason": "action verified",
                }
            ],
        )

        different_payload = json.loads(first_selection.read_text(encoding="utf-8"))
        different_payload["operator_confirmation"]["confirmed_at"] = (
            "2026-07-23T11:01:00+08:00"
        )
        different_first_selection = self._json(
            "different-round-one-selection.json", different_payload
        )
        with self.assertRaisesRegex(ContractError, "round-one selection"):
            build_rough_cut_finalize_receipt(
                self.workspace_path,
                different_first_selection,
                retry_plan_path=retry_path,
                round_two_candidates_path=round_two_candidates,
                round_two_selection_path=second_selection,
                confirmed_by="operator",
                confirmed_at="2026-07-23T12:00:00+08:00",
            )

        receipt = build_rough_cut_finalize_receipt(
            self.workspace_path,
            first_selection,
            retry_plan_path=retry_path,
            round_two_candidates_path=round_two_candidates,
            round_two_selection_path=second_selection,
            confirmed_by="operator",
            confirmed_at="2026-07-23T12:00:00+08:00",
        )

        self.assertEqual(receipt["schema"], ROUGH_CUT_FINALIZE_RECEIPT_SCHEMA)
        self.assertTrue(receipt["readiness"]["render_eligible"])
        self.assertEqual(
            [row["candidate_id"] for row in receipt["final_decisions"]],
            ["asset-a", "asset-b-alt"],
        )
        validate_rough_cut_finalize_receipt(receipt)
        round_two_candidates.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_rough_cut_finalize_receipt(receipt)

    def test_unresolved_second_round_remains_visible_and_cannot_render(self) -> None:
        first_selection = self._selection(
            "round-one-selection.json",
            1,
            [
                {
                    "shot_id": "shot-001",
                    "status": "accepted",
                    "candidate_id": "asset-a",
                    "reason": "matched",
                },
                {
                    "shot_id": "shot-002",
                    "status": "rejected",
                    "candidate_id": "asset-b",
                    "reason": "action mismatch",
                },
            ],
        )
        retry_path = self.root / "retry-plan.json"
        write_rough_cut_contract(
            build_material_retry_plan(self.workspace_path, first_selection),
            retry_path,
        )
        candidates = self._json(
            "round-two-candidates.json",
            {
                "schema": "video_creation_pipeline.material_candidates.v1",
                "shots": [
                    {
                        "shot_id": "shot-002",
                        "candidates": [self._candidate("asset-b-alt", 0.7)],
                    }
                ],
            },
        )
        second_selection = self._selection(
            "round-two-selection.json",
            2,
            [
                {
                    "shot_id": "shot-002",
                    "status": "rejected",
                    "candidate_id": "asset-b-alt",
                    "reason": "continuity still fails",
                }
            ],
        )

        receipt = build_rough_cut_finalize_receipt(
            self.workspace_path,
            first_selection,
            retry_plan_path=retry_path,
            round_two_candidates_path=candidates,
            round_two_selection_path=second_selection,
            confirmed_by="operator",
            confirmed_at="2026-07-23T12:00:00+08:00",
        )

        self.assertFalse(receipt["readiness"]["render_eligible"])
        self.assertEqual(receipt["readiness"]["status"], "unresolved_material")
        self.assertEqual(receipt["unresolved_shots"][0]["shot_id"], "shot-002")

    def test_round_two_cannot_touch_locked_shots_or_escape_allowlist(self) -> None:
        first_selection = self._selection(
            "round-one-selection.json",
            1,
            [
                {
                    "shot_id": "shot-001",
                    "status": "accepted",
                    "candidate_id": "asset-a",
                    "reason": "matched",
                },
                {
                    "shot_id": "shot-002",
                    "status": "rejected",
                    "candidate_id": "asset-b",
                    "reason": "action mismatch",
                },
            ],
        )
        retry_path = self.root / "retry-plan.json"
        write_rough_cut_contract(
            build_material_retry_plan(self.workspace_path, first_selection),
            retry_path,
        )
        illegal_candidates = self._json(
            "illegal-round-two.json",
            {
                "schema": "video_creation_pipeline.material_candidates.v1",
                "shots": [
                    {
                        "shot_id": "shot-001",
                        "candidates": [self._candidate("asset-a", 0.95)],
                    },
                    {
                        "shot_id": "shot-002",
                        "candidates": [self._candidate("outside-pool", 0.95)],
                    },
                ],
            },
        )
        second_selection = self._selection(
            "round-two-selection.json",
            2,
            [
                {
                    "shot_id": "shot-002",
                    "status": "accepted",
                    "candidate_id": "outside-pool",
                    "reason": "illegal",
                }
            ],
        )

        with self.assertRaisesRegex(ContractError, "exactly the rejected shots"):
            build_rough_cut_finalize_receipt(
                self.workspace_path,
                first_selection,
                retry_plan_path=retry_path,
                round_two_candidates_path=illegal_candidates,
                round_two_selection_path=second_selection,
                confirmed_by="operator",
                confirmed_at="2026-07-23T12:00:00+08:00",
            )

    def test_all_passed_round_one_forbids_an_unnecessary_retry(self) -> None:
        selection = self._selection(
            "round-one-selection.json",
            1,
            [
                {
                    "shot_id": "shot-001",
                    "status": "accepted",
                    "candidate_id": "asset-a",
                    "reason": "matched",
                },
                {
                    "shot_id": "shot-002",
                    "status": "accepted",
                    "candidate_id": "asset-b",
                    "reason": "matched",
                },
            ],
        )

        with self.assertRaisesRegex(ContractError, "retry plan is unnecessary"):
            build_material_retry_plan(self.workspace_path, selection)
        receipt = build_rough_cut_finalize_receipt(
            self.workspace_path,
            selection,
            confirmed_by="operator",
            confirmed_at="2026-07-23T12:00:00+08:00",
        )
        self.assertTrue(receipt["readiness"]["render_eligible"])
        self.assertIsNone(receipt["round_two"])

    def _selection(self, name: str, round_number: int, decisions: list[dict]) -> Path:
        return self._json(
            name,
            {
                "schema": ROUGH_CUT_SELECTION_SCHEMA,
                "workspace_sha256": self.workspace["workspace_sha256"],
                "round": round_number,
                "decisions": decisions,
                "operator_confirmation": {
                    "confirmed": True,
                    "method": "visible_operator_selection",
                    "confirmed_by": "operator",
                    "confirmed_at": "2026-07-23T11:00:00+08:00",
                },
            },
        )

    @staticmethod
    def _storyboard() -> dict:
        return {
            "schema": "video_creation_pipeline.reference_recipe.v1",
            "shots": [
                {
                    "shot_id": "shot-001",
                    "start_s": 0,
                    "end_s": 2,
                    "script": "开场",
                },
                {
                    "shot_id": "shot-002",
                    "start_s": 2,
                    "end_s": 5,
                    "script": "演示",
                },
            ],
        }

    def _round_one_candidates(self) -> dict:
        return {
            "schema": "video_creation_pipeline.material_candidates.v1",
            "allowed_material_ids": ["asset-a", "asset-b", "asset-b-alt"],
            "shots": [
                {
                    "shot_id": "shot-001",
                    "candidates": [self._candidate("asset-a", 0.9)],
                },
                {
                    "shot_id": "shot-002",
                    "candidates": [self._candidate("asset-b", 0.82)],
                },
            ],
        }

    @staticmethod
    def _candidate(asset_id: str, confidence: float) -> dict:
        return {
            "asset_id": asset_id,
            "path": f"raw/{asset_id}.mp4",
            "confidence": confidence,
            "score_components": {"semantic": confidence},
            "source_media_id": asset_id,
            "reason": "candidate",
        }

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
