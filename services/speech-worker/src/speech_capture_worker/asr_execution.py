"""Restart-safe ASR chunk execution over the deterministic normalized-audio plan."""

from __future__ import annotations

import time
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

from speech_capture_worker.asr_domain import AsrAttemptRecord, AsrAttemptState
from speech_capture_worker.asr_probe import ACCURACY_MODEL_ID, SPEED_MODEL_ID
from speech_capture_worker.audio_preprocessing import (
    AudioChunkPlan,
    AudioPreprocessor,
    NormalizedAudioPlan,
)
from speech_capture_worker.completeness import (
    CoverageIssue,
    evaluate_chunk_coverage,
    validate_timestamp_segments,
)
from speech_capture_worker.domain import JobRecord, JobState, ModelProfile, ResourceStatus
from speech_capture_worker.errors import AsrExecutionFailed, InvalidJobRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import ResourceReport, check_resource_preflight
from speech_capture_worker.transcript import (
    DiarizationStatus,
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
)

DEFAULT_MAX_ATTEMPTS = 3
BOUNDARY_HEADROOM_BYTES = 256 * 1024 * 1024
MAX_SENTENCE_PAUSE_BRIDGE_MS = 1000


class AsrEngine(Protocol):
    model_id: str

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
        language_hint: str | None,
        context: str,
    ) -> dict[str, Any]: ...


class AsrRunOutcome(StrEnum):
    CHUNK_COMPLETED = "chunk_completed"
    RETRYABLE_FAILURE = "retryable_failure"
    SAFE_PAUSED = "safe_paused"
    TRANSCRIPTION_COMPLETED = "transcription_completed"
    PARTIAL = "partial"
    BATCH_LIMIT_REACHED = "batch_limit_reached"


@dataclass(frozen=True)
class AsrRunResult:
    outcome: AsrRunOutcome
    job: JobRecord
    chunk_index: int | None
    attempt: AsrAttemptRecord | None
    issues: tuple[CoverageIssue, ...]
    resource_report: ResourceReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict(),
            "chunk_index": self.chunk_index,
            "attempt": self.attempt.to_dict() if self.attempt is not None else None,
            "issues": [asdict(issue) for issue in self.issues],
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
        }


@dataclass(frozen=True)
class AsrBatchResult:
    outcome: AsrRunOutcome
    job: JobRecord
    completed_chunks: int
    total_chunks: int
    attempts_used: int
    last_chunk_index: int | None
    resource_report: ResourceReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict(),
            "completed_chunks": self.completed_chunks,
            "total_chunks": self.total_chunks,
            "attempts_used": self.attempts_used,
            "last_chunk_index": self.last_chunk_index,
            "resource_report": (
                self.resource_report.to_dict()
                if self.resource_report is not None
                else None
            ),
        }


class MlxQwenAsrEngine:
    """Lazy real-model adapter; construction does not load model weights."""

    def __init__(
        self,
        *,
        model_profile: ModelProfile,
        model_target: str | None = None,
    ) -> None:
        if not isinstance(model_profile, ModelProfile):
            raise InvalidJobRequest("model_profile is not supported.")
        self.model_id = (
            ACCURACY_MODEL_ID
            if model_profile is ModelProfile.ACCURACY
            else SPEED_MODEL_ID
        )
        self._model_target = model_target or self.model_id
        self._session: Any | None = None

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
        language_hint: str | None,
        context: str,
    ) -> dict[str, Any]:
        if sample_rate != 16_000:
            raise AsrExecutionFailed("The ASR engine requires 16 kHz normalized audio.")
        if self._session is None:
            from mlx_qwen3_asr import Session

            self._session = Session(model=self._model_target)
        result = self._session.transcribe(
            (audio, sample_rate),
            context=context,
            language=language_hint,
            return_timestamps=True,
            return_chunks=True,
        )
        return asdict(result)


BoundaryPreflight = Callable[..., ResourceReport]


class AsrChunkExecutor:
    """Execute or replay exactly one durable ASR chunk per call."""

    def __init__(
        self,
        store: JobStore,
        engine: AsrEngine,
        *,
        preprocessor: AudioPreprocessor | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        boundary_preflight: BoundaryPreflight = check_resource_preflight,
    ) -> None:
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
            or max_attempts > 10
        ):
            raise InvalidJobRequest("max_attempts must be between 1 and 10.")
        if (
            not isinstance(engine.model_id, str)
            or not engine.model_id
            or len(engine.model_id) > 200
        ):
            raise InvalidJobRequest("ASR engine model_id is invalid.")
        self.store = store
        self.engine = engine
        self.preprocessor = preprocessor or AudioPreprocessor(store)
        self.max_attempts = max_attempts
        self._boundary_preflight = boundary_preflight

    def run_next(self, job_id: str) -> AsrRunResult:
        job = self.store.get_job(job_id)
        if job.state is JobState.PREPROCESSING:
            plan, _ = self.preprocessor.prepare(job_id)
            job = self.store.transition_job(
                job_id,
                JobState.TRANSCRIBING,
                expected_revision=job.revision,
                reason_code="normalized_audio_ready",
                event_type="job.transcription_started",
            )
        elif job.state is JobState.TRANSCRIBING:
            plan = self.preprocessor.get_plan(job_id)
        else:
            raise InvalidJobRequest(
                "ASR execution requires a preprocessing or transcribing job."
            )

        materialized = self._materialized_chunks(job_id)
        attempts = self.store.list_asr_attempts(job_id)
        attempts_by_chunk = {
            chunk.chunk_index: [
                attempt for attempt in attempts if attempt.chunk_index == chunk.chunk_index
            ]
            for chunk in plan.chunks
        }

        for chunk in plan.chunks:
            if chunk.chunk_index in materialized:
                continue
            succeeded = next(
                (
                    attempt
                    for attempt in reversed(attempts_by_chunk[chunk.chunk_index])
                    if attempt.state is AsrAttemptState.SUCCEEDED
                ),
                None,
            )
            if succeeded is not None:
                return self._materialize_succeeded_attempt(
                    job,
                    plan,
                    chunk,
                    succeeded,
                )
            prior_attempts = attempts_by_chunk[chunk.chunk_index]
            if len(prior_attempts) >= self.max_attempts:
                return self._mark_chunk_partial(
                    job,
                    plan,
                    chunk,
                    prior_attempts[-1],
                    issues=(
                        CoverageIssue(
                            code="ASR_CHUNK_RETRIES_EXHAUSTED",
                            message="The ASR chunk exhausted its retry limit.",
                            start_sec=chunk.start_ms / 1000,
                            end_sec=chunk.end_ms / 1000,
                        ),
                    ),
                )
            return self._execute_chunk(
                job,
                plan,
                chunk,
                attempt_number=len(prior_attempts) + 1,
            )

        current = self.store.get_job(job_id)
        self._record_progress(
            current,
            plan,
            plan.chunks[-1],
        )
        aligned = self.store.transition_job(
            job_id,
            JobState.ALIGNING,
            expected_revision=current.revision,
            reason_code="all_asr_chunks_materialized",
            event_type="job.transcription_completed",
        )
        return AsrRunResult(
            outcome=AsrRunOutcome.TRANSCRIPTION_COMPLETED,
            job=aligned,
            chunk_index=None,
            attempt=None,
            issues=(),
            resource_report=None,
        )

    def run_all(self, job_id: str, *, max_chunks: int | None = None) -> AsrBatchResult:
        if max_chunks is not None and (
            not isinstance(max_chunks, int)
            or isinstance(max_chunks, bool)
            or max_chunks < 1
        ):
            raise InvalidJobRequest("max_chunks must be a positive integer or None.")
        completed = 0
        attempts_used = 0
        last_chunk_index: int | None = None
        last_resource: ResourceReport | None = None
        while True:
            result = self.run_next(job_id)
            last_resource = result.resource_report
            if result.chunk_index is not None:
                last_chunk_index = result.chunk_index
            if result.attempt is not None:
                attempts_used += 1
            if result.outcome is AsrRunOutcome.CHUNK_COMPLETED:
                completed += 1
                if max_chunks is not None and completed >= max_chunks:
                    plan = self.preprocessor.get_plan(job_id)
                    return AsrBatchResult(
                        outcome=AsrRunOutcome.BATCH_LIMIT_REACHED,
                        job=self.store.get_job(job_id),
                        completed_chunks=len(self._materialized_chunks(job_id)),
                        total_chunks=len(plan.chunks),
                        attempts_used=attempts_used,
                        last_chunk_index=last_chunk_index,
                        resource_report=last_resource,
                    )
                continue
            if result.outcome is AsrRunOutcome.RETRYABLE_FAILURE:
                continue
            plan = self.preprocessor.get_plan(job_id)
            return AsrBatchResult(
                outcome=result.outcome,
                job=result.job,
                completed_chunks=len(self._materialized_chunks(job_id)),
                total_chunks=len(plan.chunks),
                attempts_used=attempts_used,
                last_chunk_index=last_chunk_index,
                resource_report=last_resource,
            )

    def _execute_chunk(
        self,
        job: JobRecord,
        plan: NormalizedAudioPlan,
        chunk: AudioChunkPlan,
        *,
        attempt_number: int,
    ) -> AsrRunResult:
        resource_report = self._boundary_preflight(
            self.store.data_directory,
            estimated_required_bytes=BOUNDARY_HEADROOM_BYTES,
            model_profile=job.model_profile,
        )
        self.store.put_checkpoint(
            job.job_id,
            stage="transcribing",
            checkpoint_key=f"resource_boundary_{chunk.chunk_index:08d}",
            payload=resource_report.to_dict(),
        )
        if resource_report.status is ResourceStatus.BLOCKED:
            current = self.store.get_job(job.job_id)
            paused = self.store.transition_job(
                job.job_id,
                JobState.PAUSED,
                expected_revision=current.revision,
                reason_code="resource_boundary_blocked",
                error_code="RESOURCE_BOUNDARY_BLOCKED",
                error_message=(
                    "Worker resources must recover before the next ASR chunk can start."
                ),
                event_type="resource.safe_paused",
            )
            return AsrRunResult(
                outcome=AsrRunOutcome.SAFE_PAUSED,
                job=paused,
                chunk_index=chunk.chunk_index,
                attempt=None,
                issues=(),
                resource_report=resource_report,
            )

        audio = _read_chunk_audio(
            self.preprocessor.get_normalized_path(job.job_id),
            chunk,
        )
        started = time.monotonic()
        try:
            raw_payload = self.engine.transcribe(
                audio,
                sample_rate=plan.sample_rate,
                language_hint=job.language_hint,
                context=_job_vocabulary_context(job),
            )
        except Exception as exc:
            elapsed_seconds = time.monotonic() - started
            raw_payload = {
                "exception_type": type(exc).__name__,
            }
            attempt, _ = self.store.commit_asr_attempt(
                job.job_id,
                chunk_index=chunk.chunk_index,
                attempt_number=attempt_number,
                attempt_key=_attempt_key(chunk.chunk_index, attempt_number),
                state=AsrAttemptState.FAILED,
                model_id=self.engine.model_id,
                start_frame=chunk.start_frame,
                end_frame=chunk.end_frame,
                start_ms=chunk.start_ms,
                end_ms=chunk.end_ms,
                raw_payload=raw_payload,
                elapsed_seconds=elapsed_seconds,
                error_code="ASR_ENGINE_EXCEPTION",
            )
            if attempt_number >= self.max_attempts:
                return self._mark_chunk_partial(
                    job,
                    plan,
                    chunk,
                    attempt,
                    issues=(
                        CoverageIssue(
                            code="ASR_ENGINE_EXCEPTION",
                            message="The local ASR engine failed for this chunk.",
                            start_sec=chunk.start_ms / 1000,
                            end_sec=chunk.end_ms / 1000,
                        ),
                    ),
                )
            return AsrRunResult(
                outcome=AsrRunOutcome.RETRYABLE_FAILURE,
                job=self.store.get_job(job.job_id),
                chunk_index=chunk.chunk_index,
                attempt=attempt,
                issues=(
                    CoverageIssue(
                        code="ASR_ENGINE_EXCEPTION",
                        message="The local ASR engine failed for this chunk.",
                    ),
                ),
                resource_report=resource_report,
            )

        elapsed_seconds = time.monotonic() - started
        if not isinstance(raw_payload, dict):
            raw_payload = {
                "invalid_result_type": type(raw_payload).__name__,
            }
        issues = _validate_raw_result(raw_payload, chunk=chunk)
        state = AsrAttemptState.REJECTED if issues else AsrAttemptState.SUCCEEDED
        error_code = "ASR_RESULT_REJECTED" if issues else None
        attempt, _ = self.store.commit_asr_attempt(
            job.job_id,
            chunk_index=chunk.chunk_index,
            attempt_number=attempt_number,
            attempt_key=_attempt_key(chunk.chunk_index, attempt_number),
            state=state,
            model_id=self.engine.model_id,
            start_frame=chunk.start_frame,
            end_frame=chunk.end_frame,
            start_ms=chunk.start_ms,
            end_ms=chunk.end_ms,
            raw_payload=raw_payload,
            language=_optional_string(raw_payload.get("language")),
            finish_reason=_optional_string(raw_payload.get("finish_reason")),
            truncated=bool(raw_payload.get("truncated", False)),
            elapsed_seconds=elapsed_seconds,
            error_code=error_code,
        )
        if issues:
            if attempt_number >= self.max_attempts:
                return self._mark_chunk_partial(
                    job,
                    plan,
                    chunk,
                    attempt,
                    issues=issues,
                )
            return AsrRunResult(
                outcome=AsrRunOutcome.RETRYABLE_FAILURE,
                job=self.store.get_job(job.job_id),
                chunk_index=chunk.chunk_index,
                attempt=attempt,
                issues=issues,
                resource_report=resource_report,
            )
        return self._materialize_succeeded_attempt(
            job,
            plan,
            chunk,
            attempt,
            resource_report=resource_report,
        )

    def _materialize_succeeded_attempt(
        self,
        job: JobRecord,
        plan: NormalizedAudioPlan,
        chunk: AudioChunkPlan,
        attempt: AsrAttemptRecord,
        *,
        resource_report: ResourceReport | None = None,
    ) -> AsrRunResult:
        raw_payload = self.store.get_asr_attempt_payload(
            job.job_id,
            chunk_index=chunk.chunk_index,
            attempt_number=attempt.attempt_number,
        )
        issues = _validate_raw_result(raw_payload, chunk=chunk)
        if issues:
            raise AsrExecutionFailed(
                "A previously succeeded ASR attempt no longer passes validation.",
                details={"chunk_index": chunk.chunk_index},
            )
        source_duration_ms = self.store.get_job_duration_ms(job.job_id)
        segments = _result_segments(
            raw_payload,
            chunk=chunk,
            source_duration_ms=source_duration_ms,
        )
        committed_segment_ids: list[str] = []
        for index, item in enumerate(segments):
            segment, _ = self.store.commit_transcript_segment(
                job.job_id,
                commit_key=f"chunk_{chunk.chunk_index:08d}_segment_{index:04d}",
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                outcome=TranscriptOutcome.TRANSCRIBED,
                text=item["text"],
                language=item["language"],
                timing_status=item["timing_status"],
                speaker_label_status=SpeakerLabelStatus.PENDING,
            )
            committed_segment_ids.append(segment.segment_id)
        self._record_progress(
            job,
            plan,
            chunk,
        )
        self.store.put_checkpoint(
            job.job_id,
            stage="transcribing",
            checkpoint_key=f"chunk_{chunk.chunk_index:08d}_materialized",
            payload={
                "attempt_number": attempt.attempt_number,
                "raw_sha256": attempt.raw_sha256,
                "segment_ids": committed_segment_ids,
                "start_ms": chunk.start_ms,
                "end_ms": chunk.end_ms,
            },
        )
        all_materialized = len(self._materialized_chunks(job.job_id)) == len(plan.chunks)
        if all_materialized:
            current = self.store.get_job(job.job_id)
            aligned = self.store.transition_job(
                job.job_id,
                JobState.ALIGNING,
                expected_revision=current.revision,
                reason_code="all_asr_chunks_materialized",
                event_type="job.transcription_completed",
            )
            return AsrRunResult(
                outcome=AsrRunOutcome.TRANSCRIPTION_COMPLETED,
                job=aligned,
                chunk_index=chunk.chunk_index,
                attempt=attempt,
                issues=(),
                resource_report=resource_report,
            )
        return AsrRunResult(
            outcome=AsrRunOutcome.CHUNK_COMPLETED,
            job=self.store.get_job(job.job_id),
            chunk_index=chunk.chunk_index,
            attempt=attempt,
            issues=(),
            resource_report=resource_report,
        )

    def _mark_chunk_partial(
        self,
        job: JobRecord,
        plan: NormalizedAudioPlan,
        chunk: AudioChunkPlan,
        attempt: AsrAttemptRecord,
        *,
        issues: tuple[CoverageIssue, ...],
    ) -> AsrRunResult:
        source_duration_ms = self.store.get_job_duration_ms(job.job_id)
        self.store.commit_transcript_segment(
            job.job_id,
            commit_key=f"chunk_{chunk.chunk_index:08d}_failed",
            start_ms=chunk.start_ms,
            end_ms=min(chunk.end_ms, source_duration_ms),
            outcome=TranscriptOutcome.FAILED,
            speaker_label_status=SpeakerLabelStatus.UNAVAILABLE,
            error_code="ASR_CHUNK_RETRIES_EXHAUSTED",
        )
        self._record_progress(
            job,
            plan,
            chunk,
        )
        current = self.store.get_job(job.job_id)
        partial = self.store.transition_job(
            job.job_id,
            JobState.PARTIAL,
            expected_revision=current.revision,
            reason_code="asr_chunk_retries_exhausted",
            error_code="ASR_CHUNK_RETRIES_EXHAUSTED",
            error_message="One audio range could not be transcribed after safe retries.",
            event_type="job.partial",
        )
        return AsrRunResult(
            outcome=AsrRunOutcome.PARTIAL,
            job=partial,
            chunk_index=chunk.chunk_index,
            attempt=attempt,
            issues=issues,
            resource_report=None,
        )

    def _materialized_chunks(self, job_id: str) -> set[int]:
        materialized: set[int] = set()
        for checkpoint in self.store.list_checkpoints(job_id, stage="transcribing"):
            prefix = "chunk_"
            suffix = "_materialized"
            if checkpoint.checkpoint_key.startswith(
                prefix
            ) and checkpoint.checkpoint_key.endswith(suffix):
                raw_index = checkpoint.checkpoint_key[
                    len(prefix) : -len(suffix)
                ]
                if raw_index.isdigit():
                    materialized.add(int(raw_index))
        return materialized

    def _record_progress(
        self,
        job: JobRecord,
        plan: NormalizedAudioPlan,
        chunk: AudioChunkPlan,
    ) -> None:
        elapsed_seconds = sum(
            recorded.elapsed_seconds
            for recorded in self.store.list_asr_attempts(job.job_id)
        )
        self.store.put_job_progress(
            job.job_id,
            processed_ms=min(
                chunk.end_ms,
                self.store.get_job_duration_ms(job.job_id),
            ),
            stage_progress=(chunk.chunk_index + 1) / len(plan.chunks),
            elapsed_seconds=elapsed_seconds,
            diarization_status=DiarizationStatus.NOT_STARTED,
        )


def _read_chunk_audio(path: Any, chunk: AudioChunkPlan) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getframerate() != 16_000
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
            ):
                raise AsrExecutionFailed(
                    "Normalized audio changed before ASR execution."
                )
            audio.setpos(chunk.start_frame)
            raw = audio.readframes(chunk.end_frame - chunk.start_frame)
    except (OSError, EOFError, wave.Error) as exc:
        raise AsrExecutionFailed(
            "Normalized audio could not be read for ASR execution."
        ) from exc
    samples = np.frombuffer(raw, dtype="<i2").copy()
    if samples.size != chunk.end_frame - chunk.start_frame:
        raise AsrExecutionFailed(
            "Normalized audio ended before the planned chunk boundary."
        )
    return samples


def _validate_raw_result(
    payload: dict[str, Any],
    *,
    chunk: AudioChunkPlan,
) -> tuple[CoverageIssue, ...]:
    if not isinstance(payload, dict):
        return (
            CoverageIssue(
                code="INVALID_ASR_RESULT",
                message="The ASR engine did not return an object.",
            ),
        )
    duration_seconds = (chunk.end_frame - chunk.start_frame) / 16_000
    raw_chunks = payload.get("chunks")
    if not isinstance(raw_chunks, list):
        return (
            CoverageIssue(
                code="INVALID_ASR_CHUNKS",
                message="The ASR result did not contain a chunk list.",
            ),
        )
    issues = list(
        evaluate_chunk_coverage(
            raw_chunks,
            source_duration_sec=duration_seconds,
        ).issues
    )
    timestamp_segments = payload.get("segments")
    if timestamp_segments is not None and not isinstance(timestamp_segments, list):
        issues.append(
            CoverageIssue(
                code="INVALID_TIMESTAMP_SEGMENTS",
                message="The ASR timestamp payload is not a segment list.",
            )
        )
        timestamp_segments = []
    issues.extend(
        validate_timestamp_segments(
            timestamp_segments,
            source_duration_sec=duration_seconds,
        )
    )
    if payload.get("truncated") and not any(
        issue.code in {"TRUNCATED_CHUNK", "TRUNCATED_RESULT"} for issue in issues
    ):
        issues.append(
            CoverageIssue(
                code="TRUNCATED_RESULT",
                message="The ASR result reports generation truncation.",
            )
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        issues.append(
            CoverageIssue(
                code="EMPTY_ASR_RESULT",
                message="The ASR result did not contain transcript text.",
            )
        )
    timestamp_cursor = 0.0
    for index, segment in enumerate(timestamp_segments or []):
        if not isinstance(segment, dict) or not str(segment.get("text", "")).strip():
            issues.append(
                CoverageIssue(
                    code="EMPTY_TIMESTAMP_SEGMENT",
                    message=f"Timestamp segment {index} does not contain text.",
                )
            )
        try:
            start_seconds = float(segment["start"])
            end_seconds = float(segment["end"])
            if end_seconds < start_seconds:
                issues.append(
                    CoverageIssue(
                        code="INVALID_TIMESTAMP_RANGE",
                        message=f"Timestamp segment {index} ends before it starts.",
                    )
                )
            if start_seconds < timestamp_cursor:
                issues.append(
                    CoverageIssue(
                        code="OVERLAPPING_TIMESTAMP_SEGMENT",
                        message=f"Timestamp segment {index} overlaps prior stable text.",
                    )
                )
            timestamp_cursor = max(timestamp_cursor, end_seconds)
        except (KeyError, TypeError, ValueError):
            pass
    return tuple(issues)


def _result_segments(
    payload: dict[str, Any],
    *,
    chunk: AudioChunkPlan,
    source_duration_ms: int,
) -> list[dict[str, Any]]:
    language = _optional_string(payload.get("language"))
    raw_segments = payload.get("segments") or []
    if not raw_segments:
        return [
            {
                "start_ms": chunk.start_ms,
                "end_ms": min(chunk.end_ms, source_duration_ms),
                "text": str(payload["text"]).strip(),
                "language": language,
                "timing_status": TranscriptTimingStatus.ESTIMATED,
            }
        ]
    normalized_raw: list[tuple[float, float, str]] = []
    for raw_segment in raw_segments:
        try:
            start_seconds = float(raw_segment["start"])
            end_seconds = float(raw_segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(raw_segment.get("text", "")).strip()
        if text:
            normalized_raw.append((start_seconds, end_seconds, text))
    if not normalized_raw:
        return [
            {
                "start_ms": chunk.start_ms,
                "end_ms": min(chunk.end_ms, source_duration_ms),
                "text": str(payload.get("text", "")).strip(),
                "language": language,
                "timing_status": TranscriptTimingStatus.ESTIMATED,
            }
        ]
    normalized_raw = _restore_full_text_separators(
        normalized_raw,
        transcript_text=str(payload.get("text", "")),
    )

    merged: list[dict[str, Any]] = []
    buffer_text: list[str] = []
    buffer_start_ms: int | None = None
    buffer_end_ms: int | None = None
    for index, (start_seconds, end_seconds, text) in enumerate(normalized_raw):
        start_ms = chunk.start_ms + round(start_seconds * 1000)
        end_ms = chunk.start_ms + round(end_seconds * 1000)
        if buffer_start_ms is None:
            buffer_start_ms = start_ms
        if end_ms > start_ms:
            buffer_end_ms = end_ms
        buffer_text.append(text)
        next_start_seconds = (
            normalized_raw[index + 1][0] if index + 1 < len(normalized_raw) else None
        )
        next_gap_ms = (
            chunk.start_ms + round(next_start_seconds * 1000) - (buffer_end_ms or start_ms)
            if next_start_seconds is not None and buffer_end_ms is not None
            else 0
        )
        buffer_chars = sum(len(value) for value in buffer_text)
        should_close = (
            index + 1 == len(normalized_raw)
            or buffer_chars >= 80
            or next_gap_ms > 1500
            or text.endswith(("。", "！", "？", "…", "；", ";", ".", "!", "?"))
        )
        if should_close:
            merged.append(
                {
                    "start_ms": buffer_start_ms,
                    "end_ms": buffer_end_ms if buffer_end_ms is not None else buffer_start_ms + 1,
                    "text": "".join(buffer_text),
                    "language": language,
                    "timing_status": TranscriptTimingStatus.ALIGNED,
                }
            )
            buffer_text = []
            buffer_start_ms = None
            buffer_end_ms = None
    if buffer_text:
        if merged:
            merged[-1]["text"] += "".join(buffer_text)
        else:
            return [
                {
                    "start_ms": chunk.start_ms,
                    "end_ms": min(chunk.end_ms, source_duration_ms),
                    "text": str(payload.get("text", "")).strip(),
                    "language": language,
                    "timing_status": TranscriptTimingStatus.ESTIMATED,
                }
            ]
    for current, following in zip(merged, merged[1:], strict=False):
        gap_ms = following["start_ms"] - current["end_ms"]
        if 0 < gap_ms <= MAX_SENTENCE_PAUSE_BRIDGE_MS:
            current["end_ms"] = following["start_ms"]
    previous_end_ms: int | None = None
    for item in merged:
        item["start_ms"] = min(item["start_ms"], source_duration_ms - 1)
        if previous_end_ms is not None and item["start_ms"] < previous_end_ms:
            # A zero-duration timestamp token may close one readable sentence at
            # exactly the same instant the following sentence begins. Expanding
            # the former to the required one-millisecond stable range must not
            # make the latter overlap it.
            item["start_ms"] = previous_end_ms
        item["end_ms"] = max(item["end_ms"], item["start_ms"] + 1)
        item["end_ms"] = min(item["end_ms"], source_duration_ms)
        previous_end_ms = item["end_ms"]
    return merged


def _restore_full_text_separators(
    timestamp_segments: list[tuple[float, float, str]],
    *,
    transcript_text: str,
) -> list[tuple[float, float, str]]:
    """Restore punctuation omitted by timestamp tokens without changing ASR words."""
    if not transcript_text or not timestamp_segments:
        return timestamp_segments
    restored: list[list[Any]] = []
    cursor = 0
    for start_seconds, end_seconds, token in timestamp_segments:
        original_token = token
        location = transcript_text.find(original_token, cursor)
        if location < 0:
            return timestamp_segments
        separator = transcript_text[cursor:location]
        if separator:
            if restored:
                restored[-1][2] += separator
            else:
                token = separator + token
        restored.append([start_seconds, end_seconds, token])
        cursor = location + len(original_token)
    if cursor < len(transcript_text):
        restored[-1][2] += transcript_text[cursor:]
    rebuilt = "".join(str(item[2]) for item in restored)
    if rebuilt != transcript_text:
        return timestamp_segments
    return [
        (float(start_seconds), float(end_seconds), str(text))
        for start_seconds, end_seconds, text in restored
    ]


def _attempt_key(chunk_index: int, attempt_number: int) -> str:
    return f"chunk_{chunk_index:08d}_attempt_{attempt_number:04d}"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _job_vocabulary_context(job: JobRecord) -> str:
    value = job.options.get("vocabulary_context", "")
    return value if isinstance(value, str) else ""
