"""Evidence-only speech-activity observations for unresolved transcript gaps."""

from __future__ import annotations

import importlib.metadata
import math
import re
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from speech_capture_worker.alignment import CHECKPOINT_STAGE
from speech_capture_worker.audio_preprocessing import AudioPreprocessor, NormalizedAudioPlan
from speech_capture_worker.domain import JobRecord, JobState, ModelProfile, ResourceStatus
from speech_capture_worker.errors import (
    InvalidJobRequest,
    NormalizedAudioInvalid,
    SpeechActivityDetectionFailed,
)
from speech_capture_worker.gap_analysis import (
    GAP_ANALYSIS_CHECKPOINT_KEY,
    GapAudioEvidence,
    GapEvidenceClassification,
    TranscriptGapAnalyzer,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import GIB, ResourceReport, check_resource_preflight

SPEECH_ACTIVITY_SCHEMA_VERSION = "1.0.0"
SPEECH_ACTIVITY_CHECKPOINT_KEY = "gap_speech_activity_evidence"
SPEECH_ACTIVITY_RESOURCE_CHECKPOINT_KEY = "gap_speech_activity_resource_boundary"
SPEECH_ACTIVITY_HEADROOM_BYTES = 2 * GIB
PYANNOTE_SEGMENTATION_MODEL_ID = "pyannote/segmentation"
PYANNOTE_CONFIGURATION_ID = "dihard3-default-v1"
PYANNOTE_PARAMETERS = {
    "onset": 0.767,
    "offset": 0.377,
    "min_duration_on": 0.136,
    "min_duration_off": 0.067,
}
MAX_DETECTOR_BOUNDARY_OVERRUN_SECONDS = 0.05
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SpeechActivityDetectorIdentity:
    detector_id: str
    detector_version: str
    model_id: str
    model_revision: str
    configuration_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class DetectedSpeechRegion:
    start_seconds: float
    end_seconds: float


class SpeechActivityDetector(Protocol):
    identity: SpeechActivityDetectorIdentity

    def detect(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
    ) -> tuple[DetectedSpeechRegion, ...]: ...


class PyannoteVoiceActivityDetector:
    """Lazy adapter around one revision-pinned pyannote VAD model."""

    def __init__(
        self,
        *,
        model_revision: str,
        cache_dir: Path,
        pipeline_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not isinstance(model_revision, str) or not _COMMIT_SHA_PATTERN.fullmatch(
            model_revision
        ):
            raise InvalidJobRequest(
                "model_revision must be a full lowercase 40-character commit SHA."
            )
        try:
            detector_version = importlib.metadata.version("pyannote.audio")
        except importlib.metadata.PackageNotFoundError as exc:
            raise SpeechActivityDetectionFailed(
                "pyannote.audio is not installed; install the diarization extra first."
            ) from exc
        self.identity = SpeechActivityDetectorIdentity(
            detector_id="pyannote_voice_activity_detection",
            detector_version=detector_version,
            model_id=PYANNOTE_SEGMENTATION_MODEL_ID,
            model_revision=model_revision,
            configuration_id=PYANNOTE_CONFIGURATION_ID,
        )
        self._cache_dir = cache_dir
        self._pipeline_factory = pipeline_factory
        self._pipeline: Any | None = None

    def detect(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int,
    ) -> tuple[DetectedSpeechRegion, ...]:
        if sample_rate != 16_000:
            raise SpeechActivityDetectionFailed(
                "The pyannote speech-activity detector requires 16 kHz audio."
            )
        if audio.ndim != 1 or audio.dtype != np.float32:
            raise SpeechActivityDetectionFailed(
                "The speech-activity detector received invalid normalized audio."
            )
        try:
            pipeline = self._load_pipeline()
            import torch

            annotation = pipeline(
                {
                    "waveform": torch.from_numpy(audio).unsqueeze(0),
                    "sample_rate": sample_rate,
                    "uri": "speech-capture-gap-evaluation",
                }
            )
            timeline = annotation.get_timeline().support()
            return tuple(
                DetectedSpeechRegion(
                    start_seconds=float(segment.start),
                    end_seconds=float(segment.end),
                )
                for segment in timeline
            )
        except SpeechActivityDetectionFailed:
            raise
        except Exception as exc:
            raise SpeechActivityDetectionFailed(
                "The revision-pinned speech-activity detector could not run."
            ) from exc

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        if self._pipeline_factory is not None:
            self._pipeline = self._pipeline_factory()
            return self._pipeline
        try:
            from pyannote.audio.pipelines import VoiceActivityDetection

            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._pipeline = VoiceActivityDetection(
                segmentation={
                    "checkpoint": self.identity.model_id,
                    "revision": self.identity.model_revision,
                },
                cache_dir=self._cache_dir,
            ).instantiate(PYANNOTE_PARAMETERS)
        except Exception as exc:
            raise SpeechActivityDetectionFailed(
                "The revision-pinned pyannote VAD model could not be loaded."
            ) from exc
        return self._pipeline


class SpeechActivityObservation(StrEnum):
    SPEECH_DETECTED = "speech_detected"
    NO_SPEECH_DETECTED = "no_speech_detected"


@dataclass(frozen=True)
class SpeechRegionEvidence:
    start_ms: int
    end_ms: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class GapSpeechActivityEvidence:
    start_ms: int
    end_ms: int
    duration_ms: int
    speech_duration_ms: int
    speech_ratio: float
    observation: SpeechActivityObservation
    reason_code: str
    materialization_authorized: bool
    speech_regions: tuple[SpeechRegionEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "speech_duration_ms": self.speech_duration_ms,
            "speech_ratio": self.speech_ratio,
            "observation": self.observation.value,
            "reason_code": self.reason_code,
            "materialization_authorized": self.materialization_authorized,
            "speech_regions": [region.to_dict() for region in self.speech_regions],
        }


@dataclass(frozen=True)
class GapSpeechActivityReport:
    schema_version: str
    detector: SpeechActivityDetectorIdentity
    gap_analysis_generation: int
    gap_analysis_sha256: str
    alignment_report_generation: int
    alignment_report_sha256: str
    normalized_sha256: str
    sample_rate: int
    source_duration_ms: int
    evaluated_gap_count: int
    speech_detected_count: int
    no_speech_detected_count: int
    automatic_materialization_authorized: bool
    evidence: tuple[GapSpeechActivityEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "detector": self.detector.to_dict(),
            "gap_analysis_generation": self.gap_analysis_generation,
            "gap_analysis_sha256": self.gap_analysis_sha256,
            "alignment_report_generation": self.alignment_report_generation,
            "alignment_report_sha256": self.alignment_report_sha256,
            "normalized_sha256": self.normalized_sha256,
            "sample_rate": self.sample_rate,
            "source_duration_ms": self.source_duration_ms,
            "evaluated_gap_count": self.evaluated_gap_count,
            "speech_detected_count": self.speech_detected_count,
            "no_speech_detected_count": self.no_speech_detected_count,
            "automatic_materialization_authorized": (
                self.automatic_materialization_authorized
            ),
            "evidence": [item.to_dict() for item in self.evidence],
        }


class GapSpeechActivityOutcome(StrEnum):
    EVIDENCE_RECORDED = "evidence_recorded"
    NO_UNRESOLVED_RANGES = "no_unresolved_ranges"
    SAFE_PAUSED = "safe_paused"


@dataclass(frozen=True)
class GapSpeechActivityResult:
    outcome: GapSpeechActivityOutcome
    job: JobRecord
    report: GapSpeechActivityReport | None
    checkpoint_generation: int | None
    resource_report: ResourceReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "job": self.job.to_dict(),
            "report": self.report.to_dict() if self.report is not None else None,
            "checkpoint_generation": self.checkpoint_generation,
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
        }


BoundaryPreflight = Callable[..., ResourceReport]


class GapSpeechActivityAnalyzer:
    """Record detector observations without authorizing transcript outcomes."""

    def __init__(
        self,
        store: JobStore,
        detector: SpeechActivityDetector,
        *,
        gap_analyzer: TranscriptGapAnalyzer | None = None,
        preprocessor: AudioPreprocessor | None = None,
        boundary_preflight: BoundaryPreflight = check_resource_preflight,
    ) -> None:
        _validate_detector_identity(detector.identity)
        self.store = store
        self.detector = detector
        self.gap_analyzer = gap_analyzer or TranscriptGapAnalyzer(store)
        self.preprocessor = preprocessor or AudioPreprocessor(store)
        self._boundary_preflight = boundary_preflight

    def analyze(self, job_id: str) -> GapSpeechActivityResult:
        job = self.store.get_job(job_id)
        if job.state is not JobState.ALIGNING:
            raise InvalidJobRequest("Speech-activity analysis requires an aligning job.")

        gap_analysis = self.gap_analyzer.analyze(job_id)
        gap_checkpoint = _checkpoint_by_key(
            self.store,
            job_id,
            GAP_ANALYSIS_CHECKPOINT_KEY,
        )
        if (
            gap_checkpoint is None
            or gap_checkpoint.generation != gap_analysis.checkpoint_generation
            or gap_checkpoint.payload != gap_analysis.report.to_dict()
        ):
            raise InvalidJobRequest("The durable gap evidence changed before VAD evaluation.")

        unresolved = tuple(
            item
            for item in gap_analysis.report.evidence
            if item.classification is GapEvidenceClassification.UNRESOLVED
        )
        resource_report: ResourceReport | None = None
        normalized_regions: tuple[tuple[int, int], ...] = ()
        plan = self.preprocessor.get_plan(job_id)

        if unresolved:
            resource_report = self._boundary_preflight(
                self.store.data_directory,
                estimated_required_bytes=SPEECH_ACTIVITY_HEADROOM_BYTES,
                model_profile=ModelProfile.SPEED,
            )
            self.store.put_checkpoint(
                job_id,
                stage=CHECKPOINT_STAGE,
                checkpoint_key=SPEECH_ACTIVITY_RESOURCE_CHECKPOINT_KEY,
                payload={
                    "schema_version": SPEECH_ACTIVITY_SCHEMA_VERSION,
                    "detector": self.detector.identity.to_dict(),
                    "gap_analysis_generation": gap_checkpoint.generation,
                    "gap_analysis_sha256": gap_checkpoint.payload_sha256,
                    "resource_report": resource_report.to_dict(),
                },
            )
            if resource_report.status is ResourceStatus.BLOCKED:
                return GapSpeechActivityResult(
                    outcome=GapSpeechActivityOutcome.SAFE_PAUSED,
                    job=job,
                    report=None,
                    checkpoint_generation=None,
                    resource_report=resource_report,
                )
            audio = _read_normalized_audio(
                self.preprocessor.get_normalized_path(job_id),
                plan=plan,
            )
            detected = self.detector.detect(audio, sample_rate=plan.sample_rate)
            normalized_regions = validate_detected_speech_regions(
                detected,
                sample_rate=plan.sample_rate,
                total_frames=plan.total_frames,
            )
            if self.preprocessor.get_plan(job_id) != plan:
                raise NormalizedAudioInvalid(
                    "Normalized audio changed during speech-activity analysis."
                )

        evidence = tuple(
            _build_gap_evidence(
                gap,
                normalized_regions=normalized_regions,
                sample_rate=plan.sample_rate,
            )
            for gap in unresolved
        )
        speech_detected_count = sum(
            item.observation is SpeechActivityObservation.SPEECH_DETECTED for item in evidence
        )
        report = GapSpeechActivityReport(
            schema_version=SPEECH_ACTIVITY_SCHEMA_VERSION,
            detector=self.detector.identity,
            gap_analysis_generation=gap_checkpoint.generation,
            gap_analysis_sha256=gap_checkpoint.payload_sha256,
            alignment_report_generation=(gap_analysis.report.alignment_report_generation),
            alignment_report_sha256=gap_analysis.report.alignment_report_sha256,
            normalized_sha256=gap_analysis.report.normalized_sha256,
            sample_rate=gap_analysis.report.sample_rate,
            source_duration_ms=gap_analysis.report.source_duration_ms,
            evaluated_gap_count=len(evidence),
            speech_detected_count=speech_detected_count,
            no_speech_detected_count=len(evidence) - speech_detected_count,
            automatic_materialization_authorized=False,
            evidence=evidence,
        )

        current_gap_checkpoint = _checkpoint_by_key(
            self.store,
            job_id,
            GAP_ANALYSIS_CHECKPOINT_KEY,
        )
        current_job = self.store.get_job(job_id)
        if (
            current_job.state is not JobState.ALIGNING
            or current_gap_checkpoint is None
            or current_gap_checkpoint.generation != gap_checkpoint.generation
            or current_gap_checkpoint.payload_sha256 != gap_checkpoint.payload_sha256
        ):
            raise InvalidJobRequest("The gap evidence changed during VAD evaluation.")
        checkpoint, _ = self.store.put_checkpoint(
            job_id,
            stage=CHECKPOINT_STAGE,
            checkpoint_key=SPEECH_ACTIVITY_CHECKPOINT_KEY,
            payload=report.to_dict(),
        )
        return GapSpeechActivityResult(
            outcome=(
                GapSpeechActivityOutcome.EVIDENCE_RECORDED
                if evidence
                else GapSpeechActivityOutcome.NO_UNRESOLVED_RANGES
            ),
            job=current_job,
            report=report,
            checkpoint_generation=checkpoint.generation,
            resource_report=resource_report,
        )


def _validate_detector_identity(identity: SpeechActivityDetectorIdentity) -> None:
    for name, value in asdict(identity).items():
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 200
            or any(not character.isprintable() for character in value)
        ):
            raise InvalidJobRequest(f"Speech-activity detector {name} is invalid.")


def _read_normalized_audio(path: Path, *, plan: NormalizedAudioPlan) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getframerate() != plan.sample_rate
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getnframes() != plan.total_frames
            ):
                raise NormalizedAudioInvalid(
                    "Normalized audio changed before speech-activity analysis."
                )
            raw = audio.readframes(plan.total_frames)
    except (OSError, EOFError, wave.Error) as exc:
        raise NormalizedAudioInvalid(
            "Normalized audio could not be read for speech-activity analysis."
        ) from exc
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size != plan.total_frames:
        raise NormalizedAudioInvalid(
            "Normalized audio ended during speech-activity analysis."
        )
    return (samples.astype(np.float32) / 32768.0).copy()


def validate_detected_speech_regions(
    regions: tuple[DetectedSpeechRegion, ...],
    *,
    sample_rate: int,
    total_frames: int,
) -> tuple[tuple[int, int], ...]:
    if not isinstance(regions, tuple):
        raise SpeechActivityDetectionFailed("The detector returned an invalid region collection.")
    normalized: list[tuple[int, int]] = []
    cursor = 0
    duration_seconds = total_frames / sample_rate
    for region in regions:
        if (
            not isinstance(region, DetectedSpeechRegion)
            or not math.isfinite(region.start_seconds)
            or not math.isfinite(region.end_seconds)
            or region.start_seconds < 0
            or region.end_seconds <= region.start_seconds
            or region.end_seconds
            > duration_seconds + MAX_DETECTOR_BOUNDARY_OVERRUN_SECONDS
        ):
            raise SpeechActivityDetectionFailed("The detector returned an invalid speech region.")
        start_frame = min(total_frames, max(0, round(region.start_seconds * sample_rate)))
        end_frame = min(total_frames, max(start_frame, round(region.end_seconds * sample_rate)))
        if start_frame < cursor or end_frame <= start_frame:
            raise SpeechActivityDetectionFailed(
                "Detector speech regions must be ordered, non-overlapping, and non-empty."
            )
        normalized.append((start_frame, end_frame))
        cursor = end_frame
    return tuple(normalized)


def _build_gap_evidence(
    gap: GapAudioEvidence,
    *,
    normalized_regions: tuple[tuple[int, int], ...],
    sample_rate: int,
) -> GapSpeechActivityEvidence:
    intersections: list[tuple[int, int]] = []
    for start_frame, end_frame in normalized_regions:
        overlap_start = max(gap.start_frame, start_frame)
        overlap_end = min(gap.end_frame, end_frame)
        if overlap_end > overlap_start:
            intersections.append((overlap_start, overlap_end))
    speech_frames = sum(end - start for start, end in intersections)
    speech_duration_ms = round(speech_frames * 1000 / sample_rate)
    speech_regions = tuple(
        SpeechRegionEvidence(
            start_ms=round(start * 1000 / sample_rate),
            end_ms=round(end * 1000 / sample_rate),
        )
        for start, end in intersections
    )
    detected = bool(intersections)
    return GapSpeechActivityEvidence(
        start_ms=gap.start_ms,
        end_ms=gap.end_ms,
        duration_ms=gap.duration_ms,
        speech_duration_ms=speech_duration_ms,
        speech_ratio=round(speech_frames / gap.frame_count, 8) if gap.frame_count else 0,
        observation=(
            SpeechActivityObservation.SPEECH_DETECTED
            if detected
            else SpeechActivityObservation.NO_SPEECH_DETECTED
        ),
        reason_code=(
            "DETECTOR_RETURNED_SPEECH_REGIONS"
            if detected
            else "DETECTOR_RETURNED_NO_SPEECH_REGIONS"
        ),
        materialization_authorized=False,
        speech_regions=speech_regions,
    )


def _checkpoint_by_key(
    store: JobStore,
    job_id: str,
    checkpoint_key: str,
):
    return next(
        (
            checkpoint
            for checkpoint in store.list_checkpoints(job_id, stage=CHECKPOINT_STAGE)
            if checkpoint.checkpoint_key == checkpoint_key
        ),
        None,
    )
