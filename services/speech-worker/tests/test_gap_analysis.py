import hashlib
import io
import json
import shutil
import wave

import numpy as np
import pytest

from speech_capture_worker.alignment import (
    ALIGNMENT_REPORT_SCHEMA_VERSION,
    AlignmentFinalizationOutcome,
    TimelineRange,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor
from speech_capture_worker.audio_preprocessing import (
    CHECKPOINT_KEY as AUDIO_PLAN_CHECKPOINT_KEY,
)
from speech_capture_worker.audio_preprocessing import (
    CHECKPOINT_STAGE as AUDIO_PLAN_CHECKPOINT_STAGE,
)
from speech_capture_worker.audio_preprocessing import (
    NORMALIZED_AUDIO_SCHEMA_VERSION,
    NORMALIZED_FILENAME,
    AudioChunkPlan,
    NormalizedAudioPlan,
)
from speech_capture_worker.domain import (
    JobState,
    ResourceStatus,
    UploadCreateRequest,
)
from speech_capture_worker.errors import (
    InvalidJobRequest,
    NormalizedAudioInvalid,
    TranscriptConflict,
)
from speech_capture_worker.gap_analysis import (
    GAP_ANALYSIS_CHECKPOINT_KEY,
    DefiniteSilenceMaterializer,
    GapEvidenceClassification,
    SilenceMaterializationOutcome,
    TranscriptGapAnalyzer,
    _analyze_wav_ranges,
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


def wav_bytes() -> bytes:
    sample_rate = 16_000
    samples = np.zeros(sample_rate, dtype="<i2")
    time = np.arange(sample_rate // 4, dtype=np.float64) / sample_rate
    samples[-sample_rate // 4 :] = (np.sin(2 * np.pi * 330 * time) * 3000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())
    return output.getvalue()


def source_probe(_):
    return MediaProbeResult(
        duration_seconds=1,
        audio_stream_count=1,
        format_name="wav",
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


class FakeGapEngine:
    model_id = "fake/local-asr"

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        assert sample_rate == 16_000
        duration = len(audio) / sample_rate
        return {
            "text": "private transcript",
            "language": "English",
            "segments": [
                {
                    "text": "private transcript",
                    "start": 0.75,
                    "end": duration,
                }
            ],
            "chunks": [
                {
                    "text": "private transcript",
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


def create_aligning_job(
    store: JobStore,
    *,
    ranges: list[tuple[int, int]] | None,
    transcribed_ranges: list[tuple[int, int]] | None = None,
):
    content = wav_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name="gap-analysis.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key="gap-analysis-upload",
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
        idempotency_key="gap-analysis-job",
    )
    preprocessing = store.claim_job_for_processing(
        queued.job_id,
        expected_revision=queued.revision,
    )

    stage_directory = store.get_job_stage_directory(
        queued.job_id,
        stage=AUDIO_PLAN_CHECKPOINT_STAGE,
    )
    normalized_path = stage_directory / NORMALIZED_FILENAME
    normalized_path.write_bytes(content)
    plan = NormalizedAudioPlan(
        schema_version=NORMALIZED_AUDIO_SCHEMA_VERSION,
        algorithm="pcm16_mono_energy_boundary_v1",
        relative_path=normalized_path.relative_to(store.data_directory).as_posix(),
        source_sha256=checksum,
        normalized_sha256=checksum,
        normalized_size_bytes=len(content),
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
        total_frames=16_000,
        duration_ms=1000,
        max_chunk_ms=30_000,
        min_chunk_ms=5_000,
        boundary_search_ms=3_000,
        energy_window_ms=100,
        chunks=(
            AudioChunkPlan(
                chunk_index=0,
                start_frame=0,
                end_frame=16_000,
                start_ms=0,
                end_ms=1000,
            ),
        ),
    )
    store.put_checkpoint(
        queued.job_id,
        stage=AUDIO_PLAN_CHECKPOINT_STAGE,
        checkpoint_key=AUDIO_PLAN_CHECKPOINT_KEY,
        payload=plan.to_dict(),
    )
    transcribing = store.transition_job(
        queued.job_id,
        JobState.TRANSCRIBING,
        expected_revision=preprocessing.revision,
    )
    for index, (start_ms, end_ms) in enumerate(transcribed_ranges or []):
        store.commit_transcript_segment(
            queued.job_id,
            commit_key=f"test_transcribed_{index:04d}",
            start_ms=start_ms,
            end_ms=end_ms,
            outcome=TranscriptOutcome.TRANSCRIBED,
            text=f"private transcript {index}",
            timing_status=TranscriptTimingStatus.ALIGNED,
            speaker_label_status=SpeakerLabelStatus.PENDING,
        )
    aligning = store.transition_job(
        queued.job_id,
        JobState.ALIGNING,
        expected_revision=transcribing.revision,
    )
    if ranges is not None:
        put_alignment_report(store, queued.job_id, ranges)
    return aligning, normalized_path


def put_alignment_report(
    store: JobStore,
    job_id: str,
    ranges: list[tuple[int, int]],
):
    return store.put_checkpoint(
        job_id,
        stage="aligning",
        checkpoint_key="transcript_alignment_report",
        payload={
            "schema_version": ALIGNMENT_REPORT_SCHEMA_VERSION,
            "source_duration_ms": 1000,
            "unresolved_duration_ms": sum(end - start for start, end in ranges),
            "unresolved_ranges": [{"start_ms": start, "end_ms": end} for start, end in ranges],
        },
    )[0]


def test_gap_analysis_classifies_only_definite_silence_and_persists_safe_evidence(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(
            store,
            ranges=[(0, 250), (750, 1000)],
        )
        result = TranscriptGapAnalyzer(store).analyze(job.job_id)
        checkpoint = next(
            value
            for value in store.list_checkpoints(job.job_id, stage="aligning")
            if value.checkpoint_key == GAP_ANALYSIS_CHECKPOINT_KEY
        )

    assert result.job.state is JobState.ALIGNING
    assert result.report.gap_count == 2
    assert result.report.definite_silence_count == 1
    assert result.report.unresolved_count == 1
    assert [item.classification for item in result.report.evidence] == [
        GapEvidenceClassification.DEFINITE_SILENCE,
        GapEvidenceClassification.UNRESOLVED,
    ]
    assert result.report.evidence[0].reason_code == "PCM_NEAR_DIGITAL_SILENCE"
    assert result.report.evidence[0].peak_absolute_amplitude == 0
    assert result.report.evidence[1].reason_code == "AUDIBLE_OR_UNCERTAIN_PCM"
    assert result.report.evidence[1].peak_absolute_amplitude > 1000
    assert checkpoint.payload == result.report.to_dict()
    assert "gap-analysis.wav" not in json.dumps(checkpoint.payload)


def test_gap_analysis_is_idempotent_and_anchored_to_alignment_generation(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(store, ranges=[(0, 250), (750, 1000)])
        first = TranscriptGapAnalyzer(store).analyze(job.job_id)
        second = TranscriptGapAnalyzer(store).analyze(job.job_id)
        revised_alignment = put_alignment_report(store, job.job_id, [(0, 250)])
        third = TranscriptGapAnalyzer(store).analyze(job.job_id)

    assert first.checkpoint_generation == 1
    assert second.checkpoint_generation == 1
    assert first.report == second.report
    assert first.report.alignment_report_generation == 1
    assert third.checkpoint_generation == 2
    assert third.report.alignment_report_generation == 2
    assert third.report.alignment_report_sha256 == revised_alignment.payload_sha256
    assert third.report.gap_count == 1


def test_short_digital_silence_remains_unresolved(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(store, ranges=[(0, 50)])
        result = TranscriptGapAnalyzer(store).analyze(job.job_id)

    assert result.report.definite_silence_count == 0
    assert result.report.unresolved_count == 1
    assert result.report.evidence[0].classification is (GapEvidenceClassification.UNRESOLVED)
    assert result.report.evidence[0].reason_code == "GAP_TOO_SHORT_FOR_SILENCE_CLAIM"


def test_gap_analysis_does_not_claim_silence_without_full_pcm_coverage(
    tmp_path,
) -> None:
    path = tmp_path / "short-normalized.wav"
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(np.zeros(14_400, dtype="<i2").tobytes())
    path.write_bytes(output.getvalue())

    evidence = _analyze_wav_ranges(
        path,
        ranges=(TimelineRange(start_ms=850, end_ms=1000),),
        sample_rate=16_000,
        total_frames=14_400,
        window_ms=20,
        minimum_definite_silence_ms=100,
        definite_silence_peak_threshold=8,
    )

    assert evidence[0].classification is GapEvidenceClassification.UNRESOLVED
    assert evidence[0].reason_code == "PCM_RANGE_UNAVAILABLE"
    assert evidence[0].frame_count == 800


@pytest.mark.parametrize(
    ("ranges", "payload_update"),
    [
        ([(0, 250)], {"unresolved_duration_ms": 249}),
        ([(0, 250)], {"source_duration_ms": 1000.0}),
        ([(0, 250)], {"unresolved_ranges": [{"start_ms": False, "end_ms": 250}]}),
        (
            [(0, 250), (750, 1000)],
            {
                "unresolved_ranges": [
                    {"start_ms": 750, "end_ms": 1000},
                    {"start_ms": 0, "end_ms": 250},
                ]
            },
        ),
    ],
)
def test_gap_analysis_rejects_invalid_alignment_report(
    tmp_path,
    ranges,
    payload_update,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(store, ranges=ranges)
        checkpoint = next(
            value
            for value in store.list_checkpoints(job.job_id, stage="aligning")
            if value.checkpoint_key == "transcript_alignment_report"
        )
        payload = dict(checkpoint.payload)
        payload.update(payload_update)
        store.put_checkpoint(
            job.job_id,
            stage="aligning",
            checkpoint_key="transcript_alignment_report",
            payload=payload,
        )

        with pytest.raises(InvalidJobRequest):
            TranscriptGapAnalyzer(store).analyze(job.job_id)


def test_gap_analysis_requires_alignment_report_and_unchanged_normalized_audio(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, normalized_path = create_aligning_job(store, ranges=None)
        with pytest.raises(InvalidJobRequest):
            TranscriptGapAnalyzer(store).analyze(job.job_id)

        put_alignment_report(store, job.job_id, [(0, 250)])
        content = bytearray(normalized_path.read_bytes())
        content[-1] ^= 1
        normalized_path.write_bytes(content)
        with pytest.raises(NormalizedAudioInvalid):
            TranscriptGapAnalyzer(store).analyze(job.job_id)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_ms": 4},
        {"window_ms": True},
        {"window_ms": 20, "minimum_definite_silence_ms": 19},
        {"definite_silence_peak_threshold": 32_768},
    ],
)
def test_gap_analysis_rejects_unsafe_measurement_policy(tmp_path, kwargs) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        with pytest.raises(InvalidJobRequest):
            TranscriptGapAnalyzer(store, **kwargs)


def test_cli_analyzes_gaps_with_machine_readable_output(tmp_path, capsys) -> None:
    data_path = tmp_path / "runtime"
    with JobStore(
        data_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(
            store,
            ranges=[(0, 250), (750, 1000)],
        )

    exit_code = main(
        [
            "analyze-gaps",
            "--data-dir",
            str(data_path),
            job.job_id,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["job"]["state"] == "aligning"
    assert payload["report"]["definite_silence_count"] == 1
    assert payload["report"]["unresolved_count"] == 1
    assert [item["classification"] for item in payload["report"]["evidence"]] == [
        "definite_silence",
        "unresolved",
    ]
    assert payload["checkpoint_generation"] == 1


def test_materializer_backfills_proven_silence_and_refreshes_alignment(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(
            store,
            ranges=[(0, 250), (750, 1000)],
            transcribed_ranges=[(250, 750)],
        )
        first = DefiniteSilenceMaterializer(store).materialize(job.job_id)
        first_snapshot = store.get_job_snapshot(job.job_id)
        evidence_checkpoints = [
            value
            for value in store.list_checkpoints(job.job_id, stage="aligning")
            if value.checkpoint_key.startswith("gap_silence_")
        ]
        second = DefiniteSilenceMaterializer(store).materialize(job.job_id)
        second_snapshot = store.get_job_snapshot(job.job_id)
        current_gap_checkpoint = next(
            value
            for value in store.list_checkpoints(job.job_id, stage="aligning")
            if value.checkpoint_key == GAP_ANALYSIS_CHECKPOINT_KEY
        )

    first_timeline = sorted(
        first_snapshot.stable_segments,
        key=lambda value: value.start_ms,
    )
    assert first.outcome is SilenceMaterializationOutcome.MATERIALIZED
    assert first.created_segment_count == 1
    assert first.materializations[0].segment.outcome is TranscriptOutcome.NON_SPEECH
    assert first.materializations[0].segment.timing_status is (TranscriptTimingStatus.ALIGNED)
    assert first.materializations[0].segment.speaker_label_status is (
        SpeakerLabelStatus.UNAVAILABLE
    )
    assert [(value.start_ms, value.end_ms, value.outcome) for value in first_timeline] == [
        (0, 250, TranscriptOutcome.NON_SPEECH),
        (250, 750, TranscriptOutcome.TRANSCRIBED),
    ]
    assert first.alignment.outcome is AlignmentFinalizationOutcome.EVIDENCE_INCOMPLETE
    assert [value.to_dict() for value in first.alignment.report.unresolved_ranges] == [
        {"start_ms": 750, "end_ms": 1000}
    ]
    assert len(evidence_checkpoints) == 1
    assert evidence_checkpoints[0].generation == 1
    assert (
        evidence_checkpoints[0].payload["gap_analysis_sha256"]
        != current_gap_checkpoint.payload_sha256
    )
    assert second.outcome is SilenceMaterializationOutcome.NO_DEFINITE_SILENCE
    assert second.created_segment_count == 0
    assert len(second_snapshot.stable_segments) == 2


def test_materializer_can_insert_silence_between_existing_segments(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(
            store,
            ranges=[(250, 750)],
            transcribed_ranges=[(0, 250), (750, 1000)],
        )
        result = DefiniteSilenceMaterializer(store).materialize(job.job_id)
        snapshot = store.get_job_snapshot(job.job_id)

    timeline = sorted(snapshot.stable_segments, key=lambda value: value.start_ms)
    assert result.created_segment_count == 1
    assert [value.segment_sequence for value in timeline] == [1, 3, 2]
    assert [(value.start_ms, value.end_ms, value.outcome) for value in timeline] == [
        (0, 250, TranscriptOutcome.TRANSCRIBED),
        (250, 750, TranscriptOutcome.NON_SPEECH),
        (750, 1000, TranscriptOutcome.TRANSCRIBED),
    ]
    assert result.alignment.report.timeline_accounted is True
    assert result.alignment.report.transcript_complete is True
    assert result.alignment.report.unresolved_ranges == ()
    assert result.job.state is JobState.ALIGNING


def test_silence_commit_rejects_audible_or_stale_gap_evidence(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(
            store,
            ranges=[(0, 250), (750, 1000)],
            transcribed_ranges=[(250, 750)],
        )
        unsafe_analysis = TranscriptGapAnalyzer(
            store,
            definite_silence_peak_threshold=32_767,
        ).analyze(job.job_id)
        unsafe_checkpoint = next(
            value
            for value in store.list_checkpoints(job.job_id, stage="aligning")
            if value.checkpoint_key == GAP_ANALYSIS_CHECKPOINT_KEY
        )
        with pytest.raises(InvalidJobRequest, match="not safe"):
            store.commit_definite_silence_segment(
                job.job_id,
                commit_key="gap_silence_custom_policy",
                start_ms=750,
                end_ms=1000,
                gap_analysis_generation=unsafe_analysis.checkpoint_generation,
                gap_analysis_sha256=unsafe_checkpoint.payload_sha256,
            )

        analysis = TranscriptGapAnalyzer(store).analyze(job.job_id)
        gap_checkpoint = next(
            value
            for value in store.list_checkpoints(job.job_id, stage="aligning")
            if value.checkpoint_key == GAP_ANALYSIS_CHECKPOINT_KEY
        )

        with pytest.raises(InvalidJobRequest, match="not proven"):
            store.commit_definite_silence_segment(
                job.job_id,
                commit_key="gap_silence_audible",
                start_ms=750,
                end_ms=1000,
                gap_analysis_generation=analysis.checkpoint_generation,
                gap_analysis_sha256=gap_checkpoint.payload_sha256,
            )

        transcribed = store.get_job_snapshot(job.job_id).stable_segments[0]
        store.update_transcript_segment_metadata(
            job.job_id,
            transcribed.segment_id,
            expected_revision=transcribed.revision,
            start_ms=200,
            timing_status=TranscriptTimingStatus.ALIGNED,
        )
        with pytest.raises(TranscriptConflict, match="overlap"):
            store.commit_definite_silence_segment(
                job.job_id,
                commit_key="gap_silence_overlap",
                start_ms=0,
                end_ms=250,
                gap_analysis_generation=analysis.checkpoint_generation,
                gap_analysis_sha256=gap_checkpoint.payload_sha256,
            )

        put_alignment_report(store, job.job_id, [(0, 200), (750, 1000)])
        with pytest.raises(InvalidJobRequest, match="changed after gap analysis"):
            store.commit_definite_silence_segment(
                job.job_id,
                commit_key="gap_silence_stale",
                start_ms=0,
                end_ms=250,
                gap_analysis_generation=analysis.checkpoint_generation,
                gap_analysis_sha256=gap_checkpoint.payload_sha256,
            )


def test_materializer_repairs_interruption_after_segment_commit(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(
            store,
            ranges=[(0, 250), (750, 1000)],
            transcribed_ranges=[(250, 750)],
        )
        analysis = TranscriptGapAnalyzer(store).analyze(job.job_id)
        gap_checkpoint = next(
            value
            for value in store.list_checkpoints(job.job_id, stage="aligning")
            if value.checkpoint_key == GAP_ANALYSIS_CHECKPOINT_KEY
        )
        interrupted_segment, created = store.commit_definite_silence_segment(
            job.job_id,
            commit_key="gap_silence_000000000000_000000000250",
            start_ms=0,
            end_ms=250,
            gap_analysis_generation=analysis.checkpoint_generation,
            gap_analysis_sha256=gap_checkpoint.payload_sha256,
        )

        repaired = DefiniteSilenceMaterializer(store).materialize(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="aligning")

    assert created is True
    assert repaired.outcome is SilenceMaterializationOutcome.MATERIALIZED
    assert repaired.created_segment_count == 0
    assert repaired.materializations[0].segment.segment_id == interrupted_segment.segment_id
    assert any(
        value.checkpoint_key
        == "gap_silence_000000000000_000000000250_materialized"
        for value in checkpoints
    )
    assert [
        value.to_dict() for value in repaired.alignment.report.unresolved_ranges
    ] == [{"start_ms": 750, "end_ms": 1000}]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_materialized_silence_can_complete_alignment_exit_gate(tmp_path) -> None:
    content = wav_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        upload, _ = store.create_upload(
            UploadCreateRequest(
                vault_id="vault_primary",
                source_display_name="gap-exit.wav",
                source_sha256=checksum,
                source_size_bytes=len(content),
                media_type="audio/wav",
            ),
            idempotency_key="gap-exit-upload",
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
            idempotency_key="gap-exit-job",
        )
        preprocessing = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )
        transcribed = AsrChunkExecutor(
            store,
            FakeGapEngine(),
            boundary_preflight=ready_preflight,
        ).run_next(preprocessing.job_id)
        incomplete = TranscriptAlignmentFinalizer(store).finalize(
            preprocessing.job_id
        )
        completed = DefiniteSilenceMaterializer(store).materialize(
            preprocessing.job_id
        )
        repeated = DefiniteSilenceMaterializer(store).materialize(
            preprocessing.job_id
        )

    assert transcribed.job.state is JobState.ALIGNING
    assert incomplete.outcome is AlignmentFinalizationOutcome.TIMELINE_INCOMPLETE
    assert [value.to_dict() for value in incomplete.report.unresolved_ranges] == [
        {"start_ms": 0, "end_ms": 750}
    ]
    assert completed.created_segment_count == 1
    assert completed.alignment.outcome is (
        AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
    )
    assert completed.alignment.report.timeline_accounted is True
    assert completed.alignment.report.transcript_complete is True
    assert completed.job.state is JobState.DIARIZING
    assert repeated.outcome is SilenceMaterializationOutcome.ALREADY_FINALIZED
    assert repeated.created_segment_count == 0
    assert repeated.job.state is JobState.DIARIZING


def test_cli_materializes_default_policy_silence(tmp_path, capsys) -> None:
    data_path = tmp_path / "runtime"
    with JobStore(
        data_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job, _ = create_aligning_job(
            store,
            ranges=[(0, 250), (750, 1000)],
            transcribed_ranges=[(250, 750)],
        )

    exit_code = main(
        [
            "materialize-silence",
            "--data-dir",
            str(data_path),
            job.job_id,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["outcome"] == "materialized"
    assert payload["created_segment_count"] == 1
    assert payload["materializations"][0]["segment"]["outcome"] == "non_speech"
    assert payload["alignment"]["report"]["unresolved_ranges"] == [
        {"start_ms": 750, "end_ms": 1000}
    ]
    assert "private transcript" not in json.dumps(payload)
