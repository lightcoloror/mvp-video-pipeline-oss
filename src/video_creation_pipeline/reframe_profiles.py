from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .contract_io import canonical_json_sha256
from .errors import ContractError

REFRAME_PROFILES_SCHEMA = "video_creation_pipeline.reframe_profiles.v1"


def default_reframe_profiles_path() -> Path:
    return Path(__file__).resolve().parent / "policies" / "reframe_profiles.v1.json"


def load_reframe_profiles(path: str | Path | None = None) -> dict[str, Any]:
    source = (
        Path(path).expanduser().resolve()
        if path is not None
        else default_reframe_profiles_path()
    )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"reframe profiles are unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError("reframe profiles must be a JSON object")
    validate_reframe_profiles(payload)
    return payload


def get_reframe_profile(
    profile_id: str,
    *,
    profiles_path: str | Path | None = None,
) -> dict[str, Any]:
    catalog = load_reframe_profiles(profiles_path)
    try:
        raw = catalog["profiles"][profile_id]
    except KeyError as exc:
        raise ContractError(f"unsupported reframe profile: {profile_id}") from exc
    profile = deepcopy(raw)
    profile["profile_id"] = profile_id
    profile["catalog_version"] = catalog["version"]
    profile["profile_sha256"] = canonical_json_sha256(profile)
    return profile


def validate_reframe_profiles(payload: dict[str, Any]) -> None:
    if payload.get("schema") != REFRAME_PROFILES_SCHEMA:
        raise ContractError("unsupported reframe profiles schema")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(payload.get("version") or "")):
        raise ContractError("reframe profiles require a semantic version")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ContractError("reframe profiles require at least one profile")
    for profile_id, raw in profiles.items():
        if not str(profile_id).strip() or not isinstance(raw, dict):
            raise ContractError("reframe profile identity is invalid")
        width = _positive_int(raw.get("width"), f"{profile_id} width")
        height = _positive_int(raw.get("height"), f"{profile_id} height")
        expected_ratio = _parse_ratio(raw.get("aspect_ratio"))
        if abs(width / height - expected_ratio) > 0.001:
            raise ContractError(f"{profile_id} dimensions drift from aspect ratio")
        safe = raw.get("safe_zones")
        if not isinstance(safe, dict) or set(safe) != {
            "top",
            "right",
            "bottom",
            "left",
        }:
            raise ContractError(f"{profile_id} safe zones are incomplete")
        for edge, value in safe.items():
            number = _fraction(value, f"{profile_id} safe zone {edge}")
            if number >= 0.5:
                raise ContractError(f"{profile_id} safe zone {edge} is excessive")
        if safe["top"] + safe["bottom"] >= 0.8:
            raise ContractError(
                f"{profile_id} vertical safe zones leave no usable frame"
            )
        if safe["left"] + safe["right"] >= 0.8:
            raise ContractError(
                f"{profile_id} horizontal safe zones leave no usable frame"
            )
        _fraction(raw.get("face_target_y"), f"{profile_id} face target")
        _fraction(
            raw.get("minimum_face_height_ratio"),
            f"{profile_id} minimum face height ratio",
        )
        _fraction(
            raw.get("face_headroom_ratio", 0.0), f"{profile_id} face headroom ratio"
        )
        if not str(raw.get("effective_date") or "").strip():
            raise ContractError(f"{profile_id} effective date is required")
    policy = payload.get("policy")
    expected = {
        "profiles_are_versioned_candidates": True,
        "platform_ui_may_change": True,
        "independent_composition_per_ratio": True,
        "mechanical_cross_ratio_crop_forbidden": True,
        "automatic_publish": False,
    }
    if policy != expected:
        raise ContractError("reframe profile policy drifted")


def _parse_ratio(value: Any) -> float:
    match = re.fullmatch(r"(\d+):(\d+)", str(value or ""))
    if not match or int(match.group(2)) == 0:
        raise ContractError("aspect ratio must use positive W:H notation")
    return int(match.group(1)) / int(match.group(2))


def _positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be an integer") from exc
    if number <= 0:
        raise ContractError(f"{label} must be positive")
    return number


def _fraction(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not 0 <= number <= 1:
        raise ContractError(f"{label} must be within [0, 1]")
    return number
