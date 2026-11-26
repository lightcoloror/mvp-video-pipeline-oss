# Seven cover composition routes

Implementation date: 2026-09-26. Tool: Codex. Runtime model identity is not inferred.

## Scope

Extend existing case-cover plan -> projection -> Remotion CoverStill. No second
renderer, media upload, model generation, git publication or overwrite of old renders.
Existing plans without composition_recipe_id retain their layout.

## Contract

visual_route.composition_recipe_id selects minimal-whitespace,
person-side-whitespace, partial-person-object-led, product-ui-led,
comparison-cards, editorial-collage or evidence-focus. The route validates the
existing recipe account and composition scopes. Account scopes are not silently
expanded to family branding.

cover_recipes.recipe_layout supplies ratio-aware starting boxes; all five existing
profiles use canonical typography minima. Caller must still validate and hash-bind
the complete plan and safe-media assets before materialization.

Each placement declares recipe_role. Each used asset explicitly declares
generated=false, privacy_reviewed=true and use_approved=true. These are caller
attestations, not automated rights or privacy verification. Object/UI/evidence
assets additionally require original_preserved=true. Evidence requires
verified_focus. Comparison requires basis, labels, and equal display area.

Text cannot intersect media boxes. Images use contain, never implicit cropping.
Generated imagery remains a separately authorized workflow, not a recipe toggle.

## Verification and remaining acceptance

Unit matrix: seven recipes x five profiles, projection and rejection paths.
This does not prove browser rendering, visual quality, mobile legibility or user
preference. Live Remotion still rendering and visual inspection remain necessary
before claiming all-route visual acceptance. Family media trials require an
explicit family profile rather than mislabeling as either existing Lucas account.

### Verified 2026-09-26

- Six recipe tests passed, including 35 recipe/profile projection combinations.
- Six typography regression tests and 14 Remotion project regression tests passed.
- Seven actual 1080x1440 PNGs rendered sequentially with installed Remotion
  4.0.477 and local headless Chrome; all were visually inspected as synthetic
  composition smoke tests. No family photograph or external service was used.
- Evidence: `outputs/title-cover-real-remotion-v2/recipe-smoke-20260926-b/smoke-results.json`.
- Initial sandbox spawn EPERM is retained in the `recipe-smoke-20260926-a`
  diagnostic directory; the authorized local process retry succeeded.
- No claim of all-ratio browser QA, production pipeline acceptance, measured
  typography receipts, real-media quality, or cover PK winner. Those remain
  distinct from this executable-route implementation and synthetic smoke.

Run the local smoke with `scripts/smoke_cover_recipes.py --runtime <installed
Remotion project> --browser <local headless Chrome> --name <new-child-name>`.
No install or download is performed. The helper is QA only and cannot replace
the production `video-remotion-skill run-covers` entrypoint.
