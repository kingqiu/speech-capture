"""Bound non-convergent automatic gap repair without discarding evidence."""

from __future__ import annotations

from dataclasses import dataclass
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
from speech_capture_worker.errors import InvalidJobRequest, TranscriptConflict
from speech_capture_worker.gap_speech_activity import (
    SPEECH_ACTIVITY_CHECKPOINT_KEY,
    SpeechActivityObservation,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.transcript import (
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
)

BOUNDED_GAP_SCHEMA_VERSION = "1.0.0"
BOUNDED_GAP_EVIDENCE_TYPE = "bounded_automatic_gap_resolution"
MAX_AUTOMATED_ALIGNMENT_GENERATIONS = 3


@dataclass(frozen=True)
class BoundedGapResolutionResult:
    job: JobRecord
    non_speech_count: int
    inaudible_count: int
    created_segment_count: int
    alignment: AlignmentFinalizationResult


class BoundedGapMaterializer:
    """Close current gaps after repeated repair stops improving the timeline."""

    def __init__(
        self,
        store: JobStore,
        *,
        finalizer: TranscriptAlignmentFinalizer | None = None,
    ) -> None:
        self.store = store
        self.finalizer = finalizer or TranscriptAlignmentFinalizer(store)

    def materialize(self, job_id: str) -> BoundedGapResolutionResult:
        job = self.store.get_job(job_id)
        if job.state is not JobState.ALIGNING:
            raise InvalidJobRequest("Bounded gap resolution requires an aligning job.")

        checkpoints = self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE)
        alignment_checkpoint = _checkpoint_by_key(
            checkpoints,
            ALIGNMENT_CHECKPOINT_KEY,
        )
        speech_checkpoint = _checkpoint_by_key(
            checkpoints,
            SPEECH_ACTIVITY_CHECKPOINT_KEY,
        )
        if alignment_checkpoint is None:
            raise InvalidJobRequest("Bounded gap resolution requires alignment evidence.")
        if alignment_checkpoint.generation < MAX_AUTOMATED_ALIGNMENT_GENERATIONS:
            raise InvalidJobRequest(
                "Automatic gap repair has not reached its bounded generation limit."
            )

        unresolved = _unresolved_ranges(alignment_checkpoint.payload)
        speech_evidence = (
            _speech_evidence_by_range(speech_checkpoint.payload)
            if speech_checkpoint is not None
            else {}
        )
        speech_alignment_matches = bool(
            speech_checkpoint is not None
            and speech_checkpoint.payload.get("alignment_report_generation")
            == alignment_checkpoint.generation
            and speech_checkpoint.payload.get("alignment_report_sha256")
            == alignment_checkpoint.payload_sha256
        )

        non_speech_count = 0
        inaudible_count = 0
        created_count = 0
        for start_ms, end_ms in unresolved:
            observation = speech_evidence.get((start_ms, end_ms))
            outcome, reason_code = _bounded_outcome(observation)
            range_key = f"{start_ms:010d}_{end_ms:010d}"
            evidence_payload = {
                "schema_version": BOUNDED_GAP_SCHEMA_VERSION,
                "evidence_type": BOUNDED_GAP_EVIDENCE_TYPE,
                "range_key": range_key,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "outcome": outcome.value,
                "reason_code": reason_code,
                "alignment_report_generation": alignment_checkpoint.generation,
                "alignment_report_sha256": alignment_checkpoint.payload_sha256,
                "speech_activity_generation": (
                    speech_checkpoint.generation
                    if speech_checkpoint is not None
                    else None
                ),
                "speech_activity_sha256": (
                    speech_checkpoint.payload_sha256
                    if speech_checkpoint is not None
                    else None
                ),
                "speech_activity_alignment_matches_current": (
                    speech_alignment_matches
                ),
                "speech_evidence_exact_range_match": observation is not None,
                "speech_evidence": observation,
            }
            evidence_checkpoint, _ = self.store.put_checkpoint(
                job_id,
                stage=ALIGNMENT_STAGE,
                checkpoint_key=f"bounded_gap_{range_key}_evidence",
                payload=evidence_payload,
            )
            segment, created = self.store.commit_transcript_segment(
                job_id,
                commit_key=f"bounded_gap_{range_key}",
                start_ms=start_ms,
                end_ms=end_ms,
                outcome=outcome,
                timing_status=TranscriptTimingStatus.ALIGNED,
                speaker_label_status=SpeakerLabelStatus.UNAVAILABLE,
                allow_aligning=True,
            )
            materialization_payload = {
                **evidence_payload,
                "segment_id": segment.segment_id,
                "commit_key": segment.commit_key,
                "evidence_checkpoint_generation": evidence_checkpoint.generation,
                "evidence_checkpoint_sha256": evidence_checkpoint.payload_sha256,
            }
            materialization_checkpoint, _ = self.store.put_checkpoint(
                job_id,
                stage=ALIGNMENT_STAGE,
                checkpoint_key=f"bounded_gap_{range_key}_materialized",
                payload=materialization_payload,
            )
            if (
                materialization_checkpoint.payload.get("segment_id")
                != segment.segment_id
            ):
                raise TranscriptConflict(
                    "Bounded gap materialization no longer matches its segment."
                )
            created_count += int(created)
            non_speech_count += int(outcome is TranscriptOutcome.NON_SPEECH)
            inaudible_count += int(outcome is TranscriptOutcome.INAUDIBLE)

        alignment = self.finalizer.finalize(job_id)
        return BoundedGapResolutionResult(
            job=alignment.job,
            non_speech_count=non_speech_count,
            inaudible_count=inaudible_count,
            created_segment_count=created_count,
            alignment=alignment,
        )


def _unresolved_ranges(payload: dict[str, Any]) -> list[tuple[int, int]]:
    raw_ranges = payload.get("unresolved_ranges")
    if not isinstance(raw_ranges, list):
        raise InvalidJobRequest("The current alignment report is invalid.")
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for raw in raw_ranges:
        if (
            not isinstance(raw, dict)
            or not _strict_int(raw.get("start_ms"))
            or not _strict_int(raw.get("end_ms"))
            or raw["start_ms"] < cursor
            or raw["end_ms"] <= raw["start_ms"]
        ):
            raise InvalidJobRequest("The current alignment ranges are invalid.")
        ranges.append((raw["start_ms"], raw["end_ms"]))
        cursor = raw["end_ms"]
    return ranges


def _speech_evidence_by_range(
    payload: dict[str, Any],
) -> dict[tuple[int, int], dict[str, Any]]:
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        raise InvalidJobRequest("The speech-activity evidence is invalid.")
    evidence: dict[tuple[int, int], dict[str, Any]] = {}
    for item in raw_evidence:
        if (
            not isinstance(item, dict)
            or not _strict_int(item.get("start_ms"))
            or not _strict_int(item.get("end_ms"))
            or item["end_ms"] <= item["start_ms"]
        ):
            raise InvalidJobRequest("A speech-activity observation is invalid.")
        key = (item["start_ms"], item["end_ms"])
        if key in evidence:
            raise InvalidJobRequest("Speech-activity evidence contains duplicate ranges.")
        evidence[key] = item
    return evidence


def _bounded_outcome(
    evidence: dict[str, Any] | None,
) -> tuple[TranscriptOutcome, str]:
    if evidence is None:
        return (
            TranscriptOutcome.INAUDIBLE,
            "AUTOMATED_GAP_REPAIR_EXHAUSTED_WITHOUT_EXACT_VAD_RANGE",
        )
    observation = evidence.get("observation")
    if observation == SpeechActivityObservation.NO_SPEECH_DETECTED.value:
        if (
            evidence.get("speech_duration_ms") != 0
            or evidence.get("speech_regions") != []
        ):
            return (
                TranscriptOutcome.INAUDIBLE,
                "AUTOMATED_GAP_REPAIR_EXHAUSTED_WITH_CONTRADICTORY_VAD",
            )
        return TranscriptOutcome.NON_SPEECH, "VAD_CONFIRMED_NO_SPEECH"
    if observation == SpeechActivityObservation.SPEECH_DETECTED.value:
        if not _strict_int(evidence.get("speech_duration_ms"), minimum=1):
            return (
                TranscriptOutcome.INAUDIBLE,
                "AUTOMATED_GAP_REPAIR_EXHAUSTED_WITH_CONTRADICTORY_VAD",
            )
        return (
            TranscriptOutcome.INAUDIBLE,
            "AUTOMATED_GAP_REPAIR_EXHAUSTED_WITH_SPEECH",
        )
    return (
        TranscriptOutcome.INAUDIBLE,
        "AUTOMATED_GAP_REPAIR_EXHAUSTED_WITH_UNSUPPORTED_VAD",
    )


def _checkpoint_by_key(
    checkpoints: list[CheckpointRecord],
    key: str,
) -> CheckpointRecord | None:
    return next(
        (checkpoint for checkpoint in checkpoints if checkpoint.checkpoint_key == key),
        None,
    )


def _strict_int(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum
