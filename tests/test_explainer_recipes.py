from copy import deepcopy
import pytest
from author_binding_fixture import make_case
from video_creation_pipeline.contract_io import atomic_write_json, read_json_object
from video_creation_pipeline.explainer_recipes import build_explainer_recipes, validate_recipe_plan, materialize_recipe_assets, check_recipe_shots
from video_creation_pipeline.errors import ContractError


def case(tmp_path, recipe="sequence"):
    root = make_case(tmp_path / "case")
    unit = read_json_object(root / "content-plan.json", "content")["shot_units"][0]
    row = {"scene_id": unit["scene_id"], "recipe": recipe,
           "explanation_target": "示例解释", "visual_protagonist": "证据",
           "relationship": "处理顺序", "intended_understanding": "区分来源状态",
           "nodes": [{"id": str(i), "text": text, "source_quote": unit["narration"],
                      "evidence_refs": unit["evidence_refs"]} for i, text in enumerate(["统一输出", "已审核", "支持证据"])]}
    atomic_write_json({"schema": "video_creation_pipeline.explainer_recipe_spec.v1", "scenes": [row]}, root / "recipes.json")
    return root


@pytest.mark.parametrize("recipe", ["causal", "comparison", "sequence"])
def test_recipes_materialize_and_require_motion_coverage(tmp_path, recipe):
    root = case(tmp_path, recipe)
    plan = build_explainer_recipes(root / "content-plan.json", root / "recipes.json")
    validate_recipe_plan(plan)
    path = atomic_write_json(plan, root / "plan.json")
    result = materialize_recipe_assets(path, root / "assets")
    assert len(result["assets"]) == 1
    assert materialize_recipe_assets(path, root / "assets") == result
    assert check_recipe_shots(path, [])["findings"][0]["code"] == "missing_scene"


def test_reject_chinese_and_numeric_source_drift(tmp_path):
    root = case(tmp_path)
    source = read_json_object(root / "recipes.json", "spec")
    for text in ["未经审核", "100%保证"]:
        spec = deepcopy(source)
        spec["scenes"][0]["nodes"][0]["text"] = text
        atomic_write_json(spec, root / "recipes.json")
        with pytest.raises(ContractError, match="frozen narration"):
            build_explainer_recipes(root / "content-plan.json", root / "recipes.json")


def test_missing_and_duplicate_scene_rejected(tmp_path):
    root = case(tmp_path)
    spec = read_json_object(root / "recipes.json", "spec")
    spec["scenes"] *= 2
    atomic_write_json(spec, root / "recipes.json")
    with pytest.raises(ContractError, match="every content scene"):
        build_explainer_recipes(root / "content-plan.json", root / "recipes.json")


def test_recipe_asset_native_motion_correspondence(tmp_path):
    from author_binding_fixture import refresh_fixture_approvals, approval_args
    from video_creation_pipeline.author_bindings import build_author_bound_motion_bundle
    from video_creation_pipeline.semantic_motion import materialize_hyperframes_project
    root = case(tmp_path)
    plan = build_explainer_recipes(root / 'content-plan.json', root / 'recipes.json')
    path = atomic_write_json(plan, root / 'plan.json')
    assets = materialize_recipe_assets(path, root / 'assets')
    spec_path = root / 'binding-spec.json'
    spec = read_json_object(spec_path, 'spec')
    original_asset = spec['source_asset']
    spec['source_asset'] = assets['assets'][0]['asset']
    spec['cue_bindings'][0]['cue']['label']['text'] = '支持证据'
    atomic_write_json(spec, spec_path)
    refresh_fixture_approvals(root)
    bundle = build_author_bound_motion_bundle(spec_path, root / 'bundle', **approval_args(root))
    result = check_recipe_shots(path, [bundle['motion_plan']])
    assert result['status'] == 'passed_structured_checks', result
    project = materialize_hyperframes_project(bundle['motion_plan'], root / 'project')
    assert project['html'].exists()
    spec['source_asset'] = original_asset
    atomic_write_json(spec, spec_path)
    refresh_fixture_approvals(root)
    wrong = build_author_bound_motion_bundle(spec_path, root / 'wrong', **approval_args(root))
    assert 'recipe_asset_mismatch' in {r['code'] for r in check_recipe_shots(path, [wrong['motion_plan']])['findings']}
