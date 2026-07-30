import hashlib
import io
import shutil
import stat
import wave

import numpy as np
import pytest

from speech_capture_worker.audio_preprocessing import (
    CHECKPOINT_KEY,
    AudioPreprocessor,
    plan_wav_chunks,
)
from speech_capture_worker.domain import JobState, UploadCreateRequest
from speech_capture_worker.errors import (
    AudioNormalizationUnavailable,
    InvalidJobRequest,
    NormalizedAudioInvalid,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult


def wav_bytes(*, duration_seconds: float, sample_rate: int = 16_000) -> bytes:
    frame_count = round(duration_seconds * sample_rate)
    time = np.arange(frame_count, dtype=np.float64) / sample_rate
    samples = (np.sin(2 * np.pi * 440 * time) * 4000).astype("<i2")
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


def create_job(
    store: JobStore,
    *,
    content: bytes,
    suffix: str,
    claim: bool = True,
):
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"source-{suffix}.wav",
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
    job, _ = store.create_job_from_upload(
        upload.upload_id,
        idempotency_key=f"job-{suffix}",
    )
    if claim:
        job = store.claim_job_for_processing(
            job.job_id,
            expected_revision=job.revision,
        )
    return job


def write_normalized_wav(path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(samples.astype("<i2").tobytes())


def test_energy_chunk_plan_is_contiguous_bounded_and_prefers_quiet_boundaries(
    tmp_path,
) -> None:
    sample_rate = 16_000
    duration_seconds = 65
    samples = np.full(duration_seconds * sample_rate, 3000, dtype=np.int16)
    for center_seconds in (28, 56):
        start = round((center_seconds - 0.2) * sample_rate)
        end = round((center_seconds + 0.2) * sample_rate)
        samples[start:end] = 0
    path = tmp_path / "normalized.wav"
    write_normalized_wav(path, samples)

    chunks = plan_wav_chunks(path)

    assert chunks[0].start_frame == 0
    assert chunks[-1].end_frame == len(samples)
    assert all(
        left.end_frame == right.start_frame
        for left, right in zip(chunks, chunks[1:])
    )
    assert all(chunk.duration_ms <= 30_001 for chunk in chunks)
    assert abs(chunks[0].end_ms - 28_000) <= 250
    assert abs(chunks[1].end_ms - 56_000) <= 250


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_normalization_plan_is_private_idempotent_and_survives_reopen(tmp_path) -> None:
    database = tmp_path / "runtime" / "worker.sqlite3"
    content = wav_bytes(duration_seconds=3)
    with JobStore(
        database,
        source_probe=source_probe_for(3),
    ) as store:
        job = create_job(store, content=content, suffix="idempotent")
        preprocessor = AudioPreprocessor(store)
        first, first_created = preprocessor.prepare(job.job_id)
        repeated, repeated_created = preprocessor.prepare(job.job_id)
        normalized_path = preprocessor.get_normalized_path(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="preprocessing")

    with JobStore(database) as reopened:
        restored = AudioPreprocessor(reopened).get_plan(job.job_id)

    assert job.state is JobState.PREPROCESSING
    assert first_created is True
    assert repeated_created is False
    assert repeated == first == restored
    assert first.relative_path.startswith(f"jobs/{job.job_id}/preprocessing/")
    assert not first.relative_path.startswith("/")
    assert first.duration_ms == 3000
    assert len(first.chunks) == 1
    assert checkpoints[0].checkpoint_key == CHECKPOINT_KEY
    assert stat.S_IMODE(normalized_path.stat().st_mode) & 0o077 == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_corrupt_normalized_audio_is_rebuilt_from_verified_source(tmp_path) -> None:
    content = wav_bytes(duration_seconds=2)
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(2),
    ) as store:
        job = create_job(store, content=content, suffix="rebuild")
        preprocessor = AudioPreprocessor(store)
        first, _ = preprocessor.prepare(job.job_id)
        normalized_path = preprocessor.get_normalized_path(job.job_id)
        normalized_path.write_bytes(b"corrupt")

        rebuilt, changed = preprocessor.prepare(job.job_id)
        checkpoint = store.list_checkpoints(job.job_id, stage="preprocessing")[0]

    assert changed is True
    assert rebuilt.normalized_sha256 == first.normalized_sha256
    assert checkpoint.generation == 1


def test_normalization_requires_preprocessing_state(tmp_path) -> None:
    content = wav_bytes(duration_seconds=1)
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        queued = create_job(
            store,
            content=content,
            suffix="queued",
            claim=False,
        )

        with pytest.raises(InvalidJobRequest, match="preprocessing"):
            AudioPreprocessor(store).prepare(queued.job_id)


def test_missing_ffmpeg_returns_stable_safe_error(tmp_path) -> None:
    content = wav_bytes(duration_seconds=1)
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(1),
    ) as store:
        job = create_job(store, content=content, suffix="missing-ffmpeg")

        with pytest.raises(AudioNormalizationUnavailable) as caught:
            AudioPreprocessor(
                store,
                ffmpeg_executable="/missing/speech-capture-ffmpeg",
            ).prepare(job.job_id)

    assert caught.value.code == "AUDIO_NORMALIZATION_UNAVAILABLE"
    assert str(tmp_path) not in caught.value.message


def test_plan_validation_rejects_non_normalized_wav(tmp_path) -> None:
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44_100)
        audio.writeframes(b"\x00\x00" * 2 * 100)

    with pytest.raises(NormalizedAudioInvalid):
        plan_wav_chunks(path)
