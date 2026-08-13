"""Backend artifact package generation tests."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from speech_capture_worker.alignment import (
    AlignmentFinalizationOutcome,
    TranscriptAlignmentFinalizer,
)
from speech_capture_worker.artifact_generation import (
    ArtifactGenerator,
    ArtifactOutcome,
    _apply_corrections,
    _filter_scene_actions,
    _read_manual_section,
)
from speech_capture_worker.asr_execution import AsrChunkExecutor, AsrRunOutcome
from speech_capture_worker.corrections import (
    CorrectionField,
    CorrectionRecord,
    encode_segment_review,
)
from speech_capture_worker.domain import JobState, ResourceStatus, UploadCreateRequest
from speech_capture_worker.errors import (
    ArtifactGenerationFailed,
    InvalidJobRequest,
    PublicationConflict,
    PublicationVerificationFailed,
    RevisionConflict,
    UploadStorageError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult
from speech_capture_worker.recording_metadata import RECORDING_DATE_OPTION
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
from speech_capture_worker.transcript import (
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptSegment,
    TranscriptTimingStatus,
)
from speech_capture_worker.vault_publication import (
    PUBLISHED_PACKAGE_FILES,
    VaultPublicationOutcome,
    VaultPublisher,
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


def test_speech_actions_require_independent_action_evidence() -> None:
    document = {
        "actions": [
            {"task": "泛化启发", "evidence": ["seg_idea"]},
            {"task": "明确安排", "evidence": ["seg_action"]},
        ]
    }
    findings = (
        SimpleNamespace(
            unsupported=False,
            kind=SimpleNamespace(value="action_item"),
            evidence=("seg_action",),
        ),
    )

    filtered = _filter_scene_actions(
        document,
        findings=findings,
        content_type="speech",
    )

    assert filtered["actions"] == [{"task": "明确安排", "evidence": ["seg_action"]}]
    assert len(document["actions"]) == 2


def test_first_explicit_speaker_rename_overrides_derived_display_name() -> None:
    segment = TranscriptSegment(
        job_id="job_speaker_rename",
        segment_sequence=1,
        segment_id="seg_speaker_rename",
        commit_key="speaker-rename-segment",
        revision=1,
        start_ms=0,
        end_ms=1_000,
        outcome=TranscriptOutcome.TRANSCRIBED,
        text="王总介绍了客户背景。",
        language="zh",
        confidence=0.9,
        timing_status=TranscriptTimingStatus.ALIGNED,
        speaker_id="speaker_0",
        speaker_label_status=SpeakerLabelStatus.ANONYMOUS,
        error_code=None,
        created_at="2026-08-08T00:00:00Z",
        updated_at="2026-08-08T00:00:00Z",
    )
    document = {
        "speaker_summaries": [
            {
                "speaker_id": "speaker_0",
                "display_name": "模型推断名称",
                "affiliation": "",
                "role": "",
                "summary": "介绍了客户背景。",
                "evidence": [segment.segment_id],
            }
        ]
    }
    correction = CorrectionRecord(
        sequence=1,
        correction_id="cor_speaker_rename",
        job_id=segment.job_id,
        job_revision=2,
        field=CorrectionField.SPEAKER_DISPLAY_NAME,
        target_id="speaker_0",
        before="Speaker 0",
        after="王总",
        author="test-user",
        idempotency_key="speaker-rename-1",
        created_at="2026-08-08T00:00:01Z",
    )

    effective = _apply_corrections(
        segments=[segment],
        transcript_edits={},
        document=document,
        corrections=[correction],
        recording_date=None,
    )

    assert effective.speaker_display_names == {"speaker_0": "王总"}
    assert effective.speaker_assignments == {segment.segment_id: "speaker_0"}
    assert effective.document["speaker_summaries"][0]["display_name"] == "王总"


def test_manual_section_reader_rejects_symlink_and_invalid_utf8(tmp_path) -> None:
    target = tmp_path / "target.md"
    target.write_text("## 我的补充\n\n用户内容。\n", "utf-8")
    symlink = tmp_path / "note-symlink.md"
    symlink.symlink_to(target)
    invalid = tmp_path / "note-invalid.md"
    invalid.write_bytes(b"## \xff\n")

    with pytest.raises(UploadStorageError, match="cannot be a symlink"):
        _read_manual_section(symlink)
    with pytest.raises(ArtifactGenerationFailed, match="not valid UTF-8"):
        _read_manual_section(invalid)


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

    def __init__(
        self,
        findings=None,
        *,
        content_type="meeting",
        scene_kind=None,
        scene_title=None,
    ):
        self.findings = findings or []
        self.content_type = content_type
        self.scene_kind = scene_kind
        self.scene_title = scene_title
        self.recording_context = None

    def set_recording_context(self, context):
        self.recording_context = context

    def classify(self, segments, *, speaker_count):
        return {
            "type": self.content_type,
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
        document = {
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
            "highlights": [{"text": f"{text}{index}", "evidence": evidence} for index in range(5)],
            "topics": [
                {
                    "title": f"平台规划{index}",
                    "summary": text,
                    "details": [{"text": text, "evidence": evidence}],
                    "evidence": evidence,
                }
                for index in range(5)
            ],
            "timeline_sections": [
                {
                    "title": "平台规划",
                    "summary": text,
                    "details": [text],
                    "start_segment_id": segments[0]["segment_id"],
                    "end_segment_id": segments[-1]["segment_id"],
                }
            ],
            "speaker_summaries": [],
            "decisions": decisions,
            "actions": actions,
            "risks": [],
            "open_questions": [],
        }
        if self.content_type != "meeting":
            document["title"] = self.scene_title or "场景化内容笔记"
            document["context"] = []
            document["scene_sections"] = [
                {
                    "kind": self.scene_kind,
                    "title": self.scene_title or "场景化正文",
                    "summary": text,
                    "details": [{"text": text, "evidence": evidence}],
                    "evidence": evidence,
                }
            ]
        return document

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

    def synthesize_missing_scene_sections(self, document, findings, segments, *, content_type):
        return []

    def refine_interview_document(self, document, segments):
        return dict(document)

    def refine_meeting_document(self, document, segments):
        return dict(document)

    def refine_meeting_outcomes(self, document, segments):
        return dict(document)

    def refine_voice_memo_document(self, document, segments):
        return dict(document)

    def synthesize_speaker_summaries(self, segments, *, speaker_ids, content_type):
        return []

    def reconcile_decisions(self, document, segments, *, content_type):
        return list(document.get("decisions", []))

    def polish_transcript_batch(self, segments):
        return [
            {
                "segment_id": item["segment_id"],
                "text": (
                    "假设先验证付费意愿是否成立。"
                    if self.content_type == "voice_memo"
                    else item["text"] + "。"
                ),
            }
            for item in segments
        ]


def create_quality_check_job(
    store: JobStore,
    *,
    duration_seconds: float,
    suffix: str,
    with_structuring: bool = True,
    content_type: str = "meeting",
    scene_kind: str | None = None,
    scene_title: str | None = None,
    speaker_id: str | None = None,
    recording_date: str | None = None,
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
        options=(
            {RECORDING_DATE_OPTION: recording_date}
            if recording_date is not None
            else {}
        ),
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
    if speaker_id is not None:
        diarizing = finalized.job
        segment = store.get_job_snapshot(claimed.job_id).stable_segments[0]
        store.update_transcript_segment_metadata(
            claimed.job_id,
            segment.segment_id,
            expected_revision=segment.revision,
            speaker_id=speaker_id,
            speaker_label_status=SpeakerLabelStatus.ANONYMOUS,
        )
        structuring = store.transition_job(
            claimed.job_id,
            JobState.STRUCTURING,
            expected_revision=diarizing.revision,
            reason_code="test_enter_structuring",
        )
    else:
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
        ],
        content_type=content_type,
        scene_kind=scene_kind,
        scene_title=scene_title,
    )
    result = StructuringExecutor(
        store,
        engine,
        boundary_preflight=preflight(),
    ).run(claimed.job_id)
    assert result.job.state is JobState.QUALITY_CHECK
    return result.job


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_artifact_generation_writes_four_markdown_files_and_two_machine_records(
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
            recording_date="2026-07-31",
        )
        result = ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        manifest = json.loads((package / "artifact-manifest.json").read_text("utf-8"))
        raw = json.loads((package / "transcript.raw.json").read_text("utf-8"))
        transcript = (package / "transcript.md").read_text("utf-8")
        speech_record = json.loads((package / "speech-record.json").read_text("utf-8"))
        note = (package / "note.md").read_text("utf-8")
        evidence_note = (package / "note.evidence.md").read_text("utf-8")
        timeline = (package / "timeline.md").read_text("utf-8")

        assert result.outcome is ArtifactOutcome.GENERATED
        assert result.job.state is JobState.PROCESSED
        assert result.artifact_count == 6
        assert manifest["artifact_count"] == 6
        for name in (
            "transcript.raw.json",
            "transcript.md",
            "speech-record.json",
            "note.md",
            "note.evidence.md",
            "timeline.md",
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
        assert speech_record["content"]["source"] == "automatic"
        assert speech_record["content"]["automatic_type"] == "meeting"
        assert speech_record["document"]["title"] == "平台规划会议"
        assert speech_record["document"]["timeline_sections"]
        assert speech_record["dates"]["recording_date"] == "2026-07-31"
        assert speech_record["segments"]
        assert speech_record["segments"][0]["raw_text"]
        assert speech_record["findings"][0]["evidence"]
        assert "## 我的补充" in note
        assert note.startswith("# 平台规划会议\n")
        assert "## 内容总结" in note
        assert "## 背景与参与方" in note
        assert "## 议题与讨论" in note
        assert "## 讨论演变与当前方向" in note
        assert "最初建议从销售预测切入" in note
        assert "当前方向转向计划排程" in note
        assert "[[transcript" not in note
        assert "依据：" not in note
        assert "## 章节导航" not in note
        assert "## 会议信息" not in note
        assert "## 我的补充" not in evidence_note
        assert "[[transcript#^sp-" in evidence_note
        assert "## 章节导航" in evidence_note
        assert timeline.startswith("# 按时间阅读\n")
        assert "## 00:00:00–00:01:35｜平台规划" in timeline
        assert "[[transcript" not in timeline
        assert result.speech_id == speech_record["speech_id"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
@pytest.mark.parametrize(
    ("content_type", "scene_kind", "scene_title", "body_heading", "kind_label"),
    [
        (
            "interview",
            "question_answer",
            "如何判断产品方向",
            "访谈内容",
            "关键问答",
        ),
        ("course", "concept", "反馈回路", "知识结构", "核心概念"),
        ("speech", "argument", "技术服务真实问题", "演讲脉络", "核心论点"),
        ("voice_memo", "hypothesis", "先验证付费意愿", "想法整理", "待验证假设"),
        ("generic", "insight", "材料的主要发现", "内容结构", "核心信息"),
    ],
)
def test_each_nonmeeting_profile_renders_clean_and_evidence_note_structures(
    tmp_path,
    content_type: str,
    scene_kind: str,
    scene_title: str,
    body_heading: str,
    kind_label: str,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix=f"profile-{content_type}",
            content_type=content_type,
            scene_kind=scene_kind,
            scene_title=scene_title,
        )
        ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        note = (package / "note.md").read_text("utf-8")
        evidence_note = (package / "note.evidence.md").read_text("utf-8")
        speech_record = json.loads((package / "speech-record.json").read_text("utf-8"))

    assert speech_record["content"]["type"] == content_type
    assert speech_record["document"]["scene_sections"][0]["kind"] == scene_kind
    assert f"## {body_heading}" in note
    if content_type in {"voice_memo", "generic"}:
        assert f"### 1. {scene_title}" in note
        assert f"{kind_label}｜{scene_title}" not in note
    else:
        assert f"{kind_label}｜{scene_title}" in note
    assert "[[transcript" not in note
    assert "[[transcript#^sp-" in evidence_note
    assert "## 议题与讨论" not in note
    assert "## 参与者与各方观点" not in note
    assert "暂无明确待办" not in note


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
def test_regeneration_preserves_manual_note_section_verbatim_and_rehashes_package(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="manual-section",
        )
        first = ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        note_path = package / "note.md"
        original = note_path.read_text("utf-8")
        manual_section = (
            "## 我的补充\n\n"
            "这是用户手写的判断，必须原样保留。\n\n"
            "- [ ] 后续核对客户名称\n\n"
            "> [!note] 私人备注\n"
            "> 可以包含 Obsidian callout。\n\n"
            "## 我自己的二级标题\n\n"
            "这里仍属于终端人工区域。\n"
        )
        edited = original.replace("# 平台规划会议", "# 用户不应改写生成区", 1)
        edited = edited[: edited.index("## 我的补充")] + manual_section
        note_path.write_text(edited, "utf-8")

        regenerated = ArtifactGenerator(store).generate(job.job_id)
        note = note_path.read_text("utf-8")
        evidence_note = (package / "note.evidence.md").read_text("utf-8")
        manifest = json.loads((package / "artifact-manifest.json").read_text("utf-8"))
        replayed = ArtifactGenerator(store).generate(job.job_id)

    assert regenerated.outcome is ArtifactOutcome.REGENERATED
    assert regenerated.manifest_sha256 != first.manifest_sha256
    assert note.startswith("# 平台规划会议\n")
    assert note.endswith(manual_section)
    assert note.count("## 我的补充") == 1
    assert "私人备注" not in evidence_note
    assert manifest["files"]["note.md"] == hashlib.sha256(
        note.encode("utf-8")
    ).hexdigest()
    assert replayed.outcome is ArtifactOutcome.ALREADY_GENERATED


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_append_only_corrections_regenerate_only_derived_artifacts(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="corrections",
            speaker_id="speaker_0",
            recording_date="2026-07-31",
        )
        ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        raw_before = (package / "transcript.raw.json").read_bytes()
        original_segment = store.get_job_snapshot(job.job_id).stable_segments[1]
        original_record = json.loads((package / "speech-record.json").read_text("utf-8"))
        current_text = original_record["segments"][1]["text"]
        revised_text = current_text.replace("稳定文字", "校订文字")

        current_job = store.get_job(job.job_id)
        text_correction, created = store.append_correction(
            job.job_id,
            field=CorrectionField.TRANSCRIPT_TEXT,
            target_id=original_segment.segment_id,
            before=current_text,
            after=revised_text,
            author="test-user",
            idempotency_key="correction-text-1",
            expected_revision=current_job.revision,
        )
        assert created is True
        replayed, created = store.append_correction(
            job.job_id,
            field=CorrectionField.TRANSCRIPT_TEXT,
            target_id=original_segment.segment_id,
            before=current_text,
            after=revised_text,
            author="test-user",
            idempotency_key="correction-text-1",
            expected_revision=current_job.revision,
        )
        assert created is False
        assert replayed.correction_id == text_correction.correction_id
        with pytest.raises(RevisionConflict):
            store.append_correction(
                job.job_id,
                field=CorrectionField.TRANSCRIPT_TEXT,
                target_id=original_segment.segment_id,
                before=revised_text,
                after=revised_text + "已再次修改。",
                author="test-user",
                idempotency_key="correction-text-stale",
                expected_revision=current_job.revision,
            )

        current_job = store.get_job(job.job_id)
        segment_review, created = store.append_correction(
            job.job_id,
            field=CorrectionField.SEGMENT_REVIEW,
            target_id=original_segment.segment_id,
            before=encode_segment_review(
                text=revised_text,
                speaker_id=original_segment.speaker_id,
            ),
            after=encode_segment_review(
                text=revised_text + "并已复核。",
                speaker_id="speaker_0",
            ),
            author="test-user",
            idempotency_key="correction-segment-review-1",
            expected_revision=current_job.revision,
        )
        assert created is True
        assert segment_review.field is CorrectionField.SEGMENT_REVIEW

        current_job = store.get_job(job.job_id)
        store.append_correction(
            job.job_id,
            field=CorrectionField.SPEAKER_DISPLAY_NAME,
            target_id="speaker_0",
            before="Speaker 0",
            after="王总",
            author="test-user",
            idempotency_key="correction-speaker-1",
            expected_revision=current_job.revision,
        )
        current_job = store.get_job(job.job_id)
        store.append_correction(
            job.job_id,
            field=CorrectionField.RECORDING_DATE,
            target_id=None,
            before="2026-07-31",
            after="2026-08-01",
            author="test-user",
            idempotency_key="correction-date-1",
            expected_revision=current_job.revision,
        )

        regenerated = ArtifactGenerator(store).generate(job.job_id)
        transcript = (package / "transcript.md").read_text("utf-8")
        record = json.loads((package / "speech-record.json").read_text("utf-8"))
        evidence_note = (package / "note.evidence.md").read_text("utf-8")
        stable_after = store.get_job_snapshot(job.job_id).stable_segments[0]
        already = ArtifactGenerator(store).generate(job.job_id)

    assert regenerated.outcome is ArtifactOutcome.REGENERATED
    assert already.outcome is ArtifactOutcome.ALREADY_GENERATED
    assert (package / "transcript.raw.json").read_bytes() == raw_before
    assert stable_after.text == original_segment.text
    assert revised_text + "并已复核。" in transcript
    assert "· 王总" in transcript
    assert record["segments"][1]["text"] == revised_text + "并已复核。"
    assert record["segments"][1]["raw_text"] == original_segment.text
    assert record["segments"][1]["speaker_id"] == "speaker_0"
    assert record["segments"][1]["raw_speaker_id"] is None
    assert record["dates"]["recording_date"] == "2026-08-01"
    assert {"display_name": "王总", "speaker_id": "speaker_0"} in record["speakers"]
    assert [item["field"] for item in record["corrections"][-4:]] == [
        "transcript_text",
        "segment_review",
        "speaker_display_name",
        "recording_date",
    ]
    assert "recording_date: 2026-08-01" in evidence_note
    assert "  - 王总" in evidence_note


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_regeneration_refuses_to_overwrite_existing_note_without_manual_boundary(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="missing-manual-boundary",
        )
        ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        note_path = package / "note.md"
        unsafe_content = "# 用户改写的整篇笔记\n\n没有受保护区域标记。\n"
        note_path.write_text(unsafe_content, "utf-8")

        with pytest.raises(ArtifactGenerationFailed, match="protected '我的补充' section"):
            ArtifactGenerator(store).generate(job.job_id)

        assert note_path.read_text("utf-8") == unsafe_content


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_processed_job_regenerates_when_artifact_schema_changes(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="artifact-schema-upgrade",
        )
        ArtifactGenerator(store).generate(job.job_id)
        checkpoint = next(
            item
            for item in store.list_checkpoints(job.job_id, stage="artifacts")
            if item.checkpoint_key == "artifacts_generation"
        )
        legacy_payload = dict(checkpoint.payload)
        legacy_payload["schema_version"] = "1.4.0"
        store.put_checkpoint(
            job.job_id,
            stage="artifacts",
            checkpoint_key="artifacts_generation",
            payload=legacy_payload,
        )

        result = ArtifactGenerator(store).generate(job.job_id)

    assert result.outcome is ArtifactOutcome.REGENERATED
    assert result.artifact_count == 6


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
        regenerated = ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        note = (package / "note.md").read_text("utf-8")
        evidence_note = (package / "note.evidence.md").read_text("utf-8")
        manifest = json.loads((package / "artifact-manifest.json").read_text("utf-8"))

    assert structured.outcome.value == "regenerated"
    assert structured.job.state is JobState.PROCESSED
    assert regenerated.outcome is ArtifactOutcome.REGENERATED
    assert regenerated.job.state is JobState.PROCESSED
    assert regenerated.manifest_sha256 != first.manifest_sha256
    assert "## 内容总结" in note
    assert "> [!abstract] 内容总结" in evidence_note
    assert "重新生成后的有效结论。" in note
    assert "[[transcript" not in note
    assert "[[transcript#^sp-" in evidence_note
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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_vault_publisher_atomically_publishes_and_verifies_complete_package(
    tmp_path,
) -> None:
    vault = tmp_path / "Vault"
    vault.mkdir()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="vault-publish",
        )
        first = ArtifactGenerator(store).generate(job.job_id)
        current = store.get_job(job.job_id)
        store.append_correction(
            job.job_id,
            field=CorrectionField.RECORDING_DATE,
            target_id=None,
            before=None,
            after="2026-08-01",
            author="test-user",
            idempotency_key="publish-recording-date",
            expected_revision=current.revision,
        )
        regenerated = ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        publisher = VaultPublisher(
            store,
            vault_root=vault,
            publisher_id="local_vault",
            output_root="Published Speech",
        )

        result = publisher.publish(
            job.job_id,
            expected_revision=regenerated.job.revision,
        )
        target = vault / result.target_relative_path
        replayed = publisher.publish(
            job.job_id,
            expected_revision=result.job.revision,
        )

        assert first.job.state is JobState.PROCESSED
        assert result.outcome is VaultPublicationOutcome.PUBLISHED
        assert result.job.state is JobState.PUBLISHED
        assert result.target_relative_path.startswith("Published Speech/2026/08/2026-08-01-")
        assert result.target_relative_path.endswith(f"--{regenerated.speech_id}")
        assert {item.name for item in target.iterdir()} == set(PUBLISHED_PACKAGE_FILES)
        for name in PUBLISHED_PACKAGE_FILES:
            assert (target / name).read_bytes() == (package / name).read_bytes()
        assert not any(item.name.startswith(".") for item in target.parent.iterdir())
        assert replayed.outcome is VaultPublicationOutcome.ALREADY_PUBLISHED
        assert replayed.receipt == result.receipt

        edited = (target / "note.md").read_text("utf-8") + "\n用户在 Vault 中的修改。\n"
        (target / "note.md").write_text(edited, "utf-8")
        with pytest.raises(PublicationConflict):
            publisher.publish(job.job_id, expected_revision=result.job.revision)
        assert (target / "note.md").read_text("utf-8") == edited


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_vault_publication_conflict_preserves_user_and_worker_files(tmp_path) -> None:
    vault = tmp_path / "Vault"
    vault.mkdir()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="vault-conflict",
        )
        generated = ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        worker_note = (package / "note.md").read_bytes()
        target = vault / "Speech" / "Undated" / f"平台规划会议--{generated.speech_id}"
        target.mkdir(parents=True)
        user_file = target / "user-note.md"
        user_file.write_text("用户已有内容，不得覆盖。", "utf-8")
        publisher = VaultPublisher(
            store,
            vault_root=vault,
            publisher_id="local_vault",
            output_root="Speech",
        )

        with pytest.raises(PublicationConflict, match="user changes"):
            publisher.publish(job.job_id, expected_revision=generated.job.revision)

        assert store.get_job(job.job_id).state is JobState.PROCESSED
        assert user_file.read_text("utf-8") == "用户已有内容，不得覆盖。"
        assert (package / "note.md").read_bytes() == worker_note
        assert store.get_publication_receipt(job.job_id) is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_publication_can_resume_after_directory_commit_before_acknowledgement(
    tmp_path, monkeypatch
) -> None:
    vault = tmp_path / "Vault"
    vault.mkdir()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="vault-resume",
        )
        generated = ArtifactGenerator(store).generate(job.job_id)
        publisher = VaultPublisher(
            store,
            vault_root=vault,
            publisher_id="local_vault",
            output_root="Speech",
        )
        acknowledge = store.acknowledge_publication

        def interrupt_acknowledgement(*args, **kwargs):
            raise PublicationVerificationFailed("simulated acknowledgement interruption")

        monkeypatch.setattr(store, "acknowledge_publication", interrupt_acknowledgement)
        with pytest.raises(PublicationVerificationFailed, match="simulated"):
            publisher.publish(job.job_id, expected_revision=generated.job.revision)
        processed = store.get_job(job.job_id)
        target = vault / "Speech" / "Undated" / f"平台规划会议--{generated.speech_id}"
        assert processed.state is JobState.PROCESSED
        assert target.is_dir()
        assert {item.name for item in target.iterdir()} == set(PUBLISHED_PACKAGE_FILES)

        monkeypatch.setattr(store, "acknowledge_publication", acknowledge)
        resumed = publisher.publish(job.job_id, expected_revision=processed.revision)

    assert resumed.outcome is VaultPublicationOutcome.PUBLISHED
    assert resumed.job.state is JobState.PUBLISHED


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_vault_publication_rejects_symlink_escape_before_claim(tmp_path) -> None:
    vault = tmp_path / "Vault"
    outside = tmp_path / "Outside"
    vault.mkdir()
    outside.mkdir()
    (vault / "Speech").symlink_to(outside, target_is_directory=True)
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="vault-symlink",
        )
        generated = ArtifactGenerator(store).generate(job.job_id)
        publisher = VaultPublisher(
            store,
            vault_root=vault,
            publisher_id="local_vault",
            output_root="Speech",
        )

        with pytest.raises(PublicationConflict, match="outside"):
            publisher.publish(job.job_id, expected_revision=generated.job.revision)

        assert store.get_job(job.job_id).state is JobState.PROCESSED
        assert store.list_publication_leases(job.job_id) == []
        assert list(outside.iterdir()) == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_vault_publication_rejects_corrupt_worker_artifact_before_claim(tmp_path) -> None:
    vault = tmp_path / "Vault"
    vault.mkdir()
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_quality_check_job(
            store,
            duration_seconds=95,
            suffix="vault-corrupt-worker-artifact",
        )
        generated = ArtifactGenerator(store).generate(job.job_id)
        package = store.get_job_stage_directory(job.job_id, stage="artifacts")
        transcript = package / "transcript.md"
        transcript.write_text(
            transcript.read_text("utf-8") + "\n未经清单记录的修改。\n",
            "utf-8",
        )
        publisher = VaultPublisher(
            store,
            vault_root=vault,
            publisher_id="local_vault",
            output_root="Speech",
        )

        with pytest.raises(PublicationVerificationFailed, match="checksum"):
            publisher.publish(job.job_id, expected_revision=generated.job.revision)

        assert store.get_job(job.job_id).state is JobState.PROCESSED
        assert store.list_publication_leases(job.job_id) == []
        assert list(vault.iterdir()) == []
