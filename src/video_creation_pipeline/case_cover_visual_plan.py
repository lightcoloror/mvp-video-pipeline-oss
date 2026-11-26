from __future__ import annotations

from pathlib import Path
import math
from typing import Any

from .contract_io import read_json_object, validate_artifact_reference
from .errors import ContractError
from .photo_layout import validate_safe_media_layout_plan
from .typography_gates import build_typography_gate, validate_typography_gate
from .cover_recipes import validate_recipe
from .photo_editorial_cover import validate_photo_editorial
from .cover_layers import validate_cover_layers


CASE_COVER_VISUAL_PLAN_SCHEMA = "lucas.case_cover_visual_plan.v1"
CASE_COVER_VISUAL_PLAN_COLLECTION_SCHEMA = "lucas.case_cover_visual_plan_collection.v1"
PROFILE_DIMENSIONS = {
    "youtube_16_9": (3840, 2160),
    "bilibili_16_9": (1920, 1080),
    "bilibili_4_3": (1440, 1080),
    "vertical_profile_3_4": (1080, 1440),
    "vertical_first_frame_9_16": (1080, 1920),
}
COMPOSITION_STRATEGIES = {
    "person_led",
    "evidence_led",
    "person_plus_evidence",
    "typography_only",
    "editorial_metaphor",
}
RENDER_KINDS = {"person", "evidence", "structure", "illustration"}
GRAPHIC_KINDS = {
    "none",
    "one_to_four",
    "two_opinions_choice",
    "cashflow_two_steps",
    "seven_treatment_weeks",
    "calendar_reminder",
    "check_timeline",
    "three_layer_filter",
    "decision_order",
    "question_path",
    "boundary_split",
    "split_payment",
    "payment_claim_sequence",
    "quote_cashflow",
    "two_amount_relationship",
    "responsibility_boundary",
    "life_rhythm",
    "three_factor_tradeoff",
    "coverage_distance_tradeoff",
}


def validate_case_cover_visual_plan(
    plan: dict[str, Any],
    *,
    safe_media_layout_path: str | Path | None = None,
) -> None:
    if plan.get("schema") != CASE_COVER_VISUAL_PLAN_SCHEMA:
        raise ContractError("unsupported Lucas case cover visual plan schema")
    if plan.get("candidate_only") is not True or plan.get("manual_publish_only") is not True:
        raise ContractError("Lucas case cover plan must remain candidate-only and manual-publish-only")

    title_ref = _object(plan.get("title_cover_plan_ref"), "title cover plan ref")
    title_path = _hash_reference_path(title_ref, "title cover plan")
    candidate = _object(plan.get("candidate"), "candidate")
    title_plan = read_json_object(title_path, "title cover plan")
    item = _bound_title_item(title_plan, candidate)
    if (
        str(candidate.get("title") or "") != str(item.get("publication_title") or "")
        or str(candidate.get("overlay_text") or "") != "／".join(
            str(value) for value in item.get("cover_overlay") or []
        )
    ):
        raise ContractError("Lucas case cover candidate drifted from the selected title-cover package")

    route = _object(plan.get("visual_route"), "visual route")
    composition = str(route.get("composition_strategy") or "")
    if composition not in COMPOSITION_STRATEGIES:
        raise ContractError("Lucas case cover composition strategy is invalid")
    hero_count = route.get("hero_element_count")
    if not isinstance(hero_count, int) or not 1 <= hero_count <= 2:
        raise ContractError("Lucas case cover hero element count must be one or two")

    assets = plan.get("assets")
    if not isinstance(assets, list):
        raise ContractError("Lucas case cover assets must be a list")
    available_assets: dict[str, dict[str, Any]] = {}
    seen_assets: set[str] = set()
    for index, raw in enumerate(assets):
        asset = _object(raw, f"cover asset {index}")
        asset_id = str(asset.get("asset_id") or "").strip()
        if not asset_id or asset_id in seen_assets:
            raise ContractError("Lucas case cover asset ids must be unique and non-empty")
        seen_assets.add(asset_id)
        if asset.get("external_upload_allowed") is not False:
            raise ContractError("Lucas case cover assets cannot allow external upload")
        if asset.get("availability") == "available" and asset.get("source_kind") != "typography_only":
            artifact = _object(asset.get("artifact"), f"cover asset {asset_id} artifact")
            _hash_reference_path(artifact, f"cover asset {asset_id}")
            available_assets[asset_id] = asset

    render_spec = _object(plan.get("render_spec"), "render spec")
    if render_spec.get("kind") not in RENDER_KINDS:
        raise ContractError("Lucas case cover render kind is invalid")
    if render_spec.get("graphic_kind") not in GRAPHIC_KINDS:
        raise ContractError("Lucas case cover graphic kind is invalid")
    profile_layouts = _object(render_spec.get("profile_layouts"), "cover profile layouts")

    profiles = plan.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ContractError("Lucas case cover profiles are required")
    profile_ids: set[str] = set()
    referenced_assets: set[str] = set()
    for raw in profiles:
        profile = _object(raw, "cover profile")
        profile_id = str(profile.get("profile_id") or "")
        expected = PROFILE_DIMENSIONS.get(profile_id)
        if expected is None or profile_id in profile_ids:
            raise ContractError("Lucas case cover profile id is invalid or duplicated")
        profile_ids.add(profile_id)
        if (profile.get("width"), profile.get("height")) != expected:
            raise ContractError("Lucas case cover profile dimensions drifted")
        # Intent/decision: profiles bind the canonical type gate before projection.
        # Reason/evidence/scope: see policies/typography-gates.v1.json governance.
        build_typography_gate(
            profile_id,
            width=int(profile["width"]),
            height=int(profile["height"]),
        )
        layout = _object(profile_layouts.get(profile_id), f"cover layout {profile_id}")
        _normalized_rect(layout.get("text_box"), f"cover text box {profile_id}")
        if layout.get("text_align") not in {"left", "center", "right"}:
            raise ContractError("Lucas case cover text alignment is invalid")
        media = layout.get("media")
        if not isinstance(media, list):
            raise ContractError("Lucas case cover media layout must be a list")
        for raw_media in media:
            media_row = _object(raw_media, "cover media placement")
            asset_id = str(media_row.get("asset_id") or "")
            if asset_id not in available_assets:
                raise ContractError("Lucas case cover media placement references an unavailable asset")
            if media_row.get("fit_mode") != "contain" or media_row.get("crop_allowed") is not False:
                raise ContractError("Lucas case cover media must use contain with cropping disabled")
            _normalized_rect(media_row.get("box"), "cover media box")
            referenced_assets.add(asset_id)

    if set(profile_layouts) != profile_ids:
        raise ContractError("Lucas case cover profile layouts drifted from requested profiles")
    validate_recipe(plan)
    validate_photo_editorial(plan)
    validate_cover_layers(plan)

    if referenced_assets:
        if safe_media_layout_path is None:
            raise ContractError("media-backed Lucas case covers require a safe media layout plan")
        layout_path = Path(safe_media_layout_path).expanduser().resolve()
        layout = read_json_object(layout_path, "safe media layout plan")
        validate_safe_media_layout_plan(layout)
        layout_assets = {str(_object(row, "safe placement").get("asset_id") or "") for row in layout.get("placements") or []}
        if not referenced_assets.issubset(layout_assets):
            raise ContractError("Lucas case cover media is not fully bound by the safe media layout")
        for row in layout.get("placements") or []:
            asset_id = row.get("asset_id")
            if asset_id in referenced_assets and row.get("source_asset", {}).get("sha256") != available_assets[asset_id]["artifact"].get("sha256"):
                raise ContractError("cover media hash drifted from the safe media layout")
    elif safe_media_layout_path is not None:
        layout = read_json_object(safe_media_layout_path, "safe media layout plan")
        validate_safe_media_layout_plan(layout)

    handoff = _object(plan.get("render_handoff"), "render handoff")
    if (
        handoff.get("renderer") != "existing_remotion_still"
        or handoff.get("new_renderer_created") is not False
    ):
        raise ContractError("Lucas case cover render handoff is invalid")


def case_cover_variant_projection(
    plan: dict[str, Any],
    *,
    safe_layout: dict[str, Any] | None,
    recipe_account_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    validate_recipe(plan, recipe_account_policy=recipe_account_policy)
    validate_photo_editorial(plan)
    validate_cover_layers(plan)
    candidate = _object(plan["candidate"], "candidate")
    route = _object(plan["visual_route"], "visual route")
    render_spec = _object(plan["render_spec"], "render spec")
    profile_layouts = _object(render_spec["profile_layouts"], "profile layouts")
    public_paths = {
        str(_object(row, "safe placement").get("assetId") or ""): str(
            _object(row, "safe placement").get("publicPath") or ""
        )
        for row in (safe_layout or {}).get("placements") or []
    }
    variants: list[dict[str, Any]] = []
    for raw in plan["profiles"]:
        profile = _object(raw, "cover profile")
        profile_id = str(profile["profile_id"])
        typography_gate = build_typography_gate(
            profile_id,
            width=int(profile["width"]),
            height=int(profile["height"]),
        )
        layout = _object(profile_layouts[profile_id], "cover profile layout")
        media_projection: list[dict[str, Any]] = []
        for raw_media in layout.get("media") or []:
            media = dict(_object(raw_media, "cover media placement"))
            asset_id = str(media["asset_id"])
            public_path = public_paths.get(asset_id)
            if not public_path:
                raise ContractError("Lucas case cover media was not materialized from its safe layout")
            media_projection.append({**media, "public_path": public_path})
        variant = {
                "compositionId": f"Cover-{candidate['candidate_id']}-{profile_id}".replace("_", "-"),
                "candidateId": str(candidate["candidate_id"]),
                "strategy": str(candidate["strategy"]),
                "title": str(candidate["title"]),
                "overlayText": str(candidate["overlay_text"]),
                "visualFocus": str(route.get("primary_reading_target") or ""),
                "audiencePromise": str(candidate.get("promise") or ""),
                "sourceKind": str(render_spec["kind"]),
                "renderEligible": True,
                "profileId": profile_id,
                "width": int(profile["width"]),
                "height": int(profile["height"]),
                "safeZone": _normalized_rect(profile.get("safe_zone"), "cover safe zone"),
                "candidateOnly": True,
                "caseCover": {
                    "account": str(_object(plan["brand"], "brand").get("account") or ""),
                    "palette": dict(_object(plan["brand"], "brand").get("palette") or {}),
                    "compositionStrategy": str(route["composition_strategy"]),
                    "compositionRecipeId": route.get("composition_recipe_id"),
                    "comparisonBasis": str(route.get("comparison_basis") or ""),
                    "primaryReadingTarget": str(route["primary_reading_target"]),
                    "renderKind": str(render_spec["kind"]),
                    "graphicKind": str(render_spec["graphic_kind"]),
                    "tag": str(render_spec.get("tag") or "梳理"),
                    "helperLabels": [str(value) for value in render_spec.get("helper_labels") or []],
                    # Intent: pass renderer-ready values, never a prose guideline.
                    # Decision: this is an exact canonical binding, validated below.
                    # Reason/evidence/scope: see typography-gates.v1.json governance.
                    "typography": typography_gate,
                    "layout": {**layout, "media": media_projection},
                },
            }
        validate_case_cover_variant_projection(variant)
        variants.append(variant)
    return variants


def validate_case_cover_variant_projection(variant: dict[str, Any]) -> None:
    """Reject a projected case-cover variant if type evidence is absent or weakened.

    Intent: prevent a downstream renderer from receiving a smaller manual override.
    Decision: exact policy equality is required for every case-cover projection.
    Reason/evidence/scope: policy governance documents the approved mobile QA minima;
    this checks candidate-only in-memory projections and creates no renderer/state machine.
    """

    profile_id = str(variant.get("profileId") or "")
    width = int(variant.get("width") or 0)
    height = int(variant.get("height") or 0)
    cover = _object(variant.get("caseCover"), "projected case cover")
    validate_typography_gate(
        cover.get("typography"),
        profile_id=profile_id,
        width=width,
        height=height,
    )


def validate_case_cover_visual_plan_collection(
    collection: dict[str, Any],
    *,
    safe_media_layout_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    if collection.get("schema") != CASE_COVER_VISUAL_PLAN_COLLECTION_SCHEMA:
        raise ContractError("unsupported Lucas case cover visual plan collection schema")
    if collection.get("candidate_only") is not True:
        raise ContractError("Lucas case cover collection must remain candidate-only")
    references = collection.get("plans")
    if not isinstance(references, list) or not references:
        raise ContractError("Lucas case cover collection has no plans")
    plans: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    for index, raw in enumerate(references):
        reference = _object(raw, f"case cover plan reference {index}")
        path = validate_artifact_reference(reference, f"case cover plan reference {index}")
        plan = read_json_object(path, f"Lucas case cover plan {index}")
        validate_case_cover_visual_plan(
            plan,
            safe_media_layout_path=safe_media_layout_path,
        )
        candidate_id = str(_object(plan.get("candidate"), "candidate").get("candidate_id") or "")
        if not candidate_id or candidate_id in candidate_ids:
            raise ContractError("Lucas case cover collection candidate ids must be unique")
        candidate_ids.add(candidate_id)
        plans.append(plan)
    return plans


def case_cover_collection_variant_projection(
    plans: list[dict[str, Any]],
    *,
    safe_layout: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    variants = [
        variant
        for plan in plans
        for variant in case_cover_variant_projection(plan, safe_layout=safe_layout)
    ]
    composition_ids = {str(row["compositionId"]) for row in variants}
    if len(composition_ids) != len(variants):
        raise ContractError("Lucas case cover collection composition ids are duplicated")
    return variants


def _bound_title_item(
    plan: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    if plan.get("schema") == "video_creation_pipeline.title_cover_candidate_plan.v1":
        from .title_cover import validate_title_cover_candidate_plan
        validate_title_cover_candidate_plan(plan)
        matches = [row for row in plan["candidates"] if row.get("candidate_id") == candidate.get("candidate_id")]
        if len(matches) != 1:
            raise ContractError("cover candidate missing from canonical title plan")
        row = matches[0]
        return {"publication_title": row["title"], "cover_overlay": [row["cover_hypothesis"]["overlay_text"]]}
    items = plan.get("items")
    if not isinstance(items, list):
        raise ContractError("title cover plan has no items")
    candidate_id = str(candidate.get("candidate_id") or "")
    item_id = str(candidate.get("source_item_id") or candidate_id)
    package_id = str(candidate.get("package_id") or "")
    for raw in items:
        item = _object(raw, "title cover item")
        if str(item.get("id") or "") != item_id:
            continue
        if package_id:
            packages = item.get("packages") or item.get("test_packages")
            if not isinstance(packages, list):
                raise ContractError("title cover item has no test packages")
            for raw_package in packages:
                package = _object(raw_package, "title cover test package")
                if str(package.get("package_id") or "") == package_id:
                    return {
                        "publication_title": str(package.get("full_title") or ""),
                        "cover_overlay": [str(package.get("cover_overlay") or "")],
                    }
            raise ContractError("Lucas case cover package is absent from the title cover plan")
        if str(item.get("id") or "") == item_id:
            return _object(item.get("selected"), "selected title cover item")
    raise ContractError("Lucas case cover candidate is absent from the title cover plan")


def _hash_reference_path(reference: dict[str, Any], label: str) -> Path:
    path = Path(str(reference.get("path") or "")).expanduser().resolve()
    expected = str(reference.get("sha256") or "").lower()
    if not path.is_file() or len(expected) != 64:
        raise ContractError(f"{label} reference is invalid")
    from .contract_io import file_sha256

    if file_sha256(path) != expected:
        raise ContractError(f"{label} changed after planning")
    return path


def _normalized_rect(value: Any, label: str) -> dict[str, float]:
    rect = _object(value, label)
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        raw = rect.get(key)
        if isinstance(raw, bool):
            raise ContractError(f"{label} is invalid")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{label} is invalid") from exc
        if not math.isfinite(number) or number < 0 or (key in {"width", "height"} and number <= 0):
            raise ContractError(f"{label} is invalid")
        result[key] = number
    if result["x"] + result["width"] > 1 or result["y"] + result["height"] > 1:
        raise ContractError(f"{label} escapes the frame")
    return result


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value
