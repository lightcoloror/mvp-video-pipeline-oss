from __future__ import annotations

import importlib.util
import math
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    file_sha256,
    payload_sha256,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import probe_media_metadata
from .reframe_tracking import track_sampled_subjects

SMART_REFRAME_EVIDENCE_SCHEMA = "video_creation_pipeline.smart_reframe_evidence.v1"
VKP_REFRAME_PROJECTION_SCHEMA = (
    "video_knowledge_pipeline.reframe_evidence_projection.v1"
)
PYSCENEDETECT_COMMIT = "131c5f60c9d40dd8fd281941d9b10a6a44fed4cc"
MEDIAPIPE_COMMIT = "85c0cdc7d70653888903c88df45962b764296342"
LIBVIPS_COMMIT = "8bc2e4fc64f108a5aa830f24c96ee49d91b18649"


def smart_reframe_doctor(
    *,
    mediapipe_model_path: str | Path | None = None,
    font_path: str | Path | None = None,
) -> dict[str, Any]:
    from .reframe_visual_qa import supervision_doctor

    model = (
        Path(mediapipe_model_path).expanduser().resolve()
        if mediapipe_model_path is not None
        else None
    )
    probes = {
        "ffmpeg": _command_probe("ffmpeg"),
        "ffprobe": _command_probe("ffprobe"),
        "opencv": _module_probe("cv2"),
        "pyscenedetect": _module_probe("scenedetect"),
        "mediapipe": _module_probe("mediapipe"),
        "norfair": _module_probe("norfair"),
        "pyvips": _module_probe("pyvips"),
        "supervision_qa": supervision_doctor(font_path),
        "mediapipe_model": {
            "status": "ready" if model and model.is_file() else "not_supplied",
            "path": str(model) if model else "",
            "sha256": file_sha256(model) if model and model.is_file() else "",
        },
    }
    basic_ready = (
        probes["ffmpeg"]["status"] == "ready" and probes["ffprobe"]["status"] == "ready"
    )
    return {
        "schema": "video_creation_pipeline.smart_reframe_doctor.v1",
        "status": "ready_for_evidence_driven_candidate_plan"
        if basic_ready
        else "blocked_missing_media_runtime",
        "probes": probes,
        "capabilities": {
            "evidence_driven_planning": "ready" if basic_ready else "blocked",
            "scene_detection": "ready"
            if probes["pyscenedetect"]["status"] == "ready"
            else "blocked_optional_dependency",
            "face_detection": "ready"
            if probes["mediapipe"]["status"] == "ready"
            and probes["mediapipe_model"]["status"] == "ready"
            else "blocked_optional_dependency_or_model",
            "norfair_tracking": "ready"
            if probes["norfair"]["status"] == "ready"
            else "blocked_optional_dependency",
            "static_smartcrop": "ready"
            if probes["pyvips"]["status"] == "ready"
            else "blocked_optional_dependency",
            "render_candidate": "ready" if basic_ready else "blocked",
            "sampled_visual_qa": probes["supervision_qa"]["status"],
            "tracking_mode": "frame_batched_iou_sampling_clock_v1",
        },
        "authority_boundary": {
            "candidate_only": True,
            "automatic_crop_enabled": False,
            "models_downloaded": False,
            "network_execution_performed": False,
            "source_media_modified": False,
            "automatic_publish": False,
        },
    }


def detect_scene_boundaries(
    source_media_path: str | Path,
    *,
    detector: str = "adaptive",
    threshold: float | None = None,
) -> dict[str, Any]:
    media = _media_path(source_media_path)
    try:
        from scenedetect import (
            AdaptiveDetector,
            ContentDetector,
            SceneManager,
            open_video,
        )
    except ImportError as exc:
        raise ContractError(
            f"optional PySceneDetect runtime is unavailable: {exc}"
        ) from exc
    if detector not in {"adaptive", "content"}:
        raise ContractError("scene detector must be adaptive or content")
    video = open_video(str(media))
    manager = SceneManager()
    if detector == "adaptive":
        kwargs = (
            {"adaptive_threshold": float(threshold)} if threshold is not None else {}
        )
        manager.add_detector(AdaptiveDetector(**kwargs))
    else:
        kwargs = {"threshold": float(threshold)} if threshold is not None else {}
        manager.add_detector(ContentDetector(**kwargs))
    manager.detect_scenes(video=video, show_progress=False)
    scenes = manager.get_scene_list(start_in_scene=True)
    observations = [
        {
            "evidence_id": f"shot-{index:04d}",
            "kind": "shot_boundary",
            "start_s": round(start.get_seconds(), 6),
            "end_s": round(end.get_seconds(), 6),
            "time_s": round(start.get_seconds(), 6),
            "required": True,
            "score": 1.0,
            "rect": None,
            "source_modality": "video_frame_difference",
        }
        for index, (start, end) in enumerate(scenes, start=1)
    ]
    return build_smart_reframe_evidence(
        media,
        provider={
            "name": "PySceneDetect",
            "source_commit": PYSCENEDETECT_COMMIT,
            "mode": detector,
            "threshold": threshold,
        },
        observations=observations,
    )


def detect_faces_with_mediapipe(
    source_media_path: str | Path,
    model_path: str | Path,
    *,
    sample_interval_s: float = 0.5,
    minimum_confidence: float = 0.5,
) -> dict[str, Any]:
    media = _media_path(source_media_path)
    model = Path(model_path).expanduser().resolve()
    if not model.is_file():
        raise ContractError(f"MediaPipe face detector model not found: {model}")
    if sample_interval_s <= 0:
        raise ContractError("face detector sample interval must be positive")
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
    except ImportError as exc:
        raise ContractError(
            f"optional MediaPipe runtime is unavailable: {exc}"
        ) from exc

    capture = cv2.VideoCapture(str(media))
    if not capture.isOpened():
        raise ContractError("OpenCV could not open source media for face detection")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        capture.release()
        raise ContractError("source media FPS is unavailable")
    step = max(1, int(round(fps * sample_interval_s)))
    options = vision.FaceDetectorOptions(
        base_options=python.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.VIDEO,
        min_detection_confidence=float(minimum_confidence),
    )
    observations: list[dict[str, Any]] = []
    sample_times: list[float] = []
    frame_index = 0
    with vision.FaceDetector.create_from_options(options) as face_detector:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_index % step:
                frame_index += 1
                continue
            height, width = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            time_s = _capture_sample_time(capture, cv2, sample_times)
            timestamp_ms = round(time_s * 1000)
            result = face_detector.detect_for_video(image, timestamp_ms)
            for face_index, detection in enumerate(result.detections, start=1):
                box = detection.bounding_box
                rect = _pixel_rect(
                    box.origin_x, box.origin_y, box.width, box.height, width, height
                )
                if rect is None:
                    continue
                score = (
                    float(detection.categories[0].score)
                    if detection.categories
                    else 0.0
                )
                observations.append(
                    {
                        "evidence_id": f"face-{frame_index:08d}-{face_index:02d}",
                        "kind": "face",
                        "time_s": time_s,
                        "required": True,
                        "score": round(score, 6),
                        "rect": rect,
                        "source_modality": "video_frame",
                    }
                )
            frame_index += 1
    capture.release()
    return build_smart_reframe_evidence(
        media,
        provider={
            "name": "MediaPipe FaceDetector",
            "source_commit": MEDIAPIPE_COMMIT,
            "model_sha256": file_sha256(model),
            "sample_interval_s": float(sample_interval_s),
            "sample_times_s": sample_times,
            "timestamp_source": "opencv_decoded_presentation_time",
            "minimum_confidence": float(minimum_confidence),
        },
        observations=observations,
    )


def detect_faces_with_opencv_haar(
    source_media_path: str | Path,
    *,
    sample_interval_s: float = 0.5,
    scale_factor: float = 1.1,
    min_neighbors: int = 5,
    min_size: tuple[int, int] = (48, 48),
) -> dict[str, Any]:
    """Produce low-confidence candidate-only face evidence with OpenCV Haar."""
    media = _media_path(source_media_path)
    if sample_interval_s <= 0 or scale_factor <= 1.0 or min_neighbors < 1:
        raise ContractError("invalid OpenCV Haar detector parameters")
    try:
        import cv2
    except ImportError as exc:
        raise ContractError(f"OpenCV runtime is unavailable: {exc}") from exc
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not cascade_path.is_file():
        raise ContractError(f"OpenCV Haar cascade not found: {cascade_path}")
    classifier = cv2.CascadeClassifier(str(cascade_path))
    if classifier.empty():
        raise ContractError(f"OpenCV Haar cascade could not be loaded: {cascade_path}")
    capture = cv2.VideoCapture(str(media))
    if not capture.isOpened():
        raise ContractError("OpenCV could not open source media for face detection")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        capture.release()
        raise ContractError("source media FPS is unavailable")
    step = max(1, int(round(fps * sample_interval_s)))
    observations: list[dict[str, Any]] = []
    sample_times: list[float] = []
    frame_index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index % step:
            frame_index += 1
            continue
        height, width = frame.shape[:2]
        time_s = _capture_sample_time(capture, cv2, sample_times)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = classifier.detectMultiScale(
            gray, scaleFactor=scale_factor, minNeighbors=min_neighbors, minSize=min_size
        )
        for face_index, (x, y, w, h) in enumerate(faces, start=1):
            rect = _pixel_rect(x, y, w, h, width, height)
            if rect is not None:
                observations.append(
                    {
                        "evidence_id": f"face-haar-{frame_index:08d}-{face_index:02d}",
                        "kind": "face",
                        "time_s": time_s,
                        "required": True,
                        "score": 0.35,
                        "rect": rect,
                        "source_modality": "opencv_haar_video_frame",
                    }
                )
        frame_index += 1
    capture.release()
    return build_smart_reframe_evidence(
        media,
        provider={
            "name": "OpenCV Haar face candidate",
            "source_commit": "local-opencv-runtime",
            "confidence_tier": "low_candidate_below_mediapipe",
            "opencv_version": cv2.__version__,
            "cascade_sha256": file_sha256(cascade_path),
            "sample_interval_s": float(sample_interval_s),
            "sample_times_s": sample_times,
            "timestamp_source": "opencv_decoded_presentation_time",
            "scale_factor": float(scale_factor),
            "min_neighbors": int(min_neighbors),
            "min_size": list(min_size),
        },
        observations=observations,
    )


def detect_static_attention_with_libvips(
    source_image_path: str | Path,
    *,
    target_width: int,
    target_height: int,
) -> dict[str, Any]:
    image_path = _media_path(source_image_path)
    try:
        import pyvips
    except ImportError as exc:
        raise ContractError(f"optional pyvips runtime is unavailable: {exc}") from exc
    if target_width <= 0 or target_height <= 0:
        raise ContractError("libvips smartcrop target dimensions must be positive")
    image = pyvips.Image.new_from_file(str(image_path), access="sequential")
    crop = image.smartcrop(
        int(target_width), int(target_height), interesting="attention"
    )
    x = max(0, -int(crop.xoffset))
    y = max(0, -int(crop.yoffset))
    rect = _pixel_rect(x, y, crop.width, crop.height, image.width, image.height)
    if rect is None:
        raise ContractError("libvips returned an invalid smartcrop window")
    return build_smart_reframe_evidence(
        image_path,
        provider={
            "name": "libvips smartcrop",
            "source_commit": LIBVIPS_COMMIT,
            "interesting": "attention",
        },
        observations=[
            {
                "evidence_id": "static-attention-0001",
                "kind": "visual_attention",
                "time_s": 0.0,
                "required": False,
                "score": 0.5,
                "rect": rect,
                "source_modality": "image_attention",
            }
        ],
    )


def adapt_vkp_reframe_evidence(
    source_media_path: str | Path,
    projection_path: str | Path,
) -> dict[str, Any]:
    """Consume a hash-bound VKP projection without reading or mutating VKP state."""

    media = _media_path(source_media_path)
    projection_file = Path(projection_path).expanduser().resolve()
    try:
        import json

        projection = json.loads(projection_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"VKP reframe projection is unreadable: {exc}") from exc
    if not isinstance(projection, dict):
        raise ContractError("VKP reframe projection must be an object")
    if projection.get("schema") != VKP_REFRAME_PROJECTION_SCHEMA:
        raise ContractError("unsupported VKP reframe projection schema")
    if projection.get("projection_sha256") != payload_sha256(
        projection, "projection_sha256"
    ):
        raise ContractError("VKP reframe projection integrity check failed")
    if projection.get("source_media_sha256") != file_sha256(media):
        raise ContractError("VKP reframe projection binds another source media")
    artifacts = projection.get("source_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ContractError("VKP reframe projection requires source artifacts")
    artifact_ids: set[str] = set()
    for index, row in enumerate(artifacts, start=1):
        item = _object(row)
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id or artifact_id in artifact_ids:
            raise ContractError("VKP reframe projection artifact identity is invalid")
        artifact_ids.add(artifact_id)
        validate_artifact_reference(
            _object(item.get("artifact")), f"VKP reframe source artifact {index}"
        )
    observations = projection.get("observations")
    if not isinstance(observations, list):
        raise ContractError("VKP reframe projection observations must be a list")
    for row in observations:
        item = _object(row)
        if str(item.get("evidence_ref") or "") not in artifact_ids:
            raise ContractError("VKP reframe observation lacks a source artifact ref")
    if projection.get("authority_boundary") != {
        "read_only_projection": True,
        "mutates_vkp_timeline": False,
        "creates_second_evidence_store": False,
        "identity_recognition_performed": False,
    }:
        raise ContractError("VKP reframe projection authority boundary drifted")
    return build_smart_reframe_evidence(
        media,
        provider={
            "name": "VKP reframe evidence projection",
            "projection": artifact_reference(
                projection_file, "VKP reframe evidence projection"
            ),
            "read_only": True,
        },
        observations=[_object(row) for row in observations],
    )


def track_smart_reframe_evidence_with_norfair(
    evidence: dict[str, Any],
    *,
    scene_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add non-identity track IDs while preserving the detector observations."""

    validate_smart_reframe_evidence(evidence)
    provider = dict(evidence["provider"])
    if provider.get("selected_track_id") or any(
        row["kind"] == "user_hint"
        and row.get("track_id")
        or row["kind"] in {"face", "person"}
        and any(
            row.get(field)
            for field in (
                "speaker_id",
                "asr_speaker_id",
                "visual_speaker_id",
                "role_mapping_speaker_id",
                "role_mapping_confirmed",
            )
        )
        or row["kind"] == "active_speaker"
        and row.get("track_id")
        for row in evidence["observations"]
    ):
        raise ContractError(
            "retrack raw evidence before selecting a subject or mapping roles"
        )
    scene_rows = evidence["observations"]
    if scene_evidence is not None:
        validate_smart_reframe_evidence(scene_evidence)
        if (
            scene_evidence["source_media"]["sha256"]
            != evidence["source_media"]["sha256"]
        ):
            raise ContractError("scene evidence source hash mismatch")
        scene_rows = [*scene_rows, *scene_evidence["observations"]]
    cuts = sorted(
        {float(row["start_s"]) for row in scene_rows if row["kind"] == "shot_boundary"}
    )
    tracks = track_sampled_subjects(
        evidence["observations"],
        sample_times_s=provider.get("sample_times_s"),
        sample_interval_s=float(provider.get("sample_interval_s", 0.5)),
        scene_cuts_s=cuts,
    )
    by_id = {row["evidence_id"]: row for row in tracks}
    observations = []
    for row in evidence["observations"]:
        copy = dict(row)
        tracked = by_id.get(str(row["evidence_id"]))
        if tracked is not None:
            copy["track_id"] = tracked["track_id"]
        observations.append(copy)
    if scene_evidence is not None:
        existing = {row["evidence_id"]: row for row in observations}
        for row in scene_evidence["observations"]:
            if row["kind"] != "shot_boundary":
                continue
            if row["evidence_id"] in existing and existing[row["evidence_id"]] != row:
                raise ContractError("conflicting scene evidence IDs")
            if row["evidence_id"] not in existing:
                observations.append(dict(row))
    provider["tracking"] = {
        "name": "Norfair",
        "source_commit": "e517b4236f6b67a6ecf342f5df1fccb7788dbc54",
        "identity_claimed": False,
        "mode": "frame_batched_iou_sampling_clock_v1",
        "sampling_coverage": "known"
        if "sample_times_s" in provider
        else "unknown_legacy_v1",
        "sample_interval_s": float(provider.get("sample_interval_s", 0.5)),
        "hit_counter_max": 8,
        "scene_cuts_s": cuts,
        "scene_evidence_sha256": scene_evidence["evidence_sha256"]
        if scene_evidence
        else None,
        "estimated_boxes_are_source_evidence": False,
    }
    source = validate_artifact_reference(
        _object(evidence["source_media"]), "smart reframe source media"
    )
    return build_smart_reframe_evidence(
        source, provider=provider, observations=observations
    )


def build_smart_reframe_evidence(
    source_media_path: str | Path,
    *,
    provider: dict[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    media = _media_path(source_media_path)
    normalized = [_normalize_observation(row) for row in observations]
    normalized.sort(key=lambda row: (float(row["time_s"]), str(row["evidence_id"])))
    payload: dict[str, Any] = {
        "schema": SMART_REFRAME_EVIDENCE_SCHEMA,
        "source_media": artifact_reference(media, "smart reframe source media"),
        "provider": dict(provider),
        "observations": normalized,
        "authority_boundary": {
            "candidate_evidence_only": True,
            "identity_recognition_performed": False,
            "source_media_modified": False,
            "automatic_crop_enabled": False,
            "automatic_publish": False,
        },
    }
    payload["evidence_sha256"] = payload_sha256(payload, "evidence_sha256")
    validate_smart_reframe_evidence(payload)
    return payload


def select_smart_reframe_track(
    evidence: dict[str, Any], track_id: str
) -> dict[str, Any]:
    """Project an explicit user-selected track into canonical user-hint evidence."""

    validate_smart_reframe_evidence(evidence)
    selected = str(track_id).strip()
    if not selected:
        raise ContractError("smart reframe track selection requires track_id")
    subject_kinds = {"face", "person", "active_speaker", "user_hint"}
    selected_rows = [
        row
        for row in evidence["observations"]
        if row["kind"] in subject_kinds and str(row.get("track_id") or "") == selected
    ]
    if not selected_rows:
        raise ContractError(f"smart reframe track was not found: {selected}")
    observations: list[dict[str, Any]] = []
    for row in evidence["observations"]:
        if row["kind"] not in subject_kinds:
            observations.append(dict(row))
            continue
        if str(row.get("track_id") or "") != selected:
            continue
        chosen = dict(row)
        chosen["required"] = True
        observations.append(chosen)
        if row["kind"] != "user_hint":
            observations.append(
                {
                    "evidence_id": f"user-hint-{row['evidence_id']}",
                    "kind": "user_hint",
                    "time_s": row["time_s"],
                    "required": True,
                    "score": 1.0,
                    "rect": dict(row["rect"]),
                    "source_modality": "explicit_user_track_selection",
                    "track_id": selected,
                    "evidence_ref": row["evidence_id"],
                }
            )
    source = validate_artifact_reference(
        _object(evidence["source_media"]), "smart reframe source media"
    )
    return build_smart_reframe_evidence(
        source,
        provider={
            "name": "explicit user track selection",
            "selected_track_id": selected,
            "source_evidence_sha256": evidence["evidence_sha256"],
            **{
                key: evidence["provider"][key]
                for key in (
                    "sample_times_s",
                    "sample_interval_s",
                    "timestamp_source",
                    "tracking",
                )
                if key in evidence["provider"]
            },
        },
        observations=observations,
    )


def validate_smart_reframe_evidence(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SMART_REFRAME_EVIDENCE_SCHEMA:
        raise ContractError("unsupported smart reframe evidence schema")
    if payload.get("evidence_sha256") != payload_sha256(payload, "evidence_sha256"):
        raise ContractError("smart reframe evidence integrity check failed")
    validate_artifact_reference(
        _object(payload.get("source_media")), "smart reframe source media"
    )
    provider = _object(payload.get("provider"))
    if not str(provider.get("name") or "").strip():
        raise ContractError("smart reframe evidence provider is required")
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ContractError("smart reframe evidence observations must be a list")
    normalized = [_normalize_observation(_object(row)) for row in observations]
    if "sample_times_s" in provider:
        times = provider["sample_times_s"]
        if (
            not isinstance(times, list)
            or any(
                not isinstance(t, (int, float))
                or isinstance(t, bool)
                or not math.isfinite(t)
                or t < 0
                or round(t, 6) != t
                for t in times
            )
            or times != sorted(set(times))
        ):
            raise ContractError(
                "sample_times_s must be canonical increasing timestamps"
            )
        if not {
            r["time_s"] for r in normalized if r["kind"] in {"face", "person"}
        }.issubset(times):
            raise ContractError("sample_times_s omits subject observations")
    if observations != sorted(
        normalized, key=lambda row: (float(row["time_s"]), str(row["evidence_id"]))
    ):
        raise ContractError("smart reframe evidence observations are not canonical")
    if len({row["evidence_id"] for row in normalized}) != len(normalized):
        raise ContractError("smart reframe evidence IDs must be unique")
    expected_boundary = {
        "candidate_evidence_only": True,
        "identity_recognition_performed": False,
        "source_media_modified": False,
        "automatic_crop_enabled": False,
        "automatic_publish": False,
    }
    if payload.get("authority_boundary") != expected_boundary:
        raise ContractError("smart reframe evidence authority boundary drifted")


def write_smart_reframe_evidence(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_smart_reframe_evidence(payload)
    return atomic_write_json(payload, output_path)


def source_media_dimensions(source_media_path: str | Path) -> dict[str, Any]:
    source = _media_path(source_media_path)
    payload = probe_media_metadata(source)
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    if not isinstance(video, dict):
        raise ContractError("source media has no video stream")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ContractError("source media dimensions are invalid")
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    if not mime_type.startswith(("video/", "image/")):
        raise ContractError(f"unsupported smart reframe media MIME: {mime_type}")
    media_kind = "image" if mime_type.startswith("image/") else "video"
    if media_kind == "image":
        duration = 1.0
        fps = 1.0
    else:
        duration = _duration(payload, video)
        fps = _fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    return {
        "width": width,
        "height": height,
        "duration_s": duration,
        "fps": fps,
        "has_audio": any(row.get("codec_type") == "audio" for row in streams),
        "media_kind": media_kind,
        "mime_type": mime_type,
    }


def _normalize_observation(row: dict[str, Any]) -> dict[str, Any]:
    evidence_id = str(row.get("evidence_id") or "").strip()
    kind = str(row.get("kind") or "").strip()
    if not evidence_id or kind not in {
        "shot_boundary",
        "face",
        "person",
        "active_speaker",
        "object",
        "ocr_text",
        "chart",
        "logo",
        "user_hint",
        "visual_attention",
    }:
        raise ContractError(
            "smart reframe evidence observation identity or kind is invalid"
        )
    try:
        time_s = float(row.get("time_s", 0.0))
        score = float(row.get("score", 0.0))
    except (TypeError, ValueError) as exc:
        raise ContractError("smart reframe evidence time or score is invalid") from exc
    if not math.isfinite(time_s) or time_s < 0 or not 0 <= score <= 1:
        raise ContractError("smart reframe evidence time or score is out of range")
    required = row.get("required")
    if not isinstance(required, bool):
        raise ContractError("smart reframe evidence required flag must be boolean")
    rect = row.get("rect")
    if kind == "shot_boundary":
        start_s = float(row.get("start_s", time_s))
        end_s = float(row.get("end_s", 0.0))
        if start_s < 0 or end_s <= start_s:
            raise ContractError("shot boundary interval is invalid")
        normalized_rect = None
    else:
        start_s = None
        end_s = None
        normalized_rect = _normalize_rect(rect)
    normalized: dict[str, Any] = {
        "evidence_id": evidence_id,
        "kind": kind,
        "time_s": round(time_s, 6),
        "required": required,
        "score": round(score, 6),
        "rect": normalized_rect,
        "source_modality": str(row.get("source_modality") or "unknown"),
    }
    if kind == "shot_boundary":
        normalized["start_s"] = round(float(start_s), 6)
        normalized["end_s"] = round(float(end_s), 6)
    for field in (
        "speaker_id",
        "track_id",
        "text",
        "evidence_ref",
        "asr_speaker_id",
        "visual_speaker_id",
        "role_mapping_speaker_id",
    ):
        if row.get(field) is not None:
            normalized[field] = str(row[field])
    if row.get("role_mapping_confirmed") is not None:
        if not isinstance(row["role_mapping_confirmed"], bool):
            raise ContractError("role mapping confirmation must be boolean")
        normalized["role_mapping_confirmed"] = row["role_mapping_confirmed"]
    return normalized


def _normalize_rect(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ContractError("spatial smart reframe evidence requires a rectangle")
    try:
        rect = {field: float(value[field]) for field in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("smart reframe evidence rectangle is invalid") from exc
    if rect["width"] <= 0 or rect["height"] <= 0:
        raise ContractError("smart reframe evidence rectangle must be positive")
    if (
        rect["x"] < 0
        or rect["y"] < 0
        or rect["x"] + rect["width"] > 1.0000001
        or rect["y"] + rect["height"] > 1.0000001
    ):
        raise ContractError("smart reframe evidence rectangle leaves source bounds")
    return {field: round(number, 8) for field, number in rect.items()}


def _pixel_rect(
    x: int, y: int, width: int, height: int, source_width: int, source_height: int
) -> dict[str, float] | None:
    left = max(0, int(x))
    top = max(0, int(y))
    right = min(source_width, left + int(width))
    bottom = min(source_height, top + int(height))
    if right <= left or bottom <= top:
        return None
    return {
        "x": round(left / source_width, 8),
        "y": round(top / source_height, 8),
        "width": round((right - left) / source_width, 8),
        "height": round((bottom - top) / source_height, 8),
    }


def _media_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"smart reframe source media not found: {path}")
    return path


def _capture_sample_time(capture: Any, cv2: Any, times: list[float]) -> float:
    time_s = round(float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000, 6)
    if not math.isfinite(time_s) or time_s < 0 or times and time_s <= times[-1]:
        capture.release()
        raise ContractError("decoded frame presentation timestamps are unavailable")
    times.append(time_s)
    return time_s


def _module_probe(name: str) -> dict[str, Any]:
    return {
        "status": "ready" if importlib.util.find_spec(name) else "missing",
        "module": name,
    }


def _command_probe(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"status": "ready" if path else "missing", "path": path or ""}


def _duration(probe: dict[str, Any], video: dict[str, Any]) -> float:
    for value in (video.get("duration"), (probe.get("format") or {}).get("duration")):
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0 and math.isfinite(duration):
            return round(duration, 6)
    raise ContractError("source media duration is unavailable")


def _fps(value: Any) -> float:
    text = str(value or "0")
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            result = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            result = 0.0
    else:
        try:
            result = float(text)
        except ValueError:
            result = 0.0
    if result <= 0 or not math.isfinite(result):
        raise ContractError("source media FPS is invalid")
    return round(result, 6)


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value
