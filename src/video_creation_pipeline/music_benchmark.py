from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .music_pipeline import (
    PINNED_MSCLAP_PROVIDER_ID,
    validate_music_retrieval_results,
)

MUSIC_RETRIEVAL_GOLD_SET_SCHEMA = "video_creation_pipeline.music_retrieval_gold_set.v1"
MUSIC_RETRIEVAL_BENCHMARK_SCHEMA = (
    "video_creation_pipeline.music_retrieval_benchmark.v1"
)


def build_music_retrieval_benchmark(
    retrieval_path: str | Path,
    gold_set_path: str | Path,
    *,
    min_query_count: int = 10,
    min_recall_at_5: float = 0.8,
    min_recall_at_10: float = 0.9,
    min_mrr: float = 0.6,
) -> dict[str, Any]:
    retrieval_file = Path(retrieval_path).expanduser().resolve()
    gold_file = Path(gold_set_path).expanduser().resolve()
    retrieval = read_json_object(retrieval_file, "music retrieval results")
    validate_music_retrieval_results(retrieval)
    gold = read_json_object(gold_file, "music retrieval gold set")
    thresholds = _normalize_thresholds(
        min_query_count,
        min_recall_at_5,
        min_recall_at_10,
        min_mrr,
    )
    per_query, metrics, readiness = _evaluate(retrieval, gold, thresholds)
    payload: dict[str, Any] = {
        "schema": MUSIC_RETRIEVAL_BENCHMARK_SCHEMA,
        "retrieval_results": artifact_reference(
            retrieval_file, "music retrieval results"
        ),
        "retrieval_results_sha256": retrieval["retrieval_results_sha256"],
        "provider": dict(retrieval["provider"]),
        "provider_identity": music_retrieval_provider_identity(retrieval["provider"]),
        "provider_identity_sha256": canonical_json_sha256(
            music_retrieval_provider_identity(retrieval["provider"])
        ),
        "gold_set": artifact_reference(gold_file, "music retrieval gold set"),
        "thresholds": thresholds,
        "per_query": per_query,
        "metrics": metrics,
        "readiness": readiness,
        "authority_boundary": {
            "benchmark_only": True,
            "modifies_retrieval_results": False,
            "modifies_music_assets": False,
            "automatic_provider_fallback": False,
            "automatic_publish": False,
        },
    }
    payload["benchmark_sha256"] = payload_sha256(payload, "benchmark_sha256")
    validate_music_retrieval_benchmark(payload)
    return payload


def validate_music_retrieval_benchmark(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MUSIC_RETRIEVAL_BENCHMARK_SCHEMA:
        raise ContractError("unsupported music retrieval benchmark schema")
    if payload.get("benchmark_sha256") != payload_sha256(payload, "benchmark_sha256"):
        raise ContractError("music retrieval benchmark integrity check failed")
    retrieval_path = validate_artifact_reference(
        _object(payload.get("retrieval_results")), "music retrieval results"
    )
    retrieval = read_json_object(retrieval_path, "music retrieval results")
    validate_music_retrieval_results(retrieval)
    expected_identity = music_retrieval_provider_identity(retrieval["provider"])
    expected_identity_sha256 = canonical_json_sha256(expected_identity)
    if (
        payload.get("retrieval_results_sha256")
        != retrieval.get("retrieval_results_sha256")
        or payload.get("provider") != retrieval.get("provider")
        or payload.get("provider_identity") != expected_identity
        or payload.get("provider_identity_sha256") != expected_identity_sha256
    ):
        raise ContractError("music retrieval benchmark targets another provider run")
    gold_path = validate_artifact_reference(
        _object(payload.get("gold_set")), "music retrieval gold set"
    )
    gold = read_json_object(gold_path, "music retrieval gold set")
    thresholds = _normalize_thresholds_from_payload(payload.get("thresholds"))
    expected_queries, expected_metrics, expected_readiness = _evaluate(
        retrieval, gold, thresholds
    )
    if (
        payload.get("per_query") != expected_queries
        or payload.get("metrics") != expected_metrics
        or payload.get("readiness") != expected_readiness
    ):
        raise ContractError("music retrieval benchmark metrics are inaccurate")
    boundary = _object(payload.get("authority_boundary"))
    if boundary.get("benchmark_only") is not True or any(
        boundary.get(field) is not False
        for field in (
            "modifies_retrieval_results",
            "modifies_music_assets",
            "automatic_provider_fallback",
            "automatic_publish",
        )
    ):
        raise ContractError("music retrieval benchmark authority boundary is invalid")


def write_music_retrieval_benchmark(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_music_retrieval_benchmark(payload)
    return atomic_write_json(payload, output_path)


def music_retrieval_provider_identity(provider: dict[str, Any]) -> dict[str, Any]:
    identity = {
        key: provider.get(key)
        for key in (
            "id",
            "model",
            "version",
            "source_commit",
            "execution_location",
            "model_license",
            "model_sha256",
            "model_license_snapshot_sha256",
            "commercial_use_allowed",
            "source_sha256",
            "network_execution_performed",
            "automatic_model_download",
        )
    }
    if identity["id"] == PINNED_MSCLAP_PROVIDER_ID:
        for field in (
            "model_sha256",
            "model_license_snapshot_sha256",
            "source_sha256",
        ):
            value = str(identity.get(field) or "")
            if len(value) != 64:
                raise ContractError(f"pinned Microsoft CLAP provider requires {field}")
        if (
            identity["execution_location"] != "local"
            or identity["network_execution_performed"] is not False
            or identity["automatic_model_download"] is not False
        ):
            raise ContractError(
                "pinned Microsoft CLAP benchmark must remain local and offline"
            )
    return identity


def _evaluate(
    retrieval: dict[str, Any],
    gold: dict[str, Any],
    thresholds: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    queries = _normalize_gold_set(gold)
    matches_by_segment: dict[str, list[dict[str, Any]]] = {}
    for match in retrieval["matches"]:
        matches_by_segment.setdefault(match["segment_id"], []).append(match)
    available = set(matches_by_segment)
    expected = {row["segment_id"] for row in queries}
    if available != expected:
        raise ContractError(
            "music retrieval benchmark gold segments must exactly match the retrieval run"
        )
    rows: list[dict[str, Any]] = []
    recalls_at_5: list[float] = []
    recalls_at_10: list[float] = []
    reciprocal_ranks: list[float] = []
    for query in queries:
        ranked = sorted(
            matches_by_segment[query["segment_id"]],
            key=lambda row: (
                -float(row["semantic_similarity"]),
                row["asset_id"],
                row["window_id"],
            ),
        )
        relevant = {(row["asset_id"], row["window_id"]) for row in query["relevant"]}
        keys = [(row["asset_id"], row["window_id"]) for row in ranked]
        recall_5 = _recall(keys[:5], relevant)
        recall_10 = _recall(keys[:10], relevant)
        first_rank = next(
            (index for index, key in enumerate(keys, 1) if key in relevant), None
        )
        reciprocal_rank = round(1.0 / first_rank, 6) if first_rank else 0.0
        recalls_at_5.append(recall_5)
        recalls_at_10.append(recall_10)
        reciprocal_ranks.append(reciprocal_rank)
        rows.append(
            {
                "segment_id": query["segment_id"],
                "relevant_count": len(relevant),
                "retrieved_count": len(keys),
                "recall_at_5": recall_5,
                "recall_at_10": recall_10,
                "reciprocal_rank": reciprocal_rank,
                "first_relevant_rank": first_rank,
                "top_10": [
                    {
                        "asset_id": row["asset_id"],
                        "window_id": row["window_id"],
                        "semantic_similarity": row["semantic_similarity"],
                        "relevant": (row["asset_id"], row["window_id"]) in relevant,
                    }
                    for row in ranked[:10]
                ],
            }
        )
    count = len(rows)
    metrics = {
        "query_count": count,
        "macro_recall_at_5": _mean(recalls_at_5),
        "macro_recall_at_10": _mean(recalls_at_10),
        "mean_reciprocal_rank": _mean(reciprocal_ranks),
    }
    failures = []
    if count < thresholds["min_query_count"]:
        failures.append("query_count_below_minimum")
    if metrics["macro_recall_at_5"] < thresholds["min_recall_at_5"]:
        failures.append("recall_at_5_below_minimum")
    if metrics["macro_recall_at_10"] < thresholds["min_recall_at_10"]:
        failures.append("recall_at_10_below_minimum")
    if metrics["mean_reciprocal_rank"] < thresholds["min_mrr"]:
        failures.append("mrr_below_minimum")
    passed = not failures
    readiness = {
        "passed": passed,
        "status": "accepted_for_candidate_generation" if passed else "benchmark_failed",
        "failures": failures,
        "automatic_candidate_generation_allowed": passed,
    }
    return rows, metrics, readiness


def _normalize_gold_set(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schema") != MUSIC_RETRIEVAL_GOLD_SET_SCHEMA:
        raise ContractError("unsupported music retrieval gold set schema")
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ContractError("music retrieval gold set requires queries")
    normalized: list[dict[str, Any]] = []
    seen_segments: set[str] = set()
    for raw in raw_queries:
        if not isinstance(raw, dict):
            raise ContractError("music retrieval gold query must be an object")
        segment_id = _required_text(raw.get("segment_id"), "gold segment id")
        if segment_id in seen_segments:
            raise ContractError("music retrieval gold segment ids must be unique")
        seen_segments.add(segment_id)
        raw_relevant = raw.get("relevant")
        if not isinstance(raw_relevant, list) or not raw_relevant:
            raise ContractError("music retrieval gold query requires relevant windows")
        relevant: list[dict[str, str]] = []
        seen_relevant: set[tuple[str, str]] = set()
        for item in raw_relevant:
            if not isinstance(item, dict):
                raise ContractError("music retrieval gold relevance must be an object")
            key = (
                _required_text(item.get("asset_id"), "gold asset id"),
                _required_text(item.get("window_id"), "gold window id"),
            )
            if key in seen_relevant:
                raise ContractError("music retrieval gold relevance is duplicated")
            seen_relevant.add(key)
            relevant.append({"asset_id": key[0], "window_id": key[1]})
        normalized.append({"segment_id": segment_id, "relevant": relevant})
    return normalized


def _normalize_thresholds(
    min_query_count: int,
    min_recall_at_5: float,
    min_recall_at_10: float,
    min_mrr: float,
) -> dict[str, Any]:
    if isinstance(min_query_count, bool) or not isinstance(min_query_count, int):
        raise ContractError("music benchmark query count must be an integer")
    if min_query_count < 1:
        raise ContractError("music benchmark query count must be positive")
    return {
        "min_query_count": min_query_count,
        "min_recall_at_5": _ratio(min_recall_at_5, "minimum Recall@5"),
        "min_recall_at_10": _ratio(min_recall_at_10, "minimum Recall@10"),
        "min_mrr": _ratio(min_mrr, "minimum MRR"),
    }


def _normalize_thresholds_from_payload(value: Any) -> dict[str, Any]:
    raw = _object(value)
    return _normalize_thresholds(
        raw.get("min_query_count"),
        raw.get("min_recall_at_5"),
        raw.get("min_recall_at_10"),
        raw.get("min_mrr"),
    )


def _recall(retrieved: list[tuple[str, str]], relevant: set[tuple[str, str]]) -> float:
    return round(len(set(retrieved) & relevant) / len(relevant), 6)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _required_text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{label} is required")
    return result


def _ratio(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be between zero and one")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be between zero and one") from exc
    if result < 0 or result > 1:
        raise ContractError(f"{label} must be between zero and one")
    return round(result, 6)
