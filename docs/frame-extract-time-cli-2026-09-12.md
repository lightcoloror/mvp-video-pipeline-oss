# Frame extraction time through the stable CLI

Updated: 2026-09-12 22:57:36 (Asia/Shanghai). Tool/model: Codex / GPT-6 Astra.
Dispatch: MVP-FRAME-RECEIPT-QA-CLOSEOUT-20260912-01.

Intent: make the supported CLI able to create valid frame extraction receipts before downstream review consumes them.

Decision: add repeatable `--output-media-time-s` values in the same order as `--output`. Frame jobs require exactly one input and role `frame` for every output. The adapter reuses the existing receipt numeric validator and shared metadata probe, rejects missing/nonfinite/negative times and times at or beyond the source duration before FFmpeg execution, then forwards each `media_time_s` to the existing receipt builder. Other operations retain their previous argument behavior and reject misplaced frame-time options.

Reason: the receipt contract already required a frame time, while the CLI could forward only role/path. An `@2` filename suffix cannot represent this metadata and caused an output artifact lookup failure.

Evidence: the focused managed unittest run reproduced the missing-time execution and unsupported-positive-argument failure before the fix. After the fix, four test methods pass, including eight invalid-time/cardinality cases and the existing non-frame forwarding case. Fourteen real frame extractions and seven audio QA commands completed through the guarded outlet. Exact local receipts and the review packet are in the dispatch's runtime delivery directory; no real source content or paths were added to tests.

Scope: CLI argument adaptation and its focused tests only. Existing receipt serialization/validation and execution scheduling remain authoritative. The focused test's temporary root now honors `TEST_TMP_ROOT` supplied by the managed Windows runtime. No ASR or full candidate rendering was run in this dispatch.

Rollback: reverse only this dispatch's changes to `scripts/ffmpeg_outlet.py` and `tests/test_ffmpeg_outlet_cli.py`, using the saved before copies after checking for later edits. Preserve the pre-existing dirty tree and all historical receipts/media. Removing this adapter restores the known frame CLI limitation; it is not a data cleanup instruction.

Example (generic paths):

```text
python scripts/ffmpeg_outlet.py --operation frame_extract --input media=source.mp4 --output frame=frame.jpg --output-media-time-s 2 --receipt frame.receipt.json -- ffmpeg -ss 2 -i source.mp4 -frames:v 1 frame.jpg
```

The timestamp describes the requested source time; it does not prove phonetic alignment, subjective viewing or a frame's visual quality. A missing/unreadable duration fails closed. Multiple inputs are deliberately rejected for frame jobs because an output-to-source mapping would otherwise be ambiguous.
