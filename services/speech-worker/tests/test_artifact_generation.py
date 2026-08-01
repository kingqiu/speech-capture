"""Backend artifact package generation tests."""

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
from speech_capture_worker.artifact_generation import (
    ArtifactGenerator,
    ArtifactOutcome,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor, AsrRunOutcome
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.errors import ArtifactGenerationFailed, InvalidJobRequest
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
    StructuringExecutor,
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


def preflight(_status: ResourceStatus = ResourceStatus.READY):
    def check(*_, **__):
        return ResourceReport(
            status=_status,
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
            issues=(
                (
                    ResourceIssue(
                        code="MEMORY_PRESSURE_BLOCKED",
                        status=ResourceStatus.BLOCKED,
                        message="Memory pressure is too high.",
                        action="Close large applications, then resume.",
                    ),
                )
                if _status is ResourceStatus.BLOCKED
                else ()
            ),
        )

    return check


class FakeAsrEngine:
    model_id = "fake/local-asr"

    def transcribe(self, audio, *, sample_rate, language_hint, context):
        duration = len(audio) / sample_rate
        text = "这是产物生成测试的稳定文字，而不是旧方案。"
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

    def __init__(self, findings=None):
        self.findings = findings or []

    def classify(self, segments, *, speaker_count):
        return {
            "type": "meeting",
            "traits": ["multi_speaker"],
            "confidence": 0.9,
        }

    def extract_batch(self, segments, *, content_type):
        return [dict(finding) for finding in self.findings]

    def synthesize_document(self, findings, segments, *, content_type):
        evidence = list(findings[0]["evidence"])
        text = findings[0]["text"]
        actions = []
        decisions = []
        if findings[0]["kind"] == "action_item":
            actions.append({"task": text, "owner": "", "deadline": "", "evidence": evidence})
        if findings[0]["kind"] == "decision":
            decisions.append({"text": text, "evidence": evidence})
        return {
            "title": "平台规划会议",
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
                    "title": f"平台规划{index}",
                    "summary": text,
                    "details": [{"text": text, "evidence": evidence}],
                    "evidence": evidence,
                }
                for index in range(5)
            ],
            "speaker_summaries": [],
            "decisions": decisions,
            "actions": actions,
            "risks": [],
            "open_questions": [],
        }

    def synthesize_discussion_threads(self, segments, *, content_type):
        if segments[0]["segment_id"] == segments[-1]["segment_id"]:
            return []
        return [
            {
                "title": "方案切入口",
                "initial_position": {
                    "text": "最初建议从销售预测切入。",
                    "evidence": [segments[0]["segment_id"]],
                },
                "developments": [
                    {
                        "text": "随后修正为不能只看销售预测。",
                        "evidence": [segments[-1]["segment_id"]],
                    }
                ],
                "current_direction": {
                    "text": "当前方向转向计划排程。",
                    "evidence": [segments[-1]["segment_id"]],
                },
                "status": "tentative",
            }
        ]

    def reconcile_decisions(self, document, segments, *, content_type):
        return list(document.get("decisions", []))

    def polish_transcript_batch(self, segments):
        return [
            {"segment_id": item["segment_id"], "text": item["text"] + "。"} for item in segments
        ]


def create_quality_check_job(
    store: JobStore,
    *,
    duration_seconds: float,
    suffix: str,
    with_structuring: bool = True,
):
    content = wav_bytes(duration_seconds=duration_seconds)
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=f"artifacts-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"artifacts-upload-{suffix}",
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
        idempotency_key=f"artifacts-job-{suffix}",
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
    structuring = store.transition_job(
        claimed.job_id,
        JobState.STRUCTURING,
        expected_revision=finalized.job.revision,
        reason_code="test_enter_structuring",
    )
    if not with_structuring:
        return store.transition_job(
            claimed.job_id,
            JobState.QUALITY_CHECK,
            expected_revision=structuring.revision,
            reason_code="test_enter_quality_check",
        )
    segment_id = store.get_job_snapshot(claimed.job_id).stable_segments[0].segment_id
    engine = FakeStructuringEngine(
        [
            {
                "kind": "topic",
                "text": "平台规划。",
                "evidence": [segment_id],
                "confidence": 0.9,
            }
        ]
    )
    result = StructuringExecutor(
        store,
        engine,
        boundary_preflight=preflight(),
    ).run(claimed.job_id)
    assert result.job.state is JobState.QUALITY_CHECK
    return result.job


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_artifact_generation_writes_four_files_and_advances_to_processed(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="complete",
        )
        result = ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        manifest = json.loads((package / "artifact-manifest.json").read_text("utf-8"))
        raw = json.loads((package / "transcript.raw.json").read_text("utf-8"))
        transcript = (package / "transcript.md").read_text("utf-8")
        speech_record = json.loads((package / "speech-record.json").read_text("utf-8"))
        note = (package / "note.md").read_text("utf-8")

        assert result.outcome is ArtifactOutcome.GENERATED
        assert result.job.state is JobState.PROCESSED
        assert result.artifact_count == 4
        assert manifest["artifact_count"] == 4
        for name in (
            "transcript.raw.json",
            "transcript.md",
            "speech-record.json",
            "note.md",
        ):
            content = (package / name).read_bytes()
            assert manifest["files"][name] == hashlib.sha256(content).hexdigest()
        manifest_bytes = (package / "artifact-manifest.json").read_bytes()
        assert result.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
        assert raw["schema_version"] == "1.0.0"
        assert raw["attempts"]
        assert transcript.startswith("## ")
        assert "^sp-" in transcript
        assert speech_record["content"]["type"] == "meeting"
        assert speech_record["document"]["title"] == "平台规划会议"
        assert speech_record["segments"]
        assert speech_record["segments"][0]["raw_text"]
        assert speech_record["findings"][0]["evidence"]
        assert "## 我的补充" in note
        assert "## 背景与参与方" in note
        assert "## 议题与讨论" in note
        assert "## 讨论演变与当前方向" in note
        assert "最初建议从销售预测切入" in note
        assert "当前方向转向计划排程" in note
        assert "[[transcript#^sp-" in note
        assert result.speech_id == speech_record["speech_id"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_artifact_generation_is_idempotent_after_restart(tmp_path) -> None:
    database = tmp_path / "worker.sqlite3"
    with JobStore(
        database,
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="replay",
        )
        ArtifactGenerator(store).generate(job.job_id)

    with JobStore(database) as restarted:
        result = ArtifactGenerator(restarted).generate(job.job_id)
        assert result.outcome is ArtifactOutcome.ALREADY_GENERATED
        assert result.job.state is JobState.PROCESSED


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_processed_job_can_restructure_and_regenerate_useful_note(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="regenerate",
        )
        first = ArtifactGenerator(store).generate(job.job_id)
        segment_id = store.get_job_snapshot(job.job_id).stable_segments[0].segment_id
        engine = FakeStructuringEngine(
            [
                {
                    "kind": "decision",
                    "text": "重新生成后的有效结论。",
                    "evidence": [segment_id],
                    "confidence": 0.95,
                }
            ]
        )

        structured = StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id, force=True)
        regenerated = ArtifactGenerator(store).generate(job.job_id, force=True)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        note = (package / "note.md").read_text("utf-8")
        manifest = json.loads((package / "artifact-manifest.json").read_text("utf-8"))

    assert structured.outcome.value == "regenerated"
    assert structured.job.state is JobState.PROCESSED
    assert regenerated.outcome is ArtifactOutcome.REGENERATED
    assert regenerated.job.state is JobState.PROCESSED
    assert regenerated.manifest_sha256 != first.manifest_sha256
    assert "> [!abstract] 内容总结" in note
    assert "重新生成后的有效结论。" in note
    assert "[[transcript#^sp-" in note
    assert manifest["structuring_checkpoint_generation"] == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_artifact_generation_requires_quality_check_state(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        content = wav_bytes(duration_seconds=95)
        checksum = hashlib.sha256(content).hexdigest()
        upload, _ = store.create_upload(
            UploadCreateRequest(
                vault_id="vault_primary",
                source_display_name="artifacts-guard.wav",
                source_sha256=checksum,
                source_size_bytes=len(content),
                media_type="audio/wav",
            ),
            idempotency_key="artifacts-guard-upload",
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
            idempotency_key="artifacts-guard-job",
        )
        claimed = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )
        AsrChunkExecutor(
            store,
            FakeAsrEngine(),
            boundary_preflight=preflight(),
        ).run_all(claimed.job_id)
        finalized = TranscriptAlignmentFinalizer(store).finalize(claimed.job_id)
        assert finalized.job.state is JobState.DIARIZING

        with pytest.raises(InvalidJobRequest):
            ArtifactGenerator(store).generate(claimed.job_id)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_artifact_generation_requires_structuring_evidence(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="missing-structuring",
            with_structuring=False,
        )

        with pytest.raises(ArtifactGenerationFailed):
            ArtifactGenerator(store).generate(job.job_id)
