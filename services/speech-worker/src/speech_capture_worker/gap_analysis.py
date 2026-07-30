"""Conservative PCM evidence for uncovered transcript timeline ranges."""

from __future__ import annotations

import math
import wave
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from speech_capture_worker.alignment import (
    ALIGNMENT_REPORT_SCHEMA_VERSION,
    CHECKPOINT_STAGE,
    TimelineRange,
)
from speech_capture_worker.alignment import (
    CHECKPOINT_KEY as ALIGNMENT_CHECKPOINT_KEY,
)
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.domain import JobRecord, JobState
from speech_capture_worker.errors import InvalidJobRequest, NormalizedAudioInvalid
from speech_capture_worker.job_store import JobStore

GAP_ANALYSIS_SCHEMA_VERSION = "1.0.0"
GAP_ANALYSIS_CHECKPOINT_KEY = "gap_audio_evidence"
DEFAULT_WINDOW_MS = 20
DEFAULT_MIN_DEFINITE_SILENCE_MS = 100
DEFAULT_DEFINITE_SILENCE_PEAK = 8


class GapEvidenceClassification(StrEnum):
    DEFINITE_SILENCE = "definite_silence"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class GapAudioEvidence:
    start_ms: int
    end_ms: int
    start_frame: int
    end_frame: int
    frame_count: int
    duration_ms: int
    peak_absolute_amplitude: int
    rms_amplitude: float
    nonzero_sample_ratio: float
    quiet_window_ratio: float
    classification: GapEvidenceClassification
    reason_code: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["classification"] = self.classification.value
        return payload


@dataclass(frozen=True)
class GapAnalysisReport:
    schema_version: str
    alignment_report_schema_version: str
    alignment_report_generation: int
    alignment_report_sha256: str
    source_duration_ms: int
    normalized_sha256: str
    sample_rate: int
    window_ms: int
    minimum_definite_silence_ms: int
    definite_silence_peak_threshold: int
    gap_count: int
    definite_silence_count: int
    unresolved_count: int
    evidence: tuple[GapAudioEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "alignment_report_schema_version": self.alignment_report_schema_version,
            "alignment_report_generation": self.alignment_report_generation,
            "alignment_report_sha256": self.alignment_report_sha256,
            "source_duration_ms": self.source_duration_ms,
            "normalized_sha256": self.normalized_sha256,
            "sample_rate": self.sample_rate,
            "window_ms": self.window_ms,
            "minimum_definite_silence_ms": self.minimum_definite_silence_ms,
            "definite_silence_peak_threshold": self.definite_silence_peak_threshold,
            "gap_count": self.gap_count,
            "definite_silence_count": self.definite_silence_count,
            "unresolved_count": self.unresolved_count,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class GapAnalysisResult:
    job: JobRecord
    report: GapAnalysisReport
    checkpoint_generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "report": self.report.to_dict(),
            "checkpoint_generation": self.checkpoint_generation,
        }


class TranscriptGapAnalyzer:
    """Measure gaps without claiming uncertain audio is non-speech."""

    def __init__(
        self,
        store: JobStore,
        *,
        preprocessor: AudioPreprocessor | None = None,
        window_ms: int = DEFAULT_WINDOW_MS,
        minimum_definite_silence_ms: int = DEFAULT_MIN_DEFINITE_SILENCE_MS,
        definite_silence_peak_threshold: int = DEFAULT_DEFINITE_SILENCE_PEAK,
    ) -> None:
        if (
            not isinstance(window_ms, int)
            or isinstance(window_ms, bool)
            or window_ms < 5
            or window_ms > 1000
        ):
            raise InvalidJobRequest("window_ms must be between 5 and 1000.")
        if (
            not isinstance(minimum_definite_silence_ms, int)
            or isinstance(minimum_definite_silence_ms, bool)
            or minimum_definite_silence_ms < window_ms
            or minimum_definite_silence_ms > 60_000
        ):
            raise InvalidJobRequest("minimum_definite_silence_ms must be at least one window.")
        if (
            not isinstance(definite_silence_peak_threshold, int)
            or isinstance(definite_silence_peak_threshold, bool)
            or definite_silence_peak_threshold < 0
            or definite_silence_peak_threshold > 32_767
        ):
            raise InvalidJobRequest("definite_silence_peak_threshold must be between 0 and 32767.")
        self.store = store
        self.preprocessor = preprocessor or AudioPreprocessor(store)
        self.window_ms = window_ms
        self.minimum_definite_silence_ms = minimum_definite_silence_ms
        self.definite_silence_peak_threshold = definite_silence_peak_threshold

    def analyze(self, job_id: str) -> GapAnalysisResult:
        job = self.store.get_job(job_id)
        if job.state is not JobState.ALIGNING:
            raise InvalidJobRequest("Gap analysis requires an aligning job.")
        source_duration_ms = self.store.get_job_duration_ms(job_id)
        (
            ranges,
            alignment_report_generation,
            alignment_report_sha256,
        ) = self._load_unresolved_ranges(
            job_id,
            source_duration_ms=source_duration_ms,
        )
        plan = self.preprocessor.get_plan(job_id)
        path = self.preprocessor.get_normalized_path(job_id)
        evidence = _analyze_wav_ranges(
            path,
            ranges=ranges,
            sample_rate=plan.sample_rate,
            total_frames=plan.total_frames,
            window_ms=self.window_ms,
            minimum_definite_silence_ms=self.minimum_definite_silence_ms,
            definite_silence_peak_threshold=self.definite_silence_peak_threshold,
        )
        definite_silence_count = sum(
            item.classification is GapEvidenceClassification.DEFINITE_SILENCE for item in evidence
        )
        report = GapAnalysisReport(
            schema_version=GAP_ANALYSIS_SCHEMA_VERSION,
            alignment_report_schema_version=ALIGNMENT_REPORT_SCHEMA_VERSION,
            alignment_report_generation=alignment_report_generation,
            alignment_report_sha256=alignment_report_sha256,
            source_duration_ms=source_duration_ms,
            normalized_sha256=plan.normalized_sha256,
            sample_rate=plan.sample_rate,
            window_ms=self.window_ms,
            minimum_definite_silence_ms=self.minimum_definite_silence_ms,
            definite_silence_peak_threshold=self.definite_silence_peak_threshold,
            gap_count=len(evidence),
            definite_silence_count=definite_silence_count,
            unresolved_count=len(evidence) - definite_silence_count,
            evidence=evidence,
        )
        checkpoint, _ = self.store.put_checkpoint(
            job_id,
            stage=CHECKPOINT_STAGE,
            checkpoint_key=GAP_ANALYSIS_CHECKPOINT_KEY,
            payload=report.to_dict(),
        )
        return GapAnalysisResult(
            job=job,
            report=report,
            checkpoint_generation=checkpoint.generation,
        )

    def _load_unresolved_ranges(
        self,
        job_id: str,
        *,
        source_duration_ms: int,
    ) -> tuple[tuple[TimelineRange, ...], int, str]:
        checkpoint = next(
            (
                value
                for value in self.store.list_checkpoints(
                    job_id,
                    stage=CHECKPOINT_STAGE,
                )
                if value.checkpoint_key == ALIGNMENT_CHECKPOINT_KEY
            ),
            None,
        )
        if checkpoint is None:
            raise InvalidJobRequest("Gap analysis requires a durable transcript alignment report.")
        payload = checkpoint.payload
        if (
            payload.get("schema_version") != ALIGNMENT_REPORT_SCHEMA_VERSION
            or not _is_int(payload.get("source_duration_ms"))
            or payload["source_duration_ms"] != source_duration_ms
            or not isinstance(payload.get("unresolved_ranges"), list)
            or not _is_int(payload.get("unresolved_duration_ms"))
        ):
            raise InvalidJobRequest("The transcript alignment report is invalid.")
        ranges: list[TimelineRange] = []
        cursor = 0
        for raw in payload["unresolved_ranges"]:
            if (
                not isinstance(raw, dict)
                or not _is_int(raw.get("start_ms"))
                or not _is_int(raw.get("end_ms"))
            ):
                raise InvalidJobRequest(
                    "The transcript alignment report contains an invalid range."
                )
            start_ms = raw["start_ms"]
            end_ms = raw["end_ms"]
            if (
                start_ms < cursor
                or start_ms < 0
                or end_ms <= start_ms
                or end_ms > source_duration_ms
            ):
                raise InvalidJobRequest(
                    "The transcript alignment report contains an invalid range."
                )
            ranges.append(TimelineRange(start_ms=start_ms, end_ms=end_ms))
            cursor = end_ms
        if sum(value.duration_ms for value in ranges) != payload["unresolved_duration_ms"]:
            raise InvalidJobRequest("The transcript alignment report is invalid.")
        return tuple(ranges), checkpoint.generation, checkpoint.payload_sha256


def _analyze_wav_ranges(
    path: Path,
    *,
    ranges: tuple[TimelineRange, ...],
    sample_rate: int,
    total_frames: int,
    window_ms: int,
    minimum_definite_silence_ms: int,
    definite_silence_peak_threshold: int,
) -> tuple[GapAudioEvidence, ...]:
    window_frames = max(1, round(window_ms * sample_rate / 1000))
    evidence: list[GapAudioEvidence] = []
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getframerate() != sample_rate
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getnframes() != total_frames
            ):
                raise NormalizedAudioInvalid("Normalized audio changed before gap analysis.")
            for timeline_range in ranges:
                start_frame = min(
                    total_frames,
                    max(0, round(timeline_range.start_ms * sample_rate / 1000)),
                )
                end_frame = min(
                    total_frames,
                    max(start_frame, round(timeline_range.end_ms * sample_rate / 1000)),
                )
                audio.setpos(start_frame)
                raw = audio.readframes(end_frame - start_frame)
                samples = np.frombuffer(raw, dtype="<i2").astype(np.int64)
                if samples.size != end_frame - start_frame:
                    raise NormalizedAudioInvalid("Normalized audio ended during gap analysis.")
                evidence.append(
                    _measure_gap(
                        timeline_range,
                        samples=samples,
                        start_frame=start_frame,
                        end_frame=end_frame,
                        window_frames=window_frames,
                        minimum_definite_silence_ms=minimum_definite_silence_ms,
                        definite_silence_peak_threshold=definite_silence_peak_threshold,
                    )
                )
    except (OSError, EOFError, wave.Error) as exc:
        raise NormalizedAudioInvalid(
            "Normalized audio could not be read for gap analysis."
        ) from exc
    return tuple(evidence)


def _measure_gap(
    timeline_range: TimelineRange,
    *,
    samples: np.ndarray,
    start_frame: int,
    end_frame: int,
    window_frames: int,
    minimum_definite_silence_ms: int,
    definite_silence_peak_threshold: int,
) -> GapAudioEvidence:
    absolute = np.abs(samples)
    peak = int(absolute.max(initial=0))
    rms = math.sqrt(float(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0
    nonzero_ratio = float(np.count_nonzero(samples) / samples.size) if samples.size else 0
    window_peaks = [
        int(absolute[start : start + window_frames].max(initial=0))
        for start in range(0, samples.size, window_frames)
    ]
    quiet_windows = sum(value <= definite_silence_peak_threshold for value in window_peaks)
    quiet_window_ratio = quiet_windows / len(window_peaks) if window_peaks else 0
    duration_ms = timeline_range.duration_ms
    pcm_range_available = end_frame > start_frame and samples.size > 0
    definite_silence = (
        pcm_range_available
        and duration_ms >= minimum_definite_silence_ms
        and peak <= definite_silence_peak_threshold
        and quiet_window_ratio == 1
    )
    if definite_silence:
        classification = GapEvidenceClassification.DEFINITE_SILENCE
        reason_code = "PCM_NEAR_DIGITAL_SILENCE"
    elif not pcm_range_available:
        classification = GapEvidenceClassification.UNRESOLVED
        reason_code = "PCM_RANGE_UNAVAILABLE"
    elif duration_ms < minimum_definite_silence_ms:
        classification = GapEvidenceClassification.UNRESOLVED
        reason_code = "GAP_TOO_SHORT_FOR_SILENCE_CLAIM"
    else:
        classification = GapEvidenceClassification.UNRESOLVED
        reason_code = "AUDIBLE_OR_UNCERTAIN_PCM"
    return GapAudioEvidence(
        start_ms=timeline_range.start_ms,
        end_ms=timeline_range.end_ms,
        start_frame=start_frame,
        end_frame=end_frame,
        frame_count=end_frame - start_frame,
        duration_ms=duration_ms,
        peak_absolute_amplitude=peak,
        rms_amplitude=round(rms, 6),
        nonzero_sample_ratio=round(nonzero_ratio, 8),
        quiet_window_ratio=round(quiet_window_ratio, 8),
        classification=classification,
        reason_code=reason_code,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
