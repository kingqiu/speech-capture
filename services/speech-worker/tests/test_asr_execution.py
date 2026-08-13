import hashlib
import io
import json
import shutil
import stat
import threading
import wave

import numpy as np
import pytest

from speech_capture_worker.asr_domain import AsrAttemptState
from speech_capture_worker.asr_execution import (
    AsrChunkExecutor,
    AsrRunOutcome,
    _result_segments,
    _validate_raw_result,
)
from speech_capture_worker.audio_preprocessing import (
    AudioChunkPlan,
    AudioPreprocessor,
)
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.errors import (
    AsrAttemptConflict,
    InvalidJobRequest,
    UploadStorageError,
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
from speech_capture_worker.transcript import TranscriptOutcome


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
            source_display_name=f"meeting-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"upload-{suffix}",
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
        idempotency_key=f"job-{suffix}",
    )
    return store.claim_job_for_processing(
        queued.job_id,
        expected_revision=queued.revision,
    )


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


def valid_result(audio: np.ndarray) -> dict:
    duration = len(audio) / 16_000
    return {
        "text": "这是已经写入原始证据的稳定文字。",
        "language": "Chinese",
        "segments": [
            {
                "text": "这是已经写入原始证据的稳定文字。",
                "start": 0.0,
                "end": duration,
            }
        ],
        "chunks": [
            {
                "text": "这是已经写入原始证据的稳定文字。",
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


class FakeEngine:
    model_id = "fake/local-asr"

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = 0

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        assert sample_rate == 16_000
        self.calls += 1
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            if callable(result):
                return result(audio)
            return result
        return valid_result(audio)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_executor_persists_raw_before_visible_text_and_completes_one_chunk(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(3),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=3,
            suffix="complete",
        )
        engine = FakeEngine()
        result = AsrChunkExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run_next(job.job_id)
        attempts = store.list_asr_attempts(job.job_id)
        raw = store.get_asr_attempt_payload(
            job.job_id,
            chunk_index=0,
            attempt_number=1,
        )
        snapshot = store.get_job_snapshot(job.job_id)
        updates, _ = store.list_job_updates(job.job_id)

    assert result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    assert result.job.state is JobState.ALIGNING
    assert engine.calls == 1
    assert attempts[0].state is AsrAttemptState.SUCCEEDED
    assert raw["text"] == "这是已经写入原始证据的稳定文字。"
    assert snapshot.stable_segments[0].text == raw["text"]
    assert snapshot.stable_segments[0].outcome is TranscriptOutcome.TRANSCRIBED
    assert snapshot.progress is not None
    assert snapshot.progress.processed_ms == 3000
    assert raw["text"] not in json.dumps(
        [update.to_dict() for update in updates],
        ensure_ascii=False,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_normalized_tail_is_clamped_to_verified_container_duration(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(0.995),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="duration-clamp",
        )
        result = AsrChunkExecutor(
            store,
            FakeEngine(),
            boundary_preflight=preflight(),
        ).run_next(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)

    assert result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    assert snapshot.progress is not None
    assert snapshot.progress.duration_ms == 995
    assert snapshot.progress.processed_ms == 995
    assert snapshot.stable_segments[-1].end_ms == 995


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_restart_materializes_existing_success_without_rerunning_model(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(2),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=2,
            suffix="replay",
        )
        preprocessor = AudioPreprocessor(store)
        plan, _ = preprocessor.prepare(job.job_id)
        transcribing = store.transition_job(
            job.job_id,
            JobState.TRANSCRIBING,
            expected_revision=job.revision,
        )
        chunk = plan.chunks[0]
        with wave.open(str(preprocessor.get_normalized_path(job.job_id)), "rb") as audio:
            raw_audio = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        store.commit_asr_attempt(
            job.job_id,
            chunk_index=0,
            attempt_number=1,
            attempt_key="chunk_00000000_attempt_0001",
            state=AsrAttemptState.SUCCEEDED,
            model_id="fake/local-asr",
            start_frame=chunk.start_frame,
            end_frame=chunk.end_frame,
            start_ms=chunk.start_ms,
            end_ms=chunk.end_ms,
            raw_payload=valid_result(raw_audio),
            language="Chinese",
            finish_reason="stop",
        )
        assert transcribing.state is JobState.TRANSCRIBING

    with JobStore(database) as restarted:
        engine = FakeEngine()
        result = AsrChunkExecutor(
            restarted,
            engine,
            boundary_preflight=preflight(),
        ).run_next(job.job_id)
        snapshot = restarted.get_job_snapshot(job.job_id)

    assert result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    assert engine.calls == 0
    assert snapshot.stable_segments[0].text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_rejected_attempt_retries_without_losing_raw_evidence(tmp_path) -> None:
    truncated = valid_result(np.zeros(16_000, dtype=np.int16))
    truncated["truncated"] = True
    truncated["finish_reason"] = "length"
    truncated["chunks"][0]["truncated"] = True
    truncated["chunks"][0]["finish_reason"] = "length"
    engine = FakeEngine([truncated, valid_result])
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="retry",
        )
        executor = AsrChunkExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        )
        first = executor.run_next(job.job_id)
        second = executor.run_next(job.job_id)
        attempts = store.list_asr_attempts(job.job_id)

    assert first.outcome is AsrRunOutcome.RETRYABLE_FAILURE
    assert second.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    assert [attempt.state for attempt in attempts] == [
        AsrAttemptState.REJECTED,
        AsrAttemptState.SUCCEEDED,
    ]
    assert attempts[0].raw_relative_path != attempts[1].raw_relative_path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_retry_exhaustion_records_failed_range_and_partial_job(tmp_path) -> None:
    engine = FakeEngine([RuntimeError("private failure"), RuntimeError("private failure")])
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="partial",
        )
        executor = AsrChunkExecutor(
            store,
            engine,
            max_attempts=2,
            boundary_preflight=preflight(),
        )
        first = executor.run_next(job.job_id)
        second = executor.run_next(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)
        attempts = store.list_asr_attempts(job.job_id)
        raw = store.get_asr_attempt_payload(
            job.job_id,
            chunk_index=0,
            attempt_number=1,
        )

    assert first.outcome is AsrRunOutcome.RETRYABLE_FAILURE
    assert second.outcome is AsrRunOutcome.PARTIAL
    assert second.job.state is JobState.PARTIAL
    assert len(attempts) == 2
    assert raw == {"exception_type": "RuntimeError"}
    assert snapshot.stable_segments[0].outcome is TranscriptOutcome.FAILED
    assert snapshot.stable_segments[0].error_code == "ASR_CHUNK_RETRIES_EXHAUSTED"
    assert "private failure" not in json.dumps(raw)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_boundary_resource_block_safely_pauses_before_model_call(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="pressure",
        )
        engine = FakeEngine()
        result = AsrChunkExecutor(
            store,
            engine,
            boundary_preflight=preflight(ResourceStatus.BLOCKED),
        ).run_next(job.job_id)

    assert result.outcome is AsrRunOutcome.SAFE_PAUSED
    assert result.job.state is JobState.PAUSED
    assert result.job.last_error_code == "RESOURCE_BOUNDARY_BLOCKED"
    assert engine.calls == 0


def test_zero_duration_timestamp_segments_are_merged_without_losing_text() -> None:
    chunk = AudioChunkPlan(
        chunk_index=0,
        start_frame=0,
        end_frame=80_000,
        start_ms=0,
        end_ms=5000,
    )
    payload = {
        "text": "甲乙丙丁",
        "language": "Chinese",
        "segments": [
            {"text": "甲", "start": 0.0, "end": 1.0},
            {"text": "乙", "start": 1.0, "end": 1.0},
            {"text": "丙", "start": 1.0, "end": 2.0},
            {"text": "丁", "start": 4.0, "end": 5.0},
        ],
    }

    issues = _validate_raw_result(payload, chunk=chunk)
    segments = _result_segments(payload, chunk=chunk, source_duration_ms=5000)

    assert not any(
        issue.code in {"INVALID_TIMESTAMP_RANGE", "REVERSED_TIMESTAMP"}
        for issue in issues
    )
    assert [item["text"] for item in segments] == ["甲乙丙", "丁"]
    assert all(item["end_ms"] > item["start_ms"] for item in segments)
    assert (segments[0]["start_ms"], segments[0]["end_ms"]) == (0, 2000)
    assert (segments[1]["start_ms"], segments[1]["end_ms"]) == (4000, 5000)


def test_timestamp_tokens_restore_punctuation_and_split_readable_sentences() -> None:
    chunk = AudioChunkPlan(
        chunk_index=0,
        start_frame=0,
        end_frame=64_000,
        start_ms=0,
        end_ms=4000,
    )
    payload = {
        "text": "甲乙。丙丁！",
        "language": "Chinese",
        "segments": [
            {"text": "甲", "start": 0.0, "end": 1.0},
            {"text": "乙", "start": 1.0, "end": 2.0},
            {"text": "丙", "start": 2.0, "end": 3.0},
            {"text": "丁", "start": 3.0, "end": 4.0},
        ],
    }

    segments = _result_segments(payload, chunk=chunk, source_duration_ms=4000)

    assert [item["text"] for item in segments] == ["甲乙。", "丙丁！"]
    assert [(item["start_ms"], item["end_ms"]) for item in segments] == [
        (0, 2000),
        (2000, 4000),
    ]


def test_zero_duration_sentence_boundary_does_not_overlap_following_text() -> None:
    chunk = AudioChunkPlan(
        chunk_index=0,
        start_frame=0,
        end_frame=48_000,
        start_ms=0,
        end_ms=3000,
    )
    payload = {
        "text": "甲。乙。",
        "language": "Chinese",
        "segments": [
            {"text": "甲", "start": 1.0, "end": 1.0},
            {"text": "乙", "start": 1.0, "end": 2.0},
        ],
    }

    segments = _result_segments(payload, chunk=chunk, source_duration_ms=3000)

    assert [item["text"] for item in segments] == ["甲。", "乙。"]
    assert [(item["start_ms"], item["end_ms"]) for item in segments] == [
        (1000, 1001),
        (1001, 2000),
    ]


def test_short_pause_between_sentences_is_accounted_without_merging_text() -> None:
    chunk = AudioChunkPlan(
        chunk_index=0,
        start_frame=0,
        end_frame=48_000,
        start_ms=0,
        end_ms=3000,
    )
    payload = {
        "text": "甲。乙。",
        "language": "Chinese",
        "segments": [
            {"text": "甲", "start": 0.0, "end": 1.0},
            {"text": "乙", "start": 1.4, "end": 2.4},
        ],
    }

    segments = _result_segments(payload, chunk=chunk, source_duration_ms=3000)

    assert [item["text"] for item in segments] == ["甲。", "乙。"]
    assert [(item["start_ms"], item["end_ms"]) for item in segments] == [
        (0, 1400),
        (1400, 2400),
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_executor_materializes_zero_duration_timestamp_segments(tmp_path) -> None:
    class ZeroDurationEngine(FakeEngine):
        def transcribe(self, audio, *, sample_rate, language_hint, context):
            duration = len(audio) / sample_rate
            return {
                "text": "甲乙丙",
                "language": "Chinese",
                "segments": [
                    {"text": "甲", "start": 0.0, "end": 1.0},
                    {"text": "乙", "start": 1.0, "end": 1.0},
                    {"text": "丙", "start": 1.0, "end": duration},
                ],
                "chunks": [
                    {
                        "text": "甲乙丙",
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

    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=2,
            suffix="zero-duration",
        )
        result = AsrChunkExecutor(
            store,
            ZeroDurationEngine(),
            boundary_preflight=preflight(),
        ).run_next(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)

    assert result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    assert "".join(segment.text or "" for segment in snapshot.stable_segments) == "甲乙丙"
    assert all(
        segment.end_ms > segment.start_ms for segment in snapshot.stable_segments
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_run_all_executes_every_chunk_and_finishes_transcription(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=95,
            suffix="batch-complete",
        )
        engine = FakeEngine()
        result = AsrChunkExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run_all(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)

    assert result.total_chunks > 1
    assert result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    assert result.job.state is JobState.ALIGNING
    assert result.completed_chunks == result.total_chunks
    assert result.attempts_used == result.total_chunks
    assert engine.calls == result.total_chunks
    assert snapshot.progress is not None
    assert snapshot.progress.processed_ms == 95000


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_run_all_respects_batch_limit_and_resumes_remaining_chunks(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=95,
            suffix="batch-limit",
        )
        engine = FakeEngine()
        executor = AsrChunkExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        )
        first = executor.run_all(job.job_id, max_chunks=2)

        assert first.outcome is AsrRunOutcome.BATCH_LIMIT_REACHED
        assert first.job.state is JobState.TRANSCRIBING
        assert first.completed_chunks == 2
        assert first.total_chunks > 2
        assert engine.calls == 2

        second = executor.run_all(job.job_id)

        assert second.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
        assert second.job.state is JobState.ALIGNING
        assert second.completed_chunks == second.total_chunks == first.total_chunks
        assert engine.calls == second.total_chunks


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_run_all_survives_worker_restart_between_batches(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(95),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=95,
            suffix="batch-restart",
        )
        first = AsrChunkExecutor(
            store,
            FakeEngine(),
            boundary_preflight=preflight(),
        ).run_all(job.job_id, max_chunks=1)

        assert first.outcome is AsrRunOutcome.BATCH_LIMIT_REACHED
        assert first.completed_chunks == 1
        assert first.total_chunks > 2

    with JobStore(database) as restarted:
        engine = FakeEngine()
        second = AsrChunkExecutor(
            restarted,
            engine,
            boundary_preflight=preflight(),
        ).run_all(job.job_id)

        assert second.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
        assert second.completed_chunks == second.total_chunks == first.total_chunks
        assert engine.calls == second.total_chunks - 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_run_all_safely_pauses_when_resources_block_next_chunk(tmp_path) -> None:
    class FlippingPreflight:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *_, **__):
            self.calls += 1
            return resource_report(
                ResourceStatus.READY if self.calls == 1 else ResourceStatus.BLOCKED
            )

    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=95,
            suffix="batch-pressure",
        )
        engine = FakeEngine()
        result = AsrChunkExecutor(
            store,
            engine,
            boundary_preflight=FlippingPreflight(),
        ).run_all(job.job_id)

        assert result.outcome is AsrRunOutcome.SAFE_PAUSED
        assert result.job.state is JobState.PAUSED
        assert result.job.last_error_code == "RESOURCE_BOUNDARY_BLOCKED"
        assert result.completed_chunks == 1
        assert result.total_chunks > 1
        assert engine.calls == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_run_all_retries_rejected_chunks_before_continuing(tmp_path) -> None:
    rejected = valid_result(np.zeros(16_000, dtype=np.int16))
    rejected["truncated"] = True
    rejected["finish_reason"] = "length"
    rejected["chunks"][0]["truncated"] = True
    rejected["chunks"][0]["finish_reason"] = "length"
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=95,
            suffix="batch-retry",
        )
        engine = FakeEngine([rejected, *[valid_result] * 8])
        result = AsrChunkExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run_all(job.job_id)
        attempts = store.list_asr_attempts(job.job_id)

        assert result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
        assert result.completed_chunks == result.total_chunks
        assert result.attempts_used == result.total_chunks + 1
        assert engine.calls == result.total_chunks + 1
        assert [attempt.state for attempt in attempts[:2]] == [
            AsrAttemptState.REJECTED,
            AsrAttemptState.SUCCEEDED,
        ]


def test_run_all_rejects_invalid_batch_limits(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        executor = AsrChunkExecutor(
            store,
            FakeEngine(),
            boundary_preflight=preflight(),
        )
        with pytest.raises(InvalidJobRequest):
            executor.run_all("any-job", max_chunks=0)
        with pytest.raises(InvalidJobRequest):
            executor.run_all("any-job", max_chunks=-1)


def test_raw_attempt_is_private_idempotent_immutable_and_checksum_verified(
    tmp_path,
) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(1),
    ) as store:
        job = create_preprocessing_job(
            store,
            duration_seconds=1,
            suffix="raw",
        )
        store.transition_job(
            job.job_id,
            JobState.TRANSCRIBING,
            expected_revision=job.revision,
        )
        payload = {"text": "private raw transcript", "finish_reason": "stop"}
        first, first_created = store.commit_asr_attempt(
            job.job_id,
            chunk_index=0,
            attempt_number=1,
            attempt_key="chunk_00000000_attempt_0001",
            state=AsrAttemptState.SUCCEEDED,
            model_id="fake/local-asr",
            start_frame=0,
            end_frame=16_000,
            start_ms=0,
            end_ms=1000,
            raw_payload=payload,
            finish_reason="stop",
        )
        repeated, repeated_created = store.commit_asr_attempt(
            job.job_id,
            chunk_index=0,
            attempt_number=1,
            attempt_key="chunk_00000000_attempt_0001",
            state=AsrAttemptState.SUCCEEDED,
            model_id="fake/local-asr",
            start_frame=0,
            end_frame=16_000,
            start_ms=0,
            end_ms=1000,
            raw_payload=payload,
            finish_reason="stop",
        )
        raw_path = store.data_directory / first.raw_relative_path

        with pytest.raises(AsrAttemptConflict):
            store.commit_asr_attempt(
                job.job_id,
                chunk_index=0,
                attempt_number=1,
                attempt_key="chunk_00000000_attempt_0001",
                state=AsrAttemptState.SUCCEEDED,
                model_id="fake/local-asr",
                start_frame=0,
                end_frame=16_000,
                start_ms=0,
                end_ms=1000,
                raw_payload={"text": "changed"},
                finish_reason="stop",
            )

        assert stat.S_IMODE(raw_path.stat().st_mode) & 0o077 == 0
        raw_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(UploadStorageError, match="checksum"):
            store.get_asr_attempt_payload(
                job.job_id,
                chunk_index=0,
                attempt_number=1,
            )

    assert first_created is True
    assert repeated_created is False
    assert first == repeated


def test_concurrent_identical_raw_attempt_commit_creates_one_record(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(1),
    ) as bootstrap:
        job = create_preprocessing_job(
            bootstrap,
            duration_seconds=1,
            suffix="raw-race",
        )
        bootstrap.transition_job(
            job.job_id,
            JobState.TRANSCRIBING,
            expected_revision=job.revision,
        )

    first_store = JobStore(database)
    second_store = JobStore(database)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def commit(store):
        try:
            barrier.wait(timeout=5)
            results.append(
                store.commit_asr_attempt(
                    job.job_id,
                    chunk_index=0,
                    attempt_number=1,
                    attempt_key="chunk_00000000_attempt_0001",
                    state=AsrAttemptState.SUCCEEDED,
                    model_id="fake/local-asr",
                    start_frame=0,
                    end_frame=16_000,
                    start_ms=0,
                    end_ms=1000,
                    raw_payload={"text": "same private result"},
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=commit, args=(first_store,))
    second = threading.Thread(target=commit, args=(second_store,))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    attempts = first_store.list_asr_attempts(job.job_id)
    first_store.close()
    second_store.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sum(created for _, created in results) == 1
    assert len(attempts) == 1
