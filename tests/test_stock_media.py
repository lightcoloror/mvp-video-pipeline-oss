from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import read_json_object
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.music_pipeline import validate_music_asset_profile
from video_creation_pipeline.stock_media import (
    build_licensed_media_asset,
    build_licensed_media_catalog,
    build_music_profile_from_licensed_asset,
    build_sound_effect_cue_sheet,
    build_sound_effect_edit_plan,
    build_sound_effect_profile,
    build_stock_source_registry,
    execute_sound_effect_mix,
    validate_licensed_media_asset,
    validate_licensed_media_catalog,
    validate_sound_effect_edit_plan,
    write_stock_media_contract,
)
from video_creation_pipeline.stock_media_cli import main as stock_media_cli_main


class StockMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.snapshot = self.root / "license.txt"
        self.snapshot.write_text("reviewed fixture license", encoding="utf-8")

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_registry_lists_reviewed_sources_without_network_authority(self) -> None:
        registry = build_stock_source_registry()
        self.assertEqual(
            [row["source_id"] for row in registry["sources"]],
            [
                "freesound",
                "lobe_icons",
                "mixkit",
                "pexels",
                "pixabay",
                "thiings_free",
                "thiings_paid",
                "unsplash",
                "youtube_audio_library",
            ],
        )
        self.assertFalse(registry["authority_boundary"]["automatic_download"])
        self.assertFalse(registry["authority_boundary"]["bulk_scraping"])

    def test_icon_sources_preserve_commercial_and_trademark_boundaries(self) -> None:
        icon = self._media("brand.png", b"brand-icon")
        lobe = build_licensed_media_asset(
            icon,
            self.snapshot,
            self._spec(
                "brand-icon",
                source_id="lobe_icons",
                media_type="image",
                license_id="mit",
                item_url="https://icons.lobehub.com/icons/openai",
                attribution_text="Lobe Icons, MIT License",
                third_party_rights={
                    "recognizable_people": False,
                    "trademarks": True,
                    "status": "cleared",
                    "evidence": "documented nominative brand reference",
                },
            ),
            purpose="commercial",
            platform="youtube",
        )
        self.assertTrue(lobe["readiness"]["commercial_use_eligible"])
        self.assertTrue(lobe["third_party_rights"]["trademarks"])

        thiings_spec = self._spec(
            "personal-object",
            source_id="thiings_free",
            media_type="image",
            license_id="thiings-free-license",
            item_url="https://www.thiings.co/things/camera",
            attribution_text="Thing from Thiings.co",
            third_party_rights={
                "recognizable_people": False,
                "trademarks": False,
                "status": "not_present",
                "evidence": "",
            },
        )
        with self.assertRaisesRegex(ContractError, "does not allow commercial use"):
            build_licensed_media_asset(
                icon,
                self.snapshot,
                thiings_spec,
                purpose="commercial",
                platform="youtube",
            )
        personal = build_licensed_media_asset(
            icon,
            self.snapshot,
            thiings_spec,
            purpose="personal",
            platform="youtube",
        )
        self.assertEqual(personal["license"]["purpose"], "personal")
        self.assertFalse(personal["license"]["commercial_use_allowed"])

    def test_pixabay_music_feeds_existing_profile_and_catalog(self) -> None:
        audio = self._media("calm.wav", b"music-fixture")
        manifest_path = self._write_asset(
            "calm",
            audio,
            source_id="pixabay",
            media_type="music",
            license_id="pixabay-content-license",
            item_url="https://pixabay.com/music/calm-1/",
            tags=["calm", "instrumental"],
        )
        catalog = build_licensed_media_catalog(
            [manifest_path], purpose="commercial", platform="youtube"
        )
        validate_licensed_media_catalog(catalog)
        profile = build_music_profile_from_licensed_asset(
            manifest_path,
            {
                "duration_s": 12,
                "windows": [
                    {
                        "window_id": "calm-main",
                        "start_s": 0,
                        "end_s": 12,
                        "structure_label": "main",
                        "structure_confidence": 0.9,
                        "descriptors": self._descriptors(),
                        "vocal_probability": 0,
                        "tags": ["calm", "instrumental"],
                        "loopable": True,
                    }
                ],
                "analyzer": self._analyzer(),
            },
        )
        validate_music_asset_profile(profile)
        self.assertEqual(profile["asset_id"], "calm")
        self.assertEqual(
            profile["licensed_asset_manifest_sha256"],
            read_json_object(manifest_path, "asset")["asset_manifest_sha256"],
        )

    def test_rejects_nc_restricted_and_missing_attribution(self) -> None:
        audio = self._media("effect.wav", b"effect")
        with self.assertRaisesRegex(ContractError, "not cleared"):
            build_licensed_media_asset(
                audio,
                self.snapshot,
                self._spec(
                    "effect",
                    source_id="freesound",
                    media_type="sound_effect",
                    license_id="cc-by-nc-4.0",
                    item_url="https://freesound.org/s/123/",
                ),
                purpose="commercial",
                platform="youtube",
            )
        with self.assertRaisesRegex(ContractError, "requires attribution"):
            build_licensed_media_asset(
                audio,
                self.snapshot,
                self._spec(
                    "effect",
                    source_id="freesound",
                    media_type="sound_effect",
                    license_id="cc-by-4.0",
                    item_url="https://freesound.org/s/123/",
                ),
                purpose="commercial",
                platform="youtube",
            )
        with self.assertRaisesRegex(ContractError, "not cleared"):
            build_licensed_media_asset(
                audio,
                self.snapshot,
                self._spec(
                    "effect",
                    source_id="mixkit",
                    media_type="sound_effect",
                    license_id="mixkit-restricted-license",
                    item_url="https://mixkit.co/free-sound-effects/whoosh/",
                ),
                purpose="commercial",
                platform="youtube",
            )

    def test_youtube_audio_library_is_fail_closed_off_platform(self) -> None:
        audio = self._media("youtube.mp3", b"youtube-audio")
        with self.assertRaisesRegex(ContractError, "distribution platform"):
            build_licensed_media_asset(
                audio,
                self.snapshot,
                self._spec(
                    "youtube-audio",
                    source_id="youtube_audio_library",
                    media_type="music",
                    license_id="youtube-audio-library-standard",
                    item_url="https://www.youtube.com/audiolibrary/music",
                ),
                purpose="commercial",
                platform="social_media",
            )

    def test_visual_rights_mime_and_cue_bounds_are_hard_gates(self) -> None:
        image = self._media("photo.jpg", b"image")
        visual_spec = self._spec(
            "photo",
            source_id="pexels",
            media_type="image",
            license_id="pexels-license",
            item_url="https://www.pexels.com/photo/example-1/",
        )
        with self.assertRaisesRegex(ContractError, "rights declaration"):
            build_licensed_media_asset(
                image,
                self.snapshot,
                visual_spec,
                purpose="commercial",
                platform="youtube",
            )
        visual_spec["third_party_rights"] = {
            "recognizable_people": True,
            "trademarks": False,
            "status": "cleared",
        }
        with self.assertRaisesRegex(ContractError, "require evidence"):
            build_licensed_media_asset(
                image,
                self.snapshot,
                visual_spec,
                purpose="commercial",
                platform="youtube",
            )
        source = self._json("short-timeline.json", {"duration_s": 1})
        with self.assertRaisesRegex(ContractError, "exceeds the timeline"):
            build_sound_effect_cue_sheet(
                source,
                [
                    {
                        "cue_id": "late",
                        "timeline_start_s": 0.9,
                        "duration_s": 0.2,
                        "query": "late effect",
                    }
                ],
                timeline_duration_s=1.0,
                purpose="commercial",
                platform="youtube",
            )

    def test_license_snapshot_or_media_drift_invalidates_asset(self) -> None:
        audio = self._media("drift.wav", b"original")
        manifest = build_licensed_media_asset(
            audio,
            self.snapshot,
            self._spec(
                "drift",
                source_id="mixkit",
                media_type="sound_effect",
                license_id="mixkit-free-license",
                item_url="https://mixkit.co/free-sound-effects/whoosh/",
            ),
            purpose="commercial",
            platform="youtube",
        )
        self.snapshot.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_licensed_media_asset(manifest)

    def test_sound_effect_plan_auto_selects_or_reports_missing_material(self) -> None:
        profile_path = self._sound_effect_profile("whoosh", ["transition", "whoosh"])
        source = self._json("timeline.json", {"events": ["transition"]})
        cues_path = self.root / "cues.json"
        write_stock_media_contract(
            build_sound_effect_cue_sheet(
                source,
                [
                    {
                        "cue_id": "transition-1",
                        "timeline_start_s": 0.5,
                        "duration_s": 0.4,
                        "query": "transition whoosh",
                        "required_tags": ["transition", "whoosh"],
                        "evidence_ids": ["event-1"],
                    }
                ],
                timeline_duration_s=2.0,
                purpose="commercial",
                platform="youtube",
            ),
            cues_path,
        )
        plan = build_sound_effect_edit_plan(cues_path, [profile_path])
        validate_sound_effect_edit_plan(plan)
        self.assertTrue(plan["readiness"]["render_eligible"])
        self.assertEqual(plan["selections"][0]["status"], "auto_selected")
        self.assertEqual(
            plan["ffmpeg_handoff"]["outlet_id"],
            "video_creation_pipeline.single_ffmpeg_outlet",
        )

        missing_path = self.root / "missing-cues.json"
        write_stock_media_contract(
            build_sound_effect_cue_sheet(
                source,
                [
                    {
                        "cue_id": "bell-1",
                        "timeline_start_s": 0.2,
                        "duration_s": 0.2,
                        "query": "notification bell",
                        "required_tags": ["bell"],
                    }
                ],
                timeline_duration_s=2.0,
                purpose="commercial",
                platform="youtube",
            ),
            missing_path,
        )
        missing = build_sound_effect_edit_plan(missing_path, [profile_path])
        self.assertFalse(missing["readiness"]["render_eligible"])
        self.assertEqual(missing["selections"][0]["status"], "missing_material")

    def test_semantic_sound_effect_spotting_keeps_candidate_mix_automatic(self) -> None:
        profile_path = self._sound_effect_profile("whoosh", ["transition", "whoosh"])
        source = self._json("semantic-timeline.json", {"events": ["section change"]})
        cues_path = self.root / "semantic-cues.json"
        cue_sheet = build_sound_effect_cue_sheet(
            source,
            [
                {
                    "cue_id": "section-1",
                    "timeline_start_s": 4.0,
                    "duration_s": 0.4,
                    "query": "transition whoosh",
                    "required_tags": ["transition", "whoosh"],
                    "evidence_ids": ["event-1"],
                    "event": "flow.connect",
                    "function": "transition",
                    "priority": "primary",
                    "visual_anchor": {
                        "action": "chapter card settles into the next section",
                        "trigger": "settle",
                    },
                    "narration_anchor": {
                        "text": "next, compare the two choices",
                        "position": "after",
                    },
                    "rationale": "the sound marks a real section boundary",
                }
            ],
            timeline_duration_s=30.0,
            purpose="commercial",
            platform="youtube",
            semantic_spotting={
                "timing_authority": "final_audio_or_srt",
                "sound_style": "general_knowledge",
                "density_plan": {"profile": "sparse"},
                "omissions": [
                    {
                        "omission_id": "cta-silence",
                        "start_s": 20.0,
                        "end_s": 25.0,
                        "reason": "keep the spoken call to action clear",
                        "evidence_ids": ["event-2"],
                    }
                ],
            },
        )
        write_stock_media_contract(cue_sheet, cues_path)
        self.assertEqual(
            cue_sheet["semantic_spotting"]["density_plan"]["planned_cue_count"], 1
        )
        self.assertEqual(
            cue_sheet["semantic_spotting"]["omissions"][0]["omission_id"],
            "cta-silence",
        )

        plan = build_sound_effect_edit_plan(cues_path, [profile_path])
        validate_sound_effect_edit_plan(plan)
        self.assertTrue(plan["readiness"]["render_eligible"])
        self.assertFalse(plan["readiness"]["release_eligible"])
        self.assertEqual(plan["readiness"]["audition_pending_count"], 1)
        self.assertEqual(plan["selections"][0]["status"], "auto_selected")
        self.assertEqual(plan["selections"][0]["review_status"], "needs_audition")
        self.assertTrue(
            plan["selection_policy"]["candidate_mix_before_audition_allowed"]
        )
        self.assertFalse(plan["authority_boundary"]["release_without_audition_allowed"])

    def test_semantic_sound_effect_spotting_rejects_fake_approval(self) -> None:
        source = self._json("semantic-source.json", {"events": ["warning"]})
        cue = {
            "cue_id": "warning-1",
            "timeline_start_s": 1.0,
            "duration_s": 0.3,
            "query": "warning",
            "event": "risk.flag",
            "function": "caution",
            "priority": "primary",
            "visual_anchor": {"action": "warning badge appears", "trigger": "impact"},
            "narration_anchor": {"text": "pay attention", "position": "during"},
            "rationale": "the risk condition needs emphasis",
            "spotting_status": "approved",
        }
        with self.assertRaisesRegex(ContractError, "separate approval record"):
            build_sound_effect_cue_sheet(
                source,
                [cue],
                timeline_duration_s=10.0,
                semantic_spotting={
                    "timing_authority": "scene_timeline",
                    "density_plan": {"profile": "sparse"},
                },
            )

    def test_cli_ingests_a_commercial_asset_without_downloading(self) -> None:
        audio = self._media("cli.wav", b"cli")
        spec = self._json(
            "ingest.json",
            self._spec(
                "cli-effect",
                source_id="mixkit",
                media_type="sound_effect",
                license_id="mixkit-free-license",
                item_url="https://mixkit.co/free-sound-effects/click/",
                tags=["click"],
            ),
        )
        output = self.root / "asset.json"
        code = stock_media_cli_main(
            [
                "ingest",
                "--media",
                str(audio),
                "--license-snapshot",
                str(self.snapshot),
                "--spec",
                str(spec),
                "--purpose",
                "commercial",
                "--platform",
                "youtube",
                "--output",
                str(output),
            ]
        )
        self.assertEqual(code, 0)
        payload = read_json_object(output, "licensed asset")
        self.assertTrue(payload["readiness"]["commercial_use_eligible"])
        self.assertFalse(payload["source"]["network_execution_performed_by_pipeline"])
        self.assertEqual(
            stock_media_cli_main(["validate", "--artifact", str(output)]), 0
        )

    def test_doctor_reports_optional_semantic_runtime_without_fallback(self) -> None:
        self.assertEqual(stock_media_cli_main(["doctor"]), 0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_sound_effect_is_mixed_through_the_single_ffmpeg_outlet(self) -> None:
        program = self.root / "program.mp4"
        effect = self.root / "whoosh.wav"
        self._run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:d=1.2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1.2",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(program),
            ]
        )
        self._run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=0.3",
                "-c:a",
                "pcm_s16le",
                str(effect),
            ]
        )
        profile_path = self._sound_effect_profile(
            "whoosh", ["transition", "whoosh"], media_path=effect, duration_s=0.3
        )
        source = self._json("script.json", {"event": "transition"})
        cues_path = self.root / "cues.json"
        write_stock_media_contract(
            build_sound_effect_cue_sheet(
                source,
                [
                    {
                        "cue_id": "transition",
                        "timeline_start_s": 0.4,
                        "duration_s": 0.25,
                        "query": "transition whoosh",
                        "required_tags": ["transition", "whoosh"],
                        "gain_db": -10,
                    }
                ],
                timeline_duration_s=1.2,
                purpose="commercial",
                platform="youtube",
            ),
            cues_path,
        )
        plan_path = self.root / "sfx-plan.json"
        write_stock_media_contract(
            build_sound_effect_edit_plan(cues_path, [profile_path]), plan_path
        )
        output = self.root / "candidate-with-sfx.mp4"
        receipt_path = self.root / "sfx-receipt.json"
        receipt = execute_sound_effect_mix(
            program,
            plan_path,
            output,
            receipt_path,
            deterministic=True,
        )
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(receipt["operation"], "sound_effect_mix")
        self.assertEqual(
            receipt["outlet_id"], "video_creation_pipeline.single_ffmpeg_outlet"
        )

    def _sound_effect_profile(
        self,
        asset_id: str,
        tags: list[str],
        *,
        media_path: Path | None = None,
        duration_s: float = 1.0,
    ) -> Path:
        media = media_path or self._media(f"{asset_id}.wav", b"effect-fixture")
        manifest_path = self._write_asset(
            asset_id,
            media,
            source_id="mixkit",
            media_type="sound_effect",
            license_id="mixkit-free-license",
            item_url=f"https://mixkit.co/free-sound-effects/{asset_id}/",
            tags=tags,
        )
        profile = build_sound_effect_profile(
            manifest_path,
            duration_s=duration_s,
            category="transition",
            tags=tags,
            description="transition whoosh sweep",
        )
        profile_path = self.root / f"{asset_id}-profile.json"
        write_stock_media_contract(profile, profile_path)
        return profile_path

    def _write_asset(
        self,
        asset_id: str,
        media: Path,
        **kwargs: object,
    ) -> Path:
        payload = build_licensed_media_asset(
            media,
            self.snapshot,
            self._spec(asset_id, **kwargs),
            purpose="commercial",
            platform="youtube",
        )
        path = self.root / f"{asset_id}-asset.json"
        write_stock_media_contract(payload, path)
        return path

    def _spec(self, asset_id: str, **overrides: object) -> dict:
        payload = {
            "asset_id": asset_id,
            "media_type": "sound_effect",
            "source_id": "mixkit",
            "item_url": f"https://mixkit.co/free-sound-effects/{asset_id}/",
            "license_id": "mixkit-free-license",
            "title": asset_id,
            "author": "fixture-author",
            "description": "fixture asset",
            "tags": [],
            "download_method": "manual",
            "downloaded_at": "2026-08-15T20:00:00+08:00",
            "attribution_text": "",
        }
        payload.update(overrides)
        return payload

    def _analyzer(self) -> dict:
        return {
            "id": "reviewed-manual-analysis",
            "model": "operator-tags",
            "version": "1",
            "source_commit": "not-applicable",
            "execution_location": "local",
            "model_license": "not-applicable",
            "automatic_fallback_used": False,
        }

    def _descriptors(self) -> dict[str, float]:
        return {
            "valence": 0.6,
            "arousal": 0.3,
            "tension": 0.2,
            "intimacy_scale": 0.5,
            "warmth": 0.6,
            "movement": 0.2,
            "engagement": 0.3,
        }

    def _media(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _run_ffmpeg(self, args: list[str]) -> None:
        executable = shutil.which("ffmpeg")
        if executable and Path(executable).suffix.casefold() in {".cmd", ".bat"}:
            executable = str(
                (
                    Path(executable).resolve().parent
                    / ".."
                    / "app"
                    / "ffmpeg"
                    / "ffmpeg.exe"
                ).resolve()
            )
        result = subprocess.run(
            [str(executable), "-y", "-loglevel", "error", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            self.fail(result.stderr)


if __name__ == "__main__":
    unittest.main()
