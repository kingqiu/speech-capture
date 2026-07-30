"""Deterministic private audio normalization and restart-safe ASR chunk planning."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from speech_capture_worker.domain import JobState
from speech_capture_worker.errors import (
    AudioNormalizationFailed,
    AudioNormalizationUnavailable,
    InvalidJobRequest,
    NormalizedAudioInvalid,
)
from speech_capture_worker.job_store import JobStore

NORMALIZED_AUDIO_SCHEMA_VERSION = "1.0.0"
NORMALIZED_SAMPLE_RATE = 16_000
NORMALIZED_CHANNELS = 1
NORMALIZED_SAMPLE_WIDTH_BYTES = 2
DEFAULT_MAX_CHUNK_MS = 30_000
DEFAULT_MIN_CHUNK_MS = 5_000
DEFAULT_BOUNDARY_SEARCH_MS = 3_000
DEFAULT_ENERGY_WINDOW_MS = 100
NORMALIZED_FILENAME = "normalized-v1.wav"
CHECKPOINT_STAGE = "preprocessing"
CHECKPOINT_KEY = "normalized_audio_plan"
FFMPEG_TIMEOUT_BASE_SECONDS = 120
FFMPEG_TIMEOUT_REALTIME_FACTOR = 2.0


@dataclass(frozen=True)
class AudioChunkPlan:
    chunk_index: int
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedAudioPlan:
    schema_version: str
    algorithm: str
    relative_path: str
    source_sha256: str
    normalized_sha256: str
    normalized_size_bytes: int
    sample_rate: int
    channels: int
    sample_width_bytes: int
    total_frames: int
    duration_ms: int
    max_chunk_ms: int
    min_chunk_ms: int
    boundary_search_ms: int
    energy_window_ms: int
    chunks: tuple[AudioChunkPlan, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["chunks"] = [chunk.to_dict() for chunk in self.chunks]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NormalizedAudioPlan:
        try:
            chunks = tuple(AudioChunkPlan(**chunk) for chunk in payload["chunks"])
            plan = cls(
                schema_version=str(payload["schema_version"]),
                algorithm=str(payload["algorithm"]),
                relative_path=str(payload["relative_path"]),
                source_sha256=str(payload["source_sha256"]),
                normalized_sha256=str(payload["normalized_sha256"]),
                normalized_size_bytes=int(payload["normalized_size_bytes"]),
                sample_rate=int(payload["sample_rate"]),
                channels=int(payload["channels"]),
                sample_width_bytes=int(payload["sample_width_bytes"]),
                total_frames=int(payload["total_frames"]),
                duration_ms=int(payload["duration_ms"]),
                max_chunk_ms=int(payload["max_chunk_ms"]),
                min_chunk_ms=int(payload["min_chunk_ms"]),
                boundary_search_ms=int(payload["boundary_search_ms"]),
                energy_window_ms=int(payload["energy_window_ms"]),
                chunks=chunks,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NormalizedAudioInvalid(
                "The persisted normalized-audio plan is invalid."
            ) from exc
        validate_audio_plan(plan)
        return plan


class AudioPreprocessor:
    """Normalize one verified source and persist an exact frame-based chunk plan."""

    def __init__(
        self,
        store: JobStore,
        *,
        ffmpeg_executable: str | None = None,
        max_chunk_ms: int = DEFAULT_MAX_CHUNK_MS,
        min_chunk_ms: int = DEFAULT_MIN_CHUNK_MS,
        boundary_search_ms: int = DEFAULT_BOUNDARY_SEARCH_MS,
        energy_window_ms: int = DEFAULT_ENERGY_WINDOW_MS,
    ) -> None:
        self.store = store
        self.ffmpeg_executable = ffmpeg_executable or shutil.which("ffmpeg")
        self.max_chunk_ms = max_chunk_ms
        self.min_chunk_ms = min_chunk_ms
        self.boundary_search_ms = boundary_search_ms
        self.energy_window_ms = energy_window_ms
        _validate_chunk_policy(
            max_chunk_ms=max_chunk_ms,
            min_chunk_ms=min_chunk_ms,
            boundary_search_ms=boundary_search_ms,
            energy_window_ms=energy_window_ms,
        )

    def prepare(self, job_id: str) -> tuple[NormalizedAudioPlan, bool]:
        """Build or reuse the normalized WAV and its durable chunk plan."""

        job = self.store.get_job(job_id)
        if job.state is not JobState.PREPROCESSING:
            raise InvalidJobRequest(
                "Audio normalization can run only while the job is preprocessing."
            )
        source_path = self.store.get_job_verified_source_path(job_id)
        assert job.source_upload_id is not None
        upload = self.store.get_upload(job.source_upload_id)
        if upload.duration_seconds is None:
            raise NormalizedAudioInvalid(
                "The verified source does not have a media duration."
            )
        stage_directory = self.store.get_job_stage_directory(
            job_id,
            stage=CHECKPOINT_STAGE,
        )
        normalized_path = stage_directory / NORMALIZED_FILENAME
        expected_relative_path = normalized_path.relative_to(
            self.store.data_directory
        ).as_posix()

        prior = self._load_checkpoint_plan(job_id)
        if prior is not None and self._plan_matches_file(
            prior,
            normalized_path=normalized_path,
            source_sha256=job.source_sha256,
            expected_relative_path=expected_relative_path,
        ):
            return prior, False

        if normalized_path.is_file() and not normalized_path.is_symlink():
            try:
                plan = self._build_plan(
                    normalized_path,
                    source_sha256=job.source_sha256,
                    relative_path=expected_relative_path,
                    expected_duration_ms=round(upload.duration_seconds * 1000),
                )
            except NormalizedAudioInvalid:
                plan = self._normalize_and_plan(
                    source_path,
                    normalized_path=normalized_path,
                    source_sha256=job.source_sha256,
                    relative_path=expected_relative_path,
                    expected_duration_ms=round(upload.duration_seconds * 1000),
                )
                normalized_created = True
            else:
                normalized_created = False
        else:
            plan = self._normalize_and_plan(
                source_path,
                normalized_path=normalized_path,
                source_sha256=job.source_sha256,
                relative_path=expected_relative_path,
                expected_duration_ms=round(upload.duration_seconds * 1000),
            )
            normalized_created = True

        _, checkpoint_created = self.store.put_checkpoint(
            job_id,
            stage=CHECKPOINT_STAGE,
            checkpoint_key=CHECKPOINT_KEY,
            payload=plan.to_dict(),
        )
        return plan, normalized_created or checkpoint_created

    def get_plan(self, job_id: str) -> NormalizedAudioPlan:
        plan = self._load_checkpoint_plan(job_id)
        if plan is None:
            raise NormalizedAudioInvalid(
                "The job does not have a normalized-audio plan."
            )
        stage_directory = self.store.get_job_stage_directory(
            job_id,
            stage=CHECKPOINT_STAGE,
        )
        path = stage_directory / NORMALIZED_FILENAME
        expected_relative_path = path.relative_to(self.store.data_directory).as_posix()
        job = self.store.get_job(job_id)
        if not self._plan_matches_file(
            plan,
            normalized_path=path,
            source_sha256=job.source_sha256,
            expected_relative_path=expected_relative_path,
        ):
            raise NormalizedAudioInvalid(
                "The normalized audio no longer matches its durable plan."
            )
        return plan

    def get_normalized_path(self, job_id: str) -> Path:
        plan = self.get_plan(job_id)
        path = (self.store.data_directory / plan.relative_path).resolve()
        root = self.store.jobs_directory.resolve()
        if not path.is_relative_to(root):
            raise NormalizedAudioInvalid(
                "The normalized audio resolved outside private Worker storage."
            )
        return path

    def _load_checkpoint_plan(self, job_id: str) -> NormalizedAudioPlan | None:
        checkpoints = self.store.list_checkpoints(
            job_id,
            stage=CHECKPOINT_STAGE,
        )
        for checkpoint in checkpoints:
            if checkpoint.checkpoint_key == CHECKPOINT_KEY:
                return NormalizedAudioPlan.from_dict(checkpoint.payload)
        return None

    def _plan_matches_file(
        self,
        plan: NormalizedAudioPlan,
        *,
        normalized_path: Path,
        source_sha256: str,
        expected_relative_path: str,
    ) -> bool:
        if (
            plan.source_sha256 != source_sha256
            or plan.relative_path != expected_relative_path
            or normalized_path.is_symlink()
            or not normalized_path.is_file()
        ):
            return False
        try:
            if normalized_path.stat().st_size != plan.normalized_size_bytes:
                return False
            if _sha256(normalized_path) != plan.normalized_sha256:
                return False
            facts = inspect_normalized_wav(normalized_path)
        except (OSError, NormalizedAudioInvalid):
            return False
        return (
            facts["sample_rate"] == plan.sample_rate
            and facts["channels"] == plan.channels
            and facts["sample_width_bytes"] == plan.sample_width_bytes
            and facts["total_frames"] == plan.total_frames
        )

    def _normalize_and_plan(
        self,
        source_path: Path,
        *,
        normalized_path: Path,
        source_sha256: str,
        relative_path: str,
        expected_duration_ms: int,
    ) -> NormalizedAudioPlan:
        if self.ffmpeg_executable is None:
            raise AudioNormalizationUnavailable(
                "FFmpeg is required to normalize audio before transcription."
            )
        if normalized_path.is_symlink() or normalized_path.parent.is_symlink():
            raise NormalizedAudioInvalid(
                "Normalized audio storage must not contain symbolic links."
            )
        temporary_path = normalized_path.with_name(
            f".{normalized_path.name}.{uuid4().hex}.tmp"
        )
        timeout_seconds = max(
            FFMPEG_TIMEOUT_BASE_SECONDS,
            expected_duration_ms / 1000 * FFMPEG_TIMEOUT_REALTIME_FACTOR,
        )
        command = [
            self.ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-ac",
            str(NORMALIZED_CHANNELS),
            "-ar",
            str(NORMALIZED_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(temporary_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise AudioNormalizationUnavailable(
                "FFmpeg is required to normalize audio before transcription."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioNormalizationFailed(
                "Audio normalization exceeded its safe execution window."
            ) from exc
        except OSError as exc:
            raise AudioNormalizationFailed(
                "Audio normalization could not start."
            ) from exc
        try:
            if completed.returncode != 0:
                raise AudioNormalizationFailed(
                    "FFmpeg could not normalize the verified audio source.",
                    details={"return_code": completed.returncode},
                )
            plan = self._build_plan(
                temporary_path,
                source_sha256=source_sha256,
                relative_path=relative_path,
                expected_duration_ms=expected_duration_ms,
            )
            with temporary_path.open("rb") as normalized:
                os.fsync(normalized.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, normalized_path)
            _fsync_directory(normalized_path.parent)
            return plan
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _build_plan(
        self,
        normalized_path: Path,
        *,
        source_sha256: str,
        relative_path: str,
        expected_duration_ms: int,
    ) -> NormalizedAudioPlan:
        facts = inspect_normalized_wav(normalized_path)
        duration_ms = round(
            facts["total_frames"] * 1000 / facts["sample_rate"]
        )
        tolerance_ms = max(250, round(expected_duration_ms * 0.01))
        if abs(duration_ms - expected_duration_ms) > tolerance_ms:
            raise NormalizedAudioInvalid(
                "Normalized audio duration does not match the verified source.",
                details={
                    "expected_duration_ms": expected_duration_ms,
                    "actual_duration_ms": duration_ms,
                },
            )
        chunks = plan_wav_chunks(
            normalized_path,
            max_chunk_ms=self.max_chunk_ms,
            min_chunk_ms=self.min_chunk_ms,
            boundary_search_ms=self.boundary_search_ms,
            energy_window_ms=self.energy_window_ms,
        )
        plan = NormalizedAudioPlan(
            schema_version=NORMALIZED_AUDIO_SCHEMA_VERSION,
            algorithm="pcm16_mono_energy_boundary_v1",
            relative_path=relative_path,
            source_sha256=source_sha256,
            normalized_sha256=_sha256(normalized_path),
            normalized_size_bytes=normalized_path.stat().st_size,
            sample_rate=facts["sample_rate"],
            channels=facts["channels"],
            sample_width_bytes=facts["sample_width_bytes"],
            total_frames=facts["total_frames"],
            duration_ms=duration_ms,
            max_chunk_ms=self.max_chunk_ms,
            min_chunk_ms=self.min_chunk_ms,
            boundary_search_ms=self.boundary_search_ms,
            energy_window_ms=self.energy_window_ms,
            chunks=chunks,
        )
        validate_audio_plan(plan)
        return plan


def inspect_normalized_wav(path: Path) -> dict[str, int]:
    try:
        with wave.open(str(path), "rb") as audio:
            facts = {
                "sample_rate": audio.getframerate(),
                "channels": audio.getnchannels(),
                "sample_width_bytes": audio.getsampwidth(),
                "total_frames": audio.getnframes(),
                "compression_type": 0 if audio.getcomptype() == "NONE" else 1,
            }
    except (OSError, EOFError, wave.Error) as exc:
        raise NormalizedAudioInvalid(
            "The normalized audio is not a readable PCM WAV file."
        ) from exc
    if (
        facts["sample_rate"] != NORMALIZED_SAMPLE_RATE
        or facts["channels"] != NORMALIZED_CHANNELS
        or facts["sample_width_bytes"] != NORMALIZED_SAMPLE_WIDTH_BYTES
        or facts["total_frames"] <= 0
        or facts["compression_type"] != 0
    ):
        raise NormalizedAudioInvalid(
            "Normalized audio must be 16 kHz mono 16-bit PCM."
        )
    return facts


def plan_wav_chunks(
    path: Path,
    *,
    max_chunk_ms: int = DEFAULT_MAX_CHUNK_MS,
    min_chunk_ms: int = DEFAULT_MIN_CHUNK_MS,
    boundary_search_ms: int = DEFAULT_BOUNDARY_SEARCH_MS,
    energy_window_ms: int = DEFAULT_ENERGY_WINDOW_MS,
) -> tuple[AudioChunkPlan, ...]:
    _validate_chunk_policy(
        max_chunk_ms=max_chunk_ms,
        min_chunk_ms=min_chunk_ms,
        boundary_search_ms=boundary_search_ms,
        energy_window_ms=energy_window_ms,
    )
    facts = inspect_normalized_wav(path)
    sample_rate = facts["sample_rate"]
    total_frames = facts["total_frames"]
    max_frames = _ms_to_frames(max_chunk_ms, sample_rate)
    min_frames = _ms_to_frames(min_chunk_ms, sample_rate)
    search_frames = _ms_to_frames(boundary_search_ms, sample_rate)
    window_frames = _ms_to_frames(energy_window_ms, sample_rate)

    boundaries = [0]
    cursor = 0
    with wave.open(str(path), "rb") as audio:
        while total_frames - cursor > max_frames:
            nominal = cursor + max_frames
            latest = min(nominal, total_frames - min_frames)
            earliest = max(cursor + min_frames, latest - search_frames)
            boundary = _quietest_boundary(
                audio,
                earliest_frame=earliest,
                latest_frame=latest,
                window_frames=window_frames,
            )
            if boundary <= cursor or boundary > nominal:
                boundary = latest
            boundaries.append(boundary)
            cursor = boundary
    boundaries.append(total_frames)

    chunks = tuple(
        AudioChunkPlan(
            chunk_index=index,
            start_frame=start,
            end_frame=end,
            start_ms=round(start * 1000 / sample_rate),
            end_ms=round(end * 1000 / sample_rate),
        )
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]))
    )
    return chunks


def validate_audio_plan(plan: NormalizedAudioPlan) -> None:
    if (
        plan.schema_version != NORMALIZED_AUDIO_SCHEMA_VERSION
        or plan.algorithm != "pcm16_mono_energy_boundary_v1"
        or not plan.relative_path
        or Path(plan.relative_path).is_absolute()
        or ".." in Path(plan.relative_path).parts
        or len(plan.source_sha256) != 64
        or len(plan.normalized_sha256) != 64
        or plan.normalized_size_bytes <= 0
        or plan.sample_rate != NORMALIZED_SAMPLE_RATE
        or plan.channels != NORMALIZED_CHANNELS
        or plan.sample_width_bytes != NORMALIZED_SAMPLE_WIDTH_BYTES
        or plan.total_frames <= 0
        or plan.duration_ms <= 0
        or not plan.chunks
    ):
        raise NormalizedAudioInvalid("The normalized-audio plan is invalid.")
    _validate_chunk_policy(
        max_chunk_ms=plan.max_chunk_ms,
        min_chunk_ms=plan.min_chunk_ms,
        boundary_search_ms=plan.boundary_search_ms,
        energy_window_ms=plan.energy_window_ms,
    )
    cursor = 0
    for expected_index, chunk in enumerate(plan.chunks):
        if (
            chunk.chunk_index != expected_index
            or chunk.start_frame != cursor
            or chunk.end_frame <= chunk.start_frame
            or chunk.end_frame > plan.total_frames
            or chunk.start_ms != round(chunk.start_frame * 1000 / plan.sample_rate)
            or chunk.end_ms != round(chunk.end_frame * 1000 / plan.sample_rate)
            or chunk.duration_ms <= 0
            or chunk.duration_ms > plan.max_chunk_ms + 1
        ):
            raise NormalizedAudioInvalid(
                "Normalized-audio chunks must be ordered, contiguous, and bounded."
            )
        cursor = chunk.end_frame
    if cursor != plan.total_frames:
        raise NormalizedAudioInvalid(
            "Normalized-audio chunks do not account for the complete timeline."
        )


def _quietest_boundary(
    audio: wave.Wave_read,
    *,
    earliest_frame: int,
    latest_frame: int,
    window_frames: int,
) -> int:
    if latest_frame <= earliest_frame:
        return latest_frame
    best_boundary = latest_frame
    best_score = float("inf")
    half_window = max(1, window_frames // 2)
    step = max(1, window_frames)
    for boundary in range(earliest_frame, latest_frame + 1, step):
        window_start = max(0, boundary - half_window)
        window_end = min(audio.getnframes(), boundary + half_window)
        audio.setpos(window_start)
        raw = audio.readframes(window_end - window_start)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        score = float(np.mean(samples * samples)) if samples.size else float("inf")
        ranking = (score, abs(latest_frame - boundary))
        if ranking < (best_score, abs(latest_frame - best_boundary)):
            best_score = score
            best_boundary = boundary
    return best_boundary


def _validate_chunk_policy(
    *,
    max_chunk_ms: int,
    min_chunk_ms: int,
    boundary_search_ms: int,
    energy_window_ms: int,
) -> None:
    values = (max_chunk_ms, min_chunk_ms, boundary_search_ms, energy_window_ms)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise InvalidJobRequest("Audio chunk policy values must be integer milliseconds.")
    if (
        max_chunk_ms < 1000
        or min_chunk_ms < 100
        or min_chunk_ms >= max_chunk_ms
        or boundary_search_ms < 0
        or boundary_search_ms >= max_chunk_ms - min_chunk_ms
        or energy_window_ms < 10
        or energy_window_ms > max_chunk_ms
    ):
        raise InvalidJobRequest("Audio chunk policy values are inconsistent.")


def _ms_to_frames(milliseconds: int, sample_rate: int) -> int:
    return max(1, round(milliseconds * sample_rate / 1000))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise NormalizedAudioInvalid(
            "The normalized audio could not be verified."
        ) from exc
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
