"""Local-only labeled evaluation for speech-activity detector candidates."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from speech_capture_worker.domain import ModelProfile
from speech_capture_worker.errors import (
    InvalidJobRequest,
    ResourceBlocked,
    SpeechActivityDetectionFailed,
    VadEvaluationFailed,
)
from speech_capture_worker.gap_speech_activity import (
    SPEECH_ACTIVITY_HEADROOM_BYTES,
    SpeechActivityDetector,
    SpeechActivityDetectorIdentity,
    validate_detected_speech_regions,
)
from speech_capture_worker.media_probe import probe_audio_source
from speech_capture_worker.resources import (
    ResourceReport,
    check_resource_preflight,
)

VAD_GOLD_MANIFEST_SCHEMA_VERSION = "1.0.0"
VAD_EVALUATION_REPORT_SCHEMA_VERSION = "1.0.0"
VAD_SAMPLE_RATE = 16_000
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SAMPLE_COUNT = 100
MAX_LABEL_COUNT_PER_SAMPLE = 10_000
VAD_DETECTION_WINDOW_SECONDS = 10 * 60
VAD_DETECTION_MARGIN_SECONDS = 2.0
_SAFE_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class VadReferenceClass(StrEnum):
    SPEECH = "speech"
    NON_SPEECH = "non_speech"


@dataclass(frozen=True)
class VadReferenceRange:
    start_ms: int
    end_ms: int
    reference_class: VadReferenceClass


@dataclass(frozen=True)
class VadGoldSample:
    sample_id: str
    audio_path: Path
    labels: tuple[VadReferenceRange, ...]


@dataclass(frozen=True)
class VadGoldManifest:
    schema_version: str
    dataset_id: str
    manifest_sha256: str
    samples: tuple[VadGoldSample, ...]


@dataclass(frozen=True)
class VadConfusionMetrics:
    sample_rate: int
    true_positive_frames: int
    false_negative_frames: int
    false_positive_frames: int
    true_negative_frames: int
    speech_reference_frames: int
    non_speech_reference_frames: int
    speech_recall: float | None
    speech_miss_rate: float | None
    non_speech_specificity: float | None
    false_speech_rate: float | None

    @property
    def evaluated_frames(self) -> int:
        return self.speech_reference_frames + self.non_speech_reference_frames

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "true_positive_ms": _frames_to_ms(self.true_positive_frames, self.sample_rate),
            "false_negative_ms": _frames_to_ms(
                self.false_negative_frames, self.sample_rate
            ),
            "false_positive_ms": _frames_to_ms(
                self.false_positive_frames, self.sample_rate
            ),
            "true_negative_ms": _frames_to_ms(self.true_negative_frames, self.sample_rate),
            "speech_reference_ms": _frames_to_ms(
                self.speech_reference_frames, self.sample_rate
            ),
            "non_speech_reference_ms": _frames_to_ms(
                self.non_speech_reference_frames, self.sample_rate
            ),
            "evaluated_ms": _frames_to_ms(self.evaluated_frames, self.sample_rate),
            "speech_recall": self.speech_recall,
            "speech_miss_rate": self.speech_miss_rate,
            "non_speech_specificity": self.non_speech_specificity,
            "false_speech_rate": self.false_speech_rate,
        }


@dataclass(frozen=True)
class VadSampleEvaluation:
    sample_id: str
    source_sha256: str
    duration_ms: int
    reference_range_count: int
    detected_region_count: int
    metrics: VadConfusionMetrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "source_sha256": self.source_sha256,
            "duration_ms": self.duration_ms,
            "reference_range_count": self.reference_range_count,
            "detected_region_count": self.detected_region_count,
            "metrics": self.metrics.to_dict(),
        }


@dataclass(frozen=True)
class VadAcceptancePolicy:
    max_speech_miss_rate: float
    max_false_speech_rate: float
    minimum_speech_reference_ms: int
    minimum_non_speech_reference_ms: int

    def __post_init__(self) -> None:
        for name in ("max_speech_miss_rate", "max_false_speech_rate"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise InvalidJobRequest(f"{name} must be a finite ratio.")
            if not math.isfinite(value) or value < 0 or value > 1:
                raise InvalidJobRequest(f"{name} must be between 0 and 1.")
        for name in ("minimum_speech_reference_ms", "minimum_non_speech_reference_ms"):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > 86_400_000
            ):
                raise InvalidJobRequest(f"{name} must be a positive bounded duration.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VadAcceptanceResult:
    policy: VadAcceptancePolicy | None
    passed: bool | None
    issue_codes: tuple[str, ...]
    automatic_materialization_authorized: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_supplied": self.policy is not None,
            "policy": self.policy.to_dict() if self.policy is not None else None,
            "passed": self.passed,
            "issue_codes": list(self.issue_codes),
            "automatic_materialization_authorized": (
                self.automatic_materialization_authorized
            ),
        }


@dataclass(frozen=True)
class VadEvaluationReport:
    schema_version: str
    dataset_id: str
    manifest_sha256: str
    detector: SpeechActivityDetectorIdentity
    sample_count: int
    missed_speech_sample_count: int
    false_speech_sample_count: int
    metrics: VadConfusionMetrics
    acceptance: VadAcceptanceResult
    samples: tuple[VadSampleEvaluation, ...]
    resource_report: ResourceReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "manifest_sha256": self.manifest_sha256,
            "detector": self.detector.to_dict(),
            "sample_count": self.sample_count,
            "missed_speech_sample_count": self.missed_speech_sample_count,
            "false_speech_sample_count": self.false_speech_sample_count,
            "metrics": self.metrics.to_dict(),
            "acceptance": self.acceptance.to_dict(),
            "samples": [sample.to_dict() for sample in self.samples],
            "resource_report": self.resource_report.to_dict(),
        }


DecodedAudio = tuple[np.ndarray, str]
AudioDecoder = Callable[[Path], DecodedAudio]
BoundaryPreflight = Callable[..., ResourceReport]


class VadGoldEvaluator:
    """Run one detector over private labeled samples and produce safe metrics."""

    def __init__(
        self,
        detector: SpeechActivityDetector,
        *,
        audio_decoder: AudioDecoder | None = None,
        boundary_preflight: BoundaryPreflight = check_resource_preflight,
        detection_window_seconds: float = VAD_DETECTION_WINDOW_SECONDS,
        detection_margin_seconds: float = VAD_DETECTION_MARGIN_SECONDS,
    ) -> None:
        if (
            not isinstance(detection_window_seconds, (int, float))
            or isinstance(detection_window_seconds, bool)
            or not math.isfinite(detection_window_seconds)
            or detection_window_seconds <= 0
        ):
            raise InvalidJobRequest(
                "detection_window_seconds must be a positive finite duration."
            )
        if (
            not isinstance(detection_margin_seconds, (int, float))
            or isinstance(detection_margin_seconds, bool)
            or not math.isfinite(detection_margin_seconds)
            or detection_margin_seconds < 0
            or detection_margin_seconds >= detection_window_seconds / 2
        ):
            raise InvalidJobRequest(
                "detection_margin_seconds must be non-negative and shorter than half the window."
            )
        self.detector = detector
        self._audio_decoder = audio_decoder or decode_audio_source
        self._boundary_preflight = boundary_preflight
        self._detection_window_seconds = detection_window_seconds
        self._detection_margin_seconds = detection_margin_seconds

    def evaluate(
        self,
        manifest: VadGoldManifest,
        *,
        storage_path: Path,
        policy: VadAcceptancePolicy | None = None,
    ) -> VadEvaluationReport:
        resource_report = self._boundary_preflight(
            storage_path,
            estimated_required_bytes=SPEECH_ACTIVITY_HEADROOM_BYTES,
            model_profile=ModelProfile.SPEED,
        )
        if not resource_report.can_start:
            raise ResourceBlocked(
                "VAD evaluation resource preflight blocked model work.",
                details={"issues": [issue.to_dict() for issue in resource_report.issues]},
            )

        sample_reports: list[VadSampleEvaluation] = []
        for sample in manifest.samples:
            audio, source_sha256 = self._audio_decoder(sample.audio_path)
            if audio.dtype != np.float32 or audio.ndim != 1 or audio.size == 0:
                raise VadEvaluationFailed(
                    "A gold-standard sample did not decode to valid normalized audio."
                )
            detected_regions = self._detect_in_windows(audio)
            metrics = evaluate_vad_ranges(
                labels=sample.labels,
                detected_regions=detected_regions,
                sample_rate=VAD_SAMPLE_RATE,
                total_frames=audio.size,
            )
            sample_reports.append(
                VadSampleEvaluation(
                    sample_id=sample.sample_id,
                    source_sha256=source_sha256,
                    duration_ms=_frames_to_ms(audio.size, VAD_SAMPLE_RATE),
                    reference_range_count=len(sample.labels),
                    detected_region_count=len(detected_regions),
                    metrics=metrics,
                )
            )

        aggregate = _sum_metrics(tuple(report.metrics for report in sample_reports))
        acceptance = evaluate_acceptance(aggregate, policy=policy)
        return VadEvaluationReport(
            schema_version=VAD_EVALUATION_REPORT_SCHEMA_VERSION,
            dataset_id=manifest.dataset_id,
            manifest_sha256=manifest.manifest_sha256,
            detector=self.detector.identity,
            sample_count=len(sample_reports),
            missed_speech_sample_count=sum(
                report.metrics.false_negative_frames > 0 for report in sample_reports
            ),
            false_speech_sample_count=sum(
                report.metrics.false_positive_frames > 0 for report in sample_reports
            ),
            metrics=aggregate,
            acceptance=acceptance,
            samples=tuple(sample_reports),
            resource_report=resource_report,
        )

    def _detect_in_windows(self, audio: np.ndarray) -> tuple[tuple[int, int], ...]:
        sample_rate = VAD_SAMPLE_RATE
        total_frames = audio.size
        window_frames = max(1, round(self._detection_window_seconds * sample_rate))
        margin_frames = max(0, round(self._detection_margin_seconds * sample_rate))
        frame_regions: list[tuple[int, int]] = []
        core_start = 0
        while core_start < total_frames:
            core_end = min(core_start + window_frames, total_frames)
            expanded_start = max(0, core_start - margin_frames)
            expanded_end = min(total_frames, core_end + margin_frames)
            window = audio[expanded_start:expanded_end]
            window_regions = validate_detected_speech_regions(
                self.detector.detect(window, sample_rate=sample_rate),
                sample_rate=sample_rate,
                total_frames=window.size,
            )
            for start_frame, end_frame in window_regions:
                absolute_start = expanded_start + start_frame
                absolute_end = expanded_start + end_frame
                clipped_start = max(core_start, absolute_start)
                clipped_end = min(core_end, absolute_end)
                if clipped_end > clipped_start:
                    frame_regions.append((clipped_start, clipped_end))
            core_start = core_end
        return _validate_frame_regions(frame_regions, total_frames=total_frames)


def load_vad_gold_manifest(path: Path) -> VadGoldManifest:
    manifest_path = path.resolve()
    if path.is_symlink() or not manifest_path.is_file():
        raise InvalidJobRequest("The VAD gold manifest must be a regular local file.")
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise InvalidJobRequest("The VAD gold manifest could not be read.") from exc
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise InvalidJobRequest("The VAD gold manifest size is invalid.")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidJobRequest("The VAD gold manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidJobRequest("The VAD gold manifest root must be an object.")
    if payload.get("schema_version") != VAD_GOLD_MANIFEST_SCHEMA_VERSION:
        raise InvalidJobRequest("The VAD gold manifest schema version is unsupported.")
    dataset_id = _safe_identifier("dataset_id", payload.get("dataset_id"))
    raw_samples = payload.get("samples")
    if (
        not isinstance(raw_samples, list)
        or not raw_samples
        or len(raw_samples) > MAX_SAMPLE_COUNT
    ):
        raise InvalidJobRequest("The VAD gold manifest sample count is invalid.")

    root = manifest_path.parent.resolve()
    samples: list[VadGoldSample] = []
    seen_sample_ids: set[str] = set()
    for raw_sample in raw_samples:
        if not isinstance(raw_sample, dict):
            raise InvalidJobRequest("Each VAD gold sample must be an object.")
        sample_id = _safe_identifier("sample_id", raw_sample.get("sample_id"))
        if sample_id in seen_sample_ids:
            raise InvalidJobRequest("VAD gold sample IDs must be unique.")
        seen_sample_ids.add(sample_id)
        audio_value = raw_sample.get("audio")
        if not isinstance(audio_value, str) or not audio_value or len(audio_value) > 500:
            raise InvalidJobRequest("Each VAD gold sample needs a relative audio path.")
        relative_audio = Path(audio_value)
        if relative_audio.is_absolute():
            raise InvalidJobRequest("VAD gold audio paths must be relative.")
        audio_candidate = root / relative_audio
        audio_path = audio_candidate.resolve()
        if (
            not audio_path.is_relative_to(root)
            or audio_candidate.is_symlink()
            or not audio_path.is_file()
        ):
            raise InvalidJobRequest(
                "A VAD gold audio path escapes the manifest directory or is unavailable."
            )
        labels = _parse_labels(raw_sample.get("labels"))
        samples.append(
            VadGoldSample(
                sample_id=sample_id,
                audio_path=audio_path,
                labels=labels,
            )
        )

    return VadGoldManifest(
        schema_version=VAD_GOLD_MANIFEST_SCHEMA_VERSION,
        dataset_id=dataset_id,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        samples=tuple(samples),
    )


def evaluate_vad_ranges(
    *,
    labels: tuple[VadReferenceRange, ...],
    detected_regions: tuple[tuple[int, int], ...],
    sample_rate: int,
    total_frames: int,
) -> VadConfusionMetrics:
    true_positive = 0
    false_negative = 0
    false_positive = 0
    true_negative = 0
    for label in labels:
        start_frame = round(label.start_ms * sample_rate / 1000)
        end_frame = round(label.end_ms * sample_rate / 1000)
        if (
            start_frame < 0
            or end_frame <= start_frame
            or end_frame > total_frames
        ):
            raise VadEvaluationFailed(
                "A labeled VAD range falls outside the decoded sample."
            )
        predicted_frames = sum(
            max(0, min(end_frame, detected_end) - max(start_frame, detected_start))
            for detected_start, detected_end in detected_regions
        )
        reference_frames = end_frame - start_frame
        if label.reference_class is VadReferenceClass.SPEECH:
            true_positive += predicted_frames
            false_negative += reference_frames - predicted_frames
        else:
            false_positive += predicted_frames
            true_negative += reference_frames - predicted_frames
    return _build_metrics(
        sample_rate=sample_rate,
        true_positive=true_positive,
        false_negative=false_negative,
        false_positive=false_positive,
        true_negative=true_negative,
    )


def evaluate_acceptance(
    metrics: VadConfusionMetrics,
    *,
    policy: VadAcceptancePolicy | None,
) -> VadAcceptanceResult:
    if policy is None:
        return VadAcceptanceResult(
            policy=None,
            passed=None,
            issue_codes=("ACCEPTANCE_POLICY_NOT_SUPPLIED",),
            automatic_materialization_authorized=False,
        )
    issues: list[str] = []
    speech_ms = _frames_to_ms(metrics.speech_reference_frames, metrics.sample_rate)
    non_speech_ms = _frames_to_ms(
        metrics.non_speech_reference_frames, metrics.sample_rate
    )
    if speech_ms < policy.minimum_speech_reference_ms:
        issues.append("INSUFFICIENT_SPEECH_REFERENCE")
    if non_speech_ms < policy.minimum_non_speech_reference_ms:
        issues.append("INSUFFICIENT_NON_SPEECH_REFERENCE")
    if (
        metrics.speech_miss_rate is None
        or metrics.speech_miss_rate > policy.max_speech_miss_rate
    ):
        issues.append("SPEECH_MISS_RATE_EXCEEDED")
    if (
        metrics.false_speech_rate is None
        or metrics.false_speech_rate > policy.max_false_speech_rate
    ):
        issues.append("FALSE_SPEECH_RATE_EXCEEDED")
    return VadAcceptanceResult(
        policy=policy,
        passed=not issues,
        issue_codes=tuple(issues),
        automatic_materialization_authorized=False,
    )


def decode_audio_source(path: Path) -> DecodedAudio:
    source_sha256 = _sha256(path)
    probe = probe_audio_source(path)
    timeout_seconds = max(60.0, min(3600.0, probe.duration_seconds * 3))
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                str(VAD_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            ],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise VadEvaluationFailed(
            "FFmpeg is required to decode VAD gold samples."
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VadEvaluationFailed(
            "A VAD gold sample did not decode within the safe local boundary."
        ) from exc
    if completed.returncode != 0 or not completed.stdout or len(completed.stdout) % 2:
        raise VadEvaluationFailed("A VAD gold sample could not be decoded.")
    pcm = np.frombuffer(completed.stdout, dtype="<i2")
    if pcm.size == 0:
        raise VadEvaluationFailed("A VAD gold sample decoded to empty audio.")
    if _sha256(path) != source_sha256:
        raise VadEvaluationFailed("A VAD gold sample changed during evaluation.")
    return (
        (pcm.astype(np.float32) / 32768.0).copy(),
        source_sha256,
    )


def write_private_vad_report(path: Path, report: VadEvaluationReport) -> None:
    destination = path.resolve()
    if path.is_symlink() or (
        destination.exists() and not destination.is_file()
    ):
        raise InvalidJobRequest("The VAD report destination is invalid.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.is_symlink():
        raise InvalidJobRequest("The VAD report directory cannot be a symbolic link.")
    handle, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(report.to_dict(), output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except OSError as exc:
        raise VadEvaluationFailed("The private VAD report could not be stored.") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _parse_labels(raw_labels: Any) -> tuple[VadReferenceRange, ...]:
    if (
        not isinstance(raw_labels, list)
        or not raw_labels
        or len(raw_labels) > MAX_LABEL_COUNT_PER_SAMPLE
    ):
        raise InvalidJobRequest("Each VAD gold sample needs a bounded label list.")
    labels: list[VadReferenceRange] = []
    cursor = 0
    for raw_label in raw_labels:
        if not isinstance(raw_label, dict):
            raise InvalidJobRequest("Each VAD gold label must be an object.")
        start_ms = raw_label.get("start_ms")
        end_ms = raw_label.get("end_ms")
        if (
            not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or start_ms < cursor
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise InvalidJobRequest(
                "VAD gold labels must be ordered, non-overlapping, and non-empty."
            )
        try:
            reference_class = VadReferenceClass(raw_label.get("class"))
        except (TypeError, ValueError) as exc:
            raise InvalidJobRequest(
                "VAD gold labels accept only speech or non_speech."
            ) from exc
        labels.append(
            VadReferenceRange(
                start_ms=start_ms,
                end_ms=end_ms,
                reference_class=reference_class,
            )
        )
        cursor = end_ms
    return tuple(labels)


def _validate_frame_regions(
    regions: list[tuple[int, int]],
    *,
    total_frames: int,
) -> tuple[tuple[int, int], ...]:
    normalized: list[tuple[int, int]] = []
    cursor = 0
    for start_frame, end_frame in regions:
        if start_frame < cursor or end_frame <= start_frame or end_frame > total_frames:
            raise SpeechActivityDetectionFailed(
                "Detector speech regions must be ordered, non-overlapping, and bounded."
            )
        normalized.append((start_frame, end_frame))
        cursor = end_frame
    return tuple(normalized)


def _safe_identifier(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or value[0] not in _SAFE_ID_CHARACTERS
        or any(character not in _SAFE_ID_CHARACTERS for character in value)
    ):
        raise InvalidJobRequest(f"{name} must be a safe opaque identifier.")
    return value


def _build_metrics(
    *,
    sample_rate: int,
    true_positive: int,
    false_negative: int,
    false_positive: int,
    true_negative: int,
) -> VadConfusionMetrics:
    speech_reference = true_positive + false_negative
    non_speech_reference = false_positive + true_negative
    return VadConfusionMetrics(
        sample_rate=sample_rate,
        true_positive_frames=true_positive,
        false_negative_frames=false_negative,
        false_positive_frames=false_positive,
        true_negative_frames=true_negative,
        speech_reference_frames=speech_reference,
        non_speech_reference_frames=non_speech_reference,
        speech_recall=_ratio(true_positive, speech_reference),
        speech_miss_rate=_ratio(false_negative, speech_reference),
        non_speech_specificity=_ratio(true_negative, non_speech_reference),
        false_speech_rate=_ratio(false_positive, non_speech_reference),
    )


def _sum_metrics(metrics: tuple[VadConfusionMetrics, ...]) -> VadConfusionMetrics:
    return _build_metrics(
        sample_rate=VAD_SAMPLE_RATE,
        true_positive=sum(value.true_positive_frames for value in metrics),
        false_negative=sum(value.false_negative_frames for value in metrics),
        false_positive=sum(value.false_positive_frames for value in metrics),
        true_negative=sum(value.true_negative_frames for value in metrics),
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 8) if denominator else None


def _frames_to_ms(frames: int, sample_rate: int) -> int:
    return round(frames * 1000 / sample_rate)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise VadEvaluationFailed("A VAD gold sample changed during evaluation.") from exc
    return digest.hexdigest()
