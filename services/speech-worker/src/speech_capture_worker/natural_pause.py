"""Conservative automatic materialization of ordinary recording pauses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

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
from speech_capture_worker.domain import CheckpointRecord, JobRecord, JobState
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.gap_retranscription import (
    BOUNDARY_FRAGMENT_REJECTION_PREFIX,
)
from speech_capture_worker.gap_speech_activity import (
    SPEECH_ACTIVITY_CHECKPOINT_KEY,
    SpeechActivityObservation,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.transcript import TranscriptOutcome, TranscriptSegment

NATURAL_PAUSE_SCHEMA_VERSION = "1.0.0"
NATURAL_PAUSE_EVIDENCE_TYPE = "combined_natural_pause"
MAX_BOUNDARY_SPEECH_DURATION_MS = 350
MAX_BOUNDARY_SPEECH_RATIO = 0.25
MAX_BOUNDARY_DISTANCE_MS = 350
FAILED_RETRANSCRIPTION_PREFIX = "gap_retranscription_failed_"


class NaturalPauseOutcome(StrEnum):
    MATERIALIZED = "materialized"
    NO_SAFE_PAUSES = "no_safe_pauses"
    ALREADY_FINALIZED = "already_finalized"


@dataclass(frozen=True)
class NaturalPauseResult:
    outcome: NaturalPauseOutcome
    job: JobRecord
    created_segment_count: int
    alignment: AlignmentFinalizationResult


class NaturalPauseMaterializer:
    """Use aligned transcript, VAD, and ASR rejection evidence together."""

    def __init__(
        self,
        store: JobStore,
        *,
        finalizer: TranscriptAlignmentFinalizer | None = None,
    ) -> None:
        self.store = store
        self.finalizer = finalizer or TranscriptAlignmentFinalizer(store)

    def materialize(self, job_id: str) -> NaturalPauseResult:
        job = self.store.get_job(job_id)
        if job.state is JobState.DIARIZING:
            alignment = self.finalizer.finalize(job_id)
            return NaturalPauseResult(
                outcome=NaturalPauseOutcome.ALREADY_FINALIZED,
                job=alignment.job,
                created_segment_count=0,
                alignment=alignment,
            )
        if job.state is not JobState.ALIGNING:
            raise InvalidJobRequest(
                "Natural-pause materialization requires an aligning or diarizing job."
            )

        checkpoints = self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE)
        alignment_checkpoint = _checkpoint_by_key(
            checkpoints,
            ALIGNMENT_CHECKPOINT_KEY,
        )
        speech_checkpoint = _checkpoint_by_key(
            checkpoints,
            SPEECH_ACTIVITY_CHECKPOINT_KEY,
        )
        if alignment_checkpoint is None or speech_checkpoint is None:
            raise InvalidJobRequest(
                "Natural-pause materialization requires alignment and VAD evidence."
            )
        speech_payload = speech_checkpoint.payload
        if (
            speech_payload.get("alignment_report_generation")
            != alignment_checkpoint.generation
            or speech_payload.get("alignment_report_sha256")
            != alignment_checkpoint.payload_sha256
            or not isinstance(speech_payload.get("evidence"), list)
        ):
            raise InvalidJobRequest(
                "Speech-activity evidence is stale relative to alignment."
            )

        unresolved = _unresolved_ranges(alignment_checkpoint.payload)
        segments = _all_segments(self.store, job_id)
        candidates: list[tuple[dict[str, Any], str, CheckpointRecord | None]] = []
        for evidence in speech_payload["evidence"]:
            if not isinstance(evidence, dict):
                continue
            range_value = (evidence.get("start_ms"), evidence.get("end_ms"))
            if range_value not in unresolved:
                continue
            if _is_vad_no_speech(evidence):
                candidates.append((evidence, "VAD_CONFIRMED_NO_SPEECH", None))
                continue
            if _boundary_residual_side(evidence, segments=segments) is None:
                continue
            retranscription = _retranscription_checkpoint(
                checkpoints,
                start_ms=range_value[0],
                end_ms=range_value[1],
            )
            if retranscription is not None:
                candidates.append(
                    (
                        evidence,
                        "VAD_BOUNDARY_RESIDUAL_AND_ASR_REJECTED",
                        retranscription,
                    )
                )

        created_count = 0
        for evidence, reason_code, retranscription in candidates:
            start_ms = evidence["start_ms"]
            end_ms = evidence["end_ms"]
            commit_key = f"natural_pause_{start_ms:010d}_{end_ms:010d}"
            payload: dict[str, Any] = {
                "schema_version": NATURAL_PAUSE_SCHEMA_VERSION,
                "evidence_type": NATURAL_PAUSE_EVIDENCE_TYPE,
                "commit_key": commit_key,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "outcome": TranscriptOutcome.NON_SPEECH.value,
                "reason_code": reason_code,
                "source_duration_ms": self.store.get_job_duration_ms(job_id),
                "alignment_report_generation": alignment_checkpoint.generation,
                "alignment_report_sha256": alignment_checkpoint.payload_sha256,
                "speech_activity_generation": speech_checkpoint.generation,
                "speech_activity_sha256": speech_checkpoint.payload_sha256,
                "speech_evidence": evidence,
                "retranscription_checkpoint_key": (
                    retranscription.checkpoint_key if retranscription is not None else None
                ),
            }
            if retranscription is not None:
                payload.update(
                    {
                        "retranscription_checkpoint_generation": retranscription.generation,
                        "retranscription_checkpoint_sha256": retranscription.payload_sha256,
                    }
                )
            evidence_checkpoint, _ = self.store.put_checkpoint(
                job_id,
                stage=ALIGNMENT_STAGE,
                checkpoint_key=f"{commit_key}_evidence",
                payload=payload,
            )
            _segment, created = self.store.commit_natural_pause_segment(
                job_id,
                commit_key=commit_key,
                start_ms=start_ms,
                end_ms=end_ms,
                evidence_checkpoint_generation=evidence_checkpoint.generation,
                evidence_checkpoint_sha256=evidence_checkpoint.payload_sha256,
            )
            created_count += int(created)

        alignment = self.finalizer.finalize(job_id)
        return NaturalPauseResult(
            outcome=(
                NaturalPauseOutcome.MATERIALIZED
                if candidates
                else NaturalPauseOutcome.NO_SAFE_PAUSES
            ),
            job=alignment.job,
            created_segment_count=created_count,
            alignment=alignment,
        )


def _unresolved_ranges(payload: dict[str, Any]) -> set[tuple[int, int]]:
    values = payload.get("unresolved_ranges")
    if not isinstance(values, list):
        raise InvalidJobRequest("The transcript alignment report is invalid.")
    ranges: set[tuple[int, int]] = set()
    for value in values:
        if (
            not isinstance(value, dict)
            or not _is_int(value.get("start_ms"))
            or not _is_int(value.get("end_ms"))
            or value["end_ms"] <= value["start_ms"]
        ):
            raise InvalidJobRequest("The transcript alignment report is invalid.")
        ranges.add((value["start_ms"], value["end_ms"]))
    if len(ranges) != len(values):
        raise InvalidJobRequest("The transcript alignment report has duplicate ranges.")
    return ranges


def _is_vad_no_speech(evidence: dict[str, Any]) -> bool:
    return (
        evidence.get("observation")
        == SpeechActivityObservation.NO_SPEECH_DETECTED.value
        and evidence.get("speech_duration_ms") == 0
        and evidence.get("speech_ratio") == 0
        and evidence.get("speech_regions") == []
    )


def _boundary_residual_side(
    evidence: dict[str, Any],
    *,
    segments: list[TranscriptSegment],
) -> str | None:
    start_ms = evidence.get("start_ms")
    end_ms = evidence.get("end_ms")
    speech_duration_ms = evidence.get("speech_duration_ms")
    speech_ratio = evidence.get("speech_ratio")
    regions = evidence.get("speech_regions")
    if (
        evidence.get("observation")
        != SpeechActivityObservation.SPEECH_DETECTED.value
        or not _is_int(start_ms)
        or not _is_int(end_ms)
        or not _is_int(speech_duration_ms)
        or speech_duration_ms <= 0
        or speech_duration_ms > MAX_BOUNDARY_SPEECH_DURATION_MS
        or not isinstance(speech_ratio, (int, float))
        or isinstance(speech_ratio, bool)
        or speech_ratio <= 0
        or speech_ratio > MAX_BOUNDARY_SPEECH_RATIO
        or not isinstance(regions, list)
        or not regions
    ):
        return None
    if any(
        not isinstance(region, dict)
        or not _is_int(region.get("start_ms"))
        or not _is_int(region.get("end_ms"))
        or region["start_ms"] < start_ms
        or region["end_ms"] <= region["start_ms"]
        or region["end_ms"] > end_ms
        for region in regions
    ):
        return None
    previous_touches = any(
        segment.outcome is TranscriptOutcome.TRANSCRIBED and segment.end_ms == start_ms
        for segment in segments
    )
    next_touches = any(
        segment.outcome is TranscriptOutcome.TRANSCRIBED and segment.start_ms == end_ms
        for segment in segments
    )
    if previous_touches and all(
        region["end_ms"] <= start_ms + MAX_BOUNDARY_DISTANCE_MS for region in regions
    ):
        return "start"
    if next_touches and all(
        region["start_ms"] >= end_ms - MAX_BOUNDARY_DISTANCE_MS for region in regions
    ):
        return "end"
    return None


def _retranscription_checkpoint(
    checkpoints: list[CheckpointRecord],
    *,
    start_ms: int,
    end_ms: int,
) -> CheckpointRecord | None:
    suffix = f"{start_ms:010d}_{end_ms:010d}"
    for prefix in (BOUNDARY_FRAGMENT_REJECTION_PREFIX, FAILED_RETRANSCRIPTION_PREFIX):
        checkpoint = _checkpoint_by_key(checkpoints, f"{prefix}{suffix}")
        if checkpoint is not None:
            return checkpoint
    return None


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
        (value for value in checkpoints if value.checkpoint_key == checkpoint_key),
        None,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
