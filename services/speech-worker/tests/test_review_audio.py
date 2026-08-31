"""Private review-audio generation, verification, authorization, and Range tests."""

from __future__ import annotations

import hashlib
import io
import stat
import wave

import numpy as np
from fastapi.testclient import TestClient

from speech_capture_worker.api import create_app
from speech_capture_worker.api_auth import ApiCredential, ApiPrincipal, CredentialVerifier
from speech_capture_worker.audio_preprocessing import (
    CHECKPOINT_KEY,
    CHECKPOINT_STAGE,
    NORMALIZED_FILENAME,
    AudioChunkPlan,
    NormalizedAudioPlan,
)
from speech_capture_worker.domain import JobCreateRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.review_audio import (
    REVIEW_AUDIO_CHECKPOINT_KEY,
    REVIEW_AUDIO_STAGE,
    ReviewAudioManager,
)

TOKEN = "review-token-abcdefghijklmnopqrstuvwxyz0123456789"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def _normalized_wav(*, duration_ms: int) -> bytes:
    sample_rate = 16_000
    frame_count = round(duration_ms * sample_rate / 1000)
    time = np.arange(frame_count, dtype=np.float64) / sample_rate
    samples = (np.sin(2 * np.pi * 440 * time) * 8_000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())
    return output.getvalue()


def _verifier(vault_id: str) -> CredentialVerifier:
    principal = ApiPrincipal(
        device_id="device_review",
        allowed_vault_ids=frozenset({vault_id}),
    )
    return CredentialVerifier((ApiCredential.from_plaintext(TOKEN, principal),))


def _job_with_normalized_audio(
    store: JobStore,
    *,
    duration_ms: int = 3_000,
) -> tuple[str, NormalizedAudioPlan, bytes]:
    content = _normalized_wav(duration_ms=duration_ms)
    source_sha256 = hashlib.sha256(content).hexdigest()
    job, _ = store.create_job(
        JobCreateRequest(
            vault_id="vault_primary",
            source_display_name="synthetic-review.wav",
            source_sha256=source_sha256,
            source_size_bytes=len(content),
        ),
        idempotency_key="review-job",
    )
    stage = store.get_job_stage_directory(job.job_id, stage=CHECKPOINT_STAGE)
    normalized_path = stage / NORMALIZED_FILENAME
    normalized_path.write_bytes(content)
    normalized_path.chmod(0o600)
    frame_count = duration_ms * 16
    plan = NormalizedAudioPlan(
        schema_version="1.0.0",
        algorithm="pcm16_mono_energy_boundary_v1",
        relative_path=normalized_path.relative_to(store.data_directory).as_posix(),
        source_sha256=source_sha256,
        normalized_sha256=source_sha256,
        normalized_size_bytes=len(content),
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
        total_frames=frame_count,
        duration_ms=duration_ms,
        max_chunk_ms=30_000,
        min_chunk_ms=5_000,
        boundary_search_ms=3_000,
        energy_window_ms=100,
        chunks=(
            AudioChunkPlan(
                chunk_index=0,
                start_frame=0,
                end_frame=frame_count,
                start_ms=0,
                end_ms=duration_ms,
            ),
        ),
    )
    store.put_checkpoint(
        job.job_id,
        stage=CHECKPOINT_STAGE,
        checkpoint_key=CHECKPOINT_KEY,
        payload=plan.to_dict(),
    )
    return job.job_id, plan, content


def test_review_audio_is_private_idempotent_and_preserves_timeline(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job_id, plan, normalized = _job_with_normalized_audio(store)
        normalized_path = store.data_directory / plan.relative_path
        manager = ReviewAudioManager(store)

        first, created = manager.prepare(
            job_id,
            normalized_path=normalized_path,
            normalized_sha256=plan.normalized_sha256,
            duration_ms=plan.duration_ms,
        )
        repeated, repeated_created = manager.prepare(
            job_id,
            normalized_path=normalized_path,
            normalized_sha256=plan.normalized_sha256,
            duration_ms=plan.duration_ms,
        )
        review_path = manager.path_for(job_id, first)

    assert created is True
    assert repeated_created is False
    assert repeated == first
    assert first.duration_ms == plan.duration_ms
    assert first.sample_rate == 8_000
    assert first.channels == 1
    assert first.bits_per_sample == 8
    assert first.size_bytes < len(normalized) // 2
    assert stat.S_IMODE(review_path.stat().st_mode) & 0o077 == 0


def test_review_audio_api_requires_vault_auth_and_supports_ranges(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job_id, plan, _ = _job_with_normalized_audio(store)
        normalized_path = store.data_directory / plan.relative_path
        manager = ReviewAudioManager(store)
        descriptor, _ = manager.prepare(
            job_id,
            normalized_path=normalized_path,
            normalized_sha256=plan.normalized_sha256,
            duration_ms=plan.duration_ms,
        )
        client = TestClient(
            create_app(store=store, credential_verifier=_verifier("vault_primary"))
        )

        missing_auth = client.get(f"/v1/jobs/{job_id}/review-audio")
        metadata = client.get(
            f"/v1/jobs/{job_id}/review-audio",
            headers=AUTHORIZATION,
        )
        ranged = client.get(
            f"/v1/jobs/{job_id}/review-audio/content",
            headers={**AUTHORIZATION, "Range": "bytes=0-31"},
        )
        invalid_range = client.get(
            f"/v1/jobs/{job_id}/review-audio/content",
            headers={**AUTHORIZATION, "Range": "bytes=999999-1000000"},
        )
        denied = TestClient(
            create_app(store=store, credential_verifier=_verifier("vault_other"))
        ).get(f"/v1/jobs/{job_id}/review-audio", headers=AUTHORIZATION)

    assert missing_auth.status_code == 401
    assert metadata.status_code == 200
    assert metadata.json() == {
        "job_id": job_id,
        "status": "available",
        "media_type": "audio/wav",
        "size_bytes": descriptor.size_bytes,
        "sha256": descriptor.sha256,
        "duration_ms": 3_000,
        "sample_rate": 8_000,
        "channels": 1,
        "bits_per_sample": 8,
        "accept_ranges": "bytes",
        "content_path": f"/v1/jobs/{job_id}/review-audio/content",
        "retention": "job_lifetime",
    }
    assert ranged.status_code == 206
    assert len(ranged.content) == 32
    assert ranged.headers["accept-ranges"] == "bytes"
    assert ranged.headers["content-range"] == f"bytes 0-31/{descriptor.size_bytes}"
    assert invalid_range.status_code == 416
    assert invalid_range.headers["content-range"] == f"bytes */{descriptor.size_bytes}"
    assert denied.status_code == 404


def test_review_audio_tampering_is_rejected_without_leaking_content(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job_id, plan, _ = _job_with_normalized_audio(store)
        normalized_path = store.data_directory / plan.relative_path
        manager = ReviewAudioManager(store)
        descriptor, _ = manager.prepare(
            job_id,
            normalized_path=normalized_path,
            normalized_sha256=plan.normalized_sha256,
            duration_ms=plan.duration_ms,
        )
        review_path = manager.path_for(job_id, descriptor)
        review_path.write_bytes(b"private tampered audio")
        response = TestClient(
            create_app(store=store, credential_verifier=_verifier("vault_primary"))
        ).get(f"/v1/jobs/{job_id}/review-audio", headers=AUTHORIZATION)
        review_path.unlink()
        outside = tmp_path / "outside-review.wav"
        outside.write_bytes(b"private outside audio")
        review_path.symlink_to(outside)
        symlinked = TestClient(
            create_app(store=store, credential_verifier=_verifier("vault_primary"))
        ).get(f"/v1/jobs/{job_id}/review-audio", headers=AUTHORIZATION)
        store.put_checkpoint(
            job_id,
            stage=REVIEW_AUDIO_STAGE,
            checkpoint_key=REVIEW_AUDIO_CHECKPOINT_KEY,
            payload={"schema_version": "private invalid checkpoint"},
        )
        invalid_checkpoint = TestClient(
            create_app(store=store, credential_verifier=_verifier("vault_primary"))
        ).get(f"/v1/jobs/{job_id}/review-audio", headers=AUTHORIZATION)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_AUDIO_VERIFICATION_FAILED"
    assert "private tampered audio" not in response.text
    assert symlinked.status_code == 409
    assert "private outside audio" not in symlinked.text
    assert invalid_checkpoint.status_code == 409
    assert "private invalid checkpoint" not in invalid_checkpoint.text
