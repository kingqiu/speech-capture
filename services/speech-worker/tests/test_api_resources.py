"""Authenticated Worker API resource and privacy tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from speech_capture_worker.api import create_app
from speech_capture_worker.api_auth import ApiCredential, ApiPrincipal, CredentialVerifier
from speech_capture_worker.artifact_generation import (
    ARTIFACT_CHECKPOINT_KEY,
    ARTIFACT_FILES,
    ARTIFACT_MANIFEST,
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_STAGE,
)
from speech_capture_worker.job_store import MAX_UPLOAD_CHUNK_SIZE_BYTES, JobStore
from speech_capture_worker.media_probe import MediaProbeResult

SOURCE = b"abcdefghij"
TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz0123456789"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def _probe(source_path: Path) -> MediaProbeResult:
    assert source_path.read_bytes() == SOURCE
    return MediaProbeResult(
        duration_seconds=12.5,
        audio_stream_count=1,
        format_name="test-audio",
    )


def _verifier(*vault_ids: str) -> CredentialVerifier:
    principal = ApiPrincipal(
        device_id="device_test",
        allowed_vault_ids=frozenset(vault_ids),
    )
    return CredentialVerifier((ApiCredential.from_plaintext(TOKEN, principal),))


def _create_upload(client: TestClient, *, vault_id: str = "vault_primary") -> dict:
    response = client.post(
        "/v1/uploads",
        headers={**AUTHORIZATION, "Idempotency-Key": f"upload-{vault_id}"},
        json={
            "vault_id": vault_id,
            "source_display_name": "meeting.m4a",
            "source_sha256": hashlib.sha256(SOURCE).hexdigest(),
            "source_size_bytes": len(SOURCE),
            "media_type": "audio/mp4",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _complete_upload(client: TestClient, upload: dict) -> None:
    upload_id = upload["upload"]["upload_id"]
    chunk_size = upload["upload"]["chunk_size_bytes"]
    for part_number in range(1, upload["upload"]["part_count"] + 1):
        start = (part_number - 1) * chunk_size
        part = SOURCE[start : start + chunk_size]
        response = client.put(
            f"/v1/uploads/{upload_id}/parts/{part_number}",
            headers={
                **AUTHORIZATION,
                "Content-Type": "application/octet-stream",
                "X-Part-SHA256": hashlib.sha256(part).hexdigest(),
            },
            content=part,
        )
        assert response.status_code == 200, response.text
        assert response.json()["part"]["size_bytes"] == len(part)
    response = client.post(
        f"/v1/uploads/{upload_id}/complete",
        headers=AUTHORIZATION,
    )
    assert response.status_code == 200, response.text
    assert response.json()["upload"]["state"] == "complete"


def _create_job(client: TestClient, upload_id: str) -> dict:
    response = client.post(
        "/v1/jobs",
        headers={**AUTHORIZATION, "Idempotency-Key": "job-primary"},
        json={
            "upload_id": upload_id,
            "recording_context": "客户公司正确名称是聚衣堂。",
            "recording_date": "2026-08-03",
            "content_type_override": "meeting",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_private_routes_require_authentication_and_redact_validation_input(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))

        missing = client.get("/v1/jobs", params={"vault_id": "vault_primary"})
        invalid = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_primary"},
            headers={"Authorization": "Bearer invalid-invalid-invalid-invalid"},
        )
        private_value = "private-transcript-content"
        malformed = client.post(
            "/v1/uploads",
            headers={**AUTHORIZATION, "Idempotency-Key": "invalid-upload"},
            json={"vault_id": "vault_primary", "unexpected": private_value},
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert malformed.status_code == 422
    assert private_value not in malformed.text
    assert TOKEN not in malformed.text
    assert malformed.json()["error"]["request_id"] == malformed.headers["x-request-id"]


def test_unconfigured_private_api_fails_closed() -> None:
    response = TestClient(create_app()).get(
        "/v1/jobs",
        params={"vault_id": "vault_primary"},
        headers=AUTHORIZATION,
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTHENTICATION_NOT_CONFIGURED"


def test_unexpected_api_error_never_echoes_exception_content() -> None:
    private_value = "/Users/private/customer-recording.wav secret transcript text"
    test_app = create_app(credential_verifier=_verifier("vault_primary"))

    @test_app.get("/test-unexpected-error")
    def explode() -> None:
        raise RuntimeError(private_value)

    response = TestClient(test_app, raise_server_exceptions=False).get(
        "/test-unexpected-error"
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_WORKER_ERROR"
    assert private_value not in response.text
    assert "/Users/" not in response.text


def test_resumable_upload_job_snapshot_updates_and_vault_isolation(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        upload = _create_upload(client)
        assert upload["created"] is True
        assert upload["missing_part_numbers"] == [1, 2, 3]

        wrong_media_type = client.put(
            f"/v1/uploads/{upload['upload']['upload_id']}/parts/1",
            headers={
                **AUTHORIZATION,
                "Content-Type": "text/plain",
                "X-Part-SHA256": hashlib.sha256(SOURCE[:4]).hexdigest(),
            },
            content=SOURCE[:4],
        )
        declared_too_large = client.put(
            f"/v1/uploads/{upload['upload']['upload_id']}/parts/1",
            headers={
                **AUTHORIZATION,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(MAX_UPLOAD_CHUNK_SIZE_BYTES + 1),
                "X-Part-SHA256": hashlib.sha256(SOURCE[:4]).hexdigest(),
            },
            content=SOURCE[:4],
        )
        assert wrong_media_type.status_code == 415
        assert declared_too_large.status_code == 413

        first_part = SOURCE[:4]
        first = client.put(
            f"/v1/uploads/{upload['upload']['upload_id']}/parts/1",
            headers={
                **AUTHORIZATION,
                "Content-Type": "application/octet-stream",
                "X-Part-SHA256": hashlib.sha256(first_part).hexdigest(),
            },
            content=first_part,
        )
        replay = client.put(
            f"/v1/uploads/{upload['upload']['upload_id']}/parts/1",
            headers={
                **AUTHORIZATION,
                "Content-Type": "application/octet-stream",
                "X-Part-SHA256": hashlib.sha256(first_part).hexdigest(),
            },
            content=first_part,
        )
        assert first.json()["created"] is True
        assert replay.json()["created"] is False

        _complete_upload(client, upload)
        repeated_completion = client.post(
            f"/v1/uploads/{upload['upload']['upload_id']}/complete",
            headers=AUTHORIZATION,
        )
        assert repeated_completion.status_code == 200
        assert repeated_completion.json()["created"] is False

        job = _create_job(client, upload["upload"]["upload_id"])
        assert job["created"] is True
        assert job["job"]["state"] == "queued"
        assert job["job"]["recording_context"] == "客户公司正确名称是聚衣堂。"
        assert job["job"]["recording_date"] == "2026-08-03"
        job_id = job["job"]["job_id"]

        pause = client.post(
            f"/v1/jobs/{job_id}/pause",
            headers={**AUTHORIZATION, "Idempotency-Key": "pause-primary"},
            json={"expected_revision": job["job"]["revision"]},
        )
        pause_replay = client.post(
            f"/v1/jobs/{job_id}/pause",
            headers={**AUTHORIZATION, "Idempotency-Key": "pause-primary"},
            json={"expected_revision": job["job"]["revision"]},
        )
        assert pause.status_code == 200
        assert pause.json()["job"]["state"] == "paused"
        assert pause.json()["applied"] is True
        assert pause_replay.json() == {**pause.json(), "applied": False}

        resume = client.post(
            f"/v1/jobs/{job_id}/resume",
            headers={**AUTHORIZATION, "Idempotency-Key": "resume-primary"},
            json={"expected_revision": pause.json()["job"]["revision"]},
        )
        cancel = client.post(
            f"/v1/jobs/{job_id}/cancel",
            headers={**AUTHORIZATION, "Idempotency-Key": "cancel-primary"},
            json={"expected_revision": resume.json()["job"]["revision"]},
        )
        assert resume.json()["job"]["state"] == "queued"
        assert cancel.json()["job"]["state"] == "cancelled"

        snapshot = client.get(f"/v1/jobs/{job_id}/snapshot", headers=AUTHORIZATION)
        updates = client.get(f"/v1/jobs/{job_id}/events", headers=AUTHORIZATION)
        listing = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_primary"},
            headers=AUTHORIZATION,
        )
        denied_listing = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_other"},
            headers=AUTHORIZATION,
        )

    assert snapshot.status_code == 200
    assert snapshot.json()["stable_segments"] == []
    assert snapshot.json()["job"]["job_id"] == job_id
    assert updates.status_code == 200
    assert updates.json()["updates"]
    assert "聚衣堂" not in updates.text
    assert [item["job_id"] for item in listing.json()["jobs"]] == [job_id]
    assert denied_listing.status_code == 403


def test_job_creation_rejects_invalid_calendar_date_without_echoing_input(tmp_path) -> None:
    invalid_date = "2026-02-31"
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        upload = _create_upload(client)
        _complete_upload(client, upload)
        response = client.post(
            "/v1/jobs",
            headers={**AUTHORIZATION, "Idempotency-Key": "job-invalid-date"},
            json={
                "upload_id": upload["upload"]["upload_id"],
                "recording_date": invalid_date,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert invalid_date not in response.text


def test_existing_resource_in_another_vault_is_hidden(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3", upload_chunk_size_bytes=4) as store:
        other_upload, _ = store.create_upload(
            request=_upload_request("vault_other"),
            idempotency_key="other-upload",
        )
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        response = client.get(
            f"/v1/uploads/{other_upload.upload_id}",
            headers=AUTHORIZATION,
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_artifact_listing_download_and_integrity_failure(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        upload = _create_upload(client)
        _complete_upload(client, upload)
        job = _create_job(client, upload["upload"]["upload_id"])
        job_id = job["job"]["job_id"]
        package_dir = store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE)
        contents = {
            name: (f"# {name}\n" if name.endswith(".md") else "{}\n").encode()
            for name in ARTIFACT_FILES
        }
        hashes = {}
        for name, content in contents.items():
            (package_dir / name).write_bytes(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "speech_id": "sp_test",
            "job_id": job_id,
            "artifact_count": len(ARTIFACT_FILES),
            "files": hashes,
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        (package_dir / ARTIFACT_MANIFEST).write_bytes(manifest_bytes)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        store.put_checkpoint(
            job_id,
            stage=ARTIFACT_STAGE,
            checkpoint_key=ARTIFACT_CHECKPOINT_KEY,
            payload={
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "speech_id": "sp_test",
                "artifact_count": len(ARTIFACT_FILES),
                "manifest_sha256": manifest_sha256,
                "package_relative_path": package_dir.relative_to(
                    store.data_directory
                ).as_posix(),
                "files": hashes,
            },
        )

        listing = client.get(f"/v1/jobs/{job_id}/artifacts", headers=AUTHORIZATION)
        download = client.get(
            f"/v1/jobs/{job_id}/artifacts/note.md",
            headers=AUTHORIZATION,
        )
        (package_dir / "note.md").write_text("tampered private note", encoding="utf-8")
        tampered = client.get(f"/v1/jobs/{job_id}/artifacts", headers=AUTHORIZATION)
        (package_dir / "note.md").unlink()
        outside = tmp_path / "outside-note.md"
        outside.write_bytes(contents["note.md"])
        (package_dir / "note.md").symlink_to(outside)
        symlinked = client.get(f"/v1/jobs/{job_id}/artifacts", headers=AUTHORIZATION)

    assert listing.status_code == 200
    assert len(listing.json()["artifacts"]) == 7
    assert listing.json()["manifest_sha256"] == manifest_sha256
    assert download.status_code == 200
    assert download.text == "# note.md\n"
    assert tampered.status_code == 409
    assert "tampered private note" not in tampered.text
    assert symlinked.status_code == 409


def _upload_request(vault_id: str):
    from speech_capture_worker.domain import UploadCreateRequest

    return UploadCreateRequest(
        vault_id=vault_id,
        source_display_name="other.m4a",
        source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        source_size_bytes=len(SOURCE),
        media_type="audio/mp4",
    )
