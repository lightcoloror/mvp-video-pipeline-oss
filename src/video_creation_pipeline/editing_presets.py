"""Shared editorial protections used by existing cutters, not a new editor."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .errors import ContractError

POLICY = json.loads(
    (Path(__file__).parent / "policies/editing-presets.v1.json").read_text("utf-8")
)


def number(value, label="time"):
    if isinstance(value, bool):
        raise ContractError(f"{label} must be numeric")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not math.isfinite(value):
        raise ContractError(f"{label} must be finite")
    return value


def interval(row):
    if not isinstance(row, dict):
        raise ContractError("interval must be an object")
    a, b = number(row.get("start")), number(row.get("end"))
    if not 0 <= a < b:
        raise ContractError("invalid editorial interval")
    return a, b


def overlaps(a, b, c, d):
    return min(b, d) > max(a, c) + 1e-7


def reviewed(value, fields, label):
    if not isinstance(value, dict) or any(value.get(k) is not True for k in fields):
        raise ContractError(f"{label} requires explicit review: {', '.join(fields)}")
    if not isinstance(value.get("evidence"), str) or not value["evidence"].strip():
        raise ContractError(f"{label} requires review evidence")


def take_rank(take):
    """Rank supplied assessments only; missing ratings do not become measurements."""
    values = [take.get("complete") is True]
    for key in POLICY["take_selection"]["order"][1:]:
        value = number(take.get(key, 0), key)
        if not 0 <= value <= 1:
            raise ContractError(f"{key} must be between zero and one")
        values.append(value)
    return tuple(values)


def preferred_take(takes):
    """Last recording is the ordinary candidate; explicit problems trigger comparison."""
    if not takes:
        raise ContractError("take group is empty")
    ordered = sorted(takes, key=lambda r: interval(r)[0])
    for take in ordered:
        take_rank(take)
        flags = take.get("review_flags", [])
        if not isinstance(flags, list) or any(
            f not in POLICY["take_selection"]["exception_flags"] for f in flags
        ):
            raise ContractError("unsupported take review flags")
    last = ordered[-1]
    flags = last.get("review_flags", [])
    if not isinstance(flags, list) or any(
        f not in POLICY["take_selection"]["exception_flags"] for f in flags
    ):
        raise ContractError("unsupported take review flags")
    if last.get("complete") is not False and not flags:
        return last
    # A visible exception is evidence to compare; this still does not authorize a cut.
    viable = [
        r
        for r in ordered
        if r.get("complete") is not False
        and not set(r.get("review_flags", []))
        & {"incomplete", "misstatement", "quality_problem"}
    ]
    return max(viable or ordered, key=lambda r: (take_rank(r), interval(r)[0]))


def validate_take_review(review, removed, deletes=()):
    reviewed(
        review,
        [
            "complete_checked",
            "meaning_checked",
            "natural_delivery_checked",
            "quality_checked",
            "unique_information_accounted",
        ],
        "take meaning comparison",
    )
    retained = interval(review.get("retained_interval"))
    comparisons = review.get("compared_intervals")
    if not isinstance(comparisons, list) or len(comparisons) < 2:
        raise ContractError("take comparison requires both source intervals")
    compared = [interval(r) for r in comparisons]
    a, b = interval(removed)
    if retained not in compared or not any(c <= a < b <= d for c, d in compared):
        raise ContractError(
            "take comparison does not bind removed and retained intervals"
        )
    if overlaps(a, b, *retained) or any(
        overlaps(*interval(d), *retained) for d in deletes
    ):
        raise ContractError("selected take would also be removed")


def validate_protected_events(events, duration=None):
    if not isinstance(events, list):
        raise ContractError("protected_events must be an explicit list")
    ids = set()
    for row in events:
        a, b = interval(row)
        ident = row.get("event_id")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ContractError("protected event ids must be unique")
        ids.add(ident)
        if row.get("kind") not in POLICY["boundaries"]["protected_event_kinds"]:
            raise ContractError("unsupported protected event kind")
        if duration is not None and b > duration + 1e-7:
            raise ContractError("protected event outside media")


def validate_edit_boundaries(deletes, words, events):
    validate_protected_events(events)
    for cut in deletes:
        a, b = interval(cut)
        for event in events:
            if overlaps(a, b, *interval(event)):
                raise ContractError(
                    "deletion consumes a protected response or source event"
                )
        for word in words:
            if word.get("isGap") is True:
                continue
            c, d = interval(word)
            if overlaps(a, b, c, d) and not (a <= c + 1e-7 and b >= d - 1e-7):
                raise ContractError("deletion cuts through a spoken word")


def boundary_review_template():
    return {
        "opening_checked": False,
        "ending_checked": False,
        "cut_boundaries_checked": False,
        "events_checked": False,
        "evidence": "",
    }


def validate_boundary_review(value):
    reviewed(
        value,
        [
            "opening_checked",
            "ending_checked",
            "cut_boundaries_checked",
            "events_checked",
        ],
        "source boundary and response protection",
    )


def splice_fades(keeps, words, events):
    """Tiny in-place edge fades only in unoccupied handles; never overlap takes."""
    occupied = [interval(w) for w in words if w.get("isGap") is not True]
    occupied += [interval(e) for e in events]
    default = POLICY["audio_splices"]["default_fade_s"]
    rows = []
    for i, keep in enumerate(keeps):
        a, b = interval(keep)
        duration = b - a
        fade = min(default, duration / 4)
        incoming = i > 0 and not any(overlaps(a, a + fade, c, d) for c, d in occupied)
        outgoing = i < len(keeps) - 1 and not any(
            overlaps(b - fade, b, c, d) for c, d in occupied
        )
        rows.append(
            {
                "start": a,
                "end": b,
                "fade_in_s": fade if incoming else 0.0,
                "fade_out_s": fade if outgoing else 0.0,
                "audition_required": i > 0 or i < len(keeps) - 1,
            }
        )
    return rows


def fade_filter(row):
    duration = row["end"] - row["start"]
    filters = []
    for key in ("fade_in_s", "fade_out_s"):
        amount = number(row[key], key)
        if (
            not 0
            <= amount
            <= min(POLICY["audio_splices"]["maximum_fade_s"], duration / 4) + 1e-7
        ):
            raise ContractError("unsafe splice fade duration")
        if amount:
            start = 0 if key == "fade_in_s" else duration - amount
            kind = "in" if key == "fade_in_s" else "out"
            filters.append(f"afade=t={kind}:st={start:.9f}:d={amount:.9f}")
    return ",".join(filters)


def boundary_windows(keeps, output_duration):
    radius = POLICY["boundaries"]["review_radius_s"]
    points = [("opening", 0.0), ("ending", output_duration)]
    cursor = 0.0
    for row in keeps[:-1]:
        a, b = interval(row)
        cursor += b - a
        points.append(("splice", cursor))
    return [
        {
            "kind": kind,
            "focus_s": t,
            "start_s": max(0, t - radius),
            "end_s": min(output_duration, t + radius),
            "audition_status": "pending",
        }
        for kind, t in points
    ]
