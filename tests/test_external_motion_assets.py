from __future__ import annotations

import shutil
import sys
import unittest
import wave
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.external_motion_assets import (
    CLIPKIT_COMMIT,
    CLIPKIT_PACK_ID,
    ONDA_COMMIT,
    REMOTION_BITS_COMMIT,
    bootstrap_clipkit_sfx_pack,
    build_external_motion_catalog,
    build_external_motion_selection_plan,
    profile_paths_from_clipkit_pack_receipt,
    validate_external_motion_catalog,
    validate_external_motion_selection_plan,
    write_external_motion_contract,
)
from video_creation_pipeline.remotion_project import (
    _external_motion_selection_projection,
)


class ExternalMotionAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".external-motion-runtime" / f"test-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.onda = self.root / "onda"
        self.bits = self.root / "remotion-bits"
        self.clipkit = self.root / "clipkit"
        self._write_onda()
        self._write_bits()
        self._write_clipkit()
        self.git_patch = patch(
            "video_creation_pipeline.external_motion_assets._git_head",
            side_effect=self._git_head,
        )
        self.git_patch.start()

    def tearDown(self) -> None:
        self.git_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_catalog_and_selection_plan_are_hash_bound_without_arbitrary_fallback(
        self,
    ) -> None:
        with patch(
            "video_creation_pipeline.external_motion_assets._run_clipkit",
            side_effect=self._clipkit_runtime,
        ):
            catalog = build_external_motion_catalog(
                self.onda,
                remotion_bits_root=self.bits,
                clipkit_root=self.clipkit,
            )
            catalog_path = write_external_motion_contract(
                catalog, self.root / "catalog.json"
            )
            semantic = {
                "schema": "video_creation_pipeline.semantic_motion_plan.v1",
                "cues": [
                    {
                        "cue_id": "cue-number",
                        "story_segment_ref": "scene-01",
                        "information_role": "number",
                    },
                    {
                        "cue_id": "cue-risk",
                        "story_segment_ref": "scene-02",
                        "information_role": "evidence",
                    },
                ],
            }
            semantic["plan_sha256"] = payload_sha256(semantic, "plan_sha256")
            semantic_path = atomic_write_json(
                semantic, self.root / "semantic-motion.json"
            )
            with patch(
                "video_creation_pipeline.external_motion_assets."
                "validate_semantic_motion_plan"
            ):
                plan = build_external_motion_selection_plan(catalog_path, semantic_path)
                validate_external_motion_selection_plan(plan)
                plan_path = write_external_motion_contract(
                    plan, self.root / "selection.json"
                )
                projection = _external_motion_selection_projection(
                    {
                        "external_motion_selection_plan": artifact_reference(
                            plan_path, "external motion selection plan"
                        )
                    }
                )

            self.assertEqual(7, catalog["counts"]["motion_items"])
            self.assertEqual(6, catalog["counts"]["sound_items"])
            self.assertEqual(
                "count-up", plan["selections"][0]["motion_component"]["item_id"]
            )
            self.assertEqual(
                "clipkit-soft-tick", plan["selections"][0]["sound_effect"]["asset_id"]
            )
            self.assertFalse(plan["selection_policy"]["arbitrary_fallback"])
            self.assertEqual("count-up", projection[0]["componentId"])
            self.assertEqual("clipkit-soft-tick", projection[0]["soundAssetId"])
            self.assertTrue(projection[0]["centralTimelineAudio"])
            self.assertTrue(projection[0]["componentEmbeddedAudioForbidden"])

            tampered = deepcopy(catalog)
            tampered["motion_items"][0]["title"] = "drifted"
            tampered["catalog_sha256"] = payload_sha256(tampered, "catalog_sha256")
            with self.assertRaisesRegex(ContractError, "drifted"):
                validate_external_motion_catalog(tampered)

    def test_clipkit_pack_uses_upstream_runtime_and_existing_profiles(self) -> None:
        with patch(
            "video_creation_pipeline.external_motion_assets._run_clipkit",
            side_effect=self._clipkit_runtime,
        ):
            first = bootstrap_clipkit_sfx_pack(
                self.clipkit, self.root / "pack", purpose="commercial"
            )
            second = bootstrap_clipkit_sfx_pack(
                self.clipkit, self.root / "pack", purpose="commercial"
            )
            profiles = profile_paths_from_clipkit_pack_receipt(first["receipt"])

        self.assertEqual(CLIPKIT_PACK_ID, first["payload"]["pack_id"])
        self.assertEqual(
            first["payload"]["pack_sha256"], second["payload"]["pack_sha256"]
        )
        self.assertEqual(6, len(profiles))
        self.assertTrue(all(path.is_file() for path in profiles))
        self.assertFalse(first["payload"]["selection_policy"]["arbitrary_fallback"])

    def test_source_commit_drift_fails_closed(self) -> None:
        with patch(
            "video_creation_pipeline.external_motion_assets._git_head",
            return_value="0" * 40,
        ):
            with self.assertRaisesRegex(ContractError, "Onda source commit"):
                build_external_motion_catalog(self.onda)

    def _git_head(self, root: Path) -> str:
        if root.name == "onda":
            return ONDA_COMMIT
        if root.name == "remotion-bits":
            return REMOTION_BITS_COMMIT
        return CLIPKIT_COMMIT

    def _clipkit_runtime(self, root: Path, *, action: str, **kwargs: object):
        del root
        if action == "list":
            names = [
                "whoosh",
                "sweep",
                "impact",
                "boom",
                "braam",
                "riser",
                "downlifter",
                "pop",
                "tick",
                "notification",
                "ding-correct",
                "buzzer-wrong",
                "power-up",
                "coin",
                "shimmer",
                "glitch",
            ]
            return [
                {"name": name, "label": name, "category": "fixture", "tags": []}
                for name in names
            ]
        output = Path(str(kwargs["output"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(44100)
            audio.writeframes(b"\x00\x00\x00\x00" * 4410)
        return {
            "engine": "@clipkit/sfx",
            "package_version": "1.0.0",
            "source_commit": CLIPKIT_COMMIT,
            "source_name": kwargs.get("name"),
            "seed": kwargs.get("seed"),
            "tune": kwargs.get("tune"),
            "finish_profile": "restrained-v1",
            "sample_rate": 44100,
            "frames": 4410,
            "node_major": 22,
        }

    def _write_onda(self) -> None:
        (self.onda / "registry" / "r").mkdir(parents=True)
        (self.onda / "LICENSE").write_text("MIT fixture\n", encoding="utf-8")
        items = []
        for name in (
            "chapter-card",
            "node-graph",
            "timeline",
            "split-screen",
            "count-up",
            "highlight",
        ):
            item = {
                "name": name,
                "title": name,
                "description": f"{name} fixture",
                "categories": ["fixture"],
                "dependencies": ["remotion"],
            }
            items.append(item)
            atomic_write_json(item, self.onda / "registry" / "r" / f"{name}.json")
        atomic_write_json(
            {"name": "onda", "items": items},
            self.onda / "registry" / "registry.json",
        )

    def _write_bits(self) -> None:
        self.bits.mkdir()
        atomic_write_json(
            {"name": "remotion-bits", "license": "MIT"},
            self.bits / "package.json",
        )
        source = self.bits / "src" / "Fade.tsx"
        source.parent.mkdir()
        source.write_text("export const Fade = () => null;\n", encoding="utf-8")
        atomic_write_json(
            {
                "name": "remotion-bits",
                "items": [
                    {
                        "name": "bit-fade-in",
                        "title": "Fade",
                        "description": "fixture",
                        "type": "bit",
                        "dependencies": [],
                        "files": [{"relativePath": "src/Fade.tsx"}],
                    }
                ],
            },
            self.bits / "registry.json",
        )

    def _write_clipkit(self) -> None:
        package = self.clipkit / "packages" / "sfx"
        source = package / "src"
        source.mkdir(parents=True)
        atomic_write_json(
            {
                "name": "@clipkit/sfx",
                "version": "1.0.0",
                "license": "Apache-2.0",
                "dependencies": {},
            },
            package / "package.json",
        )
        (package / "LICENSE").write_text("Apache-2.0 fixture\n", encoding="utf-8")
        for name in ("catalog.ts", "encode-wav.ts", "finish.ts", "index.ts", "sfx.ts"):
            (source / name).write_text("export {};\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
