"""One-active-job scheduling boundary for heavy local model work."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from speech_capture_worker.domain import JobRecord, JobState, ResourceStatus
from speech_capture_worker.errors import (
    RevisionConflict,
    SchedulerBusy,
    UploadStorageError,
    VerifiedUploadRequired,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import (
    ResourceReport,
    check_resource_preflight,
    estimate_job_disk_bytes,
)

ResourcePreflight = Callable[..., ResourceReport]


class SchedulerOutcome(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    BLOCKED = "blocked"
    CLAIMED = "claimed"


@dataclass(frozen=True)
class SchedulerResult:
    outcome: SchedulerOutcome
    job: JobRecord | None
    resource_report: ResourceReport | None
    active_job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict() if self.job is not None else None,
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
            "active_job_id": self.active_job_id,
        }


class JobScheduler:
    """Select, preflight, and atomically claim at most one queued job."""

    def __init__(
        self,
        store: JobStore,
        *,
        storage_path: Path | None = None,
        resource_preflight: ResourcePreflight = check_resource_preflight,
    ) -> None:
        self.store = store
        self.storage_path = (
            storage_path.resolve() if storage_path is not None else store.data_directory
        )
        self._resource_preflight = resource_preflight

    def run_once(self) -> SchedulerResult:
        active = self.store.get_active_processing_job()
        if active is not None:
            return SchedulerResult(
                outcome=SchedulerOutcome.BUSY,
                job=None,
                resource_report=None,
                active_job_id=active.job_id,
            )

        queued = self.store.get_next_schedulable_job()
        if queued is None:
            return SchedulerResult(
                outcome=SchedulerOutcome.IDLE,
                job=None,
                resource_report=None,
            )

        assert queued.source_upload_id is not None
        upload = self.store.get_upload(queued.source_upload_id)
        assert upload.duration_seconds is not None
        estimated_bytes = estimate_job_disk_bytes(
            source_size_bytes=upload.source_size_bytes,
            duration_sec=upload.duration_seconds,
            include_source_staging=False,
        )
        report = self._resource_preflight(
            self.storage_path,
            estimated_required_bytes=estimated_bytes,
            model_profile=queued.model_profile,
        )
        self.store.put_checkpoint(
            queued.job_id,
            stage="scheduler",
            checkpoint_key="resource_preflight",
            payload=report.to_dict(),
        )

        if report.status is ResourceStatus.BLOCKED:
            try:
                paused = self.store.transition_job(
                    queued.job_id,
                    JobState.PAUSED,
                    expected_revision=queued.revision,
                    reason_code="resource_preflight_blocked",
                    error_code="RESOURCE_PREFLIGHT_BLOCKED",
                    error_message=(
                        "Worker resources must be resolved before model work can start."
                    ),
                    event_type="resource.preflight_blocked",
                )
            except RevisionConflict:
                active = self.store.get_active_processing_job()
                return SchedulerResult(
                    outcome=SchedulerOutcome.BUSY,
                    job=None,
                    resource_report=report,
                    active_job_id=active.job_id if active is not None else None,
                )
            return SchedulerResult(
                outcome=SchedulerOutcome.BLOCKED,
                job=paused,
                resource_report=report,
            )

        try:
            claimed = self.store.claim_job_for_processing(
                queued.job_id,
                expected_revision=queued.revision,
            )
        except (UploadStorageError, VerifiedUploadRequired) as exc:
            failed = self.store.transition_job(
                queued.job_id,
                JobState.FAILED,
                expected_revision=queued.revision,
                reason_code="verified_source_unavailable",
                error_code=exc.code,
                error_message=exc.message,
                event_type="job.source_unavailable",
            )
            return SchedulerResult(
                outcome=SchedulerOutcome.BLOCKED,
                job=failed,
                resource_report=report,
            )
        except (RevisionConflict, SchedulerBusy):
            active = self.store.get_active_processing_job()
            return SchedulerResult(
                outcome=SchedulerOutcome.BUSY,
                job=None,
                resource_report=report,
                active_job_id=active.job_id if active is not None else None,
            )
        return SchedulerResult(
            outcome=SchedulerOutcome.CLAIMED,
            job=claimed,
            resource_report=report,
        )
