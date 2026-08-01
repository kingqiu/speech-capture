"""Targeted ASR re-transcription of VAD-identified speech gaps."""

from __future__ import annotations

import hashlib
import json
import os
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import numpy as np

from speech_capture_worker.alignment import (
    CHECKPOINT_KEY as ALIGNMENT_CHECKPOINT_KEY,
)
from speech_capture_worker.alignment import (
    CHECKPOINT_STAGE as ALIGNMENT_STAGE,
)
from speech_capture_worker.alignment import (
    AlignmentFinalizationResult,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.asr_execution import (
    _result_segments,
    _validate_raw_result,
)
from speech_capture_worker.audio_preprocessing import (
    AudioChunkPlan,
    AudioPreprocessor,
    NormalizedAudioPlan,
)
from speech_capture_worker.domain import JobRecord, JobState, ResourceStatus
from speech_capture_worker.errors import (
    GapRetranscriptionFailed,
    InvalidJobRequest,
    NormalizedAudioInvalid,
    UploadStorageError,
)
from speech_capture_worker.gap_speech_activity import (
    SPEECH_ACTIVITY_CHECKPOINT_KEY,
    SpeechActivityObservation,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import GIB, ResourceReport, check_resource_preflight
from speech_capture_worker.transcript import (
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
)

GAP_RETRANSCRIPTION_SCHEMA_VERSION = "1.0.0"
GAP_RETRANSCRIPTION_RAW_SCHEMA_VERSION = "1.0.0"
GAP_RETRANSCRIPTION_HEADROOM_BYTES = GIB
GAP_RETRANSCRIPTION_PREFIX = "gap_retranscription_"


class GapRetranscriptionEngine(Protocol):
    model_id: str

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
        language_hint: str | None,
        context: str,
    ) -> dict[str, Any]: ...


class GapRetranscriptionOutcome(StrEnum):
    COMPLETED = "completed"
    NO_SPEECH_GAPS = "no_speech_gaps"
    SAFE_PAUSED = "safe_paused"


@dataclass(frozen=True)
class GapRetranscriptionResult:
    outcome: GapRetranscriptionOutcome
    job: JobRecord
    retranscribed_gap_count: int
    failed_gap_count: int
    added_segment_count: int
    alignment: AlignmentFinalizationResult | None
    resource_report: ResourceReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict(),
            "retranscribed_gap_count": self.retranscribed_gap_count,
            "failed_gap_count": self.failed_gap_count,
            "added_segment_count": self.added_segment_count,
            "alignment": self.alignment.to_dict() if self.alignment is not None else None,
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
        }


BoundaryPreflight = Callable[..., ResourceReport]


class GapRetranscriptionExecutor:
    """Transcribe unresolved speech gaps with durable raw evidence."""

    def __init__(
        self,
        store: JobStore,
        engine: GapRetranscriptionEngine,
        *,
        preprocessor: AudioPreprocessor | None = None,
        finalizer: TranscriptAlignmentFinalizer | None = None,
        boundary_preflight: BoundaryPreflight = check_resource_preflight,
        max_attempts: int = 3,
    ) -> None:
        if (
            not isinstance(engine.model_id, str)
            or not engine.model_id
            or len(engine.model_id) > 200
            or any(not character.isprintable() for character in engine.model_id)
        ):
            raise InvalidJobRequest("Gap retranscription engine model_id is invalid.")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
            or max_attempts > 5
        ):
            raise InvalidJobRequest("max_attempts must be between 1 and 5.")
        self.store = store
        self.engine = engine
        self.preprocessor = preprocessor or AudioPreprocessor(store)
        self.finalizer = finalizer or TranscriptAlignmentFinalizer(store)
        self._boundary_preflight = boundary_preflight
        self._max_attempts = max_attempts

    def run(self, job_id: str) -> GapRetranscriptionResult:
        job = self.store.get_job(job_id)
        if job.state is JobState.DIARIZING:
            alignment = self.finalizer.finalize(job_id)
            return GapRetranscriptionResult(
                outcome=GapRetranscriptionOutcome.COMPLETED,
                job=alignment.job,
                retranscribed_gap_count=0,
                failed_gap_count=0,
                added_segment_count=0,
                alignment=alignment,
                resource_report=None,
            )
        if job.state is not JobState.ALIGNING:
            raise InvalidJobRequest(
                "Gap retranscription requires an aligning or diarizing job."
            )
        alignment_checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE),
            ALIGNMENT_CHECKPOINT_KEY,
        )
        if alignment_checkpoint is None:
            raise GapRetranscriptionFailed(
                "Gap retranscription requires the current alignment report."
            )
        speech_checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE),
            SPEECH_ACTIVITY_CHECKPOINT_KEY,
        )
        if speech_checkpoint is None:
            raise GapRetranscriptionFailed(
                "Gap retranscription requires speech-activity evidence."
            )
        if (
            speech_checkpoint.payload.get("alignment_report_generation")
            != alignment_checkpoint.generation
            or speech_checkpoint.payload.get("alignment_report_sha256")
            != alignment_checkpoint.payload_sha256
        ):
            raise GapRetranscriptionFailed(
                "Speech-activity evidence is stale relative to the alignment report."
            )

        gaps = [
            evidence
            for evidence in speech_checkpoint.payload.get("evidence", [])
            if isinstance(evidence, dict)
            and evidence.get("observation") == SpeechActivityObservation.SPEECH_DETECTED.value
        ]
        gaps.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
        if not gaps:
            alignment = self.finalizer.finalize(job_id)
            return GapRetranscriptionResult(
                outcome=GapRetranscriptionOutcome.NO_SPEECH_GAPS,
                job=alignment.job,
                retranscribed_gap_count=0,
                failed_gap_count=0,
                added_segment_count=0,
                alignment=alignment,
                resource_report=None,
            )

        plan = self.preprocessor.get_plan(job_id)
        retranscribed = 0
        failed = 0
        added_segments = 0
        last_resource: ResourceReport | None = None
        for index, gap in enumerate(gaps):
            start_ms = gap["start_ms"]
            end_ms = gap["end_ms"]
            if not isinstance(start_ms, int) or not isinstance(end_ms, int) or end_ms <= start_ms:
                failed += 1
                continue
            if self._has_successful_evidence(job_id, start_ms, end_ms):
                continue
            resource_report = self._boundary_preflight(
                self.store.data_directory,
                estimated_required_bytes=GAP_RETRANSCRIPTION_HEADROOM_BYTES,
                model_profile=job.model_profile,
            )
            last_resource = resource_report
            self.store.put_checkpoint(
                job_id,
                stage=ALIGNMENT_STAGE,
                checkpoint_key=f"gap_retranscription_resource_{start_ms:010d}",
                payload=resource_report.to_dict(),
            )
            if resource_report.status is ResourceStatus.BLOCKED:
                current = self.store.get_job(job_id)
                paused = self.store.transition_job(
                    job_id,
                    JobState.PAUSED,
                    expected_revision=current.revision,
                    reason_code="gap_retranscription_resource_blocked",
                    error_code="GAP_RETRANSCRIPTION_RESOURCE_BLOCKED",
                    error_message=(
                        "Worker resources must recover before gap retranscription can start."
                    ),
                    event_type="resource.safe_paused",
                )
                return GapRetranscriptionResult(
                    outcome=GapRetranscriptionOutcome.SAFE_PAUSED,
                    job=paused,
                    retranscribed_gap_count=retranscribed,
                    failed_gap_count=failed,
                    added_segment_count=added_segments,
                    alignment=None,
                    resource_report=resource_report,
                )

            segment_ids: list[str] = []
            segment_commit_keys: list[str] = []
            attempt = 0
            last_error: str | None = None
            while attempt < self._max_attempts:
                attempt += 1
                audio, start_frame, end_frame = _read_gap_audio(
                    self.preprocessor.get_normalized_path(job_id),
                    plan=plan,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
                started = time.monotonic()
                try:
                    raw_payload = self.engine.transcribe(
                        audio,
                        sample_rate=plan.sample_rate,
                        language_hint=job.language_hint,
                        context="",
                    )
                except Exception as exc:
                    last_error = type(exc).__name__
                    raw_payload = {"exception_type": last_error}
                    issues = (
                        GapRetranscriptionFailed(
                            "The ASR engine failed for a speech gap.",
                            details={"start_ms": start_ms, "end_ms": end_ms},
                        ),
                    )
                else:
                    elapsed_seconds = time.monotonic() - started
                    chunk = AudioChunkPlan(
                        chunk_index=index,
                        start_frame=start_frame,
                        end_frame=end_frame,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    issues = _validate_raw_result(raw_payload, chunk=chunk)
                if not issues:
                    elapsed_seconds = time.monotonic() - started
                    chunk = AudioChunkPlan(
                        chunk_index=index,
                        start_frame=start_frame,
                        end_frame=end_frame,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    segments = _result_segments(
                        raw_payload,
                        chunk=chunk,
                        source_duration_ms=self.store.get_job_duration_ms(job_id),
                    )
                    raw_bytes = _canonical_json(
                        {
                            "schema_version": GAP_RETRANSCRIPTION_RAW_SCHEMA_VERSION,
                            "model_id": self.engine.model_id,
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "start_frame": start_frame,
                            "end_frame": end_frame,
                            "normalized_sha256": plan.normalized_sha256,
                            "payload": raw_payload,
                        }
                    ).encode("utf-8")
                    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                    raw_relative_path = self._write_private_evidence(
                        job_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        raw_sha256=raw_sha256,
                        raw_bytes=raw_bytes,
                    )
                    for segment_index, item in enumerate(segments):
                        segment, _ = self.store.commit_transcript_segment(
                            job_id,
                            commit_key=(
                                f"gap_{start_ms:010d}_{end_ms:010d}"
                                f"_segment_{segment_index:04d}"
                            ),
                            start_ms=item["start_ms"],
                            end_ms=item["end_ms"],
                            outcome=TranscriptOutcome.TRANSCRIBED,
                            text=item["text"],
                            language=item["language"],
                            timing_status=TranscriptTimingStatus.ALIGNED,
                            speaker_label_status=SpeakerLabelStatus.PENDING,
                            allow_aligning=True,
                        )
                        segment_ids.append(segment.segment_id)
                        segment_commit_keys.append(segment.commit_key)
                        added_segments += 1
                    self.store.put_checkpoint(
                        job_id,
                        stage=ALIGNMENT_STAGE,
                        checkpoint_key=self._evidence_key(start_ms, end_ms),
                        payload={
                            "schema_version": GAP_RETRANSCRIPTION_SCHEMA_VERSION,
                            "alignment_report_generation": alignment_checkpoint.generation,
                            "alignment_report_sha256": alignment_checkpoint.payload_sha256,
                            "model_id": self.engine.model_id,
                            "normalized_sha256": plan.normalized_sha256,
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "raw_relative_path": raw_relative_path,
                            "raw_sha256": raw_sha256,
                            "segment_ids": segment_ids,
                            "segment_commit_keys": segment_commit_keys,
                            "elapsed_seconds": round(elapsed_seconds, 6),
                        },
                    )
                    retranscribed += 1
                    break
                if attempt >= self._max_attempts:
                    self.store.put_checkpoint(
                        job_id,
                        stage=ALIGNMENT_STAGE,
                        checkpoint_key=(
                            f"gap_retranscription_failed_{start_ms:010d}_{end_ms:010d}"
                        ),
                        payload={
                            "schema_version": GAP_RETRANSCRIPTION_SCHEMA_VERSION,
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "attempt_count": attempt,
                            "last_error_code": last_error,
                        },
                    )
                    failed += 1

        alignment = self.finalizer.finalize(job_id)
        return GapRetranscriptionResult(
            outcome=GapRetranscriptionOutcome.COMPLETED,
            job=alignment.job,
            retranscribed_gap_count=retranscribed,
            failed_gap_count=failed,
            added_segment_count=added_segments,
            alignment=alignment,
            resource_report=last_resource,
        )

    def _has_successful_evidence(self, job_id: str, start_ms: int, end_ms: int) -> bool:
        return (
            _checkpoint_by_key(
                self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE),
                self._evidence_key(start_ms, end_ms),
            )
            is not None
        )

    @staticmethod
    def _evidence_key(start_ms: int, end_ms: int) -> str:
        return f"{GAP_RETRANSCRIPTION_PREFIX}{start_ms:010d}_{end_ms:010d}"

    def _write_private_evidence(
        self,
        job_id: str,
        *,
        start_ms: int,
        end_ms: int,
        raw_sha256: str,
        raw_bytes: bytes,
    ) -> str:
        directory = self.store.get_job_stage_directory(
            job_id,
            stage="gap_retranscription_raw",
        )
        path = directory / f"gap-{start_ms:010d}-{end_ms:010d}-{raw_sha256[:16]}.json"
        if path.is_symlink():
            raise UploadStorageError(
                "Private gap-retranscription evidence must not be a symbolic link."
            )
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise UploadStorageError(
                    "Private gap-retranscription evidence could not be verified."
                ) from exc
            if hashlib.sha256(existing).hexdigest() != raw_sha256:
                raise UploadStorageError(
                    "Private gap-retranscription evidence has conflicting content."
                )
        else:
            _atomic_write_bytes(path, raw_bytes)
        return path.relative_to(self.store.data_directory).as_posix()


def _read_gap_audio(
    path: Path,
    *,
    plan: NormalizedAudioPlan,
    start_ms: int,
    end_ms: int,
) -> tuple[np.ndarray, int, int]:
    start_frame = round(start_ms * plan.sample_rate / 1000)
    end_frame = round(end_ms * plan.sample_rate / 1000)
    if start_frame < 0 or end_frame <= start_frame or end_frame > plan.total_frames:
        raise NormalizedAudioInvalid("The gap range does not map to normalized PCM.")
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getframerate() != plan.sample_rate
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getnframes() != plan.total_frames
            ):
                raise NormalizedAudioInvalid(
                    "Normalized audio changed before gap retranscription."
                )
            audio.setpos(start_frame)
            raw = audio.readframes(end_frame - start_frame)
    except (OSError, EOFError, wave.Error) as exc:
        raise NormalizedAudioInvalid(
            "Normalized audio could not be read for gap retranscription."
        ) from exc
    samples = np.frombuffer(raw, dtype="<i2").copy()
    if samples.size != end_frame - start_frame:
        raise NormalizedAudioInvalid(
            "Normalized audio ended during gap retranscription."
        )
    return samples, start_frame, end_frame


def _checkpoint_by_key(checkpoints: list[Any], checkpoint_key: str) -> Any | None:
    return next(
        (checkpoint for checkpoint in checkpoints if checkpoint.checkpoint_key == checkpoint_key),
        None,
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    if destination.parent.is_symlink() or destination.is_symlink():
        raise UploadStorageError(
            "Private gap-retranscription storage must not contain symbolic links."
        )
    temporary_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        file_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    file_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
