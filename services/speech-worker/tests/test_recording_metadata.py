"""Recording metadata validation tests."""

from __future__ import annotations

import pytest

from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.recording_metadata import (
    RECORDING_DATE_OPTION,
    normalize_recording_date,
    recording_date_from_options,
)


@pytest.mark.parametrize("value", [None, 20260803, "2026-2-03", "2026-02-31"])
def test_recording_date_rejects_noncanonical_or_invalid_values(value: object) -> None:
    with pytest.raises(InvalidJobRequest):
        normalize_recording_date(value)


def test_recording_date_round_trips_from_job_options() -> None:
    assert normalize_recording_date("2026-08-03") == "2026-08-03"
    assert recording_date_from_options({RECORDING_DATE_OPTION: "2026-08-03"}) == (
        "2026-08-03"
    )
    assert recording_date_from_options({}) is None
