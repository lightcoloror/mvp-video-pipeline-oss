"""Shared talking-head pause preset: suggestions are not edit authorization."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import ContractError

DEFAULT_NATURAL_PACING_POLICY = json.loads(
    (Path(__file__).parent / "policies" / "spoken-pacing.v3.json").read_text("utf-8")
)
LEGACY_PACING_V2 = json.loads(
    (Path(__file__).parent / "policies" / "spoken-pacing.v2.json").read_text("utf-8")
)
PROTECTED_PAUSE_ROLES = frozenset(DEFAULT_NATURAL_PACING_POLICY["protected_pause_roles"])
PAUSE_LOCATIONS = {"leading", "within_sentence", "between_sentences"}


def _timing(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("pause timing must be numeric") from exc
    if isinstance(value, bool) or not math.isfinite(number) or number < 0:
        raise ContractError("pause timing must be finite and nonnegative")
    return number


def pause_recommendation(start: float, end: float, where: str, role: str = "unknown") -> dict[str, Any]:
    policy = DEFAULT_NATURAL_PACING_POLICY
    if role in PROTECTED_PAUSE_ROLES or where not in PAUSE_LOCATIONS:
        return {"action": "keep", "reason": "保留语义停顿或尚未确定边界的间隔"}
    key = "leading_pause" if where == "leading" else where
    if end - start + 1e-9 < policy[f"{key}_compress_from_s"]:
        return {"action": "keep", "reason": "短停顿保留原长，不补长也不压缩"}
    return {
        "action": "compress",
        "preserve_s": policy[f"{key}_preserve_s"],
        "reason": "仅建议压缩；须回听确认普通空隙、完整尾音和下一词起音",
    }


def pause_cut_window(start: float, end: float, preserve_s: float, where: str) -> tuple[float, float]:
    """Millisecond cuts rounded inward, with separate tail and onset protection."""
    start, end, preserve_s = map(_timing, (start, end, preserve_s))
    policy = DEFAULT_NATURAL_PACING_POLICY
    if where not in PAUSE_LOCATIONS or end <= start or not 0 < preserve_s < end - start:
        raise ContractError("pause compression interval or location is invalid")
    if where == "leading":
        if preserve_s < policy["leading_pause_preserve_s"] - 1e-9:
            raise ContractError("leading pause preservation is below preset guard")
        left, right = 0.0, preserve_s
    else:
        left = max(policy["word_tail_guard_s"], preserve_s / 2)
        right = preserve_s - left
        if right < policy["word_onset_guard_s"] - 1e-9:
            raise ContractError("pause compression violates tail/onset guards")
    a = math.ceil((start + left - 1e-9) * 1000) / 1000
    b = math.floor((end - right + 1e-9) * 1000) / 1000
    if b <= a:
        raise ContractError("pause has no removable middle after guards")
    return a, b


def validate_pause_compression(start: float, end: float, preserve_s: float, where: str, review: Any) -> None:
    start, end, preserve_s = map(_timing, (start, end, preserve_s))
    if not isinstance(review, dict) or not (
        review.get("audio_checked") is True
        and review.get("context_checked") is True
        and review.get("semantic_role") == "ordinary"
        and isinstance(review.get("evidence"), str)
        and review["evidence"].strip()
    ):
        raise ContractError("pause compression requires explicit audio/context review of an ordinary gap")
    if where == "between_sentences" and preserve_s < DEFAULT_NATURAL_PACING_POLICY["between_sentences_preserve_range_s"][0]:
        if not isinstance(review.get("override_reason"), str) or not review["override_reason"].strip():
            raise ContractError("shorter sentence gap requires a reviewed pacing override_reason")
    pause_cut_window(start, end, preserve_s, where)


def validate_pause_decision(row: dict[str, Any], candidate: dict[str, Any], tokens: list[dict[str, Any]]) -> None:
    if row.get("kind") != candidate.get("kind"):
        raise ContractError("objective decision kind does not match its candidate")
    if candidate["kind"] != "pause":
        return
    if any(abs(float(row[k]) - float(candidate[k])) > 1e-7 for k in ("start", "end")):
        raise ContractError("pause decision must retain the candidate interval")
    if row.get("where") != candidate.get("where", ""):
        raise ContractError("pause location must match candidate evidence")
    review = row.get("pause_review") or {}
    if not isinstance(review, dict):
        raise ContractError("pause_review must be an object")
    allowed_roles = PROTECTED_PAUSE_ROLES | {"ordinary", "unknown"}
    candidate_role = candidate.get("pause_role", "unknown")
    reviewed_role = review.get("semantic_role", "unknown")
    if any(not isinstance(role, str) or role not in allowed_roles for role in (candidate_role, reviewed_role)):
        raise ContractError("invalid pause semantic role")
    roles = {candidate_role, reviewed_role}
    if roles & PROTECTED_PAUSE_ROLES and row["action"] not in {"keep", "review_required"}:
        raise ContractError("protected semantic pause must keep its original duration")
    if row["action"] == "delete":
        raise ContractError("pause deletion is forbidden; review and compress with guards instead")
    if row["action"] != "compress":
        return
    validate_pause_compression(row["start"], row["end"], row["preserve_s"], row["where"], review)
    a, b = pause_cut_window(row["start"], row["end"], row["preserve_s"], row["where"])
    if any(not token["isGap"] and min(b, float(token["end"])) > max(a, float(token["start"])) + 1e-7 for token in tokens):
        raise ContractError("pause compression would overlap a spoken token")


def bound_snapped_pauses(snapped: list[dict[str, Any]], prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows = {row["id"]: row for row in prepared if row["kind"] == "pause"}
    seen = set()
    result = []
    for row in snapped:
        row = dict(row)
        window = windows.get(row.get("id"))
        if window:
            if row.get("kind") != "pause" or row["id"] in seen:
                raise ContractError("snapping changed pause identity")
            seen.add(row["id"])
            # The pinned auto_cut writes centiseconds. Round inward first so
            # its later nearest rounding cannot eat a few ms of the guard.
            row["start"] = math.ceil((max(row["start"], window["start"]) - 1e-9) * 100) / 100
            row["end"] = math.floor((min(row["end"], window["end"]) + 1e-9) * 100) / 100
            if row["end"] <= row["start"]:
                raise ContractError("snapping leaves no safe pause deletion")
        result.append(row)
    if seen != set(windows):
        raise ContractError("snapping omitted a reviewed pause")
    return result


def validate_compiled_pause_bounds(decisions: list[dict[str, Any]], deletes: list[dict[str, float]]) -> None:
    """Check actual compiled ranges; a merger/snapper cannot consume kept pause edges."""
    for row in decisions:
        if row["kind"] != "pause":
            continue
        start, end = float(row["start"]), float(row["end"])
        if row["action"] == "compress":
            a, b = pause_cut_window(start, end, row["preserve_s"], row["where"])
            protected = [(start, a), (b, end)]
        elif row["action"] == "keep":
            protected = [(start, end)]
        else:
            continue
        if any(min(d["end"], b) > max(d["start"], a) + 1e-7 for a, b in protected for d in deletes):
            raise ContractError("compiled deletion consumes a protected pause or tail/onset guard")
