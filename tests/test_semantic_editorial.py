from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_creation_pipeline.contract_io import (
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
)
from video_creation_pipeline.creative_gates import build_approval_record
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.semantic_editorial import (
    aggregate_editorial_preferences,
    build_editorial_feedback,
    build_editorial_packaging_projection,
    build_editorial_visual_beats,
    build_semantic_editorial_plan,
    editorial_review_handoff,
    validate_editorial_artifact,
    validate_screen_recording_profile,
    write_editorial_artifact,
)
from video_creation_pipeline.semantic_editorial_cli import main
from video_creation_pipeline.visual_routing import (
    build_content_visual_routing_plan,
    build_visual_style_profile,
)


def make_fixture(root: Path) -> tuple[Path, Path]:
    """Pure synthetic wire fixture matching VKP's published v1 projection."""
    refs = {}
    for role in ("media", "manifest", "timeline", "transcript"):
        path = root / f"synthetic-{role}.txt"
        path.write_text(f"synthetic {role} only", encoding="utf-8")
        refs[role] = {
            "role": role,
            "scope": "bundle_relative",
            "path": path.name,
            "locator": f"bundle:{path.name}",
            "name": path.name,
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    segments = [
        {
            "segment_id": f"segment-{index}",
            "start_ms": index * 2000,
            "end_ms": index * 2000 + 2000,
            "source_index": index,
            "timestamp_basis": "absolute_media_milliseconds",
        }
        for index in range(5)
    ]
    identity = canonical_json_sha256(
        {
            "artifacts": {
                key: {"bytes": ref["bytes"], "sha256": ref["sha256"]}
                for key, ref in refs.items()
            },
            "timestamp_policy": {
                "basis": "absolute_media_milliseconds",
                "origin": "media_start",
                "tolerance_ms": 1,
            },
        }
    )
    value = {
        "schema": "video_knowledge_pipeline.narration_timebase.v1",
        "contract_version": "1.0.0",
        "closure_run_id": "synthetic-editorial",
        "status": "complete_draft",
        "downstream_ready": True,
        "candidate_only": True,
        "review_required": True,
        "source_identity_sha256": identity,
        "artifacts": refs,
        "duration_ms": 10000,
        "timestamp_basis": {
            "timestamp_basis": "absolute_media_milliseconds",
            "origin": "media_start",
            "unit": "milliseconds",
            "numerator": 1,
            "denominator": 1000,
            "tolerance_ms": 1,
            "adapter_offset_applied_ms": 0,
            "offset_application": "upstream_already_applied_once",
        },
        "sentence_segments": deepcopy(segments),
        "semantic_segments": segments,
        "chapters": [],
        "long_video_resume": {},
        "human_review": {"required": True},
        "projection_resume": {},
        "downstream_time_authority": {
            "mvp_handoff_must_preserve_absolute_times": True,
            "renderer_or_music_may_rewrite_semantic_boundaries": False,
        },
        "upstream_version_and_invalidation": {
            "invalidate_on_any_artifact_hash_change": True
        },
        "review_gaps": [],
        "operator_boundary": {
            "local_only": True,
            "human_review_required": True,
            "provider_called": False,
            "uploaded": False,
            "published": False,
            "timeline_modified": False,
            "chunk_offsets_applied_by_adapter": False,
        },
    }
    value["contract_sha256"] = payload_sha256(value, "contract_sha256")
    timebase = atomic_write_json(value, root / "timebase.json")
    spec = {
        "schema": "video_creation_pipeline.semantic_editorial_spec.v1",
        "decisions": [
            {
                "segment_id": row["segment_id"],
                "action": "keep",
                "rhetorical_role": role,
                "redundancy_group": "examples" if role == "example" else "bookends",
                "keep_reason": "Preserve the documented function",
                "removal_risk": "Context may be lost",
                "confidence": 0.9,
                "evidence_refs": [f"semantic:{row['segment_id']}"],
            }
            for row, role in zip(
                segments,
                ["hook", "example", "example", "example", "recap"],
                strict=True,
            )
        ],
    }
    return timebase, atomic_write_json(spec, root / "editorial-spec.json")


class SemanticEditorialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMP_ROOT"))
        self.root = Path(self.temp.name)
        self.timebase, self.spec = make_fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def change_spec(self, index, **changes):
        value = self.read(self.spec)
        value["decisions"][index].update(changes)
        atomic_write_json(value, self.spec)

    def change_timebase(self, callback):
        value = self.read(self.timebase)
        callback(value)
        value["contract_sha256"] = payload_sha256(value, "contract_sha256")
        atomic_write_json(value, self.timebase)

    def plan(self):
        return build_semantic_editorial_plan(self.timebase, self.root, self.spec)

    def approval(self, path):
        return atomic_write_json(
            build_approval_record(
                "storyboard",
                path,
                approved_by="synthetic-reviewer",
                approved_at="2026-09-05T08:00:00+00:00",
                comment="Synthetic test only",
            ),
            self.root / f"{path.stem}-approval.json",
        )

    def test_plan_double_run_preserves_times_and_source(self):
        before = file_sha256(self.timebase)
        plan = self.plan()
        self.assertEqual(plan, self.plan())
        self.assertEqual(
            plan["segments"][3]["source_interval_ms"], {"start": 6000, "end": 8000}
        )
        self.assertEqual(before, file_sha256(self.timebase))
        validate_editorial_artifact(plan)

    def test_three_examples_choose_one_without_auto_deleting_others(self):
        self.change_spec(
            1, action="select_best_example", selected_segment_id="segment-3"
        )
        plan = self.plan()
        self.assertEqual(plan["segments"][1]["effective_action"], "select_best_example")
        self.assertEqual(plan["segments"][2]["effective_action"], "keep")
        self.assertFalse(plan["boundary"]["execute"])

    def test_hook_recap_not_duplicates(self):
        self.change_spec(4, action="delete")
        self.assertIn(
            "different_rhetorical_roles_are_not_duplicates",
            self.plan()["segments"][4]["review_reasons"],
        )

    def test_low_confidence_and_no_evidence_require_review(self):
        self.change_spec(1, action="delete", confidence=0.2, evidence_refs=[])
        self.assertEqual(
            self.plan()["segments"][1]["effective_action"], "review_required"
        )

    def test_unknown_evidence_rejected(self):
        self.change_spec(1, evidence_refs=["invented:claim"])
        with self.assertRaises(ContractError):
            self.plan()

    def test_new_fact_field_rejected(self):
        self.change_spec(1, rewritten_text="Invented factual promise")
        with self.assertRaises(ContractError):
            self.plan()

    def test_human_locked_boundary_is_not_edited(self):
        self.change_timebase(
            lambda v: v["semantic_segments"][1].update(human_boundary_confirmed=True)
        )
        self.change_spec(1, action="compress")
        self.assertEqual(
            self.plan()["segments"][1]["effective_action"], "review_required"
        )

    def test_fact_removal_is_review_only(self):
        self.change_spec(1, action="delete", rhetorical_role="evidence")
        self.assertIn(
            "fact_or_qualification_requires_review",
            self.plan()["segments"][1]["review_reasons"],
        )

    def test_move_needs_approval_and_retains_source_times(self):
        self.change_spec(1, action="move", after_segment_id="segment-3")
        path = write_editorial_artifact(self.plan(), self.root / "plan.json")
        with self.assertRaises(ContractError):
            editorial_review_handoff(path, self.root / "missing.json")
        handoff = editorial_review_handoff(path, self.approval(path))
        self.assertFalse(handoff["boundary"]["execute"])
        self.assertEqual(handoff["segments"][1]["source_interval_ms"]["start"], 2000)
        validate_editorial_artifact(handoff)

    def test_move_cycle_rejected(self):
        self.change_spec(1, action="move", after_segment_id="segment-2")
        self.change_spec(2, action="move", after_segment_id="segment-1")
        with self.assertRaises(ContractError):
            self.plan()

    def test_inconsistent_example_winners_rejected(self):
        self.change_spec(
            1, action="select_best_example", selected_segment_id="segment-2"
        )
        self.change_spec(
            2, action="select_best_example", selected_segment_id="segment-3"
        )
        with self.assertRaises(ContractError):
            self.plan()

    def test_duplicate_decision_rejected(self):
        self.change_spec(1, segment_id="segment-0")
        with self.assertRaises(ContractError):
            self.plan()

    def test_hash_drift_rejected(self):
        value = self.read(self.timebase)
        value["duration_ms"] += 1
        atomic_write_json(value, self.timebase)
        with self.assertRaises(ContractError):
            self.plan()

    def test_source_hash_drift_rejected(self):
        (self.root / "synthetic-transcript.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(ContractError):
            self.plan()

    def test_identity_drift_rejected_even_with_new_contract_hash(self):
        self.change_timebase(lambda v: v.update(source_identity_sha256="0" * 64))
        with self.assertRaises(ContractError):
            self.plan()

    def test_unknown_version_degraded_and_offset_rejected(self):
        for edit in (
            lambda v: v.update(contract_version="2.0.0"),
            lambda v: v.update(status="degraded_draft"),
            lambda v: v["timestamp_basis"].update(adapter_offset_applied_ms=1000),
        ):
            with self.subTest(edit=edit):
                original = self.read(self.timebase)
                self.change_timebase(edit)
                with self.assertRaises(ContractError):
                    self.plan()
                atomic_write_json(original, self.timebase)

    def test_external_path_and_escape_rejected(self):
        self.change_timebase(
            lambda v: v["artifacts"]["media"].update(path="../other.txt")
        )
        with self.assertRaises(ContractError):
            self.plan()

    def test_output_conflict_and_double_write(self):
        plan = self.plan()
        path = self.root / "plan.json"
        self.assertEqual(
            write_editorial_artifact(plan, path), write_editorial_artifact(plan, path)
        )
        self.change_spec(1, confidence=0.8)
        with self.assertRaises(ContractError):
            write_editorial_artifact(self.plan(), path)

    def test_projection_tampering_rejected(self):
        plan = self.plan()
        plan["segments"][0]["source_interval_ms"]["start"] = 100
        plan["artifact_sha256"] = payload_sha256(plan, "artifact_sha256")
        with self.assertRaises(ContractError):
            validate_editorial_artifact(plan)

    def test_approval_invalidates_after_spec_change(self):
        path = write_editorial_artifact(self.plan(), self.root / "plan.json")
        approval = self.approval(path)
        self.change_spec(1, confidence=0.8)
        with self.assertRaises(ContractError):
            editorial_review_handoff(path, approval)

    def routing(self):
        path = write_editorial_artifact(self.plan(), self.root / "plan.json")
        packaging = build_editorial_packaging_projection(path)
        kinds = [
            "evidence_document",
            "concept_explanation",
            "process_sequence",
            "speaker_connection",
            "real_world_action",
        ]
        for index in (3, 4):
            packaging["scenes"][index]["material_status"] = (
                "authorized_synthetic_local_candidate"
            )
        packaging_path = atomic_write_json(packaging, self.root / "packaging.json")
        spec = {
            "schema": "video_creation_pipeline.content_visual_routing_spec.v1",
            "scenes": [
                {
                    "scene_id": f"segment-{i}",
                    "content_kind": kind,
                    "communication_goal": "Explain synthetic evidence",
                    "generation_allowed": False,
                }
                for i, kind in enumerate(kinds)
            ],
        }
        spec_path = atomic_write_json(spec, self.root / "routing-spec.json")
        style_path = atomic_write_json(
            build_visual_style_profile("editorial_clean"), self.root / "style.json"
        )
        routing_path = atomic_write_json(
            build_content_visual_routing_plan(packaging_path, spec_path, style_path),
            self.root / "routing.json",
        )
        beats = {
            "schema": "video_creation_pipeline.editorial_visual_spec.v1",
            "scenes": [
                {
                    "scene_id": f"segment-{i}",
                    "visual_role": role,
                    "semantic_trigger": "Source statement",
                    "information_advance": "Show the evidence",
                    "safe_zone": "content_safe_area",
                    "motion_intensity": "low",
                    "sfx_role": "none",
                    "source_or_evidence_refs": [f"semantic:segment-{i}"],
                    "visual_motif": "source-focus",
                }
                for i, role in enumerate(
                    [
                        "source_evidence",
                        "knowledge_explanation",
                        "knowledge_explanation",
                        "human_trust",
                        "screen_demo",
                    ]
                )
            ],
        }
        return routing_path, atomic_write_json(beats, self.root / "beat-spec.json")

    def profile(self):
        return {
            "schema": "video_creation_pipeline.screen_recording_profile.v1",
            "privacy_preflight": {
                "notifications_hidden": True,
                "address_bar_hidden": True,
                "bookmarks_hidden": True,
                "sensitive_content_checked": True,
            },
            "speaker_notes_visible": False,
            "safe_zone_ids": ["content_safe_area"],
            "focus_events": [
                {
                    "start_ms": 8000,
                    "end_ms": 9500,
                    "target_ref": "semantic:segment-4",
                    "safe_zone_id": "content_safe_area",
                }
            ],
            "output": {"width": 720, "height": 1280, "fps": 30},
        }

    def test_real_existing_visual_routing_projection(self):
        routing, spec = self.routing()
        profile = atomic_write_json(self.profile(), self.root / "screen.json")
        beats = build_editorial_visual_beats(routing, spec, profile)
        validate_editorial_artifact(beats)
        self.assertEqual(beats, build_editorial_visual_beats(routing, spec, profile))
        self.assertEqual(beats["beats"][0]["renderer"], "hyperframes")
        self.assertEqual(beats["beats"][1]["renderer"], "remotion")
        self.assertEqual(beats["beats"][4]["renderer"], "existing_single_ffmpeg_outlet")
        self.assertEqual(beats["beats"][4]["start_s"], 8)

    def test_decorative_and_missing_screen_are_review_only(self):
        routing, spec = self.routing()
        value = self.read(spec)
        value["scenes"][0]["information_advance"] = ""
        atomic_write_json(value, spec)
        beats = build_editorial_visual_beats(routing, spec)
        self.assertEqual(beats["beats"][0]["status"], "review_required")
        self.assertEqual(beats["beats"][4]["status"], "review_required")

    def test_screen_privacy_and_notes_rejected(self):
        for field in (
            "notifications_hidden",
            "address_bar_hidden",
            "bookmarks_hidden",
            "sensitive_content_checked",
        ):
            profile = self.profile()
            profile["privacy_preflight"][field] = False
            with self.assertRaises(ContractError):
                validate_screen_recording_profile(profile)
        profile = self.profile()
        profile["speaker_notes_visible"] = True
        with self.assertRaises(ContractError):
            validate_screen_recording_profile(profile)

    def test_screen_invalid_focus_rejected(self):
        profile = self.profile()
        profile["focus_events"][0]["safe_zone_id"] = "unknown"
        with self.assertRaises(ContractError):
            validate_screen_recording_profile(profile)

    def test_visual_evidence_and_style_drift_rejected(self):
        routing, spec = self.routing()
        value = self.read(spec)
        value["scenes"][0]["source_or_evidence_refs"] = ["invented"]
        atomic_write_json(value, spec)
        with self.assertRaises(ContractError):
            build_editorial_visual_beats(routing, spec)

    def test_feedback_requires_exact_human_approval(self):
        plan = self.plan()
        path = write_editorial_artifact(plan, self.root / "plan.json")
        feedback = {
            "schema": "video_creation_pipeline.editorial_feedback_spec.v1",
            "plan_sha256": plan["artifact_sha256"],
            "content_type": "synthetic_tutorial",
            "decisions": [
                {
                    "segment_id": "segment-1",
                    "outcome": "edit",
                    "final_choice": "keep",
                    "reason": "Retain useful context",
                    "amount_changed": 0.1,
                    "time_saved_seconds": 30,
                    "rework_seconds": 5,
                    "marginal_return_stop": True,
                    "explicit_user_pin": False,
                }
            ],
        }
        feedback_path = atomic_write_json(feedback, self.root / "feedback.json")
        with self.assertRaises(ContractError):
            build_editorial_feedback(path, feedback_path, self.approval(path))
        receipt = build_editorial_feedback(
            path, feedback_path, self.approval(feedback_path)
        )
        validate_editorial_artifact(receipt)
        self.assertFalse(receipt["preference_candidates"]["candidates"][0]["eligible"])
        self.assertFalse(receipt["rules_written"])
        self.assertEqual(receipt["decisions"][0]["ai_recommendation"]["action"], "keep")

    def test_cli_plan_validate_and_doctor(self):
        path = self.root / "cli-plan.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["doctor"]), 0)
            self.assertEqual(
                main(
                    [
                        "plan",
                        "--timebase",
                        str(self.timebase),
                        "--bundle-root",
                        str(self.root),
                        "--spec",
                        str(self.spec),
                        "--output",
                        str(path),
                    ]
                ),
                0,
            )
            self.assertEqual(main(["validate", str(path)]), 0)
            self.assertEqual(main(["validate", str(self.root / "missing.json")]), 2)

    def test_selected_example_cannot_be_deleted(self):
        self.change_spec(
            1, action="select_best_example", selected_segment_id="segment-3"
        )
        self.change_spec(3, action="delete")
        with self.assertRaises(ContractError):
            self.plan()

    def test_screen_focus_requires_bound_evidence(self):
        routing, spec = self.routing()
        profile = self.profile()
        profile["focus_events"][0]["target_ref"] = "invented:button"
        path = atomic_write_json(profile, self.root / "screen.json")
        with self.assertRaises(ContractError):
            build_editorial_visual_beats(routing, spec, path)

    def test_vkp_unsafe_boundary_rejected(self):
        self.change_timebase(lambda v: v["operator_boundary"].update(uploaded=True))
        with self.assertRaises(ContractError):
            self.plan()

    def test_vkp_overlapping_segments_rejected(self):
        self.change_timebase(lambda v: v["semantic_segments"][1].update(start_ms=1))
        with self.assertRaises(ContractError):
            self.plan()

    def test_vkp_null_required_ref_rejected(self):
        self.change_timebase(lambda v: v["artifacts"].update(media=None))
        with self.assertRaises(ContractError):
            self.plan()

    def test_nested_editorial_staleness_rejected_by_visual_beats(self):
        routing, spec = self.routing()
        self.change_spec(0, confidence=0.8)
        with self.assertRaises(ContractError):
            build_editorial_visual_beats(routing, spec)

    def test_feedback_duplicate_receipt_does_not_fake_three_videos(self):
        plan = self.plan()
        path = write_editorial_artifact(plan, self.root / "plan.json")
        spec = {
            "schema": "video_creation_pipeline.editorial_feedback_spec.v1",
            "plan_sha256": plan["artifact_sha256"],
            "content_type": "tutorial",
            "decisions": [
                {
                    "segment_id": "segment-1",
                    "outcome": "accept",
                    "final_choice": "keep",
                    "reason": "Keep context",
                    "amount_changed": 0,
                    "time_saved_seconds": 10,
                    "rework_seconds": 0,
                    "marginal_return_stop": True,
                    "explicit_user_pin": False,
                }
            ],
        }
        spec_path = atomic_write_json(spec, self.root / "feedback.json")
        receipt = build_editorial_feedback(path, spec_path, self.approval(spec_path))
        receipt_path = write_editorial_artifact(
            receipt, self.root / "feedback-receipt.json"
        )
        result = aggregate_editorial_preferences([receipt_path] * 3)
        self.assertEqual(result["candidates"][0]["evidence_video_count"], 1)
        self.assertFalse(result["candidates"][0]["eligible"])


if __name__ == "__main__":
    unittest.main()
