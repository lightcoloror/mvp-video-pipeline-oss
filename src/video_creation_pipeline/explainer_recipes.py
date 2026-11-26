"""Small explanatory diagrams backed by the existing content plan.

Independently authored recipes. Assets enter the existing image/motion route;
this module neither introduces a renderer nor approves factual claims.
"""
from html import escape
from pathlib import Path
from .contract_io import artifact_reference, read_json_object, validate_artifact_reference, payload_sha256
from .production_strategy import content_visual_source
from .errors import ContractError

SCHEMA = "video_creation_pipeline.explainer_recipe_plan.v1"
RECIPES = {"causal": "原因 → 变化 → 结果", "comparison": "按同一维度并列比较", "sequence": "按顺序展示步骤"}


def build_performance_recipe(packaging_path, spec_path):
    """Optional acted synthetic recipe using the existing packaging scene/cues."""
    from .performance_recipe import build_plan
    return build_plan(packaging_path, spec_path)


def build_explainer_recipes(content_path, spec_path):
    spec = read_json_object(spec_path, "explainer recipe spec")
    content = read_json_object(content_path, "content plan")
    rows = spec.get("scenes")
    if spec.get("schema") != "video_creation_pipeline.explainer_recipe_spec.v1" or not isinstance(rows, list) or not rows:
        raise ContractError("explainer recipe spec needs scenes")
    ids = [r.get("scene_id") for r in rows]
    expected = [u["scene_id"] for u in content.get("shot_units", [])]
    if len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise ContractError("recipes must cover every content scene exactly once")
    scenes = []
    for row in rows:
        _, unit = content_visual_source(content_path, row["scene_id"])
        if row.get("recipe") not in RECIPES:
            raise ContractError("unknown explanatory recipe")
        for key in ("explanation_target", "visual_protagonist", "relationship", "intended_understanding"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ContractError(f"recipe requires {key}")
        nodes = row.get("nodes")
        if not isinstance(nodes, list) or not 2 <= len(nodes) <= 4:
            raise ContractError("recipe needs two to four legible nodes")
        if row["recipe"] == "causal" and len(nodes) != 3:
            raise ContractError("causal recipe needs cause, change and result")
        node_ids = []
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {"id", "text", "source_quote", "evidence_refs"}:
                raise ContractError("node requires id, text, source_quote and evidence_refs")
            node_ids.append(node["id"])
            if not isinstance(node["id"], str) or not node["id"].strip():
                raise ContractError("node identity is required")
            if not isinstance(node["text"], str) or not 1 <= len(node["text"]) <= 24:
                raise ContractError("node text must contain 1 to 24 characters")
            # Literal provenance is deliberately stricter than fuzzy fact matching.
            # Paraphrases need a reviewed upstream script change first.
            quote = node["source_quote"]
            if not isinstance(quote, str) or not quote or quote not in unit["narration"] or node["text"] not in quote:
                raise ContractError("node text must match its exact frozen narration quote")
            refs = node["evidence_refs"]
            if not isinstance(refs, list) or not refs or any(ref not in unit["evidence_refs"] for ref in refs):
                raise ContractError("node evidence must belong to the content scene")
        if len(set(node_ids)) != len(node_ids):
            raise ContractError("node IDs must be unique within a scene")
        scenes.append({**row, "duration_s": unit["duration_s"],
                       "source_unit_sha256": unit["unit_sha256"],
                       "relationship_review": "human_review_required"})
    plan = {"schema": SCHEMA, "candidate_only": True,
            "source_content_plan": artifact_reference(content_path, "content plan"),
            "source_spec": artifact_reference(spec_path, "recipe spec"),
            "scenes": scenes, "renderer": "existing_semantic_motion_image_route",
            "fact_status": "literal_source_match_not_fact_approval"}
    plan["artifact_sha256"] = payload_sha256(plan, "artifact_sha256")
    return plan


def validate_recipe_plan(plan):
    if plan.get("schema") != SCHEMA:
        raise ContractError("unsupported recipe plan")
    content = validate_artifact_reference(plan["source_content_plan"], "content")
    spec = validate_artifact_reference(plan["source_spec"], "spec")
    if plan != build_explainer_recipes(content, spec):
        raise ContractError("recipe plan drifted from its inputs")


def recipe_svg(scene):
    """Neutral diagram; no copied upstream theme, script or executable markup."""
    nodes = scene["nodes"]
    body = ['<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1080" viewBox="0 0 1080 1080">',
            '<rect width="1080" height="1080" fill="#f6f4ee"/>']
    for i, node in enumerate(nodes):
        comparison = scene["recipe"] == "comparison"
        x, y, width, height = ((60 + (i % 2) * 500, 180 + (i // 2) * 370, 460, 280)
                               if comparison else (100, 90 + i * 225, 880, 160))
        body.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="20" fill="#ffffff" stroke="#233b45" stroke-width="3"/>')
        text = node["text"]
        wrap = 9 if comparison else 12
        lines = [text[j:j+wrap] for j in range(0, len(text), wrap)]
        for j, line in enumerate(lines):
            body.append(f'<text x="{x+width//2}" y="{y+65+j*48}" text-anchor="middle" font-family="sans-serif" font-size="40" fill="#172d36">{escape(line)}</text>')
        if i < len(nodes)-1 and scene["recipe"] != "comparison":
            body.append(f'<path d="M540 {y+170} v40 m-12 -12 l12 12 12 -12" fill="none" stroke="#233b45" stroke-width="5"/>')
    body.append('</svg>')
    return "\n".join(body)


def materialize_recipe_assets(plan_path, output_dir):
    plan = read_json_object(plan_path, "recipe plan")
    validate_recipe_plan(plan)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    result = []
    for index, scene in enumerate(plan["scenes"]):
        # Never derive filenames from externally supplied scene IDs.
        path = output / f"recipe-{index+1:03d}.svg"
        data = recipe_svg(scene).encode("utf-8")
        if path.exists() and path.read_bytes() != data:
            raise ContractError("existing recipe asset differs; choose a new output directory")
        path.write_bytes(data)
        result.append({"scene_id": scene["scene_id"], "asset": artifact_reference(path, "recipe SVG"),
                       "media_type": "image", "candidate_only": True})
    return {"source_plan": artifact_reference(plan_path, "recipe plan"), "assets": result}


def check_recipe_shots(plan_path, motion_paths):
    from .semantic_motion import validate_semantic_motion_plan
    plan = read_json_object(plan_path, "recipe plan")
    validate_recipe_plan(plan)
    expected = {row["scene_id"]: row for row in plan["scenes"]}
    findings, seen = [], set()
    for path in motion_paths:
        motion = read_json_object(path, "motion plan")
        validate_semantic_motion_plan(motion)
        for cue in motion["cues"]:
            scene_id = cue["story_segment_ref"]
            if scene_id not in expected:
                findings.append({"code": "unknown_scene", "scene_id": scene_id})
                continue
            seen.add(scene_id)
            scene = expected[scene_id]
            asset_path = validate_artifact_reference(motion["source_asset"], "motion asset")
            if asset_path.read_bytes() != recipe_svg(scene).encode("utf-8"):
                findings.append({"code": "recipe_asset_mismatch", "scene_id": scene_id})
            text = cue["label"]["text"]
            if text and not any(text in n["source_quote"] for n in scene["nodes"]):
                findings.append({"code": "unmatched_label_text", "scene_id": scene_id, "text": text})
            hold = cue["timing"]["hold_s"]
            # An explicit conservative heuristic, not proof of actual readability.
            needed = max(1.0, sum(len(n["text"]) for n in scene["nodes"]) / 6)
            if hold < needed:
                findings.append({"code": "short_reading_hold", "scene_id": scene_id, "actual_s": hold, "suggested_s": needed})
    findings.extend({"code": "missing_scene", "scene_id": key} for key in expected if key not in seen)
    return {"status": "review_required" if findings else "passed_structured_checks",
            "findings": findings, "source_plan": artifact_reference(plan_path, "recipe plan"),
            "rendered_frame_check": "not_performed", "relationship_truth": "requires_human_review"}
