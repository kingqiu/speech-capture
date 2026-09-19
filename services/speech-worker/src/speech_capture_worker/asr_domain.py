"""Durable immutable ASR-attempt records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class AsrAttemptState(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True)
class AsrAttemptRecord:
    job_id: str
    chunk_index: int
    attempt_number: int
    attempt_key: str
    state: AsrAttemptState
    model_id: str
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int
    language: str | None
    finish_reason: str | None
    truncated: bool
    elapsed_seconds: float
    raw_relative_path: str
    raw_sha256: str
    error_code: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
