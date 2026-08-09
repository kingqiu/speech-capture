"""Durable SQLite job, event, and checkpoint storage."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from speech_capture_worker.asr_domain import AsrAttemptRecord, AsrAttemptState
from speech_capture_worker.corrections import (
    CorrectionField,
    CorrectionRecord,
    decode_segment_review,
    encode_segment_review,
    validate_correction,
)
from speech_capture_worker.domain import (
    ACTIVE_PROCESSING_STATES,
    RECOVERY_TARGETS,
    SAFE_IDENTIFIER_PATTERN,
    SHA256_PATTERN,
    CheckpointRecord,
    JobCreateRequest,
    JobEvent,
    JobRecord,
    JobState,
    ModelProfile,
    UploadCreateRequest,
    UploadPartRecord,
    UploadRecord,
    UploadState,
    ensure_transition_allowed,
    validate_content_type_override,
    validate_idempotency_key,
    validate_reason_code,
    validate_safe_message,
)
from speech_capture_worker.errors import (
    AsrAttemptConflict,
    IdempotencyConflict,
    InvalidJobRequest,
    JobNotFound,
    PublicationLeaseConflict,
    RevisionConflict,
    SchedulerBusy,
    TranscriptConflict,
    TranscriptRevisionConflict,
    UploadChecksumMismatch,
    UploadIncomplete,
    UploadNotFound,
    UploadPartChecksumMismatch,
    UploadPartConflict,
    UploadStateConflict,
    UploadStorageError,
    VerifiedUploadRequired,
    WorkerCoreError,
)
from speech_capture_worker.media_probe import MediaProbeResult, probe_audio_source
from speech_capture_worker.publication_domain import (
    DEFAULT_PUBLICATION_LEASE_SECONDS,
    PublicationLeaseRecord,
    PublicationLeaseState,
    PublicationReceiptRecord,
    validate_lease_seconds,
    validate_publication_lease_request,
    validate_publisher_id,
)
from speech_capture_worker.recording_context import (
    RECORDING_CONTEXT_OPTION,
    normalize_recording_context,
    recording_context_from_options,
    recording_context_sha256,
)
from speech_capture_worker.transcript import (
    DiarizationStatus,
    JobProgress,
    JobSnapshot,
    JobUpdate,
    ProvisionalTranscript,
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptSegment,
    TranscriptTimingStatus,
    validate_commit_key,
    validate_confidence,
    validate_language,
    validate_progress_number,
    validate_speaker_id,
    validate_time_range,
    validate_transcript_text,
)

SCHEMA_VERSION = 8
DEFAULT_UPLOAD_CHUNK_SIZE_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_CHUNK_SIZE_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_PARTS = 10_000
COPY_BUFFER_BYTES = 1024 * 1024
_UNSET = object()


class JobStore:
    """Single-process facade over a durable SQLite Worker database."""

    def __init__(
        self,
        database_path: Path,
        *,
        upload_chunk_size_bytes: int = DEFAULT_UPLOAD_CHUNK_SIZE_BYTES,
        source_probe: Callable[[Path], MediaProbeResult] = probe_audio_source,
    ) -> None:
        if (
            not isinstance(upload_chunk_size_bytes, int)
            or isinstance(upload_chunk_size_bytes, bool)
            or upload_chunk_size_bytes < 1
            or upload_chunk_size_bytes > MAX_UPLOAD_CHUNK_SIZE_BYTES
        ):
            raise InvalidJobRequest("upload_chunk_size_bytes must be between 1 byte and 64 MiB.")
        self.database_path = database_path.resolve()
        self.data_directory = self.database_path.parent
        self.uploads_directory = self.data_directory / "uploads"
        self.sources_directory = self.data_directory / "sources"
        self.jobs_directory = self.data_directory / "jobs"
        self.upload_chunk_size_bytes = upload_chunk_size_bytes
        self._source_probe = source_probe
        parent_existed = self.database_path.parent.exists()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            try:
                self.database_path.parent.chmod(0o700)
            except OSError:
                pass
        _ensure_private_directory(self.uploads_directory, root=self.data_directory)
        _ensure_private_directory(self.sources_directory, root=self.data_directory)
        _ensure_private_directory(self.jobs_directory, root=self.data_directory)

        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._migrate()
        except BaseException:
            self._connection.close()
            raise
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def __enter__(self) -> JobStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_job(
        self,
        request: JobCreateRequest,
        *,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        """Create one job or return the prior identical idempotent result."""

        request.validate()
        validate_idempotency_key(idempotency_key)
        request_payload = {
            "vault_id": request.vault_id,
            "source_display_name": request.source_display_name,
            "source_sha256": request.source_sha256,
            "source_size_bytes": request.source_size_bytes,
            "model_profile": request.model_profile.value,
            "language_hint": request.language_hint,
            "content_type_override": request.content_type_override,
            "options": request.options,
        }
        if request.source_upload_id is not None:
            request_payload["source_upload_id"] = request.source_upload_id
        canonical_request = _canonical_json(request_payload)
        request_fingerprint = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        options_json = _canonical_json(request.options)

        with self._transaction():
            prior = self._connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE vault_id = ? AND idempotency_key = ?
                """,
                (request.vault_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict(
                        "The idempotency key is already bound to a different job request.",
                        details={"vault_id": request.vault_id},
                    )
                return self._row_to_job(prior), False

            if request.source_upload_id is not None:
                self._require_complete_upload_for_request(request)

            job_id = f"job_{uuid4().hex}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    vault_id,
                    source_upload_id,
                    source_display_name,
                    source_sha256,
                    source_size_bytes,
                    state,
                    model_profile,
                    language_hint,
                    content_type_override,
                    options_json,
                    idempotency_key,
                    request_fingerprint,
                    revision,
                    last_error_code,
                    last_error_message,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)
                """,
                (
                    job_id,
                    request.vault_id,
                    request.source_upload_id,
                    request.source_display_name,
                    request.source_sha256,
                    request.source_size_bytes,
                    JobState.CREATED.value,
                    request.model_profile.value,
                    request.language_hint,
                    request.content_type_override,
                    options_json,
                    idempotency_key,
                    request_fingerprint,
                    now,
                    now,
                ),
            )
            self._insert_event(
                job_id=job_id,
                revision=0,
                event_type="job.created",
                from_state=None,
                to_state=JobState.CREATED,
                reason_code=None,
                payload={
                    key: value
                    for key, value in {
                        "model_profile": request.model_profile.value,
                        "source_upload_id": request.source_upload_id,
                    }.items()
                    if value is not None
                },
                created_at=now,
            )
            job = self._row_to_job(self._fetch_job_row(job_id))
            if request.source_upload_id is not None:
                job = self._transition_in_transaction(
                    current=job,
                    target_state=JobState.UPLOADING,
                    reason_code="verified_upload_attached",
                    error_code=None,
                    error_message=None,
                    event_type="job.source_attached",
                )
                job = self._transition_in_transaction(
                    current=job,
                    target_state=JobState.VERIFYING,
                    reason_code="source_verification_reused",
                    error_code=None,
                    error_message=None,
                    event_type="job.source_verified",
                )
                job = self._transition_in_transaction(
                    current=job,
                    target_state=JobState.QUEUED,
                    reason_code="verified_source_ready",
                    error_code=None,
                    error_message=None,
                    event_type="job.queued",
                )
            return job, True

    def create_job_from_upload(
        self,
        upload_id: str,
        *,
        idempotency_key: str,
        model_profile: ModelProfile = ModelProfile.ACCURACY,
        language_hint: str | None = None,
        content_type_override: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[JobRecord, bool]:
        """Create a queued job whose immutable source is one complete upload."""

        upload = self.get_upload(upload_id)
        request = JobCreateRequest(
            vault_id=upload.vault_id,
            source_upload_id=upload.upload_id,
            source_display_name=upload.source_display_name,
            source_sha256=upload.source_sha256,
            source_size_bytes=upload.source_size_bytes,
            model_profile=model_profile,
            language_hint=language_hint,
            content_type_override=content_type_override,
            options=options if options is not None else {},
        )
        return self.create_job(request, idempotency_key=idempotency_key)

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._row_to_job(self._fetch_job_row(job_id))

    def update_job_recording_context(
        self,
        job_id: str,
        *,
        context: str | None,
        expected_revision: int,
    ) -> tuple[JobRecord, bool]:
        """Revision-guard one job's optional post-ASR processing context."""

        normalized = normalize_recording_context(context)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise InvalidJobRequest("expected_revision must be zero or greater.")
        with self._transaction():
            current = self._row_to_job(self._fetch_job_row(job_id))
            if current.revision != expected_revision:
                raise RevisionConflict(
                    "The job changed after the recording-context snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            if current.state in {JobState.PUBLISHING, JobState.PUBLISHED}:
                raise InvalidJobRequest(
                    "Recording context cannot change during or after publication."
                )
            existing = recording_context_from_options(current.options)
            if existing == normalized:
                return current, False
            options = dict(current.options)
            if normalized is None:
                options.pop(RECORDING_CONTEXT_OPTION, None)
            else:
                options[RECORDING_CONTEXT_OPTION] = normalized
            revision = current.revision + 1
            now = _utc_now()
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET options_json = ?, revision = ?, updated_at = ?
                WHERE job_id = ? AND revision = ?
                """,
                (
                    _canonical_json(options),
                    revision,
                    now,
                    job_id,
                    current.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    "The job changed while recording context was being saved.",
                    details={"job_id": job_id},
                )
            self._insert_event(
                job_id=job_id,
                revision=revision,
                event_type="job.recording_context_updated",
                from_state=current.state,
                to_state=current.state,
                reason_code=(
                    "recording_context_cleared"
                    if normalized is None
                    else "recording_context_saved"
                ),
                payload={
                    "context_supplied": normalized is not None,
                    "context_sha256": recording_context_sha256(normalized),
                },
                created_at=now,
            )
            return self._row_to_job(self._fetch_job_row(job_id)), True

    def update_job_content_type_override(
        self,
        job_id: str,
        *,
        content_type: str | None,
        expected_revision: int,
    ) -> tuple[JobRecord, bool]:
        """Revision-guard one job's user-selected content type."""

        normalized = validate_content_type_override(content_type)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise InvalidJobRequest("expected_revision must be zero or greater.")
        with self._transaction():
            current = self._row_to_job(self._fetch_job_row(job_id))
            if current.revision != expected_revision:
                raise RevisionConflict(
                    "The job changed after the content-type snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            if current.state in {JobState.PUBLISHING, JobState.PUBLISHED}:
                raise InvalidJobRequest(
                    "Content type cannot change during or after publication."
                )
            if current.content_type_override == normalized:
                return current, False
            revision = current.revision + 1
            now = _utc_now()
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET content_type_override = ?, revision = ?, updated_at = ?
                WHERE job_id = ? AND revision = ?
                """,
                (normalized, revision, now, job_id, current.revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    "The job changed while the content type was being saved.",
                    details={"job_id": job_id},
                )
            self._insert_event(
                job_id=job_id,
                revision=revision,
                event_type="job.content_type_override_updated",
                from_state=current.state,
                to_state=current.state,
                reason_code=(
                    "content_type_override_cleared"
                    if normalized is None
                    else "content_type_override_saved"
                ),
                payload={"content_type_override": normalized},
                created_at=now,
            )
            return self._row_to_job(self._fetch_job_row(job_id)), True

    def append_correction(
        self,
        job_id: str,
        *,
        field: CorrectionField,
        target_id: str | None,
        before: str | None,
        after: str,
        author: str,
        idempotency_key: str,
        expected_revision: int,
    ) -> tuple[CorrectionRecord, bool]:
        """Append one revision-guarded correction without mutating source evidence."""

        validate_correction(
            field=field,
            target_id=target_id,
            before=before,
            after=after,
            author=author,
        )
        validate_idempotency_key(idempotency_key)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise InvalidJobRequest("expected_revision must be zero or greater.")
        request_payload = {
            "field": field.value,
            "target_id": target_id,
            "before": before,
            "after": after,
            "author": author,
        }
        request_fingerprint = hashlib.sha256(
            _canonical_json(request_payload).encode("utf-8")
        ).hexdigest()
        with self._transaction():
            current = self._row_to_job(self._fetch_job_row(job_id))
            prior = self._connection.execute(
                "SELECT * FROM corrections WHERE job_id = ? AND idempotency_key = ?",
                (job_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != request_fingerprint:
                    raise IdempotencyConflict(
                        "The idempotency key is already bound to a different correction.",
                        details={"job_id": job_id},
                    )
                return self._row_to_correction(prior), False
            if current.revision != expected_revision:
                raise RevisionConflict(
                    "The job changed after the correction snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            if current.state is not JobState.PROCESSED:
                raise InvalidJobRequest("Corrections require a processed job.")
            self._validate_correction_target(
                job_id=job_id,
                field=field,
                target_id=target_id,
            )
            if field is CorrectionField.SEGMENT_REVIEW:
                _after_text, after_speaker_id = decode_segment_review(after)
                if after_speaker_id is not None:
                    speaker_exists = self._connection.execute(
                        """
                        SELECT 1 FROM transcript_segments
                        WHERE job_id = ? AND speaker_id = ?
                        LIMIT 1
                        """,
                        (job_id, after_speaker_id),
                    ).fetchone()
                    if speaker_exists is None:
                        raise InvalidJobRequest(
                            "The reviewed speaker_id does not exist in this job."
                        )
            previous = self._connection.execute(
                """
                SELECT after_value
                FROM corrections
                WHERE job_id = ? AND field = ? AND target_id IS ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (job_id, field.value, target_id),
            ).fetchone()
            if previous is not None and str(previous["after_value"]) != before:
                raise RevisionConflict(
                    "The corrected value changed after the caller's snapshot.",
                    details={"job_id": job_id, "field": field.value, "target_id": target_id},
                )
            if field is CorrectionField.SEGMENT_REVIEW:
                assert target_id is not None and before is not None
                segment = self._row_to_transcript_segment(
                    self._fetch_transcript_segment_row(job_id, target_id)
                )
                current_text = segment.text or ""
                current_speaker_id = segment.speaker_id
                review_rows = self._connection.execute(
                    """
                    SELECT field, after_value FROM corrections
                    WHERE job_id = ? AND target_id = ?
                      AND field IN (?, ?)
                    ORDER BY sequence ASC
                    """,
                    (
                        job_id,
                        target_id,
                        CorrectionField.TRANSCRIPT_TEXT.value,
                        CorrectionField.SEGMENT_REVIEW.value,
                    ),
                ).fetchall()
                for review_row in review_rows:
                    if str(review_row["field"]) == CorrectionField.TRANSCRIPT_TEXT.value:
                        current_text = str(review_row["after_value"])
                    else:
                        current_text, current_speaker_id = decode_segment_review(
                            str(review_row["after_value"])
                        )
                expected_before = encode_segment_review(
                    text=current_text,
                    speaker_id=current_speaker_id,
                )
                if before != expected_before:
                    raise RevisionConflict(
                        "The reviewed segment changed after the caller's snapshot.",
                        details={"job_id": job_id, "segment_id": target_id},
                    )
            revision = current.revision + 1
            correction_id = f"cor_{uuid4().hex}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO corrections (
                    correction_id, job_id, job_revision, field, target_id,
                    before_value, after_value, author, idempotency_key,
                    request_fingerprint, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correction_id,
                    job_id,
                    revision,
                    field.value,
                    target_id,
                    before,
                    after,
                    author.strip(),
                    idempotency_key,
                    request_fingerprint,
                    now,
                ),
            )
            cursor = self._connection.execute(
                """
                UPDATE jobs SET revision = ?, updated_at = ?
                WHERE job_id = ? AND revision = ?
                """,
                (revision, now, job_id, current.revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    "The job changed while the correction was being saved.",
                    details={"job_id": job_id},
                )
            self._insert_event(
                job_id=job_id,
                revision=revision,
                event_type="job.correction_appended",
                from_state=current.state,
                to_state=current.state,
                reason_code="user_correction_saved",
                payload={
                    "correction_id": correction_id,
                    "field": field.value,
                    "target_id": target_id,
                },
                created_at=now,
            )
            row = self._connection.execute(
                "SELECT * FROM corrections WHERE correction_id = ?",
                (correction_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_correction(row), True

    def list_corrections(self, job_id: str) -> list[CorrectionRecord]:
        """Return a job's immutable corrections in application order."""

        with self._lock:
            self._fetch_job_row(job_id)
            rows = self._connection.execute(
                "SELECT * FROM corrections WHERE job_id = ? ORDER BY sequence ASC",
                (job_id,),
            ).fetchall()
            return [self._row_to_correction(row) for row in rows]

    def claim_publication(
        self,
        job_id: str,
        *,
        publisher_id: str,
        target_relative_path: str,
        manifest_sha256: str,
        expected_revision: int,
        lease_seconds: int = DEFAULT_PUBLICATION_LEASE_SECONDS,
    ) -> tuple[PublicationLeaseRecord, JobRecord, bool]:
        """Claim the only active publication lease for one processed package."""

        validate_publication_lease_request(
            publisher_id=publisher_id,
            target_relative_path=target_relative_path,
            manifest_sha256=manifest_sha256,
            lease_seconds=lease_seconds,
        )
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise InvalidJobRequest("expected_revision must be zero or greater.")
        with self._transaction():
            current = self._row_to_job(self._fetch_job_row(job_id))
            active = self._fetch_active_publication_lease_row(job_id)
            now = _utc_now()
            if active is not None and not _publication_lease_expired(active, now=now):
                lease = self._row_to_publication_lease(active)
                if (
                    lease.publisher_id == publisher_id
                    and lease.target_relative_path == target_relative_path
                    and lease.manifest_sha256 == manifest_sha256
                ):
                    return lease, current, False
                raise PublicationLeaseConflict(
                    "Another publisher currently owns this job's publication lease.",
                    details={
                        "job_id": job_id,
                        "lease_expires_at": str(active["expires_at"]),
                    },
                )
            if current.revision != expected_revision:
                raise RevisionConflict(
                    "The job changed after the publication snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            if active is not None:
                self._complete_publication_lease(
                    str(active["lease_id"]),
                    state=PublicationLeaseState.EXPIRED,
                    completed_at=now,
                )
                if current.state is not JobState.PUBLISHING:
                    raise PublicationLeaseConflict(
                        "The expired lease does not match the current job state.",
                        details={"job_id": job_id},
                    )
                current = self._transition_in_transaction(
                    current=current,
                    target_state=JobState.PROCESSED,
                    reason_code="publication_lease_expired",
                    error_code=None,
                    error_message=None,
                    event_type="publication.lease_expired",
                )
            if current.state is not JobState.PROCESSED:
                raise InvalidJobRequest("Only a processed job can be claimed for publication.")
            publishing = self._transition_in_transaction(
                current=current,
                target_state=JobState.PUBLISHING,
                reason_code="publication_lease_claimed",
                error_code=None,
                error_message=None,
                event_type="publication.claimed",
            )
            generation = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM publication_leases "
                    "WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
            )
            lease_id = f"lease_{uuid4().hex}"
            expires_at = _future_utc(now, seconds=lease_seconds)
            self._connection.execute(
                """
                INSERT INTO publication_leases (
                    lease_id, job_id, generation, publisher_id,
                    target_relative_path, manifest_sha256, state,
                    expires_at, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    lease_id,
                    job_id,
                    generation,
                    publisher_id,
                    target_relative_path,
                    manifest_sha256,
                    PublicationLeaseState.ACTIVE.value,
                    expires_at,
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM publication_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_publication_lease(row), publishing, True

    def renew_publication_lease(
        self,
        job_id: str,
        *,
        lease_id: str,
        publisher_id: str,
        lease_seconds: int = DEFAULT_PUBLICATION_LEASE_SECONDS,
    ) -> PublicationLeaseRecord:
        """Extend an unexpired active lease without changing the job revision."""

        validate_publisher_id(publisher_id)
        validate_lease_seconds(lease_seconds)
        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            row = self._fetch_publication_lease_row(lease_id)
            now = _utc_now()
            self._require_owned_active_lease(
                row,
                job_id=job_id,
                publisher_id=publisher_id,
                now=now,
            )
            if job.state is not JobState.PUBLISHING:
                raise PublicationLeaseConflict(
                    "The publication lease is not attached to a publishing job."
                )
            self._connection.execute(
                """
                UPDATE publication_leases
                SET expires_at = ?, updated_at = ?
                WHERE lease_id = ? AND state = ?
                """,
                (
                    _future_utc(now, seconds=lease_seconds),
                    now,
                    lease_id,
                    PublicationLeaseState.ACTIVE.value,
                ),
            )
            return self._row_to_publication_lease(
                self._fetch_publication_lease_row(lease_id)
            )

    def release_publication_lease(
        self,
        job_id: str,
        *,
        lease_id: str,
        publisher_id: str,
        reason_code: str = "publisher_unavailable",
    ) -> JobRecord:
        """Release publication back to processed while retaining Worker artifacts."""

        validate_publisher_id(publisher_id)
        validate_reason_code(reason_code)
        with self._transaction():
            current = self._row_to_job(self._fetch_job_row(job_id))
            row = self._fetch_publication_lease_row(lease_id)
            now = _utc_now()
            self._require_owned_active_lease(
                row,
                job_id=job_id,
                publisher_id=publisher_id,
                now=now,
                allow_expired=True,
            )
            if current.state is not JobState.PUBLISHING:
                raise PublicationLeaseConflict("The job is not currently publishing.")
            self._complete_publication_lease(
                lease_id,
                state=PublicationLeaseState.RELEASED,
                completed_at=now,
            )
            return self._transition_in_transaction(
                current=current,
                target_state=JobState.PROCESSED,
                reason_code=reason_code,
                error_code=None,
                error_message=None,
                event_type="publication.released",
            )

    def acknowledge_publication(
        self,
        job_id: str,
        *,
        lease_id: str,
        publisher_id: str,
        manifest_sha256: str,
    ) -> tuple[PublicationReceiptRecord, JobRecord, bool]:
        """Acknowledge one fully written and verified Vault package."""

        validate_publisher_id(publisher_id)
        if not isinstance(manifest_sha256, str) or not SHA256_PATTERN.fullmatch(
            manifest_sha256
        ):
            raise InvalidJobRequest("manifest_sha256 must be lowercase SHA-256.")
        with self._transaction():
            current = self._row_to_job(self._fetch_job_row(job_id))
            prior = self._connection.execute(
                "SELECT * FROM publication_receipts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if prior is not None:
                receipt = self._row_to_publication_receipt(prior)
                if (
                    receipt.lease_id == lease_id
                    and receipt.publisher_id == publisher_id
                    and receipt.manifest_sha256 == manifest_sha256
                    and current.state is JobState.PUBLISHED
                ):
                    return receipt, current, False
                raise PublicationLeaseConflict(
                    "The job already has a different publication acknowledgement."
                )
            row = self._fetch_publication_lease_row(lease_id)
            now = _utc_now()
            self._require_owned_active_lease(
                row,
                job_id=job_id,
                publisher_id=publisher_id,
                now=now,
            )
            if str(row["manifest_sha256"]) != manifest_sha256:
                raise PublicationLeaseConflict(
                    "The acknowledgement manifest does not match the claimed package."
                )
            if current.state is not JobState.PUBLISHING:
                raise PublicationLeaseConflict("The job is not currently publishing.")
            self._connection.execute(
                """
                INSERT INTO publication_receipts (
                    job_id, lease_id, publisher_id, target_relative_path,
                    manifest_sha256, published_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    lease_id,
                    publisher_id,
                    str(row["target_relative_path"]),
                    manifest_sha256,
                    now,
                ),
            )
            self._complete_publication_lease(
                lease_id,
                state=PublicationLeaseState.ACKNOWLEDGED,
                completed_at=now,
            )
            published = self._transition_in_transaction(
                current=current,
                target_state=JobState.PUBLISHED,
                reason_code="publication_verified",
                error_code=None,
                error_message=None,
                event_type="publication.acknowledged",
            )
            receipt_row = self._connection.execute(
                "SELECT * FROM publication_receipts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            assert receipt_row is not None
            return self._row_to_publication_receipt(receipt_row), published, True

    def list_publication_leases(self, job_id: str) -> list[PublicationLeaseRecord]:
        with self._lock:
            self._fetch_job_row(job_id)
            rows = self._connection.execute(
                "SELECT * FROM publication_leases WHERE job_id = ? ORDER BY generation ASC",
                (job_id,),
            ).fetchall()
            return [self._row_to_publication_lease(row) for row in rows]

    def get_publication_receipt(self, job_id: str) -> PublicationReceiptRecord | None:
        with self._lock:
            self._fetch_job_row(job_id)
            row = self._connection.execute(
                "SELECT * FROM publication_receipts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return self._row_to_publication_receipt(row) if row is not None else None

    def list_jobs(
        self,
        *,
        vault_id: str | None = None,
        states: Sequence[JobState] | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        if limit < 1 or limit > 1000:
            raise InvalidJobRequest("limit must be between 1 and 1000.")
        if vault_id is not None and not SAFE_IDENTIFIER_PATTERN.fullmatch(vault_id):
            raise InvalidJobRequest("vault_id contains unsupported characters.")
        with self._lock:
            if states and vault_id is not None:
                placeholders = ", ".join("?" for _ in states)
                rows = self._connection.execute(
                    f"""
                    SELECT *
                    FROM jobs
                    WHERE vault_id = ? AND state IN ({placeholders})
                    ORDER BY created_at ASC, job_id ASC
                    LIMIT ?
                    """,
                    (vault_id, *[state.value for state in states], limit),
                ).fetchall()
            elif states:
                placeholders = ", ".join("?" for _ in states)
                rows = self._connection.execute(
                    f"""
                    SELECT *
                    FROM jobs
                    WHERE state IN ({placeholders})
                    ORDER BY created_at ASC, job_id ASC
                    LIMIT ?
                    """,
                    (*[state.value for state in states], limit),
                ).fetchall()
            elif vault_id is not None:
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE vault_id = ?
                    ORDER BY created_at ASC, job_id ASC
                    LIMIT ?
                    """,
                    (vault_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM jobs
                    ORDER BY created_at ASC, job_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def count_jobs_by_state(self, vault_ids: Sequence[str]) -> dict[JobState, int]:
        normalized = tuple(dict.fromkeys(vault_ids))
        if len(normalized) > 64 or any(
            not SAFE_IDENTIFIER_PATTERN.fullmatch(vault_id) for vault_id in normalized
        ):
            raise InvalidJobRequest("Diagnostic Vault scope is invalid.")
        if not normalized:
            return {}
        placeholders = ", ".join("?" for _ in normalized)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT state, COUNT(*) AS job_count
                FROM jobs
                WHERE vault_id IN ({placeholders})
                GROUP BY state
                ORDER BY state
                """,
                normalized,
            ).fetchall()
        return {JobState(str(row["state"])): int(row["job_count"]) for row in rows}

    def get_active_processing_job(self) -> JobRecord | None:
        with self._lock:
            placeholders = ", ".join("?" for _ in ACTIVE_PROCESSING_STATES)
            row = self._connection.execute(
                f"""
                SELECT *
                FROM jobs
                WHERE state IN ({placeholders})
                ORDER BY updated_at ASC, job_id ASC
                LIMIT 1
                """,
                tuple(state.value for state in ACTIVE_PROCESSING_STATES),
            ).fetchone()
            return self._row_to_job(row) if row is not None else None

    def get_next_schedulable_job(self) -> JobRecord | None:
        """Return the oldest queued job whose Worker source remains verified."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT jobs.*
                FROM jobs
                JOIN uploads ON uploads.upload_id = jobs.source_upload_id
                WHERE jobs.state = ? AND uploads.state = ?
                ORDER BY jobs.created_at ASC, jobs.job_id ASC
                LIMIT 1
                """,
                (JobState.QUEUED.value, UploadState.COMPLETE.value),
            ).fetchone()
            return self._row_to_job(row) if row is not None else None

    def claim_job_for_processing(
        self,
        job_id: str,
        *,
        expected_revision: int,
    ) -> JobRecord:
        """Atomically enforce one active heavy-processing job."""

        with self._transaction():
            current = self._row_to_job(self._fetch_job_row(job_id))
            if current.revision != expected_revision:
                raise RevisionConflict(
                    "The job changed after the scheduler's snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            if current.state in ACTIVE_PROCESSING_STATES:
                raise SchedulerBusy(
                    "The job has already been claimed for heavy processing.",
                    details={"active_job_id": current.job_id},
                )
            if current.state is not JobState.QUEUED:
                raise InvalidJobRequest("Only a queued job can be claimed for processing.")
            self._require_job_verified_source(current)
            active = self._fetch_active_processing_row(excluding_job_id=job_id)
            if active is not None:
                raise SchedulerBusy(
                    "Another heavy processing job is already active.",
                    details={"active_job_id": str(active["job_id"])},
                )
            return self._transition_in_transaction(
                current=current,
                target_state=JobState.PREPROCESSING,
                reason_code="scheduler_claimed",
                error_code=None,
                error_message=None,
                event_type="job.processing_claimed",
            )

    def get_job_verified_source_path(self, job_id: str) -> Path:
        with self._lock:
            job = self._row_to_job(self._fetch_job_row(job_id))
            self._require_job_verified_source(job)
            assert job.source_upload_id is not None
            return self.get_verified_source_path(job.source_upload_id)

    def get_job_stage_directory(self, job_id: str, *, stage: str) -> Path:
        """Return one private Worker-owned stage directory for internal artifacts."""

        _validate_checkpoint_identifier("stage", stage)
        with self._lock:
            self._fetch_job_row(job_id)
            job_directory = self.jobs_directory / job_id
            _ensure_private_directory(job_directory, root=self.jobs_directory)
            stage_directory = job_directory / stage
            _ensure_private_directory(stage_directory, root=job_directory)
            return stage_directory

    def get_job_duration_ms(self, job_id: str) -> int:
        with self._lock:
            job = self._row_to_job(self._fetch_job_row(job_id))
            return self._job_duration_ms(job)

    def transition_job(
        self,
        job_id: str,
        target_state: JobState,
        *,
        expected_revision: int,
        reason_code: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        event_type: str = "job.stage_changed",
    ) -> JobRecord:
        validate_reason_code(reason_code)
        validate_reason_code(error_code)
        validate_safe_message(error_message)
        _validate_event_type(event_type)
        error_states = {
            JobState.PAUSED,
            JobState.WAITING_USER,
            JobState.PARTIAL,
            JobState.FAILED,
        }
        required_error_states = {
            JobState.WAITING_USER,
            JobState.PARTIAL,
            JobState.FAILED,
        }
        if error_message is not None and error_code is None:
            raise InvalidJobRequest("error_message requires error_code.")
        if error_code is not None and target_state not in error_states:
            raise InvalidJobRequest(
                "Error details are allowed only for paused, waiting, partial, or failed states."
            )
        if target_state in required_error_states and error_code is None:
            raise InvalidJobRequest(
                f"Transition to {target_state.value} requires a stable error_code."
            )

        with self._transaction():
            current_row = self._fetch_job_row(job_id)
            current = self._row_to_job(current_row)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    "The job changed after the caller's snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            if target_state in {JobState.PUBLISHING, JobState.PUBLISHED}:
                raise InvalidJobRequest(
                    "Publishing states can be entered only through the publication lease protocol."
                )
            if target_state in ACTIVE_PROCESSING_STATES:
                active = self._fetch_active_processing_row(excluding_job_id=job_id)
                if active is not None:
                    raise SchedulerBusy(
                        "Another heavy processing job is already active.",
                        details={"active_job_id": str(active["job_id"])},
                    )
            ensure_transition_allowed(current.state, target_state)
            if (
                current.state is JobState.TRANSCRIBING
                and target_state is JobState.ALIGNING
                and self._connection.execute(
                    "SELECT 1 FROM provisional_transcripts WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                is not None
            ):
                raise InvalidJobRequest(
                    "The provisional transcript must be committed or cleared before alignment."
                )
            return self._transition_in_transaction(
                current=current,
                target_state=target_state,
                reason_code=reason_code,
                error_code=error_code,
                error_message=error_message,
                event_type=event_type,
            )

    def apply_job_action(
        self,
        job_id: str,
        *,
        action: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[JobRecord, bool]:
        """Apply one user lifecycle action exactly once across network retries."""

        action_contract = {
            "pause": (JobState.PAUSED, "user_paused", "job.paused"),
            "resume": (JobState.QUEUED, "user_resumed", "job.resumed"),
            "cancel": (JobState.CANCELLED, "user_cancelled", "job.cancelled"),
            "retry": (JobState.QUEUED, "user_retry_requested", "job.retry_requested"),
        }
        if action not in action_contract:
            raise InvalidJobRequest("The requested job action is not supported.")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise InvalidJobRequest("expected_revision must be zero or greater.")
        validate_idempotency_key(idempotency_key)
        target_state, reason_code, event_type = action_contract[action]
        request_fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "action": action,
                    "expected_revision": expected_revision,
                    "target_state": target_state.value,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self._transaction():
            prior = self._connection.execute(
                """
                SELECT request_fingerprint, response_json
                FROM job_action_requests
                WHERE job_id = ? AND idempotency_key = ?
                """,
                (job_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != request_fingerprint:
                    raise IdempotencyConflict(
                        "The idempotency key is already bound to a different job action.",
                        details={"job_id": job_id},
                    )
                return _job_record_from_dict(_json_object(str(prior["response_json"]))), False

            current = self._row_to_job(self._fetch_job_row(job_id))
            if current.revision != expected_revision:
                raise RevisionConflict(
                    "The job changed after the action snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            if action == "resume" and current.state is not JobState.PAUSED:
                raise InvalidJobRequest("Only a paused job can be resumed.")
            if action == "retry" and current.state not in {
                JobState.FAILED,
                JobState.PARTIAL,
                JobState.WAITING_USER,
            }:
                raise InvalidJobRequest(
                    "Only a failed, partial, or waiting job can be retried."
                )
            ensure_transition_allowed(current.state, target_state)
            result = self._transition_in_transaction(
                current=current,
                target_state=target_state,
                reason_code=reason_code,
                error_code=None,
                error_message=None,
                event_type=event_type,
            )
            self._connection.execute(
                """
                INSERT INTO job_action_requests (
                    job_id, idempotency_key, action, request_fingerprint,
                    response_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    action,
                    request_fingerprint,
                    _canonical_json(result.to_dict()),
                    _utc_now(),
                ),
            )
            return result, True

    def recover_interrupted_jobs(self) -> list[JobRecord]:
        """Move interrupted active work to its safe restart boundary."""

        recovered: list[JobRecord] = []
        with self._transaction():
            placeholders = ", ".join("?" for _ in RECOVERY_TARGETS)
            rows = self._connection.execute(
                f"""
                SELECT *
                FROM jobs
                WHERE state IN ({placeholders})
                ORDER BY created_at ASC, job_id ASC
                """,
                tuple(state.value for state in RECOVERY_TARGETS),
            ).fetchall()
            for row in rows:
                current = self._row_to_job(row)
                target = RECOVERY_TARGETS[current.state]
                if current.state is JobState.PUBLISHING:
                    now = _utc_now()
                    self._connection.execute(
                        """
                        UPDATE publication_leases
                        SET state = ?, updated_at = ?, completed_at = ?
                        WHERE job_id = ? AND state = ?
                        """,
                        (
                            PublicationLeaseState.RECOVERED.value,
                            now,
                            now,
                            current.job_id,
                            PublicationLeaseState.ACTIVE.value,
                        ),
                    )
                recovered.append(
                    self._transition_in_transaction(
                        current=current,
                        target_state=target,
                        reason_code="worker_restart",
                        error_code=None,
                        error_message=None,
                        event_type="job.recovered",
                    )
                )
        return recovered

    def list_events(self, job_id: str, *, after_sequence: int = 0) -> list[JobEvent]:
        if after_sequence < 0:
            raise InvalidJobRequest("after_sequence must be zero or greater.")
        with self._lock:
            self._fetch_job_row(job_id)
            rows = self._connection.execute(
                """
                SELECT *
                FROM job_events
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (job_id, after_sequence),
            ).fetchall()
            return [self._row_to_event(row) for row in rows]

    def list_job_updates(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> tuple[list[JobUpdate], bool]:
        """Read a bounded reconnect feed without transcript text in event payloads."""

        if after_sequence < 0:
            raise InvalidJobRequest("after_sequence must be zero or greater.")
        if limit < 1 or limit > 1000:
            raise InvalidJobRequest("limit must be between 1 and 1000.")
        with self._lock:
            self._fetch_job_row(job_id)
            rows = self._connection.execute(
                """
                SELECT *
                FROM job_updates
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (job_id, after_sequence, limit + 1),
            ).fetchall()
            return (
                [self._row_to_job_update(row) for row in rows[:limit]],
                len(rows) > limit,
            )

    def put_job_progress(
        self,
        job_id: str,
        *,
        processed_ms: int,
        stage_progress: float,
        elapsed_seconds: float,
        estimated_remaining_seconds: float | None = None,
        diarization_status: DiarizationStatus = DiarizationStatus.NOT_STARTED,
    ) -> tuple[JobProgress, bool]:
        """Persist one monotonic progress snapshot and emit a content-free update."""

        if not isinstance(processed_ms, int) or isinstance(processed_ms, bool) or processed_ms < 0:
            raise InvalidJobRequest("processed_ms must be zero or greater.")
        validate_progress_number(
            "stage_progress",
            stage_progress,
            minimum=0,
            maximum=1,
        )
        validate_progress_number("elapsed_seconds", elapsed_seconds, minimum=0)
        if estimated_remaining_seconds is not None:
            validate_progress_number(
                "estimated_remaining_seconds",
                estimated_remaining_seconds,
                minimum=0,
            )
        if not isinstance(diarization_status, DiarizationStatus):
            raise InvalidJobRequest("diarization_status is not supported.")

        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            if job.state not in ACTIVE_PROCESSING_STATES:
                raise InvalidJobRequest(
                    "Progress can be recorded only while the job is actively processing."
                )
            duration_ms = self._job_duration_ms(job)
            if processed_ms > duration_ms:
                raise InvalidJobRequest("processed_ms cannot exceed the source duration.")
            payload = {
                "stage": job.state.value,
                "processed_ms": processed_ms,
                "duration_ms": duration_ms,
                "stage_progress": float(stage_progress),
                "elapsed_seconds": float(elapsed_seconds),
                "estimated_remaining_seconds": (
                    float(estimated_remaining_seconds)
                    if estimated_remaining_seconds is not None
                    else None
                ),
                "diarization_status": diarization_status.value,
            }
            payload_json = _canonical_json(payload)
            payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            prior = self._connection.execute(
                "SELECT * FROM job_progress WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if prior is not None and prior["payload_sha256"] == payload_sha256:
                return self._row_to_job_progress(prior), False
            if prior is not None:
                if processed_ms < int(prior["processed_ms"]):
                    raise InvalidJobRequest("processed_ms cannot move backwards.")
                if str(prior["stage"]) == job.state.value and float(stage_progress) < float(
                    prior["stage_progress"]
                ):
                    raise InvalidJobRequest(
                        "stage_progress cannot move backwards within the same stage."
                    )
                if float(elapsed_seconds) < float(prior["elapsed_seconds"]):
                    raise InvalidJobRequest("elapsed_seconds cannot move backwards.")
                generation = int(prior["generation"]) + 1
            else:
                generation = 1
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO job_progress (
                    job_id,
                    generation,
                    stage,
                    processed_ms,
                    duration_ms,
                    stage_progress,
                    elapsed_seconds,
                    estimated_remaining_seconds,
                    diarization_status,
                    payload_sha256,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    generation = excluded.generation,
                    stage = excluded.stage,
                    processed_ms = excluded.processed_ms,
                    duration_ms = excluded.duration_ms,
                    stage_progress = excluded.stage_progress,
                    elapsed_seconds = excluded.elapsed_seconds,
                    estimated_remaining_seconds = excluded.estimated_remaining_seconds,
                    diarization_status = excluded.diarization_status,
                    payload_sha256 = excluded.payload_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    generation,
                    job.state.value,
                    processed_ms,
                    duration_ms,
                    float(stage_progress),
                    float(elapsed_seconds),
                    (
                        float(estimated_remaining_seconds)
                        if estimated_remaining_seconds is not None
                        else None
                    ),
                    diarization_status.value,
                    payload_sha256,
                    now,
                ),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="job.progress",
                payload=payload,
                created_at=now,
            )
            row = self._connection.execute(
                "SELECT * FROM job_progress WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_job_progress(row), True

    def put_provisional_transcript(
        self,
        job_id: str,
        *,
        expected_generation: int,
        start_ms: int,
        end_ms: int,
        text: str,
        language: str | None = None,
    ) -> tuple[ProvisionalTranscript, bool]:
        """Create or revise the one explicitly unstable transcript tail."""

        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise InvalidJobRequest("expected_generation must be zero or greater.")
        validate_transcript_text(text)
        validate_language(language)
        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            if job.state is not JobState.TRANSCRIBING:
                raise InvalidJobRequest(
                    "A provisional transcript can be written only while transcribing."
                )
            duration_ms = self._job_duration_ms(job)
            validate_time_range(start_ms, end_ms, duration_ms=duration_ms)
            latest_stable_end = self._latest_stable_segment_end_ms(job_id)
            if start_ms < latest_stable_end:
                raise InvalidJobRequest(
                    "A provisional transcript cannot overlap a committed segment."
                )
            payload = {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
                "language": language,
            }
            payload_json = _canonical_json(payload)
            payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            prior = self._connection.execute(
                "SELECT * FROM provisional_transcripts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if prior is not None and prior["payload_sha256"] == payload_sha256:
                return self._row_to_provisional(prior), False
            current_generation = int(prior["generation"]) if prior is not None else 0
            if current_generation != expected_generation:
                raise TranscriptRevisionConflict(
                    "The provisional transcript changed after the caller's snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_generation": expected_generation,
                        "current_generation": current_generation,
                    },
                )
            generation = current_generation + 1
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO provisional_transcripts (
                    job_id,
                    generation,
                    start_ms,
                    end_ms,
                    text,
                    language,
                    payload_sha256,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    generation = excluded.generation,
                    start_ms = excluded.start_ms,
                    end_ms = excluded.end_ms,
                    text = excluded.text,
                    language = excluded.language,
                    payload_sha256 = excluded.payload_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    generation,
                    start_ms,
                    end_ms,
                    text,
                    language,
                    payload_sha256,
                    now,
                ),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="transcript.provisional_revised",
                payload={
                    "generation": generation,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text_length": len(text),
                },
                created_at=now,
            )
            row = self._connection.execute(
                "SELECT * FROM provisional_transcripts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_provisional(row), True

    def clear_provisional_transcript(
        self,
        job_id: str,
        *,
        expected_generation: int,
    ) -> bool:
        """Clear the unstable tail with an optimistic generation guard."""

        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise InvalidJobRequest("expected_generation must be zero or greater.")
        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            prior = self._connection.execute(
                "SELECT * FROM provisional_transcripts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if prior is None:
                return False
            current_generation = int(prior["generation"])
            if current_generation != expected_generation:
                raise TranscriptRevisionConflict(
                    "The provisional transcript changed after the caller's snapshot.",
                    details={
                        "job_id": job_id,
                        "expected_generation": expected_generation,
                        "current_generation": current_generation,
                    },
                )
            self._connection.execute(
                "DELETE FROM provisional_transcripts WHERE job_id = ?",
                (job_id,),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="transcript.provisional_cleared",
                payload={"generation": current_generation},
                created_at=_utc_now(),
            )
            return True

    def commit_transcript_segment(
        self,
        job_id: str,
        *,
        commit_key: str,
        start_ms: int,
        end_ms: int,
        outcome: TranscriptOutcome,
        text: str | None = None,
        language: str | None = None,
        confidence: float | None = None,
        timing_status: TranscriptTimingStatus = TranscriptTimingStatus.ESTIMATED,
        speaker_id: str | None = None,
        speaker_label_status: SpeakerLabelStatus = SpeakerLabelStatus.PENDING,
        error_code: str | None = None,
        allow_aligning: bool = False,
    ) -> tuple[TranscriptSegment, bool]:
        """Commit one non-overlapping stable timeline outcome idempotently."""

        validate_commit_key(commit_key)
        validate_language(language)
        validate_confidence(confidence)
        validate_speaker_id(speaker_id)
        validate_reason_code(error_code)
        if not isinstance(outcome, TranscriptOutcome):
            raise InvalidJobRequest("outcome is not supported.")
        if not isinstance(timing_status, TranscriptTimingStatus):
            raise InvalidJobRequest("timing_status is not supported.")
        if not isinstance(speaker_label_status, SpeakerLabelStatus):
            raise InvalidJobRequest("speaker_label_status is not supported.")
        if not isinstance(allow_aligning, bool):
            raise InvalidJobRequest("allow_aligning must be a boolean.")
        self._validate_segment_content(
            outcome=outcome,
            text=text,
            speaker_id=speaker_id,
            speaker_label_status=speaker_label_status,
            error_code=error_code,
        )

        request_payload = {
            "commit_key": commit_key,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "outcome": outcome.value,
            "text": text,
            "language": language,
            "confidence": float(confidence) if confidence is not None else None,
            "timing_status": timing_status.value,
            "speaker_id": speaker_id,
            "speaker_label_status": speaker_label_status.value,
            "error_code": error_code,
        }
        fingerprint = hashlib.sha256(_canonical_json(request_payload).encode("utf-8")).hexdigest()
        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            allowed = job.state is JobState.TRANSCRIBING or (
                allow_aligning and job.state is JobState.ALIGNING
            )
            if not allowed:
                raise InvalidJobRequest(
                    "Stable transcript outcomes can be committed only while "
                    "transcribing or aligning."
                )
            duration_ms = self._job_duration_ms(job)
            validate_time_range(start_ms, end_ms, duration_ms=duration_ms)
            prior = self._connection.execute(
                """
                SELECT *
                FROM transcript_segments
                WHERE job_id = ? AND commit_key = ?
                """,
                (job_id, commit_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != fingerprint:
                    raise TranscriptConflict(
                        "The commit key is already bound to a different transcript segment.",
                        details={"job_id": job_id, "commit_key": commit_key},
                    )
                return self._row_to_transcript_segment(prior), False
            latest_stable_end = self._latest_stable_segment_end_ms(job_id)
            if start_ms < latest_stable_end:
                raise TranscriptConflict(
                    "Stable transcript segments must be committed in timeline order.",
                    details={"latest_stable_end_ms": latest_stable_end},
                )
            overlap = self._connection.execute(
                """
                SELECT segment_id
                FROM transcript_segments
                WHERE job_id = ? AND start_ms < ? AND end_ms > ?
                LIMIT 1
                """,
                (job_id, end_ms, start_ms),
            ).fetchone()
            if overlap is not None:
                raise TranscriptConflict(
                    "A stable transcript segment cannot overlap an existing segment.",
                    details={"conflicting_segment_id": str(overlap["segment_id"])},
                )
            sequence = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(segment_sequence), 0) + 1
                    FROM transcript_segments
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()[0]
            )
            segment_id = f"seg_{sequence:08d}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO transcript_segments (
                    job_id,
                    segment_sequence,
                    segment_id,
                    commit_key,
                    request_fingerprint,
                    revision,
                    start_ms,
                    end_ms,
                    outcome,
                    text,
                    language,
                    confidence,
                    timing_status,
                    speaker_id,
                    speaker_label_status,
                    error_code,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    sequence,
                    segment_id,
                    commit_key,
                    fingerprint,
                    start_ms,
                    end_ms,
                    outcome.value,
                    text,
                    language,
                    float(confidence) if confidence is not None else None,
                    timing_status.value,
                    speaker_id,
                    speaker_label_status.value,
                    error_code,
                    now,
                    now,
                ),
            )
            provisional = self._connection.execute(
                "SELECT * FROM provisional_transcripts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if provisional is not None and int(provisional["start_ms"]) < end_ms:
                self._connection.execute(
                    "DELETE FROM provisional_transcripts WHERE job_id = ?",
                    (job_id,),
                )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="transcript.segment_committed",
                payload={
                    "segment_sequence": sequence,
                    "segment_id": segment_id,
                    "revision": 1,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "outcome": outcome.value,
                    "text_length": len(text) if text is not None else 0,
                    "speaker_label_status": speaker_label_status.value,
                },
                created_at=now,
            )
            row = self._fetch_transcript_segment_row(job_id, segment_id)
            return self._row_to_transcript_segment(row), True

    def commit_definite_silence_segment(
        self,
        job_id: str,
        *,
        commit_key: str,
        start_ms: int,
        end_ms: int,
        gap_analysis_generation: int,
        gap_analysis_sha256: str,
    ) -> tuple[TranscriptSegment, bool]:
        """Backfill one proven-silent alignment gap from current durable evidence."""

        validate_commit_key(commit_key)
        if (
            not isinstance(gap_analysis_generation, int)
            or isinstance(gap_analysis_generation, bool)
            or gap_analysis_generation < 1
            or not isinstance(gap_analysis_sha256, str)
            or not SHA256_PATTERN.fullmatch(gap_analysis_sha256)
        ):
            raise InvalidJobRequest("Gap-analysis checkpoint identity is invalid.")

        request_payload = {
            "commit_key": commit_key,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "outcome": TranscriptOutcome.NON_SPEECH.value,
            "text": None,
            "language": None,
            "confidence": None,
            "timing_status": TranscriptTimingStatus.ALIGNED.value,
            "speaker_id": None,
            "speaker_label_status": SpeakerLabelStatus.UNAVAILABLE.value,
            "error_code": None,
            "gap_analysis_generation": gap_analysis_generation,
            "gap_analysis_sha256": gap_analysis_sha256,
        }
        fingerprint = hashlib.sha256(_canonical_json(request_payload).encode("utf-8")).hexdigest()

        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            if job.state is not JobState.ALIGNING:
                raise InvalidJobRequest(
                    "Definite-silence gaps can be committed only while aligning."
                )
            duration_ms = self._job_duration_ms(job)
            validate_time_range(start_ms, end_ms, duration_ms=duration_ms)
            self._require_current_definite_silence_evidence(
                job_id,
                start_ms=start_ms,
                end_ms=end_ms,
                source_duration_ms=duration_ms,
                gap_analysis_generation=gap_analysis_generation,
                gap_analysis_sha256=gap_analysis_sha256,
            )

            prior = self._connection.execute(
                """
                SELECT *
                FROM transcript_segments
                WHERE job_id = ? AND commit_key = ?
                """,
                (job_id, commit_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != fingerprint:
                    raise TranscriptConflict(
                        "The commit key is already bound to different silence evidence.",
                        details={"job_id": job_id, "commit_key": commit_key},
                    )
                return self._row_to_transcript_segment(prior), False

            overlap = self._connection.execute(
                """
                SELECT segment_id
                FROM transcript_segments
                WHERE job_id = ? AND start_ms < ? AND end_ms > ?
                LIMIT 1
                """,
                (job_id, end_ms, start_ms),
            ).fetchone()
            if overlap is not None:
                raise TranscriptConflict(
                    "A definite-silence gap cannot overlap an existing segment.",
                    details={"conflicting_segment_id": str(overlap["segment_id"])},
                )

            sequence = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(segment_sequence), 0) + 1
                    FROM transcript_segments
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()[0]
            )
            segment_id = f"seg_{sequence:08d}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO transcript_segments (
                    job_id,
                    segment_sequence,
                    segment_id,
                    commit_key,
                    request_fingerprint,
                    revision,
                    start_ms,
                    end_ms,
                    outcome,
                    text,
                    language,
                    confidence,
                    timing_status,
                    speaker_id,
                    speaker_label_status,
                    error_code,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    sequence,
                    segment_id,
                    commit_key,
                    fingerprint,
                    start_ms,
                    end_ms,
                    TranscriptOutcome.NON_SPEECH.value,
                    TranscriptTimingStatus.ALIGNED.value,
                    SpeakerLabelStatus.UNAVAILABLE.value,
                    now,
                    now,
                ),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="transcript.segment_committed",
                payload={
                    "segment_sequence": sequence,
                    "segment_id": segment_id,
                    "revision": 1,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "outcome": TranscriptOutcome.NON_SPEECH.value,
                    "text_length": 0,
                    "speaker_label_status": SpeakerLabelStatus.UNAVAILABLE.value,
                },
                created_at=now,
            )
            row = self._fetch_transcript_segment_row(job_id, segment_id)
            return self._row_to_transcript_segment(row), True

    def commit_reviewed_gap_segment(
        self,
        job_id: str,
        *,
        review_key: str,
        start_ms: int,
        end_ms: int,
        outcome: TranscriptOutcome,
        review_checkpoint_generation: int,
        review_checkpoint_sha256: str,
    ) -> tuple[TranscriptSegment, bool]:
        """Backfill one exact alignment gap from explicit human review."""

        _validate_gap_review_key(review_key)
        if not isinstance(outcome, TranscriptOutcome) or outcome not in {
            TranscriptOutcome.NON_SPEECH,
            TranscriptOutcome.INAUDIBLE,
        }:
            raise InvalidJobRequest("A reviewed gap outcome must be non_speech or inaudible.")
        if (
            not isinstance(review_checkpoint_generation, int)
            or isinstance(review_checkpoint_generation, bool)
            or review_checkpoint_generation < 1
            or not isinstance(review_checkpoint_sha256, str)
            or not SHA256_PATTERN.fullmatch(review_checkpoint_sha256)
        ):
            raise InvalidJobRequest("Gap-review checkpoint identity is invalid.")

        commit_key = f"gap_review_{review_key}"
        request_payload = {
            "commit_key": commit_key,
            "review_key": review_key,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "outcome": outcome.value,
            "text": None,
            "language": None,
            "confidence": None,
            "timing_status": TranscriptTimingStatus.ALIGNED.value,
            "speaker_id": None,
            "speaker_label_status": SpeakerLabelStatus.UNAVAILABLE.value,
            "error_code": None,
            "review_checkpoint_generation": review_checkpoint_generation,
            "review_checkpoint_sha256": review_checkpoint_sha256,
        }
        fingerprint = hashlib.sha256(_canonical_json(request_payload).encode("utf-8")).hexdigest()

        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            if job.state is not JobState.ALIGNING:
                raise InvalidJobRequest("Reviewed gaps can be committed only while aligning.")
            duration_ms = self._job_duration_ms(job)
            validate_time_range(start_ms, end_ms, duration_ms=duration_ms)
            self._require_current_gap_review_evidence(
                job_id,
                review_key=review_key,
                start_ms=start_ms,
                end_ms=end_ms,
                outcome=outcome,
                source_duration_ms=duration_ms,
                review_checkpoint_generation=review_checkpoint_generation,
                review_checkpoint_sha256=review_checkpoint_sha256,
            )

            prior = self._connection.execute(
                """
                SELECT *
                FROM transcript_segments
                WHERE job_id = ? AND commit_key = ?
                """,
                (job_id, commit_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != fingerprint:
                    raise TranscriptConflict(
                        "The review key is already bound to a different gap decision.",
                        details={"job_id": job_id, "review_key": review_key},
                    )
                return self._row_to_transcript_segment(prior), False

            overlap = self._connection.execute(
                """
                SELECT segment_id
                FROM transcript_segments
                WHERE job_id = ? AND start_ms < ? AND end_ms > ?
                LIMIT 1
                """,
                (job_id, end_ms, start_ms),
            ).fetchone()
            if overlap is not None:
                raise TranscriptConflict(
                    "A reviewed gap cannot overlap an existing segment.",
                    details={"conflicting_segment_id": str(overlap["segment_id"])},
                )

            sequence = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(segment_sequence), 0) + 1
                    FROM transcript_segments
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()[0]
            )
            segment_id = f"seg_{sequence:08d}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO transcript_segments (
                    job_id,
                    segment_sequence,
                    segment_id,
                    commit_key,
                    request_fingerprint,
                    revision,
                    start_ms,
                    end_ms,
                    outcome,
                    text,
                    language,
                    confidence,
                    timing_status,
                    speaker_id,
                    speaker_label_status,
                    error_code,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    sequence,
                    segment_id,
                    commit_key,
                    fingerprint,
                    start_ms,
                    end_ms,
                    outcome.value,
                    TranscriptTimingStatus.ALIGNED.value,
                    SpeakerLabelStatus.UNAVAILABLE.value,
                    now,
                    now,
                ),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="transcript.segment_committed",
                payload={
                    "segment_sequence": sequence,
                    "segment_id": segment_id,
                    "revision": 1,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "outcome": outcome.value,
                    "text_length": 0,
                    "speaker_label_status": SpeakerLabelStatus.UNAVAILABLE.value,
                    "evidence_type": "explicit_human_review",
                },
                created_at=now,
            )
            row = self._fetch_transcript_segment_row(job_id, segment_id)
            return self._row_to_transcript_segment(row), True

    def commit_natural_pause_segment(
        self,
        job_id: str,
        *,
        commit_key: str,
        start_ms: int,
        end_ms: int,
        evidence_checkpoint_generation: int,
        evidence_checkpoint_sha256: str,
    ) -> tuple[TranscriptSegment, bool]:
        """Backfill one conservative natural pause from combined durable evidence."""

        validate_commit_key(commit_key)
        if not commit_key.startswith("natural_pause_"):
            raise InvalidJobRequest("Natural-pause commit_key is invalid.")
        if (
            not isinstance(evidence_checkpoint_generation, int)
            or isinstance(evidence_checkpoint_generation, bool)
            or evidence_checkpoint_generation < 1
            or not isinstance(evidence_checkpoint_sha256, str)
            or not SHA256_PATTERN.fullmatch(evidence_checkpoint_sha256)
        ):
            raise InvalidJobRequest("Natural-pause checkpoint identity is invalid.")

        request_payload = {
            "commit_key": commit_key,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "outcome": TranscriptOutcome.NON_SPEECH.value,
            "text": None,
            "language": None,
            "confidence": None,
            "timing_status": TranscriptTimingStatus.ALIGNED.value,
            "speaker_id": None,
            "speaker_label_status": SpeakerLabelStatus.UNAVAILABLE.value,
            "error_code": None,
            "evidence_checkpoint_generation": evidence_checkpoint_generation,
            "evidence_checkpoint_sha256": evidence_checkpoint_sha256,
        }
        fingerprint = hashlib.sha256(
            _canonical_json(request_payload).encode("utf-8")
        ).hexdigest()

        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            if job.state is not JobState.ALIGNING:
                raise InvalidJobRequest(
                    "Natural pauses can be committed only while aligning."
                )
            duration_ms = self._job_duration_ms(job)
            validate_time_range(start_ms, end_ms, duration_ms=duration_ms)
            self._require_current_natural_pause_evidence(
                job_id,
                commit_key=commit_key,
                start_ms=start_ms,
                end_ms=end_ms,
                source_duration_ms=duration_ms,
                evidence_checkpoint_generation=evidence_checkpoint_generation,
                evidence_checkpoint_sha256=evidence_checkpoint_sha256,
            )

            prior = self._connection.execute(
                """
                SELECT *
                FROM transcript_segments
                WHERE job_id = ? AND commit_key = ?
                """,
                (job_id, commit_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != fingerprint:
                    raise TranscriptConflict(
                        "The commit key is already bound to different pause evidence.",
                        details={"job_id": job_id, "commit_key": commit_key},
                    )
                return self._row_to_transcript_segment(prior), False

            overlap = self._connection.execute(
                """
                SELECT segment_id
                FROM transcript_segments
                WHERE job_id = ? AND start_ms < ? AND end_ms > ?
                LIMIT 1
                """,
                (job_id, end_ms, start_ms),
            ).fetchone()
            if overlap is not None:
                raise TranscriptConflict(
                    "A natural pause cannot overlap an existing segment.",
                    details={"conflicting_segment_id": str(overlap["segment_id"])},
                )

            sequence = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(segment_sequence), 0) + 1
                    FROM transcript_segments
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()[0]
            )
            segment_id = f"seg_{sequence:08d}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO transcript_segments (
                    job_id, segment_sequence, segment_id, commit_key,
                    request_fingerprint, revision, start_ms, end_ms, outcome,
                    text, language, confidence, timing_status, speaker_id,
                    speaker_label_status, error_code, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    sequence,
                    segment_id,
                    commit_key,
                    fingerprint,
                    start_ms,
                    end_ms,
                    TranscriptOutcome.NON_SPEECH.value,
                    TranscriptTimingStatus.ALIGNED.value,
                    SpeakerLabelStatus.UNAVAILABLE.value,
                    now,
                    now,
                ),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="transcript.segment_committed",
                payload={
                    "segment_sequence": sequence,
                    "segment_id": segment_id,
                    "revision": 1,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "outcome": TranscriptOutcome.NON_SPEECH.value,
                    "text_length": 0,
                    "speaker_label_status": SpeakerLabelStatus.UNAVAILABLE.value,
                    "evidence_type": "combined_natural_pause",
                },
                created_at=now,
            )
            row = self._fetch_transcript_segment_row(job_id, segment_id)
            return self._row_to_transcript_segment(row), True

    def commit_gap_retranscription_segment(
        self,
        job_id: str,
        *,
        commit_key: str,
        start_ms: int,
        end_ms: int,
        text: str,
        language: str | None,
        confidence: float | None,
        raw_sha256: str,
        raw_relative_path: str,
    ) -> tuple[TranscriptSegment, bool]:
        """Insert one gap re-transcription outcome into the stable timeline."""

        validate_commit_key(commit_key)
        validate_language(language)
        validate_confidence(confidence)
        validate_transcript_text(text)
        if not isinstance(raw_sha256, str) or not SHA256_PATTERN.fullmatch(raw_sha256):
            raise InvalidJobRequest("Gap re-transcription raw_sha256 is invalid.")
        if (
            not isinstance(raw_relative_path, str)
            or not raw_relative_path
            or len(raw_relative_path) > 500
        ):
            raise InvalidJobRequest("Gap re-transcription raw path is invalid.")

        request_payload = {
            "commit_key": commit_key,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "outcome": TranscriptOutcome.TRANSCRIBED.value,
            "text": text,
            "language": language,
            "confidence": float(confidence) if confidence is not None else None,
            "timing_status": TranscriptTimingStatus.ALIGNED.value,
            "speaker_id": None,
            "speaker_label_status": SpeakerLabelStatus.PENDING.value,
            "error_code": None,
            "raw_sha256": raw_sha256,
            "raw_relative_path": raw_relative_path,
        }
        fingerprint = hashlib.sha256(
            _canonical_json(request_payload).encode("utf-8")
        ).hexdigest()

        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            if job.state is not JobState.ALIGNING:
                raise InvalidJobRequest(
                    "Gap re-transcription segments can be committed only while aligning."
                )
            duration_ms = self._job_duration_ms(job)
            validate_time_range(start_ms, end_ms, duration_ms=duration_ms)
            prior = self._connection.execute(
                """
                SELECT *
                FROM transcript_segments
                WHERE job_id = ? AND commit_key = ?
                """,
                (job_id, commit_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != fingerprint:
                    raise TranscriptConflict(
                        "The commit key is already bound to different gap evidence.",
                        details={"job_id": job_id, "commit_key": commit_key},
                    )
                return self._row_to_transcript_segment(prior), False
            overlap = self._connection.execute(
                """
                SELECT segment_id
                FROM transcript_segments
                WHERE job_id = ? AND start_ms < ? AND end_ms > ?
                LIMIT 1
                """,
                (job_id, end_ms, start_ms),
            ).fetchone()
            if overlap is not None:
                raise TranscriptConflict(
                    "A gap re-transcription segment cannot overlap existing segments.",
                    details={"conflicting_segment_id": str(overlap["segment_id"])},
                )
            sequence = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(segment_sequence), 0) + 1
                    FROM transcript_segments
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()[0]
            )
            segment_id = f"seg_{sequence:08d}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO transcript_segments (
                    job_id,
                    segment_sequence,
                    segment_id,
                    commit_key,
                    request_fingerprint,
                    revision,
                    start_ms,
                    end_ms,
                    outcome,
                    text,
                    language,
                    confidence,
                    timing_status,
                    speaker_id,
                    speaker_label_status,
                    error_code,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    sequence,
                    segment_id,
                    commit_key,
                    fingerprint,
                    start_ms,
                    end_ms,
                    TranscriptOutcome.TRANSCRIBED.value,
                    text,
                    language,
                    float(confidence) if confidence is not None else None,
                    TranscriptTimingStatus.ALIGNED.value,
                    SpeakerLabelStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="transcript.segment_committed",
                payload={
                    "segment_sequence": sequence,
                    "segment_id": segment_id,
                    "revision": 1,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "outcome": TranscriptOutcome.TRANSCRIBED.value,
                    "text_length": len(text),
                    "speaker_label_status": SpeakerLabelStatus.PENDING.value,
                    "evidence_type": "gap_retranscription",
                },
                created_at=now,
            )
            row = self._fetch_transcript_segment_row(job_id, segment_id)
            return self._row_to_transcript_segment(row), True

    def update_transcript_segment_metadata(
        self,
        job_id: str,
        segment_id: str,
        *,
        expected_revision: int,
        start_ms: int | None = None,
        end_ms: int | None = None,
        timing_status: TranscriptTimingStatus | None = None,
        speaker_id: str | None | object = _UNSET,
        speaker_label_status: SpeakerLabelStatus | None = None,
    ) -> TranscriptSegment:
        """Revise alignment or speaker attribution without rewriting stable text."""

        if expected_revision < 1:
            raise InvalidJobRequest("expected_revision must be one or greater.")
        if (
            start_ms is None
            and end_ms is None
            and timing_status is None
            and speaker_id is _UNSET
            and speaker_label_status is None
        ):
            raise InvalidJobRequest("At least one segment metadata field must be supplied.")
        if speaker_id is not _UNSET:
            if speaker_id is not None and not isinstance(speaker_id, str):
                raise InvalidJobRequest("speaker_id contains unsupported characters.")
            validate_speaker_id(speaker_id)
        if timing_status is not None and not isinstance(timing_status, TranscriptTimingStatus):
            raise InvalidJobRequest("timing_status is not supported.")
        if speaker_label_status is not None and not isinstance(
            speaker_label_status, SpeakerLabelStatus
        ):
            raise InvalidJobRequest("speaker_label_status is not supported.")

        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            current = self._row_to_transcript_segment(
                self._fetch_transcript_segment_row(job_id, segment_id)
            )
            if current.revision != expected_revision:
                raise TranscriptRevisionConflict(
                    "The transcript segment changed after the caller's snapshot.",
                    details={
                        "segment_id": segment_id,
                        "expected_revision": expected_revision,
                        "current_revision": current.revision,
                    },
                )
            revised_start = current.start_ms if start_ms is None else start_ms
            revised_end = current.end_ms if end_ms is None else end_ms
            revised_timing = timing_status or current.timing_status
            revised_speaker = current.speaker_id if speaker_id is _UNSET else speaker_id
            revised_speaker_status = speaker_label_status or current.speaker_label_status
            duration_ms = self._job_duration_ms(job)
            validate_time_range(revised_start, revised_end, duration_ms=duration_ms)
            self._validate_segment_content(
                outcome=current.outcome,
                text=current.text,
                speaker_id=revised_speaker,
                speaker_label_status=revised_speaker_status,
                error_code=current.error_code,
            )
            neighbor = self._connection.execute(
                """
                SELECT segment_id
                FROM transcript_segments
                WHERE
                    job_id = ?
                    AND segment_id != ?
                    AND start_ms < ?
                    AND end_ms > ?
                LIMIT 1
                """,
                (job_id, segment_id, revised_end, revised_start),
            ).fetchone()
            if neighbor is not None:
                raise TranscriptConflict(
                    "Revised transcript timing would overlap another stable segment.",
                    details={"conflicting_segment_id": str(neighbor["segment_id"])},
                )
            timing_changed = (
                revised_start != current.start_ms
                or revised_end != current.end_ms
                or revised_timing is not current.timing_status
            )
            speaker_changed = (
                revised_speaker != current.speaker_id
                or revised_speaker_status is not current.speaker_label_status
            )
            if not timing_changed and not speaker_changed:
                return current
            if timing_changed and job.state is not JobState.ALIGNING:
                raise InvalidJobRequest(
                    "Transcript timing metadata can be revised only while aligning."
                )
            if speaker_changed and job.state is not JobState.DIARIZING:
                raise InvalidJobRequest("Speaker attribution can be revised only while diarizing.")
            revision = current.revision + 1
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE transcript_segments
                SET
                    revision = ?,
                    start_ms = ?,
                    end_ms = ?,
                    timing_status = ?,
                    speaker_id = ?,
                    speaker_label_status = ?,
                    updated_at = ?
                WHERE job_id = ? AND segment_id = ? AND revision = ?
                """,
                (
                    revision,
                    revised_start,
                    revised_end,
                    revised_timing.value,
                    revised_speaker,
                    revised_speaker_status.value,
                    now,
                    job_id,
                    segment_id,
                    current.revision,
                ),
            )
            if speaker_changed and not timing_changed:
                event_type = "speaker.attribution_updated"
            elif timing_changed and not speaker_changed:
                event_type = "transcript.segment_timing_updated"
            else:
                event_type = "transcript.segment_metadata_updated"
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type=event_type,
                payload={
                    "segment_id": segment_id,
                    "segment_sequence": current.segment_sequence,
                    "revision": revision,
                    "start_ms": revised_start,
                    "end_ms": revised_end,
                    "timing_status": revised_timing.value,
                    "speaker_id": revised_speaker,
                    "speaker_label_status": revised_speaker_status.value,
                },
                created_at=now,
            )
            return self._row_to_transcript_segment(
                self._fetch_transcript_segment_row(job_id, segment_id)
            )

    def get_job_snapshot(
        self,
        job_id: str,
        *,
        after_segment_sequence: int = 0,
        segment_limit: int = 100,
    ) -> JobSnapshot:
        """Read one internally consistent, bounded reconnect snapshot."""

        if after_segment_sequence < 0:
            raise InvalidJobRequest("after_segment_sequence must be zero or greater.")
        if segment_limit < 1 or segment_limit > 500:
            raise InvalidJobRequest("segment_limit must be between 1 and 500.")
        with self._read_transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            progress_row = self._connection.execute(
                "SELECT * FROM job_progress WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            segment_rows = self._connection.execute(
                """
                SELECT *
                FROM transcript_segments
                WHERE job_id = ? AND segment_sequence > ?
                ORDER BY segment_sequence ASC
                LIMIT ?
                """,
                (job_id, after_segment_sequence, segment_limit + 1),
            ).fetchall()
            selected_rows = segment_rows[:segment_limit]
            segments = [self._row_to_transcript_segment(row) for row in selected_rows]
            provisional_row = self._connection.execute(
                "SELECT * FROM provisional_transcripts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            resource_row = self._connection.execute(
                """
                SELECT payload_json
                FROM job_checkpoints
                WHERE
                    job_id = ?
                    AND stage = 'scheduler'
                    AND checkpoint_key = 'resource_preflight'
                """,
                (job_id,),
            ).fetchone()
            latest_sequence = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM job_updates
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()[0]
            )
            return JobSnapshot(
                job=job,
                progress=(
                    self._row_to_job_progress(progress_row) if progress_row is not None else None
                ),
                stable_segments=segments,
                provisional=(
                    self._row_to_provisional(provisional_row)
                    if provisional_row is not None
                    else None
                ),
                resource_report=(
                    _json_object(resource_row["payload_json"]) if resource_row is not None else None
                ),
                latest_event_sequence=latest_sequence,
                next_after_segment_sequence=(
                    segments[-1].segment_sequence if segments else after_segment_sequence
                ),
                has_more_segments=len(segment_rows) > segment_limit,
            )

    def put_checkpoint(
        self,
        job_id: str,
        *,
        stage: str,
        checkpoint_key: str,
        payload: dict[str, Any],
    ) -> tuple[CheckpointRecord, bool]:
        """Insert or revise one durable private processing checkpoint."""

        _validate_checkpoint_identifier("stage", stage)
        _validate_checkpoint_identifier("checkpoint_key", checkpoint_key)
        payload_json = _canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = _utc_now()

        with self._transaction():
            self._fetch_job_row(job_id)
            prior = self._connection.execute(
                """
                SELECT *
                FROM job_checkpoints
                WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
                """,
                (job_id, stage, checkpoint_key),
            ).fetchone()
            if prior is not None and prior["payload_sha256"] == payload_sha256:
                return self._row_to_checkpoint(prior), False

            if prior is None:
                generation = 1
                created_at = now
                self._connection.execute(
                    """
                    INSERT INTO job_checkpoints (
                        job_id,
                        stage,
                        checkpoint_key,
                        generation,
                        payload_json,
                        payload_sha256,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        stage,
                        checkpoint_key,
                        generation,
                        payload_json,
                        payload_sha256,
                        created_at,
                        now,
                    ),
                )
                created = True
            else:
                generation = int(prior["generation"]) + 1
                created_at = str(prior["created_at"])
                self._connection.execute(
                    """
                    UPDATE job_checkpoints
                    SET generation = ?, payload_json = ?, payload_sha256 = ?, updated_at = ?
                    WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
                    """,
                    (
                        generation,
                        payload_json,
                        payload_sha256,
                        now,
                        job_id,
                        stage,
                        checkpoint_key,
                    ),
                )
                created = False

            row = self._connection.execute(
                """
                SELECT *
                FROM job_checkpoints
                WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
                """,
                (job_id, stage, checkpoint_key),
            ).fetchone()
            assert row is not None
            checkpoint = self._row_to_checkpoint(row)
            assert checkpoint.created_at == created_at
            return checkpoint, created

    def list_checkpoints(
        self,
        job_id: str,
        *,
        stage: str | None = None,
    ) -> list[CheckpointRecord]:
        with self._lock:
            self._fetch_job_row(job_id)
            if stage is None:
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM job_checkpoints
                    WHERE job_id = ?
                    ORDER BY stage ASC, checkpoint_key ASC
                    """,
                    (job_id,),
                ).fetchall()
            else:
                _validate_checkpoint_identifier("stage", stage)
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM job_checkpoints
                    WHERE job_id = ? AND stage = ?
                    ORDER BY checkpoint_key ASC
                    """,
                    (job_id, stage),
                ).fetchall()
            return [self._row_to_checkpoint(row) for row in rows]

    def commit_asr_attempt(
        self,
        job_id: str,
        *,
        chunk_index: int,
        attempt_number: int,
        attempt_key: str,
        state: AsrAttemptState,
        model_id: str,
        start_frame: int,
        end_frame: int,
        start_ms: int,
        end_ms: int,
        raw_payload: dict[str, Any],
        language: str | None = None,
        finish_reason: str | None = None,
        truncated: bool = False,
        elapsed_seconds: float = 0,
        error_code: str | None = None,
    ) -> tuple[AsrAttemptRecord, bool]:
        """Commit one immutable private raw ASR attempt and safe metadata."""

        if not isinstance(chunk_index, int) or isinstance(chunk_index, bool) or chunk_index < 0:
            raise InvalidJobRequest("chunk_index must be zero or greater.")
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number < 1
        ):
            raise InvalidJobRequest("attempt_number must be one or greater.")
        _validate_checkpoint_identifier("attempt_key", attempt_key)
        if not isinstance(state, AsrAttemptState):
            raise InvalidJobRequest("ASR attempt state is not supported.")
        if (
            not isinstance(model_id, str)
            or not model_id
            or len(model_id) > 200
            or any(not character.isprintable() for character in model_id)
        ):
            raise InvalidJobRequest("model_id must contain 1 to 200 printable characters.")
        if (
            not isinstance(start_frame, int)
            or isinstance(start_frame, bool)
            or not isinstance(end_frame, int)
            or isinstance(end_frame, bool)
            or start_frame < 0
            or end_frame <= start_frame
        ):
            raise InvalidJobRequest("ASR attempt frame range is invalid.")
        if (
            not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise InvalidJobRequest("ASR attempt time range is invalid.")
        validate_language(language)
        if finish_reason is not None and (
            not finish_reason
            or len(finish_reason) > 100
            or any(not character.isprintable() for character in finish_reason)
        ):
            raise InvalidJobRequest("finish_reason must contain 1 to 100 printable characters.")
        if not isinstance(truncated, bool):
            raise InvalidJobRequest("truncated must be a boolean.")
        if (
            not isinstance(elapsed_seconds, (int, float))
            or isinstance(elapsed_seconds, bool)
            or not math.isfinite(float(elapsed_seconds))
            or elapsed_seconds < 0
        ):
            raise InvalidJobRequest("elapsed_seconds must be a finite value of zero or greater.")
        validate_reason_code(error_code)
        if state is AsrAttemptState.SUCCEEDED and error_code is not None:
            raise InvalidJobRequest("A succeeded ASR attempt cannot contain error_code.")
        if state is not AsrAttemptState.SUCCEEDED and error_code is None:
            raise InvalidJobRequest("A rejected or failed ASR attempt requires error_code.")

        raw_json = _canonical_json(raw_payload)
        raw_bytes = raw_json.encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        request_fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "chunk_index": chunk_index,
                    "attempt_number": attempt_number,
                    "attempt_key": attempt_key,
                    "state": state.value,
                    "model_id": model_id,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "language": language,
                    "finish_reason": finish_reason,
                    "truncated": truncated,
                    "elapsed_seconds": float(elapsed_seconds),
                    "raw_sha256": raw_sha256,
                    "error_code": error_code,
                }
            ).encode("utf-8")
        ).hexdigest()

        with self._transaction():
            job = self._row_to_job(self._fetch_job_row(job_id))
            if job.state is not JobState.TRANSCRIBING:
                raise InvalidJobRequest("ASR attempts can be committed only while transcribing.")
            prior = self._connection.execute(
                """
                SELECT *
                FROM asr_attempts
                WHERE job_id = ? AND attempt_key = ?
                """,
                (job_id, attempt_key),
            ).fetchone()
            if prior is not None:
                if str(prior["request_fingerprint"]) != request_fingerprint:
                    raise AsrAttemptConflict(
                        "The attempt key is already bound to different ASR evidence.",
                        details={
                            "job_id": job_id,
                            "chunk_index": chunk_index,
                            "attempt_number": attempt_number,
                        },
                    )
                return self._row_to_asr_attempt(prior), False
            same_number = self._connection.execute(
                """
                SELECT attempt_key
                FROM asr_attempts
                WHERE job_id = ? AND chunk_index = ? AND attempt_number = ?
                """,
                (job_id, chunk_index, attempt_number),
            ).fetchone()
            if same_number is not None:
                raise AsrAttemptConflict(
                    "The ASR attempt number already exists for this chunk.",
                    details={
                        "job_id": job_id,
                        "chunk_index": chunk_index,
                        "attempt_number": attempt_number,
                    },
                )
            expected_attempt_number = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt_number), 0) + 1
                    FROM asr_attempts
                    WHERE job_id = ? AND chunk_index = ?
                    """,
                    (job_id, chunk_index),
                ).fetchone()[0]
            )
            if attempt_number != expected_attempt_number:
                raise AsrAttemptConflict(
                    "ASR attempt numbers must be committed without gaps.",
                    details={
                        "chunk_index": chunk_index,
                        "expected_attempt_number": expected_attempt_number,
                    },
                )

            raw_directory = self.get_job_stage_directory(job_id, stage="asr_raw")
            raw_path = raw_directory / (
                f"chunk-{chunk_index:08d}-attempt-{attempt_number:04d}.json"
            )
            if raw_path.is_symlink():
                raise UploadStorageError(
                    "Private ASR evidence storage must not contain symbolic links."
                )
            if raw_path.exists():
                try:
                    existing_content = raw_path.read_bytes()
                except OSError as exc:
                    raise UploadStorageError(
                        "Existing private ASR evidence could not be verified."
                    ) from exc
                existing_sha256 = hashlib.sha256(existing_content).hexdigest()
                if existing_sha256 != raw_sha256:
                    raise AsrAttemptConflict(
                        "Existing raw ASR evidence differs from this attempt.",
                        details={
                            "chunk_index": chunk_index,
                            "attempt_number": attempt_number,
                        },
                    )
            else:
                _atomic_write_bytes(raw_path, raw_bytes)
            raw_relative_path = raw_path.relative_to(self.data_directory).as_posix()
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO asr_attempts (
                    job_id,
                    chunk_index,
                    attempt_number,
                    attempt_key,
                    request_fingerprint,
                    state,
                    model_id,
                    start_frame,
                    end_frame,
                    start_ms,
                    end_ms,
                    language,
                    finish_reason,
                    truncated,
                    elapsed_seconds,
                    raw_relative_path,
                    raw_sha256,
                    error_code,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    chunk_index,
                    attempt_number,
                    attempt_key,
                    request_fingerprint,
                    state.value,
                    model_id,
                    start_frame,
                    end_frame,
                    start_ms,
                    end_ms,
                    language,
                    finish_reason,
                    int(truncated),
                    float(elapsed_seconds),
                    raw_relative_path,
                    raw_sha256,
                    error_code,
                    now,
                ),
            )
            self._insert_update(
                job_id=job_id,
                job_revision=job.revision,
                event_type="asr.attempt_recorded",
                payload={
                    "chunk_index": chunk_index,
                    "attempt_number": attempt_number,
                    "state": state.value,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "language": language,
                    "finish_reason": finish_reason,
                    "truncated": truncated,
                    "elapsed_seconds": float(elapsed_seconds),
                    "error_code": error_code,
                },
                created_at=now,
            )
            row = self._connection.execute(
                """
                SELECT *
                FROM asr_attempts
                WHERE job_id = ? AND chunk_index = ? AND attempt_number = ?
                """,
                (job_id, chunk_index, attempt_number),
            ).fetchone()
            assert row is not None
            return self._row_to_asr_attempt(row), True

    def list_asr_attempts(
        self,
        job_id: str,
        *,
        chunk_index: int | None = None,
    ) -> list[AsrAttemptRecord]:
        if chunk_index is not None and (
            not isinstance(chunk_index, int) or isinstance(chunk_index, bool) or chunk_index < 0
        ):
            raise InvalidJobRequest("chunk_index must be zero or greater.")
        with self._lock:
            self._fetch_job_row(job_id)
            if chunk_index is None:
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM asr_attempts
                    WHERE job_id = ?
                    ORDER BY chunk_index ASC, attempt_number ASC
                    """,
                    (job_id,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT *
                    FROM asr_attempts
                    WHERE job_id = ? AND chunk_index = ?
                    ORDER BY attempt_number ASC
                    """,
                    (job_id, chunk_index),
                ).fetchall()
            return [self._row_to_asr_attempt(row) for row in rows]

    def get_asr_attempt_payload(
        self,
        job_id: str,
        *,
        chunk_index: int,
        attempt_number: int,
    ) -> dict[str, Any]:
        with self._lock:
            self._fetch_job_row(job_id)
            row = self._connection.execute(
                """
                SELECT *
                FROM asr_attempts
                WHERE job_id = ? AND chunk_index = ? AND attempt_number = ?
                """,
                (job_id, chunk_index, attempt_number),
            ).fetchone()
            if row is None:
                raise InvalidJobRequest("The requested ASR attempt does not exist.")
            attempt = self._row_to_asr_attempt(row)
            path = (self.data_directory / attempt.raw_relative_path).resolve()
            root = self.jobs_directory.resolve()
            if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
                raise UploadStorageError("Private raw ASR evidence is unavailable.")
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise UploadStorageError("Private raw ASR evidence could not be read.") from exc
            if hashlib.sha256(content).hexdigest() != attempt.raw_sha256:
                raise UploadStorageError("Private raw ASR evidence failed checksum verification.")
            return _json_object(content.decode("utf-8"))

    def create_upload(
        self,
        request: UploadCreateRequest,
        *,
        idempotency_key: str,
    ) -> tuple[UploadRecord, bool]:
        """Create a durable upload manifest or reuse its identical request."""

        request.validate()
        validate_idempotency_key(idempotency_key)
        request_payload = {
            "vault_id": request.vault_id,
            "source_display_name": request.source_display_name,
            "source_sha256": request.source_sha256,
            "source_size_bytes": request.source_size_bytes,
            "media_type": request.media_type,
        }
        request_fingerprint = hashlib.sha256(
            _canonical_json(request_payload).encode("utf-8")
        ).hexdigest()
        selected_chunk_size = max(
            self.upload_chunk_size_bytes,
            (request.source_size_bytes + MAX_UPLOAD_PARTS - 1) // MAX_UPLOAD_PARTS,
        )
        if selected_chunk_size > MAX_UPLOAD_CHUNK_SIZE_BYTES:
            raise InvalidJobRequest("The source is too large for the supported upload-part limits.")
        part_count = (request.source_size_bytes + selected_chunk_size - 1) // selected_chunk_size

        with self._transaction():
            prior = self._connection.execute(
                """
                SELECT upload_id
                FROM uploads
                WHERE vault_id = ? AND idempotency_key = ?
                """,
                (request.vault_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                row = self._fetch_upload_row(str(prior["upload_id"]))
                if row["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict(
                        "The idempotency key is already bound to a different upload request.",
                        details={"vault_id": request.vault_id},
                    )
                return self._row_to_upload(row), False

            upload_id = f"upl_{uuid4().hex}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO uploads (
                    upload_id,
                    vault_id,
                    source_display_name,
                    source_sha256,
                    source_size_bytes,
                    media_type,
                    state,
                    chunk_size_bytes,
                    part_count,
                    idempotency_key,
                    request_fingerprint,
                    source_relative_path,
                    duration_seconds,
                    audio_stream_count,
                    detected_format_name,
                    last_error_code,
                    last_error_message,
                    created_at,
                    updated_at,
                    completed_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL
                )
                """,
                (
                    upload_id,
                    request.vault_id,
                    request.source_display_name,
                    request.source_sha256,
                    request.source_size_bytes,
                    request.media_type,
                    UploadState.UPLOADING.value,
                    selected_chunk_size,
                    part_count,
                    idempotency_key,
                    request_fingerprint,
                    now,
                    now,
                ),
            )
            return self._row_to_upload(self._fetch_upload_row(upload_id)), True

    def get_upload(self, upload_id: str) -> UploadRecord:
        with self._lock:
            return self._row_to_upload(self._fetch_upload_row(upload_id))

    def list_upload_parts(self, upload_id: str) -> list[UploadPartRecord]:
        with self._lock:
            self._fetch_upload_row(upload_id)
            rows = self._connection.execute(
                """
                SELECT *
                FROM upload_parts
                WHERE upload_id = ?
                ORDER BY part_number ASC
                """,
                (upload_id,),
            ).fetchall()
            return [self._row_to_upload_part(row) for row in rows]

    def list_missing_upload_parts(self, upload_id: str) -> list[int]:
        with self._lock:
            upload = self._row_to_upload(self._fetch_upload_row(upload_id))
            rows = self._connection.execute(
                """
                SELECT part_number
                FROM upload_parts
                WHERE upload_id = ?
                ORDER BY part_number ASC
                """,
                (upload_id,),
            ).fetchall()
            received = {int(row["part_number"]) for row in rows}
            return [
                part_number
                for part_number in range(1, upload.part_count + 1)
                if part_number not in received
            ]

    def put_upload_part(
        self,
        upload_id: str,
        *,
        part_number: int,
        content: bytes,
        part_sha256: str,
    ) -> tuple[UploadPartRecord, bool]:
        """Atomically store one checksum-bound upload part."""

        if not isinstance(part_number, int) or isinstance(part_number, bool) or part_number < 1:
            raise InvalidJobRequest("part_number must be a positive integer.")
        if not isinstance(content, bytes):
            raise InvalidJobRequest("Upload part content must be bytes.")
        if not _is_sha256(part_sha256):
            raise InvalidJobRequest("part_sha256 must be 64 lowercase hexadecimal characters.")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != part_sha256:
            raise UploadPartChecksumMismatch(
                "The received upload part did not match its checksum.",
                details={"part_number": part_number},
            )

        with self._transaction():
            upload = self._row_to_upload(self._fetch_upload_row(upload_id))
            if upload.state is not UploadState.UPLOADING:
                raise UploadStateConflict(
                    "Upload parts are accepted only while the upload is active.",
                    details={"upload_id": upload_id, "state": upload.state.value},
                )
            if part_number > upload.part_count:
                raise InvalidJobRequest(f"part_number must be between 1 and {upload.part_count}.")
            expected_size = _expected_part_size(upload, part_number)
            if len(content) != expected_size:
                raise InvalidJobRequest(
                    "The upload part size does not match the manifest.",
                )

            prior = self._connection.execute(
                """
                SELECT *
                FROM upload_parts
                WHERE upload_id = ? AND part_number = ?
                """,
                (upload_id, part_number),
            ).fetchone()
            if prior is not None and (
                prior["sha256"] != part_sha256 or int(prior["size_bytes"]) != len(content)
            ):
                raise UploadPartConflict(
                    "The upload part number is already bound to different content.",
                    details={"upload_id": upload_id, "part_number": part_number},
                )

            part_path = self._upload_part_path(upload_id, part_number)
            _atomic_write_bytes(part_path, content)
            now = _utc_now()
            if prior is None:
                self._connection.execute(
                    """
                    INSERT INTO upload_parts (
                        upload_id,
                        part_number,
                        size_bytes,
                        sha256,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (upload_id, part_number, len(content), part_sha256, now, now),
                )
                created = True
            else:
                self._connection.execute(
                    """
                    UPDATE upload_parts
                    SET updated_at = ?
                    WHERE upload_id = ? AND part_number = ?
                    """,
                    (now, upload_id, part_number),
                )
                created = False

            self._connection.execute(
                """
                UPDATE uploads
                SET last_error_code = NULL, last_error_message = NULL, updated_at = ?
                WHERE upload_id = ?
                """,
                (now, upload_id),
            )
            row = self._connection.execute(
                """
                SELECT *
                FROM upload_parts
                WHERE upload_id = ? AND part_number = ?
                """,
                (upload_id, part_number),
            ).fetchone()
            assert row is not None
            return self._row_to_upload_part(row), created

    def complete_upload(self, upload_id: str) -> tuple[UploadRecord, bool]:
        """Assemble, checksum, probe, and atomically accept one complete source."""

        with self._transaction():
            upload = self._row_to_upload(self._fetch_upload_row(upload_id))
            if upload.state is UploadState.COMPLETE:
                return upload, False
            if upload.state is UploadState.VERIFYING:
                raise UploadStateConflict(
                    "The upload is already being verified.",
                    details={"upload_id": upload_id, "state": upload.state.value},
                )
            missing_parts = self._list_missing_upload_parts_in_transaction(upload)
            if missing_parts:
                raise UploadIncomplete(
                    "The upload cannot be completed until every part is received.",
                    details={
                        "upload_id": upload_id,
                        "missing_part_numbers": missing_parts,
                    },
                )
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE uploads
                SET
                    state = ?,
                    last_error_code = NULL,
                    last_error_message = NULL,
                    updated_at = ?
                WHERE upload_id = ?
                """,
                (UploadState.VERIFYING.value, now, upload_id),
            )

        try:
            source_relative_path, probe = self._assemble_and_probe_upload(upload)
        except UploadPartChecksumMismatch as exc:
            self._return_corrupt_part_to_uploading(upload_id, exc)
            raise
        except WorkerCoreError as exc:
            self._mark_upload_failed(upload_id, exc)
            raise
        except OSError as exc:
            safe_error = UploadStorageError(
                "The Worker could not safely assemble the uploaded source.",
                details={"recommended_action": "Check Worker storage and retry."},
            )
            self._mark_upload_failed(upload_id, safe_error)
            raise safe_error from exc

        with self._transaction():
            current = self._row_to_upload(self._fetch_upload_row(upload_id))
            if current.state is not UploadState.VERIFYING:
                raise UploadStateConflict(
                    "The upload state changed before verification could be committed.",
                    details={"upload_id": upload_id, "state": current.state.value},
                )
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE uploads
                SET
                    state = ?,
                    source_relative_path = ?,
                    duration_seconds = ?,
                    audio_stream_count = ?,
                    detected_format_name = ?,
                    last_error_code = NULL,
                    last_error_message = NULL,
                    updated_at = ?,
                    completed_at = ?
                WHERE upload_id = ?
                """,
                (
                    UploadState.COMPLETE.value,
                    source_relative_path,
                    probe.duration_seconds,
                    probe.audio_stream_count,
                    probe.format_name,
                    now,
                    now,
                    upload_id,
                ),
            )
            return self._row_to_upload(self._fetch_upload_row(upload_id)), True

    def recover_interrupted_uploads(self) -> list[UploadRecord]:
        """Return interrupted verification to a resumable upload boundary."""

        with self._transaction():
            rows = self._connection.execute(
                """
                SELECT upload_id
                FROM uploads
                WHERE state = ?
                ORDER BY created_at ASC, upload_id ASC
                """,
                (UploadState.VERIFYING.value,),
            ).fetchall()
            now = _utc_now()
            for row in rows:
                self._connection.execute(
                    """
                    UPDATE uploads
                    SET
                        state = ?,
                        last_error_code = ?,
                        last_error_message = ?,
                        updated_at = ?
                    WHERE upload_id = ?
                    """,
                    (
                        UploadState.UPLOADING.value,
                        "UPLOAD_VERIFICATION_INTERRUPTED",
                        "Verification was interrupted and can be retried.",
                        now,
                        row["upload_id"],
                    ),
                )
            recovered = [
                self._row_to_upload(self._fetch_upload_row(str(row["upload_id"]))) for row in rows
            ]

        for temporary_path in self.sources_directory.glob(".*.assembling"):
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        return recovered

    def get_verified_source_path(self, upload_id: str) -> Path:
        """Resolve a complete Worker-owned source without exposing it in records."""

        with self._lock:
            row = self._fetch_upload_row(upload_id)
            upload = self._row_to_upload(row)
            relative_path = row["source_relative_path"]
            if upload.state is not UploadState.COMPLETE or relative_path is None:
                raise UploadStateConflict(
                    "The upload does not have a verified source.",
                    details={"upload_id": upload_id, "state": upload.state.value},
                )
            source_path = (self.data_directory / str(relative_path)).resolve()
            if source_path.parent != self.sources_directory.resolve() or not source_path.is_file():
                raise UploadStorageError(
                    "The verified Worker source is unavailable.",
                    details={"upload_id": upload_id},
                )
            if source_path.stat().st_size != upload.source_size_bytes:
                raise UploadStorageError(
                    "The verified Worker source size no longer matches its manifest.",
                    details={"upload_id": upload_id},
                )
            return source_path

    def quick_check(self) -> bool:
        with self._lock:
            row = self._connection.execute("PRAGMA quick_check").fetchone()
            return bool(row and row[0] == "ok")

    def _migrate(self) -> None:
        with self._lock:
            current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Worker database schema {current} is newer than supported {SCHEMA_VERSION}."
                )
            if current == 0:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        vault_id TEXT NOT NULL,
                        source_display_name TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes > 0),
                        state TEXT NOT NULL,
                        model_profile TEXT NOT NULL,
                        language_hint TEXT,
                        content_type_override TEXT,
                        options_json TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision >= 0),
                        last_error_code TEXT,
                        last_error_message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (vault_id, idempotency_key)
                    );

                    CREATE INDEX jobs_state_created_idx
                    ON jobs (state, created_at, job_id);

                    CREATE TABLE job_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        revision INTEGER NOT NULL CHECK (revision >= 0),
                        event_type TEXT NOT NULL,
                        from_state TEXT,
                        to_state TEXT NOT NULL,
                        reason_code TEXT,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (job_id, revision)
                    );

                    CREATE INDEX job_events_job_sequence_idx
                    ON job_events (job_id, sequence);

                    CREATE TABLE job_checkpoints (
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        checkpoint_key TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (job_id, stage, checkpoint_key)
                    );

                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
                current = 1
            if current == 1:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE uploads (
                        upload_id TEXT PRIMARY KEY,
                        vault_id TEXT NOT NULL,
                        source_display_name TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes > 0),
                        media_type TEXT NOT NULL,
                        state TEXT NOT NULL,
                        chunk_size_bytes INTEGER NOT NULL CHECK (chunk_size_bytes > 0),
                        part_count INTEGER NOT NULL CHECK (part_count > 0),
                        idempotency_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        source_relative_path TEXT,
                        duration_seconds REAL CHECK (
                            duration_seconds IS NULL OR duration_seconds > 0
                        ),
                        audio_stream_count INTEGER CHECK (
                            audio_stream_count IS NULL OR audio_stream_count > 0
                        ),
                        detected_format_name TEXT,
                        last_error_code TEXT,
                        last_error_message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        UNIQUE (vault_id, idempotency_key)
                    );

                    CREATE INDEX uploads_state_created_idx
                    ON uploads (state, created_at, upload_id);

                    CREATE TABLE upload_parts (
                        upload_id TEXT NOT NULL
                            REFERENCES uploads(upload_id) ON DELETE CASCADE,
                        part_number INTEGER NOT NULL CHECK (part_number > 0),
                        size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                        sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (upload_id, part_number)
                    );

                    CREATE INDEX upload_parts_upload_number_idx
                    ON upload_parts (upload_id, part_number);

                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
                current = 2
            if current == 2:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    ALTER TABLE jobs
                    ADD COLUMN source_upload_id TEXT REFERENCES uploads(upload_id);

                    CREATE INDEX jobs_source_upload_idx
                    ON jobs (source_upload_id);

                    PRAGMA user_version = 3;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
                current = 3
            if current == 3:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE transcript_segments (
                        job_id TEXT NOT NULL
                            REFERENCES jobs(job_id) ON DELETE CASCADE,
                        segment_sequence INTEGER NOT NULL CHECK (segment_sequence > 0),
                        segment_id TEXT NOT NULL,
                        commit_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
                        end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
                        outcome TEXT NOT NULL,
                        text TEXT,
                        language TEXT,
                        confidence REAL CHECK (
                            confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
                        ),
                        timing_status TEXT NOT NULL,
                        speaker_id TEXT,
                        speaker_label_status TEXT NOT NULL,
                        error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (job_id, segment_id),
                        UNIQUE (job_id, segment_sequence),
                        UNIQUE (job_id, commit_key)
                    );

                    CREATE INDEX transcript_segments_job_time_idx
                    ON transcript_segments (job_id, start_ms, end_ms);

                    CREATE TABLE provisional_transcripts (
                        job_id TEXT PRIMARY KEY
                            REFERENCES jobs(job_id) ON DELETE CASCADE,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
                        end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
                        text TEXT NOT NULL,
                        language TEXT,
                        payload_sha256 TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE job_progress (
                        job_id TEXT PRIMARY KEY
                            REFERENCES jobs(job_id) ON DELETE CASCADE,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        stage TEXT NOT NULL,
                        processed_ms INTEGER NOT NULL CHECK (processed_ms >= 0),
                        duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
                        stage_progress REAL NOT NULL CHECK (
                            stage_progress >= 0 AND stage_progress <= 1
                        ),
                        elapsed_seconds REAL NOT NULL CHECK (elapsed_seconds >= 0),
                        estimated_remaining_seconds REAL CHECK (
                            estimated_remaining_seconds IS NULL
                            OR estimated_remaining_seconds >= 0
                        ),
                        diarization_status TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE job_updates (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL
                            REFERENCES jobs(job_id) ON DELETE CASCADE,
                        job_revision INTEGER NOT NULL CHECK (job_revision >= 0),
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX job_updates_job_sequence_idx
                    ON job_updates (job_id, sequence);

                    INSERT INTO job_updates (
                        job_id,
                        job_revision,
                        event_type,
                        payload_json,
                        created_at
                    )
                    SELECT
                        job_id,
                        revision,
                        event_type,
                        payload_json,
                        created_at
                    FROM job_events
                    ORDER BY sequence ASC;

                    PRAGMA user_version = 4;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
                current = 4
            if current == 4:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE asr_attempts (
                        job_id TEXT NOT NULL
                            REFERENCES jobs(job_id) ON DELETE CASCADE,
                        chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
                        attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
                        attempt_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        start_frame INTEGER NOT NULL CHECK (start_frame >= 0),
                        end_frame INTEGER NOT NULL CHECK (end_frame > start_frame),
                        start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
                        end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
                        language TEXT,
                        finish_reason TEXT,
                        truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
                        elapsed_seconds REAL NOT NULL CHECK (elapsed_seconds >= 0),
                        raw_relative_path TEXT NOT NULL,
                        raw_sha256 TEXT NOT NULL,
                        error_code TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (job_id, chunk_index, attempt_number),
                        UNIQUE (job_id, attempt_key),
                        UNIQUE (raw_relative_path)
                    );

                    CREATE INDEX asr_attempts_job_state_idx
                    ON asr_attempts (job_id, state, chunk_index, attempt_number);

                    PRAGMA user_version = 5;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
                current = 5
            if current == 5:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE corrections (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        correction_id TEXT NOT NULL UNIQUE,
                        job_id TEXT NOT NULL
                            REFERENCES jobs(job_id) ON DELETE CASCADE,
                        job_revision INTEGER NOT NULL CHECK (job_revision > 0),
                        field TEXT NOT NULL,
                        target_id TEXT,
                        before_value TEXT,
                        after_value TEXT NOT NULL,
                        author TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (job_id, idempotency_key)
                    );

                    CREATE INDEX corrections_job_sequence_idx
                    ON corrections (job_id, sequence);

                    CREATE INDEX corrections_job_target_idx
                    ON corrections (job_id, field, target_id, sequence);

                    PRAGMA user_version = 6;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
                current = 6
            if current == 6:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE publication_leases (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        lease_id TEXT NOT NULL UNIQUE,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        publisher_id TEXT NOT NULL,
                        target_relative_path TEXT NOT NULL,
                        manifest_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        UNIQUE (job_id, generation)
                    );

                    CREATE UNIQUE INDEX publication_leases_job_active_idx
                    ON publication_leases (job_id)
                    WHERE state = 'active';

                    CREATE INDEX publication_leases_job_generation_idx
                    ON publication_leases (job_id, generation);

                    CREATE TABLE publication_receipts (
                        job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                        lease_id TEXT NOT NULL UNIQUE
                            REFERENCES publication_leases(lease_id),
                        publisher_id TEXT NOT NULL,
                        target_relative_path TEXT NOT NULL,
                        manifest_sha256 TEXT NOT NULL,
                        published_at TEXT NOT NULL
                    );

                    PRAGMA user_version = 7;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
                current = 7
            if current == 7:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE job_action_requests (
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        idempotency_key TEXT NOT NULL,
                        action TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (job_id, idempotency_key)
                    );

                    CREATE INDEX job_action_requests_job_created_idx
                    ON job_action_requests (job_id, created_at);

                    PRAGMA user_version = 8;
                    COMMIT;
                    """
                    )
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _fetch_job_row(self, job_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFound("The requested job does not exist.", details={"job_id": job_id})
        return row

    def _fetch_active_processing_row(
        self,
        *,
        excluding_job_id: str,
    ) -> sqlite3.Row | None:
        placeholders = ", ".join("?" for _ in ACTIVE_PROCESSING_STATES)
        return self._connection.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE state IN ({placeholders}) AND job_id != ?
            ORDER BY updated_at ASC, job_id ASC
            LIMIT 1
            """,
            (*[state.value for state in ACTIVE_PROCESSING_STATES], excluding_job_id),
        ).fetchone()

    def _require_complete_upload_for_request(self, request: JobCreateRequest) -> None:
        assert request.source_upload_id is not None
        upload = self._row_to_upload(self._fetch_upload_row(request.source_upload_id))
        if upload.state is not UploadState.COMPLETE:
            raise VerifiedUploadRequired(
                "A job can enter the processing queue only from a complete verified upload.",
                details={
                    "upload_id": upload.upload_id,
                    "upload_state": upload.state.value,
                },
            )
        mismatched_fields = [
            name
            for name, job_value, upload_value in (
                ("vault_id", request.vault_id, upload.vault_id),
                (
                    "source_display_name",
                    request.source_display_name,
                    upload.source_display_name,
                ),
                ("source_sha256", request.source_sha256, upload.source_sha256),
                (
                    "source_size_bytes",
                    request.source_size_bytes,
                    upload.source_size_bytes,
                ),
            )
            if job_value != upload_value
        ]
        if mismatched_fields:
            raise InvalidJobRequest(
                "Job source metadata must match its verified upload.",
                details={"mismatched_fields": mismatched_fields},
            )
        self.get_verified_source_path(upload.upload_id)

    def _require_job_verified_source(self, job: JobRecord) -> None:
        if job.source_upload_id is None:
            raise VerifiedUploadRequired(
                "The job is not bound to a verified Worker upload.",
                details={"job_id": job.job_id},
            )
        request = JobCreateRequest(
            vault_id=job.vault_id,
            source_upload_id=job.source_upload_id,
            source_display_name=job.source_display_name,
            source_sha256=job.source_sha256,
            source_size_bytes=job.source_size_bytes,
            model_profile=job.model_profile,
            language_hint=job.language_hint,
            content_type_override=job.content_type_override,
            options=job.options,
        )
        self._require_complete_upload_for_request(request)

    def _job_duration_ms(self, job: JobRecord) -> int:
        if job.source_upload_id is None:
            raise VerifiedUploadRequired(
                "Progressive transcript data requires a verified Worker upload.",
                details={"job_id": job.job_id},
            )
        upload = self._row_to_upload(self._fetch_upload_row(job.source_upload_id))
        if upload.state is not UploadState.COMPLETE or upload.duration_seconds is None:
            raise VerifiedUploadRequired(
                "Progressive transcript data requires a complete media-verified upload.",
                details={"upload_id": upload.upload_id},
            )
        return max(1, round(upload.duration_seconds * 1000))

    def _latest_stable_segment_end_ms(self, job_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(end_ms), 0)
            FROM transcript_segments
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        return int(row[0])

    def _fetch_transcript_segment_row(
        self,
        job_id: str,
        segment_id: str,
    ) -> sqlite3.Row:
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(segment_id) or not segment_id.startswith("seg_"):
            raise InvalidJobRequest("segment_id contains unsupported characters.")
        row = self._connection.execute(
            """
            SELECT *
            FROM transcript_segments
            WHERE job_id = ? AND segment_id = ?
            """,
            (job_id, segment_id),
        ).fetchone()
        if row is None:
            raise InvalidJobRequest(
                "The requested transcript segment does not exist.",
                details={"job_id": job_id, "segment_id": segment_id},
            )
        return row

    def _validate_correction_target(
        self,
        *,
        job_id: str,
        field: CorrectionField,
        target_id: str | None,
    ) -> None:
        if field in {CorrectionField.TRANSCRIPT_TEXT, CorrectionField.SEGMENT_REVIEW}:
            assert target_id is not None
            segment = self._row_to_transcript_segment(
                self._fetch_transcript_segment_row(job_id, target_id)
            )
            if segment.outcome is not TranscriptOutcome.TRANSCRIBED:
                raise InvalidJobRequest(
                    "Only a transcribed segment can receive a text correction."
                )
            return
        if field is CorrectionField.SPEAKER_DISPLAY_NAME:
            assert target_id is not None
            row = self._connection.execute(
                """
                SELECT 1 FROM transcript_segments
                WHERE job_id = ? AND speaker_id = ?
                LIMIT 1
                """,
                (job_id, target_id),
            ).fetchone()
            if row is None:
                raise InvalidJobRequest(
                    "The correction speaker_id does not exist in this job."
                )

    @staticmethod
    def _row_to_correction(row: sqlite3.Row) -> CorrectionRecord:
        return CorrectionRecord(
            sequence=int(row["sequence"]),
            correction_id=str(row["correction_id"]),
            job_id=str(row["job_id"]),
            job_revision=int(row["job_revision"]),
            field=CorrectionField(str(row["field"])),
            target_id=(str(row["target_id"]) if row["target_id"] is not None else None),
            before=(
                str(row["before_value"]) if row["before_value"] is not None else None
            ),
            after=str(row["after_value"]),
            author=str(row["author"]),
            idempotency_key=str(row["idempotency_key"]),
            created_at=str(row["created_at"]),
        )

    def _fetch_active_publication_lease_row(self, job_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM publication_leases WHERE job_id = ? AND state = ?",
            (job_id, PublicationLeaseState.ACTIVE.value),
        ).fetchone()

    def _fetch_publication_lease_row(self, lease_id: str) -> sqlite3.Row:
        if (
            not isinstance(lease_id, str)
            or not SAFE_IDENTIFIER_PATTERN.fullmatch(lease_id)
            or not lease_id.startswith("lease_")
        ):
            raise InvalidJobRequest("lease_id contains unsupported characters.")
        row = self._connection.execute(
            "SELECT * FROM publication_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if row is None:
            raise PublicationLeaseConflict("The publication lease does not exist.")
        return row

    @staticmethod
    def _require_owned_active_lease(
        row: sqlite3.Row,
        *,
        job_id: str,
        publisher_id: str,
        now: str,
        allow_expired: bool = False,
    ) -> None:
        if (
            str(row["job_id"]) != job_id
            or str(row["publisher_id"]) != publisher_id
            or str(row["state"]) != PublicationLeaseState.ACTIVE.value
        ):
            raise PublicationLeaseConflict("The publisher does not own the active lease.")
        if not allow_expired and _publication_lease_expired(row, now=now):
            raise PublicationLeaseConflict("The publication lease has expired.")

    def _complete_publication_lease(
        self,
        lease_id: str,
        *,
        state: PublicationLeaseState,
        completed_at: str,
    ) -> None:
        self._connection.execute(
            """
            UPDATE publication_leases
            SET state = ?, updated_at = ?, completed_at = ?
            WHERE lease_id = ? AND state = ?
            """,
            (
                state.value,
                completed_at,
                completed_at,
                lease_id,
                PublicationLeaseState.ACTIVE.value,
            ),
        )

    @staticmethod
    def _row_to_publication_lease(row: sqlite3.Row) -> PublicationLeaseRecord:
        return PublicationLeaseRecord(
            sequence=int(row["sequence"]),
            lease_id=str(row["lease_id"]),
            job_id=str(row["job_id"]),
            generation=int(row["generation"]),
            publisher_id=str(row["publisher_id"]),
            target_relative_path=str(row["target_relative_path"]),
            manifest_sha256=str(row["manifest_sha256"]),
            state=PublicationLeaseState(str(row["state"])),
            expires_at=str(row["expires_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
        )

    @staticmethod
    def _row_to_publication_receipt(row: sqlite3.Row) -> PublicationReceiptRecord:
        return PublicationReceiptRecord(
            job_id=str(row["job_id"]),
            lease_id=str(row["lease_id"]),
            publisher_id=str(row["publisher_id"]),
            target_relative_path=str(row["target_relative_path"]),
            manifest_sha256=str(row["manifest_sha256"]),
            published_at=str(row["published_at"]),
        )

    @staticmethod
    def _validate_segment_content(
        *,
        outcome: TranscriptOutcome,
        text: str | None,
        speaker_id: str | None,
        speaker_label_status: SpeakerLabelStatus,
        error_code: str | None,
    ) -> None:
        if outcome is TranscriptOutcome.TRANSCRIBED:
            if text is None:
                raise InvalidJobRequest("A transcribed segment requires text.")
            validate_transcript_text(text)
            if speaker_id is None and speaker_label_status in {
                SpeakerLabelStatus.ANONYMOUS,
                SpeakerLabelStatus.CONFIRMED,
            }:
                raise InvalidJobRequest(
                    "An anonymous or confirmed speaker status requires speaker_id."
                )
            if speaker_id is not None and speaker_label_status in {
                SpeakerLabelStatus.PENDING,
                SpeakerLabelStatus.UNAVAILABLE,
            }:
                raise InvalidJobRequest(
                    "A speaker_id requires anonymous or confirmed speaker status."
                )
        else:
            if text is not None:
                raise InvalidJobRequest(
                    "Only a transcribed timeline outcome may contain transcript text."
                )
            if speaker_id is not None or speaker_label_status is not SpeakerLabelStatus.UNAVAILABLE:
                raise InvalidJobRequest(
                    "Non-transcribed timeline outcomes cannot carry speaker attribution."
                )
        if outcome is TranscriptOutcome.FAILED and error_code is None:
            raise InvalidJobRequest("A failed timeline outcome requires error_code.")
        if outcome is not TranscriptOutcome.FAILED and error_code is not None:
            raise InvalidJobRequest("error_code is allowed only for a failed timeline outcome.")

    def _require_current_definite_silence_evidence(
        self,
        job_id: str,
        *,
        start_ms: int,
        end_ms: int,
        source_duration_ms: int,
        gap_analysis_generation: int,
        gap_analysis_sha256: str,
    ) -> None:
        gap_row = self._connection.execute(
            """
            SELECT generation, payload_json, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", "gap_audio_evidence"),
        ).fetchone()
        if (
            gap_row is None
            or int(gap_row["generation"]) != gap_analysis_generation
            or str(gap_row["payload_sha256"]) != gap_analysis_sha256
        ):
            raise InvalidJobRequest(
                "Definite-silence materialization requires the current gap evidence."
            )

        payload = _json_object(gap_row["payload_json"])
        if (
            payload.get("schema_version") != "1.0.0"
            or payload.get("alignment_report_schema_version") != "1.0.0"
            or not isinstance(payload.get("source_duration_ms"), int)
            or isinstance(payload["source_duration_ms"], bool)
            or payload.get("source_duration_ms") != source_duration_ms
            or not isinstance(payload.get("window_ms"), int)
            or isinstance(payload["window_ms"], bool)
            or payload.get("window_ms") != 20
            or not isinstance(payload.get("minimum_definite_silence_ms"), int)
            or isinstance(payload["minimum_definite_silence_ms"], bool)
            or payload.get("minimum_definite_silence_ms") != 100
            or not isinstance(
                payload.get("definite_silence_peak_threshold"),
                int,
            )
            or isinstance(payload["definite_silence_peak_threshold"], bool)
            or payload.get("definite_silence_peak_threshold") != 8
            or not isinstance(payload.get("normalized_sha256"), str)
            or not SHA256_PATTERN.fullmatch(payload["normalized_sha256"])
            or not isinstance(payload.get("sample_rate"), int)
            or isinstance(payload["sample_rate"], bool)
            or payload["sample_rate"] <= 0
            or not isinstance(payload.get("alignment_report_generation"), int)
            or isinstance(payload["alignment_report_generation"], bool)
            or payload["alignment_report_generation"] < 1
            or not isinstance(payload.get("alignment_report_sha256"), str)
            or not SHA256_PATTERN.fullmatch(payload["alignment_report_sha256"])
            or not isinstance(payload.get("evidence"), list)
        ):
            raise InvalidJobRequest("The current gap evidence is not safe to materialize.")

        alignment_row = self._connection.execute(
            """
            SELECT generation, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", "transcript_alignment_report"),
        ).fetchone()
        if (
            alignment_row is None
            or int(alignment_row["generation"]) != payload["alignment_report_generation"]
            or str(alignment_row["payload_sha256"]) != payload["alignment_report_sha256"]
        ):
            raise InvalidJobRequest("The alignment report changed after gap analysis.")

        matching = [
            value
            for value in payload["evidence"]
            if isinstance(value, dict)
            and value.get("start_ms") == start_ms
            and value.get("end_ms") == end_ms
        ]
        if len(matching) != 1:
            raise InvalidJobRequest(
                "The requested range is not present in the current gap evidence."
            )
        evidence = matching[0]
        strict_integer_fields = (
            "start_ms",
            "end_ms",
            "start_frame",
            "end_frame",
            "frame_count",
            "duration_ms",
            "peak_absolute_amplitude",
        )
        if (
            any(
                not isinstance(evidence.get(field), int) or isinstance(evidence[field], bool)
                for field in strict_integer_fields
            )
            or evidence["duration_ms"] != end_ms - start_ms
            or evidence["start_frame"] < 0
            or evidence["end_frame"] <= evidence["start_frame"]
            or evidence["start_frame"] != round(start_ms * payload["sample_rate"] / 1000)
            or evidence["end_frame"] != round(end_ms * payload["sample_rate"] / 1000)
            or evidence["frame_count"] != evidence["end_frame"] - evidence["start_frame"]
            or evidence["peak_absolute_amplitude"] < 0
            or evidence["peak_absolute_amplitude"] > 8
            or not isinstance(evidence.get("quiet_window_ratio"), (int, float))
            or isinstance(evidence["quiet_window_ratio"], bool)
            or evidence["quiet_window_ratio"] != 1
            or evidence.get("classification") != "definite_silence"
            or evidence.get("reason_code") != "PCM_NEAR_DIGITAL_SILENCE"
        ):
            raise InvalidJobRequest("The requested range is not proven definite silence.")

    def _require_current_gap_review_evidence(
        self,
        job_id: str,
        *,
        review_key: str,
        start_ms: int,
        end_ms: int,
        outcome: TranscriptOutcome,
        source_duration_ms: int,
        review_checkpoint_generation: int,
        review_checkpoint_sha256: str,
    ) -> None:
        review_row = self._connection.execute(
            """
            SELECT generation, payload_json, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", f"gap_review_{review_key}_evidence"),
        ).fetchone()
        if (
            review_row is None
            or int(review_row["generation"]) != review_checkpoint_generation
            or str(review_row["payload_sha256"]) != review_checkpoint_sha256
        ):
            raise InvalidJobRequest(
                "Reviewed-gap materialization requires the current review evidence."
            )

        payload = _json_object(review_row["payload_json"])
        expected_reason_code = (
            "HUMAN_CONFIRMED_NON_SPEECH"
            if outcome is TranscriptOutcome.NON_SPEECH
            else "HUMAN_CONFIRMED_INAUDIBLE"
        )
        if (
            payload.get("schema_version") != "1.0.0"
            or payload.get("evidence_type") != "explicit_human_review"
            or payload.get("review_key") != review_key
            or not isinstance(payload.get("start_ms"), int)
            or isinstance(payload["start_ms"], bool)
            or payload.get("start_ms") != start_ms
            or not isinstance(payload.get("end_ms"), int)
            or isinstance(payload["end_ms"], bool)
            or payload.get("end_ms") != end_ms
            or payload.get("outcome") != outcome.value
            or payload.get("reason_code") != expected_reason_code
            or not isinstance(payload.get("source_duration_ms"), int)
            or isinstance(payload["source_duration_ms"], bool)
            or payload.get("source_duration_ms") != source_duration_ms
            or payload.get("alignment_report_schema_version") != "1.0.0"
            or not isinstance(payload.get("alignment_report_generation"), int)
            or isinstance(payload["alignment_report_generation"], bool)
            or payload["alignment_report_generation"] < 1
            or not isinstance(payload.get("alignment_report_sha256"), str)
            or not SHA256_PATTERN.fullmatch(payload["alignment_report_sha256"])
        ):
            raise InvalidJobRequest("The current gap-review evidence is invalid.")

        alignment_row = self._connection.execute(
            """
            SELECT generation, payload_json, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", "transcript_alignment_report"),
        ).fetchone()
        if (
            alignment_row is None
            or int(alignment_row["generation"]) != payload["alignment_report_generation"]
            or str(alignment_row["payload_sha256"]) != payload["alignment_report_sha256"]
        ):
            raise InvalidJobRequest("The alignment report changed after gap review.")

        alignment_payload = _json_object(alignment_row["payload_json"])
        unresolved_ranges = alignment_payload.get("unresolved_ranges")
        if (
            alignment_payload.get("schema_version") != "1.0.0"
            or alignment_payload.get("source_duration_ms") != source_duration_ms
            or not isinstance(unresolved_ranges, list)
        ):
            raise InvalidJobRequest("The current alignment report is invalid.")
        cursor = 0
        unresolved_duration_ms = 0
        for value in unresolved_ranges:
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("start_ms"), int)
                or isinstance(value["start_ms"], bool)
                or not isinstance(value.get("end_ms"), int)
                or isinstance(value["end_ms"], bool)
                or value["start_ms"] < cursor
                or value["start_ms"] < 0
                or value["end_ms"] <= value["start_ms"]
                or value["end_ms"] > source_duration_ms
            ):
                raise InvalidJobRequest("The current alignment report is invalid.")
            unresolved_duration_ms += value["end_ms"] - value["start_ms"]
            cursor = value["end_ms"]
        if unresolved_duration_ms != alignment_payload.get("unresolved_duration_ms"):
            raise InvalidJobRequest("The current alignment report is invalid.")
        matching = [
            value
            for value in unresolved_ranges
            if isinstance(value, dict)
            and value.get("start_ms") == start_ms
            and value.get("end_ms") == end_ms
        ]
        if len(matching) != 1:
            raise InvalidJobRequest("A review must match one complete current unresolved range.")

    def _require_current_natural_pause_evidence(
        self,
        job_id: str,
        *,
        commit_key: str,
        start_ms: int,
        end_ms: int,
        source_duration_ms: int,
        evidence_checkpoint_generation: int,
        evidence_checkpoint_sha256: str,
    ) -> None:
        evidence_row = self._connection.execute(
            """
            SELECT generation, payload_json, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", f"{commit_key}_evidence"),
        ).fetchone()
        if (
            evidence_row is None
            or int(evidence_row["generation"]) != evidence_checkpoint_generation
            or str(evidence_row["payload_sha256"]) != evidence_checkpoint_sha256
        ):
            raise InvalidJobRequest(
                "Natural-pause materialization requires its current combined evidence."
            )
        payload = _json_object(evidence_row["payload_json"])
        if (
            payload.get("schema_version") != "1.0.0"
            or payload.get("evidence_type") != "combined_natural_pause"
            or payload.get("commit_key") != commit_key
            or payload.get("start_ms") != start_ms
            or payload.get("end_ms") != end_ms
            or payload.get("outcome") != TranscriptOutcome.NON_SPEECH.value
            or payload.get("source_duration_ms") != source_duration_ms
            or payload.get("reason_code")
            not in {
                "VAD_CONFIRMED_NO_SPEECH",
                "VAD_BOUNDARY_RESIDUAL_AND_ASR_REJECTED",
            }
            or not isinstance(payload.get("alignment_report_generation"), int)
            or isinstance(payload["alignment_report_generation"], bool)
            or payload["alignment_report_generation"] < 1
            or not isinstance(payload.get("alignment_report_sha256"), str)
            or not SHA256_PATTERN.fullmatch(payload["alignment_report_sha256"])
            or not isinstance(payload.get("speech_activity_generation"), int)
            or isinstance(payload["speech_activity_generation"], bool)
            or payload["speech_activity_generation"] < 1
            or not isinstance(payload.get("speech_activity_sha256"), str)
            or not SHA256_PATTERN.fullmatch(payload["speech_activity_sha256"])
            or not isinstance(payload.get("speech_evidence"), dict)
        ):
            raise InvalidJobRequest("The natural-pause evidence is invalid.")

        alignment_row = self._connection.execute(
            """
            SELECT generation, payload_json, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", "transcript_alignment_report"),
        ).fetchone()
        if (
            alignment_row is None
            or int(alignment_row["generation"])
            != payload["alignment_report_generation"]
            or str(alignment_row["payload_sha256"])
            != payload["alignment_report_sha256"]
        ):
            raise InvalidJobRequest("The alignment changed after pause evidence was recorded.")
        alignment_payload = _json_object(alignment_row["payload_json"])
        unresolved_ranges = alignment_payload.get("unresolved_ranges")
        if (
            alignment_payload.get("schema_version") != "1.0.0"
            or alignment_payload.get("source_duration_ms") != source_duration_ms
            or not isinstance(unresolved_ranges, list)
        ):
            raise InvalidJobRequest("The pause no longer matches one current unresolved range.")
        cursor = 0
        unresolved_duration_ms = 0
        matching_range_count = 0
        for value in unresolved_ranges:
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("start_ms"), int)
                or isinstance(value["start_ms"], bool)
                or not isinstance(value.get("end_ms"), int)
                or isinstance(value["end_ms"], bool)
                or value["start_ms"] < cursor
                or value["start_ms"] < 0
                or value["end_ms"] <= value["start_ms"]
                or value["end_ms"] > source_duration_ms
            ):
                raise InvalidJobRequest(
                    "The pause no longer matches one current unresolved range."
                )
            unresolved_duration_ms += value["end_ms"] - value["start_ms"]
            matching_range_count += int(
                value["start_ms"] == start_ms and value["end_ms"] == end_ms
            )
            cursor = value["end_ms"]
        if (
            unresolved_duration_ms != alignment_payload.get("unresolved_duration_ms")
            or matching_range_count != 1
        ):
            raise InvalidJobRequest(
                "The pause no longer matches one current unresolved range."
            )

        speech_row = self._connection.execute(
            """
            SELECT generation, payload_json, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", "gap_speech_activity_evidence"),
        ).fetchone()
        if (
            speech_row is None
            or int(speech_row["generation"]) != payload["speech_activity_generation"]
            or str(speech_row["payload_sha256"]) != payload["speech_activity_sha256"]
        ):
            raise InvalidJobRequest("The speech-activity evidence changed.")
        speech_payload = _json_object(speech_row["payload_json"])
        speech_values = speech_payload.get("evidence")
        if not isinstance(speech_values, list):
            raise InvalidJobRequest("The speech-activity evidence is not current.")
        matching_speech = [
            value
            for value in speech_values
            if isinstance(value, dict)
            and value.get("start_ms") == start_ms
            and value.get("end_ms") == end_ms
        ]
        if (
            speech_payload.get("alignment_report_generation")
            != payload["alignment_report_generation"]
            or speech_payload.get("alignment_report_sha256")
            != payload["alignment_report_sha256"]
            or len(matching_speech) != 1
            or matching_speech[0] != payload["speech_evidence"]
        ):
            raise InvalidJobRequest("The speech-activity evidence is not current.")

        retranscription_key = payload.get("retranscription_checkpoint_key")
        if payload["reason_code"] == "VAD_CONFIRMED_NO_SPEECH":
            if (
                payload["speech_evidence"].get("observation") != "no_speech_detected"
                or payload["speech_evidence"].get("speech_duration_ms") != 0
                or payload["speech_evidence"].get("speech_regions") != []
                or retranscription_key is not None
            ):
                raise InvalidJobRequest("The no-speech pause evidence is invalid.")
            return

        if (
            payload["speech_evidence"].get("observation") != "speech_detected"
            or not isinstance(retranscription_key, str)
            or not (
                retranscription_key.startswith("gap_retranscription_rejected_")
                or retranscription_key.startswith("gap_retranscription_failed_")
            )
            or not isinstance(payload.get("retranscription_checkpoint_generation"), int)
            or isinstance(payload["retranscription_checkpoint_generation"], bool)
            or not isinstance(payload.get("retranscription_checkpoint_sha256"), str)
            or not SHA256_PATTERN.fullmatch(payload["retranscription_checkpoint_sha256"])
        ):
            raise InvalidJobRequest("The boundary-residual pause evidence is invalid.")
        retranscription_row = self._connection.execute(
            """
            SELECT generation, payload_json, payload_sha256
            FROM job_checkpoints
            WHERE job_id = ? AND stage = ? AND checkpoint_key = ?
            """,
            (job_id, "aligning", retranscription_key),
        ).fetchone()
        if (
            retranscription_row is None
            or int(retranscription_row["generation"])
            != payload["retranscription_checkpoint_generation"]
            or str(retranscription_row["payload_sha256"])
            != payload["retranscription_checkpoint_sha256"]
        ):
            raise InvalidJobRequest("The gap retranscription evidence changed.")
        retranscription_payload = _json_object(retranscription_row["payload_json"])
        if (
            retranscription_payload.get("start_ms") != start_ms
            or retranscription_payload.get("end_ms") != end_ms
        ):
            raise InvalidJobRequest("The gap retranscription evidence range is invalid.")

    def _fetch_upload_row(self, upload_id: str) -> sqlite3.Row:
        _validate_upload_id(upload_id)
        row = self._connection.execute(
            """
            SELECT
                uploads.*,
                COUNT(upload_parts.part_number) AS received_part_count,
                COALESCE(SUM(upload_parts.size_bytes), 0) AS received_bytes
            FROM uploads
            LEFT JOIN upload_parts ON upload_parts.upload_id = uploads.upload_id
            WHERE uploads.upload_id = ?
            GROUP BY uploads.upload_id
            """,
            (upload_id,),
        ).fetchone()
        if row is None:
            raise UploadNotFound(
                "The requested upload does not exist.",
                details={"upload_id": upload_id},
            )
        return row

    def _list_missing_upload_parts_in_transaction(self, upload: UploadRecord) -> list[int]:
        rows = self._connection.execute(
            """
            SELECT part_number
            FROM upload_parts
            WHERE upload_id = ?
            ORDER BY part_number ASC
            """,
            (upload.upload_id,),
        ).fetchall()
        received = {int(row["part_number"]) for row in rows}
        return [
            part_number
            for part_number in range(1, upload.part_count + 1)
            if part_number not in received
        ]

    def _upload_part_path(self, upload_id: str, part_number: int) -> Path:
        _validate_upload_id(upload_id)
        part_directory = self.uploads_directory / upload_id / "parts"
        _ensure_private_directory(part_directory, root=self.uploads_directory)
        return part_directory / f"{part_number:08d}.part"

    def _assemble_and_probe_upload(
        self,
        upload: UploadRecord,
    ) -> tuple[str, MediaProbeResult]:
        source_path = self.sources_directory / f"{upload.upload_id}.source"
        temporary_path = self.sources_directory / f".{upload.upload_id}.{uuid4().hex}.assembling"
        whole_hasher = hashlib.sha256()
        total_size = 0
        _ensure_private_directory(self.sources_directory, root=self.data_directory)

        try:
            file_descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(file_descriptor, "wb") as assembled:
                for part in self.list_upload_parts(upload.upload_id):
                    part_path = self._upload_part_path(upload.upload_id, part.part_number)
                    part_hasher = hashlib.sha256()
                    part_size = 0
                    try:
                        with part_path.open("rb") as source:
                            while buffer := source.read(COPY_BUFFER_BYTES):
                                assembled.write(buffer)
                                whole_hasher.update(buffer)
                                part_hasher.update(buffer)
                                buffer_size = len(buffer)
                                total_size += buffer_size
                                part_size += buffer_size
                    except FileNotFoundError as exc:
                        raise UploadPartChecksumMismatch(
                            "A previously received upload part is unavailable.",
                            details={"part_number": part.part_number},
                        ) from exc
                    if part_size != part.size_bytes or part_hasher.hexdigest() != part.sha256:
                        raise UploadPartChecksumMismatch(
                            "A previously received upload part failed verification.",
                            details={"part_number": part.part_number},
                        )
                assembled.flush()
                os.fsync(assembled.fileno())

            if total_size != upload.source_size_bytes:
                raise UploadChecksumMismatch(
                    "The assembled source size did not match the upload manifest.",
                    details={
                        "expected_size_bytes": upload.source_size_bytes,
                        "actual_size_bytes": total_size,
                    },
                )
            if whole_hasher.hexdigest() != upload.source_sha256:
                raise UploadChecksumMismatch(
                    "The assembled source checksum did not match the upload manifest.",
                )

            probe = self._source_probe(temporary_path)
            os.replace(temporary_path, source_path)
            _fsync_directory(self.sources_directory)
            return source_path.relative_to(self.data_directory).as_posix(), probe
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _return_corrupt_part_to_uploading(
        self,
        upload_id: str,
        error: UploadPartChecksumMismatch,
    ) -> None:
        part_number = error.details.get("part_number")
        with self._transaction():
            if isinstance(part_number, int):
                self._connection.execute(
                    """
                    DELETE FROM upload_parts
                    WHERE upload_id = ? AND part_number = ?
                    """,
                    (upload_id, part_number),
                )
            self._connection.execute(
                """
                UPDATE uploads
                SET
                    state = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    updated_at = ?
                WHERE upload_id = ?
                """,
                (
                    UploadState.UPLOADING.value,
                    error.code,
                    error.message,
                    _utc_now(),
                    upload_id,
                ),
            )

    def _mark_upload_failed(self, upload_id: str, error: WorkerCoreError) -> None:
        with self._transaction():
            self._connection.execute(
                """
                UPDATE uploads
                SET
                    state = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    updated_at = ?
                WHERE upload_id = ?
                """,
                (
                    UploadState.FAILED.value,
                    error.code,
                    error.message,
                    _utc_now(),
                    upload_id,
                ),
            )

    def _transition_in_transaction(
        self,
        *,
        current: JobRecord,
        target_state: JobState,
        reason_code: str | None,
        error_code: str | None,
        error_message: str | None,
        event_type: str,
    ) -> JobRecord:
        revision = current.revision + 1
        now = _utc_now()
        cursor = self._connection.execute(
            """
            UPDATE jobs
            SET
                state = ?,
                revision = ?,
                last_error_code = ?,
                last_error_message = ?,
                updated_at = ?
            WHERE job_id = ? AND revision = ?
            """,
            (
                target_state.value,
                revision,
                error_code,
                error_message,
                now,
                current.job_id,
                current.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict(
                "The job changed while the transition was being committed.",
                details={"job_id": current.job_id},
            )
        self._insert_event(
            job_id=current.job_id,
            revision=revision,
            event_type=event_type,
            from_state=current.state,
            to_state=target_state,
            reason_code=reason_code,
            payload={
                key: value
                for key, value in {
                    "error_code": error_code,
                    "error_message": error_message,
                }.items()
                if value is not None
            },
            created_at=now,
        )
        return self._row_to_job(self._fetch_job_row(current.job_id))

    def _insert_event(
        self,
        *,
        job_id: str,
        revision: int,
        event_type: str,
        from_state: JobState | None,
        to_state: JobState,
        reason_code: str | None,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        event_payload = {
            **payload,
            "from_state": from_state.value if from_state is not None else None,
            "to_state": to_state.value,
            "reason_code": reason_code,
        }
        self._connection.execute(
            """
            INSERT INTO job_events (
                job_id,
                revision,
                event_type,
                from_state,
                to_state,
                reason_code,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                revision,
                event_type,
                from_state.value if from_state is not None else None,
                to_state.value,
                reason_code,
                _canonical_json(payload),
                created_at,
            ),
        )
        self._insert_update(
            job_id=job_id,
            job_revision=revision,
            event_type=event_type,
            payload=event_payload,
            created_at=created_at,
        )

    def _insert_update(
        self,
        *,
        job_id: str,
        job_revision: int,
        event_type: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        _validate_event_type(event_type)
        self._connection.execute(
            """
            INSERT INTO job_updates (
                job_id,
                job_revision,
                event_type,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_id,
                job_revision,
                event_type,
                _canonical_json(payload),
                created_at,
            ),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=str(row["job_id"]),
            vault_id=str(row["vault_id"]),
            source_upload_id=(
                str(row["source_upload_id"]) if row["source_upload_id"] is not None else None
            ),
            source_display_name=str(row["source_display_name"]),
            source_sha256=str(row["source_sha256"]),
            source_size_bytes=int(row["source_size_bytes"]),
            state=JobState(row["state"]),
            model_profile=ModelProfile(row["model_profile"]),
            language_hint=row["language_hint"],
            content_type_override=row["content_type_override"],
            options=_json_object(row["options_json"]),
            revision=int(row["revision"]),
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> JobEvent:
        from_state = row["from_state"]
        return JobEvent(
            sequence=int(row["sequence"]),
            job_id=str(row["job_id"]),
            revision=int(row["revision"]),
            event_type=str(row["event_type"]),
            from_state=JobState(from_state) if from_state is not None else None,
            to_state=JobState(row["to_state"]),
            reason_code=row["reason_code"],
            payload=_json_object(row["payload_json"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> CheckpointRecord:
        return CheckpointRecord(
            job_id=str(row["job_id"]),
            stage=str(row["stage"]),
            checkpoint_key=str(row["checkpoint_key"]),
            generation=int(row["generation"]),
            payload=_json_object(row["payload_json"]),
            payload_sha256=str(row["payload_sha256"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_upload(row: sqlite3.Row) -> UploadRecord:
        return UploadRecord(
            upload_id=str(row["upload_id"]),
            vault_id=str(row["vault_id"]),
            source_display_name=str(row["source_display_name"]),
            source_sha256=str(row["source_sha256"]),
            source_size_bytes=int(row["source_size_bytes"]),
            media_type=str(row["media_type"]),
            state=UploadState(row["state"]),
            chunk_size_bytes=int(row["chunk_size_bytes"]),
            part_count=int(row["part_count"]),
            received_part_count=int(row["received_part_count"]),
            received_bytes=int(row["received_bytes"]),
            duration_seconds=(
                float(row["duration_seconds"]) if row["duration_seconds"] is not None else None
            ),
            audio_stream_count=(
                int(row["audio_stream_count"]) if row["audio_stream_count"] is not None else None
            ),
            detected_format_name=(
                str(row["detected_format_name"])
                if row["detected_format_name"] is not None
                else None
            ),
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=(str(row["completed_at"]) if row["completed_at"] is not None else None),
        )

    @staticmethod
    def _row_to_upload_part(row: sqlite3.Row) -> UploadPartRecord:
        return UploadPartRecord(
            upload_id=str(row["upload_id"]),
            part_number=int(row["part_number"]),
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_job_progress(row: sqlite3.Row) -> JobProgress:
        return JobProgress(
            job_id=str(row["job_id"]),
            generation=int(row["generation"]),
            stage=JobState(row["stage"]),
            processed_ms=int(row["processed_ms"]),
            duration_ms=int(row["duration_ms"]),
            stage_progress=float(row["stage_progress"]),
            elapsed_seconds=float(row["elapsed_seconds"]),
            estimated_remaining_seconds=(
                float(row["estimated_remaining_seconds"])
                if row["estimated_remaining_seconds"] is not None
                else None
            ),
            diarization_status=DiarizationStatus(row["diarization_status"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_provisional(row: sqlite3.Row) -> ProvisionalTranscript:
        return ProvisionalTranscript(
            job_id=str(row["job_id"]),
            generation=int(row["generation"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            text=str(row["text"]),
            language=str(row["language"]) if row["language"] is not None else None,
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_transcript_segment(row: sqlite3.Row) -> TranscriptSegment:
        return TranscriptSegment(
            job_id=str(row["job_id"]),
            segment_sequence=int(row["segment_sequence"]),
            segment_id=str(row["segment_id"]),
            commit_key=str(row["commit_key"]),
            revision=int(row["revision"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            outcome=TranscriptOutcome(row["outcome"]),
            text=str(row["text"]) if row["text"] is not None else None,
            language=str(row["language"]) if row["language"] is not None else None,
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            timing_status=TranscriptTimingStatus(row["timing_status"]),
            speaker_id=str(row["speaker_id"]) if row["speaker_id"] is not None else None,
            speaker_label_status=SpeakerLabelStatus(row["speaker_label_status"]),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_job_update(row: sqlite3.Row) -> JobUpdate:
        return JobUpdate(
            sequence=int(row["sequence"]),
            job_id=str(row["job_id"]),
            job_revision=int(row["job_revision"]),
            event_type=str(row["event_type"]),
            payload=_json_object(row["payload_json"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_asr_attempt(row: sqlite3.Row) -> AsrAttemptRecord:
        return AsrAttemptRecord(
            job_id=str(row["job_id"]),
            chunk_index=int(row["chunk_index"]),
            attempt_number=int(row["attempt_number"]),
            attempt_key=str(row["attempt_key"]),
            state=AsrAttemptState(row["state"]),
            model_id=str(row["model_id"]),
            start_frame=int(row["start_frame"]),
            end_frame=int(row["end_frame"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            language=str(row["language"]) if row["language"] is not None else None,
            finish_reason=(str(row["finish_reason"]) if row["finish_reason"] is not None else None),
            truncated=bool(row["truncated"]),
            elapsed_seconds=float(row["elapsed_seconds"]),
            raw_relative_path=str(row["raw_relative_path"]),
            raw_sha256=str(row["raw_sha256"]),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            created_at=str(row["created_at"]),
        )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidJobRequest("Job data must be valid finite JSON.") from exc


def _job_record_from_dict(value: dict[str, Any]) -> JobRecord:
    return JobRecord(
        job_id=str(value["job_id"]),
        vault_id=str(value["vault_id"]),
        source_upload_id=(
            str(value["source_upload_id"])
            if value.get("source_upload_id") is not None
            else None
        ),
        source_display_name=str(value["source_display_name"]),
        source_sha256=str(value["source_sha256"]),
        source_size_bytes=int(value["source_size_bytes"]),
        state=JobState(str(value["state"])),
        model_profile=ModelProfile(str(value["model_profile"])),
        language_hint=(
            str(value["language_hint"]) if value.get("language_hint") is not None else None
        ),
        content_type_override=(
            str(value["content_type_override"])
            if value.get("content_type_override") is not None
            else None
        ),
        options=dict(value.get("options", {})),
        revision=int(value["revision"]),
        last_error_code=(
            str(value["last_error_code"])
            if value.get("last_error_code") is not None
            else None
        ),
        last_error_message=(
            str(value["last_error_message"])
            if value.get("last_error_message") is not None
            else None
        ),
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
    )


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError("Worker database JSON payload is not an object.")
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _future_utc(now: str, *, seconds: int) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat()


def _publication_lease_expired(row: sqlite3.Row, *, now: str) -> bool:
    return datetime.fromisoformat(str(row["expires_at"])) <= datetime.fromisoformat(now)


def _validate_checkpoint_identifier(name: str, value: str) -> None:
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidJobRequest(f"{name} contains unsupported characters.")


def _validate_gap_review_key(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 80
        or not SAFE_IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise InvalidJobRequest("review_key contains unsupported characters.")


def _validate_event_type(value: str) -> None:
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidJobRequest("event_type contains unsupported characters.")


def _validate_upload_id(value: str) -> None:
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(value) or not value.startswith("upl_"):
        raise InvalidJobRequest("upload_id contains unsupported characters.")


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _expected_part_size(upload: UploadRecord, part_number: int) -> int:
    if part_number < upload.part_count:
        return upload.chunk_size_bytes
    return upload.source_size_bytes - upload.chunk_size_bytes * (upload.part_count - 1)


def _ensure_private_directory(directory: Path, *, root: Path | None = None) -> None:
    if directory.is_symlink():
        raise UploadStorageError("Worker storage directories must not be symbolic links.")
    if root is not None:
        resolved_root = root.resolve()
        resolved_parent = directory.parent.resolve()
        if resolved_parent != resolved_root and not resolved_parent.is_relative_to(resolved_root):
            raise UploadStorageError("Worker storage resolved outside its application directory.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root is not None:
        resolved_directory = directory.resolve()
        if resolved_directory != resolved_root and not resolved_directory.is_relative_to(
            resolved_root
        ):
            raise UploadStorageError("Worker storage resolved outside its application directory.")
    try:
        directory.chmod(0o700)
    except OSError as exc:
        raise UploadStorageError("Worker storage permissions could not be secured.") from exc


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    if destination.parent.is_symlink() or destination.is_symlink():
        raise UploadStorageError("Worker upload storage must not contain symbolic links.")
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
