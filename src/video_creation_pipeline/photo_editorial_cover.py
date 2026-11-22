"""Photo-first local cover layer validation, used by the existing CoverStill.

This explicit layout treatment permits text in reviewed photo negative space,
not on a protected face/gesture. It does not expand operator-defined recipe account scopes.
"""
from __future__ import annotations

import math
from .errors import ContractError
from .typography_gates import build_typography_gate


def rect(value):
    if not isinstance(value, dict):
        raise ContractError("photo editorial rectangle required")
    result = {}
    for key in ("x", "y", "width", "height"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ContractError("invalid photo editorial rectangle")
        result[key] = raw
    if min(result.values()) < 0 or result["width"] <= 0 or result["height"] <= 0 or result["x"] + result["width"] > 1.00001 or result["y"] + result["height"] > 1.00001:
        raise ContractError("photo editorial rectangle escapes frame")
    return result


def overlaps(a, b):
    return a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]


def validate_photo_editorial(plan):
    layouts = plan["render_spec"]["profile_layouts"]
    if not any(layout.get("photo_editorial") is not None for layout in layouts.values()):
        return
    assets = {a["asset_id"]: a for a in plan["assets"]}
    for profile in plan["profiles"]:
        layout = layouts[profile["profile_id"]]
        editorial = layout.get("photo_editorial")
        if editorial is None:
            continue
        if plan["visual_route"].get("composition_recipe_id") is not None:
            raise ContractError("photo editorial treatment cannot silently override a recipe")
        if not isinstance(editorial, dict) or editorial.get("schema") != "local.photo_editorial_cover.v1":
            raise ContractError("unsupported photo editorial treatment")
        hero = assets.get(editorial.get("hero_asset_id"), {})
        if hero.get("generated") is not False or hero.get("use_approved") is not True or hero.get("privacy_reviewed") is not True:
            raise ContractError("photo editorial hero must be approved real media")
        protected = [rect(r) for r in editorial.get("protected_regions", [])]
        if not protected:
            raise ContractError("photo editorial requires reviewed protected regions")
        placements = layout["media"]
        if not placements or placements[0]["asset_id"] != editorial["hero_asset_id"] or placements[0]["box"] != dict(x=0, y=0, width=1, height=1):
            raise ContractError("photo editorial hero must fill frame without renderer crop")
        if hero.get("dimensions") != [profile["width"], profile["height"]]:
            raise ContractError("photo editorial needs an explicit aspect-matched derivative")
        for media in placements[1:]:
            a = assets[media["asset_id"]]
            if a.get("role") != "decoration" or a.get("use_approved") is not True:
                raise ContractError("photo editorial extra asset must be approved decoration")
            if a.get("generated") is True:
                auth = plan.get("image_generation", {})
                request = auth.get("request_text", "").replace(" ", "")
                if auth.get("authorization_status") != "explicit-user-authorized" or not any(t in request for t in ("AI生图", "模型生成图片")):
                    raise ContractError("generated decoration requires explicit user authorization")
            if any(overlaps(rect(media["box"]), r) for r in protected):
                raise ContractError("decoration covers protected subject")
        gate = build_typography_gate(profile["profile_id"], width=profile["width"], height=profile["height"])
        nodes = editorial.get("text_nodes", [])
        if not 1 <= len(nodes) <= 6:
            raise ContractError("photo editorial needs one to six text nodes")
        text_boxes = []
        for node in nodes:
            box = rect(node["box"])
            role = node.get("role")
            size = node.get("font_px")
            if role not in gate["roles"] or not isinstance(size, (float, int)) or not math.isfinite(size) or size < gate["roles"][role]:
                raise ContractError("photo editorial font below canonical minimum")
            if not str(node.get("text") or "").strip():
                raise ContractError("photo editorial text missing")
            if any(overlaps(box, r) for r in protected + text_boxes):
                raise ContractError("photo editorial text overlaps protected subject or text")
            text_boxes.append(box)
        if any(assets[m["asset_id"]].get("generated") is True for m in placements[1:]) and not any(n["role"] == "ai_disclosure" and "AI" in n["text"] for n in nodes):
            raise ContractError("generated decoration needs visible disclosure")
