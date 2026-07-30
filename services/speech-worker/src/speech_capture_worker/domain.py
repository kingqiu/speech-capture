"""Worker job domain types and state-machine rules."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from speech_capture_worker.errors import InvalidJobRequest, InvalidTransition

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class JobState(StrEnum):
    CREATED = "created"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    TRANSCRIBING = "transcribing"
    ALIGNING = "aligning"
    DIARIZING = "diarizing"
    STRUCTURING = "structuring"
    QUALITY_CHECK = "quality_check"
    PROCESSED = "processed"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelProfile(StrEnum):
    ACCURACY = "accuracy"
    SPEED = "speed"


class ResourceStatus(StrEnum):
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"


ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.CREATED: frozenset({JobState.UPLOADING, JobState.CANCELLED}),
    JobState.UPLOADING: frozenset(
        {JobState.VERIFYING, JobState.PAUSED, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.VERIFYING: frozenset(
        {JobState.QUEUED, JobState.WAITING_USER, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.QUEUED: frozenset(
        {JobState.PREPROCESSING, JobState.PAUSED, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.PREPROCESSING: frozenset(
        {
            JobState.TRANSCRIBING,
            JobState.PAUSED,
            JobState.WAITING_USER,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.TRANSCRIBING: frozenset(
        {
            JobState.ALIGNING,
            JobState.PAUSED,
            JobState.WAITING_USER,
            JobState.PARTIAL,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.ALIGNING: frozenset(
        {
            JobState.DIARIZING,
            JobState.STRUCTURING,
            JobState.PAUSED,
            JobState.WAITING_USER,
            JobState.PARTIAL,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.DIARIZING: frozenset(
        {
            JobState.STRUCTURING,
            JobState.PAUSED,
            JobState.WAITING_USER,
            JobState.PARTIAL,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.STRUCTURING: frozenset(
        {
            JobState.QUALITY_CHECK,
            JobState.PAUSED,
            JobState.WAITING_USER,
            JobState.PARTIAL,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.QUALITY_CHECK: frozenset(
        {
            JobState.PROCESSED,
            JobState.PARTIAL,
            JobState.WAITING_USER,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.PROCESSED: frozenset({JobState.PUBLISHING}),
    JobState.PUBLISHING: frozenset({JobState.PUBLISHED, JobState.PROCESSED, JobState.FAILED}),
    JobState.PUBLISHED: frozenset(),
    JobState.PAUSED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.WAITING_USER: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.PARTIAL: frozenset({JobState.QUEUED, JobState.PUBLISHING, JobState.CANCELLED}),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.CANCELLED: frozenset(),
}

RECOVERY_TARGETS: dict[JobState, JobState] = {
    JobState.VERIFYING: JobState.UPLOADING,
    JobState.PREPROCESSING: JobState.QUEUED,
    JobState.TRANSCRIBING: JobState.QUEUED,
    JobState.ALIGNING: JobState.QUEUED,
    JobState.DIARIZING: JobState.QUEUED,
    JobState.STRUCTURING: JobState.QUEUED,
    JobState.QUALITY_CHECK: JobState.QUEUED,
    JobState.PUBLISHING: JobState.PROCESSED,
}


@dataclass(frozen=True)
class JobCreateRequest:
    vault_id: str
    source_display_name: str
    source_sha256: str
    source_size_bytes: int
    model_profile: ModelProfile = ModelProfile.ACCURACY
    language_hint: str | None = None
    content_type_override: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _validate_identifier("vault_id", self.vault_id)
        if (
            not self.source_display_name
            or self.source_display_name in {".", ".."}
            or len(self.source_display_name) > 255
            or "\x00" in self.source_display_name
            or any(not character.isprintable() for character in self.source_display_name)
            or PurePath(self.source_display_name).name != self.source_display_name
            or "/" in self.source_display_name
            or "\\" in self.source_display_name
        ):
            raise InvalidJobRequest(
                "source_display_name must be a filename without directory components."
            )
        if not SHA256_PATTERN.fullmatch(self.source_sha256):
            raise InvalidJobRequest("source_sha256 must be 64 lowercase hexadecimal characters.")
        if (
            not isinstance(self.source_size_bytes, int)
            or isinstance(self.source_size_bytes, bool)
            or self.source_size_bytes <= 0
        ):
            raise InvalidJobRequest("source_size_bytes must be greater than zero.")
        if not isinstance(self.model_profile, ModelProfile):
            raise InvalidJobRequest("model_profile is not supported.")
        if not isinstance(self.options, dict):
            raise InvalidJobRequest("options must be a JSON object.")
        if self.language_hint is not None and (
            not self.language_hint
            or len(self.language_hint) > 64
            or any(not character.isprintable() for character in self.language_hint)
        ):
            raise InvalidJobRequest(
                "language_hint must be a printable value of 1 to 64 characters."
            )
        if self.content_type_override is not None:
            _validate_identifier("content_type_override", self.content_type_override)


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    vault_id: str
    source_display_name: str
    source_sha256: str
    source_size_bytes: int
    state: JobState
    model_profile: ModelProfile
    language_hint: str | None
    content_type_override: str | None
    options: dict[str, Any]
    revision: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobEvent:
    sequence: int
    job_id: str
    revision: int
    event_type: str
    from_state: JobState | None
    to_state: JobState
    reason_code: str | None
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CheckpointRecord:
    job_id: str
    stage: str
    checkpoint_key: str
    generation: int
    payload: dict[str, Any]
    payload_sha256: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_transition_allowed(current: JobState, target: JobState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(
            f"Job cannot transition from {current.value} to {target.value}.",
            details={"from_state": current.value, "to_state": target.value},
        )


def validate_idempotency_key(value: str) -> None:
    if not value or len(value) > 200 or any(not character.isprintable() for character in value):
        raise InvalidJobRequest("idempotency_key must contain 1 to 200 printable characters.")


def validate_reason_code(value: str | None) -> None:
    if value is not None:
        _validate_identifier("reason_code", value)


def validate_safe_message(value: str | None) -> None:
    if value is not None and (
        not value or len(value) > 500 or "\x00" in value or "\n" in value or "\r" in value
    ):
        raise InvalidJobRequest("error_message must be a single safe line of 1 to 500 characters.")


def _validate_identifier(name: str, value: str) -> None:
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidJobRequest(
            f"{name} must start with a letter or number and contain only "
            "safe identifier characters."
        )
