from __future__ import annotations

import copy
import json
import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

import pytest

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
from video_creation_pipeline.generation_contracts import (
    build_shot_generation_unit,
    write_generation_contract,
)
from video_creation_pipeline.privacy_exclusion_zones import (
    build_privacy_exclusion_zones,
    build_privacy_layout_collision_report,
    project_identity_privacy_exclusion_zones,
    write_privacy_exclusion_zones,
    write_privacy_layout_collision_report,
)
from video_creation_pipeline.production_strategy import (
    build_visual_production_plan,
    write_production_strategy_contract,
)
from video_creation_pipeline.semantic_motion import (
    HYPERFRAMES_SOURCE_COMMIT,
    build_hyperframes_html,
    build_semantic_motion_plan,
    materialize_hyperframes_project,
    validate_semantic_motion_plan,
    write_semantic_motion_plan,
)
from video_creation_pipeline.visual_routing import (
    CONTENT_VISUAL_ROUTING_SPEC_SCHEMA,
    build_content_visual_routing_plan,
    build_visual_style_profile,
    write_visual_routing_contract,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job


class SemanticMotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"semantic-motion-{uuid4().hex}"
        shutil.copytree(FIXTURES, self.root)
        self.packaging = self.root / "packaging-plan.json"
        self.script = self.root / "script-anchor.json"
        self.asset = self.root / "source-evidence.svg"
        self.spec = json.loads(
            (self.root / "build-spec.json").read_text(encoding="utf-8")
        )
        bundle = self.root / "vkp-bundle"
        shutil.copytree(ROOT / "tests" / "fixtures" / "vkp_bundle", bundle)
        edit_job_path = self.root / "edit-job.json"
        write_edit_job(build_edit_job(bundle), edit_job_path)
        unit_path = self.root / "shot-unit.json"
        write_generation_contract(
            build_shot_generation_unit(
                edit_job_path,
                "storyboard-0001",
                generator="remotion",
                visual_prompt="Fixture layout contract for semantic motion.",
            ),
            unit_path,
        )
        self.visual_plan = self.root / "visual-production-plan.json"
        write_production_strategy_contract(
            build_visual_production_plan(unit_path), self.visual_plan
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_build_is_hash_stable_and_preserves_candidate_boundary(self) -> None:
        first = self._build()
        second = self._build()

        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(
            first["render_policy"]["renderer_source_commit"],
            HYPERFRAMES_SOURCE_COMMIT,
        )
        self.assertTrue(first["authority_boundary"]["candidate_only"])
        self.assertFalse(first["authority_boundary"]["publish_performed"])
        self.assertEqual(first["cues"][0]["script_anchor"]["status"], "confirmed")

    def test_materialized_project_uses_native_waapi_without_remote_runtime(
        self,
    ) -> None:
        plan = self._build()
        plan_path = write_semantic_motion_plan(plan, self.root / "plan.json")
        paths = materialize_hyperframes_project(plan_path, self.root / "project")
        source = paths["html"].read_text(encoding="utf-8")

        self.assertIn(".animate(", source)
        self.assertNotIn("gsap", source.lower())
        self.assertNotIn("https://", source)
        self.assertIn('data-composition-id="main"', source)
        self.assertEqual(paths["asset"].read_bytes(), self.asset.read_bytes())

    def test_static_fallback_is_explicit_and_deterministic(self) -> None:
        plan = self._build()
        first = build_hyperframes_html(plan, "assets/source.svg", static_fallback=True)
        second = build_hyperframes_html(plan, "assets/source.svg", static_fallback=True)

        self.assertEqual(first, second)
        self.assertIn('"static":true', first)
        self.assertIn("full_frame_with_box_and_label", json.dumps(plan))

    def test_content_route_and_style_drive_evidence_focus_html(self) -> None:
        style_path = self.root / "visual-style.json"
        write_visual_routing_contract(
            build_visual_style_profile("paper_cutout"), style_path
        )
        route_spec = self.root / "visual-route-spec.json"
        route_spec.write_text(
            json.dumps(
                {
                    "schema": CONTENT_VISUAL_ROUTING_SPEC_SCHEMA,
                    "scenes": [
                        {
                            "scene_id": "segment-04",
                            "content_kind": "evidence_document",
                            "communication_goal": "focus the verified evidence",
                            "generation_allowed": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        route_path = self.root / "visual-routes.json"
        write_visual_routing_contract(
            build_content_visual_routing_plan(self.packaging, route_spec, style_path),
            route_path,
        )
        plan = self._build(
            content_visual_routing_plan_path=route_path,
            visual_style_profile_path=style_path,
        )
        source = build_hyperframes_html(plan, "assets/source.svg")
        self.assertIn("background:#f4efe6", source)
        self.assertIn('stroke="#23777d"', source)
        self.assertIn("source_content_visual_routing_plan", plan)
        self.assertIn("source_visual_style_profile", plan)

    def test_source_hash_drift_fails_closed(self) -> None:
        plan = self._build()
        self.asset.write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_semantic_motion_plan(plan)

    def test_visual_production_plan_is_required_and_hash_bound(self) -> None:
        plan = self._build()
        del plan["source_visual_production_plan"]
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "schema validation failed"):
            validate_semantic_motion_plan(plan)

        plan = self._build()
        self.visual_plan.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_semantic_motion_plan(plan)

    def test_unauthorized_or_identified_asset_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "schema validation failed"):
            self._build(
                authorization_status="unreviewed",
                deidentified=False,
            )

    def test_out_of_bounds_focus_fails_closed(self) -> None:
        plan = self._build()
        plan["cues"][0]["focus_destination"]["x"] = 0.8
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "normalized bounds"):
            validate_semantic_motion_plan(plan)

    def test_subtitle_collision_fails_closed(self) -> None:
        plan = self._build()
        plan["cues"][0]["focus_destination"] = {
            "x": 0.2,
            "y": 0.65,
            "width": 0.4,
            "height": 0.08,
        }
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "collides with protected region"):
            validate_semantic_motion_plan(plan)

    def test_label_overflow_fails_closed(self) -> None:
        plan = self._build()
        plan["cues"][0]["label"]["text"] = (
            "这是一段远远超过安全标签区域长度的合成说明文字"
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "overflows"):
            validate_semantic_motion_plan(plan)

    def test_motion_label_cannot_restate_the_spoken_subtitle(self) -> None:
        plan = self._build()
        plan["cues"][0]["label"]["text"] = "正式治疗前再支付二十一万七千多"
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "restates the spoken subtitle"):
            validate_semantic_motion_plan(plan)

    def test_visible_person_requires_a_protected_person_region(self) -> None:
        plan = self._build()
        plan["cues"][0]["presentation_mode"] = "person_with_overlay"
        plan["cues"][0]["layout_decision"] = self._person_layout_decision()
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "person is required"):
            validate_semantic_motion_plan(plan)

    def test_visible_person_region_is_collision_protected(self) -> None:
        plan = self._build()
        plan["cues"][0]["presentation_mode"] = "person_with_overlay"
        plan["cues"][0]["layout_decision"] = self._person_layout_decision()
        plan["cues"][0]["protected_regions"].append(
            {
                "id": "person",
                "rect": {"x": 0.05, "y": 0.2, "width": 0.7, "height": 0.35},
            }
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "collides with protected region"):
            validate_semantic_motion_plan(plan)

    def test_three_peer_items_require_all_layout_candidates(self) -> None:
        plan = self._build()
        decision = plan["cues"][0]["layout_decision"]
        decision["peer_item_count"] = 4
        decision["selected_layout"] = "vertical_list"
        decision["layout_candidates"] = [
            {"layout": "vertical_list", "reason": "keeps all items readable"}
        ]
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "three or more peer items"):
            validate_semantic_motion_plan(plan)

    def test_peer_layout_and_top_corner_review_can_pass(self) -> None:
        plan = self._build()
        cue = plan["cues"][0]
        cue["presentation_mode"] = "person_with_overlay"
        decision = self._person_layout_decision()
        decision["peer_item_count"] = 4
        decision["selected_layout"] = "vertical_list"
        decision["layout_candidates"] = [
            {
                "layout": "vertical_list",
                "reason": "largest common font and one spoken-order path",
            },
            {
                "layout": "horizontal_row",
                "reason": "rejected because four columns are too narrow in 9:16",
            },
            {
                "layout": "grid",
                "reason": "rejected because medium labels wrap unevenly",
            },
        ]
        decision["selection_reason"] = (
            "vertical list preserves the largest shared type and yields the center"
        )
        cue["layout_decision"] = decision
        cue["protected_regions"].append(
            {
                "id": "person",
                "rect": {"x": 0.05, "y": 0.05, "width": 0.2, "height": 0.12},
            }
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        validate_semantic_motion_plan(plan)

    def test_person_window_requires_both_top_corner_candidates(self) -> None:
        plan = self._build()
        plan["cues"][0]["presentation_mode"] = "person_with_overlay"
        decision = self._person_layout_decision()
        decision["person_window"]["corner_candidates"] = [
            decision["person_window"]["corner_candidates"][0]
        ]
        plan["cues"][0]["layout_decision"] = decision
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "top-left and top-right"):
            validate_semantic_motion_plan(plan)

    def test_lower_person_window_corner_is_an_explicit_exception(self) -> None:
        plan = self._build()
        cue = plan["cues"][0]
        cue["presentation_mode"] = "person_with_overlay"
        decision = self._person_layout_decision()
        person = decision["person_window"]
        person["selected_corner"] = "bottom_left"
        person["corner_candidates"] = [
            {"corner": "top_left", "available": False, "reason": "main heading"},
            {"corner": "top_right", "available": False, "reason": "platform controls"},
            {"corner": "bottom_left", "available": True, "reason": "only clear region"},
        ]
        person["exception_reason"] = (
            "both top corners are blocked and the subtitle is temporarily absent"
        )
        cue["layout_decision"] = decision
        cue["protected_regions"].append(
            {
                "id": "person",
                "rect": {"x": 0.01, "y": 0.45, "width": 0.05, "height": 0.1},
            }
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        validate_semantic_motion_plan(plan)

    def test_lower_person_window_corner_fails_when_a_top_corner_is_available(
        self,
    ) -> None:
        plan = self._build()
        cue = plan["cues"][0]
        cue["presentation_mode"] = "person_with_overlay"
        decision = self._person_layout_decision()
        person = decision["person_window"]
        person["selected_corner"] = "bottom_left"
        person["corner_candidates"].append(
            {"corner": "bottom_left", "available": True, "reason": "clear"}
        )
        person["exception_reason"] = "requested as an exception"
        cue["layout_decision"] = decision
        cue["protected_regions"].append(
            {
                "id": "person",
                "rect": {"x": 0.01, "y": 0.45, "width": 0.05, "height": 0.1},
            }
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "both top corners unavailable"):
            validate_semantic_motion_plan(plan)

    def test_human_composition_review_cannot_be_pending(self) -> None:
        plan = self._build()
        plan["cues"][0]["layout_decision"]["human_composition_review"]["status"] = (
            "pending"
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "schema validation failed"):
            validate_semantic_motion_plan(plan)

    def test_motion_cannot_escape_bound_packaging_scene(self) -> None:
        plan = self._build()
        packaging = json.loads(self.packaging.read_text(encoding="utf-8"))
        packaging["scenes"][0]["end_s"] = 5.0
        self.packaging.write_text(json.dumps(packaging), encoding="utf-8")
        plan["source_packaging_plan"]["bytes"] = self.packaging.stat().st_size
        from video_creation_pipeline.contract_io import (
            artifact_reference,
        )

        plan["source_packaging_plan"] = artifact_reference(
            self.packaging, "video packaging plan"
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "outside its packaging scene"):
            validate_semantic_motion_plan(plan)

    def test_write_refuses_to_overwrite_bound_input(self) -> None:
        plan = self._build()

        with self.assertRaisesRegex(ContractError, "cannot overwrite"):
            write_semantic_motion_plan(plan, self.packaging)

    def test_privacy_zones_relocate_label_and_become_hard_protection(self) -> None:
        zones_path = self._privacy_zones()
        plan = self._build(privacy_exclusion_zones_path=zones_path)
        cue = plan["cues"][0]
        privacy = [
            row
            for row in cue["protected_regions"]
            if row["id"].startswith("privacy_face_")
        ]
        self.assertEqual(1, len(privacy))
        self.assertEqual("label_relocated", cue["privacy_avoidance"]["status"])
        self.assertTrue(cue["privacy_avoidance"]["label_position_changed"])
        self.assertFalse(cue["privacy_avoidance"]["runtime_reposition_allowed"])
        self.assertIn("source_privacy_exclusion_zones", plan)
        normal_html = build_hyperframes_html(plan, "assets/source.svg")
        debug_html = build_hyperframes_html(
            plan, "assets/source.svg", privacy_debug=True
        )
        self.assertNotIn('class="privacy-debug-region"', normal_html)
        self.assertIn('class="privacy-debug-region"', debug_html)
        self.assertIn(privacy[0]["id"], debug_html)
        self.assertIn('data-privacy-debug="true"', debug_html)
        validate_semantic_motion_plan(plan)

        tampered = copy.deepcopy(plan)
        tampered["cues"][0]["protected_regions"] = [
            row
            for row in tampered["cues"][0]["protected_regions"]
            if not row["id"].startswith("privacy_")
        ]
        tampered["plan_sha256"] = payload_sha256(tampered, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "privacy regions drifted"):
            validate_semantic_motion_plan(tampered)

    def test_dynamic_privacy_layout_e2e_is_deterministic(self) -> None:
        pytest.importorskip("norfair")
        source = self.root / "dynamic-privacy-source.mp4"
        redacted = self.root / "dynamic-privacy-redacted.mp4"
        source.write_bytes(b"synthetic privacy source")
        redacted.write_bytes(b"synthetic privacy redacted")
        job_path = self.root / "dynamic-privacy-job.json"
        receipt_path = self.root / "dynamic-privacy-receipt.json"
        job = {
            "schema": "video_creation_pipeline.privacy_redaction_job.v1",
            "input": artifact_reference(source, "synthetic privacy source"),
            "output_path": str(redacted.resolve()),
            "parameters": {"dilation": 112, "window": 5},
        }
        job["job_sha256"] = payload_sha256(job, "job_sha256")
        atomic_write_json(job, job_path)
        receipt = {
            "schema": "video_creation_pipeline.privacy_redaction_receipt.v1",
            "job_sha256": job["job_sha256"],
            "input": job["input"],
            "output": artifact_reference(redacted, "synthetic privacy redacted"),
            "engine": {
                "source_commit": "ded52d269b6e8e19dbbaf79f353426da3a228a98",
                "model_hash": "a" * 64,
            },
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        atomic_write_json(receipt, receipt_path)
        clock = {
            "fps": 10.0,
            "frame_count": 60,
            "width": 1080,
            "height": 1920,
            "duration_s": 6.0,
        }
        detections = [
            {
                "frame_index": frame,
                "frame_width": 1080,
                "frame_height": 1920,
                "xyxy": [900 + frame % 3, 600, 1000 + frame % 3, 850],
                "confidence": 0.96,
                "target_type": "face",
            }
            for frame in range(60)
        ]
        source_zones = build_privacy_exclusion_zones(
            source,
            redacted,
            job_path,
            receipt_path,
            detections,
            media_clock=clock,
        )
        self.assertEqual("complete", source_zones["coverage"]["status"])
        source_zones_path = write_privacy_exclusion_zones(
            source_zones, self.root / "dynamic-privacy-zones.source.json"
        )
        final_zones = project_identity_privacy_exclusion_zones(
            source_zones_path, target_width=1080, target_height=1920
        )
        zones_path = write_privacy_exclusion_zones(
            final_zones, self.root / "dynamic-privacy-zones.json"
        )

        first = self._build(privacy_exclusion_zones_path=zones_path)
        second = self._build(privacy_exclusion_zones_path=zones_path)
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(
            "label_relocated", first["cues"][0]["privacy_avoidance"]["status"]
        )
        plan_path = write_semantic_motion_plan(
            first, self.root / "dynamic-privacy-motion.json"
        )
        report = build_privacy_layout_collision_report(
            plan_path, zones_path, redaction_order="redaction_after_graphics"
        )
        self.assertEqual(0, report["summary"]["collision_count"])
        self.assertTrue(report["summary"]["geometry_gate_passed"])
        report_path = write_privacy_layout_collision_report(
            report, self.root / "dynamic-privacy-collision-report.json"
        )
        repeated = build_privacy_layout_collision_report(
            plan_path, zones_path, redaction_order="redaction_after_graphics"
        )
        self.assertEqual(report["report_sha256"], repeated["report_sha256"])
        self.assertTrue(report_path.is_file())

        formal = materialize_hyperframes_project(
            plan_path, self.root / "dynamic-privacy-formal"
        )
        debug = materialize_hyperframes_project(
            plan_path, self.root / "dynamic-privacy-debug", privacy_debug=True
        )
        self.assertNotIn(
            'class="privacy-debug-region"', formal["html"].read_text(encoding="utf-8")
        )
        self.assertIn(
            'class="privacy-debug-region"', debug["html"].read_text(encoding="utf-8")
        )

    def _build(self, **overrides: object) -> dict:
        options = copy.deepcopy(self.spec)
        options.update(overrides)
        return build_semantic_motion_plan(
            self.packaging,
            self.script,
            self.asset,
            self.visual_plan,
            asset_id=str(options["asset_id"]),
            media_type=str(options["media_type"]),
            cues=list(options["cues"]),
            width=int(options["width"]),
            height=int(options["height"]),
            fps=int(options["fps"]),
            duration_s=float(options["duration_s"]),
            evidence_status=str(options["evidence_status"]),
            authorization_status=str(options["authorization_status"]),
            deidentified=options["deidentified"] is True,
            content_visual_routing_plan_path=options.get(
                "content_visual_routing_plan_path"
            ),
            visual_style_profile_path=options.get("visual_style_profile_path"),
            privacy_exclusion_zones_path=options.get("privacy_exclusion_zones_path"),
            smart_reframe_plan_path=options.get("smart_reframe_plan_path"),
        )

    def _privacy_zones(self) -> Path:
        source = self.root / "privacy-source.mp4"
        redacted = self.root / "privacy-redacted.mp4"
        source.write_bytes(b"privacy source")
        redacted.write_bytes(b"privacy redacted")
        job = self.root / "privacy-job.json"
        receipt = self.root / "privacy-receipt.json"
        atomic_write_json({"fixture": "job"}, job)
        atomic_write_json({"fixture": "receipt"}, receipt)
        zones = {
            "schema": "video_creation_pipeline.privacy_exclusion_zones.v1",
            "source_media": artifact_reference(source, "privacy source"),
            "redacted_media": artifact_reference(redacted, "privacy redacted"),
            "source_redaction_job": artifact_reference(job, "privacy job"),
            "source_redaction_receipt": artifact_reference(receipt, "privacy receipt"),
            "coordinate_space": "final_canvas_normalized",
            "media_clock": {
                "fps": 30.0,
                "frame_count": 180,
                "width": 1080,
                "height": 1920,
                "duration_s": 6.0,
            },
            "detector": {
                "tool": "SentryBlur",
                "source_commit": "ded52d269b6e8e19dbbaf79f353426da3a228a98",
                "model_hash": "a" * 64,
                "target_types": ["face"],
                "same_invocation_as_redaction": True,
            },
            "redaction_parameters": {
                "dilation_px": 112,
                "temporal_window_frames": 5,
            },
            "scene_cuts": [],
            "tracks": [
                {
                    "track_id": "anonymous-face-scene-0-track-1",
                    "target_type": "face",
                    "identity_claimed": False,
                    "zones": [
                        {
                            "frame_index": 6,
                            "pts_s": 0.2,
                            "confidence": 0.96,
                            "source": "same_invocation_detection",
                            "raw_rect": {
                                "x": 0.76,
                                "y": 0.31,
                                "width": 0.12,
                                "height": 0.16,
                            },
                            "effective_rect": {
                                "x": 0.72,
                                "y": 0.27,
                                "width": 0.2,
                                "height": 0.24,
                            },
                            "visible": True,
                        }
                    ],
                }
            ],
            "coverage": {
                "status": "complete",
                "covered_frame_count": 1,
                "uncovered_spans": [],
                "review_required": False,
            },
            "authority_boundary": {
                "candidate_only": True,
                "identity_recognition_performed": False,
                "layout_authority": "semantic_motion_plan",
                "publish_allowed": False,
                "final_privacy_rescan_required": True,
            },
        }
        zones["zones_sha256"] = payload_sha256(zones, "zones_sha256")
        return atomic_write_json(zones, self.root / "privacy-zones.json")

    def _person_layout_decision(self) -> dict:
        return {
            "semantic_purpose": "keep the speaker identifiable while information dominates",
            "peer_item_count": 1,
            "selected_layout": "single_focus",
            "layout_candidates": [
                {"layout": "single_focus", "reason": "one primary information object"}
            ],
            "selection_reason": "the information object keeps the main canvas",
            "person_window": {
                "visible": True,
                "selected_corner": "top_left",
                "corner_candidates": [
                    {"corner": "top_left", "available": True, "reason": "clear"},
                    {
                        "corner": "top_right",
                        "available": False,
                        "reason": "platform controls",
                    },
                ],
                "placement_reason": "top left yields the largest information region",
                "information_space_yielded": True,
                "exception_reason": None,
            },
            "human_composition_review": {
                "status": "approved",
                "reviewed_by": "fixture-human",
                "reviewed_at": "2026-08-20T22:00:00+08:00",
                "dimensions": {
                    "primary_information": "pass",
                    "space_yield": "pass",
                    "visual_balance": "pass",
                },
            },
        }


if __name__ == "__main__":
    unittest.main()
