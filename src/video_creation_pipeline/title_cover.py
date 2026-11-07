from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .content_video_pipeline import validate_content_video_project_plan
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .insurance_content_gate import build_insurance_content_scan_request

TITLE_COVER_PLAN_SCHEMA = "video_creation_pipeline.title_cover_candidate_plan.v1"
TITLE_COVER_SPEC_SCHEMA = "video_creation_pipeline.title_cover_candidate_spec.v1"
TITLE_COVER_DYNAMIC_SPEC_SCHEMA = (
    "video_creation_pipeline.title_cover_candidate_spec.v2"
)
TITLE_COVER_ALIGNMENT_SPEC_SCHEMA = (
    "video_creation_pipeline.title_cover_alignment_projection_spec.v1"
)

TITLE_STRATEGIES = ("searchable", "curiosity", "evidence_led")
DYNAMIC_COVER_FORMULAS = (
    "story_scene",
    "creator_service_action",
    "customer_testimonial",
    "evidence_focus",
    "numeric_contrast",
    "process_sequence",
    "option_decision",
    "tradeoff",
    "service_boundary",
    "personal_experience",
    "editorial_metaphor",
)
ROUTING_SCORE_WEIGHTS = {
    "content_fit": 25,
    "visual_asset_strength": 20,
    "information_gap": 20,
    "credibility": 10,
    "title_complementarity": 10,
    "small_preview_readability": 10,
    "target_audience_fit": 5,
}
ROUTING_HARD_GATES = (
    "fact_accurate",
    "authorization_confirmed",
    "privacy_cleared",
    "medical_insurance_boundary_passed",
    "opening_payoff_supported",
)
PUBLICATION_TITLE_PROFILES: dict[str, dict[str, Any]] = {
    "douyin": {
        "hard_max_chars": 30,
        "recommended_min_chars": 12,
        "recommended_max_chars": 22,
        "core_prefix_max_chars": 14,
    },
    "xiaohongshu": {
        "hard_max_chars": 20,
        "recommended_min_chars": 12,
        "recommended_max_chars": 18,
        "core_prefix_max_chars": 12,
    },
    "wechat_channels": {
        "hard_max_chars": 64,
        "recommended_min_chars": 12,
        "recommended_max_chars": 24,
        "core_prefix_max_chars": 16,
    },
    "bilibili": {
        "hard_max_chars": 80,
        "recommended_min_chars": 16,
        "recommended_max_chars": 28,
        "core_prefix_max_chars": 20,
    },
}
TITLE_WRITING_ROUTES = (
    "search_answer",
    "browse_conflict",
    "evidence_result",
    "decision_tradeoff",
    "service_action",
    "customer_quote",
    "boundary_explainer",
)
SOURCE_KINDS = {
    "dedicated_photo",
    "video_frame",
    "evidence_visual",
    "typography_only",
}
SOURCE_AVAILABILITY = {"planned", "available"}

PLATFORM_PROFILES: dict[str, dict[str, Any]] = {
    "youtube_16_9": {
        "width": 3840,
        "height": 2160,
        "aspect_ratio": "16:9",
        "purpose": "youtube_custom_thumbnail_candidate",
        "safe_zone": {"x": 0.05, "y": 0.05, "width": 0.9, "height": 0.9},
    },
    "bilibili_16_9": {
        "width": 1920,
        "height": 1080,
        "aspect_ratio": "16:9",
        "purpose": "bilibili_landscape_cover_candidate",
        "safe_zone": {"x": 0.05, "y": 0.05, "width": 0.9, "height": 0.9},
    },
    "bilibili_4_3": {
        "width": 1440,
        "height": 1080,
        "aspect_ratio": "4:3",
        "purpose": "bilibili_feed_cover_candidate",
        "safe_zone": {"x": 0.06, "y": 0.06, "width": 0.88, "height": 0.88},
    },
    "vertical_profile_3_4": {
        "width": 1080,
        "height": 1440,
        "aspect_ratio": "3:4",
        "purpose": "vertical_profile_cover_candidate",
        "safe_zone": {"x": 0.08, "y": 0.08, "width": 0.84, "height": 0.84},
    },
    "vertical_first_frame_9_16": {
        "width": 1080,
        "height": 1920,
        "aspect_ratio": "9:16",
        "purpose": "vertical_autoplay_first_frame_candidate",
        "safe_zone": {"x": 0.08, "y": 0.08, "width": 0.72, "height": 0.72},
    },
}


def build_title_cover_candidate_plan(
    project_plan_path: str | Path,
    spec_path: str | Path,
) -> dict[str, Any]:
    project_path = Path(project_plan_path).expanduser().resolve()
    source_spec_path = Path(spec_path).expanduser().resolve()
    project = read_json_object(project_path, "content video project plan")
    spec = read_json_object(source_spec_path, "title and cover candidate spec")
    validate_content_video_project_plan(project)
    normalized = _normalize_spec(spec, project)

    profiles = {
        profile_id: {
            "profile_id": profile_id,
            **PLATFORM_PROFILES[profile_id],
            "renderer": "existing_remotion_still",
            "small_size_preview_width_px": 360,
        }
        for profile_id in normalized["profile_ids"]
    }
    publication_profiles = _publication_title_profiles(
        normalized["publication_platforms"]
    )
    candidates = []
    for candidate in normalized["candidates"]:
        source = candidate["cover_source"]
        render_ready = source["source_kind"] == "typography_only"
        candidates.append(
            {
                **candidate,
                "profile_ids": list(normalized["profile_ids"]),
                "render_readiness": {
                    "status": (
                        "ready_for_remotion_still_candidate"
                        if render_ready
                        else "blocked_requires_safe_layout_source_binding"
                    ),
                    "render_eligible": render_ready,
                    "visual_quality_review_required": True,
                },
            }
        )

    working_id = normalized["working_candidate_id"]
    plan: dict[str, Any] = {
        "schema": TITLE_COVER_PLAN_SCHEMA,
        "source_project_plan": artifact_reference(
            project_path, "content video project plan"
        ),
        "source_spec": artifact_reference(source_spec_path, "title cover spec"),
        "project_plan_sha256": project["plan_sha256"],
        "base_premise": {
            "base_title": project["title"],
            "audience": project["audience"],
            "thesis": project["thesis"],
        },
        "platform_profiles": profiles,
        "publication_title_profiles": publication_profiles,
        "candidates": candidates,
        "selection": {
            "status": "working_candidate" if working_id else "unselected",
            "candidate_id": working_id,
            "selection_is_experiment_hypothesis": working_id is not None,
            "creative_approval_claimed": False,
        },
        "process_policy": _process_policy(normalized),
        "authority_boundary": _authority_boundary(),
    }
    if normalized["routing_selection"] is not None:
        plan["routing_selection"] = normalized["routing_selection"]
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_title_cover_candidate_plan(plan)
    return plan


def validate_title_cover_candidate_plan(plan: dict[str, Any]) -> None:
    _validate_schema(plan)
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("title cover candidate plan integrity check failed")
    project_path = validate_artifact_reference(
        _object(plan.get("source_project_plan")), "content video project plan"
    )
    spec_path = validate_artifact_reference(
        _object(plan.get("source_spec")), "title cover spec"
    )
    project = read_json_object(project_path, "content video project plan")
    spec = read_json_object(spec_path, "title and cover candidate spec")
    validate_content_video_project_plan(project)
    normalized = _normalize_spec(spec, project)
    if plan.get("project_plan_sha256") != project.get("plan_sha256"):
        raise ContractError("title cover candidate plan project binding drifted")
    expected_profiles = {
        profile_id: {
            "profile_id": profile_id,
            **PLATFORM_PROFILES[profile_id],
            "renderer": "existing_remotion_still",
            "small_size_preview_width_px": 360,
        }
        for profile_id in normalized["profile_ids"]
    }
    expected_publication_profiles = _publication_title_profiles(
        normalized["publication_platforms"]
    )
    expected_candidates = []
    for candidate in normalized["candidates"]:
        source = candidate["cover_source"]
        render_ready = source["source_kind"] == "typography_only"
        expected_candidates.append(
            {
                **candidate,
                "profile_ids": list(normalized["profile_ids"]),
                "render_readiness": {
                    "status": (
                        "ready_for_remotion_still_candidate"
                        if render_ready
                        else "blocked_requires_safe_layout_source_binding"
                    ),
                    "render_eligible": render_ready,
                    "visual_quality_review_required": True,
                },
            }
        )
    if (
        plan.get("platform_profiles") != expected_profiles
        or plan.get("publication_title_profiles", {}) != expected_publication_profiles
        or plan.get("candidates") != expected_candidates
    ):
        raise ContractError("title cover candidate plan drifted from its inputs")
    if plan.get("base_premise") != {
        "base_title": project["title"],
        "audience": project["audience"],
        "thesis": project["thesis"],
    }:
        raise ContractError("title cover base premise drifted")
    selection = _object(plan.get("selection"))
    working_id = normalized["working_candidate_id"]
    if selection != {
        "status": "working_candidate" if working_id else "unselected",
        "candidate_id": working_id,
        "selection_is_experiment_hypothesis": working_id is not None,
        "creative_approval_claimed": False,
    }:
        raise ContractError("title cover working selection drifted")
    if plan.get("routing_selection") != normalized["routing_selection"]:
        raise ContractError("title cover routing selection drifted")
    if plan.get("process_policy") != _process_policy(normalized):
        raise ContractError("title cover process policy drifted")
    if plan.get("authority_boundary") != _authority_boundary():
        raise ContractError("title cover authority boundary drifted")


def write_title_cover_candidate_plan(
    plan: dict[str, Any], output_path: str | Path
) -> Path:
    validate_title_cover_candidate_plan(plan)
    return atomic_write_json(plan, output_path)


def selected_title_cover_candidate(plan: dict[str, Any]) -> dict[str, Any]:
    validate_title_cover_candidate_plan(plan)
    selection = _object(plan.get("selection"))
    candidate_id = selection.get("candidate_id")
    if not candidate_id:
        raise ContractError("title cover plan has no working candidate selection")
    for raw in plan.get("candidates") or []:
        candidate = _object(raw)
        if candidate.get("candidate_id") == candidate_id:
            return candidate
    raise ContractError("title cover working candidate is missing")


def build_creative_alignment_spec_from_title_cover_plan(
    plan_path: str | Path,
    projection_spec_path: str | Path,
) -> dict[str, Any]:
    source_plan_path = Path(plan_path).expanduser().resolve()
    source_projection_path = Path(projection_spec_path).expanduser().resolve()
    plan = read_json_object(source_plan_path, "title cover candidate plan")
    projection = read_json_object(
        source_projection_path, "title cover alignment projection spec"
    )
    candidate = selected_title_cover_candidate(plan)
    if projection.get("schema") != TITLE_COVER_ALIGNMENT_SPEC_SCHEMA:
        raise ContractError("unsupported title cover alignment projection spec")
    production_mode = str(projection.get("production_mode") or "")
    if production_mode not in {
        "live_action_primary",
        "ai_assisted",
        "ai_led_sandbox",
    }:
        raise ContractError("title cover alignment production mode is invalid")
    assets = projection.get("reference_assets")
    if not isinstance(assets, list) or not assets:
        raise ContractError("title cover alignment projection needs reference assets")
    return {
        "schema": "video_creation_pipeline.creative_alignment_spec.v1",
        "selected_title": candidate["title"],
        "production_mode": production_mode,
        "cover_hypothesis": dict(candidate["cover_hypothesis"]),
        "reference_assets": [dict(_object(row)) for row in assets],
        "title_cover_candidate_plan_path": str(source_plan_path),
        "selected_title_cover_candidate_id": candidate["candidate_id"],
    }


def materialize_title_cover_insurance_request(
    plan_path: str | Path,
    policy_pack_path: str | Path,
    output_dir: str | Path,
    *,
    evidence: Iterable[dict[str, Any]] = (),
    created_at: str | None = None,
    distribution_channel: str = "unspecified",
) -> tuple[dict[str, Any], dict[str, Path]]:
    source_plan_path = Path(plan_path).expanduser().resolve()
    plan = read_json_object(source_plan_path, "title cover candidate plan")
    validate_title_cover_candidate_plan(plan)
    root = Path(output_dir).expanduser().resolve()
    title_path = atomic_write_json(
        {
            "candidates": [
                {
                    "candidate_id": row["candidate_id"],
                    "text": row["title"],
                }
                for row in plan["candidates"]
            ]
            + [
                {
                    "candidate_id": row["candidate_id"],
                    "platform_id": platform_id,
                    "text": title,
                }
                for row in plan["candidates"]
                for platform_id, title in row.get("publication_titles", {}).items()
            ]
        },
        root / "title-candidates.json",
    )
    cover_path = atomic_write_json(
        {
            "candidates": [
                {
                    "candidate_id": row["candidate_id"],
                    "text": row["cover_hypothesis"]["overlay_text"],
                }
                for row in plan["candidates"]
            ]
        },
        root / "cover-text-candidates.json",
    )
    request = build_insurance_content_scan_request(
        policy_pack_path,
        content=[
            {"content_id": "title-candidates", "role": "title", "path": title_path},
            {
                "content_id": "cover-text-candidates",
                "role": "cover_text",
                "path": cover_path,
            },
        ],
        evidence=evidence,
        created_at=created_at,
        distribution_channel=distribution_channel,
    )
    return request, {"titles": title_path, "cover_text": cover_path}


def _normalize_spec(spec: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    schema = spec.get("schema")
    if schema not in {TITLE_COVER_SPEC_SCHEMA, TITLE_COVER_DYNAMIC_SPEC_SCHEMA}:
        raise ContractError("unsupported title cover candidate spec schema")
    profile_ids = [str(value or "").strip() for value in spec.get("profile_ids") or []]
    if not profile_ids or len(profile_ids) != len(set(profile_ids)):
        raise ContractError("title cover platform profile ids must be unique")
    unknown_profiles = set(profile_ids) - set(PLATFORM_PROFILES)
    if unknown_profiles:
        raise ContractError(
            "unsupported title cover platform profile: "
            + ", ".join(sorted(unknown_profiles))
        )
    publication_platforms = [
        str(value or "").strip() for value in spec.get("publication_platforms") or []
    ]
    if len(publication_platforms) != len(set(publication_platforms)):
        raise ContractError("publication title platforms must be unique")
    unknown_publication_platforms = set(publication_platforms) - set(
        PUBLICATION_TITLE_PROFILES
    )
    if unknown_publication_platforms:
        raise ContractError(
            "unsupported publication title platform: "
            + ", ".join(sorted(unknown_publication_platforms))
        )
    scene_by_id = {
        str(_object(row).get("scene_id") or ""): _object(row)
        for row in project["packaging_plan"]["scenes"]
    }
    raw_candidates = spec.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ContractError("title cover candidates must be an array")
    routing_selection = None
    if schema == TITLE_COVER_SPEC_SCHEMA:
        if len(raw_candidates) != 3:
            raise ContractError("title cover spec requires exactly three candidates")
        candidates = [
            _normalize_candidate(
                _object(raw),
                scene_by_id,
                allowed_strategies=TITLE_STRATEGIES,
                publication_platforms=publication_platforms,
            )
            for raw in raw_candidates
        ]
        if {row["strategy"] for row in candidates} != set(TITLE_STRATEGIES):
            raise ContractError(
                "title cover candidates must include searchable, curiosity, and evidence_led"
            )
        candidates.sort(key=lambda row: TITLE_STRATEGIES.index(row["strategy"]))
    else:
        if not 2 <= len(raw_candidates) <= len(DYNAMIC_COVER_FORMULAS):
            raise ContractError(
                "dynamic title cover routing requires 2 to 11 eligible formula candidates"
            )
        pool = [
            _normalize_candidate(
                _object(raw),
                scene_by_id,
                allowed_strategies=DYNAMIC_COVER_FORMULAS,
                require_routing_assessment=True,
                publication_platforms=publication_platforms,
            )
            for raw in raw_candidates
        ]
        pool_ids = [row["candidate_id"] for row in pool]
        pool_titles = [_normalized_compare(row["title"]) for row in pool]
        if len(pool_ids) != len(set(pool_ids)) or len(pool_titles) != len(
            set(pool_titles)
        ):
            raise ContractError(
                "dynamic title cover candidate ids and titles must be unique"
            )
        formulas = [row["strategy"] for row in pool]
        if len(formulas) != len(set(formulas)):
            raise ContractError("dynamic title cover formulas must be unique")
        candidate_limit = spec.get("candidate_limit", 3)
        if candidate_limit not in {2, 3}:
            raise ContractError("dynamic title cover candidate limit must be 2 or 3")
        pool.sort(
            key=lambda row: (
                -int(_object(row["routing_assessment"])["total_score"]),
                row["candidate_id"],
            )
        )
        candidates = pool[: min(candidate_limit, len(pool))]
        for rank, row in enumerate(candidates, start=1):
            _object(row["routing_assessment"])["selected_rank"] = rank
        routing_selection = {
            "selection_method": "weighted_content_formula_routing",
            "candidate_limit": candidate_limit,
            "considered_candidate_count": len(pool),
            "selected_candidate_ids": [row["candidate_id"] for row in candidates],
            "score_weights": dict(ROUTING_SCORE_WEIGHTS),
            "hard_gate_fields": list(ROUTING_HARD_GATES),
            "diversity_policy": "tie_break_only",
            "ctr_prediction_claimed": False,
        }
    ids = [row["candidate_id"] for row in candidates]
    titles = [_normalized_compare(row["title"]) for row in candidates]
    if len(ids) != len(set(ids)) or len(titles) != len(set(titles)):
        raise ContractError("title cover candidate ids and titles must be unique")
    working_id = str(spec.get("working_candidate_id") or "").strip() or None
    if working_id is not None and working_id not in {
        row["candidate_id"] for row in candidates
    }:
        raise ContractError(
            "title cover working candidate is unknown or was not selected by routing"
        )
    return {
        "mode": "legacy_fixed"
        if schema == TITLE_COVER_SPEC_SCHEMA
        else "dynamic_formula_routing",
        "profile_ids": profile_ids,
        "publication_platforms": publication_platforms,
        "candidates": candidates,
        "working_candidate_id": working_id,
        "routing_selection": routing_selection,
    }


def _normalize_candidate(
    candidate: dict[str, Any],
    scene_by_id: dict[str, dict[str, Any]],
    *,
    allowed_strategies: tuple[str, ...],
    require_routing_assessment: bool = False,
    publication_platforms: list[str] | None = None,
) -> dict[str, Any]:
    candidate_id = _required_text(candidate.get("candidate_id"), "candidate id", 80)
    if not candidate_id.replace("-", "").replace("_", "").isalnum():
        raise ContractError("title cover candidate id contains unsupported characters")
    strategy = str(candidate.get("strategy") or "")
    if strategy not in allowed_strategies:
        raise ContractError(f"unsupported title cover strategy: {strategy}")
    title = _required_text(candidate.get("title"), "candidate title", 100)
    cover = _object(candidate.get("cover_hypothesis"))
    hypothesis = {
        "visual_focus": _required_text(
            cover.get("visual_focus"), "cover visual focus", 240
        ),
        "audience_promise": _required_text(
            cover.get("audience_promise"), "cover audience promise", 240
        ),
        "click_reason": _required_text(
            cover.get("click_reason"), "cover click reason", 240
        ),
        "overlay_text": str(cover.get("overlay_text") or "").strip(),
    }
    if len(hypothesis["overlay_text"]) > 24:
        raise ContractError("cover overlay text must not exceed 24 characters")
    if _normalized_compare(title) == _normalized_compare(hypothesis["overlay_text"]):
        raise ContractError("title and cover overlay must be complementary")
    binding = _object(candidate.get("opening_payoff_binding"))
    scene_id = str(binding.get("scene_id") or "").strip()
    scene = scene_by_id.get(scene_id)
    if scene is None:
        raise ContractError(
            f"title cover candidate uses unknown opening scene: {scene_id}"
        )
    evidence_refs = [dict(_object(row)) for row in binding.get("evidence_refs") or []]
    if not evidence_refs:
        raise ContractError("title cover opening payoff requires evidence refs")
    available = {
        canonical_json_sha256(_object(row)) for row in scene.get("evidence_refs") or []
    }
    requested = {canonical_json_sha256(row) for row in evidence_refs}
    if not requested <= available:
        raise ContractError(
            "title cover promise uses evidence outside its opening scene"
        )
    source = _object(candidate.get("cover_source"))
    kind = str(source.get("source_kind") or "")
    availability = str(source.get("availability") or "")
    if kind not in SOURCE_KINDS or availability not in SOURCE_AVAILABILITY:
        raise ContractError("title cover source kind or availability is invalid")
    normalized_source: dict[str, Any] = {
        "source_kind": kind,
        "availability": availability,
        "source_note": _required_text(
            source.get("source_note"), "cover source note", 240
        ),
    }
    raw_path = str(source.get("path") or "").strip()
    if raw_path:
        normalized_source["artifact"] = artifact_reference(
            raw_path, f"title cover source {candidate_id}"
        )
    if availability == "available" and kind != "typography_only" and not raw_path:
        raise ContractError("available title cover source requires an artifact")
    if availability == "planned" and raw_path:
        raise ContractError("planned title cover source cannot claim an artifact")
    if kind == "typography_only" and raw_path:
        raise ContractError("typography-only cover cannot bind a source artifact")
    normalized = {
        "candidate_id": candidate_id,
        "strategy": strategy,
        "title": title,
        "cover_hypothesis": hypothesis,
        "opening_payoff_binding": {
            "scene_id": scene_id,
            "payoff_summary": _required_text(
                binding.get("payoff_summary"), "opening payoff summary", 240
            ),
            "evidence_refs": evidence_refs,
        },
        "cover_source": normalized_source,
    }
    platforms = list(publication_platforms or [])
    if platforms:
        route = str(candidate.get("title_route") or "").strip()
        if route not in TITLE_WRITING_ROUTES:
            raise ContractError(
                "publication title route must be one of: "
                + ", ".join(TITLE_WRITING_ROUTES)
            )
        supplied_titles = _object(candidate.get("platform_titles"))
        unknown_title_platforms = set(supplied_titles) - set(platforms)
        if unknown_title_platforms:
            raise ContractError(
                "publication title supplied for an unselected platform: "
                + ", ".join(sorted(unknown_title_platforms))
            )
        publication_titles: dict[str, str] = {}
        publication_assessments: dict[str, dict[str, Any]] = {}
        for platform_id in platforms:
            platform_title = _required_text(
                supplied_titles.get(platform_id, title),
                f"{platform_id} publication title",
                PUBLICATION_TITLE_PROFILES[platform_id]["hard_max_chars"],
            )
            publication_titles[platform_id] = platform_title
            publication_assessments[platform_id] = _assess_publication_title(
                platform_id, platform_title, route
            )
        normalized["title_route"] = route
        normalized["publication_titles"] = publication_titles
        normalized["publication_title_assessments"] = publication_assessments
    if require_routing_assessment:
        normalized["routing_assessment"] = _normalize_routing_assessment(
            _object(candidate.get("routing_assessment"))
        )
    return normalized


def _normalize_routing_assessment(assessment: dict[str, Any]) -> dict[str, Any]:
    hard_gates = _object(assessment.get("hard_gates"))
    normalized_gates = {
        field: hard_gates.get(field) is True for field in ROUTING_HARD_GATES
    }
    failed = [field for field, passed in normalized_gates.items() if not passed]
    if failed:
        raise ContractError(
            "dynamic title cover candidate failed hard gate: " + ", ".join(failed)
        )
    scores = _object(assessment.get("scores"))
    normalized_scores: dict[str, int] = {}
    for field in ROUTING_SCORE_WEIGHTS:
        value = scores.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 5:
            raise ContractError(
                f"dynamic title cover routing score {field} must be an integer from 0 to 5"
            )
        normalized_scores[field] = value
    total_score = sum(
        normalized_scores[field] * weight // 5
        for field, weight in ROUTING_SCORE_WEIGHTS.items()
    )
    return {
        "hard_gates": normalized_gates,
        "scores": normalized_scores,
        "total_score": total_score,
        "selected_rank": None,
    }


def _publication_title_profiles(platform_ids: list[str]) -> dict[str, Any]:
    return {
        platform_id: {
            "platform_id": platform_id,
            **PUBLICATION_TITLE_PROFILES[platform_id],
            "count_method": "unicode_code_points_after_trim",
            "hard_limit_enforcement": "fail_closed",
            "recommended_range_enforcement": "warning_only",
            "recommendation_scope": (
                "chinese_case_video_working_range_not_platform_optimum"
            ),
            "platform_ui_recheck_before_publish": True,
        }
        for platform_id in platform_ids
    }


def _assess_publication_title(
    platform_id: str, title: str, route: str
) -> dict[str, Any]:
    profile = PUBLICATION_TITLE_PROFILES[platform_id]
    count = len(title)
    recommended = (
        profile["recommended_min_chars"] <= count <= profile["recommended_max_chars"]
    )
    return {
        "char_count": count,
        "within_hard_limit": True,
        "within_recommended_range": recommended,
        "quality_status": (
            "recommended_range" if recommended else "usable_needs_length_review"
        ),
        "title_route": route,
        "core_prefix_review_chars": min(count, profile["core_prefix_max_chars"]),
        "semantic_review_required": [
            "accuracy",
            "single_primary_promise",
            "important_words_front_loaded",
            "opening_payoff",
            "title_cover_complementarity",
            "target_audience_fit",
        ],
    }


def _validate_schema(plan: dict[str, Any]) -> None:
    schema = read_json_object(
        Path(__file__).with_name("schemas")
        / "title_cover_candidate_plan.v1.schema.json",
        "title cover candidate plan schema",
    )
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(plan)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(
            f"title cover candidate schema validation failed: {exc.message}"
        ) from exc


def _process_policy(normalized: dict[str, Any]) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "candidate_strategies": [row["strategy"] for row in normalized["candidates"]],
        "candidate_generation_aids": ["dbs-xhs-title"],
        "opening_payoff_review_aid": "dbs-hook",
        "frame_shortlist_method": "scene_change_plus_uniform_samples",
        "frame_shortlist_reference": "thumbnail-video MIT source review",
        "dedicated_cover_capture_preferred": True,
        "title_and_overlay_must_be_complementary": True,
        "insurance_content_scan_required_before_candidate_render": True,
        "visual_qa_required_after_render": [
            "dimensions",
            "small_size_readability",
            "text_overflow",
            "blur",
            "eyes_and_face",
            "subject_occlusion",
            "safe_zone",
            "duplicate_frame",
        ],
        "renderer": "existing_remotion_still",
        "platform_native_test_metric": "watch_time_share_or_platform_equivalent",
        "ctr_only_optimization_forbidden": True,
    }
    if normalized["mode"] == "dynamic_formula_routing":
        policy.update(
            {
                "candidate_formula_pool": list(DYNAMIC_COVER_FORMULAS),
                "selection_method": "weighted_content_formula_routing",
                "routing_score_weights": dict(ROUTING_SCORE_WEIGHTS),
                "routing_hard_gates": list(ROUTING_HARD_GATES),
                "candidate_limit": _object(normalized["routing_selection"])[
                    "candidate_limit"
                ],
                "diversity_tiebreak_only": True,
            }
        )
    if normalized["publication_platforms"]:
        policy["publication_title_policy"] = {
            "platforms": list(normalized["publication_platforms"]),
            "hard_limit_source": "user_confirmed_platform_ui_2026_08_26",
            "hard_limit_is_release_gate": True,
            "recommended_range_is_warning_only": True,
            "shortest_complete_promise_not_shortest_possible": True,
            "important_words_front_loaded": True,
            "series_and_branding_at_end": True,
            "one_primary_promise": True,
            "title_routes": list(TITLE_WRITING_ROUTES),
            "platform_ui_recheck_before_publish": True,
        }
    return policy


def _authority_boundary() -> dict[str, Any]:
    return {
        "candidate_only": True,
        "semantic_accuracy_proven": False,
        "creative_approval_claimed": False,
        "render_execution_performed": False,
        "platform_data_read": False,
        "upload_performed": False,
        "publish_performed": False,
        "second_renderer_created": False,
        "second_ffmpeg_outlet_created": False,
    }


def _required_text(value: Any, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{label} is required")
    if len(text) > maximum:
        raise ContractError(f"{label} is too long")
    return text


def _normalized_compare(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
