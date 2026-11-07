"""Scene defaults and reviewed source recommendations inside existing routes."""
from copy import deepcopy
from importlib import import_module
from pathlib import Path

from .contract_io import canonical_json_sha256, file_sha256, read_json_object
from .errors import ContractError

JOBS = {
    "evidence": ("evidence_document", "hyperframes_evidence_focus", None),
    "causality": ("concept_explanation", "remotion_knowledge_animation", "relation_flow_2d"),
    "flow": ("process_sequence", "remotion_knowledge_animation", "relation_flow_2d"),
    "stages": ("process_sequence", "remotion_knowledge_animation", "relation_flow_2d"),
    "abstract_relationship": ("concept_explanation", "remotion_knowledge_animation", "relation_flow_2d"),
    "comparison": ("comparison", "remotion_knowledge_animation", None),
    "quantity": ("number_explanation", "remotion_knowledge_animation", None),
    "space": ("concept_explanation", "remotion_knowledge_animation", None),
    "queue": ("process_sequence", "remotion_knowledge_animation", "queue_flow_2d"),
    "mechanism": ("concept_explanation", "remotion_knowledge_animation", "slider_crank_3d"),
    "interface": ("real_world_action", "existing_media_broll", None),
    "person_or_emotion": ("emotional_story", "existing_media_broll", None),
    "emphasis": ("transition_or_emphasis", "remotion_kinetic_typography", None),
    "authored_performance": ("emotional_story", "existing_media_broll", None),
}
SPECIALISTS = {"complex_model_or_material": "blender", "continuous_2d_deformation": "motion_canvas",
               "precise_geometry_or_curve": "manim"}


def recommend_scene_route(visual_job: str, *, needs_depth: bool = False,
                          capability_gap: str | None = None,
                          techniques: list[str] | None = None,
                          delivery: str | None = None,
                          source_projects: list[str] | None = None,
                          narration_timing: str | None = None) -> dict:
    if visual_job not in JOBS or type(needs_depth) is not bool:
        raise ContractError("choose a supported scene understanding job and explicit depth need")
    if capability_gap is not None and capability_gap not in SPECIALISTS:
        raise ContractError("specialist engines require an identified capability gap")
    kind, route, component = JOBS[visual_job]
    specialist = SPECIALISTS.get(capability_gap)
    if visual_job in {"evidence", "interface", "person_or_emotion"} and specialist:
        raise ContractError("source evidence and people must retain their existing material route")
    if visual_job == "stages" and needs_depth and not specialist:
        component = "stage_timeline_3d"
    elif needs_depth and visual_job not in {"evidence", "mechanism"} and not specialist:
        component = None
    result = {"visual_job": visual_job, "content_kind": kind, "visual_route": route,
            "component_id": None if specialist else component,
            "requires_scene_design": (component is None and route == "remotion_knowledge_animation") or visual_job == "authored_performance",
            "specialist_handoff": specialist, "capability_gap": capability_gap,
            "new_tool_installation_performed": False, "style_authority": "existing project visual tokens",
            "source_media_required": visual_job in {"interface", "person_or_emotion"},
            "timing_authority": "existing packaging and measured word anchors"}
    if any(value is not None for value in (techniques, delivery, source_projects, narration_timing)):
        result["module_selection"] = _recommend_sources(
            result, [] if techniques is None else techniques,
            "video" if delivery is None else delivery,
            [] if source_projects is None else source_projects,
            "unverified" if narration_timing is None else narration_timing,
        )
        # A job label cannot establish that a crank, queue or relation diagram
        # matches the user's actual mechanism. Keep the old default as a candidate.
        result["base_component_candidate_id"] = result["component_id"]
        result["component_id"] = None
        if not result["source_media_required"] and visual_job != "evidence":
            result["requires_scene_design"] = True
        if delivery in {"interactive", "still"}:
            result["visual_route"] = None
            result["component_id"] = None
            result["delivery_route"] = "native_web_interaction" if delivery == "interactive" else "static_composition"
            result["timing_authority"] = "native interaction events" if delivery == "interactive" else "static composition"
    if visual_job == "authored_performance":
        result["authored_media_handoff_required"] = True
    return result


def animation_source_catalog() -> dict:
    """Read the reviewed map; source availability is checked during selection."""
    catalog = read_json_object(
        Path(__file__).with_name("policies") / "animation-source-catalog.v1.json",
        "animation source catalog",
    )
    if catalog.get("schema") != "video_creation_pipeline.animation_source_catalog.v1":
        raise ContractError("unsupported animation source catalog")
    projects = catalog.get("projects")
    techniques = catalog.get("techniques")
    if not isinstance(projects, list) or not projects or not isinstance(techniques, dict):
        raise ContractError("animation source catalog requires projects and techniques")
    ids = [item["id"] for item in projects]
    if len(ids) != len(set(ids)):
        raise ContractError("duplicate animation source project")
    for recipe in techniques.values():
        if not recipe.get("projects") or set(recipe["projects"]) - set(ids):
            raise ContractError("animation technique refers to unknown sources")
    catalog["catalog_sha256"] = canonical_json_sha256(catalog)
    return catalog


def _source_evidence(project: dict) -> dict:
    checks = []
    for source in project.get("local_evidence", []):
        path = Path(source["path"])
        try:
            state = ("verified_snapshot" if path.is_file() and file_sha256(path) == source["sha256"]
                     else "changed" if path.is_file() else "missing")
        except OSError:
            state = "unreadable"
        checks.append({"path": str(path), "state": state})
    states = {row["state"] for row in checks}
    status = ("source_changed" if "changed" in states else
              "source_missing" if states & {"missing", "unreadable"} else
              "snapshot_verified" if checks else "no_local_source_evidence")
    return {"status": status, "files": checks,
            "runtime_test_performed": False, "installed_or_callable_claimed": False}


def _recommend_sources(route: dict, techniques: list[str], delivery: str,
                       source_projects: list[str], narration_timing: str) -> dict:
    if delivery not in {"video", "interactive", "still"}:
        raise ContractError("delivery must be video, interactive or still")
    if narration_timing not in {"unverified", "sentence", "word", "none"}:
        raise ContractError("narration timing must be unverified, sentence, word or none")
    if (not isinstance(techniques, list) or not all(isinstance(x, str) for x in techniques)
            or not isinstance(source_projects, list) or not all(isinstance(x, str) for x in source_projects)):
        raise ContractError("techniques and source projects must be lists of ids")
    catalog = animation_source_catalog()
    recipes = catalog["techniques"]
    projects = {row["id"]: row for row in catalog["projects"]}
    if set(techniques) - set(recipes):
        raise ContractError("unknown animation technique: " + ", ".join(sorted(set(techniques) - set(recipes))))
    if set(source_projects) - set(projects):
        raise ContractError("unknown source project: " + ", ".join(sorted(set(source_projects) - set(projects))))
    chosen = list(dict.fromkeys(techniques))
    # A source-only query is allowed, but must not invent an expressive intention.
    ids = list(dict.fromkeys(source_projects or [p for t in chosen for p in recipes[t]["projects"]]))
    layers: dict[str, list[dict]] = {}
    source_preservation = route["visual_job"] in {"evidence", "interface", "person_or_emotion"}
    for identity in ids:
        project = projects[identity]
        item = deepcopy(project)
        evidence = _source_evidence(project)
        item["evidence_check"] = evidence
        reasons = []
        if project.get("identity_status") in {"unresolved", "provisional"}:
            reasons.append("project_identity_" + project["identity_status"])
        if evidence["status"] in {"source_changed", "source_missing"}:
            reasons.append(evidence["status"])
        if evidence["status"] == "no_local_source_evidence":
            reasons.append("local_source_or_native_api_evidence_required")
        if chosen and not any(identity in recipes[t]["projects"] for t in chosen):
            reasons.append("explicit_source_technique_fit_not_reviewed")
        if delivery not in project["deliveries"]:
            reasons.append("delivery_requires_adapter")
        if delivery == "video" and project.get("clock") == "stateful_interactive":
            reasons.append("deterministic_time_adapter_required")
        if source_preservation and project["layer"] in {"shot", "foundation"}:
            reasons.append("preserve_source_pixels_use_annotation_layer_only")
        item["matched_techniques"] = [t for t in chosen if identity in recipes[t]["projects"]]
        item["explicitly_requested"] = identity in source_projects
        item["selection_status"] = "needs_adaptation" if not reasons else "needs_resolution"
        if project.get("adoption") in {"reference_only", "unresolved"}:
            item["selection_status"] = "reference_only"
        item["required_resolutions"] = reasons
        item["execution_authorized"] = False
        layers.setdefault(project["layer"], []).append(item)
    try:
        component_catalog = import_module(".object_scenes", __package__).object_scene_component_catalog()
        component_catalog_status = "existing_source_inventory_available"
    except (ImportError, OSError, ContractError):
        # Object authoring is an optional local integration, not a prerequisite
        # for browsing upstream sources in a clean checkout.
        component_catalog = {"components": []}
        component_catalog_status = "existing_source_inventory_unavailable"
    components = [row for row in component_catalog["components"] if row["id"] == route["component_id"] and delivery == "video"]
    checkpoints = list(dict.fromkeys(c for t in chosen for c in recipes[t]["checks"]))
    return {
        "schema": "video_creation_pipeline.animation_source_selection.v1",
        "catalog_sha256": catalog["catalog_sha256"],
        "techniques": chosen, "delivery": delivery,
        "selection_basis": "explicit_semantic_choices_not_keyword_score",
        "source_selection_mode": "explicit_sources_only" if source_projects else "technique_sources",
        "status": "source_recommendations_ready" if ids else "needs_technique_or_source",
        "source_layers": layers,
        "existing_scene_components": components,
        "existing_component_catalog_status": component_catalog_status,
        "existing_component_fit": "unverified_objects_relations_and_state_model",
        "component_inventory_is_runtime_doctor": False,
        "existing_route": route["visual_route"] if delivery == "video" else None,
        "video_execution_applicable": delivery == "video",
        "existing_consumers": (deepcopy(catalog["existing_consumers"]) if delivery == "video" else {}),
        "time_binding": {
            "authority": route["timing_authority"] if delivery == "video" else "native interaction events or static composition",
            "declared_evidence": narration_timing,
            "evidence_verified_by_this_selection": False,
            "next_action": ("use_native_events_or_static_export_no_narration_required" if delivery != "video"
                            else "use_existing_packaging_scene_windows" if narration_timing == "none"
                            else "bind_and_validate_current_timebase_artifact" if narration_timing in {"word", "sentence"}
                            else "obtain_measured_audio_anchors_before_speech_synchronous_animation"),
            "estimate_from_character_count": False,
        },
        "checks": checkpoints,
        "next_steps": [
            "inspect_existing_component_and_fixed_source_module_before_authoring",
            "resolve_missing_identity_sources_and_dependencies_without_auto_install",
            "adapt_selected_source_inputs_assets_and_time_interface",
            "approve_complete_static_composition_then_test_event_boundaries_and_midpoints",
            ("bind_existing_packaging_then_use_existing_renderer_and_single_ffmpeg_outlet"
             if delivery == "video" else "use_target_native_interaction_or_static_export_and_review"),
        ],
        "authority_boundary": {"candidate_only": True, "execution_performed": False,
                               "automatic_install": False, "upload_performed": False,
                               "publish_performed": False},
    }
