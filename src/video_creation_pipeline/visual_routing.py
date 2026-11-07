from __future__ import annotations

import importlib.util
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .content_video_pipeline import VIDEO_PACKAGING_PLAN_SCHEMA
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    atomic_write_text,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .safe_media_scene_plan import validate_safe_media_scene_plan
from .smart_reframe import validate_smart_reframe_plan

VISUAL_STYLE_PROFILE_SCHEMA = "video_creation_pipeline.visual_style_profile.v1"
CONTENT_VISUAL_ROUTING_SPEC_SCHEMA = (
    "video_creation_pipeline.content_visual_routing_spec.v1"
)
CONTENT_VISUAL_ROUTING_PLAN_SCHEMA = (
    "video_creation_pipeline.content_visual_routing_plan.v1"
)
HYPERFRAMES_MUSIC_HANDOFF_SCHEMA = (
    "video_creation_pipeline.hyperframes_music_to_video_handoff.v1"
)
HYPERFRAMES_MUSIC_ANALYSIS_RECEIPT_SCHEMA = (
    "video_creation_pipeline.hyperframes_music_analysis_receipt.v1"
)
HYPERFRAMES_MUSIC_SOURCE_COMMIT = "477e09642b6db735866316301d924d0dfdc3cf38"

CONTENT_KINDS = {
    "evidence_document",
    "concept_explanation",
    "process_sequence",
    "comparison",
    "number_explanation",
    "real_world_action",
    "emotional_story",
    "speaker_connection",
    "transition_or_emphasis",
    "music_driven_promo",
}
VISUAL_ROUTES = {
    "hyperframes_evidence_focus",
    "hyperframes_music_to_video",
    "remotion_knowledge_animation",
    "remotion_kinetic_typography",
    "existing_media_broll",
    "provider_generated_broll",
    "missing_material",
}

_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "paper_cutout": {
        "colors": {
            "background": "#f4efe6",
            "foreground": "#1d2935",
            "primary": "#c43d32",
            "accent": "#23777d",
            "muted": "#8b7656",
            "panel": "#fffaf0",
        },
        "typography": {
            "display": "Noto Sans SC, sans-serif",
            "body": "Noto Sans SC, sans-serif",
        },
        "material": "layered_paper",
        "depth": "shallow_cutout_shadow",
        "motion": "stop_motion_soft",
        "provider_prompt": (
            "layered paper cutout, tactile paper fibers, shallow handcrafted depth, "
            "restrained stop-motion movement, consistent palette"
        ),
        "negative_constraints": [
            "photorealistic material",
            "plastic 3d surface",
            "inconsistent character or prop design",
            "invented factual labels",
        ],
    },
    "editorial_clean": {
        "colors": {
            "background": "#f7f8fa",
            "foreground": "#15191e",
            "primary": "#2457a6",
            "accent": "#c43b3b",
            "muted": "#66717d",
            "panel": "#ffffff",
        },
        "typography": {
            "display": "Noto Sans SC, sans-serif",
            "body": "Noto Sans SC, sans-serif",
        },
        "material": "flat_editorial",
        "depth": "minimal",
        "motion": "precise_reserved",
        "provider_prompt": (
            "clean editorial illustration, restrained geometry, clear hierarchy, "
            "high legibility, consistent neutral lighting"
        ),
        "negative_constraints": [
            "decorative clutter",
            "illegible text",
            "exaggerated depth",
            "invented factual labels",
        ],
    },
    "documentary_warm": {
        "colors": {
            "background": "#202426",
            "foreground": "#f6f4ef",
            "primary": "#d8b24c",
            "accent": "#5fa8a0",
            "muted": "#9ca4a5",
            "panel": "#303638",
        },
        "typography": {
            "display": "Noto Sans SC, sans-serif",
            "body": "Noto Sans SC, sans-serif",
        },
        "material": "documentary_texture",
        "depth": "natural",
        "motion": "observational_slow",
        "provider_prompt": (
            "documentary realism, natural practical light, restrained warm highlights, "
            "credible lived-in detail, consistent subject continuity"
        ),
        "negative_constraints": [
            "glossy advertisement look",
            "overstaged emotion",
            "identity drift",
            "invented factual labels",
        ],
    },
    "technical_diagram": {
        "colors": {
            "background": "#f3f6f8",
            "foreground": "#17212b",
            "primary": "#007f7b",
            "accent": "#d99b18",
            "muted": "#617181",
            "panel": "#ffffff",
        },
        "typography": {
            "display": "Noto Sans SC, sans-serif",
            "body": "Noto Sans SC, sans-serif",
        },
        "material": "diagrammatic",
        "depth": "flat",
        "motion": "stepwise_explanatory",
        "provider_prompt": (
            "precise technical diagram, simple shapes, explicit spatial relationships, "
            "high contrast, consistent scale"
        ),
        "negative_constraints": [
            "ornamental texture",
            "ambiguous arrows",
            "dense unreadable labels",
            "invented factual labels",
        ],
    },
    "evidence_neutral": {
        "colors": {
            "background": "#161616",
            "foreground": "#ffffff",
            "primary": "#f8e16c",
            "accent": "#d94f4f",
            "muted": "#a0a7ad",
            "panel": "#242424",
        },
        "typography": {
            "display": "Noto Sans SC, sans-serif",
            "body": "Noto Sans SC, sans-serif",
        },
        "material": "source_preserving",
        "depth": "none",
        "motion": "evidence_focus_only",
        "provider_prompt": "neutral evidence presentation with source pixels preserved",
        "negative_constraints": [
            "restyled evidence pixels",
            "invented labels",
            "decorative obstruction",
            "cropped source context",
        ],
    },
}


def build_visual_style_profile(preset_id: str) -> dict[str, Any]:
    preset = str(preset_id or "").strip()
    try:
        tokens = _STYLE_PRESETS[preset]
    except KeyError as exc:
        raise ContractError(f"unsupported visual style preset: {preset}") from exc
    profile = _style_profile_payload(preset, tokens)
    validate_visual_style_profile(profile)
    return profile


def _style_profile_payload(preset: str, tokens: dict[str, Any]) -> dict[str, Any]:
    colors = dict(tokens["colors"])
    typography = dict(tokens["typography"])
    profile: dict[str, Any] = {
        "schema": VISUAL_STYLE_PROFILE_SCHEMA,
        "preset_id": preset,
        "tokens": {
            "colors": colors,
            "typography": typography,
            "material": tokens["material"],
            "depth": tokens["depth"],
            "motion": tokens["motion"],
        },
        "renderer_bindings": {
            "remotion": {
                "background": colors["background"],
                "foreground": colors["foreground"],
                "primary": colors["primary"],
                "accent": colors["accent"],
                "muted": colors["muted"],
                "panel": colors["panel"],
                "display_font": typography["display"],
                "body_font": typography["body"],
                "motion_profile": tokens["motion"],
            },
            "hyperframes": {
                "css_variables": {
                    "--background": colors["background"],
                    "--foreground": colors["foreground"],
                    "--primary": colors["primary"],
                    "--accent": colors["accent"],
                    "--muted": colors["muted"],
                    "--panel": colors["panel"],
                },
                "font_family": typography["body"],
                "motion_profile": tokens["motion"],
            },
            "provider_prompt": {
                "style_prefix": tokens["provider_prompt"],
                "negative_constraints": list(tokens["negative_constraints"]),
            },
        },
        "evidence_policy": {
            "source_pixels_must_remain_unstyled": True,
            "factual_labels_must_come_from_evidence": True,
            "style_may_change_framing_not_evidence": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "renderer_execution_performed": False,
            "provider_execution_performed": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    profile["profile_sha256"] = payload_sha256(profile, "profile_sha256")
    return profile


def validate_visual_style_profile(profile: dict[str, Any]) -> None:
    _validate_schema(
        profile, "visual_style_profile.v1.schema.json", "visual style profile"
    )
    if profile.get("schema") != VISUAL_STYLE_PROFILE_SCHEMA:
        raise ContractError("unsupported visual style profile schema")
    if profile.get("profile_sha256") != payload_sha256(profile, "profile_sha256"):
        raise ContractError("visual style profile integrity check failed")
    preset_id = str(profile.get("preset_id") or "")
    try:
        expected = _style_profile_payload(preset_id, _STYLE_PRESETS[preset_id])
    except KeyError as exc:
        raise ContractError(f"unsupported visual style preset: {preset_id}") from exc
    if profile != expected:
        raise ContractError("visual style profile drifted from the reviewed preset")


def build_content_visual_routing_plan(
    packaging_plan_path: str | Path,
    routing_spec_path: str | Path,
    style_profile_path: str | Path,
    *,
    safe_media_scene_plan_path: str | Path | None = None,
    smart_reframe_plan_path: str | Path | None = None,
    music_handoff_path: str | Path | None = None,
) -> dict[str, Any]:
    packaging_path = Path(packaging_plan_path).expanduser().resolve()
    spec_path = Path(routing_spec_path).expanduser().resolve()
    style_path = Path(style_profile_path).expanduser().resolve()
    packaging = read_json_object(packaging_path, "video packaging plan")
    spec = read_json_object(spec_path, "content visual routing spec")
    style = read_json_object(style_path, "visual style profile")
    _validate_packaging(packaging)
    _validate_routing_spec(spec, packaging)
    validate_visual_style_profile(style)

    inputs: dict[str, Any] = {
        "packaging_plan": artifact_reference(packaging_path, "video packaging plan"),
        "routing_spec": artifact_reference(spec_path, "content visual routing spec"),
        "visual_style_profile": artifact_reference(style_path, "visual style profile"),
    }
    scene_media = _packaging_media_status(packaging)
    if safe_media_scene_plan_path is not None:
        safe_path = Path(safe_media_scene_plan_path).expanduser().resolve()
        safe_plan = read_json_object(safe_path, "safe media scene plan")
        validate_safe_media_scene_plan(safe_plan)
        if _object(safe_plan.get("source_packaging_plan")) != inputs["packaging_plan"]:
            raise ContractError("safe media scene plan must bind the routing packaging")
        inputs["safe_media_scene_plan"] = artifact_reference(
            safe_path, "safe media scene plan"
        )
        scene_media.update(_safe_scene_media_status(safe_plan))

    if smart_reframe_plan_path is not None:
        reframe_path = Path(smart_reframe_plan_path).expanduser().resolve()
        reframe_plan = read_json_object(reframe_path, "smart reframe plan")
        validate_smart_reframe_plan(reframe_plan)
        if reframe_plan["summary"]["status"] != "candidate_ready":
            raise ContractError("visual routing cannot bind a blocked reframe plan")
        inputs["smart_reframe_plan"] = artifact_reference(
            reframe_path, "smart reframe plan"
        )

    music_ready = False
    music_sha256: str | None = None
    if music_handoff_path is not None:
        music_path = Path(music_handoff_path).expanduser().resolve()
        music = read_json_object(music_path, "HyperFrames music handoff")
        validate_hyperframes_music_handoff(music)
        inputs["hyperframes_music_handoff"] = artifact_reference(
            music_path, "HyperFrames music handoff"
        )
        music_ready = True
        music_sha256 = str(music["handoff_sha256"])

    scene_routes = _project_scene_routes(
        packaging,
        spec,
        scene_media=scene_media,
        music_ready=music_ready,
        style_sha256=str(style["profile_sha256"]),
        music_sha256=music_sha256,
    )
    plan: dict[str, Any] = {
        "schema": CONTENT_VISUAL_ROUTING_PLAN_SCHEMA,
        "inputs": inputs,
        "timing_authority": {
            "canonical": "video_packaging_plan",
            "subtitle_srt_role": "projection_only",
            "music_audiomap_role": "music_cue_projection_only",
            "scene_boundaries_must_not_be_rewritten": True,
        },
        "style": {
            "preset_id": style["preset_id"],
            "profile_sha256": style["profile_sha256"],
        },
        "scene_routes": scene_routes,
        "approval_projection": {
            "storyboard": "existing_approval_record_target_storyboard",
            "keyframe_or_material": "existing_approval_record_target_materials",
            "motion": "existing_approval_record_target_visuals",
            "content_change_invalidates_bound_approvals": True,
            "new_approval_ledger_created": False,
        },
        "local_regeneration_policy": {
            "scope": "scene_only",
            "preserve_unrelated_scene_artifacts": True,
            "invalidate_changed_scene_outputs": True,
            "invalidate_final_render_and_release_gate": True,
            "preserve_packaging_and_raw_evidence": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "automatic_provider_fallback": False,
            "provider_execution_performed": False,
            "upload_performed": False,
            "publish_performed": False,
            "second_pipeline_created": False,
            "second_state_machine_created": False,
            "second_ffmpeg_outlet_created": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_content_visual_routing_plan(plan)
    return plan


def validate_content_visual_routing_plan(plan: dict[str, Any]) -> None:
    _validate_schema(
        plan,
        "content_visual_routing_plan.v1.schema.json",
        "content visual routing plan",
    )
    if plan.get("schema") != CONTENT_VISUAL_ROUTING_PLAN_SCHEMA:
        raise ContractError("unsupported content visual routing plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("content visual routing plan integrity check failed")
    inputs = _object(plan.get("inputs"))
    packaging_ref = _object(inputs.get("packaging_plan"))
    packaging_path = validate_artifact_reference(packaging_ref, "video packaging plan")
    spec_path = validate_artifact_reference(
        _object(inputs.get("routing_spec")), "content visual routing spec"
    )
    style_path = validate_artifact_reference(
        _object(inputs.get("visual_style_profile")), "visual style profile"
    )
    packaging = read_json_object(packaging_path, "video packaging plan")
    spec = read_json_object(spec_path, "content visual routing spec")
    style = read_json_object(style_path, "visual style profile")
    _validate_packaging(packaging)
    _validate_routing_spec(spec, packaging)
    validate_visual_style_profile(style)
    if _object(plan.get("style")) != {
        "preset_id": style["preset_id"],
        "profile_sha256": style["profile_sha256"],
    }:
        raise ContractError("content visual routing style binding drifted")

    scene_media = _packaging_media_status(packaging)
    if "safe_media_scene_plan" in inputs:
        safe_path = validate_artifact_reference(
            _object(inputs["safe_media_scene_plan"]), "safe media scene plan"
        )
        safe_plan = read_json_object(safe_path, "safe media scene plan")
        validate_safe_media_scene_plan(safe_plan)
        if _object(safe_plan.get("source_packaging_plan")) != packaging_ref:
            raise ContractError("safe media scene plan packaging binding drifted")
        scene_media.update(_safe_scene_media_status(safe_plan))
    if "smart_reframe_plan" in inputs:
        reframe_path = validate_artifact_reference(
            _object(inputs["smart_reframe_plan"]), "smart reframe plan"
        )
        reframe_plan = read_json_object(reframe_path, "smart reframe plan")
        validate_smart_reframe_plan(reframe_plan)
        if reframe_plan["summary"]["status"] != "candidate_ready":
            raise ContractError("visual routing smart reframe plan is blocked")
    music_ready = "hyperframes_music_handoff" in inputs
    music_sha256: str | None = None
    if music_ready:
        music_path = validate_artifact_reference(
            _object(inputs["hyperframes_music_handoff"]),
            "HyperFrames music handoff",
        )
        music = read_json_object(music_path, "HyperFrames music handoff")
        validate_hyperframes_music_handoff(music)
        music_sha256 = str(music["handoff_sha256"])
    expected_routes = _project_scene_routes(
        packaging,
        spec,
        scene_media=scene_media,
        music_ready=music_ready,
        style_sha256=str(style["profile_sha256"]),
        music_sha256=music_sha256,
    )
    if plan.get("scene_routes") != expected_routes:
        raise ContractError("content visual scene routes drifted from their inputs")
    if _object(plan.get("timing_authority")) != {
        "canonical": "video_packaging_plan",
        "subtitle_srt_role": "projection_only",
        "music_audiomap_role": "music_cue_projection_only",
        "scene_boundaries_must_not_be_rewritten": True,
    }:
        raise ContractError("content visual timing authority is invalid")
    if _object(plan.get("approval_projection")) != {
        "storyboard": "existing_approval_record_target_storyboard",
        "keyframe_or_material": "existing_approval_record_target_materials",
        "motion": "existing_approval_record_target_visuals",
        "content_change_invalidates_bound_approvals": True,
        "new_approval_ledger_created": False,
    }:
        raise ContractError("content visual approval projection is invalid")
    if _object(plan.get("local_regeneration_policy")) != {
        "scope": "scene_only",
        "preserve_unrelated_scene_artifacts": True,
        "invalidate_changed_scene_outputs": True,
        "invalidate_final_render_and_release_gate": True,
        "preserve_packaging_and_raw_evidence": True,
    }:
        raise ContractError("content visual local regeneration policy is invalid")
    if _object(plan.get("authority_boundary")) != {
        "candidate_only": True,
        "automatic_provider_fallback": False,
        "provider_execution_performed": False,
        "upload_performed": False,
        "publish_performed": False,
        "second_pipeline_created": False,
        "second_state_machine_created": False,
        "second_ffmpeg_outlet_created": False,
    }:
        raise ContractError("content visual routing authority boundary is invalid")


def build_hyperframes_music_handoff(
    source_root: str | Path,
    audio_path: str | Path,
    audiomap_path: str | Path,
) -> dict[str, Any]:
    source = _hyperframes_music_source(Path(source_root))
    audio = Path(audio_path).expanduser().resolve()
    audiomap_file = Path(audiomap_path).expanduser().resolve()
    audiomap = read_json_object(audiomap_file, "HyperFrames audiomap")
    _validate_audiomap(audiomap, audio)
    pacing = _music_pacing(audiomap)
    handoff: dict[str, Any] = {
        "schema": HYPERFRAMES_MUSIC_HANDOFF_SCHEMA,
        "source": source,
        "audio": artifact_reference(audio, "music-to-video audio"),
        "audiomap": artifact_reference(audiomap_file, "HyperFrames audiomap"),
        "audiomap_summary": {
            "version": audiomap["version"],
            "duration_s": float(_object(audiomap["audio"])["duration_sec"]),
            "pacing": pacing,
            "energy_phase_count": len(audiomap.get("energy_phases") or []),
            "key_moment_count": len(audiomap.get("key_moments") or []),
            "phrase_count": len(audiomap.get("phrases") or []),
        },
        "integration_policy": {
            "analyzer": "upstream_analyze_beatgrid_only",
            "beat_cut_only_when_rhythm_is_supported": True,
            "phrase_flow_for_sparse_or_calm_audio": True,
            "packaging_plan_remains_scene_timing_authority": True,
            "single_ffmpeg_outlet_required_after_render": True,
            "no_secondary_audio_analyzer": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "analysis_execution_claimed": False,
            "renderer_execution_performed": False,
            "provider_execution_performed": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    handoff["handoff_sha256"] = payload_sha256(handoff, "handoff_sha256")
    validate_hyperframes_music_handoff(handoff)
    return handoff


def run_hyperframes_music_analysis(
    source_root: str | Path,
    audio_path: str | Path,
    output_audiomap_path: str | Path,
    receipt_path: str | Path,
    *,
    python_executable: str | Path = sys.executable,
    ffmpeg_path: str | Path,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    source = _hyperframes_music_source(Path(source_root))
    audio = Path(audio_path).expanduser().resolve()
    if not audio.is_file():
        raise ContractError(f"music-to-video audio not found: {audio}")
    output = Path(output_audiomap_path).expanduser().resolve()
    receipt_file = Path(receipt_path).expanduser().resolve()
    python = _resolve_executable(python_executable, "Python")
    ffmpeg = _resolve_executable(ffmpeg_path, "FFmpeg")
    if output == audio or receipt_file in {audio, output}:
        raise ContractError("music analysis outputs must not overwrite bound artifacts")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp.json")
    analyzer_ref = _object(_object(source["files"])["analyzer"])
    analyzer = validate_artifact_reference(
        analyzer_ref, "HyperFrames music-to-video analyzer"
    )
    command = [
        str(python),
        str(analyzer),
        str(audio),
        "-o",
        str(temporary),
    ]
    try:
        env = os.environ.copy()
        env["PATH"] = f"{ffmpeg.parent}{os.pathsep}{env.get('PATH', '')}"
        result = subprocess.run(
            command,
            cwd=Path(str(source["source_root"])),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=float(timeout_s),
            check=False,
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown failure")[-1600:]
            raise ContractError(f"HyperFrames music analysis failed: {detail.strip()}")
        audiomap = read_json_object(temporary, "temporary HyperFrames audiomap")
        _validate_audiomap(audiomap, audio)
        atomic_write_json(audiomap, output)
    except subprocess.TimeoutExpired as exc:
        raise ContractError("HyperFrames music analysis timed out") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    log_path = receipt_file.with_name(f"{receipt_file.stem}.log")
    atomic_write_text(
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}\n",
        log_path,
    )
    receipt: dict[str, Any] = {
        "schema": HYPERFRAMES_MUSIC_ANALYSIS_RECEIPT_SCHEMA,
        "status": "completed_local_analysis",
        "source": source,
        "source_audio": artifact_reference(audio, "music-to-video audio"),
        "output_audiomap": artifact_reference(output, "HyperFrames audiomap"),
        "execution": {
            "python_executable": str(python),
            "analyzer": analyzer_ref,
            "ffmpeg": artifact_reference(ffmpeg, "HyperFrames music analysis FFmpeg"),
            "returncode": result.returncode,
            "log": artifact_reference(log_path, "HyperFrames music analysis log"),
            "timeout_s": float(timeout_s),
        },
        "authority_boundary": {
            "candidate_only": True,
            "local_analysis_performed": True,
            "renderer_execution_performed": False,
            "provider_execution_performed": False,
            "network_execution_performed": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_hyperframes_music_analysis_receipt(receipt)
    atomic_write_json(receipt, receipt_file)
    return receipt


def validate_hyperframes_music_analysis_receipt(receipt: dict[str, Any]) -> None:
    _validate_schema(
        receipt,
        "hyperframes_music_analysis_receipt.v1.schema.json",
        "HyperFrames music analysis receipt",
    )
    if receipt.get("schema") != HYPERFRAMES_MUSIC_ANALYSIS_RECEIPT_SCHEMA:
        raise ContractError("unsupported HyperFrames music analysis receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("HyperFrames music analysis receipt integrity check failed")
    source = _object(receipt.get("source"))
    root = Path(str(source.get("source_root") or "")).expanduser().resolve()
    if source != _hyperframes_music_source(root):
        raise ContractError("HyperFrames music analysis source evidence drifted")
    audio = validate_artifact_reference(
        _object(receipt.get("source_audio")), "music-to-video audio"
    )
    audiomap_path = validate_artifact_reference(
        _object(receipt.get("output_audiomap")), "HyperFrames audiomap"
    )
    _validate_audiomap(read_json_object(audiomap_path, "HyperFrames audiomap"), audio)
    execution = _object(receipt.get("execution"))
    validate_artifact_reference(
        _object(execution.get("analyzer")), "HyperFrames music-to-video analyzer"
    )
    validate_artifact_reference(
        _object(execution.get("ffmpeg")), "HyperFrames music analysis FFmpeg"
    )
    validate_artifact_reference(
        _object(execution.get("log")), "HyperFrames music analysis log"
    )
    if (
        receipt.get("status") != "completed_local_analysis"
        or execution.get("returncode") != 0
    ):
        raise ContractError("HyperFrames music analysis did not complete")
    if _object(receipt.get("authority_boundary")) != {
        "candidate_only": True,
        "local_analysis_performed": True,
        "renderer_execution_performed": False,
        "provider_execution_performed": False,
        "network_execution_performed": False,
        "upload_performed": False,
        "publish_performed": False,
    }:
        raise ContractError("HyperFrames music analysis authority boundary is invalid")


def validate_hyperframes_music_handoff(handoff: dict[str, Any]) -> None:
    _validate_schema(
        handoff,
        "hyperframes_music_to_video_handoff.v1.schema.json",
        "HyperFrames music handoff",
    )
    if handoff.get("schema") != HYPERFRAMES_MUSIC_HANDOFF_SCHEMA:
        raise ContractError("unsupported HyperFrames music handoff schema")
    if handoff.get("handoff_sha256") != payload_sha256(handoff, "handoff_sha256"):
        raise ContractError("HyperFrames music handoff integrity check failed")
    source = _object(handoff.get("source"))
    root = Path(str(source.get("source_root") or "")).expanduser().resolve()
    if source != _hyperframes_music_source(root):
        raise ContractError("HyperFrames music source evidence drifted")
    audio_path = validate_artifact_reference(
        _object(handoff.get("audio")), "music-to-video audio"
    )
    audiomap_path = validate_artifact_reference(
        _object(handoff.get("audiomap")), "HyperFrames audiomap"
    )
    audiomap = read_json_object(audiomap_path, "HyperFrames audiomap")
    _validate_audiomap(audiomap, audio_path)
    summary = _object(handoff.get("audiomap_summary"))
    if summary != {
        "version": audiomap["version"],
        "duration_s": float(_object(audiomap["audio"])["duration_sec"]),
        "pacing": _music_pacing(audiomap),
        "energy_phase_count": len(audiomap.get("energy_phases") or []),
        "key_moment_count": len(audiomap.get("key_moments") or []),
        "phrase_count": len(audiomap.get("phrases") or []),
    }:
        raise ContractError("HyperFrames music handoff summary drifted")
    if _object(handoff.get("integration_policy")) != {
        "analyzer": "upstream_analyze_beatgrid_only",
        "beat_cut_only_when_rhythm_is_supported": True,
        "phrase_flow_for_sparse_or_calm_audio": True,
        "packaging_plan_remains_scene_timing_authority": True,
        "single_ffmpeg_outlet_required_after_render": True,
        "no_secondary_audio_analyzer": True,
    }:
        raise ContractError("HyperFrames music handoff integration policy is invalid")
    boundary = _object(handoff.get("authority_boundary"))
    if boundary != {
        "candidate_only": True,
        "analysis_execution_claimed": False,
        "renderer_execution_performed": False,
        "provider_execution_performed": False,
        "upload_performed": False,
        "publish_performed": False,
    }:
        raise ContractError("HyperFrames music handoff authority boundary is invalid")


def hyperframes_music_doctor(source_root: str | Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "video_creation_pipeline.hyperframes_music_doctor.v1",
        "status": "not_supplied",
        "analysis_execution_performed": False,
        "renderer_execution_performed": False,
        "network_execution_performed": False,
    }
    if source_root is None:
        return result
    try:
        result["source"] = _hyperframes_music_source(Path(source_root))
    except ContractError as exc:
        result.update({"status": "blocked_source", "error": str(exc)})
        return result
    missing = [
        name
        for name in ("librosa", "numpy", "soundfile")
        if importlib.util.find_spec(name) is None
    ]
    result["optional_dependencies"] = {
        "required": ["librosa", "numpy", "soundfile", "ffmpeg", "ffprobe"],
        "missing_python_modules": missing,
        "missing_binaries": [
            name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None
        ],
    }
    result["status"] = (
        "blocked_missing_optional_dependencies"
        if missing or result["optional_dependencies"]["missing_binaries"]
        else "ready_for_explicit_local_analysis"
    )
    return result


def write_visual_routing_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    schema = payload.get("schema")
    if schema == VISUAL_STYLE_PROFILE_SCHEMA:
        validate_visual_style_profile(payload)
    elif schema == CONTENT_VISUAL_ROUTING_PLAN_SCHEMA:
        validate_content_visual_routing_plan(payload)
    elif schema == HYPERFRAMES_MUSIC_HANDOFF_SCHEMA:
        validate_hyperframes_music_handoff(payload)
    elif schema == HYPERFRAMES_MUSIC_ANALYSIS_RECEIPT_SCHEMA:
        validate_hyperframes_music_analysis_receipt(payload)
    else:
        raise ContractError("unsupported visual routing contract schema")
    return atomic_write_json(payload, output_path)


def _project_scene_routes(
    packaging: dict[str, Any],
    spec: dict[str, Any],
    *,
    scene_media: dict[str, bool],
    music_ready: bool,
    style_sha256: str,
    music_sha256: str | None,
) -> list[dict[str, Any]]:
    specs = {
        str(_object(row).get("scene_id") or ""): _object(row) for row in spec["scenes"]
    }
    routes: list[dict[str, Any]] = []
    for raw in packaging["scenes"]:
        scene = _object(raw)
        scene_id = str(scene["scene_id"])
        row = specs[scene_id]
        kind = str(row["content_kind"])
        route, status, reason = _select_route(
            kind,
            has_local_media=scene_media.get(scene_id, False),
            generation_allowed=row.get("generation_allowed") is True,
            music_ready=music_ready,
        )
        preferred = str(row.get("preferred_route") or "auto")
        if preferred != "auto" and preferred != route:
            raise ContractError(
                f"scene {scene_id} preferred route {preferred} is incompatible; "
                f"deterministic route is {route}"
            )
        start = _number(scene.get("start_s"), f"scene {scene_id} start")
        end = _number(scene.get("end_s"), f"scene {scene_id} end")
        evidence = scene.get("evidence_refs")
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        if kind == "evidence_document" and evidence_count == 0:
            raise ContractError(f"evidence scene {scene_id} has no evidence refs")
        routes.append(
            {
                "scene_id": scene_id,
                "start_s": start,
                "end_s": end,
                "content_kind": kind,
                "communication_goal": str(row["communication_goal"]),
                "visual_route": route,
                "renderer": _renderer(route),
                "execution_status": status,
                "decision_reason": reason,
                "material_ready": scene_media.get(scene_id, False),
                "evidence_ref_count": evidence_count,
                "review_stages": _review_stages(route),
                "generation_scope": {
                    "scene_local": True,
                    "regeneration_key": canonical_json_sha256(
                        {
                            "scene": scene,
                            "spec": row,
                            "route": route,
                            "style_sha256": style_sha256,
                            "music_sha256": (
                                music_sha256 if kind == "music_driven_promo" else None
                            ),
                        }
                    ),
                    "preserve_other_scenes": True,
                },
            }
        )
    return routes


def _select_route(
    kind: str,
    *,
    has_local_media: bool,
    generation_allowed: bool,
    music_ready: bool,
) -> tuple[str, str, str]:
    if kind == "evidence_document":
        return (
            "hyperframes_evidence_focus",
            "candidate_ready",
            "source evidence needs pixel-preserving focus motion",
        )
    if kind in {
        "concept_explanation",
        "process_sequence",
        "comparison",
        "number_explanation",
    }:
        return (
            "remotion_knowledge_animation",
            "candidate_ready",
            "structured knowledge is best expressed with deterministic code animation",
        )
    if kind == "transition_or_emphasis":
        return (
            "remotion_kinetic_typography",
            "candidate_ready",
            "short emphasis needs deterministic type motion rather than generated video",
        )
    if kind == "music_driven_promo":
        if music_ready:
            return (
                "hyperframes_music_to_video",
                "candidate_ready",
                "reviewed audiomap is available for beat or phrase pacing",
            )
        return (
            "missing_material",
            "blocked_missing_music_audiomap",
            "music-driven route requires a hash-bound HyperFrames audiomap handoff",
        )
    if has_local_media:
        return (
            "existing_media_broll",
            "candidate_ready",
            "hash-bound local media is available",
        )
    if kind in {"real_world_action", "emotional_story"} and generation_allowed:
        return (
            "provider_generated_broll",
            "handoff_required",
            "no local material exists and generation was explicitly allowed",
        )
    return (
        "missing_material",
        "blocked_missing_material",
        "the scene requires source media and no authorized material is available",
    )


def _renderer(route: str) -> str:
    if route.startswith("remotion_"):
        return "remotion"
    if route.startswith("hyperframes_"):
        return "hyperframes"
    if route == "provider_generated_broll":
        return "provider_handoff"
    if route == "existing_media_broll":
        return "existing_single_ffmpeg_outlet"
    return "none"


def _review_stages(route: str) -> list[dict[str, str]]:
    if route == "provider_generated_broll":
        stages = ("storyboard", "keyframe_or_material", "motion")
    elif route in {
        "remotion_knowledge_animation",
        "remotion_kinetic_typography",
        "hyperframes_music_to_video",
    }:
        stages = ("storyboard", "motion")
    elif route in {"hyperframes_evidence_focus", "existing_media_broll"}:
        stages = ("storyboard", "keyframe_or_material")
    else:
        stages = ("storyboard",)
    return [
        {"stage": stage, "approval_target": _approval_target(stage)} for stage in stages
    ]


def _approval_target(stage: str) -> str:
    return {
        "storyboard": "storyboard",
        "keyframe_or_material": "materials",
        "motion": "visuals",
    }[stage]


def _validate_routing_spec(spec: dict[str, Any], packaging: dict[str, Any]) -> None:
    _validate_schema(
        spec,
        "content_visual_routing_spec.v1.schema.json",
        "content visual routing spec",
    )
    if spec.get("schema") != CONTENT_VISUAL_ROUTING_SPEC_SCHEMA:
        raise ContractError("unsupported content visual routing spec schema")
    scenes = spec.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ContractError("content visual routing spec requires scenes")
    packaging_ids = [
        str(_object(row).get("scene_id") or "") for row in packaging["scenes"]
    ]
    seen: set[str] = set()
    for raw in scenes:
        row = _object(raw)
        scene_id = str(row.get("scene_id") or "").strip()
        if scene_id in seen or scene_id not in packaging_ids:
            raise ContractError(
                f"content visual routing spec scene is invalid: {scene_id}"
            )
        seen.add(scene_id)
        if row.get("content_kind") not in CONTENT_KINDS:
            raise ContractError(f"scene {scene_id} has an unsupported content kind")
        if not str(row.get("communication_goal") or "").strip():
            raise ContractError(f"scene {scene_id} lacks a communication goal")
        preferred = str(row.get("preferred_route") or "auto")
        if preferred != "auto" and preferred not in VISUAL_ROUTES:
            raise ContractError(f"scene {scene_id} has an unsupported preferred route")
        if not isinstance(row.get("generation_allowed", False), bool):
            raise ContractError(f"scene {scene_id} generation_allowed must be boolean")
    if set(seen) != set(packaging_ids) or len(seen) != len(packaging_ids):
        raise ContractError(
            "routing spec must cover every packaging scene exactly once"
        )


def _validate_packaging(packaging: dict[str, Any]) -> None:
    if packaging.get("schema") != VIDEO_PACKAGING_PLAN_SCHEMA:
        raise ContractError("visual routing requires a video packaging plan")
    scenes = packaging.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ContractError("video packaging plan requires scenes")
    seen: set[str] = set()
    cursor: float | None = None
    for raw in scenes:
        row = _object(raw)
        scene_id = str(row.get("scene_id") or "").strip()
        start = _number(row.get("start_s"), f"scene {scene_id} start")
        end = _number(row.get("end_s"), f"scene {scene_id} end")
        if not scene_id or scene_id in seen or end <= start:
            raise ContractError("video packaging scene is invalid")
        if cursor is not None and start < cursor - 0.001:
            raise ContractError("video packaging scenes overlap")
        seen.add(scene_id)
        cursor = end


def _packaging_media_status(packaging: dict[str, Any]) -> dict[str, bool]:
    return {
        str(_object(row)["scene_id"]): (
            str(_object(row).get("material_status") or "missing_material")
            not in {"", "missing_material"}
        )
        for row in packaging["scenes"]
    }


def _safe_scene_media_status(plan: dict[str, Any]) -> dict[str, bool]:
    return {
        str(_object(row)["scene_id"]): (
            _object(row).get("assignment_status") == "layout_ready"
        )
        for row in plan["scenes"]
    }


def _hyperframes_music_source(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if _git_head(root) != HYPERFRAMES_MUSIC_SOURCE_COMMIT:
        raise ContractError("HyperFrames music-to-video source commit drifted")
    files = {
        "skill": root / "skills" / "music-to-video" / "SKILL.md",
        "analyzer": root
        / "skills"
        / "music-to-video"
        / "scripts"
        / "analyze-beatgrid.py",
        "frame_skeleton": root
        / "skills"
        / "music-to-video"
        / "references"
        / "frame-skeleton.md",
        "storyboard_format": root
        / "skills"
        / "music-to-video"
        / "references"
        / "storyboard-format.md",
        "license": root / "LICENSE",
    }
    for label, path in files.items():
        if not path.is_file():
            raise ContractError(f"HyperFrames music-to-video {label} is missing")
    license_text = files["license"].read_text(encoding="utf-8", errors="replace")
    if "Apache License" not in license_text or "Version 2.0" not in license_text:
        raise ContractError("HyperFrames music-to-video license evidence is invalid")
    return {
        "name": "heygen-com/hyperframes music-to-video",
        "source_root": str(root),
        "source_commit": HYPERFRAMES_MUSIC_SOURCE_COMMIT,
        "source_license": "Apache-2.0",
        "files": {
            label: artifact_reference(path, f"HyperFrames music-to-video {label}")
            for label, path in files.items()
        },
        "reuse_mode": "direct_upstream_analyzer_with_project_contract_adapter",
    }


def _validate_audiomap(audiomap: dict[str, Any], audio_path: Path) -> None:
    if audiomap.get("version") != 2:
        raise ContractError("HyperFrames audiomap version is unsupported")
    audio = _object(audiomap.get("audio"))
    duration = _number(audio.get("duration_sec"), "audiomap duration")
    if duration <= 0:
        raise ContractError("HyperFrames audiomap duration must be positive")
    mapped_path = Path(str(audio.get("path") or "")).expanduser()
    if not mapped_path.is_absolute():
        mapped_path = (audio_path.parent / mapped_path).resolve()
    else:
        mapped_path = mapped_path.resolve()
    if mapped_path != audio_path.resolve():
        raise ContractError("HyperFrames audiomap does not bind the supplied audio")
    phases = audiomap.get("energy_phases")
    if not isinstance(phases, list) or not phases:
        raise ContractError("HyperFrames audiomap requires energy phases")
    for phase in phases:
        row = _object(phase)
        start = _number(row.get("start"), "audiomap phase start")
        end = _number(row.get("end"), "audiomap phase end")
        zero_length_terminal_marker = (
            abs(end - start) <= 0.001 and abs(end - duration) <= 0.001
        )
        if (
            end < start
            or (end <= start and not zero_length_terminal_marker)
            or start < 0
            or end > duration + 1.001
        ):
            raise ContractError("HyperFrames audiomap phase timing is invalid")


def _music_pacing(audiomap: dict[str, Any]) -> str:
    phases = [_object(row) for row in audiomap.get("energy_phases") or []]
    rolls = audiomap.get("rolls") or []
    events = audiomap.get("events") or []
    duration = float(_object(audiomap["audio"])["duration_sec"])
    onset_rate = len(events) / max(duration, 0.001)
    dense = any(row.get("density") == "dense" for row in phases)
    has_grid = len(_object(audiomap.get("grid")).get("beats_sec") or []) >= 4
    return (
        "beat_cut"
        if rolls or dense or (onset_rate >= 1.8 and has_grid)
        else "phrase_flow"
    )


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
        raise ContractError("HyperFrames source commit could not be read") from exc
    if result.returncode != 0:
        raise ContractError("HyperFrames source commit could not be read")
    return result.stdout.strip().lower()


def _resolve_executable(value: str | Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    discovered = shutil.which(str(value))
    if discovered:
        return Path(discovered).resolve()
    raise ContractError(f"HyperFrames music analysis {label} not found: {value}")


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    schema = read_json_object(
        Path(__file__).with_name("schemas") / filename, f"{label} schema"
    )
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise ContractError(f"{label} must be finite")
    return number
