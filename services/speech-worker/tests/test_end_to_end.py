"""First synthetic multi-chunk end-to-end pass through the durable Worker core."""

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
from speech_capture_worker.artifact_generation import (
    ArtifactGenerator,
    ArtifactOutcome,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor, AsrRunOutcome
from speech_capture_worker.diarization_execution import (
    DiarizationOutcome,
    SpeakerDiarizationExecutor,
)
from speech_capture_worker.domain import JobState, UploadCreateRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceReport,
    ResourceStatus,
)
from speech_capture_worker.scheduler import JobScheduler, SchedulerOutcome
from speech_capture_worker.structuring_execution import (
    StructuringExecutor,
    StructuringOutcome,
)
from speech_capture_worker.transcript import SpeakerLabelStatus


def synthetic_wav_bytes(*, duration_seconds: float) -> bytes:
    sample_rate = 16_000
    frames = round(duration_seconds * sample_rate)
    time = np.arange(frames, dtype=np.float64) / sample_rate
    samples = (np.sin(2 * np.pi * 330 * time) * 3000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())
    return output.getvalue()


class FakeEngine:
    model_id = "fake/local-asr"

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        assert sample_rate == 16_000
        self.calls += 1
        duration = len(audio) / sample_rate
        text = "这是合成多块端到端测试的稳定文字。"
        return {
            "text": text,
            "language": "Chinese",
            "segments": [{"text": text, "start": 0.0, "end": duration}],
            "chunks": [
                {
                    "text": text,
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


class FakeDiarizationEngine:
    model_id = "fake/speaker-diarization"

    def diarize(self, audio, *, sample_rate):
        duration = len(audio) / sample_rate
        return [
            {
                "start_ms": 0,
                "end_ms": round(duration * 1000),
                "speaker": "SPEAKER_00",
            }
        ]


class FakeStructuringEngine:
    model_id = "fake/structuring"

    def classify(self, segments, *, speaker_count):
        return {
            "type": "meeting",
            "traits": ["multi_speaker"],
            "confidence": 0.9,
        }

    def extract_batch(self, segments, *, content_type):
        evidence = segments[0]["segment_id"] if segments else "none"
        return [
            {
                "kind": "topic",
                "text": "端到端测试主题。",
                "evidence": [evidence],
                "confidence": 0.9,
            }
        ]


def ready_preflight(*_, **__) -> ResourceReport:
    return ResourceReport(
        status=ResourceStatus.READY,
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
        issues=(),
    )


def intake_and_transcribe(store: JobStore, runtime) -> tuple[str, int]:
    content = synthetic_wav_bytes(duration_seconds=95)
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name="e2e-multichunk.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key="e2e-upload",
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
        idempotency_key="e2e-job",
    )
    claimed = JobScheduler(
        store,
        storage_path=runtime,
        resource_preflight=ready_preflight,
    ).run_once()
    assert claimed.outcome is SchedulerOutcome.CLAIMED
    job_id = claimed.job.job_id
    engine = FakeEngine()
    batch = AsrChunkExecutor(
        store,
        engine,
        boundary_preflight=ready_preflight,
    ).run_all(job_id)
    assert batch.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    assert batch.total_chunks > 1
    assert batch.completed_chunks == batch.total_chunks
    assert engine.calls == batch.total_chunks
    finalized = TranscriptAlignmentFinalizer(store).finalize(job_id)
    assert finalized.outcome is AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
    assert finalized.job.state is JobState.DIARIZING
    assert finalized.report.ready_for_diarization is True
    assert finalized.report.unresolved_duration_ms == 0
    assert (
        finalized.report.aligned_transcribed_segment_count
        == finalized.report.segment_count
    )
    return job_id, batch.total_chunks


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_synthetic_multi_chunk_end_to_end_reaches_diarizing_and_survives_restart(
    tmp_path,
) -> None:
    runtime = tmp_path / "runtime"
    database = runtime / "worker.sqlite3"

    with JobStore(database) as store:
        job_id, batch_total_chunks = intake_and_transcribe(store, runtime)
        snapshot = store.get_job_snapshot(job_id)
        assert len(snapshot.stable_segments) == batch_total_chunks
        assert snapshot.progress is not None
        assert snapshot.progress.processed_ms == 95000

    with JobStore(database) as restarted:
        repeated = TranscriptAlignmentFinalizer(restarted).finalize(job_id)
        assert repeated.outcome is AlignmentFinalizationOutcome.ALREADY_FINALIZED
        assert repeated.job.state is JobState.DIARIZING
        assert len(restarted.list_asr_attempts(job_id)) == batch_total_chunks


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_synthetic_multi_chunk_end_to_end_reaches_structuring_with_anonymous_speakers(
    tmp_path,
) -> None:
    runtime = tmp_path / "runtime"
    with JobStore(runtime / "worker.sqlite3") as store:
        job_id, _ = intake_and_transcribe(store, runtime)
        result = SpeakerDiarizationExecutor(
            store,
            FakeDiarizationEngine(),
            boundary_preflight=ready_preflight,
        ).run(job_id)
        snapshot = store.get_job_snapshot(job_id)

        assert result.outcome is DiarizationOutcome.COMPLETED
        assert result.job.state is JobState.STRUCTURING
        assert result.attributed_segment_count == len(snapshot.stable_segments)
        assert result.unavailable_segment_count == 0
        assert all(
            segment.speaker_label_status is SpeakerLabelStatus.ANONYMOUS
            for segment in snapshot.stable_segments
        )
        structuring = StructuringExecutor(
            store,
            FakeStructuringEngine(),
            boundary_preflight=ready_preflight,
        ).run(job_id)
        assert structuring.outcome is StructuringOutcome.COMPLETED
        assert structuring.job.state is JobState.QUALITY_CHECK
        assert structuring.finding_count >= 1
        artifacts = ArtifactGenerator(store).generate(job_id)
        package = store.get_job_stage_directory(job_id, stage="artifacts")
        assert artifacts.outcome is ArtifactOutcome.GENERATED
        assert artifacts.job.state is JobState.PROCESSED
        assert (package / "transcript.raw.json").is_file()
        assert (package / "transcript.md").is_file()
        assert (package / "speech-record.json").is_file()
        assert (package / "note.md").is_file()
        assert (package / "artifact-manifest.json").is_file()
