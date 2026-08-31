"""Restart-safe anonymous speaker attribution over stable transcript segments."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
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
    CHECKPOINT_STAGE as ALIGNMENT_STAGE,
)
from speech_capture_worker.audio_preprocessing import (
    AudioPreprocessor,
    NormalizedAudioPlan,
)
from speech_capture_worker.domain import CheckpointRecord, JobRecord, JobState, ResourceStatus
from speech_capture_worker.errors import (
    DiarizationFailed,
    InvalidJobRequest,
    NormalizedAudioInvalid,
    UploadStorageError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import GIB, ResourceReport, check_resource_preflight
from speech_capture_worker.transcript import (
    DiarizationStatus,
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptSegment,
    chronological_segments,
)

DIARIZATION_SCHEMA_VERSION = "1.0.0"
DIARIZATION_RAW_SCHEMA_VERSION = "1.0.0"
DIARIZATION_MODEL_ID = "pyannote/speaker-diarization-3.1"
DIARIZATION_MODEL_REVISION = "84fd25912480287da0247647c3d2b4853cb3ee5d"
DIARIZATION_HEADROOM_BYTES = 3 * GIB
DIARIZATION_STAGE = "diarizing"
DIARIZATION_CHECKPOINT_KEY = "speaker_attribution_evidence"
MAX_DIARIZATION_BOUNDARY_OVERRUN_MS = 50
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpeakerAttribution:
    segment_id: str
    speaker_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SpeakerDiarizationEngine(Protocol):
    model_id: str

    def diarize(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
    ) -> list[dict[str, Any]]: ...


class PyannoteSpeakerDiarizationEngine:
    """Lazy adapter around a revision-pinned pyannote speaker pipeline."""

    model_id = DIARIZATION_MODEL_ID

    def __init__(
        self,
        *,
        model_revision: str = DIARIZATION_MODEL_REVISION,
        cache_dir: Path | None = None,
    ) -> None:
        if not isinstance(model_revision, str) or not _COMMIT_SHA_PATTERN.fullmatch(
            model_revision
        ):
            raise InvalidJobRequest(
                "model_revision must be a full lowercase 40-character commit SHA."
            )
        self.model_revision = model_revision
        self._cache_dir = cache_dir
        self._pipeline: Any | None = None

    def diarize(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
    ) -> list[dict[str, Any]]:
        if sample_rate != 16_000:
            raise DiarizationFailed("Speaker diarization requires 16 kHz normalized audio.")
        if audio.ndim != 1 or audio.dtype != np.float32 or audio.size == 0:
            raise DiarizationFailed(
                "Speaker diarization received invalid normalized audio."
            )
        pipeline = self._load_pipeline()
        import torch

        annotation = pipeline(
            {
                "waveform": torch.from_numpy(audio).unsqueeze(0),
                "sample_rate": sample_rate,
            }
        )
        if isinstance(annotation, dict):
            annotation = annotation.get("diarization") or annotation.get("annotation")
        if annotation is None:
            return []
        if hasattr(annotation, "speaker_diarization"):
            annotation = annotation.speaker_diarization
        turns: list[dict[str, Any]] = []
        for segment, _, speaker in annotation.itertracks(yield_label=True):
            turns.append(
                {
                    "start_ms": round(segment.start * 1000),
                    "end_ms": round(segment.end * 1000),
                    "speaker": str(speaker),
                }
            )
        return turns

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        from huggingface_hub import get_token
        from pyannote.audio import Pipeline

        token = get_token()
        self._pipeline = Pipeline.from_pretrained(
            self.model_id,
            revision=self.model_revision,
            token=token,
            cache_dir=str(self._cache_dir) if self._cache_dir is not None else None,
        )
        return self._pipeline


class DiarizationOutcome(StrEnum):
    COMPLETED = "completed"
    REPLAYED = "replayed"
    SAFE_PAUSED = "safe_paused"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True)
class DiarizationResult:
    outcome: DiarizationOutcome
    job: JobRecord
    evidence_checkpoint_generation: int | None
    speaker_turn_count: int
    attributed_segment_count: int
    unavailable_segment_count: int
    resource_report: ResourceReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict(),
            "evidence_checkpoint_generation": self.evidence_checkpoint_generation,
            "speaker_turn_count": self.speaker_turn_count,
            "attributed_segment_count": self.attributed_segment_count,
            "unavailable_segment_count": self.unavailable_segment_count,
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
        }


BoundaryPreflight = Callable[..., ResourceReport]


class SpeakerDiarizationExecutor:
    """Attach anonymous speakers to stable transcribed segments once, durably."""

    def __init__(
        self,
        store: JobStore,
        engine: SpeakerDiarizationEngine,
        *,
        preprocessor: AudioPreprocessor | None = None,
        boundary_preflight: BoundaryPreflight = check_resource_preflight,
    ) -> None:
        if (
            not isinstance(engine.model_id, str)
            or not engine.model_id
            or len(engine.model_id) > 200
            or any(not character.isprintable() for character in engine.model_id)
        ):
            raise InvalidJobRequest("Speaker-diarization engine model_id is invalid.")
        self.store = store
        self.engine = engine
        self.preprocessor = preprocessor or AudioPreprocessor(store)
        self._boundary_preflight = boundary_preflight

    def run(self, job_id: str) -> DiarizationResult:
        job = self.store.get_job(job_id)
        if job.state is JobState.STRUCTURING:
            return DiarizationResult(
                outcome=DiarizationOutcome.ALREADY_COMPLETED,
                job=job,
                evidence_checkpoint_generation=None,
                speaker_turn_count=0,
                attributed_segment_count=0,
                unavailable_segment_count=0,
                resource_report=None,
            )
        if job.state is not JobState.DIARIZING:
            raise InvalidJobRequest(
                "Speaker diarization requires a diarizing or structuring job."
            )

        alignment_checkpoint = self._current_alignment_checkpoint(job_id)
        plan = self.preprocessor.get_plan(job_id)
        segments = self._list_all_segments(job_id)
        segments_sha256 = _segments_identity_sha256(segments)
        source_duration_ms = self.store.get_job_duration_ms(job_id)
        evidence = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=DIARIZATION_STAGE),
            DIARIZATION_CHECKPOINT_KEY,
        )
        resource_report: ResourceReport | None = None
        replayed = evidence is not None
        if evidence is None:
            resource_report = self._boundary_preflight(
                self.store.data_directory,
                estimated_required_bytes=DIARIZATION_HEADROOM_BYTES,
                model_profile=job.model_profile,
            )
            self.store.put_checkpoint(
                job_id,
                stage=DIARIZATION_STAGE,
                checkpoint_key="diarization_resource_boundary",
                payload=resource_report.to_dict(),
            )
            if resource_report.status is ResourceStatus.BLOCKED:
                current = self.store.get_job(job_id)
                paused = self.store.transition_job(
                    job_id,
                    JobState.PAUSED,
                    expected_revision=current.revision,
                    reason_code="diarization_resource_blocked",
                    error_code="DIARIZATION_RESOURCE_BLOCKED",
                    error_message=(
                        "Worker resources must recover before speaker diarization can start."
                    ),
                    event_type="resource.safe_paused",
                )
                return DiarizationResult(
                    outcome=DiarizationOutcome.SAFE_PAUSED,
                    job=paused,
                    evidence_checkpoint_generation=None,
                    speaker_turn_count=0,
                    attributed_segment_count=0,
                    unavailable_segment_count=0,
                    resource_report=resource_report,
                )

            audio = _read_normalized_audio(
                self.preprocessor.get_normalized_path(job_id),
                plan=plan,
            )
            started = time.monotonic()
            unavailable_reason: str | None = None
            try:
                raw_turns = self.engine.diarize(audio, sample_rate=plan.sample_rate)
            except DiarizationFailed:
                raise
            except Exception as exc:
                unavailable_reason = type(exc).__name__
                raw_turns = []
            elapsed_seconds = time.monotonic() - started
            turns = _validate_turns(
                raw_turns,
                source_duration_ms=source_duration_ms,
            )
            attributions = _assign_speakers(turns, segments)
            raw_payload = {
                "schema_version": DIARIZATION_RAW_SCHEMA_VERSION,
                "model_id": self.engine.model_id,
                "model_revision": getattr(self.engine, "model_revision", None),
                "normalized_sha256": plan.normalized_sha256,
                "sample_rate": plan.sample_rate,
                "source_duration_ms": source_duration_ms,
                "segments_sha256": segments_sha256,
                "unavailable_reason_code": unavailable_reason,
                "turns": [turn.to_dict() for turn in turns],
                "attributions": [attribution.to_dict() for attribution in attributions],
            }
            raw_bytes = _canonical_json(raw_payload).encode("utf-8")
            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            raw_relative_path = self._write_private_evidence(
                job_id,
                raw_sha256=raw_sha256,
                raw_bytes=raw_bytes,
            )
            evidence, _ = self.store.put_checkpoint(
                job_id,
                stage=DIARIZATION_STAGE,
                checkpoint_key=DIARIZATION_CHECKPOINT_KEY,
                payload={
                    "schema_version": DIARIZATION_SCHEMA_VERSION,
                    "alignment_report_generation": alignment_checkpoint.generation,
                    "alignment_report_sha256": alignment_checkpoint.payload_sha256,
                    "model_id": self.engine.model_id,
                    "model_revision": getattr(self.engine, "model_revision", None),
                    "normalized_sha256": plan.normalized_sha256,
                    "sample_rate": plan.sample_rate,
                    "source_duration_ms": source_duration_ms,
                    "segments_sha256": segments_sha256,
                    "turn_count": len(turns),
                    "attribution_count": len(attributions),
                    "unavailable_reason_code": unavailable_reason,
                    "raw_relative_path": raw_relative_path,
                    "raw_sha256": raw_sha256,
                    "elapsed_seconds": round(elapsed_seconds, 6),
                },
            )
        else:
            turns, attributions = self._load_durable_evidence(
                job_id,
                evidence=evidence,
                plan=plan,
                alignment_checkpoint=alignment_checkpoint,
                segments_sha256=segments_sha256,
                source_duration_ms=source_duration_ms,
            )

        attributed_segment_count = 0
        unavailable_segment_count = 0
        attribution_by_segment = {
            attribution.segment_id: attribution for attribution in attributions
        }
        for segment in segments:
            if segment.outcome is not TranscriptOutcome.TRANSCRIBED:
                continue
            attribution = attribution_by_segment.get(segment.segment_id)
            speaker_id = attribution.speaker_id if attribution is not None else None
            status = (
                SpeakerLabelStatus.ANONYMOUS
                if speaker_id is not None
                else SpeakerLabelStatus.UNAVAILABLE
            )
            if speaker_id is not None:
                attributed_segment_count += 1
            else:
                unavailable_segment_count += 1
            if segment.speaker_id == speaker_id and segment.speaker_label_status == status:
                continue
            self.store.update_transcript_segment_metadata(
                job_id,
                segment.segment_id,
                expected_revision=segment.revision,
                speaker_id=speaker_id,
                speaker_label_status=status,
            )

        current_segments = self._list_all_segments(job_id)
        if _segments_identity_sha256(current_segments) != segments_sha256:
            raise DiarizationFailed(
                "The transcript timeline changed during speaker diarization."
            )
        prior_progress = self.store.get_job_snapshot(job_id).progress
        prior_elapsed_seconds = (
            float(prior_progress.elapsed_seconds) if prior_progress is not None else 0.0
        )
        elapsed_seconds = prior_elapsed_seconds + float(
            evidence.payload.get("elapsed_seconds", 0) or 0
        )
        self.store.put_job_progress(
            job_id,
            processed_ms=source_duration_ms,
            stage_progress=1.0,
            elapsed_seconds=elapsed_seconds,
            diarization_status=DiarizationStatus.READY,
        )
        current = self.store.get_job(job_id)
        structuring = self.store.transition_job(
            job_id,
            JobState.STRUCTURING,
            expected_revision=current.revision,
            reason_code="speaker_attribution_complete",
            event_type="job.diarization_completed",
        )
        return DiarizationResult(
            outcome=(
                DiarizationOutcome.REPLAYED if replayed else DiarizationOutcome.COMPLETED
            ),
            job=structuring,
            evidence_checkpoint_generation=evidence.generation,
            speaker_turn_count=len(turns),
            attributed_segment_count=attributed_segment_count,
            unavailable_segment_count=unavailable_segment_count,
            resource_report=resource_report,
        )

    def _load_durable_evidence(
        self,
        job_id: str,
        *,
        evidence: CheckpointRecord,
        plan: NormalizedAudioPlan,
        alignment_checkpoint: CheckpointRecord,
        segments_sha256: str,
        source_duration_ms: int,
    ) -> tuple[tuple[SpeakerTurn, ...], tuple[SpeakerAttribution, ...]]:
        payload = evidence.payload
        if (
            payload.get("schema_version") != DIARIZATION_SCHEMA_VERSION
            or payload.get("alignment_report_generation") != alignment_checkpoint.generation
            or payload.get("alignment_report_sha256") != alignment_checkpoint.payload_sha256
            or payload.get("model_id") != self.engine.model_id
            or payload.get("normalized_sha256") != plan.normalized_sha256
            or payload.get("sample_rate") != plan.sample_rate
            or payload.get("source_duration_ms") != source_duration_ms
            or payload.get("segments_sha256") != segments_sha256
            or not isinstance(payload.get("turn_count"), int)
            or not isinstance(payload.get("attribution_count"), int)
            or not isinstance(payload.get("raw_relative_path"), str)
            or not isinstance(payload.get("raw_sha256"), str)
        ):
            raise DiarizationFailed("The durable speaker-diarization evidence is invalid.")
        raw_payload = self._read_private_evidence(
            job_id,
            relative_path=payload["raw_relative_path"],
            expected_sha256=payload["raw_sha256"],
        )
        if (
            raw_payload.get("schema_version") != DIARIZATION_RAW_SCHEMA_VERSION
            or raw_payload.get("model_id") != self.engine.model_id
            or raw_payload.get("normalized_sha256") != plan.normalized_sha256
            or raw_payload.get("sample_rate") != plan.sample_rate
            or raw_payload.get("source_duration_ms") != source_duration_ms
            or raw_payload.get("segments_sha256") != segments_sha256
            or not isinstance(raw_payload.get("turns"), list)
            or not isinstance(raw_payload.get("attributions"), list)
        ):
            raise DiarizationFailed(
                "The private speaker-diarization evidence does not match its checkpoint."
            )
        turns = _validate_turns(
            raw_payload["turns"],
            source_duration_ms=source_duration_ms,
        )
        attributions = _validate_attributions(
            raw_payload["attributions"],
            segments=self._list_all_segments(job_id),
        )
        if (
            len(turns) != payload["turn_count"]
            or len(attributions) != payload["attribution_count"]
        ):
            raise DiarizationFailed(
                "The private speaker-diarization evidence changed after validation."
            )
        return turns, attributions

    def _current_alignment_checkpoint(self, job_id: str) -> CheckpointRecord:
        checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE),
            ALIGNMENT_CHECKPOINT_KEY,
        )
        if checkpoint is None:
            raise DiarizationFailed("Speaker diarization requires the current alignment report.")
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
                return chronological_segments(segments)
            if snapshot.next_after_segment_sequence <= after_sequence:
                raise DiarizationFailed(
                    "Transcript pagination did not advance during speaker diarization."
                )
            after_sequence = snapshot.next_after_segment_sequence

    def _write_private_evidence(
        self,
        job_id: str,
        *,
        raw_sha256: str,
        raw_bytes: bytes,
    ) -> str:
        directory = self.store.get_job_stage_directory(
            job_id,
            stage="diarization_raw",
        )
        path = directory / f"speaker-attribution-{raw_sha256[:16]}.json"
        if path.is_symlink():
            raise UploadStorageError(
                "Private speaker-diarization evidence must not be a symbolic link."
            )
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise UploadStorageError(
                    "Private speaker-diarization evidence could not be verified."
                ) from exc
            if hashlib.sha256(existing).hexdigest() != raw_sha256:
                raise UploadStorageError(
                    "Private speaker-diarization evidence has conflicting content."
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
            stage="diarization_raw",
        ).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise UploadStorageError("Private speaker-diarization evidence is unavailable.")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise UploadStorageError(
                "Private speaker-diarization evidence could not be read."
            ) from exc
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise UploadStorageError(
                "Private speaker-diarization evidence failed checksum verification."
            )
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadStorageError(
                "Private speaker-diarization evidence is not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise UploadStorageError(
                "Private speaker-diarization evidence is not an object."
            )
        return payload


def _validate_turns(
    raw_turns: Any,
    *,
    source_duration_ms: int,
) -> tuple[SpeakerTurn, ...]:
    if not isinstance(raw_turns, list):
        raise DiarizationFailed("The diarization engine did not return a turn list.")
    normalized: list[SpeakerTurn] = []
    for index, raw_turn in enumerate(raw_turns):
        if not isinstance(raw_turn, dict):
            raise DiarizationFailed(
                "The diarization engine returned an invalid speaker turn.",
                details={"turn_index": index},
            )
        start_ms = raw_turn.get("start_ms")
        end_ms = raw_turn.get("end_ms")
        speaker = raw_turn.get("speaker")
        if (
            not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or start_ms < 0
            or start_ms >= source_duration_ms
            or end_ms <= start_ms
            or end_ms > source_duration_ms + MAX_DIARIZATION_BOUNDARY_OVERRUN_MS
            or not isinstance(speaker, str)
            or not speaker
            or any(not character.isprintable() for character in speaker)
        ):
            raise DiarizationFailed(
                "The diarization engine returned an out-of-bounds or malformed turn.",
                details={"turn_index": index},
            )
        normalized.append(
            SpeakerTurn(
                start_ms=start_ms,
                end_ms=min(end_ms, source_duration_ms),
                speaker=speaker,
            )
        )
    return tuple(sorted(normalized, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker)))


def _validate_attributions(
    raw_attributions: Any,
    *,
    segments: list[TranscriptSegment],
) -> tuple[SpeakerAttribution, ...]:
    if not isinstance(raw_attributions, list):
        raise DiarizationFailed("The private attribution list is invalid.")
    segment_ids = {segment.segment_id for segment in segments}
    normalized: list[SpeakerAttribution] = []
    for raw_attribution in raw_attributions:
        if not isinstance(raw_attribution, dict):
            raise DiarizationFailed("A private speaker attribution is invalid.")
        segment_id = raw_attribution.get("segment_id")
        speaker_id = raw_attribution.get("speaker_id")
        if (
            not isinstance(segment_id, str)
            or segment_id not in segment_ids
            or (speaker_id is not None and not isinstance(speaker_id, str))
        ):
            raise DiarizationFailed("A private speaker attribution references unknown data.")
        normalized.append(SpeakerAttribution(segment_id=segment_id, speaker_id=speaker_id))
    return tuple(normalized)


def _assign_speakers(
    turns: tuple[SpeakerTurn, ...],
    segments: list[TranscriptSegment],
) -> tuple[SpeakerAttribution, ...]:
    anonymous_by_raw: dict[str, str] = {}
    for turn in turns:
        if turn.speaker not in anonymous_by_raw:
            anonymous_by_raw[turn.speaker] = f"speaker_{len(anonymous_by_raw) + 1:02d}"
    attributions: list[SpeakerAttribution] = []
    for segment in segments:
        if segment.outcome is not TranscriptOutcome.TRANSCRIBED:
            continue
        best_speaker: str | None = None
        best_overlap = 0
        for turn in turns:
            overlap = min(segment.end_ms, turn.end_ms) - max(
                segment.start_ms, turn.start_ms
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = anonymous_by_raw[turn.speaker]
        attributions.append(
            SpeakerAttribution(segment_id=segment.segment_id, speaker_id=best_speaker)
        )
    return tuple(attributions)


def _read_normalized_audio(
    path: Path,
    *,
    plan: NormalizedAudioPlan,
) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getframerate() != plan.sample_rate
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getnframes() != plan.total_frames
            ):
                raise NormalizedAudioInvalid(
                    "Normalized audio changed before speaker diarization."
                )
            raw = audio.readframes(audio.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise NormalizedAudioInvalid(
            "Normalized audio could not be read for speaker diarization."
        ) from exc
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size != plan.total_frames:
        raise NormalizedAudioInvalid(
            "Normalized audio ended during speaker diarization."
        )
    return (samples.astype(np.float32) / 32768.0).copy()


def _segments_identity_sha256(segments: list[TranscriptSegment]) -> str:
    payload = [
        {
            "segment_id": segment.segment_id,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "outcome": segment.outcome.value,
            "text_sha256": _text_sha256(segment.text or ""),
        }
        for segment in segments
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
            "Private speaker-diarization storage must not contain symbolic links."
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
