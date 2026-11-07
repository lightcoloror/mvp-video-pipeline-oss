from __future__ import annotations

# ruff: noqa: E402

import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "content-video-brief-v1" / "upstream-canonical"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.content_video_pipeline import (
    build_content_video_project_plan,
    build_plan_dry_run,
    validate_content_video_inputs,
    validate_content_video_project_plan,
    write_content_video_plan_bundle,
)
from video_creation_pipeline.capability_truth import (
    validate_content_synthetic_run_receipt,
)
from video_creation_pipeline.content_video_synthetic import (
    generate_synthetic_candidate,
    validate_content_video_atomic_output_manifest,
)
from video_creation_pipeline.contract_io import (
    artifact_reference,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError


class ContentVideoPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            ROOT
            / ".test-runtime"
            / (f"content-video-pipeline-{self._testMethodName}-{uuid4().hex}")
        )
        self.root.mkdir(parents=True)
        self.brief = FIXTURES / "content-video-brief.v1.json"
        self.brief_schema = FIXTURES / "content-video-brief.v1.schema.json"
        self.research = FIXTURES / "research-output.v1.json"
        self.research_schema = FIXTURES / "research-output.v1.schema.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _case_handoff_fixture(
        self,
        name: str = "case-handoff",
    ) -> tuple[Path, Path, str]:
        root = self.root / name
        root.mkdir(parents=True, exist_ok=True)
        brief = json.loads(self.brief.read_text(encoding="utf-8"))
        research = json.loads(self.research.read_text(encoding="utf-8"))
        manifest_path = root / "case-handoff.json"
        brief_path = root / "content-video-brief.v1.json"
        adapter_version = "1.1.0"
        brief["chapters"][0]["purpose"] = "hook"
        brief["chapters"][0]["title"] = "selected-hook"
        brief["scenes"][0]["purpose"] = "selected-hook"
        input_specs = {
            "case_video_script": (
                "case-video-script",
                "case-script-synthetic-001",
                "case-video-script.v1",
            ),
            "content_item": (
                "content-item",
                brief["content_id"],
                "0.2",
            ),
            "fact_card": (
                "fact-card",
                "fact-card-synthetic-001",
                "content-fact-card.v0",
            ),
            "route_card": (
                "route-card",
                "route-card-synthetic-001",
                "content-route-card.v0",
            ),
            "review_receipt": (
                "case-review",
                "case-review-synthetic-001",
                "case-video-review-receipt.v1",
            ),
        }
        inputs = {
            "research_output": {
                "artifact_id": research["research_id"],
                "schema_version": research["schema_version"],
                "sha256": canonical_json_sha256(research),
            }
        }
        for key, (label, artifact_id, schema_version) in input_specs.items():
            reference = {
                "artifact_id": artifact_id,
                "schema_version": schema_version,
                "sha256": canonical_json_sha256(
                    {"artifact_id": artifact_id, "schema_version": schema_version}
                ),
            }
            inputs[key] = reference
            brief["provenance"]["upstream_input_files"].append(
                {
                    "path": f"artifact://{label}/{artifact_id}",
                    "sha256": reference["sha256"],
                    "status": "present",
                }
            )
        brief["provenance"]["adapter_version"] = f"0.1.0+case-script.{adapter_version}"
        brief["provenance"]["manifest_ref"] = manifest_path.name
        brief_path.write_text(
            json.dumps(brief, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        output = {
            "path": brief_path.name,
            "brief_id": brief["brief_id"],
            "schema_version": brief["schema_version"],
            "sha256": canonical_json_sha256(brief),
        }
        boundaries = {
            "draft_only": True,
            "manual_only": True,
            "candidate_visuals_only": True,
            "external_api_called": False,
            "would_publish": False,
        }
        manifest = {
            "schema_version": "case-video-script-handoff.v1",
            "status": "generated_draft_only",
            "adapter": "case-video-script-sidecar",
            "adapter_version": adapter_version,
            "handoff_id": "case-handoff-synthetic-001",
            "synthetic_only": True,
            "script_id": inputs["case_video_script"]["artifact_id"],
            "content_id": brief["content_id"],
            "brief_id": brief["brief_id"],
            "selected_hook_id": "hook-01",
            "selected_hook_binding": {
                "hook_id": "hook-01",
                "text_sha256": canonical_json_sha256(
                    brief["chapters"][0]["narration_draft"]
                ),
                "evidence_refs_sha256": canonical_json_sha256(
                    brief["chapters"][0]["evidence_refs"]
                ),
            },
            "interview_slots": [
                {
                    "slot_id": "slot-synthetic-001",
                    "state": "planned",
                    "story_segment_ref": "segment-synthetic-001",
                    "fallback_edit": "Use verified synthetic narration only.",
                }
            ],
            "experiment": {
                "variable_to_change": "selected_hook_id",
                "constants": ["body", "duration", "subtitles"],
                "no_signal_meaning": "content_no_signal_is_observation_only",
            },
            "inputs": inputs,
            "output": output,
            "boundaries": boundaries,
        }
        projection = dict(manifest)
        manifest["brief_projection_sha256"] = canonical_json_sha256(projection)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return brief_path, manifest_path, canonical_json_sha256(manifest)

    def test_canonical_fixture_preserves_evidence_and_missing_material(self) -> None:
        plan = build_content_video_project_plan(self._inputs())

        self.assertEqual(plan["readiness"]["status"], "candidate_plan_ready")
        self.assertFalse(plan["readiness"]["production_render_eligible"])
        self.assertTrue(plan["readiness"]["synthetic_fixture_render_eligible"])
        unit = plan["shot_units"][0]
        self.assertEqual(unit["material"]["status"], "missing_material")
        self.assertEqual(
            unit["evidence_refs"],
            json.loads(self.brief.read_text(encoding="utf-8"))["scenes"][0][
                "evidence_refs"
            ],
        )
        self.assertEqual(
            plan["inputs"]["referenced_source_ids"],
            ["source_fixture_primary", "source_fixture_video"],
        )
        self.assertEqual(
            plan["inputs"]["freshness_policy"]["time_sensitivity"],
            "standard",
        )
        self.assertFalse(plan["inputs"]["freshness_policy"]["age_gate_applied"])
        self.assertFalse(plan["authority_boundary"]["production_success_claimed"])

    def test_research_hash_tamper_fails_closed(self) -> None:
        tampered = self.root / "research-output.v1.json"
        payload = json.loads(self.research.read_text(encoding="utf-8"))
        payload["question"] += " changed"
        tampered.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "research input hash is stale"):
            validate_content_video_inputs(
                self.brief,
                self.brief_schema,
                tampered,
                self.research_schema,
                max_age_hours=1_000_000,
            )

    def test_case_handoff_accepts_hash_bound_brief_and_manifest(self) -> None:
        brief, manifest, manifest_sha256 = self._case_handoff_fixture()

        inputs = validate_content_video_inputs(
            brief,
            self.brief_schema,
            self.research,
            self.research_schema,
            max_age_hours=1_000_000,
            case_handoff_manifest_path=manifest,
            case_handoff_manifest_sha256=manifest_sha256,
            require_case_handoff=True,
        )
        plan = build_content_video_project_plan(inputs)

        self.assertEqual(
            inputs["case_handoff_manifest_artifact"]["canonical_sha256"],
            manifest_sha256,
        )
        self.assertEqual(
            plan["inputs"]["case_projection_sha256"],
            inputs["case_projection_sha256"],
        )
        self.assertEqual(
            plan["inputs"]["case_script_id"],
            "case-script-synthetic-001",
        )

    def test_explicit_case_route_blocks_double_marker_downgrade(self) -> None:
        brief_path, _, _ = self._case_handoff_fixture("case-route-downgrade")
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        brief["provenance"]["adapter_version"] = "0.1.0"
        brief["provenance"].pop("manifest_ref", None)
        brief["provenance"]["upstream_input_files"] = [
            row
            for row in brief["provenance"]["upstream_input_files"]
            if not row["path"].startswith("artifact://case-video-script/")
        ]
        brief_path.write_text(
            json.dumps(brief, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ContractError,
            "case handoff is required by the selected consumer route",
        ):
            validate_content_video_inputs(
                brief_path,
                self.brief_schema,
                self.research,
                self.research_schema,
                max_age_hours=1_000_000,
                require_case_handoff=True,
            )

    def test_case_handoff_arguments_are_required_as_a_pair(self) -> None:
        brief, manifest, manifest_sha256 = self._case_handoff_fixture()

        with self.assertRaisesRegex(
            ContractError,
            "case handoff manifest required for case projection",
        ):
            validate_content_video_inputs(
                brief,
                self.brief_schema,
                self.research,
                self.research_schema,
                max_age_hours=1_000_000,
            )
        for manifest_arg, hash_arg in (
            (manifest, None),
            (None, manifest_sha256),
        ):
            with self.subTest(
                manifest_supplied=manifest_arg is not None,
                hash_supplied=hash_arg is not None,
            ):
                with self.assertRaisesRegex(
                    ContractError,
                    "case handoff arguments must be supplied together",
                ):
                    validate_content_video_inputs(
                        brief,
                        self.brief_schema,
                        self.research,
                        self.research_schema,
                        max_age_hours=1_000_000,
                        case_handoff_manifest_path=manifest_arg,
                        case_handoff_manifest_sha256=hash_arg,
                    )

    def test_generic_brief_rejects_case_manifest_but_remains_backward_compatible(
        self,
    ) -> None:
        generic_inputs = self._inputs()
        self.assertNotIn("case_handoff_manifest_artifact", generic_inputs)
        _, manifest, manifest_sha256 = self._case_handoff_fixture()

        with self.assertRaisesRegex(
            ContractError,
            "case handoff manifest supplied for non-case brief",
        ):
            validate_content_video_inputs(
                self.brief,
                self.brief_schema,
                self.research,
                self.research_schema,
                max_age_hours=1_000_000,
                case_handoff_manifest_path=manifest,
                case_handoff_manifest_sha256=manifest_sha256,
            )

    def test_case_handoff_full_brief_hash_catches_narration_and_thesis_tamper(
        self,
    ) -> None:
        for field in ("narration", "thesis"):
            with self.subTest(field=field):
                brief_path, manifest, manifest_sha256 = self._case_handoff_fixture(
                    f"tamper-{field}"
                )
                brief = json.loads(brief_path.read_text(encoding="utf-8"))
                if field == "narration":
                    brief["scenes"][0]["narration_draft"] += " unsupported change"
                else:
                    brief["thesis"] += " unsupported change"
                brief_path.write_text(
                    json.dumps(brief, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ContractError,
                    "case handoff brief hash is stale",
                ):
                    validate_content_video_inputs(
                        brief_path,
                        self.brief_schema,
                        self.research,
                        self.research_schema,
                        max_age_hours=1_000_000,
                        case_handoff_manifest_path=manifest,
                        case_handoff_manifest_sha256=manifest_sha256,
                    )

    def test_case_handoff_manifest_tamper_and_hash_drift_fail_closed(self) -> None:
        for mutation in ("manifest", "expected_hash"):
            with self.subTest(mutation=mutation):
                brief, manifest_path, manifest_sha256 = self._case_handoff_fixture(
                    f"manifest-{mutation}"
                )
                if mutation == "manifest":
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["status"] = "changed"
                    manifest_path.write_text(
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                else:
                    manifest_sha256 = "0" * 64

                with self.assertRaisesRegex(
                    ContractError,
                    "case handoff manifest hash is stale",
                ):
                    validate_content_video_inputs(
                        brief,
                        self.brief_schema,
                        self.research,
                        self.research_schema,
                        max_age_hours=1_000_000,
                        case_handoff_manifest_path=manifest_path,
                        case_handoff_manifest_sha256=manifest_sha256,
                    )

    def test_case_handoff_projection_hash_fails_even_with_recomputed_manifest_hash(
        self,
    ) -> None:
        brief, manifest_path, _ = self._case_handoff_fixture("projection-tamper")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["brief_projection_sha256"] = "0" * 64
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ContractError,
            "case handoff projection hash is stale",
        ):
            validate_content_video_inputs(
                brief,
                self.brief_schema,
                self.research,
                self.research_schema,
                max_age_hours=1_000_000,
                case_handoff_manifest_path=manifest_path,
                case_handoff_manifest_sha256=canonical_json_sha256(manifest),
            )

    def test_case_handoff_rejects_unknown_manifest_shape_even_when_rehashed(
        self,
    ) -> None:
        mutations = {
            "top_level": lambda manifest: manifest.__setitem__("unexpected", True),
            "inputs": lambda manifest: manifest["inputs"].__setitem__(
                "unexpected",
                {"artifact_id": "x", "schema_version": "x", "sha256": "0" * 64},
            ),
            "output": lambda manifest: manifest["output"].__setitem__(
                "unexpected", True
            ),
            "boundaries": lambda manifest: manifest["boundaries"].__setitem__(
                "unexpected", True
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                brief, manifest_path, _ = self._case_handoff_fixture(f"unknown-{name}")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(manifest)
                projection = {
                    key: value
                    for key, value in manifest.items()
                    if key != "brief_projection_sha256"
                }
                manifest["brief_projection_sha256"] = canonical_json_sha256(projection)
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ContractError,
                    "case handoff manifest shape is invalid",
                ):
                    validate_content_video_inputs(
                        brief,
                        self.brief_schema,
                        self.research,
                        self.research_schema,
                        max_age_hours=1_000_000,
                        case_handoff_manifest_path=manifest_path,
                        case_handoff_manifest_sha256=canonical_json_sha256(manifest),
                    )

    def test_case_handoff_rejects_semantic_boundary_drift_even_when_rehashed(
        self,
    ) -> None:
        def synthetic_downgrade(manifest: dict[str, object]) -> None:
            manifest["synthetic_only"] = False

        def recorded_interview(manifest: dict[str, object]) -> None:
            manifest["interview_slots"][0]["state"] = "recorded"  # type: ignore[index]

        def script_identity_drift(manifest: dict[str, object]) -> None:
            manifest["script_id"] = "case-script-forged-001"

        def hook_projection_drift(manifest: dict[str, object]) -> None:
            manifest["selected_hook_binding"]["text_sha256"] = "0" * 64  # type: ignore[index]

        def experiment_shape_drift(manifest: dict[str, object]) -> None:
            manifest["experiment"]["second_variable"] = "duration"  # type: ignore[index]

        cases = {
            "synthetic": (
                synthetic_downgrade,
                "real case handoff review owner is unavailable",
            ),
            "interview": (recorded_interview, "interview is not authorized"),
            "script": (script_identity_drift, "script id drifted"),
            "hook": (hook_projection_drift, "selected hook projection drifted"),
            "experiment": (experiment_shape_drift, "manifest shape is invalid"),
        }
        for name, (mutate, expected) in cases.items():
            with self.subTest(name=name):
                brief, manifest_path, _ = self._case_handoff_fixture(f"semantic-{name}")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(manifest)
                projection = {
                    key: value
                    for key, value in manifest.items()
                    if key != "brief_projection_sha256"
                }
                manifest["brief_projection_sha256"] = canonical_json_sha256(projection)
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ContractError, expected):
                    validate_content_video_inputs(
                        brief,
                        self.brief_schema,
                        self.research,
                        self.research_schema,
                        max_age_hours=1_000_000,
                        case_handoff_manifest_path=manifest_path,
                        case_handoff_manifest_sha256=canonical_json_sha256(manifest),
                        require_case_handoff=True,
                    )

    def test_project_plan_detects_case_manifest_changed_after_validation(self) -> None:
        brief, manifest_path, manifest_sha256 = self._case_handoff_fixture(
            "post-plan-tamper"
        )
        inputs = validate_content_video_inputs(
            brief,
            self.brief_schema,
            self.research,
            self.research_schema,
            max_age_hours=1_000_000,
            case_handoff_manifest_path=manifest_path,
            case_handoff_manifest_sha256=manifest_sha256,
        )
        plan = build_content_video_project_plan(inputs)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "changed-after-plan"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ContractError,
            "case handoff manifest changed after the contract was created",
        ):
            validate_content_video_project_plan(plan)

    def test_project_plan_rejects_case_metadata_downgrade_after_rehash(self) -> None:
        brief, manifest_path, manifest_sha256 = self._case_handoff_fixture(
            "plan-case-metadata-downgrade"
        )
        inputs = validate_content_video_inputs(
            brief,
            self.brief_schema,
            self.research,
            self.research_schema,
            max_age_hours=1_000_000,
            case_handoff_manifest_path=manifest_path,
            case_handoff_manifest_sha256=manifest_sha256,
            require_case_handoff=True,
        )
        plan = build_content_video_project_plan(inputs)
        for field in (
            "case_handoff_manifest",
            "case_projection_sha256",
            "case_script_id",
        ):
            plan["inputs"].pop(field)
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(
            ContractError,
            "case handoff manifest required",
        ):
            validate_content_video_project_plan(plan)

    def test_project_plan_rejects_case_script_id_drift_after_rehash(self) -> None:
        brief, manifest_path, manifest_sha256 = self._case_handoff_fixture(
            "plan-case-script-drift"
        )
        inputs = validate_content_video_inputs(
            brief,
            self.brief_schema,
            self.research,
            self.research_schema,
            max_age_hours=1_000_000,
            case_handoff_manifest_path=manifest_path,
            case_handoff_manifest_sha256=manifest_sha256,
            require_case_handoff=True,
        )
        plan = build_content_video_project_plan(inputs)
        plan["inputs"]["case_script_id"] = "case-script-forged"
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(
            ContractError,
            "case script id changed",
        ):
            validate_content_video_project_plan(plan)

    def test_project_plan_revalidates_resigned_case_manifest(self) -> None:
        brief, manifest_path, manifest_sha256 = self._case_handoff_fixture(
            "plan-case-manifest-resign"
        )
        inputs = validate_content_video_inputs(
            brief,
            self.brief_schema,
            self.research,
            self.research_schema,
            max_age_hours=1_000_000,
            case_handoff_manifest_path=manifest_path,
            case_handoff_manifest_sha256=manifest_sha256,
            require_case_handoff=True,
        )
        plan = build_content_video_project_plan(inputs)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["synthetic_only"] = False
        projection = {
            key: value
            for key, value in manifest.items()
            if key != "brief_projection_sha256"
        }
        manifest["brief_projection_sha256"] = canonical_json_sha256(projection)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        plan["inputs"]["case_handoff_manifest"] = artifact_reference(
            manifest_path,
            "case handoff manifest",
        )
        plan["inputs"]["case_projection_sha256"] = manifest["brief_projection_sha256"]
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(
            ContractError,
            "real case handoff review owner is unavailable",
        ):
            validate_content_video_project_plan(plan)

    def test_project_plan_rejects_case_narration_drift_after_rehash(self) -> None:
        brief, manifest_path, manifest_sha256 = self._case_handoff_fixture(
            "plan-case-narration-drift"
        )
        inputs = validate_content_video_inputs(
            brief,
            self.brief_schema,
            self.research,
            self.research_schema,
            max_age_hours=1_000_000,
            case_handoff_manifest_path=manifest_path,
            case_handoff_manifest_sha256=manifest_sha256,
            require_case_handoff=True,
        )
        plan = build_content_video_project_plan(inputs)
        plan["shot_units"][0]["narration"] = "这次一定能赔。"
        plan["shot_units"][0]["unit_sha256"] = payload_sha256(
            plan["shot_units"][0],
            "unit_sha256",
        )
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(
            ContractError,
            "case scene projection changed",
        ):
            validate_content_video_project_plan(plan)

    def test_referenced_source_hash_must_be_verified(self) -> None:
        research = json.loads(self.research.read_text(encoding="utf-8"))
        for source in research["sources"]:
            if source["source_id"] == "source_fixture_video":
                source["hash"]["status"] = "recorded"
        research_path = self.root / "research-output.v1.json"
        research_path.write_text(json.dumps(research), encoding="utf-8")
        brief = json.loads(self.brief.read_text(encoding="utf-8"))
        brief["provenance"]["input_sha256"] = canonical_json_sha256(research)
        brief_path = self.root / "content-video-brief.v1.json"
        brief_path.write_text(json.dumps(brief), encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "hash is not verified"):
            validate_content_video_inputs(
                brief_path,
                self.brief_schema,
                research_path,
                self.research_schema,
                max_age_hours=1_000_000,
            )

    def test_standard_and_evergreen_inputs_do_not_expire_by_age(self) -> None:
        brief_path = self.root / "content-video-brief.v1.json"
        shutil.copy2(self.brief, brief_path)
        stale = time.time() - 7200
        os.utime(brief_path, (stale, stale))

        for sensitivity in ("standard", "evergreen"):
            with self.subTest(time_sensitivity=sensitivity):
                inputs = validate_content_video_inputs(
                    brief_path,
                    self.brief_schema,
                    self.research,
                    self.research_schema,
                    max_age_hours=1,
                    time_sensitivity=sensitivity,
                )
                self.assertFalse(inputs["freshness_policy"]["age_gate_applied"])
                self.assertEqual(
                    inputs["freshness_evaluation"]["decision"],
                    "age_not_required",
                )

    def test_high_temporal_stale_brief_file_fails_before_planning(self) -> None:
        brief_path = self.root / "content-video-brief.v1.json"
        shutil.copy2(self.brief, brief_path)
        stale = time.time() - 7200
        os.utime(brief_path, (stale, stale))

        with self.assertRaisesRegex(ContractError, "is stale"):
            validate_content_video_inputs(
                brief_path,
                self.brief_schema,
                self.research,
                self.research_schema,
                max_age_hours=1,
                time_sensitivity="high_temporal",
            )

    def test_source_freshness_is_conditional_on_high_temporal_policy(self) -> None:
        research = json.loads(self.research.read_text(encoding="utf-8"))
        for source in research["sources"]:
            if source["source_id"] == "source_fixture_video":
                source["freshness"] = "stale"
        research_path = self.root / "stale-source-research.json"
        research_path.write_text(json.dumps(research), encoding="utf-8")
        brief = json.loads(self.brief.read_text(encoding="utf-8"))
        brief["provenance"]["input_sha256"] = canonical_json_sha256(research)
        brief_path = self.root / "stale-source-brief.json"
        brief_path.write_text(json.dumps(brief), encoding="utf-8")

        standard = validate_content_video_inputs(
            brief_path,
            self.brief_schema,
            research_path,
            self.research_schema,
            max_age_hours=1_000_000,
            time_sensitivity="standard",
        )
        source_rows = standard["freshness_evaluation"]["referenced_sources"]
        stale_row = next(
            row for row in source_rows if row["source_id"] == "source_fixture_video"
        )
        self.assertEqual(stale_row["decision"], "recorded_not_gated")

        with self.assertRaisesRegex(ContractError, "high-temporal.*not current"):
            validate_content_video_inputs(
                brief_path,
                self.brief_schema,
                research_path,
                self.research_schema,
                max_age_hours=1_000_000,
                time_sensitivity="high_temporal",
            )

    def test_future_timestamp_is_rejected_for_every_time_policy(self) -> None:
        research = json.loads(self.research.read_text(encoding="utf-8"))
        research["created_at"] = "2999-01-01T00:00:00+00:00"
        research_path = self.root / "future-research.json"
        research_path.write_text(json.dumps(research), encoding="utf-8")
        brief = json.loads(self.brief.read_text(encoding="utf-8"))
        brief["provenance"]["input_sha256"] = canonical_json_sha256(research)
        brief_path = self.root / "future-brief.json"
        brief_path.write_text(json.dumps(brief), encoding="utf-8")

        for sensitivity in ("high_temporal", "standard", "evergreen"):
            with self.subTest(time_sensitivity=sensitivity):
                with self.assertRaisesRegex(
                    ContractError, "timestamp is in the future"
                ):
                    validate_content_video_inputs(
                        brief_path,
                        self.brief_schema,
                        research_path,
                        self.research_schema,
                        max_age_hours=1_000_000,
                        time_sensitivity=sensitivity,
                    )

    def test_plan_bundle_double_write_is_stable(self) -> None:
        plan = build_content_video_project_plan(self._inputs())
        output_root = self.root / "plan"
        first = write_content_video_plan_bundle(plan, output_root)
        first_hashes = {key: file_sha256(path) for key, path in first.items()}
        second = write_content_video_plan_bundle(plan, output_root)
        second_hashes = {key: file_sha256(path) for key, path in second.items()}

        self.assertEqual(first_hashes, second_hashes)
        self.assertEqual(len(first_hashes), 11)
        self.assertTrue((output_root / "edit" / "subtitles.srt").is_file())
        self.assertTrue((output_root / "audio" / "tts-job.json").is_file())
        self.assertTrue((output_root / "audio" / "music-intent-curve.json").is_file())

    def test_dynamic_age_evaluation_does_not_change_plan_hash(self) -> None:
        first = build_content_video_project_plan(self._inputs())
        time.sleep(0.01)
        second = build_content_video_project_plan(self._inputs())

        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(
            first["inputs"]["freshness_policy"],
            second["inputs"]["freshness_policy"],
        )

    def test_project_plan_rejects_freshness_policy_downgrade_after_rehash(self) -> None:
        plan = build_content_video_project_plan(self._inputs())
        plan["inputs"]["freshness_policy"]["age_gate_applied"] = True
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")

        with self.assertRaisesRegex(ContractError, "policy gates are inconsistent"):
            validate_content_video_project_plan(plan)

    def test_dry_run_does_not_create_output_root(self) -> None:
        plan = build_content_video_project_plan(self._inputs())
        output_root = self.root / "dry-run"

        receipt = build_plan_dry_run(plan, output_root)

        self.assertFalse(output_root.exists())
        self.assertFalse(receipt["would_execute_ffmpeg"])
        self.assertFalse(receipt["would_call_provider"])
        self.assertFalse(receipt["would_upload"])
        self.assertFalse(receipt["would_publish"])

    def test_plan_refuses_nonempty_unmanaged_output_root(self) -> None:
        plan = build_content_video_project_plan(self._inputs())
        output_root = self.root / "unmanaged"
        media_path = output_root / "publish" / "candidate-synthetic.mp4"
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(b"user-media-sentinel")

        with self.assertRaisesRegex(ContractError, "non-empty and unmanaged"):
            write_content_video_plan_bundle(plan, output_root)

        self.assertEqual(media_path.read_bytes(), b"user-media-sentinel")
        self.assertFalse((output_root / "project-plan.json").exists())

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and ffprobe are required for the synthetic end-to-end test",
    )
    def test_synthetic_candidate_double_run_is_hash_stable_and_candidate_only(
        self,
    ) -> None:
        plan = build_content_video_project_plan(self._inputs())
        output_root = self.root / "synthetic"
        write_content_video_plan_bundle(plan, output_root)

        first = generate_synthetic_candidate(output_root, timeout_s=180)
        first_hash = file_sha256(output_root / "publish" / "candidate-synthetic.mp4")
        second = generate_synthetic_candidate(output_root, timeout_s=180)
        second_hash = file_sha256(output_root / "publish" / "candidate-synthetic.mp4")

        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first["status"], "completed_candidate_only")
        self.assertEqual(second["status"], "completed_candidate_only")
        self.assertTrue(second["synthetic_material_only"])
        self.assertFalse(second["production_success_claimed"])
        self.assertFalse(second["upload_performed"])
        self.assertFalse(second["publish_performed"])
        validate_content_synthetic_run_receipt(second)
        qa = json.loads(
            (output_root / "qa" / "qa-report.json").read_text(encoding="utf-8")
        )
        self.assertTrue(qa["readiness"]["technical_ready"])
        self.assertFalse(qa["readiness"]["release_eligible"])
        manifest = json.loads(
            (output_root / "render-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "candidate_only")
        self.assertFalse(manifest["production_success_claimed"])
        self.assertFalse(manifest["external_provider_called"])
        self.assertFalse(manifest["upload_performed"])
        self.assertFalse(manifest["publish_performed"])
        atomic_manifest = json.loads(
            Path(manifest["atomic_output_manifest"]["path"]).read_text(encoding="utf-8")
        )
        validate_content_video_atomic_output_manifest(atomic_manifest)
        self.assertEqual(
            atomic_manifest["ffmpeg_receipt_count"],
            second["ffmpeg_receipt_count"],
        )
        self.assertEqual(
            {row["ffmpeg_receipt"]["path"] for row in atomic_manifest["outputs"]},
            {reference["path"] for reference in manifest["ffmpeg_receipts"]},
        )
        self.assertTrue(
            all(
                row["ffmpeg_output"]["sha256"] == row["committed_output"]["sha256"]
                for row in atomic_manifest["outputs"]
            )
        )
        for reference in manifest["ffmpeg_receipts"]:
            receipt = json.loads(Path(reference["path"]).read_text(encoding="utf-8"))
            self.assertEqual(
                receipt["outlet_id"],
                "video_creation_pipeline.single_ffmpeg_outlet",
            )

    def _inputs(self) -> dict[str, object]:
        return validate_content_video_inputs(
            self.brief,
            self.brief_schema,
            self.research,
            self.research_schema,
            max_age_hours=1_000_000,
        )


if __name__ == "__main__":
    unittest.main()
