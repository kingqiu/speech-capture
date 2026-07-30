import hashlib
import io
import json
import wave

import numpy as np
import pytest

from speech_capture_worker.alignment import ALIGNMENT_REPORT_SCHEMA_VERSION
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
from speech_capture_worker.domain import JobState, UploadCreateRequest
from speech_capture_worker.errors import InvalidJobRequest, NormalizedAudioInvalid
from speech_capture_worker.gap_analysis import (
    GAP_ANALYSIS_CHECKPOINT_KEY,
    GapEvidenceClassification,
    TranscriptGapAnalyzer,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
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


def create_aligning_job(
    store: JobStore,
    *,
    ranges: list[tuple[int, int]] | None,
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
    assert result.report.evidence[0].classification is (
        GapEvidenceClassification.UNRESOLVED
    )
    assert (
        result.report.evidence[0].reason_code
        == "GAP_TOO_SHORT_FOR_SILENCE_CLAIM"
    )


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
