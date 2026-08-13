"""Targeted gap re-transcription tests."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import wave

import numpy as np
import pytest

from speech_capture_worker.alignment import (
    AlignmentFinalizationOutcome,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor, AsrRunOutcome
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.downstream_revision import DownstreamRevisionCreator
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.gap_retranscription import (
    GapRetranscriptionExecutor,
    GapRetranscriptionOutcome,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.natural_pause import (
    NaturalPauseMaterializer,
    NaturalPauseOutcome,
)
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceIssue,
    ResourceReport,
)


def wav_bytes(*, duration_seconds: float) -> bytes:
    sample_rate = 16_000
    frame_count = round(duration_seconds * sample_rate)
    time = np.arange(frame_count, dtype=np.float64) / sample_rate
    samples = (np.sin(2 * np.pi * 330 * time) * 3000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())
    return output.getvalue()


def source_probe_for(duration_seconds: float):
    def probe(_):
        return MediaProbeResult(
            duration_seconds=duration_seconds,
            audio_stream_count=1,
            format_name="wav",
        )

    return probe


def preflight(status: ResourceStatus = ResourceStatus.READY):
    def check(*_, **__):
        return ResourceReport(
            status=status,
            estimated_required_bytes=256 * 1024 * 1024,
            disk_reserve_bytes=20 * GIB,
            disk_free_after_bytes=40 * GIB,
            disk=DiskSnapshot(total_bytes=256 * GIB, free_bytes=80 * GIB),
            memory=MemorySnapshot(
                total_bytes=32 * GIB,
                available_bytes=20 * GIB,
                used_percent=40,
                swap_used_bytes=0,
            ),
            issues=(
                (
                    ResourceIssue(
                        code="MEMORY_PRESSURE_BLOCKED",
                        status=ResourceStatus.BLOCKED,
                        message="Memory pressure is too high.",
                        action="Close large applications, then resume.",
                    ),
                )
                if status is ResourceStatus.BLOCKED
                else ()
            ),
        )

    return check


class PartialAsrEngine:
    model_id = "fake/partial-asr"

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        duration = len(audio) / sample_rate
        return {
            "text": "甲",
            "language": "Chinese",
            "segments": [{"text": "甲", "start": 0.0, "end": min(0.5, duration)}],
            "chunks": [
                {
                    "text": "甲",
                    "start": 0.0,
                    "end": duration,
                    "chunk_index": 0,
                    "finish_reason": "stop",
                    "truncated": False,
                }
            ],
            "finish_reason": "stop",
            "truncated": False,
        }


class GapEngine:
    model_id = "fake/gap-asr"

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        self.calls += 1
        duration = len(audio) / sample_rate
        return {
            "text": "乙",
            "language": "Chinese",
            "segments": [{"text": "乙", "start": 0.0, "end": duration}],
            "chunks": [
                {
                    "text": "乙",
                    "start": 0.0,
                    "end": duration,
                    "chunk_index": 0,
                    "finish_reason": "stop",
                    "truncated": False,
                }
            ],
            "finish_reason": "stop",
            "truncated": False,
        }


class BoundaryFragmentEngine:
    model_id = "fake/boundary-fragment-asr"

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        return {
            "text": "种。",
            "language": "Chinese",
            "segments": [{"text": "种", "start": 0.0, "end": 0.08}],
            "chunks": [
                {
                    "text": "种。",
                    "start": 0.0,
                    "end": len(audio) / sample_rate,
                    "chunk_index": 0,
                    "finish_reason": "stop",
                    "truncated": False,
                }
            ],
            "finish_reason": "stop",
            "truncated": False,
        }


def create_aligning_job_with_gap(
    store: JobStore,
    *,
    suffix: str,
    duration_seconds: float = 1,
):
    content = wav_bytes(duration_seconds=duration_seconds)
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"gap-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"gap-upload-{suffix}",
    )
    store.put_upload_part(
        upload.upload_id,
        part_number=1,
        content=content,
        part_sha256=checksum,
    )
    store.complete_upload(upload.upload_id)
    queued, _ = store.create_job_from_upload(
        upload.upload_id,
        idempotency_key=f"gap-job-{suffix}",
    )
    claimed = store.claim_job_for_processing(
        queued.job_id,
        expected_revision=queued.revision,
    )
    result = AsrChunkExecutor(
        store,
        PartialAsrEngine(),
        boundary_preflight=preflight(),
    ).run_next(claimed.job_id)
    assert result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    finalized = TranscriptAlignmentFinalizer(store).finalize(claimed.job_id)
    assert finalized.outcome is AlignmentFinalizationOutcome.TIMELINE_INCOMPLETE
    return finalized.job


def write_speech_evidence(store: JobStore, job_id: str) -> None:
    alignment = next(
        checkpoint
        for checkpoint in store.list_checkpoints(job_id, stage="aligning")
        if checkpoint.checkpoint_key == "transcript_alignment_report"
    )
    store.put_checkpoint(
        job_id,
        stage="aligning",
        checkpoint_key="gap_speech_activity_evidence",
        payload={
            "schema_version": "1.0.0",
            "detector": {
                "detector_id": "fixture_vad",
                "detector_version": "1.0.0",
                "model_id": "fixture/model",
                "model_revision": "fixture-revision",
                "configuration_id": "fixture-config",
            },
            "gap_analysis_generation": 1,
            "gap_analysis_sha256": "a" * 64,
            "alignment_report_generation": alignment.generation,
            "alignment_report_sha256": alignment.payload_sha256,
            "normalized_sha256": "a" * 64,
            "sample_rate": 16_000,
            "source_duration_ms": 1000,
            "evaluated_gap_count": 1,
            "speech_detected_count": 1,
            "no_speech_detected_count": 0,
            "automatic_materialization_authorized": False,
            "evidence": [
                {
                    "start_ms": 480,
                    "end_ms": 1020,
                    "duration_ms": 540,
                    "speech_duration_ms": 500,
                    "speech_ratio": 1.0,
                    "observation": "speech_detected",
                    "reason_code": "DETECTOR_RETURNED_SPEECH_REGIONS",
                    "materialization_authorized": False,
                    "speech_regions": [],
                }
            ],
        },
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_gap_retranscription_fills_speech_gap_with_durable_evidence(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_aligning_job_with_gap(store, suffix="fill")
        write_speech_evidence(store, job.job_id)
        engine = GapEngine()
        result = GapRetranscriptionExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="aligning")

        assert result.outcome is GapRetranscriptionOutcome.COMPLETED
        assert result.retranscribed_gap_count == 1
        assert result.added_segment_count == 1
        assert engine.calls == 1
        assert result.alignment.outcome is AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
        assert result.job.state is JobState.DIARIZING
        assert [segment.text for segment in snapshot.stable_segments] == ["甲", "乙"]
        assert any(
            checkpoint.checkpoint_key.startswith("gap_retranscription_")
            for checkpoint in checkpoints
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_gap_retranscription_is_idempotent(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_aligning_job_with_gap(store, suffix="replay")
        write_speech_evidence(store, job.job_id)
        executor = GapRetranscriptionExecutor(
            store,
            GapEngine(),
            boundary_preflight=preflight(),
        )
        first = executor.run(job.job_id)
        engine = GapEngine()
        second = GapRetranscriptionExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)

        assert first.retranscribed_gap_count == 1
        assert second.retranscribed_gap_count == 0
        assert engine.calls == 0
        assert second.job.state is JobState.DIARIZING


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_gap_retranscription_rejects_low_coverage_boundary_fragment(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        job = create_aligning_job_with_gap(
            store,
            suffix="boundary-fragment",
            duration_seconds=2,
        )
        alignment = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )
        unresolved = alignment.payload["unresolved_ranges"][0]
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key="gap_speech_activity_evidence",
            payload={
                "schema_version": "1.0.0",
                "alignment_report_generation": alignment.generation,
                "alignment_report_sha256": alignment.payload_sha256,
                "evidence": [
                    {
                        **unresolved,
                        "duration_ms": unresolved["end_ms"] - unresolved["start_ms"],
                        "speech_duration_ms": 80,
                        "speech_ratio": round(
                            80 / (unresolved["end_ms"] - unresolved["start_ms"]),
                            8,
                        ),
                        "observation": "speech_detected",
                        "reason_code": "DETECTOR_RETURNED_SPEECH_REGIONS",
                        "materialization_authorized": False,
                        "speech_regions": [
                            {
                                "start_ms": unresolved["start_ms"],
                                "end_ms": unresolved["start_ms"] + 80,
                            }
                        ],
                    }
                ],
            },
        )

        result = GapRetranscriptionExecutor(
            store,
            BoundaryFragmentEngine(),
            boundary_preflight=preflight(),
        ).run(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="aligning")
        assert result.retranscribed_gap_count == 0
        assert result.failed_gap_count == 1
        assert result.added_segment_count == 0
        assert [segment.text for segment in snapshot.stable_segments] == ["甲"]
        rejected = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.checkpoint_key.startswith("gap_retranscription_rejected_")
        )
        assert rejected.payload["reason_code"] == "LOW_COVERAGE_BOUNDARY_FRAGMENT"
        assert rejected.payload["segment_count"] == 1
        assert rejected.payload["transcribed_duration_ms"] == 80

        pauses = NaturalPauseMaterializer(store).materialize(job.job_id)
        final_snapshot = store.get_job_snapshot(job.job_id)

    assert pauses.outcome is NaturalPauseOutcome.MATERIALIZED
    assert pauses.created_segment_count == 1
    assert pauses.job.state is JobState.DIARIZING
    assert [segment.outcome.value for segment in final_snapshot.stable_segments] == [
        "transcribed",
        "non_speech",
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_natural_pause_materializes_vad_confirmed_no_speech(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        job = create_aligning_job_with_gap(
            store,
            suffix="vad-no-speech",
            duration_seconds=2,
        )
        alignment = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )
        unresolved = alignment.payload["unresolved_ranges"][0]
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key="gap_speech_activity_evidence",
            payload={
                "schema_version": "1.0.0",
                "alignment_report_generation": alignment.generation,
                "alignment_report_sha256": alignment.payload_sha256,
                "evidence": [
                    {
                        **unresolved,
                        "duration_ms": unresolved["end_ms"] - unresolved["start_ms"],
                        "speech_duration_ms": 0,
                        "speech_ratio": 0.0,
                        "observation": "no_speech_detected",
                        "reason_code": "DETECTOR_RETURNED_NO_SPEECH_REGIONS",
                        "materialization_authorized": False,
                        "speech_regions": [],
                    }
                ],
            },
        )

        result = NaturalPauseMaterializer(store).materialize(job.job_id)
        replayed = NaturalPauseMaterializer(store).materialize(job.job_id)

    assert result.outcome is NaturalPauseOutcome.MATERIALIZED
    assert result.created_segment_count == 1
    assert result.job.state is JobState.DIARIZING
    assert replayed.outcome is NaturalPauseOutcome.ALREADY_FINALIZED
    assert replayed.created_segment_count == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_natural_pause_refuses_interior_speech_even_after_asr_rejection(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        job = create_aligning_job_with_gap(
            store,
            suffix="interior-speech",
            duration_seconds=2,
        )
        alignment = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )
        unresolved = alignment.payload["unresolved_ranges"][0]
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key="gap_speech_activity_evidence",
            payload={
                "schema_version": "1.0.0",
                "alignment_report_generation": alignment.generation,
                "alignment_report_sha256": alignment.payload_sha256,
                "evidence": [
                    {
                        **unresolved,
                        "duration_ms": unresolved["end_ms"] - unresolved["start_ms"],
                        "speech_duration_ms": 200,
                        "speech_ratio": round(
                            200 / (unresolved["end_ms"] - unresolved["start_ms"]),
                            8,
                        ),
                        "observation": "speech_detected",
                        "reason_code": "DETECTOR_RETURNED_SPEECH_REGIONS",
                        "materialization_authorized": False,
                        "speech_regions": [
                            {
                                "start_ms": unresolved["start_ms"] + 600,
                                "end_ms": unresolved["start_ms"] + 800,
                            }
                        ],
                    }
                ],
            },
        )
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key=(
                "gap_retranscription_rejected_"
                f"{unresolved['start_ms']:010d}_{unresolved['end_ms']:010d}"
            ),
            payload={
                "schema_version": "1.0.0",
                "start_ms": unresolved["start_ms"],
                "end_ms": unresolved["end_ms"],
                "reason_code": "LOW_COVERAGE_BOUNDARY_FRAGMENT",
            },
        )

        result = NaturalPauseMaterializer(store).materialize(job.job_id)

    assert result.outcome is NaturalPauseOutcome.NO_SAFE_PAUSES
    assert result.created_segment_count == 0
    assert result.job.state is JobState.ALIGNING


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_natural_pause_materializes_failed_boundary_residual(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        job = create_aligning_job_with_gap(
            store,
            suffix="failed-boundary-residual",
            duration_seconds=2,
        )
        alignment = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )
        unresolved = alignment.payload["unresolved_ranges"][0]
        duration_ms = unresolved["end_ms"] - unresolved["start_ms"]
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key="gap_speech_activity_evidence",
            payload={
                "schema_version": "1.0.0",
                "alignment_report_generation": alignment.generation,
                "alignment_report_sha256": alignment.payload_sha256,
                "evidence": [
                    {
                        **unresolved,
                        "duration_ms": duration_ms,
                        "speech_duration_ms": 150,
                        "speech_ratio": round(150 / duration_ms, 8),
                        "observation": "speech_detected",
                        "reason_code": "DETECTOR_RETURNED_SPEECH_REGIONS",
                        "materialization_authorized": False,
                        "speech_regions": [
                            {
                                "start_ms": unresolved["start_ms"],
                                "end_ms": unresolved["start_ms"] + 150,
                            },
                        ],
                    }
                ],
            },
        )
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key=(
                "gap_retranscription_failed_"
                f"{unresolved['start_ms']:010d}_{unresolved['end_ms']:010d}"
            ),
            payload={
                "schema_version": "1.0.0",
                "start_ms": unresolved["start_ms"],
                "end_ms": unresolved["end_ms"],
                "attempt_count": 3,
                "last_error_code": None,
            },
        )

        result = NaturalPauseMaterializer(store).materialize(job.job_id)

    assert result.outcome is NaturalPauseOutcome.MATERIALIZED
    assert result.created_segment_count == 1
    assert result.job.state is JobState.DIARIZING


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_natural_pause_materializes_rejected_boundary_filler(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        job = create_aligning_job_with_gap(
            store,
            suffix="rejected-boundary-filler",
            duration_seconds=2,
        )
        alignment = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )
        unresolved = alignment.payload["unresolved_ranges"][0]
        duration_ms = unresolved["end_ms"] - unresolved["start_ms"]
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key="gap_speech_activity_evidence",
            payload={
                "schema_version": "1.0.0",
                "alignment_report_generation": alignment.generation,
                "alignment_report_sha256": alignment.payload_sha256,
                "evidence": [
                    {
                        **unresolved,
                        "duration_ms": duration_ms,
                        "speech_duration_ms": 900,
                        "speech_ratio": round(900 / duration_ms, 8),
                        "observation": "speech_detected",
                        "reason_code": "DETECTOR_RETURNED_SPEECH_REGIONS",
                        "materialization_authorized": False,
                        "speech_regions": [
                            {
                                "start_ms": unresolved["start_ms"],
                                "end_ms": unresolved["start_ms"] + 900,
                            }
                        ],
                    }
                ],
            },
        )
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key=(
                "gap_retranscription_rejected_"
                f"{unresolved['start_ms']:010d}_{unresolved['end_ms']:010d}"
            ),
            payload={
                "schema_version": "1.0.0",
                "start_ms": unresolved["start_ms"],
                "end_ms": unresolved["end_ms"],
                "reason_code": "LOW_COVERAGE_BOUNDARY_FRAGMENT",
                "transcribed_duration_ms": 80,
            },
        )

        result = NaturalPauseMaterializer(store).materialize(job.job_id)

    assert result.outcome is NaturalPauseOutcome.MATERIALIZED
    assert result.created_segment_count == 1
    assert result.job.state is JobState.DIARIZING


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_downstream_revision_recomputes_gap_evidence_when_timeline_changed(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        source = create_aligning_job_with_gap(
            store,
            suffix="downstream-recompute-vad",
        )
        write_speech_evidence(store, source.job_id)
        current = store.get_job(source.job_id)
        for state in (
            JobState.DIARIZING,
            JobState.STRUCTURING,
            JobState.QUALITY_CHECK,
            JobState.PROCESSED,
        ):
            current = store.transition_job(
                source.job_id,
                state,
                expected_revision=current.revision,
            )
        source_attempts = store.list_asr_attempts(source.job_id)

        result = DownstreamRevisionCreator(store).create(
            source.job_id,
            idempotency_key="derived-recompute-vad-v1",
        )
        revision_attempts = store.list_asr_attempts(result.revision_job.job_id)
        provenance = next(
            checkpoint
            for checkpoint in store.list_checkpoints(
                result.revision_job.job_id,
                stage="derived_revision",
            )
            if checkpoint.checkpoint_key == "immutable_asr_replay"
        )

    assert result.created is True
    assert result.revision_job.state is JobState.ALIGNING
    assert result.copied_asr_attempt_count == len(source_attempts) == 1
    assert [attempt.raw_sha256 for attempt in revision_attempts] == [
        attempt.raw_sha256 for attempt in source_attempts
    ]
    assert provenance.payload["status"] == "awaiting_recomputed_gap_evidence"
    assert provenance.payload["fresh_asr_inference"] is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_downstream_revision_replays_raw_asr_without_boundary_fragment(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        source = create_aligning_job_with_gap(
            store,
            suffix="downstream-revision",
            duration_seconds=2,
        )
        alignment = next(
            checkpoint
            for checkpoint in store.list_checkpoints(source.job_id, stage="aligning")
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )
        gap = alignment.payload["unresolved_ranges"][0]
        start_ms = gap["start_ms"]
        end_ms = gap["end_ms"]
        raw_content = json.dumps(
            {"payload": {"text": "种", "segments": []}},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_content).hexdigest()
        raw_path = (
            store.get_job_stage_directory(
                source.job_id,
                stage="gap_retranscription_raw",
            )
            / f"gap-{start_ms:010d}-{end_ms:010d}-{raw_sha256[:16]}.json"
        )
        raw_path.write_bytes(raw_content)
        fragment, _ = store.commit_gap_retranscription_segment(
            source.job_id,
            commit_key=f"gap_{start_ms:010d}_{end_ms:010d}_segment_0000",
            start_ms=start_ms,
            end_ms=start_ms + 80,
            text="种",
            language="Chinese",
            confidence=None,
            raw_sha256=raw_sha256,
            raw_relative_path=raw_path.relative_to(store.data_directory).as_posix(),
        )
        store.put_checkpoint(
            source.job_id,
            stage="aligning",
            checkpoint_key=f"gap_retranscription_{start_ms:010d}_{end_ms:010d}",
            payload={
                "schema_version": "1.0.0",
                "alignment_report_generation": alignment.generation,
                "alignment_report_sha256": alignment.payload_sha256,
                "model_id": "fixture/old-gap-asr",
                "normalized_sha256": "a" * 64,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "raw_relative_path": raw_path.relative_to(
                    store.data_directory
                ).as_posix(),
                "raw_sha256": raw_sha256,
                "segment_ids": [fragment.segment_id],
                "segment_commit_keys": [fragment.commit_key],
                "elapsed_seconds": 0.1,
            },
        )
        store.put_checkpoint(
            source.job_id,
            stage="aligning",
            checkpoint_key="gap_speech_activity_evidence",
            payload={
                "schema_version": "1.0.0",
                "alignment_report_generation": alignment.generation,
                "alignment_report_sha256": alignment.payload_sha256,
                "evidence": [
                    {
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "duration_ms": end_ms - start_ms,
                        "speech_duration_ms": 80,
                        "speech_ratio": round(80 / (end_ms - start_ms), 8),
                        "observation": "speech_detected",
                        "reason_code": "DETECTOR_RETURNED_SPEECH_REGIONS",
                        "materialization_authorized": False,
                        "speech_regions": [
                            {"start_ms": start_ms, "end_ms": start_ms + 80}
                        ],
                    }
                ],
            },
        )
        current = store.get_job(source.job_id)
        for state in (
            JobState.DIARIZING,
            JobState.STRUCTURING,
            JobState.QUALITY_CHECK,
            JobState.PROCESSED,
        ):
            current = store.transition_job(
                source.job_id,
                state,
                expected_revision=current.revision,
            )
        source_attempt_sha256 = [
            attempt.raw_sha256 for attempt in store.list_asr_attempts(source.job_id)
        ]

        result = DownstreamRevisionCreator(store).create(
            source.job_id,
            idempotency_key="derived-natural-pause-v1",
        )
        replay = DownstreamRevisionCreator(store).create(
            source.job_id,
            idempotency_key="derived-natural-pause-v1",
        )
        source_snapshot = store.get_job_snapshot(source.job_id)
        revision_snapshot = store.get_job_snapshot(result.revision_job.job_id)
        final_source_attempt_sha256 = [
            attempt.raw_sha256 for attempt in store.list_asr_attempts(source.job_id)
        ]

    assert result.created is True
    assert result.revision_job.state is JobState.DIARIZING
    assert result.copied_asr_attempt_count == 1
    assert result.rejected_boundary_fragment_count == 1
    assert result.materialized_pause_count == 1
    assert replay.created is False
    assert replay.revision_job.job_id == result.revision_job.job_id
    assert final_source_attempt_sha256 == source_attempt_sha256
    assert [segment.text for segment in source_snapshot.stable_segments] == ["甲", "种"]
    assert [segment.text for segment in revision_snapshot.stable_segments] == ["甲", None]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_gap_retranscription_safely_pauses_before_model(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_aligning_job_with_gap(store, suffix="pressure")
        write_speech_evidence(store, job.job_id)
        engine = GapEngine()
        result = GapRetranscriptionExecutor(
            store,
            engine,
            boundary_preflight=preflight(ResourceStatus.BLOCKED),
        ).run(job.job_id)

        assert result.outcome is GapRetranscriptionOutcome.SAFE_PAUSED
        assert result.job.state is JobState.PAUSED
        assert result.job.last_error_code == "GAP_RETRANSCRIPTION_RESOURCE_BLOCKED"
        assert engine.calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_gap_retranscription_requires_aligning_job(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        content = wav_bytes(duration_seconds=1)
        checksum = hashlib.sha256(content).hexdigest()
        upload, _ = store.create_upload(
            UploadCreateRequest(
                vault_id="vault_primary",
                source_display_name="gap-guard.wav",
                source_sha256=checksum,
                source_size_bytes=len(content),
                media_type="audio/wav",
            ),
            idempotency_key="gap-guard-upload",
        )
        store.put_upload_part(
            upload.upload_id,
            part_number=1,
            content=content,
            part_sha256=checksum,
        )
        store.complete_upload(upload.upload_id)
        queued, _ = store.create_job_from_upload(
            upload.upload_id,
            idempotency_key="gap-guard-job",
        )
        claimed = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )
        assert claimed.state is JobState.PREPROCESSING

        with pytest.raises(InvalidJobRequest):
            GapRetranscriptionExecutor(
                store,
                GapEngine(),
                boundary_preflight=preflight(),
            ).run(claimed.job_id)
