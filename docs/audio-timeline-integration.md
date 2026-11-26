# Audio timeline integration

Updated: 2026-10-02, Asia/Shanghai. Tool: Codex; exact runtime model not inferred from dispatch text.

## Intent and decision

Bind explicit local audio, rendered diagram assets and subtitles to the existing `video_packaging_plan.v1`. Use the production `build_remotion_timeline_preview` projection and the single guarded FFmpeg outlet. Do not introduce another renderer, narration generator or publication gate.

`scripts/audio_timeline.py validate --packaging <packaging.json> --binding <binding.json>` validates inputs. `render` additionally requires `--cache <segment-cache> --output <new-output-directory>`.

Binding schema is `video_creation_pipeline.audio_timeline_binding.v1`: `packaging` and `audio` are existing artifact references. Each ordered `segments` item has `scene_id`, hash-bound `visual` and `provenance` references, `semantic_description`, `visual_start_s`, and explicit subtitle cues (`start_s`, `end_s`, `text`) in program time. See the isolated synthetic fixture in `outputs/development-closeout-20261002/video/fixture/` at workspace root.

## Why and scope

- WAV sample count is the audio clock: 16-bit mono/stereo PCM, complete contiguous coverage, no implicit tail trimming, silence padding or playback-rate changes. Packaging scene boundaries must align with output frames.
- Required visual files are hash checked, probed for a video stream and checked for sufficient duration. No missing-asset color fallback exists. Semantic relevance still needs review; a caller's description does not prove image meaning.
- Subtitles cannot cross scene boundaries or overlap. This initial adapter accepts one short plain-text line per cue, uses a proportional readable font and a bottom safe area. Source diagram avoidance is verified for the delivered synthetic sample, not arbitrary footage or platform overlays.
- Cache keys bind actual local PCM samples, visual/provenance hashes, captions, output geometry, scene projection and adapter implementation hash. An edit invalidates only dependent segments. Corrupt cache output is rejected. The final manifest separately binds the complete audio and packaging inputs.
- Intermediate segments retain PCM; final concatenation copies video and encodes audio once. Existing outlet receipts, stream duration checks and full video frame decoding are required before the final manifest.
- Execution is serial. The caller supplies task admission and an enforced subprocess runner where task resource budgets require one. There is no multi-process cache scheduler. Use a new cache after changing FFmpeg or upstream rendering dependencies; cache keys do not fingerprint the entire installed toolchain.
- Upstream object-scene authoring/approval is unchanged. This composes already rendered hash-bound diagrams. Regenerating a changed diagram remains the existing Remotion authoring workflow, after which the new visual asset must be rebound.

## Evidence and rollback

Actual isolated-source acceptance: 12 seconds, four meaningful existing object-scene diagrams, synthetic audible tones, 360 decoded video frames. Changing only scene 2's caption rendered scene 2 and reused scenes 1/3/4. Missing visual, absent subtitle and out-of-range subtitle/visual inputs failed. Eight boundary/invalidation unit regressions passed, including local PCM changes and uncovered audio tails.

Evidence: `outputs/development-closeout-20261002/video/verification.json`, `actual-resources.json`, `isolated-source-manifest.json`, `candidate-v2/manifest.json` and exact final dispatch receipt. Synthetic tones are not spoken narration or narration-quality acceptance. Full human playback review remains separate from technical decoding and contact-sheet review.

Changes add only this document, `src/video_creation_pipeline/audio_timeline.py`, `scripts/audio_timeline.py`, and `tests/test_audio_timeline.py`. Roll back by ceasing use of this new entrypoint; preserve output evidence and unrelated pre-existing dirty files. No registry, production routing, service, knowledge-base or remote repository mutation is required.
