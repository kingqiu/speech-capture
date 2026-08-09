"""Whole-version summary review tests."""

import hashlib
from types import SimpleNamespace

import pytest

from speech_capture_worker.artifact_generation import ARTIFACT_STAGE, NOTE_MARKDOWN
from speech_capture_worker.corrections import CorrectionField, corrections_sha256
from speech_capture_worker.domain import JobState, UploadCreateRequest
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.structuring_execution import (
    STRUCTURING_CHECKPOINT_KEY,
    STRUCTURING_STAGE,
    SUMMARY_REVISION_SCHEMA_VERSION,
    SUMMARY_REVISION_STAGE,
)
from speech_capture_worker.summary_revisions import (
    SummaryRevisionStatus,
    decide_summary_revision,
    list_summary_revisions,
    regenerate_summary_revision,
)
from speech_capture_worker.transcript import SpeakerLabelStatus, TranscriptOutcome


def _processed_job(store: JobStore):
    source = b"synthetic-summary-audio"
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_summary_review",
            source_display_name="synthetic.wav",
            source_sha256=hashlib.sha256(source).hexdigest(),
            source_size_bytes=len(source),
            media_type="audio/wav",
        ),
        idempotency_key="summary-review-upload",
    )
    store.put_upload_part(
        upload.upload_id,
        part_number=1,
        content=source,
        part_sha256=hashlib.sha256(source).hexdigest(),
    )
    upload, _ = store.complete_upload(upload.upload_id)
    job, _ = store.create_job_from_upload(
        upload.upload_id,
        idempotency_key="summary-review-job",
        options={},
    )
    current = store.claim_job_for_processing(
        job.job_id,
        expected_revision=job.revision,
    )
    for state in (
        JobState.TRANSCRIBING,
        JobState.ALIGNING,
        JobState.DIARIZING,
        JobState.STRUCTURING,
        JobState.QUALITY_CHECK,
        JobState.PROCESSED,
    ):
        current = store.transition_job(
            job.job_id,
            state,
            expected_revision=current.revision,
        )
        if state is JobState.TRANSCRIBING:
            store.commit_transcript_segment(
                job.job_id,
                commit_key="summary-review-segment",
                start_ms=0,
                end_ms=1_000,
                outcome=TranscriptOutcome.TRANSCRIBED,
                text="合成原始文字。",
                language="zh",
                speaker_id="speaker_0",
                speaker_label_status=SpeakerLabelStatus.ANONYMOUS,
            )
    return store.get_job(job.job_id)


def _probe(_path) -> MediaProbeResult:
    return MediaProbeResult(
        duration_seconds=1.0,
        audio_stream_count=1,
        format_name="synthetic-wav",
    )


def _seed_revision(store: JobStore, job_id: str) -> str:
    before_checkpoint = {"raw_sha256": "b" * 64, "raw_relative_path": "before.json"}
    after_checkpoint = {"raw_sha256": "c" * 64, "raw_relative_path": "after.json"}
    store.put_checkpoint(
        job_id,
        stage=STRUCTURING_STAGE,
        checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
        payload=after_checkpoint,
    )
    revision_key = "revision_00000002"
    store.put_checkpoint(
        job_id,
        stage=SUMMARY_REVISION_STAGE,
        checkpoint_key=revision_key,
        payload={
            "schema_version": SUMMARY_REVISION_SCHEMA_VERSION,
            "structuring_generation": 2,
            "candidate_version": 2,
            "corrections_sha256": "d" * 64,
            "text_correction_count": 2,
            "speaker_rename_count": 1,
            "before_sha256": "e" * 64,
            "after_sha256": "f" * 64,
            "before_document": {"summary": {"text": "旧版摘要", "evidence": ["seg_00000001"]}},
            "after_document": {"summary": {"text": "候选摘要", "evidence": ["seg_00000001"]}},
            "before_checkpoint": before_checkpoint,
            "after_checkpoint": after_checkpoint,
            "changed": True,
            "diff": "synthetic diff",
            "diff_truncated": False,
        },
    )
    package = store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE)
    package.mkdir(parents=True, exist_ok=True)
    (package / NOTE_MARKDOWN).write_text("# 合成笔记\n\n## 我的补充\n\n人工内容。\n", "utf-8")
    return revision_key


def test_reject_restores_prior_structuring_evidence_and_keeps_manual_section(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3", source_probe=_probe) as store:
        job = _processed_job(store)
        revision_key = _seed_revision(store, job.job_id)

        result = decide_summary_revision(
            store,
            job.job_id,
            revision_key=revision_key,
            decision=SummaryRevisionStatus.REJECTED,
            expected_revision=job.revision,
            idempotency_key="reject-summary-candidate",
        )
        replay = decide_summary_revision(
            store,
            job.job_id,
            revision_key=revision_key,
            decision=SummaryRevisionStatus.REJECTED,
            expected_revision=job.revision,
            idempotency_key="reject-summary-candidate-replay",
        )
        current = store.list_checkpoints(job.job_id, stage=STRUCTURING_STAGE)[0]
        collection = list_summary_revisions(store, job.job_id)

    assert result.applied is True
    assert replay.applied is False
    assert current.payload["raw_sha256"] == "b" * 64
    assert collection.current_version == 1
    assert collection.revisions[0].status is SummaryRevisionStatus.REJECTED
    assert collection.manual_section_markdown == "## 我的补充\n\n人工内容。\n"


def test_accept_regenerates_artifacts_before_recording_decision(tmp_path, monkeypatch) -> None:
    generated: list[tuple[str, bool]] = []

    class FakeArtifactGenerator:
        def __init__(self, _store):
            pass

        def generate(self, job_id: str, *, force: bool = False):
            generated.append((job_id, force))
            return SimpleNamespace(manifest_sha256="9" * 64)

    monkeypatch.setattr(
        "speech_capture_worker.summary_revisions.ArtifactGenerator",
        FakeArtifactGenerator,
    )
    with JobStore(tmp_path / "worker.sqlite3", source_probe=_probe) as store:
        job = _processed_job(store)
        revision_key = _seed_revision(store, job.job_id)

        result = decide_summary_revision(
            store,
            job.job_id,
            revision_key=revision_key,
            decision=SummaryRevisionStatus.ACCEPTED,
            expected_revision=job.revision,
            idempotency_key="accept-summary-candidate",
        )
        collection = list_summary_revisions(store, job.job_id)
        with pytest.raises(InvalidJobRequest, match="different decision"):
            decide_summary_revision(
                store,
                job.job_id,
                revision_key=revision_key,
                decision=SummaryRevisionStatus.REJECTED,
                expected_revision=job.revision,
                idempotency_key="change-summary-decision",
            )

    assert result.applied is True
    assert generated == [(job.job_id, True)]
    assert collection.current_version == 2
    assert collection.revisions[0].status is SummaryRevisionStatus.ACCEPTED
    assert collection.revisions[0].artifact_manifest_sha256 == "9" * 64


def test_regeneration_requires_new_corrections_and_replays_pending_candidate(tmp_path) -> None:
    calls: list[str] = []
    with JobStore(tmp_path / "worker.sqlite3", source_probe=_probe) as store:
        job = _processed_job(store)
        store.append_correction(
            job.job_id,
            field=CorrectionField.TRANSCRIPT_TEXT,
            target_id="seg_00000001",
            before="合成原始文字。",
            after="合成校订文字。",
            author="test-user",
            idempotency_key="summary-regeneration-correction",
            expected_revision=job.revision,
        )
        revised_job = store.get_job(job.job_id)
        assert list_summary_revisions(store, job.job_id).can_regenerate is True

        def regenerate(job_id: str) -> None:
            calls.append(job_id)
            revision_key = _seed_revision(store, job_id)
            checkpoint = next(
                item
                for item in store.list_checkpoints(job_id, stage=SUMMARY_REVISION_STAGE)
                if item.checkpoint_key == revision_key
            )
            payload = dict(checkpoint.payload)
            payload["corrections_sha256"] = corrections_sha256(store.list_corrections(job_id))
            store.put_checkpoint(
                job_id,
                stage=SUMMARY_REVISION_STAGE,
                checkpoint_key=revision_key,
                payload=payload,
            )

        result = regenerate_summary_revision(
            store,
            job.job_id,
            expected_revision=revised_job.revision,
            idempotency_key="regenerate-summary-candidate",
            regenerate=regenerate,
        )
        replay = regenerate_summary_revision(
            store,
            job.job_id,
            expected_revision=revised_job.revision,
            idempotency_key="regenerate-summary-candidate-replay",
            regenerate=regenerate,
        )
        collection = list_summary_revisions(store, job.job_id)

    assert calls == [job.job_id]
    assert result.applied is True
    assert replay.applied is False
    assert result.revision.status is SummaryRevisionStatus.PENDING
    assert collection.can_regenerate is False
