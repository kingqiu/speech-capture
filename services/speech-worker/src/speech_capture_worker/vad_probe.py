"""Developer CLI for private, local-only VAD gold-set evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from speech_capture_worker.errors import InvalidJobRequest, WorkerCoreError
from speech_capture_worker.gap_speech_activity import PyannoteVoiceActivityDetector
from speech_capture_worker.vad_evaluation import (
    VadAcceptancePolicy,
    VadGoldEvaluator,
    load_vad_gold_manifest,
    write_private_vad_report,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _evaluate(args)
    except WorkerCoreError as exc:
        print(
            json.dumps({"error": exc.to_dict()}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speech-capture-vad-probe",
        description="Evaluate revision-pinned VAD locally against a private gold manifest.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-speech-miss-rate", type=float)
    parser.add_argument("--max-false-speech-rate", type=float)
    parser.add_argument("--minimum-speech-reference-ms", type=int)
    parser.add_argument("--minimum-non-speech-reference-ms", type=int)
    return parser


def _evaluate(args: argparse.Namespace) -> int:
    manifest = load_vad_gold_manifest(args.manifest)
    private_root = args.manifest.resolve().parent
    if "test-data-private" not in private_root.parts:
        raise InvalidJobRequest(
            "The VAD gold manifest must be stored under test-data-private."
        )
    output = args.output.resolve()
    cache_dir = args.cache_dir.resolve()
    if not output.is_relative_to(private_root) or not cache_dir.is_relative_to(
        private_root
    ):
        raise InvalidJobRequest(
            "The VAD report and model cache must stay inside the private manifest directory."
        )
    if output == args.manifest.resolve() or any(
        output == sample.audio_path for sample in manifest.samples
    ):
        raise InvalidJobRequest("The VAD report cannot replace its private inputs.")
    if args.cache_dir.is_symlink():
        raise InvalidJobRequest("The VAD model cache cannot be a symbolic link.")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InvalidJobRequest("The VAD model cache could not be prepared.") from exc

    policy = _policy_from_args(args)
    detector = PyannoteVoiceActivityDetector(
        model_revision=args.model_revision,
        cache_dir=cache_dir,
    )
    report = VadGoldEvaluator(detector).evaluate(
        manifest,
        storage_path=cache_dir,
        policy=policy,
    )
    write_private_vad_report(output, report)
    print(
        json.dumps(
            {
                "event": "vad_evaluation_completed",
                "dataset_id": report.dataset_id,
                "sample_count": report.sample_count,
                "metrics": report.metrics.to_dict(),
                "acceptance": report.acceptance.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if report.acceptance.passed is False else 0


def _policy_from_args(args: argparse.Namespace) -> VadAcceptancePolicy | None:
    values = (
        args.max_speech_miss_rate,
        args.max_false_speech_rate,
        args.minimum_speech_reference_ms,
        args.minimum_non_speech_reference_ms,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise InvalidJobRequest(
            "All four VAD acceptance-policy options must be supplied together."
        )
    return VadAcceptancePolicy(
        max_speech_miss_rate=args.max_speech_miss_rate,
        max_false_speech_rate=args.max_false_speech_rate,
        minimum_speech_reference_ms=args.minimum_speech_reference_ms,
        minimum_non_speech_reference_ms=args.minimum_non_speech_reference_ms,
    )
