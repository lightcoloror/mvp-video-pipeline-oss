from __future__ import annotations

import sys

from common import PROJECT, ROOT, media_duration, read_json

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.music_pipeline import (  # noqa: E402
    build_music_asset_profile,
    build_music_candidate_set,
    build_music_edit_plan,
    build_music_intent_curve,
    build_music_retrieval_results,
    write_music_contract,
)


def main() -> None:
    render_dir = PROJECT / "render"
    music_dir = PROJECT / "assets" / "music"
    packaging_path = render_dir / "video_packaging_plan.json"
    packaging = read_json(packaging_path)
    total_duration = round(
        3.0 + max(float(scene["end"]) for scene in packaging["scenes"]), 3
    )

    intent_path = render_dir / "music_intent_curve.json"
    intent = build_music_intent_curve(
        packaging_path,
        [
            {
                "segment_id": "final-program",
                "start_s": 0.0,
                "end_s": total_duration,
                "relationship_mode": "neutral_support",
                "text_query": "清晰克制的极简氛围配乐，无人声，低能量，工作流演示",
                "text_query_en": "clear restrained minimal instrumental background for a workflow demo",
                "target": _descriptors(
                    valence=0.62,
                    arousal=0.28,
                    tension=0.18,
                    intimacy_scale=0.35,
                    warmth=0.58,
                    movement=0.24,
                    engagement=0.18,
                ),
                "vocal_policy": "instrumental_required",
                "narration_density": 0.8,
                "prohibited_tags": ["vocal", "aggressive"],
                "evidence_ids": [
                    f"packaging-scene-{index}"
                    for index, _ in enumerate(packaging["scenes"], 1)
                ],
            }
        ],
        created_by="offline_demo_fixture",
    )
    write_music_contract(intent, intent_path)

    clear_profile_path = _write_profile(
        music_dir / "bgm_clear.wav",
        asset_id="bgm-clear",
        descriptors=_descriptors(
            valence=0.64,
            arousal=0.27,
            tension=0.16,
            intimacy_scale=0.36,
            warmth=0.60,
            movement=0.22,
            engagement=0.16,
        ),
        tags=["instrumental", "minimal", "calm", "background"],
    )
    tense_profile_path = _write_profile(
        music_dir / "bgm_tense.wav",
        asset_id="bgm-tense",
        descriptors=_descriptors(
            valence=0.18,
            arousal=0.82,
            tension=0.90,
            intimacy_scale=0.70,
            warmth=0.12,
            movement=0.78,
            engagement=0.88,
        ),
        tags=["instrumental", "tense", "aggressive", "foreground"],
    )

    retrieval_path = render_dir / "music_retrieval_results.json"
    retrieval = build_music_retrieval_results(
        intent_path,
        [
            {
                "segment_id": "final-program",
                "asset_id": "bgm-clear",
                "window_id": "bgm-clear-main",
                "semantic_similarity": 0.96,
                "continuity_diversity": 0.8,
                "query_text": intent["segments"][0]["text_query_en"],
            },
            {
                "segment_id": "final-program",
                "asset_id": "bgm-tense",
                "window_id": "bgm-tense-main",
                "semantic_similarity": 0.42,
                "continuity_diversity": 0.7,
                "query_text": intent["segments"][0]["text_query_en"],
            },
        ],
        provider={
            "id": "reviewed-offline-fixture",
            "model": "deterministic-demo-retrieval",
            "version": "1",
            "source_commit": "local-fixture-no-model-download",
            "execution_location": "local",
            "model_license": "project-owned-fixture",
        },
    )
    write_music_contract(retrieval, retrieval_path)

    candidates_path = render_dir / "music_candidate_set.json"
    candidates = build_music_candidate_set(
        intent_path,
        retrieval_path,
        [clear_profile_path, tense_profile_path],
        purpose="commercial",
    )
    write_music_contract(candidates, candidates_path)

    plan_path = render_dir / "music_edit_plan.json"
    plan = build_music_edit_plan(candidates_path)
    write_music_contract(plan, plan_path)
    if plan["readiness"]["render_eligible"] is not True:
        raise SystemExit("music selection did not pass the automatic confidence gate")
    selected = plan["selections"][0]
    print(
        f"BGM selected: {selected['asset_id']} "
        f"score={selected['score']} margin={selected['score_margin']}"
    )


def _write_profile(
    audio_path,
    *,
    asset_id: str,
    descriptors: dict[str, float],
    tags: list[str],
):
    duration = round(media_duration(audio_path), 3)
    profile = build_music_asset_profile(
        audio_path,
        asset_id=asset_id,
        duration_s=duration,
        windows=[
            {
                "window_id": f"{asset_id}-main",
                "start_s": 0.0,
                "end_s": duration,
                "structure_label": "ambient-bed",
                "structure_confidence": 0.9,
                "descriptors": descriptors,
                "vocal_probability": 0.0,
                "tags": tags,
                "loopable": True,
            }
        ],
        license_info={
            "status": "allowed",
            "commercial_use_allowed": True,
            "license_uri": f"local://project-owned-demo/{asset_id}",
            "license_snapshot": "generated project test fixture",
            "attribution": "",
        },
        analyzer={
            "id": "reviewed-manual-fixture",
            "model": "deterministic-demo-profile",
            "version": "1",
            "source_commit": "local-fixture-no-model-download",
            "execution_location": "local",
            "model_license": "project-owned-fixture",
            "automatic_fallback_used": False,
        },
    )
    output = PROJECT / "render" / f"{asset_id}_profile.json"
    write_music_contract(profile, output)
    return output


def _descriptors(**values: float) -> dict[str, float]:
    return dict(values)


if __name__ == "__main__":
    main()
