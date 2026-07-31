"""Durable whole-transcript alignment and timeline finalization."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from speech_capture_worker.asr_domain import AsrAttemptRecord, AsrAttemptState
from speech_capture_worker.audio_preprocessing import (
    AudioChunkPlan,
    AudioPreprocessor,
    NormalizedAudioPlan,
)
from speech_capture_worker.domain import SHA256_PATTERN, JobRecord, JobState
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.transcript import (
    DiarizationStatus,
    TranscriptOutcome,
    TranscriptSegment,
    TranscriptTimingStatus,
)

ALIGNMENT_REPORT_SCHEMA_VERSION = "1.0.0"
CHECKPOINT_STAGE = "aligning"
CHECKPOINT_KEY = "transcript_alignment_report"
TRANSCRIPT_PAGE_SIZE = 500


class AlignmentFinalizationOutcome(StrEnum):
    READY_FOR_DIARIZATION = "ready_for_diarization"
    ALREADY_FINALIZED = "already_finalized"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    ALIGNMENT_INCOMPLETE = "alignment_incomplete"
    TIMELINE_INCOMPLETE = "timeline_incomplete"
    TRANSCRIPT_PARTIAL = "transcript_partial"


@dataclass(frozen=True)
class TimelineRange:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class AlignmentIssue:
    code: str
    message: str
    start_ms: int | None = None
    end_ms: int | None = None
    chunk_index: int | None = None
    segment_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AlignmentReport:
    schema_version: str
    evidence_complete: bool
    alignment_complete: bool
    timeline_accounted: bool
    transcript_complete: bool
    ready_for_diarization: bool
    source_duration_ms: int
    normalized_duration_ms: int
    normalized_total_frames: int
    planned_chunk_count: int
    materialized_chunk_count: int
    segment_count: int
    aligned_transcribed_segment_count: int
    accounted_duration_ms: int
    unresolved_duration_ms: int
    outcome_counts: dict[str, int]
    outcome_durations_ms: dict[str, int]
    unresolved_ranges: tuple[TimelineRange, ...]
    issues: tuple[AlignmentIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_complete": self.evidence_complete,
            "alignment_complete": self.alignment_complete,
            "timeline_accounted": self.timeline_accounted,
            "transcript_complete": self.transcript_complete,
            "ready_for_diarization": self.ready_for_diarization,
            "source_duration_ms": self.source_duration_ms,
            "normalized_duration_ms": self.normalized_duration_ms,
            "normalized_total_frames": self.normalized_total_frames,
            "planned_chunk_count": self.planned_chunk_count,
            "materialized_chunk_count": self.materialized_chunk_count,
            "segment_count": self.segment_count,
            "aligned_transcribed_segment_count": (self.aligned_transcribed_segment_count),
            "accounted_duration_ms": self.accounted_duration_ms,
            "unresolved_duration_ms": self.unresolved_duration_ms,
            "outcome_counts": dict(self.outcome_counts),
            "outcome_durations_ms": dict(self.outcome_durations_ms),
            "unresolved_ranges": [
                timeline_range.to_dict() for timeline_range in self.unresolved_ranges
            ],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class AlignmentFinalizationResult:
    outcome: AlignmentFinalizationOutcome
    job: JobRecord
    report: AlignmentReport
    checkpoint_generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict(),
            "report": self.report.to_dict(),
            "checkpoint_generation": self.checkpoint_generation,
        }


class TranscriptAlignmentFinalizer:
    """Prove alignment and timeline readiness before speaker diarization."""

    def __init__(
        self,
        store: JobStore,
        *,
        preprocessor: AudioPreprocessor | None = None,
    ) -> None:
        self.store = store
        self.preprocessor = preprocessor or AudioPreprocessor(store)

    def finalize(self, job_id: str) -> AlignmentFinalizationResult:
        job = self.store.get_job(job_id)
        if job.state not in {JobState.ALIGNING, JobState.DIARIZING}:
            raise InvalidJobRequest("Alignment finalization requires an aligning or diarizing job.")

        plan = self.preprocessor.get_plan(job_id)
        segments = self._list_all_segments(job_id)
        report = self._build_report(
            job,
            plan=plan,
            segments=segments,
        )
        checkpoint, _ = self.store.put_checkpoint(
            job_id,
            stage=CHECKPOINT_STAGE,
            checkpoint_key=CHECKPOINT_KEY,
            payload=report.to_dict(),
        )

        if job.state is JobState.DIARIZING:
            if not report.ready_for_diarization:
                raise InvalidJobRequest(
                    "A diarizing job no longer satisfies its alignment evidence gate."
                )
            return AlignmentFinalizationResult(
                outcome=AlignmentFinalizationOutcome.ALREADY_FINALIZED,
                job=job,
                report=report,
                checkpoint_generation=checkpoint.generation,
            )

        elapsed_seconds = self._elapsed_seconds(job_id)
        alignment_progress = (
            report.aligned_transcribed_segment_count
            / report.outcome_counts[TranscriptOutcome.TRANSCRIBED.value]
            if report.outcome_counts[TranscriptOutcome.TRANSCRIBED.value]
            else 1.0
        )
        self.store.put_job_progress(
            job_id,
            processed_ms=report.source_duration_ms,
            stage_progress=alignment_progress,
            elapsed_seconds=elapsed_seconds,
            diarization_status=DiarizationStatus.NOT_STARTED,
        )

        outcome = _report_outcome(report)
        if not report.ready_for_diarization:
            return AlignmentFinalizationResult(
                outcome=outcome,
                job=self.store.get_job(job_id),
                report=report,
                checkpoint_generation=checkpoint.generation,
            )

        current = self.store.get_job(job_id)
        diarizing = self.store.transition_job(
            job_id,
            JobState.DIARIZING,
            expected_revision=current.revision,
            reason_code="alignment_and_timeline_verified",
            event_type="job.diarization_started",
        )
        self.store.put_job_progress(
            job_id,
            processed_ms=report.source_duration_ms,
            stage_progress=0,
            elapsed_seconds=elapsed_seconds,
            diarization_status=DiarizationStatus.NOT_STARTED,
        )
        return AlignmentFinalizationResult(
            outcome=AlignmentFinalizationOutcome.READY_FOR_DIARIZATION,
            job=diarizing,
            report=report,
            checkpoint_generation=checkpoint.generation,
        )

    def _build_report(
        self,
        job: JobRecord,
        *,
        plan: NormalizedAudioPlan,
        segments: list[TranscriptSegment],
    ) -> AlignmentReport:
        source_duration_ms = self.store.get_job_duration_ms(job.job_id)
        evidence_issues, materialized_chunk_count = self._evidence_issues(
            job.job_id,
            plan=plan,
            segments=segments,
        )
        (
            transcript_issues,
            unresolved_ranges,
            accounted_duration_ms,
            outcome_counts,
            outcome_durations_ms,
            aligned_transcribed_segment_count,
        ) = _evaluate_transcript_timeline(
            segments,
            source_duration_ms=source_duration_ms,
        )
        issues = (*evidence_issues, *transcript_issues)
        evidence_complete = not evidence_issues
        alignment_complete = not any(
            issue.code == "UNALIGNED_TRANSCRIBED_SEGMENT" for issue in transcript_issues
        )
        timeline_accounted = not any(
            issue.code
            in {
                "UNCOVERED_TRANSCRIPT_RANGE",
                "OVERLAPPING_TRANSCRIPT_RANGE",
                "OUT_OF_BOUNDS_TRANSCRIPT_RANGE",
            }
            for issue in transcript_issues
        )
        transcript_complete = timeline_accounted and not any(
            issue.code in {"INAUDIBLE_TRANSCRIPT_RANGE", "FAILED_TRANSCRIPT_RANGE"}
            for issue in transcript_issues
        )
        ready_for_diarization = (
            evidence_complete and alignment_complete and timeline_accounted and transcript_complete
        )
        return AlignmentReport(
            schema_version=ALIGNMENT_REPORT_SCHEMA_VERSION,
            evidence_complete=evidence_complete,
            alignment_complete=alignment_complete,
            timeline_accounted=timeline_accounted,
            transcript_complete=transcript_complete,
            ready_for_diarization=ready_for_diarization,
            source_duration_ms=source_duration_ms,
            normalized_duration_ms=plan.duration_ms,
            normalized_total_frames=plan.total_frames,
            planned_chunk_count=len(plan.chunks),
            materialized_chunk_count=materialized_chunk_count,
            segment_count=len(segments),
            aligned_transcribed_segment_count=aligned_transcribed_segment_count,
            accounted_duration_ms=accounted_duration_ms,
            unresolved_duration_ms=sum(
                timeline_range.duration_ms for timeline_range in unresolved_ranges
            ),
            outcome_counts=outcome_counts,
            outcome_durations_ms=outcome_durations_ms,
            unresolved_ranges=unresolved_ranges,
            issues=issues,
        )

    def _evidence_issues(
        self,
        job_id: str,
        *,
        plan: NormalizedAudioPlan,
        segments: list[TranscriptSegment],
    ) -> tuple[tuple[AlignmentIssue, ...], int]:
        issues: list[AlignmentIssue] = []
        segments_by_id = {segment.segment_id: segment for segment in segments}
        attempts = {
            (attempt.chunk_index, attempt.attempt_number): attempt
            for attempt in self.store.list_asr_attempts(job_id)
        }
        materialized = _materialized_chunk_checkpoints(self.store, job_id)
        forced_alignment = _forced_alignment_checkpoints(self.store, job_id)
        materialized_chunk_count = 0

        for chunk in plan.chunks:
            checkpoint_payload = materialized.get(chunk.chunk_index)
            if checkpoint_payload is None:
                issues.append(
                    AlignmentIssue(
                        code="MISSING_MATERIALIZED_CHUNK",
                        message="A planned audio chunk has no durable materialization record.",
                        start_ms=chunk.start_ms,
                        end_ms=chunk.end_ms,
                        chunk_index=chunk.chunk_index,
                    )
                )
                continue
            try:
                attempt_number = int(checkpoint_payload["attempt_number"])
                raw_sha256 = str(checkpoint_payload["raw_sha256"])
                checkpoint_start_ms = int(checkpoint_payload["start_ms"])
                checkpoint_end_ms = int(checkpoint_payload["end_ms"])
                raw_segment_ids = checkpoint_payload["segment_ids"]
                if not isinstance(raw_segment_ids, list) or any(
                    not isinstance(value, str) or not value for value in raw_segment_ids
                ):
                    raise TypeError
                checkpoint_segment_ids = tuple(raw_segment_ids)
            except (KeyError, TypeError, ValueError):
                issues.append(
                    AlignmentIssue(
                        code="INVALID_MATERIALIZED_CHUNK",
                        message="A materialized chunk checkpoint is invalid.",
                        chunk_index=chunk.chunk_index,
                    )
                )
                continue
            attempt = attempts.get((chunk.chunk_index, attempt_number))
            if (
                attempt is None
                or attempt.state is not AsrAttemptState.SUCCEEDED
                or attempt.raw_sha256 != raw_sha256
            ):
                issues.append(
                    AlignmentIssue(
                        code="MISSING_SUCCEEDED_ASR_ATTEMPT",
                        message=(
                            "A materialized chunk does not reference matching "
                            "successful raw evidence."
                        ),
                        chunk_index=chunk.chunk_index,
                    )
                )
                continue
            if not _attempt_matches_chunk(
                attempt,
                chunk,
                checkpoint_start_ms=checkpoint_start_ms,
                checkpoint_end_ms=checkpoint_end_ms,
            ):
                issues.append(
                    AlignmentIssue(
                        code="ASR_ATTEMPT_RANGE_MISMATCH",
                        message=(
                            "A materialized raw attempt does not match its normalized-audio chunk."
                        ),
                        chunk_index=chunk.chunk_index,
                    )
                )
                continue
            missing_segment_ids = sorted(
                segment_id
                for segment_id in checkpoint_segment_ids
                if segment_id not in segments_by_id
            )
            if missing_segment_ids:
                issues.append(
                    AlignmentIssue(
                        code="MISSING_MATERIALIZED_SEGMENT",
                        message=(
                            "A materialized raw attempt references a missing "
                            "stable transcript segment."
                        ),
                        chunk_index=chunk.chunk_index,
                        segment_id=missing_segment_ids[0],
                    )
                )
                continue
            invalid_segment_ids = sorted(
                segment_id
                for segment_id in checkpoint_segment_ids
                if not _segment_matches_chunk(
                    segments_by_id[segment_id],
                    chunk,
                )
            )
            if invalid_segment_ids:
                issues.append(
                    AlignmentIssue(
                        code="MATERIALIZED_SEGMENT_RANGE_MISMATCH",
                        message=(
                            "A materialized stable segment is not a transcribed "
                            "outcome inside its normalized-audio chunk."
                        ),
                        chunk_index=chunk.chunk_index,
                        segment_id=invalid_segment_ids[0],
                    )
                )
                continue
            raw_payload = self.store.get_asr_attempt_payload(
                job_id,
                chunk_index=chunk.chunk_index,
                attempt_number=attempt_number,
            )
            raw_timestamp_segments = raw_payload.get("segments")
            if not raw_timestamp_segments:
                for segment_id in checkpoint_segment_ids:
                    segment = segments_by_id[segment_id]
                    if (
                        segment.timing_status is TranscriptTimingStatus.ALIGNED
                        and not _forced_alignment_evidence_valid(
                            self.store,
                            job_id,
                            segment=segment,
                            plan=plan,
                            payload=forced_alignment.get(segment_id),
                        )
                    ):
                        issues.append(
                            AlignmentIssue(
                                code="MISSING_FORCED_ALIGNMENT_EVIDENCE",
                                message=(
                                    "An aligned fallback segment lacks matching "
                                    "private forced-alignment evidence."
                                ),
                                start_ms=segment.start_ms,
                                end_ms=segment.end_ms,
                                chunk_index=chunk.chunk_index,
                                segment_id=segment.segment_id,
                            )
                        )
            materialized_chunk_count += 1
        return tuple(issues), materialized_chunk_count

    def _list_all_segments(self, job_id: str) -> list[TranscriptSegment]:
        segments: list[TranscriptSegment] = []
        after_sequence = 0
        while True:
            snapshot = self.store.get_job_snapshot(
                job_id,
                after_segment_sequence=after_sequence,
                segment_limit=TRANSCRIPT_PAGE_SIZE,
            )
            segments.extend(snapshot.stable_segments)
            if not snapshot.has_more_segments:
                return segments
            if snapshot.next_after_segment_sequence <= after_sequence:
                raise InvalidJobRequest("Transcript pagination did not advance during alignment.")
            after_sequence = snapshot.next_after_segment_sequence

    def _elapsed_seconds(self, job_id: str) -> float:
        progress = self.store.get_job_snapshot(job_id, segment_limit=1).progress
        return progress.elapsed_seconds if progress is not None else 0


def _evaluate_transcript_timeline(
    segments: list[TranscriptSegment],
    *,
    source_duration_ms: int,
) -> tuple[
    tuple[AlignmentIssue, ...],
    tuple[TimelineRange, ...],
    int,
    dict[str, int],
    dict[str, int],
    int,
]:
    issues: list[AlignmentIssue] = []
    unresolved_ranges: list[TimelineRange] = []
    outcome_counts = {outcome.value: 0 for outcome in TranscriptOutcome}
    outcome_durations_ms = {outcome.value: 0 for outcome in TranscriptOutcome}
    aligned_transcribed_segment_count = 0
    accounted_duration_ms = 0
    cursor = 0

    for segment in sorted(
        segments,
        key=lambda value: (value.start_ms, value.end_ms, value.segment_sequence),
    ):
        outcome_counts[segment.outcome.value] += 1
        outcome_durations_ms[segment.outcome.value] += segment.end_ms - segment.start_ms
        if segment.start_ms > cursor:
            unresolved = TimelineRange(cursor, segment.start_ms)
            unresolved_ranges.append(unresolved)
            issues.append(
                AlignmentIssue(
                    code="UNCOVERED_TRANSCRIPT_RANGE",
                    message="A source range has no stable transcript outcome.",
                    start_ms=unresolved.start_ms,
                    end_ms=unresolved.end_ms,
                )
            )
        elif segment.start_ms < cursor:
            issues.append(
                AlignmentIssue(
                    code="OVERLAPPING_TRANSCRIPT_RANGE",
                    message="Stable transcript outcomes overlap on the source timeline.",
                    start_ms=segment.start_ms,
                    end_ms=min(cursor, segment.end_ms),
                    segment_id=segment.segment_id,
                )
            )
        if segment.start_ms < 0 or segment.end_ms > source_duration_ms:
            issues.append(
                AlignmentIssue(
                    code="OUT_OF_BOUNDS_TRANSCRIPT_RANGE",
                    message="A stable transcript outcome is outside the source timeline.",
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    segment_id=segment.segment_id,
                )
            )
        union_start = max(cursor, segment.start_ms, 0)
        union_end = min(segment.end_ms, source_duration_ms)
        if union_end > union_start:
            accounted_duration_ms += union_end - union_start
        cursor = max(cursor, min(segment.end_ms, source_duration_ms))

        if segment.outcome is TranscriptOutcome.TRANSCRIBED:
            if segment.timing_status is TranscriptTimingStatus.ALIGNED:
                aligned_transcribed_segment_count += 1
            else:
                issues.append(
                    AlignmentIssue(
                        code="UNALIGNED_TRANSCRIBED_SEGMENT",
                        message="A transcribed segment still has estimated timing.",
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        segment_id=segment.segment_id,
                    )
                )
        elif segment.outcome is TranscriptOutcome.INAUDIBLE:
            issues.append(
                AlignmentIssue(
                    code="INAUDIBLE_TRANSCRIPT_RANGE",
                    message="A source range could not be transcribed reliably.",
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    segment_id=segment.segment_id,
                )
            )
        elif segment.outcome is TranscriptOutcome.FAILED:
            issues.append(
                AlignmentIssue(
                    code="FAILED_TRANSCRIPT_RANGE",
                    message="A source range exhausted safe transcription attempts.",
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    segment_id=segment.segment_id,
                )
            )

    if cursor < source_duration_ms:
        unresolved = TimelineRange(cursor, source_duration_ms)
        unresolved_ranges.append(unresolved)
        issues.append(
            AlignmentIssue(
                code="UNCOVERED_TRANSCRIPT_RANGE",
                message="The final source range has no stable transcript outcome.",
                start_ms=unresolved.start_ms,
                end_ms=unresolved.end_ms,
            )
        )
    return (
        tuple(issues),
        tuple(unresolved_ranges),
        accounted_duration_ms,
        outcome_counts,
        outcome_durations_ms,
        aligned_transcribed_segment_count,
    )


def _materialized_chunk_checkpoints(
    store: JobStore,
    job_id: str,
) -> dict[int, dict[str, Any]]:
    materialized: dict[int, dict[str, Any]] = {}
    prefix = "chunk_"
    suffix = "_materialized"
    for checkpoint in store.list_checkpoints(job_id, stage="transcribing"):
        if not (
            checkpoint.checkpoint_key.startswith(prefix)
            and checkpoint.checkpoint_key.endswith(suffix)
        ):
            continue
        raw_index = checkpoint.checkpoint_key[len(prefix) : -len(suffix)]
        if raw_index.isdigit():
            materialized[int(raw_index)] = checkpoint.payload
    return materialized


def _forced_alignment_checkpoints(
    store: JobStore,
    job_id: str,
) -> dict[str, dict[str, Any]]:
    checkpoints: dict[str, dict[str, Any]] = {}
    for checkpoint in store.list_checkpoints(job_id, stage=CHECKPOINT_STAGE):
        if not checkpoint.checkpoint_key.startswith("forced_alignment_seg_"):
            continue
        segment_id = checkpoint.payload.get("segment_id")
        if isinstance(segment_id, str):
            checkpoints[segment_id] = checkpoint.payload
    return checkpoints


def _forced_alignment_evidence_valid(
    store: JobStore,
    job_id: str,
    *,
    segment: TranscriptSegment,
    plan: NormalizedAudioPlan,
    payload: dict[str, Any] | None,
) -> bool:
    if payload is None or segment.text is None:
        return False
    original_start_ms = payload.get("segment_start_ms")
    original_end_ms = payload.get("segment_end_ms")
    original_revision = payload.get("segment_revision")
    raw_relative_path = payload.get("raw_relative_path")
    raw_sha256 = payload.get("raw_sha256")
    if (
        payload.get("schema_version") != "1.0.0"
        or not _strict_int(payload.get("alignment_report_generation"), minimum=1)
        or not isinstance(payload.get("alignment_report_sha256"), str)
        or not SHA256_PATTERN.fullmatch(payload["alignment_report_sha256"])
        or payload.get("segment_id") != segment.segment_id
        or not _strict_int(original_revision, minimum=1)
        or original_revision + 1 != segment.revision
        or not _strict_int(original_start_ms, minimum=0)
        or not _strict_int(original_end_ms, minimum=1)
        or original_end_ms <= original_start_ms
        or payload.get("segment_text_sha256")
        != hashlib.sha256(segment.text.encode("utf-8")).hexdigest()
        or not isinstance(payload.get("language"), str)
        or not payload["language"]
        or not isinstance(payload.get("model_id"), str)
        or not payload["model_id"]
        or payload.get("normalized_sha256") != plan.normalized_sha256
        or payload.get("sample_rate") != plan.sample_rate
        or payload.get("start_frame") != round(original_start_ms * plan.sample_rate / 1000)
        or payload.get("end_frame") != round(original_end_ms * plan.sample_rate / 1000)
        or payload.get("aligned_start_ms") != segment.start_ms
        or payload.get("aligned_end_ms") != segment.end_ms
        or segment.start_ms < original_start_ms
        or segment.end_ms > original_end_ms
        or not _strict_int(payload.get("word_count"), minimum=1)
        or payload.get("normalized_text_sha256")
        != hashlib.sha256(
            _normalize_forced_alignment_text(segment.text).encode("utf-8")
        ).hexdigest()
        or not isinstance(raw_relative_path, str)
        or not isinstance(raw_sha256, str)
        or not SHA256_PATTERN.fullmatch(raw_sha256)
    ):
        return False

    unresolved_path = store.data_directory / raw_relative_path
    root = store.get_job_stage_directory(
        job_id,
        stage="forced_alignment_raw",
    ).resolve()
    try:
        if unresolved_path.is_symlink():
            return False
        path = unresolved_path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return False
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != raw_sha256:
            return False
        raw_payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(raw_payload, dict)
        or raw_payload.get("schema_version") != "1.0.0"
        or raw_payload.get("segment_id") != segment.segment_id
        or raw_payload.get("segment_revision") != original_revision
        or raw_payload.get("segment_start_ms") != original_start_ms
        or raw_payload.get("segment_end_ms") != original_end_ms
        or raw_payload.get("segment_text_sha256") != payload["segment_text_sha256"]
        or raw_payload.get("language") != payload["language"]
        or raw_payload.get("model_id") != payload["model_id"]
        or raw_payload.get("normalized_sha256") != plan.normalized_sha256
        or raw_payload.get("sample_rate") != plan.sample_rate
        or raw_payload.get("start_frame") != payload["start_frame"]
        or raw_payload.get("end_frame") != payload["end_frame"]
        or not isinstance(raw_payload.get("words"), list)
        or len(raw_payload["words"]) != payload["word_count"]
    ):
        return False

    duration_seconds = (original_end_ms - original_start_ms) / 1000
    prior_end = 0.0
    aligned_text: list[str] = []
    normalized_words: list[tuple[float, float]] = []
    for word in raw_payload["words"]:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"]:
            return False
        try:
            start_time = float(word["start_time"])
            end_time = float(word["end_time"])
        except (KeyError, TypeError, ValueError):
            return False
        if (
            not math.isfinite(start_time)
            or not math.isfinite(end_time)
            or start_time < 0
            or end_time < start_time
            or start_time < prior_end
            or end_time > duration_seconds
        ):
            return False
        normalized_words.append((start_time, end_time))
        aligned_text.append(word["text"])
        prior_end = end_time
    if not normalized_words:
        return False
    if (
        original_start_ms + round(normalized_words[0][0] * 1000) != segment.start_ms
        or original_start_ms + round(normalized_words[-1][1] * 1000) != segment.end_ms
        or _normalize_forced_alignment_text("".join(aligned_text))
        != _normalize_forced_alignment_text(segment.text)
    ):
        return False
    return True


def _normalize_forced_alignment_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if character == "'" or unicodedata.category(character).startswith(("L", "N"))
    )


def _strict_int(value: Any, *, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _attempt_matches_chunk(
    attempt: AsrAttemptRecord,
    chunk: AudioChunkPlan,
    *,
    checkpoint_start_ms: int,
    checkpoint_end_ms: int,
) -> bool:
    return (
        attempt.start_frame == chunk.start_frame
        and attempt.end_frame == chunk.end_frame
        and attempt.start_ms == chunk.start_ms
        and attempt.end_ms == chunk.end_ms
        and checkpoint_start_ms == chunk.start_ms
        and checkpoint_end_ms == chunk.end_ms
    )


def _segment_matches_chunk(
    segment: TranscriptSegment,
    chunk: AudioChunkPlan,
) -> bool:
    return (
        segment.outcome is TranscriptOutcome.TRANSCRIBED
        and segment.start_ms >= chunk.start_ms
        and segment.end_ms <= chunk.end_ms
    )


def _report_outcome(report: AlignmentReport) -> AlignmentFinalizationOutcome:
    if not report.evidence_complete:
        return AlignmentFinalizationOutcome.EVIDENCE_INCOMPLETE
    if not report.alignment_complete:
        return AlignmentFinalizationOutcome.ALIGNMENT_INCOMPLETE
    if not report.timeline_accounted:
        return AlignmentFinalizationOutcome.TIMELINE_INCOMPLETE
    if not report.transcript_complete:
        return AlignmentFinalizationOutcome.TRANSCRIPT_PARTIAL
    return AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
