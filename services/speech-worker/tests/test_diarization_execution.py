"""Durable anonymous speaker attribution tests."""

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
from speech_capture_worker.diarization_execution import (
    DiarizationOutcome,
    PyannoteSpeakerDiarizationEngine,
    SpeakerDiarizationExecutor,
)
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.errors import DiarizationFailed, InvalidJobRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceIssue,
    ResourceReport,
)
from speech_capture_worker.transcript import SpeakerLabelStatus


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


def resource_report(status: ResourceStatus) -> ResourceReport:
    issues = ()
    if status is ResourceStatus.BLOCKED:
        issues = (
            ResourceIssue(
                code="MEMORY_PRESSURE_BLOCKED",
                status=ResourceStatus.BLOCKED,
                message="Memory pressure is too high.",
                action="Close large applications, then resume.",
            ),
        )
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
        issues=issues,
    )


def preflight(status: ResourceStatus = ResourceStatus.READY):
    report = resource_report(status)

    def check(*_, **__):
        return report

    return check


class FakeEngine:
    model_id = "fake/local-asr"

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        duration = len(audio) / sample_rate
        text = "这是说话人识别测试的稳定文字。"
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

    def __init__(self, turns=None, error=None):
        self.turns = list(turns or [])
        self.error = error
        self.calls = 0

    def diarize(self, audio, *, sample_rate):
        assert sample_rate == 16_000
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.turns)


def create_diarizing_job(
    store: JobStore,
    *,
    duration_seconds: float,
    suffix: str,
    finalize: bool = True,
):
    content = wav_bytes(duration_seconds=duration_seconds)
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"diarization-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"diarization-upload-{suffix}",
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
        idempotency_key=f"diarization-job-{suffix}",
    )
    claimed = store.claim_job_for_processing(
        queued.job_id,
        expected_revision=queued.revision,
    )
    batch = AsrChunkExecutor(
        store,
        FakeEngine(),
        boundary_preflight=preflight(),
    ).run_all(claimed.job_id)
    assert batch.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    if not finalize:
        return store.get_job(claimed.job_id)
    finalized = TranscriptAlignmentFinalizer(store).finalize(claimed.job_id)
    assert finalized.outcome is AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
    return finalized.job


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_diarization_assigns_anonymous_speakers_and_advances_to_structuring(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_diarizing_job(
            store,
            duration_seconds=95,
            suffix="assign",
        )
        engine = FakeDiarizationEngine(
            [
                {"start_ms": 0, "end_ms": 50000, "speaker": "SPEAKER_00"},
                {"start_ms": 50000, "end_ms": 95000, "speaker": "SPEAKER_01"},
            ]
        )
        result = SpeakerDiarizationExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="diarizing")

        assert result.outcome is DiarizationOutcome.COMPLETED
        assert result.job.state is JobState.STRUCTURING
        assert result.speaker_turn_count == 2
        assert result.attributed_segment_count == len(snapshot.stable_segments)
        assert result.unavailable_segment_count == 0
        assert engine.calls == 1
        assert all(
            segment.speaker_label_status is SpeakerLabelStatus.ANONYMOUS
            for segment in snapshot.stable_segments
        )
        assert {
            segment.speaker_id for segment in snapshot.stable_segments
        } <= {"speaker_01", "speaker_02"}
        assert any(
            checkpoint.checkpoint_key == "speaker_attribution_evidence"
            for checkpoint in checkpoints
        )
        assert snapshot.progress is not None
        assert snapshot.progress.diarization_status.value == "ready"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_diarization_already_completed_is_idempotent_after_restart(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(95),
    ) as store:
        job = create_diarizing_job(
            store,
            duration_seconds=95,
            suffix="replay",
        )
        SpeakerDiarizationExecutor(
            store,
            FakeDiarizationEngine(
                [
                    {"start_ms": 0, "end_ms": 95000, "speaker": "SPEAKER_00"},
                ]
            ),
            boundary_preflight=preflight(),
        ).run(job.job_id)

    with JobStore(database) as restarted:
        engine = FakeDiarizationEngine()
        result = SpeakerDiarizationExecutor(
            restarted,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)

        assert result.outcome is DiarizationOutcome.ALREADY_COMPLETED
        assert result.job.state is JobState.STRUCTURING
        assert engine.calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_diarization_safely_pauses_before_model_work(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_diarizing_job(
            store,
            duration_seconds=95,
            suffix="pressure",
        )
        engine = FakeDiarizationEngine()
        result = SpeakerDiarizationExecutor(
            store,
            engine,
            boundary_preflight=preflight(ResourceStatus.BLOCKED),
        ).run(job.job_id)

        assert result.outcome is DiarizationOutcome.SAFE_PAUSED
        assert result.job.state is JobState.PAUSED
        assert result.job.last_error_code == "DIARIZATION_RESOURCE_BLOCKED"
        assert engine.calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_diarization_engine_failure_degrades_to_unavailable_without_losing_text(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_diarizing_job(
            store,
            duration_seconds=95,
            suffix="fallback",
        )
        engine = FakeDiarizationEngine(error=RuntimeError("private failure detail"))
        result = SpeakerDiarizationExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="diarizing")
        evidence = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.checkpoint_key == "speaker_attribution_evidence"
        )
        raw_path = store.data_directory / evidence.payload["raw_relative_path"]

        assert result.outcome is DiarizationOutcome.COMPLETED
        assert result.job.state is JobState.STRUCTURING
        assert result.attributed_segment_count == 0
        assert result.unavailable_segment_count == len(snapshot.stable_segments)
        assert all(
            segment.speaker_label_status is SpeakerLabelStatus.UNAVAILABLE
            for segment in snapshot.stable_segments
        )
        assert evidence.payload["unavailable_reason_code"] == "RuntimeError"
        assert b"private failure detail" not in raw_path.read_bytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_diarization_empty_turns_mark_segments_unavailable(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_diarizing_job(
            store,
            duration_seconds=95,
            suffix="empty-turns",
        )
        result = SpeakerDiarizationExecutor(
            store,
            FakeDiarizationEngine(),
            boundary_preflight=preflight(),
        ).run(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)

        assert result.outcome is DiarizationOutcome.COMPLETED
        assert result.speaker_turn_count == 0
        assert result.attributed_segment_count == 0
        assert result.unavailable_segment_count == len(snapshot.stable_segments)
        assert all(
            segment.speaker_label_status is SpeakerLabelStatus.UNAVAILABLE
            for segment in snapshot.stable_segments
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_diarization_rejects_out_of_bounds_or_malformed_turns(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_diarizing_job(
            store,
            duration_seconds=95,
            suffix="invalid-turn",
        )
        out_of_bounds = FakeDiarizationEngine(
            [{"start_ms": 0, "end_ms": 96000, "speaker": "SPEAKER_00"}]
        )
        malformed = FakeDiarizationEngine(
            [{"start_ms": 0, "end_ms": 1000, "speaker": ""}]
        )

        with pytest.raises(DiarizationFailed):
            SpeakerDiarizationExecutor(
                store,
                out_of_bounds,
                boundary_preflight=preflight(),
            ).run(job.job_id)
        with pytest.raises(DiarizationFailed):
            SpeakerDiarizationExecutor(
                store,
                malformed,
                boundary_preflight=preflight(),
            ).run(job.job_id)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_diarization_requires_a_diarizing_job(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_diarizing_job(
            store,
            duration_seconds=95,
            suffix="guard",
            finalize=False,
        )
        assert job.state is JobState.ALIGNING

        with pytest.raises(InvalidJobRequest):
            SpeakerDiarizationExecutor(
                store,
                FakeDiarizationEngine(),
                boundary_preflight=preflight(),
            ).run(job.job_id)


def test_pyannote_engine_requires_full_revision_sha() -> None:
    with pytest.raises(InvalidJobRequest):
        PyannoteSpeakerDiarizationEngine(model_revision="main")
