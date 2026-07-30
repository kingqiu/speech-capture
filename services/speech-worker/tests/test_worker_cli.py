import hashlib
import json

from speech_capture_worker.worker_cli import main


def test_cli_initializes_database_without_exposing_path(tmp_path, capsys) -> None:
    result = main(["init", "--data-dir", str(tmp_path / "runtime")])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload == {"database_ready": True, "schema_ready": True}


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
