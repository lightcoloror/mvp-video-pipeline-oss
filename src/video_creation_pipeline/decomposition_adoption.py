from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError

VKP_DECOMPOSITION_REPORT_SCHEMA = (
    "video_knowledge_pipeline.video_decomposition_report.v1"
)
DECOMPOSITION_ADOPTION_SCHEMA = "video_creation_pipeline.decomposition_adoption.v1"

FINDING_STATUSES = {"confirmed", "inferred", "unavailable"}
MODALITIES = (
    "video",
    "audio",
    "transcript",
    "platform_metadata",
    "user_material",
    "link_acquisition",
)
FINDING_DIMENSIONS = {
    "positioning",
    "hook",
    "body_structure",
    "payoff",
    "content_value",
    "emotion",
    "expression",
    "visual_language",
    "subtitle_packaging",
    "voice_delivery",
    "bgm_style",
    "bgm_identity",
    "audiovisual_coordination",
    "reusable_framework",
    "technique",
    "non_copyable_factor",
    "imitation_risk",
    "differentiation",
    "author_identity",
    "behind_the_scenes",
    "performance_metrics",
}
CREATIVE_CATEGORIES = (
    "reusable_frameworks",
    "techniques",
    "non_copyable_factors",
    "imitation_risks",
    "differentiation",
)

_SENSITIVE_DIRECT_EVIDENCE = {
    "bgm_identity": {
        "platform_metadata",
        "reliable_music_recognition",
        "user_material",
    },
    "author_identity": {"platform_metadata", "user_material"},
    "behind_the_scenes": {"user_material"},
    "performance_metrics": {"platform_metadata", "user_material"},
}
_DIMENSION_MODALITY_RULES = {
    "visual_language": {"video"},
    "subtitle_packaging": {"video"},
    "voice_delivery": {"audio"},
    "bgm_style": {"audio"},
    "bgm_identity": {"audio", "platform_metadata", "user_material"},
    "audiovisual_coordination": {"video", "audio"},
    "author_identity": {"platform_metadata", "user_material"},
    "behind_the_scenes": {"user_material"},
    "performance_metrics": {"platform_metadata", "user_material"},
}


def validate_vkp_decomposition_report(report: dict[str, Any]) -> None:
    if report.get("schema") != VKP_DECOMPOSITION_REPORT_SCHEMA:
        raise ContractError("unsupported VKP decomposition report schema")
    if report.get("report_sha256") != payload_sha256(report, "report_sha256"):
        raise ContractError("VKP decomposition report integrity check failed")
    if not str(report.get("report_id") or "").strip():
        raise ContractError("VKP decomposition report id is required")
    if not str(report.get("title") or "").strip():
        raise ContractError("VKP decomposition report title is required")

    _validate_source_artifacts(report.get("source_artifacts"))
    coverage = _validate_modality_coverage(report.get("modality_coverage"))
    findings = _validate_findings(report.get("findings"), coverage)
    _validate_structure_segments(report.get("structure_segments"), findings)
    _validate_creative_strategy(report.get("creative_strategy"), findings)


def build_decomposition_adoption(report_path: str | Path) -> dict[str, Any]:
    source_path, report = _load_report(report_path)
    findings = {str(row["finding_id"]): row for row in report["findings"]}
    strategy: dict[str, list[dict[str, Any]]] = {}
    for category in CREATIVE_CATEGORIES:
        rows: list[dict[str, Any]] = []
        for finding_id in report["creative_strategy"][category]:
            finding = findings[finding_id]
            rows.append(
                {
                    "finding_id": finding_id,
                    "status": finding["status"],
                    "claim": finding["claim"],
                    "planning_status": (
                        "usable_for_planning"
                        if finding["status"] == "confirmed"
                        else "candidate_only"
                    ),
                    "automatic_instruction_allowed": (finding["status"] == "confirmed"),
                    "evidence_ids": list(finding["evidence_ids"]),
                    "evidence_paths": list(finding["evidence_paths"]),
                    "time_range": dict(finding["time_range"]),
                }
            )
        strategy[category] = rows

    status_counts = {
        status: sum(row["status"] == status for row in findings.values())
        for status in sorted(FINDING_STATUSES)
    }
    payload: dict[str, Any] = {
        "schema": DECOMPOSITION_ADOPTION_SCHEMA,
        "source_report": artifact_reference(source_path, "VKP decomposition report"),
        "source_report_sha256": report["report_sha256"],
        "source_report_id": report["report_id"],
        "modality_gate": {
            "coverage": report["modality_coverage"],
            "unavailable_finding_ids": [
                finding_id
                for finding_id, row in findings.items()
                if row["status"] == "unavailable"
            ],
        },
        "status_counts": status_counts,
        "creative_strategy": strategy,
        "recipe_eligible_segment_ids": [
            row["segment_id"]
            for row in report["structure_segments"]
            if row["status"] == "confirmed"
        ],
        "execution_boundary": {
            "status": "derived_candidate_only",
            "network_execution_performed": False,
            "ffmpeg_execution_performed": False,
            "asr_execution_performed": False,
            "source_artifacts_mutated": False,
            "automatic_instruction_from_inferred": False,
            "automatic_instruction_from_unavailable": False,
            "automatic_publish": False,
        },
    }
    payload["adoption_sha256"] = payload_sha256(payload, "adoption_sha256")
    validate_decomposition_adoption(payload)
    return payload


def validate_decomposition_adoption(payload: dict[str, Any]) -> None:
    if payload.get("schema") != DECOMPOSITION_ADOPTION_SCHEMA:
        raise ContractError("unsupported decomposition adoption schema")
    if payload.get("adoption_sha256") != payload_sha256(payload, "adoption_sha256"):
        raise ContractError("decomposition adoption integrity check failed")

    report_path = validate_artifact_reference(
        _object(payload.get("source_report")), "VKP decomposition report"
    )
    report = read_json_object(report_path, "VKP decomposition report")
    validate_vkp_decomposition_report(report)
    if payload.get("source_report_sha256") != report.get(
        "report_sha256"
    ) or payload.get("source_report_id") != report.get("report_id"):
        raise ContractError("decomposition adoption drifted from its report")

    findings = {str(row["finding_id"]): row for row in report["findings"]}
    strategy = _object(payload.get("creative_strategy"))
    for category in CREATIVE_CATEGORIES:
        rows = strategy.get(category)
        if not isinstance(rows, list):
            raise ContractError(f"decomposition adoption {category} must be a list")
        expected_ids = report["creative_strategy"][category]
        if [
            str(row.get("finding_id") or "") for row in rows if isinstance(row, dict)
        ] != expected_ids:
            raise ContractError(
                f"decomposition adoption {category} drifted from its report"
            )
        for row in rows:
            _validate_strategy_projection(row, findings)

    expected_counts = {
        status: sum(row["status"] == status for row in findings.values())
        for status in sorted(FINDING_STATUSES)
    }
    if payload.get("status_counts") != expected_counts:
        raise ContractError("decomposition adoption status counts are stale")
    gate = _object(payload.get("modality_gate"))
    if gate.get("coverage") != report.get("modality_coverage"):
        raise ContractError("decomposition adoption modality coverage is stale")
    expected_unavailable = [
        finding_id
        for finding_id, row in findings.items()
        if row["status"] == "unavailable"
    ]
    if gate.get("unavailable_finding_ids") != expected_unavailable:
        raise ContractError("decomposition adoption unavailable findings are stale")
    expected_segments = [
        row["segment_id"]
        for row in report["structure_segments"]
        if row["status"] == "confirmed"
    ]
    if payload.get("recipe_eligible_segment_ids") != expected_segments:
        raise ContractError("decomposition adoption recipe segments are stale")
    if payload.get("execution_boundary") != {
        "status": "derived_candidate_only",
        "network_execution_performed": False,
        "ffmpeg_execution_performed": False,
        "asr_execution_performed": False,
        "source_artifacts_mutated": False,
        "automatic_instruction_from_inferred": False,
        "automatic_instruction_from_unavailable": False,
        "automatic_publish": False,
    }:
        raise ContractError("decomposition adoption execution boundary is invalid")


def build_reference_recipe_from_decomposition(
    report_path: str | Path,
) -> dict[str, Any]:
    source_path, report = _load_report(report_path)
    shots, bindings = _confirmed_recipe_shots(report)
    if not shots:
        raise ContractError(
            "VKP decomposition report has no confirmed structure segments"
        )
    raw_recipe = {
        "schema": "video_creation_pipeline.reference_recipe.v1",
        "reference": {
            "kind": "vkp_decomposition_report",
            "title": report["title"],
            "source_report": artifact_reference(
                source_path, "VKP decomposition report"
            ),
            "source_report_sha256": report["report_sha256"],
            "source_report_id": report["report_id"],
            "adoption_policy": "confirmed_structure_only",
            "shot_bindings": bindings,
            "excluded_segment_ids": [
                row["segment_id"]
                for row in report["structure_segments"]
                if row["status"] != "confirmed"
            ],
        },
        "shots": shots,
    }
    validate_decomposition_reference_recipe(raw_recipe)

    from .reference_remix import normalize_reference_recipe

    return normalize_reference_recipe(raw_recipe)


def validate_decomposition_reference_recipe(
    recipe: dict[str, Any],
) -> None:
    reference = _object(recipe.get("reference"))
    if reference.get("kind") != "vkp_decomposition_report":
        raise ContractError("recipe is not bound to a VKP decomposition report")
    report_path = validate_artifact_reference(
        _object(reference.get("source_report")), "VKP decomposition report"
    )
    report = read_json_object(report_path, "VKP decomposition report")
    validate_vkp_decomposition_report(report)
    if (
        reference.get("source_report_sha256") != report.get("report_sha256")
        or reference.get("source_report_id") != report.get("report_id")
        or reference.get("adoption_policy") != "confirmed_structure_only"
    ):
        raise ContractError("reference recipe drifted from its decomposition report")

    expected_shots, expected_bindings = _confirmed_recipe_shots(report)
    actual_shots = recipe.get("shots")
    if not isinstance(actual_shots, list):
        raise ContractError("reference recipe shots must be a list")
    if [_recipe_shot_projection(row) for row in actual_shots] != expected_shots:
        raise ContractError(
            "reference recipe contains non-confirmed or stale instructions"
        )
    if reference.get("shot_bindings") != expected_bindings:
        raise ContractError("reference recipe shot bindings are stale")
    expected_excluded = [
        row["segment_id"]
        for row in report["structure_segments"]
        if row["status"] != "confirmed"
    ]
    if reference.get("excluded_segment_ids") != expected_excluded:
        raise ContractError("reference recipe excluded segments are stale")


def write_decomposition_adoption(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_decomposition_adoption(payload)
    return atomic_write_json(payload, output_path)


def write_decomposition_reference_recipe(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_decomposition_reference_recipe(payload)
    return atomic_write_json(payload, output_path)


def _load_report(
    report_path: str | Path,
) -> tuple[Path, dict[str, Any]]:
    path = Path(report_path).expanduser().resolve()
    report = read_json_object(path, "VKP decomposition report")
    validate_vkp_decomposition_report(report)
    return path, report


def _validate_source_artifacts(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ContractError("VKP decomposition report requires source artifacts")
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ContractError("source artifact must be an object")
        artifact_id = str(row.get("artifact_id") or "").strip()
        if not artifact_id or artifact_id in seen:
            raise ContractError("source artifact ids must be non-empty and unique")
        seen.add(artifact_id)
        if not str(row.get("role") or "").strip():
            raise ContractError("source artifact role is required")
        validate_artifact_reference(row, f"source artifact {artifact_id}")


def _validate_modality_coverage(value: Any) -> dict[str, dict[str, Any]]:
    coverage = _object(value)
    if set(coverage) != set(MODALITIES):
        raise ContractError("modality coverage must declare every supported modality")
    result: dict[str, dict[str, Any]] = {}
    for modality in MODALITIES:
        row = _object(coverage.get(modality))
        status = str(row.get("status") or "")
        if status not in {"available", "unavailable"}:
            raise ContractError(f"unsupported modality status for {modality}: {status}")
        evidence_ids = _string_list(row.get("evidence_ids"), f"{modality} evidence ids")
        missing_evidence = _string_list(
            row.get("missing_evidence"),
            f"{modality} missing evidence",
        )
        if status == "available" and not evidence_ids:
            raise ContractError(f"available modality {modality} requires evidence ids")
        if status == "unavailable" and not missing_evidence:
            raise ContractError(
                f"unavailable modality {modality} requires missing evidence"
            )
        result[modality] = row
    return result


def _validate_findings(
    value: Any,
    coverage: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ContractError("VKP decomposition report requires findings")
    result: dict[str, dict[str, Any]] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ContractError("decomposition finding must be an object")
        finding_id = str(row.get("finding_id") or "").strip()
        if not finding_id or finding_id in result:
            raise ContractError("finding ids must be non-empty and unique")
        dimension = str(row.get("dimension") or "").strip()
        if dimension not in FINDING_DIMENSIONS:
            raise ContractError(
                f"unsupported decomposition finding dimension: {dimension}"
            )
        status = str(row.get("status") or "")
        if status not in FINDING_STATUSES:
            raise ContractError(f"unsupported finding status: {status}")
        if not str(row.get("claim") or "").strip():
            raise ContractError(f"finding {finding_id} claim is required")

        source_modalities = _string_list(
            row.get("source_modalities"),
            f"finding {finding_id} source modalities",
        )
        if any(modality not in MODALITIES for modality in source_modalities):
            raise ContractError(f"finding {finding_id} uses an unsupported modality")
        evidence_ids = _string_list(
            row.get("evidence_ids"),
            f"finding {finding_id} evidence ids",
        )
        evidence_paths = _string_list(
            row.get("evidence_paths"),
            f"finding {finding_id} evidence paths",
        )
        missing = _string_list(
            row.get("missing_evidence"),
            f"finding {finding_id} missing evidence",
        )
        _validate_time_range(row.get("time_range"), finding_id)

        if status == "unavailable":
            if evidence_ids or evidence_paths or not missing:
                raise ContractError(
                    f"unavailable finding {finding_id} must expose only missing evidence"
                )
        else:
            if not source_modalities or not (evidence_ids or evidence_paths):
                raise ContractError(
                    f"finding {finding_id} requires source modalities and evidence"
                )
            unavailable_modalities = [
                modality
                for modality in source_modalities
                if coverage[modality]["status"] != "available"
            ]
            if unavailable_modalities:
                raise ContractError(
                    f"finding {finding_id} cites unavailable modalities"
                )
            if status == "inferred" and len(set(evidence_ids + evidence_paths)) < 2:
                raise ContractError(
                    f"inferred finding {finding_id} requires multiple evidence items"
                )
            _validate_dimension_modalities(finding_id, dimension, source_modalities)

        direct_allowed = _SENSITIVE_DIRECT_EVIDENCE.get(dimension)
        if direct_allowed:
            direct_kind = str(row.get("direct_evidence_kind") or "")
            if status == "inferred":
                raise ContractError(
                    f"sensitive finding {finding_id} cannot be inferred"
                )
            if status == "confirmed" and direct_kind not in direct_allowed:
                raise ContractError(
                    f"finding {finding_id} lacks accepted direct evidence"
                )
        result[finding_id] = row
    return result


def _validate_structure_segments(
    value: Any,
    findings: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(value, list):
        raise ContractError("structure segments must be a list")
    seen: set[str] = set()
    previous_end = -1.0
    for row in value:
        if not isinstance(row, dict):
            raise ContractError("structure segment must be an object")
        segment_id = str(row.get("segment_id") or "").strip()
        if not segment_id or segment_id in seen:
            raise ContractError("structure segment ids must be non-empty and unique")
        seen.add(segment_id)
        status = str(row.get("status") or "")
        if status not in FINDING_STATUSES:
            raise ContractError(f"unsupported structure segment status: {status}")
        if not str(row.get("label") or "").strip():
            raise ContractError(f"structure segment {segment_id} label is required")
        if not str(row.get("summary") or "").strip():
            raise ContractError(f"structure segment {segment_id} summary is required")
        start = _finite_seconds(row.get("start_s"), f"{segment_id}.start_s")
        end = _finite_seconds(row.get("end_s"), f"{segment_id}.end_s")
        if end <= start or start < previous_end:
            raise ContractError(
                "structure segments must be ordered, non-overlapping intervals"
            )
        previous_end = end
        finding_ids = _string_list(row.get("finding_ids"), f"{segment_id} finding ids")
        if not finding_ids or any(
            finding_id not in findings for finding_id in finding_ids
        ):
            raise ContractError(
                f"structure segment {segment_id} has invalid finding ids"
            )
        linked_statuses = {findings[finding_id]["status"] for finding_id in finding_ids}
        if status == "confirmed" and linked_statuses != {"confirmed"}:
            raise ContractError(
                f"confirmed segment {segment_id} requires confirmed findings"
            )
        if status == "inferred" and "unavailable" in linked_statuses:
            raise ContractError(
                f"inferred segment {segment_id} cannot cite unavailable findings"
            )
        requirements = _confirmed_requirements(
            row.get("confirmed_requirements"), segment_id
        )
        if status != "confirmed" and any(
            value for key, value in requirements.items() if key != "avoid"
        ):
            raise ContractError(
                f"non-confirmed segment {segment_id} cannot contain instructions"
            )
        if status != "confirmed" and requirements["avoid"]:
            raise ContractError(
                f"non-confirmed segment {segment_id} cannot contain avoid instructions"
            )


def _validate_creative_strategy(
    value: Any,
    findings: dict[str, dict[str, Any]],
) -> None:
    strategy = _object(value)
    if set(strategy) != set(CREATIVE_CATEGORIES):
        raise ContractError("creative strategy must declare every supported category")
    for category in CREATIVE_CATEGORIES:
        finding_ids = _string_list(
            strategy.get(category), f"creative strategy {category}"
        )
        for finding_id in finding_ids:
            if finding_id not in findings:
                raise ContractError(
                    f"creative strategy cites unknown finding {finding_id}"
                )
            if findings[finding_id]["status"] == "unavailable":
                raise ContractError(
                    f"creative strategy cannot adopt unavailable finding {finding_id}"
                )


def _validate_strategy_projection(
    row: Any,
    findings: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(row, dict):
        raise ContractError("creative strategy projection must be an object")
    finding_id = str(row.get("finding_id") or "")
    finding = findings.get(finding_id)
    if not finding:
        raise ContractError("creative strategy projection has an unknown finding")
    expected_status = finding["status"]
    expected_planning = (
        "usable_for_planning" if expected_status == "confirmed" else "candidate_only"
    )
    if (
        row.get("status") != expected_status
        or row.get("claim") != finding["claim"]
        or row.get("planning_status") != expected_planning
        or row.get("automatic_instruction_allowed")
        is not (expected_status == "confirmed")
        or row.get("evidence_ids") != finding["evidence_ids"]
        or row.get("evidence_paths") != finding["evidence_paths"]
        or row.get("time_range") != finding["time_range"]
    ):
        raise ContractError(f"creative strategy projection drifted for {finding_id}")


def _confirmed_recipe_shots(
    report: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shots: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for row in report["structure_segments"]:
        if row["status"] != "confirmed":
            continue
        shot_id = f"decomposition-{len(shots) + 1:03d}"
        requirements = _confirmed_requirements(
            row.get("confirmed_requirements"), row["segment_id"]
        )
        shots.append(
            {
                "shot_id": shot_id,
                "start_s": round(float(row["start_s"]), 3),
                "end_s": round(float(row["end_s"]), 3),
                "script": str(row["summary"]).strip(),
                "requirements": requirements,
            }
        )
        bindings.append(
            {
                "shot_id": shot_id,
                "segment_id": row["segment_id"],
                "finding_ids": list(row["finding_ids"]),
                "status": "confirmed",
            }
        )
    return shots, bindings


def _recipe_shot_projection(value: Any) -> dict[str, Any]:
    row = _object(value)
    requirements = _confirmed_requirements(
        row.get("requirements"), str(row.get("shot_id") or "shot")
    )
    return {
        "shot_id": str(row.get("shot_id") or "").strip(),
        "start_s": round(_finite_seconds(row.get("start_s"), "shot.start_s"), 3),
        "end_s": round(_finite_seconds(row.get("end_s"), "shot.end_s"), 3),
        "script": str(row.get("script") or "").strip(),
        "requirements": requirements,
    }


def _confirmed_requirements(value: Any, label: str) -> dict[str, Any]:
    requirements = _object(value)
    avoid = _string_list(requirements.get("avoid") or [], f"{label} avoid instructions")
    return {
        "scene": str(requirements.get("scene") or "").strip(),
        "action": str(requirements.get("action") or "").strip(),
        "shot_size": str(requirements.get("shot_size") or "").strip(),
        "avoid": avoid,
    }


def _validate_dimension_modalities(
    finding_id: str,
    dimension: str,
    source_modalities: list[str],
) -> None:
    allowed = _DIMENSION_MODALITY_RULES.get(dimension)
    if not allowed:
        if not set(source_modalities).intersection(
            {"video", "audio", "transcript", "user_material"}
        ):
            raise ContractError(
                f"finding {finding_id} lacks a content evidence modality"
            )
        return
    if dimension == "audiovisual_coordination":
        if not allowed.issubset(set(source_modalities)):
            raise ContractError(
                f"finding {finding_id} requires video and audio evidence"
            )
        return
    if not set(source_modalities).intersection(allowed):
        raise ContractError(f"finding {finding_id} uses the wrong evidence modality")


def _validate_time_range(value: Any, finding_id: str) -> None:
    row = _object(value)
    scope = str(row.get("scope") or "")
    if scope == "global":
        if row.get("start_s") is not None or row.get("end_s") is not None:
            raise ContractError(
                f"global finding {finding_id} cannot claim a local interval"
            )
        return
    if scope != "local":
        raise ContractError(f"finding {finding_id} time range scope is invalid")
    start = _finite_seconds(row.get("start_s"), f"{finding_id}.start_s")
    end = _finite_seconds(row.get("end_s"), f"{finding_id}.end_s")
    if end <= start:
        raise ContractError(
            f"finding {finding_id} time range must have positive duration"
        )


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list")
    result: list[str] = []
    for raw in value:
        item = str(raw or "").strip()
        if not item:
            raise ContractError(f"{label} cannot contain empty values")
        if item in result:
            raise ContractError(f"{label} cannot contain duplicates")
        result.append(item)
    return result


def _finite_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative number") from exc
    if result < 0 or result != result or result in {float("inf"), float("-inf")}:
        raise ContractError(f"{label} must be a non-negative finite number")
    return result


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
