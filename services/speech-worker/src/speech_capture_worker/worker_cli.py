"""Developer CLI for exercising the durable Worker core."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from speech_capture_worker.domain import JobCreateRequest, JobState, ModelProfile
from speech_capture_worker.errors import WorkerCoreError
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import (
    check_resource_preflight,
    estimate_job_disk_bytes,
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

    recover = subparsers.add_parser(
        "recover",
        help="Move interrupted active jobs to their safe restart boundary.",
    )
    _add_data_dir(recover)

    integrity = subparsers.add_parser("integrity", help="Run SQLite quick integrity check.")
    _add_data_dir(integrity)

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
        if args.command == "recover":
            recovered = store.recover_interrupted_jobs()
            _write_json({"recovered": [job.to_dict() for job in recovered]})
            return 0
        if args.command == "integrity":
            healthy = store.quick_check()
            _write_json({"database_healthy": healthy})
            return 0 if healthy else 2
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
