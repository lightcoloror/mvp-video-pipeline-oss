"""Process invariants and native source-bound generation; no real speech claim."""
import math
from copy import deepcopy

import pytest

from author_binding_fixture import approval_args, fixture_approval
from test_object_scenes import object_case
from video_creation_pipeline.contract_io import atomic_write_json, read_json_object
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.object_process_models import queue_counts, queue_schedule, slider_crank_state
from video_creation_pipeline.object_scene_routes import recommend_scene_route
from video_creation_pipeline.object_scenes import build_object_scene_plan, component_sources, object_project_sources


def parameters():
    return {"parcel_count": 32, "slow_arrival_frames": 60, "fast_arrival_frames": 30,
            "service_frames": 48, "approach_frames": 70, "exit_travel_frames": 42,
            "fork_headway_frames": 18,
            "speed_event_id": "faster", "open_event_id": "open",
            "parcel_object_id": "stage-a", "first_exit_object_id": "stage-c", "second_exit_object_id": "stage-b"}


def test_queue_conserves_objects_and_drains_with_unchanged_fast_input():
    events = [{"id": "faster", "action": "accelerate", "startFrame": 60, "endFrame": 120},
              {"id": "open", "action": "open_exit", "startFrame": 390, "endFrame": 420}]
    parcels = queue_schedule(parameters(), events, 840)
    assert len({row["id"] for row in parcels}) == len(parcels)
    assert all(row["releaseFrame"] >= 420 for row in parcels if row["gateIndex"] == 1)
    assert all(a["releaseFrame"] + 18 <= b["releaseFrame"] for a, b in zip(parcels, parcels[1:]))
    for gate in range(2):
        jobs = [row for row in parcels if row["gateIndex"] == gate]
        assert all(a["releaseFrame"] + 48 <= b["releaseFrame"] for a, b in zip(jobs, jobs[1:]))
    for frame in range(840):
        counts = queue_counts(parcels, frame)
        assert counts["entered"] == counts["inSystem"] + counts["done"]
        assert 0 <= counts["waiting"] <= counts["inSystem"]
    assert queue_counts(parcels, 410)["waiting"] >= 3
    assert queue_counts(parcels, 780)["waiting"] == 0
    assert any(row["entryFrame"] > 780 for row in parcels)  # Input has not stopped to fake the drain.


def test_rod_constraint_and_stroke_hold_across_all_angles():
    for step in range(1441):
        state = slider_crank_state(0.78, 2.5, step * math.pi / 360)
        assert state["slider"][0] == 0
        assert math.dist(state["pin"], state["slider"]) == pytest.approx(2.5, abs=1e-12)
        assert 2.5 - 0.78 <= state["slider"][1] <= 2.5 + 0.78


def queue_case(root):
    object_case(root)
    spec = read_json_object(root / "object-spec.json", "spec")
    row = spec["scenes"][0]
    row["component_id"] = "queue_flow_2d"
    row["objects"].append({**deepcopy(row["objects"][0]), "id": "stage-c"})
    row["initial_split"] = 1
    row["parameters"] = {**parameters(), "approach_frames": 12}
    row["events"][0].update(id="faster", action="accelerate")
    row["events"][1].update(id="open", action="open_exit")
    spec["scenes"] = [row]
    spec["component_sources"] = component_sources(["queue_flow_2d"])
    atomic_write_json(spec, root / "object-spec.json")
    fixture_approval("storyboard", root / "object-spec.json", root / "storyboard-approval.json")
    return spec


def test_native_plan_projects_parameters_and_immutable_schedule(tmp_path):
    root = tmp_path / "case"
    queue_case(root)
    plan = build_object_scene_plan(root / "object-spec.json", **approval_args(root))
    scene = plan["scenes"][0]
    assert scene["process"]["parcels"]
    files = object_project_sources([scene])
    assert "src/object-scenes/QueueFlow2D.tsx" in files
    assert "<QueueFlow2D scene=" in files["src/object-scenes/ProcessObjectScene.tsx"]
    assert "SliderCrank3D.tsx" not in " ".join(files)
    assert "from './SliderCrank3D'" not in files["src/object-scenes/ProcessObjectScene.tsx"]


def test_material_colors_are_independent_of_personal_tokens_and_overrideable(tmp_path):
    root = tmp_path / "case"
    spec = queue_case(root)
    spec["visual_tokens"]["primary"] = "#B823BF"
    spec["scenes"][0]["parameters"]["colors"] = {"parcel": "#BB8844"}
    atomic_write_json(spec, root / "object-spec.json")
    fixture_approval("storyboard", root / "object-spec.json", root / "storyboard-approval.json")
    plan = build_object_scene_plan(root / "object-spec.json", **approval_args(root))
    process = plan["scenes"][0]["process"]
    assert process["colors"]["parcel"] == "#BB8844"
    assert process["colors"]["belt"] == "#626D78"
    assert "colors" not in process["parameters"]  # Keep simulation parameters scalar.


@pytest.mark.parametrize("colors", [{"unknown_material": "#123456"}, {"belt": "#ABC"},
                                     {"parcel": True}, ["#123456"]])
def test_invalid_or_ambiguous_color_roles_are_rejected(tmp_path, colors):
    root = tmp_path / "case"
    spec = queue_case(root)
    spec["scenes"][0]["parameters"]["colors"] = colors
    atomic_write_json(spec, root / "object-spec.json")
    fixture_approval("storyboard", root / "object-spec.json", root / "storyboard-approval.json")
    with pytest.raises(ContractError, match="colors"):
        build_object_scene_plan(root / "object-spec.json", **approval_args(root))


@pytest.mark.parametrize("mutation", ["extra", "bool", "speed", "role", "event", "ignored_parameters"])
def test_process_ambiguity_and_invalid_values_fail_closed(tmp_path, mutation):
    root = tmp_path / "case"
    spec = queue_case(root)
    row = spec["scenes"][0]
    if mutation == "extra": row["parameters"]["unknown_setting"] = 1
    elif mutation == "bool": row["parameters"]["service_frames"] = True
    elif mutation == "speed": row["parameters"]["fast_arrival_frames"] = 80
    elif mutation == "role": row["parameters"]["first_exit_object_id"] = "missing"
    elif mutation == "event": row["events"][1]["action"] = "connect"
    else:
        row["component_id"] = "relation_flow_2d"
        row["events"][0]["action"] = "connect"
        row["events"][1]["action"] = "split"
    spec["component_sources"] = component_sources([row["component_id"]])
    atomic_write_json(spec, root / "object-spec.json")
    fixture_approval("storyboard", root / "object-spec.json", root / "storyboard-approval.json")
    with pytest.raises(ContractError):
        build_object_scene_plan(root / "object-spec.json", **approval_args(root))


def test_new_scene_routes_use_the_existing_renderer():
    assert recommend_scene_route("queue")["component_id"] == "queue_flow_2d"
    assert recommend_scene_route("mechanism", needs_depth=True)["component_id"] == "slider_crank_3d"
    assert recommend_scene_route("mechanism")["visual_route"] == "remotion_knowledge_animation"
