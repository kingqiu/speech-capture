"""Content-type classification and evidence-linked extraction tests."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import wave

import numpy as np
import pytest

from speech_capture_worker.alignment import (
    AlignmentFinalizationOutcome,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor, AsrRunOutcome
from speech_capture_worker.corrections import CorrectionField
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.errors import InvalidJobRequest, StructuringFailed
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.note_prompt_profiles import synthesis_guidance
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceIssue,
    ResourceReport,
)
from speech_capture_worker.structuring_execution import (
    ContentType,
    OllamaStructuringEngine,
    StructuringExecutor,
    StructuringOutcome,
    _document_json_schema,
    _validate_document,
)


def wav_bytes(*, duration_seconds: float) -> bytes:
    sample_rate = 16_000
    frame_count = round(duration_seconds * sample_rate)
    time = np.arange(frame_count, dtype=np.float64) / sample_rate
    samples = (np.sin(2 * np.pi * 330 * time) * 3000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())
    return output.getvalue()


def source_probe_for(duration_seconds: float):
    def probe(_):
        return MediaProbeResult(
            duration_seconds=duration_seconds,
            audio_stream_count=1,
            format_name="wav",
        )

    return probe


def resource_report(status: ResourceStatus) -> ResourceReport:
    issues = ()
    if status is ResourceStatus.BLOCKED:
        issues = (
            ResourceIssue(
                code="MEMORY_PRESSURE_BLOCKED",
                status=ResourceStatus.BLOCKED,
                message="Memory pressure is too high.",
                action="Close large applications, then resume.",
            ),
        )
    return ResourceReport(
        status=status,
        estimated_required_bytes=256 * 1024 * 1024,
        disk_reserve_bytes=20 * GIB,
        disk_free_after_bytes=40 * GIB,
        disk=DiskSnapshot(total_bytes=256 * GIB, free_bytes=80 * GIB),
        memory=MemorySnapshot(
            total_bytes=32 * GIB,
            available_bytes=20 * GIB,
            used_percent=40,
            swap_used_bytes=0,
        ),
        issues=issues,
    )


def preflight(status: ResourceStatus = ResourceStatus.READY):
    report = resource_report(status)

    def check(*_, **__):
        return report

    return check


class FakeAsrEngine:
    model_id = "fake/local-asr"

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        duration = len(audio) / sample_rate
        text = "这是结构提炼测试的稳定文字。"
        return {
            "text": text,
            "language": "Chinese",
            "segments": [{"text": text, "start": 0.0, "end": duration}],
            "chunks": [
                {
                    "text": text,
                    "start": 0.0,
                    "end": duration,
                    "chunk_index": 0,
                    "finish_reason": "stop",
                    "truncated": False,
                }
            ],
            "finish_reason": "stop",
            "truncated": False,
        }


class FakeStructuringEngine:
    model_id = "fake/structuring"

    def __init__(
        self,
        *,
        classification=None,
        findings=None,
        error=None,
        polish_error=None,
        polish_replacements=None,
        coverage_sections=None,
        max_extract_segments=None,
        summary_from_transcript=False,
    ):
        self.classification = classification or {
            "type": "meeting",
            "traits": ["multi_speaker", "action_oriented"],
            "confidence": 0.92,
        }
        self.findings = findings or []
        self.error = error
        self.polish_error = polish_error
        self.polish_replacements = polish_replacements or {}
        self.coverage_sections = coverage_sections or []
        self.max_extract_segments = max_extract_segments
        self.summary_from_transcript = summary_from_transcript
        self.classify_calls = 0
        self.extract_calls = 0
        self.synthesize_calls = 0
        self.speaker_supplement_calls = 0
        self.polish_calls = 0
        self.coverage_calls = 0
        self.interview_repair_calls = 0
        self.voice_memo_repair_calls = 0
        self.extract_inputs = []
        self.synthesize_inputs = []
        self.recording_context = None

    def set_recording_context(self, context):
        self.recording_context = context

    def classify(self, segments, *, speaker_count):
        self.classify_calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.classification)

    def extract_batch(self, segments, *, content_type):
        self.extract_calls += 1
        self.extract_inputs.append([dict(item) for item in segments])
        if self.error is not None:
            raise self.error
        if self.max_extract_segments is not None and len(segments) > self.max_extract_segments:
            raise ValueError("batch is too large")
        return [dict(finding) for finding in self.findings]

    def synthesize_document(self, findings, segments, *, content_type):
        self.synthesize_calls += 1
        self.synthesize_inputs.append([dict(item) for item in segments])
        if self.error is not None:
            raise self.error
        evidence = list(findings[0]["evidence"]) if findings else [segments[0]["segment_id"]]
        text = (
            segments[0]["text"]
            if self.summary_from_transcript
            else findings[0]["text"]
            if findings
            else "从完整逐字稿生成的笔记。"
        )
        document = {
            "title": "结构提炼测试会议",
            "summary": {"text": text, "evidence": evidence},
            "context": [
                {
                    "kind": "purpose",
                    "title": "会议目的",
                    "text": text,
                    "evidence": evidence,
                },
                {
                    "kind": "background",
                    "title": "会议背景",
                    "text": text,
                    "evidence": evidence,
                },
            ],
            "highlights": [{"text": f"{text}{index}", "evidence": evidence} for index in range(5)],
            "topics": [
                {
                    "title": f"主要议题{index}",
                    "summary": text,
                    "details": [{"text": text, "evidence": evidence}],
                    "evidence": evidence,
                }
                for index in range(5)
            ],
            "timeline_sections": [
                {
                    "title": "结构提炼过程",
                    "summary": text,
                    "details": [text],
                    "start_segment_id": segments[0]["segment_id"],
                    "end_segment_id": segments[-1]["segment_id"],
                }
            ],
            "speaker_summaries": [],
            "decisions": [],
            "actions": [],
            "risks": [],
            "open_questions": [],
        }
        if content_type is not ContentType.MEETING:
            scene_kind = {
                ContentType.INTERVIEW: "viewpoint",
                ContentType.COURSE: "concept",
                ContentType.SPEECH: "argument",
                ContentType.VOICE_MEMO: "idea",
                ContentType.GENERIC: "theme",
            }[content_type]
            document["scene_sections"] = [
                {
                    "kind": scene_kind,
                    "title": "已有核心论点",
                    "summary": text,
                    "details": [{"text": text, "evidence": evidence}],
                    "evidence": evidence,
                }
            ]
        return document

    def synthesize_speaker_summaries(self, segments, *, speaker_ids, content_type):
        self.speaker_supplement_calls += 1
        return [
            {
                "speaker_id": speaker_id,
                "display_name": "",
                "affiliation": "",
                "role": "",
                "summary": f"{speaker_id} 的核心观点。",
                "evidence": [
                    next(
                        item["segment_id"] for item in segments if item["speaker_id"] == speaker_id
                    )
                ],
            }
            for speaker_id in speaker_ids
        ]

    def synthesize_missing_scene_sections(self, document, findings, segments, *, content_type):
        self.coverage_calls += 1
        return [dict(section) for section in self.coverage_sections]

    def refine_interview_document(self, document, segments):
        self.interview_repair_calls += 1
        return dict(document)

    def refine_voice_memo_document(self, document, segments):
        self.voice_memo_repair_calls += 1
        return dict(document)

    def synthesize_discussion_threads(self, segments, *, content_type):
        return []

    def reconcile_decisions(self, document, segments, *, content_type):
        return list(document.get("decisions", []))

    def polish_transcript_batch(self, segments):
        self.polish_calls += 1
        if self.error is not None:
            raise self.error
        if self.polish_error is not None:
            raise self.polish_error
        results = []
        for item in segments:
            text = item["text"] + "。"
            for old, new in self.polish_replacements.items():
                text = text.replace(old, new)
            results.append({"segment_id": item["segment_id"], "text": text})
        return results


def create_structuring_job(
    store: JobStore,
    *,
    duration_seconds: float,
    suffix: str,
    finalize: bool = True,
    content_type_override: str | None = None,
):
    content = wav_bytes(duration_seconds=duration_seconds)
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"structuring-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"structuring-upload-{suffix}",
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
        idempotency_key=f"structuring-job-{suffix}",
        content_type_override=content_type_override,
    )
    claimed = store.claim_job_for_processing(
        queued.job_id,
        expected_revision=queued.revision,
    )
    batch = AsrChunkExecutor(
        store,
        FakeAsrEngine(),
        boundary_preflight=preflight(),
    ).run_all(claimed.job_id)
    assert batch.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED
    finalized = TranscriptAlignmentFinalizer(store).finalize(claimed.job_id)
    assert finalized.outcome is AlignmentFinalizationOutcome.READY_FOR_DIARIZATION
    if not finalize:
        return finalized.job
    structuring = store.transition_job(
        claimed.job_id,
        JobState.STRUCTURING,
        expected_revision=finalized.job.revision,
        reason_code="test_enter_structuring",
    )
    return structuring


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_classifies_and_extracts_evidence_linked_findings(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="complete",
        )
        engine = FakeStructuringEngine(
            findings=[
                {
                    "kind": "action_item",
                    "text": "完成迁移计划。",
                    "evidence": [],
                    "confidence": 0.94,
                }
            ]
        )
        snapshot = store.get_job_snapshot(job.job_id)
        segment_id = snapshot.stable_segments[0].segment_id
        engine.findings[0]["evidence"] = [segment_id]
        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="structuring")
        evidence = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.checkpoint_key == "structuring_result"
        )
        raw = json.loads(
            (store.data_directory / evidence.payload["raw_relative_path"]).read_text("utf-8")
        )

        assert result.outcome is StructuringOutcome.COMPLETED
        assert result.job.state is JobState.QUALITY_CHECK
        assert result.content_type is ContentType.MEETING
        assert result.finding_count == 1
        assert result.unsupported_finding_count == 0
        assert result.batch_count >= 1
        assert engine.classify_calls == 1
        assert engine.extract_calls >= 1
        assert engine.synthesize_calls == 1
        assert engine.polish_calls >= 1
        assert all(item["text"].endswith("。") for batch in engine.extract_inputs for item in batch)
        assert len(engine.synthesize_inputs[0]) == len(
            [segment for segment in snapshot.stable_segments if segment.text]
        )
        assert all(item["segment_id"].startswith("s") for item in engine.synthesize_inputs[0])
        assert all(item["text"].endswith("。") for item in engine.synthesize_inputs[0])
        assert raw["prompt_version"] == "2026-08-02.9"
        assert raw["document"]["title"] == "结构提炼测试会议"
        assert sum(
            len(batch["transcript_edits"]) for batch in raw["transcript_edit_results"]
        ) == len(snapshot.stable_segments)
        assert any(checkpoint.checkpoint_key == "structuring_result" for checkpoint in checkpoints)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_confirmed_recording_context_corrects_only_derived_structuring_text(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="recording-context",
        )
        segment_id = store.get_job_snapshot(job.job_id).stable_segments[0].segment_id
        engine = FakeStructuringEngine(
            findings=[
                {
                    "kind": "topic",
                    "text": "聚一堂推进AI转型。",
                    "evidence": [segment_id],
                    "confidence": 0.9,
                }
            ],
            polish_replacements={"结构提炼测试": "聚一堂"},
        )
        StructuringExecutor(store, engine, boundary_preflight=preflight()).run(job.job_id)
        raw_segment_text = store.get_job_snapshot(job.job_id).stable_segments[0].text
        current = store.get_job(job.job_id)
        store.update_job_recording_context(
            job.job_id,
            context="正确公司名是聚衣堂",
            expected_revision=current.revision,
        )

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).apply_recording_context_corrections(job.job_id)
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        raw = json.loads(
            (store.data_directory / checkpoint.payload["raw_relative_path"]).read_text("utf-8")
        )

        assert result.outcome is StructuringOutcome.REGENERATED
        assert result.evidence_checkpoint_generation == 2
        assert checkpoint.payload["schema_version"] == "1.6.0"
        assert checkpoint.payload["recording_context_applied"] is True
        corrected_sections = json.dumps(
            {
                "batch_results": raw["batch_results"],
                "document": raw["document"],
                "transcript_edit_results": raw["transcript_edit_results"],
            },
            ensure_ascii=False,
        )
        assert "聚一堂" not in corrected_sections
        assert "聚衣堂" in corrected_sections
        assert len(raw["context_corrections"]) == 1
        assert raw["context_corrections"][0]["from"] == "聚一堂"
        assert raw["context_corrections"][0]["to"] == "聚衣堂"
        assert raw["context_corrections"][0]["occurrences"] >= 1
        assert store.get_job_snapshot(job.job_id).stable_segments[0].text == raw_segment_text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_user_content_type_override_controls_extraction_and_synthesis(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="content-type-override",
            content_type_override="speech",
        )
        engine = FakeStructuringEngine(
            classification={
                "type": "meeting",
                "traits": ["multi_speaker"],
                "confidence": 0.91,
            }
        )
        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        raw = json.loads(
            (store.data_directory / checkpoint.payload["raw_relative_path"]).read_text("utf-8")
        )

    assert result.content_type is ContentType.SPEECH
    assert checkpoint.payload["content_type_source"] == "user_override"
    assert checkpoint.payload["automatic_content_type"] == "meeting"
    assert raw["classification"]["type"] == "speech"
    assert raw["automatic_classification"]["type"] == "meeting"
    assert raw["classification_source"] == "user_override"
    assert raw["document"]["scene_sections"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_nonmeeting_synthesis_repairs_an_uncovered_late_case(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="scene-coverage-repair",
            content_type_override="speech",
        )
        segments = store.get_job_snapshot(job.job_id).stable_segments
        first_segment_id = segments[0].segment_id
        last_segment_id = segments[-1].segment_id
        engine = FakeStructuringEngine(
            findings=[
                {
                    "kind": "topic",
                    "text": "数据库管理案例。",
                    "evidence": [first_segment_id],
                    "confidence": 0.9,
                },
                {
                    "kind": "topic",
                    "text": "机房管理案例。",
                    "evidence": [last_segment_id],
                    "confidence": 0.91,
                },
            ],
            coverage_sections=[
                {
                    "kind": "example",
                    "title": "机房管理案例",
                    "summary": "后半段介绍了独立的机房管理实践。",
                    "details": [
                        {
                            "text": "该案例不能只停留在摘要。",
                            "evidence": [last_segment_id],
                        }
                    ],
                    "evidence": [last_segment_id],
                }
            ],
        )
        StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        raw = json.loads(
            (store.data_directory / checkpoint.payload["raw_relative_path"]).read_text("utf-8")
        )

    assert engine.coverage_calls == 1
    assert [section["title"] for section in raw["document"]["scene_sections"]] == [
        "已有核心论点",
        "机房管理案例",
    ]
    assert raw["scene_coverage_repair_version"] == "2026-08-02.4"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_voice_memo_runs_versioned_quality_repair(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="voice-memo-quality-repair",
            content_type_override="voice_memo",
        )
        segment_id = store.get_job_snapshot(job.job_id).stable_segments[0].segment_id
        engine = FakeStructuringEngine(
            findings=[
                {
                    "kind": "idea",
                    "text": "先整理真实需求。",
                    "evidence": [segment_id],
                    "confidence": 0.9,
                }
            ]
        )
        StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        raw = json.loads(
            (store.data_directory / checkpoint.payload["raw_relative_path"]).read_text("utf-8")
        )

    assert engine.voice_memo_repair_calls == 1
    assert raw["voice_memo_quality_repair_version"] == "2026-08-02.2"
    assert checkpoint.payload["voice_memo_quality_repair_version"] == "2026-08-02.2"


def test_document_recovery_uses_only_matching_validated_artifact(tmp_path) -> None:
    with JobStore(tmp_path / "worker.sqlite3") as store:
        executor = StructuringExecutor(
            store,
            FakeStructuringEngine(),
            boundary_preflight=preflight(),
        )
        job_id = "job_artifact_recovery"
        artifact_dir = store.data_directory / "jobs" / job_id / "artifacts"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "speech-record.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "content": {"type": "voice_memo"},
                    "document": {"title": "已验证个人笔记", "chapters": []},
                }
            ),
            "utf-8",
        )

        recovered = executor._load_artifact_document_fallback(
            job_id,
            content_type=ContentType.VOICE_MEMO,
        )
        mismatched = executor._load_artifact_document_fallback(
            job_id,
            content_type=ContentType.INTERVIEW,
        )

    assert recovered == {"title": "已验证个人笔记"}
    assert mismatched is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_is_idempotent_after_restart(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="replay",
        )
        engine = FakeStructuringEngine(
            findings=[
                {
                    "kind": "topic",
                    "text": "平台规划。",
                    "evidence": [],
                    "confidence": 0.8,
                }
            ]
        )
        snapshot = store.get_job_snapshot(job.job_id)
        engine.findings[0]["evidence"] = [snapshot.stable_segments[0].segment_id]
        StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)

    with JobStore(database) as restarted:
        engine = FakeStructuringEngine()
        result = StructuringExecutor(
            restarted,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)

        assert result.outcome is StructuringOutcome.ALREADY_COMPLETED
        assert result.job.state is JobState.QUALITY_CHECK
        assert engine.classify_calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_document_can_be_resynthesized_without_reextracting_batches(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="document-only",
        )
        segment_id = store.get_job_snapshot(job.job_id).stable_segments[0].segment_id
        findings = [
            {
                "kind": "topic",
                "text": "平台规划。",
                "evidence": [segment_id],
                "confidence": 0.9,
            }
        ]
        StructuringExecutor(
            store,
            FakeStructuringEngine(findings=findings),
            boundary_preflight=preflight(),
        ).run(job.job_id)
        current = store.get_job(job.job_id)
        store.update_job_recording_context(
            job.job_id,
            context="这是关于客户组织转型的多人会议。",
            expected_revision=current.revision,
        )
        engine = FakeStructuringEngine(findings=findings)
        engine.model_id = "fake/faster-structuring"

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).resynthesize_document(job.job_id)

        assert result.outcome is StructuringOutcome.REGENERATED
        assert result.evidence_checkpoint_generation == 2
        assert result.content_type is ContentType.MEETING
        assert engine.synthesize_calls == 1
        assert engine.classify_calls == 0
        assert engine.extract_calls == 0
        assert engine.polish_calls == 0
        assert engine.recording_context == "这是关于客户组织转型的多人会议。"
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        assert checkpoint.payload["model_id"] == "fake/faster-structuring"
        assert checkpoint.payload["content_type"] == "meeting"
        assert checkpoint.payload["content_type_source"] == "automatic"
        assert checkpoint.payload["automatic_content_type"] == "meeting"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_summary_only_regeneration_reads_text_corrections_and_records_diff(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="corrected-summary",
        )
        initial_engine = FakeStructuringEngine(summary_from_transcript=True)
        first = StructuringExecutor(
            store,
            initial_engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)
        processed = store.transition_job(
            job.job_id,
            JobState.PROCESSED,
            expected_revision=first.job.revision,
            reason_code="test_artifacts_generated",
        )
        segment = store.get_job_snapshot(job.job_id).stable_segments[0]
        polished_text = initial_engine.synthesize_inputs[0][0]["text"]
        corrected_text = polished_text.replace("稳定文字", "人工校订文字")
        correction, _ = store.append_correction(
            job.job_id,
            field=CorrectionField.TRANSCRIPT_TEXT,
            target_id=segment.segment_id,
            before=polished_text,
            after=corrected_text,
            author="test-user",
            idempotency_key="summary-text-correction",
            expected_revision=processed.revision,
        )
        assert correction.after == corrected_text
        raw_attempts_before = [item.raw_sha256 for item in store.list_asr_attempts(job.job_id)]
        engine = FakeStructuringEngine(summary_from_transcript=True)

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).resynthesize_document(job.job_id)
        revisions = store.list_checkpoints(job.job_id, stage="summary_revisions")
        stable_after = store.get_job_snapshot(job.job_id).stable_segments[0]
        raw_attempts_after = [item.raw_sha256 for item in store.list_asr_attempts(job.job_id)]

    assert result.outcome is StructuringOutcome.REGENERATED
    assert result.summary_changed is True
    assert result.summary_revision_key == revisions[0].checkpoint_key
    assert engine.synthesize_calls == 1
    assert engine.extract_calls == 0
    assert engine.polish_calls == 0
    assert engine.synthesize_inputs[0][0]["text"] == corrected_text
    assert revisions[0].payload["changed"] is True
    assert revisions[0].payload["text_correction_count"] == 1
    assert revisions[0].payload["speaker_rename_count"] == 0
    assert revisions[0].payload["before_document"] != revisions[0].payload["after_document"]
    assert (
        revisions[0].payload["before_checkpoint"]["raw_sha256"]
        != (revisions[0].payload["after_checkpoint"]["raw_sha256"])
    )
    assert corrected_text in revisions[0].payload["diff"]
    assert revisions[0].payload["diff_truncated"] is False
    assert stable_after.text == segment.text
    assert raw_attempts_after == raw_attempts_before


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_content_type_change_reextracts_without_repolishing_transcript(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="document-type-change",
        )
        segment_id = store.get_job_snapshot(job.job_id).stable_segments[0].segment_id
        findings = [
            {
                "kind": "topic",
                "text": "平台规划。",
                "evidence": [segment_id],
                "confidence": 0.9,
            }
        ]
        StructuringExecutor(
            store,
            FakeStructuringEngine(findings=findings),
            boundary_preflight=preflight(),
        ).run(job.job_id)
        current = store.get_job(job.job_id)
        store.update_job_content_type_override(
            job.job_id,
            content_type="speech",
            expected_revision=current.revision,
        )
        engine = FakeStructuringEngine(findings=findings)

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).resynthesize_document(job.job_id)
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        raw = json.loads(
            (store.data_directory / checkpoint.payload["raw_relative_path"]).read_text("utf-8")
        )

        assert result.content_type is ContentType.SPEECH
        assert engine.classify_calls == 0
        assert engine.extract_calls >= 1
        assert engine.polish_calls == 0
        assert engine.synthesize_calls == 1
        assert raw["extraction_content_type"] == "speech"
        assert checkpoint.payload["content_type_source"] == "user_override"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_failed_transcript_edits_can_be_repaired_without_reextracting(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="edit-repair",
        )
        segment_id = store.get_job_snapshot(job.job_id).stable_segments[0].segment_id
        findings = [
            {
                "kind": "topic",
                "text": "平台规划。",
                "evidence": [segment_id],
                "confidence": 0.9,
            }
        ]
        first = StructuringExecutor(
            store,
            FakeStructuringEngine(
                findings=findings,
                polish_error=RuntimeError("edit failed"),
            ),
            boundary_preflight=preflight(),
        ).run(job.job_id)
        assert first.unavailable_reason_code == "RuntimeError"
        engine = FakeStructuringEngine(findings=findings)

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).repair_transcript_edits(job.job_id)

        assert result.outcome is StructuringOutcome.REGENERATED
        assert result.evidence_checkpoint_generation == 2
        assert result.unavailable_reason_code is None
        assert engine.synthesize_calls == 0
        assert engine.classify_calls == 0
        assert engine.extract_calls == 0
        assert engine.polish_calls >= 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_safely_pauses_before_model_work(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="pressure",
        )
        engine = FakeStructuringEngine()
        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(ResourceStatus.BLOCKED),
        ).run(job.job_id)

        assert result.outcome is StructuringOutcome.SAFE_PAUSED
        assert result.job.state is JobState.PAUSED
        assert result.job.last_error_code == "STRUCTURING_RESOURCE_BLOCKED"
        assert engine.classify_calls == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_engine_failure_degrades_without_inventing_findings(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="fallback",
        )
        result = StructuringExecutor(
            store,
            FakeStructuringEngine(error=RuntimeError("private failure detail")),
            boundary_preflight=preflight(),
        ).run(job.job_id)
        checkpoints = store.list_checkpoints(job.job_id, stage="structuring")
        evidence = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.checkpoint_key == "structuring_result"
        )
        raw_path = store.data_directory / evidence.payload["raw_relative_path"]

        assert result.outcome is StructuringOutcome.COMPLETED
        assert result.job.state is JobState.QUALITY_CHECK
        assert result.content_type is ContentType.GENERIC
        assert result.finding_count == 0
        assert result.unavailable_reason_code == "RuntimeError"
        assert b"private failure detail" not in raw_path.read_bytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_degrades_findings_without_transcript_evidence(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="invalid-evidence",
        )
        engine = FakeStructuringEngine(
            findings=[
                {
                    "kind": "fact",
                    "text": "没有证据的内容。",
                    "evidence": ["missing_segment"],
                    "confidence": 0.9,
                }
            ]
        )

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)

        assert result.job.state is JobState.QUALITY_CHECK
        assert result.content_type is ContentType.MEETING
        assert result.finding_count == 0
        assert result.unavailable_reason_code == "StructuringFailed"
        assert engine.synthesize_calls == 1
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        raw = json.loads(
            (store.data_directory / checkpoint.payload["raw_relative_path"]).read_text("utf-8")
        )
        assert raw["document"]["title"] == "结构提炼测试会议"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_failed_finding_batch_retries_as_two_smaller_batches(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="finding-retry",
        )
        snapshot = store.get_job_snapshot(job.job_id)
        segment_id = snapshot.stable_segments[0].segment_id
        engine = FakeStructuringEngine(
            findings=[
                {
                    "kind": "fact",
                    "text": "分批重试保留了证据。",
                    "evidence": [segment_id],
                    "confidence": 0.9,
                }
            ],
            max_extract_segments=2,
        )

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
            batch_max_chars=50_000,
        ).run(job.job_id)

        assert result.finding_count == 1
        assert result.unavailable_reason_code is None
        assert engine.extract_calls == 3
        assert len(engine.extract_inputs[0]) > 2
        assert all(len(batch) <= 2 for batch in engine.extract_inputs[1:])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_requires_a_structuring_job(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="guard",
            finalize=False,
        )
        assert job.state is JobState.DIARIZING

        with pytest.raises(InvalidJobRequest):
            StructuringExecutor(
                store,
                FakeStructuringEngine(),
                boundary_preflight=preflight(),
            ).run(job.job_id)


def test_discussion_thread_enforces_transcript_order_and_deduplicates() -> None:
    segment_ids = {"seg_initial", "seg_correction", "seg_current"}
    evidence = ["seg_initial"]
    document = {
        "title": "方案讨论会议",
        "summary": {
            "text": "讨论了业务切入口。会议最终确定采用旧方案。",
            "evidence": evidence,
        },
        "context": [
            {
                "kind": "purpose",
                "title": "会议目的",
                "text": "讨论业务切入口。",
                "evidence": evidence,
            },
            {
                "kind": "background",
                "title": "会议背景",
                "text": "团队需要确定试点方向。",
                "evidence": evidence,
            },
        ],
        "highlights": [
            {"text": "销售预测和计划排程作为初始重点场景。", "evidence": evidence},
            {"text": "核心信息1", "evidence": evidence},
            {"text": "核心信息2", "evidence": evidence},
        ],
        "topics": [
            {
                "title": f"议题{index}",
                "summary": "讨论业务方向。",
                "details": [],
                "evidence": evidence,
            }
            for index in range(3)
        ],
        "discussion_threads": [
            {
                "title": "试点切入口",
                "initial_position": {
                    "text": "最初建议从销售预测切入。",
                    "evidence": ["seg_initial"],
                },
                "developments": [
                    {
                        "text": "随后明确不能只做销售预测。",
                        "evidence": ["seg_correction"],
                    }
                ],
                "current_direction": {
                    "text": "当前方向转向计划排程。",
                    "evidence": ["seg_initial", "seg_current"],
                },
                "status": "tentative",
            }
        ],
        "speaker_summaries": [],
        "decisions": [
            {
                "text": "把最初建议误写成已经决定。",
                "evidence": ["seg_initial"],
            }
        ],
        "actions": [],
        "risks": [],
        "open_questions": [],
    }
    validation_kwargs = {
        "segment_ids": segment_ids,
        "speaker_ids": set(),
        "content_type": ContentType.MEETING,
        "segment_texts": {
            "seg_initial": "最初建议从销售预测切入。",
            "seg_correction": "随后明确不能只做销售预测。",
            "seg_current": "当前转向APS计划排程。",
        },
        "segment_speakers": {segment_id: None for segment_id in segment_ids},
        "segment_starts": {
            "seg_initial": 1_000,
            "seg_correction": 2_000,
            "seg_current": 3_000,
        },
    }
    document["discussion_threads"].append(
        json.loads(json.dumps(document["discussion_threads"][0], ensure_ascii=False))
    )

    validated = _validate_document(document, **validation_kwargs)
    assert validated["discussion_threads"][0]["status"] == "tentative"
    assert len(validated["discussion_threads"]) == 1
    assert validated["decisions"] == []
    assert "最终确定" not in validated["summary"]["text"]
    assert len(validated["highlights"]) == 2
    assert "APS" in validated["discussion_threads"][0]["current_direction"]["text"]

    out_of_order = json.loads(json.dumps(document, ensure_ascii=False))
    out_of_order["discussion_threads"][0]["current_direction"]["evidence"] = ["seg_initial"]
    repaired = _validate_document(out_of_order, **validation_kwargs)
    assert repaired["discussion_threads"][0]["current_direction"]["evidence"][-1] == ("seg_current")


def test_ollama_engine_requires_valid_model_name() -> None:
    with pytest.raises(InvalidJobRequest):
        OllamaStructuringEngine(model="   ")


@pytest.mark.parametrize(
    ("content_type", "kind", "label"),
    [
        (ContentType.INTERVIEW, "question_answer", "关键问答"),
        (ContentType.COURSE, "concept", "核心概念"),
        (ContentType.SPEECH, "argument", "核心论点"),
        (ContentType.VOICE_MEMO, "hypothesis", "待验证假设"),
        (ContentType.GENERIC, "insight", "核心信息"),
    ],
)
def test_nonmeeting_profiles_have_scene_specific_synthesis_contracts(
    monkeypatch, content_type: ContentType, kind: str, label: str
) -> None:
    engine = OllamaStructuringEngine(model="qwen3:14b", editor_model="qwen3:8b")
    captured = {}

    def generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["schema"] = kwargs["format_schema"]
        return "{}"

    monkeypatch.setattr(engine, "_generate", generate)
    engine.synthesize_document(
        [
            {
                "finding_id": "finding_coverage",
                "kind": "fact",
                "text": "不可遗漏的候选事实",
                "evidence": ["seg_profile"],
                "confidence": 0.9,
                "unsupported": False,
                "occurrences": 1,
            }
        ],
        [
            {
                "segment_id": "seg_profile",
                "text": "这是一段用于验证场景结构的完整原文。",
                "speaker_id": "speaker_1",
            }
        ],
        content_type=content_type,
    )

    assert "scene_sections" in captured["schema"]["required"]
    assert "timeline_sections" in captured["schema"]["required"]
    allowed = captured["schema"]["properties"]["scene_sections"]["items"]["properties"]["kind"][
        "enum"
    ]
    assert kind in allowed
    assert label in captured["prompt"]
    assert "不得为了固定条数填充" in captured["prompt"]
    assert "不可遗漏的候选事实" in captured["prompt"]
    assert "候选信息索引仅用于检查" in captured["prompt"]
    assert "完整校订后逐字稿" in captured["prompt"]
    assert "独立于内容类型模板的顺序摘要" in captured["prompt"]


def test_meeting_profile_preserves_approved_document_contract() -> None:
    schema = _document_json_schema(ContentType.MEETING)

    assert "scene_sections" not in schema["properties"]
    assert "topics" in schema["required"]
    assert "timeline_sections" in schema["required"]


def test_timeline_sections_must_cover_corrected_transcript_continuously() -> None:
    document = {
        "title": "顺序摘要测试",
        "summary": {"text": "内容依次讨论甲、乙、丙。", "evidence": ["seg_1"]},
        "context": [],
        "highlights": [],
        "topics": [],
        "scene_sections": [
            {
                "kind": "theme",
                "title": "三个连续主题",
                "summary": "内容依次讨论甲、乙、丙。",
                "details": [],
                "evidence": ["seg_1"],
            }
        ],
        "timeline_sections": [
            {
                "title": "主题甲",
                "summary": "先讨论甲。",
                "details": [],
                "start_segment_id": "seg_1",
                "end_segment_id": "seg_1",
            },
            {
                "title": "主题乙与丙",
                "summary": "随后讨论乙和丙。",
                "details": ["乙之后继续谈到丙。"],
                "start_segment_id": "seg_2",
                "end_segment_id": "seg_3",
            },
        ],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
    }
    validation_kwargs = {
        "segment_ids": {"seg_1", "seg_2", "seg_3"},
        "speaker_ids": set(),
        "content_type": ContentType.GENERIC,
        "segment_texts": {"seg_1": "甲。", "seg_2": "乙。", "seg_3": "丙。"},
        "segment_speakers": {"seg_1": None, "seg_2": None, "seg_3": None},
        "segment_starts": {"seg_1": 0, "seg_2": 60_000, "seg_3": 120_000},
    }

    validated = _validate_document(document, **validation_kwargs)
    assert validated["timeline_sections"][1]["end_segment_id"] == "seg_3"

    document["timeline_sections"][1]["start_segment_id"] = "seg_1"
    with pytest.raises(StructuringFailed, match="strictly ordered semantic starts"):
        _validate_document(document, **validation_kwargs)


@pytest.mark.parametrize(
    ("content_type", "kind", "section_title"),
    [
        (ContentType.INTERVIEW, "question_answer", "如何选择职业方向"),
        (ContentType.COURSE, "concept", "反馈回路"),
        (ContentType.SPEECH, "argument", "技术应服务真实问题"),
        (ContentType.VOICE_MEMO, "hypothesis", "先验证付费意愿"),
        (ContentType.GENERIC, "insight", "材料的主要发现"),
    ],
)
def test_nonmeeting_documents_pass_evidence_bound_scene_quality_gate(
    content_type: ContentType, kind: str, section_title: str
) -> None:
    document = {
        "title": f"{section_title}笔记",
        "summary": {"text": "摘要忠实保留原文重点。", "evidence": ["seg_profile"]},
        "context": [],
        "highlights": [{"text": "只保留一条真正重要的信息。", "evidence": ["seg_profile"]}],
        "topics": [],
        "scene_sections": [
            {
                "kind": kind,
                "title": section_title,
                "summary": "按该内容类型的语义组织正文。",
                "details": [{"text": "保留可核对的具体细节。", "evidence": ["seg_profile"]}],
                "evidence": ["seg_profile"],
            }
        ],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
    }
    evidence_text = (
        "假设先验证付费意愿是否成立。"
        if content_type is ContentType.VOICE_MEMO
        else "这是可以核对的原文证据。"
    )
    validated = _validate_document(
        document,
        segment_ids={"seg_profile"},
        speaker_ids=set(),
        content_type=content_type,
        segment_texts={"seg_profile": evidence_text},
        segment_speakers={"seg_profile": None},
        segment_starts={"seg_profile": 1_000},
    )

    assert validated["scene_sections"][0]["kind"] == kind
    assert validated["chapters"] == [
        {
            "title": section_title,
            "summary": "按该内容类型的语义组织正文。",
            "evidence": ["seg_profile"],
        }
    ]
    assert len(validated["highlights"]) == 1


def test_voice_memo_profile_requires_concrete_titles_and_complete_methods() -> None:
    guidance = synthesis_guidance(ContentType.VOICE_MEMO.value)
    schema = _document_json_schema(ContentType.VOICE_MEMO)

    assert "不能直接使用“记录意图" in guidance
    assert "完整保留顺序" in guidance
    assert "不自动构成任务" in guidance
    assert "MVP进行验证" in guidance
    assert schema["properties"]["scene_sections"]["items"]["properties"]["details"]["maxItems"] == 8


def test_generic_profile_requires_specific_nonduplicated_content_structure() -> None:
    guidance = synthesis_guidance(ContentType.GENERIC.value)

    assert "不能把整句处理目标" in guidance
    assert "不能直接使用“背景、主题、核心信息" in guidance
    assert "已经整理方案图”不是待办" in guidance
    assert "speaker_summaries 应为空" in guidance


def test_generic_normalizes_meta_sections_tentative_plans_and_single_speaker() -> None:
    document = {
        "title": "梳理AI需求的两个大类、涉及的能力模块以及前期如何选择少量需求作为切入点",
        "summary": {
            "text": (
                "本次记录旨在梳理AI需求的两个大类，涉及的能力模块，以及前期如何选择少量"
                "需求作为切入点。主要讨论了需求分为面向运营场景和业务侧两个方面，涉及"
                "知识库建设、智能体建设、小模型应用、ASR/TTS等能力模块。初期选择几个点"
                "作为切入点，并基于这几个点进行案例方案图的延展。"
            ),
            "evidence": ["seg_one", "seg_two", "seg_six"],
        },
        "context": [
            {
                "kind": "background",
                "title": "背景",
                "text": "本次记录是在梳理AI需求分类和能力模块。",
                "evidence": ["seg_one"],
            }
        ],
        "highlights": [
            {
                "text": "需求分为面向运营场景和业务侧两个方面",
                "evidence": ["seg_one"],
            },
            {
                "text": "初期选择几个点作为切入点，并延展方案图",
                "evidence": ["seg_six", "seg_seven"],
            },
            {
                "text": "后续将根据需求点进行实际实施",
                "evidence": ["seg_seven"],
            },
        ],
        "topics": [],
        "scene_sections": [
            {
                "kind": "context",
                "title": "背景",
                "summary": "本次记录旨在梳理AI需求分类和能力模块。",
                "details": [
                    {
                        "text": "需求分为面向运营场景和业务侧两个方面",
                        "evidence": ["seg_one"],
                    },
                    {
                        "text": "涉及知识库建设、智能体建设、小模型应用、ASR/TTS等能力模块",
                        "evidence": ["seg_two"],
                    },
                ],
                "evidence": ["seg_one", "seg_two"],
            },
            {
                "kind": "insight",
                "title": "核心信息",
                "summary": "本次记录的核心信息是AI需求分类、能力模块和前期切入。",
                "details": [
                    {
                        "text": "初期选择几个点作为切入点，并基于这几个点进行案例方案图的延展",
                        "evidence": ["seg_six", "seg_seven"],
                    }
                ],
                "evidence": ["seg_six", "seg_seven"],
            },
            {
                "kind": "action",
                "title": "后续行动",
                "summary": "基于需求点制作方案图并延展思路。",
                "details": [],
                "evidence": ["seg_seven"],
            },
            {
                "kind": "open_question",
                "title": "开放问题",
                "summary": "具体如何选择需求点作为切入点尚未明确。",
                "details": [],
                "evidence": ["seg_six", "seg_seven"],
            },
        ],
        "discussion_threads": [],
        "speaker_summaries": [
            {
                "speaker_id": "speaker_01",
                "display_name": "",
                "affiliation": "",
                "role": "",
                "summary": "说话人介绍了AI需求分类与前期切入。",
                "evidence": ["seg_one", "seg_two"],
            }
        ],
        "decisions": [],
        "actions": [
            {
                "task": "基于需求点制作方案图并延展思路",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_seven"],
            }
        ],
        "risks": [],
        "open_questions": [
            {
                "text": "具体如何选择需求点作为切入点尚未明确",
                "evidence": ["seg_six", "seg_seven"],
            }
        ],
    }
    segment_texts = {
        "seg_one": "需求主要分为运营场景和业务侧两个方面。",
        "seg_two": "能力包括知识库、智能体、小模型、ASR和TTS。",
        "seg_six": "可能会跟您看一下，初期拿几个点作为切入。",
        "seg_seven": "已经用两三个需求点做了方案图，后续以哪些需求切入还要再看。",
    }

    validated = _validate_document(
        document,
        segment_ids=set(segment_texts),
        speaker_ids={"speaker_01"},
        content_type=ContentType.GENERIC,
        segment_texts=segment_texts,
        segment_speakers={segment_id: "speaker_01" for segment_id in segment_texts},
        segment_starts={
            segment_id: index * 1_000 for index, segment_id in enumerate(segment_texts)
        },
    )

    assert validated["title"] == "AI需求分类、能力模块与前期需求切入"
    assert validated["context"] == []
    assert validated["speaker_summaries"] == []
    assert validated["actions"] == []
    assert len(validated["highlights"]) == 2
    assert "拟选择少量需求点" in validated["highlights"][1]["text"]
    assert [section["title"] for section in validated["scene_sections"]] == [
        "运营侧与业务侧两类AI需求",
        "知识库、智能体与语音技术等能力模块",
        "从少量需求点启动方案设计",
    ]
    assert "实际实施" not in validated["summary"]["text"]


def test_voice_memo_removes_meta_repetition_and_separates_method_from_tasks() -> None:
    document = {
        "title": "企业AI落地方法",
        "summary": {"text": "记录提出企业AI落地的六步方法。", "evidence": ["seg_method"]},
        "context": [],
        "highlights": [],
        "topics": [],
        "scene_sections": [
            {
                "kind": "intent",
                "title": "记录意图",
                "summary": "这段语音备忘记录了企业AI落地的思考。",
                "details": [],
                "evidence": ["seg_method"],
            },
            {
                "kind": "task",
                "title": "明确任务",
                "summary": "企业AI落地应遵循六步方法。",
                "details": [
                    {"text": f"第{index}步保留原始方法。", "evidence": ["seg_method"]}
                    for index in range(1, 7)
                ],
                "evidence": ["seg_method"],
            },
            {
                "kind": "hypothesis",
                "title": "待验证假设",
                "summary": "用MVP验证真实业务价值。",
                "details": [],
                "evidence": ["seg_method"],
            },
        ],
        "discussion_threads": [],
        "speaker_summaries": [
            {
                "speaker_id": "speaker_01",
                "display_name": "",
                "affiliation": "",
                "role": "",
                "summary": "重复正文。",
                "evidence": ["seg_method"],
            }
        ],
        "decisions": [],
        "actions": [
            {
                "task": "把六步方法全部执行",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_method"],
            },
            {
                "task": "整理重点场景",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_next"],
            },
        ],
        "risks": [],
        "open_questions": [],
    }
    segment_texts = {
        "seg_method": "方法包括战略对齐、场景筛选、技术底座、MVP验证、灰度复制和长期运营。",
        "seg_next": "下一步要整理重点场景。",
    }

    validated = _validate_document(
        document,
        segment_ids=set(segment_texts),
        speaker_ids={"speaker_01"},
        content_type=ContentType.VOICE_MEMO,
        segment_texts=segment_texts,
        segment_speakers={"seg_method": "speaker_01", "seg_next": "speaker_01"},
        segment_starts={"seg_method": 1_000, "seg_next": 2_000},
    )

    assert len(validated["scene_sections"]) == 1
    assert [item["kind"] for item in validated["scene_sections"]] == ["idea"]
    assert validated["scene_sections"][0]["title"] == "企业AI落地应遵循六步方法"
    assert len(validated["scene_sections"][0]["details"]) == 6
    assert validated["speaker_summaries"] == []
    assert [item["task"] for item in validated["actions"]] == ["整理重点场景"]


def test_voice_memo_reconstructs_ordered_method_and_explicit_next_step() -> None:
    document = {
        "title": "企业AI落地的思考与实施步骤",
        "summary": {"text": "原文提出分阶段落地AI。", "evidence": ["seg_one"]},
        "context": [],
        "highlights": [],
        "topics": [],
        "scene_sections": [
            {
                "kind": "idea",
                "title": "分阶段落地AI",
                "summary": "原文提出分阶段落地AI。",
                "details": [],
                "evidence": ["seg_one"],
            }
        ],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
    }
    segment_texts = {
        "seg_one": "方法要分六步。第一步是战略对齐。",
        "seg_filler": "啊",
        "seg_one_end": "第二步是筛选场景。",
        "seg_two": "第三步是搭建底座。第四步是做MVP验证。第五步是灰度复制。",
        "seg_three": "最后是长期运营。",
        "seg_next": "所以今天我们可以先讨论战略方向，然后筛选重点场景。",
    }

    validated = _validate_document(
        document,
        segment_ids=set(segment_texts),
        speaker_ids=set(),
        content_type=ContentType.VOICE_MEMO,
        segment_texts=segment_texts,
        segment_speakers={segment_id: None for segment_id in segment_texts},
        segment_starts={
            "seg_one": 1_000,
            "seg_filler": 1_500,
            "seg_one_end": 2_000,
            "seg_two": 3_000,
            "seg_three": 4_000,
            "seg_next": 5_000,
        },
    )

    assert len(validated["scene_sections"]) == 1
    method = validated["scene_sections"][0]
    assert method["title"] == "企业AI落地的六步实施路径"
    assert len(method["details"]) == 6
    assert [detail["text"].split("：", 1)[0] for detail in method["details"]] == [
        "第一步",
        "第二步",
        "第三步",
        "第四步",
        "第五步",
        "第六步",
    ]
    assert "seg_filler" not in method["details"][0]["evidence"]
    assert [action["task"] for action in validated["actions"]] == [
        "先讨论战略方向，然后筛选重点场景。"
    ]


def test_voice_memo_polishes_an_existing_ordered_method_from_document_sources() -> None:
    document = {
        "title": "企业AI落地的思考与实施步骤",
        "summary": {"text": "原文提出分阶段落地AI。", "evidence": ["seg_one"]},
        "context": [
            {
                "kind": "background",
                "title": "长期运营",
                "text": "AI需要通过指标监控、安全合规和持续优化形成运营闭环。",
                "evidence": ["seg_six"],
            }
        ],
        "highlights": [],
        "topics": [
            {
                "title": "战略对齐",
                "summary": "由企业高层牵头完成战略对齐，并建立AI落地组织。",
                "details": [],
                "evidence": ["seg_one"],
            },
            {
                "title": "筛选场景",
                "summary": "优先筛选高频、标准化、数据完备且低风险的高价值场景。",
                "details": [],
                "evidence": ["seg_two"],
            },
        ],
        "scene_sections": [
            {
                "kind": "judgment",
                "title": "企业AI落地的六步实施路径",
                "summary": "原文提出六个依次推进的阶段。",
                "details": [
                    {"text": "第一步：可能是要去做一个战略的对齐。", "evidence": ["seg_one"]},
                    {"text": "第二步：就是说去筛选一些高价值的一些场景。", "evidence": ["seg_two"]},
                    {"text": "第三步：搭建技术底座。", "evidence": ["seg_three"]},
                    {"text": "第四步：做MVP验证。", "evidence": ["seg_four"]},
                    {"text": "第五步：灰度复制。", "evidence": ["seg_five"]},
                    {"text": "第六步：长期运营。", "evidence": ["seg_six"]},
                ],
                "evidence": ["seg_one", "seg_six"],
            }
        ],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
    }
    segment_texts = {
        "seg_one": "第一步可能是要做战略对齐。",
        "seg_two": "第二步筛选高价值场景。",
        "seg_three": "第三步搭建技术底座。",
        "seg_four": "第四步做MVP验证。",
        "seg_five": "第五步灰度复制。",
        "seg_six": "第六步长期运营并形成闭环。",
    }

    validated = _validate_document(
        document,
        segment_ids=set(segment_texts),
        speaker_ids=set(),
        content_type=ContentType.VOICE_MEMO,
        segment_texts=segment_texts,
        segment_speakers={segment_id: None for segment_id in segment_texts},
        segment_starts={
            segment_id: index * 1_000 for index, segment_id in enumerate(segment_texts)
        },
    )

    details = validated["scene_sections"][0]["details"]
    assert details[0]["text"] == "第一步：由企业高层牵头完成战略对齐，并建立AI落地组织。"
    assert details[1]["text"] == "第二步：优先筛选高频、标准化、数据完备且低风险的高价值场景。"
    assert details[-1]["text"] == "第六步：AI需要通过指标监控、安全合规和持续优化形成运营闭环。"


def test_scene_quality_gate_rejects_a_meeting_kind_in_an_interview() -> None:
    schema = _document_json_schema(ContentType.INTERVIEW)
    allowed = schema["properties"]["scene_sections"]["items"]["properties"]["kind"]["enum"]

    assert "decision" not in allowed
    assert "question_answer" in allowed


def test_speaker_identity_fields_are_cleared_when_evidence_does_not_name_them() -> None:
    document = {
        "title": "演讲记录",
        "summary": {"text": "主讲人介绍了实践案例。", "evidence": ["seg_speaker"]},
        "context": [],
        "highlights": [],
        "topics": [],
        "scene_sections": [
            {
                "kind": "example",
                "title": "实践案例",
                "summary": "介绍实践案例。",
                "details": [],
                "evidence": ["seg_speaker"],
            }
        ],
        "discussion_threads": [],
        "speaker_summaries": [
            {
                "speaker_id": "speaker_02",
                "display_name": "邱总",
                "affiliation": "OBC",
                "role": "顾问",
                "summary": "询问了工具原先的使用方式。",
                "evidence": ["seg_speaker"],
            }
        ],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
    }

    validated = _validate_document(
        document,
        segment_ids={"seg_speaker"},
        speaker_ids={"speaker_02"},
        content_type=ContentType.SPEECH,
        segment_texts={"seg_speaker": "这个需求现在是给自己用，还是给团队使用？"},
        segment_speakers={"seg_speaker": "speaker_02"},
        segment_starts={"seg_speaker": 1_000},
    )

    speaker = validated["speaker_summaries"][0]
    assert speaker["display_name"] == ""
    assert speaker["affiliation"] == ""
    assert speaker["role"] == ""


def test_speech_actions_keep_only_explicit_future_work() -> None:
    document = {
        "title": "演讲记录",
        "summary": {"text": "分享了项目进展。", "evidence": ["seg_history"]},
        "context": [],
        "highlights": [],
        "topics": [],
        "scene_sections": [
            {
                "kind": "example",
                "title": "项目进展",
                "summary": "项目已有进展。",
                "details": [],
                "evidence": ["seg_history"],
            }
        ],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [
            {
                "task": "组织训练营",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_history"],
            },
            {
                "task": "启动AI扣定项目",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_started"],
            },
            {
                "task": "整理案例",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_future"],
            },
        ],
        "risks": [],
        "open_questions": [],
    }

    validated = _validate_document(
        document,
        segment_ids={"seg_history", "seg_started", "seg_future"},
        speaker_ids=set(),
        content_type=ContentType.SPEECH,
        segment_texts={
            "seg_history": "已经组织了三期训练营，效果不错。",
            "seg_started": "整理完以后再复制，然后就开始搞AI扣定，这两个上了。",
            "seg_future": "下一步需要整理案例并形成分享材料。",
        },
        segment_speakers={
            "seg_history": None,
            "seg_started": None,
            "seg_future": None,
        },
        segment_starts={
            "seg_history": 1_000,
            "seg_started": 2_000,
            "seg_future": 3_000,
        },
    )

    assert [action["task"] for action in validated["actions"]] == ["整理案例"]


def test_interview_filters_historical_actions_answered_questions_and_filler_risks() -> None:
    document = {
        "title": "运维实践访谈",
        "summary": {"text": "访谈介绍了已经落地的运维实践。", "evidence": ["seg_case"]},
        "context": [],
        "highlights": [],
        "topics": [],
        "scene_sections": [
            {
                "kind": "question_answer",
                "title": "工具由谁使用",
                "summary": "工具先由负责人使用，再提供给团队。",
                "details": [],
                "evidence": ["seg_answered"],
            }
        ],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [
            {
                "task": "建立统一资源评价体系",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_case"],
            },
            {
                "task": "整理访谈案例",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_future"],
            },
        ],
        "risks": [
            {"text": "部分语句含糊，表明不确定或思考状态", "evidence": ["seg_filler"]},
            {"text": "内部部署需要处理账号加密与访问安全", "evidence": ["seg_risk"]},
        ],
        "open_questions": [
            {"text": "工具最终由谁使用", "evidence": ["seg_answered"]},
            {"text": "下一轮试点如何评估效果", "evidence": ["seg_open"]},
        ],
    }

    segment_texts = {
        "seg_case": "系统已经建立了统一资源评价体系，并发布到生产环境。",
        "seg_future": "下一步需要整理访谈案例并补充评估材料。",
        "seg_filler": "嗯，啊，哦。",
        "seg_risk": "内部部署需要处理账号加密与访问安全。",
        "seg_answered": "现在是给自己用还是自己先用，嗯，完了以后给团队去用。",
        "seg_open": "下一轮试点如何评估效果？",
    }
    validated = _validate_document(
        document,
        segment_ids=set(segment_texts),
        speaker_ids=set(),
        content_type=ContentType.INTERVIEW,
        segment_texts=segment_texts,
        segment_speakers={segment_id: None for segment_id in segment_texts},
        segment_starts={
            segment_id: index * 1_000 for index, segment_id in enumerate(segment_texts, start=1)
        },
    )

    assert [action["task"] for action in validated["actions"]] == ["整理访谈案例"]
    assert [risk["text"] for risk in validated["risks"]] == ["内部部署需要处理账号加密与访问安全"]
    assert [question["text"] for question in validated["open_questions"]] == [
        "下一轮试点如何评估效果"
    ]


def test_interview_drops_a_distant_status_claim_from_another_case() -> None:
    document = {
        "title": "运维实践访谈",
        "summary": {"text": "访谈介绍了机柜管理案例。", "evidence": ["seg_machine"]},
        "context": [],
        "highlights": [],
        "topics": [],
        "scene_sections": [
            {
                "kind": "question_answer",
                "title": "机柜功率管理系统",
                "summary": "系统通过算法推荐机柜位置，并已上线生产。",
                "details": [
                    {
                        "text": "通过功耗匹配算法推荐设备位置。",
                        "evidence": ["seg_machine"],
                    },
                    {
                        "text": "系统经过安全扫描后上线生产。",
                        "evidence": ["seg_dba"],
                    },
                ],
                "evidence": ["seg_machine", "seg_dba"],
            }
        ],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
    }

    validated = _validate_document(
        document,
        segment_ids={"seg_dba", "seg_machine"},
        speaker_ids=set(),
        content_type=ContentType.INTERVIEW,
        segment_texts={
            "seg_dba": "数据库系统经过安全扫描，修复漏洞后上线生产。",
            "seg_machine": "机柜按功耗匹配算法推荐设备放置位置。",
        },
        segment_speakers={"seg_dba": None, "seg_machine": None},
        segment_starts={"seg_dba": 100_000, "seg_machine": 800_000},
    )

    section = validated["scene_sections"][0]
    assert section["kind"] == "experience"
    assert section["evidence"] == ["seg_machine"]
    assert "上线" not in section["summary"]
    assert section["details"] == [
        {
            "text": "通过功耗匹配算法推荐设备位置。",
            "evidence": ["seg_machine"],
        }
    ]
