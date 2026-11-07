"""Hash-bound layout validation, including explicitly nonessential overlays."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .contract_io import file_sha256, read_json_object
from .errors import ContractError
from .privacy_exclusion_zones import (
    layout_intersection_area,
    normalize_layout_rect,
)

SCHEMA_PATH = Path(__file__).with_name("schemas") / "final_layout_safety.v1.schema.json"


def rounded_corner_regions() -> list[dict[str, Any]]:
    """Existing vertical-production corner preset; callers cannot shrink it away."""
    return [
        {"region_id": name, "region_kind": "rounded_corner", "rect": {
            "x": x, "y": y, "width": .10, "height": .075}}
        for name, x, y in (("corner-top-left", 0, 0), ("corner-top-right", .9, 0),
                           ("corner-bottom-left", 0, .925), ("corner-bottom-right", .9, .925))
    ]


def _load_json(path: Path) -> dict[str, Any]:
    return read_json_object(path, "final layout artifact")


def _verify_artifact(workspace_root: Path, artifact: dict[str, Any]) -> None:
    root = workspace_root.resolve()
    target = (root / artifact["path"]).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ContractError("final layout artifact escapes workspace") from error
    if not target.is_file():
        raise ContractError(f"final layout artifact is missing: {artifact['path']}")
    if file_sha256(target) != artifact["expected_sha256"]:
        raise ContractError(f"final layout artifact hash drifted: {artifact['path']}")


def _time_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return not (
        float(left["end_seconds"]) <= float(right["start_seconds"])
        or float(right["end_seconds"]) <= float(left["start_seconds"])
    )


def validate_final_layout(
    document: dict[str, Any], workspace_root: Path
) -> dict[str, Any]:
    schema = _load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            document
        ),
        key=lambda item: list(item.path),
    )
    if errors:
        messages = [
            f"{'.'.join(str(part) for part in error.path) or '$'}: {error.message}"
            for error in errors
        ]
        raise ContractError("final layout schema failed: " + "; ".join(messages))

    _verify_artifact(workspace_root, document["candidate"])
    _verify_artifact(workspace_root, document["layout_evidence"])
    duration = float(document["duration_seconds"])
    if not math.isfinite(duration):
        raise ContractError("final layout duration must be finite")
    reserved: list[tuple[str, str, dict[str, float]]] = []
    region_ids: set[str] = set()
    rounded_corner_count = 0
    for region in document["reserved_regions"]:
        region_id = region["region_id"]
        if region_id in region_ids:
            raise ContractError(f"duplicate final layout reserved region: {region_id}")
        region_ids.add(region_id)
        if region["region_kind"] == "rounded_corner":
            rounded_corner_count += 1
        reserved.append(
            (
                region_id,
                region["region_kind"],
                normalize_layout_rect(region["rect"], f"reserved region {region_id}"),
            )
        )
    if rounded_corner_count != 4:
        raise ContractError("final layout requires exactly four rounded-corner regions")
    # Retain larger/custom reservations while enforcing the existing minimum.
    # An auxiliary overlay may enter these regions; important content may not.
    reserved += [("preset-" + r["region_id"], r["region_kind"], r["rect"])
                 for r in rounded_corner_regions()]

    elements: list[dict[str, Any]] = []
    element_ids: set[str] = set()
    present_roles: set[str] = set()
    auxiliary_overlaps: list[dict[str, str]] = []
    for element in document["elements"]:
        element_id = element["element_id"]
        if element_id in element_ids:
            raise ContractError(f"duplicate final layout element: {element_id}")
        element_ids.add(element_id)
        start = float(element["start_seconds"])
        end = float(element["end_seconds"])
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or end <= start
            or end > duration + 1e-6
        ):
            raise ContractError(
                f"final layout element time range is invalid: {element_id}"
            )
        normalized = {
            **element,
            "rect": normalize_layout_rect(element["rect"], f"element {element_id}"),
        }
        if not element["critical_content"] and not element["auxiliary_visibility"]["reason"].strip():
            raise ContractError(f"auxiliary visibility reason is empty: {element_id}")
        present_roles.add(element["role"])
        for region_id, region_kind, region_rect in reserved:
            if layout_intersection_area(normalized["rect"], region_rect) > 0:
                if not element["critical_content"] and region_kind in {"rounded_corner", "platform_overlay"}:
                    auxiliary_overlaps.append({"element_id": element_id, "region_id": region_id})
                    continue
                raise ContractError(
                    f"final layout element {element_id} enters reserved region {region_id}"
                )
        elements.append(normalized)

    # User's general production preference, independent of insurance identity.
    # Omission is default-on; only an explicit no-AI record can opt out.
    disclosure = document.get("ai_disclosure", {
        "mode": "default_required", "stages": [],
        "reason": "Creator requires an AI disclosure by default for all videos",
    })
    if not disclosure["reason"].strip():
        raise ContractError("AI disclosure reason must not be blank")
    ai_required = disclosure["mode"] != "no_ai"
    required_roles = set(document["required_roles"])
    if ai_required:
        required_roles.add("ai_label")
    missing_roles = required_roles - present_roles
    if missing_roles:
        raise ContractError(
            "final layout required roles are missing: "
            + ", ".join(sorted(missing_roles))
        )
    labels = [e for e in elements if e["role"] == "ai_label"]
    for label in labels:
        if not re.search(r"(?i)(?<![a-z])ai(?![a-z])|人工智能", label["text"]):
            raise ContractError("AI label must explicitly name AI or 人工智能")
    if disclosure["mode"] == "no_ai" and labels:
        raise ContractError("no_ai record contradicts visible AI labels")
    if ai_required:
        covered_until = 0.0
        for label in sorted(labels, key=lambda e: e["start_seconds"]):
            if label["start_seconds"] > covered_until + 1e-6:
                raise ContractError("AI label must cover the full video without gaps")
            covered_until = max(covered_until, label["end_seconds"])
        if covered_until < duration - 1e-6:
            raise ContractError("AI label must cover the full video without gaps")
    collisions: list[dict[str, str]] = []
    for index, left in enumerate(elements):
        for right in elements[index + 1 :]:
            if not _time_overlaps(left, right):
                continue
            if layout_intersection_area(left["rect"], right["rect"]) <= 0:
                continue
            collisions.append(
                {
                    "left": left["element_id"],
                    "right": right["element_id"],
                }
            )
    if collisions:
        pairs = ", ".join(f"{row['left']}/{row['right']}" for row in collisions)
        raise ContractError(f"final layout critical elements collide: {pairs}")
    return {
        "schema_version": "video-final-layout-safety-receipt.v1",
        "layout_id": document["layout_id"],
        "candidate_sha256": document["candidate"]["expected_sha256"],
        "layout_evidence_sha256": document["layout_evidence"]["expected_sha256"],
        "checked_elements": len(elements),
        "checked_reserved_regions": len(reserved),
        "required_roles": sorted(required_roles),
        "ai_disclosure": {
            **disclosure, "required": ai_required,
            "policy_revision": "creator-ai-disclosure-default-20261029",
            "full_duration_covered": ai_required,
            "labels": [{"element_id": e["element_id"], "text": e["text"]} for e in labels],
            "verification_scope": "declared_layout_and_bound_evidence; not automatic OCR",
        },
        "auxiliary_reserved_region_overlaps": auxiliary_overlaps,
        "auxiliary_visibility_review": [
            {"element_id": e["element_id"], **e["auxiliary_visibility"]}
            for e in elements if not e["critical_content"]
        ],
        "status": "passed",
        "candidate_only": True,
        "automatic_publish": False,
    }


def validate_final_layout_path(
    document_path: Path, workspace_root: Path
) -> dict[str, Any]:
    return validate_final_layout(_load_json(document_path), workspace_root)
