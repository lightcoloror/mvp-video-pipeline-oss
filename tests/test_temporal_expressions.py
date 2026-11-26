import pytest
from video_creation_pipeline.temporal_expressions import resolve_window
from video_creation_pipeline.errors import ContractError

WORDS = {"a": {"start_ms": 1000, "end_ms": 1500}, "b": {"start_ms": 3000, "end_ms": 3500}}

def resolve(expr, **kw):
    return resolve_window(expr, WORDS, fps=kw.get("fps", 30), recording_start_s=0, recording_end_s=5)

def test_units_and_tie_later():
    result = resolve({"kind": "at_for", "anchor": {"word_id": "a", "boundary": "start", "offset": {"value": "1/2", "unit": "frames"}}, "duration": {"value": 8, "unit": "frames"}})
    assert (result["start_frame"], result["end_frame"]) == (31, 39)
    assert result["start_exact"] == "61/60"

def test_span_and_until():
    a = {"word_id": "a", "boundary": "start"}
    b = {"word_id": "b", "boundary": "end"}
    assert resolve({"kind": "span", "start": a, "end": b})["end_s"] == 3.5
    assert resolve({"kind": "until_for", "anchor": b, "duration": {"value": 1, "unit": "seconds"}})["start_s"] == 2.5

@pytest.mark.parametrize("duration", [0, -1, 10, True, "NaN"])
def test_invalid_windows(duration):
    with pytest.raises(ContractError):
        resolve({"kind": "at_for", "anchor": {"word_id": "a", "boundary": "start"}, "duration": {"value": duration, "unit": "seconds"}})
