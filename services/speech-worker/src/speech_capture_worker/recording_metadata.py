"""Validated per-recording metadata that belongs to the derived output."""

from __future__ import annotations

from datetime import date
from typing import Any

from speech_capture_worker.errors import InvalidJobRequest

RECORDING_DATE_OPTION = "recording_date"


def normalize_recording_date(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidJobRequest("recording date must use YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidJobRequest("recording date must be a valid calendar date.") from exc
    if parsed.isoformat() != value:
        raise InvalidJobRequest("recording date must use YYYY-MM-DD.")
    return value


def recording_date_from_options(options: dict[str, Any]) -> str | None:
    value = options.get(RECORDING_DATE_OPTION)
    return None if value is None else normalize_recording_date(value)
