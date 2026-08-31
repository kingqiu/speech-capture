"""Controlled forced-alignment fallback for stable estimated transcript segments."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import unicodedata
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import numpy as np

from speech_capture_worker.alignment import (
    CHECKPOINT_KEY as ALIGNMENT_CHECKPOINT_KEY,
)
from speech_capture_worker.alignment import (
    CHECKPOINT_STAGE,
    AlignmentFinalizationResult,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.audio_preprocessing import (
    AudioPreprocessor,
    NormalizedAudioPlan,
)
from speech_capture_worker.domain import CheckpointRecord, JobRecord, JobState, ResourceStatus
from speech_capture_worker.errors import (
    ForcedAlignmentFailed,
    InvalidJobRequest,
    NormalizedAudioInvalid,
    UploadStorageError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import GIB, ResourceReport, check_resource_preflight
from speech_capture_worker.transcript import (
    TranscriptOutcome,
    TranscriptSegment,
    TranscriptTimingStatus,
)

FORCED_ALIGNMENT_SCHEMA_VERSION = "1.0.0"
FORCED_ALIGNMENT_RAW_SCHEMA_VERSION = "1.0.0"
FORCED_ALIGNER_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B"
FORCED_ALIGNMENT_HEADROOM_BYTES = 3 * GIB
FORCED_ALIGNMENT_STAGE = "aligning"
FORCED_ALIGNMENT_CHECKPOINT_PREFIX = "forced_alignment_"


class ForcedAlignmentEngine(Protocol):
    model_id: str

    def align(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
        text: str,
        language: str,
    ) -> list[dict[str, Any]]: ...


class MlxQwenForcedAlignmentEngine:
    """Lazy adapter around the pinned native MLX forced aligner."""

    model_id = FORCED_ALIGNER_MODEL_ID

    def __init__(self, *, model_target: str | None = None) -> None:
        self._model_target = model_target or self.model_id
        self._aligner: Any | None = None

    def align(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
        text: str,
        language: str,
    ) -> list[dict[str, Any]]:
        if self._aligner is None:
            from mlx_qwen3_asr import ForcedAligner

            self._aligner = ForcedAligner(model_path=self._model_target)
        normalized_audio = _prepare_aligner_audio(audio, sample_rate=sample_rate)
        words = [
            asdict(word)
            for word in self._aligner.align(
                normalized_audio,
                text,
                language,
            )
        ]
        return _clamp_alignment_words(
            words,
            duration_seconds=normalized_audio.size / sample_rate,
        )


class ForcedAlignmentOutcome(StrEnum):
    ALIGNED = "aligned"
    REPLAYED = "replayed"
    NO_ESTIMATED_SEGMENTS = "no_estimated_segments"
    SAFE_PAUSED = "safe_paused"
    ALREADY_FINALIZED = "already_finalized"


@dataclass(frozen=True)
class ValidatedForcedAlignment:
    aligned_start_ms: int
    aligned_end_ms: int
    word_count: int
    normalized_text_sha256: str
    words: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ForcedAlignmentResult:
    outcome: ForcedAlignmentOutcome
    job: JobRecord
    segment: TranscriptSegment | None
    evidence_checkpoint_generation: int | None
    alignment: AlignmentFinalizationResult
    resource_report: ResourceReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "job": self.job.to_dict(),
            "segment": (
                {
                    "segment_id": self.segment.segment_id,
                    "revision": self.segment.revision,
                    "start_ms": self.segment.start_ms,
                    "end_ms": self.segment.end_ms,
                    "outcome": self.segment.outcome.value,
                    "timing_status": self.segment.timing_status.value,
                }
                if self.segment is not None
                else None
            ),
            "evidence_checkpoint_generation": self.evidence_checkpoint_generation,
            "alignment": self.alignment.to_dict(),
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
        }


BoundaryPreflight = Callable[..., ResourceReport]


class ForcedAlignmentExecutor:
    """Align at most one current estimated segment per call."""

    def __init__(
        self,
        store: JobStore,
        engine: ForcedAlignmentEngine,
        *,
        preprocessor: AudioPreprocessor | None = None,
        finalizer: TranscriptAlignmentFinalizer | None = None,
        boundary_preflight: BoundaryPreflight = check_resource_preflight,
    ) -> None:
        if (
            not isinstance(engine.model_id, str)
            or not engine.model_id
            or len(engine.model_id) > 200
            or any(not character.isprintable() for character in engine.model_id)
        ):
            raise InvalidJobRequest("Forced-alignment engine model_id is invalid.")
        self.store = store
        self.engine = engine
        self.preprocessor = preprocessor or AudioPreprocessor(store)
        self.finalizer = finalizer or TranscriptAlignmentFinalizer(store)
        self._boundary_preflight = boundary_preflight

    def run_next(self, job_id: str) -> ForcedAlignmentResult:
        job = self.store.get_job(job_id)
        if job.state is JobState.DIARIZING:
            alignment = self.finalizer.finalize(job_id)
            return ForcedAlignmentResult(
                outcome=ForcedAlignmentOutcome.ALREADY_FINALIZED,
                job=alignment.job,
                segment=None,
                evidence_checkpoint_generation=None,
                alignment=alignment,
                resource_report=None,
            )
        if job.state is not JobState.ALIGNING:
            raise InvalidJobRequest("Forced alignment requires an aligning or diarizing job.")

        initial_alignment = self.finalizer.finalize(job_id)
        if initial_alignment.job.state is JobState.DIARIZING:
            return ForcedAlignmentResult(
                outcome=ForcedAlignmentOutcome.ALREADY_FINALIZED,
                job=initial_alignment.job,
                segment=None,
                evidence_checkpoint_generation=None,
                alignment=initial_alignment,
                resource_report=None,
            )
        alignment_checkpoint = self._current_alignment_checkpoint(
            job_id,
            expected_generation=initial_alignment.checkpoint_generation,
        )
        segments = self._list_all_segments(job_id)
        target = next(
            (
                segment
                for segment in segments
                if segment.outcome is TranscriptOutcome.TRANSCRIBED
                and segment.timing_status is TranscriptTimingStatus.ESTIMATED
            ),
            None,
        )
        if target is None:
            return ForcedAlignmentResult(
                outcome=ForcedAlignmentOutcome.NO_ESTIMATED_SEGMENTS,
                job=initial_alignment.job,
                segment=None,
                evidence_checkpoint_generation=None,
                alignment=initial_alignment,
                resource_report=None,
            )
        if not any(
            issue.code == "UNALIGNED_TRANSCRIBED_SEGMENT" and issue.segment_id == target.segment_id
            for issue in initial_alignment.report.issues
        ):
            raise ForcedAlignmentFailed(
                "The selected segment is not present in the current alignment issues.",
                details={"segment_id": target.segment_id},
            )
        language = target.language or job.language_hint
        if not isinstance(language, str) or not language.strip():
            raise ForcedAlignmentFailed(
                "Forced alignment requires segment language metadata or a job language hint.",
                details={"segment_id": target.segment_id},
            )
        language = language.strip()

        plan = self.preprocessor.get_plan(job_id)
        checkpoint_key = _checkpoint_key(target.segment_id)
        evidence_checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=FORCED_ALIGNMENT_STAGE),
            checkpoint_key,
        )
        resource_report: ResourceReport | None = None
        replayed = evidence_checkpoint is not None
        if evidence_checkpoint is not None:
            validated = self._load_durable_evidence(
                job,
                target,
                language=language,
                plan=plan,
                alignment_checkpoint=alignment_checkpoint,
                evidence_checkpoint=evidence_checkpoint,
            )
        else:
            resource_report = self._boundary_preflight(
                self.store.data_directory,
                estimated_required_bytes=FORCED_ALIGNMENT_HEADROOM_BYTES,
                model_profile=job.model_profile,
            )
            self.store.put_checkpoint(
                job_id,
                stage=FORCED_ALIGNMENT_STAGE,
                checkpoint_key=f"forced_alignment_resource_{target.segment_id}",
                payload=resource_report.to_dict(),
            )
            if resource_report.status is ResourceStatus.BLOCKED:
                current = self.store.get_job(job_id)
                paused = self.store.transition_job(
                    job_id,
                    JobState.PAUSED,
                    expected_revision=current.revision,
                    reason_code="forced_alignment_resource_blocked",
                    error_code="FORCED_ALIGNMENT_RESOURCE_BLOCKED",
                    error_message=(
                        "Worker resources must recover before forced alignment can start."
                    ),
                    event_type="resource.safe_paused",
                )
                return ForcedAlignmentResult(
                    outcome=ForcedAlignmentOutcome.SAFE_PAUSED,
                    job=paused,
                    segment=target,
                    evidence_checkpoint_generation=None,
                    alignment=initial_alignment,
                    resource_report=resource_report,
                )

            audio, start_frame, end_frame = _read_segment_audio(
                self.preprocessor.get_normalized_path(job_id),
                plan=plan,
                start_ms=target.start_ms,
                end_ms=target.end_ms,
            )
            started = time.monotonic()
            try:
                words = self.engine.align(
                    audio,
                    sample_rate=plan.sample_rate,
                    text=target.text or "",
                    language=language,
                )
            except ForcedAlignmentFailed:
                raise
            except Exception as exc:
                raise ForcedAlignmentFailed(
                    "The local forced-alignment engine failed.",
                    details={
                        "segment_id": target.segment_id,
                        "exception_type": type(exc).__name__,
                    },
                ) from exc
            elapsed_seconds = time.monotonic() - started
            validated = _validate_alignment_words(
                words,
                stable_text=target.text or "",
                segment_start_ms=target.start_ms,
                segment_end_ms=target.end_ms,
            )
            self._require_source_unchanged(
                job_id,
                target=target,
                plan=plan,
                alignment_checkpoint=alignment_checkpoint,
            )
            raw_payload = {
                "schema_version": FORCED_ALIGNMENT_RAW_SCHEMA_VERSION,
                "segment_id": target.segment_id,
                "segment_revision": target.revision,
                "segment_start_ms": target.start_ms,
                "segment_end_ms": target.end_ms,
                "segment_text_sha256": _text_sha256(target.text or ""),
                "language": language,
                "model_id": self.engine.model_id,
                "normalized_sha256": plan.normalized_sha256,
                "sample_rate": plan.sample_rate,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "words": list(validated.words),
            }
            raw_bytes = _canonical_json(raw_payload).encode("utf-8")
            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            raw_relative_path = self._write_private_evidence(
                job_id,
                segment_id=target.segment_id,
                raw_sha256=raw_sha256,
                raw_bytes=raw_bytes,
            )
            evidence_checkpoint, _ = self.store.put_checkpoint(
                job_id,
                stage=FORCED_ALIGNMENT_STAGE,
                checkpoint_key=checkpoint_key,
                payload={
                    "schema_version": FORCED_ALIGNMENT_SCHEMA_VERSION,
                    "alignment_report_generation": alignment_checkpoint.generation,
                    "alignment_report_sha256": alignment_checkpoint.payload_sha256,
                    "segment_id": target.segment_id,
                    "segment_revision": target.revision,
                    "segment_start_ms": target.start_ms,
                    "segment_end_ms": target.end_ms,
                    "segment_text_sha256": _text_sha256(target.text or ""),
                    "language": language,
                    "model_id": self.engine.model_id,
                    "normalized_sha256": plan.normalized_sha256,
                    "sample_rate": plan.sample_rate,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "aligned_start_ms": validated.aligned_start_ms,
                    "aligned_end_ms": validated.aligned_end_ms,
                    "word_count": validated.word_count,
                    "normalized_text_sha256": validated.normalized_text_sha256,
                    "raw_relative_path": raw_relative_path,
                    "raw_sha256": raw_sha256,
                    "elapsed_seconds": round(elapsed_seconds, 6),
                },
            )

        updated = self.store.update_transcript_segment_metadata(
            job_id,
            target.segment_id,
            expected_revision=target.revision,
            start_ms=validated.aligned_start_ms,
            end_ms=validated.aligned_end_ms,
            timing_status=TranscriptTimingStatus.ALIGNED,
        )
        alignment = self.finalizer.finalize(job_id)
        return ForcedAlignmentResult(
            outcome=(
                ForcedAlignmentOutcome.REPLAYED if replayed else ForcedAlignmentOutcome.ALIGNED
            ),
            job=alignment.job,
            segment=updated,
            evidence_checkpoint_generation=evidence_checkpoint.generation,
            alignment=alignment,
            resource_report=resource_report,
        )

    def _load_durable_evidence(
        self,
        job: JobRecord,
        segment: TranscriptSegment,
        *,
        language: str,
        plan: NormalizedAudioPlan,
        alignment_checkpoint: CheckpointRecord,
        evidence_checkpoint: CheckpointRecord,
    ) -> ValidatedForcedAlignment:
        payload = evidence_checkpoint.payload
        expected_start_frame = round(segment.start_ms * plan.sample_rate / 1000)
        expected_end_frame = round(segment.end_ms * plan.sample_rate / 1000)
        if (
            payload.get("schema_version") != FORCED_ALIGNMENT_SCHEMA_VERSION
            or payload.get("alignment_report_generation") != alignment_checkpoint.generation
            or payload.get("alignment_report_sha256") != alignment_checkpoint.payload_sha256
            or payload.get("segment_id") != segment.segment_id
            or payload.get("segment_revision") != segment.revision
            or payload.get("segment_start_ms") != segment.start_ms
            or payload.get("segment_end_ms") != segment.end_ms
            or payload.get("segment_text_sha256") != _text_sha256(segment.text or "")
            or payload.get("language") != language
            or payload.get("model_id") != self.engine.model_id
            or payload.get("normalized_sha256") != plan.normalized_sha256
            or payload.get("sample_rate") != plan.sample_rate
            or payload.get("start_frame") != expected_start_frame
            or payload.get("end_frame") != expected_end_frame
            or not _is_int(payload.get("aligned_start_ms"))
            or not _is_int(payload.get("aligned_end_ms"))
            or not _is_int(payload.get("word_count"))
            or not isinstance(payload.get("normalized_text_sha256"), str)
            or not isinstance(payload.get("raw_relative_path"), str)
            or not isinstance(payload.get("raw_sha256"), str)
        ):
            raise ForcedAlignmentFailed(
                "The durable forced-alignment evidence is invalid.",
                details={"segment_id": segment.segment_id},
            )
        raw_payload = self._read_private_evidence(
            job.job_id,
            relative_path=payload["raw_relative_path"],
            expected_sha256=payload["raw_sha256"],
        )
        if (
            raw_payload.get("schema_version") != FORCED_ALIGNMENT_RAW_SCHEMA_VERSION
            or raw_payload.get("segment_id") != segment.segment_id
            or raw_payload.get("segment_revision") != segment.revision
            or raw_payload.get("segment_start_ms") != segment.start_ms
            or raw_payload.get("segment_end_ms") != segment.end_ms
            or raw_payload.get("segment_text_sha256") != _text_sha256(segment.text or "")
            or raw_payload.get("language") != language
            or raw_payload.get("model_id") != self.engine.model_id
            or raw_payload.get("normalized_sha256") != plan.normalized_sha256
            or raw_payload.get("sample_rate") != plan.sample_rate
            or raw_payload.get("start_frame") != expected_start_frame
            or raw_payload.get("end_frame") != expected_end_frame
            or not isinstance(raw_payload.get("words"), list)
        ):
            raise ForcedAlignmentFailed(
                "The private forced-alignment evidence does not match its checkpoint.",
                details={"segment_id": segment.segment_id},
            )
        validated = _validate_alignment_words(
            raw_payload["words"],
            stable_text=segment.text or "",
            segment_start_ms=segment.start_ms,
            segment_end_ms=segment.end_ms,
        )
        if (
            validated.aligned_start_ms != payload["aligned_start_ms"]
            or validated.aligned_end_ms != payload["aligned_end_ms"]
            or validated.word_count != payload["word_count"]
            or validated.normalized_text_sha256 != payload["normalized_text_sha256"]
        ):
            raise ForcedAlignmentFailed(
                "The private forced-alignment result changed after validation.",
                details={"segment_id": segment.segment_id},
            )
        return validated

    def _require_source_unchanged(
        self,
        job_id: str,
        *,
        target: TranscriptSegment,
        plan: NormalizedAudioPlan,
        alignment_checkpoint: CheckpointRecord,
    ) -> None:
        current_job = self.store.get_job(job_id)
        if current_job.state is not JobState.ALIGNING:
            raise ForcedAlignmentFailed("The job changed state during forced alignment.")
        current_segment = self._find_segment(job_id, target.segment_id)
        if current_segment != target:
            raise ForcedAlignmentFailed(
                "The transcript segment changed during forced alignment.",
                details={"segment_id": target.segment_id},
            )
        current_plan = self.preprocessor.get_plan(job_id)
        if current_plan.normalized_sha256 != plan.normalized_sha256:
            raise ForcedAlignmentFailed("Normalized audio changed during forced alignment.")
        current_alignment = self._current_alignment_checkpoint(
            job_id,
            expected_generation=alignment_checkpoint.generation,
        )
        if current_alignment.payload_sha256 != alignment_checkpoint.payload_sha256:
            raise ForcedAlignmentFailed("The alignment report changed during forced alignment.")

    def _current_alignment_checkpoint(
        self,
        job_id: str,
        *,
        expected_generation: int,
    ) -> CheckpointRecord:
        checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=CHECKPOINT_STAGE),
            ALIGNMENT_CHECKPOINT_KEY,
        )
        if checkpoint is None or checkpoint.generation != expected_generation:
            raise ForcedAlignmentFailed("Forced alignment requires the current alignment report.")
        return checkpoint

    def _list_all_segments(self, job_id: str) -> list[TranscriptSegment]:
        segments: list[TranscriptSegment] = []
        after_sequence = 0
        while True:
            snapshot = self.store.get_job_snapshot(
                job_id,
                after_segment_sequence=after_sequence,
                segment_limit=500,
            )
            segments.extend(snapshot.stable_segments)
            if not snapshot.has_more_segments:
                return segments
            if snapshot.next_after_segment_sequence <= after_sequence:
                raise ForcedAlignmentFailed(
                    "Transcript pagination did not advance during forced alignment."
                )
            after_sequence = snapshot.next_after_segment_sequence

    def _find_segment(self, job_id: str, segment_id: str) -> TranscriptSegment:
        return next(
            (
                segment
                for segment in self._list_all_segments(job_id)
                if segment.segment_id == segment_id
            ),
            None,
        ) or _raise_missing_segment(segment_id)

    def _write_private_evidence(
        self,
        job_id: str,
        *,
        segment_id: str,
        raw_sha256: str,
        raw_bytes: bytes,
    ) -> str:
        directory = self.store.get_job_stage_directory(
            job_id,
            stage="forced_alignment_raw",
        )
        path = directory / f"{segment_id}-{raw_sha256[:16]}.json"
        if path.is_symlink():
            raise UploadStorageError(
                "Private forced-alignment evidence must not be a symbolic link."
            )
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise UploadStorageError(
                    "Private forced-alignment evidence could not be verified."
                ) from exc
            if hashlib.sha256(existing).hexdigest() != raw_sha256:
                raise UploadStorageError(
                    "Private forced-alignment evidence has conflicting content."
                )
        else:
            _atomic_write_bytes(path, raw_bytes)
        return path.relative_to(self.store.data_directory).as_posix()

    def _read_private_evidence(
        self,
        job_id: str,
        *,
        relative_path: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        path = (self.store.data_directory / relative_path).resolve()
        root = self.store.get_job_stage_directory(
            job_id,
            stage="forced_alignment_raw",
        ).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise UploadStorageError("Private forced-alignment evidence is unavailable.")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise UploadStorageError(
                "Private forced-alignment evidence could not be read."
            ) from exc
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise UploadStorageError(
                "Private forced-alignment evidence failed checksum verification."
            )
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadStorageError(
                "Private forced-alignment evidence is not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise UploadStorageError("Private forced-alignment evidence is not an object.")
        return payload


def _read_segment_audio(
    path: Path,
    *,
    plan: NormalizedAudioPlan,
    start_ms: int,
    end_ms: int,
) -> tuple[np.ndarray, int, int]:
    start_frame = round(start_ms * plan.sample_rate / 1000)
    end_frame = round(end_ms * plan.sample_rate / 1000)
    if start_frame < 0 or end_frame <= start_frame or end_frame > plan.total_frames:
        raise NormalizedAudioInvalid(
            "The estimated segment does not map to complete normalized PCM."
        )
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getframerate() != plan.sample_rate
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getnframes() != plan.total_frames
            ):
                raise NormalizedAudioInvalid("Normalized audio changed before forced alignment.")
            audio.setpos(start_frame)
            raw = audio.readframes(end_frame - start_frame)
    except (OSError, EOFError, wave.Error) as exc:
        raise NormalizedAudioInvalid(
            "Normalized audio could not be read for forced alignment."
        ) from exc
    samples = np.frombuffer(raw, dtype="<i2").copy()
    if samples.size != end_frame - start_frame:
        raise NormalizedAudioInvalid("Normalized audio ended during forced alignment.")
    return samples, start_frame, end_frame


def _prepare_aligner_audio(audio: np.ndarray, *, sample_rate: int) -> np.ndarray:
    if sample_rate != 16_000:
        raise ForcedAlignmentFailed("The forced aligner requires 16 kHz normalized audio.")
    if audio.ndim != 1 or audio.size == 0:
        raise ForcedAlignmentFailed("The forced aligner received invalid audio.")
    if audio.dtype == np.int16:
        return (audio.astype(np.float32) / 32768.0).copy()
    if audio.dtype != np.float32:
        raise ForcedAlignmentFailed("The forced aligner requires int16 or float32 audio.")
    return audio


def _clamp_alignment_words(
    words: list[dict[str, Any]],
    *,
    duration_seconds: float,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for word in words:
        try:
            start_time = float(word["start_time"])
            end_time = float(word["end_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ForcedAlignmentFailed(
                "The forced aligner returned invalid timing metadata."
            ) from exc
        start_time = max(0.0, min(start_time, duration_seconds))
        end_time = max(start_time, min(end_time, duration_seconds))
        normalized.append(
            {
                "text": word["text"],
                "start_time": round(start_time, 6),
                "end_time": round(end_time, 6),
            }
        )
    return normalized


def _validate_alignment_words(
    words: Any,
    *,
    stable_text: str,
    segment_start_ms: int,
    segment_end_ms: int,
) -> ValidatedForcedAlignment:
    if not isinstance(words, list) or not words:
        raise ForcedAlignmentFailed("The forced aligner did not return word timing evidence.")
    duration_seconds = (segment_end_ms - segment_start_ms) / 1000
    normalized_words: list[dict[str, Any]] = []
    prior_end = 0.0
    aligned_text_parts: list[str] = []
    for index, word in enumerate(words):
        if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"]:
            raise ForcedAlignmentFailed(
                "The forced aligner returned an invalid word.",
                details={"word_index": index},
            )
        try:
            start_time = float(word["start_time"])
            end_time = float(word["end_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ForcedAlignmentFailed(
                "The forced aligner returned invalid timing metadata.",
                details={"word_index": index},
            ) from exc
        if (
            not math.isfinite(start_time)
            or not math.isfinite(end_time)
            or start_time < 0
            or end_time < start_time
            or start_time < prior_end
            or end_time > duration_seconds
        ):
            raise ForcedAlignmentFailed(
                "The forced aligner returned non-monotonic or out-of-bounds timing.",
                details={"word_index": index},
            )
        normalized_words.append(
            {
                "text": word["text"],
                "start_time": round(start_time, 6),
                "end_time": round(end_time, 6),
            }
        )
        aligned_text_parts.append(word["text"])
        prior_end = end_time

    stable_normalized = _normalize_alignment_text(stable_text)
    aligned_normalized = _normalize_alignment_text("".join(aligned_text_parts))
    if not stable_normalized or aligned_normalized != stable_normalized:
        raise ForcedAlignmentFailed(
            "The forced-alignment evidence does not account for the stable text."
        )
    aligned_start_ms = segment_start_ms + round(normalized_words[0]["start_time"] * 1000)
    aligned_end_ms = segment_start_ms + round(normalized_words[-1]["end_time"] * 1000)
    if (
        aligned_start_ms < segment_start_ms
        or aligned_end_ms > segment_end_ms
        or aligned_end_ms <= aligned_start_ms
    ):
        raise ForcedAlignmentFailed(
            "The forced-alignment result does not produce a positive segment range."
        )
    return ValidatedForcedAlignment(
        aligned_start_ms=aligned_start_ms,
        aligned_end_ms=aligned_end_ms,
        word_count=len(normalized_words),
        normalized_text_sha256=hashlib.sha256(stable_normalized.encode("utf-8")).hexdigest(),
        words=tuple(normalized_words),
    )


def _normalize_alignment_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if character == "'" or unicodedata.category(character).startswith(("L", "N"))
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checkpoint_key(segment_id: str) -> str:
    return f"{FORCED_ALIGNMENT_CHECKPOINT_PREFIX}{segment_id}"


def _checkpoint_by_key(
    checkpoints: list[CheckpointRecord],
    checkpoint_key: str,
) -> CheckpointRecord | None:
    return next(
        (checkpoint for checkpoint in checkpoints if checkpoint.checkpoint_key == checkpoint_key),
        None,
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    if destination.parent.is_symlink() or destination.is_symlink():
        raise UploadStorageError(
            "Private forced-alignment storage must not contain symbolic links."
        )
    temporary_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        file_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    file_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _raise_missing_segment(segment_id: str) -> TranscriptSegment:
    raise ForcedAlignmentFailed(
        "The selected transcript segment no longer exists.",
        details={"segment_id": segment_id},
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
