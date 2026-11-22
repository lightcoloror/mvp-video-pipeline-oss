import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from video_creation_pipeline.cover_layers import validate_cover_layers
from video_creation_pipeline.cover_recipes import RECIPES, recipe_layout, validate_recipe
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.typography_gates import build_typography_gate, validate_typography_gate


def fixture():
    return {
        'assets': [], 'visual_route': {},
        'profiles': [{'profile_id': 'vertical_profile_3_4', 'width': 1080, 'height': 1440}],
        'render_spec': {'profile_layouts': {'vertical_profile_3_4': {
            'media': [], 'cover_layers': {
                'schema': 'local.cover_layers.v1', 'generative_model_used': False,
                'background': '#ffffff', 'layers': [{
                    'id': 'headline', 'type': 'text', 'text': 'Test', 'role': 'overlay',
                    'font_px': 144, 'color': '#000000',
                    'box': {'x': .1, 'y': .1, 'width': .8, 'height': .3},
                }],
            },
        }}},
    }


def synthetic_recipe_plan(
    *,
    recipe_id='evidence-focus',
    strategy='evidence_led',
    account='example-account-alpha',
    role='evidence',
):
    return {
        'brand': {'account': account},
        'assets': [{
            'asset_id': 'asset-1',
            'generated': False,
            'privacy_reviewed': True,
            'use_approved': True,
            'original_preserved': True,
            'verified_focus': 'synthetic-focus-clause',
        }],
        'visual_route': {
            'composition_recipe_id': recipe_id,
            'composition_strategy': strategy,
            'render_mode': 'deterministic-composite',
        },
        'render_spec': {
            'profile_layouts': {
                'vertical_profile_3_4': {
                    'text_box': {'x': 0.07, 'y': 0.07, 'width': 0.86, 'height': 0.25},
                    'media': [{
                        'asset_id': 'asset-1',
                        'recipe_role': role,
                        'box': {'x': 0.12, 'y': 0.38, 'width': 0.76, 'height': 0.49},
                    }],
                },
            },
        },
    }


class CoverContractReviewTests(unittest.TestCase):
    def test_valid_text_scene(self):
        validate_cover_layers(fixture())

    def test_malformed_and_conflicting_treatments(self):
        for value in ({}, [], False, 'invalid'):
            plan = fixture()
            plan['render_spec']['profile_layouts']['vertical_profile_3_4']['cover_layers'] = value
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_cover_layers(plan)
        plan = fixture()
        plan['render_spec']['profile_layouts']['vertical_profile_3_4']['photo_editorial'] = {}
        with self.assertRaisesRegex(ContractError, 'override'):
            validate_cover_layers(plan)

    def test_font_fit_and_later_occlusion(self):
        for mutation in ('small', 'long', 'occlusion'):
            plan = fixture()
            layers = plan['render_spec']['profile_layouts']['vertical_profile_3_4']['cover_layers']['layers']
            if mutation == 'small':
                layers[0]['font_px'] = 20
            elif mutation == 'long':
                layers[0]['text'] = 'text ' * 100
            else:
                layers.append({'id': 'mask', 'type': 'rect', 'color': '#ffffff', 'box': copy.deepcopy(layers[0]['box'])})
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                validate_cover_layers(plan)

    def test_gate_detects_drift(self):
        gate = build_typography_gate('vertical_profile_3_4', width=1080, height=1440)
        validate_typography_gate(gate, profile_id='vertical_profile_3_4', width=1080, height=1440)
        gate['roles']['overlay'] = 1
        with self.assertRaisesRegex(ContractError, 'drifted'):
            validate_typography_gate(gate, profile_id='vertical_profile_3_4', width=1080, height=1440)

    def test_recipe_rectangles_stay_normalized(self):
        for recipe, (_, minimum, _) in RECIPES.items():
            for width, height in ((1080, 1440), (1920, 1080)):
                layout = recipe_layout(recipe, width=width, height=height, media=[{'asset_id': str(i)} for i in range(minimum)])
                for box in [layout['text_box']] + [m['box'] for m in layout['media']]:
                    with self.subTest(recipe=recipe, width=width):
                        self.assertGreater(box['width'], 0)
                        self.assertGreater(box['height'], 0)
                        self.assertLessEqual(box['x'] + box['width'], 1)
                        self.assertLessEqual(box['y'] + box['height'], 1)

    def test_validate_recipe_no_recipe_legacy_returns_none(self):
        self.assertIsNone(validate_recipe(fixture()))

    def test_validate_recipe_authorized_with_explicit_policy(self):
        plan = synthetic_recipe_plan()
        policy = {'evidence-focus': {'example-account-alpha'}}
        self.assertEqual(validate_recipe(plan, recipe_account_policy=policy), 'evidence-focus')

    def test_validate_recipe_missing_policy_fails_closed(self):
        plan = synthetic_recipe_plan()
        with self.assertRaises(ContractError):
            validate_recipe(plan)
        with self.assertRaises(ContractError):
            validate_recipe(plan, recipe_account_policy={})

    def test_validate_recipe_wrong_account_rejected(self):
        plan = synthetic_recipe_plan(account='example-account-beta')
        policy = {'evidence-focus': ['example-account-alpha']}
        with self.assertRaisesRegex(ContractError, 'scope mismatch'):
            validate_recipe(plan, recipe_account_policy=policy)

    def test_validate_recipe_plan_supplied_policy_rejected(self):
        for key in ('recipe_account_policy', 'account_scope'):
            plan_top = synthetic_recipe_plan()
            plan_top[key] = {'evidence-focus': ['example-account-alpha']}
            with self.subTest(location='plan', key=key), self.assertRaisesRegex(ContractError, 'separately'):
                validate_recipe(plan_top, recipe_account_policy={'evidence-focus': ['example-account-alpha']})
            plan_route = synthetic_recipe_plan()
            plan_route['visual_route'][key] = {'evidence-focus': ['example-account-alpha']}
            with self.subTest(location='visual_route', key=key), self.assertRaisesRegex(ContractError, 'separately'):
                validate_recipe(plan_route, recipe_account_policy={'evidence-focus': ['example-account-alpha']})

    def test_validate_recipe_malformed_policy_and_substring_rejected(self):
        plan = synthetic_recipe_plan(account='alpha')
        for bad_policy in (
            {'evidence-focus': 'example-account-alpha'},
            {'evidence-focus': []},
            {'evidence-focus': {'bad account!'}},
            ['example-account-alpha'],
        ):
            with self.subTest(bad_policy=bad_policy), self.assertRaises(ContractError):
                validate_recipe(plan, recipe_account_policy=bad_policy)
        for bad_account in ('', 'bad account', 'alpha/beta', None, 123):
            bad_plan = synthetic_recipe_plan(account=bad_account)
            with self.subTest(bad_account=bad_account), self.assertRaises(ContractError):
                validate_recipe(bad_plan, recipe_account_policy={'evidence-focus': ['alpha']})

    def test_validate_recipe_existing_guards_reject_bad_plan(self):
        policy = {'evidence-focus': ['example-account-alpha']}
        overlap_plan = synthetic_recipe_plan()
        overlap_plan['render_spec']['profile_layouts']['vertical_profile_3_4']['media'][0]['box'] = {
            'x': 0.08, 'y': 0.08, 'width': 0.5, 'height': 0.5,
        }
        with self.assertRaisesRegex(ContractError, 'intersects'):
            validate_recipe(overlap_plan, recipe_account_policy=policy)

        unreviewed_plan = synthetic_recipe_plan()
        unreviewed_plan['assets'][0]['privacy_reviewed'] = False
        with self.assertRaisesRegex(ContractError, 'privacy'):
            validate_recipe(unreviewed_plan, recipe_account_policy=policy)

        generated_plan = synthetic_recipe_plan()
        generated_plan['assets'][0]['generated'] = True
        with self.assertRaisesRegex(ContractError, 'generated=false'):
            validate_recipe(generated_plan, recipe_account_policy=policy)

        mode_plan = synthetic_recipe_plan()
        mode_plan['visual_route']['render_mode'] = 'ai-generated'
        with self.assertRaisesRegex(ContractError, 'generation workflow'):
            validate_recipe(mode_plan, recipe_account_policy=policy)
