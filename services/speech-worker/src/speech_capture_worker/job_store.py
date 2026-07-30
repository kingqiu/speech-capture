"""Durable SQLite job, event, and checkpoint storage."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from speech_capture_worker.domain import (
    RECOVERY_TARGETS,
    SAFE_IDENTIFIER_PATTERN,
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
    validate_idempotency_key,
    validate_reason_code,
    validate_safe_message,
)
from speech_capture_worker.errors import (
    IdempotencyConflict,
    InvalidJobRequest,
    JobNotFound,
    RevisionConflict,
    UploadChecksumMismatch,
    UploadIncomplete,
    UploadNotFound,
    UploadPartChecksumMismatch,
    UploadPartConflict,
    UploadStateConflict,
    UploadStorageError,
    WorkerCoreError,
)
from speech_capture_worker.media_probe import MediaProbeResult, probe_audio_source

SCHEMA_VERSION = 2
DEFAULT_UPLOAD_CHUNK_SIZE_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_CHUNK_SIZE_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_PARTS = 10_000
COPY_BUFFER_BYTES = 1024 * 1024


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

            job_id = f"job_{uuid4().hex}"
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    vault_id,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)
                """,
                (
                    job_id,
                    request.vault_id,
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
                payload={"model_profile": request.model_profile.value},
                created_at=now,
            )
            row = self._fetch_job_row(job_id)
            return self._row_to_job(row), True

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._row_to_job(self._fetch_job_row(job_id))

    def list_jobs(
        self,
        *,
        states: Sequence[JobState] | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        if limit < 1 or limit > 1000:
            raise InvalidJobRequest("limit must be between 1 and 1000.")
        with self._lock:
            if states:
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
            ensure_transition_allowed(current.state, target_state)
            return self._transition_in_transaction(
                current=current,
                target_state=target_state,
                reason_code=reason_code,
                error_code=error_code,
                error_message=error_message,
                event_type=event_type,
            )

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

    def _fetch_job_row(self, job_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFound("The requested job does not exist.", details={"job_id": job_id})
        return row

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

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=str(row["job_id"]),
            vault_id=str(row["vault_id"]),
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


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError("Worker database JSON payload is not an object.")
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_checkpoint_identifier(name: str, value: str) -> None:
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidJobRequest(f"{name} contains unsupported characters.")


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
