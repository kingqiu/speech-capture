import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import replace

import pytest

from speech_capture_worker.domain import JobCreateRequest, JobState, UploadCreateRequest
from speech_capture_worker.errors import (
    InvalidJobRequest,
    TranscriptConflict,
    TranscriptRevisionConflict,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.transcript import (
    DiarizationStatus,
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
    chronological_segments,
)

SOURCE = b"synthetic-progressive-audio"


def source_probe(_):
    return MediaProbeResult(
        duration_seconds=120.0,
        audio_stream_count=1,
        format_name="wav",
    )


def create_transcribing_job(store: JobStore, *, suffix: str = "one"):
    checksum = hashlib.sha256(SOURCE).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"meeting-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(SOURCE),
            media_type="audio/wav",
        ),
        idempotency_key=f"upload-{suffix}",
    )
    store.put_upload_part(
        upload.upload_id,
        part_number=1,
        content=SOURCE,
        part_sha256=checksum,
    )
    store.complete_upload(upload.upload_id)
    job, _ = store.create_job_from_upload(
        upload.upload_id,
        idempotency_key=f"job-{suffix}",
    )
    preprocessing = store.claim_job_for_processing(
        job.job_id,
        expected_revision=job.revision,
    )
    return store.transition_job(
        job.job_id,
        JobState.TRANSCRIBING,
        expected_revision=preprocessing.revision,
    )


def commit_text(
    store: JobStore,
    job_id: str,
    *,
    key: str,
    start_ms: int,
    end_ms: int,
    text: str,
):
    return store.commit_transcript_segment(
        job_id,
        commit_key=key,
        start_ms=start_ms,
        end_ms=end_ms,
        outcome=TranscriptOutcome.TRANSCRIBED,
        text=text,
        language="zh",
    )


def test_chronological_segments_separates_reading_order_from_append_order(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store, suffix="chronological")
        first, _ = commit_text(
            store,
            job.job_id,
            key="first",
            start_ms=0,
            end_ms=1000,
            text="第一段",
        )

    later = replace(
        first,
        segment_sequence=2,
        segment_id="seg_00000002",
        commit_key="later",
        start_ms=5000,
        end_ms=6000,
    )
    recovered_gap = replace(
        first,
        segment_sequence=3,
        segment_id="seg_00000003",
        commit_key="recovered_gap",
        start_ms=2500,
        end_ms=3000,
    )
    appended = [first, later, recovered_gap]

    ordered = chronological_segments(appended)

    assert [segment.segment_id for segment in ordered] == [
        "seg_00000001",
        "seg_00000003",
        "seg_00000002",
    ]
    assert [segment.segment_id for segment in appended] == [
        "seg_00000001",
        "seg_00000002",
        "seg_00000003",
    ]


def test_provisional_tail_is_revision_guarded_idempotent_and_private(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store)
        first, first_changed = store.put_provisional_transcript(
            job.job_id,
            expected_generation=0,
            start_ms=0,
            end_ms=2500,
            text="这是一段不会进入事件正文的临时转写",
            language="zh",
        )
        repeated, repeated_changed = store.put_provisional_transcript(
            job.job_id,
            expected_generation=0,
            start_ms=0,
            end_ms=2500,
            text="这是一段不会进入事件正文的临时转写",
            language="zh",
        )

        with pytest.raises(TranscriptRevisionConflict):
            store.put_provisional_transcript(
                job.job_id,
                expected_generation=0,
                start_ms=0,
                end_ms=3000,
                text="过期写入",
            )

        snapshot = store.get_job_snapshot(job.job_id)
        updates, _ = store.list_job_updates(job.job_id)

    assert first_changed is True
    assert repeated_changed is False
    assert first.generation == repeated.generation == 1
    assert snapshot.provisional is not None
    assert snapshot.provisional.text.startswith("这是一段")
    assert "不会进入事件正文" not in json.dumps(
        [update.to_dict() for update in updates],
        ensure_ascii=False,
    )
    assert updates[-1].payload["text_length"] == len(first.text)


def test_stable_segment_commit_is_idempotent_and_clears_overlapping_tail(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store)
        store.put_provisional_transcript(
            job.job_id,
            expected_generation=0,
            start_ms=0,
            end_ms=5000,
            text="正在识别",
        )
        first, first_created = commit_text(
            store,
            job.job_id,
            key="chunk_000001_segment_0001",
            start_ms=0,
            end_ms=4500,
            text="我们先确认今天的迁移计划。",
        )
        repeated, repeated_created = commit_text(
            store,
            job.job_id,
            key="chunk_000001_segment_0001",
            start_ms=0,
            end_ms=4500,
            text="我们先确认今天的迁移计划。",
        )
        snapshot = store.get_job_snapshot(job.job_id)

        with pytest.raises(TranscriptConflict):
            commit_text(
                store,
                job.job_id,
                key="chunk_000001_segment_0001",
                start_ms=0,
                end_ms=4500,
                text="同一个键不能换成别的文字。",
            )

    assert first_created is True
    assert repeated_created is False
    assert repeated.segment_id == first.segment_id == "seg_00000001"
    assert snapshot.provisional is None
    assert snapshot.stable_segments[0].text == "我们先确认今天的迁移计划。"


def test_alignment_cannot_start_with_an_unresolved_provisional_tail(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store)
        store.put_provisional_transcript(
            job.job_id,
            expected_generation=0,
            start_ms=0,
            end_ms=2500,
            text="尚未决定句子边界",
        )

        with pytest.raises(InvalidJobRequest, match="committed or cleared"):
            store.transition_job(
                job.job_id,
                JobState.ALIGNING,
                expected_revision=job.revision,
            )

        cleared = store.clear_provisional_transcript(
            job.job_id,
            expected_generation=1,
        )
        repeated_clear = store.clear_provisional_transcript(
            job.job_id,
            expected_generation=1,
        )
        aligning = store.transition_job(
            job.job_id,
            JobState.ALIGNING,
            expected_revision=job.revision,
        )

    assert cleared is True
    assert repeated_clear is False
    assert aligning.state is JobState.ALIGNING


def test_timeline_rejects_overlap_and_records_explicit_non_text_outcomes(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store)
        commit_text(
            store,
            job.job_id,
            key="segment_one",
            start_ms=0,
            end_ms=4000,
            text="第一段",
        )

        with pytest.raises(TranscriptConflict):
            commit_text(
                store,
                job.job_id,
                key="overlap",
                start_ms=3500,
                end_ms=5000,
                text="重叠",
            )

        silence, _ = store.commit_transcript_segment(
            job.job_id,
            commit_key="silence",
            start_ms=4000,
            end_ms=7000,
            outcome=TranscriptOutcome.NON_SPEECH,
            speaker_label_status=SpeakerLabelStatus.UNAVAILABLE,
        )
        failed, _ = store.commit_transcript_segment(
            job.job_id,
            commit_key="failed_range",
            start_ms=7000,
            end_ms=9000,
            outcome=TranscriptOutcome.FAILED,
            speaker_label_status=SpeakerLabelStatus.UNAVAILABLE,
            error_code="ASR_CHUNK_FAILED",
        )

        with pytest.raises(InvalidJobRequest):
            store.commit_transcript_segment(
                job.job_id,
                commit_key="invalid_failed",
                start_ms=9000,
                end_ms=10_000,
                outcome=TranscriptOutcome.FAILED,
                speaker_label_status=SpeakerLabelStatus.UNAVAILABLE,
            )

    assert silence.text is None
    assert silence.outcome is TranscriptOutcome.NON_SPEECH
    assert failed.error_code == "ASR_CHUNK_FAILED"


def test_alignment_and_speaker_updates_preserve_stable_text(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store)
        segment, _ = commit_text(
            store,
            job.job_id,
            key="segment_one",
            start_ms=1000,
            end_ms=4000,
            text="请林工在周五前复核迁移方案。",
        )
        aligning = store.transition_job(
            job.job_id,
            JobState.ALIGNING,
            expected_revision=job.revision,
        )
        aligned = store.update_transcript_segment_metadata(
            job.job_id,
            segment.segment_id,
            expected_revision=segment.revision,
            start_ms=900,
            end_ms=4200,
            timing_status=TranscriptTimingStatus.ALIGNED,
        )
        store.transition_job(
            job.job_id,
            JobState.DIARIZING,
            expected_revision=aligning.revision,
        )
        attributed = store.update_transcript_segment_metadata(
            job.job_id,
            segment.segment_id,
            expected_revision=aligned.revision,
            speaker_id="speaker_02",
            speaker_label_status=SpeakerLabelStatus.ANONYMOUS,
        )
        cleared = store.update_transcript_segment_metadata(
            job.job_id,
            segment.segment_id,
            expected_revision=attributed.revision,
            speaker_id=None,
            speaker_label_status=SpeakerLabelStatus.UNAVAILABLE,
        )
        updates, _ = store.list_job_updates(job.job_id)

        with pytest.raises(TranscriptRevisionConflict):
            store.update_transcript_segment_metadata(
                job.job_id,
                segment.segment_id,
                expected_revision=1,
                speaker_id="speaker_03",
                speaker_label_status=SpeakerLabelStatus.ANONYMOUS,
            )

    assert cleared.text == segment.text
    assert cleared.revision == 4
    assert cleared.timing_status is TranscriptTimingStatus.ALIGNED
    assert cleared.speaker_id is None
    assert cleared.speaker_label_status is SpeakerLabelStatus.UNAVAILABLE
    assert updates[-1].event_type == "speaker.attribution_updated"
    assert segment.text not in json.dumps(
        [update.to_dict() for update in updates],
        ensure_ascii=False,
    )


def test_progress_is_monotonic_idempotent_and_survives_reopen(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database, source_probe=source_probe) as store:
        job = create_transcribing_job(store)
        first, first_changed = store.put_job_progress(
            job.job_id,
            processed_ms=12_000,
            stage_progress=0.1,
            elapsed_seconds=8,
            estimated_remaining_seconds=72,
        )
        repeated, repeated_changed = store.put_job_progress(
            job.job_id,
            processed_ms=12_000,
            stage_progress=0.1,
            elapsed_seconds=8,
            estimated_remaining_seconds=72,
        )
        second, second_changed = store.put_job_progress(
            job.job_id,
            processed_ms=24_000,
            stage_progress=0.2,
            elapsed_seconds=16,
            estimated_remaining_seconds=64,
            diarization_status=DiarizationStatus.NOT_STARTED,
        )

        with pytest.raises(InvalidJobRequest):
            store.put_job_progress(
                job.job_id,
                processed_ms=23_000,
                stage_progress=0.3,
                elapsed_seconds=17,
            )
        with pytest.raises(InvalidJobRequest):
            store.put_job_progress(
                job.job_id,
                processed_ms=25_000,
                stage_progress=math.nan,
                elapsed_seconds=18,
            )

    with JobStore(database) as reopened:
        snapshot = reopened.get_job_snapshot(job.job_id)

    assert first_changed is True
    assert repeated_changed is False
    assert second_changed is True
    assert first.generation == repeated.generation == 1
    assert second.generation == 2
    assert snapshot.progress is not None
    assert snapshot.progress.processed_ms == 24_000
    assert snapshot.progress.duration_ms == 120_000


def test_segments_must_commit_in_timeline_order(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store)
        commit_text(
            store,
            job.job_id,
            key="later",
            start_ms=10_000,
            end_ms=12_000,
            text="后面的片段",
        )

        with pytest.raises(TranscriptConflict, match="timeline order"):
            commit_text(
                store,
                job.job_id,
                key="earlier",
                start_ms=0,
                end_ms=5000,
                text="不能后补到已提交片段之前",
            )


def test_concurrent_identical_segment_commit_creates_one_stable_record(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database, source_probe=source_probe) as bootstrap:
        job = create_transcribing_job(bootstrap)

    first_store = JobStore(database)
    second_store = JobStore(database)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def commit(store):
        try:
            barrier.wait(timeout=5)
            results.append(
                commit_text(
                    store,
                    job.job_id,
                    key="concurrent_segment",
                    start_ms=0,
                    end_ms=4000,
                    text="并发重试只能形成一个稳定片段。",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=commit, args=(first_store,))
    second = threading.Thread(target=commit, args=(second_store,))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    snapshot = first_store.get_job_snapshot(job.job_id)
    first_store.close()
    second_store.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert sum(created for _, created in results) == 1
    assert {segment.segment_id for segment, _ in results} == {"seg_00000001"}
    assert len(snapshot.stable_segments) == 1


def test_snapshot_segment_pages_and_update_cursor_reconstruct_preview(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe,
    ) as store:
        job = create_transcribing_job(store)
        for index in range(3):
            commit_text(
                store,
                job.job_id,
                key=f"segment_{index}",
                start_ms=index * 5000,
                end_ms=(index + 1) * 5000,
                text=f"第 {index + 1} 段",
            )

        first_page = store.get_job_snapshot(job.job_id, segment_limit=2)
        second_page = store.get_job_snapshot(
            job.job_id,
            after_segment_sequence=first_page.next_after_segment_sequence,
            segment_limit=2,
        )
        first_updates, has_more = store.list_job_updates(job.job_id, limit=2)
        next_updates, _ = store.list_job_updates(
            job.job_id,
            after_sequence=first_updates[-1].sequence,
            limit=100,
        )

    assert [segment.segment_sequence for segment in first_page.stable_segments] == [1, 2]
    assert first_page.has_more_segments is True
    assert [segment.segment_sequence for segment in second_page.stable_segments] == [3]
    assert second_page.has_more_segments is False
    assert has_more is True
    assert all(
        later.sequence > first_updates[-1].sequence
        for later in next_updates
    )
    assert first_page.latest_event_sequence >= next_updates[-1].sequence


def test_restart_requeues_job_but_preserves_preview_data(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database, source_probe=source_probe) as store:
        job = create_transcribing_job(store)
        commit_text(
            store,
            job.job_id,
            key="segment_one",
            start_ms=0,
            end_ms=4000,
            text="已经稳定保存的内容",
        )
        store.put_provisional_transcript(
            job.job_id,
            expected_generation=0,
            start_ms=4000,
            end_ms=6000,
            text="尚未稳定的尾段",
        )

    with JobStore(database) as restarted:
        recovered = restarted.recover_interrupted_jobs()
        snapshot = restarted.get_job_snapshot(job.job_id)

    assert recovered[0].state is JobState.QUEUED
    assert snapshot.stable_segments[0].text == "已经稳定保存的内容"
    assert snapshot.provisional is not None
    assert snapshot.provisional.text == "尚未稳定的尾段"
    assert snapshot.job.state is JobState.QUEUED


def test_schema_three_migration_backfills_state_events_into_update_feed(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(database) as store:
        job, _ = store.create_job(
            JobCreateRequest(
                vault_id="vault_primary",
                source_display_name="legacy.m4a",
                source_sha256="a" * 64,
                source_size_bytes=1024,
            ),
            idempotency_key="legacy-job",
        )

    connection = sqlite3.connect(database)
    connection.executescript(
        """
        DROP TABLE job_action_requests;
        DROP TABLE publication_receipts;
        DROP TABLE publication_leases;
        DROP TABLE corrections;
        DROP TABLE asr_attempts;
        DROP TABLE job_updates;
        DROP TABLE job_progress;
        DROP TABLE provisional_transcripts;
        DROP TABLE transcript_segments;
        PRAGMA user_version = 3;
        """
    )
    connection.close()

    with JobStore(database) as migrated:
        updates, has_more = migrated.list_job_updates(job.job_id)
        assert [update.event_type for update in updates] == ["job.created"]
        assert has_more is False
        assert migrated.quick_check() is True
