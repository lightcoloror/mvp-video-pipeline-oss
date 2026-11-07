from __future__ import annotations

import html
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .content_video_pipeline import VIDEO_PACKAGING_PLAN_SCHEMA
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    execute_ffmpeg_with_receipt,
    validate_ffmpeg_execution_receipt,
)
from .production_strategy import validate_visual_production_plan
from .technical_qa import (
    build_technical_qa_report,
    validate_technical_qa_report,
    write_technical_qa_report,
)
from .visual_routing import (
    validate_content_visual_routing_plan,
    validate_visual_style_profile,
)

SEMANTIC_MOTION_PLAN_SCHEMA = "video_creation_pipeline.semantic_motion_plan.v1"
SEMANTIC_MOTION_RUN_RECEIPT_SCHEMA = (
    "video_creation_pipeline.semantic_motion_run_receipt.v1"
)
HYPERFRAMES_VERSION = "0.7.109"
HYPERFRAMES_SOURCE_COMMIT = "67edb01bf4aa2f5931e838e46e14e0f51a5809ee"
HYPERFRAMES_CLI_RELATIVE = Path("packages/cli/bin/hyperframes.mjs")
ALLOWED_RECIPES = {
    "full_to_focus",
    "dim_context",
    "box_highlight",
    "marker_highlight",
    "leader_line_label",
    "focus_to_full",
    "focus_to_next",
}
INFORMATION_ROLES = {
    "structure",
    "relationship",
    "sequence",
    "comparison",
    "number",
    "evidence",
}
PRESENTATION_MODES = {"fullscreen_evidence", "person_with_overlay"}
LAYOUT_MODES = {
    "single_focus",
    "fullscreen_evidence",
    "vertical_list",
    "horizontal_row",
    "grid",
    "flow",
    "comparison",
}
REQUIRED_PEER_LAYOUTS = {"vertical_list", "horizontal_row", "grid"}
PREFERRED_PERSON_WINDOW_CORNERS = {"top_left", "top_right"}
EXCEPTION_PERSON_WINDOW_CORNERS = {"bottom_left", "bottom_right"}
PERSON_WINDOW_CORNERS = (
    PREFERRED_PERSON_WINDOW_CORNERS | EXCEPTION_PERSON_WINDOW_CORNERS
)
HUMAN_REVIEW_DIMENSIONS = {
    "primary_information",
    "space_yield",
    "visual_balance",
}


def build_semantic_motion_plan(
    packaging_plan_path: str | Path,
    script_anchor_path: str | Path,
    source_asset_path: str | Path,
    visual_production_plan_path: str | Path,
    *,
    asset_id: str,
    media_type: str,
    cues: list[dict[str, Any]],
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    duration_s: float | None = None,
    evidence_status: str = "verified",
    authorization_status: str = "authorized_local_candidate",
    deidentified: bool = True,
    content_visual_routing_plan_path: str | Path | None = None,
    visual_style_profile_path: str | Path | None = None,
    privacy_exclusion_zones_path: str | Path | None = None,
    smart_reframe_plan_path: str | Path | None = None,
) -> dict[str, Any]:
    packaging_path = Path(packaging_plan_path).expanduser().resolve()
    script_path = Path(script_anchor_path).expanduser().resolve()
    asset_path = Path(source_asset_path).expanduser().resolve()
    visual_plan_path = Path(visual_production_plan_path).expanduser().resolve()
    packaging = read_json_object(packaging_path, "video packaging plan")
    script = read_json_object(script_path, "script anchor artifact")
    visual_plan = read_json_object(visual_plan_path, "visual production plan")
    _validate_packaging_plan(packaging)
    validate_visual_production_plan(visual_plan)
    routing_binding = _optional_visual_routing_binding(
        packaging_path,
        content_visual_routing_plan_path,
        visual_style_profile_path,
    )
    privacy_binding = _optional_privacy_binding(
        privacy_exclusion_zones_path, smart_reframe_plan_path
    )
    anchors = _script_anchors(script)
    if not cues:
        raise ContractError("semantic motion plan requires at least one cue")
    if duration_s is None:
        duration_s = max(
            _number(row.get("timeline_start_s"), "cue timeline_start_s")
            + _cue_duration(row)
            for row in cues
        )

    normalized_cues = []
    for raw in cues:
        cue = dict(raw)
        story_ref = str(cue.get("story_segment_ref") or "").strip()
        anchor = anchors.get(story_ref)
        if anchor is None:
            raise ContractError(f"script anchor is missing for {story_ref or 'cue'}")
        cue["script_anchor"] = dict(anchor)
        cue["source_asset_id"] = str(asset_id)
        cue["source_asset_sha256"] = file_sha256(asset_path)
        cue.setdefault("full_frame_first", True)
        cue.setdefault("information_role", "evidence")
        cue.setdefault("presentation_mode", "fullscreen_evidence")
        cue.setdefault("reduced_motion_fallback", "static_focus_with_label")
        cue.setdefault("static_fallback", "full_frame_with_box_and_label")
        if privacy_binding is not None:
            _apply_privacy_regions_to_cue(cue, privacy_binding["zones"])
        normalized_cues.append(cue)

    plan: dict[str, Any] = {
        "schema": SEMANTIC_MOTION_PLAN_SCHEMA,
        "source_packaging_plan": artifact_reference(
            packaging_path, "video packaging plan"
        ),
        "source_script_anchor": artifact_reference(
            script_path, "script anchor artifact"
        ),
        "source_asset": artifact_reference(asset_path, "semantic motion asset"),
        "source_visual_production_plan": artifact_reference(
            visual_plan_path, "visual production plan"
        ),
        "asset": {
            "asset_id": str(asset_id),
            "media_type": str(media_type),
            "evidence_status": evidence_status,
            "authorization_status": authorization_status,
            "deidentified": deidentified,
        },
        "output": {
            "width": int(width),
            "height": int(height),
            "fps": int(fps),
            "duration_s": round(float(duration_s), 3),
        },
        "cues": normalized_cues,
        "render_policy": {
            "renderer": "hyperframes",
            "renderer_version": HYPERFRAMES_VERSION,
            "renderer_source_commit": HYPERFRAMES_SOURCE_COMMIT,
            "animation_runtime": "native_waapi_svg",
            "reduced_motion_fallback": "static_focus_with_label",
            "static_fallback": "full_frame_with_box_and_label",
        },
        "authority_boundary": {
            "status": "ready_for_renderer_slot",
            "candidate_only": True,
            "source_media_modified": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "second_pipeline_created": False,
            "second_state_machine_created": False,
            "second_ffmpeg_outlet_created": False,
            "creative_approval_claimed": False,
        },
    }
    if routing_binding is not None:
        plan.update(routing_binding)
    if privacy_binding is not None:
        plan.update(privacy_binding["references"])
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_semantic_motion_plan(plan)
    return plan


def validate_semantic_motion_plan(plan: dict[str, Any]) -> None:
    schema = _load_schema()
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(plan)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(
            f"semantic motion schema validation failed: {exc.message}"
        ) from exc
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("semantic motion plan integrity check failed")

    packaging_path = validate_artifact_reference(
        _object(plan.get("source_packaging_plan")), "video packaging plan"
    )
    script_path = validate_artifact_reference(
        _object(plan.get("source_script_anchor")), "script anchor artifact"
    )
    asset_path = validate_artifact_reference(
        _object(plan.get("source_asset")), "semantic motion asset"
    )
    visual_plan_path = validate_artifact_reference(
        _object(plan.get("source_visual_production_plan")),
        "visual production plan",
    )
    packaging = read_json_object(packaging_path, "video packaging plan")
    _validate_packaging_plan(packaging)
    validate_visual_production_plan(
        read_json_object(visual_plan_path, "visual production plan")
    )
    routing = _validated_optional_visual_routing(plan, packaging_path)
    privacy_zones = _validated_optional_privacy_binding(plan)
    anchors = _script_anchors(read_json_object(script_path, "script anchor artifact"))
    output = _object(plan.get("output"))
    width = int(output["width"])
    height = int(output["height"])
    duration_s = _number(output.get("duration_s"), "motion duration")
    source_sha = file_sha256(asset_path)
    asset_id = str(_object(plan.get("asset")).get("asset_id") or "")
    seen: set[str] = set()

    for cue_value in plan.get("cues") or []:
        cue = _object(cue_value)
        cue_id = str(cue.get("cue_id") or "")
        if cue_id in seen:
            raise ContractError("semantic motion cue ids must be unique")
        seen.add(cue_id)
        if cue.get("source_asset_id") != asset_id:
            raise ContractError(f"motion cue {cue_id} asset id drifted")
        if cue.get("source_asset_sha256") != source_sha:
            raise ContractError(f"motion cue {cue_id} source asset hash drifted")
        story_ref = str(cue.get("story_segment_ref") or "")
        if cue.get("script_anchor") != anchors.get(story_ref):
            raise ContractError(f"motion cue {cue_id} script anchor drifted")
        _validate_rect(_object(cue.get("target_rect")), f"{cue_id} target")
        if cue.get("next_target_rect") is not None:
            _validate_rect(
                _object(cue.get("next_target_rect")), f"{cue_id} next target"
            )
        _validate_rect(
            _object(cue.get("focus_destination")), f"{cue_id} focus destination"
        )
        timing = _object(cue.get("timing"))
        start = _number(timing.get("timeline_start_s"), f"{cue_id} start")
        end = start + _cue_duration(cue)
        anchor = _object(cue.get("script_anchor"))
        if start < _number(anchor.get("timeline_start_s"), "anchor start") - 0.001:
            raise ContractError(f"motion cue {cue_id} starts before its script anchor")
        if end > duration_s + 0.001:
            raise ContractError(f"motion cue {cue_id} exceeds output duration")
        if not _cue_inside_packaging_scene(cue, packaging):
            raise ContractError(f"motion cue {cue_id} is outside its packaging scene")
        if routing is not None:
            scene_route = routing.get(story_ref)
            if scene_route != "hyperframes_evidence_focus":
                raise ContractError(
                    f"motion cue {cue_id} requires the evidence-focus visual route"
                )
        recipes = set(cue.get("focus_recipe") or [])
        if not recipes <= ALLOWED_RECIPES:
            raise ContractError(f"motion cue {cue_id} uses an unsupported recipe")
        if "focus_to_next" in recipes and cue.get("next_target_rect") is None:
            raise ContractError(f"motion cue {cue_id} lacks next_target_rect")
        information_role = str(cue.get("information_role") or "evidence")
        if information_role not in INFORMATION_ROLES:
            raise ContractError(f"motion cue {cue_id} has an invalid information role")
        presentation_mode = str(cue.get("presentation_mode") or "fullscreen_evidence")
        if presentation_mode not in PRESENTATION_MODES:
            raise ContractError(f"motion cue {cue_id} has an invalid presentation mode")
        _validate_layout_decision(cue, presentation_mode)
        _validate_information_complementarity(cue)
        if privacy_zones is not None:
            _validate_cue_privacy_regions(cue, privacy_zones)
        _validate_output_collisions(cue, width, height)


def write_semantic_motion_plan(plan: dict[str, Any], output_path: str | Path) -> Path:
    validate_semantic_motion_plan(plan)
    return atomic_write_json(plan, output_path)


def materialize_hyperframes_project(
    plan_path: str | Path,
    project_dir: str | Path,
    *,
    static_fallback: bool = False,
    privacy_debug: bool = False,
) -> dict[str, Path]:
    source_plan_path = Path(plan_path).expanduser().resolve()
    plan = read_json_object(source_plan_path, "semantic motion plan")
    validate_semantic_motion_plan(plan)
    root = Path(project_dir).expanduser().resolve()
    owner_path = root / ".semantic-motion-owner.json"
    if root.is_dir() and any(root.iterdir()):
        if not owner_path.is_file():
            raise ContractError(
                "HyperFrames project directory is non-empty and unmanaged"
            )
        owner = read_json_object(owner_path, "semantic motion project owner")
        if owner.get("source_plan_sha256") != plan.get("plan_sha256"):
            raise ContractError(
                "HyperFrames project belongs to a different motion plan"
            )
    root.mkdir(parents=True, exist_ok=True)
    owner = {
        "schema": "video_creation_pipeline.semantic_motion_output_owner.v1",
        "source_plan_sha256": plan["plan_sha256"],
        "managed_outputs_only": True,
        "user_media_overwrite_allowed": False,
    }
    owner["owner_sha256"] = payload_sha256(owner, "owner_sha256")
    atomic_write_json(owner, owner_path)

    asset_source = validate_artifact_reference(
        _object(plan["source_asset"]), "semantic motion asset"
    )
    asset_dir = root / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    suffix = asset_source.suffix.lower() or ".bin"
    asset_target = asset_dir / f"source-{file_sha256(asset_source)[:16]}{suffix}"
    if not asset_target.is_file() or file_sha256(asset_target) != file_sha256(
        asset_source
    ):
        shutil.copy2(asset_source, asset_target)
    html_path = root / "index.html"
    atomic_write_text(
        build_hyperframes_html(
            plan,
            f"assets/{asset_target.name}",
            static_fallback=static_fallback,
            privacy_debug=privacy_debug,
        ),
        html_path,
    )
    meta = {
        "id": f"semantic-motion-{plan['plan_sha256'][:12]}",
        "name": "semantic-motion-candidate",
        "renderer": "hyperframes",
        "rendererVersion": HYPERFRAMES_VERSION,
        "sourceCommit": HYPERFRAMES_SOURCE_COMMIT,
        "candidateOnly": True,
    }
    meta_path = atomic_write_json(meta, root / "meta.json")
    return {
        "owner": owner_path,
        "html": html_path,
        "meta": meta_path,
        "asset": asset_target,
    }


def build_hyperframes_html(
    plan: dict[str, Any],
    asset_url: str,
    *,
    static_fallback: bool = False,
    privacy_debug: bool = False,
) -> str:
    validate_semantic_motion_plan(plan)
    output = _object(plan["output"])
    style = _semantic_style(plan)
    width = int(output["width"])
    height = int(output["height"])
    cue_markup = []
    animation_rows = []
    for index, cue_value in enumerate(plan["cues"]):
        cue = _object(cue_value)
        timing = _object(cue["timing"])
        start_ms = round(float(timing["timeline_start_s"]) * 1000)
        full_ms = round(float(timing["full_frame_s"]) * 1000)
        enter_ms = round(float(timing["enter_s"]) * 1000)
        hold_ms = round(float(timing["hold_s"]) * 1000)
        exit_ms = round(float(timing["exit_s"]) * 1000)
        focus = _object(cue["focus_destination"])
        target = _object(cue["target_rect"])
        label = _object(cue["label"])
        watermark = _object(cue["watermark"])
        disclaimer = _object(cue["disclaimer"])
        privacy_debug_markup = ""
        if privacy_debug:
            privacy_debug_markup = "".join(
                f'<div class="privacy-debug-region" data-region-id="{html.escape(str(_object(region)["id"]))}" style="{_rect_css(_object(_object(region)["rect"]))}"></div>'
                for region in cue.get("protected_regions") or []
                if str(_object(region).get("id") or "").startswith("privacy_")
            )
        target_center = (
            round((float(target["x"]) + float(target["width"]) / 2) * 100, 3),
            round((float(target["y"]) + float(target["height"]) / 2) * 100, 3),
        )
        cue_markup.append(
            f'''<section id="cue-{index}" class="cue clip" data-start="{float(timing["timeline_start_s"]):.3f}" data-duration="{_cue_duration(cue):.3f}">
  <div class="full-layer"><img src="{html.escape(asset_url)}" alt="" /></div>
  <div class="context-dim"></div>
  <div class="focus-layer" style="{_rect_css(focus)}background-image:url('{html.escape(asset_url)}');background-position:{target_center[0]}% {target_center[1]}%;">
    <svg class="focus-overlay" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
      <rect x="18" y="18" width="964" height="964" rx="10" fill="none" stroke="{style["accent"]}" stroke-width="16" />
      <path d="M75 900 L300 900" stroke="{style["accent"]}" stroke-width="30" stroke-linecap="round" />
    </svg>
  </div>
  <div class="leader" style="{_leader_css(focus, _object(label["rect"]))}"></div>
  <div class="label" style="{_rect_css(_object(label["rect"]))}">{html.escape(str(label["text"]))}</div>
  <div class="watermark" style="{_rect_css(_object(watermark["rect"]))}">{html.escape(str(watermark["text"]))}</div>
  <div class="disclaimer" style="{_rect_css(_object(disclaimer["rect"]))}">{html.escape(str(disclaimer["text"]))}</div>
  {privacy_debug_markup}
</section>'''
        )
        animation_rows.append(
            {
                "id": index,
                "start": start_ms,
                "full": full_ms,
                "enter": enter_ms,
                "hold": hold_ms,
                "exit": exit_ms,
                "static": bool(static_fallback),
            }
        )
    animations_json = json.dumps(animation_rows, separators=(",", ":"))
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width={width}, height={height}" />
  <style>
    * {{ box-sizing:border-box; }}
    html,body {{ margin:0; width:{width}px; height:{height}px; overflow:hidden; background:{style["background"]}; font-family:{style["font_family"]}; }}
    #root {{ position:relative; width:{width}px; height:{height}px; overflow:hidden; background:{style["background"]}; }}
    .cue {{ position:absolute; inset:0; opacity:1; overflow:hidden; }}
    .full-layer {{ position:absolute; left:8.9%; top:9.4%; width:68.9%; height:54.7%; display:flex; align-items:center; justify-content:center; background:{style["panel"]}; }}
    .full-layer img {{ width:100%; height:100%; object-fit:contain; }}
    .context-dim {{ position:absolute; inset:0; background:{style["dim"]}; opacity:0; }}
    .focus-layer {{ position:absolute; overflow:hidden; background-color:{style["panel"]}; background-repeat:no-repeat; background-size:cover; border:2px solid {style["primary"]}; opacity:0; transform:scale(.97); transform-origin:center; }}
    .focus-overlay {{ position:absolute; inset:0; width:100%; height:100%; }}
    .label {{ position:absolute; display:flex; align-items:center; color:{style["foreground"]}; font-size:32px; line-height:1.35; font-weight:700; padding:14px 18px; background:{style["panel"]}; border-left:8px solid {style["accent"]}; opacity:0; overflow:hidden; }}
    .leader {{ position:absolute; height:4px; background:{style["accent"]}; transform-origin:left center; opacity:0; }}
    .watermark {{ position:absolute; display:flex; justify-content:flex-end; align-items:center; color:{style["primary"]}; font-size:20px; line-height:1.2; font-weight:700; opacity:.78; overflow:hidden; }}
    .disclaimer {{ position:absolute; display:flex; align-items:center; color:{style["muted"]}; font-size:20px; line-height:1.35; padding:8px 0; border-top:1px solid {style["muted"]}; overflow:hidden; }}
    .privacy-debug-region {{ position:absolute; z-index:9999; border:5px dashed #ff2d55; background:rgba(255,45,85,.22); pointer-events:none; }}
  </style>
</head>
<body>
<main id="root" data-composition-id="main" data-no-timeline data-start="0" data-duration="{output["duration_s"]}" data-width="{width}" data-height="{height}" data-fps="{output["fps"]}" data-privacy-debug="{str(privacy_debug).lower()}">
{"".join(cue_markup)}
</main>
<script>
  const rows={animations_json};
  const animate=(node,frames,timing)=>{{ const a=node.animate(frames,{{...timing,fill:"both"}}); a.pause(); return a; }};
  for (const row of rows) {{
    const cue=document.getElementById(`cue-${{row.id}}`);
    const visible=row.full+row.enter+row.hold+row.exit;
    animate(cue,[{{opacity:1,offset:0}},{{opacity:1,offset:.99}},{{opacity:0,offset:1}}],{{delay:row.start,duration:visible,easing:"linear"}});
    if (row.static) {{
      animate(cue.querySelector(".focus-layer"),[{{opacity:1,transform:"scale(1)"}},{{opacity:1,transform:"scale(1)"}}],{{delay:row.start,duration:visible}});
      animate(cue.querySelector(".label"),[{{opacity:1}},{{opacity:1}}],{{delay:row.start,duration:visible}});
      continue;
    }}
    const focusStart=row.start+row.full;
    const focusDuration=row.enter+row.hold+row.exit;
    const enterOffset=row.enter/focusDuration;
    const exitOffset=(row.enter+row.hold)/focusDuration;
    animate(cue.querySelector(".context-dim"),[{{opacity:0,offset:0}},{{opacity:1,offset:enterOffset}},{{opacity:1,offset:exitOffset}},{{opacity:0,offset:1}}],{{delay:focusStart,duration:focusDuration,easing:"ease-in-out"}});
    animate(cue.querySelector(".focus-layer"),[{{opacity:0,transform:"scale(.97)",offset:0}},{{opacity:1,transform:"scale(1)",offset:enterOffset}},{{opacity:1,transform:"scale(1)",offset:exitOffset}},{{opacity:0,transform:"scale(1.02)",offset:1}}],{{delay:focusStart,duration:focusDuration,easing:"ease-in-out"}});
    for (const selector of [".label",".leader"]) {{
      animate(cue.querySelector(selector),[{{opacity:0,offset:0}},{{opacity:1,offset:enterOffset}},{{opacity:1,offset:exitOffset}},{{opacity:0,offset:1}}],{{delay:focusStart,duration:focusDuration,easing:"ease-in-out"}});
    }}
  }}
</script>
</body>
</html>'''


def run_hyperframes_candidate(
    plan_path: str | Path,
    project_dir: str | Path,
    hyperframes_source_root: str | Path,
    *,
    browser_path: str | Path,
    ffmpeg_path: str | Path,
    ffprobe_path: str | Path,
    timeout_s: float = 300.0,
    static_fallback: bool = False,
    render_assurance_plan_path: str | Path | None = None,
    node_path: str | Path = "node",
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "semantic motion plan")
    validate_semantic_motion_plan(plan)
    project = Path(project_dir).expanduser().resolve()
    materialized = materialize_hyperframes_project(
        plan_file, project, static_fallback=static_fallback
    )
    source_root = Path(hyperframes_source_root).expanduser().resolve()
    cli = source_root / HYPERFRAMES_CLI_RELATIVE
    package_path = source_root / "packages" / "cli" / "package.json"
    if not cli.is_file() or not package_path.is_file():
        raise ContractError("fixed HyperFrames source CLI was not found")
    package = read_json_object(package_path, "HyperFrames CLI package")
    if package.get("version") != HYPERFRAMES_VERSION:
        raise ContractError("HyperFrames CLI version drifted from the reviewed source")
    commit = _git_head(source_root)
    if commit != HYPERFRAMES_SOURCE_COMMIT:
        raise ContractError(
            "HyperFrames source commit drifted from the reviewed source"
        )

    env = os.environ.copy()
    env.update(
        {
            "HYPERFRAMES_BROWSER_PATH": str(Path(browser_path).resolve()),
            "HYPERFRAMES_FFMPEG_PATH": str(Path(ffmpeg_path).resolve()),
            "HYPERFRAMES_FFPROBE_PATH": str(Path(ffprobe_path).resolve()),
            "HYPERFRAMES_NO_TELEMETRY": "1",
        }
    )
    evidence_dir = project / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    render_assurance = None
    if render_assurance_plan_path is not None:
        from .hyperframes_renderkit import (
            OFFICIAL_ROUTE,
            run_render_assurance_preflight,
        )

        render_assurance = run_render_assurance_preflight(
            render_assurance_plan_path,
            evidence_dir / "renderkit-preflight-receipt.json",
            node_path=node_path,
            timeout_s=min(timeout_s, 120.0),
        )
        if (
            render_assurance.get("status") != "passed_for_official_hyperframes_faithful"
            or render_assurance.get("selected_route") != OFFICIAL_ROUTE
        ):
            raise ContractError(
                "the official HyperFrames runner requires a passed official-route "
                "RenderKit assurance receipt"
            )
    checks: list[dict[str, Any]] = []
    for name, args in (
        ("check", ["check", str(project), "--json", "--strict"]),
        (
            "inspect",
            ["inspect", str(project), "--json", "--at-transitions", "--strict"],
        ),
        (
            "snapshot",
            [
                "snapshot",
                str(project),
                "--output",
                str(project / "snapshots"),
                "--frames",
                "5",
                "--describe",
                "false",
                "--no-browser-gpu",
            ],
        ),
    ):
        result = _run_hyperframes(cli, args, source_root, env, timeout_s)
        log = evidence_dir / f"hyperframes-{name}.log"
        atomic_write_text(result["output"], log)
        checks.append(
            {
                "command": name,
                "status": "passed",
                "log": artifact_reference(log, f"HyperFrames {name} log"),
            }
        )
    raw_output = project / "renders" / "hyperframes-intermediate.mp4"
    result = _run_hyperframes(
        cli,
        [
            "render",
            str(project),
            "--output",
            str(raw_output),
            "--fps",
            str(plan["output"]["fps"]),
            "--workers",
            "1",
            "--quality",
            "draft",
            "--strict",
            "--no-browser-gpu",
            "--no-best-effort",
        ],
        source_root,
        env,
        timeout_s,
    )
    render_log = evidence_dir / "hyperframes-render.log"
    atomic_write_text(result["output"], render_log)
    checks.append(
        {
            "command": "render",
            "status": "passed",
            "log": artifact_reference(render_log, "HyperFrames render log"),
        }
    )

    receipt = _normalize_and_qa(
        plan_file,
        plan,
        project,
        raw_output,
        ffprobe_path=Path(ffprobe_path),
        timeout_s=timeout_s,
    )
    receipt["hyperframes"] = {
        "version": HYPERFRAMES_VERSION,
        "source_commit": HYPERFRAMES_SOURCE_COMMIT,
        "cli_sha256": file_sha256(cli),
        "checks": checks,
        "intermediate_video": artifact_reference(
            raw_output, "HyperFrames intermediate video"
        ),
        "project_html": artifact_reference(
            materialized["html"], "HyperFrames project HTML"
        ),
    }
    if render_assurance is not None:
        receipt["hyperframes"]["render_assurance"] = artifact_reference(
            evidence_dir / "renderkit-preflight-receipt.json",
            "HyperFrames RenderKit preflight receipt",
        )
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_semantic_motion_run_receipt(receipt)
    atomic_write_json(receipt, project / "semantic-motion-run-receipt.json")
    return receipt


def validate_semantic_motion_run_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != SEMANTIC_MOTION_RUN_RECEIPT_SCHEMA:
        raise ContractError("unsupported semantic motion run receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("semantic motion run receipt integrity check failed")
    if receipt.get("status") != "completed_candidate_only":
        raise ContractError("semantic motion output must remain candidate-only")
    paths: dict[str, Path] = {}
    for field, label in (
        ("source_plan", "semantic motion plan"),
        ("candidate_video", "semantic motion candidate video"),
        ("qa_report", "semantic motion QA report"),
        ("ffmpeg_receipt", "semantic motion FFmpeg receipt"),
    ):
        paths[field] = validate_artifact_reference(_object(receipt.get(field)), label)
    plan = read_json_object(paths["source_plan"], "semantic motion plan")
    validate_semantic_motion_plan(plan)
    ffmpeg_receipt = read_json_object(
        paths["ffmpeg_receipt"], "semantic motion FFmpeg receipt"
    )
    validate_ffmpeg_execution_receipt(ffmpeg_receipt)
    output_rows = ffmpeg_receipt.get("outputs") or []
    if len(output_rows) != 1 or _object(output_rows[0]).get("artifact") != receipt.get(
        "candidate_video"
    ):
        raise ContractError("semantic motion candidate drifted from the FFmpeg receipt")
    qa_report = read_json_object(paths["qa_report"], "semantic motion QA report")
    validate_technical_qa_report(qa_report)
    if (
        _object(qa_report.get("readiness")).get("technical_ready") is not True
        or _object(qa_report.get("readiness")).get("release_eligible") is not False
        or _object(_object(qa_report.get("inputs")).get("media")).get("sha256")
        != _object(receipt.get("candidate_video")).get("sha256")
    ):
        raise ContractError("semantic motion candidate is not bound to passing QA")
    renderer = _object(receipt.get("hyperframes"))
    if (
        renderer.get("version") != HYPERFRAMES_VERSION
        or renderer.get("source_commit") != HYPERFRAMES_SOURCE_COMMIT
    ):
        raise ContractError("semantic motion renderer provenance drifted")
    if not isinstance(renderer.get("checks"), list) or len(renderer["checks"]) != 4:
        raise ContractError("semantic motion renderer checks are incomplete")
    expected_checks = {"check", "inspect", "snapshot", "render"}
    actual_checks = set()
    for value in renderer["checks"]:
        row = _object(value)
        actual_checks.add(str(row.get("command") or ""))
        if row.get("status") != "passed":
            raise ContractError("semantic motion renderer check did not pass")
        validate_artifact_reference(
            _object(row.get("log")), "semantic motion renderer check log"
        )
    if actual_checks != expected_checks:
        raise ContractError("semantic motion renderer check set is incomplete")
    for field, label in (
        ("intermediate_video", "HyperFrames intermediate video"),
        ("project_html", "HyperFrames project HTML"),
    ):
        validate_artifact_reference(_object(renderer.get(field)), label)
    if len(str(renderer.get("cli_sha256") or "")) != 64:
        raise ContractError("semantic motion renderer CLI hash is invalid")
    if renderer.get("render_assurance") is not None:
        from .hyperframes_renderkit import (
            OFFICIAL_ROUTE,
            validate_render_preflight_receipt,
        )

        assurance_path = validate_artifact_reference(
            _object(renderer.get("render_assurance")),
            "HyperFrames RenderKit preflight receipt",
        )
        assurance = read_json_object(
            assurance_path, "HyperFrames RenderKit preflight receipt"
        )
        validate_render_preflight_receipt(assurance)
        if (
            assurance.get("status") != "passed_for_official_hyperframes_faithful"
            or assurance.get("selected_route") != OFFICIAL_ROUTE
        ):
            raise ContractError(
                "semantic motion render assurance did not authorize the official route"
            )
    boundary = _object(receipt.get("authority_boundary"))
    if boundary != {
        "candidate_only": True,
        "creative_approval_claimed": False,
        "release_eligible": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
        "single_ffmpeg_outlet_used": True,
    }:
        raise ContractError("semantic motion run authority boundary is invalid")


def _normalize_and_qa(
    plan_path: Path,
    plan: dict[str, Any],
    project: Path,
    raw_output: Path,
    *,
    ffprobe_path: Path,
    timeout_s: float,
) -> dict[str, Any]:
    output = _object(plan["output"])
    publish_dir = project / "candidate"
    qa_dir = project / "qa"
    receipts_dir = project / "receipts"
    publish_dir.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    candidate = publish_dir / "semantic-motion-candidate.mp4"
    normalize_receipt = receipts_dir / "semantic-motion-normalize.json"
    execute_ffmpeg_with_receipt(
        "transcode",
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_output),
            "-map_metadata",
            "-1",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(output["fps"]),
            "-s",
            f"{output['width']}x{output['height']}",
            "-movflags",
            "+faststart",
            str(candidate),
        ],
        [
            {"role": "hyperframes_intermediate", "path": raw_output},
            {"role": "semantic_motion_plan", "path": plan_path},
        ],
        [{"role": "candidate_video", "path": candidate}],
        normalize_receipt,
        timeout_s=timeout_s,
    )
    probe = _probe(candidate, ffprobe_path)
    probe_path = atomic_write_json(probe, qa_dir / "ffprobe.json")
    detector_log = qa_dir / "detector.log"
    detector_receipt = receipts_dir / "semantic-motion-technical-qa.json"
    execute_ffmpeg_with_receipt(
        "technical_qa",
        [
            "ffmpeg",
            "-i",
            str(candidate),
            "-vf",
            "blackdetect=d=0.1:pix_th=0.02,freezedetect=n=0.003:d=2",
            "-f",
            "null",
            "-",
        ],
        [{"role": "media", "path": candidate}],
        [{"role": "detector_log", "path": detector_log}],
        detector_receipt,
        capture_log_path=detector_log,
        timeout_s=timeout_s,
    )
    qa = build_technical_qa_report(
        candidate,
        probe_path,
        detector_receipt,
        packaging_plan_path=Path(plan["source_packaging_plan"]["path"]),
        expected_duration_s=float(output["duration_s"]),
        policy={
            "target_width": int(output["width"]),
            "target_height": int(output["height"]),
            "target_fps": float(output["fps"]),
            "max_black_ratio": 0.05,
            "max_silence_ratio": 1.0,
            "freeze_warning_ratio": 0.95,
            "require_audio": False,
            "require_subtitles": False,
            "require_packaging_plan": True,
        },
    )
    qa_path = write_technical_qa_report(qa, qa_dir / "technical-qa.json")
    if qa["readiness"]["technical_ready"] is not True:
        raise ContractError("semantic motion candidate failed automatic technical QA")
    return {
        "schema": SEMANTIC_MOTION_RUN_RECEIPT_SCHEMA,
        "status": "completed_candidate_only",
        "source_plan": artifact_reference(plan_path, "semantic motion plan"),
        "candidate_video": artifact_reference(candidate, "semantic motion candidate"),
        "qa_report": artifact_reference(qa_path, "semantic motion QA report"),
        "ffmpeg_receipt": artifact_reference(
            normalize_receipt, "semantic motion FFmpeg receipt"
        ),
        "authority_boundary": {
            "candidate_only": True,
            "creative_approval_claimed": False,
            "release_eligible": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "single_ffmpeg_outlet_used": True,
        },
    }


def _load_schema() -> dict[str, Any]:
    return read_json_object(
        Path(__file__).with_name("schemas") / "semantic_motion_plan.v1.schema.json",
        "semantic motion plan schema",
    )


def _optional_visual_routing_binding(
    packaging_path: Path,
    routing_plan_path: str | Path | None,
    style_profile_path: str | Path | None,
) -> dict[str, Any] | None:
    if routing_plan_path is None and style_profile_path is None:
        return None
    if routing_plan_path is None or style_profile_path is None:
        raise ContractError(
            "semantic motion visual routing plan and style profile must be supplied together"
        )
    route_path = Path(routing_plan_path).expanduser().resolve()
    style_path = Path(style_profile_path).expanduser().resolve()
    route = read_json_object(route_path, "content visual routing plan")
    style = read_json_object(style_path, "visual style profile")
    validate_content_visual_routing_plan(route)
    validate_visual_style_profile(style)
    route_inputs = _object(route.get("inputs"))
    if _object(route_inputs.get("packaging_plan")) != artifact_reference(
        packaging_path, "video packaging plan"
    ):
        raise ContractError("semantic motion routing plan must bind its packaging plan")
    if _object(route_inputs.get("visual_style_profile")) != artifact_reference(
        style_path, "visual style profile"
    ):
        raise ContractError("semantic motion routing plan must bind its style profile")
    return {
        "source_content_visual_routing_plan": artifact_reference(
            route_path, "content visual routing plan"
        ),
        "source_visual_style_profile": artifact_reference(
            style_path, "visual style profile"
        ),
    }


def _validated_optional_visual_routing(
    plan: dict[str, Any], packaging_path: Path
) -> dict[str, str] | None:
    route_ref = plan.get("source_content_visual_routing_plan")
    style_ref = plan.get("source_visual_style_profile")
    if route_ref is None and style_ref is None:
        return None
    if not isinstance(route_ref, dict) or not isinstance(style_ref, dict):
        raise ContractError(
            "semantic motion visual routing plan and style profile must be bound together"
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
    if _object(route_inputs.get("packaging_plan")) != artifact_reference(
        packaging_path, "video packaging plan"
    ):
        raise ContractError("semantic motion visual routing packaging drifted")
    if _object(route_inputs.get("visual_style_profile")) != _object(style_ref):
        raise ContractError("semantic motion visual style binding drifted")
    return {
        str(_object(row).get("scene_id") or ""): str(
            _object(row).get("visual_route") or ""
        )
        for row in route.get("scene_routes") or []
    }


def _optional_privacy_binding(
    zones_path: str | Path | None,
    smart_reframe_plan_path: str | Path | None,
) -> dict[str, Any] | None:
    if zones_path is None and smart_reframe_plan_path is None:
        return None
    if zones_path is None:
        raise ContractError("smart reframe privacy projection requires privacy zones")
    from .privacy_exclusion_zones import (
        project_privacy_exclusion_zones,
        validate_privacy_exclusion_zones,
    )

    source_path = Path(zones_path).expanduser().resolve()
    zones = read_json_object(source_path, "privacy exclusion zones")
    validate_privacy_exclusion_zones(zones)
    references = {
        "source_privacy_exclusion_zones": artifact_reference(
            source_path, "privacy exclusion zones"
        )
    }
    if zones["coordinate_space"] == "redaction_input_normalized":
        if smart_reframe_plan_path is None:
            raise ContractError(
                "source-space privacy zones require a smart reframe plan"
            )
        reframe_path = Path(smart_reframe_plan_path).expanduser().resolve()
        zones = project_privacy_exclusion_zones(source_path, reframe_path)
        references["source_smart_reframe_plan"] = artifact_reference(
            reframe_path, "smart reframe plan"
        )
    elif smart_reframe_plan_path is not None:
        raise ContractError(
            "final-canvas privacy zones cannot be projected through a second plan"
        )
    if zones["coverage"]["review_required"]:
        raise ContractError(
            "privacy exclusion zones contain uncovered spans and require review"
        )
    return {"zones": zones, "references": references}


def _validated_optional_privacy_binding(
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    zones_ref = plan.get("source_privacy_exclusion_zones")
    reframe_ref = plan.get("source_smart_reframe_plan")
    if zones_ref is None and reframe_ref is None:
        return None
    if not isinstance(zones_ref, dict):
        raise ContractError("semantic motion privacy zones reference is missing")
    from .privacy_exclusion_zones import (
        project_privacy_exclusion_zones,
        validate_privacy_exclusion_zones,
    )

    zones_path = validate_artifact_reference(zones_ref, "privacy exclusion zones")
    zones = read_json_object(zones_path, "privacy exclusion zones")
    validate_privacy_exclusion_zones(zones)
    if zones["coordinate_space"] == "redaction_input_normalized":
        if not isinstance(reframe_ref, dict):
            raise ContractError(
                "semantic motion source-space privacy zones lack reframe binding"
            )
        reframe_path = validate_artifact_reference(
            reframe_ref, "privacy smart reframe plan"
        )
        zones = project_privacy_exclusion_zones(zones_path, reframe_path)
    elif reframe_ref is not None:
        raise ContractError("semantic motion privacy projection binding drifted")
    if zones["coverage"]["review_required"]:
        raise ContractError("semantic motion privacy zones still require review")
    output = _object(plan.get("output"))
    if int(zones["media_clock"]["width"]) != int(output["width"]) or int(
        zones["media_clock"]["height"]
    ) != int(output["height"]):
        raise ContractError("semantic motion and privacy canvas dimensions differ")
    return zones


def _apply_privacy_regions_to_cue(cue: dict[str, Any], zones: dict[str, Any]) -> None:
    from .privacy_exclusion_zones import privacy_regions_for_interval

    timing = _object(cue.get("timing"))
    start_s = _number(timing.get("timeline_start_s"), "privacy cue start")
    end_s = start_s + _cue_duration(cue)
    dynamic = privacy_regions_for_interval(zones, start_s, end_s)
    static = [
        dict(_object(row))
        for row in cue.get("protected_regions") or []
        if not str(_object(row).get("id") or "").startswith("privacy_")
    ]
    if len(static) + len(dynamic) > 12:
        raise ContractError(
            "privacy layout has too many non-overlapping regions for one cue"
        )
    cue["protected_regions"] = [*static, *dynamic]
    original = dict(_object(_object(cue.get("label")).get("rect")))
    changed = False
    if any(_overlaps(original, region["rect"]) for region in dynamic):
        candidate = _privacy_safe_label_candidate(original, cue["protected_regions"])
        if candidate is None:
            raise ContractError("privacy layout has no safe label candidate")
        cue["label"] = {**_object(cue.get("label")), "rect": candidate}
        changed = candidate != original
    focus = _object(cue.get("focus_destination"))
    if any(_overlaps(focus, region["rect"]) for region in dynamic):
        raise ContractError(
            "privacy region collides with the evidence focus; use a safe full-screen layout"
        )
    cue["privacy_avoidance"] = {
        "status": (
            "no_active_regions"
            if not dynamic
            else "label_relocated"
            if changed
            else "original_layout_clear"
        ),
        "source": "privacy_exclusion_zones.v1",
        "region_ids": [region["id"] for region in dynamic],
        "label_position_changed": changed,
        "selected_label_rect": dict(_object(cue["label"])["rect"]),
        "runtime_reposition_allowed": False,
    }


def _validate_cue_privacy_regions(cue: dict[str, Any], zones: dict[str, Any]) -> None:
    from .privacy_exclusion_zones import privacy_regions_for_interval

    timing = _object(cue.get("timing"))
    start_s = _number(timing.get("timeline_start_s"), "privacy cue start")
    end_s = start_s + _cue_duration(cue)
    expected = privacy_regions_for_interval(zones, start_s, end_s)
    actual = [
        row
        for row in cue.get("protected_regions") or []
        if str(_object(row).get("id") or "").startswith("privacy_")
    ]
    if actual != expected:
        raise ContractError("semantic motion privacy regions drifted from source zones")
    decision = _object(cue.get("privacy_avoidance"))
    if decision.get("source") != "privacy_exclusion_zones.v1":
        raise ContractError("semantic motion privacy avoidance source drifted")
    if decision.get("region_ids") != [region["id"] for region in expected]:
        raise ContractError("semantic motion privacy avoidance region ids drifted")
    if decision.get("selected_label_rect") != _object(cue["label"])["rect"]:
        raise ContractError("semantic motion privacy label decision drifted")
    if decision.get("runtime_reposition_allowed") is not False:
        raise ContractError("renderer cannot reposition privacy-aware layout")


def _privacy_safe_label_candidate(
    original: dict[str, Any], protected_regions: list[dict[str, Any]]
) -> dict[str, float] | None:
    width = float(original["width"])
    height = float(original["height"])
    candidates = [dict(original)]
    for y in (0.08, 0.28, 0.48, 0.62):
        for x in (0.04, 0.96 - width):
            if x >= 0 and y + height <= 0.96:
                candidates.append(
                    {
                        "x": round(x, 8),
                        "y": round(y, 8),
                        "width": round(width, 8),
                        "height": round(height, 8),
                    }
                )
    ordered = sorted(
        candidates,
        key=lambda rect: (
            abs(float(rect["x"]) - float(original["x"]))
            + abs(float(rect["y"]) - float(original["y"])),
            float(rect["y"]),
            float(rect["x"]),
        ),
    )
    for candidate in ordered:
        if not any(
            _overlaps(candidate, _object(region.get("rect")))
            for region in protected_regions
        ):
            return candidate
    return None


def _semantic_style(plan: dict[str, Any]) -> dict[str, str]:
    reference = plan.get("source_visual_style_profile")
    if reference is None:
        return {
            "background": "#F7F8F5",
            "foreground": "#173D3C",
            "primary": "#245B5A",
            "accent": "#D55F45",
            "muted": "#4B5B5A",
            "panel": "#ffffff",
            "dim": "rgba(36,91,90,.16)",
            "font_family": '"Microsoft YaHei",sans-serif',
        }
    style_path = validate_artifact_reference(_object(reference), "visual style profile")
    style = read_json_object(style_path, "visual style profile")
    validate_visual_style_profile(style)
    binding = _object(_object(style["renderer_bindings"])["hyperframes"])
    css = _object(binding["css_variables"])
    primary = str(css["--primary"])
    return {
        "background": str(css["--background"]),
        "foreground": str(css["--foreground"]),
        "primary": primary,
        "accent": str(css["--accent"]),
        "muted": str(css["--muted"]),
        "panel": str(css["--panel"]),
        "dim": f"{primary}29",
        "font_family": str(binding["font_family"]),
    }


def _validate_packaging_plan(packaging: dict[str, Any]) -> None:
    if packaging.get("schema") != VIDEO_PACKAGING_PLAN_SCHEMA:
        raise ContractError("unsupported video packaging plan schema")
    scenes = packaging.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ContractError("video packaging plan requires scenes")


def _script_anchors(script: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if script.get("schema") != "video_creation_pipeline.script_anchor.v1":
        raise ContractError("unsupported script anchor artifact schema")
    values = script.get("anchors")
    if not isinstance(values, list) or not values:
        raise ContractError("script anchor artifact requires anchors")
    anchors: dict[str, dict[str, Any]] = {}
    required = {
        "status",
        "quote",
        "timeline_start_s",
        "timeline_end_s",
        "evidence_refs",
    }
    for value in values:
        row = _object(value)
        story_ref = str(row.get("story_segment_ref") or "").strip()
        if not story_ref or story_ref in anchors:
            raise ContractError("script anchor identities must be unique")
        anchor = {key: row.get(key) for key in required}
        if anchor["status"] != "confirmed":
            raise ContractError(f"script anchor {story_ref} is not confirmed")
        if not str(anchor["quote"] or "").strip():
            raise ContractError(f"script anchor {story_ref} quote is missing")
        start = _number(anchor["timeline_start_s"], "script anchor start")
        end = _number(anchor["timeline_end_s"], "script anchor end")
        if end <= start:
            raise ContractError(f"script anchor {story_ref} range is invalid")
        refs = anchor["evidence_refs"]
        if (
            not isinstance(refs, list)
            or not refs
            or not all(isinstance(item, str) and item.strip() for item in refs)
        ):
            raise ContractError(f"script anchor {story_ref} evidence is missing")
        anchors[story_ref] = anchor
    return anchors


def _cue_inside_packaging_scene(cue: dict[str, Any], packaging: dict[str, Any]) -> bool:
    story_ref = str(cue.get("story_segment_ref") or "")
    timing = _object(cue.get("timing"))
    start = float(timing["timeline_start_s"])
    end = start + _cue_duration(cue)
    for value in packaging.get("scenes") or []:
        scene = _object(value)
        scene_id = str(scene.get("story_segment_ref") or scene.get("scene_id") or "")
        scene_start = scene.get("start_s", scene.get("start"))
        scene_end = scene.get("end_s", scene.get("end"))
        if scene_id == story_ref and scene_start is not None and scene_end is not None:
            return (
                start >= float(scene_start) - 0.001 and end <= float(scene_end) + 0.001
            )
    return False


def _validate_output_collisions(cue: dict[str, Any], width: int, height: int) -> None:
    protected = []
    protected_by_id: dict[str, dict[str, Any]] = {}
    ids: set[str] = set()
    for value in cue.get("protected_regions") or []:
        region = _object(value)
        region_id = str(region.get("id") or "")
        if region_id in ids:
            raise ContractError("protected region ids must be unique")
        ids.add(region_id)
        rect = _object(region.get("rect"))
        _validate_rect(rect, f"protected region {region_id}")
        protected.append((region_id, rect))
        protected_by_id[region_id] = rect
    for required in ("subtitle", "watermark", "disclaimer"):
        if required not in ids:
            raise ContractError(f"protected region {required} is required")
    if cue.get("presentation_mode") == "person_with_overlay" and "person" not in ids:
        raise ContractError(
            "protected region person is required when a person remains visible"
        )
    label = _object(_object(cue.get("label")).get("rect"))
    focus = _object(cue.get("focus_destination"))
    watermark = _object(_object(cue.get("watermark")).get("rect"))
    disclaimer = _object(_object(cue.get("disclaimer")).get("rect"))
    for name, rect in (
        ("label", label),
        ("watermark", watermark),
        ("disclaimer", disclaimer),
    ):
        _validate_rect(rect, name)
    if protected_by_id["watermark"] != watermark:
        raise ContractError("watermark protection rectangle drifted")
    if protected_by_id["disclaimer"] != disclaimer:
        raise ContractError("disclaimer protection rectangle drifted")
    for region_id, rect in protected:
        if region_id not in {"watermark", "disclaimer"} and _overlaps(label, rect):
            raise ContractError(
                f"motion label collides with protected region {region_id}"
            )
        if _overlaps(focus, rect):
            raise ContractError(
                f"motion focus collides with protected region {region_id}"
            )
    if _overlaps(label, watermark) or _overlaps(label, disclaimer):
        raise ContractError("motion label collides with watermark or disclaimer")
    max_chars = max(4, int((float(label["width"]) * width - 36) / 32))
    if len(str(_object(cue.get("label")).get("text") or "")) > max_chars:
        raise ContractError("motion label text overflows its declared region")
    if float(label["height"]) * height < 48:
        raise ContractError("motion label region is too short for readable text")


def _validate_layout_decision(cue: dict[str, Any], presentation_mode: str) -> None:
    decision = _object(cue.get("layout_decision"))
    purpose = str(decision.get("semantic_purpose") or "").strip()
    selection_reason = str(decision.get("selection_reason") or "").strip()
    if not purpose or not selection_reason:
        raise ContractError("motion layout decision requires purpose and reason")

    raw_peer_count = decision.get("peer_item_count")
    if isinstance(raw_peer_count, bool):
        raise ContractError("motion layout peer item count must be an integer")
    try:
        peer_count = int(raw_peer_count)
    except (TypeError, ValueError) as exc:
        raise ContractError("motion layout peer item count must be an integer") from exc
    if peer_count < 0 or float(peer_count) != float(raw_peer_count):
        raise ContractError(
            "motion layout peer item count must be a non-negative integer"
        )

    selected_layout = str(decision.get("selected_layout") or "")
    candidates = decision.get("layout_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ContractError("motion layout decision requires candidate layouts")
    candidate_layouts: set[str] = set()
    for value in candidates:
        candidate = _object(value)
        layout = str(candidate.get("layout") or "")
        reason = str(candidate.get("reason") or "").strip()
        if layout not in LAYOUT_MODES or not reason:
            raise ContractError("motion layout candidate is invalid")
        if layout in candidate_layouts:
            raise ContractError("motion layout candidates must be unique")
        candidate_layouts.add(layout)
    if selected_layout not in candidate_layouts:
        raise ContractError("selected motion layout was not evaluated")
    if peer_count >= 3 and not REQUIRED_PEER_LAYOUTS <= candidate_layouts:
        raise ContractError(
            "three or more peer items require vertical, horizontal, and grid candidates"
        )

    person = _object(decision.get("person_window"))
    visible = person.get("visible")
    if not isinstance(visible, bool):
        raise ContractError("motion person window visibility must be boolean")
    selected_corner = person.get("selected_corner")
    corner_candidates = person.get("corner_candidates")
    placement_reason = str(person.get("placement_reason") or "").strip()
    exception_reason = str(person.get("exception_reason") or "").strip()
    if not placement_reason or person.get("information_space_yielded") is not True:
        raise ContractError("motion person window must explain information-space yield")
    if presentation_mode == "person_with_overlay":
        if visible is not True:
            raise ContractError("person-with-overlay requires a visible person window")
        if not isinstance(corner_candidates, list):
            raise ContractError("person window corner candidates must be a list")
        evaluated: dict[str, bool] = {}
        for value in corner_candidates:
            candidate = _object(value)
            corner = str(candidate.get("corner") or "")
            available = candidate.get("available")
            reason = str(candidate.get("reason") or "").strip()
            if (
                corner not in PERSON_WINDOW_CORNERS
                or not isinstance(available, bool)
                or not reason
                or corner in evaluated
            ):
                raise ContractError("person window corner candidate is invalid")
            evaluated[corner] = available
        if not PREFERRED_PERSON_WINDOW_CORNERS <= set(evaluated):
            raise ContractError(
                "person window must evaluate top-left and top-right by default"
            )
        if selected_corner not in PERSON_WINDOW_CORNERS or not evaluated.get(
            str(selected_corner), False
        ):
            raise ContractError("selected person window corner is unavailable")
        if selected_corner in EXCEPTION_PERSON_WINDOW_CORNERS:
            if any(evaluated[corner] for corner in PREFERRED_PERSON_WINDOW_CORNERS):
                raise ContractError(
                    "lower person window corner requires both top corners unavailable"
                )
            if not exception_reason:
                raise ContractError(
                    "lower person window corner requires an exception reason"
                )
        elif exception_reason:
            raise ContractError(
                "top person window corner cannot carry a lower-corner exception"
            )
    elif (
        visible is not False
        or selected_corner is not None
        or corner_candidates != []
        or exception_reason
    ):
        raise ContractError("fullscreen motion cannot retain a person window decision")

    review = _object(decision.get("human_composition_review"))
    dimensions = _object(review.get("dimensions"))
    if (
        review.get("status") != "approved"
        or not str(review.get("reviewed_by") or "").strip()
        or not str(review.get("reviewed_at") or "").strip()
        or set(dimensions) != HUMAN_REVIEW_DIMENSIONS
        or any(value != "pass" for value in dimensions.values())
    ):
        raise ContractError("motion layout requires approved human composition review")


def _validate_information_complementarity(cue: dict[str, Any]) -> None:
    label_text = _normalize_reading_text(
        str(_object(cue.get("label")).get("text") or "")
    )
    spoken_text = _normalize_reading_text(
        str(_object(cue.get("script_anchor")).get("quote") or "")
    )
    if not label_text or not spoken_text:
        return
    shorter = min(len(label_text), len(spoken_text))
    longer = max(len(label_text), len(spoken_text))
    near_full_restatement = (
        shorter >= 8
        and shorter / longer >= 0.75
        and (label_text in spoken_text or spoken_text in label_text)
    )
    if label_text == spoken_text or near_full_restatement:
        raise ContractError(
            "motion label restates the spoken subtitle instead of adding information"
        )


def _normalize_reading_text(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _validate_rect(rect: dict[str, Any], label: str) -> None:
    try:
        x = float(rect["x"])
        y = float(rect["y"])
        width = float(rect["width"])
        height = float(rect["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"{label} rectangle is invalid") from exc
    if width <= 0 or height <= 0 or min(x, y) < 0:
        raise ContractError(f"{label} rectangle is invalid")
    if x + width > 1.000001 or y + height > 1.000001:
        raise ContractError(f"{label} rectangle exceeds normalized bounds")


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return not (
        float(left["x"]) + float(left["width"]) <= float(right["x"])
        or float(right["x"]) + float(right["width"]) <= float(left["x"])
        or float(left["y"]) + float(left["height"]) <= float(right["y"])
        or float(right["y"]) + float(right["height"]) <= float(left["y"])
    )


def _rect_css(rect: dict[str, Any]) -> str:
    return (
        f"left:{float(rect['x']) * 100:.4f}%;"
        f"top:{float(rect['y']) * 100:.4f}%;"
        f"width:{float(rect['width']) * 100:.4f}%;"
        f"height:{float(rect['height']) * 100:.4f}%;"
    )


def _leader_css(focus: dict[str, Any], label: dict[str, Any]) -> str:
    x1 = float(focus["x"]) + float(focus["width"])
    y1 = float(focus["y"]) + float(focus["height"]) / 2
    x2 = float(label["x"])
    y2 = float(label["y"]) + float(label["height"]) / 2
    dx = x2 - x1
    dy = y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    angle = math.degrees(math.atan2(dy, dx))
    return (
        f"left:{x1 * 100:.4f}%;top:{y1 * 100:.4f}%;"
        f"width:{length * 100:.4f}%;transform:rotate({angle:.4f}deg);"
    )


def _cue_duration(cue: dict[str, Any]) -> float:
    timing = _object(cue.get("timing"))
    return sum(
        _number(timing.get(field), f"cue timing {field}")
        for field in ("full_frame_s", "enter_s", "hold_s", "exit_s")
    )


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return result


def _run_hyperframes(
    cli: Path,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["node", str(cli), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=float(timeout_s),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"HyperFrames execution failed: {exc}") from exc
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise ContractError(
            f"HyperFrames {args[0]} failed with exit code {result.returncode}: "
            f"{output.strip()[-2000:]}"
        )
    return {"returncode": result.returncode, "output": output}


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError("HyperFrames source commit could not be read")
    return result.stdout.strip().lower()


def _probe(media: Path, executable: Path) -> dict[str, Any]:
    if not executable.is_file():
        raise ContractError("ffprobe executable was not found")
    result = subprocess.run(
        [
            str(executable),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(f"ffprobe failed: {result.stderr.strip()}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("ffprobe returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("ffprobe returned an invalid document")
    return value


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
