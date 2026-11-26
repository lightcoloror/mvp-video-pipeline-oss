from __future__ import annotations

import math
import re
import subprocess
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_copy_promote,
    atomic_write_json,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .external_motion_assets import (
    CLIPKIT_SFX_PACK_RECEIPT_SCHEMA,
    profile_paths_from_clipkit_pack_receipt,
    validate_clipkit_sfx_pack_receipt,
    validate_external_motion_selection_plan,
)
from .execution_receipts import (
    probe_media_metadata,
    validate_ffmpeg_execution_receipt,
)
from .music_pipeline import _load_srt_subtitles, _load_word_timestamps
from .stock_media import (
    SFX_DENSITY_TARGETS,
    build_licensed_media_asset,
    build_sound_effect_cue_sheet,
    build_sound_effect_edit_plan,
    build_sound_effect_profile,
    execute_sound_effect_mix,
    validate_sound_effect_cue_sheet,
    validate_sound_effect_edit_plan,
    validate_sound_effect_profile,
    write_stock_media_contract,
)
from .video_talkcraft_adapter import validate_video_talkcraft_recipe_plan

MOTION_SFX_SOURCE_MANIFEST_SCHEMA = (
    "video_creation_pipeline.motion_sfx_source_manifest.v1"
)
MOTION_SFX_PACK_RECEIPT_SCHEMA = "video_creation_pipeline.motion_sfx_pack_receipt.v1"
MOTION_SFX_BATCH_RECEIPT_SCHEMA = "video_creation_pipeline.motion_sfx_batch_receipt.v1"

VIDEO_SHOTCRAFT_COMMIT = "0d6f0b57f0d4d6700761644c07f7ef03c3e50234"
WARM_PAPER_WOOD_PACK_ID = "warm-paper-wood-v1"

_ROUTE_SCOPE = [
    "existing_media_broll",
    "hyperframes_evidence_focus",
    "hyperframes_music_to_video",
    "missing_material",
    "provider_generated_broll",
    "remotion_knowledge_animation",
    "remotion_kinetic_typography",
    "unrouted_contract_preview",
]

_PACK_ITEMS = (
    {
        "asset_id": "warm-paper-move",
        "relative_path": "assets/audio/sfx/paper/paper-move-quick.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/2380/2380-preview.mp3",
        "title": "Paper quick movement",
        "category": "paper_transition",
        "tags": ["orientation", "paper", "soft", "transition", "whoosh"],
        "description": "Warm paper movement for restrained scene entrances.",
    },
    {
        "asset_id": "warm-page-turn",
        "relative_path": "assets/audio/sfx/paper/paper-page-turn.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/1104/1104-preview.mp3",
        "title": "Page turn single",
        "category": "paper_reveal",
        "tags": ["orientation", "paper", "reveal", "soft", "transition"],
        "description": "Short page turn for evidence and card reveals.",
    },
    {
        "asset_id": "warm-paper-slide",
        "relative_path": "assets/audio/sfx/paper/paper-slide.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/1530/1530-preview.mp3",
        "title": "Paper slide",
        "category": "paper_causality",
        "tags": ["causality", "paper", "soft", "transition"],
        "description": "Paper slide for relationship and progression cues.",
    },
    {
        "asset_id": "warm-paper-staple",
        "relative_path": "assets/audio/sfx/paper/paper-staple.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/2995/2995-preview.mp3",
        "title": "Stapling paper",
        "category": "paper_commitment",
        "tags": ["commitment", "confirmation", "impact", "paper"],
        "description": "Dry paper staple for a firm but non-cinematic commitment cue.",
    },
    {
        "asset_id": "warm-wood-tap",
        "relative_path": "assets/audio/sfx/ui/switch-tap.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/2585/2585-preview.mp3",
        "title": "Light switch tap",
        "category": "wood_emphasis",
        "tags": ["commitment", "emphasis", "soft", "tap", "wood"],
        "description": "Dry physical tap adapted as a restrained wooden emphasis.",
    },
    {
        "asset_id": "warm-low-confirm",
        "relative_path": "assets/audio/sfx/ui/switch-light.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/2579/2579-preview.mp3",
        "title": "Light switch sound",
        "category": "wood_resolution",
        "tags": ["commitment", "confirm", "resolution", "tap", "wood"],
        "description": "Short physical confirmation for conclusions and resolved states.",
    },
    {
        "asset_id": "warm-weak-emphasis",
        "relative_path": "assets/audio/sfx/impact/hit-weak.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/2148/2148-preview.mp3",
        "title": "Weak hit impact",
        "category": "restrained_caution",
        "tags": ["caution", "emphasis", "low", "soft"],
        "description": "Weak low impact for risk and limitation cues.",
    },
    {
        "asset_id": "warm-pencil-mark",
        "relative_path": "assets/audio/sfx/text/pencil-write-short.mp3",
        "item_url": "https://assets.mixkit.co/active_storage/sfx/2376/2376-preview.mp3",
        "title": "Short pencil writing",
        "category": "paper_evidence",
        "tags": ["causality", "draw", "evidence", "paper"],
        "description": "Quiet pencil gesture for evidence and relationship annotations.",
    },
)

_FUNCTION_POLICY = {
    "orientation": {
        "asset_id": "warm-paper-move",
        "duration_s": 0.60,
        "gain_db": -20.0,
        "required_tags": ["orientation"],
        "optional_tags": ["paper", "soft"],
        "query": "warm soft paper orientation",
    },
    "transition": {
        "asset_id": "warm-page-turn",
        "duration_s": 0.45,
        "gain_db": -19.0,
        "required_tags": ["transition"],
        "optional_tags": ["paper", "soft"],
        "query": "warm soft paper transition",
    },
    "emphasis": {
        "asset_id": "warm-wood-tap",
        "duration_s": 0.28,
        "gain_db": -19.0,
        "required_tags": ["emphasis"],
        "optional_tags": ["tap", "wood"],
        "query": "restrained warm wood tap emphasis",
    },
    "caution": {
        "asset_id": "warm-weak-emphasis",
        "duration_s": 0.42,
        "gain_db": -21.0,
        "required_tags": ["caution"],
        "optional_tags": ["low", "soft"],
        "query": "restrained low soft caution",
    },
    "commitment": {
        "asset_id": "warm-paper-staple",
        "duration_s": 0.40,
        "gain_db": -20.0,
        "required_tags": ["commitment"],
        "optional_tags": ["paper", "impact"],
        "query": "dry paper commitment confirmation",
    },
    "causality": {
        "asset_id": "warm-pencil-mark",
        "duration_s": 0.70,
        "gain_db": -24.0,
        "required_tags": ["causality"],
        "optional_tags": ["evidence", "paper"],
        "query": "quiet pencil evidence causality",
    },
    "resolution": {
        "asset_id": "warm-low-confirm",
        "duration_s": 0.17,
        "gain_db": -19.0,
        "required_tags": ["resolution"],
        "optional_tags": ["confirm", "wood"],
        "query": "short warm physical resolution",
    },
}

_CAUTION_TERMS = (
    "风险",
    "免责",
    "不保证",
    "不能",
    "限制",
    "注意",
    "费用",
    "退保",
    "损失",
    "例外",
)
_COMMITMENT_TERMS = ("必须", "关键", "记住", "第一", "第二", "第三", "第四")
_RESOLUTION_TERMS = ("所以", "总结", "最后", "建议", "结论", "这样")
_CAUSALITY_TERMS = ("因为", "因此", "导致", "关系", "取决于")
_CTA_TERMS = ("关注", "点赞", "转发", "私信", "评论区", "立即咨询", "联系我们")


def build_motion_sfx_source_manifest(
    packaging_plan_path: str | Path,
    *,
    semantic_motion_plan_path: str | Path | None = None,
    remotion_project_data_path: str | Path | None = None,
    visual_routing_plan_path: str | Path | None = None,
    script_anchor_path: str | Path | None = None,
    script_path: str | Path | None = None,
    subtitle_path: str | Path | None = None,
    word_timestamps_path: str | Path | None = None,
    video_talkcraft_recipe_plan_path: str | Path | None = None,
    external_motion_selection_plan_path: str | Path | None = None,
    density_profile: str = "balanced",
    palette_id: str = WARM_PAPER_WOOD_PACK_ID,
) -> dict[str, Any]:
    packaging = Path(packaging_plan_path).expanduser().resolve()
    packaging_payload = read_json_object(packaging, "video packaging plan")
    scenes, duration_s = _packaging_scenes(packaging_payload)
    if density_profile not in SFX_DENSITY_TARGETS:
        raise ContractError("motion SFX density profile is unsupported")

    inputs: dict[str, Any] = {
        "packaging_plan": artifact_reference(packaging, "video packaging plan")
    }
    optional_paths = {
        "semantic_motion_plan": semantic_motion_plan_path,
        "remotion_project_data": remotion_project_data_path,
        "visual_routing_plan": visual_routing_plan_path,
        "script_anchor": script_anchor_path,
        "script": script_path,
        "subtitles": subtitle_path,
        "word_timestamps": word_timestamps_path,
        "video_talkcraft_recipe_plan": video_talkcraft_recipe_plan_path,
        "external_motion_selection_plan": external_motion_selection_plan_path,
    }
    for role, raw_path in optional_paths.items():
        if raw_path is not None:
            inputs[role] = artifact_reference(raw_path, role.replace("_", " "))

    if "word_timestamps" in inputs:
        timing_authority = "final_audio_or_srt"
        timing_detail = "word_timestamps"
    elif "subtitles" in inputs:
        timing_authority = "final_audio_or_srt"
        timing_detail = "srt_subtitles"
    elif (
        "semantic_motion_plan" in inputs
        or "remotion_project_data" in inputs
        or "video_talkcraft_recipe_plan" in inputs
        or "external_motion_selection_plan" in inputs
    ):
        timing_authority = "scene_timeline"
        timing_detail = "renderer_motion_timeline"
    elif "script_anchor" in inputs:
        timing_authority = "script_anchors"
        timing_detail = "script_anchor_intervals"
    else:
        timing_authority = "scene_timeline"
        timing_detail = "packaging_scene_intervals"

    payload: dict[str, Any] = {
        "schema": MOTION_SFX_SOURCE_MANIFEST_SCHEMA,
        "inputs": inputs,
        "timeline": {
            "duration_s": duration_s,
            "scene_count": len(scenes),
            "timing_authority": timing_authority,
            "timing_detail": timing_detail,
            "semantic_timeline_moved": False,
        },
        "projection_policy": {
            "density_profile": density_profile,
            "palette_id": palette_id,
            "route_scope": list(_ROUTE_SCOPE),
            "route_scope_mode": "all_packaging_scenes",
            "explicit_motion_preferred": True,
            "scene_transition_fallback": True,
            "script_only_alignment": "scene_level_candidate_only",
            "minimum_event_gap_s": 1.25,
            "cta_and_customer_interview_quiet": True,
            "automatic_sound_generation": False,
            "automatic_asset_download": False,
        },
        "authority_boundary": {
            "candidate_only": True,
            "source_timeline_modified": False,
            "renderer_timeline_modified": False,
            "second_audio_index_created": False,
            "second_ffmpeg_outlet_created": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    payload["manifest_sha256"] = payload_sha256(payload, "manifest_sha256")
    validate_motion_sfx_source_manifest(payload)
    return payload


def validate_motion_sfx_source_manifest(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MOTION_SFX_SOURCE_MANIFEST_SCHEMA:
        raise ContractError("unsupported motion SFX source manifest schema")
    if payload.get("manifest_sha256") != payload_sha256(payload, "manifest_sha256"):
        raise ContractError("motion SFX source manifest integrity check failed")
    inputs = _object(payload.get("inputs"))
    packaging_path = validate_artifact_reference(
        _object(inputs.get("packaging_plan")), "video packaging plan"
    )
    packaging = read_json_object(packaging_path, "video packaging plan")
    scenes, duration_s = _packaging_scenes(packaging)
    for role, reference in inputs.items():
        if role == "packaging_plan":
            continue
        validate_artifact_reference(_object(reference), role.replace("_", " "))
    _validate_optional_inputs(inputs, packaging_path, scenes)

    timeline = _object(payload.get("timeline"))
    if (
        float(timeline.get("duration_s") or 0) != duration_s
        or int(timeline.get("scene_count") or 0) != len(scenes)
        or timeline.get("timing_authority")
        not in {"final_audio_or_srt", "scene_timeline", "script_anchors"}
        or timeline.get("semantic_timeline_moved") is not False
    ):
        raise ContractError("motion SFX timeline projection drifted")
    policy = _object(payload.get("projection_policy"))
    if (
        policy.get("density_profile") not in SFX_DENSITY_TARGETS
        or policy.get("palette_id") != WARM_PAPER_WOOD_PACK_ID
        or policy.get("route_scope") != _ROUTE_SCOPE
        or policy.get("route_scope_mode") != "all_packaging_scenes"
        or policy.get("explicit_motion_preferred") is not True
        or policy.get("scene_transition_fallback") is not True
        or policy.get("script_only_alignment") != "scene_level_candidate_only"
        or float(policy.get("minimum_event_gap_s") or 0) != 1.25
        or policy.get("cta_and_customer_interview_quiet") is not True
        or policy.get("automatic_sound_generation") is not False
        or policy.get("automatic_asset_download") is not False
    ):
        raise ContractError("motion SFX projection policy is invalid")
    boundary = _object(payload.get("authority_boundary"))
    expected_false = (
        "source_timeline_modified",
        "renderer_timeline_modified",
        "second_audio_index_created",
        "second_ffmpeg_outlet_created",
        "external_provider_called",
        "upload_performed",
        "publish_performed",
    )
    if boundary.get("candidate_only") is not True or any(
        boundary.get(field) is not False for field in expected_false
    ):
        raise ContractError("motion SFX authority boundary is invalid")


def derive_motion_sfx_cue_sheet(
    source_manifest_path: str | Path,
    *,
    purpose: str = "commercial",
    platform: str = "social_media",
) -> dict[str, Any]:
    manifest_path = Path(source_manifest_path).expanduser().resolve()
    manifest = read_json_object(manifest_path, "motion SFX source manifest")
    validate_motion_sfx_source_manifest(manifest)
    inputs = _object(manifest["inputs"])
    packaging = read_json_object(
        validate_artifact_reference(
            _object(inputs["packaging_plan"]), "video packaging plan"
        ),
        "video packaging plan",
    )
    scenes, duration_s = _packaging_scenes(packaging)
    route_map = _load_route_map(inputs)
    narration = _scene_narration(inputs, scenes)
    omissions = _derive_omissions(inputs, scenes, narration)

    candidates: list[dict[str, Any]] = []
    candidates.extend(_scene_candidates(scenes, narration, route_map))
    candidates.extend(_motion_candidates(inputs, narration, scenes))
    candidates.extend(_script_candidates(inputs, narration, scenes))
    candidates = _remove_omitted_candidates(candidates, omissions)
    cues = _select_balanced_candidates(
        candidates,
        duration_s=duration_s,
        density_profile=str(manifest["projection_policy"]["density_profile"]),
    )
    if not cues:
        raise ContractError("no semantically justified sound effect cues were found")
    return build_sound_effect_cue_sheet(
        manifest_path,
        cues,
        timeline_duration_s=duration_s,
        purpose=purpose,
        platform=platform,
        semantic_spotting={
            "timing_authority": manifest["timeline"]["timing_authority"],
            "sound_style": "general_knowledge",
            "density_plan": {
                "profile": manifest["projection_policy"]["density_profile"],
                "target_cues_per_min": list(
                    SFX_DENSITY_TARGETS[
                        str(manifest["projection_policy"]["density_profile"])
                    ]
                ),
                "max_uncovered_semantic_gap_s": _maximum_cue_gap(cues, duration_s),
            },
            "omissions": omissions,
        },
    )


def build_motion_sfx_batch_receipt(
    source_manifest_path: str | Path,
    cue_sheet_path: str | Path,
    *,
    edit_plan_path: str | Path | None = None,
    ffmpeg_receipt_path: str | Path | None = None,
    pack_receipt_path: str | Path | None = None,
) -> dict[str, Any]:
    source_path = Path(source_manifest_path).expanduser().resolve()
    cue_path = Path(cue_sheet_path).expanduser().resolve()
    source = read_json_object(source_path, "motion SFX source manifest")
    cues = read_json_object(cue_path, "sound effect cue sheet")
    validate_motion_sfx_source_manifest(source)
    validate_sound_effect_cue_sheet(cues)
    if _object(cues.get("source")).get("sha256") != file_sha256(source_path):
        raise ContractError("motion SFX cue sheet is stale against its source manifest")
    artifacts: dict[str, Any] = {
        "source_manifest": artifact_reference(
            source_path, "motion SFX source manifest"
        ),
        "cue_sheet": artifact_reference(cue_path, "sound effect cue sheet"),
    }
    render_eligible = False
    status = "cue_sheet_ready"
    if edit_plan_path is not None:
        plan_path = Path(edit_plan_path).expanduser().resolve()
        plan = read_json_object(plan_path, "sound effect edit plan")
        validate_sound_effect_edit_plan(plan)
        artifacts["edit_plan"] = artifact_reference(plan_path, "sound effect edit plan")
        render_eligible = bool(plan["readiness"]["render_eligible"])
        status = "edit_plan_ready" if render_eligible else "missing_material"
    if pack_receipt_path is not None:
        pack_path = Path(pack_receipt_path).expanduser().resolve()
        pack = read_json_object(pack_path, "motion SFX pack receipt")
        _validate_sfx_pack_receipt(pack)
        artifacts["pack_receipt"] = artifact_reference(
            pack_path, "motion SFX pack receipt"
        )
    if ffmpeg_receipt_path is not None:
        if edit_plan_path is None:
            raise ContractError("motion SFX candidate mix requires an edit plan")
        ffmpeg_path = Path(ffmpeg_receipt_path).expanduser().resolve()
        ffmpeg = read_json_object(ffmpeg_path, "sound effect FFmpeg receipt")
        validate_ffmpeg_execution_receipt(ffmpeg)
        if ffmpeg.get("operation") != "sound_effect_mix":
            raise ContractError("motion SFX batch bound an unrelated FFmpeg receipt")
        _validate_ffmpeg_edit_plan_binding(ffmpeg, Path(edit_plan_path).resolve())
        artifacts["ffmpeg_receipt"] = artifact_reference(
            ffmpeg_path, "sound effect FFmpeg receipt"
        )
        status = "candidate_mixed"
    payload: dict[str, Any] = {
        "schema": MOTION_SFX_BATCH_RECEIPT_SCHEMA,
        "status": status,
        "artifacts": artifacts,
        "readiness": {
            "cue_count": len(cues["cues"]),
            "density_within_target": cues["semantic_spotting"]["density_plan"][
                "density_within_target"
            ],
            "render_eligible": render_eligible,
            "candidate_only": True,
            "release_eligible": False,
        },
        "authority_boundary": {
            "semantic_timeline_moved": False,
            "second_ffmpeg_outlet_created": False,
            "external_provider_called": False,
            "automatic_download": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    payload["receipt_sha256"] = payload_sha256(payload, "receipt_sha256")
    validate_motion_sfx_batch_receipt(payload)
    return payload


def validate_motion_sfx_batch_receipt(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MOTION_SFX_BATCH_RECEIPT_SCHEMA:
        raise ContractError("unsupported motion SFX batch receipt schema")
    if payload.get("receipt_sha256") != payload_sha256(payload, "receipt_sha256"):
        raise ContractError("motion SFX batch receipt integrity check failed")
    artifacts = _object(payload.get("artifacts"))
    source_path = validate_artifact_reference(
        _object(artifacts.get("source_manifest")), "motion SFX source manifest"
    )
    cue_path = validate_artifact_reference(
        _object(artifacts.get("cue_sheet")), "sound effect cue sheet"
    )
    validate_motion_sfx_source_manifest(
        read_json_object(source_path, "motion SFX source manifest")
    )
    cue_sheet = read_json_object(cue_path, "sound effect cue sheet")
    validate_sound_effect_cue_sheet(cue_sheet)
    if _object(cue_sheet.get("source")).get("sha256") != file_sha256(source_path):
        raise ContractError("motion SFX cue sheet is stale against its source manifest")
    expected_status = "cue_sheet_ready"
    expected_render_eligible = False
    edit_plan: dict[str, Any] | None = None
    if "edit_plan" in artifacts:
        plan_path = validate_artifact_reference(
            _object(artifacts["edit_plan"]), "sound effect edit plan"
        )
        edit_plan = read_json_object(plan_path, "sound effect edit plan")
        validate_sound_effect_edit_plan(edit_plan)
        expected_render_eligible = bool(edit_plan["readiness"]["render_eligible"])
        expected_status = (
            "edit_plan_ready" if expected_render_eligible else "missing_material"
        )
    if "pack_receipt" in artifacts:
        pack_path = validate_artifact_reference(
            _object(artifacts["pack_receipt"]), "motion SFX pack receipt"
        )
        pack = read_json_object(pack_path, "motion SFX pack receipt")
        _validate_sfx_pack_receipt(pack)
        if edit_plan is not None:
            _validate_pack_edit_plan_binding(pack, edit_plan)
    if "ffmpeg_receipt" in artifacts:
        if edit_plan is None or not expected_render_eligible:
            raise ContractError(
                "motion SFX candidate mix requires a renderable edit plan"
            )
        ffmpeg_path = validate_artifact_reference(
            _object(artifacts["ffmpeg_receipt"]), "sound effect FFmpeg receipt"
        )
        ffmpeg = read_json_object(ffmpeg_path, "sound effect FFmpeg receipt")
        validate_ffmpeg_execution_receipt(ffmpeg)
        if ffmpeg.get("operation") != "sound_effect_mix":
            raise ContractError("motion SFX batch FFmpeg operation drifted")
        plan_path = validate_artifact_reference(
            _object(artifacts["edit_plan"]), "sound effect edit plan"
        )
        _validate_ffmpeg_edit_plan_binding(ffmpeg, plan_path)
        expected_status = "candidate_mixed"
    expected_artifacts = {"source_manifest", "cue_sheet"}
    expected_artifacts.update(
        key
        for key in ("edit_plan", "pack_receipt", "ffmpeg_receipt")
        if key in artifacts
    )
    if set(artifacts) != expected_artifacts:
        raise ContractError("motion SFX batch contains unsupported artifact roles")
    readiness = _object(payload.get("readiness"))
    expected_readiness = {
        "cue_count": len(cue_sheet["cues"]),
        "density_within_target": cue_sheet["semantic_spotting"]["density_plan"][
            "density_within_target"
        ],
        "render_eligible": expected_render_eligible,
        "candidate_only": True,
        "release_eligible": False,
    }
    if payload.get("status") != expected_status or readiness != expected_readiness:
        raise ContractError("motion SFX batch readiness or status drifted")
    boundary = _object(payload.get("authority_boundary"))
    expected_boundary = {
        "semantic_timeline_moved": False,
        "second_ffmpeg_outlet_created": False,
        "external_provider_called": False,
        "automatic_download": False,
        "upload_performed": False,
        "publish_performed": False,
    }
    if boundary != expected_boundary:
        raise ContractError("motion SFX batch authority boundary is invalid")


def bootstrap_warm_paper_wood_pack(
    video_shotcraft_root: str | Path,
    output_root: str | Path,
    *,
    purpose: str = "commercial",
    platform: str = "social_media",
) -> dict[str, Any]:
    source_root = Path(video_shotcraft_root).expanduser().resolve()
    if _git_head(source_root) != VIDEO_SHOTCRAFT_COMMIT:
        raise ContractError("Video Shotcraft source commit drifted")
    attribution = source_root / "assets" / "audio" / "ATTRIBUTION.md"
    if not attribution.is_file():
        raise ContractError("Video Shotcraft audio attribution snapshot is missing")
    root = Path(output_root).expanduser().resolve()
    owner = {
        "schema": "video_creation_pipeline.motion_sfx_pack_owner.v1",
        "pack_id": WARM_PAPER_WOOD_PACK_ID,
        "source_commit": VIDEO_SHOTCRAFT_COMMIT,
        "managed_outputs_only": True,
        "user_media_overwrite_allowed": False,
    }
    _prepare_managed_root(root, owner, ".motion-sfx-pack-owner.json")
    snapshot = root / "license" / "mixkit-attribution-snapshot.md"
    _copy_exact(attribution, snapshot, "Mixkit attribution snapshot")

    profile_paths: list[Path] = []
    asset_references: list[dict[str, Any]] = []
    for item in _PACK_ITEMS:
        source = source_root / str(item["relative_path"])
        if not source.is_file():
            raise ContractError(f"warm SFX source is missing: {item['asset_id']}")
        media = root / "assets" / f"{item['asset_id']}{source.suffix.lower()}"
        _copy_exact(source, media, f"warm SFX asset {item['asset_id']}")
        manifest = build_licensed_media_asset(
            media,
            snapshot,
            {
                "asset_id": item["asset_id"],
                "media_type": "sound_effect",
                "source_id": "mixkit",
                "item_url": item["item_url"],
                "license_id": "mixkit-free-license",
                "title": item["title"],
                "author": "Mixkit",
                "description": item["description"],
                "tags": item["tags"],
                "download_method": "manual",
                "downloaded_at": "2026-07-27T00:00:00+08:00",
                "attribution_text": "",
            },
            purpose=purpose,
            platform=platform,
        )
        manifest_path = root / "manifests" / f"{item['asset_id']}.asset.json"
        write_stock_media_contract(manifest, manifest_path)
        duration_s = _media_duration(media)
        profile = build_sound_effect_profile(
            manifest_path,
            duration_s=duration_s,
            category=str(item["category"]),
            tags=item["tags"],
            description=str(item["description"]),
        )
        profile_path = root / "profiles" / f"{item['asset_id']}.profile.json"
        write_stock_media_contract(profile, profile_path)
        profile_paths.append(profile_path)
        asset_references.append(
            {
                "asset_id": item["asset_id"],
                "source_relative_path": item["relative_path"],
                "source_sha256": file_sha256(source),
                "profile": artifact_reference(profile_path, "sound effect profile"),
                "profile_sha256": profile["profile_sha256"],
            }
        )

    payload: dict[str, Any] = {
        "schema": MOTION_SFX_PACK_RECEIPT_SCHEMA,
        "pack_id": WARM_PAPER_WOOD_PACK_ID,
        "source": {
            "repository": "https://github.com/Vincentwei1021/video-shotcraft",
            "commit": VIDEO_SHOTCRAFT_COMMIT,
            "attribution_snapshot": artifact_reference(
                snapshot, "Mixkit attribution snapshot"
            ),
        },
        "purpose": purpose,
        "platform": platform,
        "assets": sorted(asset_references, key=lambda row: str(row["asset_id"])),
        "selection_policy": {
            "palette": "warm_paper_wood",
            "balanced_density_default": True,
            "profiles_are_existing_stock_media_contracts": True,
            "automatic_download": False,
        },
        "authority_boundary": {
            "candidate_assets_only": True,
            "source_repository_modified": False,
            "automatic_download": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    payload["pack_sha256"] = payload_sha256(payload, "pack_sha256")
    validate_motion_sfx_pack_receipt(payload)
    receipt_path = root / "motion-sfx-pack-receipt.json"
    atomic_write_json(payload, receipt_path)
    return {
        "receipt": receipt_path,
        "profiles": profile_paths,
        "payload": payload,
    }


def validate_motion_sfx_pack_receipt(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MOTION_SFX_PACK_RECEIPT_SCHEMA:
        raise ContractError("unsupported motion SFX pack receipt schema")
    if payload.get("pack_sha256") != payload_sha256(payload, "pack_sha256"):
        raise ContractError("motion SFX pack receipt integrity check failed")
    if payload.get("pack_id") != WARM_PAPER_WOOD_PACK_ID:
        raise ContractError("motion SFX pack id drifted")
    source = _object(payload.get("source"))
    if source.get("commit") != VIDEO_SHOTCRAFT_COMMIT:
        raise ContractError("motion SFX pack source commit drifted")
    validate_artifact_reference(
        _object(source.get("attribution_snapshot")), "Mixkit attribution snapshot"
    )
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) != len(_PACK_ITEMS):
        raise ContractError("motion SFX pack asset set is incomplete")
    expected_ids = sorted(str(row["asset_id"]) for row in _PACK_ITEMS)
    actual_ids: list[str] = []
    for raw in assets:
        row = _object(raw)
        asset_id = str(row.get("asset_id") or "")
        profile_path = validate_artifact_reference(
            _object(row.get("profile")), "sound effect profile"
        )
        profile = read_json_object(profile_path, "sound effect profile")
        validate_sound_effect_profile(profile)
        if profile.get("asset_id") != asset_id or profile.get(
            "profile_sha256"
        ) != row.get("profile_sha256"):
            raise ContractError("motion SFX pack profile binding drifted")
        actual_ids.append(asset_id)
    if actual_ids != expected_ids:
        raise ContractError("motion SFX pack asset identities drifted")
    boundary = _object(payload.get("authority_boundary"))
    if boundary.get("candidate_assets_only") is not True or any(
        boundary.get(field) is not False
        for field in (
            "source_repository_modified",
            "automatic_download",
            "external_provider_called",
            "upload_performed",
            "publish_performed",
        )
    ):
        raise ContractError("motion SFX pack authority boundary is invalid")


def _validate_sfx_pack_receipt(payload: dict[str, Any]) -> None:
    if payload.get("schema") == CLIPKIT_SFX_PACK_RECEIPT_SCHEMA:
        validate_clipkit_sfx_pack_receipt(payload)
        return
    validate_motion_sfx_pack_receipt(payload)


def profile_paths_from_pack_receipt(receipt_path: str | Path) -> list[Path]:
    payload = read_json_object(receipt_path, "motion SFX pack receipt")
    if payload.get("schema") == CLIPKIT_SFX_PACK_RECEIPT_SCHEMA:
        return profile_paths_from_clipkit_pack_receipt(receipt_path)
    validate_motion_sfx_pack_receipt(payload)
    return [
        Path(str(_object(_object(row).get("profile")).get("path"))).resolve()
        for row in payload["assets"]
    ]


def execute_motion_sfx_mix(
    program_video: str | Path,
    edit_plan_path: str | Path,
    output_path: str | Path,
    receipt_path: str | Path,
    *,
    timeout_s: float = 120.0,
    deterministic: bool = False,
) -> dict[str, Any]:
    return execute_sound_effect_mix(
        program_video,
        edit_plan_path,
        output_path,
        receipt_path,
        timeout_s=timeout_s,
        deterministic=deterministic,
    )


def write_motion_sfx_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    validate_motion_sfx_contract(payload)
    return atomic_write_json(payload, output_path)


def validate_motion_sfx_contract(payload: dict[str, Any]) -> None:
    validators = {
        MOTION_SFX_SOURCE_MANIFEST_SCHEMA: validate_motion_sfx_source_manifest,
        MOTION_SFX_PACK_RECEIPT_SCHEMA: validate_motion_sfx_pack_receipt,
        CLIPKIT_SFX_PACK_RECEIPT_SCHEMA: validate_clipkit_sfx_pack_receipt,
        MOTION_SFX_BATCH_RECEIPT_SCHEMA: validate_motion_sfx_batch_receipt,
    }
    validator = validators.get(str(payload.get("schema")))
    if validator is None:
        raise ContractError("unsupported motion SFX contract")
    validator(payload)


def prepare_motion_sfx_output_root(
    output_root: str | Path, source_manifest: dict[str, Any]
) -> Path:
    validate_motion_sfx_source_manifest(source_manifest)
    root = Path(output_root).expanduser().resolve()
    owner = {
        "schema": "video_creation_pipeline.motion_sfx_output_owner.v1",
        "source_manifest_sha256": source_manifest["manifest_sha256"],
        "managed_outputs_only": True,
        "user_media_overwrite_allowed": False,
    }
    _prepare_managed_root(root, owner, ".motion-sfx-owner.json")
    return root


def build_motion_sfx_edit_plan(
    cue_sheet_path: str | Path,
    profile_paths: Iterable[str | Path],
    *,
    min_score: float = 0.65,
    min_margin: float = 0.10,
) -> dict[str, Any]:
    return build_sound_effect_edit_plan(
        cue_sheet_path,
        profile_paths,
        min_score=min_score,
        min_margin=min_margin,
    )


def _validate_optional_inputs(
    inputs: dict[str, Any], packaging_path: Path, scenes: list[dict[str, Any]]
) -> None:
    if "semantic_motion_plan" in inputs:
        path = Path(str(_object(inputs["semantic_motion_plan"])["path"]))
        payload = read_json_object(path, "semantic motion plan")
        if payload.get("schema") != "video_creation_pipeline.semantic_motion_plan.v1":
            raise ContractError("motion SFX requires a semantic motion plan v1")
        if payload.get("plan_sha256") != payload_sha256(payload, "plan_sha256"):
            raise ContractError("semantic motion plan integrity check failed")
        if payload.get("source_author_binding_projection") is not None:
            from .semantic_motion import validate_semantic_motion_plan
            validate_semantic_motion_plan(payload)
            if set(inputs) - {"packaging_plan", "semantic_motion_plan", "script_anchor"}:
                raise ContractError("author-bound SFX must use the same isolated motion projection")
            if ("script_anchor" in inputs
                    and inputs["script_anchor"] != payload["source_script_anchor"]):
                raise ContractError("author-bound SFX script anchor differs from the motion projection")
        bound = _object(payload.get("source_packaging_plan"))
        if bound and Path(str(bound.get("path") or "")).resolve() != packaging_path:
            raise ContractError("semantic motion plan binds a different packaging plan")
    if "visual_routing_plan" in inputs:
        path = Path(str(_object(inputs["visual_routing_plan"])["path"]))
        payload = read_json_object(path, "visual routing plan")
        if (
            payload.get("schema")
            != "video_creation_pipeline.content_visual_routing_plan.v1"
        ):
            raise ContractError("motion SFX requires a content visual routing plan v1")
        if payload.get("plan_sha256") != payload_sha256(payload, "plan_sha256"):
            raise ContractError("visual routing plan integrity check failed")
    if "remotion_project_data" in inputs:
        path = Path(str(_object(inputs["remotion_project_data"])["path"]))
        payload = read_json_object(path, "Remotion project data")
        rows = payload.get("scenes")
        if not isinstance(rows, list) or not rows:
            raise ContractError("Remotion project data requires scenes")
        expected_ids = {str(row["scene_id"]) for row in scenes}
        actual_ids = {str(_object(row).get("sceneId") or "") for row in rows}
        if actual_ids != expected_ids:
            raise ContractError("Remotion project data drifted from packaging scenes")
    if "script_anchor" in inputs:
        path = Path(str(_object(inputs["script_anchor"])["path"]))
        payload = read_json_object(path, "script anchor")
        if payload.get("schema") != "video_creation_pipeline.script_anchor.v1":
            raise ContractError("motion SFX requires a script anchor v1")
    if "subtitles" in inputs:
        _load_srt_subtitles(Path(str(_object(inputs["subtitles"])["path"])))
    if "word_timestamps" in inputs:
        _load_word_timestamps(Path(str(_object(inputs["word_timestamps"])["path"])))
    if "video_talkcraft_recipe_plan" in inputs:
        if "semantic_motion_plan" not in inputs:
            raise ContractError(
                "video-talkcraft SFX projection requires the semantic motion plan"
            )
        path = Path(str(_object(inputs["video_talkcraft_recipe_plan"])["path"]))
        payload = read_json_object(path, "video-talkcraft recipe plan")
        validate_video_talkcraft_recipe_plan(payload)
        bound = _object(payload.get("source_semantic_motion_plan"))
        semantic = _object(inputs["semantic_motion_plan"])
        if (
            Path(str(bound.get("path") or "")).resolve()
            != Path(str(semantic.get("path") or "")).resolve()
        ):
            raise ContractError(
                "video-talkcraft recipe plan binds a different semantic motion plan"
            )
    if "external_motion_selection_plan" in inputs:
        if "semantic_motion_plan" not in inputs:
            raise ContractError(
                "external motion selection requires the semantic motion plan"
            )
        path = Path(str(_object(inputs["external_motion_selection_plan"])["path"]))
        payload = read_json_object(path, "external motion selection plan")
        validate_external_motion_selection_plan(payload)
        bound = _object(payload.get("source_semantic_motion_plan"))
        semantic = _object(inputs["semantic_motion_plan"])
        if Path(str(bound.get("path") or "")).resolve() != Path(
            str(semantic.get("path") or "")
        ).resolve() or bound.get("sha256") != semantic.get("sha256"):
            raise ContractError(
                "external motion selection binds a different semantic motion plan"
            )


def _packaging_scenes(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    if payload.get("schema") != "video_creation_pipeline.video_packaging_plan.v1":
        raise ContractError("motion SFX requires a video packaging plan v1")
    values = payload.get("scenes")
    if not isinstance(values, list) or not values:
        raise ContractError("video packaging plan requires scenes")
    scenes: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = 0.0
    for raw in values:
        row = _object(raw)
        scene_id = str(row.get("scene_id") or "").strip()
        start_s = _finite(row.get("start_s", row.get("start")), "scene start")
        end_s = _finite(row.get("end_s", row.get("end")), "scene end")
        if not scene_id or scene_id in seen or start_s < 0 or end_s <= start_s:
            raise ContractError("video packaging scene is invalid")
        if start_s < cursor - 0.001:
            raise ContractError("video packaging scenes overlap")
        scene = dict(row)
        scene["scene_id"] = scene_id
        scene["start_s"] = start_s
        scene["end_s"] = end_s
        scenes.append(scene)
        seen.add(scene_id)
        cursor = end_s
    return scenes, round(max(row["end_s"] for row in scenes), 6)


def _scene_narration(
    inputs: dict[str, Any], scenes: list[dict[str, Any]]
) -> dict[str, str]:
    narration = {str(scene["scene_id"]): _scene_text(scene) for scene in scenes}
    if "script_anchor" in inputs:
        path = Path(str(_object(inputs["script_anchor"])["path"]))
        payload = read_json_object(path, "script anchor")
        for raw in payload.get("anchors") or []:
            row = _object(raw)
            scene_id = str(row.get("story_segment_ref") or "")
            quote = str(row.get("quote") or "").strip()
            if scene_id in narration and quote:
                narration[scene_id] = quote
    elif "script" in inputs:
        path = Path(str(_object(inputs["script"])["path"]))
        text = path.read_text(encoding="utf-8-sig")
        sentences = [
            value.strip()
            for value in re.split(r"(?<=[。！？!?；;])\s*|\r?\n+", text)
            if value.strip()
        ]
        if sentences:
            groups: list[list[str]] = [[] for _ in scenes]
            for index, sentence in enumerate(sentences):
                bucket = min(len(scenes) - 1, index * len(scenes) // len(sentences))
                groups[bucket].append(sentence)
            for scene, group in zip(scenes, groups, strict=True):
                if group:
                    narration[str(scene["scene_id"])] = " ".join(group)
    return narration


def _scene_text(scene: dict[str, Any]) -> str:
    values: list[str] = []
    for field in ("purpose", "visual_intent", "narration", "text"):
        value = str(scene.get(field) or "").strip()
        if value:
            values.append(value)
    subtitles = scene.get("subtitle_candidates")
    if isinstance(subtitles, list):
        values.extend(str(value).strip() for value in subtitles if str(value).strip())
    return " ".join(values) or f"场景 {scene['scene_id']}"


def _load_route_map(inputs: dict[str, Any]) -> dict[str, str]:
    if "visual_routing_plan" not in inputs:
        return {}
    path = Path(str(_object(inputs["visual_routing_plan"])["path"]))
    payload = read_json_object(path, "visual routing plan")
    return {
        str(_object(row).get("scene_id") or ""): str(
            _object(row).get("visual_route") or "unrouted_contract_preview"
        )
        for row in payload.get("scene_routes") or []
    }


def _scene_candidates(
    scenes: list[dict[str, Any]],
    narration: dict[str, str],
    route_map: dict[str, str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes):
        scene_id = str(scene["scene_id"])
        function = "orientation" if index == 0 else "transition"
        route = route_map.get(scene_id, "unrouted_contract_preview")
        candidates.append(
            _cue_candidate(
                cue_id=f"scene-{scene_id}-{function}",
                timeline_start_s=float(scene["start_s"]),
                function=function,
                event=f"{route} scene {'opening' if index == 0 else 'transition'}",
                priority="supporting",
                action=f"{route} scene enters",
                trigger="onset",
                narration_text=narration[scene_id],
                narration_position="before",
                evidence_ids=[f"scene:{scene_id}", f"route:{route}"],
                rationale="场景边界是所有渲染路线共享且可复核的同步点。",
                rank=55 if index == 0 else 45,
                source_kind="scene_timeline",
            )
        )
    return candidates


def _motion_candidates(
    inputs: dict[str, Any], narration: dict[str, str], scenes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    external_sfx = _external_motion_sfx_map(inputs)
    if "semantic_motion_plan" in inputs:
        path = Path(str(_object(inputs["semantic_motion_plan"])["path"]))
        payload = read_json_object(path, "semantic motion plan")
        for raw in payload.get("cues") or []:
            cue = _object(raw)
            cue_id = str(cue.get("cue_id") or "motion")
            timing = _object(cue.get("timing"))
            start = (
                _finite(timing.get("timeline_start_s"), "motion start")
                + _finite(timing.get("full_frame_s"), "motion full frame")
                + 0.82 * _finite(timing.get("enter_s"), "motion enter")
            )
            scene_id = str(cue.get("story_segment_ref") or "")
            label = str(_object(cue.get("label")).get("text") or "证据聚焦")
            narration_text = narration.get(scene_id, label)
            function = _classify_script_text(
                f"{label} {narration_text}"
            ) or _information_role_function(str(cue.get("information_role") or ""))
            candidates.append(
                _cue_candidate(
                    cue_id=f"motion-{cue_id}",
                    timeline_start_s=start,
                    function=function,
                    event=f"semantic motion focus: {label}",
                    priority="primary",
                    action="HyperFrames evidence focus reaches its impact point",
                    trigger="impact",
                    narration_text=narration_text,
                    narration_position="during",
                    evidence_ids=[f"motion:{cue_id}", f"scene:{scene_id}"],
                    rationale="使用语义动效已有的精确时间，不重新推断渲染时间线。",
                    rank=100,
                    source_kind="semantic_motion",
                    preferred_asset_id=external_sfx.get(cue_id),
                )
            )
            seen.add(cue_id)
    if "remotion_project_data" in inputs:
        path = Path(str(_object(inputs["remotion_project_data"])["path"]))
        payload = read_json_object(path, "Remotion project data")
        fps = max(1, int(payload.get("fps") or 30))
        for raw in payload.get("motionCues") or []:
            cue = _object(raw)
            cue_id = str(cue.get("cueId") or "motion")
            if cue_id in seen:
                continue
            scene_id = str(cue.get("sceneId") or "")
            start = _finite(cue.get("startFrame"), "Remotion cue frame") / fps
            duration = (
                _finite(cue.get("durationInFrames"), "Remotion cue duration") / fps
            )
            start += min(duration * 0.35, 0.45)
            label = str(cue.get("label") or "知识动画")
            narration_text = narration.get(scene_id, label)
            function = _classify_script_text(f"{label} {narration_text}") or "emphasis"
            candidates.append(
                _cue_candidate(
                    cue_id=f"remotion-{cue_id}",
                    timeline_start_s=start,
                    function=function,
                    event=f"Remotion motion cue: {label}",
                    priority="primary",
                    action="Remotion scene element reaches its visible emphasis",
                    trigger="impact",
                    narration_text=narration_text,
                    narration_position="during",
                    evidence_ids=[f"remotion:{cue_id}", f"scene:{scene_id}"],
                    rationale="复用 Remotion project-data 的帧级 cue，不创建第二条动画时间线。",
                    rank=95,
                    source_kind="remotion_motion",
                )
            )
    candidates.extend(_video_talkcraft_recipe_candidates(inputs, narration))
    return _clamp_candidates_to_scenes(candidates, scenes)


def _video_talkcraft_recipe_candidates(
    inputs: dict[str, Any], narration: dict[str, str]
) -> list[dict[str, Any]]:
    if "video_talkcraft_recipe_plan" not in inputs:
        return []
    path = Path(str(_object(inputs["video_talkcraft_recipe_plan"])["path"]))
    payload = read_json_object(path, "video-talkcraft recipe plan")
    validate_video_talkcraft_recipe_plan(payload)
    candidates: list[dict[str, Any]] = []
    for raw in payload.get("cues") or []:
        cue = _object(raw)
        cue_id = str(cue.get("cue_id") or "motion")
        scene_id = str(cue.get("story_segment_ref") or "")
        recipe_id = str(cue.get("recipe_id") or "recipe")
        narration_text = narration.get(scene_id, recipe_id)
        for raw_sfx in cue.get("sfx_candidates") or []:
            sfx = _object(raw_sfx)
            function = str(sfx.get("function") or "emphasis")
            candidates.append(
                _cue_candidate(
                    cue_id=str(sfx.get("candidate_id") or f"talkcraft-{cue_id}"),
                    timeline_start_s=_finite(
                        sfx.get("timeline_start_s"), "video-talkcraft SFX start"
                    ),
                    function=function,
                    event=f"video-talkcraft recipe beat: {recipe_id}",
                    priority="primary",
                    action=str(sfx.get("action") or f"{recipe_id} visual beat"),
                    trigger="impact",
                    narration_text=narration_text,
                    narration_position="during",
                    evidence_ids=[
                        f"motion:{cue_id}",
                        f"recipe:{recipe_id}",
                        f"scene:{scene_id}",
                    ],
                    rationale=(
                        "复用固定 video-talkcraft 配方的动作 cue，只映射到现有本地"
                        "音效资产，不复制上游音频或另建时间线。"
                    ),
                    rank=108,
                    source_kind="video_talkcraft_recipe",
                )
            )
    return candidates


def _script_candidates(
    inputs: dict[str, Any], narration: dict[str, str], scenes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if "word_timestamps" in inputs:
        rows, _ = _load_word_timestamps(
            Path(str(_object(inputs["word_timestamps"])["path"]))
        )
    elif "subtitles" in inputs:
        rows, _ = _load_srt_subtitles(Path(str(_object(inputs["subtitles"])["path"])))
    elif "script_anchor" in inputs:
        path = Path(str(_object(inputs["script_anchor"])["path"]))
        payload = read_json_object(path, "script anchor")
        rows = [
            {
                "text": str(_object(row).get("quote") or ""),
                "start_s": _object(row).get("timeline_start_s"),
                "end_s": _object(row).get("timeline_end_s"),
                "scene_id": str(_object(row).get("story_segment_ref") or ""),
            }
            for row in payload.get("anchors") or []
        ]
    else:
        rows = [
            {
                "text": narration[str(scene["scene_id"])],
                "start_s": scene["start_s"],
                "end_s": scene["end_s"],
                "scene_id": scene["scene_id"],
            }
            for scene in scenes
        ]
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        row = _object(raw)
        text = str(row.get("text", row.get("word", row.get("token", ""))) or "").strip()
        function = _classify_script_text(text)
        if function is None:
            continue
        start = _finite(row.get("start_s", row.get("start")), "script cue start")
        scene = _scene_at_time(scenes, start)
        if scene is None:
            continue
        scene_id = str(row.get("scene_id") or scene["scene_id"])
        candidates.append(
            _cue_candidate(
                cue_id=f"script-{scene_id}-{index + 1:03d}-{function}",
                timeline_start_s=start,
                function=function,
                event=f"spoken {function}: {text[:80]}",
                priority="primary" if function == "caution" else "supporting",
                action="key phrase receives a restrained visible emphasis",
                trigger="impact",
                narration_text=text or narration.get(scene_id, "关键话语"),
                narration_position="during",
                evidence_ids=[f"script:{scene_id}", f"timing-row:{index + 1}"],
                rationale="提示音只落在已有台词时间证据中的高价值语义点。",
                rank=90 if function == "caution" else 68,
                source_kind="script_timing",
            )
        )
    return candidates


def _derive_omissions(
    inputs: dict[str, Any], scenes: list[dict[str, Any]], narration: dict[str, str]
) -> list[dict[str, Any]]:
    customer_scenes: set[str] = set()
    if "script_anchor" in inputs:
        path = Path(str(_object(inputs["script_anchor"])["path"]))
        payload = read_json_object(path, "script anchor")
        customer_scenes = {
            str(_object(row).get("story_segment_ref") or "")
            for row in payload.get("anchors") or []
            if _object(row).get("source_media_role") == "customer_interview"
        }
    omissions: list[dict[str, Any]] = []
    for scene in scenes:
        scene_id = str(scene["scene_id"])
        text = narration[scene_id]
        reason = ""
        if scene_id in customer_scenes:
            reason = "客户采访保持自然，不自动叠加提示音"
        elif any(term in text for term in _CTA_TERMS):
            reason = "行动号召保持安静，避免提示音制造销售压迫感"
        if reason:
            omissions.append(
                {
                    "omission_id": f"quiet-{scene_id}",
                    "start_s": float(scene["start_s"]),
                    "end_s": float(scene["end_s"]),
                    "reason": reason,
                    "evidence_ids": [f"scene:{scene_id}"],
                }
            )
    return omissions


def _remove_omitted_candidates(
    candidates: list[dict[str, Any]], omissions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        row
        for row in candidates
        if not any(
            float(omission["start_s"])
            <= float(row["timeline_start_s"])
            < float(omission["end_s"])
            for omission in omissions
        )
    ]


def _select_balanced_candidates(
    candidates: list[dict[str, Any]], *, duration_s: float, density_profile: str
) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda row: (
            float(row["timeline_start_s"]),
            -int(row["_rank"]),
            str(row["cue_id"]),
        ),
    )
    deduped: list[dict[str, Any]] = []
    for row in ordered:
        conflict = next(
            (
                existing
                for existing in deduped
                if abs(
                    float(existing["timeline_start_s"]) - float(row["timeline_start_s"])
                )
                < 1.25
            ),
            None,
        )
        if conflict is None:
            deduped.append(row)
        elif int(row["_rank"]) > int(conflict["_rank"]):
            deduped[deduped.index(conflict)] = row
    low, high = SFX_DENSITY_TARGETS[density_profile]
    del low
    max_count = max(1, math.floor((duration_s / 60.0) * high))
    if len(deduped) > max_count:
        ranked = sorted(
            deduped,
            key=lambda row: (
                -int(row["_rank"]),
                float(row["timeline_start_s"]),
                str(row["cue_id"]),
            ),
        )
        selected: list[dict[str, Any]] = []
        minimum_gap = max(1.25, duration_s / max(1, max_count * 2))
        for row in ranked:
            if all(
                abs(
                    float(existing["timeline_start_s"]) - float(row["timeline_start_s"])
                )
                >= minimum_gap
                for existing in selected
            ):
                selected.append(row)
                if len(selected) == max_count:
                    break
        if len(selected) < max_count:
            remaining = [row for row in ranked if row not in selected]
            while remaining and len(selected) < max_count:
                row = max(
                    remaining,
                    key=lambda item: (
                        min(
                            (
                                abs(
                                    float(item["timeline_start_s"])
                                    - float(existing["timeline_start_s"])
                                )
                                for existing in selected
                            ),
                            default=duration_s,
                        ),
                        int(item["_rank"]),
                        -float(item["timeline_start_s"]),
                    ),
                )
                selected.append(row)
                remaining.remove(row)
        deduped = selected
    result: list[dict[str, Any]] = []
    for row in sorted(deduped, key=lambda item: float(item["timeline_start_s"])):
        clean = {key: value for key, value in row.items() if not key.startswith("_")}
        clean["duration_s"] = min(
            float(clean["duration_s"]),
            max(0.01, duration_s - float(clean["timeline_start_s"])),
        )
        result.append(clean)
    return result


def _cue_candidate(
    *,
    cue_id: str,
    timeline_start_s: float,
    function: str,
    event: str,
    priority: str,
    action: str,
    trigger: str,
    narration_text: str,
    narration_position: str,
    evidence_ids: list[str],
    rationale: str,
    rank: int,
    source_kind: str,
    preferred_asset_id: str | None = None,
) -> dict[str, Any]:
    policy = _FUNCTION_POLICY[function]
    return {
        "cue_id": cue_id,
        "timeline_start_s": round(max(0.0, timeline_start_s), 6),
        "duration_s": policy["duration_s"],
        "query": policy["query"],
        "required_tags": list(policy["required_tags"]),
        "optional_tags": list(policy["optional_tags"]),
        "prohibited_tags": ["alarm", "cinematic", "glitch", "jingle", "vocal"],
        "preferred_asset_id": preferred_asset_id or policy["asset_id"],
        "gain_db": policy["gain_db"],
        "fade_in_s": 0.01,
        "fade_out_s": min(0.06, float(policy["duration_s"]) / 3),
        "evidence_ids": evidence_ids,
        "event": event,
        "function": function,
        "priority": priority,
        "visual_anchor": {"action": action, "trigger": trigger},
        "narration_anchor": {
            "text": narration_text[:500] or "同步点",
            "position": narration_position,
        },
        "spotting_status": "needs_audition",
        "rationale": rationale,
        "_rank": rank,
        "_source_kind": source_kind,
    }


def _external_motion_sfx_map(inputs: dict[str, Any]) -> dict[str, str]:
    if "external_motion_selection_plan" not in inputs:
        return {}
    path = Path(str(_object(inputs["external_motion_selection_plan"])["path"]))
    payload = read_json_object(path, "external motion selection plan")
    validate_external_motion_selection_plan(payload)
    result: dict[str, str] = {}
    for raw in payload.get("selections") or []:
        row = _object(raw)
        sound = _object(row.get("sound_effect"))
        asset_id = sound.get("asset_id")
        if asset_id is not None:
            result[str(row.get("cue_id") or "")] = str(asset_id)
    return result


def _information_role_function(role: str) -> str:
    return {
        "structure": "orientation",
        "relationship": "causality",
        "sequence": "commitment",
        "comparison": "emphasis",
        "number": "emphasis",
        "evidence": "emphasis",
    }.get(role, "emphasis")


def _classify_script_text(text: str) -> str | None:
    if any(term in text for term in _CAUTION_TERMS):
        return "caution"
    if any(term in text for term in _COMMITMENT_TERMS):
        return "commitment"
    if any(term in text for term in _RESOLUTION_TERMS):
        return "resolution"
    if any(term in text for term in _CAUSALITY_TERMS):
        return "causality"
    if re.search(r"\d|[一二三四五六七八九十百千万亿]+(?:元|万|年|个|条|次)", text):
        return "emphasis"
    return None


def _scene_at_time(
    scenes: list[dict[str, Any]], timestamp_s: float
) -> dict[str, Any] | None:
    return next(
        (
            scene
            for scene in scenes
            if float(scene["start_s"]) <= timestamp_s < float(scene["end_s"])
        ),
        None,
    )


def _clamp_candidates_to_scenes(
    candidates: list[dict[str, Any]], scenes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    duration_s = max(float(scene["end_s"]) for scene in scenes)
    for row in candidates:
        start = float(row["timeline_start_s"])
        if 0 <= start < duration_s:
            result.append(row)
    return result


def _maximum_cue_gap(cues: list[dict[str, Any]], duration_s: float) -> float:
    points = [0.0, *[float(row["timeline_start_s"]) for row in cues], duration_s]
    points.sort()
    return round(max(b - a for a, b in pairwise(points)), 3)


def _media_duration(path: Path) -> float:
    payload = probe_media_metadata(path)
    value = _object(payload.get("format")).get("duration")
    duration = _finite(value, "sound effect media duration")
    if duration <= 0:
        raise ContractError("sound effect media duration must be positive")
    return round(duration, 6)


def _validate_ffmpeg_edit_plan_binding(
    receipt: dict[str, Any], edit_plan_path: Path
) -> None:
    rows = receipt.get("inputs")
    if not isinstance(rows, list):
        raise ContractError("motion SFX FFmpeg receipt inputs are invalid")
    matches = [
        _object(row)
        for row in rows
        if _object(row).get("role") == "sound_effect_edit_plan"
    ]
    if len(matches) != 1:
        raise ContractError("motion SFX FFmpeg receipt must bind one edit plan")
    bound_path = validate_artifact_reference(
        _object(matches[0].get("artifact")), "sound effect edit plan"
    )
    if bound_path != edit_plan_path.resolve():
        raise ContractError("motion SFX FFmpeg receipt binds a different edit plan")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list) or not any(
        _object(row).get("role") == "sound_effect_mixed_video" for row in outputs
    ):
        raise ContractError("motion SFX FFmpeg receipt lacks the candidate output")


def _validate_pack_edit_plan_binding(
    pack: dict[str, Any], edit_plan: dict[str, Any]
) -> None:
    pack_profiles = {
        (
            str(_object(row).get("asset_id") or ""),
            str(_object(row).get("profile_sha256") or ""),
        )
        for row in pack.get("assets") or []
    }
    plan_profiles = {
        (
            str(_object(row).get("asset_id") or ""),
            str(_object(row).get("profile_sha256") or ""),
        )
        for row in edit_plan.get("profiles") or []
    }
    if plan_profiles != pack_profiles:
        raise ContractError("motion SFX edit plan drifted from its fixed asset pack")


def _copy_exact(source: Path, target: Path, label: str) -> None:
    if target.is_file():
        if file_sha256(target) != file_sha256(source):
            raise ContractError(f"{label} conflicts with an existing managed file")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_copy_promote(source, target, label=label)


def _prepare_managed_root(root: Path, owner: dict[str, Any], owner_name: str) -> None:
    owner_path = root / owner_name
    if root.is_dir() and any(root.iterdir()):
        if not owner_path.is_file():
            raise ContractError("motion SFX output root is non-empty and unmanaged")
        existing = read_json_object(owner_path, "motion SFX output owner")
        if existing != owner:
            raise ContractError("motion SFX output root belongs to a different input")
        return
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(owner, owner_path)


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"source Git inspection failed: {exc}") from exc
    if result.returncode != 0:
        raise ContractError("source Git inspection failed")
    return result.stdout.strip().lower()


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return result


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
