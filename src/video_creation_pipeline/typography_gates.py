"""Canonical typography-gate loader and validator.

Intent: enforce role-aware, mobile-readable type minima before rendering.
Decision: bind each renderer plan to a profile copied exactly from the canonical JSON,
while leaving each renderer free to choose an actual size at or above that minimum.
Reason: plans may not silently substitute smaller text to preserve a crowded composition;
the minimum is not a ceiling or a fixed rendered value.
Evidence: the policy file records the approved vertical HTML and cover QA sources.
Effective scope: local candidate cover and semantic-motion plans only; no publication authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract_io import file_sha256, read_json_object
from .errors import ContractError


TYPOGRAPHY_GATES_SCHEMA = "lucas.typography_gates.v1"
TYPOGRAPHY_GATES_PATH = Path(__file__).with_name("policies") / "typography-gates.v1.json"


def default_typography_gates_path() -> Path:
    """Return the immutable local policy path used by all MVP consumers."""

    return TYPOGRAPHY_GATES_PATH


def build_typography_gate(profile_id: str, *, width: int, height: int) -> dict[str, Any]:
    """Bind exact minimum values to one canvas; actual renderer type may be larger."""

    policy = _load_policy()
    profiles = _object(policy.get("profiles"), "typography profiles")
    profile = _object(profiles.get(profile_id), f"typography profile {profile_id}")
    viewport = _object(profile.get("viewport"), f"typography viewport {profile_id}")
    expected_width = _integer(viewport.get("width"), "typography viewport width")
    expected_height = _integer(viewport.get("height"), "typography viewport height")
    if (width, height) != (expected_width, expected_height):
        raise ContractError(
            f"typography profile {profile_id} requires {expected_width}x{expected_height}, got {width}x{height}"
        )
    if profile.get("hard_gate") is not True:
        raise ContractError(f"typography profile {profile_id} is not fail-closed")
    preview = _object(profile.get("preview"), f"typography preview {profile_id}")
    roles = _normalized_roles(_object(profile.get("roles"), f"typography roles {profile_id}"))
    return {
        "policy_path": str(TYPOGRAPHY_GATES_PATH),
        "policy_sha256": file_sha256(TYPOGRAPHY_GATES_PATH),
        "profile_id": profile_id,
        "viewport": {"width": expected_width, "height": expected_height},
        "preview": {
            "width": _integer(preview.get("width"), "typography preview width"),
            "height": _integer(preview.get("height"), "typography preview height"),
        },
        "hard_gate": True,
        "roles": roles,
    }


def validate_typography_gate(
    gate: dict[str, Any] | None,
    *,
    profile_id: str,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Fail closed on binding drift; actual renderer sizes are checked separately >= minima."""

    if not isinstance(gate, dict):
        raise ContractError("typography gate binding is required")
    expected = build_typography_gate(profile_id, width=width, height=height)
    if gate != expected:
        raise ContractError("typography gate binding drifted from the canonical policy")
    return expected


def _load_policy() -> dict[str, Any]:
    policy = read_json_object(TYPOGRAPHY_GATES_PATH, "typography gates policy")
    if policy.get("schema") != TYPOGRAPHY_GATES_SCHEMA:
        raise ContractError("unsupported typography gates policy schema")
    if not isinstance(policy.get("version"), str) or not policy["version"].strip():
        raise ContractError("typography gates policy version is invalid")
    _object(policy.get("governance"), "typography policy governance")
    return policy


def _normalized_roles(roles: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for role, raw in roles.items():
        name = str(role).strip()
        if not name:
            raise ContractError("typography role name is invalid")
        entry = _object(raw, f"typography role {name}")
        result[name] = _integer(entry.get("min_font_px"), f"typography role {name} minimum")
        if result[name] < 1:
            raise ContractError(f"typography role {name} minimum must be positive")
    if not result:
        raise ContractError("typography roles are required")
    return result


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be an integer") from exc
    if float(number) != float(value):
        raise ContractError(f"{label} must be an integer")
    return number
