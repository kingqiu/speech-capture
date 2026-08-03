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
from speech_capture_worker.artifact_generation import (
    ArtifactGenerator,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor, MlxQwenAsrEngine
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.corrections import CorrectionField
from speech_capture_worker.device_security import DeviceSecurityStore
from speech_capture_worker.diarization_execution import (
    DiarizationOutcome,
    PyannoteSpeakerDiarizationEngine,
    SpeakerDiarizationExecutor,
)
from speech_capture_worker.domain import (
    SUPPORTED_CONTENT_TYPES,
    JobCreateRequest,
    JobState,
    ModelProfile,
    UploadCreateRequest,
)
from speech_capture_worker.errors import InvalidJobRequest, UploadStorageError, WorkerCoreError
from speech_capture_worker.forced_alignment import (
    ForcedAlignmentExecutor,
    ForcedAlignmentOutcome,
    MlxQwenForcedAlignmentEngine,
)
from speech_capture_worker.gap_analysis import (
    DEFAULT_DEFINITE_SILENCE_PEAK,
    DEFAULT_MIN_DEFINITE_SILENCE_MS,
    DEFAULT_WINDOW_MS,
    DefiniteSilenceMaterializer,
    TranscriptGapAnalyzer,
)
from speech_capture_worker.gap_retranscription import (
    GapRetranscriptionExecutor,
    GapRetranscriptionOutcome,
)
from speech_capture_worker.gap_review import ReviewedGapMaterializer
from speech_capture_worker.gap_speech_activity import (
    GapSpeechActivityAnalyzer,
    PyannoteVoiceActivityDetector,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.model_activation import resolve_active_model_target
from speech_capture_worker.publication_domain import DEFAULT_PUBLICATION_LEASE_SECONDS
from speech_capture_worker.recording_context import (
    MAX_RECORDING_CONTEXT_CHARACTERS,
    RECORDING_CONTEXT_OPTION,
    normalize_recording_context,
    recording_context_sha256,
)
from speech_capture_worker.redaction import public_cli_error_payload
from speech_capture_worker.resources import (
    check_resource_preflight,
    estimate_job_disk_bytes,
)
from speech_capture_worker.scheduler import JobScheduler, SchedulerOutcome
from speech_capture_worker.structuring_execution import (
    SUMMARY_REVISION_STAGE,
    OllamaStructuringEngine,
    StructuringExecutor,
    StructuringOutcome,
)
from speech_capture_worker.transcript import (
    DiarizationStatus,
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
)
from speech_capture_worker.vault_publication import (
    DEFAULT_VAULT_OUTPUT_ROOT,
    VaultPublisher,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except WorkerCoreError as exc:
        _write_json(
            {"error": public_cli_error_payload(exc.code, exc.message)},
            stream=sys.stderr,
        )
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
    create.add_argument("--content-type", choices=sorted(SUPPORTED_CONTENT_TYPES))
    create.add_argument(
        "--recording-context-file",
        type=Path,
        help="Optional UTF-8 free-form context used only after raw ASR.",
    )

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
    create_from_upload.add_argument(
        "--content-type", choices=sorted(SUPPORTED_CONTENT_TYPES)
    )
    create_from_upload.add_argument(
        "--recording-context-file",
        type=Path,
        help="Optional UTF-8 free-form context used only after raw ASR.",
    )

    set_recording_context = subparsers.add_parser(
        "set-recording-context",
        help="Save, replace, or clear one job's optional post-ASR context.",
    )
    _add_data_dir(set_recording_context)
    set_recording_context.add_argument("job_id")
    set_recording_context.add_argument("--expected-revision", type=int, required=True)
    context_source = set_recording_context.add_mutually_exclusive_group(required=True)
    context_source.add_argument("--context-file", type=Path)
    context_source.add_argument("--clear", action="store_true")

    set_content_type = subparsers.add_parser(
        "set-content-type",
        help="Save or clear one job's user-selected content type.",
    )
    _add_data_dir(set_content_type)
    set_content_type.add_argument("job_id")
    set_content_type.add_argument("--expected-revision", type=int, required=True)
    content_type_source = set_content_type.add_mutually_exclusive_group(required=True)
    content_type_source.add_argument(
        "--content-type", choices=sorted(SUPPORTED_CONTENT_TYPES)
    )
    content_type_source.add_argument("--clear", action="store_true")

    add_correction = subparsers.add_parser(
        "add-correction",
        help="Append a correction to derived output without rewriting raw ASR.",
    )
    _add_data_dir(add_correction)
    add_correction.add_argument("job_id")
    add_correction.add_argument(
        "--field", choices=[field.value for field in CorrectionField], required=True
    )
    add_correction.add_argument("--target-id")
    add_correction.add_argument("--before")
    add_correction.add_argument("--after", required=True)
    add_correction.add_argument("--author", required=True)
    add_correction.add_argument("--idempotency-key", required=True)
    add_correction.add_argument("--expected-revision", type=int, required=True)

    list_corrections = subparsers.add_parser(
        "list-corrections",
        help="List a job's immutable correction history.",
    )
    _add_data_dir(list_corrections)
    list_corrections.add_argument("job_id")

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

    run_asr_all = subparsers.add_parser(
        "run-asr-all",
        help="Execute all remaining ASR chunks with safe retries and recovery.",
    )
    _add_data_dir(run_asr_all)
    run_asr_all.add_argument("job_id")
    run_asr_all.add_argument("--max-attempts", type=int, default=3)
    run_asr_all.add_argument("--max-chunks", type=int)

    finalize_alignment = subparsers.add_parser(
        "finalize-alignment",
        help="Verify whole-transcript alignment and timeline readiness.",
    )
    _add_data_dir(finalize_alignment)
    finalize_alignment.add_argument("job_id")

    force_align_next = subparsers.add_parser(
        "force-align-next",
        help="Align the next stable estimated transcript segment without rewriting text.",
    )
    _add_data_dir(force_align_next)
    force_align_next.add_argument("job_id")

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

    review_gap = subparsers.add_parser(
        "review-gap",
        help="Materialize one exact unresolved range from explicit human review.",
    )
    _add_data_dir(review_gap)
    review_gap.add_argument("job_id")
    review_gap.add_argument("--review-key", required=True)
    review_gap.add_argument("--start-ms", type=int, required=True)
    review_gap.add_argument("--end-ms", type=int, required=True)
    review_gap.add_argument(
        "--outcome",
        choices=[
            TranscriptOutcome.NON_SPEECH.value,
            TranscriptOutcome.INAUDIBLE.value,
        ],
        required=True,
    )

    analyze_speech_activity = subparsers.add_parser(
        "analyze-speech-activity",
        help="Record revision-pinned VAD observations without materializing gap outcomes.",
    )
    _add_data_dir(analyze_speech_activity)
    analyze_speech_activity.add_argument("job_id")
    analyze_speech_activity.add_argument(
        "--model-revision",
        required=True,
        help="Full lowercase commit SHA for pyannote/segmentation.",
    )

    run_diarization = subparsers.add_parser(
        "run-diarization",
        help="Run revision-pinned speaker diarization and anonymous attribution.",
    )
    _add_data_dir(run_diarization)
    run_diarization.add_argument("job_id")
    run_diarization.add_argument(
        "--model-revision",
        required=True,
        help="Full lowercase commit SHA for pyannote/speaker-diarization-3.1.",
    )

    run_structuring = subparsers.add_parser(
        "run-structuring",
        help="Classify content and extract evidence-linked findings through local Ollama.",
    )
    _add_data_dir(run_structuring)
    run_structuring.add_argument("job_id")
    run_structuring.add_argument(
        "--model",
        default=None,
        help="Ollama model name for classification and extraction.",
    )
    run_structuring.add_argument(
        "--editor-model",
        default=None,
        help="Ollama model name for faithful transcript punctuation and cleanup.",
    )
    run_structuring.add_argument(
        "--force",
        action="store_true",
        help="Recompute structuring evidence for a quality-check or processed job.",
    )
    run_structuring.add_argument(
        "--document-only",
        action="store_true",
        help="Re-synthesize only the global note from durable extracted findings.",
    )
    run_structuring.add_argument(
        "--transcript-edits-only",
        action="store_true",
        help="Retry only failed transcript-edit batches with smaller contexts.",
    )
    run_structuring.add_argument(
        "--context-corrections-only",
        action="store_true",
        help=(
            "Apply explicit user-confirmed term corrections to accepted derived text "
            "without rerunning ASR or the full note models."
        ),
    )

    generate_artifacts = subparsers.add_parser(
        "generate-artifacts",
        help="Generate the deterministic backend artifact package.",
    )
    _add_data_dir(generate_artifacts)
    generate_artifacts.add_argument("job_id")
    generate_artifacts.add_argument(
        "--force",
        action="store_true",
        help="Regenerate artifacts for an already processed job.",
    )

    summary_revisions = subparsers.add_parser(
        "summary-revisions",
        help="List private before/after comparisons from summary-only regeneration.",
    )
    _add_data_dir(summary_revisions)
    summary_revisions.add_argument("job_id")

    claim_publication = subparsers.add_parser(
        "claim-publication",
        help="Claim the only active lease for a processed artifact package.",
    )
    _add_data_dir(claim_publication)
    claim_publication.add_argument("job_id")
    claim_publication.add_argument("--publisher-id", required=True)
    claim_publication.add_argument("--target-relative-path", required=True)
    claim_publication.add_argument("--manifest-sha256", required=True)
    claim_publication.add_argument("--expected-revision", type=int, required=True)
    claim_publication.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_PUBLICATION_LEASE_SECONDS
    )

    renew_publication = subparsers.add_parser(
        "renew-publication",
        help="Renew an unexpired publication lease.",
    )
    _add_data_dir(renew_publication)
    renew_publication.add_argument("job_id")
    renew_publication.add_argument("--lease-id", required=True)
    renew_publication.add_argument("--publisher-id", required=True)
    renew_publication.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_PUBLICATION_LEASE_SECONDS
    )

    release_publication = subparsers.add_parser(
        "release-publication",
        help="Release a publication lease and return the job to processed.",
    )
    _add_data_dir(release_publication)
    release_publication.add_argument("job_id")
    release_publication.add_argument("--lease-id", required=True)
    release_publication.add_argument("--publisher-id", required=True)
    release_publication.add_argument(
        "--reason-code", default="publisher_unavailable"
    )

    acknowledge_publication = subparsers.add_parser(
        "acknowledge-publication",
        help="Acknowledge a fully written and verified Vault package.",
    )
    _add_data_dir(acknowledge_publication)
    acknowledge_publication.add_argument("job_id")
    acknowledge_publication.add_argument("--lease-id", required=True)
    acknowledge_publication.add_argument("--publisher-id", required=True)
    acknowledge_publication.add_argument("--manifest-sha256", required=True)

    publication_status = subparsers.add_parser(
        "publication-status",
        help="Read publication lease history and the final acknowledgement.",
    )
    _add_data_dir(publication_status)
    publication_status.add_argument("job_id")

    publish_to_vault = subparsers.add_parser(
        "publish-to-vault",
        help="Atomically publish a processed package into a local Vault root.",
    )
    _add_data_dir(publish_to_vault)
    publish_to_vault.add_argument("job_id")
    publish_to_vault.add_argument("--vault-root", type=Path, required=True)
    publish_to_vault.add_argument("--publisher-id", required=True)
    publish_to_vault.add_argument(
        "--output-root", default=DEFAULT_VAULT_OUTPUT_ROOT
    )
    publish_to_vault.add_argument("--expected-revision", type=int, required=True)
    publish_to_vault.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_PUBLICATION_LEASE_SECONDS
    )

    retranscribe_gaps = subparsers.add_parser(
        "retranscribe-gaps",
        help="Re-transcribe VAD-identified speech gaps with raw evidence.",
    )
    _add_data_dir(retranscribe_gaps)
    retranscribe_gaps.add_argument("job_id")
    retranscribe_gaps.add_argument("--max-attempts", type=int, default=3)

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

    create_pairing = subparsers.add_parser(
        "create-pairing-session",
        help="Create one short-lived local pairing code for a device.",
    )
    _add_data_dir(create_pairing)
    create_pairing.add_argument("--device-id", required=True)
    create_pairing.add_argument("--vault-id", action="append", required=True)
    create_pairing.add_argument("--ttl-seconds", type=int, default=300)

    list_devices = subparsers.add_parser(
        "list-paired-devices",
        help="List paired devices without credential hashes or tokens.",
    )
    _add_data_dir(list_devices)

    revoke_device = subparsers.add_parser(
        "revoke-device",
        help="Immediately revoke one device's active Worker credential.",
    )
    _add_data_dir(revoke_device)
    revoke_device.add_argument("device_id")

    serve = subparsers.add_parser(
        "serve",
        help="Run the authenticated Worker API with fail-closed network settings.",
    )
    _add_data_dir(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--ssl-certfile", type=Path)
    serve.add_argument("--ssl-keyfile", type=Path)
    return parser


def _add_data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Dedicated Worker application-data directory.",
    )


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "serve":
        from speech_capture_worker.server import ServerConfig, serve

        serve(
            ServerConfig(
                data_dir=args.data_dir,
                host=args.host,
                port=args.port,
                ssl_certfile=args.ssl_certfile,
                ssl_keyfile=args.ssl_keyfile,
            )
        )
        return 0

    if args.command == "preflight":
        estimated_bytes = _resolve_estimated_bytes(args)
        report = check_resource_preflight(
            args.storage_path,
            estimated_required_bytes=estimated_bytes,
            model_profile=ModelProfile(args.profile),
        )
        _write_json(report.to_dict())
        return 0 if report.can_start else 2

    if args.command in {
        "create-pairing-session",
        "list-paired-devices",
        "revoke-device",
    }:
        security_path = args.data_dir.resolve() / "security.sqlite3"
        with DeviceSecurityStore(security_path) as security:
            if args.command == "create-pairing-session":
                session = security.create_pairing_session(
                    device_id=args.device_id,
                    allowed_vault_ids=tuple(args.vault_id),
                    ttl_seconds=args.ttl_seconds,
                )
                _write_json({
                    "session_id": session.session_id,
                    "pairing_code": session.pairing_code,
                    "pairing_ticket": session.pairing_ticket,
                    "device_id": session.device_id,
                    "allowed_vault_ids": session.allowed_vault_ids,
                    "expires_at": session.expires_at,
                })
                return 0
            if args.command == "list-paired-devices":
                _write_json({
                    "devices": [
                        {
                            "credential_id": device.credential_id,
                            "device_id": device.device_id,
                            "allowed_vault_ids": device.allowed_vault_ids,
                            "generation": device.generation,
                            "created_at": device.created_at,
                            "last_used_at": device.last_used_at,
                            "revoked_at": device.revoked_at,
                        }
                        for device in security.list_devices()
                    ]
                })
                return 0
            changed = security.revoke_device(args.device_id)
            _write_json({"device_id": args.device_id, "revoked": changed})
            return 0

    database_path = args.data_dir.resolve() / "worker.sqlite3"
    with JobStore(database_path) as store:
        if args.command == "init":
            _write_json({"database_ready": store.quick_check(), "schema_ready": True})
            return 0
        if args.command == "create-job":
            context = _read_recording_context_file(args.recording_context_file)
            request = JobCreateRequest(
                vault_id=args.vault_id,
                source_display_name=args.source_name,
                source_sha256=args.source_sha256,
                source_size_bytes=args.source_size_bytes,
                model_profile=ModelProfile(args.profile),
                language_hint=args.language_hint,
                content_type_override=args.content_type,
                options=(
                    {RECORDING_CONTEXT_OPTION: context} if context is not None else {}
                ),
            )
            job, created = store.create_job(request, idempotency_key=args.idempotency_key)
            _write_json({"created": created, "job": job.to_dict()})
            return 0
        if args.command == "create-job-from-upload":
            context = _read_recording_context_file(args.recording_context_file)
            job, created = store.create_job_from_upload(
                args.upload_id,
                idempotency_key=args.idempotency_key,
                model_profile=ModelProfile(args.profile),
                language_hint=args.language_hint,
                content_type_override=args.content_type,
                options=(
                    {RECORDING_CONTEXT_OPTION: context} if context is not None else {}
                ),
            )
            _write_json({"created": created, "job": job.to_dict()})
            return 0
        if args.command == "set-recording-context":
            context = None if args.clear else _read_recording_context_file(args.context_file)
            job, changed = store.update_job_recording_context(
                args.job_id,
                context=context,
                expected_revision=args.expected_revision,
            )
            saved_context = job.options.get(RECORDING_CONTEXT_OPTION)
            _write_json(
                {
                    "changed": changed,
                    "job_id": job.job_id,
                    "job_revision": job.revision,
                    "context_supplied": isinstance(saved_context, str) and bool(saved_context),
                    "context_sha256": recording_context_sha256(
                        saved_context if isinstance(saved_context, str) else None
                    ),
                }
            )
            return 0
        if args.command == "set-content-type":
            content_type = None if args.clear else args.content_type
            job, changed = store.update_job_content_type_override(
                args.job_id,
                content_type=content_type,
                expected_revision=args.expected_revision,
            )
            _write_json(
                {
                    "changed": changed,
                    "job_id": job.job_id,
                    "job_revision": job.revision,
                    "content_type_override": job.content_type_override,
                }
            )
            return 0
        if args.command == "add-correction":
            correction, created = store.append_correction(
                args.job_id,
                field=CorrectionField(args.field),
                target_id=args.target_id,
                before=args.before,
                after=args.after,
                author=args.author,
                idempotency_key=args.idempotency_key,
                expected_revision=args.expected_revision,
            )
            _write_json(
                {
                    "created": created,
                    "correction": correction.to_dict(),
                    "job_revision": store.get_job(args.job_id).revision,
                }
            )
            return 0
        if args.command == "list-corrections":
            corrections = store.list_corrections(args.job_id)
            _write_json({"corrections": [item.to_dict() for item in corrections]})
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
                MlxQwenAsrEngine(
                    model_profile=job.model_profile,
                    model_target=_active_asr_target(args.data_dir, job.model_profile),
                ),
                max_attempts=args.max_attempts,
            ).run_next(args.job_id)
            _write_json(result.to_dict())
            return (
                2 if result.outcome.value in {"retryable_failure", "safe_paused", "partial"} else 0
            )
        if args.command == "run-asr-all":
            job = store.get_job(args.job_id)
            result = AsrChunkExecutor(
                store,
                MlxQwenAsrEngine(
                    model_profile=job.model_profile,
                    model_target=_active_asr_target(args.data_dir, job.model_profile),
                ),
                max_attempts=args.max_attempts,
            ).run_all(args.job_id, max_chunks=args.max_chunks)
            _write_json(result.to_dict())
            return (
                2
                if result.outcome.value
                in {"retryable_failure", "safe_paused", "partial"}
                else 0
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
        if args.command == "force-align-next":
            job = store.get_job(args.job_id)
            result = ForcedAlignmentExecutor(
                store,
                MlxQwenForcedAlignmentEngine(
                    model_target=resolve_active_model_target(
                        args.data_dir,
                        profile=job.model_profile.value,
                        key="aligner",
                        fallback="Qwen/Qwen3-ForcedAligner-0.6B",
                    )
                ),
            ).run_next(args.job_id)
            _write_json(result.to_dict())
            return 2 if result.outcome is ForcedAlignmentOutcome.SAFE_PAUSED else 0
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
        if args.command == "review-gap":
            result = ReviewedGapMaterializer(store).materialize(
                args.job_id,
                review_key=args.review_key,
                start_ms=args.start_ms,
                end_ms=args.end_ms,
                outcome=TranscriptOutcome(args.outcome),
            )
            _write_json(result.to_dict())
            return 0
        if args.command == "analyze-speech-activity":
            detector = PyannoteVoiceActivityDetector(
                model_revision=args.model_revision,
                cache_dir=args.data_dir.resolve() / "models" / "pyannote",
            )
            result = GapSpeechActivityAnalyzer(store, detector).analyze(args.job_id)
            _write_json(result.to_dict())
            return 2 if result.outcome.value == "safe_paused" else 0
        if args.command == "run-diarization":
            engine = PyannoteSpeakerDiarizationEngine(
                model_revision=args.model_revision,
                cache_dir=args.data_dir.resolve() / "models" / "pyannote",
            )
            result = SpeakerDiarizationExecutor(store, engine).run(args.job_id)
            _write_json(result.to_dict())
            return 2 if result.outcome is DiarizationOutcome.SAFE_PAUSED else 0
        if args.command == "run-structuring":
            job = store.get_job(args.job_id)
            profile = job.model_profile.value
            main_key = "ollama_accuracy" if profile == "accuracy" else "ollama_editor"
            executor = StructuringExecutor(
                store,
                OllamaStructuringEngine(
                    model=args.model
                    or resolve_active_model_target(
                        args.data_dir,
                        profile=profile,
                        key=main_key,
                        fallback="qwen3:14b" if profile == "accuracy" else "qwen3:8b",
                    ),
                    editor_model=args.editor_model
                    or resolve_active_model_target(
                        args.data_dir,
                        profile=profile,
                        key="ollama_editor",
                        fallback="qwen3:8b",
                    ),
                ),
            )
            selected_modes = sum(
                int(value)
                for value in (
                    args.document_only,
                    args.transcript_edits_only,
                    args.context_corrections_only,
                )
            )
            if selected_modes > 1:
                raise InvalidJobRequest(
                    "Choose only one incremental structuring mode."
                )
            if args.document_only:
                result = executor.resynthesize_document(args.job_id)
            elif args.transcript_edits_only:
                result = executor.repair_transcript_edits(args.job_id)
            elif args.context_corrections_only:
                result = executor.apply_recording_context_corrections(args.job_id)
            else:
                result = executor.run(args.job_id, force=args.force)
            _write_json(result.to_dict())
            return 2 if result.outcome is StructuringOutcome.SAFE_PAUSED else 0
        if args.command == "generate-artifacts":
            result = ArtifactGenerator(store).generate(args.job_id, force=args.force)
            _write_json(result.to_dict())
            return 0
        if args.command == "summary-revisions":
            revisions = store.list_checkpoints(
                args.job_id,
                stage=SUMMARY_REVISION_STAGE,
            )
            _write_json(
                {
                    "summary_revisions": [
                        {
                            "revision_key": item.checkpoint_key,
                            "created_at": item.created_at,
                            **item.payload,
                        }
                        for item in revisions
                    ]
                }
            )
            return 0
        if args.command == "claim-publication":
            lease, job, created = store.claim_publication(
                args.job_id,
                publisher_id=args.publisher_id,
                target_relative_path=args.target_relative_path,
                manifest_sha256=args.manifest_sha256,
                expected_revision=args.expected_revision,
                lease_seconds=args.lease_seconds,
            )
            _write_json(
                {"created": created, "lease": lease.to_dict(), "job": job.to_dict()}
            )
            return 0
        if args.command == "renew-publication":
            lease = store.renew_publication_lease(
                args.job_id,
                lease_id=args.lease_id,
                publisher_id=args.publisher_id,
                lease_seconds=args.lease_seconds,
            )
            _write_json({"lease": lease.to_dict()})
            return 0
        if args.command == "release-publication":
            job = store.release_publication_lease(
                args.job_id,
                lease_id=args.lease_id,
                publisher_id=args.publisher_id,
                reason_code=args.reason_code,
            )
            _write_json({"job": job.to_dict()})
            return 0
        if args.command == "acknowledge-publication":
            receipt, job, created = store.acknowledge_publication(
                args.job_id,
                lease_id=args.lease_id,
                publisher_id=args.publisher_id,
                manifest_sha256=args.manifest_sha256,
            )
            _write_json(
                {
                    "created": created,
                    "receipt": receipt.to_dict(),
                    "job": job.to_dict(),
                }
            )
            return 0
        if args.command == "publication-status":
            receipt = store.get_publication_receipt(args.job_id)
            _write_json(
                {
                    "leases": [
                        lease.to_dict()
                        for lease in store.list_publication_leases(args.job_id)
                    ],
                    "receipt": receipt.to_dict() if receipt is not None else None,
                }
            )
            return 0
        if args.command == "publish-to-vault":
            result = VaultPublisher(
                store,
                vault_root=args.vault_root,
                publisher_id=args.publisher_id,
                output_root=args.output_root,
                lease_seconds=args.lease_seconds,
            ).publish(args.job_id, expected_revision=args.expected_revision)
            _write_json(result.to_dict())
            return 0
        if args.command == "retranscribe-gaps":
            job = store.get_job(args.job_id)
            result = GapRetranscriptionExecutor(
                store,
                MlxQwenAsrEngine(
                    model_profile=job.model_profile,
                    model_target=_active_asr_target(args.data_dir, job.model_profile),
                ),
                max_attempts=args.max_attempts,
            ).run(args.job_id)
            _write_json(result.to_dict())
            return 2 if result.outcome is GapRetranscriptionOutcome.SAFE_PAUSED else 0
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


def _active_asr_target(data_dir: Path, profile: ModelProfile) -> str:
    accuracy = profile is ModelProfile.ACCURACY
    return resolve_active_model_target(
        data_dir,
        profile=profile.value,
        key="asr_accuracy" if accuracy else "asr_speed",
        fallback=(
            "Qwen/Qwen3-ASR-1.7B" if accuracy else "Qwen/Qwen3-ASR-0.6B"
        ),
    )


def _read_recording_context_file(path: Path | None) -> str | None:
    if path is None:
        return None
    if path.is_symlink() or not path.is_file():
        raise InvalidJobRequest("The recording-context file must be a regular file.")
    try:
        if path.stat().st_size > MAX_RECORDING_CONTEXT_CHARACTERS * 4:
            raise InvalidJobRequest("The recording-context file is too large.")
        value = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidJobRequest("The recording-context file must be UTF-8.") from exc
    except OSError as exc:
        raise InvalidJobRequest("The recording-context file could not be read.") from exc
    normalized = normalize_recording_context(value)
    if normalized is None:
        raise InvalidJobRequest("The recording-context file must not be empty.")
    return normalized


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
