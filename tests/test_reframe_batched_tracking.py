from __future__ import annotations

import copy
import random

import pytest

from video_creation_pipeline.contract_io import payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.reframe_detectors import (
    build_smart_reframe_evidence,
    select_smart_reframe_track,
    track_smart_reframe_evidence_with_norfair,
    validate_smart_reframe_evidence,
)
from video_creation_pipeline.reframe_tracking import (
    optional_norfair_track_centers,
    track_sampled_subjects,
)


def observation(index, who="a", *, kind="face", time=None):
    return {
        "evidence_id": f"{who}-{index}",
        "time_s": index * 0.5 if time is None else time,
        "kind": kind,
        "required": True,
        "score": 0.9,
        "rect": {
            "x": (0.2 if who == "a" else 0.4) + min(index, 40) * 0.002,
            "y": 0.2,
            "width": 0.09,
            "height": 0.15,
        },
        "source_modality": "synthetic_fixture",
    }


def test_same_frame_people_remain_distinct_and_order_invariant():
    rows = [
        observation(i, who)
        for i in range(40)
        for who in ("a", "b")
        if not (who == "a" and 10 <= i <= 13)
    ]
    first = track_sampled_subjects(rows, sample_times_s=[i * 0.5 for i in range(40)])
    legacy = optional_norfair_track_centers(rows)
    collisions = sum(
        len(ids) != len(set(ids))
        for i in range(40)
        for ids in [[r["track_id"] for r in legacy if r["time_s"] == i * 0.5]]
    )
    assert collisions == 36
    random.Random(12).shuffle(rows)
    second = track_sampled_subjects(rows, sample_times_s=[i * 0.5 for i in range(40)])
    assert first == second
    for i in range(40):
        ids = [r["track_id"] for r in first if r["time_s"] == i * 0.5]
        assert len(ids) == len(set(ids))
    assert len({r["track_id"] for r in first if r["evidence_id"].startswith("b")}) == 1


def test_sparse_2hz_samples_do_not_age_at_30fps():
    rows = track_sampled_subjects([observation(i) for i in range(20)])
    assert len({r["track_id"] for r in rows}) == 1


def test_explicit_empty_samples_age_tracks_but_create_no_ghosts():
    source = [observation(0), observation(12)]
    rows = track_sampled_subjects(source, sample_times_s=[i * 0.5 for i in range(13)])
    assert len(rows) == 2
    assert rows[0]["track_id"] != rows[1]["track_id"]


def test_legacy_long_gap_resets_without_inventing_samples():
    rows = track_sampled_subjects([observation(0), observation(1, time=9)])
    assert rows[0]["track_id"] != rows[1]["track_id"]


def test_scene_reset_and_no_face_person_identity_inference():
    rows = track_sampled_subjects(
        [
            observation(0),
            observation(1),
            observation(2),
            observation(2, "p", kind="person"),
        ],
        scene_cuts_s=[1.0],
    )
    assert rows[0]["track_id"] == rows[1]["track_id"]
    assert rows[1]["track_id"] != rows[2]["track_id"]
    assert rows[2]["track_id"] != rows[3]["track_id"]
    assert all(r["identity_claimed"] is False for r in rows)


@pytest.mark.parametrize("times", [[0, 0], [0.5, 0], [float("nan")], [-1], [0.5]])
def test_bad_sample_clock_rejected(times):
    with pytest.raises(ContractError):
        track_sampled_subjects([observation(0)], sample_times_s=times)


def test_v1_evidence_preserves_boxes_and_non_subjects(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-contract-only")
    raw = build_smart_reframe_evidence(
        source,
        provider={"name": "fixture"},
        observations=[
            observation(0),
            observation(1),
            observation(0, "ocr", kind="ocr_text"),
            observation(0, "hint", kind="user_hint"),
        ],
    )
    tracked = track_smart_reframe_evidence_with_norfair(raw)
    assert tracked["provider"]["tracking"]["sampling_coverage"] == "unknown_legacy_v1"
    for old, new in zip(raw["observations"], tracked["observations"]):
        assert new["rect"] == old["rect"]
        assert "estimated_rect" not in new
        if old["kind"] != "face":
            assert old == new
    assert tracked == track_smart_reframe_evidence_with_norfair(raw)
    selected = select_smart_reframe_track(
        tracked,
        next(r["track_id"] for r in tracked["observations"] if r["kind"] == "face"),
    )
    with pytest.raises(ContractError, match="retrack raw"):
        track_smart_reframe_evidence_with_norfair(selected)
    bad = copy.deepcopy(raw)
    bad["observations"][0]["score"] = 0.8
    with pytest.raises(ContractError, match="integrity"):
        track_smart_reframe_evidence_with_norfair(bad)


def test_sampling_metadata_validated_and_scene_source_bound(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    raw = build_smart_reframe_evidence(
        source,
        provider={"name": "fixture", "sample_times_s": [0, 0.5, 1.0]},
        observations=[observation(0), observation(2)],
    )
    assert (
        track_smart_reframe_evidence_with_norfair(raw)["provider"]["tracking"][
            "sampling_coverage"
        ]
        == "known"
    )
    bad = copy.deepcopy(raw)
    bad["provider"]["sample_times_s"] = [0]
    bad["evidence_sha256"] = payload_sha256(bad, "evidence_sha256")
    with pytest.raises(ContractError, match="omits"):
        validate_smart_reframe_evidence(bad)
    other = tmp_path / "other.mp4"
    other.write_bytes(b"other")
    scene = build_smart_reframe_evidence(
        other, provider={"name": "scene"}, observations=[]
    )
    with pytest.raises(ContractError, match="source hash"):
        track_smart_reframe_evidence_with_norfair(raw, scene_evidence=scene)


def test_scene_boundaries_propagate_and_role_mappings_are_not_reassigned(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic")
    raw = build_smart_reframe_evidence(
        source,
        provider={"name": "fixture"},
        observations=[observation(0), observation(2)],
    )
    scenes = build_smart_reframe_evidence(
        source,
        provider={"name": "scene"},
        observations=[
            {
                "evidence_id": "cut",
                "kind": "shot_boundary",
                "time_s": 1.0,
                "start_s": 1.0,
                "end_s": 2.0,
                "required": True,
                "score": 1.0,
                "rect": None,
                "source_modality": "synthetic",
            }
        ],
    )
    result = track_smart_reframe_evidence_with_norfair(raw, scene_evidence=scenes)
    assert any(r["kind"] == "shot_boundary" for r in result["observations"])
    faces = [r for r in result["observations"] if r["kind"] == "face"]
    assert faces[0]["track_id"] != faces[1]["track_id"]
    row = observation(0)
    row["speaker_id"] = "protected-role"
    mapped = build_smart_reframe_evidence(
        source, provider={"name": "fixture"}, observations=[row]
    )
    with pytest.raises(ContractError, match="mapping roles"):
        track_smart_reframe_evidence_with_norfair(mapped)
