"""Stable Worker core errors safe to expose through a future API."""

from __future__ import annotations

from typing import Any


class WorkerCoreError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "WORKER_CORE_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class InvalidJobRequest(WorkerCoreError):
    code = "INVALID_JOB_REQUEST"


class JobNotFound(WorkerCoreError):
    code = "JOB_NOT_FOUND"


class InvalidTransition(WorkerCoreError):
    code = "INVALID_JOB_TRANSITION"


class RevisionConflict(WorkerCoreError):
    code = "JOB_REVISION_CONFLICT"


class IdempotencyConflict(WorkerCoreError):
    code = "IDEMPOTENCY_KEY_CONFLICT"


class ResourceBlocked(WorkerCoreError):
    code = "RESOURCE_PREFLIGHT_BLOCKED"
