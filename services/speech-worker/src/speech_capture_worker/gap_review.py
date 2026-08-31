"""Explicit human-review evidence for unresolved transcript timeline ranges."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from speech_capture_worker.alignment import (
    ALIGNMENT_REPORT_SCHEMA_VERSION,
    CHECKPOINT_STAGE,
    AlignmentFinalizationResult,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.alignment import (
    CHECKPOINT_KEY as ALIGNMENT_CHECKPOINT_KEY,
)
from speech_capture_worker.domain import (
    SAFE_IDENTIFIER_PATTERN,
    CheckpointRecord,
    JobRecord,
    JobState,
)
from speech_capture_worker.errors import InvalidJobRequest, TranscriptConflict
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.transcript import (
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptSegment,
    TranscriptTimingStatus,
)

GAP_REVIEW_SCHEMA_VERSION = "1.0.0"
GAP_REVIEW_EVIDENCE_TYPE = "explicit_human_review"


class GapReviewResultOutcome(StrEnum):
    MATERIALIZED = "materialized"
    ALREADY_MATERIALIZED = "already_materialized"


@dataclass(frozen=True)
class GapReviewResult:
    outcome: GapReviewResultOutcome
    job: JobRecord
    segment: TranscriptSegment
    created: bool
    review_checkpoint_generation: int
    materialization_checkpoint_generation: int
    alignment: AlignmentFinalizationResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "job": self.job.to_dict(),
            "segment": self.segment.to_dict(),
            "created": self.created,
            "review_checkpoint_generation": self.review_checkpoint_generation,
            "materialization_checkpoint_generation": (self.materialization_checkpoint_generation),
            "alignment": self.alignment.to_dict(),
        }


class ReviewedGapMaterializer:
    """Commit one exact current gap from an explicit, idempotent review decision."""

    def __init__(
        self,
        store: JobStore,
        *,
        finalizer: TranscriptAlignmentFinalizer | None = None,
    ) -> None:
        self.store = store
        self.finalizer = finalizer or TranscriptAlignmentFinalizer(store)

    def materialize(
        self,
        job_id: str,
        *,
        review_key: str,
        start_ms: int,
        end_ms: int,
        outcome: TranscriptOutcome,
    ) -> GapReviewResult:
        _validate_request(
            review_key=review_key,
            start_ms=start_ms,
            end_ms=end_ms,
            outcome=outcome,
        )
        job = self.store.get_job(job_id)
        if job.state not in {JobState.ALIGNING, JobState.DIARIZING}:
            raise InvalidJobRequest(
                "Reviewed-gap materialization requires an aligning or diarizing job."
            )

        evidence_key = _evidence_checkpoint_key(review_key)
        materialization_key = _materialization_checkpoint_key(review_key)
        checkpoints = self.store.list_checkpoints(job_id, stage=CHECKPOINT_STAGE)
        evidence_checkpoint = _checkpoint_by_key(checkpoints, evidence_key)
        materialization_checkpoint = _checkpoint_by_key(
            checkpoints,
            materialization_key,
        )
        if materialization_checkpoint is not None:
            return self._replay_materialized(
                job_id,
                review_key=review_key,
                start_ms=start_ms,
                end_ms=end_ms,
                outcome=outcome,
                evidence_checkpoint=evidence_checkpoint,
                materialization_checkpoint=materialization_checkpoint,
            )
        if job.state is JobState.DIARIZING:
            raise InvalidJobRequest(
                "A new gap review cannot be applied after alignment is finalized."
            )

        alignment_checkpoint, source_duration_ms = self._load_current_alignment(
            job_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        review_payload = _review_payload(
            review_key=review_key,
            start_ms=start_ms,
            end_ms=end_ms,
            outcome=outcome,
            source_duration_ms=source_duration_ms,
            alignment_checkpoint=alignment_checkpoint,
        )
        if evidence_checkpoint is not None:
            if evidence_checkpoint.payload != review_payload:
                raise TranscriptConflict(
                    "The review key is already bound to a different gap decision.",
                    details={"job_id": job_id, "review_key": review_key},
                )
        else:
            evidence_checkpoint, _ = self.store.put_checkpoint(
                job_id,
                stage=CHECKPOINT_STAGE,
                checkpoint_key=evidence_key,
                payload=review_payload,
            )

        segment, created = self.store.commit_reviewed_gap_segment(
            job_id,
            review_key=review_key,
            start_ms=start_ms,
            end_ms=end_ms,
            outcome=outcome,
            review_checkpoint_generation=evidence_checkpoint.generation,
            review_checkpoint_sha256=evidence_checkpoint.payload_sha256,
        )
        materialization_checkpoint, _ = self.store.put_checkpoint(
            job_id,
            stage=CHECKPOINT_STAGE,
            checkpoint_key=materialization_key,
            payload={
                "schema_version": GAP_REVIEW_SCHEMA_VERSION,
                "evidence_type": GAP_REVIEW_EVIDENCE_TYPE,
                "review_key": review_key,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "outcome": outcome.value,
                "segment_id": segment.segment_id,
                "commit_key": segment.commit_key,
                "review_checkpoint_generation": evidence_checkpoint.generation,
                "review_checkpoint_sha256": evidence_checkpoint.payload_sha256,
            },
        )
        alignment = self.finalizer.finalize(job_id)
        return GapReviewResult(
            outcome=GapReviewResultOutcome.MATERIALIZED,
            job=alignment.job,
            segment=segment,
            created=created,
            review_checkpoint_generation=evidence_checkpoint.generation,
            materialization_checkpoint_generation=materialization_checkpoint.generation,
            alignment=alignment,
        )

    def _replay_materialized(
        self,
        job_id: str,
        *,
        review_key: str,
        start_ms: int,
        end_ms: int,
        outcome: TranscriptOutcome,
        evidence_checkpoint: CheckpointRecord | None,
        materialization_checkpoint: CheckpointRecord,
    ) -> GapReviewResult:
        payload = materialization_checkpoint.payload
        if (
            payload.get("schema_version") != GAP_REVIEW_SCHEMA_VERSION
            or payload.get("evidence_type") != GAP_REVIEW_EVIDENCE_TYPE
            or payload.get("review_key") != review_key
            or payload.get("start_ms") != start_ms
            or payload.get("end_ms") != end_ms
            or payload.get("outcome") != outcome.value
            or not isinstance(payload.get("segment_id"), str)
            or payload.get("commit_key") != f"gap_review_{review_key}"
            or evidence_checkpoint is None
            or payload.get("review_checkpoint_generation") != evidence_checkpoint.generation
            or payload.get("review_checkpoint_sha256") != evidence_checkpoint.payload_sha256
        ):
            raise TranscriptConflict(
                "The review key is already bound to a different gap decision.",
                details={"job_id": job_id, "review_key": review_key},
            )
        segment = self._find_segment(job_id, payload["segment_id"])
        if (
            segment.commit_key != payload["commit_key"]
            or segment.start_ms != start_ms
            or segment.end_ms != end_ms
            or segment.outcome is not outcome
            or segment.text is not None
            or segment.timing_status is not TranscriptTimingStatus.ALIGNED
            or segment.speaker_label_status is not SpeakerLabelStatus.UNAVAILABLE
        ):
            raise InvalidJobRequest(
                "The materialized reviewed-gap segment no longer matches its evidence."
            )
        alignment = self.finalizer.finalize(job_id)
        return GapReviewResult(
            outcome=GapReviewResultOutcome.ALREADY_MATERIALIZED,
            job=alignment.job,
            segment=segment,
            created=False,
            review_checkpoint_generation=evidence_checkpoint.generation,
            materialization_checkpoint_generation=materialization_checkpoint.generation,
            alignment=alignment,
        )

    def _load_current_alignment(
        self,
        job_id: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> tuple[CheckpointRecord, int]:
        checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=CHECKPOINT_STAGE),
            ALIGNMENT_CHECKPOINT_KEY,
        )
        if checkpoint is None:
            raise InvalidJobRequest("Gap review requires a durable transcript alignment report.")
        source_duration_ms = self.store.get_job_duration_ms(job_id)
        payload = checkpoint.payload
        ranges = payload.get("unresolved_ranges")
        if (
            payload.get("schema_version") != ALIGNMENT_REPORT_SCHEMA_VERSION
            or not _is_int(payload.get("source_duration_ms"))
            or payload["source_duration_ms"] != source_duration_ms
            or not _is_int(payload.get("unresolved_duration_ms"))
            or not isinstance(ranges, list)
        ):
            raise InvalidJobRequest("The transcript alignment report is invalid.")

        parsed_ranges: list[tuple[int, int]] = []
        cursor = 0
        for value in ranges:
            if (
                not isinstance(value, dict)
                or not _is_int(value.get("start_ms"))
                or not _is_int(value.get("end_ms"))
            ):
                raise InvalidJobRequest(
                    "The transcript alignment report contains an invalid range."
                )
            current_start = value["start_ms"]
            current_end = value["end_ms"]
            if (
                current_start < cursor
                or current_start < 0
                or current_end <= current_start
                or current_end > source_duration_ms
            ):
                raise InvalidJobRequest(
                    "The transcript alignment report contains an invalid range."
                )
            parsed_ranges.append((current_start, current_end))
            cursor = current_end
        if (
            sum(current_end - current_start for current_start, current_end in parsed_ranges)
            != payload["unresolved_duration_ms"]
        ):
            raise InvalidJobRequest("The transcript alignment report is invalid.")
        if parsed_ranges.count((start_ms, end_ms)) != 1:
            raise InvalidJobRequest("A review must match one complete current unresolved range.")
        return checkpoint, source_duration_ms

    def _find_segment(self, job_id: str, segment_id: str) -> TranscriptSegment:
        after_sequence = 0
        while True:
            snapshot = self.store.get_job_snapshot(
                job_id,
                after_segment_sequence=after_sequence,
                segment_limit=500,
            )
            for segment in snapshot.stable_segments:
                if segment.segment_id == segment_id:
                    return segment
            if not snapshot.has_more_segments:
                raise InvalidJobRequest("The materialized reviewed-gap segment is missing.")
            after_sequence = snapshot.next_after_segment_sequence


def _review_payload(
    *,
    review_key: str,
    start_ms: int,
    end_ms: int,
    outcome: TranscriptOutcome,
    source_duration_ms: int,
    alignment_checkpoint: CheckpointRecord,
) -> dict[str, Any]:
    return {
        "schema_version": GAP_REVIEW_SCHEMA_VERSION,
        "evidence_type": GAP_REVIEW_EVIDENCE_TYPE,
        "review_key": review_key,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "outcome": outcome.value,
        "reason_code": (
            "HUMAN_CONFIRMED_NON_SPEECH"
            if outcome is TranscriptOutcome.NON_SPEECH
            else "HUMAN_CONFIRMED_INAUDIBLE"
        ),
        "source_duration_ms": source_duration_ms,
        "alignment_report_schema_version": ALIGNMENT_REPORT_SCHEMA_VERSION,
        "alignment_report_generation": alignment_checkpoint.generation,
        "alignment_report_sha256": alignment_checkpoint.payload_sha256,
    }


def _validate_request(
    *,
    review_key: str,
    start_ms: int,
    end_ms: int,
    outcome: TranscriptOutcome,
) -> None:
    if (
        not isinstance(review_key, str)
        or len(review_key) > 80
        or not SAFE_IDENTIFIER_PATTERN.fullmatch(review_key)
    ):
        raise InvalidJobRequest("review_key contains unsupported characters.")
    if not _is_int(start_ms) or not _is_int(end_ms) or end_ms <= start_ms:
        raise InvalidJobRequest("The reviewed gap range is invalid.")
    if not isinstance(outcome, TranscriptOutcome) or outcome not in {
        TranscriptOutcome.NON_SPEECH,
        TranscriptOutcome.INAUDIBLE,
    }:
        raise InvalidJobRequest("A reviewed gap outcome must be non_speech or inaudible.")


def _checkpoint_by_key(
    checkpoints: list[CheckpointRecord],
    checkpoint_key: str,
) -> CheckpointRecord | None:
    return next(
        (checkpoint for checkpoint in checkpoints if checkpoint.checkpoint_key == checkpoint_key),
        None,
    )


def _evidence_checkpoint_key(review_key: str) -> str:
    return f"gap_review_{review_key}_evidence"


def _materialization_checkpoint_key(review_key: str) -> str:
    return f"gap_review_{review_key}_materialized"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
