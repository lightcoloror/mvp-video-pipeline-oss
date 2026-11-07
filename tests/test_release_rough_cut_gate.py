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

from video_creation_pipeline.contract_io import canonical_json_sha256
from video_creation_pipeline.creative_gates import (
    build_approval_record,
    build_release_gate,
    validate_release_gate,
    write_creative_contract,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.rough_cut_workspace import (
    ROUGH_CUT_SELECTION_SCHEMA,
    build_rough_cut_finalize_receipt,
    build_rough_cut_workspace,
    write_rough_cut_contract,
)


class RoughCutReleaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_release_gate_binds_latest_resolved_rough_cut(self) -> None:
        storyboard = self._json(
            "storyboard.json",
            {
                "schema": "video_creation_pipeline.reference_recipe.v1",
                "shots": [{"shot_id": "shot-001", "start_s": 0, "end_s": 2}],
            },
        )
        candidates = self._json(
            "candidates.json",
            {
                "schema": "video_creation_pipeline.material_candidates.v1",
                "shots": [
                    {
                        "shot_id": "shot-001",
                        "candidates": [
                            {
                                "asset_id": "asset-a",
                                "path": "raw/asset-a.mp4",
                                "confidence": 0.9,
                                "score_components": {"semantic": 0.9},
                            }
                        ],
                    }
                ],
            },
        )
        workspace = build_rough_cut_workspace(storyboard, candidates)
        workspace_path = self.root / "workspace.json"
        write_rough_cut_contract(workspace, workspace_path)
        selection = self._json(
            "selection.json",
            {
                "schema": ROUGH_CUT_SELECTION_SCHEMA,
                "workspace_sha256": workspace["workspace_sha256"],
                "round": 1,
                "decisions": [
                    {
                        "shot_id": "shot-001",
                        "status": "accepted",
                        "candidate_id": "asset-a",
                        "reason": "matched",
                    }
                ],
                "operator_confirmation": {
                    "confirmed": True,
                    "method": "visible_operator_selection",
                    "confirmed_by": "operator",
                    "confirmed_at": "2026-07-23T12:00:00+08:00",
                },
            },
        )
        rough_cut = build_rough_cut_finalize_receipt(
            workspace_path,
            selection,
            confirmed_by="operator",
            confirmed_at="2026-07-23T12:01:00+08:00",
        )
        rough_cut_path = self.root / "rough-cut-finalize.json"
        write_rough_cut_contract(rough_cut, rough_cut_path)
        approvals, visual_plan = self._approvals()
        visual_payload = json.loads(visual_plan.read_text(encoding="utf-8"))
        render_manifest = self._json(
            "render-manifest.json",
            {
                "visual_plan_sha256": canonical_json_sha256(visual_payload),
                "rough_cut_finalize_sha256": rough_cut["finalize_receipt_sha256"],
            },
        )

        stale_manifest = self._json(
            "stale-render-manifest.json",
            {
                "visual_plan_sha256": canonical_json_sha256(visual_payload),
                "rough_cut_finalize_sha256": "0" * 64,
            },
        )
        with self.assertRaisesRegex(ContractError, "stale for the resolved rough cut"):
            build_release_gate(
                approvals,
                render_manifest_path=stale_manifest,
                visual_plan_path=visual_plan,
                rough_cut_finalize_path=rough_cut_path,
            )

        gate = build_release_gate(
            approvals,
            render_manifest_path=render_manifest,
            visual_plan_path=visual_plan,
            rough_cut_finalize_path=rough_cut_path,
        )

        validate_release_gate(gate)
        self.assertIsNotNone(gate["rough_cut_finalize"])
        selection.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_release_gate(gate)

    def _approvals(self) -> tuple[list[Path], Path]:
        script = self._json("script.json", {"text": "approved"})
        voice = self._json("voice.json", {"segments": [{"id": "voice-1"}]})
        measurements = self._json("measurements.json", {"duration_s": 2})
        audio = self.root / "voice.wav"
        audio.write_bytes(b"voice")
        visual = self._json("visual.json", {"shots": [{"id": "shot-001"}]})
        review = self.root / "director-review.md"
        review.write_text("approved", encoding="utf-8")
        records = [
            build_approval_record(
                "script",
                script,
                approved_by="operator",
                approved_at="2026-07-23T12:00:00+08:00",
            ),
            build_approval_record(
                "voice",
                voice,
                approved_by="operator",
                approved_at="2026-07-23T12:00:00+08:00",
                attachments=[
                    {"role": "voice_measurements", "path": measurements},
                    {"role": "audio", "path": audio},
                ],
            ),
            build_approval_record(
                "visuals",
                visual,
                approved_by="operator",
                approved_at="2026-07-23T12:00:00+08:00",
                attachments=[{"role": "director_review_sheet", "path": review}],
            ),
        ]
        paths = []
        for record in records:
            path = self.root / f"approval-{record['target']}.json"
            write_creative_contract(record, path)
            paths.append(path)
        return paths, visual

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
