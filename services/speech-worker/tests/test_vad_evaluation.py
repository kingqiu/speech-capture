import hashlib
import io
import json
import os
import shutil
import wave

import numpy as np
import pytest

import speech_capture_worker.vad_probe as vad_probe
from speech_capture_worker.domain import ResourceStatus
from speech_capture_worker.errors import (
    InvalidJobRequest,
    ResourceBlocked,
    VadEvaluationFailed,
)
from speech_capture_worker.gap_speech_activity import (
    DetectedSpeechRegion,
    SpeechActivityDetectorIdentity,
)
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceReport,
)
from speech_capture_worker.vad_evaluation import (
    VAD_SAMPLE_RATE,
    VadAcceptancePolicy,
    VadGoldEvaluator,
    VadReferenceClass,
    VadReferenceRange,
    decode_audio_source,
    evaluate_acceptance,
    evaluate_vad_ranges,
    load_vad_gold_manifest,
    write_private_vad_report,
)
from speech_capture_worker.vad_probe import main


def wav_bytes(duration_ms: int = 1000) -> bytes:
    samples = np.zeros(round(VAD_SAMPLE_RATE * duration_ms / 1000), dtype="<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(VAD_SAMPLE_RATE)
        audio.writeframes(samples.tobytes())
    return output.getvalue()


def ready_preflight(*_, **__) -> ResourceReport:
    return ResourceReport(
        status=ResourceStatus.READY,
        estimated_required_bytes=2 * GIB,
        disk_reserve_bytes=20 * GIB,
        disk_free_after_bytes=40 * GIB,
        disk=DiskSnapshot(total_bytes=256 * GIB, free_bytes=80 * GIB),
        memory=MemorySnapshot(
            total_bytes=32 * GIB,
            available_bytes=20 * GIB,
            used_percent=40,
            swap_used_bytes=0,
        ),
        issues=(),
    )


def write_manifest(root, *, audio="../escape.wav", labels=None):
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "dataset_id": "private-vad-v1",
                "samples": [
                    {
                        "sample_id": "sample-001",
                        "audio": audio,
                        "labels": labels
                        or [
                            {"start_ms": 0, "end_ms": 500, "class": "speech"},
                            {"start_ms": 500, "end_ms": 1000, "class": "non_speech"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


class FakeDetector:
    identity = SpeechActivityDetectorIdentity(
        detector_id="fixture_vad",
        detector_version="1.0.0",
        model_id="fixture/model",
        model_revision="fixture-revision",
        configuration_id="fixture-config",
    )

    def __init__(self):
        self.calls = 0

    def detect(self, audio, *, sample_rate):
        self.calls += 1
        assert audio.dtype == np.float32
        assert sample_rate == VAD_SAMPLE_RATE
        return (
            DetectedSpeechRegion(start_seconds=0.1, end_seconds=0.4),
            DetectedSpeechRegion(start_seconds=0.6, end_seconds=0.7),
        )


def test_manifest_resolves_private_relative_audio_and_hashes_exact_manifest(tmp_path) -> None:
    private = tmp_path / "test-data-private"
    audio = private / "audio.wav"
    private.mkdir()
    audio.write_bytes(wav_bytes())
    manifest_path = write_manifest(private, audio="audio.wav")

    manifest = load_vad_gold_manifest(manifest_path)

    assert manifest.dataset_id == "private-vad-v1"
    assert manifest.samples[0].sample_id == "sample-001"
    assert manifest.samples[0].audio_path == audio.resolve()
    assert manifest.manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def test_manifest_rejects_path_escape_and_overlapping_labels(tmp_path) -> None:
    outside = tmp_path / "escape.wav"
    outside.write_bytes(wav_bytes())
    escaped = write_manifest(tmp_path / "test-data-private")

    with pytest.raises(InvalidJobRequest):
        load_vad_gold_manifest(escaped)

    private = tmp_path / "other" / "test-data-private"
    private.mkdir(parents=True)
    (private / "audio.wav").write_bytes(wav_bytes())
    overlapping = write_manifest(
        private,
        audio="audio.wav",
        labels=[
            {"start_ms": 0, "end_ms": 600, "class": "speech"},
            {"start_ms": 500, "end_ms": 1000, "class": "non_speech"},
        ],
    )
    with pytest.raises(InvalidJobRequest):
        load_vad_gold_manifest(overlapping)


def test_vad_metrics_measure_missed_speech_and_false_speech_by_duration() -> None:
    metrics = evaluate_vad_ranges(
        labels=(
            VadReferenceRange(0, 500, VadReferenceClass.SPEECH),
            VadReferenceRange(500, 1000, VadReferenceClass.NON_SPEECH),
        ),
        detected_regions=((100, 400), (600, 700)),
        sample_rate=1000,
        total_frames=1000,
    )

    assert metrics.true_positive_frames == 300
    assert metrics.false_negative_frames == 200
    assert metrics.false_positive_frames == 100
    assert metrics.true_negative_frames == 400
    assert metrics.speech_recall == 0.6
    assert metrics.speech_miss_rate == 0.4
    assert metrics.non_speech_specificity == 0.8
    assert metrics.false_speech_rate == 0.2


def test_acceptance_requires_explicit_policy_and_never_authorizes_materialization() -> None:
    metrics = evaluate_vad_ranges(
        labels=(
            VadReferenceRange(0, 500, VadReferenceClass.SPEECH),
            VadReferenceRange(500, 1000, VadReferenceClass.NON_SPEECH),
        ),
        detected_regions=((0, 500),),
        sample_rate=1000,
        total_frames=1000,
    )

    unapproved = evaluate_acceptance(metrics, policy=None)
    passed = evaluate_acceptance(
        metrics,
        policy=VadAcceptancePolicy(
            max_speech_miss_rate=0,
            max_false_speech_rate=0,
            minimum_speech_reference_ms=500,
            minimum_non_speech_reference_ms=500,
        ),
    )

    assert unapproved.passed is None
    assert unapproved.issue_codes == ("ACCEPTANCE_POLICY_NOT_SUPPLIED",)
    assert unapproved.automatic_materialization_authorized is False
    assert passed.passed is True
    assert passed.issue_codes == ()
    assert passed.automatic_materialization_authorized is False


def test_acceptance_reports_insufficient_coverage_and_rate_failures() -> None:
    metrics = evaluate_vad_ranges(
        labels=(
            VadReferenceRange(0, 500, VadReferenceClass.SPEECH),
            VadReferenceRange(500, 1000, VadReferenceClass.NON_SPEECH),
        ),
        detected_regions=((250, 750),),
        sample_rate=1000,
        total_frames=1000,
    )
    result = evaluate_acceptance(
        metrics,
        policy=VadAcceptancePolicy(
            max_speech_miss_rate=0.1,
            max_false_speech_rate=0.1,
            minimum_speech_reference_ms=1000,
            minimum_non_speech_reference_ms=1000,
        ),
    )

    assert result.passed is False
    assert result.issue_codes == (
        "INSUFFICIENT_SPEECH_REFERENCE",
        "INSUFFICIENT_NON_SPEECH_REFERENCE",
        "SPEECH_MISS_RATE_EXCEEDED",
        "FALSE_SPEECH_RATE_EXCEEDED",
    )


def test_gold_evaluator_produces_path_free_report_and_private_atomic_output(tmp_path) -> None:
    private = tmp_path / "test-data-private"
    private.mkdir()
    secret_audio = private / "private-secret-name.wav"
    secret_audio.write_bytes(wav_bytes())
    manifest = load_vad_gold_manifest(
        write_manifest(private, audio=secret_audio.name)
    )
    detector = FakeDetector()
    report = VadGoldEvaluator(
        detector,
        audio_decoder=lambda _: (
            np.zeros(VAD_SAMPLE_RATE, dtype=np.float32),
            "a" * 64,
        ),
        boundary_preflight=ready_preflight,
    ).evaluate(
        manifest,
        storage_path=private,
    )
    output = private / "report.json"
    write_private_vad_report(output, report)
    serialized = output.read_text(encoding="utf-8")

    assert detector.calls == 1
    assert report.metrics.speech_miss_rate == 0.4
    assert report.metrics.false_speech_rate == 0.2
    assert report.acceptance.passed is None
    assert report.acceptance.automatic_materialization_authorized is False
    assert "private-secret-name" not in serialized
    assert str(private) not in serialized
    assert oct(output.stat().st_mode & 0o777) == "0o600"


def test_gold_evaluator_rejects_labels_beyond_decoded_audio(tmp_path) -> None:
    class EmptyDetector(FakeDetector):
        def detect(self, audio, *, sample_rate):
            self.calls += 1
            return ()

    private = tmp_path / "test-data-private"
    private.mkdir()
    audio = private / "audio.wav"
    audio.write_bytes(wav_bytes())
    manifest = load_vad_gold_manifest(write_manifest(private, audio=audio.name))

    with pytest.raises(VadEvaluationFailed):
        VadGoldEvaluator(
            EmptyDetector(),
            audio_decoder=lambda _: (
                np.zeros(VAD_SAMPLE_RATE // 2, dtype=np.float32),
                "a" * 64,
            ),
            boundary_preflight=ready_preflight,
        ).evaluate(manifest, storage_path=private)


def test_gold_evaluator_weights_aggregate_metrics_by_labeled_duration(tmp_path) -> None:
    class SequencedDetector(FakeDetector):
        def detect(self, audio, *, sample_rate):
            self.calls += 1
            if self.calls == 1:
                return (DetectedSpeechRegion(start_seconds=0, end_seconds=0.1),)
            return ()

    private = tmp_path / "test-data-private"
    private.mkdir()
    (private / "short.wav").write_bytes(wav_bytes(100))
    (private / "long.wav").write_bytes(wav_bytes(900))
    manifest_path = private / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "dataset_id": "weighted-vad-v1",
                "samples": [
                    {
                        "sample_id": "short",
                        "audio": "short.wav",
                        "labels": [{"start_ms": 0, "end_ms": 100, "class": "speech"}],
                    },
                    {
                        "sample_id": "long",
                        "audio": "long.wav",
                        "labels": [{"start_ms": 0, "end_ms": 900, "class": "speech"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = load_vad_gold_manifest(manifest_path)
    audio_by_name = {
        "short.wav": np.zeros(VAD_SAMPLE_RATE // 10, dtype=np.float32),
        "long.wav": np.zeros(round(VAD_SAMPLE_RATE * 0.9), dtype=np.float32),
    }
    report = VadGoldEvaluator(
        SequencedDetector(),
        audio_decoder=lambda path: (audio_by_name[path.name], "a" * 64),
        boundary_preflight=ready_preflight,
    ).evaluate(manifest, storage_path=private)

    assert report.samples[0].metrics.speech_recall == 1
    assert report.samples[1].metrics.speech_recall == 0
    assert report.metrics.speech_recall == 0.1
    assert report.metrics.speech_miss_rate == 0.9


def test_gold_evaluator_blocks_before_decode_or_detector_when_resources_fail(
    tmp_path,
) -> None:
    private = tmp_path / "test-data-private"
    private.mkdir()
    audio = private / "audio.wav"
    audio.write_bytes(wav_bytes())
    manifest = load_vad_gold_manifest(write_manifest(private, audio=audio.name))
    detector = FakeDetector()
    blocked = ResourceReport(
        status=ResourceStatus.BLOCKED,
        estimated_required_bytes=2 * GIB,
        disk_reserve_bytes=20 * GIB,
        disk_free_after_bytes=10 * GIB,
        disk=DiskSnapshot(total_bytes=256 * GIB, free_bytes=12 * GIB),
        memory=MemorySnapshot(
            total_bytes=32 * GIB,
            available_bytes=1 * GIB,
            used_percent=97,
            swap_used_bytes=5 * GIB,
        ),
        issues=(),
    )

    with pytest.raises(ResourceBlocked):
        VadGoldEvaluator(
            detector,
            audio_decoder=lambda _: pytest.fail("decoder should not run"),
            boundary_preflight=lambda *_, **__: blocked,
        ).evaluate(manifest, storage_path=private)

    assert detector.calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_audio_decoder_normalizes_wav_without_exposing_source_path(tmp_path) -> None:
    source = tmp_path / "private-source.wav"
    content = wav_bytes(250)
    source.write_bytes(content)

    audio, source_sha256 = decode_audio_source(source)

    assert audio.dtype == np.float32
    assert audio.shape == (4000,)
    assert source_sha256 == hashlib.sha256(content).hexdigest()


def test_cli_requires_complete_user_supplied_acceptance_policy(tmp_path, capsys) -> None:
    private = tmp_path / "test-data-private"
    private.mkdir()
    audio = private / "audio.wav"
    audio.write_bytes(wav_bytes())
    manifest = write_manifest(private, audio=audio.name)
    output = private / "report.json"

    exit_code = main(
        [
            "--manifest",
            str(manifest),
            "--model-revision",
            "a" * 40,
            "--cache-dir",
            str(private / "model-cache"),
            "--output",
            str(output),
            "--max-speech-miss-rate",
            "0.01",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["error"]["code"] == "INVALID_JOB_REQUEST"
    assert "all four" in payload["error"]["message"].lower()
    assert not output.exists()
    assert os.path.basename(str(private)) not in captured.err


def test_cli_keeps_manifest_cache_and_report_in_private_workspace(
    tmp_path,
    capsys,
) -> None:
    unprotected = tmp_path / "ordinary-directory"
    unprotected.mkdir()
    audio = unprotected / "audio.wav"
    audio.write_bytes(wav_bytes())
    manifest = write_manifest(unprotected, audio=audio.name)

    exit_code = main(
        [
            "--manifest",
            str(manifest),
            "--model-revision",
            "a" * 40,
            "--cache-dir",
            str(unprotected / "model-cache"),
            "--output",
            str(unprotected / "report.json"),
        ]
    )
    payload = json.loads(capsys.readouterr().err)

    assert exit_code == 2
    assert payload["error"]["code"] == "INVALID_JOB_REQUEST"
    assert "test-data-private" in payload["error"]["message"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg unavailable")
def test_cli_writes_safe_metrics_for_explicit_policy(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    private = tmp_path / "test-data-private"
    private.mkdir()
    audio = private / "private-source-name.wav"
    audio.write_bytes(wav_bytes())
    manifest = write_manifest(private, audio=audio.name)
    output = private / "report.json"
    monkeypatch.setattr(
        vad_probe,
        "PyannoteVoiceActivityDetector",
        lambda **_: FakeDetector(),
    )
    monkeypatch.setattr(
        vad_probe,
        "VadGoldEvaluator",
        lambda detector: VadGoldEvaluator(
            detector,
            boundary_preflight=ready_preflight,
        ),
    )

    exit_code = main(
        [
            "--manifest",
            str(manifest),
            "--model-revision",
            "a" * 40,
            "--cache-dir",
            str(private / "model-cache"),
            "--output",
            str(output),
            "--max-speech-miss-rate",
            "0.5",
            "--max-false-speech-rate",
            "0.5",
            "--minimum-speech-reference-ms",
            "500",
            "--minimum-non-speech-reference-ms",
            "500",
        ]
    )
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert summary["acceptance"]["passed"] is True
    assert summary["acceptance"]["automatic_materialization_authorized"] is False
    assert report["metrics"]["speech_miss_rate"] == 0.4
    assert report["metrics"]["false_speech_rate"] == 0.2
    assert "private-source-name" not in captured.out
    assert "private-source-name" not in json.dumps(report)
