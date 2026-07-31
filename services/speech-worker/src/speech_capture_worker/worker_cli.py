"""Developer CLI for exercising the durable Worker core."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from speech_capture_worker.alignment import (
    AlignmentFinalizationOutcome,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor, MlxQwenAsrEngine
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.domain import (
    JobCreateRequest,
    JobState,
    ModelProfile,
    UploadCreateRequest,
)
from speech_capture_worker.errors import UploadStorageError, WorkerCoreError
from speech_capture_worker.gap_analysis import (
    DEFAULT_DEFINITE_SILENCE_PEAK,
    DEFAULT_MIN_DEFINITE_SILENCE_MS,
    DEFAULT_WINDOW_MS,
    DefiniteSilenceMaterializer,
    TranscriptGapAnalyzer,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import (
    check_resource_preflight,
    estimate_job_disk_bytes,
)
from speech_capture_worker.scheduler import JobScheduler, SchedulerOutcome
from speech_capture_worker.transcript import (
    DiarizationStatus,
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except WorkerCoreError as exc:
        _write_json({"error": exc.to_dict()}, stream=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speech-capture-worker",
        description="Exercise Speech Capture's persistent Worker core.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Create or migrate the Worker database.")
    _add_data_dir(initialize)

    create = subparsers.add_parser("create-job", help="Create an idempotent job record.")
    _add_data_dir(create)
    create.add_argument("--vault-id", required=True)
    create.add_argument("--source-name", required=True)
    create.add_argument("--source-sha256", required=True)
    create.add_argument("--source-size-bytes", required=True, type=int)
    create.add_argument("--idempotency-key", required=True)
    create.add_argument(
        "--profile",
        choices=[profile.value for profile in ModelProfile],
        default=ModelProfile.ACCURACY.value,
    )
    create.add_argument("--language-hint")
    create.add_argument("--content-type")

    create_from_upload = subparsers.add_parser(
        "create-job-from-upload",
        help="Create a queued job from a complete verified upload.",
    )
    _add_data_dir(create_from_upload)
    create_from_upload.add_argument("upload_id")
    create_from_upload.add_argument("--idempotency-key", required=True)
    create_from_upload.add_argument(
        "--profile",
        choices=[profile.value for profile in ModelProfile],
        default=ModelProfile.ACCURACY.value,
    )
    create_from_upload.add_argument("--language-hint")
    create_from_upload.add_argument("--content-type")

    create_upload = subparsers.add_parser(
        "create-upload",
        help="Create an idempotent resumable-upload manifest.",
    )
    _add_data_dir(create_upload)
    create_upload.add_argument("--vault-id", required=True)
    create_upload.add_argument("--source-name", required=True)
    create_upload.add_argument("--source-sha256", required=True)
    create_upload.add_argument("--source-size-bytes", required=True, type=int)
    create_upload.add_argument("--media-type", required=True)
    create_upload.add_argument("--idempotency-key", required=True)

    get_upload = subparsers.add_parser(
        "get-upload",
        help="Read upload progress and missing part numbers.",
    )
    _add_data_dir(get_upload)
    get_upload.add_argument("upload_id")

    put_upload_part = subparsers.add_parser(
        "put-upload-part",
        help="Store one checksum-bound upload part.",
    )
    _add_data_dir(put_upload_part)
    put_upload_part.add_argument("upload_id")
    put_upload_part.add_argument("part_number", type=int)
    put_upload_part.add_argument("--part-file", type=Path, required=True)
    put_upload_part.add_argument("--part-sha256", required=True)

    complete_upload = subparsers.add_parser(
        "complete-upload",
        help="Assemble, checksum, and media-verify a complete upload.",
    )
    _add_data_dir(complete_upload)
    complete_upload.add_argument("upload_id")

    recover_uploads = subparsers.add_parser(
        "recover-uploads",
        help="Return interrupted upload verification to its resumable boundary.",
    )
    _add_data_dir(recover_uploads)

    list_jobs = subparsers.add_parser("list-jobs", help="List persisted jobs.")
    _add_data_dir(list_jobs)
    list_jobs.add_argument(
        "--state",
        action="append",
        choices=[state.value for state in JobState],
    )
    list_jobs.add_argument("--limit", type=int, default=100)

    transition = subparsers.add_parser("transition", help="Apply one guarded state transition.")
    _add_data_dir(transition)
    transition.add_argument("job_id")
    transition.add_argument("target_state", choices=[state.value for state in JobState])
    transition.add_argument("--expected-revision", type=int, required=True)
    transition.add_argument("--reason-code")
    transition.add_argument("--error-code")
    transition.add_argument("--error-message")

    events = subparsers.add_parser("events", help="Read the durable event history.")
    _add_data_dir(events)
    events.add_argument("job_id")
    events.add_argument("--after-sequence", type=int, default=0)

    updates = subparsers.add_parser(
        "updates",
        help="Read the bounded reconnect event feed.",
    )
    _add_data_dir(updates)
    updates.add_argument("job_id")
    updates.add_argument("--after-sequence", type=int, default=0)
    updates.add_argument("--limit", type=int, default=200)

    snapshot = subparsers.add_parser(
        "snapshot",
        help="Read a bounded progressive transcript snapshot.",
    )
    _add_data_dir(snapshot)
    snapshot.add_argument("job_id")
    snapshot.add_argument("--after-segment-sequence", type=int, default=0)
    snapshot.add_argument("--segment-limit", type=int, default=100)

    progress = subparsers.add_parser(
        "record-progress",
        help="Persist monotonic active-job progress.",
    )
    _add_data_dir(progress)
    progress.add_argument("job_id")
    progress.add_argument("--processed-ms", type=int, required=True)
    progress.add_argument("--stage-progress", type=float, required=True)
    progress.add_argument("--elapsed-seconds", type=float, required=True)
    progress.add_argument("--estimated-remaining-seconds", type=float)
    progress.add_argument(
        "--diarization-status",
        choices=[status.value for status in DiarizationStatus],
        default=DiarizationStatus.NOT_STARTED.value,
    )

    provisional = subparsers.add_parser(
        "put-provisional",
        help="Create or revise the unstable transcript tail.",
    )
    _add_data_dir(provisional)
    provisional.add_argument("job_id")
    provisional.add_argument("--expected-generation", type=int, required=True)
    provisional.add_argument("--start-ms", type=int, required=True)
    provisional.add_argument("--end-ms", type=int, required=True)
    provisional.add_argument("--text", required=True)
    provisional.add_argument("--language")

    clear_provisional = subparsers.add_parser(
        "clear-provisional",
        help="Clear the unstable transcript tail.",
    )
    _add_data_dir(clear_provisional)
    clear_provisional.add_argument("job_id")
    clear_provisional.add_argument("--expected-generation", type=int, required=True)

    commit_segment = subparsers.add_parser(
        "commit-segment",
        help="Commit one stable, idempotent transcript timeline outcome.",
    )
    _add_data_dir(commit_segment)
    commit_segment.add_argument("job_id")
    commit_segment.add_argument("--commit-key", required=True)
    commit_segment.add_argument("--start-ms", type=int, required=True)
    commit_segment.add_argument("--end-ms", type=int, required=True)
    commit_segment.add_argument(
        "--outcome",
        choices=[outcome.value for outcome in TranscriptOutcome],
        required=True,
    )
    commit_segment.add_argument("--text")
    commit_segment.add_argument("--language")
    commit_segment.add_argument("--confidence", type=float)
    commit_segment.add_argument(
        "--timing-status",
        choices=[status.value for status in TranscriptTimingStatus],
        default=TranscriptTimingStatus.ESTIMATED.value,
    )
    commit_segment.add_argument("--speaker-id")
    commit_segment.add_argument(
        "--speaker-label-status",
        choices=[status.value for status in SpeakerLabelStatus],
    )
    commit_segment.add_argument("--error-code")

    update_segment = subparsers.add_parser(
        "update-segment-metadata",
        help="Revise alignment or speaker attribution without rewriting text.",
    )
    _add_data_dir(update_segment)
    update_segment.add_argument("job_id")
    update_segment.add_argument("segment_id")
    update_segment.add_argument("--expected-revision", type=int, required=True)
    update_segment.add_argument("--start-ms", type=int)
    update_segment.add_argument("--end-ms", type=int)
    update_segment.add_argument(
        "--timing-status",
        choices=[status.value for status in TranscriptTimingStatus],
    )
    speaker = update_segment.add_mutually_exclusive_group()
    speaker.add_argument("--speaker-id")
    speaker.add_argument("--clear-speaker", action="store_true")
    update_segment.add_argument(
        "--speaker-label-status",
        choices=[status.value for status in SpeakerLabelStatus],
    )

    recover = subparsers.add_parser(
        "recover",
        help="Move interrupted active jobs to their safe restart boundary.",
    )
    _add_data_dir(recover)

    integrity = subparsers.add_parser("integrity", help="Run SQLite quick integrity check.")
    _add_data_dir(integrity)

    schedule_once = subparsers.add_parser(
        "schedule-once",
        help="Preflight and claim at most one verified queued job.",
    )
    _add_data_dir(schedule_once)

    prepare_audio = subparsers.add_parser(
        "prepare-audio",
        help="Normalize verified audio and persist its deterministic chunk plan.",
    )
    _add_data_dir(prepare_audio)
    prepare_audio.add_argument("job_id")

    run_asr_next = subparsers.add_parser(
        "run-asr-next",
        help="Execute or replay the next durable local ASR chunk.",
    )
    _add_data_dir(run_asr_next)
    run_asr_next.add_argument("job_id")
    run_asr_next.add_argument("--max-attempts", type=int, default=3)

    finalize_alignment = subparsers.add_parser(
        "finalize-alignment",
        help="Verify whole-transcript alignment and timeline readiness.",
    )
    _add_data_dir(finalize_alignment)
    finalize_alignment.add_argument("job_id")

    analyze_gaps = subparsers.add_parser(
        "analyze-gaps",
        help="Measure conservative PCM evidence for uncovered timeline ranges.",
    )
    _add_data_dir(analyze_gaps)
    analyze_gaps.add_argument("job_id")
    analyze_gaps.add_argument("--window-ms", type=int, default=DEFAULT_WINDOW_MS)
    analyze_gaps.add_argument(
        "--minimum-definite-silence-ms",
        type=int,
        default=DEFAULT_MIN_DEFINITE_SILENCE_MS,
    )
    analyze_gaps.add_argument(
        "--definite-silence-peak-threshold",
        type=int,
        default=DEFAULT_DEFINITE_SILENCE_PEAK,
    )

    materialize_silence = subparsers.add_parser(
        "materialize-silence",
        help="Commit default-policy definite silence and refresh alignment.",
    )
    _add_data_dir(materialize_silence)
    materialize_silence.add_argument("job_id")

    list_asr_attempts = subparsers.add_parser(
        "list-asr-attempts",
        help="List safe raw-attempt metadata without transcript content.",
    )
    _add_data_dir(list_asr_attempts)
    list_asr_attempts.add_argument("job_id")
    list_asr_attempts.add_argument("--chunk-index", type=int)

    preflight = subparsers.add_parser(
        "preflight",
        help="Evaluate current disk and memory before starting model work.",
    )
    preflight.add_argument("--storage-path", type=Path, default=Path("."))
    preflight.add_argument(
        "--profile",
        choices=[profile.value for profile in ModelProfile],
        default=ModelProfile.ACCURACY.value,
    )
    estimate = preflight.add_mutually_exclusive_group(required=True)
    estimate.add_argument("--estimated-bytes", type=int)
    estimate.add_argument("--source-size-bytes", type=int)
    preflight.add_argument("--duration-sec", type=float)
    return parser


def _add_data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Dedicated Worker application-data directory.",
    )


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "preflight":
        estimated_bytes = _resolve_estimated_bytes(args)
        report = check_resource_preflight(
            args.storage_path,
            estimated_required_bytes=estimated_bytes,
            model_profile=ModelProfile(args.profile),
        )
        _write_json(report.to_dict())
        return 0 if report.can_start else 2

    database_path = args.data_dir.resolve() / "worker.sqlite3"
    with JobStore(database_path) as store:
        if args.command == "init":
            _write_json({"database_ready": store.quick_check(), "schema_ready": True})
            return 0
        if args.command == "create-job":
            request = JobCreateRequest(
                vault_id=args.vault_id,
                source_display_name=args.source_name,
                source_sha256=args.source_sha256,
                source_size_bytes=args.source_size_bytes,
                model_profile=ModelProfile(args.profile),
                language_hint=args.language_hint,
                content_type_override=args.content_type,
            )
            job, created = store.create_job(request, idempotency_key=args.idempotency_key)
            _write_json({"created": created, "job": job.to_dict()})
            return 0
        if args.command == "create-job-from-upload":
            job, created = store.create_job_from_upload(
                args.upload_id,
                idempotency_key=args.idempotency_key,
                model_profile=ModelProfile(args.profile),
                language_hint=args.language_hint,
                content_type_override=args.content_type,
            )
            _write_json({"created": created, "job": job.to_dict()})
            return 0
        if args.command == "create-upload":
            request = UploadCreateRequest(
                vault_id=args.vault_id,
                source_display_name=args.source_name,
                source_sha256=args.source_sha256,
                source_size_bytes=args.source_size_bytes,
                media_type=args.media_type,
            )
            upload, created = store.create_upload(
                request,
                idempotency_key=args.idempotency_key,
            )
            _write_json(
                {
                    "created": created,
                    "upload": upload.to_dict(),
                    "missing_part_numbers": store.list_missing_upload_parts(upload.upload_id),
                }
            )
            return 0
        if args.command == "get-upload":
            upload = store.get_upload(args.upload_id)
            _write_json(
                {
                    "upload": upload.to_dict(),
                    "missing_part_numbers": store.list_missing_upload_parts(upload.upload_id),
                }
            )
            return 0
        if args.command == "put-upload-part":
            try:
                content = args.part_file.read_bytes()
            except OSError as exc:
                raise UploadStorageError("The local upload-part file could not be read.") from exc
            part, created = store.put_upload_part(
                args.upload_id,
                part_number=args.part_number,
                content=content,
                part_sha256=args.part_sha256,
            )
            upload = store.get_upload(args.upload_id)
            _write_json(
                {
                    "created": created,
                    "part": part.to_dict(),
                    "upload": upload.to_dict(),
                }
            )
            return 0
        if args.command == "complete-upload":
            upload, completed = store.complete_upload(args.upload_id)
            _write_json({"completed": completed, "upload": upload.to_dict()})
            return 0
        if args.command == "recover-uploads":
            recovered = store.recover_interrupted_uploads()
            _write_json({"recovered_uploads": [upload.to_dict() for upload in recovered]})
            return 0
        if args.command == "list-jobs":
            states = [JobState(value) for value in args.state] if args.state else None
            jobs = store.list_jobs(states=states, limit=args.limit)
            _write_json({"jobs": [job.to_dict() for job in jobs]})
            return 0
        if args.command == "transition":
            job = store.transition_job(
                args.job_id,
                JobState(args.target_state),
                expected_revision=args.expected_revision,
                reason_code=args.reason_code,
                error_code=args.error_code,
                error_message=args.error_message,
            )
            _write_json({"job": job.to_dict()})
            return 0
        if args.command == "events":
            events = store.list_events(args.job_id, after_sequence=args.after_sequence)
            _write_json({"events": [event.to_dict() for event in events]})
            return 0
        if args.command == "updates":
            updates, has_more = store.list_job_updates(
                args.job_id,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
            _write_json(
                {
                    "updates": [update.to_dict() for update in updates],
                    "has_more": has_more,
                    "next_after_sequence": (
                        updates[-1].sequence if updates else args.after_sequence
                    ),
                }
            )
            return 0
        if args.command == "snapshot":
            snapshot = store.get_job_snapshot(
                args.job_id,
                after_segment_sequence=args.after_segment_sequence,
                segment_limit=args.segment_limit,
            )
            _write_json({"snapshot": snapshot.to_dict()})
            return 0
        if args.command == "record-progress":
            progress, changed = store.put_job_progress(
                args.job_id,
                processed_ms=args.processed_ms,
                stage_progress=args.stage_progress,
                elapsed_seconds=args.elapsed_seconds,
                estimated_remaining_seconds=args.estimated_remaining_seconds,
                diarization_status=DiarizationStatus(args.diarization_status),
            )
            _write_json({"changed": changed, "progress": progress.to_dict()})
            return 0
        if args.command == "put-provisional":
            provisional, changed = store.put_provisional_transcript(
                args.job_id,
                expected_generation=args.expected_generation,
                start_ms=args.start_ms,
                end_ms=args.end_ms,
                text=args.text,
                language=args.language,
            )
            _write_json({"changed": changed, "provisional": provisional.to_dict()})
            return 0
        if args.command == "clear-provisional":
            cleared = store.clear_provisional_transcript(
                args.job_id,
                expected_generation=args.expected_generation,
            )
            _write_json({"cleared": cleared})
            return 0
        if args.command == "commit-segment":
            outcome = TranscriptOutcome(args.outcome)
            speaker_status = (
                SpeakerLabelStatus(args.speaker_label_status)
                if args.speaker_label_status is not None
                else (
                    SpeakerLabelStatus.PENDING
                    if outcome is TranscriptOutcome.TRANSCRIBED
                    else SpeakerLabelStatus.UNAVAILABLE
                )
            )
            segment, created = store.commit_transcript_segment(
                args.job_id,
                commit_key=args.commit_key,
                start_ms=args.start_ms,
                end_ms=args.end_ms,
                outcome=outcome,
                text=args.text,
                language=args.language,
                confidence=args.confidence,
                timing_status=TranscriptTimingStatus(args.timing_status),
                speaker_id=args.speaker_id,
                speaker_label_status=speaker_status,
                error_code=args.error_code,
            )
            _write_json({"created": created, "segment": segment.to_dict()})
            return 0
        if args.command == "update-segment-metadata":
            metadata: dict[str, Any] = {
                "expected_revision": args.expected_revision,
                "start_ms": args.start_ms,
                "end_ms": args.end_ms,
                "timing_status": (
                    TranscriptTimingStatus(args.timing_status)
                    if args.timing_status is not None
                    else None
                ),
                "speaker_label_status": (
                    SpeakerLabelStatus(args.speaker_label_status)
                    if args.speaker_label_status is not None
                    else None
                ),
            }
            if args.speaker_id is not None:
                metadata["speaker_id"] = args.speaker_id
            elif args.clear_speaker:
                metadata["speaker_id"] = None
            segment = store.update_transcript_segment_metadata(
                args.job_id,
                args.segment_id,
                **metadata,
            )
            _write_json({"segment": segment.to_dict()})
            return 0
        if args.command == "recover":
            recovered = store.recover_interrupted_jobs()
            _write_json({"recovered": [job.to_dict() for job in recovered]})
            return 0
        if args.command == "integrity":
            healthy = store.quick_check()
            _write_json({"database_healthy": healthy})
            return 0 if healthy else 2
        if args.command == "schedule-once":
            result = JobScheduler(store).run_once()
            _write_json(result.to_dict())
            return 2 if result.outcome is SchedulerOutcome.BLOCKED else 0
        if args.command == "prepare-audio":
            plan, changed = AudioPreprocessor(store).prepare(args.job_id)
            _write_json({"changed": changed, "plan": plan.to_dict()})
            return 0
        if args.command == "run-asr-next":
            job = store.get_job(args.job_id)
            result = AsrChunkExecutor(
                store,
                MlxQwenAsrEngine(model_profile=job.model_profile),
                max_attempts=args.max_attempts,
            ).run_next(args.job_id)
            _write_json(result.to_dict())
            return (
                2 if result.outcome.value in {"retryable_failure", "safe_paused", "partial"} else 0
            )
        if args.command == "finalize-alignment":
            result = TranscriptAlignmentFinalizer(store).finalize(args.job_id)
            _write_json(result.to_dict())
            return (
                0
                if result.outcome
                in {
                    AlignmentFinalizationOutcome.READY_FOR_DIARIZATION,
                    AlignmentFinalizationOutcome.ALREADY_FINALIZED,
                }
                else 2
            )
        if args.command == "analyze-gaps":
            result = TranscriptGapAnalyzer(
                store,
                window_ms=args.window_ms,
                minimum_definite_silence_ms=args.minimum_definite_silence_ms,
                definite_silence_peak_threshold=(args.definite_silence_peak_threshold),
            ).analyze(args.job_id)
            _write_json(result.to_dict())
            return 0
        if args.command == "materialize-silence":
            result = DefiniteSilenceMaterializer(store).materialize(args.job_id)
            _write_json(result.to_dict())
            return 0
        if args.command == "list-asr-attempts":
            attempts = store.list_asr_attempts(
                args.job_id,
                chunk_index=args.chunk_index,
            )
            _write_json({"attempts": [attempt.to_dict() for attempt in attempts]})
            return 0
    parser_error = {"error": {"code": "UNKNOWN_COMMAND", "message": args.command}}
    _write_json(parser_error, stream=sys.stderr)
    return 2


def _resolve_estimated_bytes(args: argparse.Namespace) -> int:
    if args.estimated_bytes is not None:
        if args.duration_sec is not None:
            raise WorkerCoreError("--duration-sec cannot be used with --estimated-bytes.")
        return args.estimated_bytes
    if args.duration_sec is None:
        raise WorkerCoreError("--duration-sec is required with --source-size-bytes.")
    return estimate_job_disk_bytes(
        source_size_bytes=args.source_size_bytes,
        duration_sec=args.duration_sec,
    )


def _write_json(payload: dict[str, Any], *, stream: Any | None = None) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, indent=2),
        file=stream if stream is not None else sys.stdout,
    )
