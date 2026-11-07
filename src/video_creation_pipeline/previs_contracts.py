from __future__ import annotations

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
from .generation_contracts import (
    SHOT_GENERATION_UNIT_SCHEMA,
    validate_shot_generation_unit,
)

PREVIS_SCENE_SCHEMA = "video_creation_pipeline.previs_scene.v1"
PREVIS_CAPTURE_MANIFEST_SCHEMA = "video_creation_pipeline.previs_capture_manifest.v1"
PREVIS_CAPTURE_VALIDATION_SCHEMA = (
    "video_creation_pipeline.previs_capture_validation.v1"
)


def build_previs_scene(unit_path: str | Path) -> dict[str, Any]:
    source_path = Path(unit_path).expanduser().resolve()
    unit = read_json_object(source_path, "shot generation unit")
    validate_shot_generation_unit(unit)
    if unit.get("schema") != SHOT_GENERATION_UNIT_SCHEMA:
        raise ContractError("previs requires a shot generation unit")
    width = int(unit["output"]["width"])
    height = int(unit["output"]["height"])
    camera_id = "camera-main"
    scene: dict[str, Any] = {
        "schema": PREVIS_SCENE_SCHEMA,
        "source_unit": artifact_reference(source_path, "shot generation unit"),
        "source_unit_sha256": unit["unit_sha256"],
        "project_version": 1,
        "scene": {
            "scene_id": unit["scene_id"],
            "name": unit["title"],
            "duration_s": unit["duration_s"],
            "visual_prompt": unit["visual_prompt"],
            "timeline_indexes": list(unit.get("timeline_indexes") or []),
        },
        "assets": [],
        "objects": [],
        "cameras": [
            {
                "camera_id": camera_id,
                "name": "Main Camera",
                "fov": 50.0,
                "position": [0.0, 1.6, 5.0],
                "rotation": [0.0, 0.0, 0.0],
                "target": [0.0, 1.5, 0.0],
                "frame": {
                    "width": width,
                    "height": height,
                    "aspect_ratio": f"{width}:{height}",
                    "rule_of_thirds": True,
                },
            }
        ],
        "active_camera_id": camera_id,
        "panorama_asset_id": None,
        "captures": [],
        "host_bridge": {
            "session_message": "video-creation:previs-session",
            "capture_message": "video-creation:previs-captures",
            "same_origin_required": True,
        },
        "authority_boundary": {
            "derived_artifact": True,
            "browser_local_storage_authoritative": False,
            "vkp_timeline_mutated": False,
            "final_render_claimed": False,
            "acceptance_status": "candidate_only",
        },
    }
    scene["previs_scene_sha256"] = payload_sha256(scene, "previs_scene_sha256")
    validate_previs_scene(scene)
    return scene


def validate_previs_scene(scene: dict[str, Any]) -> None:
    if scene.get("schema") != PREVIS_SCENE_SCHEMA:
        raise ContractError("unsupported previs scene schema")
    if scene.get("previs_scene_sha256") != payload_sha256(scene, "previs_scene_sha256"):
        raise ContractError("previs scene integrity check failed")
    unit_path = validate_artifact_reference(
        scene.get("source_unit") if isinstance(scene.get("source_unit"), dict) else {},
        "shot generation unit",
    )
    unit = read_json_object(unit_path, "shot generation unit")
    validate_shot_generation_unit(unit)
    if scene.get("source_unit_sha256") != unit.get("unit_sha256"):
        raise ContractError("previs scene does not bind the exact shot unit")
    cameras = scene.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ContractError("previs scene requires at least one camera")
    camera_ids: set[str] = set()
    for row in cameras:
        if not isinstance(row, dict):
            raise ContractError("previs camera must be an object")
        camera_id = str(row.get("camera_id") or "")
        if not camera_id or camera_id in camera_ids:
            raise ContractError("previs camera ids must be non-empty and unique")
        camera_ids.add(camera_id)
        if not _vector(row.get("position")) or not _vector(row.get("target")):
            raise ContractError("previs camera position and target must be 3D vectors")
        fov = float(row.get("fov") or 0)
        if not 1 <= fov <= 179:
            raise ContractError("previs camera fov must be between 1 and 179")
        frame = row.get("frame") if isinstance(row.get("frame"), dict) else {}
        if int(frame.get("width") or 0) <= 0 or int(frame.get("height") or 0) <= 0:
            raise ContractError("previs camera frame dimensions are invalid")
    if scene.get("active_camera_id") not in camera_ids:
        raise ContractError("previs active camera is missing")
    boundary = (
        scene.get("authority_boundary")
        if isinstance(scene.get("authority_boundary"), dict)
        else {}
    )
    if (
        boundary.get("derived_artifact") is not True
        or boundary.get("browser_local_storage_authoritative") is not False
        or boundary.get("vkp_timeline_mutated") is not False
        or boundary.get("final_render_claimed") is not False
        or boundary.get("acceptance_status") != "candidate_only"
    ):
        raise ContractError("previs authority boundary is invalid")


def validate_previs_capture_manifest(
    scene: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    validate_previs_scene(scene)
    if manifest.get("schema") != PREVIS_CAPTURE_MANIFEST_SCHEMA:
        raise ContractError("unsupported previs capture manifest schema")
    if manifest.get("previs_scene_sha256") != scene.get("previs_scene_sha256"):
        raise ContractError("capture manifest does not bind the previs scene")
    if manifest.get("acceptance_status") != "candidate_only":
        raise ContractError("previs captures must remain candidates")
    if manifest.get("final_render_claimed") is not False:
        raise ContractError("previs capture cannot claim a final render")
    cameras = {row["camera_id"]: row for row in scene["cameras"]}
    captures = manifest.get("captures")
    if not isinstance(captures, list) or not captures:
        raise ContractError("previs capture manifest requires at least one capture")
    capture_ids: set[str] = set()
    for index, capture in enumerate(captures, 1):
        if not isinstance(capture, dict):
            raise ContractError("previs capture must be an object")
        capture_id = str(capture.get("capture_id") or "")
        if not capture_id or capture_id in capture_ids:
            raise ContractError("previs capture ids must be non-empty and unique")
        capture_ids.add(capture_id)
        camera = cameras.get(str(capture.get("camera_id") or ""))
        if camera is None:
            raise ContractError(
                f"previs capture {capture_id} references an unknown camera"
            )
        reference = (
            capture.get("artifact") if isinstance(capture.get("artifact"), dict) else {}
        )
        validate_artifact_reference(reference, f"previs capture {index}")
        if (
            capture.get("fov") != camera["fov"]
            or capture.get("position") != camera["position"]
            or capture.get("target") != camera["target"]
            or capture.get("width") != camera["frame"]["width"]
            or capture.get("height") != camera["frame"]["height"]
        ):
            raise ContractError(f"previs capture {capture_id} camera metadata drifted")
    return {
        "schema": PREVIS_CAPTURE_VALIDATION_SCHEMA,
        "valid": True,
        "previs_scene_sha256": scene["previs_scene_sha256"],
        "capture_count": len(captures),
        "acceptance_status": "candidate_only",
        "next_action": "human_previs_review",
    }


def verify_previs_capture_files(
    scene_path: str | Path, manifest_path: str | Path
) -> dict[str, Any]:
    scene = read_json_object(scene_path, "previs scene")
    manifest = read_json_object(manifest_path, "previs capture manifest")
    return validate_previs_capture_manifest(scene, manifest)


def write_previs_scene(scene: dict[str, Any], output_path: str | Path) -> Path:
    validate_previs_scene(scene)
    return atomic_write_json(scene, output_path)


def _vector(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False
    try:
        [float(item) for item in value]
    except (TypeError, ValueError):
        return False
    return True
