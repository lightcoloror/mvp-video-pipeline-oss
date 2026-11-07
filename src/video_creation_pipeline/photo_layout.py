from __future__ import annotations

import hashlib
import json
import math
import shutil
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
from .execution_receipts import probe_media_metadata
from .privacy_gate import detect_face_boxes

PHOTO_MEDIA_PREFLIGHT_SCHEMA = "video_creation_pipeline.photo_media_preflight.v1"
SAFE_MEDIA_LAYOUT_PLAN_SCHEMA = "video_creation_pipeline.safe_media_layout_plan.v1"
JUSTIFIED_LAYOUT_VERSION = "4.1.0"
JUSTIFIED_LAYOUT_SOURCE_COMMIT = "9d4db49cdbf45604c63a8b25293650af4fe952ab"

IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
HEIC_EXTENSIONS = {".heic", ".heif"}
VIDEO_EXTENSIONS = {".avi", ".mkv", ".mov", ".mp4", ".m4v", ".webm"}

_LAYOUT_ENGINE_RUNNER = """
const fs = require('node:fs');
const entry = process.argv.at(-1);
const layout = require(entry);
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
  try {
    const input = JSON.parse(raw);
    const result = layout(input.items, input.config);
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    process.stderr.write(String(error && error.stack ? error.stack : error));
    process.exitCode = 2;
  }
});
"""


def photo_layout_doctor(
    engine_entry_path: str | Path | None = None,
) -> dict[str, Any]:
    node = shutil.which("node")
    try:
        import cv2

        opencv: dict[str, Any] = {"status": "ready", "version": str(cv2.__version__)}
    except ImportError as exc:
        opencv = {"status": "missing", "error": str(exc)}

    engine: dict[str, Any] = {"status": "not_supplied"}
    if engine_entry_path is not None:
        try:
            engine = _engine_metadata(Path(engine_entry_path))
            engine["status"] = "ready" if node else "blocked_missing_node"
        except ContractError as exc:
            engine = {"status": "blocked", "error": str(exc)}

    return {
        "schema": "video_creation_pipeline.photo_layout_doctor.v1",
        "status": "ready" if node and opencv["status"] == "ready" else "blocked",
        "node": {"status": "ready" if node else "missing", "path": node},
        "opencv": opencv,
        "layout_engine": engine,
        "network_execution_performed": False,
        "source_media_modified": False,
        "automatic_render": False,
    }


def build_photo_media_preflight(
    source_dir: str | Path,
    *,
    recursive: bool = True,
    exclude_tokens: list[str] | None = None,
) -> dict[str, Any]:
    root = Path(source_dir).expanduser().resolve()
    if not root.is_dir():
        raise ContractError(f"photo media source directory not found: {root}")
    tokens = _normalized_tokens(exclude_tokens)
    paths = root.rglob("*") if recursive else root.glob("*")
    assets: list[dict[str, Any]] = []
    excluded_count = 0
    unsupported_count = 0
    for path in sorted(
        (item for item in paths if item.is_file()), key=lambda item: str(item)
    ):
        relative = path.relative_to(root).as_posix()
        if any(token in relative.casefold() for token in tokens):
            excluded_count += 1
            continue
        suffix = path.suffix.casefold()
        if suffix in IMAGE_EXTENSIONS:
            assets.append(_inspect_image(path, relative))
        elif suffix in HEIC_EXTENSIONS:
            assets.append(_blocked_heic_asset(path, relative))
        elif suffix in VIDEO_EXTENSIONS:
            assets.append(_inspect_video(path, relative))
        else:
            unsupported_count += 1

    if not assets:
        raise ContractError("photo media preflight found no supported media files")
    payload: dict[str, Any] = {
        "schema": PHOTO_MEDIA_PREFLIGHT_SCHEMA,
        "source_root": str(root),
        "scan_policy": {
            "recursive": bool(recursive),
            "exclude_tokens": tokens,
            "supported_image_extensions": sorted(IMAGE_EXTENSIONS),
            "supported_heic_extensions": sorted(HEIC_EXTENSIONS),
            "supported_video_extensions": sorted(VIDEO_EXTENSIONS),
            "exclude_by_default": False,
        },
        "assets": assets,
        "summary": {
            "asset_count": len(assets),
            "ready_count": sum(row["preflight_status"] == "ready" for row in assets),
            "blocked_count": sum(row["preflight_status"] != "ready" for row in assets),
            "excluded_count": excluded_count,
            "unsupported_count": unsupported_count,
        },
        "authority_boundary": {
            "source_media_modified": False,
            "derivative_media_created": False,
            "face_identity_recognition_performed": False,
            "face_detection_is_candidate_only": True,
            "network_execution_performed": False,
            "automatic_render": False,
            "automatic_publish": False,
        },
    }
    payload["preflight_sha256"] = payload_sha256(payload, "preflight_sha256")
    validate_photo_media_preflight(payload)
    return payload


def build_safe_media_layout_plan(
    preflight_path: str | Path,
    engine_entry_path: str | Path,
    *,
    asset_ids: list[str],
    width: int,
    height: int,
    padding_px: int = 36,
    gap_px: int = 18,
    target_row_height_px: int | None = None,
    target_row_height_tolerance: float = 0.15,
) -> dict[str, Any]:
    preflight_source = Path(preflight_path).expanduser().resolve()
    preflight = read_json_object(preflight_source, "photo media preflight")
    validate_photo_media_preflight(preflight)
    selected = _selected_assets(preflight, asset_ids)
    frame_width = _positive_int(width, "output width")
    frame_height = _positive_int(height, "output height")
    padding = _nonnegative_int(padding_px, "padding_px")
    gap = _nonnegative_int(gap_px, "gap_px")
    if padding * 2 >= frame_width or padding * 2 >= frame_height:
        raise ContractError("layout padding leaves no usable frame area")
    if not 0 < float(target_row_height_tolerance) <= 1:
        raise ContractError("target row height tolerance must be within (0, 1]")
    row_height = _positive_int(
        target_row_height_px
        if target_row_height_px is not None
        else max(1, int((frame_height - padding * 2) * 0.55)),
        "target_row_height_px",
    )
    entry = Path(engine_entry_path).expanduser().resolve()
    engine = _engine_metadata(entry)
    raw_geometry = _run_justified_layout(
        entry,
        [
            {"width": row["dimensions"]["width"], "height": row["dimensions"]["height"]}
            for row in selected
        ],
        {
            "containerWidth": frame_width,
            "containerPadding": padding,
            "boxSpacing": gap,
            "targetRowHeight": row_height,
            "targetRowHeightTolerance": float(target_row_height_tolerance),
            "showWidows": True,
            "fullWidthBreakoutRowCadence": False,
            "forceAspectRatio": False,
            "widowLayoutStyle": "center",
        },
    )
    placements, scale, offset_y = _safe_placements(
        raw_geometry,
        selected,
        frame_width,
        frame_height,
    )
    payload: dict[str, Any] = {
        "schema": SAFE_MEDIA_LAYOUT_PLAN_SCHEMA,
        "source_preflight": artifact_reference(
            preflight_source, "photo media preflight"
        ),
        "layout_engine": engine,
        "output": {
            "width": frame_width,
            "height": frame_height,
            "fit_mode": "contain",
            "crop_allowed": False,
            "background_policy": "neutral_unfilled_region",
        },
        "layout_config": {
            "padding_px": padding,
            "gap_px": gap,
            "target_row_height_px": row_height,
            "target_row_height_tolerance": float(target_row_height_tolerance),
            "full_width_breakout_row_cadence": False,
            "force_aspect_ratio": False,
            "widow_layout_style": "center",
            "geometry_scale": scale,
            "vertical_offset_px": offset_y,
        },
        "placements": placements,
        "preview_requirements": _preview_requirements(selected),
        "renderer_handoff": {
            "allowed_generators": ["hyperframes", "remotion"],
            "requires_existing_single_ffmpeg_outlet": True,
            "renderer_execution_performed": False,
            "candidate_only": True,
            "automatic_publish": False,
        },
        "authority_boundary": {
            "source_media_modified": False,
            "face_boxes_are_candidate_only": True,
            "face_identity_recognition_performed": False,
            "automatic_crop": False,
            "automatic_render": False,
            "network_execution_performed": False,
            "automatic_publish": False,
        },
    }
    payload["layout_plan_sha256"] = payload_sha256(payload, "layout_plan_sha256")
    validate_safe_media_layout_plan(payload)
    return payload


def validate_photo_media_preflight(payload: dict[str, Any]) -> None:
    if payload.get("schema") != PHOTO_MEDIA_PREFLIGHT_SCHEMA:
        raise ContractError("unsupported photo media preflight schema")
    if payload.get("preflight_sha256") != payload_sha256(payload, "preflight_sha256"):
        raise ContractError("photo media preflight integrity check failed")
    if not Path(str(payload.get("source_root") or "")).is_dir():
        raise ContractError("photo media preflight source root is unavailable")
    policy = _object(payload.get("scan_policy"))
    if (
        not isinstance(policy.get("recursive"), bool)
        or policy.get("exclude_by_default") is not False
    ):
        raise ContractError("photo media preflight scan policy is invalid")
    _normalized_tokens(policy.get("exclude_tokens"))
    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ContractError("photo media preflight requires assets")
    seen_ids: set[str] = set()
    for row in assets:
        _validate_preflight_asset(_object(row), seen_ids)
    summary = _object(payload.get("summary"))
    expected_summary = {
        "asset_count": len(assets),
        "ready_count": sum(row["preflight_status"] == "ready" for row in assets),
        "blocked_count": sum(row["preflight_status"] != "ready" for row in assets),
    }
    for key, expected in expected_summary.items():
        if summary.get(key) != expected:
            raise ContractError(f"photo media preflight summary drifted: {key}")
    boundary = _object(payload.get("authority_boundary"))
    expected_boundary = {
        "source_media_modified": False,
        "derivative_media_created": False,
        "face_identity_recognition_performed": False,
        "face_detection_is_candidate_only": True,
        "network_execution_performed": False,
        "automatic_render": False,
        "automatic_publish": False,
    }
    if boundary != expected_boundary:
        raise ContractError("photo media preflight authority boundary is invalid")


def validate_safe_media_layout_plan(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SAFE_MEDIA_LAYOUT_PLAN_SCHEMA:
        raise ContractError("unsupported safe media layout plan schema")
    if payload.get("layout_plan_sha256") != payload_sha256(
        payload, "layout_plan_sha256"
    ):
        raise ContractError("safe media layout plan integrity check failed")
    preflight_path = validate_artifact_reference(
        _object(payload.get("source_preflight")), "photo media preflight"
    )
    preflight = read_json_object(preflight_path, "photo media preflight")
    validate_photo_media_preflight(preflight)
    output = _object(payload.get("output"))
    width = _positive_int(output.get("width"), "layout output width")
    height = _positive_int(output.get("height"), "layout output height")
    if (
        output.get("fit_mode") != "contain"
        or output.get("crop_allowed") is not False
        or output.get("background_policy") != "neutral_unfilled_region"
    ):
        raise ContractError("safe media layout output policy is invalid")
    engine = _object(payload.get("layout_engine"))
    _validate_engine(engine)
    placements = payload.get("placements")
    if not isinstance(placements, list) or not placements or len(placements) > 5:
        raise ContractError("safe media layout requires one to five placements")
    source_by_id = {row["asset_id"]: row for row in preflight["assets"]}
    seen_ids: set[str] = set()
    for placement in placements:
        row = _object(placement)
        asset_id = str(row.get("asset_id") or "")
        if asset_id in seen_ids or asset_id not in source_by_id:
            raise ContractError("safe media layout placement asset is invalid")
        seen_ids.add(asset_id)
        source = source_by_id[asset_id]
        if source.get("preflight_status") != "ready":
            raise ContractError("safe media layout cannot use blocked media")
        if row.get("source_asset") != source.get("source_asset"):
            raise ContractError("safe media layout source asset drifted")
        if row.get("fit_mode") != "contain" or row.get("crop_allowed") is not False:
            raise ContractError("safe media layout placement crop policy is invalid")
        bounds = _object(row.get("bounds_px"))
        _validate_bounds(bounds, width, height, "safe media layout placement")
        if row.get("protected_regions") != source.get("face_boxes_normalized"):
            raise ContractError("safe media layout protected regions drifted")
    _validate_no_overlap([_object(row.get("bounds_px")) for row in placements])
    preview = _object(payload.get("preview_requirements"))
    if (
        preview.get("render_representative_sample_before_full") is not True
        or preview.get("transition_frames_per_cut") != 3
        or preview.get("full_render_allowed_before_sample") is not False
    ):
        raise ContractError("safe media layout preview requirements are invalid")
    handoff = _object(payload.get("renderer_handoff"))
    if (
        handoff.get("requires_existing_single_ffmpeg_outlet") is not True
        or handoff.get("renderer_execution_performed") is not False
        or handoff.get("candidate_only") is not True
        or handoff.get("automatic_publish") is not False
    ):
        raise ContractError("safe media layout renderer handoff is invalid")
    boundary = _object(payload.get("authority_boundary"))
    expected_boundary = {
        "source_media_modified": False,
        "face_boxes_are_candidate_only": True,
        "face_identity_recognition_performed": False,
        "automatic_crop": False,
        "automatic_render": False,
        "network_execution_performed": False,
        "automatic_publish": False,
    }
    if boundary != expected_boundary:
        raise ContractError("safe media layout authority boundary is invalid")


def write_photo_layout_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    schema = payload.get("schema")
    if schema == PHOTO_MEDIA_PREFLIGHT_SCHEMA:
        validate_photo_media_preflight(payload)
    elif schema == SAFE_MEDIA_LAYOUT_PLAN_SCHEMA:
        validate_safe_media_layout_plan(payload)
    else:
        raise ContractError("unsupported photo layout contract schema")
    return atomic_write_json(payload, output_path)


def _inspect_image(path: Path, relative: str) -> dict[str, Any]:
    asset = _base_asset(path, relative, "image")
    try:
        import cv2
    except ImportError as exc:
        asset.update(
            {
                "preflight_status": "blocked_missing_opencv",
                "error": str(exc),
                "dimensions": None,
                "face_boxes_normalized": [],
            }
        )
        return asset
    image = cv2.imread(str(path))
    if image is None:
        asset.update(
            {
                "preflight_status": "blocked_decode_failed",
                "error": "OpenCV could not decode the image",
                "dimensions": None,
                "face_boxes_normalized": [],
            }
        )
        return asset
    height, width = image.shape[:2]
    boxes = detect_face_boxes(cv2, image)
    asset.update(
        {
            "preflight_status": "ready",
            "dimensions": _dimensions(width, height),
            "face_boxes_normalized": _normalize_face_boxes(boxes, width, height),
            "metadata_probe": {"provider": "opencv", "version": str(cv2.__version__)},
        }
    )
    return asset


def _blocked_heic_asset(path: Path, relative: str) -> dict[str, Any]:
    asset = _base_asset(path, relative, "image")
    asset.update(
        {
            "preflight_status": "blocked_requires_derivative",
            "error": "HEIC/HEIF requires an explicitly approved local derivative",
            "dimensions": None,
            "face_boxes_normalized": [],
            "metadata_probe": {"provider": "none", "reason": "heic_not_auto_converted"},
        }
    )
    return asset


def _inspect_video(path: Path, relative: str) -> dict[str, Any]:
    asset = _base_asset(path, relative, "video")
    try:
        probe = probe_media_metadata(path)
        stream = next(
            (
                row
                for row in probe.get("streams", [])
                if row.get("codec_type") == "video"
            ),
            None,
        )
        if not isinstance(stream, dict):
            raise ContractError("ffprobe found no video stream")
        width = _positive_int(stream.get("width"), "video width")
        height = _positive_int(stream.get("height"), "video height")
        duration = _probe_duration(probe, stream)
        asset.update(
            {
                "preflight_status": "ready",
                "dimensions": _dimensions(width, height),
                "duration_s": duration,
                "face_boxes_normalized": [],
                "metadata_probe": {"provider": "ffprobe_json"},
            }
        )
    except ContractError as exc:
        asset.update(
            {
                "preflight_status": "blocked_metadata_probe_failed",
                "error": str(exc),
                "dimensions": None,
                "face_boxes_normalized": [],
            }
        )
    return asset


def _base_asset(path: Path, relative: str, media_type: str) -> dict[str, Any]:
    relative_sha = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    return {
        "asset_id": f"media-{relative_sha}",
        "relative_path": relative,
        "media_type": media_type,
        "source_asset": artifact_reference(path, "photo media source asset"),
        "preflight_status": "blocked_uninspected",
        "dimensions": None,
        "face_boxes_normalized": [],
    }


def _dimensions(width: int, height: int) -> dict[str, Any]:
    return {
        "width": int(width),
        "height": int(height),
        "aspect_ratio": round(width / height, 8),
        "orientation": "landscape"
        if width > height
        else "portrait"
        if height > width
        else "square",
    }


def _normalize_face_boxes(
    boxes: list[dict[str, int]], width: int, height: int
) -> list[dict[str, float]]:
    normalized = []
    for box in boxes:
        x = max(0, int(box["x"]))
        y = max(0, int(box["y"]))
        box_width = min(int(box["width"]), width - x)
        box_height = min(int(box["height"]), height - y)
        if box_width <= 0 or box_height <= 0:
            continue
        normalized.append(
            {
                "x": round(x / width, 8),
                "y": round(y / height, 8),
                "width": round(box_width / width, 8),
                "height": round(box_height / height, 8),
            }
        )
    return normalized


def _selected_assets(
    payload: dict[str, Any], asset_ids: list[str]
) -> list[dict[str, Any]]:
    selected_ids = [str(value).strip() for value in asset_ids]
    if not 1 <= len(selected_ids) <= 5 or len(set(selected_ids)) != len(selected_ids):
        raise ContractError("safe media layout requires one to five unique asset IDs")
    assets = {row["asset_id"]: row for row in payload["assets"]}
    selected = []
    for asset_id in selected_ids:
        asset = assets.get(asset_id)
        if asset is None:
            raise ContractError(f"safe media layout asset is unavailable: {asset_id}")
        if asset.get("preflight_status") != "ready":
            raise ContractError(f"safe media layout asset is not ready: {asset_id}")
        if not isinstance(asset.get("dimensions"), dict):
            raise ContractError(
                f"safe media layout asset has no dimensions: {asset_id}"
            )
        selected.append(asset)
    return selected


def _engine_metadata(entry: Path) -> dict[str, Any]:
    if not entry.is_file():
        raise ContractError(f"justified-layout entry file not found: {entry}")
    package_path = entry.parent.parent / "package.json"
    license_path = entry.parent.parent / "LICENSE"
    package = read_json_object(package_path, "justified-layout package metadata")
    if (
        package.get("name") != "justified-layout"
        or package.get("version") != JUSTIFIED_LAYOUT_VERSION
    ):
        raise ContractError("justified-layout entry is not the reviewed 4.1.0 runtime")
    if not license_path.is_file():
        raise ContractError("justified-layout LICENSE file is required")
    if _git_head(entry.parent.parent) != JUSTIFIED_LAYOUT_SOURCE_COMMIT:
        raise ContractError(
            "justified-layout source commit drifted from the reviewed source"
        )
    return {
        "name": "justified-layout",
        "version": JUSTIFIED_LAYOUT_VERSION,
        "reviewed_source_commit": JUSTIFIED_LAYOUT_SOURCE_COMMIT,
        "entry": artifact_reference(entry, "justified-layout entry"),
        "package_metadata": artifact_reference(
            package_path, "justified-layout package metadata"
        ),
        "license_file": artifact_reference(license_path, "justified-layout LICENSE"),
        "license_observation": {
            "license_file": "MIT",
            "package_metadata": str(package.get("license") or ""),
            "metadata_discrepancy_recorded": True,
        },
    }


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("justified-layout source commit could not be read") from exc
    if result.returncode != 0:
        raise ContractError("justified-layout source commit could not be read")
    return result.stdout.strip().lower()


def _run_justified_layout(
    entry: Path,
    items: list[dict[str, int]],
    config: dict[str, Any],
) -> dict[str, Any]:
    node = shutil.which("node")
    if not node:
        raise ContractError("node executable was not found for justified-layout")
    try:
        result = subprocess.run(
            [node, "-e", _LAYOUT_ENGINE_RUNNER, str(entry)],
            input=json.dumps({"items": items, "config": config}),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"justified-layout execution failed: {exc}") from exc
    if result.returncode != 0:
        raise ContractError(
            f"justified-layout execution failed: {(result.stderr or result.stdout).strip()[-2000:]}"
        )
    try:
        geometry = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("justified-layout returned invalid JSON") from exc
    if not isinstance(geometry, dict):
        raise ContractError("justified-layout returned an invalid geometry document")
    return geometry


def _safe_placements(
    geometry: dict[str, Any],
    selected: list[dict[str, Any]],
    frame_width: int,
    frame_height: int,
) -> tuple[list[dict[str, Any]], float, float]:
    raw_height = _positive_number(
        geometry.get("containerHeight"), "layout container height"
    )
    boxes = geometry.get("boxes")
    if not isinstance(boxes, list) or len(boxes) != len(selected):
        raise ContractError("justified-layout returned an unexpected number of boxes")
    scale = min(1.0, frame_height / raw_height)
    offset_x = (frame_width - frame_width * scale) / 2
    offset_y = (frame_height - raw_height * scale) / 2
    placements = []
    for asset, raw in zip(selected, boxes, strict=True):
        box = _object(raw)
        left = _nonnegative_number(box.get("left"), "layout box left")
        top = _nonnegative_number(box.get("top"), "layout box top")
        width = _positive_number(box.get("width"), "layout box width")
        height = _positive_number(box.get("height"), "layout box height")
        bounds = {
            "x": round(offset_x + left * scale, 6),
            "y": round(offset_y + top * scale, 6),
            "width": round(width * scale, 6),
            "height": round(height * scale, 6),
        }
        _validate_bounds(bounds, frame_width, frame_height, "justified-layout box")
        placements.append(
            {
                "asset_id": asset["asset_id"],
                "media_type": asset["media_type"],
                "source_asset": dict(asset["source_asset"]),
                "source_dimensions": dict(asset["dimensions"]),
                "bounds_px": bounds,
                "fit_mode": "contain",
                "crop_allowed": False,
                "protected_regions": list(asset["face_boxes_normalized"]),
            }
        )
    return placements, round(scale, 8), round(offset_y, 6)


def _preview_requirements(selected: list[dict[str, Any]]) -> dict[str, Any]:
    cases = [{"kind": "first_selected_asset", "asset_ids": [selected[0]["asset_id"]]}]
    orientations = {row["dimensions"]["orientation"] for row in selected}
    if len(orientations) > 1:
        cases.append(
            {
                "kind": "mixed_orientation_page",
                "asset_ids": [row["asset_id"] for row in selected],
            }
        )
    face_assets = [row["asset_id"] for row in selected if row["face_boxes_normalized"]]
    if face_assets:
        cases.append({"kind": "face_protected_asset", "asset_ids": face_assets})
    video_assets = [row["asset_id"] for row in selected if row["media_type"] == "video"]
    if video_assets:
        cases.append({"kind": "video_page", "asset_ids": video_assets})
    return {
        "render_representative_sample_before_full": True,
        "full_render_allowed_before_sample": False,
        "minimum_sample_duration_s": 10.0,
        "required_cases": cases,
        "transition_frames_per_cut": 3,
        "transition_offsets_s": [-0.3, 0.0, 0.3],
        "checks": [
            "asset_load_error",
            "face_or_edge_crop",
            "black_flash",
            "unintended_blank_frame",
            "audio_policy_match",
        ],
        "requires_existing_render_and_technical_qa": True,
    }


def _validate_preflight_asset(asset: dict[str, Any], seen_ids: set[str]) -> None:
    asset_id = str(asset.get("asset_id") or "")
    if not asset_id or asset_id in seen_ids:
        raise ContractError("photo media preflight asset IDs must be unique")
    seen_ids.add(asset_id)
    if asset.get("media_type") not in {"image", "video"}:
        raise ContractError("photo media preflight media type is invalid")
    validate_artifact_reference(
        _object(asset.get("source_asset")), "photo media source asset"
    )
    if asset.get("preflight_status") not in {
        "ready",
        "blocked_missing_opencv",
        "blocked_decode_failed",
        "blocked_requires_derivative",
        "blocked_metadata_probe_failed",
    }:
        raise ContractError("photo media preflight status is invalid")
    dimensions = asset.get("dimensions")
    if asset.get("preflight_status") == "ready":
        dimension = _object(dimensions)
        width = _positive_int(dimension.get("width"), "photo media width")
        height = _positive_int(dimension.get("height"), "photo media height")
        expected_ratio = round(width / height, 8)
        if dimension.get("aspect_ratio") != expected_ratio:
            raise ContractError("photo media aspect ratio drifted")
        if dimension.get("orientation") not in {"landscape", "portrait", "square"}:
            raise ContractError("photo media orientation is invalid")
    elif dimensions is not None:
        raise ContractError("blocked photo media cannot claim dimensions")
    faces = asset.get("face_boxes_normalized")
    if not isinstance(faces, list):
        raise ContractError("photo media face candidates must be a list")
    for face in faces:
        _validate_normalized_bounds(_object(face), "photo media face candidate")


def _validate_engine(engine: dict[str, Any]) -> None:
    if (
        engine.get("name") != "justified-layout"
        or engine.get("version") != JUSTIFIED_LAYOUT_VERSION
        or engine.get("reviewed_source_commit") != JUSTIFIED_LAYOUT_SOURCE_COMMIT
    ):
        raise ContractError("safe media layout engine is not the reviewed runtime")
    entry = validate_artifact_reference(
        _object(engine.get("entry")), "justified-layout entry"
    )
    validate_artifact_reference(
        _object(engine.get("package_metadata")), "justified-layout package metadata"
    )
    validate_artifact_reference(
        _object(engine.get("license_file")), "justified-layout LICENSE"
    )
    observation = _object(engine.get("license_observation"))
    if (
        observation.get("license_file") != "MIT"
        or observation.get("package_metadata") != "ISC"
        or observation.get("metadata_discrepancy_recorded") is not True
    ):
        raise ContractError("safe media layout engine license observation is invalid")
    if _git_head(entry.parent.parent) != JUSTIFIED_LAYOUT_SOURCE_COMMIT:
        raise ContractError(
            "justified-layout source commit drifted from the reviewed source"
        )


def _validate_bounds(
    bounds: dict[str, Any], width: int, height: int, label: str
) -> None:
    x = _nonnegative_number(bounds.get("x"), f"{label} x")
    y = _nonnegative_number(bounds.get("y"), f"{label} y")
    item_width = _positive_number(bounds.get("width"), f"{label} width")
    item_height = _positive_number(bounds.get("height"), f"{label} height")
    tolerance = 0.01
    if x + item_width > width + tolerance or y + item_height > height + tolerance:
        raise ContractError(f"{label} escapes the output frame")


def _validate_no_overlap(bounds_list: list[dict[str, Any]]) -> None:
    for index, first in enumerate(bounds_list):
        for second in bounds_list[index + 1 :]:
            horizontal = min(
                first["x"] + first["width"], second["x"] + second["width"]
            ) - max(first["x"], second["x"])
            vertical = min(
                first["y"] + first["height"], second["y"] + second["height"]
            ) - max(first["y"], second["y"])
            if horizontal > 0.01 and vertical > 0.01:
                raise ContractError("safe media layout placements overlap")


def _validate_normalized_bounds(bounds: dict[str, Any], label: str) -> None:
    _validate_bounds(bounds, 1, 1, label)


def _probe_duration(probe: dict[str, Any], stream: dict[str, Any]) -> float | None:
    values = [stream.get("duration"), _object(probe.get("format")).get("duration")]
    for value in values:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration >= 0:
            return round(duration, 6)
    return None


def _normalized_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractError("exclude tokens must be a list")
    tokens = [str(item).strip().casefold() for item in value]
    if any(not item for item in tokens) or len(set(tokens)) != len(tokens):
        raise ContractError("exclude tokens must be unique non-empty strings")
    return tokens


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if number <= 0 or number != value:
        raise ContractError(f"{label} must be a positive integer")
    return number


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative integer") from exc
    if number < 0 or number != value:
        raise ContractError(f"{label} must be a non-negative integer")
    return number


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ContractError(f"{label} must be a positive number")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"{label} must be a non-negative number")
    return number
