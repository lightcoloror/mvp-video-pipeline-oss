"""Bind sync-dependent data to source order, trims, speed and output timing."""

from __future__ import annotations

from .contract_io import canonical_json_sha256
from .editing_presets import POLICY, number, reviewed
from .errors import ContractError


def fingerprint(mapping):
    # 0 and 0.0 describe the same time. Preserve order and all source identity
    # fields, normalizing only the four numeric boundaries before hashing.
    rows = [
        {
            **row,
            **{
                key: number(row[key], key)
                for key in ("source_start", "source_end", "output_start", "output_end")
            },
        }
        for row in mapping
    ]
    return canonical_json_sha256(rows)


def validate_speed(mapping, review=None):
    changed = []
    for i, row in enumerate(mapping):
        source = number(row["source_end"]) - number(row["source_start"])
        output = number(row["output_end"]) - number(row["output_start"])
        if source <= 0 or output <= 0:
            raise ContractError("invalid speed mapping")
        if abs(source / output - POLICY["timebase"]["default_speed"]) > 1e-6:
            changed.append(i)
    if changed:
        reviewed(review, ["audio_checked", "meaning_preserved"], "speed change")
        if review.get("mapping_sha256") != fingerprint(mapping):
            raise ContractError("speed review belongs to an outdated timeline")


def dependency_state(mapping, previous=None):
    current = fingerprint(mapping)
    valid = isinstance(previous, dict) and previous.get("timeline_sha256") == current
    if valid and (
        not isinstance(previous.get("roles"), dict)
        or any(
            v not in ("pending", "verified", "not_used")
            for v in previous["roles"].values()
        )
    ):
        raise ContractError("invalid timeline dependency status")
    return {
        "timeline_sha256": current,
        "roles": {
            role: previous.get("roles", {}).get(role, "pending") if valid else "pending"
            for role in POLICY["timebase"]["dependent_roles"]
        },
    }


def validate_dependencies(mapping, state, required=()):
    expected = dependency_state(mapping, state)
    if state != expected:
        raise ContractError("sync dependencies are stale after timeline change")
    if any(
        expected["roles"].get(role) not in ("verified", "not_used") for role in required
    ):
        raise ContractError("sync-dependent output requires fresh verification")
