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

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.remotion_skill_handoff import (
    REMOTION_SKILLS_COMMIT,
    VIDEO_SHOTCRAFT_COMMIT,
    build_remotion_skill_handoff,
    remotion_skill_doctor,
    validate_remotion_skill_handoff,
    write_remotion_skill_handoff,
)


class RemotionSkillHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"remotion-skill-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.skills = self.root / "skills"
        self.skills.mkdir()
        (self.skills / "package.json").write_text(
            json.dumps({"name": "@remotion/skills", "version": "4.0.513"}),
            encoding="utf-8",
        )
        for relative in (
            "skills/remotion-best-practices/SKILL.md",
            "skills/remotion-markup/SKILL.md",
            "skills/remotion-interactivity/SKILL.md",
            "skills/remotion-render/SKILL.md",
            "skills/remotion-studio/SKILL.md",
            "skills/remotion-captions/SKILL.md",
        ):
            target = self.skills / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("reviewed skill fixture", encoding="utf-8")
        self.packaging = self.root / "packaging.json"
        self.packaging.write_text("{}", encoding="utf-8")
        self.subtitle = self.root / "subtitle.srt"
        self.subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nfixture\n", encoding="utf-8"
        )
        self.shotcraft = self.root / "video-shotcraft"
        (self.shotcraft / "gallery" / "api").mkdir(parents=True)
        (self.shotcraft / "references" / "shots").mkdir(parents=True)
        (self.shotcraft / "demos").mkdir(parents=True)
        (self.shotcraft / "SKILL.md").write_text(
            "reviewed Video Shotcraft fixture", encoding="utf-8"
        )
        (self.shotcraft / "LICENSE").write_text(
            "Apache License\nVersion 2.0", encoding="utf-8"
        )
        (self.shotcraft / "references" / "shots" / "focus.md").write_text(
            "## 参考实现\n`demos/Focus.tsx`\n", encoding="utf-8"
        )
        (self.shotcraft / "demos" / "Focus.tsx").write_text(
            "export const Focus = () => null;", encoding="utf-8"
        )
        (self.shotcraft / "gallery" / "api" / "library.json").write_text(
            json.dumps(
                {
                    "stats": {"cardCount": 1},
                    "cards": [
                        {
                            "name": "focus",
                            "summary": "focus fixture",
                            "source": "references/shots/focus.md",
                            "category": "camera",
                            "styles": [{"key": "focus-in"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @patch("video_creation_pipeline.remotion_skill_handoff._git_head")
    def test_build_is_hash_stable_and_candidate_only(self, git_head_mock) -> None:
        git_head_mock.return_value = REMOTION_SKILLS_COMMIT
        first = build_remotion_skill_handoff(
            self.skills,
            self.packaging,
            composition_id="main",
            subtitle_path=self.subtitle,
        )
        second = build_remotion_skill_handoff(
            self.skills,
            self.packaging,
            composition_id="main",
            subtitle_path=self.subtitle,
        )
        self.assertEqual(first["handoff_sha256"], second["handoff_sha256"])
        self.assertIn("remotion-captions", first["skill_route"])
        self.assertFalse(first["authority_boundary"]["render_execution_performed"])
        path = write_remotion_skill_handoff(first, self.root / "handoff.json")
        self.assertTrue(path.is_file())

    @patch("video_creation_pipeline.remotion_skill_handoff._git_head")
    def test_source_drift_and_missing_captions_route_fail_closed(
        self, git_head_mock
    ) -> None:
        git_head_mock.return_value = REMOTION_SKILLS_COMMIT
        handoff = build_remotion_skill_handoff(
            self.skills,
            self.packaging,
            composition_id="main",
            subtitle_path=self.subtitle,
        )
        handoff["skill_route"].remove("remotion-captions")
        from video_creation_pipeline.contract_io import payload_sha256

        handoff["handoff_sha256"] = payload_sha256(handoff, "handoff_sha256")
        with self.assertRaisesRegex(ContractError, "captions skill"):
            validate_remotion_skill_handoff(handoff)
        git_head_mock.return_value = "0" * 40
        with self.assertRaisesRegex(ContractError, "source commit drifted"):
            build_remotion_skill_handoff(
                self.skills, self.packaging, composition_id="main"
            )

    def test_doctor_without_source_has_no_execution_claim(self) -> None:
        doctor = remotion_skill_doctor()
        self.assertEqual(doctor["status"], "not_supplied")
        self.assertFalse(doctor["skill_execution_performed"])
        self.assertFalse(doctor["render_execution_performed"])

    @patch("video_creation_pipeline.remotion_skill_handoff._git_head")
    def test_shotcraft_recipe_is_source_bound_without_new_runtime(
        self, git_head_mock
    ) -> None:
        git_head_mock.side_effect = lambda root: (
            VIDEO_SHOTCRAFT_COMMIT
            if Path(root).name == "video-shotcraft"
            else REMOTION_SKILLS_COMMIT
        )
        request = [{"name": "focus", "style_key": "focus-in"}]
        first = build_remotion_skill_handoff(
            self.skills,
            self.packaging,
            composition_id="main",
            video_shotcraft_root=self.shotcraft,
            video_shotcraft_recipes=request,
        )
        second = build_remotion_skill_handoff(
            self.skills,
            self.packaging,
            composition_id="main",
            video_shotcraft_root=self.shotcraft,
            video_shotcraft_recipes=request,
        )
        selection = first["shot_recipe_selection"]
        self.assertEqual(first["handoff_sha256"], second["handoff_sha256"])
        self.assertEqual(selection["recipes"][0]["style_key"], "focus-in")
        self.assertTrue(
            selection["execution_policy"]["reuse_existing_remotion_runtime"]
        )
        self.assertTrue(
            selection["execution_policy"]["upstream_audio_direct_copy_forbidden"]
        )

    @patch("video_creation_pipeline.remotion_skill_handoff._git_head")
    def test_shotcraft_unknown_recipe_and_source_drift_fail_closed(
        self, git_head_mock
    ) -> None:
        git_head_mock.side_effect = lambda root: (
            VIDEO_SHOTCRAFT_COMMIT
            if Path(root).name == "video-shotcraft"
            else REMOTION_SKILLS_COMMIT
        )
        with self.assertRaisesRegex(ContractError, "unknown Video Shotcraft recipe"):
            build_remotion_skill_handoff(
                self.skills,
                self.packaging,
                composition_id="main",
                video_shotcraft_root=self.shotcraft,
                video_shotcraft_recipes=[{"name": "missing", "style_key": "x"}],
            )
        git_head_mock.return_value = "0" * 40
        git_head_mock.side_effect = None
        with self.assertRaisesRegex(ContractError, "source commit drifted"):
            build_remotion_skill_handoff(
                self.skills,
                self.packaging,
                composition_id="main",
                video_shotcraft_root=self.shotcraft,
                video_shotcraft_recipes=[{"name": "focus", "style_key": "focus-in"}],
            )


if __name__ == "__main__":
    unittest.main()
