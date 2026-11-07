from __future__ import annotations

import math
from bisect import bisect_left
from itertools import pairwise
from typing import Any

from .errors import ContractError


def smooth_crop_keyframes(
    keyframes: list[dict[str, Any]],
    *,
    deadband: float = 0.035,
    minimum_dwell_s: float = 0.35,
    max_speed_per_s: float = 0.6,
    max_acceleration_per_s2: float = 1.8,
) -> list[dict[str, Any]]:
    """Apply deterministic camera-motion limits inside one shot.

    Crop width/height remain decisions from the composition solver. This helper
    only limits center motion and is reset by calling it separately per shot.
    """

    if not keyframes:
        return []
    if not 0 <= deadband < 1:
        raise ContractError("reframe deadband must be within [0, 1)")
    if minimum_dwell_s < 0 or max_speed_per_s <= 0 or max_acceleration_per_s2 <= 0:
        raise ContractError("reframe temporal limits are invalid")
    ordered = sorted((_normalized_keyframe(row) for row in keyframes), key=_time)
    if any(_time(right) <= _time(left) for left, right in pairwise(ordered)):
        raise ContractError("reframe keyframe timestamps must be strictly increasing")

    output = [ordered[0]]
    velocity = (0.0, 0.0)
    last_change_time = _time(ordered[0])
    for candidate in ordered[1:]:
        previous = output[-1]
        dt = _time(candidate) - _time(previous)
        previous_center = _center(previous["crop"])
        wanted_center = _center(candidate["crop"])
        distance = math.dist(previous_center, wanted_center)
        if (
            distance <= deadband
            and _time(candidate) - last_change_time < minimum_dwell_s
        ):
            held = dict(candidate)
            held["crop"] = dict(previous["crop"])
            held["motion_reason"] = "deadband_hold"
            output.append(held)
            velocity = (0.0, 0.0)
            continue

        wanted_velocity = (
            (wanted_center[0] - previous_center[0]) / dt,
            (wanted_center[1] - previous_center[1]) / dt,
        )
        limited_velocity = _limit_vector(wanted_velocity, max_speed_per_s)
        acceleration = (
            (limited_velocity[0] - velocity[0]) / dt,
            (limited_velocity[1] - velocity[1]) / dt,
        )
        acceleration = _limit_vector(acceleration, max_acceleration_per_s2)
        velocity = (
            velocity[0] + acceleration[0] * dt,
            velocity[1] + acceleration[1] * dt,
        )
        velocity = _limit_vector(velocity, max_speed_per_s)
        center = (
            previous_center[0] + velocity[0] * dt,
            previous_center[1] + velocity[1] * dt,
        )
        crop = _crop_at_center(candidate["crop"], center)
        moved = dict(candidate)
        moved["crop"] = crop
        moved["motion_reason"] = (
            "direct_within_limits"
            if math.dist(center, wanted_center) <= 1e-7
            else "kinematic_limit"
        )
        output.append(moved)
        if math.dist(previous_center, center) > deadband:
            last_change_time = _time(candidate)
    return output


def optional_norfair_track_centers(
    observations: list[dict[str, Any]],
    *,
    distance_threshold: float = 0.25,
    hit_counter_max: int = 5,
) -> list[dict[str, Any]]:
    """Use reviewed Norfair when installed; never hide a missing dependency."""

    try:
        import numpy as np
        from norfair import Detection, Tracker
    except ImportError as exc:
        raise ContractError(f"optional Norfair runtime is unavailable: {exc}") from exc
    tracker = Tracker(
        distance_function="euclidean",
        distance_threshold=float(distance_threshold),
        hit_counter_max=int(hit_counter_max),
        initialization_delay=0,
    )
    results: list[dict[str, Any]] = []
    for row in sorted(observations, key=lambda value: float(value["time_s"])):
        rect = _normalized_rect(row.get("rect"))
        point = np.array([[_center(rect)[0], _center(rect)[1]]], dtype=float)
        tracked = tracker.update(
            detections=[
                Detection(
                    points=point,
                    scores=np.array([float(row.get("score", 1.0))]),
                    data={"evidence_id": row.get("evidence_id")},
                )
            ]
        )
        if not tracked:
            continue
        selected = min(
            tracked,
            key=lambda item: math.dist(
                tuple(float(value) for value in item.estimate[0]), tuple(point[0])
            ),
        )
        results.append(
            {
                "time_s": round(float(row["time_s"]), 6),
                "track_id": f"track-{int(selected.id)}",
                "center": {
                    "x": round(float(selected.estimate[0][0]), 8),
                    "y": round(float(selected.estimate[0][1]), 8),
                },
                "evidence_id": str(row.get("evidence_id") or ""),
                "identity_claimed": False,
            }
        )
    return results


def track_rectangles_with_norfair(
    observations: list[dict[str, Any]],
    *,
    scene_cuts: list[int] | None = None,
    distance_threshold: float = 0.7,
    hit_counter_max: int = 8,
) -> list[dict[str, Any]]:
    """Associate all rectangles in a frame with the reviewed Norfair IoU tracker.

    Tracking is deliberately anonymous and resets at every scene cut. Norfair
    supplies continuity only; callers remain responsible for conservative
    privacy coverage and must not treat a track id as a person identity.
    """

    if not 0 < float(distance_threshold) <= 1:
        raise ContractError("Norfair IoU distance threshold must be within (0, 1]")
    cuts = sorted({int(value) for value in scene_cuts or []})
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(observations):
        observation_id = str(row.get("observation_id") or f"observation-{index}")
        if observation_id in seen_ids:
            raise ContractError("privacy tracking observation ids must be unique")
        seen_ids.add(observation_id)
        try:
            frame_index = int(row["frame_index"])
            target_type = str(row["target_type"])
            score = float(row.get("score", 1.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("privacy tracking observation is invalid") from exc
        if frame_index < 0 or target_type not in {"face", "plate", "manual_region"}:
            raise ContractError("privacy tracking observation is invalid")
        rect = _normalized_rect(row.get("rect"))
        normalized.append(
            {
                **row,
                "observation_id": observation_id,
                "frame_index": frame_index,
                "target_type": target_type,
                "score": score,
                "rect": rect,
            }
        )
    if not normalized:
        return []

    return _associate_rectangles(
        normalized, cuts, distance_threshold, hit_counter_max, stable_ids=False
    )


def track_sampled_subjects(
    observations: list[dict[str, Any]],
    *,
    sample_times_s: list[float] | None = None,
    sample_interval_s: float = 0.5,
    scene_cuts_s: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Adapt evidence sampling batches to the existing privacy association kernel."""
    if not math.isfinite(sample_interval_s) or sample_interval_s <= 0:
        raise ContractError("tracking sample interval must be finite and positive")
    subjects = [row for row in observations if row["kind"] in {"face", "person"}]
    observed = {round(float(row["time_s"]), 6) for row in subjects}
    times = sorted(observed) if sample_times_s is None else list(sample_times_s)
    if (
        any(not math.isfinite(t) or t < 0 for t in times)
        or times != sorted(set(times))
        or not observed.issubset(times)
    ):
        raise ContractError("tracking sample timestamps are invalid or omit detections")
    if not subjects:
        return []
    ticks = {time: index for index, time in enumerate(times)}
    cuts = {bisect_left(times, float(t)) for t in scene_cuts_s or []}
    # An unobserved interval is not a series of negative detections. Reset instead.
    cuts.update(
        index
        for index in range(1, len(times))
        if times[index] - times[index - 1] > sample_interval_s * 8 + 1e-6
    )
    rows = [
        {
            **row,
            "observation_id": row["evidence_id"],
            "frame_index": ticks[round(float(row["time_s"]), 6)],
            "target_type": row["kind"],
        }
        for row in subjects
    ]
    rows.sort(
        key=lambda row: (
            row["frame_index"],
            row["target_type"],
            tuple(row["rect"][key] for key in ("x", "y", "width", "height")),
            row["observation_id"],
        )
    )
    return _associate_rectangles(
        rows,
        sorted(cut for cut in cuts if 0 < cut < len(times)),
        0.7,
        8,
        stable_ids=True,
    )


def _associate_rectangles(
    normalized: list[dict[str, Any]],
    cuts: list[int],
    distance_threshold: float,
    hit_counter_max: int,
    *,
    stable_ids: bool,
) -> list[dict[str, Any]]:
    try:
        import numpy as np
        from norfair import Detection, Tracker
    except ImportError as exc:
        raise ContractError(f"optional Norfair runtime is unavailable: {exc}") from exc
    seen_ids = {row["observation_id"] for row in normalized}
    if len(seen_ids) != len(normalized):
        raise ContractError("tracking observation ids must be unique")

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in normalized:
        grouped.setdefault(int(row["frame_index"]), []).append(row)
    results: dict[str, dict[str, Any]] = {}
    boundaries = [0, *cuts, max(grouped) + 1]
    for scene_index, (scene_start, scene_end) in enumerate(pairwise(boundaries)):
        scene_frames = [frame for frame in grouped if scene_start <= frame < scene_end]
        if not scene_frames:
            continue
        tracker = Tracker(
            distance_function="iou",
            distance_threshold=float(distance_threshold),
            hit_counter_max=int(hit_counter_max),
            initialization_delay=0,
        )
        local_ids: dict[int, int] = {}
        for frame_index in range(min(scene_frames), max(scene_frames) + 1):
            current = grouped.get(frame_index, [])
            detections = []
            for row in current:
                rect = row["rect"]
                detections.append(
                    Detection(
                        points=np.array(
                            [
                                [rect["x"], rect["y"]],
                                [
                                    rect["x"] + rect["width"],
                                    rect["y"] + rect["height"],
                                ],
                            ],
                            dtype=float,
                        ),
                        scores=np.array([row["score"], row["score"]], dtype=float),
                        label=row["target_type"],
                        data={"observation_id": row["observation_id"]},
                    )
                )
            tracked = tracker.update(detections=detections)
            current_by_id = {row["observation_id"]: row for row in current}
            for item in tracked:
                last = getattr(item, "last_detection", None)
                data = getattr(last, "data", None)
                observation_id = (
                    str(data.get("observation_id"))
                    if isinstance(data, dict) and data.get("observation_id")
                    else ""
                )
                if observation_id not in current_by_id:
                    continue
                source = current_by_id[observation_id]
                local_ids.setdefault(int(item.id), len(local_ids) + 1)
                track_id = local_ids[int(item.id)] if stable_ids else int(item.id)
                estimate = item.estimate
                estimated_rect = {
                    "x": float(estimate[0][0]),
                    "y": float(estimate[0][1]),
                    "width": float(estimate[1][0] - estimate[0][0]),
                    "height": float(estimate[1][1] - estimate[0][1]),
                }
                results[observation_id] = {
                    **source,
                    "track_id": f"scene-{scene_index}-track-{track_id}",
                    "estimated_rect": _normalized_rect(estimated_rect),
                    "identity_claimed": False,
                }
    if set(results) != seen_ids:
        missing = sorted(seen_ids - set(results))
        raise ContractError(
            "Norfair did not associate every privacy observation: "
            + ", ".join(missing[:5])
        )
    return [results[row["observation_id"]] for row in normalized]


def _normalized_keyframe(row: dict[str, Any]) -> dict[str, Any]:
    try:
        time_s = float(row["time_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("reframe keyframe time is invalid") from exc
    if time_s < 0:
        raise ContractError("reframe keyframe time cannot be negative")
    result = dict(row)
    result["time_s"] = round(time_s, 6)
    result["crop"] = _normalized_rect(row.get("crop"))
    return result


def _normalized_rect(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ContractError("reframe crop must be an object")
    try:
        rect = {name: float(value[name]) for name in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("reframe crop coordinates are invalid") from exc
    if rect["width"] <= 0 or rect["height"] <= 0:
        raise ContractError("reframe crop dimensions must be positive")
    if (
        rect["x"] < 0
        or rect["y"] < 0
        or rect["x"] + rect["width"] > 1.0000001
        or rect["y"] + rect["height"] > 1.0000001
    ):
        raise ContractError("reframe crop must remain inside normalized source bounds")
    return {name: round(number, 8) for name, number in rect.items()}


def _time(row: dict[str, Any]) -> float:
    return float(row["time_s"])


def _center(rect: dict[str, float]) -> tuple[float, float]:
    return rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2


def _limit_vector(value: tuple[float, float], maximum: float) -> tuple[float, float]:
    magnitude = math.hypot(*value)
    if magnitude <= maximum or magnitude == 0:
        return value
    scale = maximum / magnitude
    return value[0] * scale, value[1] * scale


def _crop_at_center(
    template: dict[str, float], center: tuple[float, float]
) -> dict[str, float]:
    width = template["width"]
    height = template["height"]
    x = min(max(center[0] - width / 2, 0.0), 1.0 - width)
    y = min(max(center[1] - height / 2, 0.0), 1.0 - height)
    return {
        "x": round(x, 8),
        "y": round(y, 8),
        "width": round(width, 8),
        "height": round(height, 8),
    }
