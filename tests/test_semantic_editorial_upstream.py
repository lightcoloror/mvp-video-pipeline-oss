"""Optional offline cross-repository test against an explicitly supplied checkout."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_creation_pipeline.contract_io import atomic_write_json
from video_creation_pipeline.semantic_editorial import (
    build_editorial_packaging_projection,
    build_semantic_editorial_plan,
    validate_editorial_artifact,
    write_editorial_artifact,
)


@unittest.skipUnless(
    os.environ.get("VKP_UPSTREAM_ROOT"), "explicit VKP source checkout required"
)
class UpstreamTimebaseConsumerTests(unittest.TestCase):
    def test_actual_vkp_producer_to_mvp_consumer_double_run(self):
        upstream = Path(os.environ["VKP_UPSTREAM_ROOT"]).resolve()
        sys.path.insert(0, str(upstream / "src"))
        from video_knowledge_pipeline.narration_timebase import (
            prepare_narration_timebase,
        )

        module_spec = importlib.util.spec_from_file_location(
            "vkp_timebase_synthetic_fixture",
            upstream / "tests/test_narration_timebase.py",
        )
        fixture_module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(fixture_module)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMP_ROOT")) as tmp:
            root = Path(tmp)
            bundle = fixture_module._fixture_bundle(root)
            first = prepare_narration_timebase(
                bundle, closure_run_id="mvp-consumer-synthetic"
            )
            self.assertTrue(first["downstream_ready"])
            from video_creation_pipeline.contract_io import read_json_object

            contract = read_json_object(first["contract_path"], "actual VKP output")
            spec = {
                "schema": "video_creation_pipeline.semantic_editorial_spec.v1",
                "decisions": [
                    {
                        "segment_id": row["segment_id"],
                        "action": "keep",
                        "rhetorical_role": "explanation",
                        "redundancy_group": "",
                        "keep_reason": "Preserve source",
                        "removal_risk": "Context loss",
                        "confidence": 1.0,
                        "evidence_refs": [f"semantic:{row['segment_id']}"],
                    }
                    for row in contract["semantic_segments"]
                ],
            }
            spec_path = atomic_write_json(spec, root / "spec.json")
            plan = build_semantic_editorial_plan(
                first["contract_path"], bundle, spec_path
            )
            second = build_semantic_editorial_plan(
                first["contract_path"], bundle, spec_path
            )
            self.assertEqual(plan, second)
            validate_editorial_artifact(plan)
            plan_path = write_editorial_artifact(plan, root / "plan.json")
            projection = build_editorial_packaging_projection(plan_path)
            self.assertEqual(projection["scenes"][1]["start_s"], 30)
            self.assertFalse(plan["boundary"]["execute"])


if __name__ == "__main__":
    unittest.main()
