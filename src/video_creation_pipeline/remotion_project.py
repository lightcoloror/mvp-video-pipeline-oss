from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    execute_ffmpeg_with_receipt,
    probe_media_metadata,
    validate_ffmpeg_execution_receipt,
)
from .object_scenes import (
    THREE_PACKAGES,
    object_project_sources,
    object_scene_projection,
)
from .object_process_models import PROCESS_COMPONENTS, THREE_COMPONENTS
from .photo_layout import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    validate_safe_media_layout_plan,
)
from .remotion_skill_handoff import validate_remotion_skill_handoff
from .safe_media_scene_plan import validate_safe_media_scene_plan
from .subtitle_styles import check_font_glyph_coverage, resolve_managed_font, resolve_subtitle_style
from .technical_qa import (
    build_technical_qa_report,
    validate_technical_qa_report,
    write_technical_qa_report,
)

REMOTION_PROJECT_MANIFEST_SCHEMA = (
    "video_creation_pipeline.remotion_project_manifest.v1"
)
REMOTION_RUN_RECEIPT_SCHEMA = "video_creation_pipeline.remotion_run_receipt.v1"
REMOTION_COVER_RUN_RECEIPT_SCHEMA = (
    "video_creation_pipeline.remotion_cover_run_receipt.v1"
)
REMOTION_RUNTIME_VERSION = "4.0.477"
REMOTION_RUNTIME_COMMIT = "e57eedcb3a6065d7db492696c030a76f6962e169"
REMOTION_LICENSE_BASES = {
    "individual",
    "for_profit_up_to_3_employees",
    "non_profit",
    "evaluation",
    "company_license",
}
_RUNTIME_PACKAGES = {
    "@remotion/captions": REMOTION_RUNTIME_VERSION,
    "@remotion/cli": REMOTION_RUNTIME_VERSION,
    "react": "19.2.3",
    "react-dom": "19.2.3",
    "remotion": REMOTION_RUNTIME_VERSION,
}
_PROJECT_FILES = (
    "package.json",
    "remotion.config.ts",
    "tsconfig.json",
    "src/index.ts",
    "src/Root.tsx",
    "src/Composition.tsx",
    "src/Cover.tsx",
    "src/Captions.tsx",
    "src/project-data.json",
    "public/subtitles.srt",
    "REMOTION_LICENSE.md",
)
_NO_MEDIA_RENDER_MODE = "contract_preview_without_source_media"
_SAFE_LAYOUT_MEDIA_RENDER_MODE = "explicit_safe_layout_media_preview"
_SCENE_SAFE_LAYOUT_MEDIA_RENDER_MODE = "explicit_scene_safe_layout_media_preview"
_REMOTION_IMAGE_EXTENSIONS = IMAGE_EXTENSIONS & {".jpeg", ".jpg", ".png", ".webp"}
_REMOTION_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS & {".m4v", ".mov", ".mp4", ".webm"}


def _requested_media_mode(
    handoff: dict[str, Any], include_safe_layout_media: bool
) -> str:
    if not include_safe_layout_media:
        return "none"
    inputs = _object(handoff.get("inputs"))
    if "safe_media_scene_plan" in inputs:
        return "explicit_copy_from_scene_plan"
    if "safe_media_layout_plan" in inputs:
        return "explicit_copy_from_safe_layout"
    raise ContractError(
        "explicit Remotion source media rendering requires a safe layout plan"
    )


def _render_mode(media_mode: str) -> str:
    modes = {
        "none": _NO_MEDIA_RENDER_MODE,
        "explicit_copy_from_safe_layout": _SAFE_LAYOUT_MEDIA_RENDER_MODE,
        "explicit_copy_from_scene_plan": _SCENE_SAFE_LAYOUT_MEDIA_RENDER_MODE,
    }
    try:
        return modes[media_mode]
    except KeyError as exc:
        raise ContractError("Remotion media materialization mode is invalid") from exc


def _has_safe_layout_metadata(data: dict[str, Any]) -> bool:
    if data.get("safeLayout") is not None:
        return True
    scenes = data.get("scenes")
    return isinstance(scenes, list) and any(
        isinstance(scene, dict) and scene.get("mediaLayout") is not None
        for scene in scenes
    )


def remotion_project_doctor(
    runtime_source_root: str | Path | None = None,
    project_dir: str | Path | None = None,
    browser_path: str | Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "video_creation_pipeline.remotion_project_doctor.v1",
        "status": "not_supplied",
        "materialization_ready": False,
        "render_ready": False,
        "dependency_installation_performed_by_doctor": False,
        "render_execution_performed": False,
    }
    try:
        if runtime_source_root is None:
            return result
        result["runtime_source"] = _runtime_source(Path(runtime_source_root))
        result["materialization_ready"] = True
        result["status"] = "ready_for_materialization"
        if project_dir is not None:
            project = Path(project_dir).expanduser().resolve()
            manifest = read_json_object(
                project / "remotion-project-manifest.json",
                "Remotion project manifest",
            )
            validate_remotion_project_manifest(manifest)
            result["capabilities"] = dict(_object(manifest.get("capabilities")))
            result["installed_runtime"] = _installed_runtime(project)
            browser = _browser(Path(browser_path) if browser_path else None)
            result["browser"] = artifact_reference(browser, "Remotion browser")
            result["render_ready"] = True
            result["status"] = "ready_for_local_candidate_render"
    except ContractError as exc:
        result.update({"status": "blocked", "error": str(exc)})
    return result


def materialize_remotion_project(
    handoff_path: str | Path,
    runtime_source_root: str | Path,
    project_dir: str | Path,
    *,
    license_basis: str,
    include_safe_layout_media: bool = False,
    allow_plain_fallback: bool = False,
) -> dict[str, Path]:
    handoff_file = Path(handoff_path).expanduser().resolve()
    handoff = read_json_object(handoff_file, "Remotion skill handoff")
    validate_remotion_skill_handoff(handoff)
    source = _runtime_source(Path(runtime_source_root))
    basis = str(license_basis).strip()
    if basis not in REMOTION_LICENSE_BASES:
        raise ContractError("Remotion runtime license basis is unsupported")
    if not isinstance(include_safe_layout_media, bool):
        raise ContractError(
            "Remotion source media materialization flag must be boolean"
        )
    media_mode = _requested_media_mode(handoff, include_safe_layout_media)

    project = Path(project_dir).expanduser().resolve()
    if project.exists():
        return _reuse_materialized_project(
            project,
            handoff,
            source,
            basis,
            media_mode=media_mode,
        )
    project.parent.mkdir(parents=True, exist_ok=True)
    staging = project.with_name(f".{project.name}.{uuid4().hex}.tmp")
    try:
        staging.mkdir(parents=True)
        data = _project_data(
            handoff,
            include_safe_layout_media=include_safe_layout_media,
            allow_plain_fallback=allow_plain_fallback,
        )
        subtitle_text = ""
        subtitle_reference = _object(handoff.get("inputs")).get("subtitle_artifact")
        subtitle_materialization: dict[str, Any] | None = None
        if subtitle_reference is not None:
            subtitle_path = validate_artifact_reference(
                _object(subtitle_reference), "Remotion subtitle artifact"
            )
            subtitle_text = subtitle_path.read_text(encoding="utf-8-sig")
            sub_style = _object(data.get("subtitleStyle"))
            resolved_font = resolve_managed_font(sub_style["fontId"])
            caption_lines = [
                line
                for line in subtitle_text.splitlines()
                if line.strip() and not line.strip().isdigit() and "-->" not in line
            ]
            check_font_glyph_coverage(resolved_font["source_path"], caption_lines)
            font_public_rel = str(sub_style["fontPublicPath"])
            staged_font = staging / "public" / Path(font_public_rel)
            staged_font.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved_font["source_path"], staged_font)
            subtitle_materialization = {
                "preset_id": sub_style["presetId"],
                "policy_sha256": sub_style["policySha256"],
                "implementation_sha256": sub_style["implementationSha256"],
                "font_id": sub_style["fontId"],
                "font_family": sub_style["fontFamily"],
                "font_weight": sub_style["fontWeight"],
                "public_path": font_public_rel,
                "source_font": artifact_reference(
                    resolved_font["source_path"], "managed subtitle font"
                ),
                "project_font": _staged_reference(
                    staged_font, project / "public" / Path(font_public_rel)
                ),
            }
        files = _project_file_contents(data, subtitle_text=subtitle_text)
        for relative, value in files.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="utf-8")
        materialized_media = (
            _materialize_safe_layout_media(
                handoff, staging, project, media_mode=media_mode
            )
            if include_safe_layout_media
            else []
        )
        license_source = Path(source["license"]["path"])
        shutil.copyfile(license_source, staging / "REMOTION_LICENSE.md")
        inputs = _manifest_inputs(handoff_file, handoff)
        capabilities: dict[str, Any] = {
            "packaging_scene_projection": True,
            "subtitle_rendering": data["subtitleFile"] is not None,
            "subtitle_capabilities": data.get("subtitleCapabilities"),
            "safe_layout_metadata_projection": _has_safe_layout_metadata(data),
            "semantic_motion_metadata_projection": bool(data["motionCues"]),
            "external_motion_selection_projection": bool(
                data["externalMotionSelections"]
            ),
            "title_cover_still_compositions": bool(data["titleCoverVariants"]),
            "source_media_rendering": include_safe_layout_media,
            "studio_editable_project": True,
            "render_mode": _render_mode(media_mode),
        }
        if data.get("objectScenes"):
            capabilities["object_scene_authoring"] = True
            capabilities["object_scene_3d_authoring"] = any(row["componentId"] in THREE_COMPONENTS for row in data["objectScenes"])
        if "safe_media_scene_plan" in _object(handoff.get("inputs")):
            capabilities["scene_media_assignment"] = True
        manifest: dict[str, Any] = {
            "schema": REMOTION_PROJECT_MANIFEST_SCHEMA,
            "source_handoff": artifact_reference(
                handoff_file, "Remotion skill handoff"
            ),
            "runtime_source": source,
            **({"object_scene_runtime_source": _three_runtime_source(Path(runtime_source_root))}
               if capabilities.get("object_scene_3d_authoring") else {}),
            "license_basis": basis,
            "project": {
                "root": str(project),
                "composition_id": data["compositionId"],
                "width": data["width"],
                "height": data["height"],
                "fps": data["fps"],
                "duration_in_frames": data["durationInFrames"],
            },
            "inputs": inputs,
            "generated_files": {
                relative: _staged_reference(staging / relative, project / relative)
                for relative in (*files, "REMOTION_LICENSE.md")
            },
            "media_materialization": {
                "mode": media_mode,
                "assets": materialized_media,
            },
            **(
                {"subtitle_materialization": subtitle_materialization}
                if subtitle_materialization is not None
                else {}
            ),
            "capabilities": capabilities,
            "authority_boundary": {
                "candidate_only": True,
                "remotion_project_materialized": True,
                "render_execution_performed": False,
                "source_media_copied": include_safe_layout_media,
                "source_media_copy_explicitly_requested": include_safe_layout_media,
                "network_install_performed": False,
                "external_provider_called": False,
                "upload_performed": False,
                "publish_performed": False,
            },
        }
        manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
        (staging / "remotion-project-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.replace(project)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    validate_remotion_project_manifest(manifest)
    return _materialized_paths(project)


def validate_remotion_project_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != REMOTION_PROJECT_MANIFEST_SCHEMA:
        raise ContractError("unsupported Remotion project manifest schema")
    if manifest.get("manifest_sha256") != payload_sha256(manifest, "manifest_sha256"):
        raise ContractError("Remotion project manifest integrity check failed")
    handoff_path = validate_artifact_reference(
        _object(manifest.get("source_handoff")), "Remotion skill handoff"
    )
    handoff = read_json_object(handoff_path, "Remotion skill handoff")
    validate_remotion_skill_handoff(handoff)
    _validate_runtime_source_snapshot(_object(manifest.get("runtime_source")))
    if manifest.get("license_basis") not in REMOTION_LICENSE_BASES:
        raise ContractError("Remotion project license basis is invalid")
    project = _object(manifest.get("project"))
    root = Path(str(project.get("root") or "")).expanduser().resolve()
    if (
        root / "remotion-project-manifest.json"
        != Path(str(root / "remotion-project-manifest.json")).resolve()
    ):
        raise ContractError("Remotion project root is invalid")
    media_materialization_value = manifest.get("media_materialization")
    legacy_media_manifest = media_materialization_value is None
    media_materialization = (
        {"mode": "none", "assets": []}
        if legacy_media_manifest
        else _object(media_materialization_value)
    )
    media_mode = str(media_materialization.get("mode") or "")
    if media_mode not in {
        "none",
        "explicit_copy_from_safe_layout",
        "explicit_copy_from_scene_plan",
    }:
        raise ContractError("Remotion media materialization mode is invalid")
    include_safe_layout_media = media_mode != "none"
    scene_media_assignment = "safe_media_scene_plan" in _object(handoff.get("inputs"))
    data_path = root / "src" / "project-data.json"
    data = read_json_object(data_path, "Remotion project data")
    expected = _project_data(
        handoff,
        include_safe_layout_media=include_safe_layout_media,
    )
    if data != expected:
        raise ContractError("Remotion project data drifted from the handoff")
    uses_three = any(row["componentId"] in THREE_COMPONENTS for row in data.get("objectScenes", []))
    if uses_three:
        source_root = Path(manifest["runtime_source"]["core_package"]["path"]).parents[2]
        if manifest.get("object_scene_runtime_source") != _three_runtime_source(source_root):
            raise ContractError("object scene Three source drifted from the pinned local checkout")
    elif "object_scene_runtime_source" in manifest:
        raise ContractError("a project without 3D cannot declare a Three source")
    if project != {
        "root": str(root),
        "composition_id": data["compositionId"],
        "width": data["width"],
        "height": data["height"],
        "fps": data["fps"],
        "duration_in_frames": data["durationInFrames"],
    }:
        raise ContractError("Remotion project composition metadata drifted")
    inputs = _object(manifest.get("inputs"))
    for name, reference in inputs.items():
        validate_artifact_reference(_object(reference), f"Remotion input {name}")
    expected_inputs = _manifest_inputs(handoff_path, handoff)
    if inputs != expected_inputs:
        raise ContractError("Remotion project inputs drifted from the handoff")
    _validate_media_materialization(
        media_materialization,
        handoff,
        root,
        media_mode=media_mode,
    )
    if "subtitle_materialization" in manifest:
        if expected.get("subtitleFile") is None:
            raise ContractError("subtitle materialization present without subtitle input")
        sub_mat = _object(manifest.get("subtitle_materialization"))
        sub_style = _object(expected.get("subtitleStyle"))
        if (
            sub_mat.get("preset_id") != sub_style.get("presetId")
            or sub_mat.get("policy_sha256") != sub_style.get("policySha256")
            or sub_mat.get("implementation_sha256") != sub_style.get("implementationSha256")
            or sub_mat.get("font_id") != sub_style.get("fontId")
            or sub_mat.get("public_path") != sub_style.get("fontPublicPath")
        ):
            raise ContractError("Remotion subtitle materialization drifted from style policy")
        proj_font = validate_artifact_reference(
            _object(sub_mat.get("project_font")), "Remotion project subtitle font"
        )
        if proj_font != (root / "public" / Path(str(sub_style["fontPublicPath"]))).resolve():
            raise ContractError("Remotion project subtitle font escaped project public directory")
        if _object(sub_mat.get("project_font")).get("sha256") != sub_style.get("fontSha256"):
            raise ContractError("Remotion project subtitle font hash drifted")
    files = _object(manifest.get("generated_files"))
    expected_files = set(_PROJECT_FILES) | set(object_project_sources(expected.get("objectScenes", [])))
    if set(files) != expected_files:
        raise ContractError("Remotion generated file inventory is incomplete")
    for relative, reference in files.items():
        path = validate_artifact_reference(
            _object(reference), f"Remotion generated file {relative}"
        )
        if path != (root / relative).resolve():
            raise ContractError("Remotion generated file escaped the project root")
    capabilities = _object(manifest.get("capabilities"))
    expected_render_mode = _render_mode(media_mode)
    if (
        capabilities.get("packaging_scene_projection") is not True
        or capabilities.get("source_media_rendering") is not include_safe_layout_media
        or capabilities.get("studio_editable_project") is not True
        or capabilities.get("render_mode") != expected_render_mode
    ):
        raise ContractError("Remotion project capabilities are invalid")
    if capabilities.get("subtitle_rendering") is not (
        expected["subtitleFile"] is not None
    ):
        raise ContractError("Remotion subtitle capability drifted")
    if capabilities.get("safe_layout_metadata_projection") is not (
        _has_safe_layout_metadata(expected)
    ):
        raise ContractError("Remotion safe layout capability drifted")
    if capabilities.get("scene_media_assignment", False) is not scene_media_assignment:
        raise ContractError("Remotion scene media assignment capability drifted")
    if capabilities.get("semantic_motion_metadata_projection") is not bool(
        expected["motionCues"]
    ):
        raise ContractError("Remotion motion capability drifted")
    if capabilities.get("title_cover_still_compositions") is not bool(
        expected["titleCoverVariants"]
    ):
        raise ContractError("Remotion title cover capability drifted")
    if bool(capabilities.get("object_scene_authoring")) != bool(expected.get("objectScenes")):
        raise ContractError("object scene capability drifted")
    if bool(capabilities.get("object_scene_3d_authoring")) != any(row["componentId"] in THREE_COMPONENTS for row in expected.get("objectScenes", [])):
        raise ContractError("object scene 3D capability drifted")
    generated = _project_file_contents(expected, subtitle_text=(root / "public/subtitles.srt").read_text(encoding="utf-8"))
    if expected.get("objectScenes"):
        for relative, value in generated.items():
            if (root / relative).read_text(encoding="utf-8") != value:
                raise ContractError("object scene generated source differs from the bound authoring inputs")
    boundary = _object(manifest.get("authority_boundary"))
    expected_boundary = {
        "candidate_only": True,
        "remotion_project_materialized": True,
        "render_execution_performed": False,
        "source_media_copied": include_safe_layout_media,
        "network_install_performed": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
    }
    if not legacy_media_manifest:
        expected_boundary["source_media_copy_explicitly_requested"] = (
            include_safe_layout_media
        )
    if boundary != expected_boundary:
        raise ContractError("Remotion project authority boundary is invalid")


def run_remotion_candidate(
    manifest_path: str | Path,
    runtime_source_root: str | Path,
    *,
    browser_path: str | Path,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = read_json_object(manifest_file, "Remotion project manifest")
    validate_remotion_project_manifest(manifest)
    source = _runtime_source(Path(runtime_source_root))
    if source != manifest.get("runtime_source"):
        raise ContractError("Remotion runtime source drifted from the project")
    project = Path(str(_object(manifest["project"])["root"])).resolve()
    installed = _installed_runtime(project)
    browser = _browser(Path(browser_path))
    evidence_dir = project / "evidence"
    intermediate_dir = project / "intermediate"
    candidate_dir = project / "candidate"
    qa_dir = project / "qa"
    receipt_dir = project / "receipts"
    for directory in (
        evidence_dir,
        intermediate_dir,
        candidate_dir,
        qa_dir,
        receipt_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    cli = Path(installed["cli_entry"]["path"])
    entry = project / "src" / "index.ts"
    composition_id = str(_object(manifest["project"])["composition_id"])
    compositions = _run_remotion(
        cli,
        [
            "compositions",
            str(entry),
            "--browser-executable",
            str(browser),
        ],
        project,
        timeout_s,
    )
    compositions_log = evidence_dir / "remotion-compositions.log"
    _atomic_text(compositions["output"], compositions_log)
    raw_output = intermediate_dir / "remotion-intermediate.mp4"
    raw_staging = intermediate_dir / (f".remotion-intermediate.{uuid4().hex}.tmp.mp4")
    try:
        render = _run_remotion(
            cli,
            [
                "render",
                str(entry),
                composition_id,
                str(raw_staging),
                "--browser-executable",
                str(browser),
                "--codec",
                "h264",
                "--pixel-format",
                "yuv420p",
                "--concurrency",
                "1",
                "--log=verbose",
                "--overwrite",
            ],
            project,
            timeout_s,
        )
        if not raw_staging.is_file():
            raise ContractError("Remotion render did not produce an intermediate video")
        raw_staging.replace(raw_output)
    finally:
        if raw_staging.exists():
            raw_staging.unlink()
    render_log = evidence_dir / "remotion-render.log"
    _atomic_text(render["output"], render_log)
    subtitle_runtime: dict[str, Any] | None = None
    if _object(manifest.get("capabilities")).get("subtitle_rendering") is True:
        font_load_events: list[dict[str, Any]] = []
        for raw_line in render["output"].splitlines():
            marker = "[subtitle-font-loaded]"
            if marker in raw_line:
                payload_text = re.sub(r"\x1b\[[0-9;]*m", "", raw_line.split(marker, 1)[1]).strip()
                try:
                    font_load_events.append(json.loads(payload_text))
                except json.JSONDecodeError:
                    font_load_events.append({"raw": payload_text})
        sub_mat = _object(manifest.get("subtitle_materialization"))
        if not font_load_events or not any(
            evt.get("status") == "loaded"
            and evt.get("sha256") == _object(sub_mat.get("project_font")).get("sha256")
            for evt in font_load_events
        ):
            raise ContractError("Remotion subtitle font failed to load in Chromium before render")
        subtitle_runtime = {
            "preset_id": sub_mat.get("preset_id"),
            "policy_sha256": sub_mat.get("policy_sha256"),
            "implementation_sha256": sub_mat.get("implementation_sha256"),
            "font_id": sub_mat.get("font_id"),
            "font_family": sub_mat.get("font_family"),
            "font_weight": sub_mat.get("font_weight"),
            "font_sha256": _object(sub_mat.get("project_font")).get("sha256"),
            "subtitle_artifact": dict(_object(manifest.get("inputs")).get("subtitle_artifact") or {}),
            "browser_font_load_verified": True,
            "browser_font_load_events": font_load_events,
        }

    project_meta = _object(manifest["project"])
    capabilities = _object(manifest["capabilities"])
    source_media_rendered = capabilities.get("source_media_rendering") is True
    candidate_name = (
        "remotion-safe-layout-media-preview.mp4"
        if source_media_rendered
        else "remotion-contract-preview.mp4"
    )
    candidate = candidate_dir / candidate_name
    candidate_staging = candidate_dir / (
        f".{Path(candidate_name).stem}.{uuid4().hex}.staging.mp4"
    )
    normalize_receipt = receipt_dir / "remotion-normalize.json"
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
            str(project_meta["fps"]),
            "-s",
            f"{project_meta['width']}x{project_meta['height']}",
            "-movflags",
            "+faststart",
            str(candidate_staging),
        ],
        [
            {"role": "remotion_intermediate", "path": raw_output},
            {"role": "remotion_project_manifest", "path": manifest_file},
        ],
        [{"role": "candidate_video_staging", "path": candidate_staging}],
        normalize_receipt,
        timeout_s=timeout_s,
    )
    _atomic_copy_promote(candidate_staging, candidate)
    probe_path = atomic_write_json(
        probe_media_metadata(candidate), qa_dir / "ffprobe.json"
    )
    detector_log = qa_dir / "detector.log"
    detector_receipt = receipt_dir / "remotion-technical-qa.json"
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
    inputs = _object(manifest["inputs"])
    subtitle = inputs.get("subtitle_artifact")
    qa = build_technical_qa_report(
        candidate,
        probe_path,
        detector_receipt,
        subtitles_path=(
            Path(str(_object(subtitle)["path"])) if subtitle is not None else None
        ),
        packaging_plan_path=Path(str(_object(inputs["packaging_plan"])["path"])),
        expected_duration_s=(
            float(project_meta["duration_in_frames"]) / float(project_meta["fps"])
        ),
        policy={
            "target_width": int(project_meta["width"]),
            "target_height": int(project_meta["height"]),
            "target_fps": float(project_meta["fps"]),
            "max_black_ratio": 0.05,
            "max_silence_ratio": 1.0,
            "freeze_warning_ratio": 0.95,
            "require_audio": False,
            "require_subtitles": subtitle is not None,
            "require_packaging_plan": True,
        },
    )
    qa_path = write_technical_qa_report(qa, qa_dir / "technical-qa.json")
    if _object(qa.get("readiness")).get("technical_ready") is not True:
        raise ContractError("Remotion candidate failed automatic technical QA")

    receipt: dict[str, Any] = {
        "schema": REMOTION_RUN_RECEIPT_SCHEMA,
        "status": "completed_candidate_only",
        "source_manifest": artifact_reference(
            manifest_file, "Remotion project manifest"
        ),
        "runtime_source": source,
        "installed_runtime": installed,
        "browser": artifact_reference(browser, "Remotion browser"),
        "checks": [
            {
                "command": "compositions",
                "status": "passed",
                "log": artifact_reference(
                    compositions_log, "Remotion compositions log"
                ),
            },
            {
                "command": "render",
                "status": "passed",
                "log": artifact_reference(render_log, "Remotion render log"),
            },
        ],
        "intermediate_video": artifact_reference(
            raw_output, "Remotion intermediate video"
        ),
        "candidate_video": artifact_reference(candidate, "Remotion candidate video"),
        "candidate_staging_video": artifact_reference(
            candidate_staging, "Remotion candidate staging video"
        ),
        "ffmpeg_receipt": artifact_reference(
            normalize_receipt, "Remotion normalize receipt"
        ),
        "qa_report": artifact_reference(qa_path, "Remotion QA report"),
        **(
            {"subtitle_runtime": subtitle_runtime}
            if subtitle_runtime is not None
            else {}
        ),
        "authority_boundary": {
            "candidate_only": True,
            "contract_preview_without_source_media": not source_media_rendered,
            "creative_approval_claimed": False,
            "release_eligible": False,
            "external_provider_called": False,
            "remote_asset_fetch_allowed": False,
            "upload_performed": False,
            "publish_performed": False,
            "atomic_intermediate_promotion": True,
            "atomic_candidate_promotion": True,
            "single_ffmpeg_outlet_used_after_render": True,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_remotion_run_receipt(receipt)
    atomic_write_json(receipt, project / "remotion-run-receipt.json")
    return receipt


def validate_remotion_run_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != REMOTION_RUN_RECEIPT_SCHEMA:
        raise ContractError("unsupported Remotion run receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("Remotion run receipt integrity check failed")
    if receipt.get("status") != "completed_candidate_only":
        raise ContractError("Remotion output must remain candidate-only")
    manifest_path = validate_artifact_reference(
        _object(receipt.get("source_manifest")), "Remotion project manifest"
    )
    manifest = read_json_object(manifest_path, "Remotion project manifest")
    validate_remotion_project_manifest(manifest)
    source_media_rendered = (
        _object(manifest.get("capabilities")).get("source_media_rendering") is True
    )
    _validate_runtime_source_snapshot(_object(receipt.get("runtime_source")))
    installed = _object(receipt.get("installed_runtime"))
    _validate_installed_runtime_snapshot(installed)
    expected_packages = set(_RUNTIME_PACKAGES)
    if _object(manifest.get("capabilities")).get("object_scene_3d_authoring"):
        expected_packages.update(THREE_PACKAGES)
    if set(installed["packages"]) != expected_packages:
        raise ContractError("render receipt dependencies differ from its object scene manifest")
    validate_artifact_reference(_object(receipt.get("browser")), "Remotion browser")
    checks = receipt.get("checks")
    if not isinstance(checks, list) or len(checks) != 2:
        raise ContractError("Remotion run checks are incomplete")
    if {str(_object(row).get("command")) for row in checks} != {
        "compositions",
        "render",
    }:
        raise ContractError("Remotion run check set is incomplete")
    for row in checks:
        value = _object(row)
        if value.get("status") != "passed":
            raise ContractError("Remotion run check did not pass")
        validate_artifact_reference(_object(value.get("log")), "Remotion run log")
    for field, label in (
        ("intermediate_video", "Remotion intermediate video"),
        ("candidate_video", "Remotion candidate video"),
        ("candidate_staging_video", "Remotion candidate staging video"),
        ("ffmpeg_receipt", "Remotion normalize receipt"),
        ("qa_report", "Remotion QA report"),
    ):
        validate_artifact_reference(_object(receipt.get(field)), label)
    ffmpeg_path = Path(str(_object(receipt["ffmpeg_receipt"])["path"]))
    ffmpeg = read_json_object(ffmpeg_path, "Remotion normalize receipt")
    validate_ffmpeg_execution_receipt(ffmpeg)
    outputs = ffmpeg.get("outputs")
    if (
        not isinstance(outputs, list)
        or len(outputs) != 1
        or _object(outputs[0]).get("artifact") != receipt.get("candidate_staging_video")
    ):
        raise ContractError("Remotion staging video drifted from its FFmpeg receipt")
    if _object(receipt.get("candidate_staging_video")).get("sha256") != _object(
        receipt.get("candidate_video")
    ).get("sha256"):
        raise ContractError("Remotion candidate drifted during atomic promotion")
    qa_path = Path(str(_object(receipt["qa_report"])["path"]))
    qa = read_json_object(qa_path, "Remotion QA report")
    validate_technical_qa_report(qa)
    if _object(qa.get("readiness")).get("technical_ready") is not True or _object(
        _object(qa.get("inputs")).get("media")
    ).get("sha256") != _object(receipt.get("candidate_video")).get("sha256"):
        raise ContractError("Remotion candidate is not bound to passing QA")
    if "subtitle_runtime" in receipt:
        sub_rt = _object(receipt.get("subtitle_runtime"))
        sub_mat = _object(manifest.get("subtitle_materialization"))
        if (
            sub_rt.get("browser_font_load_verified") is not True
            or sub_rt.get("font_sha256") != _object(sub_mat.get("project_font")).get("sha256")
            or sub_rt.get("subtitle_artifact") != _object(manifest.get("inputs")).get("subtitle_artifact")
            or sub_rt.get("preset_id") != sub_mat.get("preset_id")
            or sub_rt.get("policy_sha256") != sub_mat.get("policy_sha256")
        ):
            raise ContractError("Remotion subtitle runtime drifted from materialized project")
    boundary = _object(receipt.get("authority_boundary"))
    if boundary != {
        "candidate_only": True,
        "contract_preview_without_source_media": not source_media_rendered,
        "creative_approval_claimed": False,
        "release_eligible": False,
        "external_provider_called": False,
        "remote_asset_fetch_allowed": False,
        "upload_performed": False,
        "publish_performed": False,
        "atomic_intermediate_promotion": True,
        "atomic_candidate_promotion": True,
        "single_ffmpeg_outlet_used_after_render": True,
    }:
        raise ContractError("Remotion run authority boundary is invalid")


def run_remotion_cover_candidates(
    manifest_path: str | Path,
    runtime_source_root: str | Path,
    *,
    browser_path: str | Path,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = read_json_object(manifest_file, "Remotion project manifest")
    validate_remotion_project_manifest(manifest)
    source = _runtime_source(Path(runtime_source_root))
    if source != manifest.get("runtime_source"):
        raise ContractError("Remotion runtime source drifted from the project")
    project = Path(str(_object(manifest["project"])["root"])).resolve()
    installed = _installed_runtime(project)
    browser = _browser(Path(browser_path))
    data = read_json_object(
        project / "src" / "project-data.json", "Remotion project data"
    )
    variants = [
        _object(row)
        for row in data.get("titleCoverVariants") or []
        if _object(row).get("renderEligible") is True
    ]
    if not variants:
        raise ContractError("Remotion project has no render-eligible title covers")
    cli = Path(installed["cli_entry"]["path"])
    entry = project / "src" / "index.ts"
    evidence_dir = project / "evidence" / "covers"
    candidate_dir = project / "candidate" / "covers"
    qa_dir = project / "qa" / "covers"
    for directory in (evidence_dir, candidate_dir, qa_dir):
        directory.mkdir(parents=True, exist_ok=True)
    compositions = _run_remotion(
        cli,
        ["compositions", str(entry), "--browser-executable", str(browser)],
        project,
        timeout_s,
    )
    compositions_log = evidence_dir / "remotion-cover-compositions.log"
    _atomic_text(compositions["output"], compositions_log)

    outputs: list[dict[str, Any]] = []
    for variant in variants:
        composition_id = str(variant.get("compositionId") or "")
        candidate_name = f"{composition_id}.png"
        output = candidate_dir / candidate_name
        staging = candidate_dir / f".{composition_id}.{uuid4().hex}.tmp.png"
        try:
            render = _run_remotion(
                cli,
                [
                    "still",
                    str(entry),
                    composition_id,
                    str(staging),
                    "--browser-executable",
                    str(browser),
                    "--image-format",
                    "png",
                    "--overwrite",
                ],
                project,
                timeout_s,
            )
            if not staging.is_file():
                raise ContractError("Remotion still did not produce a cover candidate")
            staging.replace(output)
        finally:
            if staging.exists():
                staging.unlink()
        log = evidence_dir / f"{composition_id}.log"
        _atomic_text(render["output"], log)
        probe = probe_media_metadata(output)
        probe_path = atomic_write_json(probe, qa_dir / f"{composition_id}.ffprobe.json")
        actual_width, actual_height = _probe_dimensions(probe)
        expected_width = _positive_int(variant.get("width"), "cover width")
        expected_height = _positive_int(variant.get("height"), "cover height")
        if (actual_width, actual_height) != (expected_width, expected_height):
            raise ContractError("Remotion cover dimensions drifted from its profile")
        outputs.append(
            {
                "composition_id": composition_id,
                "candidate_id": str(variant.get("candidateId") or ""),
                "profile_id": str(variant.get("profileId") or ""),
                "expected_dimensions": {
                    "width": expected_width,
                    "height": expected_height,
                },
                "candidate_image": artifact_reference(
                    output, f"Remotion cover {composition_id}"
                ),
                "render_log": artifact_reference(log, "Remotion cover render log"),
                "probe": artifact_reference(probe_path, "Remotion cover probe"),
                "automatic_qa": {
                    "file_nonempty": output.stat().st_size > 0,
                    "dimensions_match": True,
                    "safe_zone_contract_bound": True,
                    "small_size_visual_review_required": True,
                    "face_blur_occlusion_review_required": True,
                },
            }
        )

    receipt: dict[str, Any] = {
        "schema": REMOTION_COVER_RUN_RECEIPT_SCHEMA,
        "status": "completed_candidate_only",
        "source_manifest": artifact_reference(
            manifest_file, "Remotion project manifest"
        ),
        "runtime_source": source,
        "installed_runtime": installed,
        "browser": artifact_reference(browser, "Remotion browser"),
        "compositions_log": artifact_reference(
            compositions_log, "Remotion cover compositions log"
        ),
        "outputs": outputs,
        "readiness": {
            "automatic_technical_qa_passed": True,
            "human_visual_review_required": True,
            "creative_approval_claimed": False,
            "release_eligible": False,
            "publish_authorized": False,
        },
        "authority_boundary": {
            "candidate_only": True,
            "remote_asset_fetch_allowed": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "atomic_candidate_promotion": True,
            "second_renderer_created": False,
            "second_ffmpeg_outlet_created": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_remotion_cover_run_receipt(receipt)
    atomic_write_json(receipt, project / "remotion-cover-run-receipt.json")
    return receipt


def validate_remotion_cover_run_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != REMOTION_COVER_RUN_RECEIPT_SCHEMA:
        raise ContractError("unsupported Remotion cover run receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("Remotion cover receipt integrity check failed")
    if receipt.get("status") != "completed_candidate_only":
        raise ContractError("Remotion covers must remain candidate-only")
    manifest_path = validate_artifact_reference(
        _object(receipt.get("source_manifest")), "Remotion project manifest"
    )
    manifest = read_json_object(manifest_path, "Remotion project manifest")
    validate_remotion_project_manifest(manifest)
    _validate_runtime_source_snapshot(_object(receipt.get("runtime_source")))
    _validate_installed_runtime_snapshot(_object(receipt.get("installed_runtime")))
    validate_artifact_reference(_object(receipt.get("browser")), "Remotion browser")
    validate_artifact_reference(
        _object(receipt.get("compositions_log")), "Remotion cover compositions log"
    )
    project = Path(str(_object(manifest["project"])["root"])).resolve()
    data = read_json_object(
        project / "src" / "project-data.json", "Remotion project data"
    )
    expected = {
        str(_object(row).get("compositionId") or ""): _object(row)
        for row in data.get("titleCoverVariants") or []
        if _object(row).get("renderEligible") is True
    }
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != len(expected):
        raise ContractError("Remotion cover output set is incomplete")
    seen: set[str] = set()
    for raw in outputs:
        row = _object(raw)
        composition_id = str(row.get("composition_id") or "")
        variant = expected.get(composition_id)
        if variant is None or composition_id in seen:
            raise ContractError("Remotion cover composition output is invalid")
        seen.add(composition_id)
        if (
            row.get("candidate_id") != variant.get("candidateId")
            or row.get("profile_id") != variant.get("profileId")
            or row.get("expected_dimensions")
            != {"width": variant.get("width"), "height": variant.get("height")}
        ):
            raise ContractError("Remotion cover output binding drifted")
        image = validate_artifact_reference(
            _object(row.get("candidate_image")), "Remotion cover image"
        )
        validate_artifact_reference(
            _object(row.get("render_log")), "Remotion cover render log"
        )
        probe_path = validate_artifact_reference(
            _object(row.get("probe")), "Remotion cover probe"
        )
        actual = _probe_dimensions(read_json_object(probe_path, "Remotion cover probe"))
        if actual != (variant.get("width"), variant.get("height")):
            raise ContractError("Remotion cover probe dimensions drifted")
        if row.get("automatic_qa") != {
            "file_nonempty": image.stat().st_size > 0,
            "dimensions_match": True,
            "safe_zone_contract_bound": True,
            "small_size_visual_review_required": True,
            "face_blur_occlusion_review_required": True,
        }:
            raise ContractError("Remotion cover automatic QA drifted")
    if set(expected) != seen:
        raise ContractError("Remotion cover output set drifted")
    if receipt.get("readiness") != {
        "automatic_technical_qa_passed": True,
        "human_visual_review_required": True,
        "creative_approval_claimed": False,
        "release_eligible": False,
        "publish_authorized": False,
    }:
        raise ContractError("Remotion cover readiness drifted")
    if receipt.get("authority_boundary") != {
        "candidate_only": True,
        "remote_asset_fetch_allowed": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
        "atomic_candidate_promotion": True,
        "second_renderer_created": False,
        "second_ffmpeg_outlet_created": False,
    }:
        raise ContractError("Remotion cover authority boundary is invalid")


def _probe_dimensions(probe: dict[str, Any]) -> tuple[int, int]:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise ContractError("Remotion cover probe has no streams")
    for raw in streams:
        stream = _object(raw)
        if stream.get("codec_type") == "video":
            return (
                _positive_int(stream.get("width"), "cover probe width"),
                _positive_int(stream.get("height"), "cover probe height"),
            )
    raise ContractError("Remotion cover probe has no image stream")


def _runtime_source(root: Path) -> dict[str, Any]:
    source = root.expanduser().resolve()
    if _git_head(source) != REMOTION_RUNTIME_COMMIT:
        raise ContractError("Remotion runtime source commit drifted")
    core = source / "packages" / "core" / "package.json"
    cli = source / "packages" / "cli" / "package.json"
    license_path = source / "LICENSE.md"
    core_meta = read_json_object(core, "Remotion runtime package")
    cli_meta = read_json_object(cli, "Remotion CLI package")
    if (
        core_meta.get("name") != "remotion"
        or core_meta.get("version") != REMOTION_RUNTIME_VERSION
        or cli_meta.get("name") != "@remotion/cli"
        or cli_meta.get("version") != REMOTION_RUNTIME_VERSION
    ):
        raise ContractError("Remotion runtime version drifted")
    if not license_path.is_file():
        raise ContractError("Remotion runtime license was not found")
    return {
        "name": "remotion-dev/remotion",
        "version": REMOTION_RUNTIME_VERSION,
        "source_commit": REMOTION_RUNTIME_COMMIT,
        "core_package": artifact_reference(core, "Remotion runtime package"),
        "cli_package": artifact_reference(cli, "Remotion CLI package"),
        "license": artifact_reference(license_path, "Remotion runtime license"),
        "license_policy": "eligibility_must_be_declared_per_materialized_project",
    }


def _three_runtime_source(root: Path) -> dict[str, Any]:
    """Bind the Three adapter to the existing fixed Remotion source checkout."""
    if _git_head(root) != REMOTION_RUNTIME_COMMIT:
        raise ContractError("Three adapter source commit drifted")
    package = root / "packages" / "three" / "package.json"
    metadata = read_json_object(package, "local Remotion Three package")
    if metadata.get("name") != "@remotion/three" or metadata.get("version") != REMOTION_RUNTIME_VERSION:
        raise ContractError("local Remotion Three version drifted")
    return {"source_commit": REMOTION_RUNTIME_COMMIT, "dependency_pins": dict(THREE_PACKAGES),
            "source_files": {name: artifact_reference(root / "packages" / "three" / name, "local Three adapter source")
                             for name in ("package.json", "src/index.ts", "src/ThreeCanvas.tsx", "src/SuspenseLoader.tsx")}}


def _validate_runtime_source_snapshot(source: dict[str, Any]) -> None:
    if (
        source.get("name") != "remotion-dev/remotion"
        or source.get("version") != REMOTION_RUNTIME_VERSION
        or source.get("source_commit") != REMOTION_RUNTIME_COMMIT
        or source.get("license_policy")
        != "eligibility_must_be_declared_per_materialized_project"
    ):
        raise ContractError("Remotion runtime source snapshot is invalid")
    core = validate_artifact_reference(
        _object(source.get("core_package")), "Remotion runtime package"
    )
    validate_artifact_reference(_object(source.get("cli_package")), "Remotion CLI")
    validate_artifact_reference(
        _object(source.get("license")), "Remotion runtime license"
    )
    root = core.parents[2]
    if _runtime_source(root) != source:
        raise ContractError("Remotion runtime source snapshot drifted")


def _installed_runtime(project: Path) -> dict[str, Any]:
    lock = project / "package-lock.json"
    if not lock.is_file():
        raise ContractError("Remotion project requires an npm package-lock.json")
    package_rows: dict[str, Any] = {}
    package_definition = read_json_object(project / "package.json", "Remotion project package")
    required_packages = dict(_RUNTIME_PACKAGES)
    if "@remotion/three" in package_definition.get("dependencies", {}):
        required_packages.update(THREE_PACKAGES)
    for name, version in required_packages.items():
        package = project / "node_modules" / Path(*name.split("/")) / "package.json"
        metadata = read_json_object(package, f"installed Remotion package {name}")
        if metadata.get("name") != name or metadata.get("version") != version:
            raise ContractError(f"installed Remotion package {name} drifted")
        package_rows[name] = artifact_reference(
            package, f"installed Remotion package {name}"
        )
    cli = project / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
    if not cli.is_file():
        raise ContractError("installed Remotion CLI entry was not found")
    snapshot = {
        "package_lock": artifact_reference(lock, "Remotion npm package lock"),
        "packages": package_rows,
        "cli_entry": artifact_reference(cli, "installed Remotion CLI entry"),
    }
    _validate_installed_runtime_snapshot(snapshot)
    return snapshot


def _validate_installed_runtime_snapshot(snapshot: dict[str, Any]) -> None:
    validate_artifact_reference(
        _object(snapshot.get("package_lock")), "Remotion npm package lock"
    )
    packages = _object(snapshot.get("packages"))
    required_packages = dict(_RUNTIME_PACKAGES)
    if "@remotion/three" in packages:
        required_packages.update(THREE_PACKAGES)
    if set(packages) != set(required_packages):
        raise ContractError("installed Remotion package inventory is incomplete")
    for name, reference in packages.items():
        package = validate_artifact_reference(
            _object(reference), f"installed Remotion package {name}"
        )
        metadata = read_json_object(package, f"installed Remotion package {name}")
        if (
            metadata.get("name") != name
            or metadata.get("version") != required_packages[name]
        ):
            raise ContractError(f"installed Remotion package {name} drifted")
    validate_artifact_reference(
        _object(snapshot.get("cli_entry")), "installed Remotion CLI entry"
    )


def build_remotion_timeline_preview(
    packaging_plan_path: str | Path,
    *,
    composition_id: str,
    safe_media_scene_plan_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only entry to the production project-data builder; never render/copy."""
    if not composition_id or not all(
        char.isascii() and (char.isalnum() or char == "-") for char in composition_id
    ):
        raise ContractError("invalid preview composition id")
    packaging_ref = artifact_reference(packaging_plan_path, "video packaging plan")
    inputs = {"packaging_plan": packaging_ref}
    if safe_media_scene_plan_path is not None:
        scene_plan = read_json_object(safe_media_scene_plan_path, "scene plan")
        validate_safe_media_scene_plan(scene_plan)
        if scene_plan["source_packaging_plan"] != packaging_ref:
            raise ContractError("preview media plan packaging drift")
        inputs["safe_media_scene_plan"] = artifact_reference(
            safe_media_scene_plan_path, "scene plan"
        )
    return _project_data(
        {"inputs": inputs, "composition": {"id": composition_id}},
        include_safe_layout_media=False,
    )


def _project_data(
    handoff: dict[str, Any],
    *,
    include_safe_layout_media: bool = False,
    allow_plain_fallback: bool = False,
) -> dict[str, Any]:
    inputs = _object(handoff.get("inputs"))
    handoff_sub_style = (
        _object(handoff.get("subtitle_style"))
        if handoff.get("subtitle_style") is not None
        else {}
    )
    requested_sub_features: list[str] = []
    if handoff_sub_style.get("speaker_colors"):
        requested_sub_features.append("speaker_colors")
    if inputs.get("expressive_cues") or handoff_sub_style.get("expressive_cues"):
        requested_sub_features.append("expressive_cues")
    if requested_sub_features and not allow_plain_fallback:
        raise ContractError(
            "remotion_path3_unsupported_expressive_or_speaker_colors: "
            + ",".join(requested_sub_features)
        )
    subtitle_capabilities = {
        "requested_features": requested_sub_features,
        "applied_features": [],
        "delivery_status": "PASS_WITH_FALLBACK" if requested_sub_features else "PASS",
        "fallback_reason": (
            "remotion_path3_plain_fallback_explicitly_allowed" if requested_sub_features else None
        ),
    }
    packaging_reference = _object(inputs["packaging_plan"])
    packaging_path = validate_artifact_reference(
        packaging_reference, "video packaging plan"
    )
    packaging = read_json_object(packaging_path, "video packaging plan")
    if packaging.get("schema") != "video_creation_pipeline.video_packaging_plan.v1":
        raise ContractError("Remotion project requires a video packaging plan")
    output = _object(packaging.get("output"))
    width = _positive_int(output.get("width"), "packaging width")
    height = _positive_int(output.get("height"), "packaging height")
    fps = _positive_int(output.get("fps"), "packaging fps")
    scenes_value = packaging.get("scenes")
    if not isinstance(scenes_value, list) or not scenes_value:
        raise ContractError("Remotion project requires packaging scenes")
    style = _remotion_style(inputs)
    route_map = _remotion_routes(inputs)
    palette = ("#12355b", "#2d6a4f", "#7f4f24", "#5a189a", "#8a1538")
    scenes: list[dict[str, Any]] = []
    for index, raw in enumerate(scenes_value):
        scene = _object(raw)
        scene_id = str(scene.get("scene_id") or "").strip()
        start_s = _nonnegative_number(scene.get("start_s"), "scene start")
        end_s = _positive_number(scene.get("end_s"), "scene end")
        if not scene_id or end_s <= start_s:
            raise ContractError("Remotion packaging scene is invalid")
        evidence = scene.get("evidence_refs")
        evidence_refs = (
            [str(value) for value in evidence if str(value).strip()]
            if isinstance(evidence, list)
            else []
        )
        scenes.append(
            {
                "sceneId": scene_id,
                "startFrame": round(start_s * fps),
                "durationInFrames": max(1, round((end_s - start_s) * fps)),
                "evidenceRefs": evidence_refs,
                "background": (
                    style["background"]
                    if "visual_style_profile" in inputs
                    else palette[index % len(palette)]
                ),
                "visualRoute": route_map.get(scene_id, {}).get(
                    "visual_route", "unrouted_contract_preview"
                ),
                "renderer": route_map.get(scene_id, {}).get("renderer", "remotion"),
            }
        )
    duration_frames = max(
        scene["startFrame"] + scene["durationInFrames"] for scene in scenes
    )
    subtitle_file = "subtitles.srt" if "subtitle_artifact" in inputs else None
    subtitle_region = (
        _default_subtitle_region(width, height) if subtitle_file is not None else None
    )
    # Intent: public subtitle rendering must bind a reviewed type floor.
    # Decision: only the reviewed 1080x1920 profile supplies a default binding.
    # Reason/evidence/scope: typography-gates.v1.json; no renderer is duplicated.
    subtitle_typography: dict[str, Any] | None = None
    subtitle_style: dict[str, Any] | None = None
    if subtitle_file is not None:
        from .typography_gates import build_typography_gate

        if (width, height) == (1080, 1920):
            subtitle_typography = build_typography_gate(
                "semantic_motion_1080x1920", width=width, height=height
            )
        elif (width, height) == (540, 960):
            subtitle_typography = build_typography_gate(
                "semantic_motion_540x960", width=width, height=height
            )
        else:
            base_gate = build_typography_gate(
                "semantic_motion_1080x1920", width=1080, height=1920
            )
            scale = min(width, height) / 1080
            subtitle_typography = {
                **base_gate,
                "viewport": {"width": width, "height": height},
                "roles": {
                    role: max(1, round(px * scale))
                    for role, px in base_gate["roles"].items()
                },
            }
        handoff_sub_style = (
            _object(handoff.get("subtitle_style"))
            if handoff.get("subtitle_style") is not None
            else {}
        )
        resolved_sub = resolve_subtitle_style(
            width,
            height,
            preset_id=handoff_sub_style.get("preset_id"),
            font_id=handoff_sub_style.get("font_id"),
            background_opacity=handoff_sub_style.get("background_opacity"),
        )
        subtitle_style = {
            "presetId": resolved_sub["preset_id"],
            "policySha256": resolved_sub["policy_sha256"],
            "implementationSha256": resolved_sub["implementation_sha256"],
            "fontId": resolved_sub["font"]["font_id"],
            "fontFamily": resolved_sub["font"]["family"],
            "fontPostscriptName": resolved_sub["font"]["postscript_name"],
            "fontWeight": resolved_sub["font"]["weight"],
            "fontPublicPath": f"fonts/{resolved_sub['font']['filename']}",
            "fontSha256": resolved_sub["font"]["sha256"],
            **resolved_sub["css"],
        }
    motion_cues: list[dict[str, Any]] = []
    motion_typography: dict[str, Any] | None = None
    if "semantic_motion_plan" in inputs:
        motion_path = Path(str(_object(inputs["semantic_motion_plan"])["path"]))
        motion = read_json_object(motion_path, "semantic motion plan")
        from .semantic_motion import validate_semantic_motion_plan
        from .typography_gates import validate_typography_gate

        validate_semantic_motion_plan(motion)
        motion_typography = validate_typography_gate(
            _object(motion.get("typography_gate")),
            profile_id="semantic_motion_1080x1920",
            width=width,
            height=height,
        )
        # One reviewed 1080x1920 binding governs both motion labels and captions.
        # The renderer may select an actual size at or above these minima.
        subtitle_typography = motion_typography
        if subtitle_file is not None:
            subtitle_region = _semantic_subtitle_region(motion)
        cues = motion.get("cues")
        if isinstance(cues, list):
            for raw in cues:
                cue = _object(raw)
                timing = _object(cue.get("timing"))
                label = _object(cue.get("label"))
                focus = _object(cue.get("focus_destination"))
                protected_regions = [
                    {
                        "id": str(_object(region).get("id") or ""),
                        "rect": {
                            key: _normalized_number(
                                _object(_object(region).get("rect")).get(key),
                                f"protected region {key}",
                            )
                            for key in ("x", "y", "width", "height")
                        },
                    }
                    for region in cue.get("protected_regions") or []
                ]
                privacy_avoidance = cue.get("privacy_avoidance")
                privacy_projection = None
                if privacy_avoidance is not None:
                    privacy_value = _object(privacy_avoidance)
                    if privacy_value.get("runtime_reposition_allowed") is not False:
                        raise ContractError(
                            "Remotion cannot reposition privacy-aware layout at runtime"
                        )
                    privacy_projection = {
                        "status": str(privacy_value.get("status") or ""),
                        "regionIds": [
                            str(value)
                            for value in privacy_value.get("region_ids") or []
                        ],
                        "labelPositionChanged": privacy_value.get(
                            "label_position_changed"
                        )
                        is True,
                        "runtimeRepositionAllowed": False,
                    }
                start_s = _nonnegative_number(
                    timing.get("timeline_start_s"), "cue start"
                )
                duration_s = sum(
                    _nonnegative_number(timing.get(field), f"cue timing {field}")
                    for field in ("full_frame_s", "enter_s", "hold_s", "exit_s")
                )
                if duration_s <= 0:
                    raise ContractError("Remotion motion cue duration must be positive")
                motion_cues.append(
                    {
                        "cueId": str(cue.get("cue_id") or "motion-cue"),
                        "sceneId": str(cue.get("story_segment_ref") or ""),
                        "startFrame": round(start_s * fps),
                        "durationInFrames": max(1, round(duration_s * fps)),
                        "label": str(label.get("text") or "Evidence focus"),
                        "focus": {
                            key: _normalized_number(focus.get(key), f"focus {key}")
                            for key in ("x", "y", "width", "height")
                        },
                        "protectedRegions": protected_regions,
                        "privacyAvoidance": privacy_projection,
                    }
                )
    safe_layout: dict[str, Any] | None = None
    if "safe_media_layout_plan" in inputs:
        layout_path = validate_artifact_reference(
            _object(inputs["safe_media_layout_plan"]), "safe media layout plan"
        )
        layout = read_json_object(layout_path, "safe media layout plan")
        safe_layout = _safe_layout_projection(
            layout, fps=fps, include_media=include_safe_layout_media
        )
    elif "safe_media_scene_plan" in inputs:
        scene_plan = _validated_scene_plan(inputs)
        assignments = {
            str(_object(row).get("scene_id") or ""): _object(row)
            for row in scene_plan.get("scenes", [])
        }
        if set(assignments) != {str(scene["sceneId"]) for scene in scenes}:
            raise ContractError(
                "Remotion scene media assignments drifted from packaging"
            )
        for scene in scenes:
            assignment = assignments[str(scene["sceneId"])]
            if assignment.get("assignment_status") == "layout_ready":
                layout_path = validate_artifact_reference(
                    _object(assignment.get("safe_media_layout_plan")),
                    "Remotion scene safe media layout plan",
                )
                layout = read_json_object(layout_path, "safe media layout plan")
                scene["mediaLayout"] = _safe_layout_projection(
                    layout,
                    fps=fps,
                    include_media=include_safe_layout_media,
                )
                scene["missingMaterialReason"] = None
            else:
                scene["mediaLayout"] = None
                scene["missingMaterialReason"] = str(
                    assignment.get("missing_material_reason") or ""
                )
    elif include_safe_layout_media:
        raise ContractError(
            "explicit Remotion source media rendering requires a safe layout plan"
        )
    object_scenes = object_scene_projection(inputs, packaging)
    return {
        **({"objectScenes": object_scenes} if object_scenes else {}),
        "compositionId": str(_object(handoff["composition"])["id"]),
        "candidateOnly": True,
        "width": width,
        "height": height,
        "fps": fps,
        "durationInFrames": duration_frames,
        "scenes": scenes,
        "motionCues": motion_cues,
        "externalMotionSelections": _external_motion_selection_projection(inputs),
        "safeLayout": safe_layout,
        "subtitleFile": subtitle_file,
        "subtitleRegion": subtitle_region,
        "subtitleCapabilities": subtitle_capabilities,
        "subtitleTypography": subtitle_typography,
        "subtitleStyle": subtitle_style,
        "motionTypography": motion_typography,
        "sourceMediaRendered": include_safe_layout_media,
        "style": style,
        "titleCoverVariants": _title_cover_variants(inputs, safe_layout=safe_layout),
    }


def _external_motion_selection_projection(
    inputs: dict[str, Any],
) -> list[dict[str, Any]]:
    reference = inputs.get("external_motion_selection_plan")
    if reference is None:
        return []
    from .external_motion_assets import validate_external_motion_selection_plan

    path = validate_artifact_reference(
        _object(reference), "external motion selection plan"
    )
    payload = read_json_object(path, "external motion selection plan")
    validate_external_motion_selection_plan(payload)
    projected: list[dict[str, Any]] = []
    for raw in payload.get("selections") or []:
        row = _object(raw)
        component = _object(row.get("motion_component"))
        sound = _object(row.get("sound_effect"))
        projected.append(
            {
                "cueId": str(row.get("cue_id") or ""),
                "sceneId": str(row.get("story_segment_ref") or ""),
                "informationRole": str(row.get("information_role") or ""),
                "componentSource": str(component.get("source_id") or ""),
                "componentId": str(component.get("item_id") or ""),
                "componentSelectionBasis": str(component.get("selection_basis") or ""),
                "sourceItem": dict(_object(component.get("source_item"))),
                "soundAssetId": sound.get("asset_id"),
                "soundSelectionBasis": str(sound.get("selection_basis") or ""),
                "centralTimelineAudio": sound.get("central_timeline_cue_required")
                is True,
                "componentEmbeddedAudioForbidden": True,
            }
        )
    return projected


def _title_cover_variants(
    inputs: dict[str, Any], *, safe_layout: dict[str, Any] | None
) -> list[dict[str, Any]]:
    collection_reference = inputs.get("case_cover_visual_plan_collection")
    if collection_reference is not None:
        from .case_cover_visual_plan import (
            case_cover_collection_variant_projection,
            validate_case_cover_visual_plan_collection,
        )

        collection_path = validate_artifact_reference(
            _object(collection_reference), "Lucas case cover visual plan collection"
        )
        collection = read_json_object(
            collection_path, "Lucas case cover visual plan collection"
        )
        safe_layout_path = None
        if "safe_media_layout_plan" in inputs:
            safe_layout_path = validate_artifact_reference(
                _object(inputs["safe_media_layout_plan"]),
                "safe media layout plan",
            )
        plans = validate_case_cover_visual_plan_collection(
            collection,
            safe_media_layout_path=safe_layout_path,
        )
        return case_cover_collection_variant_projection(
            plans,
            safe_layout=safe_layout,
        )
    case_reference = inputs.get("case_cover_visual_plan")
    if case_reference is not None:
        from .case_cover_visual_plan import (
            case_cover_variant_projection,
            validate_case_cover_visual_plan,
        )

        case_path = validate_artifact_reference(
            _object(case_reference), "Lucas case cover visual plan"
        )
        plan = read_json_object(case_path, "Lucas case cover visual plan")
        safe_layout_path = None
        if "safe_media_layout_plan" in inputs:
            safe_layout_path = validate_artifact_reference(
                _object(inputs["safe_media_layout_plan"]),
                "safe media layout plan",
            )
        validate_case_cover_visual_plan(
            plan,
            safe_media_layout_path=safe_layout_path,
        )
        return case_cover_variant_projection(plan, safe_layout=safe_layout)
    reference = inputs.get("title_cover_candidate_plan")
    if reference is None:
        return []
    from .title_cover import validate_title_cover_candidate_plan

    plan_path = validate_artifact_reference(
        _object(reference), "title cover candidate plan"
    )
    plan = read_json_object(plan_path, "title cover candidate plan")
    validate_title_cover_candidate_plan(plan)
    profiles = _object(plan.get("platform_profiles"))
    variants: list[dict[str, Any]] = []
    for raw_candidate in plan.get("candidates") or []:
        candidate = _object(raw_candidate)
        composition_candidate_id = str(candidate["candidate_id"]).replace("_", "-")
        hypothesis = _object(candidate.get("cover_hypothesis"))
        source = _object(candidate.get("cover_source"))
        render_ready = (
            _object(candidate.get("render_readiness")).get("render_eligible") is True
        )
        for profile_id in candidate.get("profile_ids") or []:
            profile = _object(profiles.get(str(profile_id)))
            if not profile:
                raise ContractError("Remotion title cover profile is missing")
            composition_profile_id = str(profile_id).replace("_", "-")
            variants.append(
                {
                    "compositionId": (
                        f"Cover-{composition_candidate_id}-{composition_profile_id}"
                    ),
                    "candidateId": str(candidate["candidate_id"]),
                    "strategy": str(candidate["strategy"]),
                    "title": str(candidate["title"]),
                    "overlayText": str(hypothesis.get("overlay_text") or ""),
                    "visualFocus": str(hypothesis.get("visual_focus") or ""),
                    "audiencePromise": str(hypothesis.get("audience_promise") or ""),
                    "sourceKind": str(source.get("source_kind") or ""),
                    "renderEligible": render_ready,
                    "profileId": str(profile_id),
                    "width": _positive_int(profile.get("width"), "cover width"),
                    "height": _positive_int(profile.get("height"), "cover height"),
                    "safeZone": {
                        key: _normalized_number(
                            _object(profile.get("safe_zone")).get(key),
                            f"cover safe zone {key}",
                        )
                        for key in ("x", "y", "width", "height")
                    },
                    "candidateOnly": True,
                }
            )
    return variants


def _remotion_style(inputs: dict[str, Any]) -> dict[str, str]:
    if "visual_style_profile" not in inputs:
        return {
            "background": "#111827",
            "foreground": "#ffffff",
            "primary": "#f8e16c",
            "accent": "#b42318",
            "muted": "#c7ccd3",
            "panel": "#111827",
            "displayFont": "Arial, sans-serif",
            "bodyFont": "Arial, sans-serif",
        }
    from .visual_routing import validate_visual_style_profile

    style_path = validate_artifact_reference(
        _object(inputs["visual_style_profile"]), "visual style profile"
    )
    profile = read_json_object(style_path, "visual style profile")
    validate_visual_style_profile(profile)
    binding = _object(_object(profile["renderer_bindings"])["remotion"])
    return {
        "background": str(binding["background"]),
        "foreground": str(binding["foreground"]),
        "primary": str(binding["primary"]),
        "accent": str(binding["accent"]),
        "muted": str(binding["muted"]),
        "panel": str(binding["panel"]),
        "displayFont": str(binding["display_font"]),
        "bodyFont": str(binding["body_font"]),
    }


def _remotion_routes(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if "content_visual_routing_plan" not in inputs:
        return {}
    from .visual_routing import validate_content_visual_routing_plan

    route_path = validate_artifact_reference(
        _object(inputs["content_visual_routing_plan"]),
        "content visual routing plan",
    )
    route = read_json_object(route_path, "content visual routing plan")
    validate_content_visual_routing_plan(route)
    return {
        str(_object(row).get("scene_id") or ""): _object(row)
        for row in route.get("scene_routes") or []
    }


def _safe_layout_projection(
    layout: dict[str, Any], *, fps: int, include_media: bool
) -> dict[str, Any]:
    validate_safe_media_layout_plan(layout)
    placements = layout.get("placements")
    if not isinstance(placements, list) or not placements:
        raise ContractError("Remotion safe layout has no placements")
    result: dict[str, Any] = {
        "placementCount": len(placements),
        "containPolicyPreserved": True,
    }
    if include_media:
        preflight_path = validate_artifact_reference(
            _object(layout.get("source_preflight")),
            "Remotion safe layout source preflight",
        )
        preflight = read_json_object(preflight_path, "photo media preflight")
        source_by_id = {
            str(_object(row).get("asset_id") or ""): _object(row)
            for row in preflight.get("assets", [])
        }
        result["placements"] = [
            _remotion_media_placement(
                _object(placement),
                source_by_id,
                fps=fps,
            )
            for placement in placements
        ]
    return result


def _validated_scene_plan(inputs: dict[str, Any]) -> dict[str, Any]:
    scene_plan_path = validate_artifact_reference(
        _object(inputs.get("safe_media_scene_plan")), "safe media scene plan"
    )
    scene_plan = read_json_object(scene_plan_path, "safe media scene plan")
    validate_safe_media_scene_plan(scene_plan)
    if _object(scene_plan.get("source_packaging_plan")) != _object(
        inputs.get("packaging_plan")
    ):
        raise ContractError("Remotion scene plan must bind the handoff packaging plan")
    return scene_plan


def _default_subtitle_region(width: int, height: int) -> dict[str, float]:
    if height > width:
        return {"x": 0.1111, "y": 0.6458, "width": 0.6481, "height": 0.0896}
    return {"x": 0.08, "y": 0.78, "width": 0.84, "height": 0.14}


def _semantic_subtitle_region(motion: dict[str, Any]) -> dict[str, float]:
    cues = motion.get("cues")
    if not isinstance(cues, list) or not cues:
        raise ContractError("subtitle rendering requires semantic motion cues")
    regions: list[dict[str, float]] = []
    for raw_cue in cues:
        cue = _object(raw_cue)
        protected = cue.get("protected_regions")
        if not isinstance(protected, list):
            raise ContractError("semantic motion cue lacks protected regions")
        matches = [
            _object(row)
            for row in protected
            if str(_object(row).get("id") or "").strip() == "subtitle"
        ]
        if len(matches) != 1:
            raise ContractError(
                "each semantic motion cue must define one subtitle region"
            )
        rect = _object(matches[0].get("rect"))
        region = {
            key: _normalized_number(rect.get(key), f"subtitle region {key}")
            for key in ("x", "y", "width", "height")
        }
        if (
            region["width"] <= 0
            or region["height"] <= 0
            or region["x"] + region["width"] > 1
            or region["y"] + region["height"] > 1
        ):
            raise ContractError("semantic subtitle region is out of bounds")
        regions.append(region)
    if any(region != regions[0] for region in regions[1:]):
        raise ContractError("semantic subtitle region drifted across cues")
    return regions[0]


def _project_file_contents(
    data: dict[str, Any], *, subtitle_text: str
) -> dict[str, str]:
    package = {
        "name": "mvp-video-remotion-contract-preview",
        "version": "0.0.0",
        "private": True,
        "license": "UNLICENSED",
        "scripts": {
            "compositions": "remotion compositions src/index.ts",
            "render": "remotion render src/index.ts",
            "studio": "remotion studio src/index.ts",
        },
        "dependencies": dict(_RUNTIME_PACKAGES),
    }
    object_sources = object_project_sources(data.get("objectScenes", []))
    if any(row["componentId"] in THREE_COMPONENTS for row in data.get("objectScenes", [])):
        package["dependencies"].update(THREE_PACKAGES)
    tsconfig = {
        "compilerOptions": {
            "target": "ES2022",
            "module": "ESNext",
            "moduleResolution": "Bundler",
            "jsx": "react-jsx",
            "strict": True,
            "resolveJsonModule": True,
            "esModuleInterop": True,
            "skipLibCheck": True,
            "noEmit": True,
        },
        "include": ["src"],
    }
    return {
        "package.json": json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        "tsconfig.json": json.dumps(tsconfig, ensure_ascii=False, indent=2) + "\n",
        "remotion.config.ts": _REMOTION_CONFIG,
        "src/index.ts": _INDEX_TS,
        "src/Root.tsx": _ROOT_TSX,
        "src/Composition.tsx": _object_composition_source(data) if data.get("objectScenes") else _COMPOSITION_TSX,
        **object_sources,
        "src/Cover.tsx": _COVER_TSX,
        "src/Captions.tsx": _CAPTIONS_TSX,
        "src/project-data.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        "public/subtitles.srt": subtitle_text,
    }


def _object_composition_source(data: dict[str, Any]) -> str:
    entry = "ProcessObjectScene as ObjectScene" if any(row["componentId"] in PROCESS_COMPONENTS for row in data["objectScenes"]) else "ObjectScene"
    file = "ProcessObjectScene" if entry != "ObjectScene" else "ObjectScene"
    source = f"import {{{entry}}} from './object-scenes/{file}';\n" + _COMPOSITION_TSX
    source = source.replace("  scenes: Scene[];", "  scenes: Scene[];\n  objectScenes: import('./object-scenes/object-motion').ObjectSceneData[];")
    source = source.replace("      {data.subtitleFile && data.subtitleRegion", "      {data.objectScenes.map((scene) => <Sequence key={scene.id} from={scene.startFrame} durationInFrames={scene.durationInFrames}><ObjectScene scene={scene} palette={data.style}/></Sequence>)}\n      {data.subtitleFile && data.subtitleRegion")
    return source


def _manifest_inputs(handoff_path: Path, handoff: dict[str, Any]) -> dict[str, Any]:
    inputs = {"handoff": artifact_reference(handoff_path, "Remotion skill handoff")}
    for name, reference in _object(handoff.get("inputs")).items():
        path = validate_artifact_reference(_object(reference), f"Remotion input {name}")
        inputs[name] = artifact_reference(path, f"Remotion input {name}")
    return inputs


def _materialize_safe_layout_media(
    handoff: dict[str, Any],
    staging: Path,
    project: Path,
    *,
    media_mode: str,
) -> list[dict[str, Any]]:
    inputs = _object(handoff.get("inputs"))
    assignments = _media_layout_assignments(inputs, media_mode)

    rows: list[dict[str, Any]] = []
    copied_public_paths: dict[str, dict[str, Any]] = {}
    for scene_id, layout in assignments:
        placements = layout.get("placements")
        if not isinstance(placements, list) or not placements:
            raise ContractError("Remotion safe layout media inventory is empty")
        for raw in placements:
            placement = _object(raw)
            source_reference = _object(placement.get("source_asset"))
            source = validate_artifact_reference(
                source_reference, "Remotion safe layout source asset"
            )
            media_type = str(placement.get("media_type") or "")
            _validate_media_extension(source, media_type)
            public_path = f"media/{_layout_media_filename(placement)}"
            previous = copied_public_paths.get(public_path)
            if previous is not None and previous != source_reference:
                raise ContractError("Remotion safe layout media filename collided")
            staged = staging / "public" / Path(public_path)
            if previous is None:
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, staged)
                copied_public_paths[public_path] = dict(source_reference)
            project_asset = _staged_reference(staged, project / "public" / public_path)
            if project_asset.get("sha256") != source_reference.get(
                "sha256"
            ) or project_asset.get("bytes") != source_reference.get("bytes"):
                raise ContractError("Remotion safe layout media changed during copy")
            row: dict[str, Any] = {
                "asset_id": str(placement.get("asset_id") or ""),
                "media_type": media_type,
                "mime_type": _media_mime_type(source, media_type),
                "public_path": public_path,
                "source_asset": dict(source_reference),
                "project_asset": project_asset,
                "bounds_px": dict(_object(placement.get("bounds_px"))),
                "fit_mode": "contain",
                "crop_allowed": False,
                "audio_policy": "muted_visual_only"
                if media_type == "video"
                else "not_applicable",
            }
            if scene_id is not None:
                row["scene_id"] = scene_id
            rows.append(row)
    return rows


def _validate_media_materialization(
    value: dict[str, Any],
    handoff: dict[str, Any],
    project: Path,
    *,
    media_mode: str,
) -> None:
    assets = value.get("assets")
    if not isinstance(assets, list):
        raise ContractError("Remotion media materialization assets must be a list")
    if media_mode == "none":
        if value.get("mode") != "none" or assets:
            raise ContractError("Remotion no-media materialization inventory drifted")
        return
    if value.get("mode") != media_mode or not assets:
        raise ContractError("Remotion explicit media materialization is incomplete")

    inputs = _object(handoff.get("inputs"))
    assignments = _media_layout_assignments(inputs, media_mode)
    expected_placements = [
        (scene_id, _object(raw))
        for scene_id, layout in assignments
        for raw in layout.get("placements", [])
    ]
    if len(assets) != len(expected_placements):
        raise ContractError("Remotion materialized media count drifted")

    seen_public_paths: dict[str, dict[str, Any]] = {}
    for raw_asset, (scene_id, placement) in zip(
        assets, expected_placements, strict=True
    ):
        asset = _object(raw_asset)
        source_reference = _object(placement.get("source_asset"))
        source = validate_artifact_reference(
            source_reference, "Remotion safe layout source asset"
        )
        media_type = str(placement.get("media_type") or "")
        _validate_media_extension(source, media_type)
        public_path = f"media/{_layout_media_filename(placement)}"
        previous = seen_public_paths.get(public_path)
        if previous is not None and previous != source_reference:
            raise ContractError("Remotion materialized media paths collided")
        seen_public_paths[public_path] = source_reference
        if asset.get("public_path") != public_path:
            raise ContractError("Remotion materialized media public path drifted")
        project_asset = _object(asset.get("project_asset"))
        project_path = validate_artifact_reference(
            project_asset, "Remotion materialized project asset"
        )
        if project_path != (project / "public" / Path(public_path)).resolve():
            raise ContractError("Remotion materialized media escaped the project")
        if project_asset.get("sha256") != source_reference.get(
            "sha256"
        ) or project_asset.get("bytes") != source_reference.get("bytes"):
            raise ContractError("Remotion materialized media drifted from its source")
        expected = {
            "asset_id": str(placement.get("asset_id") or ""),
            "media_type": media_type,
            "mime_type": _media_mime_type(source, media_type),
            "public_path": public_path,
            "source_asset": source_reference,
            "project_asset": project_asset,
            "bounds_px": _object(placement.get("bounds_px")),
            "fit_mode": "contain",
            "crop_allowed": False,
            "audio_policy": "muted_visual_only"
            if media_type == "video"
            else "not_applicable",
        }
        if scene_id is not None:
            expected["scene_id"] = scene_id
        if asset != expected:
            raise ContractError("Remotion materialized media metadata drifted")


def _media_layout_assignments(
    inputs: dict[str, Any], media_mode: str
) -> list[tuple[str | None, dict[str, Any]]]:
    if media_mode == "explicit_copy_from_safe_layout":
        layout_path = validate_artifact_reference(
            _object(inputs.get("safe_media_layout_plan")),
            "Remotion safe media layout plan",
        )
        layout = read_json_object(layout_path, "safe media layout plan")
        validate_safe_media_layout_plan(layout)
        return [(None, layout)]
    if media_mode == "explicit_copy_from_scene_plan":
        scene_plan = _validated_scene_plan(inputs)
        assignments: list[tuple[str | None, dict[str, Any]]] = []
        for raw in scene_plan.get("scenes", []):
            row = _object(raw)
            if row.get("assignment_status") != "layout_ready":
                continue
            layout_path = validate_artifact_reference(
                _object(row.get("safe_media_layout_plan")),
                "Remotion scene safe media layout plan",
            )
            layout = read_json_object(layout_path, "safe media layout plan")
            validate_safe_media_layout_plan(layout)
            assignments.append((str(row.get("scene_id") or ""), layout))
        if not assignments:
            raise ContractError("Remotion scene plan has no ready media layouts")
        return assignments
    raise ContractError("Remotion media materialization mode is invalid")


def _remotion_media_placement(
    placement: dict[str, Any],
    source_by_id: dict[str, dict[str, Any]],
    *,
    fps: int,
) -> dict[str, Any]:
    asset_id = str(placement.get("asset_id") or "")
    source = source_by_id.get(asset_id)
    if source is None:
        raise ContractError("Remotion safe layout source asset is unavailable")
    media_type = str(placement.get("media_type") or "")
    duration_frames: int | None = None
    if media_type == "video":
        duration_s = _positive_number(
            source.get("duration_s"), "Remotion source video duration"
        )
        duration_frames = max(1, round(duration_s * fps))
    return {
        "assetId": asset_id,
        "mediaType": media_type,
        "publicPath": f"media/{_layout_media_filename(placement)}",
        "boundsPx": {
            key: _nonnegative_number(
                _object(placement.get("bounds_px")).get(key),
                f"Remotion media bound {key}",
            )
            for key in ("x", "y", "width", "height")
        },
        "fitMode": "contain",
        "cropAllowed": False,
        "sourceDurationInFrames": duration_frames,
        "audioMuted": media_type == "video",
    }


def _layout_media_filename(placement: dict[str, Any]) -> str:
    source_reference = _object(placement.get("source_asset"))
    source = Path(str(source_reference.get("path") or ""))
    suffix = source.suffix.casefold()
    digest = hashlib.sha256(
        (
            f"{placement.get('asset_id') or ''!s}:"
            f"{source_reference.get('sha256') or ''!s}"
        ).encode()
    ).hexdigest()[:20]
    return f"{digest}{suffix}"


def _validate_media_extension(path: Path, media_type: str) -> None:
    suffix = path.suffix.casefold()
    allowed = (
        _REMOTION_IMAGE_EXTENSIONS
        if media_type == "image"
        else _REMOTION_VIDEO_EXTENSIONS
    )
    if media_type not in {"image", "video"} or suffix not in allowed:
        raise ContractError("Remotion safe layout media type or extension is invalid")


def _media_mime_type(path: Path, media_type: str) -> str:
    overrides = {
        ".m4v": "video/x-m4v",
        ".mkv": "video/x-matroska",
        ".webp": "image/webp",
    }
    value = overrides.get(path.suffix.casefold()) or mimetypes.guess_type(path.name)[0]
    if not value or not value.startswith(f"{media_type}/"):
        raise ContractError("Remotion safe layout media MIME type is unsupported")
    return value


def _reuse_materialized_project(
    project: Path,
    handoff: dict[str, Any],
    source: dict[str, Any],
    basis: str,
    *,
    media_mode: str,
) -> dict[str, Path]:
    manifest_path = project / "remotion-project-manifest.json"
    if not manifest_path.is_file():
        raise ContractError(
            "Remotion project directory exists without a managed manifest"
        )
    manifest = read_json_object(manifest_path, "Remotion project manifest")
    validate_remotion_project_manifest(manifest)
    handoff_ref = _object(manifest.get("source_handoff"))
    current_handoff = read_json_object(handoff_ref["path"], "Remotion handoff")
    if current_handoff != handoff:
        raise ContractError("Remotion project belongs to a different handoff")
    if (
        manifest.get("runtime_source") != source
        or manifest.get("license_basis") != basis
    ):
        raise ContractError("Remotion project runtime or license basis drifted")
    materialization_value = manifest.get("media_materialization")
    existing_media_mode = (
        "none"
        if materialization_value is None
        else str(_object(materialization_value).get("mode") or "")
    )
    if existing_media_mode != media_mode:
        raise ContractError("Remotion project source media mode drifted")
    return _materialized_paths(project)


def _materialized_paths(project: Path) -> dict[str, Path]:
    return {
        "project": project,
        "manifest": project / "remotion-project-manifest.json",
        "entry": project / "src" / "index.ts",
        "composition": project / "src" / "Composition.tsx",
        "data": project / "src" / "project-data.json",
        "license": project / "REMOTION_LICENSE.md",
    }


def _staged_reference(staged: Path, final: Path) -> dict[str, Any]:
    reference = artifact_reference(staged, f"staged Remotion file {staged.name}")
    reference["path"] = str(final.resolve())
    return reference


def _run_remotion(
    cli: Path,
    args: list[str],
    cwd: Path,
    timeout_s: float,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("REMOTION_LICENSE_KEY", None)
    env["CI"] = "1"
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
        raise ContractError(f"Remotion execution failed: {exc}") from exc
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise ContractError(
            f"Remotion {args[0]} failed with exit code {result.returncode}: "
            f"{output.strip()[-2000:]}"
        )
    return {"returncode": result.returncode, "output": output}


def _browser(path: Path | None) -> Path:
    if path is None:
        raise ContractError("Remotion browser path is required")
    browser = path.expanduser().resolve()
    if not browser.is_file():
        raise ContractError("Remotion browser executable was not found")
    return browser


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
        raise ContractError("Remotion source commit could not be read") from exc
    if result.returncode != 0:
        raise ContractError("Remotion source commit could not be read")
    return result.stdout.strip().lower()


def _atomic_text(value: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_copy_promote(source: Path, output: Path) -> None:
    if not source.is_file():
        raise ContractError("Remotion staging output was not created")
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(output)
    except OSError as exc:
        raise ContractError(f"Remotion output promotion failed: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result < 0:
        raise ContractError(f"{label} must be nonnegative")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise ContractError(f"{label} must be positive")
    return result


def _normalized_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result < 0 or result > 1:
        raise ContractError(f"{label} must be normalized")
    return result


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


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value


_REMOTION_CONFIG = """import {Config} from "@remotion/cli/config";

Config.setOverwriteOutput(true);
Config.setVideoImageFormat("jpeg");
"""

_INDEX_TS = """import {registerRoot} from "remotion";
import {RemotionRoot} from "./Root";

registerRoot(RemotionRoot);
"""

_ROOT_TSX = """import {Composition, Still} from "remotion";
import type {FC} from "react";
import {CandidateComposition} from "./Composition";
import {CoverStill} from "./Cover";
import projectData from "./project-data.json";

export const RemotionRoot: FC = () => {
  return (
    <>
      <Composition
        id={projectData.compositionId}
        component={CandidateComposition}
        durationInFrames={projectData.durationInFrames}
        fps={projectData.fps}
        width={projectData.width}
        height={projectData.height}
        defaultProps={{data: projectData}}
      />
      {projectData.titleCoverVariants
        .filter((variant) => variant.renderEligible)
        .map((variant) => (
          <Still
            key={variant.compositionId}
            id={variant.compositionId}
            component={CoverStill}
            width={variant.width}
            height={variant.height}
            defaultProps={{variant, visualStyle: projectData.style}}
          />
        ))}
    </>
  );
};
"""

_COMPOSITION_TSX = """import {
  AbsoluteFill,
  Img,
  interpolate,
  Loop,
  OffthreadVideo,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import type {FC} from "react";
import {Captions} from "./Captions";

type Rect = {x: number; y: number; width: number; height: number};
type SafePlacement = {
  assetId: string;
  mediaType: "image" | "video";
  publicPath: string;
  boundsPx: Rect;
  fitMode: "contain";
  cropAllowed: false;
  sourceDurationInFrames: number | null;
  audioMuted: boolean;
};
type SafeLayout = {
  placementCount: number;
  containPolicyPreserved: boolean;
  placements?: SafePlacement[];
};
type Scene = {
  sceneId: string;
  startFrame: number;
  durationInFrames: number;
  evidenceRefs: string[];
  background: string;
  mediaLayout?: SafeLayout | null;
  missingMaterialReason?: string | null;
  visualRoute: string;
  renderer: string;
};
type VisualStyle = {
  background: string;
  foreground: string;
  primary: string;
  accent: string;
  muted: string;
  panel: string;
  displayFont: string;
  bodyFont: string;
};
type MotionCue = {
  cueId: string;
  sceneId: string;
  startFrame: number;
  durationInFrames: number;
  label: string;
  focus: Rect;
  protectedRegions: {id: string; rect: Rect}[];
  privacyAvoidance: {
    status: string;
    regionIds: string[];
    labelPositionChanged: boolean;
    runtimeRepositionAllowed: false;
  } | null;
};
type ExternalMotionSelection = {
  cueId: string;
  sceneId: string;
  informationRole: string;
  componentSource: string;
  componentId: string;
  componentSelectionBasis: string;
  sourceItem: {path: string; sha256: string; bytes: number};
  soundAssetId: string | null;
  soundSelectionBasis: string;
  centralTimelineAudio: boolean;
  componentEmbeddedAudioForbidden: true;
};
type TypographyGate = {
  hard_gate: true;
  roles: Record<string, number>;
};
type SubtitleStyle = {
  presetId: string;
  policySha256: string;
  implementationSha256: string;
  fontId: string;
  fontFamily: string;
  fontPostscriptName: string;
  fontWeight: number;
  fontPublicPath: string;
  fontSha256: string;
  textColor: string;
  backgroundColor: string;
  backgroundHex: string | null;
  backgroundOpacity: number;
  outlineColor: string;
  outlinePx: number;
  shadowCss: string;
  paddingY: number;
  paddingX: number;
  borderRadius: number;
  fontPx: number;
};
type ProjectData = {
  height: number;
  candidateOnly: boolean;
  scenes: Scene[];
  motionCues: MotionCue[];
  externalMotionSelections: ExternalMotionSelection[];
  safeLayout: SafeLayout | null;
  subtitleFile: string | null;
  subtitleRegion: Rect | null;
  subtitleTypography: TypographyGate | null;
  subtitleStyle?: SubtitleStyle | null;
  motionTypography: TypographyGate | null;
  sourceMediaRendered: boolean;
  style: VisualStyle;
  titleCoverVariants: unknown[];
};

const MediaLayer: FC<{placements: SafePlacement[]}> = ({placements}) => {
  return (
    <>
      {placements.map((placement) => {
        const style = {
          position: "absolute" as const,
          left: placement.boundsPx.x,
          top: placement.boundsPx.y,
          width: placement.boundsPx.width,
          height: placement.boundsPx.height,
          objectFit: "contain" as const,
        };
        if (placement.mediaType === "image") {
          return (
            <Img
              key={placement.assetId}
              src={staticFile(placement.publicPath)}
              style={style}
            />
          );
        }
        if (!placement.sourceDurationInFrames || !placement.audioMuted) {
          throw new Error(`Invalid visual-only video placement: ${placement.assetId}`);
        }
        return (
          <div key={placement.assetId} style={style}>
            <Loop durationInFrames={placement.sourceDurationInFrames} layout="none">
              <OffthreadVideo
                muted
                src={staticFile(placement.publicPath)}
                style={{width: "100%", height: "100%", objectFit: "contain"}}
              />
            </Loop>
          </div>
        );
      })}
    </>
  );
};

const SceneCard: FC<{
  scene: Scene;
  cues: MotionCue[];
  safeLayout: ProjectData["safeLayout"];
  sourceMediaRendered: boolean;
  visualStyle: VisualStyle;
  typography: TypographyGate | null;
}> = ({scene, cues, safeLayout, sourceMediaRendered, visualStyle, typography}) => {
  const frame = useCurrentFrame();
  const sceneMediaRendered =
    sourceMediaRendered && Boolean(safeLayout?.placements?.length);
  const opacity = interpolate(frame, [0, 10], [0.3, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill
      style={{
        backgroundColor: scene.background,
        color: visualStyle.foreground,
        fontFamily: visualStyle.bodyFont,
      }}
    >
      {sceneMediaRendered && safeLayout?.placements ? (
        <MediaLayer placements={safeLayout.placements} />
      ) : null}
      {/* Internal contract diagnostics are deliberately not in a publish candidate.
          Intent/decision/reason/evidence/scope: typography hard-gate policy. */}
      <div
        style={{
          position: "absolute",
          left: "9%",
          top: "5%",
          width: "62%",
          display: "none",
          overflowWrap: "anywhere",
        }}
      >
        <div style={{fontSize: 28, opacity: 0.72}}>CONTRACT PREVIEW</div>
        <div style={{fontSize: 64, fontWeight: 700, marginTop: 10, opacity}}>
          {scene.sceneId}
        </div>
        <div style={{fontSize: 24, marginTop: 8, color: visualStyle.primary}}>
          {scene.visualRoute} · {scene.renderer}
        </div>
        <div style={{fontSize: 28, lineHeight: 1.35, marginTop: 12}}>
          {scene.evidenceRefs.length > 0
            ? scene.evidenceRefs.join(" · ")
            : "No evidence reference supplied"}
        </div>
        <div style={{fontSize: 22, marginTop: 10, opacity: 0.72}}>
          {sceneMediaRendered
            ? scene.mediaLayout
              ? "Explicit hash-bound scene media preview"
              : "Explicit hash-bound safe-layout media preview"
            : scene.missingMaterialReason
              ? `Missing material: ${scene.missingMaterialReason}`
              : "Source media intentionally not rendered"}
        </div>
      </div>
      {typography ? cues.map((cue) => (
        <Sequence
          key={cue.cueId}
          from={Math.max(0, cue.startFrame - scene.startFrame)}
          durationInFrames={cue.durationInFrames}
          layout="none"
        >
          <div
            style={{
              position: "absolute",
              left: `${cue.focus.x * 100}%`,
              top: `${cue.focus.y * 100}%`,
              width: `${cue.focus.width * 100}%`,
              height: `${cue.focus.height * 100}%`,
              border: `8px solid ${visualStyle.primary}`,
              borderRadius: 8,
            }}
          >
            <div
              style={{
                position: "absolute",
                left: 0,
                bottom: -58,
                backgroundColor: visualStyle.panel,
                color: visualStyle.foreground,
                // Bound policy minimum is a floor; future cue-specific values may grow.
                fontSize: Math.max(typography.roles.motion_label, 36),
                padding: "10px 14px",
              }}
            >
              {cue.label}
            </div>
          </div>
        </Sequence>
      )) : null}
    </AbsoluteFill>
  );
};

export const CandidateComposition: FC<{data: ProjectData}> = ({data}) => {
  const {fps} = useVideoConfig();
  return (
    <AbsoluteFill style={{backgroundColor: data.style.background}}>
      {data.scenes.map((scene) => (
        <Sequence
          key={scene.sceneId}
          from={scene.startFrame}
          durationInFrames={scene.durationInFrames}
        >
          <SceneCard
            scene={scene}
            cues={data.motionCues.filter((cue) => cue.sceneId === scene.sceneId)}
            safeLayout={scene.mediaLayout ?? data.safeLayout}
            sourceMediaRendered={data.sourceMediaRendered}
            visualStyle={data.style}
            typography={data.motionTypography}
          />
        </Sequence>
      ))}
      {data.subtitleFile && data.subtitleRegion && data.subtitleTypography ? (
        <Captions
          source={data.subtitleFile}
          fps={fps}
          frameHeight={data.height}
          region={data.subtitleRegion}
          foreground={data.subtitleStyle?.textColor ?? data.style.foreground}
          panel={data.subtitleStyle?.backgroundColor ?? data.style.panel}
          fontFamily={data.subtitleStyle?.fontFamily ?? data.style.bodyFont}
          minimumFontSize={data.subtitleTypography.roles.subtitle}
          subtitleStyle={data.subtitleStyle ?? null}
        />
      ) : null}
      <div
        style={{
          position: "absolute",
          top: 34,
          right: 34,
          color: data.style.foreground,
          backgroundColor: data.style.accent,
          fontFamily: data.style.displayFont,
          fontSize: 24,
          fontWeight: 700,
          padding: "10px 14px",
          // Internal diagnostic only: public candidate output must not carry unbound 24px text.
          display: "none",
        }}
      >
        CANDIDATE ONLY
      </div>
    </AbsoluteFill>
  );
};
"""

_COVER_TSX = """import {AbsoluteFill, Img, staticFile} from "remotion";
import type {FC} from "react";

type Rect = {x: number; y: number; width: number; height: number};
type VisualStyle = {
  background: string;
  foreground: string;
  primary: string;
  accent: string;
  muted: string;
  panel: string;
  displayFont: string;
  bodyFont: string;
};
type CoverVariant = {
  compositionId: string;
  candidateId: string;
  strategy: string;
  title: string;
  overlayText: string;
  visualFocus: string;
  audiencePromise: string;
  sourceKind: string;
  renderEligible: boolean;
  profileId: string;
  width: number;
  height: number;
  safeZone: Rect;
  candidateOnly: true;
  caseCover?: {
    account: string;
    palette?: Partial<Pick<VisualStyle, "background" | "foreground" | "primary" | "accent" | "muted" | "panel">>;
    compositionStrategy: string;
    compositionRecipeId?: string | null;
    comparisonBasis?: string;
    primaryReadingTarget: string;
    renderKind: string;
    graphicKind: string;
    tag: string;
    helperLabels: string[];
    // Intent: render only the exact policy binding projected by Python.
    // Decision/reason/evidence/scope: typography-gates.v1.json governance.
    typography: { hard_gate: true; roles: Record<string, number> };
    layout: {
      text_box: Rect;
      text_align: "left" | "center" | "right";
      graphic_box?: Rect;
      cover_layers?: {
        schema: string;
        background: string;
        layers: Array<{id: string; type: string; box: Rect; asset_id?: string; text?: string; role?: string; font_px?: number; color?: string; align?: "left" | "center" | "right"; stroke_px?: number}>;
      };
      photo_editorial?: {
        schema: string;
        shade?: "bottom" | "none";
        text_nodes: Array<{text: string; role: string; box: Rect; font_px: number; color: string; weight?: number; font_family?: string; shadow?: boolean; align?: "left" | "center" | "right"}>;
      };
      media: Array<{
        asset_id: string;
        public_path: string;
        box: Rect;
        fit_mode: "contain";
        crop_allowed: false;
        label?: string;
        amount?: string;
        recipe_role?: string;
      }>;
    };
  };
};

const rectStyle = (rect: Rect) => ({
  position: "absolute" as const,
  left: `${rect.x * 100}%`,
  top: `${rect.y * 100}%`,
  width: `${rect.width * 100}%`,
  height: `${rect.height * 100}%`,
});

const StructureGraphic: FC<{
  kind: string;
  labels: string[];
  box: Rect;
  visualStyle: VisualStyle;
  scale: number;
  typography: { hard_gate: true; roles: Record<string, number> };
}> = ({kind, labels, box, visualStyle, scale, typography}) => {
  // The policy supplies a floor.  Generous space may render larger type, never smaller.
  const type = (role: string, preferred: number) => Math.max(typography.roles[role], preferred);
  const frame = {
    ...rectStyle(box),
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 16 * scale,
  };
  const pill = {
    border: `${Math.max(2, 2 * scale)}px solid ${visualStyle.primary}`,
    backgroundColor: visualStyle.panel,
    color: visualStyle.foreground,
    padding: `${18 * scale}px ${22 * scale}px`,
    fontSize: type("structure", 64 * scale),
    fontWeight: 760,
    lineHeight: 1.15,
    textAlign: "center" as const,
    minWidth: 140 * scale,
  };
  if (kind === "seven_treatment_weeks") {
    return <div style={{...frame, flexDirection: "column"}}>
      <div style={{display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 9 * scale, width: "100%"}}>
        {Array.from({length: 7}).map((_, index) => <div key={index} style={{height: 96 * scale, border: `${2 * scale}px solid ${visualStyle.primary}`, backgroundColor: index === 0 || index === 6 ? visualStyle.panel : visualStyle.primary, color: index === 0 || index === 6 ? visualStyle.primary : visualStyle.panel, display: "grid", placeItems: "center", fontSize: type("structure", 64 * scale), fontWeight: 800}}>{index === 0 || index === 6 ? "" : "治"}</div>)}
      </div>
      <div style={{display: "flex", gap: 12 * scale, width: "100%", justifyContent: "center"}}>{labels.slice(0, 3).map((label) => <span key={label} style={pill}>{label}</span>)}</div>
    </div>;
  }
  if (kind === "one_to_four") {
    return <div style={frame}><div style={{...pill, backgroundColor: visualStyle.primary, color: visualStyle.panel, fontSize: type("number", 104 * scale)}}>1</div><div style={{color: visualStyle.accent, fontSize: type("number", 104 * scale)}}>→</div><div style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 * scale, flex: 1}}>{labels.slice(0, 4).map((label) => <span key={label} style={pill}>{label}</span>)}</div></div>;
  }
  if (kind === "two_opinions_choice") {
    return <div style={frame}><div style={{display: "grid", gap: 10 * scale}}>{labels.slice(0, 2).map((label) => <span key={label} style={pill}>{label}</span>)}</div><div style={{color: visualStyle.accent, fontSize: type("number", 104 * scale)}}>→</div><div style={{...pill, backgroundColor: visualStyle.primary, color: visualStyle.panel}}>{labels[2] || "患者决定"}</div></div>;
  }
  if (["boundary_split", "responsibility_boundary", "two_amount_relationship", "coverage_distance_tradeoff", "split_payment"].includes(kind)) {
    return <div style={{...frame, alignItems: "center"}}>{labels.slice(0, 2).map((label, index) => <div key={label} style={{...pill, flex: 1, minHeight: 150 * scale, display: "grid", placeItems: "center", borderTop: `${8 * scale}px solid ${index === 0 ? visualStyle.primary : visualStyle.accent}`}}>{label}</div>)}</div>;
  }
  if (["three_layer_filter", "three_factor_tradeoff"].includes(kind)) {
    return <div style={{...frame, alignItems: "center"}}>{labels.slice(0, 3).map((label, index) => <div key={label} style={{...pill, flex: 1, minHeight: 140 * scale, display: "grid", placeItems: "center", transform: `translateY(${index * 8 * scale}px)`}}>{label}</div>)}</div>;
  }
  const sequenceLabels = labels.length ? labels : ["事实", "核对", "决定"];
  return <div style={frame}>{sequenceLabels.slice(0, 5).map((label, index) => <div key={`${label}-${index}`} style={{display: "contents"}}><span style={{...pill, flex: 1}}>{label}</span>{index < Math.min(sequenceLabels.length, 5) - 1 ? <span style={{color: visualStyle.accent, fontSize: type("number", 104 * scale)}}>→</span> : null}</div>)}</div>;
};

export const CoverStill: FC<{
  variant: CoverVariant;
  visualStyle: VisualStyle;
}> = ({variant, visualStyle}) => {
  if (!variant.candidateOnly || !variant.renderEligible) {
    throw new Error(`Cover is not render eligible: ${variant.candidateId}`);
  }
  if (variant.caseCover) {
    const cover = variant.caseCover;
    const layout = cover.layout;
    const style = {...visualStyle, ...(cover.palette ?? {})};
    const scale = Math.min(variant.width / 1080, variant.height / 1080);
    const typography = cover.typography;
    if (!typography.hard_gate || !typography.roles.overlay) {
      throw new Error(`Typography gate is absent: ${variant.candidateId}`);
    }
    const minimum = (role: string) => typography.roles[role];
    const actual = (role: string, preferred: number) => Math.max(minimum(role), preferred);
    const length = Array.from(variant.overlayText).length;
    const overlayPreferred = (length <= 8 ? 172 : length <= 12 ? 156 : 144) * scale;
    const graphicBox = layout.graphic_box ?? {x: 0.08, y: 0.48, width: 0.84, height: 0.36};
    if (layout.cover_layers) {
      const scene = layout.cover_layers;
      return <AbsoluteFill style={{backgroundColor: scene.background, fontFamily: visualStyle.bodyFont}}>
        {scene.layers.map((layer) => {
          const position = rectStyle(layer.box);
          if (layer.type === "image") {
            const media = layout.media.find(m => m.asset_id === layer.asset_id);
            if (!media) throw new Error(`Unbound cover layer ${layer.id}`);
            return <Img key={layer.id} src={staticFile(media.public_path)} style={{...position, objectFit: "contain"}} />;
          }
          if (layer.type === "text") return <div key={layer.id} data-cover-layer={layer.id} style={{...position, fontSize: layer.font_px, fontWeight: layer.role === "overlay" ? 850 : 600, color: layer.color, lineHeight: 1.12, whiteSpace: "pre-wrap", textAlign: layer.align ?? "left"}}>{layer.text}</div>;
          if (layer.type === "line") return <svg key={layer.id} style={position} viewBox="0 0 100 100" preserveAspectRatio="none"><line x1="2" y1="2" x2="98" y2="98" stroke={layer.color} strokeWidth={layer.stroke_px ?? 4} vectorEffect="non-scaling-stroke" /></svg>;
          return <div key={layer.id} style={{...position, backgroundColor: layer.color, borderRadius: layer.type === "ellipse" ? "50%" : 0}} />;
        })}
      </AbsoluteFill>;
    }
    if (layout.photo_editorial) {
      const editorial = layout.photo_editorial;
      return <AbsoluteFill style={{backgroundColor: style.background, fontFamily: "Microsoft YaHei, sans-serif"}}>
        {layout.media.map((m, i) => <Img key={m.asset_id} src={staticFile(m.public_path)} style={{...rectStyle(m.box), objectFit: "contain", zIndex: i === 0 ? 0 : 2}} />)}
        {editorial.shade === "bottom" ? <AbsoluteFill style={{background: "linear-gradient(to bottom, transparent 62%, rgba(12,20,27,.10) 72%, rgba(12,20,27,.65) 100%)", zIndex: 1}} /> : null}
        {editorial.text_nodes.map((node, i) => <div key={i} data-cover-text-role={node.role} data-cover-text-box={i} style={{...rectStyle(node.box), fontSize: actual(node.role, node.font_px), fontWeight: node.weight ?? 800, color: node.color, fontFamily: node.font_family ?? "Microsoft YaHei, sans-serif", lineHeight: 1.12, whiteSpace: "pre-wrap", textAlign: node.align ?? "left", textShadow: node.shadow ? "0 3px 14px rgba(0,0,0,.8)" : "none", zIndex: 3}}>{node.text}</div>)}
      </AbsoluteFill>;
    }
    if (cover.compositionRecipeId) {
      const recipe = cover.compositionRecipeId;
      const known = ["minimal-whitespace", "person-side-whitespace", "partial-person-object-led", "product-ui-led", "comparison-cards", "editorial-collage", "evidence-focus"];
      if (!known.includes(recipe)) throw new Error(`Unknown cover recipe: ${recipe}`);
      const compare = recipe === "comparison-cards";
      const collage = recipe === "editorial-collage";
      const evidence = recipe === "evidence-focus";
      const ui = recipe === "product-ui-led";
      const minimal = recipe === "minimal-whitespace";
      return <AbsoluteFill data-cover-recipe={recipe} style={{backgroundColor: style.background, color: style.foreground, fontFamily: visualStyle.bodyFont}}>
        {layout.media.map((media, index) => <div key={media.asset_id} data-recipe-role={media.recipe_role} style={{...rectStyle(media.box), boxSizing: "border-box", padding: (ui || compare || evidence) ? 12 * scale : 0,
          backgroundColor: (compare || ui) ? style.panel : "transparent",
          border: evidence ? `${3 * scale}px solid ${style.primary}` : "none",
          borderRadius: ui ? 18 * scale : 0,
          boxShadow: collage ? `${8 * scale}px ${10 * scale}px 0 ${style.panel}` : "none"}}>
          <Img src={staticFile(media.public_path)} style={{width: "100%", height: "100%", objectFit: "contain"}} />
          {compare ? <div style={{position: "absolute", left: 0, bottom: "100%", fontSize: actual("media_label", 36 * scale), fontWeight: 750}}>{media.label}</div> : null}
          {collage ? <span style={{position: "absolute", left: 0, top: 0, color: style.primary, background: style.background, fontSize: actual("tag", 34 * scale)}}>{String(index + 1).padStart(2, "0")}</span> : null}
        </div>)}
        <div style={{...rectStyle(layout.text_box), display: "flex", flexDirection: "column", justifyContent: "center", textAlign: layout.text_align}}>
          {!minimal ? <div style={{color: style.primary, fontSize: actual("tag", 34 * scale), lineHeight: 1.15, marginBottom: 16 * scale}}>{compare ? cover.comparisonBasis : cover.tag}</div> : null}
          <div style={{fontFamily: visualStyle.displayFont, fontSize: actual("overlay", overlayPreferred), fontWeight: 900, lineHeight: 1.08, overflowWrap: "anywhere", letterSpacing: "-0.025em"}}>{variant.overlayText}</div>
        </div>
        {evidence ? <div style={{position: "absolute", left: "12%", top: "89%", width: "76%", fontSize: actual("media_label", 32 * scale), color: style.primary}}>{cover.helperLabels[0]}</div> : null}
        <div style={{position: "absolute", left: "7%", bottom: "2%", fontSize: actual("attribution", 22 * scale), color: style.muted}}>{cover.account}</div>
      </AbsoluteFill>;
    }
    return <AbsoluteFill style={{backgroundColor: style.background, color: style.foreground, fontFamily: visualStyle.bodyFont, overflow: "hidden"}}>
      <div style={{position: "absolute", width: 220 * scale, height: 220 * scale, right: -70 * scale, top: -80 * scale, borderRadius: "50%", backgroundColor: style.panel, border: `${2 * scale}px solid ${style.primary}`, opacity: 0.7}} />
      {layout.media.map((media) => <div key={media.asset_id} style={{...rectStyle(media.box), backgroundColor: style.panel, overflow: "hidden", border: `${2 * scale}px solid ${style.primary}`}}>
        <Img src={staticFile(media.public_path)} style={{width: "100%", height: "100%", objectFit: "contain"}} />
        {media.label || media.amount ? <div style={{position: "absolute", left: 16 * scale, right: 16 * scale, bottom: 14 * scale, backgroundColor: "rgba(247,248,245,0.94)", borderLeft: `${6 * scale}px solid ${style.accent}`, padding: `${10 * scale}px ${14 * scale}px`, display: "flex", justifyContent: "space-between", gap: 12 * scale, fontSize: actual("media_label", 36 * scale), fontWeight: 760}}><span>{media.label}</span><strong>{media.amount}</strong></div> : null}
      </div>)}
      {cover.renderKind === "structure" || cover.graphicKind !== "none" ? <StructureGraphic kind={cover.graphicKind} labels={cover.helperLabels} box={graphicBox} visualStyle={style} scale={scale} typography={typography} /> : null}
      <div style={{...rectStyle(layout.text_box), display: "flex", flexDirection: "column", justifyContent: "center", alignItems: layout.text_align === "center" ? "center" : layout.text_align === "right" ? "flex-end" : "flex-start", textAlign: layout.text_align, zIndex: 5}}>
        <div style={{borderLeft: `${6 * scale}px solid ${style.accent}`, paddingLeft: 14 * scale, color: style.primary, fontSize: actual("tag", 34 * scale), fontWeight: 760, lineHeight: 1.1}}>{cover.tag}</div>
        <div style={{marginTop: 22 * scale, fontFamily: visualStyle.displayFont, fontSize: actual("overlay", overlayPreferred), fontWeight: 900, lineHeight: 1.06, letterSpacing: "-0.035em", overflowWrap: "anywhere", textShadow: `0 ${2 * scale}px 0 ${style.background}`}}>{variant.overlayText}</div>
      </div>
      <div style={{position: "absolute", left: 0.05 * variant.width, bottom: 0.035 * variant.height, fontSize: actual("attribution", 22 * scale), fontWeight: 680, color: style.muted}}>{cover.account}</div>
      {cover.renderKind === "illustration" ? <div style={{position: "absolute", right: 0.04 * variant.width, bottom: 0.035 * variant.height, fontSize: actual("ai_disclosure", 20 * scale), color: style.primary}}>AI示意图</div> : null}
    </AbsoluteFill>;
  }
  const portrait = variant.height > variant.width;
  const titleSize = portrait ? 72 : 86;
  const overlaySize = portrait ? 104 : 126;
  return (
    <AbsoluteFill
      style={{
        backgroundColor: visualStyle.background,
        color: visualStyle.foreground,
        fontFamily: visualStyle.bodyFont,
      }}
    >
      <div
        style={{
          position: "absolute",
          left: `${variant.safeZone.x * 100}%`,
          top: `${variant.safeZone.y * 100}%`,
          width: `${variant.safeZone.width * 100}%`,
          height: `${variant.safeZone.height * 100}%`,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          boxSizing: "border-box",
          padding: portrait ? "7%" : "5%",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            color: visualStyle.primary,
            fontSize: titleSize,
            lineHeight: 1.2,
            overflowWrap: "anywhere",
          }}
        >
          {variant.title}
        </div>
        <div
          style={{
            color: visualStyle.foreground,
            fontFamily: visualStyle.displayFont,
            fontSize: overlaySize,
            fontWeight: 800,
            lineHeight: 1.08,
            marginTop: portrait ? 56 : 44,
            maxWidth: "92%",
            overflowWrap: "anywhere",
          }}
        >
          {variant.overlayText}
        </div>
        <div
          style={{
            color: visualStyle.muted,
            fontSize: portrait ? 34 : 38,
            lineHeight: 1.3,
            marginTop: portrait ? 40 : 30,
            maxWidth: "86%",
          }}
        >
          {variant.audiencePromise}
        </div>
      </div>
      <div
        style={{
          position: "absolute",
          right: "3%",
          bottom: "3%",
          color: visualStyle.foreground,
          backgroundColor: visualStyle.accent,
          fontSize: portrait ? 26 : 30,
          fontWeight: 700,
          padding: "12px 16px",
        }}
      >
        CANDIDATE ONLY
      </div>
    </AbsoluteFill>
  );
};
"""

_CAPTIONS_TSX = """import {parseSrt} from "@remotion/captions";
import type {Caption} from "@remotion/captions";
import {useCallback, useEffect, useState} from "react";
import type {FC} from "react";
import {Sequence, staticFile, useDelayRender} from "remotion";

type Rect = {x: number; y: number; width: number; height: number};
type SubtitleStyle = {
  presetId: string;
  fontId: string;
  fontFamily: string;
  fontWeight: number;
  fontPublicPath: string;
  fontSha256: string;
  textColor: string;
  backgroundColor: string;
  outlineColor: string;
  outlinePx: number;
  shadowCss: string;
  paddingY: number;
  paddingX: number;
  borderRadius: number;
  fontPx: number;
};

export const Captions: FC<{
  source: string;
  fps: number;
  frameHeight: number;
  region: Rect;
  foreground: string;
  panel: string;
  fontFamily: string;
  minimumFontSize: number;
  subtitleStyle?: SubtitleStyle | null;
}> = ({source, fps, frameHeight, region, foreground, panel, fontFamily, minimumFontSize, subtitleStyle}) => {
  const [captions, setCaptions] = useState<Caption[] | null>(null);
  const {delayRender, continueRender, cancelRender} = useDelayRender();
  const [handle] = useState(() => delayRender("Loading reviewed SRT and managed font"));
  const load = useCallback(async () => {
    try {
      const response = await fetch(staticFile(source));
      if (!response.ok) throw new Error(`Unable to load captions: ${response.status}`);
      const srtText = await response.text();
      if (subtitleStyle?.fontPublicPath) {
        const resolvedFamily = subtitleStyle.fontFamily || fontFamily;
        const face = new FontFace(
          resolvedFamily,
          `url(${staticFile(subtitleStyle.fontPublicPath)})`,
          {weight: String(subtitleStyle.fontWeight || 700), style: "normal"}
        );
        await face.load();
        document.fonts.add(face);
        await document.fonts.ready;
        console.log(
          "[subtitle-font-loaded]",
          JSON.stringify({
            family: resolvedFamily,
            weight: subtitleStyle.fontWeight,
            status: face.status,
            publicPath: subtitleStyle.fontPublicPath,
            sha256: subtitleStyle.fontSha256,
          })
        );
      }
      const parsed = parseSrt({input: srtText});
      setCaptions(parsed.captions);
      continueRender(handle);
    } catch (error) {
      cancelRender(error instanceof Error ? error : new Error(String(error)));
    }
  }, [cancelRender, continueRender, fontFamily, handle, source, subtitleStyle]);
  useEffect(() => { void load(); }, [load]);
  if (!captions) return null;
  const resolvedFamily = subtitleStyle?.fontFamily || fontFamily;
  const resolvedWeight = subtitleStyle?.fontWeight ?? 700;
  const textColor = subtitleStyle?.textColor ?? foreground;
  const boxBg = subtitleStyle?.backgroundColor ?? panel;
  const outlinePx = subtitleStyle?.outlinePx ?? 0;
  const outlineColor = subtitleStyle?.outlineColor ?? "#243238";
  const shadowCss = subtitleStyle?.shadowCss ?? "none";
  const padY = subtitleStyle?.paddingY ?? 8;
  const padX = subtitleStyle?.paddingX ?? 18;
  const borderRadius = subtitleStyle?.borderRadius ?? 8;
  return (
    <>
      {captions.map((caption, index) => {
        const from = Math.max(0, Math.round((caption.startMs / 1000) * fps));
        const duration = Math.max(1, Math.round(((caption.endMs - caption.startMs) / 1000) * fps));
        const regionFontSize = Math.floor(frameHeight * region.height * 0.32);
        const lengthPenalty = Math.max(0, caption.text.length - 22) * 0.45;
        // The bound minimum is a floor, not a fixed size or an upper limit.
        const baseFontSize = Math.max(minimumFontSize, regionFontSize - lengthPenalty);
        const fontSize = subtitleStyle ? Math.max(baseFontSize, subtitleStyle.fontPx) : baseFontSize;
        return (
          <Sequence key={`${caption.startMs}-${index}`} from={from} durationInFrames={duration}>
            <div
              style={{
                position: "absolute",
                left: `${region.x * 100}%`,
                bottom: `${(1 - region.y - region.height) * 100}%`,
                width: `${region.width * 100}%`,
                minHeight: `${region.height * 100}%`,
                backgroundColor: "transparent",
                boxSizing: "border-box",
                display: "flex",
                alignItems: "flex-end",
                justifyContent: "center",
                pointerEvents: "none",
              }}
            >
              <div
                style={{
                  display: "inline-block",
                  maxWidth: "100%",
                  color: textColor,
                  backgroundColor: boxBg,
                  borderRadius,
                  boxSizing: "border-box",
                  fontFamily: `"${resolvedFamily}", sans-serif`,
                  fontWeight: resolvedWeight,
                  fontSize,
                  lineHeight: 1.32,
                  overflowWrap: "anywhere",
                  padding: `${padY}px ${padX}px`,
                  textAlign: "center",
                  whiteSpace: "pre-wrap",
                  ...(outlinePx > 0
                    ? {
                        WebkitTextStroke: `${outlinePx * 2}px ${outlineColor}`,
                        paintOrder: "stroke fill",
                        textShadow: shadowCss,
                      }
                    : {}),
                }}
              >
                {caption.text}
              </div>
            </div>
          </Sequence>
        );
      })}
    </>
  );
};
"""
