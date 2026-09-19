import sqlite3
import stat
import threading

import pytest

from speech_capture_worker.domain import JobCreateRequest, JobState, ModelProfile
from speech_capture_worker.errors import (
    IdempotencyConflict,
    InvalidJobRequest,
    InvalidTransition,
    PublicationLeaseConflict,
    RevisionConflict,
)
from speech_capture_worker.job_store import JobStore


def request(*, checksum_character: str = "a", source_name: str = "meeting.m4a"):
    return JobCreateRequest(
        vault_id="vault_primary",
        source_display_name=source_name,
        source_sha256=checksum_character * 64,
        source_size_bytes=1024,
        model_profile=ModelProfile.ACCURACY,
        options={"timestamps": True},
    )


def advance(store: JobStore, job_id: str, states: list[JobState]) -> None:
    for state in states:
        current = store.get_job(job_id)
        store.transition_job(job_id, state, expected_revision=current.revision)


def test_create_job_records_initial_event(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, created = store.create_job(request(), idempotency_key="submit-001")
        events = store.list_events(job.job_id)

    assert created is True
    assert job.state is JobState.CREATED
    assert job.revision == 0
    assert len(events) == 1
    assert events[0].event_type == "job.created"
    assert events[0].from_state is None
    assert events[0].to_state is JobState.CREATED


def test_job_actions_are_revision_guarded_and_idempotent_across_restart(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as store:
        job, _ = store.create_job(request(), idempotency_key="submit-action")
        advance(
            store,
            job.job_id,
            [JobState.UPLOADING, JobState.VERIFYING, JobState.QUEUED],
        )
        queued = store.get_job(job.job_id)
        paused, applied = store.apply_job_action(
            job.job_id,
            action="pause",
            expected_revision=queued.revision,
            idempotency_key="action-pause",
        )
        replayed, replay_applied = store.apply_job_action(
            job.job_id,
            action="pause",
            expected_revision=queued.revision,
            idempotency_key="action-pause",
        )

        assert applied is True
        assert replay_applied is False
        assert replayed == paused
        assert paused.state is JobState.PAUSED
        assert [event.event_type for event in store.list_events(job.job_id)].count(
            "job.paused"
        ) == 1

        resumed, resumed_applied = store.apply_job_action(
            job.job_id,
            action="resume",
            expected_revision=paused.revision,
            idempotency_key="action-resume",
        )
        assert resumed_applied is True
        assert resumed.state is JobState.QUEUED

        with pytest.raises(IdempotencyConflict):
            store.apply_job_action(
                job.job_id,
                action="cancel",
                expected_revision=resumed.revision,
                idempotency_key="action-pause",
            )

    with JobStore(database) as restarted:
        replayed_after_restart, applied_after_restart = restarted.apply_job_action(
            job.job_id,
            action="pause",
            expected_revision=queued.revision,
            idempotency_key="action-pause",
        )

    assert applied_after_restart is False
    assert replayed_after_restart == paused


def test_retry_action_requires_a_retryable_terminal_state(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-retry")
        advance(
            store,
            job.job_id,
            [JobState.UPLOADING, JobState.VERIFYING, JobState.QUEUED],
        )
        queued = store.get_job(job.job_id)
        with pytest.raises(InvalidJobRequest, match="failed, partial, or waiting"):
            store.apply_job_action(
                job.job_id,
                action="retry",
                expected_revision=queued.revision,
                idempotency_key="retry-too-early",
            )
        failed = store.transition_job(
            job.job_id,
            JobState.FAILED,
            expected_revision=queued.revision,
            error_code="TEST_FAILURE",
            error_message="A safe test failure.",
        )
        retried, applied = store.apply_job_action(
            job.job_id,
            action="retry",
            expected_revision=failed.revision,
            idempotency_key="retry-after-failure",
        )

    assert applied is True
    assert retried.state is JobState.QUEUED


def test_identical_idempotent_request_reuses_job(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        first, first_created = store.create_job(request(), idempotency_key="submit-001")
        second, second_created = store.create_job(request(), idempotency_key="submit-001")

        assert len(store.list_events(first.job_id)) == 1

    assert first_created is True
    assert second_created is False
    assert second.job_id == first.job_id


def test_recording_context_is_revision_guarded_and_event_payload_is_safe(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-context")
        updated, changed = store.update_job_recording_context(
            job.job_id,
            context="客户公司是聚衣堂。\n这是多人会议。",
            expected_revision=job.revision,
        )
        repeated, repeated_changed = store.update_job_recording_context(
            job.job_id,
            context="客户公司是聚衣堂。\n这是多人会议。",
            expected_revision=updated.revision,
        )
        events = store.list_events(job.job_id)

        assert changed is True
        assert updated.revision == 1
        assert updated.options["recording_context"] == "客户公司是聚衣堂。\n这是多人会议。"
        assert repeated_changed is False
        assert repeated.revision == updated.revision
        assert events[-1].event_type == "job.recording_context_updated"
        assert events[-1].payload["context_supplied"] is True
        assert len(events[-1].payload["context_sha256"]) == 64
        assert "聚衣堂" not in str(events[-1].payload)

        with pytest.raises(RevisionConflict):
            store.update_job_recording_context(
                job.job_id,
                context=None,
                expected_revision=job.revision,
            )

        cleared, cleared_changed = store.update_job_recording_context(
            job.job_id,
            context=None,
            expected_revision=updated.revision,
        )
        assert cleared_changed is True
        assert "recording_context" not in cleared.options


def test_content_type_override_is_revision_guarded_and_evented(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-content-type")
        updated, changed = store.update_job_content_type_override(
            job.job_id,
            content_type="speech",
            expected_revision=job.revision,
        )
        repeated, repeated_changed = store.update_job_content_type_override(
            job.job_id,
            content_type="speech",
            expected_revision=updated.revision,
        )
        events = store.list_events(job.job_id)

        assert changed is True
        assert updated.revision == 1
        assert updated.content_type_override == "speech"
        assert repeated_changed is False
        assert repeated.revision == updated.revision
        assert events[-1].event_type == "job.content_type_override_updated"
        assert events[-1].payload == {"content_type_override": "speech"}

        with pytest.raises(RevisionConflict):
            store.update_job_content_type_override(
                job.job_id,
                content_type=None,
                expected_revision=job.revision,
            )
        with pytest.raises(InvalidJobRequest):
            store.update_job_content_type_override(
                job.job_id,
                content_type="podcast",
                expected_revision=updated.revision,
            )

        cleared, cleared_changed = store.update_job_content_type_override(
            job.job_id,
            content_type=None,
            expected_revision=updated.revision,
        )
        assert cleared_changed is True
        assert cleared.content_type_override is None


def test_idempotency_key_cannot_change_request(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        store.create_job(request(), idempotency_key="submit-001")

        with pytest.raises(IdempotencyConflict):
            store.create_job(
                request(checksum_character="b", source_name="different.m4a"),
                idempotency_key="submit-001",
            )


def test_transition_is_revision_guarded_and_evented(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-001")
        uploading = store.transition_job(
            job.job_id,
            JobState.UPLOADING,
            expected_revision=0,
            reason_code="upload_started",
        )

        with pytest.raises(RevisionConflict):
            store.transition_job(
                job.job_id,
                JobState.VERIFYING,
                expected_revision=0,
            )

        events = store.list_events(job.job_id)

    assert uploading.revision == 1
    assert uploading.state is JobState.UPLOADING
    assert [event.revision for event in events] == [0, 1]
    assert events[-1].reason_code == "upload_started"


def test_event_cursor_returns_only_newer_events(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-001")
        store.transition_job(job.job_id, JobState.UPLOADING, expected_revision=0)
        all_events = store.list_events(job.job_id)
        newer = store.list_events(job.job_id, after_sequence=all_events[0].sequence)

    assert len(newer) == 1
    assert newer[0].to_state is JobState.UPLOADING


def test_invalid_transition_is_not_written(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-001")

        with pytest.raises(InvalidTransition):
            store.transition_job(
                job.job_id,
                JobState.PROCESSED,
                expected_revision=0,
            )

        assert store.get_job(job.job_id).revision == 0
        assert len(store.list_events(job.job_id)) == 1


def test_failed_transition_requires_and_preserves_safe_error(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-001")
        store.transition_job(job.job_id, JobState.UPLOADING, expected_revision=0)

        with pytest.raises(InvalidJobRequest):
            store.transition_job(
                job.job_id,
                JobState.FAILED,
                expected_revision=1,
            )

        failed = store.transition_job(
            job.job_id,
            JobState.FAILED,
            expected_revision=1,
            error_code="UPLOAD_CHECKSUM_MISMATCH",
            error_message="The assembled source checksum did not match.",
        )
        event = store.list_events(job.job_id)[-1]

    assert failed.last_error_code == "UPLOAD_CHECKSUM_MISMATCH"
    assert event.payload == {
        "error_code": "UPLOAD_CHECKSUM_MISMATCH",
        "error_message": "The assembled source checksum did not match.",
    }


def test_job_and_events_survive_reopen(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as store:
        job, _ = store.create_job(request(), idempotency_key="submit-001")
        store.transition_job(job.job_id, JobState.UPLOADING, expected_revision=0)

    with JobStore(database) as reopened:
        restored = reopened.get_job(job.job_id)
        events = reopened.list_events(job.job_id)

    assert restored.state is JobState.UPLOADING
    assert restored.revision == 1
    assert len(events) == 2


def test_checkpoint_is_idempotent_and_revisioned(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as store:
        job, _ = store.create_job(request(), idempotency_key="submit-001")
        first, first_created = store.put_checkpoint(
            job.job_id,
            stage="transcribing",
            checkpoint_key="chunk_000001",
            payload={"start_ms": 0, "end_ms": 30000, "text": "private transcript"},
        )
        repeated, repeated_created = store.put_checkpoint(
            job.job_id,
            stage="transcribing",
            checkpoint_key="chunk_000001",
            payload={"start_ms": 0, "end_ms": 30000, "text": "private transcript"},
        )
        revised, revised_created = store.put_checkpoint(
            job.job_id,
            stage="transcribing",
            checkpoint_key="chunk_000001",
            payload={"start_ms": 0, "end_ms": 30000, "text": "corrected transcript"},
        )

    with JobStore(database) as reopened:
        restored = reopened.list_checkpoints(job.job_id)

    assert first_created is True
    assert repeated_created is False
    assert revised_created is False
    assert first.generation == repeated.generation == 1
    assert revised.generation == 2
    assert restored[0].payload["text"] == "corrected transcript"
    assert restored[0].payload_sha256 != first.payload_sha256


def test_recovery_requeues_active_work_and_preserves_checkpoint(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as store:
        job, _ = store.create_job(request(), idempotency_key="submit-active")
        advance(
            store,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
            ],
        )
        store.put_checkpoint(
            job.job_id,
            stage="transcribing",
            checkpoint_key="chunk_000001",
            payload={"complete": True},
        )

    with JobStore(database) as restarted:
        recovered = restarted.recover_interrupted_jobs()
        checkpoint = restarted.list_checkpoints(job.job_id)[0]
        events = restarted.list_events(job.job_id)

    assert len(recovered) == 1
    assert recovered[0].state is JobState.QUEUED
    assert checkpoint.payload == {"complete": True}
    assert events[-1].event_type == "job.recovered"
    assert events[-1].reason_code == "worker_restart"


def test_recovery_returns_interrupted_verification_to_upload_boundary(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-verifying")
        advance(
            store,
            job.job_id,
            [JobState.UPLOADING, JobState.VERIFYING],
        )

        recovered = store.recover_interrupted_jobs()

    assert recovered[0].state is JobState.UPLOADING
    assert recovered[0].revision == 3


def test_recovery_returns_publication_to_processed(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="submit-publishing")
        advance(
            store,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        processed = store.get_job(job.job_id)
        store.claim_publication(
            job.job_id,
            publisher_id="local_vault",
            target_relative_path="Speech/Undated/example",
            manifest_sha256="a" * 64,
            expected_revision=processed.revision,
        )

        recovered = store.recover_interrupted_jobs()
        leases = store.list_publication_leases(job.job_id)

    assert recovered[0].state is JobState.PROCESSED
    assert leases[-1].state.value == "recovered"


def test_generic_transition_cannot_bypass_publication_protocol(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="publication-bypass")
        advance(
            store,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        processed = store.get_job(job.job_id)

        with pytest.raises(InvalidJobRequest, match="publication lease protocol"):
            store.transition_job(
                job.job_id,
                JobState.PUBLISHING,
                expected_revision=processed.revision,
            )

        assert store.get_job(job.job_id) == processed


def test_publication_lease_is_exclusive_renewable_and_releasable(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="publication-lease")
        advance(
            store,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        processed = store.get_job(job.job_id)
        lease, publishing, created = store.claim_publication(
            job.job_id,
            publisher_id="device_a",
            target_relative_path="Speech/Undated/test--sp_123",
            manifest_sha256="b" * 64,
            expected_revision=processed.revision,
        )
        replayed, replayed_job, replayed_created = store.claim_publication(
            job.job_id,
            publisher_id="device_a",
            target_relative_path="Speech/Undated/test--sp_123",
            manifest_sha256="b" * 64,
            expected_revision=processed.revision,
        )

        assert created is True
        assert publishing.state is JobState.PUBLISHING
        assert replayed_created is False
        assert replayed.lease_id == lease.lease_id
        assert replayed_job.revision == publishing.revision
        with pytest.raises(PublicationLeaseConflict):
            store.claim_publication(
                job.job_id,
                publisher_id="device_b",
                target_relative_path="Speech/Undated/test--sp_123",
                manifest_sha256="b" * 64,
                expected_revision=publishing.revision,
            )

        renewed = store.renew_publication_lease(
            job.job_id,
            lease_id=lease.lease_id,
            publisher_id="device_a",
            lease_seconds=300,
        )
        released = store.release_publication_lease(
            job.job_id,
            lease_id=lease.lease_id,
            publisher_id="device_a",
        )
        leases = store.list_publication_leases(job.job_id)

    assert renewed.expires_at > lease.expires_at
    assert released.state is JobState.PROCESSED
    assert leases[0].state.value == "released"


def test_expired_publication_lease_can_be_taken_over(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as store:
        job, _ = store.create_job(request(), idempotency_key="publication-takeover")
        advance(
            store,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        processed = store.get_job(job.job_id)
        first, publishing, _ = store.claim_publication(
            job.job_id,
            publisher_id="device_a",
            target_relative_path="Speech/Undated/test--sp_123",
            manifest_sha256="c" * 64,
            expected_revision=processed.revision,
        )
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE publication_leases SET expires_at = ? WHERE lease_id = ?",
            ("2000-01-01T00:00:00+00:00", first.lease_id),
        )
        connection.commit()
        connection.close()

        second, taken_over, created = store.claim_publication(
            job.job_id,
            publisher_id="device_b",
            target_relative_path="Speech/Undated/test--sp_123",
            manifest_sha256="c" * 64,
            expected_revision=publishing.revision,
        )
        leases = store.list_publication_leases(job.job_id)

    assert created is True
    assert second.generation == 2
    assert taken_over.state is JobState.PUBLISHING
    assert taken_over.revision == publishing.revision + 2
    assert [lease.state.value for lease in leases] == ["expired", "active"]


def test_publication_acknowledgement_is_idempotent_and_restart_recovers_lease(
    tmp_path,
) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="publication-ack")
        advance(
            store,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        processed = store.get_job(job.job_id)
        lease, _, _ = store.claim_publication(
            job.job_id,
            publisher_id="device_a",
            target_relative_path="Speech/Undated/test--sp_123",
            manifest_sha256="d" * 64,
            expected_revision=processed.revision,
        )
        receipt, published, created = store.acknowledge_publication(
            job.job_id,
            lease_id=lease.lease_id,
            publisher_id="device_a",
            manifest_sha256="d" * 64,
        )
        replayed, replayed_job, replayed_created = store.acknowledge_publication(
            job.job_id,
            lease_id=lease.lease_id,
            publisher_id="device_a",
            manifest_sha256="d" * 64,
        )

        assert created is True
        assert published.state is JobState.PUBLISHED
        assert replayed_created is False
        assert replayed == receipt
        assert replayed_job.state is JobState.PUBLISHED

        second_job, _ = store.create_job(
            request(source_name="second.m4a"),
            idempotency_key="publication-recover-lease",
        )
        advance(
            store,
            second_job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        second_processed = store.get_job(second_job.job_id)
        store.claim_publication(
            second_job.job_id,
            publisher_id="device_b",
            target_relative_path="Speech/Undated/second--sp_456",
            manifest_sha256="e" * 64,
            expected_revision=second_processed.revision,
        )
        recovered = store.recover_interrupted_jobs()
        recovered_lease = store.list_publication_leases(second_job.job_id)[0]

    assert [item.job_id for item in recovered] == [second_job.job_id]
    assert recovered[0].state is JobState.PROCESSED
    assert recovered_lease.state.value == "recovered"


def test_concurrent_publishers_create_only_one_active_lease(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as setup:
        job, _ = setup.create_job(request(), idempotency_key="publication-concurrent")
        advance(
            setup,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        processed = setup.get_job(job.job_id)

    first_store = JobStore(database)
    second_store = JobStore(database)
    barrier = threading.Barrier(2)
    claimed = []
    errors = []

    def claim(store, publisher_id):
        try:
            barrier.wait(timeout=5)
            lease, _, _ = store.claim_publication(
                job.job_id,
                publisher_id=publisher_id,
                target_relative_path="Speech/Undated/test--sp_123",
                manifest_sha256="f" * 64,
                expected_revision=processed.revision,
            )
            claimed.append(lease)
        except Exception as exc:  # noqa: BLE001 - exercise cross-connection race outcome
            errors.append(exc)

    threads = [
        threading.Thread(target=claim, args=(first_store, "device_a")),
        threading.Thread(target=claim, args=(second_store, "device_b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    with JobStore(database) as inspected:
        leases = inspected.list_publication_leases(job.job_id)
    first_store.close()
    second_store.close()

    assert len(claimed) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], PublicationLeaseConflict)
    assert [lease.state.value for lease in leases] == ["active"]


def test_concurrent_identical_creation_produces_one_job(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    first_store = JobStore(database)
    second_store = JobStore(database)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def create(store):
        try:
            barrier.wait(timeout=5)
            results.append(store.create_job(request(), idempotency_key="submit-race"))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=create, args=(first_store,))
    second = threading.Thread(target=create, args=(second_store,))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    first_store.close()
    second_store.close()

    assert errors == []
    assert len(results) == 2
    assert sum(created for _, created in results) == 1
    assert len({job.job_id for job, _ in results}) == 1


def test_stale_publication_receipt_is_archived_but_matching_receipt_is_kept(
    tmp_path,
) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        job, _ = store.create_job(request(), idempotency_key="publication-stale-receipt")
        advance(
            store,
            job.job_id,
            [
                JobState.UPLOADING,
                JobState.VERIFYING,
                JobState.QUEUED,
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
                JobState.PROCESSED,
            ],
        )
        processed = store.get_job(job.job_id)
        lease, _, _ = store.claim_publication(
            job.job_id,
            publisher_id="device_a",
            target_relative_path="Speech/Undated/test--sp_123",
            manifest_sha256="a" * 64,
            expected_revision=processed.revision,
        )
        receipt, _, _ = store.acknowledge_publication(
            job.job_id,
            lease_id=lease.lease_id,
            publisher_id="device_a",
            manifest_sha256="a" * 64,
        )

        matching = store.archive_stale_publication_receipt(
            job.job_id,
            current_manifest_sha256="a" * 64,
        )
        archived = store.archive_stale_publication_receipt(
            job.job_id,
            current_manifest_sha256="b" * 64,
        )
        current = store.get_publication_receipt(job.job_id)
        history = store._connection.execute(  # noqa: SLF001 - persistence boundary assertion
            "SELECT manifest_sha256, reason_code FROM publication_receipt_history "
            "WHERE job_id = ?",
            (job.job_id,),
        ).fetchall()

    assert matching is None
    assert archived == receipt
    assert current is None
    assert [(row["manifest_sha256"], row["reason_code"]) for row in history] == [
        ("a" * 64, "artifact_manifest_replaced")
    ]


def test_database_permissions_and_integrity(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as store:
        assert store.quick_check() is True

    mode = stat.S_IMODE(database.stat().st_mode)
    assert mode & 0o077 == 0


def test_newer_database_schema_is_refused(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database):
        pass
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 999")
    connection.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        JobStore(database)
