from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .photo_layout import validate_safe_media_layout_plan
from .safe_media_scene_plan import validate_safe_media_scene_plan
from .semantic_motion import validate_semantic_motion_plan
from .title_cover import validate_title_cover_candidate_plan
from .visual_routing import (
    validate_content_visual_routing_plan,
    validate_visual_style_profile,
)

REMOTION_SKILL_HANDOFF_SCHEMA = "video_creation_pipeline.remotion_skill_handoff.v1"
REMOTION_SKILLS_VERSION = "4.0.513"
REMOTION_SKILLS_COMMIT = "21320596cf9008cf6ccaa6bf1a2b9f71c8f191c3"
VIDEO_SHOTCRAFT_COMMIT = "0d6f0b57f0d4d6700761644c07f7ef03c3e50234"
_SKILL_FILES = {
    "remotion-best-practices": "skills/remotion-best-practices/SKILL.md",
    "remotion-markup": "skills/remotion-markup/SKILL.md",
    "remotion-interactivity": "skills/remotion-interactivity/SKILL.md",
    "remotion-render": "skills/remotion-render/SKILL.md",
    "remotion-studio": "skills/remotion-studio/SKILL.md",
    "remotion-captions": "skills/remotion-captions/SKILL.md",
}


def remotion_skill_doctor(skills_root: str | Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "video_creation_pipeline.remotion_skill_doctor.v1",
        "status": "not_supplied",
        "network_execution_performed": False,
        "skill_execution_performed": False,
        "render_execution_performed": False,
    }
    if skills_root is None:
        return result
    try:
        result["source"] = _skills_source(Path(skills_root))
        result["status"] = "ready_for_candidate_handoff"
    except ContractError as exc:
        result.update({"status": "blocked", "error": str(exc)})
    return result


def build_remotion_skill_handoff(
    skills_root: str | Path,
    packaging_plan_path: str | Path,
    *,
    composition_id: str,
    safe_media_layout_path: str | Path | None = None,
    safe_media_scene_plan_path: str | Path | None = None,
    semantic_motion_path: str | Path | None = None,
    subtitle_path: str | Path | None = None,
    video_shotcraft_root: str | Path | None = None,
    video_shotcraft_recipes: list[dict[str, str]] | None = None,
    content_visual_routing_plan_path: str | Path | None = None,
    visual_style_profile_path: str | Path | None = None,
    title_cover_plan_path: str | Path | None = None,
) -> dict[str, Any]:
    if not composition_id.strip():
        raise ContractError("Remotion composition_id is required")
    source = _skills_source(Path(skills_root))
    inputs: dict[str, Any] = {
        "packaging_plan": artifact_reference(
            packaging_plan_path, "video packaging plan"
        )
    }
    route = [
        "remotion-best-practices",
        "remotion-markup",
        "remotion-interactivity",
        "remotion-render",
        "remotion-studio",
    ]
    if safe_media_layout_path is not None and safe_media_scene_plan_path is not None:
        raise ContractError(
            "Remotion handoff accepts either one global safe layout or one scene plan"
        )
    if safe_media_layout_path is not None:
        layout = read_json_object(safe_media_layout_path, "safe media layout plan")
        validate_safe_media_layout_plan(layout)
        inputs["safe_media_layout_plan"] = artifact_reference(
            safe_media_layout_path, "safe media layout plan"
        )
    if safe_media_scene_plan_path is not None:
        scene_plan = read_json_object(
            safe_media_scene_plan_path, "safe media scene plan"
        )
        validate_safe_media_scene_plan(scene_plan)
        if _object(scene_plan.get("source_packaging_plan")) != inputs["packaging_plan"]:
            raise ContractError(
                "Remotion scene plan must bind the handoff packaging plan"
            )
        inputs["safe_media_scene_plan"] = artifact_reference(
            safe_media_scene_plan_path, "safe media scene plan"
        )
    if semantic_motion_path is not None:
        motion = read_json_object(semantic_motion_path, "semantic motion plan")
        validate_semantic_motion_plan(motion)
        inputs["semantic_motion_plan"] = artifact_reference(
            semantic_motion_path, "semantic motion plan"
        )
    if subtitle_path is not None:
        inputs["subtitle_artifact"] = artifact_reference(
            subtitle_path, "subtitle artifact"
        )
        route.append("remotion-captions")
    _bind_visual_routing_inputs(
        inputs,
        content_visual_routing_plan_path,
        visual_style_profile_path,
    )
    if title_cover_plan_path is not None:
        title_cover = read_json_object(
            title_cover_plan_path, "title cover candidate plan"
        )
        validate_title_cover_candidate_plan(title_cover)
        inputs["title_cover_candidate_plan"] = artifact_reference(
            title_cover_plan_path, "title cover candidate plan"
        )
    shot_recipe_selection = _optional_video_shotcraft_selection(
        video_shotcraft_root,
        video_shotcraft_recipes,
    )
    payload: dict[str, Any] = {
        "schema": REMOTION_SKILL_HANDOFF_SCHEMA,
        "remotion_skills": source,
        "composition": {"id": composition_id, "candidate_only": True},
        "inputs": inputs,
        "skill_route": route,
        "handoff_constraints": {
            "preserve_input_artifact_hashes": True,
            "preserve_safe_layout_contain_policy": True,
            "no_automatic_source_media_copy": True,
            "no_automatic_crop": True,
            "no_automatic_subtitle_transcription": True,
            "single_ffmpeg_outlet_required_after_render": True,
            "technical_qa_required_after_render": True,
            "preserve_visual_route_and_style_bindings": True,
            "preserve_title_cover_candidate_bindings": True,
        },
        "authority_boundary": {
            "skill_execution_performed": False,
            "remotion_project_materialized": False,
            "render_execution_performed": False,
            "network_execution_performed": False,
            "automatic_publish": False,
        },
    }
    if shot_recipe_selection is not None:
        payload["shot_recipe_selection"] = shot_recipe_selection
    payload["handoff_sha256"] = payload_sha256(payload, "handoff_sha256")
    validate_remotion_skill_handoff(payload)
    return payload


def validate_remotion_skill_handoff(payload: dict[str, Any]) -> None:
    if payload.get("schema") != REMOTION_SKILL_HANDOFF_SCHEMA:
        raise ContractError("unsupported Remotion skill handoff schema")
    if payload.get("handoff_sha256") != payload_sha256(payload, "handoff_sha256"):
        raise ContractError("Remotion skill handoff integrity check failed")
    _validate_source(_object(payload.get("remotion_skills")))
    composition = _object(payload.get("composition"))
    if (
        not str(composition.get("id") or "").strip()
        or composition.get("candidate_only") is not True
    ):
        raise ContractError("Remotion composition is invalid")
    inputs = _object(payload.get("inputs"))
    packaging_reference = _object(inputs.get("packaging_plan"))
    validate_artifact_reference(packaging_reference, "video packaging plan")
    if "safe_media_layout_plan" in inputs:
        layout_path = validate_artifact_reference(
            _object(inputs["safe_media_layout_plan"]), "safe media layout plan"
        )
        validate_safe_media_layout_plan(
            read_json_object(layout_path, "safe media layout plan")
        )
    if "safe_media_scene_plan" in inputs:
        if "safe_media_layout_plan" in inputs:
            raise ContractError(
                "Remotion handoff cannot mix global and scene-specific layouts"
            )
        scene_plan_path = validate_artifact_reference(
            _object(inputs["safe_media_scene_plan"]), "safe media scene plan"
        )
        scene_plan = read_json_object(scene_plan_path, "safe media scene plan")
        validate_safe_media_scene_plan(scene_plan)
        if _object(scene_plan.get("source_packaging_plan")) != packaging_reference:
            raise ContractError(
                "Remotion scene plan must bind the handoff packaging plan"
            )
    if "semantic_motion_plan" in inputs:
        motion_path = validate_artifact_reference(
            _object(inputs["semantic_motion_plan"]), "semantic motion plan"
        )
        validate_semantic_motion_plan(
            read_json_object(motion_path, "semantic motion plan")
        )
    if "subtitle_artifact" in inputs:
        validate_artifact_reference(
            _object(inputs["subtitle_artifact"]), "subtitle artifact"
        )
    if "title_cover_candidate_plan" in inputs:
        title_cover_path = validate_artifact_reference(
            _object(inputs["title_cover_candidate_plan"]),
            "title cover candidate plan",
        )
        validate_title_cover_candidate_plan(
            read_json_object(title_cover_path, "title cover candidate plan")
        )
    _validate_visual_routing_inputs(inputs, packaging_reference)
    if "shot_recipe_selection" in payload:
        _validate_video_shotcraft_selection(_object(payload["shot_recipe_selection"]))
    route = payload.get("skill_route")
    required = {
        "remotion-best-practices",
        "remotion-markup",
        "remotion-interactivity",
        "remotion-render",
        "remotion-studio",
    }
    if not isinstance(route, list) or not required.issubset(route):
        raise ContractError("Remotion skill route is incomplete")
    if "subtitle_artifact" in inputs and "remotion-captions" not in route:
        raise ContractError("subtitle handoff requires the captions skill")
    constraints = _object(payload.get("handoff_constraints"))
    if any(constraints.get(key) is not True for key in constraints):
        raise ContractError("Remotion skill handoff constraints are invalid")
    boundary = _object(payload.get("authority_boundary"))
    if boundary != {
        "skill_execution_performed": False,
        "remotion_project_materialized": False,
        "render_execution_performed": False,
        "network_execution_performed": False,
        "automatic_publish": False,
    }:
        raise ContractError("Remotion skill handoff authority boundary is invalid")


def write_remotion_skill_handoff(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_remotion_skill_handoff(payload)
    return atomic_write_json(payload, output_path)


def _bind_visual_routing_inputs(
    inputs: dict[str, Any],
    routing_plan_path: str | Path | None,
    style_profile_path: str | Path | None,
) -> None:
    if routing_plan_path is None and style_profile_path is None:
        return
    if routing_plan_path is None or style_profile_path is None:
        raise ContractError(
            "Remotion visual routing plan and style profile must be supplied together"
        )
    route_path = Path(routing_plan_path).expanduser().resolve()
    style_path = Path(style_profile_path).expanduser().resolve()
    route = read_json_object(route_path, "content visual routing plan")
    style = read_json_object(style_path, "visual style profile")
    validate_content_visual_routing_plan(route)
    validate_visual_style_profile(style)
    route_inputs = _object(route.get("inputs"))
    if _object(route_inputs.get("packaging_plan")) != _object(
        inputs.get("packaging_plan")
    ):
        raise ContractError("Remotion visual routing plan must bind its packaging plan")
    style_reference = artifact_reference(style_path, "visual style profile")
    if _object(route_inputs.get("visual_style_profile")) != style_reference:
        raise ContractError("Remotion visual routing plan must bind its style profile")
    inputs["content_visual_routing_plan"] = artifact_reference(
        route_path, "content visual routing plan"
    )
    inputs["visual_style_profile"] = style_reference


def _validate_visual_routing_inputs(
    inputs: dict[str, Any], packaging_reference: dict[str, Any]
) -> None:
    route_ref = inputs.get("content_visual_routing_plan")
    style_ref = inputs.get("visual_style_profile")
    if route_ref is None and style_ref is None:
        return
    if not isinstance(route_ref, dict) or not isinstance(style_ref, dict):
        raise ContractError(
            "Remotion visual routing plan and style profile must be bound together"
        )
    route_path = validate_artifact_reference(
        _object(route_ref), "content visual routing plan"
    )
    style_path = validate_artifact_reference(_object(style_ref), "visual style profile")
    route = read_json_object(route_path, "content visual routing plan")
    style = read_json_object(style_path, "visual style profile")
    validate_content_visual_routing_plan(route)
    validate_visual_style_profile(style)
    route_inputs = _object(route.get("inputs"))
    if _object(route_inputs.get("packaging_plan")) != packaging_reference:
        raise ContractError("Remotion visual routing packaging binding drifted")
    if _object(route_inputs.get("visual_style_profile")) != _object(style_ref):
        raise ContractError("Remotion visual style binding drifted")


def _skills_source(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    package = read_json_object(
        root / "package.json", "Remotion Skills package metadata"
    )
    if (
        package.get("name") != "@remotion/skills"
        or package.get("version") != REMOTION_SKILLS_VERSION
    ):
        raise ContractError("Remotion Skills version drifted from the reviewed source")
    if _git_head(root) != REMOTION_SKILLS_COMMIT:
        raise ContractError(
            "Remotion Skills source commit drifted from the reviewed source"
        )
    files = {
        name: artifact_reference(root / relative, f"Remotion {name} skill")
        for name, relative in _SKILL_FILES.items()
    }
    return {
        "name": "@remotion/skills",
        "version": REMOTION_SKILLS_VERSION,
        "source_commit": REMOTION_SKILLS_COMMIT,
        "source_license_status": "no_root_license_file_observed_no_source_copied",
        "package_metadata": artifact_reference(
            root / "package.json", "Remotion Skills package metadata"
        ),
        "skill_files": files,
    }


def _validate_source(source: dict[str, Any]) -> None:
    if (
        source.get("name") != "@remotion/skills"
        or source.get("version") != REMOTION_SKILLS_VERSION
        or source.get("source_commit") != REMOTION_SKILLS_COMMIT
    ):
        raise ContractError("Remotion Skills source is not the reviewed runtime")
    if (
        source.get("source_license_status")
        != "no_root_license_file_observed_no_source_copied"
    ):
        raise ContractError("Remotion Skills license observation is invalid")
    package = validate_artifact_reference(
        _object(source.get("package_metadata")), "Remotion Skills package metadata"
    )
    _skills_source(package.parent)
    files = _object(source.get("skill_files"))
    if set(files) != set(_SKILL_FILES):
        raise ContractError("Remotion Skills file manifest drifted")
    for name, reference in files.items():
        validate_artifact_reference(_object(reference), f"Remotion {name} skill")


def _optional_video_shotcraft_selection(
    source_root: str | Path | None,
    requests: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    if source_root is None and requests is None:
        return None
    if source_root is None or requests is None:
        raise ContractError(
            "Video Shotcraft source root and recipe requests must be supplied together"
        )
    if not isinstance(requests, list) or not requests:
        raise ContractError("Video Shotcraft requires at least one recipe request")
    return _build_video_shotcraft_selection(Path(source_root), requests)


def _build_video_shotcraft_selection(
    root: Path,
    requests: list[dict[str, str]],
) -> dict[str, Any]:
    source = _video_shotcraft_source(root)
    root = Path(source["source_root"]).resolve()
    library = read_json_object(root / "gallery/api/library.json", "shot recipe library")
    cards = library.get("cards")
    if not isinstance(cards, list) or not cards:
        raise ContractError("Video Shotcraft recipe library has no cards")
    by_name = {
        str(card.get("name") or ""): card
        for card in cards
        if isinstance(card, dict) and str(card.get("name") or "")
    }
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in requests:
        request = _object(raw)
        if set(request) != {"name", "style_key"}:
            raise ContractError(
                "Video Shotcraft recipe requests require only name and style_key"
            )
        name = str(request.get("name") or "").strip()
        style_key = str(request.get("style_key") or "").strip()
        card = by_name.get(name)
        if card is None:
            raise ContractError(f"unknown Video Shotcraft recipe: {name}")
        styles = card.get("styles")
        if not isinstance(styles, list):
            raise ContractError(f"Video Shotcraft recipe has no styles: {name}")
        style = next(
            (
                item
                for item in styles
                if isinstance(item, dict) and item.get("key") == style_key
            ),
            None,
        )
        if style is None:
            raise ContractError(
                f"unknown Video Shotcraft style for {name}: {style_key}"
            )
        identity = (name, style_key)
        if identity in seen:
            raise ContractError(
                f"duplicate Video Shotcraft recipe selection: {name}/{style_key}"
            )
        seen.add(identity)
        card_path = _safe_source_file(root, card.get("source"), f"{name} recipe card")
        implementation = _safe_source_file(
            root,
            _reference_implementation(card_path),
            f"{name} reference implementation",
        )
        selected.append(
            {
                "name": name,
                "style_key": style_key,
                "category": str(card.get("category") or "uncategorized"),
                "summary": str(card.get("summary") or ""),
                "source_card": artifact_reference(card_path, f"{name} recipe card"),
                "reference_implementation": artifact_reference(
                    implementation, f"{name} reference implementation"
                ),
            }
        )
    return {
        "source": source,
        "recipes": selected,
        "execution_policy": {
            "recipe_reference_only": True,
            "reuse_existing_remotion_runtime": True,
            "read_full_card_and_reference_implementation": True,
            "preserve_existing_safe_layout_and_evidence": True,
            "upstream_template_copy_forbidden": True,
            "upstream_audio_direct_copy_forbidden": True,
            "licensed_asset_ingestion_required": True,
            "single_ffmpeg_outlet_required": True,
        },
    }


def _video_shotcraft_source(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if _git_head(root) != VIDEO_SHOTCRAFT_COMMIT:
        raise ContractError(
            "Video Shotcraft source commit drifted from the reviewed source"
        )
    skill = _safe_source_file(root, "SKILL.md", "Video Shotcraft skill")
    library_path = _safe_source_file(
        root, "gallery/api/library.json", "Video Shotcraft recipe library"
    )
    license_path = _safe_source_file(root, "LICENSE", "Video Shotcraft license")
    license_text = license_path.read_text(encoding="utf-8", errors="replace")
    if "Apache License" not in license_text or "Version 2.0" not in license_text:
        raise ContractError("Video Shotcraft Apache-2.0 license evidence is invalid")
    library = read_json_object(library_path, "Video Shotcraft recipe library")
    cards = library.get("cards")
    stats = library.get("stats")
    if (
        not isinstance(cards, list)
        or not isinstance(stats, dict)
        or stats.get("cardCount") != len(cards)
    ):
        raise ContractError("Video Shotcraft recipe library metadata is inconsistent")
    return {
        "name": "video-shotcraft",
        "source_root": str(root),
        "source_commit": VIDEO_SHOTCRAFT_COMMIT,
        "source_license": "Apache-2.0",
        "skill_file": artifact_reference(skill, "Video Shotcraft skill"),
        "library_index": artifact_reference(
            library_path, "Video Shotcraft recipe library"
        ),
        "license_file": artifact_reference(license_path, "Video Shotcraft license"),
    }


def _validate_video_shotcraft_selection(selection: dict[str, Any]) -> None:
    source = _object(selection.get("source"))
    if (
        source.get("name") != "video-shotcraft"
        or source.get("source_commit") != VIDEO_SHOTCRAFT_COMMIT
        or source.get("source_license") != "Apache-2.0"
    ):
        raise ContractError("Video Shotcraft source identity is invalid")
    root = Path(str(source.get("source_root") or "")).expanduser().resolve()
    current = _video_shotcraft_source(root)
    if source != current:
        raise ContractError("Video Shotcraft source evidence drifted")
    recipes = selection.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        raise ContractError("Video Shotcraft selection requires recipes")
    requests = [
        {
            "name": str(_object(recipe).get("name") or ""),
            "style_key": str(_object(recipe).get("style_key") or ""),
        }
        for recipe in recipes
    ]
    expected = _build_video_shotcraft_selection(root, requests)
    if selection != expected:
        raise ContractError("Video Shotcraft recipe selection drifted")


def _safe_source_file(root: Path, relative: Any, label: str) -> Path:
    value = str(relative or "").strip().strip("`")
    if not value:
        raise ContractError(f"{label} path is missing")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"{label} escapes the reviewed source root") from exc
    if not candidate.is_file():
        raise ContractError(f"{label} was not found")
    return candidate


def _reference_implementation(card_path: Path) -> str:
    lines = card_path.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        index = lines.index("## 参考实现")
    except ValueError as exc:
        raise ContractError(
            "Video Shotcraft card has no reference implementation"
        ) from exc
    values: list[str] = []
    for line in lines[index + 1 :]:
        value = line.strip()
        if value.startswith("## "):
            break
        if value:
            values.append(value)
    if values:
        first = values[0].strip("`")
        if "（" in first:
            first = first.split("（", 1)[0].strip()
        if first.endswith("/") and len(values) > 1:
            filename = values[1].strip().strip("`()（）")
            if filename:
                return f"{first}{filename}"
        return first
    raise ContractError("Video Shotcraft reference implementation path is missing")


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("Remotion Skills source commit could not be read") from exc
    if result.returncode != 0:
        raise ContractError("Remotion Skills source commit could not be read")
    return result.stdout.strip().lower()


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value
