# Video creation pipeline: public source edition

The existing Apache-2.0 first-party MVP package, with its original planning,
single FFmpeg outlet, subtitles, audio, visual routing, technical QA and manual
review interfaces. This is the reviewed first-party public package; media outputs remain candidates.
No second renderer, ASR system, database or router is introduced.

## Install and inspect

Use Python 3.11+ and a fresh virtual environment. Install this repository with
`python -m pip install .`. FFmpeg and ffprobe are separately supplied programs.
No models, external source checkouts, media, browser runtime or binaries are
bundled. Optional routes need their explicitly declared external dependencies.

```text
python scripts/owner_manifest_doctor.py --tool-id mvp-video-pipeline --format json
video-content-brief doctor
```

The no-write doctor separates contract/runtime readiness from actual synthetic
execution, real-media readiness and permission. A passing test or schema is not
author approval, asset rights, a real media result or permission to publish.

## Existing canonical synthetic chain

```text
video-canonical-demo --brief tests/fixtures/content-video-brief-v1/upstream-canonical/content-video-brief.v1.json --schema tests/fixtures/content-video-brief-v1/upstream-canonical/content-video-brief.v1.schema.json --research-output tests/fixtures/content-video-brief-v1/upstream-canonical/research-output.v1.json --research-schema tests/fixtures/content-video-brief-v1/upstream-canonical/research-output.v1.schema.json --output-root <new-empty-output-directory>
```

This invokes the existing canonical plan/synthetic media/subtitle/QA chain.
Outputs remain candidate-only; the command is shown for deliberate local use,
not executed by installation or discovery. Existing resource preflights remain
active. Real or personal media, provider calls, TTS/voice cloning, NLE writes,
uploads and publishing require their applicable explicit authorization.

## Scope and licenses

Source-only review: first-party package, CLI scripts, schemas/policies, test code
and reviewed synthetic/template fixtures. Historical task receipts, private
runtime reports, BGM catalog/audio, customer/personal material, model weights,
fonts, third-party source and binaries are excluded. Examples containing
`completed`, `allowed`, authorization policies or measured durations are inert
templates/simulated states, not actual provider receipts, consent, measurements
or asset grants. Replace every placeholder and supply real scoped evidence.

See THIRD_PARTY_NOTICES.md and MODEL_ASSET_LICENSES.md. First-party Apache-2.0
does not relicense optional external AGPL/GPL components, unlicensed source,
model weights or media. The separately reviewed local-rough-cut P1 code is not
included here and has not gained publication approval through this candidate.

766 synthetic source tests passed with 11 optional integration skips. The installed
package passed 34 CLI help checks and generated a 45.034667-second H.264 1280×720
synthetic MP4, one SRT cue and a 42-artifact pinned archive with verified digests.
These are technical synthetic results; author review and publication are separate.
See ACCEPTANCE.md and PUBLIC-PROVENANCE.json for evidence and excluded scope.

Portable metadata refs point to codex-local-governance-oss v0.1.1; exact bundle
commit/digest and the optional native review limitation are in provenance.
This does not distribute those optional native Owners or prove their runtime.
