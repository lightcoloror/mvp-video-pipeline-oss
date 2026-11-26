# Explanation recipes and local timing editor

Updated: 2026-09-20 08:56:42 (Asia/Shanghai)
Acting tool/model: Codex (GPT-6 Astra)

This integration reuses content project plans, author word alignment, native
storyboard approval, semantic motion, HyperFrames and motion SFX. No upstream
application, renderer, speech engine or publishing service is installed.

## Recipes

Run from this repository, with the existing Python runtime:

```powershell
python scripts/semantic_editorial.py explainer-recipes --content-plan content-plan.json --spec recipes.json --output recipe-plan.json
python scripts/semantic_editorial.py explainer-assets --plan recipe-plan.json --output-dir recipe-assets
python scripts/semantic_editorial.py explainer-check --plan recipe-plan.json --motion-plans motion-plan.json --output shot-check.json
```

Recipe spec schema is `video_creation_pipeline.explainer_recipe_spec.v1`.
`scenes` must cover every content scene once. Each scene supplies `scene_id`,
`recipe` (`causal`, `comparison`, `sequence`), `explanation_target`,
`visual_protagonist`, `relationship`, `intended_understanding`, and `nodes`.
Nodes have unique `id`, `text`, exact `source_quote` and scene `evidence_refs`.
There are 2–4 nodes (exactly 3 for causal), each 1–24 characters. Text and quote
must occur in the frozen narration; numbers, units and Chinese are not exempt.
Changing or paraphrasing a claim requires updating the upstream reviewed script.
Literal matching does not establish factual truth or causal validity.

Generated SVGs are neutral candidate assets. Use the selected scene's SVG as
the existing author binding spec's `source_asset`, retaining its artifact hash.
Compose through the existing visual strategy and motion cues, then obtain the
normal storyboard approval and run `build-author-bound`. The checker validates
native motion plans, exact recipe image bytes, scene coverage, label provenance
and a disclosed reading-time heuristic (6 characters/second, minimum 1 second).
`review_required` findings must be considered before adoption. It does not
replace render QA, image inspection, factual review or audition. Native
HyperFrames/FFmpeg QA remains the rendering acceptance path.

## Offline timing editor

```powershell
python scripts/semantic_motion.py export-author-editor --projection author-binding-projection.json --output editor.html
```

Open `editor.html` locally. Select the cue and word anchor, offset and duration.
The page supports at/for, until/for and a two-anchor span. Preview displays old
and new time bars; parameter changes invalidate confirmation. Export downloads
`author-edit-proposal.json`. It has no network dependency and uploads nothing.
The HTML contains local source paths and narration excerpts; keep it local.

```powershell
python scripts/semantic_motion.py edit-author-timing --projection author-binding-projection.json --proposal author-edit-proposal.json
python scripts/semantic_motion.py edit-author-timing --projection author-binding-projection.json --proposal author-edit-proposal.json --confirm --output edited-spec.json
```

The first command performs authoritative exact time validation and previews all
affected cues. The second explicitly confirms a new draft; it refuses existing
output files and stale source revisions. Existing approvals are never rewritten.
Obtain a new native storyboard approval for `edited-spec.json`, then regenerate:

```powershell
python scripts/semantic_motion.py build-author-bound --spec edited-spec.json --script-approval script-approval.json --voice-approval voice-approval.json --storyboard-approval edited-storyboard-approval.json --output-dir edited-bundle
```

The old storyboard approval fails against the new spec. Script/voice approval
can remain only while their exact source artifacts remain unchanged. Existing
materialize/run commands consume the new motion plan and retain normal gates.

## Time contract

Legacy word/boundary/offset bindings still work. An expression binding contains
only `cue` and `temporal_expression`; remove authored `hold_s` because the
expression owns duration. Supported expression shapes:

```json
{"kind":"at_for","anchor":{"word_id":"word-001","boundary":"start","offset":{"value":2,"unit":"frames"}},"duration":{"value":5,"unit":"seconds"}}
```

`until_for` uses the same keys. `span` uses `start` and `end` anchors. Quantities
retain seconds or frames; rational values are supported by the CLI. Resolution
uses exact rational arithmetic and rounds half-frame ties later. Empty,
out-of-recording or frame-collapsed windows and competing clocks are rejected.
The browser preview is approximate; Python validates the downloaded proposal
with the same resolver used by native motion generation.

## Evidence and limits

The delivery directory `source-reviews/explainer-integration-20260920` outside
this repository holds independent regression receipts, browser screenshots and
a durable `delivery-demo/case` example. Its silent audio, supplied word times
and named approval records are synthetic fixtures, not real speech or approval.
Production use still needs actual source materials and human review. No
subjective improvement, real audience outcome, publication or video rendering
is implied by the software tests.
