# Content-source visual strategy and author word bindings

Updated: 2026-09-19 21:48:40 (Asia/Shanghai) | Codex (GPT-6)

This extension lets canonical content plans enter the existing visual strategy and semantic-motion route without manufacturing a VKP edit job. It imports author timing evidence; it does not provide ASR, TTS, a second renderer, or publication authority.

## Content visual strategy

The existing `video-creative-contracts build-visual-strategy` command accepts either `--unit` (unchanged v1 VKP route), or `--content-plan <project-plan.json> --scene-id <id>` (v2 content route). All other strategy options remain unchanged.

The content route validates the complete project plan and all source references, re-runs the canonical input gates with the original freshness policy, and reproduces the brief projection. Rehashing an invented scene is insufficient. V2 records `source_kind=content_project_plan`, the complete source plan reference, scene/unit hashes and the unchanged `missing_material` status. It is a visual strategy, not evidence of acquired media. The old shot-unit validator remains strict.

For a direct content motion plan, cue identity and narration must match the source scene and the packaging must be its canonical projection. The author-binding route below permits an explicitly reviewed recording-based timeline for that same scene.

## One original-speed creator segment

Use the existing semantic-motion CLI:

```powershell
python scripts/semantic_motion.py build-author-bound --spec binding-spec.json --script-approval script-approval.json --voice-approval voice-approval.json --storyboard-approval storyboard-approval.json --output-dir candidate
python scripts/semantic_motion.py validate --plan candidate/semantic-motion-plan.json
python scripts/semantic_motion.py materialize --plan candidate/semantic-motion-plan.json --project-dir project
```

`binding-spec.json` uses `video_creation_pipeline.author_binding_spec.v1`. The complete shape is exercised by `tests/author_binding_fixture.py`; that file generates **synthetic test data and fictional attestations only**, never real approvals.

Required references are `source_visual_production_plan`, `source_recording`, `source_word_alignment`, `source_packaging_plan`, and `source_asset`. Each uses the existing artifact reference (absolute path, bytes, SHA-256, content kind and canonical JSON hash when applicable). Additional fields are:

- `scene_id`, `source_media_role=creator_narration`, `playback_rate=1`.
- `recording_start_s`, `recording_end_s`: one continuous PCM WAV range. Its duration must equal the selected packaging scene; milliseconds are the duration precision. The full recording hash and complete WAV payload are checked.
- `evidence_mode`: explicitly `measured_recording` or `synthetic_fixture`. Test evidence is never relabelled as a measurement.
- `asset`: the five existing native asset fields; `output`: width, height, fps.
- `cue_bindings`: an existing native motion cue with its scene identity, explicitly selected layout/recipe/protected regions and four phase durations, plus `word_id`, `boundary=start|end`, `offset_ms`. Omit `timing.timeline_start_s`; it is derived, not guessed. No default composition approval is created.

Word evidence uses `video_creation_pipeline.word_alignment_evidence.v1`: exact `source_script` (the canonical brief), `source_recording`, `scene_id`, `quote`, `source_raw_alignment`, engine and evidence mode, with `time_domain=recording_ms`. Each word has a unique ID, text, Python Unicode code-point `text_start`/`text_end`, start/end milliseconds and `timing_origin=measured_word` (or `synthetic_supplied_word` for fixtures). Missing, interpolated, overlapping, non-finite, out-of-range or mismatched words fail closed. A raw alignment reference and operator attestation bind provenance; a label alone does not independently prove acoustic accuracy. No upstream format is silently converted or sentence timing promoted to word timing.

The existing `approval_record.v1` validator checks all three supplied records:

- script approval targets the exact canonical brief;
- voice approval targets this recording and attaches this audio and word alignment under separate `audio` and `word_alignment` roles; the existing required `voice_measurements` attachment remains separate;
- storyboard approval targets the complete binding spec, including cue intent, timing, sources and layout.

These are existing visible-operator attestations, not cryptographic identities. The new runtime only reads them; it contains no automatic approval function. `human-voice-stage-receipt` from Self-media does not replace these audio/timing inputs.

## One clock for motion and sound

`program_time = scene_program_start + (word_recording_time - recording_start) + offset`.
`segment_local_time = program_time - scene_program_start`.

The bundle retains both coordinates and stable cue/word identities in `author-binding-projection.json`, and exports local packaging, local script anchors and a native motion plan. The motion plan references the projection. Every validation/materialization rechecks upstream files and approvals and rebuilds the projection. A fresh hash over altered motion timing cannot bypass this check.

The existing motion-SFX source-manifest and cue-sheet functions consume that exact native motion plan and local packaging. Impact timing retains the existing motion phase policy. Competing external word/subtitle clocks are rejected for this narrow bound route. The generated sound sheet remains `needs_audition`; no audio asset is downloaded, generated, selected as approved, or mixed.

Repeated identical bundle generation is idempotent. Existing different outputs are rejected; use a new output directory for a changed revision. Rewrite, re-record, alignment change, scene relocation or approval change invalidates the bound revision. Scene relocation retains cue/word identity and local timing after a new reviewed binding. Invalidation is conservatively per bundle, not a fine-grained incremental render cache.

## Handoff and acceptance

Keep the existing Self-media recipe and human gates. A case route supplies its native case handoff manifest to canonical input validation; an independent explainer must use an applicable route chosen by its owner. Do not create a fake case or customer to meet a recipe.

Code and offline fixture acceptance do not approve a real recording or candidate video. Before a real run, supply the frozen brief/research and script confirmation, real PCM recording, measured word evidence/raw provenance, three exact approvals, actual visual asset/rights/privacy evidence, and final packaging. Then use the existing render slot, single FFmpeg outlet and final-file QA/privacy/human-review contracts. No final-master, publication, real-media alignment or visual-quality acceptance is implied by materialization.

Hypit and anything2explainer remain source-reviewed references. No upstream runtime or template is copied into the application. Studio evaluation is optional and is not part of this adapter's acceptance.
