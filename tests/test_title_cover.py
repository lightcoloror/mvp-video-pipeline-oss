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
FIXTURES = ROOT / "tests" / "fixtures" / "content-video-brief-v1" / "upstream-canonical"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.content_video_pipeline import (
    build_content_video_project_plan,
    validate_content_video_inputs,
)
from video_creation_pipeline.contract_io import atomic_write_json, payload_sha256
from video_creation_pipeline.creative_alignment import (
    build_creative_alignment_plan,
    validate_creative_alignment_plan,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.insurance_content_gate import (
    default_insurance_policy_path,
    run_insurance_content_scan,
    write_insurance_content_scan_request,
)
from video_creation_pipeline.remotion_project import (
    REMOTION_RUNTIME_COMMIT,
    materialize_remotion_project,
    run_remotion_cover_candidates,
    validate_remotion_cover_run_receipt,
)
from video_creation_pipeline.remotion_skill_handoff import (
    REMOTION_SKILLS_COMMIT,
    build_remotion_skill_handoff,
    write_remotion_skill_handoff,
)
from video_creation_pipeline.title_cover import (
    build_creative_alignment_spec_from_title_cover_plan,
    build_title_cover_candidate_plan,
    materialize_title_cover_insurance_request,
    validate_title_cover_candidate_plan,
    write_title_cover_candidate_plan,
)
from video_creation_pipeline.title_cover_cli import main as title_cover_cli_main


class TitleCoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"title-cover-{uuid4().hex}"
        self.root.mkdir(parents=True)
        inputs = validate_content_video_inputs(
            FIXTURES / "content-video-brief.v1.json",
            FIXTURES / "content-video-brief.v1.schema.json",
            FIXTURES / "research-output.v1.json",
            FIXTURES / "research-output.v1.schema.json",
            max_age_hours=1_000_000,
        )
        self.project = build_content_video_project_plan(inputs)
        self.project_path = atomic_write_json(
            self.project, self.root / "project-plan.json"
        )
        self.spec_path = atomic_write_json(self._spec(), self.root / "spec.json")
        self.storyboard = self.root / "storyboard.json"
        self.storyboard.write_text('{"shots": []}\n', encoding="utf-8")
        self.voice = self.root / "temporary.wav"
        self.voice.write_bytes(b"temporary voice")
        self.cover = self.root / "cover.svg"
        self.cover.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"/>',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_three_distinct_candidates_are_hash_stable_and_source_bound(self) -> None:
        first = build_title_cover_candidate_plan(self.project_path, self.spec_path)
        second = build_title_cover_candidate_plan(self.project_path, self.spec_path)

        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(
            [row["strategy"] for row in first["candidates"]],
            ["searchable", "curiosity", "evidence_led"],
        )
        self.assertEqual(first["selection"]["status"], "working_candidate")
        self.assertFalse(first["selection"]["creative_approval_claimed"])
        self.assertEqual(first["process_policy"]["renderer"], "existing_remotion_still")
        self.assertTrue(
            all(
                row["render_readiness"]["render_eligible"]
                for row in first["candidates"]
            )
        )

    def test_duplicate_title_unbound_evidence_and_repeated_overlay_fail_closed(
        self,
    ) -> None:
        duplicate = self._spec()
        duplicate["candidates"][1]["title"] = duplicate["candidates"][0]["title"]
        path = atomic_write_json(duplicate, self.root / "duplicate.json")
        with self.assertRaisesRegex(ContractError, "ids and titles must be unique"):
            build_title_cover_candidate_plan(self.project_path, path)

        unbound = self._spec()
        unbound["candidates"][0]["opening_payoff_binding"]["evidence_refs"] = [
            {"ref_id": "invented", "ref_type": "claim"}
        ]
        path = atomic_write_json(unbound, self.root / "unbound.json")
        with self.assertRaisesRegex(ContractError, "outside its opening scene"):
            build_title_cover_candidate_plan(self.project_path, path)

        repeated = self._spec()
        repeated["candidates"][0]["cover_hypothesis"]["overlay_text"] = repeated[
            "candidates"
        ][0]["title"]
        path = atomic_write_json(repeated, self.root / "repeated.json")
        with self.assertRaisesRegex(ContractError, "must be complementary"):
            build_title_cover_candidate_plan(self.project_path, path)

    def test_selected_candidate_projects_into_existing_creative_alignment(self) -> None:
        plan = build_title_cover_candidate_plan(self.project_path, self.spec_path)
        plan_path = write_title_cover_candidate_plan(
            plan, self.root / "title-cover-plan.json"
        )
        projection_path = atomic_write_json(
            {
                "schema": "video_creation_pipeline.title_cover_alignment_projection_spec.v1",
                "production_mode": "ai_assisted",
                "reference_assets": [
                    {
                        "role": "storyboard_candidate",
                        "path": str(self.storyboard),
                        "scene_ids": ["scene-01"],
                    },
                    {
                        "role": "temporary_voice",
                        "path": str(self.voice),
                        "scene_ids": ["scene-01"],
                    },
                ],
            },
            self.root / "projection.json",
        )
        alignment_spec = build_creative_alignment_spec_from_title_cover_plan(
            plan_path, projection_path
        )
        alignment_spec_path = atomic_write_json(
            alignment_spec, self.root / "alignment-spec.json"
        )
        alignment = build_creative_alignment_plan(
            self.project_path, self.cover, alignment_spec_path
        )
        validate_creative_alignment_plan(alignment)

        self.assertNotEqual(
            alignment["premise"]["selected_title"], self.project["title"]
        )
        self.assertEqual(
            alignment["premise"]["title_cover_candidate_id"],
            "candidate-curiosity",
        )
        self.assertIn("source_title_cover_plan", alignment)

    def test_source_drift_and_recomputed_tampering_are_rejected(self) -> None:
        plan = build_title_cover_candidate_plan(self.project_path, self.spec_path)
        plan["process_policy"]["ctr_only_optimization_forbidden"] = False
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "schema validation failed"):
            validate_title_cover_candidate_plan(plan)

        original = build_title_cover_candidate_plan(self.project_path, self.spec_path)
        spec = self._spec()
        spec["working_candidate_id"] = "candidate-searchable"
        atomic_write_json(spec, self.spec_path)
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_title_cover_candidate_plan(original)

    def test_insurance_scan_consumes_all_titles_and_cover_text(self) -> None:
        spec = self._spec()
        spec["candidates"][2]["cover_hypothesis"]["overlay_text"] = "全网第一方案"
        atomic_write_json(spec, self.spec_path)
        plan = build_title_cover_candidate_plan(self.project_path, self.spec_path)
        plan_path = write_title_cover_candidate_plan(
            plan, self.root / "title-cover-plan.json"
        )
        request, artifacts = materialize_title_cover_insurance_request(
            plan_path,
            default_insurance_policy_path(),
            self.root / "insurance-inputs",
            created_at="2026-08-21T10:00:00+08:00",
            distribution_channel="short_video",
        )
        request_path = write_insurance_content_scan_request(
            request, self.root / "insurance-request.json"
        )
        report = run_insurance_content_scan(
            request_path, self.root / "insurance-report.json"
        )

        self.assertTrue(artifacts["titles"].is_file())
        self.assertEqual(report["readiness"]["status"], "needs_compliance_review")
        self.assertFalse(report["readiness"]["publish_authorized"])

    def test_cli_build_validate_and_project(self) -> None:
        plan_path = self.root / "cli-plan.json"
        self.assertEqual(
            title_cover_cli_main(
                [
                    "build",
                    "--project-plan",
                    str(self.project_path),
                    "--spec",
                    str(self.spec_path),
                    "--output",
                    str(plan_path),
                ]
            ),
            0,
        )
        self.assertEqual(
            title_cover_cli_main(["validate", "--plan", str(plan_path)]), 0
        )

    def test_existing_remotion_project_registers_hash_bound_still_variants(
        self,
    ) -> None:
        plan = build_title_cover_candidate_plan(self.project_path, self.spec_path)
        plan_path = write_title_cover_candidate_plan(
            plan, self.root / "title-cover-plan.json"
        )
        packaging_path = atomic_write_json(
            self.project["packaging_plan"], self.root / "packaging.json"
        )
        skills, runtime = self._remotion_sources()
        with patch(
            "video_creation_pipeline.remotion_skill_handoff._git_head",
            return_value=REMOTION_SKILLS_COMMIT,
        ):
            handoff = build_remotion_skill_handoff(
                skills,
                packaging_path,
                composition_id="MainCandidate",
                title_cover_plan_path=plan_path,
            )
            handoff_path = write_remotion_skill_handoff(
                handoff, self.root / "remotion-handoff.json"
            )
        with (
            patch(
                "video_creation_pipeline.remotion_skill_handoff._git_head",
                return_value=REMOTION_SKILLS_COMMIT,
            ),
            patch(
                "video_creation_pipeline.remotion_project._git_head",
                return_value=REMOTION_RUNTIME_COMMIT,
            ),
        ):
            paths = materialize_remotion_project(
                handoff_path,
                runtime,
                self.root / "remotion-project",
                license_basis="individual",
            )
        data = json.loads(paths["data"].read_text(encoding="utf-8"))
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        root_source = (paths["project"] / "src" / "Root.tsx").read_text(
            encoding="utf-8"
        )
        cover_source = (paths["project"] / "src" / "Cover.tsx").read_text(
            encoding="utf-8"
        )

        self.assertEqual(len(data["titleCoverVariants"]), 9)
        self.assertTrue(manifest["capabilities"]["title_cover_still_compositions"])
        self.assertIn("<Still", root_source)
        self.assertIn("CANDIDATE ONLY", cover_source)
        self.assertTrue(all(row["candidateOnly"] for row in data["titleCoverVariants"]))
        self.assertTrue(
            all("_" not in row["compositionId"] for row in data["titleCoverVariants"])
        )

        self._write_installed_remotion_runtime(paths["project"])
        browser = self.root / "chrome.exe"
        browser.write_bytes(b"synthetic browser")
        dimensions = {
            row["compositionId"]: (row["width"], row["height"])
            for row in data["titleCoverVariants"]
        }

        def fake_run(_cli, args, _cwd, _timeout):
            if args[0] == "still":
                Path(args[3]).write_bytes(b"synthetic png")
            return {"returncode": 0, "output": f"synthetic {args[0]}"}

        def fake_probe(path):
            composition_id = Path(path).stem
            width, height = dimensions[composition_id]
            return {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "png",
                        "width": width,
                        "height": height,
                    }
                ],
                "format": {},
            }

        with (
            patch(
                "video_creation_pipeline.remotion_skill_handoff._git_head",
                return_value=REMOTION_SKILLS_COMMIT,
            ),
            patch(
                "video_creation_pipeline.remotion_project._git_head",
                return_value=REMOTION_RUNTIME_COMMIT,
            ),
            patch(
                "video_creation_pipeline.remotion_project._run_remotion",
                side_effect=fake_run,
            ),
            patch(
                "video_creation_pipeline.remotion_project.probe_media_metadata",
                side_effect=fake_probe,
            ),
        ):
            receipt = run_remotion_cover_candidates(
                paths["manifest"], runtime, browser_path=browser
            )
            validate_remotion_cover_run_receipt(receipt)
        self.assertEqual(len(receipt["outputs"]), 9)
        self.assertTrue(receipt["readiness"]["automatic_technical_qa_passed"])
        self.assertFalse(receipt["readiness"]["publish_authorized"])

    def test_dynamic_formula_routing_selects_highest_scoring_three(self) -> None:
        spec = self._dynamic_spec()
        path = atomic_write_json(spec, self.root / "dynamic-spec.json")
        plan = build_title_cover_candidate_plan(self.project_path, path)

        self.assertEqual(
            [row["strategy"] for row in plan["candidates"]],
            ["story_scene", "evidence_focus", "numeric_contrast"],
        )
        self.assertEqual(
            [row["routing_assessment"]["selected_rank"] for row in plan["candidates"]],
            [1, 2, 3],
        )
        self.assertEqual(
            plan["routing_selection"]["selected_candidate_ids"],
            ["dynamic-story", "dynamic-evidence", "dynamic-number"],
        )
        self.assertEqual(
            plan["routing_selection"]["diversity_policy"], "tie_break_only"
        )
        self.assertFalse(plan["routing_selection"]["ctr_prediction_claimed"])
        self.assertEqual(
            plan["process_policy"]["selection_method"],
            "weighted_content_formula_routing",
        )

    def test_dynamic_formula_routing_allows_two_candidates(self) -> None:
        spec = self._dynamic_spec()
        spec["candidate_limit"] = 2
        spec["working_candidate_id"] = "dynamic-evidence"
        path = atomic_write_json(spec, self.root / "dynamic-two.json")
        plan = build_title_cover_candidate_plan(self.project_path, path)

        self.assertEqual(len(plan["candidates"]), 2)
        self.assertEqual(
            plan["routing_selection"]["selected_candidate_ids"],
            ["dynamic-story", "dynamic-evidence"],
        )

    def test_dynamic_formula_routing_fails_closed_on_hard_gate(self) -> None:
        spec = self._dynamic_spec()
        spec["candidates"][0]["routing_assessment"]["hard_gates"]["privacy_cleared"] = (
            False
        )
        path = atomic_write_json(spec, self.root / "dynamic-gate-fail.json")
        with self.assertRaisesRegex(ContractError, "failed hard gate: privacy_cleared"):
            build_title_cover_candidate_plan(self.project_path, path)

    def test_publication_title_profiles_enforce_hard_limits_and_warn_on_range(
        self,
    ) -> None:
        spec = self._dynamic_spec()
        spec["candidates"][0]["platform_titles"] = {
            "douyin": "被遗忘的复查",
            "xiaohongshu": "被遗忘的复查",
            "wechat_channels": "客户忘了复查，我怎样把这件事继续记着",
            "bilibili": "客户忘了复查，我怎样把这件事继续记着｜真实案例",
        }
        path = atomic_write_json(spec, self.root / "platform-title-spec.json")
        plan = build_title_cover_candidate_plan(self.project_path, path)

        self.assertEqual(
            plan["publication_title_profiles"]["xiaohongshu"]["hard_max_chars"],
            20,
        )
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["title_route"], "browse_conflict")
        self.assertEqual(
            candidate["publication_title_assessments"]["xiaohongshu"]["quality_status"],
            "usable_needs_length_review",
        )
        self.assertTrue(
            candidate["publication_title_assessments"]["bilibili"]["within_hard_limit"]
        )

    def test_publication_title_hard_limits_fail_closed(self) -> None:
        limits = {
            "douyin": 30,
            "xiaohongshu": 20,
            "wechat_channels": 64,
            "bilibili": 80,
        }
        for index, (platform, limit) in enumerate(limits.items()):
            with self.subTest(platform=platform):
                spec = self._dynamic_spec()
                spec["candidates"][0]["platform_titles"] = {
                    platform: "测" * (limit + 1)
                }
                path = atomic_write_json(
                    spec, self.root / f"title-too-long-{index}.json"
                )
                with self.assertRaisesRegex(ContractError, "is too long"):
                    build_title_cover_candidate_plan(self.project_path, path)

    def test_publication_title_route_is_required_when_platforms_are_selected(
        self,
    ) -> None:
        spec = self._dynamic_spec()
        del spec["candidates"][0]["title_route"]
        path = atomic_write_json(spec, self.root / "missing-title-route.json")
        with self.assertRaisesRegex(ContractError, "publication title route"):
            build_title_cover_candidate_plan(self.project_path, path)

    def test_legacy_v1_without_publication_profiles_remains_valid(self) -> None:
        plan = build_title_cover_candidate_plan(self.project_path, self.spec_path)
        plan.pop("publication_title_profiles")
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
        validate_title_cover_candidate_plan(plan)

    def test_selected_platforms_cannot_drop_the_profile_binding(self) -> None:
        path = atomic_write_json(self._dynamic_spec(), self.root / "platform-spec.json")
        plan = build_title_cover_candidate_plan(self.project_path, path)
        plan.pop("publication_title_profiles")
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
        with self.assertRaises(ContractError):
            validate_title_cover_candidate_plan(plan)

    def test_platform_specific_titles_enter_insurance_scan(self) -> None:
        spec = self._dynamic_spec()
        spec["candidates"][0]["platform_titles"] = {"douyin": "全网第一方案"}
        path = atomic_write_json(spec, self.root / "platform-spec.json")
        plan = build_title_cover_candidate_plan(self.project_path, path)
        plan_path = write_title_cover_candidate_plan(plan, self.root / "plan.json")
        request, artifacts = materialize_title_cover_insurance_request(
            plan_path,
            default_insurance_policy_path(),
            self.root / "scan-inputs",
            created_at="2026-08-21T10:00:00+08:00",
            distribution_channel="short_video",
        )
        self.assertIn("全网第一方案", artifacts["titles"].read_text(encoding="utf-8"))
        request_path = write_insurance_content_scan_request(
            request, self.root / "request.json"
        )
        report = run_insurance_content_scan(request_path, self.root / "scan.json")
        self.assertEqual(report["readiness"]["status"], "needs_compliance_review")

    def _spec(self) -> dict:
        evidence_refs = copy.deepcopy(
            self.project["packaging_plan"]["scenes"][0]["evidence_refs"]
        )
        rows = [
            (
                "candidate-searchable",
                "searchable",
                "离线视频生产如何保留证据链",
                "每个结论都有来源",
            ),
            (
                "candidate-curiosity",
                "curiosity",
                "这条视频为什么不让AI自己补画面",
                "AI不能替你补证据",
            ),
            (
                "candidate-evidence",
                "evidence_led",
                "从3类证据到1条候选视频",
                "3类证据怎样进视频",
            ),
        ]
        candidates = []
        for candidate_id, strategy, title, overlay in rows:
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "strategy": strategy,
                    "title": title,
                    "cover_hypothesis": {
                        "visual_focus": "证据关系图和清晰的人物焦点",
                        "audience_promise": "说明结论如何绑定到来源",
                        "click_reason": "观众能看到一条可核验的制作链",
                        "overlay_text": overlay,
                    },
                    "opening_payoff_binding": {
                        "scene_id": "scene-01",
                        "payoff_summary": "开头直接说明只使用已审核证据",
                        "evidence_refs": copy.deepcopy(evidence_refs),
                    },
                    "cover_source": {
                        "source_kind": "typography_only",
                        "availability": "available",
                        "source_note": "合成测试使用确定性文字封面",
                    },
                }
            )
        return {
            "schema": "video_creation_pipeline.title_cover_candidate_spec.v1",
            "profile_ids": [
                "youtube_16_9",
                "bilibili_4_3",
                "vertical_profile_3_4",
            ],
            "working_candidate_id": "candidate-curiosity",
            "candidates": candidates,
        }

    def _dynamic_spec(self) -> dict:
        evidence_refs = copy.deepcopy(
            self.project["packaging_plan"]["scenes"][0]["evidence_refs"]
        )
        rows = [
            (
                "dynamic-story",
                "story_scene",
                "一次真实提醒为什么改变了后续检查",
                "三个月到了",
                {
                    "content_fit": 5,
                    "visual_asset_strength": 5,
                    "information_gap": 5,
                    "credibility": 4,
                    "title_complementarity": 5,
                    "small_preview_readability": 5,
                    "target_audience_fit": 5,
                },
            ),
            (
                "dynamic-evidence",
                "evidence_focus",
                "复查提醒记录能证明什么",
                "提醒了两三次",
                {
                    "content_fit": 5,
                    "visual_asset_strength": 5,
                    "information_gap": 4,
                    "credibility": 5,
                    "title_complementarity": 4,
                    "small_preview_readability": 5,
                    "target_audience_fit": 4,
                },
            ),
            (
                "dynamic-number",
                "numeric_contrast",
                "准备金额和实际借款为什么不同",
                "22万不等于借22万",
                {
                    "content_fit": 5,
                    "visual_asset_strength": 4,
                    "information_gap": 5,
                    "credibility": 4,
                    "title_complementarity": 4,
                    "small_preview_readability": 5,
                    "target_audience_fit": 4,
                },
            ),
            (
                "dynamic-process",
                "process_sequence",
                "检查流程怎样一步步接上",
                "复查只是第一步",
                {
                    "content_fit": 4,
                    "visual_asset_strength": 4,
                    "information_gap": 3,
                    "credibility": 4,
                    "title_complementarity": 4,
                    "small_preview_readability": 5,
                    "target_audience_fit": 4,
                },
            ),
        ]
        hard_gates = {
            "fact_accurate": True,
            "authorization_confirmed": True,
            "privacy_cleared": True,
            "medical_insurance_boundary_passed": True,
            "opening_payoff_supported": True,
        }
        candidates = []
        for candidate_id, strategy, title, overlay, scores in rows:
            title_routes = {
                "story_scene": "browse_conflict",
                "evidence_focus": "evidence_result",
                "numeric_contrast": "evidence_result",
                "process_sequence": "search_answer",
            }
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "strategy": strategy,
                    "title": title,
                    "title_route": title_routes[strategy],
                    "cover_hypothesis": {
                        "visual_focus": "与当前内容公式匹配的唯一视觉焦点",
                        "audience_promise": "只承诺开头和证据能够兑现的内容",
                        "click_reason": "路由评分用于候选排序而不是预测点击率",
                        "overlay_text": overlay,
                    },
                    "opening_payoff_binding": {
                        "scene_id": "scene-01",
                        "payoff_summary": "开头直接兑现当前封面承诺",
                        "evidence_refs": copy.deepcopy(evidence_refs),
                    },
                    "cover_source": {
                        "source_kind": "typography_only",
                        "availability": "available",
                        "source_note": "合成测试使用确定性文字封面",
                    },
                    "routing_assessment": {
                        "hard_gates": copy.deepcopy(hard_gates),
                        "scores": scores,
                    },
                }
            )
        return {
            "schema": "video_creation_pipeline.title_cover_candidate_spec.v2",
            "profile_ids": ["bilibili_16_9", "vertical_profile_3_4"],
            "publication_platforms": [
                "douyin",
                "xiaohongshu",
                "wechat_channels",
                "bilibili",
            ],
            "candidate_limit": 3,
            "working_candidate_id": "dynamic-story",
            "candidates": candidates,
        }

    def _remotion_sources(self) -> tuple[Path, Path]:
        skills = self.root / "remotion-skills"
        skills.mkdir()
        (skills / "package.json").write_text(
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
            path = skills / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("reviewed fixture", encoding="utf-8")
        runtime = self.root / "remotion-runtime"
        for name, package_name in (("core", "remotion"), ("cli", "@remotion/cli")):
            path = runtime / "packages" / name / "package.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"name": package_name, "version": "4.0.477"}),
                encoding="utf-8",
            )
        (runtime / "LICENSE.md").write_text(
            "synthetic Remotion license fixture", encoding="utf-8"
        )
        return skills, runtime

    def _write_installed_remotion_runtime(self, project: Path) -> None:
        (project / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3}), encoding="utf-8"
        )
        versions = {
            "@remotion/captions": "4.0.477",
            "@remotion/cli": "4.0.477",
            "react": "19.2.3",
            "react-dom": "19.2.3",
            "remotion": "4.0.477",
        }
        for name, version in versions.items():
            path = project / "node_modules" / Path(*name.split("/")) / "package.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"name": name, "version": version}), encoding="utf-8"
            )
        cli = project / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
        cli.write_text("// synthetic CLI", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
