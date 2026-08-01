"""Targeted gap re-transcription tests."""

from __future__ import annotations

import hashlib
import io
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
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.gap_retranscription import (
    GapRetranscriptionExecutor,
    GapRetranscriptionOutcome,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
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


def create_aligning_job_with_gap(store: JobStore, *, suffix: str):
    content = wav_bytes(duration_seconds=1)
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
