"""Local-only Qwen3-ASR integration probe for Speech Capture."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from speech_capture_worker.completeness import (
    CoverageIssue,
    evaluate_chunk_coverage,
    validate_timestamp_segments,
)
from speech_capture_worker.quality import measure_character_error_rate

ACCURACY_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
SPEED_MODEL_ID = "Qwen/Qwen3-ASR-0.6B"
ALIGNER_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B"
MODEL_DOWNLOAD_BYTES = {
    ACCURACY_MODEL_ID: 4_703_114_308,
    SPEED_MODEL_ID: 1_880_619_678,
    ALIGNER_MODEL_ID: 1_840_072_459,
}
GIB = 1024**3
MINIMUM_RESERVE_BYTES = 20 * GIB
RESERVE_FRACTION = 0.10
DOWNLOAD_HEADROOM = 1.15
REPORT_VERSION = "1.0.0"
SAFE_PROGRESS_KEYS = frozenset(
    {
        "event",
        "total_chunks",
        "audio_duration_sec",
        "progress",
        "chunk_index",
        "chunk_offset_sec",
        "chunk_duration_sec",
        "max_new_tokens",
        "processed_audio_sec",
        "language",
        "segment_count",
        "finish_reason",
        "truncated",
        "generated_tokens",
        "speaker_segment_count",
        "word_segment_count",
        "file_index",
        "file_total",
    }
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = build_doctor_report(Path(args.storage_path))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 2
    if args.command == "download":
        return _download(args)
    if args.command == "run":
        return _run(args)
    parser.error("A command is required.")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speech-capture-asr-probe",
        description="Measure local Qwen3-ASR behavior before Worker integration.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local ASR prerequisites.")
    doctor.add_argument(
        "--storage-path",
        default=".",
        help="Filesystem used for model and temporary-space preflight.",
    )

    download = subparsers.add_parser("download", help="Download a model into the HF cache.")
    download.add_argument(
        "--model",
        choices=(ACCURACY_MODEL_ID, SPEED_MODEL_ID),
        default=ACCURACY_MODEL_ID,
    )
    download.add_argument(
        "--with-aligner",
        action="store_true",
        help=f"Also download {ALIGNER_MODEL_ID}.",
    )
    download.add_argument(
        "--storage-path",
        default=".",
        help="Filesystem used for free-space preflight.",
    )

    run = subparsers.add_parser("run", help="Transcribe one local audio file.")
    run.add_argument("audio", type=Path)
    run.add_argument(
        "--model",
        choices=(ACCURACY_MODEL_ID, SPEED_MODEL_ID),
        default=ACCURACY_MODEL_ID,
    )
    run.add_argument("--language", help="Optional forced language such as Chinese or English.")
    run.add_argument("--context", default="", help="Confirmed vocabulary context.")
    run.add_argument("--timestamps", action="store_true", help="Run the forced aligner.")
    run.add_argument("--diarize", action="store_true", help="Run optional pyannote diarization.")
    run.add_argument("--num-speakers", type=int)
    run.add_argument("--min-speakers", type=int, default=1)
    run.add_argument("--max-speakers", type=int, default=8)
    run.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Local JSON report path. Reports can contain private transcript text.",
    )
    run.add_argument(
        "--reference-file",
        type=Path,
        help="Optional reviewed UTF-8 transcript used for character error rate.",
    )
    run.add_argument(
        "--max-cer",
        type=float,
        default=0.15,
        help="Maximum accepted normalized character error rate when a reference is supplied.",
    )
    return parser


def build_doctor_report(storage_path: Path) -> dict[str, Any]:
    storage_path = storage_path.resolve()
    disk = _disk_preflight(storage_path, required_bytes=0)
    checks = {
        "apple_silicon": platform.system() == "Darwin" and platform.machine() == "arm64",
        "python_3_11_or_newer": sys.version_info >= (3, 11),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "ffprobe_available": shutil.which("ffprobe") is not None,
        "disk_reserve_safe": disk["safe"],
        "mlx_qwen3_asr_installed": _package_version("mlx-qwen3-asr") is not None,
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "optional_features": {
            "diarization": {
                "dependencies_installed": _package_version("pyannote-audio") is not None,
                "pyannote_audio": _package_version("pyannote-audio"),
                "auth_token_configured": _diarization_token_configured(),
            }
        },
        "environment": _safe_environment(),
        "disk": disk,
    }


def _download(args: argparse.Namespace) -> int:
    model_ids = [args.model]
    if args.with_aligner:
        model_ids.append(ALIGNER_MODEL_ID)
    required_bytes = sum(MODEL_DOWNLOAD_BYTES[model_id] for model_id in model_ids)
    disk = _disk_preflight(Path(args.storage_path).resolve(), required_bytes=required_bytes)
    if not disk["safe"]:
        print(json.dumps({"event": "download_blocked", "disk": disk}, indent=2), file=sys.stderr)
        return 2

    from huggingface_hub import snapshot_download

    for model_id in model_ids:
        print(json.dumps({"event": "download_started", "model": model_id}), flush=True)
        snapshot_download(
            repo_id=model_id,
            allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
        )
        print(json.dumps({"event": "download_completed", "model": model_id}), flush=True)
    return 0


def _run(args: argparse.Namespace) -> int:
    source = args.audio.resolve()
    if not source.is_file():
        raise SystemExit(f"Audio file does not exist: {source}")
    if args.output.resolve() == source:
        raise SystemExit("Output report must not replace the source audio.")
    if not 0.0 <= args.max_cer <= 1.0:
        raise SystemExit("--max-cer must be between 0 and 1.")

    reference_text: str | None = None
    reference_sha256: str | None = None
    if args.reference_file is not None:
        reference_path = args.reference_file.resolve()
        if not reference_path.is_file():
            raise SystemExit(f"Reference file does not exist: {reference_path}")
        reference_text = reference_path.read_text(encoding="utf-8")
        reference_sha256 = hashlib.sha256(reference_text.encode("utf-8")).hexdigest()

    disk = _disk_preflight(args.output.resolve().parent, required_bytes=0)
    if not disk["safe"]:
        print(json.dumps({"event": "run_blocked", "disk": disk}, indent=2), file=sys.stderr)
        return 2

    source_info = _probe_source(source)
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    progress_events: list[dict[str, Any]] = []

    def on_progress(event: dict[str, Any]) -> None:
        safe_event = sanitize_progress_event(event)
        safe_event["elapsed_sec"] = round(time.monotonic() - started_monotonic, 6)
        progress_events.append(safe_event)
        print(json.dumps(safe_event, ensure_ascii=False), file=sys.stderr, flush=True)

    from mlx_qwen3_asr import Session

    session = Session(model=args.model)
    model_loaded_sec = time.monotonic() - started_monotonic
    result = session.transcribe(
        str(source),
        context=args.context,
        language=args.language,
        return_timestamps=args.timestamps,
        diarize=args.diarize,
        diarization_num_speakers=args.num_speakers,
        diarization_min_speakers=args.min_speakers,
        diarization_max_speakers=args.max_speakers,
        return_chunks=True,
        on_progress=on_progress,
    )
    completed_monotonic = time.monotonic()

    result_dict = asdict(result)
    coverage = evaluate_chunk_coverage(
        result_dict.get("chunks"),
        source_duration_sec=source_info["duration_sec"],
    )
    timestamp_issues = validate_timestamp_segments(
        result_dict.get("segments"),
        source_duration_sec=source_info["duration_sec"],
    )
    all_issues = list(coverage.issues) + list(timestamp_issues)
    if result_dict.get("truncated") and not any(
        issue.code == "TRUNCATED_CHUNK" for issue in all_issues
    ):
        all_issues.append(
            CoverageIssue(
                code="TRUNCATED_RESULT",
                message="The aggregate ASR result reports generation truncation.",
            )
        )
    character_error = None
    if reference_text is not None:
        character_error = measure_character_error_rate(reference_text, result_dict["text"])
        if character_error.character_error_rate > args.max_cer:
            all_issues.append(
                CoverageIssue(
                    code="REFERENCE_ERROR_RATE_EXCEEDED",
                    message=(
                        "Normalized character error rate "
                        f"{character_error.character_error_rate:.4f} exceeds "
                        f"the configured maximum {args.max_cer:.4f}."
                    ),
                )
            )

    elapsed_sec = completed_monotonic - started_monotonic
    report = {
        "report_version": REPORT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": _safe_environment(),
        "source": source_info,
        "configuration": {
            "model": args.model,
            "language": args.language,
            "context_supplied": bool(args.context.strip()),
            "timestamps": args.timestamps,
            "diarize": args.diarize,
            "num_speakers": args.num_speakers,
            "min_speakers": args.min_speakers,
            "max_speakers": args.max_speakers,
            "reference_supplied": reference_text is not None,
            "reference_sha256": reference_sha256,
            "max_character_error_rate": args.max_cer if reference_text is not None else None,
        },
        "performance": {
            "started_at": started_at.isoformat(),
            "model_loaded_sec": round(model_loaded_sec, 6),
            "elapsed_sec": round(elapsed_sec, 6),
            "real_time_factor": round(elapsed_sec / source_info["duration_sec"], 6),
            "max_resident_set_bytes": _max_resident_set_bytes(),
            "time_to_first_completed_chunk_sec": _first_completed_chunk_time(progress_events),
        },
        "progress_events": progress_events,
        "result": result_dict,
        "quality": {
            "complete": not all_issues,
            "coverage": coverage.to_dict(),
            "timestamp_issues": [asdict(issue) for issue in timestamp_issues],
            "character_error": (character_error.to_dict() if character_error is not None else None),
            "issues": [asdict(issue) for issue in all_issues],
        },
        "disk_at_start": disk,
    }
    _write_json_atomic(args.output, report)
    summary = {
        "event": "probe_completed",
        "complete": report["quality"]["complete"],
        "output": args.output.name,
        "elapsed_sec": report["performance"]["elapsed_sec"],
        "real_time_factor": report["performance"]["real_time_factor"],
        "issues": [issue["code"] for issue in report["quality"]["issues"]],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["quality"]["complete"] else 2


def _probe_source(source: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=index,codec_type,codec_name,sample_rate,channels",
        "-of",
        "json",
        str(source),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"ffprobe could not decode source metadata: {exc}") from exc

    payload = json.loads(completed.stdout)
    try:
        duration_sec = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit("ffprobe did not return a valid source duration.") from exc
    audio_streams = [
        stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise SystemExit("The selected source does not contain an audio stream.")

    return {
        "display_name": source.name,
        "sha256": _sha256(source),
        "size_bytes": source.stat().st_size,
        "duration_sec": duration_sec,
        "format_name": payload["format"].get("format_name"),
        "audio_streams": audio_streams,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _disk_preflight(path: Path, *, required_bytes: int) -> dict[str, Any]:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    usage = shutil.disk_usage(existing)
    reserve_bytes = max(MINIMUM_RESERVE_BYTES, int(usage.total * RESERVE_FRACTION))
    estimated_bytes = int(required_bytes * DOWNLOAD_HEADROOM)
    free_after_bytes = usage.free - estimated_bytes
    return {
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "required_bytes_with_headroom": estimated_bytes,
        "reserve_bytes": reserve_bytes,
        "free_after_bytes": free_after_bytes,
        "safe": free_after_bytes >= reserve_bytes,
    }


def _safe_environment() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "mlx_qwen3_asr": _package_version("mlx-qwen3-asr"),
        "speech_capture_worker": _package_version("speech-capture-worker"),
        "ffmpeg": _command_version("ffmpeg"),
    }


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _diarization_token_configured() -> bool:
    return any(
        bool(os.environ.get(name))
        for name in ("PYANNOTE_AUTH_TOKEN", "HF_TOKEN", "HUGGINGFACE_TOKEN")
    )


def _command_version(name: str) -> str | None:
    executable = shutil.which(name)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    first_line = completed.stdout.splitlines()[:1]
    return first_line[0] if first_line else None


def _max_resident_set_bytes() -> int:
    raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return raw
    return raw * 1024


def _first_completed_chunk_time(events: list[dict[str, Any]]) -> float | None:
    for event in events:
        if event.get("event") == "chunk_completed":
            return float(event["elapsed_sec"])
    return None


def sanitize_progress_event(event: dict[str, Any]) -> dict[str, Any]:
    """Keep routine progress logs free of transcript text and unknown payloads."""

    return {key: value for key, value in event.items() if key in SAFE_PROGRESS_KEYS}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
