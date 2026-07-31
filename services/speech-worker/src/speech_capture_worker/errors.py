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


class UploadNotFound(WorkerCoreError):
    code = "UPLOAD_NOT_FOUND"


class UploadStateConflict(WorkerCoreError):
    code = "UPLOAD_STATE_CONFLICT"


class UploadPartConflict(WorkerCoreError):
    code = "UPLOAD_PART_CONFLICT"


class UploadPartChecksumMismatch(WorkerCoreError):
    code = "UPLOAD_PART_CHECKSUM_MISMATCH"


class UploadIncomplete(WorkerCoreError):
    code = "UPLOAD_INCOMPLETE"


class UploadChecksumMismatch(WorkerCoreError):
    code = "UPLOAD_CHECKSUM_MISMATCH"


class UploadStorageError(WorkerCoreError):
    code = "UPLOAD_STORAGE_ERROR"


class SourceUndecodable(WorkerCoreError):
    code = "SOURCE_UNDECODABLE"


class MediaProbeUnavailable(WorkerCoreError):
    code = "MEDIA_PROBE_UNAVAILABLE"


class VerifiedUploadRequired(WorkerCoreError):
    code = "SOURCE_UPLOAD_NOT_VERIFIED"


class SchedulerBusy(WorkerCoreError):
    code = "WORKER_PROCESSING_BUSY"


class TranscriptConflict(WorkerCoreError):
    code = "TRANSCRIPT_COMMIT_CONFLICT"


class TranscriptRevisionConflict(WorkerCoreError):
    code = "TRANSCRIPT_REVISION_CONFLICT"


class AudioNormalizationUnavailable(WorkerCoreError):
    code = "AUDIO_NORMALIZATION_UNAVAILABLE"


class AudioNormalizationFailed(WorkerCoreError):
    code = "AUDIO_NORMALIZATION_FAILED"


class NormalizedAudioInvalid(WorkerCoreError):
    code = "NORMALIZED_AUDIO_INVALID"


class SpeechActivityDetectionFailed(WorkerCoreError):
    code = "SPEECH_ACTIVITY_DETECTION_FAILED"


class VadEvaluationFailed(WorkerCoreError):
    code = "VAD_EVALUATION_FAILED"


class AsrAttemptConflict(WorkerCoreError):
    code = "ASR_ATTEMPT_CONFLICT"


class AsrExecutionFailed(WorkerCoreError):
    code = "ASR_EXECUTION_FAILED"


class ForcedAlignmentFailed(WorkerCoreError):
    code = "FORCED_ALIGNMENT_FAILED"
