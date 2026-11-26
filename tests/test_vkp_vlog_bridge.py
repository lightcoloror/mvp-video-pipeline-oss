from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.errors import ContractError  # noqa: E402
from video_creation_pipeline.vkp_vlog_bridge import (  # noqa: E402
    CONSUMER_PLAN_SCHEMA,
    EDIT_JOB_V4_SCHEMA,
    build_consumer_plan,
    validate_edit_job_v4,
)


def _job(preset: str = "highlight") -> dict:
    digest = "a" * 64
    aspect = "9:16" if preset == "highlight" else "16:9"
    return {
        "schema": EDIT_JOB_V4_SCHEMA,
        "contract_mode": "multi_source_personal_vlog",
        "source_project": {
            "project_id": "vlog-demo",
            "project_identity_sha256": "b" * 64,
            "source_paths_exported": False,
            "private_project_manifest_required_for_resolution": True,
        },
        "recipe": {
            "preset": preset,
            "recipe_sha256": "c" * 64,
            "aspect_ratio": aspect,
            "target_seconds": 75 if preset == "highlight" else 240,
        },
        "source_assets": [
            {
                "source_asset_id": "asset-photo",
                "media_type": "image",
                "source_sha256": digest,
                "private_resolver": {
                    "kind": "personal_vlog_project_manifest",
                    "project_id": "vlog-demo",
                    "source_path_exported": False,
                },
            },
            {
                "source_asset_id": "asset-video",
                "media_type": "video",
                "source_sha256": "d" * 64,
                "private_resolver": {
                    "kind": "personal_vlog_project_manifest",
                    "project_id": "vlog-demo",
                    "source_path_exported": False,
                },
            },
            {
                "source_asset_id": "asset-audio",
                "media_type": "audio",
                "source_sha256": "e" * 64,
                "private_resolver": {
                    "kind": "personal_vlog_project_manifest",
                    "project_id": "vlog-demo",
                    "source_path_exported": False,
                },
            },
        ],
        "candidate_clips": [
            {
                "clip_id": "clip-1",
                "source_asset_id": "asset-photo",
                "media_type": "image",
                "timeline_duration_seconds": 3.0,
                "hold_seconds": 3.0,
                "status": "candidate_only",
                "review_required": True,
                "audio_policy": {},
            },
            {
                "clip_id": "clip-2",
                "source_asset_id": "asset-video",
                "media_type": "video",
                "timeline_duration_seconds": 8.0,
                "source_range": {"start_seconds": 0.0, "end_seconds": 8.0},
                "status": "candidate_only",
                "review_required": True,
                "audio_policy": {"background_music_must_duck": True},
            },
            {
                "clip_id": "clip-3",
                "source_asset_id": "asset-audio",
                "media_type": "audio",
                "timeline_duration_seconds": 7.0,
                "source_range": {"start_seconds": 0.0, "end_seconds": 7.0},
                "status": "candidate_only",
                "review_required": True,
                "audio_policy": {"background_music_must_duck": True},
            },
        ],
        "readiness": {
            "status": "review_required",
            "candidate_only": True,
            "blockers": [],
        },
        "operator_boundary": {
            "bridge_executes_ffmpeg": False,
            "bridge_executes_asr": False,
            "bridge_copies_sources": False,
            "automatic_render": False,
            "automatic_upload": False,
            "automatic_publish": False,
            "source_mutation": False,
            "human_approval_required": True,
        },
        "handoff_sha256": "f" * 64,
    }


def test_v4_consumer_reuses_existing_modules_without_execution():
    plan = build_consumer_plan(_job())

    assert plan["schema"] == CONSUMER_PLAN_SCHEMA
    assert plan["status"] == "ready_for_human_review"
    assert plan["candidate_only"] is True
    assert plan["photo_layout_requests"][0]["consumer"].endswith("photo_layout")
    assert plan["smart_reframe_requests"][0]["request"]["schema"].endswith(
        "smart_reframe_request.v1"
    )
    assert plan["audio_requests"]
    assert plan["render_handoff"]["execute"] is False
    assert plan["operator_boundary"]["new_state_machine_created"] is False


def test_horizontal_recipe_preserves_geometry_until_review():
    plan = build_consumer_plan(_job("standard"))

    assert all(
        row["status"] == "preserve_source_geometry_until_human_review"
        for row in plan["smart_reframe_requests"]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda job: job["operator_boundary"].update(automatic_render=True), "unsafe operator"),
        (lambda job: job["candidate_clips"][0].update(review_required=False), "auto-confirmed"),
        (lambda job: job["candidate_clips"][1].update(source_asset_id="missing"), "unknown asset"),
        (lambda job: job["source_assets"][0].update(source_path="/private/example.jpg"), "private field"),
        (lambda job: job["candidate_clips"][0].update(hold_seconds=9.0), "photo hold"),
    ],
)
def test_v4_consumer_fails_closed_on_unsafe_contracts(mutation, message: str):
    job = copy.deepcopy(_job())
    mutation(job)

    with pytest.raises(ContractError, match=message):
        validate_edit_job_v4(job)
