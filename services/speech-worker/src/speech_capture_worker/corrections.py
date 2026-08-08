"""Append-only user correction records applied only to derived artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum

from speech_capture_worker.domain import SAFE_IDENTIFIER_PATTERN
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.recording_metadata import normalize_recording_date
from speech_capture_worker.transcript import validate_speaker_id, validate_transcript_text

MAX_CORRECTION_AUTHOR_CHARACTERS = 200
MAX_SPEAKER_DISPLAY_NAME_CHARACTERS = 200


class CorrectionField(StrEnum):
    TRANSCRIPT_TEXT = "transcript_text"
    SEGMENT_REVIEW = "segment_review"
    SPEAKER_DISPLAY_NAME = "speaker_display_name"
    RECORDING_DATE = "recording_date"


@dataclass(frozen=True)
class CorrectionRecord:
    sequence: int
    correction_id: str
    job_id: str
    job_revision: int
    field: CorrectionField
    target_id: str | None
    before: str | None
    after: str
    author: str
    idempotency_key: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_correction(
    *,
    field: CorrectionField,
    target_id: str | None,
    before: str | None,
    after: str,
    author: str,
) -> None:
    if not isinstance(field, CorrectionField):
        raise InvalidJobRequest("correction field is not supported.")
    _validate_author(author)
    if before == after:
        raise InvalidJobRequest("A correction must change the current value.")
    if field is CorrectionField.TRANSCRIPT_TEXT:
        if target_id is None or not SAFE_IDENTIFIER_PATTERN.fullmatch(target_id):
            raise InvalidJobRequest("A transcript-text correction requires a safe segment_id.")
        if not target_id.startswith("seg_"):
            raise InvalidJobRequest("A transcript-text correction target must be a segment_id.")
        if before is None:
            raise InvalidJobRequest("A transcript-text correction requires the current text.")
        validate_transcript_text(before)
        validate_transcript_text(after)
        return
    if field is CorrectionField.SEGMENT_REVIEW:
        if target_id is None or not SAFE_IDENTIFIER_PATTERN.fullmatch(target_id):
            raise InvalidJobRequest("A segment review requires a safe segment_id.")
        if not target_id.startswith("seg_"):
            raise InvalidJobRequest("A segment review target must be a segment_id.")
        if before is None:
            raise InvalidJobRequest("A segment review requires the current value.")
        decode_segment_review(before)
        decode_segment_review(after)
        return
    if field is CorrectionField.SPEAKER_DISPLAY_NAME:
        if target_id is None:
            raise InvalidJobRequest("A speaker-name correction requires a speaker_id.")
        validate_speaker_id(target_id)
        if before is None:
            raise InvalidJobRequest("A speaker-name correction requires the current display name.")
        _validate_display_name(before)
        _validate_display_name(after)
        return
    if target_id is not None:
        raise InvalidJobRequest("A recording-date correction does not accept a target_id.")
    if before is not None:
        _validate_iso_date(before)
    _validate_iso_date(after)


def corrections_sha256(corrections: list[CorrectionRecord]) -> str:
    """Fingerprint the ordered public ledger fields that affect derived output."""

    payload = [
        {
            "sequence": item.sequence,
            "correction_id": item.correction_id,
            "job_revision": item.job_revision,
            "field": item.field.value,
            "target_id": item.target_id,
            "before": item.before,
            "after": item.after,
            "author": item.author,
            "created_at": item.created_at,
        }
        for item in corrections
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_segment_review(*, text: str, speaker_id: str | None) -> str:
    validate_transcript_text(text)
    if speaker_id is not None:
        validate_speaker_id(speaker_id)
    return json.dumps(
        {"speaker_id": speaker_id, "text": text},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_segment_review(value: str) -> tuple[str, str | None]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidJobRequest("segment review value must be valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"speaker_id", "text"}:
        raise InvalidJobRequest("segment review value has unsupported fields.")
    text = payload.get("text")
    speaker_id = payload.get("speaker_id")
    if not isinstance(text, str):
        raise InvalidJobRequest("segment review text must be a string.")
    validate_transcript_text(text)
    if speaker_id is not None:
        if not isinstance(speaker_id, str):
            raise InvalidJobRequest("segment review speaker_id must be a string or null.")
        validate_speaker_id(speaker_id)
    return text, speaker_id


def _validate_author(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_CORRECTION_AUTHOR_CHARACTERS
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or any(not character.isprintable() for character in value)
    ):
        raise InvalidJobRequest("correction author must be one printable line.")


def _validate_display_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_SPEAKER_DISPLAY_NAME_CHARACTERS
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or any(not character.isprintable() for character in value)
    ):
        raise InvalidJobRequest("speaker display name must be one printable line.")


def _validate_iso_date(value: str) -> None:
    normalize_recording_date(value)
