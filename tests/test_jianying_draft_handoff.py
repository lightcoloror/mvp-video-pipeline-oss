from __future__ import annotations

# ruff: noqa: E402

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import file_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.jianying_draft_handoff import (
    JIANYING_SPOKEN_DRAFT_JOB_SCHEMA,
    VECTCUT_COMMIT,
    build_jianying_draft_job,
    build_jianying_spoken_draft_job,
    materialize_jianying_draft,
    materialize_jianying_project_folder,
    record_jianying_desktop_validation,
    validate_jianying_desktop_validation_receipt,
    validate_jianying_draft_job,
    validate_jianying_draft_receipt,
    validate_jianying_project_folder_receipt,
    vectcut_doctor,
    write_jianying_draft_job,
)
from video_creation_pipeline.nle_bridge import NLE_MEDIA_MAP_SCHEMA
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class JianyingDraftHandoffTests(unittest.TestCase):

    def setUp(self) -> None:
        managed_root = Path(os.environ.get("TEST_TMP_ROOT", tempfile.gettempdir()))
        self.root = managed_root / "mvp-jianying-draft" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)
        self.edit_job = self.root / "edit-job.json"
        write_edit_job(build_edit_job(self.bundle), self.edit_job)
        self.selection = self._json(
            "selection.json",
            {
                "schema": "video_creation_pipeline.human_selection.v1",
                "edit_job_sha256": file_sha256(self.edit_job),
                "selections": [
                    {"candidate_id": "timeline-0001"},
                    {"candidate_id": "timeline-0002"},
                ],
                "operator_confirmation": {
                    "confirmed": True,
                    "confirmed_at": "2026-08-20T00:00:00+00:00",
                    "method": "visible_operator_selection",
                },
            },
        )
        self.media_a = self.root / "camera-a.mp4"
        self.media_b = self.root / "camera-b.mp4"
        self.media_a.write_bytes(b"camera-a")
        self.media_b.write_bytes(b"camera-b")
        self.media_map = self._json(
            "media-map.json",
            {
                "schema": NLE_MEDIA_MAP_SCHEMA,
                "assets": [
                    {"candidate_id": "timeline-0001", "path": str(self.media_a)},
                    {"candidate_id": "timeline-0002", "path": str(self.media_b)},
                ],
            },
        )
        self.vectcut = self.root / "VectCutAPI"
        for relative in (
            "pyJianYingDraft/script_file.py",
            "draft_profiles.py",
            "pyJianYingDraft/local_materials.py",
            "pyJianYingDraft/video_segment.py",
        ):
            path = self.vectcut / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# reviewed source fixture\n", encoding="utf-8")
        (self.vectcut / "LICENSE").write_text(
            "Apache License\nVersion 2.0\n", encoding="utf-8"
        )
        (self.vectcut / "pyproject.toml").write_text(
            'classifiers = ["License :: OSI Approved :: MIT License"]\n',
            encoding="utf-8",
        )
        for relative in (
            "template_jianying_10_2/draft_content.json",
            "template_jianying_10_2/Timelines/project.json",
            "template_jianying_10_2/timeline_layout.json",
        ):
            path = self.vectcut / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    @patch(
        "video_creation_pipeline.jianying_draft_handoff._git_head",
        return_value=VECTCUT_COMMIT,
    )
    def test_job_reuses_confirmed_nle_segments_and_fails_on_media_drift(
        self, _head
    ) -> None:
        job = build_jianying_draft_job(
            self.edit_job,
            self.selection,
            self.media_map,
            self.vectcut,
        )
        validate_jianying_draft_job(job)
        self.assertEqual(len(job["segments"]), 2)
        self.assertTrue(job["output_contract"]["candidate_only"])
        self.assertFalse(job["execution_boundary"]["upstream_downloader_allowed"])
        self.assertFalse(job["execution_boundary"]["ffmpeg_orchestration_created"])

        self.media_a.write_bytes(b"changed")
        with self.assertRaisesRegex(ContractError, "confirmed NLE selection"):
            validate_jianying_draft_job(job)

    @patch(
        "video_creation_pipeline.jianying_draft_handoff._git_head",
        return_value=VECTCUT_COMMIT,
    )
    def test_materializer_writes_candidate_receipt_and_is_idempotent(
        self, _head
    ) -> None:
        job = build_jianying_draft_job(
            self.edit_job,
            self.selection,
            self.media_map,
            self.vectcut,
        )
        job_path = self.root / "jianying-job.json"
        write_jianying_draft_job(job, job_path)

        def fake_worker(command, **_kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "fps": 30,
                        "canvas_config": {
                            "width": 1080,
                            "height": 1920,
                            "ratio": "original",
                        },
                        "materials": {},
                        "tracks": [{"segments": [{"id": "a"}, {"id": "b"}]}],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        output = self.root / "candidate-draft"
        with patch(
            "video_creation_pipeline.jianying_draft_handoff.subprocess.run",
            side_effect=fake_worker,
        ) as worker:
            first = materialize_jianying_draft(job_path, output)
            second = materialize_jianying_draft(job_path, output)
        validate_jianying_draft_receipt(first)
        self.assertEqual(first, second)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(first["status"], "candidate_draft_payload_generated")
        self.assertFalse(first["full_project_folder_generated"])
        self.assertFalse(first["jianying_runtime_validated"])

    def test_doctor_requires_explicit_fixed_source(self) -> None:
        report = vectcut_doctor()
        self.assertEqual(report["status"], "blocked_missing_source")
        self.assertFalse(report["source_ready"])

    @patch(
        "video_creation_pipeline.jianying_draft_handoff._git_head",
        return_value=VECTCUT_COMMIT,
    )
    def test_materializer_removes_worker_temp_output_on_failure(self, _head) -> None:
        job = build_jianying_draft_job(
            self.edit_job,
            self.selection,
            self.media_map,
            self.vectcut,
        )
        job_path = self.root / "failed-jianying-job.json"
        write_jianying_draft_job(job, job_path)
        output = self.root / "failed-candidate-draft"

        def failing_worker(command, **_kwargs):
            temp_output = Path(command[command.index("--output") + 1])
            temp_output.write_text("partial", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, "", "serializer failed")

        with patch(
            "video_creation_pipeline.jianying_draft_handoff.subprocess.run",
            side_effect=failing_worker,
        ):
            with self.assertRaisesRegex(ContractError, "serializer failed"):
                materialize_jianying_draft(job_path, output)
        self.assertFalse((output / ".vectcut-worker-output.json").exists())

    @patch(
        "video_creation_pipeline.jianying_draft_handoff._git_head",
        return_value=VECTCUT_COMMIT,
    )
    def test_spoken_plan_can_feed_existing_jianying_serializer(self, _head) -> None:
        plan = self._json(
            "spoken-plan.json",
            {
                "schema": "video_creation_pipeline.spoken_edit_plan.v1",
                "nle_segments": [
                    {
                        "segment_id": "spoken-keep-0001",
                        "candidate_id": "spoken-keep-0001",
                        "timeline_index": 1,
                        "source_path": str(self.media_a),
                        "source_bytes": self.media_a.stat().st_size,
                        "source_sha256": file_sha256(self.media_a),
                        "in_s": 0.0,
                        "out_s": 2.0,
                        "duration_s": 2.0,
                        "label": "口播净剪保留段 1",
                    }
                ],
            },
        )
        request = self._json("delivery-request.json", {
            "schema": "video_creation_pipeline.spoken_delivery_request.v1",
            "deliverable": "editable_project", "editor": "jianying",
            "selection_basis": "Synthetic explicit request for an editable Jianying project",
        })
        # Real PCM evidence for the newly shared route guard. The serializer
        # fixture media remains synthetic; this is not desktop-import approval.
        from segment_level_fixture import make_review
        from video_creation_pipeline.contract_io import artifact_reference
        audio_review = make_review(self.root / "synthetic-route-levels", self.media_a,
            [{"start": 0, "end": 2}], [{"start": 0, "end": 2}])
        document = json.loads(plan.read_text(encoding="utf8"))
        document.update(source_video=artifact_reference(self.media_a, "synthetic source"),
            segment_leveling=audio_review, clip_gains=audio_review["clip_gains"])
        plan.write_text(json.dumps(document, ensure_ascii=False), encoding="utf8")
        with (
            patch("video_creation_pipeline.jianying_draft_handoff.validate_spoken_edit_plan"),
            patch("video_creation_pipeline.spoken_delivery.validate_spoken_edit_plan"),
        ):
            job = build_jianying_spoken_draft_job(plan, self.vectcut, delivery_request_path=request)
        self.assertEqual(job["schema"], JIANYING_SPOKEN_DRAFT_JOB_SCHEMA)
        self.assertEqual(job["segments"][0]["in_s"], 0.0)
        self.assertTrue(job["output_contract"]["candidate_only"])

    @patch(
        "video_creation_pipeline.jianying_draft_handoff._git_head",
        return_value=VECTCUT_COMMIT,
    )
    def test_full_project_folder_and_desktop_validation_are_separate(
        self, _head
    ) -> None:
        job = build_jianying_draft_job(
            self.edit_job,
            self.selection,
            self.media_map,
            self.vectcut,
        )
        job_path = self.root / "full-project-job.json"
        write_jianying_draft_job(job, job_path)

        def fake_serializer(command, **_kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "id": "timeline-fixture",
                        "fps": 30,
                        "canvas_config": {"width": 1080, "height": 1920},
                        "materials": {},
                        "tracks": [{"segments": [{"id": "a"}, {"id": "b"}]}],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        candidate_dir = self.root / "candidate-for-project"
        with patch(
            "video_creation_pipeline.jianying_draft_handoff.subprocess.run",
            side_effect=fake_serializer,
        ):
            candidate = materialize_jianying_draft(job_path, candidate_dir)
        candidate_receipt_path = candidate_dir / "jianying-draft-receipt.json"

        def fake_project_worker(command, **_kwargs):
            output = Path(command[command.index("--output-dir") + 1])
            content = Path(command[command.index("--draft-content") + 1]).read_text(
                encoding="utf-8"
            )
            timeline_id = "timeline-fixture"
            timeline = output / "Timelines" / timeline_id
            timeline.mkdir(parents=True)
            for relative in (
                "draft_content.json",
                "draft_content.json.bak",
                "template-2.tmp",
            ):
                (output / relative).write_text(content, encoding="utf-8")
            for relative in ("draft_content.json", "draft_content.json.bak"):
                (timeline / relative).write_text(content, encoding="utf-8")
            project = {
                "id": timeline_id,
                "main_timeline_id": timeline_id,
                "timelines": [{"id": timeline_id}],
            }
            for relative in ("project.json", "project.json.bak"):
                (output / "Timelines" / relative).write_text(
                    json.dumps(project), encoding="utf-8"
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        project_dir = self.root / "editable-jianying-project"
        with patch(
            "video_creation_pipeline.jianying_draft_handoff.subprocess.run",
            side_effect=fake_project_worker,
        ):
            project = materialize_jianying_project_folder(
                candidate_receipt_path, project_dir
            )
        validate_jianying_project_folder_receipt(project)
        self.assertTrue(project["full_project_folder_generated"])
        self.assertFalse(project["jianying_runtime_validated"])
        self.assertEqual(project["segment_count"], candidate["segment_count"])

        project_receipt = self.root / "editable-jianying-project.project-receipt.json"
        desktop_path = self.root / "desktop-validation.json"
        desktop = record_jianying_desktop_validation(
            project_receipt,
            desktop_path,
            jianying_version="test-fixture",
            validated_by="codex-computer-use",
            observed_segment_count=2,
            project_opened=True,
            editable_timeline_confirmed=True,
            original_audio_present=True,
            audio_sync_confirmed=True,
            project_saved_by_jianying=True,
        )
        validate_jianying_desktop_validation_receipt(desktop)
        self.assertEqual(desktop["status"], "desktop_validated_editable_candidate")

        with self.assertRaisesRegex(ContractError, "original_audio_present"):
            record_jianying_desktop_validation(
                project_receipt,
                self.root / "invalid-desktop-validation.json",
                jianying_version="test-fixture",
                validated_by="codex-computer-use",
                observed_segment_count=2,
                project_opened=True,
                editable_timeline_confirmed=True,
                original_audio_present=False,
                audio_sync_confirmed=True,
                project_saved_by_jianying=True,
            )

    @unittest.skipUnless(
        os.environ.get("VECTCUT_SOURCE_ROOT")
        and os.environ.get("VECTCUT_PYTHON_EXECUTABLE"),
        "fixed VectCut source and isolated interpreter are required",
    )
    def test_actual_fixed_vectcut_serializer_generates_candidate_payload(self) -> None:
        source_root = Path(os.environ["VECTCUT_SOURCE_ROOT"])
        interpreter = Path(os.environ["VECTCUT_PYTHON_EXECUTABLE"])
        job = build_jianying_draft_job(
            self.edit_job,
            self.selection,
            self.media_map,
            source_root,
        )
        job_path = self.root / "actual-jianying-job.json"
        write_jianying_draft_job(job, job_path)
        candidate_dir = self.root / "actual-jianying-draft"
        receipt = materialize_jianying_draft(
            job_path,
            candidate_dir,
            python_executable=interpreter,
        )
        validate_jianying_draft_receipt(receipt)
        self.assertEqual(receipt["vectcut_commit"], VECTCUT_COMMIT)
        self.assertEqual(receipt["segment_count"], 2)
        project = materialize_jianying_project_folder(
            candidate_dir / "jianying-draft-receipt.json",
            self.root / "actual-jianying-project",
            python_executable=interpreter,
        )
        validate_jianying_project_folder_receipt(project)
        self.assertTrue(project["full_project_folder_generated"])
        self.assertFalse(project["jianying_runtime_validated"])

    @unittest.skipUnless(
        os.environ.get("VECTCUT_PROJECT_SOURCE_ROOT"),
        "fixed VectCut project source is required",
    )
    def test_actual_vectcut_project_template_writer_executes(self) -> None:
        source_root = Path(os.environ["VECTCUT_PROJECT_SOURCE_ROOT"])
        draft_content = self._json(
            "actual-project-content.json",
            {
                "id": "00000000-0000-4000-8000-000000000042",
                "fps": 30,
                "canvas_config": {"width": 1080, "height": 1920},
                "materials": {},
                "tracks": [{"segments": [{"id": "fixture"}]}],
            },
        )
        output = self.root / "actual-template-project"
        worker = ROOT / "src" / "video_creation_pipeline" / "_vectcut_project_worker.py"
        result = subprocess.run(
            [
                sys.executable,
                str(worker),
                "--source-root",
                str(source_root),
                "--draft-content",
                str(draft_content),
                "--output-dir",
                str(output),
                "--profile",
                "jianying_pro_10",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / "draft_content.json").is_file())
        self.assertTrue(
            (
                output
                / "Timelines"
                / "00000000-0000-4000-8000-000000000042"
                / "draft_content.json"
            ).is_file()
        )

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
