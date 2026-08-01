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
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
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
    ):
        self.classification = classification or {
            "type": "meeting",
            "traits": ["multi_speaker", "action_oriented"],
            "confidence": 0.92,
        }
        self.findings = findings or []
        self.error = error
        self.polish_error = polish_error
        self.classify_calls = 0
        self.extract_calls = 0
        self.synthesize_calls = 0
        self.speaker_supplement_calls = 0
        self.polish_calls = 0
        self.extract_inputs = []
        self.synthesize_inputs = []

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
        return [dict(finding) for finding in self.findings]

    def synthesize_document(self, findings, segments, *, content_type):
        self.synthesize_calls += 1
        self.synthesize_inputs.append([dict(item) for item in segments])
        if self.error is not None:
            raise self.error
        evidence = (
            list(findings[0]["evidence"])
            if findings
            else [segments[0]["segment_id"]]
        )
        text = findings[0]["text"] if findings else "从完整逐字稿生成的笔记。"
        return {
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
            "highlights": [
                {"text": f"{text}{index}", "evidence": evidence} for index in range(5)
            ],
            "topics": [
                {
                    "title": f"主要议题{index}",
                    "summary": text,
                    "details": [{"text": text, "evidence": evidence}],
                    "evidence": evidence,
                }
                for index in range(5)
            ],
            "speaker_summaries": [],
            "decisions": [],
            "actions": [],
            "risks": [],
            "open_questions": [],
        }

    def synthesize_speaker_summaries(
        self, segments, *, speaker_ids, content_type
    ):
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
                        item["segment_id"]
                        for item in segments
                        if item["speaker_id"] == speaker_id
                    )
                ],
            }
            for speaker_id in speaker_ids
        ]

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
        return [
            {"segment_id": item["segment_id"], "text": item["text"] + "。"} for item in segments
        ]


def create_structuring_job(
    store: JobStore,
    *,
    duration_seconds: float,
    suffix: str,
    finalize: bool = True,
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
        assert all(
            item["text"].endswith("。")
            for batch in engine.extract_inputs
            for item in batch
        )
        assert len(engine.synthesize_inputs[0]) == len(
            [segment for segment in snapshot.stable_segments if segment.text]
        )
        assert all(
            item["segment_id"].startswith("s") for item in engine.synthesize_inputs[0]
        )
        assert all(item["text"].endswith("。") for item in engine.synthesize_inputs[0])
        assert raw["prompt_version"] == "2026-08-01.19"
        assert raw["document"]["title"] == "结构提炼测试会议"
        assert sum(
            len(batch["transcript_edits"])
            for batch in raw["transcript_edit_results"]
        ) == len(snapshot.stable_segments)
        assert any(checkpoint.checkpoint_key == "structuring_result" for checkpoint in checkpoints)


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
        findings = [{
            "kind": "topic",
            "text": "平台规划。",
            "evidence": [segment_id],
            "confidence": 0.9,
        }]
        StructuringExecutor(
            store,
            FakeStructuringEngine(findings=findings),
            boundary_preflight=preflight(),
        ).run(job.job_id)
        engine = FakeStructuringEngine(findings=findings)
        engine.model_id = "fake/faster-structuring"

        result = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).resynthesize_document(job.job_id)

        assert result.outcome is StructuringOutcome.REGENERATED
        assert result.evidence_checkpoint_generation == 2
        assert engine.synthesize_calls == 1
        assert engine.classify_calls == 0
        assert engine.extract_calls == 0
        assert engine.polish_calls == 0
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="structuring")
            if item.checkpoint_key == "structuring_result"
        )
        assert checkpoint.payload["model_id"] == "fake/faster-structuring"


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
        findings = [{
            "kind": "topic",
            "text": "平台规划。",
            "evidence": [segment_id],
            "confidence": 0.9,
        }]
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
            (store.data_directory / checkpoint.payload["raw_relative_path"]).read_text(
                "utf-8"
            )
        )
        assert raw["document"]["title"] == "结构提炼测试会议"


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
    out_of_order["discussion_threads"][0]["current_direction"]["evidence"] = [
        "seg_initial"
    ]
    repaired = _validate_document(out_of_order, **validation_kwargs)
    assert repaired["discussion_threads"][0]["current_direction"]["evidence"][-1] == (
        "seg_current"
    )


def test_ollama_engine_requires_valid_model_name() -> None:
    with pytest.raises(InvalidJobRequest):
        OllamaStructuringEngine(model="   ")
