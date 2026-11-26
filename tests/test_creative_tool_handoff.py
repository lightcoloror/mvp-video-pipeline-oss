from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_creation_pipeline.contract_io import atomic_write_json, payload_sha256  # noqa: E402
from video_creation_pipeline.creative_tool_handoff import (  # noqa: E402
    REVIEWED_TOOLS,
    build_creative_tool_handoff,
    main,
    validate_creative_tool_handoff,
)
from video_creation_pipeline.errors import ContractError  # noqa: E402
from video_creation_pipeline.generation_contracts import (  # noqa: E402
    build_shot_generation_unit,
    write_generation_contract,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job  # noqa: E402


class CreativeToolHandoffTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(os.environ.get("TEST_TMP_ROOT", ROOT / ".test-runtime"))
        runtime.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="creative-handoff-", dir=runtime)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.review = self.root / "review"
        self.review.mkdir()
        pins = copy.deepcopy(REVIEWED_TOOLS)
        self.manifest = {"files": []}
        for tool, pin in pins.items():
            for relative in pin["files"]:
                data = f"synthetic module: {tool}/{relative}\n".encode()
                local = f"upstream/{pin['repo']}/{relative}"
                file = self.review / local
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)
                digest = hashlib.sha256(data).hexdigest()
                pin["files"][relative] = digest
                self.manifest["files"].append({
                    "repo": pin["repo"], "commit": pin["commit"], "path": relative,
                    "local_path": local, "sha256": digest, "bytes": len(data),
                })
        self.save_manifest()
        self.pin_patch = patch.dict(REVIEWED_TOOLS, pins)
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        bundle = self.root / "bundle"
        shutil.copytree(ROOT / "tests/fixtures/vkp_bundle", bundle)
        self.job = self.root / "edit-job.json"
        write_edit_job(build_edit_job(bundle), self.job)
        self.unit = self.root / "unit.json"
        self.make_unit()

    def make_unit(self, media_type="video", generator="manual"):
        unit = build_shot_generation_unit(
            self.job, "storyboard-0001", generator=generator, media_type=media_type,
            visual_prompt="Synthetic scene; keep the original factual colors.",
        )
        write_generation_contract(unit, self.unit)

    def save_manifest(self):
        self.manifest_path = self.review / "source-manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def build(self, tool="shaders"):
        return build_creative_tool_handoff(tool, self.review, self.unit)

    def test_all_routes_preserve_contract_and_are_deterministic(self):
        for tool in REVIEWED_TOOLS:
            with self.subTest(tool=tool):
                self.make_unit("image" if tool == "photocraft" else "video")
                first = self.build(tool)
                self.assertEqual(first, self.build(tool))
                validate_creative_tool_handoff(first)
                unit = json.loads(self.unit.read_text(encoding="utf-8"))
                self.assertEqual(first["work_request"]["duration_s"], unit["duration_s"])
                self.assertEqual(first["work_request"]["narration"], unit["narration"])
                self.assertEqual(first["source_edit_job"], unit["source_edit_job"])
                self.assertFalse(first["execution_route"]["runtime_verified"])
                self.assertIsNone(first["execution_route"]["command"])

    def test_nonmanual_route_rejected(self):
        self.make_unit(generator="hyperframes")
        with self.assertRaisesRegex(ContractError, "manual route"):
            self.build()

    def test_media_mismatch_rejected(self):
        with self.assertRaisesRegex(ContractError, "media type"):
            self.build("photocraft")

    def test_unknown_tool_rejected(self):
        for tool in ("other", None, []):
            with self.subTest(tool=tool), self.assertRaises(ContractError):
                self.build(tool)

    def test_module_drift_rejected_even_if_manifest_is_rehashed(self):
        row = self.manifest["files"][0]
        (self.review / row["local_path"]).write_bytes(b"changed")
        row["sha256"] = hashlib.sha256(b"changed").hexdigest()
        row["bytes"] = 7
        self.save_manifest()
        with self.assertRaisesRegex(ContractError, "hash drifted"):
            self.build()

    def test_commit_drift_rejected(self):
        self.manifest["files"][0]["commit"] = "0" * 40
        self.save_manifest()
        with self.assertRaisesRegex(ContractError, "commit or path"):
            self.build()

    def test_path_escape_rejected(self):
        self.manifest["files"][0]["local_path"] = "../../outside"
        self.save_manifest()
        with self.assertRaisesRegex(ContractError, "commit or path"):
            self.build()

    def test_missing_and_duplicate_module_rejected(self):
        row = self.manifest["files"].pop(0)
        self.save_manifest()
        with self.assertRaisesRegex(ContractError, "missing or duplicated"):
            self.build()
        self.manifest["files"].extend([row, row])
        self.save_manifest()
        with self.assertRaisesRegex(ContractError, "missing or duplicated"):
            self.build()

    def test_authority_and_request_tampering_rejected_after_rehash(self):
        for section, field, value in (
            ("execution_route", "runtime_verified", True),
            ("execution_route", "command", "launch"),
            ("authority_boundary", "automatic_publish", True),
            ("work_request", "duration_s", 999),
            ("work_request", "visual_prompt", "replacement"),
        ):
            with self.subTest(field=field):
                packet = self.build()
                packet[section][field] = value
                packet["handoff_sha256"] = payload_sha256(packet, "handoff_sha256")
                with self.assertRaisesRegex(ContractError, "content or authority"):
                    validate_creative_tool_handoff(packet)

    def test_upstream_job_drift_rejected(self):
        packet = self.build()
        self.job.write_text(self.job.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_creative_tool_handoff(packet)

    def test_bound_inputs_cannot_be_overwritten(self):
        packet = self.build()
        for target in (self.unit, self.job, self.manifest_path):
            with self.subTest(target=target), self.assertRaisesRegex(ContractError, "overwrite"):
                atomic_write_json(packet, target)

    def test_cli_build_and_validate(self):
        output = self.root / "handoff.json"
        self.assertEqual(main(["build", "--tool", "shaders", "--source-review-root",
                               str(self.review), "--shot-unit", str(self.unit),
                               "--output", str(output)]), 0)
        self.assertEqual(main(["validate", "--handoff", str(output)]), 0)


if __name__ == "__main__":
    unittest.main()
