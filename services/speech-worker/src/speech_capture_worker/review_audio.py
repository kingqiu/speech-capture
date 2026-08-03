"""Deterministic private low-bitrate audio for evidence review and seeking."""

from __future__ import annotations

import hashlib
import os
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import numpy as np

from speech_capture_worker.errors import (
    ReviewAudioGenerationFailed,
    ReviewAudioNotFound,
    ReviewAudioVerificationFailed,
)
from speech_capture_worker.job_store import JobStore

REVIEW_AUDIO_SCHEMA_VERSION = "1.0.0"
REVIEW_AUDIO_STAGE = "review_audio"
REVIEW_AUDIO_CHECKPOINT_KEY = "review_audio_v1"
REVIEW_AUDIO_FILENAME = "review-v1.wav"
REVIEW_AUDIO_MEDIA_TYPE = "audio/wav"
REVIEW_AUDIO_SAMPLE_RATE = 8_000
REVIEW_AUDIO_CHANNELS = 1
REVIEW_AUDIO_SAMPLE_WIDTH_BYTES = 1
REVIEW_AUDIO_ALGORITHM = "pcm_u8_pair_average_v1"
READ_FRAMES = 1_048_576


@dataclass(frozen=True)
class ReviewAudioDescriptor:
    schema_version: str
    algorithm: str
    relative_path: str
    source_normalized_sha256: str
    media_type: str
    size_bytes: int
    sha256: str
    duration_ms: int
    sample_rate: int
    channels: int
    bits_per_sample: int

    def to_checkpoint(self) -> dict[str, object]:
        return asdict(self)


class ReviewAudioManager:
    """Create and verify one time-aligned private review-audio copy per job."""

    def __init__(self, store: JobStore) -> None:
        self.store = store

    def prepare(
        self,
        job_id: str,
        *,
        normalized_path: Path,
        normalized_sha256: str,
        duration_ms: int,
    ) -> tuple[ReviewAudioDescriptor, bool]:
        stage_directory = self.store.get_job_stage_directory(
            job_id,
            stage=REVIEW_AUDIO_STAGE,
        )
        review_path = stage_directory / REVIEW_AUDIO_FILENAME
        expected_relative_path = review_path.relative_to(
            self.store.data_directory
        ).as_posix()
        prior = self._load_checkpoint(job_id)
        if prior is not None and self._descriptor_matches_file(
            prior,
            review_path=review_path,
            normalized_sha256=normalized_sha256,
            expected_relative_path=expected_relative_path,
            duration_ms=duration_ms,
        ):
            return prior, False

        descriptor = self._build(
            normalized_path,
            review_path=review_path,
            relative_path=expected_relative_path,
            normalized_sha256=normalized_sha256,
            expected_duration_ms=duration_ms,
        )
        self.store.put_checkpoint(
            job_id,
            stage=REVIEW_AUDIO_STAGE,
            checkpoint_key=REVIEW_AUDIO_CHECKPOINT_KEY,
            payload=descriptor.to_checkpoint(),
        )
        return descriptor, True

    def get(
        self,
        job_id: str,
        *,
        normalized_sha256: str,
        duration_ms: int,
    ) -> ReviewAudioDescriptor:
        descriptor = self._load_checkpoint(job_id)
        if descriptor is None:
            raise ReviewAudioNotFound("Review audio is not available for this job.")
        expected_path = (
            self.store.get_job_stage_directory(job_id, stage=REVIEW_AUDIO_STAGE)
            / REVIEW_AUDIO_FILENAME
        )
        expected_relative_path = expected_path.relative_to(
            self.store.data_directory
        ).as_posix()
        if not self._descriptor_matches_file(
            descriptor,
            review_path=expected_path,
            normalized_sha256=normalized_sha256,
            expected_relative_path=expected_relative_path,
            duration_ms=duration_ms,
        ):
            raise ReviewAudioVerificationFailed(
                "Review audio no longer matches its private checkpoint."
            )
        return descriptor

    def path_for(self, job_id: str, descriptor: ReviewAudioDescriptor) -> Path:
        path = (self.store.data_directory / descriptor.relative_path).resolve()
        expected = (
            self.store.jobs_directory
            / job_id
            / REVIEW_AUDIO_STAGE
            / REVIEW_AUDIO_FILENAME
        ).resolve()
        if path != expected or path.is_symlink() or not path.is_file():
            raise ReviewAudioVerificationFailed("Review audio storage is unsafe.")
        return path

    def _load_checkpoint(self, job_id: str) -> ReviewAudioDescriptor | None:
        checkpoints = self.store.list_checkpoints(job_id, stage=REVIEW_AUDIO_STAGE)
        checkpoint = next(
            (
                item
                for item in checkpoints
                if item.checkpoint_key == REVIEW_AUDIO_CHECKPOINT_KEY
            ),
            None,
        )
        if checkpoint is None:
            return None
        try:
            descriptor = ReviewAudioDescriptor(**checkpoint.payload)
        except (TypeError, ValueError) as exc:
            raise ReviewAudioVerificationFailed(
                "The review-audio checkpoint is invalid."
            ) from exc
        if (
            descriptor.schema_version != REVIEW_AUDIO_SCHEMA_VERSION
            or descriptor.algorithm != REVIEW_AUDIO_ALGORITHM
            or descriptor.media_type != REVIEW_AUDIO_MEDIA_TYPE
            or not isinstance(descriptor.relative_path, str)
            or not descriptor.relative_path
            or Path(descriptor.relative_path).is_absolute()
            or not _is_sha256(descriptor.source_normalized_sha256)
            or not _is_sha256(descriptor.sha256)
            or not _positive_int(descriptor.size_bytes)
            or not _positive_int(descriptor.duration_ms)
            or descriptor.sample_rate != REVIEW_AUDIO_SAMPLE_RATE
            or descriptor.channels != REVIEW_AUDIO_CHANNELS
            or descriptor.bits_per_sample != REVIEW_AUDIO_SAMPLE_WIDTH_BYTES * 8
        ):
            raise ReviewAudioVerificationFailed(
                "The review-audio checkpoint is incompatible."
            )
        return descriptor

    def _descriptor_matches_file(
        self,
        descriptor: ReviewAudioDescriptor,
        *,
        review_path: Path,
        normalized_sha256: str,
        expected_relative_path: str,
        duration_ms: int,
    ) -> bool:
        if (
            descriptor.source_normalized_sha256 != normalized_sha256
            or descriptor.relative_path != expected_relative_path
            or abs(descriptor.duration_ms - duration_ms) > 1
            or review_path.is_symlink()
            or not review_path.is_file()
        ):
            return False
        try:
            if review_path.stat().st_size != descriptor.size_bytes:
                return False
            if _sha256(review_path) != descriptor.sha256:
                return False
            facts = _inspect_review_wav(review_path)
        except (OSError, ReviewAudioVerificationFailed):
            return False
        return (
            facts["duration_ms"] == descriptor.duration_ms
            and facts["sample_rate"] == descriptor.sample_rate
            and facts["channels"] == descriptor.channels
            and facts["bits_per_sample"] == descriptor.bits_per_sample
        )

    def _build(
        self,
        normalized_path: Path,
        *,
        review_path: Path,
        relative_path: str,
        normalized_sha256: str,
        expected_duration_ms: int,
    ) -> ReviewAudioDescriptor:
        if (
            normalized_path.is_symlink()
            or not normalized_path.is_file()
            or review_path.is_symlink()
            or review_path.parent.is_symlink()
        ):
            raise ReviewAudioGenerationFailed("Review audio input or storage is unsafe.")
        temporary = review_path.with_name(f".{review_path.name}.{uuid4().hex}.tmp")
        try:
            self._convert(normalized_path, temporary)
            facts = _inspect_review_wav(temporary)
            if abs(int(facts["duration_ms"]) - expected_duration_ms) > 1:
                raise ReviewAudioGenerationFailed(
                    "Review audio did not preserve the normalized timeline."
                )
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, review_path)
            _fsync_directory(review_path.parent)
        except ReviewAudioGenerationFailed:
            raise
        except ReviewAudioVerificationFailed as exc:
            raise ReviewAudioGenerationFailed(
                "Generated review audio failed validation."
            ) from exc
        except (OSError, wave.Error, ValueError) as exc:
            raise ReviewAudioGenerationFailed(
                "Review audio could not be generated safely."
            ) from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return ReviewAudioDescriptor(
            schema_version=REVIEW_AUDIO_SCHEMA_VERSION,
            algorithm=REVIEW_AUDIO_ALGORITHM,
            relative_path=relative_path,
            source_normalized_sha256=normalized_sha256,
            media_type=REVIEW_AUDIO_MEDIA_TYPE,
            size_bytes=review_path.stat().st_size,
            sha256=_sha256(review_path),
            duration_ms=int(facts["duration_ms"]),
            sample_rate=REVIEW_AUDIO_SAMPLE_RATE,
            channels=REVIEW_AUDIO_CHANNELS,
            bits_per_sample=REVIEW_AUDIO_SAMPLE_WIDTH_BYTES * 8,
        )

    @staticmethod
    def _convert(normalized_path: Path, output_path: Path) -> None:
        with wave.open(str(normalized_path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != 16_000
                or source.getcomptype() != "NONE"
            ):
                raise ReviewAudioGenerationFailed(
                    "Review audio requires the validated normalized PCM source."
                )
            with wave.open(str(output_path), "wb") as output:
                output.setnchannels(REVIEW_AUDIO_CHANNELS)
                output.setsampwidth(REVIEW_AUDIO_SAMPLE_WIDTH_BYTES)
                output.setframerate(REVIEW_AUDIO_SAMPLE_RATE)
                carry = np.empty(0, dtype="<i2")
                while raw := source.readframes(READ_FRAMES):
                    samples = np.frombuffer(raw, dtype="<i2")
                    if carry.size:
                        samples = np.concatenate((carry, samples))
                    even_count = samples.size - samples.size % 2
                    if even_count:
                        pairs = samples[:even_count].astype(np.int32).reshape(-1, 2)
                        averaged = np.floor_divide(pairs[:, 0] + pairs[:, 1], 2)
                        encoded = np.clip((averaged + 32_768) // 256, 0, 255).astype(
                            np.uint8
                        )
                        output.writeframesraw(encoded.tobytes())
                    carry = samples[even_count:].copy()
                if carry.size:
                    encoded = np.clip(
                        (carry.astype(np.int32) + 32_768) // 256,
                        0,
                        255,
                    ).astype(np.uint8)
                    output.writeframesraw(encoded.tobytes())


def _inspect_review_wav(path: Path) -> dict[str, int]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
            compression = audio.getcomptype()
    except (OSError, wave.Error) as exc:
        raise ReviewAudioVerificationFailed("Review audio is not a readable WAV file.") from exc
    if (
        channels != REVIEW_AUDIO_CHANNELS
        or sample_width != REVIEW_AUDIO_SAMPLE_WIDTH_BYTES
        or sample_rate != REVIEW_AUDIO_SAMPLE_RATE
        or compression != "NONE"
        or frames < 1
    ):
        raise ReviewAudioVerificationFailed("Review audio has an unexpected PCM layout.")
    return {
        "duration_ms": round(frames * 1000 / sample_rate),
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": sample_width * 8,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
