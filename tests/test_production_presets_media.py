"""Tiny generated media through the shared outlet; never personal recordings."""

import json

from video_creation_pipeline.contract_io import (
    atomic_write_json as write,
    artifact_reference as ref,
    file_sha256,
)
from video_creation_pipeline.execution_receipts import (
    execute_ffmpeg_with_receipt as execute,
    probe_media_metadata,
)
from video_creation_pipeline.production_delivery import (
    prepare_render_options,
    final_metadata_check,
)
from video_creation_pipeline.production_presets import inspect_source
from video_creation_pipeline.spoken_delivery import _render_command
from test_production_presets import audio_edit, edit_plan


def test_real_j_l_roomtone_and_export_preserve_duration_without_review_derivatives(
    tmp_path,
):
    source = tmp_path / "synthetic-original.mp4"
    spec = write(
        {"synthetic": True, "duration": 8, "size": "320x240", "fps": 25},
        tmp_path / "fixture.json",
    )
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=8",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-threads",
        "1",
        str(source),
    ]
    command[-1:-1] = [
        "-bsf:v",
        "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
    ]
    execute(
        "render",
        command,
        [{"role": "synthetic_spec", "path": spec}],
        [{"role": "source", "path": source}],
        tmp_path / "fixture-receipt.json",
        guard_resource=True,
    )
    before = file_sha256(source)
    inventory = set(tmp_path.iterdir())
    audit = inspect_source(
        source,
        checks=[{"start": 1, "end": 2, "checks": ["technical"]}],
        sample_timestamps=True,
    )
    assert set(tmp_path.iterdir()) == inventory
    assert audit["cadence"]["status"] == "no_variation_in_samples"
    plan = edit_plan(source)
    for offset in (-0.24, 0.24):
        treatment = audio_edit(plan)
        treatment["split_edits"][0]["video_cut_offset_s"] = offset
        path = write(treatment, tmp_path / f"treatment-{offset}.json")
        options = prepare_render_options(
            plan,
            {
                "deliverable": "video",
                "production": {
                    "export_profile": "web_sdr_1080",
                    "audio_edit": ref(path, "treatment"),
                },
            },
            probe_media_metadata(source),
        )
        target = tmp_path / f"synthetic-result-{offset}.mp4"
        command = _render_command(plan, target, True, options)
        receipt = execute(
            "render",
            command,
            [{"role": "source", "path": source}, {"role": "treatment", "path": path}],
            [{"role": "result", "path": target}],
            tmp_path / f"render-{offset}.json",
            guard_resource=True,
        )
        actual = probe_media_metadata(target)
        qa = final_metadata_check(actual, 4, options)
        assert qa["status"] == "passed", qa
        assert qa["normal_speed_review"] == "pending"
        assert receipt["outlet_id"] == "video_creation_pipeline.single_ffmpeg_outlet"
        assert actual["streams"][0]["width"] == 320
        assert actual["streams"][0]["height"] == 240
        execute(
            "technical_qa",
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"],
            [{"role": "candidate", "path": target}],
            [{"role": "decode_log", "path": tmp_path / f"decode-{offset}.log"}],
            tmp_path / f"decode-{offset}.json",
            capture_log_path=tmp_path / f"decode-{offset}.log",
            guard_resource=True,
        )
    assert file_sha256(source) == before
    (tmp_path / "synthetic-evidence.json").write_text(
        json.dumps(
            {
                "source_unchanged": True,
                "review_created_media": 0,
                "rendered_candidates": 2,
                "human_review": "not_performed",
                "real_media": False,
            }
        ),
        encoding="utf-8",
    )
