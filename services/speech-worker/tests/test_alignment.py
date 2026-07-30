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
from speech_capture_worker.asr_execution import AsrChunkExecutor
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceReport,
)
from speech_capture_worker.transcript import (
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
)
from speech_capture_worker.worker_cli import main


def wav_bytes(*, duration_seconds: float) -> bytes:
    frame_count = round(duration_seconds * 16_000)
    time = np.arange(frame_count, dtype=np.float64) / 16_000
    samples = (np.sin(2 * np.pi * 330 * time) * 3000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
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


def create_preprocessing_job(
    store: JobStore,
    *,
    duration_seconds: float,
    suffix: str,
):
    content = wav_bytes(duration_seconds=duration_seconds)
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"alignment-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"alignment-upload-{suffix}",
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
        idempotency_key=f"alignment-job-{suffix}",
    )
    return store.claim_job_for_processing(
        queued.job_id,
        expected_revision=queued.revision,
    )


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


def result_with_segments(
    audio: np.ndarray,
    *,
    segments: list[dict] | None,
) -> dict:
    duration = len(audio) / 16_000
    return {
        "text": "private aligned transcript",
        "language": "English",
        "segments": segments,
        "chunks": [
            {
                "text": "private aligned transcript",
                "start": 0,
                "end": duration,
                "chunk_index": 0,
                "finish_reason": "stop",
                "truncated": False,
            }
        ],
        "finish_reason": "stop",
        "truncated": False,
    }


class FakeEngine:
    model_id = "fake/local-asr"

    def __init__(self, result_factory):
        self.result_factory = result_factory
        self.calls = 0

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        assert sample_rate == 16_000
        self.calls += 1
        return self.result_factory(audio)


def run_to_alignment(store: JobStore, job_id: str, result_factory) -> None:
    result = AsrChunkExecutor(
        store,
        FakeEngine(result_factory),
        boundary_preflight=ready_preflight,
    ).run_next(job_id)
    assert result.job.state is JobState.ALIGNING


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_complete_aligned_timeline_advances_to_diarization_without_text_leak(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="complete",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(
                audio,
                segments=[
                    {
                        "text": "private aligned transcript",
                        "start": 0,
                        "end": 1,
                    }
                ],
            ),
        )
        result = TranscriptAlignmentFinalizer(store).finalize(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)
        checkpoint = next(
            checkpoint
            for checkpoint in store.list_checkpoints(
                job.job_id,
                stage="aligning",
            )
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )
        updates, _ = store.list_job_updates(job.job_id)

    assert result.outcome is AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
    assert result.job.state is JobState.DIARIZING
    assert result.report.evidence_complete is True
    assert result.report.alignment_complete is True
    assert result.report.timeline_accounted is True
    assert result.report.transcript_complete is True
    assert result.report.accounted_duration_ms == 1000
    assert result.report.unresolved_ranges == ()
    assert snapshot.progress is not None
    assert snapshot.progress.stage is JobState.DIARIZING
    assert snapshot.progress.stage_progress == 0
    assert "private aligned transcript" not in json.dumps(checkpoint.payload)
    assert "private aligned transcript" not in json.dumps(
        [update.to_dict() for update in updates]
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_uncovered_aligned_ranges_are_durable_and_block_diarization(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="gaps",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(
                audio,
                segments=[
                    {
                        "text": "private aligned transcript",
                        "start": 0.25,
                        "end": 0.75,
                    }
                ],
            ),
        )
        result = TranscriptAlignmentFinalizer(store).finalize(job.job_id)
        checkpoint = next(
            checkpoint
            for checkpoint in store.list_checkpoints(
                job.job_id,
                stage="aligning",
            )
            if checkpoint.checkpoint_key == "transcript_alignment_report"
        )

    assert result.outcome is AlignmentFinalizationOutcome.TIMELINE_INCOMPLETE
    assert result.job.state is JobState.ALIGNING
    assert result.report.evidence_complete is True
    assert result.report.alignment_complete is True
    assert result.report.timeline_accounted is False
    assert result.report.accounted_duration_ms == 500
    assert result.report.unresolved_duration_ms == 500
    assert [
        timeline_range.to_dict() for timeline_range in result.report.unresolved_ranges
    ] == [
        {"start_ms": 0, "end_ms": 250},
        {"start_ms": 750, "end_ms": 1000},
    ]
    assert checkpoint.payload["unresolved_ranges"] == [
        {"start_ms": 0, "end_ms": 250},
        {"start_ms": 750, "end_ms": 1000},
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_estimated_full_range_requires_alignment_before_diarization(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="estimated",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        result = TranscriptAlignmentFinalizer(store).finalize(job.job_id)

    assert result.outcome is AlignmentFinalizationOutcome.ALIGNMENT_INCOMPLETE
    assert result.job.state is JobState.ALIGNING
    assert result.report.evidence_complete is True
    assert result.report.alignment_complete is False
    assert result.report.timeline_accounted is True
    assert result.report.transcript_complete is True
    assert {issue.code for issue in result.report.issues} == {
        "UNALIGNED_TRANSCRIBED_SEGMENT"
    }


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_missing_raw_materialization_evidence_blocks_alignment_exit(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="missing-evidence",
        )
        AudioPreprocessor(store).prepare(job.job_id)
        transcribing = store.transition_job(
            job.job_id,
            JobState.TRANSCRIBING,
            expected_revision=job.revision,
        )
        store.commit_transcript_segment(
            job.job_id,
            commit_key="manually_aligned_full_range",
            start_ms=0,
            end_ms=1000,
            outcome=TranscriptOutcome.TRANSCRIBED,
            text="private text without raw evidence",
            timing_status=TranscriptTimingStatus.ALIGNED,
            speaker_label_status=SpeakerLabelStatus.PENDING,
        )
        store.transition_job(
            job.job_id,
            JobState.ALIGNING,
            expected_revision=transcribing.revision,
        )
        result = TranscriptAlignmentFinalizer(store).finalize(job.job_id)

    assert result.outcome is AlignmentFinalizationOutcome.EVIDENCE_INCOMPLETE
    assert result.job.state is JobState.ALIGNING
    assert result.report.evidence_complete is False
    assert result.report.timeline_accounted is True
    assert {issue.code for issue in result.report.issues} == {
        "MISSING_MATERIALIZED_CHUNK"
    }


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_alignment_finalization_is_idempotent_after_restart(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="restart",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(
                audio,
                segments=[
                    {
                        "text": "private aligned transcript",
                        "start": 0,
                        "end": 1,
                    }
                ],
            ),
        )
        first = TranscriptAlignmentFinalizer(store).finalize(job.job_id)

    with JobStore(database) as restarted:
        second = TranscriptAlignmentFinalizer(restarted).finalize(job.job_id)
        checkpoints = restarted.list_checkpoints(job.job_id, stage="aligning")

    assert first.outcome is AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
    assert second.outcome is AlignmentFinalizationOutcome.ALREADY_FINALIZED
    assert second.job.state is JobState.DIARIZING
    assert second.report == first.report
    assert len(checkpoints) == 1
    assert checkpoints[0].generation == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_cli_finalizes_alignment_and_returns_machine_readable_report(
    tmp_path,
    capsys,
) -> None:
    data_path = tmp_path / "runtime"
    with JobStore(
        data_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="cli",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(
                audio,
                segments=[
                    {
                        "text": "private aligned transcript",
                        "start": 0,
                        "end": 1,
                    }
                ],
            ),
        )

    exit_code = main(
        [
            "finalize-alignment",
            "--data-dir",
            str(data_path),
            job.job_id,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["outcome"] == "ready_for_diarization"
    assert payload["job"]["state"] == "diarizing"
    assert payload["report"]["ready_for_diarization"] is True
    assert "private aligned transcript" not in json.dumps(payload)
