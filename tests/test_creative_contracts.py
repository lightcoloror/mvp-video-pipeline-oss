from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
)
from video_creation_pipeline.creative_cli import main as creative_cli_main
from video_creation_pipeline.creative_gates import (
    build_approval_record,
    build_release_gate,
    validate_approval_record,
    validate_release_gate,
    write_creative_contract,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.generation_contracts import (
    CAPABILITY_PREFLIGHT_SCHEMA,
    GENERATION_RECEIPT_SCHEMA,
    build_generation_task,
    build_shot_generation_unit,
    validate_generation_receipt,
    write_generation_contract,
)
from video_creation_pipeline.nle_bridge import (
    NLE_MEDIA_MAP_SCHEMA,
    PREMIERE_IMPORT_JOB_SCHEMA,
    PREMIERE_IMPORT_RECEIPT_SCHEMA,
    PREMIERE_PLUGIN_STATUS_SCHEMA,
    build_premiere_import_job,
    validate_premiere_import_receipt,
    validate_premiere_plugin_status,
    write_premiere_import_job,
)
from video_creation_pipeline.previs_contracts import (
    PREVIS_CAPTURE_MANIFEST_SCHEMA,
    build_previs_scene,
    validate_previs_capture_manifest,
    write_previs_scene,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class CreativeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)
        self.work = self.root / "video-creation"
        self.work.mkdir()
        self.edit_job_path = self.work / "edit-job.json"
        write_edit_job(build_edit_job(self.bundle), self.edit_job_path)
        self.unit_path = self.work / "shot-unit.json"
        unit = build_shot_generation_unit(
            self.edit_job_path,
            "storyboard-0001",
            generator="remotion",
            visual_prompt="A verified browser workflow shown as a concise motion graphic.",
        )
        write_generation_contract(unit, self.unit_path)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_atomic_writer_replaces_without_leaving_temp_files(self) -> None:
        path = self.work / "atomic.json"
        atomic_write_json({"version": 1}, path)
        atomic_write_json({"version": 2}, path)

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 2})
        self.assertFalse(list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_voice_approval_binds_measurements_audio_and_comment(self) -> None:
        voice = self._json("voice-plan.json", {"segments": [{"id": "voice-1"}]})
        measurements = self._json(
            "voice-measurements.json", {"schema": "voice_measurements.v1"}
        )
        audio = self.work / "voice-1.wav"
        audio.write_bytes(b"wave-data")
        record = build_approval_record(
            "voice",
            voice,
            approved_by="operator",
            approved_at="2026-07-22T10:00:00+08:00",
            comment="voice approved",
            attachments=[
                {"role": "voice_measurements", "path": measurements},
                {"role": "audio:voice-1", "path": audio},
            ],
        )
        path = self.work / "voice-approval.json"
        write_creative_contract(record, path)
        validate_approval_record(record)

        tampered_comment = json.loads(json.dumps(record))
        tampered_comment["attestation"]["comment"] = "changed"
        with self.assertRaisesRegex(ContractError, "integrity"):
            validate_approval_record(tampered_comment)
        audio.write_bytes(b"changed-wave-data")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_approval_record(record)

    def test_release_gate_rejects_stale_visual_plan(self) -> None:
        approvals, visual_plan = self._release_approvals()
        visual_payload = json.loads(visual_plan.read_text(encoding="utf-8"))
        manifest = self._json(
            "render-manifest.json",
            {"visual_plan_sha256": canonical_json_sha256(visual_payload)},
        )
        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual_plan,
        )
        validate_release_gate(gate)

        visual_plan.write_text('{"shots":[{"id":"changed"}]}', encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_release_gate(gate)

    def test_generation_task_requires_preflight_and_validates_output(self) -> None:
        approval_path = self._storyboard_approval()
        unavailable = self._json(
            "preflight-unavailable.json",
            self._preflight(ready=False),
        )
        with self.assertRaisesRegex(ContractError, "unavailable"):
            build_generation_task(
                self.unit_path,
                approval_path,
                unavailable,
                output_dir=self.work / "generated",
            )

        preflight = self._json("preflight.json", self._preflight(ready=True))
        task = build_generation_task(
            self.unit_path,
            approval_path,
            preflight,
            output_dir=self.work / "generated",
        )
        task_path = self.work / "generation-task.json"
        write_generation_contract(task, task_path)
        output_dir = Path(task["output"]["directory"])
        output_dir.mkdir()
        video = output_dir / "shot.mp4"
        video.write_bytes(b"video-bytes")
        frame = output_dir / "frame-001.jpg"
        frame.write_bytes(b"frame-bytes")
        receipt = {
            "schema": GENERATION_RECEIPT_SCHEMA,
            "task_id": task["task_id"],
            "task_sha256": task["task_sha256"],
            "generator": "remotion",
            "status": "completed",
            "output": artifact_reference(video, "generated output"),
            "technical_verification": {
                "width": 1080,
                "height": 1920,
                "duration_s": 11.0,
                "has_video_stream": True,
                "probe_method": "existing_ffprobe_receipt",
            },
            "visual_verification": {
                "inspected": True,
                "blank": False,
                "black": False,
                "wrong_composition": False,
                "representative_frames": [artifact_reference(frame, "frame")],
            },
            "published": False,
            "fallback_used": False,
        }

        validation = validate_generation_receipt(task, receipt)

        self.assertEqual(validation["next_action"], "human_review")
        receipt["visual_verification"]["black"] = True
        with self.assertRaisesRegex(ContractError, "visual inspection"):
            validate_generation_receipt(task, receipt)

    def test_previs_scene_binds_camera_metadata_and_capture(self) -> None:
        scene = build_previs_scene(self.unit_path)
        scene_path = self.work / "previs-scene.json"
        write_previs_scene(scene, scene_path)
        capture = self.work / "previs.png"
        capture.write_bytes(b"png-bytes")
        camera = scene["cameras"][0]
        manifest = {
            "schema": PREVIS_CAPTURE_MANIFEST_SCHEMA,
            "previs_scene_sha256": scene["previs_scene_sha256"],
            "captures": [
                {
                    "capture_id": "capture-1",
                    "camera_id": camera["camera_id"],
                    "artifact": artifact_reference(capture, "capture"),
                    "fov": camera["fov"],
                    "position": camera["position"],
                    "target": camera["target"],
                    "width": camera["frame"]["width"],
                    "height": camera["frame"]["height"],
                }
            ],
            "acceptance_status": "candidate_only",
            "final_render_claimed": False,
        }

        result = validate_previs_capture_manifest(scene, manifest)

        self.assertEqual(result["capture_count"], 1)
        manifest["captures"][0]["fov"] = 90.0
        with self.assertRaisesRegex(ContractError, "metadata drifted"):
            validate_previs_capture_manifest(scene, manifest)

    def test_premiere_job_adds_idempotency_and_reports_partial_commit(self) -> None:
        selection = self._human_selection()
        media_a = self.work / "camera-a.mp4"
        media_b = self.work / "camera-b.mp4"
        media_a.write_bytes(b"camera-a")
        media_b.write_bytes(b"camera-b")
        media_map = self._json(
            "media-map.json",
            {
                "schema": NLE_MEDIA_MAP_SCHEMA,
                "assets": [
                    {"candidate_id": "timeline-0001", "path": str(media_a)},
                    {"candidate_id": "timeline-0002", "path": str(media_b)},
                ],
            },
        )
        task = build_premiere_import_job(
            self.edit_job_path,
            selection,
            media_map,
            playhead_s=3.0,
            project_id="project-fixture",
            sequence_id="sequence-fixture",
        )
        task_path = self.work / "premiere-job.json"
        write_premiere_import_job(task, task_path)

        self.assertEqual(task["schema"], PREMIERE_IMPORT_JOB_SCHEMA)
        self.assertFalse(task["plugin_requirements"]["basename_fallback_allowed"])
        self.assertEqual(len(task["segments"]), 2)
        receipt = self._premiere_receipt(task, completed=True)
        result = validate_premiere_import_receipt(task, receipt)
        self.assertEqual(result["next_action"], "human_timeline_review")

        partial = self._premiere_receipt(task, completed=False)
        partial["segments"] = partial["segments"][:1]
        partial["partial_commit_detected"] = True
        result = validate_premiere_import_receipt(task, partial)
        self.assertEqual(result["next_action"], "manual_partial_commit_recovery")
        partial["basename_fallback_used"] = True
        with self.assertRaisesRegex(ContractError, "basename fallback"):
            validate_premiere_import_receipt(task, partial)

    def test_premiere_heartbeat_requires_safety_capabilities(self) -> None:
        status = {
            "schema": PREMIERE_PLUGIN_STATUS_SCHEMA,
            "plugin_id": "compatible-premiere-adapter",
            "ready": True,
            "updated_at": "2026-07-22T02:00:00+00:00",
            "supported_schemas": [PREMIERE_IMPORT_JOB_SCHEMA],
            "exact_media_path_match": True,
            "idempotency_supported": True,
            "partial_commit_reporting": True,
            "project_locked_access_required": True,
            "compound_transaction_required": True,
            "project_identity_supported": True,
            "sequence_identity_supported": True,
        }
        validate_premiere_plugin_status(
            status, now=datetime(2026, 7, 22, 2, 0, 30, tzinfo=timezone.utc)
        )
        status["idempotency_supported"] = False
        with self.assertRaisesRegex(ContractError, "safety capabilities"):
            validate_premiere_plugin_status(
                status, now=datetime(2026, 7, 22, 2, 0, 30, tzinfo=timezone.utc)
            )

    def test_cli_builds_previs_without_executing_a_renderer(self) -> None:
        output = self.work / "cli-previs.json"

        code = creative_cli_main(
            [
                "build-previs-scene",
                "--unit",
                str(self.unit_path),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(payload["authority_boundary"]["final_render_claimed"])

    def _release_approvals(self) -> tuple[list[Path], Path]:
        script = self._json("script.json", {"text": "approved script"})
        voice = self._json("voice.json", {"segments": [{"id": "voice-1"}]})
        measurements = self._json("measurements.json", {"duration_s": 11.0})
        audio = self.work / "voice.wav"
        audio.write_bytes(b"voice")
        visual = self._json("visual-plan.json", {"shots": [{"id": "shot-1"}]})
        review = self.work / "director-review.md"
        review.write_text("approved", encoding="utf-8")
        records = [
            build_approval_record(
                "script",
                script,
                approved_by="operator",
                approved_at="2026-07-22T10:00:00+08:00",
            ),
            build_approval_record(
                "voice",
                voice,
                approved_by="operator",
                approved_at="2026-07-22T10:01:00+08:00",
                attachments=[
                    {"role": "voice_measurements", "path": measurements},
                    {"role": "audio", "path": audio},
                ],
            ),
            build_approval_record(
                "visuals",
                visual,
                approved_by="operator",
                approved_at="2026-07-22T10:02:00+08:00",
                attachments=[{"role": "director_review_sheet", "path": review}],
            ),
        ]
        paths = []
        for record in records:
            path = self.work / f"approval-{record['target']}.json"
            write_creative_contract(record, path)
            paths.append(path)
        return paths, visual

    def _storyboard_approval(self) -> Path:
        approval = build_approval_record(
            "storyboard",
            self.unit_path,
            approved_by="operator",
            approved_at="2026-07-22T10:00:00+08:00",
        )
        path = self.work / "storyboard-approval.json"
        write_creative_contract(approval, path)
        return path

    def _preflight(self, *, ready: bool) -> dict:
        return {
            "schema": CAPABILITY_PREFLIGHT_SCHEMA,
            "checked_at": "2026-07-22T02:00:00+00:00",
            "capabilities": [
                {
                    "id": "remotion_render",
                    "ready": ready,
                    "probe_method": "existing_runtime",
                    "installation_performed": False,
                    "evidence": {"version": "review-fixture"},
                }
            ],
        }

    def _human_selection(self) -> Path:
        path = self.work / "human-selection.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "video_creation_pipeline.human_selection.v1",
                    "edit_job_sha256": file_sha256(self.edit_job_path),
                    "selections": [
                        {"candidate_id": "timeline-0001"},
                        {"candidate_id": "timeline-0002"},
                    ],
                    "operator_confirmation": {
                        "confirmed": True,
                        "confirmed_at": "2026-07-22T02:00:00+00:00",
                        "method": "visible_operator_selection",
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def _premiere_receipt(self, task: dict, *, completed: bool) -> dict:
        cursor = float(task["playhead_s"])
        rows = []
        for segment in task["segments"]:
            end = round(cursor + float(segment["duration_s"]), 3)
            rows.append(
                {
                    "segment_id": segment["segment_id"],
                    "source_sha256": segment["source_sha256"],
                    "exact_media_path_match": True,
                    "inserted": True,
                    "already_applied": False,
                    "project_item_id": f"project-{segment['segment_id']}",
                    "sequence_item_id": f"sequence-{segment['segment_id']}",
                    "insert_start_s": cursor,
                    "insert_end_s": end,
                }
            )
            cursor = end
        return {
            "schema": PREMIERE_IMPORT_RECEIPT_SCHEMA,
            "task_id": task["task_id"],
            "task_sha256": task["task_sha256"],
            "idempotency_key": task["idempotency_key"],
            "status": "completed" if completed else "failed",
            "error": "" if completed else "plugin transaction interrupted",
            "target_identity": dict(task["target_identity"]),
            "project_locked_access_used": True,
            "compound_transaction_used": True,
            "segments": rows,
            "published": False,
            "source_media_modified": False,
            "basename_fallback_used": False,
            "automatic_retry_performed": False,
            "partial_commit_detected": False,
            "duplicate_insertion_detected": False,
        }

    def _json(self, name: str, payload: dict) -> Path:
        path = self.work / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
