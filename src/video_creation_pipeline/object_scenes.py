"""Object-level scenes using the existing author timebase and Remotion outlet."""
from __future__ import annotations

import re
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from typing import Any

from .author_bindings import _number, _object, load_author_timebase_context
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .object_process_models import PROCESS_ACTIONS, PROCESS_COMPONENTS, resolve_process
from .temporal_expressions import resolve_window

SPEC_SCHEMA = "video_creation_pipeline.object_scene_spec.v1"
PLAN_SCHEMA = "video_creation_pipeline.object_scene_plan.v1"
COMPONENTS = {"relation_flow_2d": "ObjectFlow2D.tsx", "stage_timeline_3d": "StageTimeline3D.tsx",
              "queue_flow_2d": "QueueFlow2D.tsx", "slider_crank_3d": "SliderCrank3D.tsx"}
THREE_PACKAGES = {"@remotion/three": "4.0.477", "@react-three/fiber": "9.2.0", "three": "0.178.0", "@types/three": "0.170.0"}
SOURCE_ROOT = Path(__file__).parent / "scene_components"
SHARED_FILES = ("ObjectScene.tsx", "ObjectFlow2D.tsx", "object-motion.ts", "onda-easing.ts", "ONDA_LICENSE.txt")
ACTIONS = {"reveal", "connect", "split", "advance", "focus"}


def component_sources(component_ids: list[str]) -> dict[str, dict]:
    if not component_ids or any(value not in COMPONENTS for value in component_ids):
        raise ContractError("unsupported object scene component")
    names = set(SHARED_FILES) | {COMPONENTS[value] for value in component_ids}
    if any(value in PROCESS_COMPONENTS for value in component_ids):
        names.update({"ProcessObjectScene.tsx", "process-motion.ts"})
    sources = {name: artifact_reference(SOURCE_ROOT / name, "object scene component source") for name in sorted(names)}
    if any(value in PROCESS_COMPONENTS for value in component_ids):
        sources["object_process_models.py"] = artifact_reference(SOURCE_ROOT.parent / "object_process_models.py", "deterministic authoring model source")
    return sources


def object_scene_component_catalog() -> dict:
    return {"schema": "video_creation_pipeline.object_scene_components.v1",
            "inventory_role": "first_party_scene_sources_not_runtime_doctor",
            "components": [{"id": identity, "entry_file": entry,
                "renderer_route": "remotion_knowledge_animation",
                "maximum_objects": 2 if identity == "stage_timeline_3d" else (3 if identity == "queue_flow_2d" else 4),
                "sources": component_sources([identity])} for identity, entry in COMPONENTS.items()],
            "installed_or_rendered_claimed_by_inventory": False}


def _text(value: Any, label: str, maximum: int = 48) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError(f"{label} must be nonempty text up to {maximum} characters")
    return value


def _rect(value: Any) -> dict:
    rect = _object(value, "object scene region")
    if set(rect) != {"x", "y", "width", "height"}:
        raise ContractError("object scene region requires x/y/width/height")
    values = {name: _number(rect[name], name) for name in rect}
    if (min(values.values()) < 0 or values["width"] <= 0 or values["height"] <= 0
            or values["x"] + values["width"] > 1 or values["y"] + values["height"] > 1):
        raise ContractError("object scene region exceeds the canvas")
    return values


def build_object_scene_plan(spec_path: str | Path, *, script_approval_path: str | Path,
                            voice_approval_path: str | Path, storyboard_approval_path: str | Path) -> dict:
    context = load_author_timebase_context(spec_path, script_approval_path=script_approval_path,
        voice_approval_path=voice_approval_path, storyboard_approval_path=storyboard_approval_path,
        expected_schema=SPEC_SCHEMA, source_label="object scene spec", require_asset=False)
    spec, unit = context["spec"], context["unit"]
    output = deepcopy(spec["output"])
    if any(type(output[key]) is not int or output[key] <= 0 for key in ("width", "height", "fps")):
        raise ContractError("object scene output must contain positive integer dimensions and fps")
    if output["width"] != 1080 or output["height"] != 1920:
        raise ContractError("object scenes v1 use the reviewed 1080x1920 portrait canvas")
    fps = output["fps"]
    palette = _object(spec.get("visual_tokens"), "object scene visual tokens")
    if set(palette) != {"background", "foreground", "primary", "accent", "muted", "bodyFont"}:
        raise ContractError("object scenes require explicit project visual tokens")
    for name, value in palette.items():
        if name != "bodyFont" and (not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value)):
            raise ContractError("object scene colors must be explicit hexadecimal tokens")
    _text(palette["bodyFont"], "local font stack", 200)
    narration = unit["narration"]
    entries = spec.get("scenes")
    if not isinstance(entries, list) or not entries:
        raise ContractError("object scene spec needs scenes")
    sources = component_sources([row.get("component_id") for row in entries if isinstance(row, dict)])
    if spec.get("component_sources") != sources:
        raise ContractError("object scene sources differ from the reviewed spec")
    scenes, ids = [], set()
    for raw in entries:
        row = _object(raw, "object scene")
        scene_id = _text(row.get("id"), "object scene ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", scene_id) or scene_id in ids:
            raise ContractError("object scene IDs must be unique path-safe identities")
        ids.add(scene_id)
        for field in ("understanding_target", "relationship", "initial_state", "final_state", "camera_purpose"):
            _text(row.get(field), field, 240)
        if row.get("visual_role") not in {"illustration", "metaphor"}:
            raise ContractError("object scenes must distinguish illustration/metaphor from real evidence")
        headline = _text(row.get("headline"), "headline", 20)
        if headline not in narration:
            raise ContractError("object scene headline must occur in the frozen narration")
        objects = row.get("objects")
        if not isinstance(objects, list) or not 2 <= len(objects) <= 4:
            raise ContractError("object scene needs two to four readable objects")
        if row['component_id'] == 'stage_timeline_3d' and len(objects) != 2:
            raise ContractError("the v1 Three stage component supports exactly two stages")
        object_ids = set()
        for value in objects:
            node = _object(value, "scene object")
            identity = _text(node.get("id"), "object ID")
            if identity in object_ids:
                raise ContractError("scene object IDs must be unique")
            object_ids.add(identity)
            maximum_label = 4 if len(objects) == 4 else (6 if len(objects) == 3 else 8)
            label = _text(node.get("label"), "object label", maximum_label)
            quote = _text(node.get("source_quote"), "object source quote", 1000)
            if quote not in narration or label not in quote:
                raise ContractError("object text must match its exact frozen source quote")
            refs = node.get("evidence_refs")
            if not isinstance(refs, list) or not refs or any(ref not in unit["evidence_refs"] for ref in refs):
                raise ContractError("object evidence must belong to the source scene")
            if node.get("amount_label") is not None:
                if node.get("amount_role") not in {"actual_payment", "invoice_item", "claim_payment"}:
                    raise ContractError("a numeric amount must explicitly identify its financial role")
                if _text(node["amount_label"], "amount label", maximum_label) not in quote:
                    raise ContractError("amount label must retain the frozen payment wording")
        origin_label = row.get("origin_label")
        if origin_label is not None and _text(origin_label, "origin label", 8) not in narration:
            raise ContractError("origin label must occur in the frozen narration")
        initial_split = _number(row.get("initial_split", 0), "initial split")
        initial_active = row.get("initial_active_ids", [])
        if not 0 <= initial_split <= 1 or not isinstance(initial_active, list) or any(node not in object_ids for node in initial_active):
            raise ContractError("initial layout must use bounded split and existing object IDs")
        if len(objects) > 2 and initial_split != 1:
            raise ContractError("three or four objects require the expanded layout for readable labels")
        window = resolve_window(row.get("temporal_expression"), context["words"], fps=fps,
            recording_start_s=context["recording_start"], recording_end_s=context["recording_end"])
        region = _rect(row.get("region"))
        # v1 reserves the existing subtitle/disclaimer/UI areas; cannot spill into them.
        if (region["x"] < 0.09 or region["x"] + region["width"] > 0.78
                or region["y"] < 0.18 or region["y"] + region["height"] > 0.64):
            raise ContractError("object region collides with the reviewed portrait protected areas")
        events = row.get("events")
        if not isinstance(events, list) or not events:
            raise ContractError("object scene needs explicit word-bound actions")
        projected, event_ids = [], set()
        for value in events:
            event = _object(value, "object action")
            event_id = _text(event.get("id"), "action ID")
            allowed_actions = PROCESS_ACTIONS.get(row["component_id"], ACTIONS)
            if event_id in event_ids or event.get("action") not in allowed_actions or event.get("target_id") not in object_ids:
                raise ContractError("object action has duplicate ID, unknown action or target")
            event_ids.add(event_id)
            action = resolve_window(event.get("temporal_expression"), context["words"], fps=fps,
                recording_start_s=context["recording_start"], recording_end_s=context["recording_end"])
            if action["start_frame"] < window["start_frame"] or action["end_frame"] > window["end_frame"]:
                raise ContractError("object action exceeds its scene window")
            if action["end_frame"] == window["end_frame"]:
                raise ContractError("object action needs a visible held frame after its payoff")
            projected.append({"id": event_id, "action": event["action"], "targetId": event["target_id"],
                "startFrame": action["start_frame"] - window["start_frame"],
                "endFrame": action["end_frame"] - window["start_frame"],
                "payoffFrame": action["end_frame"] - window["start_frame"],
                "sourceExpression": deepcopy(event["temporal_expression"])})
        process = {}
        if row["component_id"] in PROCESS_COMPONENTS:
            process["process"] = resolve_process(row["component_id"], row.get("parameters"), projected,
                object_ids, window["end_frame"] - window["start_frame"])
        elif "parameters" in row:
            raise ContractError("legacy object components do not consume process parameters")
        scenes.append({**process, "id": scene_id, "sceneId": context["scene_id"], "componentId": row["component_id"],
            "startFrame": window["start_frame"], "durationInFrames": window["end_frame"] - window["start_frame"],
            "headline": headline, "originLabel": origin_label, "initialSplit": initial_split,
            "initialActiveIds": deepcopy(initial_active), "objects": deepcopy(objects), "events": projected, "region": region, "palette": deepcopy(palette),
            "understandingTarget": row["understanding_target"], "visualRole": row["visual_role"],
            "relationship": row["relationship"], "initialState": row["initial_state"], "finalState": row["final_state"],
            "cameraPurpose": row["camera_purpose"], "sourceWindow": window})
    ordered = sorted(scenes, key=lambda row: row["startFrame"])
    if any(a["startFrame"] + a["durationInFrames"] > b["startFrame"] for a, b in pairwise(ordered)):
        raise ContractError("primary object scene windows must not overlap")
    plan = {"schema": PLAN_SCHEMA, "candidate_only": True, "evidence_mode": context["mode"],
        "synthetic_fixture_only": context["mode"] == "synthetic_fixture",
        "source_spec": artifact_reference(spec_path, "object scene spec"),
        "source_packaging_plan": spec["source_packaging_plan"],
        "source_recording": spec["source_recording"], "source_word_alignment": spec["source_word_alignment"],
        "source_visual_production_plan": spec["source_visual_production_plan"],
        "approvals": {role: artifact_reference(path, role + " approval") for role, path in
            (("script", script_approval_path), ("voice", voice_approval_path), ("storyboard", storyboard_approval_path))},
        "output": output, "segment": {"scene_id": context["scene_id"],
            "program_start_s": context["program_start"], "program_end_s": context["program_end"],
            "recording_start_s": context["recording_start"], "recording_end_s": context["recording_end"], "playback_rate": 1},
        "scenes": ordered, "component_sources": sources,
        "authority_boundary": {"rendered": False, "timing_measurement_performed": False, "automatic_approval": False, "published": False}}
    plan["artifact_sha256"] = payload_sha256(plan, "artifact_sha256")
    return plan


def validate_object_scene_plan(plan: dict) -> None:
    if plan.get("schema") != PLAN_SCHEMA or plan.get("artifact_sha256") != payload_sha256(plan, "artifact_sha256"):
        raise ContractError("object scene plan schema or integrity is invalid")
    spec_path = validate_artifact_reference(plan["source_spec"], "object scene spec")
    paths = {role: validate_artifact_reference(plan["approvals"][role], role + " approval") for role in ("script", "voice", "storyboard")}
    rebuilt = build_object_scene_plan(spec_path, **{role + "_approval_path": path for role, path in paths.items()})
    if rebuilt != plan:
        raise ContractError("object scene plan drifted from its reviewed sources")


def object_scene_projection(inputs: dict, packaging: dict) -> list[dict]:
    if "object_scene_plan" not in inputs:
        return []
    path = validate_artifact_reference(inputs["object_scene_plan"], "object scene plan")
    plan = read_json_object(path, "object scene plan")
    validate_object_scene_plan(plan)
    if plan["source_packaging_plan"] != inputs["packaging_plan"] or plan["output"] != packaging["output"]:
        raise ContractError("object scenes must bind the exact Remotion packaging and output")
    return [{**deepcopy(row), "startFrame": row["startFrame"] + round(plan["segment"]["program_start_s"] * plan["output"]["fps"]),
             "syntheticFixtureOnly": plan["synthetic_fixture_only"]} for row in plan["scenes"]]


def object_project_sources(rows: list[dict]) -> dict[str, str]:
    if not rows:
        return {}
    sources = component_sources([row["componentId"] for row in rows])
    files = {"src/object-scenes/" + name: Path(ref["path"]).read_text(encoding="utf-8") for name, ref in sources.items()}
    if any(row["componentId"] == "stage_timeline_3d" for row in rows):
        key = "src/object-scenes/ObjectScene.tsx"
        files[key] = files[key].replace("// THREE_IMPORT", "import {StageTimeline3D} from './StageTimeline3D';").replace(
            "/* THREE_RENDER */ null", "<StageTimeline3D scene={scene} frame={frame} palette={palette}/>")
    if any(row["componentId"] in PROCESS_COMPONENTS for row in rows):
        key = "src/object-scenes/ProcessObjectScene.tsx"
        for component, name in (("queue_flow_2d", "QueueFlow2D"), ("slider_crank_3d", "SliderCrank3D")):
            if any(row["componentId"] == component for row in rows):
                files[key] = files[key].replace("// " + name.upper() + "_IMPORT", f"import {{{name}}} from './{name}';")
                files[key] = files[key].replace("/* " + name.upper() + "_RENDER */ null", f"<{name} scene={{scene}} frame={{frame}} palette={{palette}}/>")
    return files


def write_object_scene_plan(plan: dict, path: str | Path) -> Path:
    validate_object_scene_plan(plan)
    path = Path(path)
    if path.exists() and read_json_object(path, "existing object scene plan") != plan:
        raise ContractError("object scene output differs; choose a new revision path")
    return atomic_write_json(plan, path)
