"""Progressive transcript records exposed through bounded Worker snapshots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from speech_capture_worker.domain import SAFE_IDENTIFIER_PATTERN, JobRecord, JobState
from speech_capture_worker.errors import InvalidJobRequest

MAX_TRANSCRIPT_TEXT_CHARACTERS = 50_000
MAX_LANGUAGE_TAG_CHARACTERS = 64


class TranscriptOutcome(StrEnum):
    TRANSCRIBED = "transcribed"
    INAUDIBLE = "inaudible"
    NON_SPEECH = "non_speech"
    FAILED = "failed"


class TranscriptTimingStatus(StrEnum):
    ESTIMATED = "estimated"
    ALIGNED = "aligned"


class SpeakerLabelStatus(StrEnum):
    PENDING = "pending"
    ANONYMOUS = "anonymous"
    CONFIRMED = "confirmed"
    UNAVAILABLE = "unavailable"


class DiarizationStatus(StrEnum):
    NOT_STARTED = "not_started"
    PROCESSING = "processing"
    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TranscriptSegment:
    job_id: str
    segment_sequence: int
    segment_id: str
    commit_key: str
    revision: int
    start_ms: int
    end_ms: int
    outcome: TranscriptOutcome
    text: str | None
    language: str | None
    confidence: float | None
    timing_status: TranscriptTimingStatus
    speaker_id: str | None
    speaker_label_status: SpeakerLabelStatus
    error_code: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProvisionalTranscript:
    job_id: str
    generation: int
    start_ms: int
    end_ms: int
    text: str
    language: str | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobProgress:
    job_id: str
    generation: int
    stage: JobState
    processed_ms: int
    duration_ms: int
    stage_progress: float
    elapsed_seconds: float
    estimated_remaining_seconds: float | None
    diarization_status: DiarizationStatus
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobUpdate:
    sequence: int
    job_id: str
    job_revision: int
    event_type: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobSnapshot:
    job: JobRecord
    progress: JobProgress | None
    stable_segments: list[TranscriptSegment]
    provisional: ProvisionalTranscript | None
    resource_report: dict[str, Any] | None
    latest_event_sequence: int
    next_after_segment_sequence: int
    has_more_segments: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "progress": self.progress.to_dict() if self.progress is not None else None,
            "stable_segments": [segment.to_dict() for segment in self.stable_segments],
            "provisional": (
                self.provisional.to_dict() if self.provisional is not None else None
            ),
            "resource_report": self.resource_report,
            "latest_event_sequence": self.latest_event_sequence,
            "next_after_segment_sequence": self.next_after_segment_sequence,
            "has_more_segments": self.has_more_segments,
        }


def validate_commit_key(value: str) -> None:
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidJobRequest("commit_key contains unsupported characters.")


def validate_time_range(start_ms: int, end_ms: int, *, duration_ms: int) -> None:
    if (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or not isinstance(end_ms, int)
        or isinstance(end_ms, bool)
        or start_ms < 0
        or end_ms <= start_ms
        or end_ms > duration_ms
    ):
        raise InvalidJobRequest(
            "Transcript time range must be positive, ordered, and within the source duration."
        )


def validate_transcript_text(value: str, *, allow_empty: bool = False) -> None:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or len(value) > MAX_TRANSCRIPT_TEXT_CHARACTERS
        or "\x00" in value
        or any(
            not character.isprintable() and character not in {"\n", "\r", "\t"}
            for character in value
        )
    ):
        raise InvalidJobRequest(
            "Transcript text must be valid readable text within the supported size limit."
        )


def validate_language(value: str | None) -> None:
    if value is not None and (
        not value
        or len(value) > MAX_LANGUAGE_TAG_CHARACTERS
        or any(not character.isprintable() for character in value)
    ):
        raise InvalidJobRequest("language must contain 1 to 64 printable characters.")


def validate_speaker_id(value: str | None) -> None:
    if value is not None and not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidJobRequest("speaker_id contains unsupported characters.")


def validate_confidence(value: float | None) -> None:
    if value is not None and (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise InvalidJobRequest("confidence must be between zero and one.")


def validate_progress_number(
    name: str,
    value: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        maximum_message = f" and {maximum}" if maximum is not None else " or greater"
        raise InvalidJobRequest(f"{name} must be between {minimum}{maximum_message}.")
