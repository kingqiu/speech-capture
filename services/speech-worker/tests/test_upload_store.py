import hashlib
import sqlite3
import stat
import threading

import pytest

from speech_capture_worker.domain import UploadCreateRequest, UploadState
from speech_capture_worker.errors import (
    IdempotencyConflict,
    InvalidJobRequest,
    SourceUndecodable,
    UploadChecksumMismatch,
    UploadIncomplete,
    UploadPartChecksumMismatch,
    UploadPartConflict,
    UploadStateConflict,
    UploadStorageError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult

SOURCE = b"abcdefghij"


def request(
    *,
    content: bytes = SOURCE,
    source_sha256: str | None = None,
    source_name: str = "meeting.m4a",
) -> UploadCreateRequest:
    return UploadCreateRequest(
        vault_id="vault_primary",
        source_display_name=source_name,
        source_sha256=source_sha256 or hashlib.sha256(content).hexdigest(),
        source_size_bytes=len(content),
        media_type="audio/mp4",
    )


def successful_probe(source_path):
    assert source_path.read_bytes() == SOURCE
    return MediaProbeResult(
        duration_seconds=12.5,
        audio_stream_count=1,
        format_name="mov,mp4,m4a",
    )


def put_all_parts(store: JobStore, upload_id: str, content: bytes = SOURCE) -> None:
    upload = store.get_upload(upload_id)
    for part_number in range(1, upload.part_count + 1):
        start = (part_number - 1) * upload.chunk_size_bytes
        end = min(start + upload.chunk_size_bytes, len(content))
        part = content[start:end]
        store.put_upload_part(
            upload_id,
            part_number=part_number,
            content=part,
            part_sha256=hashlib.sha256(part).hexdigest(),
        )


def test_upload_manifest_is_idempotent_and_survives_reopen(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        first, first_created = store.create_upload(
            request(),
            idempotency_key="upload-001",
        )
        second, second_created = store.create_upload(
            request(),
            idempotency_key="upload-001",
        )

    with JobStore(database) as reopened:
        restored = reopened.get_upload(first.upload_id)
        missing = reopened.list_missing_upload_parts(first.upload_id)

    assert first_created is True
    assert second_created is False
    assert first.upload_id == second.upload_id
    assert restored.state is UploadState.UPLOADING
    assert restored.chunk_size_bytes == 4
    assert restored.part_count == 3
    assert restored.received_part_count == 0
    assert missing == [1, 2, 3]


def test_schema_one_database_migrates_through_verified_job_schema(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database):
        pass
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        DROP INDEX jobs_source_upload_idx;
        ALTER TABLE jobs DROP COLUMN source_upload_id;
        DROP TABLE upload_parts;
        DROP TABLE uploads;
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    with JobStore(database) as migrated:
        upload, created = migrated.create_upload(
            request(),
            idempotency_key="upload-after-migration",
        )

        assert migrated.quick_check() is True

    assert created is True
    assert upload.state is UploadState.UPLOADING


def test_upload_idempotency_key_cannot_change_manifest(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        store.create_upload(request(), idempotency_key="upload-001")

        with pytest.raises(IdempotencyConflict):
            store.create_upload(
                request(source_name="different.m4a"),
                idempotency_key="upload-001",
            )


def test_upload_parts_are_resumable_idempotent_and_checksum_bound(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")
        third = SOURCE[8:]
        first = SOURCE[:4]

        stored_third, third_created = store.put_upload_part(
            upload.upload_id,
            part_number=3,
            content=third,
            part_sha256=hashlib.sha256(third).hexdigest(),
        )
        stored_first, first_created = store.put_upload_part(
            upload.upload_id,
            part_number=1,
            content=first,
            part_sha256=hashlib.sha256(first).hexdigest(),
        )
        repeated_first, repeated_created = store.put_upload_part(
            upload.upload_id,
            part_number=1,
            content=first,
            part_sha256=hashlib.sha256(first).hexdigest(),
        )
        progress = store.get_upload(upload.upload_id)

        with pytest.raises(UploadPartConflict):
            replacement = b"wxyz"
            store.put_upload_part(
                upload.upload_id,
                part_number=1,
                content=replacement,
                part_sha256=hashlib.sha256(replacement).hexdigest(),
            )

        with pytest.raises(UploadPartChecksumMismatch):
            store.put_upload_part(
                upload.upload_id,
                part_number=2,
                content=SOURCE[4:8],
                part_sha256="0" * 64,
            )

    assert third_created is True
    assert first_created is True
    assert repeated_created is False
    assert stored_third.part_number == 3
    assert stored_first.sha256 == repeated_first.sha256
    assert progress.received_part_count == 2
    assert progress.received_bytes == 6


def test_part_number_and_exact_size_are_enforced(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")

        with pytest.raises(InvalidJobRequest):
            store.put_upload_part(
                upload.upload_id,
                part_number=4,
                content=b"x",
                part_sha256=hashlib.sha256(b"x").hexdigest(),
            )
        with pytest.raises(InvalidJobRequest):
            store.put_upload_part(
                upload.upload_id,
                part_number=1,
                content=b"abc",
                part_sha256=hashlib.sha256(b"abc").hexdigest(),
            )


def test_completion_reports_all_missing_parts_without_changing_state(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")
        store.put_upload_part(
            upload.upload_id,
            part_number=2,
            content=SOURCE[4:8],
            part_sha256=hashlib.sha256(SOURCE[4:8]).hexdigest(),
        )

        with pytest.raises(UploadIncomplete) as caught:
            store.complete_upload(upload.upload_id)

        restored = store.get_upload(upload.upload_id)

    assert caught.value.details["missing_part_numbers"] == [1, 3]
    assert restored.state is UploadState.UPLOADING


def test_complete_upload_is_atomic_verified_and_idempotent(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")
        put_all_parts(store, upload.upload_id)

        completed, completed_now = store.complete_upload(upload.upload_id)
        source_path = store.get_verified_source_path(upload.upload_id)
        repeated, repeated_now = store.complete_upload(upload.upload_id)
        with pytest.raises(UploadStateConflict):
            store.put_upload_part(
                upload.upload_id,
                part_number=1,
                content=SOURCE[:4],
                part_sha256=hashlib.sha256(SOURCE[:4]).hexdigest(),
            )

    assert completed_now is True
    assert repeated_now is False
    assert completed.state is UploadState.COMPLETE
    assert repeated.upload_id == completed.upload_id
    assert completed.duration_seconds == 12.5
    assert completed.audio_stream_count == 1
    assert completed.detected_format_name == "mov,mp4,m4a"
    assert source_path.read_bytes() == SOURCE
    assert stat.S_IMODE(source_path.stat().st_mode) & 0o077 == 0
    assert not list((tmp_path / "sources").glob(".*.assembling"))


def test_whole_source_checksum_mismatch_preserves_parts_and_marks_failed(tmp_path) -> None:
    wrong_checksum = hashlib.sha256(b"0123456789").hexdigest()
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(
            request(source_sha256=wrong_checksum),
            idempotency_key="upload-001",
        )
        put_all_parts(store, upload.upload_id)

        with pytest.raises(UploadChecksumMismatch):
            store.complete_upload(upload.upload_id)

        restored = store.get_upload(upload.upload_id)
        parts = store.list_upload_parts(upload.upload_id)

    assert restored.state is UploadState.FAILED
    assert restored.last_error_code == "UPLOAD_CHECKSUM_MISMATCH"
    assert len(parts) == 3


def test_undecodable_source_is_a_safe_retryable_manifest_failure(tmp_path) -> None:
    def reject_source(_):
        raise SourceUndecodable("The source could not be decoded as supported media.")

    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=reject_source,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")
        put_all_parts(store, upload.upload_id)

        with pytest.raises(SourceUndecodable):
            store.complete_upload(upload.upload_id)

        restored = store.get_upload(upload.upload_id)

    assert restored.state is UploadState.FAILED
    assert restored.last_error_code == "SOURCE_UNDECODABLE"
    assert restored.received_part_count == 3


def test_corrupt_persisted_part_is_removed_from_receipts_for_resume(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")
        put_all_parts(store, upload.upload_id)
        corrupt_part = tmp_path / "uploads" / upload.upload_id / "parts" / "00000002.part"
        corrupt_part.write_bytes(b"zzzz")

        with pytest.raises(UploadPartChecksumMismatch) as caught:
            store.complete_upload(upload.upload_id)

        restored = store.get_upload(upload.upload_id)
        missing = store.list_missing_upload_parts(upload.upload_id)

    assert caught.value.details["part_number"] == 2
    assert restored.state is UploadState.UPLOADING
    assert restored.last_error_code == "UPLOAD_PART_CHECKSUM_MISMATCH"
    assert restored.received_part_count == 2
    assert missing == [2]


def test_interrupted_verification_recovers_without_losing_parts(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")
        put_all_parts(store, upload.upload_id)

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE uploads SET state = ? WHERE upload_id = ?",
        (UploadState.VERIFYING.value, upload.upload_id),
    )
    connection.commit()
    connection.close()
    temporary = tmp_path / "sources" / f".{upload.upload_id}.crash.assembling"
    temporary.write_bytes(b"partial")

    with JobStore(database) as restarted:
        recovered = restarted.recover_interrupted_uploads()
        restored = restarted.get_upload(upload.upload_id)

    assert [item.upload_id for item in recovered] == [upload.upload_id]
    assert restored.state is UploadState.UPLOADING
    assert restored.received_part_count == 3
    assert restored.last_error_code == "UPLOAD_VERIFICATION_INTERRUPTED"
    assert not temporary.exists()


def test_concurrent_identical_upload_creation_produces_one_manifest(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    first_store = JobStore(database)
    second_store = JobStore(database)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def create(store):
        try:
            barrier.wait(timeout=5)
            results.append(store.create_upload(request(), idempotency_key="upload-race"))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=create, args=(first_store,))
    second = threading.Thread(target=create, args=(second_store,))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    first_store.close()
    second_store.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert sum(created for _, created in results) == 1
    assert len({upload.upload_id for upload, _ in results}) == 1


def test_upload_storage_rejects_symlink_escape(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=successful_probe,
    ) as store:
        upload, _ = store.create_upload(request(), idempotency_key="upload-001")
        (tmp_path / "uploads" / upload.upload_id).symlink_to(
            outside,
            target_is_directory=True,
        )

        with pytest.raises(UploadStorageError):
            store.put_upload_part(
                upload.upload_id,
                part_number=1,
                content=SOURCE[:4],
                part_sha256=hashlib.sha256(SOURCE[:4]).hexdigest(),
            )

    assert list(outside.iterdir()) == []
