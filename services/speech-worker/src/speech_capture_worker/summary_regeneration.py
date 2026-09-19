"""Durable, observable background requests for Note re-synthesis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from speech_capture_worker.domain import JobRecord, JobState, validate_idempotency_key
from speech_capture_worker.errors import InvalidJobRequest, RevisionConflict
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.summary_revisions import list_summary_revisions

SUMMARY_REGENERATION_STAGE = "summary_regeneration"
SUMMARY_REGENERATION_CHECKPOINT_KEY = "current_request"
SUMMARY_REGENERATION_SCHEMA_VERSION = "1.0.0"


class SummaryRegenerationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SummaryRegenerationPhase(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    SYNTHESIZING = "synthesizing"
    QUALITY_REVIEW = "quality_review"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class SummaryRegenerationRequest:
    request_id: str
    state: SummaryRegenerationState
    phase: SummaryRegenerationPhase
    requested_at: str
    started_at: str | None
    updated_at: str
    finished_at: str | None
    elapsed_seconds: float
    revision_key: str | None
    error_code: str | None
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SummaryRegenerationEnqueueResult:
    job: JobRecord
    request: SummaryRegenerationRequest
    applied: bool


def current_summary_regeneration(
    store: JobStore,
    job_id: str,
) -> SummaryRegenerationRequest | None:
    checkpoint = _current_checkpoint(store, job_id)
    if checkpoint is None:
        return None
    return _request_from_payload(checkpoint.payload)


def enqueue_summary_regeneration(
    store: JobStore,
    job_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
) -> SummaryRegenerationEnqueueResult:
    validate_idempotency_key(idempotency_key)
    job = store.get_job(job_id)
    if job.revision != expected_revision:
        raise RevisionConflict(
            "The job changed after the transcript review was loaded.",
            details={
                "job_id": job_id,
                "expected_revision": expected_revision,
                "current_revision": job.revision,
            },
        )
    if job.state not in {JobState.PROCESSED, JobState.PUBLISHED}:
        raise InvalidJobRequest("Summary regeneration requires a processed or published job.")
    collection = list_summary_revisions(store, job_id)
    pending = next(
        (item for item in reversed(collection.revisions) if item.status.value == "pending"),
        None,
    )
    if pending is not None:
        raise InvalidJobRequest("A reviewable Note candidate already exists.")

    current_checkpoint = _current_checkpoint(store, job_id)
    current = (
        _request_from_payload(current_checkpoint.payload)
        if current_checkpoint is not None
        else None
    )
    if (
        current is not None
        and current_checkpoint is not None
        and current_checkpoint.payload.get("idempotency_key") == idempotency_key
    ):
        return SummaryRegenerationEnqueueResult(job=job, request=current, applied=False)
    if current is not None and current.state in {
        SummaryRegenerationState.QUEUED,
        SummaryRegenerationState.RUNNING,
    }:
        return SummaryRegenerationEnqueueResult(job=job, request=current, applied=False)
    if not collection.can_regenerate:
        raise InvalidJobRequest(
            "The transcript has no new corrections that require note regeneration."
        )

    now = _utc_now()
    request = SummaryRegenerationRequest(
        request_id=f"regen_{uuid4().hex}",
        state=SummaryRegenerationState.QUEUED,
        phase=SummaryRegenerationPhase.QUEUED,
        requested_at=now,
        started_at=None,
        updated_at=now,
        finished_at=None,
        elapsed_seconds=0.0,
        revision_key=None,
        error_code=None,
        error_message=None,
    )
    payload = request.to_dict()
    payload.update(
        {
            "schema_version": SUMMARY_REGENERATION_SCHEMA_VERSION,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
        }
    )
    store.put_checkpoint(
        job_id,
        stage=SUMMARY_REGENERATION_STAGE,
        checkpoint_key=SUMMARY_REGENERATION_CHECKPOINT_KEY,
        payload=payload,
    )
    return SummaryRegenerationEnqueueResult(job=job, request=request, applied=True)


class SummaryRegenerationExecutor:
    """Run at most one durable Note regeneration request per call."""

    def __init__(
        self,
        store: JobStore,
        *,
        data_dir: Path,
        regenerate: Callable[[str, Callable[[str], None]], str] | None = None,
    ) -> None:
        self.store = store
        self.data_dir = data_dir.resolve()
        self._regenerate = regenerate or self._regenerate_with_default_engine

    def run_once(self) -> bool:
        for job in self.store.list_jobs(
            states=(JobState.PROCESSED, JobState.PUBLISHED),
            limit=1000,
        ):
            request = current_summary_regeneration(self.store, job.job_id)
            if request is None or request.state not in {
                SummaryRegenerationState.QUEUED,
                SummaryRegenerationState.RUNNING,
            }:
                continue
            self._run(job, request)
            return True
        return False

    def _run(self, job: JobRecord, request: SummaryRegenerationRequest) -> None:
        checkpoint = self._checkpoint(job.job_id)
        if checkpoint is None:
            return
        payload = dict(checkpoint.payload)
        expected_revision = payload.get("expected_revision")
        if expected_revision != job.revision:
            self._finish_failed(
                job.job_id,
                payload,
                request,
                error_code="SUMMARY_REGENERATION_STALE",
                error_message="任务在候选笔记开始生成前发生了新的修订，请重新发起。",
            )
            return

        started_at = request.started_at or _utc_now()
        self._update(
            job.job_id,
            payload,
            request,
            state=SummaryRegenerationState.RUNNING,
            phase=SummaryRegenerationPhase.PREPARING,
            started_at=started_at,
        )
        try:
            def progress(phase: str) -> None:
                mapped = SummaryRegenerationPhase(phase)
                latest = current_summary_regeneration(self.store, job.job_id)
                if latest is None:
                    return
                self._update(
                    job.job_id,
                    payload,
                    latest,
                    state=SummaryRegenerationState.RUNNING,
                    phase=mapped,
                    started_at=started_at,
                )

            revision_key = self._regenerate(job.job_id, progress)
            if not revision_key:
                raise InvalidJobRequest("Note regeneration did not produce a reviewable candidate.")
            latest = current_summary_regeneration(self.store, job.job_id) or request
            self._update(
                job.job_id,
                payload,
                latest,
                state=SummaryRegenerationState.SUCCEEDED,
                phase=SummaryRegenerationPhase.COMPLETED,
                started_at=started_at,
                finished_at=_utc_now(),
                revision_key=revision_key,
            )
        except Exception as exc:
            latest = current_summary_regeneration(self.store, job.job_id) or request
            self._finish_failed(
                job.job_id,
                payload,
                latest,
                error_code=type(exc).__name__,
                error_message="候选笔记生成失败；当前已发布 Note 保持不变。",
            )

    def _regenerate_with_default_engine(
        self,
        job_id: str,
        progress: Callable[[str], None],
    ) -> str:
        from speech_capture_worker.model_activation import resolve_active_model_target
        from speech_capture_worker.structuring_execution import (
            OllamaStructuringEngine,
            StructuringExecutor,
        )

        job = self.store.get_job(job_id)
        profile = job.model_profile.value
        main_key = "ollama_accuracy" if profile == "accuracy" else "ollama_editor"
        result = StructuringExecutor(
            self.store,
            OllamaStructuringEngine.for_worker_default(
                model=resolve_active_model_target(
                    self.data_dir,
                    profile=profile,
                    key=main_key,
                    fallback="qwen3:14b" if profile == "accuracy" else "qwen3:8b",
                ),
                editor_model=resolve_active_model_target(
                    self.data_dir,
                    profile=profile,
                    key="ollama_editor",
                    fallback="qwen3:8b",
                ),
            ),
        ).resynthesize_document(job_id, progress=progress)
        return result.summary_revision_key or ""

    def _finish_failed(
        self,
        job_id: str,
        payload: dict[str, Any],
        request: SummaryRegenerationRequest,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        self._update(
            job_id,
            payload,
            request,
            state=SummaryRegenerationState.FAILED,
            phase=SummaryRegenerationPhase.FAILED,
            started_at=request.started_at,
            finished_at=_utc_now(),
            error_code=error_code,
            error_message=error_message,
        )

    def _update(
        self,
        job_id: str,
        base_payload: dict[str, Any],
        request: SummaryRegenerationRequest,
        *,
        state: SummaryRegenerationState,
        phase: SummaryRegenerationPhase,
        started_at: str | None,
        finished_at: str | None = None,
        revision_key: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = _utc_now()
        elapsed = _elapsed_seconds(started_at or request.requested_at, finished_at or now)
        updated = SummaryRegenerationRequest(
            request_id=request.request_id,
            state=state,
            phase=phase,
            requested_at=request.requested_at,
            started_at=started_at,
            updated_at=now,
            finished_at=finished_at,
            elapsed_seconds=elapsed,
            revision_key=revision_key,
            error_code=error_code,
            error_message=error_message,
        )
        payload = dict(base_payload)
        payload.update(updated.to_dict())
        self.store.put_checkpoint(
            job_id,
            stage=SUMMARY_REGENERATION_STAGE,
            checkpoint_key=SUMMARY_REGENERATION_CHECKPOINT_KEY,
            payload=payload,
        )

    def _checkpoint(self, job_id: str):
        return next(
            (
                item
                for item in self.store.list_checkpoints(
                    job_id, stage=SUMMARY_REGENERATION_STAGE
                )
                if item.checkpoint_key == SUMMARY_REGENERATION_CHECKPOINT_KEY
            ),
            None,
        )


def _request_from_payload(payload: dict[str, Any]) -> SummaryRegenerationRequest:
    return SummaryRegenerationRequest(
        request_id=str(payload["request_id"]),
        state=SummaryRegenerationState(str(payload["state"])),
        phase=SummaryRegenerationPhase(str(payload["phase"])),
        requested_at=str(payload["requested_at"]),
        started_at=(str(payload["started_at"]) if payload.get("started_at") else None),
        updated_at=str(payload["updated_at"]),
        finished_at=(str(payload["finished_at"]) if payload.get("finished_at") else None),
        elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
        revision_key=(str(payload["revision_key"]) if payload.get("revision_key") else None),
        error_code=(str(payload["error_code"]) if payload.get("error_code") else None),
        error_message=(str(payload["error_message"]) if payload.get("error_message") else None),
    )


def _current_checkpoint(store: JobStore, job_id: str):
    return next(
        (
            item
            for item in store.list_checkpoints(job_id, stage=SUMMARY_REGENERATION_STAGE)
            if item.checkpoint_key == SUMMARY_REGENERATION_CHECKPOINT_KEY
        ),
        None,
    )


def _elapsed_seconds(start: str, end: str) -> float:
    return round(
        max(
            0.0,
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(),
        ),
        3,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
