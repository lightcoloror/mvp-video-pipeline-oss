# Visual routing synthetic example

This fixture is fully synthetic and remains `candidate_only`.

```powershell
video-visual-routing build-style `
  --preset paper_cutout `
  --output visual-style.json

video-visual-routing build-routes `
  --packaging-plan packaging-plan.json `
  --routing-spec routing-spec.json `
  --style-profile visual-style.json `
  --output visual-routes.json
```

Without a hash-bound HyperFrames music handoff, `scene-05` intentionally remains
`blocked_missing_music_audiomap`. Add `--music-handoff` only after running the fixed
upstream analyzer through `video-visual-routing analyze-music` and building its handoff.
