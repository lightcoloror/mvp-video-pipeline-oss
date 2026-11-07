from __future__ import annotations

import math
from collections.abc import Mapping
from itertools import combinations
from typing import Any

from .contract_io import payload_sha256
from .errors import ContractError
from .music_timing_qa import spoken_bgm_policy

MUSIC_ARRANGEMENT_REPORT_SCHEMA = "video_creation_pipeline.music_arrangement_report.v1"


def build_music_arrangement_report(
    layers: list[dict[str, Any]],
    asset_sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Audit the whole video's BGM, including split clips and aliased assets.

    Identity is source SHA plus the actually used source interval. This is a
    metadata guard, not acoustic recognition of repeated motifs inside a file.
    Weighted source intervals avoid expanding arbitrarily long explicit loops.
    """
    policy = dict(spoken_bgm_policy()["arrangement_variety"])
    if not isinstance(layers, list) or not isinstance(asset_sources, Mapping):
        raise ContractError("music arrangement requires layers and asset_sources")
    groups: dict[str, dict[str, Any]] = {}
    timeline: list[tuple[float, float]] = []
    normalized = []
    for index, layer in enumerate(layers):
        if not isinstance(layer, Mapping):
            raise ContractError("music arrangement layer must be an object")
        asset_id = str(layer.get("asset_id") or "")
        raw = asset_sources.get(asset_id)
        if not isinstance(raw, Mapping):
            raise ContractError(f"music arrangement has no asset: {asset_id}")
        asset = _normalize_asset(asset_id, raw)
        start = _nonnegative(layer.get("source_start_s", 0), "source start")
        end = _positive(layer.get("source_end_s", asset["duration_s"]), "source end")
        duration = _positive(layer.get("duration_s"), "layer duration")
        at = _nonnegative(layer.get("timeline_start_s"), "timeline start")
        loop = layer.get("loop", False)
        if not isinstance(loop, bool):
            raise ContractError("music arrangement loop must be boolean")
        if not start < end <= asset["duration_s"] + 0.001:
            raise ContractError("music arrangement source interval is outside asset")
        width = end - start
        if not loop and duration > width + 0.001:
            raise ContractError("non-looping music arrangement exceeds source window")
        # Sub-millisecond rounding is tolerated, not treated as an extra pass.
        played = duration if loop else min(duration, width)
        group = groups.setdefault(asset["sha256"], {
            "asset_ids": set(), "duration_s": asset["duration_s"],
            "played_s": 0.0, "events": {},
        })
        if abs(group["duration_s"] - asset["duration_s"]) > 0.001:
            raise ContractError("same music SHA has conflicting duration metadata")
        group["asset_ids"].add(asset_id)
        group["played_s"] += played
        full_passes = int(played // width)
        remainder = played - full_passes * width
        events = group["events"]
        for a, b, weight in ((start, end, full_passes),
                             (start, start + remainder, 1)):
            if weight and b - a > 1e-9:
                a, b = round(a, 9), round(b, 9)
                events[a] = events.get(a, 0) + weight
                events[b] = events.get(b, 0) - weight
        timeline.append((at, at + duration))
        normalized.append({
            "layer_index": index, "segment_id": layer.get("segment_id", str(index)),
            "asset_id": asset_id, "sha256": asset["sha256"],
            "source_start_s": start, "source_end_s": end,
            "duration_s": duration, "timeline_start_s": at, "loop": loop,
        })

    coverage = 0.0
    union_end = 0.0
    for a, b in sorted(timeline):
        coverage += max(0.0, b - max(a, union_end))
        union_end = max(union_end, b)
    total_played = sum(group["played_s"] for group in groups.values())
    sources, violations = [], []
    for sha, group in sorted(groups.items()):
        active, maximum, previous = 0, 0, 0.0
        unique, excess = 0.0, 0.0
        for position, delta in sorted(group["events"].items()):
            span = position - previous
            if active > 0:
                unique += span
                maximum = max(maximum, active)
                excess += span * max(0, active - policy["max_source_passes"])
            active += delta
            previous = position
        share = group["played_s"] / total_played
        required_unique = min(policy["long_source_target_s"],
                              coverage * policy["minimum_unique_fraction"])
        row = {
            "sha256": sha, "asset_ids": sorted(group["asset_ids"]),
            "played_s": round(group["played_s"], 6),
            "unique_source_s": round(unique, 6), "max_source_passes": maximum,
            "excess_repeat_s": round(excess, 6), "play_share": round(share, 6),
        }
        sources.append(row)
        if excess > policy["repeat_tolerance_s"] + 1e-6:
            violations.append({"code": "source_passage_repeated_too_often", **row})
        if (coverage > policy["short_program_exemption_s"]
                and share >= policy["dominant_source_fraction"]
                and unique + policy["repeat_tolerance_s"] < required_unique):
            violations.append({"code": "dominant_short_source", **row,
                               "required_unique_source_s": round(required_unique, 6)})
    report = {
        "schema": MUSIC_ARRANGEMENT_REPORT_SCHEMA,
        "status": "passed" if not violations else "blocked_repetitive_arrangement",
        "policy": policy,
        "identity_basis": "audio_sha256_plus_full_used_source_intervals",
        "music_coverage_s": round(coverage, 6), "layers": normalized,
        "sources": sources, "violations": violations,
        "readiness": {"candidate_render_allowed": not violations},
        "remediation": ([] if not violations else [
            "Use a longer source, different arrangements from the same family, "
            "or non-repeating passages from a long source; keep semantic boundaries."
        ]),
        "scope": "metadata_only; listening and final mix QA remain required",
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    return report

MUSIC_BATCH_DIVERSITY_REPORT_SCHEMA = (
    "video_creation_pipeline.music_batch_diversity_report.v1"
)
MUSIC_BATCH_ALIGNMENT_REPORT_SCHEMA = (
    "video_creation_pipeline.music_batch_alignment_report.v1"
)


def build_music_batch_alignment_report(
    plans: Mapping[str, Mapping[str, Any]],
    asset_families: Mapping[str, str],
) -> dict[str, Any]:
    """Fail closed when a layer's selected asset contradicts its semantic family."""

    if not plans:
        raise ContractError("music alignment audit requires at least one plan")
    allowed_families = {"warm_story", "clear_explainer", "restrained_risk"}
    normalized_assets: dict[str, str] = {}
    for asset_id, family_id in asset_families.items():
        normalized_family = str(family_id).strip()
        if normalized_family not in allowed_families:
            raise ContractError(f"music asset has an invalid family: {asset_id}")
        normalized_assets[str(asset_id)] = normalized_family

    checks: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for video_id in sorted(plans):
        layers = plans[video_id].get("layers")
        if not isinstance(layers, list) or not layers:
            raise ContractError(f"music plan has no layers: {video_id}")
        for layer_index, raw in enumerate(layers):
            if not isinstance(raw, Mapping):
                raise ContractError(
                    f"music layer is not an object: {video_id}:{layer_index}"
                )
            asset_id = str(raw.get("asset_id") or "").strip()
            asset_family = normalized_assets.get(asset_id)
            if asset_family is None:
                raise ContractError(
                    f"music layer references an unknown asset: {asset_id}"
                )
            family_value = raw.get("music_family")
            if isinstance(family_value, Mapping):
                semantic_family = str(family_value.get("family_id") or "").strip()
            else:
                semantic_family = str(family_value or "").strip()
            if semantic_family not in allowed_families:
                raise ContractError(
                    f"music layer has no valid semantic family: {video_id}:{layer_index}"
                )
            check = {
                "video_id": str(video_id),
                "layer_index": layer_index,
                "segment_id": str(raw.get("segment_id") or ""),
                "asset_id": asset_id,
                "asset_family": asset_family,
                "semantic_family": semantic_family,
                "aligned": asset_family == semantic_family,
            }
            checks.append(check)
            if not check["aligned"]:
                violations.append(check)

    report: dict[str, Any] = {
        "schema": MUSIC_BATCH_ALIGNMENT_REPORT_SCHEMA,
        "status": "passed" if not violations else "blocked_family_mismatch",
        "policy": {
            "asset_family_must_equal_semantic_family": True,
            "silent_cross_family_fallback_allowed": False,
        },
        "video_count": len(plans),
        "layer_count": len(checks),
        "checks": checks,
        "violations": violations,
        "readiness": {
            "candidate_render_allowed": not violations,
            "violation_count": len(violations),
        },
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    return report


def build_music_batch_diversity_report(
    plans: Mapping[str, Mapping[str, Any]],
    asset_sources: Mapping[str, Mapping[str, Any]],
    *,
    max_shared_body_source_s: float = 4.0,
    brand_motif_max_s: float = 10.0,
    comparison_horizon_s: float = 24.0,
) -> dict[str, Any]:
    """Find repeated source passages across a batch of music plans.

    Asset ids are deliberately not identities. Two aliases that bind the same
    audio SHA and overlapping source interval are the same musical passage.
    """

    if not plans:
        raise ContractError("music diversity audit requires at least one plan")
    shared_limit = _nonnegative(max_shared_body_source_s, "shared body source limit")
    motif_limit = _positive(brand_motif_max_s, "brand motif duration limit")
    horizon = _positive(comparison_horizon_s, "music comparison horizon")
    normalized_assets = {
        str(asset_id): _normalize_asset(asset_id, source)
        for asset_id, source in asset_sources.items()
    }

    regions: list[dict[str, Any]] = []
    for video_id in sorted(plans):
        plan = plans[video_id]
        layers = plan.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ContractError(f"music plan has no layers: {video_id}")
        for index, raw in enumerate(layers):
            if not isinstance(raw, Mapping):
                raise ContractError(f"music layer is not an object: {video_id}:{index}")
            asset_id = str(raw.get("asset_id") or "").strip()
            asset = normalized_assets.get(asset_id)
            if asset is None:
                raise ContractError(
                    f"music layer references an unknown asset: {asset_id}"
                )
            duration_s = _positive(raw.get("duration_s"), "music layer duration")
            source_start_s = _nonnegative(
                raw.get("source_start_s", 0.0), "music layer source start"
            )
            if source_start_s >= asset["duration_s"]:
                raise ContractError(f"music layer starts after source end: {asset_id}")
            loop = raw.get("loop") is True
            if not loop and source_start_s + duration_s > asset["duration_s"] + 0.001:
                raise ContractError(
                    f"music layer exceeds a non-looping source: {asset_id}"
                )
            semantic_role = str(raw.get("semantic_role") or "unknown")
            brand_motif = raw.get("brand_motif") is True
            if brand_motif and duration_s > motif_limit + 0.001:
                raise ContractError(
                    f"brand motif exceeds {motif_limit:g}s: {video_id}:{semantic_role}"
                )
            regions.append(
                {
                    "video_id": video_id,
                    "layer_index": index,
                    "asset_id": asset_id,
                    "asset_sha256": asset["sha256"],
                    "asset_duration_s": asset["duration_s"],
                    "source_start_s": round(source_start_s, 6),
                    "source_end_s": round(
                        min(
                            asset["duration_s"],
                            source_start_s + min(duration_s, horizon),
                        ),
                        6,
                    ),
                    "source_intervals": _source_intervals(
                        source_start_s=source_start_s,
                        duration_s=min(duration_s, horizon),
                        asset_duration_s=asset["duration_s"],
                        loop=loop,
                    ),
                    "audited_duration_s": round(min(duration_s, horizon), 6),
                    "duration_s": round(duration_s, 6),
                    "loop": loop,
                    "semantic_role": semantic_role,
                    "brand_motif": brand_motif,
                }
            )

    findings: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for left, right in combinations(regions, 2):
        if left["video_id"] == right["video_id"]:
            continue
        if left["asset_sha256"] != right["asset_sha256"]:
            continue
        shared_s = _shared_source_duration(left, right)
        if shared_s <= 0.0:
            continue
        allowed_brand_motif = bool(left["brand_motif"] and right["brand_motif"])
        finding = {
            "left": _region_reference(left),
            "right": _region_reference(right),
            "shared_source_s": round(shared_s, 6),
            "same_audio_sha256": True,
            "allowed_brand_motif": allowed_brand_motif,
        }
        findings.append(finding)
        if not allowed_brand_motif and shared_s > shared_limit + 0.001:
            violations.append(finding)

    reuse_groups: list[dict[str, Any]] = []
    by_sha: dict[str, set[str]] = {}
    for region in regions:
        by_sha.setdefault(str(region["asset_sha256"]), set()).add(
            str(region["video_id"])
        )
    for sha256, video_ids in sorted(by_sha.items()):
        if len(video_ids) > 1:
            reuse_groups.append(
                {
                    "asset_sha256": sha256,
                    "video_ids": sorted(video_ids),
                    "status": "same_family_asset_reused_but_opening_passage_differs"
                    if not any(
                        finding["left"]["video_id"] in video_ids
                        and finding["right"]["video_id"] in video_ids
                        and not finding["allowed_brand_motif"]
                        for finding in violations
                    )
                    else "repeated_opening_passage_blocked",
                }
            )

    report: dict[str, Any] = {
        "schema": MUSIC_BATCH_DIVERSITY_REPORT_SCHEMA,
        "status": "passed" if not violations else "blocked_repeated_source_passage",
        "policy": {
            "identity_basis": "audio_sha256_plus_source_interval",
            "asset_aliases_count_as_diversity": False,
            "max_shared_body_source_s": shared_limit,
            "brand_motif_max_s": motif_limit,
            "comparison_horizon_s": horizon,
            "looped_audio_is_compared_by_initial_phase_window": True,
        },
        "video_count": len(plans),
        "layer_count": len(regions),
        "regions": regions,
        "findings": findings,
        "violations": violations,
        "asset_reuse_groups": reuse_groups,
        "readiness": {
            "candidate_render_allowed": not violations,
            "violation_count": len(violations),
        },
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    return report


def _normalize_asset(asset_id: object, raw: Mapping[str, Any]) -> dict[str, Any]:
    sha256 = str(raw.get("sha256") or "").strip().lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ContractError(f"music asset has an invalid SHA-256: {asset_id}")
    return {
        "sha256": sha256,
        "duration_s": _positive(raw.get("duration_s"), "music asset duration"),
    }


def _shared_source_duration(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    total = 0.0
    for left_interval in left["source_intervals"]:
        for right_interval in right["source_intervals"]:
            total += max(
                0.0,
                min(float(left_interval[1]), float(right_interval[1]))
                - max(float(left_interval[0]), float(right_interval[0])),
            )
    return min(
        total,
        float(left["audited_duration_s"]),
        float(right["audited_duration_s"]),
    )


def _source_intervals(
    *,
    source_start_s: float,
    duration_s: float,
    asset_duration_s: float,
    loop: bool,
) -> list[list[float]]:
    end_s = source_start_s + duration_s
    if end_s <= asset_duration_s + 0.001:
        return [[round(source_start_s, 6), round(min(end_s, asset_duration_s), 6)]]
    if not loop:
        raise ContractError("non-looping music audit interval exceeds the source")
    overflow_s = end_s - asset_duration_s
    if overflow_s > asset_duration_s + 0.001:
        return [[0.0, round(asset_duration_s, 6)]]
    return [
        [round(source_start_s, 6), round(asset_duration_s, 6)],
        [0.0, round(overflow_s, 6)],
    ]


def _region_reference(region: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "video_id": region["video_id"],
        "layer_index": region["layer_index"],
        "asset_id": region["asset_id"],
        "semantic_role": region["semantic_role"],
        "source_start_s": region["source_start_s"],
        "source_end_s": region["source_end_s"],
        "loop": region["loop"],
    }


def _nonnegative(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number < 0.0:
        raise ContractError(f"{label} must be finite and non-negative")
    return number


def _positive(value: object, label: str) -> float:
    number = _nonnegative(value, label)
    if number <= 0.0:
        raise ContractError(f"{label} must be positive")
    return number
