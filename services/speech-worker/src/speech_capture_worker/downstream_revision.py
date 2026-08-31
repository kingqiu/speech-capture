"""Create an auditable downstream revision without rerunning immutable raw ASR."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from speech_capture_worker.alignment import TranscriptAlignmentFinalizer
from speech_capture_worker.asr_domain import AsrAttemptRecord, AsrAttemptState
from speech_capture_worker.asr_execution import AsrChunkExecutor
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.domain import CheckpointRecord, JobRecord, JobState
from speech_capture_worker.errors import InvalidJobRequest, UploadStorageError
from speech_capture_worker.gap_retranscription import (
    BOUNDARY_FRAGMENT_REJECTION_PREFIX,
    GAP_RETRANSCRIPTION_PREFIX,
    is_low_coverage_boundary_fragment,
)
from speech_capture_worker.gap_speech_activity import SPEECH_ACTIVITY_CHECKPOINT_KEY
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.natural_pause import NaturalPauseMaterializer
from speech_capture_worker.transcript import TranscriptSegment

DERIVED_REVISION_STAGE = "derived_revision"
DERIVED_REVISION_CHECKPOINT_KEY = "immutable_asr_replay"
DERIVED_REVISION_SCHEMA_VERSION = "1.0.0"
FAILED_RETRANSCRIPTION_PREFIX = "gap_retranscription_failed_"


@dataclass(frozen=True)
class DownstreamRevisionResult:
    source_job: JobRecord
    revision_job: JobRecord
    created: bool
    copied_asr_attempt_count: int
    rejected_boundary_fragment_count: int
    materialized_pause_count: int


class _EvidenceReplayEngine:
    model_id = "evidence-replay/no-inference"

    def transcribe(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Immutable ASR replay attempted fresh model inference.")


class DownstreamRevisionCreator:
    """Replay immutable ASR into a new job, then rebuild only downstream output."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.preprocessor = AudioPreprocessor(store)

    def create(
        self,
        source_job_id: str,
        *,
        idempotency_key: str,
    ) -> DownstreamRevisionResult:
        source = self.store.get_job(source_job_id)
        if source.state not in {JobState.PROCESSED, JobState.PUBLISHED}:
            raise InvalidJobRequest(
                "A downstream revision requires a processed or published source job."
            )
        if source.source_upload_id is None:
            raise InvalidJobRequest(
                "A downstream revision requires the source's verified upload."
            )
        source_attempts = self.store.list_asr_attempts(source_job_id)
        if not source_attempts:
            raise InvalidJobRequest("The source job has no immutable raw ASR attempts.")

        revision, created = self.store.create_job_from_upload(
            source.source_upload_id,
            idempotency_key=idempotency_key,
            model_profile=source.model_profile,
            language_hint=source.language_hint,
            content_type_override=source.content_type_override,
            options=source.options,
        )
        provenance = _checkpoint_by_key(
            self.store.list_checkpoints(revision.job_id, stage=DERIVED_REVISION_STAGE),
            DERIVED_REVISION_CHECKPOINT_KEY,
        )
        if not created:
            if provenance is None or provenance.payload.get("source_job_id") != source_job_id:
                raise InvalidJobRequest(
                    "The revision idempotency key is bound to a different operation."
                )
            if provenance.payload.get("status") == "complete":
                return DownstreamRevisionResult(
                    source_job=source,
                    revision_job=revision,
                    created=False,
                    copied_asr_attempt_count=int(
                        provenance.payload.get("raw_asr_attempt_count", 0)
                    ),
                    rejected_boundary_fragment_count=int(
                        provenance.payload.get("rejected_boundary_fragment_count", 0)
                    ),
                    materialized_pause_count=int(
                        provenance.payload.get("materialized_pause_count", 0)
                    ),
                )
        else:
            self.store.put_checkpoint(
                revision.job_id,
                stage=DERIVED_REVISION_STAGE,
                checkpoint_key=DERIVED_REVISION_CHECKPOINT_KEY,
                payload={
                    "schema_version": DERIVED_REVISION_SCHEMA_VERSION,
                    "status": "preparing",
                    "source_job_id": source.job_id,
                    "source_job_revision": source.revision,
                    "source_state": source.state.value,
                    "source_sha256": source.source_sha256,
                    "source_upload_id": source.source_upload_id,
                    "fresh_asr_inference": False,
                },
            )

        copied_attempts = self._replay_primary_asr(
            source,
            revision,
            source_attempts=source_attempts,
        )
        current = self.store.get_job(revision.job_id)
        rejected_count = 0
        pause_count = 0
        if current.state is JobState.ALIGNING:
            aligned = TranscriptAlignmentFinalizer(self.store).finalize(revision.job_id)
            current = aligned.job
            if current.state is JobState.ALIGNING:
                replayed_rejections = self._replay_gap_evidence(
                    source,
                    current,
                    alignment_generation=aligned.checkpoint_generation,
                    alignment_sha256=_alignment_sha256(self.store, revision.job_id),
                )
                if replayed_rejections is None:
                    self.store.put_checkpoint(
                        revision.job_id,
                        stage=DERIVED_REVISION_STAGE,
                        checkpoint_key=DERIVED_REVISION_CHECKPOINT_KEY,
                        payload={
                            "schema_version": DERIVED_REVISION_SCHEMA_VERSION,
                            "status": "awaiting_recomputed_gap_evidence",
                            "source_job_id": source.job_id,
                            "source_job_revision": source.revision,
                            "source_state": source.state.value,
                            "source_sha256": source.source_sha256,
                            "source_upload_id": source.source_upload_id,
                            "raw_asr_attempt_count": len(source_attempts),
                            "fresh_asr_inference": False,
                            "reason_code": "REPLAYED_TIMELINE_CHANGED",
                        },
                    )
                    return DownstreamRevisionResult(
                        source_job=source,
                        revision_job=self.store.get_job(revision.job_id),
                        created=created,
                        copied_asr_attempt_count=len(source_attempts),
                        rejected_boundary_fragment_count=0,
                        materialized_pause_count=0,
                    )
                rejected_count = replayed_rejections
                pauses = NaturalPauseMaterializer(self.store).materialize(revision.job_id)
                pause_count = pauses.created_segment_count
                current = pauses.job
        if current.state is JobState.ALIGNING:
            raise InvalidJobRequest(
                "Immutable evidence did not safely account for every downstream gap."
            )
        if current.state not in {
            JobState.DIARIZING,
            JobState.STRUCTURING,
            JobState.QUALITY_CHECK,
            JobState.PROCESSED,
            JobState.PUBLISHING,
            JobState.PUBLISHED,
        }:
            raise InvalidJobRequest("The downstream revision entered an unexpected state.")

        copied_attempts = len(self.store.list_asr_attempts(revision.job_id))
        revision_alignment_checkpoints = self.store.list_checkpoints(
            revision.job_id,
            stage="aligning",
        )
        rejected_count = sum(
            checkpoint.checkpoint_key.startswith(BOUNDARY_FRAGMENT_REJECTION_PREFIX)
            for checkpoint in revision_alignment_checkpoints
        )
        pause_count = sum(
            segment.commit_key.startswith("natural_pause_")
            for segment in _all_segments(self.store, revision.job_id)
        )
        source_attempt_digest = hashlib.sha256(
            json.dumps(
                [attempt.raw_sha256 for attempt in source_attempts],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.store.put_checkpoint(
            revision.job_id,
            stage=DERIVED_REVISION_STAGE,
            checkpoint_key=DERIVED_REVISION_CHECKPOINT_KEY,
            payload={
                "schema_version": DERIVED_REVISION_SCHEMA_VERSION,
                "status": "complete",
                "source_job_id": source.job_id,
                "source_job_revision": source.revision,
                "source_state": source.state.value,
                "source_sha256": source.source_sha256,
                "source_upload_id": source.source_upload_id,
                "raw_asr_attempt_count": len(source_attempts),
                "raw_asr_sha256": source_attempt_digest,
                "fresh_asr_inference": False,
                "rejected_boundary_fragment_count": rejected_count,
                "materialized_pause_count": pause_count,
            },
        )
        return DownstreamRevisionResult(
            source_job=source,
            revision_job=self.store.get_job(revision.job_id),
            created=created,
            copied_asr_attempt_count=copied_attempts,
            rejected_boundary_fragment_count=rejected_count,
            materialized_pause_count=pause_count,
        )

    def _replay_primary_asr(
        self,
        source: JobRecord,
        revision: JobRecord,
        *,
        source_attempts: list[AsrAttemptRecord],
    ) -> int | None:
        current = self.store.get_job(revision.job_id)
        if current.state is JobState.QUEUED:
            current = self.store.claim_job_for_processing(
                current.job_id,
                expected_revision=current.revision,
            )
        if current.state is JobState.PREPROCESSING:
            target_plan, _ = self.preprocessor.prepare(current.job_id)
            source_plan = self.preprocessor.get_plan(source.job_id)
            if target_plan.normalized_sha256 != source_plan.normalized_sha256:
                raise InvalidJobRequest(
                    "Normalized audio differs from the immutable source-job evidence."
                )
            current = self.store.transition_job(
                current.job_id,
                JobState.TRANSCRIBING,
                expected_revision=current.revision,
                reason_code="derived_revision_replays_raw_asr",
                event_type="job.transcription_started",
            )
        copied = 0
        if current.state is JobState.TRANSCRIBING:
            for attempt in source_attempts:
                raw_payload = self.store.get_asr_attempt_payload(
                    source.job_id,
                    chunk_index=attempt.chunk_index,
                    attempt_number=attempt.attempt_number,
                )
                copied_attempt, was_created = self.store.commit_asr_attempt(
                    current.job_id,
                    chunk_index=attempt.chunk_index,
                    attempt_number=attempt.attempt_number,
                    attempt_key=attempt.attempt_key,
                    state=attempt.state,
                    model_id=attempt.model_id,
                    start_frame=attempt.start_frame,
                    end_frame=attempt.end_frame,
                    start_ms=attempt.start_ms,
                    end_ms=attempt.end_ms,
                    raw_payload=raw_payload,
                    language=attempt.language,
                    finish_reason=attempt.finish_reason,
                    truncated=attempt.truncated,
                    elapsed_seconds=attempt.elapsed_seconds,
                    error_code=attempt.error_code,
                )
                if copied_attempt.raw_sha256 != attempt.raw_sha256:
                    raise InvalidJobRequest("Replayed raw ASR evidence changed unexpectedly.")
                copied += int(was_created)
            target_plan = self.preprocessor.get_plan(current.job_id)
            attempts = self.store.list_asr_attempts(current.job_id)
            successful_chunks = {
                attempt.chunk_index
                for attempt in attempts
                if attempt.state is AsrAttemptState.SUCCEEDED
            }
            planned_chunks = {chunk.chunk_index for chunk in target_plan.chunks}
            if successful_chunks != planned_chunks:
                raise InvalidJobRequest(
                    "Every planned chunk needs immutable successful ASR evidence."
                )
            AsrChunkExecutor(
                self.store,
                _EvidenceReplayEngine(),
            ).run_all(current.job_id)
        return copied

    def _replay_gap_evidence(
        self,
        source: JobRecord,
        revision: JobRecord,
        *,
        alignment_generation: int,
        alignment_sha256: str,
    ) -> int:
        alignment = _checkpoint_by_key(
            self.store.list_checkpoints(revision.job_id, stage="aligning"),
            "transcript_alignment_report",
        )
        if alignment is None:
            raise InvalidJobRequest("The revision alignment evidence is unavailable.")
        unresolved = {
            (value["start_ms"], value["end_ms"])
            for value in alignment.payload.get("unresolved_ranges", [])
            if isinstance(value, dict)
            and isinstance(value.get("start_ms"), int)
            and isinstance(value.get("end_ms"), int)
        }
        source_checkpoints = self.store.list_checkpoints(source.job_id, stage="aligning")
        source_speech = _checkpoint_by_key(
            source_checkpoints,
            SPEECH_ACTIVITY_CHECKPOINT_KEY,
        )
        if source_speech is None or not isinstance(source_speech.payload.get("evidence"), list):
            raise InvalidJobRequest("The source VAD evidence is unavailable.")
        evidence_by_range = {
            (value.get("start_ms"), value.get("end_ms")): value
            for value in source_speech.payload["evidence"]
            if isinstance(value, dict)
        }
        if set(evidence_by_range) != unresolved:
            # A newer deterministic materializer may derive a safer corrected
            # timeline from the exact same immutable ASR payload. Source VAD is
            # range-pinned and must not be stretched onto those changed gaps;
            # leave the revision at ALIGNING so the normal background path can
            # compute fresh downstream gap evidence without fresh primary ASR.
            return None
        self.store.put_checkpoint(
            revision.job_id,
            stage="aligning",
            checkpoint_key=SPEECH_ACTIVITY_CHECKPOINT_KEY,
            payload={
                "schema_version": source_speech.payload.get("schema_version", "1.0.0"),
                "alignment_report_generation": alignment_generation,
                "alignment_report_sha256": alignment_sha256,
                "evidence": [evidence_by_range[value] for value in sorted(unresolved)],
                "replayed_from_job_id": source.job_id,
                "replayed_from_checkpoint_sha256": source_speech.payload_sha256,
            },
        )

        source_segments = {
            segment.segment_id: segment
            for segment in _all_segments(self.store, source.job_id)
        }
        rejected = 0
        for start_ms, end_ms in sorted(unresolved):
            vad_evidence = evidence_by_range[(start_ms, end_ms)]
            if (
                vad_evidence.get("observation") == "no_speech_detected"
                and vad_evidence.get("speech_duration_ms") == 0
                and vad_evidence.get("speech_regions") == []
            ):
                continue
            suffix = f"{start_ms:010d}_{end_ms:010d}"
            existing_rejection = _checkpoint_by_key(
                source_checkpoints,
                f"{BOUNDARY_FRAGMENT_REJECTION_PREFIX}{suffix}",
            )
            existing_failure = _checkpoint_by_key(
                source_checkpoints,
                f"{FAILED_RETRANSCRIPTION_PREFIX}{suffix}",
            )
            success = _checkpoint_by_key(
                source_checkpoints,
                f"{GAP_RETRANSCRIPTION_PREFIX}{suffix}",
            )
            if existing_rejection is not None:
                payload = dict(existing_rejection.payload)
                payload["replayed_from_job_id"] = source.job_id
                payload["replayed_from_checkpoint_sha256"] = (
                    existing_rejection.payload_sha256
                )
                self.store.put_checkpoint(
                    revision.job_id,
                    stage="aligning",
                    checkpoint_key=f"{BOUNDARY_FRAGMENT_REJECTION_PREFIX}{suffix}",
                    payload=payload,
                )
                rejected += 1
                continue
            if existing_failure is not None:
                payload = dict(existing_failure.payload)
                payload["replayed_from_job_id"] = source.job_id
                payload["replayed_from_checkpoint_sha256"] = existing_failure.payload_sha256
                self.store.put_checkpoint(
                    revision.job_id,
                    stage="aligning",
                    checkpoint_key=f"{FAILED_RETRANSCRIPTION_PREFIX}{suffix}",
                    payload=payload,
                )
                continue
            if success is None:
                raise InvalidJobRequest(
                    "A replayed speech gap has no immutable retranscription evidence."
                )
            segment_ids = success.payload.get("segment_ids")
            if not isinstance(segment_ids, list) or not segment_ids:
                raise InvalidJobRequest("The source gap evidence has no stable segments.")
            gap_segments = [source_segments.get(str(segment_id)) for segment_id in segment_ids]
            if any(segment is None for segment in gap_segments):
                raise InvalidJobRequest("The source gap evidence references missing segments.")
            values = [
                {
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "text": segment.text,
                }
                for segment in gap_segments
                if segment is not None
            ]
            if not is_low_coverage_boundary_fragment(
                values,
                gap_start_ms=start_ms,
                gap_end_ms=end_ms,
            ):
                raise InvalidJobRequest(
                    "A source gap contains meaningful speech and cannot be discarded by replay."
                )
            raw_relative_path = _copy_gap_raw_evidence(
                self.store,
                source_job_id=source.job_id,
                revision_job_id=revision.job_id,
                checkpoint=success,
            )
            self.store.put_checkpoint(
                revision.job_id,
                stage="aligning",
                checkpoint_key=f"{BOUNDARY_FRAGMENT_REJECTION_PREFIX}{suffix}",
                payload={
                    "schema_version": "1.0.0",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "reason_code": "LOW_COVERAGE_BOUNDARY_FRAGMENT",
                    "raw_relative_path": raw_relative_path,
                    "raw_sha256": success.payload.get("raw_sha256"),
                    "segment_count": len(values),
                    "transcribed_duration_ms": sum(
                        value["end_ms"] - value["start_ms"] for value in values
                    ),
                    "replayed_from_job_id": source.job_id,
                    "replayed_from_checkpoint_sha256": success.payload_sha256,
                },
            )
            rejected += 1
        return rejected


def _copy_gap_raw_evidence(
    store: JobStore,
    *,
    source_job_id: str,
    revision_job_id: str,
    checkpoint: CheckpointRecord,
) -> str:
    relative = checkpoint.payload.get("raw_relative_path")
    expected_sha256 = checkpoint.payload.get("raw_sha256")
    if not isinstance(relative, str) or not isinstance(expected_sha256, str):
        raise InvalidJobRequest("The source gap evidence path is invalid.")
    unresolved_source_path = store.data_directory / relative
    if unresolved_source_path.is_symlink():
        raise UploadStorageError("The source gap evidence is unavailable.")
    source_path = unresolved_source_path.resolve()
    source_root = (store.jobs_directory / source_job_id).resolve()
    if (
        not source_path.is_relative_to(source_root)
        or not source_path.is_file()
    ):
        raise UploadStorageError("The source gap evidence is unavailable.")
    content = source_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise UploadStorageError("The source gap evidence failed checksum verification.")
    destination_dir = store.get_job_stage_directory(
        revision_job_id,
        stage="gap_retranscription_raw",
    )
    destination = destination_dir / f"replayed-{expected_sha256[:16]}.json"
    if destination.exists():
        if destination.is_symlink():
            raise UploadStorageError(
                "The replayed gap evidence conflicts with existing data."
            )
        destination_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
        if destination_sha256 != expected_sha256:
            raise UploadStorageError("The replayed gap evidence conflicts with existing data.")
    else:
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
    return destination.relative_to(store.data_directory).as_posix()


def _alignment_sha256(store: JobStore, job_id: str) -> str:
    checkpoint = _checkpoint_by_key(
        store.list_checkpoints(job_id, stage="aligning"),
        "transcript_alignment_report",
    )
    if checkpoint is None:
        raise InvalidJobRequest("The revision alignment evidence is unavailable.")
    return checkpoint.payload_sha256


def _all_segments(store: JobStore, job_id: str) -> list[TranscriptSegment]:
    values: list[TranscriptSegment] = []
    after_sequence = 0
    while True:
        snapshot = store.get_job_snapshot(
            job_id,
            after_segment_sequence=after_sequence,
            segment_limit=500,
        )
        values.extend(snapshot.stable_segments)
        if not snapshot.has_more_segments:
            return values
        after_sequence = snapshot.next_after_segment_sequence


def _checkpoint_by_key(
    checkpoints: list[CheckpointRecord],
    checkpoint_key: str,
) -> CheckpointRecord | None:
    return next(
        (checkpoint for checkpoint in checkpoints if checkpoint.checkpoint_key == checkpoint_key),
        None,
    )
