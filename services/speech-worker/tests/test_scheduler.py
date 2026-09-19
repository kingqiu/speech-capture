import hashlib
import threading

import pytest

from speech_capture_worker.domain import (
    JobCreateRequest,
    JobState,
    ModelProfile,
    ResourceStatus,
    UploadCreateRequest,
)
from speech_capture_worker.errors import (
    InvalidJobRequest,
    SchedulerBusy,
    VerifiedUploadRequired,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceIssue,
    ResourceReport,
)
from speech_capture_worker.scheduler import JobScheduler, SchedulerOutcome


def source_probe(_):
    return MediaProbeResult(
        duration_seconds=60.0,
        audio_stream_count=1,
        format_name="wav",
    )


def create_upload(
    store: JobStore,
    *,
    suffix: str,
    complete: bool = True,
):
    content = f"synthetic-audio-{suffix}".encode()
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"meeting-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"upload-{suffix}",
    )
    if complete:
        store.put_upload_part(
            upload.upload_id,
            part_number=1,
            content=content,
            part_sha256=checksum,
        )
        upload, completed = store.complete_upload(upload.upload_id)
        assert completed is True
    return upload


def create_queued_job(
    store: JobStore,
    *,
    suffix: str,
    model_profile: ModelProfile = ModelProfile.ACCURACY,
):
    upload = create_upload(store, suffix=suffix)
    job, created = store.create_job_from_upload(
        upload.upload_id,
        idempotency_key=f"job-{suffix}",
        model_profile=model_profile,
    )
    assert created is True
    return job


def resource_report(status: ResourceStatus) -> ResourceReport:
    issues = ()
    if status is ResourceStatus.WARNING:
        issues = (
            ResourceIssue(
                code="MEMORY_PRESSURE_WARNING",
                status=ResourceStatus.WARNING,
                message="Memory pressure may make processing slower.",
                action="Close other large applications if practical.",
            ),
        )
    if status is ResourceStatus.BLOCKED:
        issues = (
            ResourceIssue(
                code="DISK_RESERVE_TOO_LOW",
                status=ResourceStatus.BLOCKED,
                message="The disk reserve would be crossed.",
                action="Free disk space manually, then retry.",
            ),
        )
    return ResourceReport(
        status=status,
        estimated_required_bytes=2 * GIB,
        disk_reserve_bytes=20 * GIB,
        disk_free_after_bytes=40 * GIB,
        disk=DiskSnapshot(total_bytes=256 * GIB, free_bytes=80 * GIB),
        memory=MemorySnapshot(
            total_bytes=32 * GIB,
            available_bytes=20 * GIB,
            used_percent=40.0,
            swap_used_bytes=0,
        ),
        issues=issues,
    )


def preflight_returning(report: ResourceReport, *, barrier=None, calls=None):
    def check(storage_path, *, estimated_required_bytes, model_profile):
        if calls is not None:
            calls.append(
                {
                    "storage_path": storage_path,
                    "estimated_required_bytes": estimated_required_bytes,
                    "model_profile": model_profile,
                }
            )
        if barrier is not None:
            barrier.wait(timeout=5)
        return report

    return check


def advance_direct_job_to_queue(store: JobStore, job_id: str) -> None:
    for state in (JobState.UPLOADING, JobState.VERIFYING, JobState.QUEUED):
        current = store.get_job(job_id)
        store.transition_job(job_id, state, expected_revision=current.revision)


def test_incomplete_upload_cannot_create_a_queue_job(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        upload = create_upload(store, suffix="incomplete", complete=False)

        with pytest.raises(VerifiedUploadRequired) as caught:
            store.create_job_from_upload(
                upload.upload_id,
                idempotency_key="job-incomplete",
            )

        jobs = store.list_jobs()

    assert caught.value.code == "SOURCE_UPLOAD_NOT_VERIFIED"
    assert jobs == []


def test_verified_upload_creates_one_auditable_queued_job(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        upload = create_upload(store, suffix="verified")
        job, created = store.create_job_from_upload(
            upload.upload_id,
            idempotency_key="job-verified",
        )
        repeated, repeated_created = store.create_job_from_upload(
            upload.upload_id,
            idempotency_key="job-verified",
        )
        events = store.list_events(job.job_id)
        source_path = store.get_job_verified_source_path(job.job_id)

    assert created is True
    assert repeated_created is False
    assert repeated.job_id == job.job_id
    assert job.source_upload_id == upload.upload_id
    assert job.state is JobState.QUEUED
    assert job.revision == 3
    assert [event.to_state for event in events] == [
        JobState.CREATED,
        JobState.UPLOADING,
        JobState.VERIFYING,
        JobState.QUEUED,
    ]
    assert [event.event_type for event in events] == [
        "job.created",
        "job.source_attached",
        "job.source_verified",
        "job.queued",
    ]
    assert source_path.name == f"{upload.upload_id}.source"


def test_job_metadata_cannot_disagree_with_bound_upload(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        upload = create_upload(store, suffix="metadata")
        request = JobCreateRequest(
            vault_id=upload.vault_id,
            source_upload_id=upload.upload_id,
            source_display_name=upload.source_display_name,
            source_sha256="0" * 64,
            source_size_bytes=upload.source_size_bytes,
        )

        with pytest.raises(InvalidJobRequest) as caught:
            store.create_job(request, idempotency_key="job-metadata")

    assert caught.value.details["mismatched_fields"] == ["source_sha256"]


def test_direct_developer_job_is_not_schedulable(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        direct, _ = store.create_job(
            JobCreateRequest(
                vault_id="vault_primary",
                source_display_name="unverified.wav",
                source_sha256="a" * 64,
                source_size_bytes=1024,
            ),
            idempotency_key="direct-job",
        )
        advance_direct_job_to_queue(store, direct.job_id)
        result = JobScheduler(
            store,
            resource_preflight=preflight_returning(resource_report(ResourceStatus.READY)),
        ).run_once()

        with pytest.raises(VerifiedUploadRequired):
            store.claim_job_for_processing(
                direct.job_id,
                expected_revision=store.get_job(direct.job_id).revision,
            )

    assert result.outcome is SchedulerOutcome.IDLE


def test_scheduler_claims_oldest_verified_job_and_then_reports_busy(tmp_path) -> None:
    calls = []
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        create_queued_job(store, suffix="first")
        create_queued_job(store, suffix="second")
        expected_first = store.list_jobs(states=[JobState.QUEUED])[0]
        scheduler = JobScheduler(
            store,
            resource_preflight=preflight_returning(
                resource_report(ResourceStatus.READY),
                calls=calls,
            ),
        )

        claimed = scheduler.run_once()
        busy = scheduler.run_once()
        checkpoint = store.list_checkpoints(
            expected_first.job_id,
            stage="scheduler",
        )[0]

    assert claimed.outcome is SchedulerOutcome.CLAIMED
    assert claimed.job is not None
    assert claimed.job.job_id == expected_first.job_id
    assert claimed.job.state is JobState.PREPROCESSING
    assert busy.outcome is SchedulerOutcome.BUSY
    assert busy.active_job_id == expected_first.job_id
    assert checkpoint.payload["status"] == "ready"
    assert calls[0]["model_profile"] is ModelProfile.ACCURACY
    assert calls[0]["estimated_required_bytes"] > 3 * GIB


def test_blocked_preflight_safely_pauses_job_with_full_evidence(tmp_path) -> None:
    blocked_report = resource_report(ResourceStatus.BLOCKED)
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        queued = create_queued_job(store, suffix="blocked")
        result = JobScheduler(
            store,
            resource_preflight=preflight_returning(blocked_report),
        ).run_once()
        checkpoint = store.list_checkpoints(
            queued.job_id,
            stage="scheduler",
        )[0]

    assert result.outcome is SchedulerOutcome.BLOCKED
    assert result.job is not None
    assert result.job.state is JobState.PAUSED
    assert result.job.last_error_code == "RESOURCE_PREFLIGHT_BLOCKED"
    assert checkpoint.payload["issues"][0]["code"] == "DISK_RESERVE_TOO_LOW"
    assert checkpoint.payload["issues"][0]["action"]


def test_warning_preflight_remains_visible_and_allows_claim(tmp_path) -> None:
    warning_report = resource_report(ResourceStatus.WARNING)
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        queued = create_queued_job(store, suffix="warning")
        result = JobScheduler(
            store,
            resource_preflight=preflight_returning(warning_report),
        ).run_once()
        checkpoint = store.list_checkpoints(
            queued.job_id,
            stage="scheduler",
        )[0]
        snapshot = store.get_job_snapshot(queued.job_id)

    assert result.outcome is SchedulerOutcome.CLAIMED
    assert result.job is not None
    assert result.job.state is JobState.PREPROCESSING
    assert result.resource_report is not None
    assert result.resource_report.status is ResourceStatus.WARNING
    assert checkpoint.payload["status"] == "warning"
    assert snapshot.resource_report is not None
    assert snapshot.resource_report["issues"][0]["code"] == "MEMORY_PRESSURE_WARNING"


def test_missing_verified_source_becomes_a_durable_failed_job(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        queued = create_queued_job(store, suffix="missing-source")
        source_path = store.get_job_verified_source_path(queued.job_id)
        source_path.unlink()
        result = JobScheduler(
            store,
            resource_preflight=preflight_returning(resource_report(ResourceStatus.READY)),
        ).run_once()
        event = store.list_events(queued.job_id)[-1]

    assert result.outcome is SchedulerOutcome.BLOCKED
    assert result.job is not None
    assert result.job.state is JobState.FAILED
    assert result.job.last_error_code == "UPLOAD_STORAGE_ERROR"
    assert event.event_type == "job.source_unavailable"


def test_two_scheduler_connections_can_claim_only_one_job(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    bootstrap = JobStore(database, source_probe=source_probe)
    queued = create_queued_job(bootstrap, suffix="race")
    bootstrap.close()

    first_store = JobStore(database, source_probe=source_probe)
    second_store = JobStore(database, source_probe=source_probe)
    barrier = threading.Barrier(2)
    ready_report = resource_report(ResourceStatus.READY)
    results = []
    errors = []

    def schedule(store):
        try:
            scheduler = JobScheduler(
                store,
                resource_preflight=preflight_returning(
                    ready_report,
                    barrier=barrier,
                ),
            )
            results.append(scheduler.run_once())
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=schedule, args=(first_store,))
    second = threading.Thread(target=schedule, args=(second_store,))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    final = first_store.get_job(queued.job_id)
    events = first_store.list_events(queued.job_id)
    first_store.close()
    second_store.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert {result.outcome for result in results} == {
        SchedulerOutcome.CLAIMED,
        SchedulerOutcome.BUSY,
    }
    assert final.state is JobState.PREPROCESSING
    assert sum(event.event_type == "job.processing_claimed" for event in events) == 1


def test_manual_transition_cannot_create_a_second_active_job(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        first = create_queued_job(store, suffix="active-first")
        second = create_queued_job(store, suffix="active-second")
        store.claim_job_for_processing(first.job_id, expected_revision=first.revision)

        with pytest.raises(SchedulerBusy) as caught:
            store.transition_job(
                second.job_id,
                JobState.PREPROCESSING,
                expected_revision=second.revision,
            )

    assert caught.value.details["active_job_id"] == first.job_id


def test_restart_requeues_claimed_job_and_scheduler_can_reclaim_it(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    ready = preflight_returning(resource_report(ResourceStatus.READY))
    with JobStore(database, source_probe=source_probe) as store:
        queued = create_queued_job(store, suffix="restart")
        first_claim = JobScheduler(store, resource_preflight=ready).run_once()
        assert first_claim.outcome is SchedulerOutcome.CLAIMED

    with JobStore(database, source_probe=source_probe) as restarted:
        recovered = restarted.recover_interrupted_jobs()
        second_claim = JobScheduler(
            restarted,
            resource_preflight=ready,
        ).run_once()
        checkpoints = restarted.list_checkpoints(
            queued.job_id,
            stage="scheduler",
        )

    assert recovered[0].state is JobState.QUEUED
    assert second_claim.outcome is SchedulerOutcome.CLAIMED
    assert second_claim.job is not None
    assert second_claim.job.state is JobState.PREPROCESSING
    assert len(checkpoints) == 1
