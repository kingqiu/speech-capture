import hashlib
import io
import json
import shutil
import wave

import pytest

from speech_capture_worker.device_security import DeviceSecurityStore
from speech_capture_worker.domain import JobCreateRequest, JobState, UploadCreateRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.worker_cli import main


def test_cli_initializes_database_without_exposing_path(tmp_path, capsys) -> None:
    result = main(["init", "--data-dir", str(tmp_path / "runtime")])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload == {"database_ready": True, "schema_ready": True}


def test_cli_bootstraps_lists_and_revokes_a_paired_device(tmp_path, capsys) -> None:
    data_dir = tmp_path / "runtime"
    assert main([
        "create-pairing-session",
        "--data-dir",
        str(data_dir),
        "--device-id",
        "laptop_cli",
        "--vault-id",
        "vault_primary",
    ]) == 0
    session = json.loads(capsys.readouterr().out)
    with DeviceSecurityStore(data_dir / "security.sqlite3") as security:
        issued = security.confirm_pairing(
            session_id=session["session_id"],
            pairing_code=session["pairing_code"],
        )

    assert main(["list-paired-devices", "--data-dir", str(data_dir)]) == 0
    listed_output = capsys.readouterr().out
    listed = json.loads(listed_output)
    assert issued.bearer_token not in listed_output
    assert listed["devices"][0]["device_id"] == "laptop_cli"

    assert main([
        "revoke-device",
        "--data-dir",
        str(data_dir),
        "laptop_cli",
    ]) == 0
    revoked = json.loads(capsys.readouterr().out)
    assert revoked == {"device_id": "laptop_cli", "revoked": True}


def test_cli_create_is_idempotent_and_listable(tmp_path, capsys) -> None:
    data_dir = str(tmp_path / "runtime")
    create_args = [
        "create-job",
        "--data-dir",
        data_dir,
        "--vault-id",
        "vault_primary",
        "--source-name",
        "meeting.m4a",
        "--source-sha256",
        "a" * 64,
        "--source-size-bytes",
        "1024",
        "--idempotency-key",
        "submit-001",
    ]

    assert main(create_args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(create_args) == 0
    second = json.loads(capsys.readouterr().out)
    assert main(["list-jobs", "--data-dir", data_dir]) == 0
    listed = json.loads(capsys.readouterr().out)

    assert first["created"] is True
    assert second["created"] is False
    assert first["job"]["job_id"] == second["job"]["job_id"]
    assert len(listed["jobs"]) == 1


def test_cli_can_set_and_clear_job_content_type(tmp_path, capsys) -> None:
    data_dir = str(tmp_path / "runtime")
    assert (
        main(
            [
                "create-job",
                "--data-dir",
                data_dir,
                "--vault-id",
                "vault_primary",
                "--source-name",
                "presentation.m4a",
                "--source-sha256",
                "a" * 64,
                "--source-size-bytes",
                "1024",
                "--idempotency-key",
                "submit-content-type",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)["job"]

    assert (
        main(
            [
                "set-content-type",
                "--data-dir",
                data_dir,
                created["job_id"],
                "--expected-revision",
                str(created["revision"]),
                "--content-type",
                "speech",
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert saved["changed"] is True
    assert saved["content_type_override"] == "speech"

    assert (
        main(
            [
                "set-content-type",
                "--data-dir",
                data_dir,
                created["job_id"],
                "--expected-revision",
                str(saved["job_revision"]),
                "--clear",
            ]
        )
        == 0
    )
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["changed"] is True
    assert cleared["content_type_override"] is None


def test_cli_appends_and_lists_idempotent_recording_date_correction(
    tmp_path, capsys
) -> None:
    data_path = tmp_path / "runtime"
    content = b"test-audio"
    checksum = hashlib.sha256(content).hexdigest()

    def probe(_):
        return MediaProbeResult(
            duration_seconds=1,
            audio_stream_count=1,
            format_name="wav",
        )

    with JobStore(data_path / "worker.sqlite3", source_probe=probe) as store:
        upload, _ = store.create_upload(
            UploadCreateRequest(
                vault_id="vault_primary",
                source_display_name="dated.wav",
                source_sha256=checksum,
                source_size_bytes=len(content),
                media_type="audio/wav",
            ),
            idempotency_key="dated-upload",
        )
        store.put_upload_part(
            upload.upload_id,
            part_number=1,
            content=content,
            part_sha256=checksum,
        )
        store.complete_upload(upload.upload_id)
        queued, _ = store.create_job_from_upload(
            upload.upload_id,
            idempotency_key="dated-job",
        )
        current = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )
        for state in (
            JobState.TRANSCRIBING,
            JobState.ALIGNING,
            JobState.STRUCTURING,
            JobState.QUALITY_CHECK,
            JobState.PROCESSED,
        ):
            current = store.transition_job(
                current.job_id,
                state,
                expected_revision=current.revision,
                reason_code="test_progress",
            )

    args = [
        "add-correction",
        "--data-dir",
        str(data_path),
        current.job_id,
        "--field",
        "recording_date",
        "--after",
        "2026-08-01",
        "--author",
        "azhua",
        "--idempotency-key",
        "date-correction-1",
        "--expected-revision",
        str(current.revision),
    ]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert main(["list-corrections", "--data-dir", str(data_path), current.job_id]) == 0
    listed = json.loads(capsys.readouterr().out)

    assert first["created"] is True
    assert replay["created"] is False
    assert first["correction"]["correction_id"] == replay["correction"]["correction_id"]
    assert listed["corrections"] == [first["correction"]]


def test_cli_manages_publication_lease_lifecycle(tmp_path, capsys) -> None:
    data_path = tmp_path / "runtime"
    content = b"publication-audio"
    checksum = hashlib.sha256(content).hexdigest()

    def probe(_):
        return MediaProbeResult(
            duration_seconds=1,
            audio_stream_count=1,
            format_name="wav",
        )

    with JobStore(data_path / "worker.sqlite3", source_probe=probe) as store:
        upload, _ = store.create_upload(
            UploadCreateRequest(
                vault_id="vault_primary",
                source_display_name="publication.wav",
                source_sha256=checksum,
                source_size_bytes=len(content),
                media_type="audio/wav",
            ),
            idempotency_key="publication-cli-upload",
        )
        store.put_upload_part(
            upload.upload_id,
            part_number=1,
            content=content,
            part_sha256=checksum,
        )
        store.complete_upload(upload.upload_id)
        queued, _ = store.create_job_from_upload(
            upload.upload_id,
            idempotency_key="publication-cli-job",
        )
        current = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )
        for state in (
            JobState.TRANSCRIBING,
            JobState.ALIGNING,
            JobState.STRUCTURING,
            JobState.QUALITY_CHECK,
            JobState.PROCESSED,
        ):
            current = store.transition_job(
                current.job_id,
                state,
                expected_revision=current.revision,
                reason_code="test_progress",
            )

    claim_args = [
        "claim-publication",
        "--data-dir",
        str(data_path),
        current.job_id,
        "--publisher-id",
        "device_a",
        "--target-relative-path",
        "Speech/Undated/publication--sp_123",
        "--manifest-sha256",
        "a" * 64,
        "--expected-revision",
        str(current.revision),
    ]
    assert main(claim_args) == 0
    claimed = json.loads(capsys.readouterr().out)
    assert claimed["created"] is True
    assert claimed["job"]["state"] == "publishing"

    assert (
        main(
            [
                "renew-publication",
                "--data-dir",
                str(data_path),
                current.job_id,
                "--lease-id",
                claimed["lease"]["lease_id"],
                "--publisher-id",
                "device_a",
                "--lease-seconds",
                "300",
            ]
        )
        == 0
    )
    renewed = json.loads(capsys.readouterr().out)
    assert renewed["lease"]["expires_at"] > claimed["lease"]["expires_at"]

    assert main(["publication-status", "--data-dir", str(data_path), current.job_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["leases"][0]["state"] == "active"
    assert status["receipt"] is None

    assert (
        main(
            [
                "release-publication",
                "--data-dir",
                str(data_path),
                current.job_id,
                "--lease-id",
                claimed["lease"]["lease_id"],
                "--publisher-id",
                "device_a",
            ]
        )
        == 0
    )
    released = json.loads(capsys.readouterr().out)
    assert released["job"]["state"] == "processed"


def test_cli_returns_stable_error_for_invalid_transition(tmp_path, capsys) -> None:
    data_dir = str(tmp_path / "runtime")
    main(
        [
            "create-job",
            "--data-dir",
            data_dir,
            "--vault-id",
            "vault_primary",
            "--source-name",
            "meeting.m4a",
            "--source-sha256",
            "a" * 64,
            "--source-size-bytes",
            "1024",
            "--idempotency-key",
            "submit-001",
        ]
    )
    created = json.loads(capsys.readouterr().out)

    result = main(
        [
            "transition",
            "--data-dir",
            data_dir,
            created["job"]["job_id"],
            "processed",
            "--expected-revision",
            "0",
        ]
    )
    error = json.loads(capsys.readouterr().err)

    assert result == 2
    assert error["error"]["code"] == "INVALID_JOB_TRANSITION"


def test_cli_upload_manifest_part_and_resume_status(tmp_path, capsys) -> None:
    data_dir = str(tmp_path / "runtime")
    content = b"small audio placeholder"
    checksum = hashlib.sha256(content).hexdigest()
    create_args = [
        "create-upload",
        "--data-dir",
        data_dir,
        "--vault-id",
        "vault_primary",
        "--source-name",
        "meeting.m4a",
        "--source-sha256",
        checksum,
        "--source-size-bytes",
        str(len(content)),
        "--media-type",
        "audio/mp4",
        "--idempotency-key",
        "upload-001",
    ]

    assert main(create_args) == 0
    created = json.loads(capsys.readouterr().out)
    upload_id = created["upload"]["upload_id"]
    part_file = tmp_path / "part.bin"
    part_file.write_bytes(content)

    assert (
        main(
            [
                "put-upload-part",
                "--data-dir",
                data_dir,
                upload_id,
                "1",
                "--part-file",
                str(part_file),
                "--part-sha256",
                checksum,
            ]
        )
        == 0
    )
    stored = json.loads(capsys.readouterr().out)
    assert main(["get-upload", "--data-dir", data_dir, upload_id]) == 0
    status = json.loads(capsys.readouterr().out)

    assert created["missing_part_numbers"] == [1]
    assert stored["created"] is True
    assert stored["upload"]["received_bytes"] == len(content)
    assert status["missing_part_numbers"] == []


def test_cli_upload_checksum_error_is_stable(tmp_path, capsys) -> None:
    data_dir = str(tmp_path / "runtime")
    content = b"small audio placeholder"
    checksum = hashlib.sha256(content).hexdigest()
    main(
        [
            "create-upload",
            "--data-dir",
            data_dir,
            "--vault-id",
            "vault_primary",
            "--source-name",
            "meeting.m4a",
            "--source-sha256",
            checksum,
            "--source-size-bytes",
            str(len(content)),
            "--media-type",
            "audio/mp4",
            "--idempotency-key",
            "upload-001",
        ]
    )
    upload_id = json.loads(capsys.readouterr().out)["upload"]["upload_id"]
    part_file = tmp_path / "part.bin"
    part_file.write_bytes(content)

    result = main(
        [
            "put-upload-part",
            "--data-dir",
            data_dir,
            upload_id,
            "1",
            "--part-file",
            str(part_file),
            "--part-sha256",
            "0" * 64,
        ]
    )
    error = json.loads(capsys.readouterr().err)

    assert result == 2
    assert error["error"]["code"] == "UPLOAD_PART_CHECKSUM_MISMATCH"


def test_cli_snapshot_and_updates_expose_reconnect_contract(tmp_path, capsys) -> None:
    data_path = tmp_path / "runtime"
    data_path.mkdir()
    with JobStore(data_path / "worker.sqlite3") as store:
        job, _ = store.create_job(
            JobCreateRequest(
                vault_id="vault_primary",
                source_display_name="meeting.m4a",
                source_sha256="a" * 64,
                source_size_bytes=1024,
            ),
            idempotency_key="submit-snapshot",
        )

    assert (
        main(
            [
                "snapshot",
                "--data-dir",
                str(data_path),
                job.job_id,
            ]
        )
        == 0
    )
    snapshot = json.loads(capsys.readouterr().out)["snapshot"]
    assert (
        main(
            [
                "updates",
                "--data-dir",
                str(data_path),
                job.job_id,
                "--limit",
                "10",
            ]
        )
        == 0
    )
    updates = json.loads(capsys.readouterr().out)

    assert snapshot["stable_segments"] == []
    assert snapshot["provisional"] is None
    assert snapshot["latest_event_sequence"] == updates["updates"][-1]["sequence"]
    assert updates["updates"][0]["event_type"] == "job.created"
    assert updates["has_more"] is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_cli_prepares_private_normalized_audio_plan(tmp_path, capsys) -> None:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 16_000)
    content = output.getvalue()
    checksum = hashlib.sha256(content).hexdigest()
    data_path = tmp_path / "runtime"

    def probe(_):
        return MediaProbeResult(
            duration_seconds=1,
            audio_stream_count=1,
            format_name="wav",
        )

    with JobStore(data_path / "worker.sqlite3", source_probe=probe) as store:
        upload, _ = store.create_upload(
            UploadCreateRequest(
                vault_id="vault_primary",
                source_display_name="one-second.wav",
                source_sha256=checksum,
                source_size_bytes=len(content),
                media_type="audio/wav",
            ),
            idempotency_key="cli-prepare-upload",
        )
        store.put_upload_part(
            upload.upload_id,
            part_number=1,
            content=content,
            part_sha256=checksum,
        )
        store.complete_upload(upload.upload_id)
        queued, _ = store.create_job_from_upload(
            upload.upload_id,
            idempotency_key="cli-prepare-job",
        )
        job = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )

    assert (
        main(
            [
                "prepare-audio",
                "--data-dir",
                str(data_path),
                job.job_id,
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "list-asr-attempts",
                "--data-dir",
                str(data_path),
                job.job_id,
            ]
        )
        == 0
    )
    attempts = json.loads(capsys.readouterr().out)

    assert prepared["changed"] is True
    assert prepared["plan"]["duration_ms"] == 1000
    assert prepared["plan"]["relative_path"].startswith("jobs/")
    assert attempts == {"attempts": []}
