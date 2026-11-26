# Restore one spoken-edit decision

Updated: 2026-09-13 10:10:10 (Asia/Shanghai). Tool/model: Codex / GPT-6 Astra.

| Element | This change |
| --- | --- |
| Intent | Make one candidate decision reversible without reverting unrelated edits or modifying source media. |
| Decision | Add `restore_spoken_edit_decision` and stable `spoken_rough_cut_cli restore-decision`; copy one row from a reviewed version bound to the same judge packet, validate both versions and the new result, and write to a new output path. |
| Reason | The existing compile flow could consume versioned decisions, but had no supported single-row restoration command. Whole-file replacement can discard independent later edits. |
| Evidence | Focused managed tests: six passed, one fixed-source test skipped; actual local fixed-source prepare/compile/restore executed separately. A six-second artificial color/tone fixture was reduced to four seconds and restored to six seconds; 180 restored frame payloads and PCM match the original. |
| Scope | Candidate decisions only. No rendering, source overwrite, release approval, new state machine or changed authority contract in the restore command. The demonstration uses the existing guarded FFmpeg outlet separately. |
| Rollback | Reverse only these function/CLI/test/doc changes after checking later edits. Immutable previous/current decision versions and media remain. Do not reset the repository or delete retained evidence. |

```text
python -m video_creation_pipeline.spoken_rough_cut_cli restore-decision --packet judge.json --decisions current.json --restore-from before.json --decision-id edit-001 --output restored.json
python -m video_creation_pipeline.spoken_rough_cut_cli compile --packet judge.json --decisions restored.json --output-dir restored-plan
```

Both input versions must be complete **agent-reviewed candidate** specs for the same packet. This does not imply user approval. The decision ID must exist in both versions, and the output path must not exist. A different packet, unreviewed spec, hash drift, missing decision or existing output is rejected. Undoing addition/removal of rows is outside this command; use a separately reviewed version for that case.

The current real-media assessment is `no_safe_edit`: uncertain recognition and CTC event gaps do not justify deletion. A short listening locator and pending proposal are delivered without modifying the original candidate. Artificial color blocks and a test tone demonstrate mechanics only, not successful semantic editing of a person's speech or subjective listening quality.

For exact synthetic media restoration, use frame and audio sample boundaries. The initial seek-based nonzero-start split lost audio samples despite matching video frames; only the affected artificial pieces were rebuilt. Preserve both failed and accepted evidence.
