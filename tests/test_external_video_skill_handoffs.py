from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import atomic_write_json
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.generation_contracts import (
    build_shot_generation_unit,
    write_generation_contract,
)
from video_creation_pipeline.seedance_prompt_handoff import (
    SEEDANCE_PROMPT_SKILL_COMMIT,
    build_seedance_prompt_handoff,
    seedance_prompt_doctor,
    validate_seedance_prompt_handoff,
    write_seedance_prompt_handoff,
)
from video_creation_pipeline.video_use_review_handoff import (
    VIDEO_USE_COMMIT,
    build_video_use_review_handoff,
    validate_video_use_review_handoff,
    video_use_review_doctor,
    write_video_use_review_handoff,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class ExternalVideoSkillHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"external-skill-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)
        self.edit_job = self.root / "edit-job.json"
        write_edit_job(build_edit_job(self.bundle), self.edit_job)
        self.shot_unit = self.root / "shot-unit.json"
        unit = build_shot_generation_unit(
            self.edit_job,
            "storyboard-0001",
            generator="manual",
            visual_prompt="一位讲解者指向一份简洁的保险保障时间线。",
        )
        write_generation_contract(unit, self.shot_unit)
        self.seedance = self.root / "seedance"
        skill = self.seedance / ".claude" / "skills" / "seedance" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("reviewed Seedance fixture", encoding="utf-8")
        self.video_use = self.root / "video-use"
        (self.video_use / "helpers").mkdir(parents=True)
        (self.video_use / "SKILL.md").write_text(
            "reviewed video-use fixture", encoding="utf-8"
        )
        (self.video_use / "helpers" / "timeline_view.py").write_text(
            "reviewed video-use helper", encoding="utf-8"
        )
        self.packaging = self.root / "packaging.json"
        atomic_write_json(
            {
                "schema": "video_creation_pipeline.video_packaging_plan.v1",
                "scenes": [
                    {"scene_id": "one", "start_s": 0.0, "end_s": 4.0},
                    {"scene_id": "two", "start_s": 4.0, "end_s": 7.0},
                ],
            },
            self.packaging,
        )
        self.candidate = self.root / "candidate.mp4"
        self.candidate.write_bytes(b"synthetic candidate")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @patch("video_creation_pipeline.seedance_prompt_handoff._git_head")
    def test_seedance_handoff_is_hash_bound_and_route_unconfigured(
        self, git_head
    ) -> None:
        git_head.return_value = SEEDANCE_PROMPT_SKILL_COMMIT
        first = build_seedance_prompt_handoff(
            self.seedance, self.shot_unit, segment_limit_s=3
        )
        second = build_seedance_prompt_handoff(
            self.seedance, self.shot_unit, segment_limit_s=3
        )
        self.assertEqual(first["handoff_sha256"], second["handoff_sha256"])
        self.assertGreater(len(first["prompt_contract"]["timeline_segments"]), 1)
        self.assertEqual(first["execution_route"]["status"], "route_unconfigured")
        self.assertFalse(first["authority_boundary"]["upload_performed"])
        output = write_seedance_prompt_handoff(
            first, self.root / "seedance-handoff.json"
        )
        self.assertTrue(output.is_file())

    @patch("video_creation_pipeline.seedance_prompt_handoff._git_head")
    def test_seedance_handoff_rejects_stale_unit_and_source(self, git_head) -> None:
        git_head.return_value = SEEDANCE_PROMPT_SKILL_COMMIT
        handoff = build_seedance_prompt_handoff(self.seedance, self.shot_unit)
        handoff["source_shot_unit_sha256"] = "0" * 64
        from video_creation_pipeline.contract_io import payload_sha256

        handoff["handoff_sha256"] = payload_sha256(handoff, "handoff_sha256")
        with self.assertRaisesRegex(ContractError, "shot unit drifted"):
            validate_seedance_prompt_handoff(handoff)
        git_head.return_value = "0" * 40
        with self.assertRaisesRegex(ContractError, "commit drifted"):
            build_seedance_prompt_handoff(self.seedance, self.shot_unit)

    def test_seedance_doctor_needs_explicit_source(self) -> None:
        doctor = seedance_prompt_doctor()
        self.assertEqual(doctor["status"], "not_supplied")
        self.assertFalse(doctor["provider_execution_performed"])

    @patch("video_creation_pipeline.video_use_review_handoff._git_head")
    def test_video_use_review_handoff_covers_cuts_and_is_read_only(
        self, git_head
    ) -> None:
        git_head.return_value = VIDEO_USE_COMMIT
        handoff = build_video_use_review_handoff(
            self.video_use, self.candidate, self.packaging
        )
        kinds = [window["kind"] for window in handoff["review_windows"]]
        self.assertEqual(kinds, ["opening", "cut:one->two", "ending"])
        self.assertFalse(handoff["execution_constraints"]["render_execution_allowed"])
        output = write_video_use_review_handoff(
            handoff, self.root / "video-use-review.json"
        )
        self.assertTrue(output.is_file())

    @patch("video_creation_pipeline.video_use_review_handoff._git_head")
    def test_video_use_review_handoff_rejects_packaging_drift_and_bad_source(
        self, git_head
    ) -> None:
        git_head.return_value = VIDEO_USE_COMMIT
        handoff = build_video_use_review_handoff(
            self.video_use, self.candidate, self.packaging
        )
        packaging = json.loads(self.packaging.read_text(encoding="utf-8"))
        packaging["scenes"][1]["start_s"] = 3.5
        self.packaging.write_text(json.dumps(packaging), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_video_use_review_handoff(handoff)
        git_head.return_value = "0" * 40
        with self.assertRaisesRegex(ContractError, "commit drifted"):
            build_video_use_review_handoff(
                self.video_use, self.candidate, self.packaging
            )

    def test_video_use_doctor_needs_explicit_source(self) -> None:
        doctor = video_use_review_doctor()
        self.assertEqual(doctor["status"], "not_supplied")
        self.assertFalse(doctor["ffmpeg_execution_performed"])


if __name__ == "__main__":
    unittest.main()
