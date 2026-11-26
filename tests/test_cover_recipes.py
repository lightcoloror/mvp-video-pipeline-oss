from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_creation_pipeline.cover_recipes import RECIPES, recipe_layout, validate_recipe
from video_creation_pipeline.case_cover_visual_plan import PROFILE_DIMENSIONS, _normalized_rect, case_cover_variant_projection
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.remotion_project import _COVER_TSX

SYNTHETIC_ACCOUNT_POLICY = {recipe: ("synthetic-account-a",) for recipe in RECIPES}


def fixture(recipe, profile="vertical_profile_3_4"):
    width, height = PROFILE_DIMENSIONS[profile]
    roles = {"minimal-whitespace": [], "person-side-whitespace": ["person"],
             "partial-person-object-led": ["object", "person"], "product-ui-led": ["ui"],
             "comparison-cards": ["comparison", "comparison"],
             "editorial-collage": ["hero", "context"], "evidence-focus": ["evidence"]}[recipe]
    media = [{"asset_id": f"synthetic-{i}", "recipe_role": role, "label": f"样本{i+1}"} for i, role in enumerate(roles)]
    return {
        "candidate": {"candidate_id": recipe, "strategy": "fixture", "title": "合成测试", "overlay_text": "构图测试"},
        "visual_route": {"composition_recipe_id": recipe, "composition_strategy": sorted(RECIPES[recipe][0])[0], "primary_reading_target": "合成测试", "comparison_basis": "相同合成指标"},
        "brand": {"account": sorted(SYNTHETIC_ACCOUNT_POLICY[recipe])[0]},
        "assets": [{"asset_id": m["asset_id"], "generated": False, "privacy_reviewed": True, "use_approved": True, "original_preserved": True, "verified_focus": "合成事实"} for m in media],
        "profiles": [{"profile_id": profile, "width": width, "height": height, "safe_zone": {"x": .05, "y": .05, "width": .9, "height": .9}}],
        "render_spec": {"kind": "person", "graphic_kind": "none", "helper_labels": ["合成事实"], "profile_layouts": {profile: recipe_layout(recipe, width=width, height=height, media=media)}}}


class CoverRecipesTests(unittest.TestCase):
    def test_all_seven_routes_all_five_profiles_project(self):
        for recipe in RECIPES:
            self.assertIn(f'"{recipe}"', _COVER_TSX)
            for profile in PROFILE_DIMENSIONS:
                with self.subTest(recipe=recipe, profile=profile):
                    plan = fixture(recipe, profile)
                    self.assertEqual(recipe, validate_recipe(plan, recipe_account_policy=SYNTHETIC_ACCOUNT_POLICY))
                    layout = plan["render_spec"]["profile_layouts"][profile]
                    _normalized_rect(layout["text_box"], "text")
                    for row in layout["media"]:
                        _normalized_rect(row["box"], "media")
                    safe = {"placements": [{"assetId": a["asset_id"], "publicPath": f'{a["asset_id"]}.png'} for a in plan["assets"]]}
                    projected = case_cover_variant_projection(plan, safe_layout=safe, recipe_account_policy=SYNTHETIC_ACCOUNT_POLICY)[0]
                    self.assertEqual(recipe, projected["caseCover"]["compositionRecipeId"])

    def test_legacy_is_unchanged(self):
        self.assertIsNone(validate_recipe({"visual_route": {}}))

    def test_bad_recipe_account_mode_and_privacy_are_blocking(self):
        base = fixture("person-side-whitespace")
        for path, value in [("recipe", "invented"), ("account", "disallowed-account"), ("mode", "explicit-generated-visual"), ("privacy", False), ("generated", True), ("role", "ui")]:
            plan = copy.deepcopy(base)
            if path == "recipe": plan["visual_route"]["composition_recipe_id"] = value
            if path == "account": plan["brand"]["account"] = value
            if path == "mode": plan["visual_route"]["render_mode"] = value
            if path == "privacy": plan["assets"][0]["privacy_reviewed"] = value
            if path == "generated": plan["assets"][0]["generated"] = value
            if path == "role": plan["render_spec"]["profile_layouts"]["vertical_profile_3_4"]["media"][0]["recipe_role"] = value
            with self.subTest(path=path), self.assertRaises(ContractError): validate_recipe(plan, recipe_account_policy=SYNTHETIC_ACCOUNT_POLICY)

    def test_comparison_requires_basis_labels_equal_area(self):
        for mutation in ("basis", "label", "area"):
            p = fixture("comparison-cards")
            media = p["render_spec"]["profile_layouts"]["vertical_profile_3_4"]["media"]
            if mutation == "basis": p["visual_route"]["comparison_basis"] = ""
            if mutation == "label": media[0]["label"] = ""
            if mutation == "area": media[0]["box"]["width"] -= .1
            with self.subTest(mutation=mutation), self.assertRaises(ContractError): validate_recipe(p, recipe_account_policy=SYNTHETIC_ACCOUNT_POLICY)

    def test_text_must_not_cover_media(self):
        p = fixture("product-ui-led")
        layout = p["render_spec"]["profile_layouts"]["vertical_profile_3_4"]
        layout["text_box"] = dict(layout["media"][0]["box"])
        with self.assertRaisesRegex(ContractError, "intersects"): validate_recipe(p, recipe_account_policy=SYNTHETIC_ACCOUNT_POLICY)

    def test_non_finite_coordinates_are_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ContractError):
                _normalized_rect(dict(x=value, y=0, width=.2, height=.2), "fixture")


if __name__ == "__main__": unittest.main()
