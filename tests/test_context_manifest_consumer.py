import importlib.util
import hashlib
import json
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.context_manifest_consumer import plan_review


REPO_ROOT = Path(__file__).resolve().parents[1]
SELF_MEDIA_ROOT = Path(
    os.environ.get(
        "SELF_MEDIA_SYSTEM_ROOT",
        str(REPO_ROOT.parents[1] / "self-media-creation-system"),
    )
)
TEST_RUNTIME_ROOT = Path(
    os.environ.get("VIDEO_PIPELINE_TEST_ROOT", str(REPO_ROOT / ".test-runtime"))
)


def load_agent():
    spec = importlib.util.spec_from_file_location(
        "mvp_test_self_media_agent", SELF_MEDIA_ROOT / "scripts" / "self_media_agent.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContextManifestConsumerTests(unittest.TestCase):
    def test_shared_manifest_and_review_plan_are_consumed(self) -> None:
        # Reuse the proven synthetic fixture builder from the shared contract tests.
        root = TEST_RUNTIME_ROOT / ("context-consumer-" + uuid.uuid4().hex)
        root.mkdir(parents=True, exist_ok=True)
        try:
            agent = load_agent()
            request = agent.load_json(
                SELF_MEDIA_ROOT
                / "fixtures"
                / "agent-entry"
                / "context-efficient-rework.request.json"
            )
            capsule_path = root / "capsule.json"
            capsule_path.write_text(
                json.dumps(agent.resolve_request(request), ensure_ascii=False),
                encoding="utf-8",
            )
            media = root / "source.mp4"
            media.write_bytes(b"synthetic-video")
            decision = root / "decision.json"
            decision.write_text('{"status":"approved"}', encoding="utf-8")
            rough = root / "rough.json"
            rough.write_text(
                '{"schema":"video_creation_pipeline.rough_cut_workspace.v2",'
                '"workspace_sha256":"synthetic"}',
                encoding="utf-8",
            )
            proxy_review = root / "proxy-review-plan.json"
            proxy_review.write_text('{"status":"ready"}', encoding="utf-8")
            asr_decision = root / "asr-decision.json"
            asr_decision.write_text(
                '{"decision":"reuse_existing_asr"}', encoding="utf-8"
            )

            def digest(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()

            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "self-media-video-review-input.v1",
                        "task_id": "mvp-synthetic",
                        "mode": "initial_review",
                        "source_media": {
                            "path": media.name,
                            "expected_sha256": digest(media),
                            "duration_seconds": 30,
                        },
                        "decision_sheet": {
                            "path": decision.name,
                            "expected_sha256": digest(decision),
                        },
                        "rough_cut_workspace": {
                            "path": rough.name,
                            "expected_sha256": digest(rough),
                        },
                        "changed_windows": [],
                        "frozen_windows": [],
                        "acceptance_frame_timestamps": [3, 15, 27],
                        "change_kinds": [],
                    }
                ),
                encoding="utf-8",
            )
            # Build both native predecessor handoffs and actual synthetic owner
            # outputs. A transport receipt never claims ASR/media execution.
            capsule = agent.load_json(capsule_path)
            shared = agent._load_context_efficiency()
            change = root / "change.json"
            change.write_text('{"synthetic_only":true,"change":"initial review"}', encoding="utf-8")
            sources = {"change_description": change, "decision_sheet": decision,
                       "rough_cut_workspace": rough, "source_media": media,
                       "proxy-review-plan": proxy_review}
            purpose_source = root / "synthetic-purpose.json"
            purpose_source.write_text(json.dumps({"synthetic_only": True,
                "purpose": "Locate exact review ranges without rendering media.",
                "audience": "Synthetic offline review testers."}), encoding="utf-8")
            purpose_artifact = {"artifact_id": "synthetic-purpose", "role": "source_manifest",
                "source_role": "editorial-purpose", "path": purpose_source.name,
                "load_mode": "reference", "expected_sha256": digest(purpose_source)}
            def purpose(step_id):
                return {"version": 1, "content_id": "synthetic-review-transport",
                    "purpose": {"artifact_id": "synthetic-purpose",
                        "quote": "Locate exact review ranges without rendering media."},
                    "audience": {"artifact_id": "synthetic-purpose",
                        "quote": "Synthetic offline review testers."},
                    "stage_duty": "Validate only synthetic review metadata at " + step_id,
                    "current_body_artifact_id": None, "feedback": [],
                    "feedback_note": "No real author feedback, draft or approval is supplied."}
            purpose_contract = importlib.import_module("purpose_continuity")
            previous = None
            for step in capsule["step_index"][:2]:
                step_context = root / (step["step_id"] + "-context.json")
                agent.write_or_print({
                    "schema_version": "self-media-context-input.v1",
                    "task_id": capsule["request_id"], "stage": step["context_stage"],
                    "budget": {"max_inline_bytes": 1000},
                    "artifacts": [{"artifact_id": "input-" + str(index),
                        "role": "source_manifest", "source_role": role,
                        "path": sources[role].name, "load_mode": "reference",
                        "expected_sha256": digest(sources[role])}
                        for index, role in enumerate(step["input_roles"])] + [purpose_artifact],
                    "editorial_context": purpose(step["step_id"]),
                }, step_context)
                step_handoff = root / (step["step_id"] + "-handoff.json")
                agent.prepare_step_handoff(capsule_path, step["step_id"], step_context,
                    root / (step["step_id"] + "-context-receipt.json"), step_handoff,
                    previous_receipt=previous)
                if step["step_id"] == "plan-bounded-review":
                    shared.build_video_review_plan(manifest, proxy_review)
                    artifact = proxy_review
                else:
                    self.assertEqual(step["step_id"], "decide-asr-need")
                    asr = shared.decide_asr(media, None, asr_decision, [])
                    self.assertEqual(asr["decision"], "rerun_full_asr")
                    artifact = asr_decision
                purpose_review = root / (step["step_id"] + "-purpose-review.json")
                agent.write_or_print({"schema_version": purpose_contract.REVIEW_SCHEMA,
                    "context_ref": {"path": step_context.name, "sha256": digest(step_context)},
                    "artifact_ref": {"path": artifact.name, "sha256": digest(artifact)},
                    "quote": artifact.read_text(encoding="utf-8"),
                    "purpose_alignment": {"stage_duty": purpose(step["step_id"])["stage_duty"],
                        "contribution": "Bind the actual synthetic plan or ASR-decision output for the next metadata step.",
                        "misunderstanding_risk": "This does not establish ASR, media, author or platform completion.",
                        "tradeoff": "Keep all execution and human approval pending."},
                    "feedback_resolutions": [], "disposition": "retained",
                    "reason": "Actual synthetic metadata output; no content quality or execution claim."}, purpose_review)
                receipt = {"schema_version": "self-media-step-output-receipt.v1",
                    "status": "candidate_ready", "step_id": step["step_id"],
                    "tool_id": step["tool_id"], "output_contract": step["output_contract"],
                    "handoff": {"path": step_handoff.name, "sha256": digest(step_handoff)},
                    "outputs": [{"path": artifact.name, "sha256": digest(artifact)},
                                {"path": purpose_review.name, "sha256": digest(purpose_review)}],
                    "boundaries": capsule["boundaries"]}
                receipt["payload_sha256"] = agent.sha256_value(receipt)
                previous = root / (step["step_id"] + "-output-receipt.json")
                agent.write_or_print(receipt, previous)
                verification = agent.verify_step_output(step_handoff, previous)
                self.assertTrue(verification["ok"])
                self.assertFalse(verification["workflow_step_completed"])
            context = root / "context-manifest.json"
            context.write_text(
                json.dumps(
                    {
                        "schema_version": "self-media-context-input.v1",
                        "task_id": request["request_id"],
                        "stage": "video",
                        "editorial_context": purpose("execute-guarded-technical-review"),
                        "budget": {"max_inline_bytes": 1000},
                        "artifacts": [purpose_artifact,
                            {
                                "artifact_id": "proxy-review-plan",
                                "role": "proxy_review_plan",
                                "source_role": "proxy-review-plan",
                                "path": proxy_review.name,
                                "load_mode": "reference",
                                "expected_sha256": digest(proxy_review),
                            },
                            {
                                "artifact_id": "asr-decision",
                                "role": "asr_decision_receipt",
                                "source_role": "asr-decision-receipt",
                                "path": asr_decision.name,
                                "load_mode": "reference",
                                "expected_sha256": digest(asr_decision),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            # Current native route names the MVP step explicitly. The context
            # still binds both required source artifacts; no media is executed.
            handoff = root / "handoff.json"
            agent.prepare_step_handoff(
                capsule_path,
                "execute-guarded-technical-review",
                context,
                root / "context-receipt.json",
                handoff,
                previous_receipt=previous,
            )
            plan = root / "plan.json"
            result = plan_review(handoff, manifest, plan)
            self.assertEqual(result, agent.load_json(proxy_review))
            self.assertEqual(
                result["review_scope"]["render_scope"], "full_timeline_source"
            )
            self.assertFalse(result["loading_strategy"]["proxy_first"])
            self.assertTrue(result["loading_strategy"]["full_resolution_inline"])
            self.assertFalse(result["authority_boundary"]["media_rendered"])
            asr_decision.write_text('{"decision":"tampered"}', encoding="utf-8")
            with self.assertRaisesRegex(Exception, "context_receipt_drift"):
                plan_review(handoff, manifest, root / "drift-plan.json")
        finally:
            shutil.rmtree(root, ignore_errors=True)
