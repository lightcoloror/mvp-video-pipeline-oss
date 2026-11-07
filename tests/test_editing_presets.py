import unittest

from video_creation_pipeline.editing_presets import (
    preferred_take,
    validate_take_review,
    validate_edit_boundaries,
    splice_fades,
    fade_filter,
)
from video_creation_pipeline.timeline_dependencies import (
    fingerprint,
    validate_speed,
    dependency_state,
    validate_dependencies,
)
from video_creation_pipeline.errors import ContractError


class EditingPresetTests(unittest.TestCase):
    def test_last_take_is_ordinary_candidate_but_explicit_incomplete_is_exception(self):
        early = dict(id="first", start=0, end=1, complete=True, quality_score=0.9)
        last = dict(id="last", start=2, end=3, complete=True, quality_score=0.7)
        self.assertEqual(preferred_take([last, early])["id"], "last")
        last["complete"] = False
        self.assertEqual(preferred_take([early, last])["id"], "first")

    def test_take_deletion_requires_unique_information_and_retained_interval(self):
        review = dict(
            complete_checked=True,
            meaning_checked=True,
            natural_delivery_checked=True,
            quality_checked=True,
            unique_information_accounted=True,
            evidence="synthetic comparison",
            retained_interval=dict(start=2, end=3),
            compared_intervals=[dict(start=0, end=1), dict(start=2, end=3)],
        )
        validate_take_review(review, dict(start=0, end=1))
        with self.assertRaisesRegex(ContractError, "also be removed"):
            validate_take_review(review, dict(start=0, end=1), [dict(start=2, end=3)])
        review["unique_information_accounted"] = False
        with self.assertRaises(ContractError):
            validate_take_review(review, dict(start=0, end=1))

    def test_partial_word_and_laughter_tail_cannot_be_trimmed(self):
        words = [dict(start=0.2, end=0.8)]
        events = [dict(event_id="dad-laugh", kind="laughter", start=2.2, end=3)]
        with self.assertRaisesRegex(ContractError, "spoken word"):
            validate_edit_boundaries([dict(start=0.4, end=1)], words, events)
        with self.assertRaisesRegex(ContractError, "protected response"):
            validate_edit_boundaries([dict(start=2.8, end=3)], words, events)
        validate_edit_boundaries([dict(start=1, end=2)], words, events)

    def test_splice_fades_use_only_unoccupied_handles_and_do_not_overlap_takes(self):
        keeps = [dict(start=0, end=1), dict(start=2, end=3)]
        rows = splice_fades(
            keeps, [dict(start=0.1, end=0.9), dict(start=2.1, end=2.9)], []
        )
        self.assertEqual(rows[0]["fade_out_s"], 0.01)
        self.assertEqual(rows[1]["fade_in_s"], 0.01)
        self.assertIn("afade=t=out:st=0.990000000:d=0.010000000", fade_filter(rows[0]))
        rows = splice_fades(keeps, [dict(start=0.9, end=1), dict(start=2, end=2.2)], [])
        self.assertEqual(rows[0]["fade_out_s"], 0)
        self.assertEqual(rows[1]["fade_in_s"], 0)

    def test_speed_requires_review_bound_to_exact_mapping_and_sync_is_invalidated(self):
        mapping = [
            dict(
                source_start=0,
                source_end=2,
                output_start=0,
                output_end=2,
                source_sha256="a",
            )
        ]
        validate_speed(mapping)
        state = dependency_state(mapping)
        state["roles"] = {k: "verified" for k in state["roles"]}
        validate_dependencies(mapping, state, required=["subtitles"])
        mapping[0]["output_end"] = 1
        with self.assertRaises(ContractError):
            validate_speed(mapping)
        review = dict(
            audio_checked=True,
            meaning_preserved=True,
            evidence="synthetic audition declaration",
            mapping_sha256=fingerprint(mapping),
        )
        validate_speed(mapping, review)
        with self.assertRaisesRegex(ContractError, "stale"):
            validate_dependencies(mapping, state)
        self.assertTrue(
            all(
                v == "pending"
                for v in dependency_state(mapping, state)["roles"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
