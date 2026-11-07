from __future__ import annotations

from unittest.mock import patch

import pytest
from smart_reframe_test_support import make_evidence, make_request, make_source

from video_creation_pipeline.contract_io import (
    file_sha256,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.reframe_detectors import (
    build_smart_reframe_evidence,
    select_smart_reframe_track,
    track_smart_reframe_evidence_with_norfair,
    write_smart_reframe_evidence,
)
from video_creation_pipeline.reframe_visual_qa import (
    build_sampled_debug_schedule,
    render_supervision_debug,
    supervision_doctor,
    validate_visual_qa_receipt,
)
from video_creation_pipeline.smart_reframe import (
    build_smart_reframe_plan,
    write_smart_reframe_contract,
)


def fixture_plan(root):
    source = make_source(root)
    request = make_request(root)
    evidence = make_evidence(root, source, "talking-head-observations.json")
    plan = build_smart_reframe_plan(source, request, evidence_paths=[evidence])
    path = root / "plan.json"
    write_smart_reframe_contract(plan, path)
    return path, plan


def selected_fixture_plan(root):
    source = make_source(root)
    evidence = build_smart_reframe_evidence(
        source,
        provider={"name": "synthetic", "sample_times_s": [0.0, 0.5, 1.0, 1.5]},
        observations=[
            {
                "evidence_id": f"face-{who}-{i}",
                "kind": "face",
                "time_s": i * 0.5,
                "score": 0.9,
                "required": False,
                "source_modality": "synthetic",
                "rect": {"x": x, "y": 0.2, "width": 0.08, "height": 0.15},
            }
            for i in range(4)
            for who, x in enumerate((0.25, 0.65))
        ],
    )
    tracked = track_smart_reframe_evidence_with_norfair(evidence)
    diagnostic_path = root / "all-tracked.json"
    write_smart_reframe_evidence(tracked, diagnostic_path)
    selected = select_smart_reframe_track(tracked, "scene-0-track-1")
    selected_path = root / "selected.json"
    write_smart_reframe_evidence(selected, selected_path)
    plan = build_smart_reframe_plan(
        source, make_request(root), evidence_paths=[selected_path]
    )
    path = root / "plan.json"
    write_smart_reframe_contract(plan, path)
    return path, plan, diagnostic_path


def test_diagnostic_evidence_preserves_excluded_people_without_changing_plan(tmp_path):
    _, plan, diagnostic = selected_fixture_plan(tmp_path)
    original_hash = plan["plan_sha256"]
    selected_only = build_sampled_debug_schedule(plan)
    all_people = build_sampled_debug_schedule(plan, diagnostic)
    assert sum(r["kind"] == "face" for r in selected_only[0]["observations"]) == 1
    assert sum(r["kind"] == "face" for r in all_people[0]["observations"]) == 2
    assert plan["plan_sha256"] == original_hash
    changed = read_json_object(diagnostic, "test")
    changed["provider"]["name"] = "unrelated diagnostic"
    changed["evidence_sha256"] = payload_sha256(changed, "evidence_sha256")
    wrong = tmp_path / "wrong-diagnostic.json"
    write_smart_reframe_evidence(changed, wrong)
    with pytest.raises(ContractError, match="selected track source"):
        build_sampled_debug_schedule(plan, wrong)


def test_missing_supervision_never_silently_falls_back(tmp_path):
    with patch(
        "video_creation_pipeline.reframe_visual_qa.importlib.import_module",
        side_effect=ImportError("missing"),
    ):
        assert supervision_doctor()["status"].startswith("blocked")
        with pytest.raises(ContractError, match="Supervision debug unavailable"):
            render_supervision_debug(
                tmp_path / "missing-plan", tmp_path / "out.mp4", tmp_path / "work"
            )
    assert not (tmp_path / "work").exists()


def test_sample_schedule_is_bounded_and_source_bound(tmp_path):
    _, plan = fixture_plan(tmp_path)
    a = build_sampled_debug_schedule(plan)
    assert a == build_sampled_debug_schedule(plan)
    assert a[0]["time_s"] == 0
    assert len(a) >= 2
    plan["source_metadata"]["duration_s"] = 121
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    with pytest.raises(ContractError):
        build_sampled_debug_schedule(plan)


def test_supervision_real_ffmpeg_synthetic_and_hash_gate(tmp_path):
    if supervision_doctor()["status"] != "ready":
        pytest.skip("reviewed optional Supervision runtime/font unavailable")
    path, plan, diagnostic = selected_fixture_plan(tmp_path)
    source_hash = file_sha256(plan["inputs"]["source_media"]["path"])
    first = render_supervision_debug(
        path,
        tmp_path / "debug.mp4",
        tmp_path / "run",
        diagnostic_evidence_path=diagnostic,
    )
    validate_visual_qa_receipt(first)
    second = render_supervision_debug(
        path,
        tmp_path / "debug2.mp4",
        tmp_path / "run2",
        diagnostic_evidence_path=diagnostic,
    )
    assert first["debug_video"]["sha256"] == second["debug_video"]["sha256"]
    assert first["sampled_diagnostic"] and first["candidate_only"]
    assert first["contact_sheet"]["sha256"]
    assert source_hash == file_sha256(plan["inputs"]["source_media"]["path"])
    with pytest.raises(ContractError, match="fresh"):
        render_supervision_debug(path, tmp_path / "debug.mp4", tmp_path / "run3")
    first["samples"][0]["observed_evidence_ids"] = ["invented"]
    first["receipt_sha256"] = payload_sha256(first, "receipt_sha256")
    with pytest.raises(ContractError, match="provenance"):
        validate_visual_qa_receipt(first)
