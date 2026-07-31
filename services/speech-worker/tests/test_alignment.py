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
from speech_capture_worker.errors import (
    ForcedAlignmentFailed,
    InvalidJobRequest,
    UploadStorageError,
)
from speech_capture_worker.forced_alignment import (
    ForcedAlignmentExecutor,
    ForcedAlignmentOutcome,
)
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


class FakeForcedAlignmentEngine:
    model_id = "fake/local-forced-aligner"

    def __init__(self, words=None):
        self.words = words or [
            {"text": "private", "start_time": 0.1, "end_time": 0.25},
            {"text": "aligned", "start_time": 0.25, "end_time": 0.5},
            {"text": "transcript", "start_time": 0.5, "end_time": 0.9},
        ]
        self.calls = 0

    def align(self, audio, *, sample_rate, text, language):
        assert sample_rate == 16_000
        assert len(audio) == 16_000
        assert text == "private aligned transcript"
        assert language == "English"
        self.calls += 1
        return self.words


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
    assert "private aligned transcript" not in json.dumps([update.to_dict() for update in updates])


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
    assert [timeline_range.to_dict() for timeline_range in result.report.unresolved_ranges] == [
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
    assert {issue.code for issue in result.report.issues} == {"UNALIGNED_TRANSCRIBED_SEGMENT"}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_aligned_fallback_without_forced_evidence_cannot_pass_exit_gate(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="missing-forced-evidence",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        estimated = store.get_job_snapshot(job.job_id).stable_segments[0]
        store.update_transcript_segment_metadata(
            job.job_id,
            estimated.segment_id,
            expected_revision=estimated.revision,
            timing_status=TranscriptTimingStatus.ALIGNED,
        )
        result = TranscriptAlignmentFinalizer(store).finalize(job.job_id)

    assert result.outcome is AlignmentFinalizationOutcome.EVIDENCE_INCOMPLETE
    assert result.job.state is JobState.ALIGNING
    assert result.report.alignment_complete is True
    assert result.report.timeline_accounted is True
    assert {issue.code for issue in result.report.issues} == {"MISSING_FORCED_ALIGNMENT_EVIDENCE"}


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
    assert {issue.code for issue in result.report.issues} == {"MISSING_MATERIALIZED_CHUNK"}


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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_forced_alignment_preserves_text_and_persists_private_evidence(
    tmp_path,
) -> None:
    engine = FakeForcedAlignmentEngine()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="forced-success",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        before = store.get_job_snapshot(job.job_id).stable_segments[0]
        result = ForcedAlignmentExecutor(
            store,
            engine,
            boundary_preflight=ready_preflight,
        ).run_next(job.job_id)
        after = store.get_job_snapshot(job.job_id).stable_segments[0]
        evidence = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key == f"forced_alignment_{before.segment_id}"
        )
        raw_path = store.data_directory / evidence.payload["raw_relative_path"]
        updates, _ = store.list_job_updates(job.job_id)

    assert engine.calls == 1
    assert result.outcome is ForcedAlignmentOutcome.ALIGNED
    assert after.segment_id == before.segment_id
    assert after.commit_key == before.commit_key
    assert after.text == before.text
    assert after.language == before.language
    assert after.revision == before.revision + 1
    assert after.start_ms == 100
    assert after.end_ms == 900
    assert after.timing_status is TranscriptTimingStatus.ALIGNED
    assert result.alignment.report.alignment_complete is True
    assert result.alignment.report.timeline_accounted is False
    assert [value.to_dict() for value in result.alignment.report.unresolved_ranges] == [
        {"start_ms": 0, "end_ms": 100},
        {"start_ms": 900, "end_ms": 1000},
    ]
    assert evidence.payload["word_count"] == 3
    assert (
        evidence.payload["segment_text_sha256"]
        == hashlib.sha256(before.text.encode("utf-8")).hexdigest()
    )
    assert "private aligned transcript" not in json.dumps(evidence.payload)
    assert "private aligned transcript" not in json.dumps([update.to_dict() for update in updates])
    assert raw_path.is_file()
    assert raw_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_forced_alignment_can_complete_alignment_exit_gate(tmp_path) -> None:
    engine = FakeForcedAlignmentEngine(
        [
            {"text": "private", "start_time": 0, "end_time": 0.25},
            {"text": "aligned", "start_time": 0.25, "end_time": 0.5},
            {"text": "transcript", "start_time": 0.5, "end_time": 1},
        ]
    )
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="forced-exit",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        completed = ForcedAlignmentExecutor(
            store,
            engine,
            boundary_preflight=ready_preflight,
        ).run_next(job.job_id)
        repeated = ForcedAlignmentExecutor(
            store,
            engine,
            boundary_preflight=ready_preflight,
        ).run_next(job.job_id)
        evidence = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key.startswith("forced_alignment_seg_")
        )
        raw_path = store.data_directory / evidence.payload["raw_relative_path"]
        raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
        with pytest.raises(InvalidJobRequest, match="no longer satisfies"):
            TranscriptAlignmentFinalizer(store).finalize(job.job_id)

    assert engine.calls == 1
    assert completed.outcome is ForcedAlignmentOutcome.ALIGNED
    assert completed.job.state is JobState.DIARIZING
    assert completed.alignment.outcome is (AlignmentFinalizationOutcome.READY_FOR_DIARIZATION)
    assert completed.alignment.report.ready_for_diarization is True
    assert repeated.outcome is ForcedAlignmentOutcome.ALREADY_FINALIZED
    assert repeated.job.state is JobState.DIARIZING


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_forced_alignment_replays_durable_evidence_after_interruption(
    tmp_path,
    monkeypatch,
) -> None:
    engine = FakeForcedAlignmentEngine()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="forced-replay",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        original_update = store.update_transcript_segment_metadata

        def interrupt_update(*args, **kwargs):
            raise RuntimeError("simulated interruption")

        monkeypatch.setattr(store, "update_transcript_segment_metadata", interrupt_update)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            ForcedAlignmentExecutor(
                store,
                engine,
                boundary_preflight=ready_preflight,
            ).run_next(job.job_id)
        monkeypatch.setattr(
            store,
            "update_transcript_segment_metadata",
            original_update,
        )
        replayed = ForcedAlignmentExecutor(
            store,
            engine,
            boundary_preflight=ready_preflight,
        ).run_next(job.job_id)

    assert engine.calls == 1
    assert replayed.outcome is ForcedAlignmentOutcome.REPLAYED
    assert replayed.segment is not None
    assert replayed.segment.timing_status is TranscriptTimingStatus.ALIGNED
    assert replayed.segment.start_ms == 100
    assert replayed.segment.end_ms == 900


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
@pytest.mark.parametrize(
    "words",
    [
        [
            {"text": "private", "start_time": 0.1, "end_time": 0.4},
            {"text": "transcript", "start_time": 0.4, "end_time": 0.9},
        ],
        [
            {"text": "private", "start_time": 0.4, "end_time": 0.5},
            {"text": "aligned", "start_time": 0.3, "end_time": 0.6},
            {"text": "transcript", "start_time": 0.6, "end_time": 0.9},
        ],
        [
            {"text": "private", "start_time": 0.1, "end_time": 0.25},
            {"text": "aligned", "start_time": 0.25, "end_time": 0.5},
            {"text": "transcript", "start_time": 0.5, "end_time": 1.1},
        ],
    ],
)
def test_forced_alignment_rejects_incomplete_or_invalid_evidence(
    tmp_path,
    words,
) -> None:
    engine = FakeForcedAlignmentEngine(words)
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix=f"forced-invalid-{len(words)}-{words[0]['start_time']}",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        before = store.get_job_snapshot(job.job_id).stable_segments[0]
        with pytest.raises(ForcedAlignmentFailed):
            ForcedAlignmentExecutor(
                store,
                engine,
                boundary_preflight=ready_preflight,
            ).run_next(job.job_id)
        after = store.get_job_snapshot(job.job_id).stable_segments[0]
        evidence = [
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key == f"forced_alignment_{before.segment_id}"
        ]

    assert engine.calls == 1
    assert after == before
    assert evidence == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_forced_alignment_requires_language_before_model_call(tmp_path) -> None:
    engine = FakeForcedAlignmentEngine()

    def result_without_language(audio):
        payload = result_with_segments(audio, segments=[])
        payload["language"] = None
        return payload

    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="forced-language",
        )
        run_to_alignment(store, job.job_id, result_without_language)
        with pytest.raises(ForcedAlignmentFailed, match="language metadata"):
            ForcedAlignmentExecutor(
                store,
                engine,
                boundary_preflight=ready_preflight,
            ).run_next(job.job_id)

    assert engine.calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_forced_alignment_resource_block_pauses_before_model_call(tmp_path) -> None:
    engine = FakeForcedAlignmentEngine()

    def blocked_preflight(*_, **__):
        report = ready_preflight()
        return ResourceReport(
            status=ResourceStatus.BLOCKED,
            estimated_required_bytes=report.estimated_required_bytes,
            disk_reserve_bytes=report.disk_reserve_bytes,
            disk_free_after_bytes=report.disk_free_after_bytes,
            disk=report.disk,
            memory=report.memory,
            issues=report.issues,
        )

    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="forced-blocked",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        result = ForcedAlignmentExecutor(
            store,
            engine,
            boundary_preflight=blocked_preflight,
        ).run_next(job.job_id)

    assert result.outcome is ForcedAlignmentOutcome.SAFE_PAUSED
    assert result.job.state is JobState.PAUSED
    assert engine.calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_forced_alignment_rejects_tampered_private_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    engine = FakeForcedAlignmentEngine()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="forced-tamper",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )
        original_update = store.update_transcript_segment_metadata
        monkeypatch.setattr(
            store,
            "update_transcript_segment_metadata",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated interruption")),
        )
        with pytest.raises(RuntimeError):
            ForcedAlignmentExecutor(
                store,
                engine,
                boundary_preflight=ready_preflight,
            ).run_next(job.job_id)
        monkeypatch.setattr(
            store,
            "update_transcript_segment_metadata",
            original_update,
        )
        evidence = next(
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="aligning")
            if checkpoint.checkpoint_key.startswith("forced_alignment_seg_")
        )
        raw_path = store.data_directory / evidence.payload["raw_relative_path"]
        raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
        with pytest.raises(UploadStorageError, match="checksum"):
            ForcedAlignmentExecutor(
                store,
                engine,
                boundary_preflight=ready_preflight,
            ).run_next(job.job_id)

    assert engine.calls == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_cli_force_aligns_next_segment_without_exposing_text(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    data_path = tmp_path / "runtime"
    engine = FakeForcedAlignmentEngine()
    with JobStore(
        data_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="forced-cli",
        )
        run_to_alignment(
            store,
            job.job_id,
            lambda audio: result_with_segments(audio, segments=[]),
        )

    monkeypatch.setattr(
        "speech_capture_worker.worker_cli.MlxQwenForcedAlignmentEngine",
        lambda: engine,
    )
    real_executor = ForcedAlignmentExecutor
    monkeypatch.setattr(
        "speech_capture_worker.worker_cli.ForcedAlignmentExecutor",
        lambda store, selected_engine: real_executor(
            store,
            selected_engine,
            boundary_preflight=ready_preflight,
        ),
    )
    exit_code = main(
        [
            "force-align-next",
            "--data-dir",
            str(data_path),
            job.job_id,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert engine.calls == 1
    assert payload["outcome"] == "aligned"
    assert payload["segment"]["start_ms"] == 100
    assert payload["segment"]["end_ms"] == 900
    assert payload["segment"]["timing_status"] == "aligned"
    assert "private aligned transcript" not in json.dumps(payload)
