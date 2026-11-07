from __future__ import annotations

import math
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
from .reference_remix import (
    MATERIAL_CANDIDATES_SCHEMA,
    material_candidate_map,
)

MATERIAL_INDEX_GOLDSET_SCHEMA = "video_creation_pipeline.material_index_goldset.v1"
MATERIAL_INDEX_RESULTS_SCHEMA = (
    "video_creation_pipeline.material_index_query_results.v1"
)
MATERIAL_INDEX_BENCHMARK_SCHEMA = "video_creation_pipeline.material_index_benchmark.v1"
MATERIAL_INDEX_SHOT_QUERIES_SCHEMA = (
    "video_creation_pipeline.material_index_shot_queries.v1"
)

SQLITE_VEC_VERSION = "v0.1.7"
SQLITE_VEC_COMMIT = "633eecf5067ab12ef331b3c4500c765f8e6d6da0"


def build_material_index_benchmark(
    goldset_path: str | Path,
    results_path: str | Path,
    *,
    min_recall_at_5: float = 0.8,
    min_recall_at_10: float = 0.9,
    min_filtered_recall: float = 0.8,
    min_adjacent_coverage: float = 0.8,
) -> dict[str, Any]:
    gold_path = Path(goldset_path).expanduser().resolve()
    query_results_path = Path(results_path).expanduser().resolve()
    goldset = read_json_object(gold_path, "material index goldset")
    results = read_json_object(query_results_path, "material index query results")
    gold = _validate_goldset(goldset)
    hits = _validate_query_results(results)
    thresholds = {
        "recall_at_5": _ratio(min_recall_at_5, "min recall@5"),
        "recall_at_10": _ratio(min_recall_at_10, "min recall@10"),
        "filtered_recall": _ratio(min_filtered_recall, "min filtered recall"),
        "adjacent_coverage": _ratio(min_adjacent_coverage, "min adjacent coverage"),
    }
    metrics = _metrics(gold, hits)
    lifecycle = _validated_lifecycle(results.get("lifecycle"))
    metric_pass = all(metrics[name] >= minimum for name, minimum in thresholds.items())
    lifecycle_pass = all(row["passed"] for row in lifecycle.values())
    benchmark: dict[str, Any] = {
        "schema": MATERIAL_INDEX_BENCHMARK_SCHEMA,
        "goldset": artifact_reference(gold_path, "material index goldset"),
        "query_results": artifact_reference(
            query_results_path, "material index query results"
        ),
        "provider": dict(results["provider"]),
        "thresholds": thresholds,
        "metrics": metrics,
        "lifecycle": lifecycle,
        "gate": {
            "passed": metric_pass and lifecycle_pass,
            "candidate_source_enabled": metric_pass and lifecycle_pass,
            "timeline_authority": False,
            "metadata_authority": False,
            "run_registry_authority": False,
            "next_action": "enable_as_candidate_source"
            if metric_pass and lifecycle_pass
            else "keep_existing_candidate_sources",
        },
    }
    benchmark["benchmark_sha256"] = payload_sha256(benchmark, "benchmark_sha256")
    validate_material_index_benchmark(benchmark)
    return benchmark


def validate_material_index_benchmark(benchmark: dict[str, Any]) -> None:
    if benchmark.get("schema") != MATERIAL_INDEX_BENCHMARK_SCHEMA:
        raise ContractError("unsupported material index benchmark schema")
    if benchmark.get("benchmark_sha256") != payload_sha256(
        benchmark, "benchmark_sha256"
    ):
        raise ContractError("material index benchmark integrity check failed")
    gold_path = validate_artifact_reference(
        _object(benchmark.get("goldset")), "material index goldset"
    )
    results_path = validate_artifact_reference(
        _object(benchmark.get("query_results")),
        "material index query results",
    )
    gold = _validate_goldset(read_json_object(gold_path, "material index goldset"))
    results_payload = read_json_object(results_path, "material index query results")
    hits = _validate_query_results(results_payload)
    if benchmark.get("provider") != results_payload.get("provider"):
        raise ContractError("material index provider provenance drifted")
    expected_metrics = _metrics(gold, hits)
    if benchmark.get("metrics") != expected_metrics:
        raise ContractError("material index benchmark metrics are inaccurate")
    thresholds = _object(benchmark.get("thresholds"))
    expected_thresholds = {
        name: _ratio(thresholds.get(name), f"threshold {name}")
        for name in (
            "recall_at_5",
            "recall_at_10",
            "filtered_recall",
            "adjacent_coverage",
        )
    }
    lifecycle = _validated_lifecycle(results_payload.get("lifecycle"))
    if benchmark.get("lifecycle") != lifecycle:
        raise ContractError("material index lifecycle evidence drifted")
    expected_pass = all(
        expected_metrics[name] >= minimum
        for name, minimum in expected_thresholds.items()
    ) and all(row["passed"] for row in lifecycle.values())
    gate = _object(benchmark.get("gate"))
    if (
        gate.get("passed") is not expected_pass
        or gate.get("candidate_source_enabled") is not expected_pass
        or gate.get("timeline_authority") is not False
        or gate.get("metadata_authority") is not False
        or gate.get("run_registry_authority") is not False
        or gate.get("next_action")
        != (
            "enable_as_candidate_source"
            if expected_pass
            else "keep_existing_candidate_sources"
        )
    ):
        raise ContractError("material index benchmark gate is inaccurate")


def build_material_candidates_from_index(
    benchmark_path: str | Path,
    shot_queries_path: str | Path,
) -> dict[str, Any]:
    benchmark_file = Path(benchmark_path).expanduser().resolve()
    mapping_file = Path(shot_queries_path).expanduser().resolve()
    benchmark = read_json_object(benchmark_file, "material index benchmark")
    validate_material_index_benchmark(benchmark)
    if benchmark.get("gate", {}).get("candidate_source_enabled") is not True:
        raise ContractError("material index benchmark has not enabled this provider")
    query_results_path = validate_artifact_reference(
        _object(benchmark.get("query_results")),
        "material index query results",
    )
    results = read_json_object(query_results_path, "material index query results")
    by_query = _validate_query_results(results)
    mapping = read_json_object(mapping_file, "material index shot queries")
    if mapping.get("schema") != MATERIAL_INDEX_SHOT_QUERIES_SCHEMA:
        raise ContractError(
            f"shot queries must use {MATERIAL_INDEX_SHOT_QUERIES_SCHEMA}"
        )
    raw_shots = mapping.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        raise ContractError("material index shot queries require shots")
    shots: list[dict[str, Any]] = []
    allowed_ids: set[str] = set()
    seen: set[str] = set()
    for raw in raw_shots:
        if not isinstance(raw, dict):
            raise ContractError("material index shot query must be an object")
        shot_id = str(raw.get("shot_id") or "").strip()
        query_id = str(raw.get("query_id") or "").strip()
        if not shot_id or shot_id in seen or query_id not in by_query:
            raise ContractError("material index shot query identity is invalid")
        seen.add(shot_id)
        limit = _positive_int(raw.get("max_candidates", 10), "max candidates")
        candidates = []
        for hit in by_query[query_id][:limit]:
            asset_id = hit["asset_id"]
            allowed_ids.add(asset_id)
            candidates.append(
                {
                    "asset_id": asset_id,
                    "path": hit["path"],
                    "confidence": hit["score"],
                    "score_components": {
                        "vector_similarity": hit["score"],
                    },
                    "source_media_id": hit["source_media_id"],
                    "room_id": hit["room_id"],
                    "composition_id": hit["composition_id"],
                    "reason": f"sqlite-vec query {query_id} rank {hit['rank']}",
                    "provider_provenance": {
                        "name": "sqlite-vec",
                        "version": SQLITE_VEC_VERSION,
                        "source_commit": SQLITE_VEC_COMMIT,
                        "benchmark_sha256": benchmark["benchmark_sha256"],
                        "query_id": query_id,
                        "rank": hit["rank"],
                    },
                }
            )
        shots.append({"shot_id": shot_id, "candidates": candidates})
    payload: dict[str, Any] = {
        "schema": MATERIAL_CANDIDATES_SCHEMA,
        "allowed_material_ids": sorted(allowed_ids),
        "shots": shots,
        "candidate_source": {
            "provider": dict(benchmark["provider"]),
            "benchmark": artifact_reference(benchmark_file, "material index benchmark"),
            "shot_queries": artifact_reference(
                mapping_file, "material index shot queries"
            ),
            "acceptance_status": "candidate_only",
            "human_review_required": True,
            "mutates_timeline": False,
            "mutates_material_metadata": False,
        },
    }
    material_candidate_map(payload)
    return payload


def write_material_index_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    if payload.get("schema") == MATERIAL_INDEX_BENCHMARK_SCHEMA:
        validate_material_index_benchmark(payload)
    elif payload.get("schema") == MATERIAL_CANDIDATES_SCHEMA:
        material_candidate_map(payload)
        source = _object(payload.get("candidate_source"))
        validate_artifact_reference(
            _object(source.get("benchmark")), "material index benchmark"
        )
        validate_artifact_reference(
            _object(source.get("shot_queries")), "material index shot queries"
        )
    else:
        raise ContractError("unsupported material index contract")
    return atomic_write_json(payload, output_path)


def _validate_goldset(
    payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if payload.get("schema") != MATERIAL_INDEX_GOLDSET_SCHEMA:
        raise ContractError(f"goldset must use {MATERIAL_INDEX_GOLDSET_SCHEMA}")
    rows = payload.get("queries")
    if not isinstance(rows, list) or not rows:
        raise ContractError("material index goldset requires queries")
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("material index gold query must be an object")
        query_id = str(raw.get("query_id") or "").strip()
        if not query_id or query_id in result:
            raise ContractError("material index gold query ids must be unique")
        relevant = _string_set(raw.get("relevant_asset_ids"), "relevant assets")
        required_tags = _string_set(
            raw.get("required_tags") or [], "required tags", allow_empty=True
        )
        adjacent = _string_set(
            raw.get("adjacent_asset_ids") or [],
            "adjacent assets",
            allow_empty=True,
        )
        result[query_id] = {
            "relevant_asset_ids": relevant,
            "required_tags": required_tags,
            "adjacent_asset_ids": adjacent,
        }
    return result


def _validate_query_results(
    payload: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    if payload.get("schema") != MATERIAL_INDEX_RESULTS_SCHEMA:
        raise ContractError(f"query results must use {MATERIAL_INDEX_RESULTS_SCHEMA}")
    provider = _object(payload.get("provider"))
    if (
        provider.get("name") != "sqlite-vec"
        or provider.get("version") != SQLITE_VEC_VERSION
        or provider.get("source_commit") != SQLITE_VEC_COMMIT
        or not str(provider.get("embedding_model") or "").strip()
        or not str(provider.get("embedding_model_version") or "").strip()
    ):
        raise ContractError("query results are not from the reviewed sqlite-vec")
    rows = payload.get("queries")
    if not isinstance(rows, list) or not rows:
        raise ContractError("material index query results require queries")
    result: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("material index query result must be an object")
        query_id = str(raw.get("query_id") or "").strip()
        hits = raw.get("hits")
        if not query_id or query_id in result or not isinstance(hits, list):
            raise ContractError("material index query result identity is invalid")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rank, hit in enumerate(hits, 1):
            if not isinstance(hit, dict):
                raise ContractError("material index hit must be an object")
            asset_id = str(hit.get("asset_id") or "").strip()
            path = str(hit.get("path") or "").strip()
            score = _ratio(hit.get("score"), "material index score")
            if not asset_id or asset_id in seen or not path:
                raise ContractError("material index hit identity is invalid")
            seen.add(asset_id)
            normalized.append(
                {
                    "asset_id": asset_id,
                    "path": path,
                    "score": score,
                    "rank": rank,
                    "tags": _string_set(
                        hit.get("tags") or [],
                        "material index hit tags",
                        allow_empty=True,
                    ),
                    "source_media_id": str(
                        hit.get("source_media_id") or asset_id
                    ).strip(),
                    "room_id": str(hit.get("room_id") or "").strip(),
                    "composition_id": str(hit.get("composition_id") or "").strip(),
                }
            )
        result[query_id] = normalized
    return result


def _metrics(
    gold: dict[str, dict[str, Any]],
    results: dict[str, list[dict[str, Any]]],
) -> dict[str, float]:
    if set(gold) != set(results):
        raise ContractError("goldset and result query ids must match exactly")
    recall_5: list[float] = []
    recall_10: list[float] = []
    filtered: list[float] = []
    adjacent: list[float] = []
    for query_id, expected in gold.items():
        hits = results[query_id]
        relevant = set(expected["relevant_asset_ids"])
        recall_5.append(_recall(relevant, hits[:5]))
        recall_10.append(_recall(relevant, hits[:10]))
        required = set(expected["required_tags"])
        filtered_hits = [
            row for row in hits[:10] if required.issubset(set(row["tags"]))
        ]
        filtered.append(_recall(relevant, filtered_hits))
        adjacent_ids = set(expected["adjacent_asset_ids"])
        if adjacent_ids:
            adjacent.append(
                1.0
                if any(row["asset_id"] in adjacent_ids for row in hits[:10])
                else 0.0
            )
    return {
        "recall_at_5": _average(recall_5),
        "recall_at_10": _average(recall_10),
        "filtered_recall": _average(filtered),
        "adjacent_coverage": _average(adjacent) if adjacent else 1.0,
        "query_count": len(gold),
    }


def _validated_lifecycle(value: Any) -> dict[str, dict[str, Any]]:
    source = _object(value)
    result: dict[str, dict[str, Any]] = {}
    for name in (
        "incremental_insert",
        "full_rebuild",
        "deletion_cleanup",
        "model_version_rebuild",
    ):
        row = _object(source.get(name))
        if not isinstance(row.get("passed"), bool):
            raise ContractError(f"sqlite-vec lifecycle {name} must report passed")
        duration = _nonnegative_number(
            row.get("duration_ms"), f"sqlite-vec lifecycle {name} duration"
        )
        result[name] = {
            "passed": row["passed"],
            "duration_ms": duration,
            "evidence": str(row.get("evidence") or "").strip(),
        }
        if not result[name]["evidence"]:
            raise ContractError(f"sqlite-vec lifecycle {name} requires evidence")
    return result


def _recall(relevant: set[str], hits: list[dict[str, Any]]) -> float:
    found = {row["asset_id"] for row in hits}
    return round(len(relevant & found) / len(relevant), 4)


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 4)


def _string_set(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list")
    result = [str(row).strip() for row in value]
    if any(not row for row in result) or len(result) != len(set(result)):
        raise ContractError(f"{label} must contain unique non-empty values")
    if not result and not allow_empty:
        raise ContractError(f"{label} must not be empty")
    return result


def _ratio(value: Any, label: str) -> float:
    number = _nonnegative_number(value, label)
    if number > 1:
        raise ContractError(f"{label} must be between 0 and 1")
    return round(number, 4)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if number <= 0 or float(number) != float(value):
        raise ContractError(f"{label} must be a positive integer")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative finite number") from exc
    if number < 0 or not math.isfinite(number):
        raise ContractError(f"{label} must be a non-negative finite number")
    return round(number, 4)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
