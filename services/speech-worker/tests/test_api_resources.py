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
    NOTE_MARKDOWN,
)
from speech_capture_worker.corrections import corrections_sha256
from speech_capture_worker.domain import JobState
from speech_capture_worker.job_store import MAX_UPLOAD_CHUNK_SIZE_BYTES, JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.structuring_execution import (
    STRUCTURING_CHECKPOINT_KEY,
    STRUCTURING_STAGE,
    SUMMARY_REVISION_SCHEMA_VERSION,
    SUMMARY_REVISION_STAGE,
)
from speech_capture_worker.transcript import (
    SpeakerLabelStatus,
    TranscriptOutcome,
)

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


def _advance_to_processed_review_job(store: JobStore, job_id: str) -> None:
    queued = store.get_job(job_id)
    preprocessing = store.claim_job_for_processing(
        job_id,
        expected_revision=queued.revision,
    )
    transcribing = store.transition_job(
        job_id,
        JobState.TRANSCRIBING,
        expected_revision=preprocessing.revision,
    )
    store.commit_transcript_segment(
        job_id,
        commit_key="api-review-segment",
        start_ms=0,
        end_ms=5_000,
        outcome=TranscriptOutcome.TRANSCRIBED,
        text="这是合成逐字稿。",
        language="zh",
        speaker_id="speaker_0",
        speaker_label_status=SpeakerLabelStatus.ANONYMOUS,
    )
    current = transcribing
    for state in (
        JobState.ALIGNING,
        JobState.DIARIZING,
        JobState.STRUCTURING,
        JobState.QUALITY_CHECK,
        JobState.PROCESSED,
    ):
        current = store.transition_job(
            job_id,
            state,
            expected_revision=current.revision,
        )


def _install_publication_package(store: JobStore, job_id: str) -> str:
    package_dir = store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE)
    speech_id = "sp_api_publication"
    contents = {
        name: (f"# {name}\n" if name.endswith(".md") else "{}\n").encode()
        for name in ARTIFACT_FILES
    }
    contents["speech-record.json"] = (
        json.dumps(
            {
                "job_id": job_id,
                "speech_id": speech_id,
                "document": {"title": "合成 发布 记录"},
                "dates": {"recording_date": "2026-08-03"},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    hashes: dict[str, str] = {}
    for name, content in contents.items():
        (package_dir / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "speech_id": speech_id,
        "job_id": job_id,
        "artifact_count": len(ARTIFACT_FILES),
        "files": hashes,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    (package_dir / ARTIFACT_MANIFEST).write_bytes(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    store.put_checkpoint(
        job_id,
        stage=ARTIFACT_STAGE,
        checkpoint_key=ARTIFACT_CHECKPOINT_KEY,
        payload={
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "speech_id": speech_id,
            "artifact_count": len(ARTIFACT_FILES),
            "manifest_sha256": manifest_sha256,
            "package_relative_path": package_dir.relative_to(store.data_directory).as_posix(),
            "files": hashes,
        },
    )
    return manifest_sha256


def test_segment_review_is_atomic_revision_guarded_and_listable(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        upload = _create_upload(client)
        _complete_upload(client, upload)
        created = _create_job(client, upload["upload"]["upload_id"])
        job_id = created["job"]["job_id"]
        _advance_to_processed_review_job(store, job_id)
        current = store.get_job(job_id)

        saved = client.post(
            f"/v1/jobs/{job_id}/segment-review",
            headers={**AUTHORIZATION, "Idempotency-Key": "review-segment-primary"},
            json={
                "expected_revision": current.revision,
                "segment_id": "seg_00000001",
                "before_text": "这是合成逐字稿。",
                "after_text": "这是校订后的合成逐字稿。",
                "before_speaker_id": "speaker_0",
                "after_speaker_id": None,
                "author": "obsidian-user",
            },
        )
        replayed = client.post(
            f"/v1/jobs/{job_id}/segment-review",
            headers={**AUTHORIZATION, "Idempotency-Key": "review-segment-primary"},
            json={
                "expected_revision": current.revision,
                "segment_id": "seg_00000001",
                "before_text": "这是合成逐字稿。",
                "after_text": "这是校订后的合成逐字稿。",
                "before_speaker_id": "speaker_0",
                "after_speaker_id": None,
                "author": "obsidian-user",
            },
        )
        renamed = client.post(
            f"/v1/jobs/{job_id}/speaker-display-name",
            headers={**AUTHORIZATION, "Idempotency-Key": "rename-speaker-primary"},
            json={
                "expected_revision": saved.json()["job"]["revision"],
                "speaker_id": "speaker_0",
                "before": "Speaker 0",
                "after": "王总",
                "author": "obsidian-user",
            },
        )
        listed = client.get(
            f"/v1/jobs/{job_id}/corrections",
            headers=AUTHORIZATION,
        )
        raw_segment = store.get_job_snapshot(job_id).stable_segments[0]

    assert saved.status_code == 200, saved.text
    assert saved.json()["created"] is True
    assert replayed.status_code == 200
    assert replayed.json()["created"] is False
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["created"] is True
    assert renamed.json()["correction"]["field"] == "speaker_display_name"
    assert listed.status_code == 200
    assert [item["field"] for item in listed.json()["corrections"]] == [
        "segment_review",
        "speaker_display_name",
    ]
    assert raw_segment.text == "这是合成逐字稿。"
    assert raw_segment.speaker_id == "speaker_0"


def test_published_job_accepts_speaker_rename_and_archives_old_receipt(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        upload = _create_upload(client)
        _complete_upload(client, upload)
        created = _create_job(client, upload["upload"]["upload_id"])
        job_id = created["job"]["job_id"]
        _advance_to_processed_review_job(store, job_id)
        processed = store.get_job(job_id)
        lease, _, _ = store.claim_publication(
            job_id,
            publisher_id="vault_primary",
            target_relative_path="Speech/2026/08/original",
            manifest_sha256="a" * 64,
            expected_revision=processed.revision,
        )
        _, published, _ = store.acknowledge_publication(
            job_id,
            lease_id=lease.lease_id,
            publisher_id="vault_primary",
            manifest_sha256="a" * 64,
        )

        renamed = client.post(
            f"/v1/jobs/{job_id}/speaker-display-name",
            headers={**AUTHORIZATION, "Idempotency-Key": "rename-published-speaker"},
            json={
                "expected_revision": published.revision,
                "speaker_id": "speaker_0",
                "before": "Speaker 0",
                "after": "王总",
                "author": "obsidian-user",
            },
        )
        revised = store.get_job(job_id)
        archived_count = int(
            store._connection.execute(  # noqa: SLF001 - persistence boundary assertion
                "SELECT COUNT(*) FROM publication_receipt_history WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
        )
        replacement_lease, publishing, replacement_created = store.claim_publication(
            job_id,
            publisher_id="vault_primary",
            target_relative_path="Speech/2026/08/revised",
            manifest_sha256="b" * 64,
            expected_revision=revised.revision,
        )
        current_receipt = store.get_publication_receipt(job_id)

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["job"]["state"] == "published"
    assert renamed.json()["correction"]["field"] == "speaker_display_name"
    assert current_receipt is None
    assert archived_count == 1
    assert replacement_created is True
    assert replacement_lease.generation == 2
    assert publishing.state is JobState.PUBLISHING


def test_summary_revision_is_private_listable_and_rejectable(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        upload = _create_upload(client)
        _complete_upload(client, upload)
        created = _create_job(client, upload["upload"]["upload_id"])
        job_id = created["job"]["job_id"]
        _advance_to_processed_review_job(store, job_id)
        current = store.get_job(job_id)
        raw_segment = store.get_job_snapshot(job_id).stable_segments[0]
        before_document = {
            "summary": {"text": "旧的一分钟总览。", "evidence": ["seg_00000001"]},
            "actions": [],
        }
        after_document = {
            "summary": {"text": "新的一分钟总览。", "evidence": ["seg_00000001"]},
            "actions": [{"task": "跟进合成事项", "evidence": ["seg_00000001"]}],
        }
        before_checkpoint = {
            "schema_version": "test",
            "raw_sha256": "1" * 64,
            "document": before_document,
        }
        after_checkpoint = {
            "schema_version": "test",
            "raw_sha256": "2" * 64,
            "document": after_document,
        }
        revision_key = "summary_revision_api_test"
        store.put_checkpoint(
            job_id,
            stage=STRUCTURING_STAGE,
            checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
            payload=after_checkpoint,
        )
        store.put_checkpoint(
            job_id,
            stage=SUMMARY_REVISION_STAGE,
            checkpoint_key=revision_key,
            payload={
                "schema_version": SUMMARY_REVISION_SCHEMA_VERSION,
                "candidate_version": 2,
                "changed": True,
                "text_correction_count": 1,
                "speaker_rename_count": 0,
                "before_document": before_document,
                "after_document": after_document,
                "before_checkpoint": before_checkpoint,
                "after_checkpoint": after_checkpoint,
                "diff_truncated": False,
            },
        )
        artifact_directory = store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE)
        artifact_directory.mkdir(parents=True, exist_ok=True)
        (artifact_directory / NOTE_MARKDOWN).write_text(
            "# 合成笔记\n\n## 我的补充\n\n人工保留内容。\n",
            encoding="utf-8",
        )

        listed = client.get(
            f"/v1/jobs/{job_id}/summary-revisions",
            headers=AUTHORIZATION,
        )
        rejected = client.post(
            f"/v1/jobs/{job_id}/summary-revisions/{revision_key}/decision",
            headers={**AUTHORIZATION, "Idempotency-Key": "reject-api-summary"},
            json={
                "expected_revision": current.revision,
                "decision": "rejected",
            },
        )
        replayed = client.post(
            f"/v1/jobs/{job_id}/summary-revisions/{revision_key}/decision",
            headers={**AUTHORIZATION, "Idempotency-Key": "reject-api-summary"},
            json={
                "expected_revision": current.revision,
                "decision": "rejected",
            },
        )
        listed_after = client.get(
            f"/v1/jobs/{job_id}/summary-revisions",
            headers=AUTHORIZATION,
        )
        raw_segment_after = store.get_job_snapshot(job_id).stable_segments[0]
        restored = next(
            item
            for item in store.list_checkpoints(job_id, stage=STRUCTURING_STAGE)
            if item.checkpoint_key == STRUCTURING_CHECKPOINT_KEY
        )

    assert listed.status_code == 200, listed.text
    assert listed.json()["current_version"] == 1
    assert listed.json()["manual_section_markdown"] == ("## 我的补充\n\n人工保留内容。\n")
    assert listed.json()["revisions"] == [
        {
            "revision_key": revision_key,
            "base_version": 1,
            "candidate_version": 2,
            "status": "pending",
            "changed": True,
            "text_correction_count": 1,
            "speaker_rename_count": 0,
            "before_document": before_document,
            "after_document": after_document,
            "diff_truncated": False,
            "created_at": listed.json()["revisions"][0]["created_at"],
            "decided_at": None,
            "artifact_manifest_sha256": None,
        }
    ]
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["applied"] is True
    assert rejected.json()["revision"]["status"] == "rejected"
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["applied"] is False
    assert listed_after.json()["current_version"] == 1
    assert listed_after.json()["revisions"][0]["status"] == "rejected"
    assert restored.payload == before_checkpoint
    assert raw_segment_after == raw_segment


def test_summary_regeneration_api_uses_new_corrections_once(tmp_path) -> None:
    calls: list[str] = []
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:

        def regenerate(job_id: str) -> None:
            calls.append(job_id)
            before_document = {"summary": {"text": "旧总览", "evidence": []}}
            after_document = {"summary": {"text": "新总览", "evidence": []}}
            before_checkpoint = {
                "raw_sha256": "3" * 64,
                "document": before_document,
            }
            after_checkpoint = {
                "raw_sha256": "4" * 64,
                "document": after_document,
            }
            store.put_checkpoint(
                job_id,
                stage=STRUCTURING_STAGE,
                checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
                payload=after_checkpoint,
            )
            store.put_checkpoint(
                job_id,
                stage=SUMMARY_REVISION_STAGE,
                checkpoint_key="summary_revision_regenerated",
                payload={
                    "schema_version": SUMMARY_REVISION_SCHEMA_VERSION,
                    "candidate_version": 2,
                    "corrections_sha256": corrections_sha256(store.list_corrections(job_id)),
                    "changed": True,
                    "text_correction_count": 1,
                    "speaker_rename_count": 0,
                    "before_document": before_document,
                    "after_document": after_document,
                    "before_checkpoint": before_checkpoint,
                    "after_checkpoint": after_checkpoint,
                    "diff_truncated": False,
                },
            )

        client = TestClient(
            create_app(
                store=store,
                credential_verifier=_verifier("vault_primary"),
                summary_regenerator=regenerate,
            )
        )
        upload = _create_upload(client)
        _complete_upload(client, upload)
        created = _create_job(client, upload["upload"]["upload_id"])
        job_id = created["job"]["job_id"]
        _advance_to_processed_review_job(store, job_id)
        current = store.get_job(job_id)
        corrected = client.post(
            f"/v1/jobs/{job_id}/segment-review",
            headers={**AUTHORIZATION, "Idempotency-Key": "regenerate-source-edit"},
            json={
                "expected_revision": current.revision,
                "segment_id": "seg_00000001",
                "before_text": "这是合成逐字稿。",
                "after_text": "这是校订后的合成逐字稿。",
                "before_speaker_id": "speaker_0",
                "after_speaker_id": "speaker_0",
                "author": "obsidian-user",
            },
        )
        revised_job = corrected.json()["job"]
        before = client.get(
            f"/v1/jobs/{job_id}/summary-revisions",
            headers=AUTHORIZATION,
        )
        generated = client.post(
            f"/v1/jobs/{job_id}/summary-revisions",
            headers={**AUTHORIZATION, "Idempotency-Key": "regenerate-summary-api"},
            json={"expected_revision": revised_job["revision"]},
        )
        replayed = client.post(
            f"/v1/jobs/{job_id}/summary-revisions",
            headers={**AUTHORIZATION, "Idempotency-Key": "regenerate-summary-api-replay"},
            json={"expected_revision": revised_job["revision"]},
        )
        after = client.get(
            f"/v1/jobs/{job_id}/summary-revisions",
            headers=AUTHORIZATION,
        )

    assert corrected.status_code == 200, corrected.text
    assert before.json()["can_regenerate"] is True
    assert generated.status_code == 200, generated.text
    assert generated.json()["applied"] is True
    assert generated.json()["revision"]["status"] == "pending"
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["applied"] is False
    assert calls == [job_id]
    assert after.json()["can_regenerate"] is False


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

    response = TestClient(test_app, raise_server_exceptions=False).get("/test-unexpected-error")

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


def test_publication_status_claim_release_and_acknowledgement(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        upload_chunk_size_bytes=4,
        source_probe=_probe,
    ) as store:
        client = TestClient(create_app(store=store, credential_verifier=_verifier("vault_primary")))
        upload = _create_upload(client)
        _complete_upload(client, upload)
        created = _create_job(client, upload["upload"]["upload_id"])
        job_id = created["job"]["job_id"]
        _advance_to_processed_review_job(store, job_id)
        manifest_sha256 = _install_publication_package(store, job_id)

        status = client.get(
            f"/v1/jobs/{job_id}/publication",
            headers=AUTHORIZATION,
            params={"output_root": "Work/Speech Notes"},
        )
        assert status.status_code == 200, status.text
        status_json = status.json()
        assert status_json["suggested_target_relative_path"] == (
            "Work/Speech Notes/2026/08/2026-08-03-合成-发布-记录--sp_api_publication"
        )
        assert status_json["manifest_sha256"] == manifest_sha256
        assert status_json["artifact_count"] == 7
        assert status_json["active_lease"] is None
        assert status_json["receipt"] is None

        claim = client.post(
            f"/v1/jobs/{job_id}/publication-claims",
            headers=AUTHORIZATION,
            json={
                "expected_revision": status_json["job"]["revision"],
                "target_relative_path": status_json["suggested_target_relative_path"],
                "manifest_sha256": manifest_sha256,
                "lease_seconds": 120,
            },
        )
        assert claim.status_code == 200, claim.text
        claim_json = claim.json()
        assert claim_json["job"]["state"] == "publishing"
        assert claim_json["lease"]["owned_by_caller"] is True

        active = client.get(
            f"/v1/jobs/{job_id}/publication",
            headers=AUTHORIZATION,
            params={"output_root": "Work/Speech Notes"},
        )
        assert active.status_code == 200
        assert active.json()["active_lease"]["lease_id"] == claim_json["lease"]["lease_id"]

        released = client.post(
            f"/v1/jobs/{job_id}/publication-claims/release",
            headers=AUTHORIZATION,
            json={"lease_id": claim_json["lease"]["lease_id"]},
        )
        assert released.status_code == 200, released.text
        assert released.json()["job"]["state"] == "processed"

        second_claim = client.post(
            f"/v1/jobs/{job_id}/publication-claims",
            headers=AUTHORIZATION,
            json={
                "expected_revision": released.json()["job"]["revision"],
                "target_relative_path": status_json["suggested_target_relative_path"],
                "manifest_sha256": manifest_sha256,
                "lease_seconds": 120,
            },
        )
        assert second_claim.status_code == 200, second_claim.text
        acknowledged = client.post(
            f"/v1/jobs/{job_id}/publication-acknowledgements",
            headers=AUTHORIZATION,
            json={
                "lease_id": second_claim.json()["lease"]["lease_id"],
                "manifest_sha256": manifest_sha256,
            },
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["job"]["state"] == "published"
        assert acknowledged.json()["receipt"]["target_relative_path"] == (
            status_json["suggested_target_relative_path"]
        )

        final_status = client.get(
            f"/v1/jobs/{job_id}/publication",
            headers=AUTHORIZATION,
            params={"output_root": "Work/Speech Notes"},
        )

    assert final_status.status_code == 200
    assert final_status.json()["active_lease"] is None
    assert final_status.json()["receipt"]["manifest_sha256"] == manifest_sha256


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
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
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
                "package_relative_path": package_dir.relative_to(store.data_directory).as_posix(),
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
