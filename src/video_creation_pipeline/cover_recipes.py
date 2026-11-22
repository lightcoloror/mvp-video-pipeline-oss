"""Executable composition contracts for the existing Remotion Still renderer.

Absent recipe means legacy layout. Explicit recipes fail closed rather than
silently falling back to a generic title card. Assets remain local and uncropped.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
import re

from .errors import ContractError


RECIPES = {
    "minimal-whitespace": ({"person_led", "typography_only", "editorial_metaphor"}, 0, 1),
    "person-side-whitespace": ({"person_led", "person_plus_evidence"}, 1, 2),
    "partial-person-object-led": ({"person_led", "person_plus_evidence", "evidence_led"}, 1, 2),
    "product-ui-led": ({"person_led", "typography_only"}, 1, 1),
    "comparison-cards": ({"evidence_led", "person_plus_evidence", "typography_only"}, 2, 2),
    "editorial-collage": ({"person_led", "editorial_metaphor"}, 2, 2),
    "evidence-focus": ({"evidence_led", "person_plus_evidence"}, 1, 1),
}
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _validate_account_id(value: object, *, label: str) -> str:
    """Require a non-empty bounded identifier rather than arbitrary text or whitespace."""
    if not isinstance(value, str) or not _ACCOUNT_ID_RE.fullmatch(value):
        raise ContractError(f"invalid {label}")
    return value


def _allowed_accounts_for_recipe(
    recipe: str, recipe_account_policy: object
) -> set[str]:
    """Resolve an operator-supplied recipe policy while blocking substring matching."""
    if not isinstance(recipe_account_policy, Mapping) or recipe not in recipe_account_policy:
        raise ContractError("recipe account policy is required for the selected recipe")
    raw_allowed = recipe_account_policy[recipe]
    # Reject strings/bytes/mappings so membership checks cannot degenerate into substring matches.
    if not isinstance(raw_allowed, (set, frozenset, list, tuple)) or not raw_allowed:
        raise ContractError(
            "recipe account policy entry must be a non-empty collection of account identifiers"
        )
    return {
        _validate_account_id(item, label="recipe account policy entry")
        for item in raw_allowed
    }


def validate_recipe(
    plan: dict,
    *,
    recipe_account_policy: Mapping[str, Collection[str]] | None = None,
) -> str | None:
    """Validate an explicit cover recipe against an externally supplied account policy."""
    if not isinstance(plan, Mapping):
        raise ContractError("cover plan must be a mapping")
    route = plan["visual_route"]
    if any(key in plan for key in ("recipe_account_policy", "account_scope")) or (
        isinstance(route, Mapping)
        and any(key in route for key in ("recipe_account_policy", "account_scope"))
    ):
        raise ContractError("recipe account policy must be supplied separately from the plan")
    recipe = route.get("composition_recipe_id")
    if recipe is None:
        return None
    if not isinstance(recipe, str) or recipe not in RECIPES:
        raise ContractError("unknown cover composition recipe")
    strategies, lower, upper = RECIPES[recipe]
    if route.get("composition_strategy") not in strategies:
        raise ContractError("recipe does not support this composition strategy")
    allowed_accounts = _allowed_accounts_for_recipe(recipe, recipe_account_policy)
    brand = plan.get("brand")
    if not isinstance(brand, Mapping):
        raise ContractError("recipe account scope mismatch")
    account = _validate_account_id(brand.get("account"), label="brand account")
    if account not in allowed_accounts:
        raise ContractError("recipe account scope mismatch")
    # Generation is a separate authorized workflow, never enabled by a recipe.
    if route.get("render_mode", "deterministic-composite") != "deterministic-composite":
        raise ContractError("generated cover execution requires a separately authorized generation workflow")
    assets = {a["asset_id"]: a for a in plan["assets"]}
    for layout in plan["render_spec"]["profile_layouts"].values():
        media = layout["media"]
        if not lower <= len(media) <= upper or len({m["asset_id"] for m in media}) != len(media):
            raise ContractError("recipe media count is invalid or duplicates an asset")
        roles = {m.get("recipe_role") for m in media}
        required = {
            "person-side-whitespace": {"person"},
            "partial-person-object-led": {"object"},
            "product-ui-led": {"ui"},
            "editorial-collage": {"hero", "context"},
            "evidence-focus": {"evidence"},
        }.get(recipe, set())
        if not required.issubset(roles):
            raise ContractError("recipe is missing its required asset role")
        if recipe == "comparison-cards":
            if not str(route.get("comparison_basis") or "").strip():
                raise ContractError("comparison recipe requires an explicit comparison basis")
            if any(not str(m.get("label") or "").strip() for m in media):
                raise ContractError("comparison recipe requires two item labels")
            a, b = [m["box"] for m in media]
            if abs(a["width"] - b["width"]) > 0.001 or abs(a["height"] - b["height"]) > 0.001:
                raise ContractError("comparison media must receive equal display area")
        for m in media:
            asset = assets[m["asset_id"]]
            if asset.get("generated") is not False:
                raise ContractError("deterministic recipe assets must explicitly declare generated=false")
            if asset.get("privacy_reviewed") is not True or asset.get("use_approved") is not True:
                raise ContractError("recipe asset privacy and use approval are required")
            if m.get("recipe_role") in {"evidence", "ui", "object"} and asset.get("original_preserved") is not True:
                raise ContractError("evidence, UI and objects must preserve the original asset")
            if recipe == "evidence-focus" and not str(asset.get("verified_focus") or "").strip():
                raise ContractError("evidence focus requires a verified focal fact")
            # No recipe may cover a media rectangle with the title. This conservative
            # contract avoids pretending that a face-detection fixture proves safety.
            if _overlap(layout["text_box"], m["box"]):
                raise ContractError("recipe text intersects protected media area")
    return recipe


def _overlap(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
            and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"])


def recipe_layout(recipe: str, *, width: int, height: int, media: list[dict]) -> dict:
    """Ratio-aware starting layouts; caller still supplies approved assets and QA.

    This is not cropping or a new renderer. Portrait and landscape reflow instead
    of scaling a landscape composition into a tall frame.
    """
    if recipe not in RECIPES or width <= 0 or height <= 0:
        raise ContractError("invalid recipe or dimensions")
    _, low, high = RECIPES[recipe]
    if not low <= len(media) <= high:
        raise ContractError("recipe media count is invalid")
    portrait = height > width
    def rect(x, y, w, h):
        return dict(x=x, y=y, width=w, height=h)
    title = rect(.07, .07, .86, .25 if portrait else .23)
    if recipe == "minimal-whitespace":
        title = rect(.09, .16, .82, .38)
        boxes = [rect(.60, .62, .30, .25)]
    elif recipe == "person-side-whitespace":
        title = rect(.07, .07, .86, .25) if portrait else rect(.07, .16, .40, .65)
        boxes = ([rect(.30, .36, .63, .55), rect(.07, .61, .20, .24)] if portrait
                 else [rect(.51, .08, .42, .83), rect(.07, .83, .22, .10)])
    elif recipe == "partial-person-object-led":
        boxes = [rect(.08, .37, .68, .51), rect(.79, .50, .14, .32)] if portrait else [rect(.08, .37, .66, .51), rect(.78, .46, .15, .40)]
    elif recipe == "product-ui-led":
        boxes = [rect(.07, .37, .86, .51)]
    elif recipe == "comparison-cards":
        boxes = ([rect(.08, .38, .84, .22), rect(.08, .65, .84, .22)] if portrait
                 else [rect(.07, .38, .40, .48), rect(.53, .38, .40, .48)])
    elif recipe == "editorial-collage":
        boxes = [rect(.07, .39, .57, .49), rect(.68, .53, .25, .32)]
    else:  # evidence-focus: preserve full material, no invented zoom or highlights.
        boxes = [rect(.12, .38, .76, .49)]
    return {"text_box": title, "text_align": "left", "media": [
        {**item, "box": boxes[i], "fit_mode": "contain", "crop_allowed": False}
        for i, item in enumerate(media)]}
