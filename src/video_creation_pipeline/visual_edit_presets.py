"""Validate deliberate cuts and bounded emphasis on the existing scene clock."""

from __future__ import annotations

from .editing_presets import POLICY, interval, overlaps
from .errors import ContractError


def visual_defaults(row, start, end):
    if row.get("cut_basis", "semantic") not in (
        "semantic",
        "action",
        "gaze",
        "reaction",
        "location",
        "time",
        "structure",
    ):
        raise ContractError(
            "fixed-interval or keyword-only cutting is not an editorial preset"
        )
    transition = row.get("transition", {"kind": "cut", "reason": ""})
    if not isinstance(transition, dict) or transition.get("kind") not in (
        "cut",
        "dissolve",
        "fade",
        "match",
        "custom",
    ):
        raise ContractError("unsupported transition choice")
    if transition["kind"] != "cut" and not str(transition.get("reason", "")).strip():
        raise ContractError("non-cut transition needs a narrative reason")
    if row.get("whole_sentence_continuous_motion", False) is not False:
        raise ContractError("continuous whole-sentence subtitle motion is disabled")
    events = row.get("emphasis_events", [])
    if not isinstance(events, list):
        raise ContractError("emphasis events must be a list")
    for event in events:
        a, b = interval(event)
        if not start <= a < b <= end:
            raise ContractError("emphasis outside scene")
        if not str(event.get("semantic_trigger", "")).strip():
            raise ContractError("emphasis requires a semantic trigger")
        if event.get("role", "primary") not in ("primary", "secondary"):
            raise ContractError("invalid emphasis role")
    primary = [e for e in events if e.get("role", "primary") == "primary"]
    if any(
        overlaps(*interval(a), *interval(b))
        for i, a in enumerate(primary)
        for b in primary[i + 1 :]
    ):
        raise ContractError("only one primary emphasis may run at a time")
    return {
        "cut_basis": row.get("cut_basis", "semantic"),
        "transition": dict(transition),
        "emphasis_events": events,
        "whole_sentence_continuous_motion": False,
        "policy_version": POLICY["version"],
    }
