from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from test_semantic_editorial import make_fixture

from video_creation_pipeline.contract_io import atomic_write_json, file_sha256, read_json_object
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.motion_entry_index import (
    BINDING_SCHEMA,
    DEFAULT_INDEX_PATH,
    INDEX_SCHEMA,
    TIER_T1,
    TIER_T2,
    build_motion_entry_index,
    find_motion_entries,
    load_motion_entry_index,
    resolve_import_closure,
    unpack_motion_entry,
    validate_motion_reference_binding,
)
from video_creation_pipeline.semantic_editorial import (
    build_editorial_packaging_projection,
    build_semantic_editorial_plan,
)
from video_creation_pipeline.semantic_editorial_cli import main
from video_creation_pipeline.semantic_render_plan import (
    build_semantic_render_plan,
    validate_semantic_render_plan,
)


class MotionEntryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMP_ROOT"))
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_build_is_deterministic_and_covers_all_three_sources(self):
        out_a = self.root / "index-a.json"
        out_b = self.root / "index-b.json"
        idx_a = build_motion_entry_index(output_path=out_a)
        idx_b = build_motion_entry_index(output_path=out_b)
        self.assertEqual(idx_a["schema"], INDEX_SCHEMA)
        self.assertEqual(idx_a["index_sha256"], idx_b["index_sha256"])
        self.assertEqual(out_a.read_bytes(), out_b.read_bytes())
        self.assertEqual(idx_a["counts_by_source"]["local_case_examples"], 13)
        self.assertEqual(idx_a["counts_by_source"]["video-shotcraft"], 152)
        self.assertEqual(idx_a["counts_by_source"]["lemo-opuscar"], 66)
        self.assertEqual(idx_a["entry_count"], 231)
        loaded = load_motion_entry_index(out_a)
        self.assertEqual(loaded["index_sha256"], idx_a["index_sha256"])

    def test_load_rejects_tampered_index_sha(self):
        out = self.root / "tampered-index.json"
        idx = build_motion_entry_index(output_path=out)
        idx["entries"][0]["title"] = "tampered"
        out.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "integrity drift"):
            load_motion_entry_index(out)

    def test_default_policy_index_is_present_and_valid(self):
        self.assertTrue(DEFAULT_INDEX_PATH.is_file())
        loaded = load_motion_entry_index()
        self.assertEqual(loaded["entry_count"], 231)

    def test_find_ranks_t1_local_cases_and_handles_bilingual_queries(self):
        morph = find_motion_entries(query="图标变形转场", limit=5)
        morph_ids = [c["entry_id"] for c in morph["candidates"]]
        self.assertIn("local:case-20261125-shotcraft-icon-morph", morph_ids[:3])
        self.assertEqual(morph["candidates"][0]["tier"], TIER_T1)
        self.assertTrue(morph["candidates"][0]["preview_stills"])

        rig = find_motion_entries(query="插画角色微动", limit=5)
        rig_ids = [c["entry_id"] for c in rig["candidates"]]
        self.assertIn("local:case-20261125-pelican-rig-motion", rig_ids[:3])
        self.assertIn("local:case-20261115-ip-illustration-movement", rig_ids[:3])

        evidence = find_motion_entries(
            visual_job="evidence", tone="严肃", query="档案证据拆解", limit=8
        )
        self.assertTrue(evidence["candidates"])
        sources = {c["source_project"] for c in evidence["candidates"]}
        self.assertTrue(sources & {"lemo-opuscar", "juzu", "wise-motion"})

        with self.assertRaises(ContractError):
            find_motion_entries(visual_job="nonexistent_job")
        with self.assertRaises(ContractError):
            find_motion_entries(tone="unknown_tone_xyz")

    def test_resolve_import_closure_handles_ts_html_css_and_blocks_escape(self):
        proj = self.root / "mini-proj"
        (proj / "src/utils").mkdir(parents=True)
        (proj / "src/utils/ease.ts").write_text("export const ease = (t: number) => t;\n", encoding="utf-8")
        (proj / "src/style.css").write_text("body { margin: 0; }\n", encoding="utf-8")
        (proj / "src/index.tsx").write_text(
            "import { ease } from './utils/ease';\nimport './style.css';\nexport const x = ease(1);\n",
            encoding="utf-8",
        )
        closure = resolve_import_closure(proj / "src/index.tsx", proj)
        rels = [row["relative"] for row in closure["files"]]
        self.assertEqual(rels, ["src/index.tsx", "src/utils/ease.ts", "src/style.css"])
        self.assertFalse(closure["truncated"])

        # HTML local <script src> and <link href>
        (proj / "demo").mkdir()
        (proj / "demo/film.js").write_text("console.log('hi');\n", encoding="utf-8")
        (proj / "demo/index.html").write_text(
            "<link rel='stylesheet' href='../src/style.css'><script src='film.js'></script>",
            encoding="utf-8",
        )
        html_closure = resolve_import_closure(proj / "demo/index.html", proj)
        html_rels = [row["relative"] for row in html_closure["files"]]
        self.assertIn("demo/film.js", html_rels)
        self.assertIn("src/style.css", html_rels)

        # Path escape outside boundary root must fail closed
        (self.root / "outside.ts").write_text("export const secret = 1;\n", encoding="utf-8")
        (proj / "src/escape.tsx").write_text("import { secret } from '../../outside';\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "escapes boundary root"):
            resolve_import_closure(proj / "src/escape.tsx", proj)

    def test_unpack_all_three_source_types_and_validate_binding_gate(self):
        for entry_id in (
            "local:case-20261125-shotcraft-icon-morph",
            "local:case-20261119-wise-practical-chart",
            "shotcraft:ui-to-brand-morph",
            "lemo:caption-news",
        ):
            with self.subTest(entry_id=entry_id):
                unpacked = unpack_motion_entry(entry_id)
                self.assertTrue(unpacked["inspected_source_files"])
                if entry_id.startswith("local:"):
                    self.assertEqual(unpacked["preview_mp4_check"]["status"], "verified")
                    self.assertTrue(unpacked["preview_stills"])
                if entry_id.startswith("lemo:"):
                    rels = [f["relative"] for f in unpacked["inspected_source_files"]]
                    self.assertTrue(any(r.endswith("STYLE.md") for r in rels))
                    self.assertTrue(unpacked["preview_stills"])
                binding = copy.deepcopy(unpacked["binding_template"])
                binding["borrow"] = ["复用阶段化变形节奏与帧时钟结构"]
                binding["change"] = ["替换为当前话题标题文案与低饱和配色 token"]
                binding["forbid"] = ["禁止拷贝上游示例品牌文案与未授权音效"]
                res = validate_motion_reference_binding(binding)
                self.assertEqual(res["status"], "valid_motion_reference_binding")
                self.assertEqual(res["entry_id"], entry_id)

    def test_binding_gate_fail_closed_negative_cases(self):
        unpacked = unpack_motion_entry("local:case-20261125-pelican-rig-motion")
        valid = copy.deepcopy(unpacked["binding_template"])
        valid["borrow"] = ["复用分层 SVG 骨架关节和纯帧函数时钟"]
        valid["change"] = ["替换为当前科普主题的插画组件与配色"]
        valid["forbid"] = ["禁止在严肃证据段加入夸张晃动"]

        # 1. Empty borrow / placeholder item
        bad_empty = copy.deepcopy(valid)
        bad_empty["borrow"] = []
        with self.assertRaisesRegex(ContractError, "non-empty list: borrow"):
            validate_motion_reference_binding(bad_empty)

        bad_placeholder = copy.deepcopy(valid)
        bad_placeholder["forbid"] = ["TBD"]
        with self.assertRaisesRegex(ContractError, "placeholder"):
            validate_motion_reference_binding(bad_placeholder)

        # 2. Forged SHA-256
        bad_sha = copy.deepcopy(valid)
        bad_sha["inspected_source_files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "sha256 mismatch"):
            validate_motion_reference_binding(bad_sha)

        # 3. Missing primary code file (only kept doc)
        bad_primary = copy.deepcopy(valid)
        bad_primary["inspected_source_files"] = [
            row for row in valid["inspected_source_files"] if not row["path"].endswith("PelicanRigCase.tsx")
        ]
        if bad_primary["inspected_source_files"]:
            with self.assertRaisesRegex(ContractError, "primary code file"):
                validate_motion_reference_binding(bad_primary)

        # 4. Unknown entry_id
        bad_entry = copy.deepcopy(valid)
        bad_entry["entry_id"] = "local:does-not-exist"
        with self.assertRaisesRegex(ContractError, "unknown entry_id"):
            validate_motion_reference_binding(bad_entry)

        # 5. Stale index_sha256
        bad_idx = copy.deepcopy(valid)
        bad_idx["index_sha256"] = "f" * 64
        with self.assertRaisesRegex(ContractError, "index_sha256 drift"):
            validate_motion_reference_binding(bad_idx)

        # 6. from_scratch_approved requires explicit human approval & reason
        with self.assertRaisesRegex(ContractError, "human_approved=true"):
            validate_motion_reference_binding(
                {
                    "schema": BINDING_SCHEMA,
                    "mode": "from_scratch_approved",
                    "human_approved": False,
                    "approved_by": "creator",
                    "human_reason": "brand new custom shader requested",
                }
            )
        approved = validate_motion_reference_binding(
            {
                "schema": BINDING_SCHEMA,
                "mode": "from_scratch_approved",
                "human_approved": True,
                "approved_by": "creator",
                "human_reason": "全新定制物理模型，索引库内无近似风格参考",
            }
        )
        self.assertEqual(approved["status"], "valid_from_scratch_approved")

    def test_cli_round_trip_and_render_plan_optional_binding_gate(self):
        # CLI motion-find -> motion-unpack -> motion-binding-check
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["motion-find", "--query", "图标变形", "--limit", "3"])
        self.assertEqual(rc, 0)
        found = json.loads(buf.getvalue())
        self.assertTrue(found["candidates"])
        eid = found["candidates"][0]["entry_id"]

        unpack_path = self.root / "unpacked.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(["motion-unpack", "--entry-id", eid, "--output", str(unpack_path)])
        self.assertEqual(rc, 0)
        unpacked = read_json_object(unpack_path, "unpacked")
        binding = copy.deepcopy(unpacked["binding_template"])
        binding["borrow"] = ["复用连续变形插值曲线与时间分段结构"]
        binding["change"] = ["替换为当前视频主题文案与色板"]
        binding["forbid"] = ["禁止照搬上游示例品牌词"]
        binding_path = atomic_write_json(binding, self.root / "binding.json")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["motion-binding-check", "--binding", str(binding_path)]), 0)

        # Tampered binding returns exit code 2
        tampered = copy.deepcopy(binding)
        tampered["inspected_source_files"][0]["sha256"] = "1" * 64
        tampered_path = atomic_write_json(tampered, self.root / "tampered-binding.json")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["motion-binding-check", "--binding", str(tampered_path)]), 2)

        # Render-plan optional integration: omitted stays backward-compatible; provided is validated & hash-bound
        timebase, spec = make_fixture(self.root)
        plan_path = atomic_write_json(
            build_semantic_editorial_plan(timebase, self.root, spec), self.root / "plan.json"
        )
        packaging_path = atomic_write_json(
            build_editorial_packaging_projection(plan_path), self.root / "packaging.json"
        )
        rp_plain = build_semantic_render_plan(plan_path, packaging_path)
        self.assertNotIn("motion_reference_binding", rp_plain["inputs"])
        validate_semantic_render_plan(rp_plain)

        rp_bound = build_semantic_render_plan(
            plan_path, packaging_path, motion_reference_binding_path=binding_path
        )
        self.assertEqual(
            rp_bound["inputs"]["motion_reference_binding"]["sha256"],
            file_sha256(binding_path),
        )
        validate_semantic_render_plan(rp_bound)

        # Invalid binding blocks render-plan creation
        with self.assertRaises(ContractError):
            build_semantic_render_plan(
                plan_path, packaging_path, motion_reference_binding_path=tampered_path
            )


if __name__ == "__main__":
    unittest.main()
