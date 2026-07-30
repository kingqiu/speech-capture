"""Durable SQLite job, event, and checkpoint storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
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
)

SCHEMA_VERSION = 1


class JobStore:
    """Single-process facade over a durable SQLite Worker database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        parent_existed = self.database_path.parent.exists()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            try:
                self.database_path.parent.chmod(0o700)
            except OSError:
                pass

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
