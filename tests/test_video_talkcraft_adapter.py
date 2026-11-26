from __future__ import annotations

import copy
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "semantic-motion"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    build_ffmpeg_execution_receipt,
)
from video_creation_pipeline.generation_contracts import (
    build_shot_generation_unit,
    write_generation_contract,
)
from video_creation_pipeline.motion_sfx import (
    build_motion_sfx_source_manifest,
    derive_motion_sfx_cue_sheet,
    validate_motion_sfx_source_manifest,
    write_motion_sfx_contract,
)
from video_creation_pipeline.production_strategy import (
    build_visual_production_plan,
    write_production_strategy_contract,
)
from video_creation_pipeline.semantic_motion import (
    build_semantic_motion_plan,
    write_semantic_motion_plan,
)
from video_creation_pipeline.video_talkcraft_adapter import (
    VIDEO_TALKCRAFT_COMMIT,
    build_video_talkcraft_execution_receipt,
    build_video_talkcraft_recipe_plan,
    inspect_video_talkcraft_source,
    validate_video_talkcraft_execution_receipt,
    validate_video_talkcraft_recipe_plan,
    video_talkcraft_doctor,
    write_video_talkcraft_contract,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

RECIPE_IDS = (
    "callout-line-label",
    "chapter-title-card",
    "focus-dim-spotlight",
    "highlighter-sweep",
    "host-shrink-to-chip",
    "info-term-card",
    "media-pop-in",
    "number-counter",
    "slow-push-in",
    "step-timeline-vertical",
)


class VideoTalkcraftAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"video-talkcraft-{uuid4().hex}"
        shutil.copytree(FIXTURES, self.root)
        self.source_root = self.root / "external-source"
        self._write_external_source()
        with patch(
            "video_creation_pipeline.video_talkcraft_adapter."
            "VIDEO_TALKCRAFT_CATALOG_SHA256",
            "",
        ):
            fixture_catalog = inspect_video_talkcraft_source(
                self.source_root, source_commit=VIDEO_TALKCRAFT_COMMIT
            )
        self.catalog_patch = patch(
            "video_creation_pipeline.video_talkcraft_adapter."
            "VIDEO_TALKCRAFT_CATALOG_SHA256",
            fixture_catalog["catalog_sha256"],
        )
        self.catalog_patch.start()
        self.semantic = self._semantic_plan()
        self.semantic_path = write_semantic_motion_plan(
            self.semantic, self.root / "semantic-motion.json"
        )
        self.words = self.root / "words.json"
        atomic_write_json(
            {
                "words": [
                    {"text": "正式", "start": 1.65, "end": 1.90},
                    {"text": "治疗", "start": 1.90, "end": 2.20},
                    {"text": "前", "start": 2.20, "end": 2.35},
                    {"text": "再支付", "start": 2.35, "end": 2.80},
                    {"text": "二十一万", "start": 2.80, "end": 3.35},
                ]
            },
            self.words,
        )

    def tearDown(self) -> None:
        self.catalog_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_source_doctor_and_recipe_plan_are_hash_bound(self) -> None:
        doctor = video_talkcraft_doctor(self.source_root)
        self.assertEqual("ready_for_local_noncommercial_pilot", doctor["status"])
        self.assertEqual(10, doctor["whitelist_count"])
        self.assertFalse(doctor["source_code_vendored"])

        first = self._plan()
        second = self._plan()
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        cue = first["cues"][0]
        self.assertEqual("highlighter-sweep", cue["recipe_id"])
        self.assertEqual("word_timestamps", cue["anchor"]["source"])
        self.assertLessEqual(
            cue["entry_contract"]["declared_first_meaningful_frame"],
            cue["entry_contract"]["deadline_frame"],
        )
        self.assertFalse(cue["sfx_candidates"][0]["source_audio_reused"])
        validate_video_talkcraft_recipe_plan(first)

        tampered = copy.deepcopy(first)
        tampered["cues"][0]["anchor"]["text"] = "伪造锚点"
        tampered["plan_sha256"] = payload_sha256(tampered, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "bound source contracts"):
            validate_video_talkcraft_recipe_plan(tampered)

        card = self.source_root / "template" / "cards" / "highlighter-sweep.tsx"
        card.write_text(
            card.read_text(encoding="utf-8") + "\n// drift\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ContractError, "reviewed catalog"):
            validate_video_talkcraft_recipe_plan(first)

    def test_unknown_or_incompatible_recipe_fails_closed(self) -> None:
        selections = self.root / "selections.json"
        atomic_write_json(
            {
                "schema": "video_creation_pipeline.video_talkcraft_selections.v1",
                "selections": [
                    {"cue_id": "focus-expense-04", "recipe_id": "not-a-card"}
                ],
            },
            selections,
        )
        with self.assertRaisesRegex(ContractError, "non-whitelisted"):
            self._plan(selections_path=selections)

        atomic_write_json(
            {
                "schema": "video_creation_pipeline.video_talkcraft_selections.v1",
                "selections": [
                    {"cue_id": "focus-expense-04", "recipe_id": "number-counter"}
                ],
            },
            selections,
        )
        with self.assertRaisesRegex(ContractError, "not compatible"):
            self._plan(selections_path=selections)

    def test_execution_receipt_checks_real_entry_and_mobile_previews(self) -> None:
        plan = self._plan()
        plan_path = write_video_talkcraft_contract(plan, self.root / "recipe-plan.json")
        source = self.root / "render-source.mp4"
        source.write_bytes(b"synthetic-render-source")
        candidate = self.root / "rendered-candidate.mp4"
        candidate.write_bytes(b"synthetic-rendered-candidate")
        render = self.root / "render-receipt.json"
        atomic_write_json(
            build_ffmpeg_execution_receipt(
                "transcode",
                ["ffmpeg", "-i", str(source), str(candidate)],
                [{"role": "source_video", "path": source}],
                [{"role": "candidate_video", "path": candidate}],
            ),
            render,
        )
        cue = plan["cues"][0]
        observations = self.root / "observations.json"
        atomic_write_json(
            self._observations(
                plan,
                cue["entry_contract"]["deadline_frame"],
                candidate,
            ),
            observations,
        )
        receipt = build_video_talkcraft_execution_receipt(
            plan_path, render, observations
        )
        self.assertEqual("passed", receipt["status"])
        self.assertTrue(receipt["release_gate"]["render_eligible"])
        validate_video_talkcraft_execution_receipt(receipt)

        tampered = copy.deepcopy(receipt)
        tampered["checks"][0]["deadline_frame"] += 1
        tampered["receipt_sha256"] = payload_sha256(tampered, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "bound observations"):
            validate_video_talkcraft_execution_receipt(tampered)

        atomic_write_json(
            self._observations(
                plan,
                cue["entry_contract"]["deadline_frame"] + 1,
                candidate,
            ),
            observations,
        )
        failed = build_video_talkcraft_execution_receipt(
            plan_path, render, observations
        )
        self.assertEqual("failed", failed["status"])
        self.assertFalse(failed["release_gate"]["render_eligible"])

        other_candidate = self.root / "other-candidate.mp4"
        other_candidate.write_bytes(b"different-rendered-candidate")
        drifted = self._observations(
            plan,
            cue["entry_contract"]["deadline_frame"],
            other_candidate,
        )
        atomic_write_json(drifted, observations)
        with self.assertRaisesRegex(ContractError, "rendered candidate"):
            build_video_talkcraft_execution_receipt(plan_path, render, observations)

        atomic_write_json({"schema": "synthetic.render_receipt.v1"}, render)
        with self.assertRaisesRegex(ContractError, "render receipt schema"):
            build_video_talkcraft_execution_receipt(plan_path, render, observations)

    def test_recipe_sfx_projection_uses_existing_local_palette(self) -> None:
        plan = self._plan()
        plan_path = write_video_talkcraft_contract(plan, self.root / "recipe-plan.json")
        manifest = build_motion_sfx_source_manifest(
            self.root / "packaging-plan.json",
            semantic_motion_plan_path=self.semantic_path,
            word_timestamps_path=self.words,
            video_talkcraft_recipe_plan_path=plan_path,
        )
        validate_motion_sfx_source_manifest(manifest)
        manifest_path = write_motion_sfx_contract(
            manifest, self.root / "motion-sfx-manifest.json"
        )
        sheet = derive_motion_sfx_cue_sheet(manifest_path)
        recipe_cues = [
            cue
            for cue in sheet["cues"]
            if "recipe:highlighter-sweep" in cue.get("evidence_ids", [])
        ]
        self.assertEqual(1, len(recipe_cues))
        self.assertEqual("warm-pencil-mark", recipe_cues[0]["preferred_asset_id"])
        self.assertEqual("causality", recipe_cues[0]["function"])

    def test_reviewed_commit_is_required(self) -> None:
        with self.assertRaisesRegex(ContractError, "reviewed commit"):
            inspect_video_talkcraft_source(self.source_root, source_commit="0" * 40)

    def _plan(self, **overrides: object) -> dict:
        return build_video_talkcraft_recipe_plan(
            self.semantic_path,
            self.source_root,
            source_commit=VIDEO_TALKCRAFT_COMMIT,
            word_timestamps_path=self.words,
            **overrides,
        )

    def _semantic_plan(self) -> dict:
        packaging = self.root / "packaging-plan.json"
        script = self.root / "script-anchor.json"
        asset = self.root / "source-evidence.svg"
        bundle = self.root / "vkp-bundle"
        shutil.copytree(ROOT / "tests" / "fixtures" / "vkp_bundle", bundle)
        edit_job = self.root / "edit-job.json"
        write_edit_job(build_edit_job(bundle), edit_job)
        unit = self.root / "shot-unit.json"
        write_generation_contract(
            build_shot_generation_unit(
                edit_job,
                "storyboard-0001",
                generator="remotion",
                visual_prompt="Synthetic video-talkcraft adapter fixture.",
            ),
            unit,
        )
        visual_plan = self.root / "visual-production-plan.json"
        write_production_strategy_contract(
            build_visual_production_plan(unit), visual_plan
        )
        spec = json.loads((self.root / "build-spec.json").read_text(encoding="utf-8"))
        return build_semantic_motion_plan(
            packaging,
            script,
            asset,
            visual_plan,
            asset_id=str(spec["asset_id"]),
            media_type=str(spec["media_type"]),
            cues=list(spec["cues"]),
            width=int(spec["width"]),
            height=int(spec["height"]),
            fps=int(spec["fps"]),
            duration_s=float(spec["duration_s"]),
            evidence_status=str(spec["evidence_status"]),
            authorization_status=str(spec["authorization_status"]),
            deidentified=spec["deidentified"] is True,
        )

    def _write_external_source(self) -> None:
        cards = self.source_root / "template" / "cards"
        cards.mkdir(parents=True)
        (self.source_root / "references").mkdir()
        (self.source_root / "demos" / "_lib").mkdir(parents=True)
        (self.source_root / "LICENSE").write_text(
            "PolyForm Noncommercial 1.0.0 fixture\n", encoding="utf-8"
        )
        (self.source_root / "references" / "taxonomy.md").write_text(
            "# Synthetic taxonomy\n", encoding="utf-8"
        )
        entries: list[str] = []
        for recipe_id in RECIPE_IDS:
            (cards / f"{recipe_id}.tsx").write_text(
                "export const meta = { width: 960, height: 540, fps: 30, "
                "durationInFrames: 180 };\n",
                encoding="utf-8",
            )
            cue = []
            if recipe_id == "highlighter-sweep":
                cue = [
                    {
                        "t": 0.7,
                        "name": "pk:text-marker-pen-line",
                        "vol": 0.45,
                        "note": "marker sweep",
                    }
                ]
            entries.append(f"{json.dumps(recipe_id)}: {json.dumps(cue)},")
        (self.source_root / "demos" / "_lib" / "sfx-map.js").write_text(
            "window.SFX_MAP = {\n" + "\n".join(entries) + "\n};\n",
            encoding="utf-8",
        )

    @staticmethod
    def _observations(plan: dict, actual_frame: int, candidate: Path) -> dict:
        cue = copy.deepcopy(plan["cues"][0])
        return {
            "schema": "video_creation_pipeline.motion_entry_observations.v1",
            "recipe_plan_sha256": plan["plan_sha256"],
            "source_candidate": artifact_reference(
                candidate, "motion entry observation candidate"
            ),
            "observations": [
                {
                    "cue_id": cue["cue_id"],
                    "observation_source": "playwright_dom_probe",
                    "observed_first_meaningful_frame": actual_frame,
                    "target_rect": cue["target_rect"],
                    "mobile_previews": [
                        {
                            "width": 270,
                            "height": 480,
                            "overflow_detected": False,
                            "text_clipped": False,
                            "critical_text_visible": True,
                            "minimum_rendered_font_px": 9.0,
                        },
                        {
                            "width": 390,
                            "height": 693,
                            "overflow_detected": False,
                            "text_clipped": False,
                            "critical_text_visible": True,
                            "minimum_rendered_font_px": 13.0,
                        },
                    ],
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
