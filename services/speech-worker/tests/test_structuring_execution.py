"""Content-type classification and evidence-linked extraction tests."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import time
import wave
from types import SimpleNamespace

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
from speech_capture_worker.note_prompt_profiles import NOTE_PROMPT_VERSION, synthesis_guidance
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourceIssue,
    ResourceReport,
)
from speech_capture_worker.structuring_execution import (
    DEFAULT_BATCH_MAX_CHARS,
    DEFAULT_BATCH_TARGET_TOKENS,
    MIN_SUBSTANTIVE_SPEAKER_CHARACTERS,
    ContentType,
    FindingKind,
    OllamaStructuringEngine,
    StructuringExecutor,
    StructuringOutcome,
    _bounded_speaker_evidence_packet,
    _build_batches,
    _compact_model_text,
    _dedupe_document_categories,
    _document_json_schema,
    _estimate_text_tokens,
    _is_substantive_finding_text,
    _meeting_highlight_is_question,
    _meeting_question_is_covered_by_action,
    _meeting_question_remains_open,
    _promote_meeting_actionable_highlights,
    _remove_unsupported_decision_claims,
    _remove_unsupported_open_question_claims,
    _remove_unsupported_speaker_host_claim,
    _repair_speaker_supplement_evidence,
    _sanitize_quality_evidence_references,
    _synthesis_segment_payload,
    _timeline_window_boundaries,
    _validate_document,
    _validate_transcript_edits,
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
        self.timeline_repair_calls = 0
        self.polish_calls = 0
        self.coverage_calls = 0
        self.meeting_repair_calls = 0
        self.interview_repair_calls = 0
        self.voice_memo_repair_calls = 0
        self.discussion_thread_calls = 0
        self.decision_reconcile_calls = 0
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

    def synthesize_timeline_sections(self, segments):
        self.timeline_repair_calls += 1
        return [
            {
                "title": "完整时间线",
                "summary": "覆盖完整录音。",
                "details": [],
                "start_segment_id": segments[0]["segment_id"],
                "end_segment_id": segments[-1]["segment_id"],
            }
        ]

    def synthesize_missing_scene_sections(self, document, findings, segments, *, content_type):
        self.coverage_calls += 1
        return [dict(section) for section in self.coverage_sections]

    def refine_interview_document(self, document, segments):
        self.interview_repair_calls += 1
        return dict(document)

    def refine_meeting_document(self, document, segments):
        self.meeting_repair_calls += 1
        return dict(document)

    def refine_meeting_outcomes(self, document, segments):
        return dict(document)

    def refine_voice_memo_document(self, document, segments):
        self.voice_memo_repair_calls += 1
        return dict(document)

    def synthesize_discussion_threads(self, segments, *, content_type):
        self.discussion_thread_calls += 1
        return []

    def reconcile_decisions(self, document, segments, *, content_type):
        self.decision_reconcile_calls += 1
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


class PartialBatchSpeakerStructuringEngine(FakeStructuringEngine):
    def __init__(self) -> None:
        super().__init__()
        self.speaker_supplement_requests = []

    def synthesize_speaker_summaries(self, segments, *, speaker_ids, content_type):
        self.speaker_supplement_requests.append(list(speaker_ids))
        requested = list(speaker_ids)
        if len(requested) > 1:
            requested = requested[:-1]
        return super().synthesize_speaker_summaries(
            segments,
            speaker_ids=requested,
            content_type=content_type,
        )


class SimulatedProcessExit(BaseException):
    pass


class ExitAfterCandidateEngine(FakeStructuringEngine):
    def refine_meeting_document(self, document, segments):
        self.meeting_repair_calls += 1
        raise SimulatedProcessExit()


def test_structuring_heartbeat_pulses_during_a_blocking_model_call(monkeypatch) -> None:
    monkeypatch.setattr(
        "speech_capture_worker.structuring_execution.STRUCTURING_HEARTBEAT_SECONDS",
        0.01,
    )
    executor = object.__new__(StructuringExecutor)
    pulses = []

    result = executor._run_with_heartbeat(
        lambda: (time.sleep(0.045), "done")[1],
        heartbeat=lambda: pulses.append(time.monotonic()),
    )

    assert result == "done"
    assert len(pulses) >= 2


def test_token_budget_batches_reduce_calls_without_splitting_segments() -> None:
    segments = [
        SimpleNamespace(
            segment_id=f"segment-{index:04d}",
            text=("数据治理方案需要明确范围、责任、接口和验收标准。" * 4),
        )
        for index in range(180)
    ]

    legacy = _build_batches(segments, max_chars=2200, max_segments=120)
    optimized = _build_batches(
        segments,
        max_chars=DEFAULT_BATCH_MAX_CHARS,
        target_tokens=DEFAULT_BATCH_TARGET_TOKENS,
        max_segments=180,
    )

    assert len(optimized) <= len(legacy) * 0.7
    assert [item.segment_id for batch in optimized for item in batch] == [
        item.segment_id for item in segments
    ]
    assert all(
        sum(_estimate_text_tokens(item.text) + 20 for item in batch)
        <= DEFAULT_BATCH_TARGET_TOKENS
        for batch in optimized
    )
    override_batches = _build_batches(
        segments[:2],
        max_chars=DEFAULT_BATCH_MAX_CHARS,
        target_tokens=100,
        text_overrides={segments[0].segment_id: "校订后扩展内容" * 30},
    )
    assert len(override_batches) == 2


def test_model_text_compaction_changes_only_representation_whitespace() -> None:
    assert _compact_model_text("  项目\t范围  不变\n\n\n下一项  ") == (
        "项目 范围 不变\n\n下一项"
    )


def test_missing_speaker_packet_is_bounded_and_keeps_chronological_coverage() -> None:
    segments = [
        {
            "segment_id": f"segment-{index:03d}",
            "start_ms": index * 1000,
            "speaker_id": "speaker_01" if index % 2 == 0 else "speaker_02",
            "text": f"第 {index} 段关于项目范围和交付要求的实质陈述。" * 3,
        }
        for index in range(80)
    ]

    packet = _bounded_speaker_evidence_packet(segments, speaker_ids={"speaker_02"})

    assert packet
    assert {item["speaker_id"] for item in packet} == {"speaker_02"}
    assert [item["start_ms"] for item in packet] == sorted(
        item["start_ms"] for item in packet
    )
    assert packet[0]["segment_id"] == "segment-001"
    assert packet[-1]["segment_id"] == "segment-079"
    assert sum(
        _estimate_text_tokens(item["text"]) + 20 for item in packet
    ) <= 1100


def test_speaker_supplement_retries_an_omitted_participant_individually() -> None:
    engine = PartialBatchSpeakerStructuringEngine()
    executor = object.__new__(StructuringExecutor)
    executor.engine = engine
    segments = [
        {
            "segment_id": f"segment-{index}",
            "speaker_id": speaker_id,
            "text": "有实质内容的发言" * 70,
        }
        for index, speaker_id in enumerate(("speaker_01", "speaker_02", "speaker_03"), start=1)
    ]

    document = executor._repair_speaker_summary_coverage(
        {"speaker_summaries": []},
        segments,
        content_type=ContentType.MEETING,
    )

    assert [item["speaker_id"] for item in document["speaker_summaries"]] == [
        "speaker_01",
        "speaker_02",
        "speaker_03",
    ]
    assert engine.speaker_supplement_requests == [
        ["speaker_01", "speaker_02", "speaker_03"],
        ["speaker_03"],
    ]


def test_speaker_supplement_repairs_evidence_from_the_wrong_participant() -> None:
    repaired = _repair_speaker_supplement_evidence(
        [
            {
                "speaker_id": "speaker_02",
                "display_name": "",
                "affiliation": "",
                "role": "",
                "summary": "重点讨论上线计划。",
                "evidence": ["seg_other"],
            }
        ],
        expected_speaker_ids={"speaker_02"},
        segments=[
            {
                "segment_id": "seg_other",
                "speaker_id": "speaker_01",
                "text": "这是其他人的发言。",
            },
            {
                "segment_id": "seg_own",
                "speaker_id": "speaker_02",
                "text": "上线计划需要分批推进。",
            },
        ],
    )

    assert repaired[0]["evidence"] == ["seg_own"]


def test_document_synthesis_repairs_a_timeline_that_omits_the_opening() -> None:
    engine = FakeStructuringEngine()
    executor = object.__new__(StructuringExecutor)
    executor.engine = engine
    segments = [
        {"segment_id": "s0001", "speaker_id": None, "text": "开场。"},
        {"segment_id": "s0002", "speaker_id": None, "text": "正文。"},
    ]
    document = engine.synthesize_document([], segments, content_type=ContentType.MEETING)
    document["timeline_sections"][0]["start_segment_id"] = "s0002"
    engine.synthesize_document = lambda *args, **kwargs: document

    repaired = executor._synthesize_document_with_speaker_coverage(
        [],
        segments,
        content_type=ContentType.MEETING,
    )

    assert repaired["timeline_sections"][0]["start_segment_id"] == "s0001"
    assert engine.timeline_repair_calls == 1


def test_timeline_windows_cover_a_long_recording_without_an_oversized_tail() -> None:
    segments = [
        {
            "segment_id": f"segment-{minute:02d}",
            "start_ms": minute * 60_000,
            "end_ms": minute * 60_000 + 45_000,
        }
        for minute in range(40)
    ]

    windows = _timeline_window_boundaries(segments)
    positions = {segment["segment_id"]: index for index, segment in enumerate(segments)}

    assert windows[0]["start_segment_id"] == "segment-00"
    assert windows[-1]["end_segment_id"] == "segment-39"
    assert all(
        (
            segments[positions[window["end_segment_id"]]]["end_ms"]
            - segments[positions[window["start_segment_id"]]]["start_ms"]
        )
        <= 8 * 60_000
        for window in windows
    )


def test_ollama_timeline_summarizes_each_fixed_window_independently(monkeypatch) -> None:
    engine = OllamaStructuringEngine(model="qwen3:14b", editor_model="qwen3:8b")
    segments = [
        {
            "segment_id": f"segment-{minute:02d}",
            "start_ms": minute * 60_000,
            "end_ms": minute * 60_000 + 45_000,
            "speaker_id": "speaker_01",
            "text": f"第 {minute} 分钟的内容。",
        }
        for minute in range(12)
    ]
    requests = []

    def generate(prompt, **kwargs):
        requests.append({"prompt": prompt, **kwargs})
        return json.dumps(
            {
                "title": "窗口摘要",
                "summary": "概括当前窗口。",
                "details": [],
                "start_segment_id": "ignored",
                "end_segment_id": "ignored",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(engine, "_generate", generate)

    result = engine.synthesize_timeline_sections(segments)
    windows = _timeline_window_boundaries(segments)

    assert len(requests) == len(windows)
    assert [item["start_segment_id"] for item in result] == [
        window["start_segment_id"] for window in windows
    ]
    assert all(request["num_ctx"] == 8192 for request in requests)


def test_meeting_validation_drops_a_thread_whose_development_predates_its_initial_state() -> None:
    engine = FakeStructuringEngine()
    segments = [
        {"segment_id": "seg_early", "speaker_id": "speaker_01", "text": "先讨论。"},
        {"segment_id": "seg_late", "speaker_id": "speaker_01", "text": "后提出方案。"},
    ]
    document = engine.synthesize_document([], segments, content_type=ContentType.MEETING)
    document["discussion_threads"] = [
        {
            "title": "时间顺序错误的议题",
            "initial_position": {"text": "后提出方案。", "evidence": ["seg_late"]},
            "developments": [{"text": "先讨论。", "evidence": ["seg_early"]}],
            "current_direction": {"text": "后提出方案。", "evidence": ["seg_late"]},
            "status": "open",
        }
    ]

    validated = _validate_document(
        document,
        segment_ids={"seg_early", "seg_late"},
        speaker_ids={"speaker_01"},
        content_type=ContentType.MEETING,
        segment_texts={"seg_early": "先讨论。", "seg_late": "后提出方案。"},
        segment_speakers={"seg_early": "speaker_01", "seg_late": "speaker_01"},
        segment_starts={"seg_early": 1_000, "seg_late": 2_000},
    )

    assert validated["discussion_threads"] == []


def test_meeting_validation_drops_a_thread_without_developments() -> None:
    engine = FakeStructuringEngine()
    segments = [
        {"segment_id": "seg_initial", "speaker_id": None, "text": "提出订单问题。"},
        {"segment_id": "seg_current", "speaker_id": None, "text": "说明当前处理方向。"},
    ]
    document = engine.synthesize_document([], segments, content_type=ContentType.MEETING)
    document["discussion_threads"] = [
        {
            "title": "只有起点和当前方向的空壳主线",
            "initial_position": {"text": "提出订单问题。", "evidence": ["seg_initial"]},
            "developments": [],
            "current_direction": {
                "text": "说明当前处理方向。",
                "evidence": ["seg_current"],
            },
            "status": "open",
        }
    ]

    validated = _validate_document(
        document,
        segment_ids={"seg_initial", "seg_current"},
        speaker_ids=set(),
        content_type=ContentType.MEETING,
        segment_texts={
            "seg_initial": "提出订单问题。",
            "seg_current": "说明当前处理方向。",
        },
        segment_speakers={"seg_initial": None, "seg_current": None},
        segment_starts={"seg_initial": 1_000, "seg_current": 2_000},
    )

    assert validated["discussion_threads"] == []


def test_meeting_document_with_empty_scene_sections_is_revalidatable() -> None:
    engine = FakeStructuringEngine()
    segments = [
        {"segment_id": "seg_one", "speaker_id": "speaker_01", "text": "会议内容。"}
    ]
    document = engine.synthesize_document([], segments, content_type=ContentType.MEETING)
    document["discussion_threads"] = []
    document["scene_sections"] = []

    validated = _validate_document(
        document,
        segment_ids={"seg_one"},
        speaker_ids={"speaker_01"},
        content_type=ContentType.MEETING,
        segment_texts={"seg_one": "会议内容。"},
        segment_speakers={"seg_one": "speaker_01"},
        segment_starts={"seg_one": 1_000},
    )

    assert validated["scene_sections"] == []


def test_meeting_validation_accepts_one_evidence_rich_context_item() -> None:
    engine = FakeStructuringEngine()
    segments = [
        {"segment_id": "seg_one", "speaker_id": "speaker_01", "text": "介绍项目背景。"},
        {"segment_id": "seg_two", "speaker_id": "speaker_02", "text": "说明会议目标。"},
    ]
    document = engine.synthesize_document([], segments, content_type=ContentType.MEETING)
    document["discussion_threads"] = []
    document["context"] = [
        {
            "kind": "background",
            "title": "项目背景、参与方与会议目标",
            "text": (
                "本次会议围绕项目操作流程、数据准确性与系统优化展开，参与方共同梳理"
                "当前业务背景、约束条件和协作边界，并以明确后续执行规则和责任分工为目标。"
                "讨论还覆盖现有问题、实施顺序和需要共同确认的验收标准。"
            ),
            "evidence": ["seg_one", "seg_two"],
        }
    ]

    validated = _validate_document(
        document,
        segment_ids={"seg_one", "seg_two"},
        speaker_ids=set(),
        content_type=ContentType.MEETING,
        segment_texts={"seg_one": "介绍项目背景。", "seg_two": "说明会议目标。"},
        segment_speakers={"seg_one": None, "seg_two": None},
        segment_starts={"seg_one": 1_000, "seg_two": 2_000},
    )

    assert len(validated["context"]) == 1


def test_meeting_validation_rejects_one_terse_context_item() -> None:
    engine = FakeStructuringEngine()
    segments = [
        {"segment_id": "seg_one", "speaker_id": "speaker_01", "text": "介绍项目背景。"},
        {"segment_id": "seg_two", "speaker_id": "speaker_02", "text": "说明会议目标。"},
    ]
    document = engine.synthesize_document([], segments, content_type=ContentType.MEETING)
    document["discussion_threads"] = []
    document["context"] = [
        {
            "kind": "background",
            "title": "会议背景",
            "text": "讨论项目。",
            "evidence": ["seg_one", "seg_two"],
        }
    ]

    with pytest.raises(StructuringFailed, match="too little background context"):
        _validate_document(
            document,
            segment_ids={"seg_one", "seg_two"},
            speaker_ids=set(),
            content_type=ContentType.MEETING,
            segment_texts={"seg_one": "介绍项目背景。", "seg_two": "说明会议目标。"},
            segment_speakers={"seg_one": None, "seg_two": None},
            segment_starts={"seg_one": 1_000, "seg_two": 2_000},
        )


def test_synthesis_packet_retains_every_substantive_speakers_own_words() -> None:
    segments = []
    sequence = 0
    for speaker_index in range(4):
        sequence += 1
        segments.append(
            SimpleNamespace(
                segment_id=f"seg_major_{speaker_index}",
                segment_sequence=sequence,
                start_ms=sequence * 1_000,
                end_ms=sequence * 1_000 + 900,
                speaker_id=f"speaker_0{speaker_index + 1}",
                text="主要发言" * 50,
            )
        )
    sequence += 1
    segments.append(
        SimpleNamespace(
            segment_id="seg_lower_volume",
            segment_sequence=sequence,
            start_ms=sequence * 1_000,
            end_ms=sequence * 1_000 + 900,
            speaker_id="speaker_05",
            text="补充观点" * 20,
        )
    )
    batch_results = [
        {
            "batch_index": 0,
            "segment_ids": [segment.segment_id for segment in segments],
            "findings": [],
        }
    ]

    payload, _ = _synthesis_segment_payload(segments, batch_results=batch_results)
    retained_lower_volume_characters = sum(
        len(item["text"])
        for item in payload
        if item["speaker_id"] == "speaker_05"
    )

    assert retained_lower_volume_characters >= MIN_SUBSTANTIVE_SPEAKER_CHARACTERS


def test_meeting_validation_accepts_nine_grounded_speaker_summaries() -> None:
    engine = FakeStructuringEngine()
    segments = [
        {
            "segment_id": f"seg_{index}",
            "speaker_id": f"speaker_{index:02d}",
            "text": f"第 {index} 位参与者说明自己的具体观点。",
        }
        for index in range(1, 10)
    ]
    document = engine.synthesize_document([], segments, content_type=ContentType.MEETING)
    document["discussion_threads"] = []
    document["speaker_summaries"] = [
        {
            "speaker_id": segment["speaker_id"],
            "display_name": "",
            "affiliation": "",
            "role": "",
            "summary": segment["text"],
            "evidence": [segment["segment_id"]],
        }
        for segment in segments
    ]

    validated = _validate_document(
        document,
        segment_ids={segment["segment_id"] for segment in segments},
        speaker_ids={segment["speaker_id"] for segment in segments},
        content_type=ContentType.MEETING,
        segment_texts={segment["segment_id"]: segment["text"] for segment in segments},
        segment_speakers={
            segment["segment_id"]: segment["speaker_id"] for segment in segments
        },
        segment_starts={
            segment["segment_id"]: index * 1_000
            for index, segment in enumerate(segments)
        },
    )

    assert len(validated["speaker_summaries"]) == 9


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
        assert engine.meeting_repair_calls == 1
        assert engine.discussion_thread_calls == 0
        assert engine.decision_reconcile_calls == 0
        assert engine.speaker_supplement_calls == 0
        assert engine.polish_calls >= 1
        assert all(item["text"].endswith("。") for batch in engine.extract_inputs for item in batch)
        assert len(engine.synthesize_inputs[0]) < len(
            [segment for segment in snapshot.stable_segments if segment.text]
        )
        assert engine.synthesize_inputs[0][0]["batch_range"] == {
            "batch_index": 0,
            "start_segment_id": "s0001",
            "end_segment_id": "s0004",
            "source_segment_count": 4,
        }
        assert all(item["segment_id"].startswith("s") for item in engine.synthesize_inputs[0])
        assert all(item["text"].endswith("。") for item in engine.synthesize_inputs[0])
        assert raw["prompt_version"] == NOTE_PROMPT_VERSION
        assert raw["document"]["title"] == "结构提炼测试会议"
        assert sum(
            len(batch["transcript_edits"]) for batch in raw["transcript_edit_results"]
        ) == len(snapshot.stable_segments)
        assert any(checkpoint.checkpoint_key == "structuring_result" for checkpoint in checkpoints)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_replaces_inherited_progress_before_first_model_call(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="progress-reset",
        )
        engine = FakeStructuringEngine()
        original_polish = engine.polish_transcript_batch
        observed_progress = []

        def observe_progress(segments):
            observed_progress.append(store.get_job_snapshot(job.job_id).progress)
            return original_polish(segments)

        engine.polish_transcript_batch = observe_progress
        StructuringExecutor(
            store,
            engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)

        assert observed_progress
        assert observed_progress[0] is not None
        assert observed_progress[0].stage is JobState.STRUCTURING
        assert observed_progress[0].stage_progress == pytest.approx(0.04)
        completed = store.get_job_snapshot(job.job_id).progress
        assert completed is not None
        assert completed.stage is JobState.STRUCTURING
        assert completed.stage_progress == pytest.approx(1.0)
        assert completed.detail is not None
        assert completed.detail.substage == "complete"
        telemetry = [
            checkpoint
            for checkpoint in store.list_checkpoints(job.job_id, stage="structuring_telemetry")
            if checkpoint.checkpoint_key == "latest"
        ]
        assert len(telemetry) == 1
        assert telemetry[0].payload["schema_version"] == "1.0.0"
        assert any(
            stage["substage"] == "evidence_extraction"
            for stage in telemetry[0].payload["stages"]
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_structuring_reuses_verified_batches_and_candidate_after_process_exit(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="checkpoint-resume",
        )
        interrupted_engine = ExitAfterCandidateEngine()
        with pytest.raises(SimulatedProcessExit):
            StructuringExecutor(
                store,
                interrupted_engine,
                boundary_preflight=preflight(),
            ).run(job.job_id)

        assert interrupted_engine.polish_calls > 0
        assert interrupted_engine.extract_calls > 0
        assert interrupted_engine.synthesize_calls == 1
        batch_checkpoints = store.list_checkpoints(job.job_id, stage="structuring_batches")
        candidate_checkpoints = store.list_checkpoints(
            job.job_id,
            stage="structuring_candidates",
        )
        assert batch_checkpoints
        assert any(
            checkpoint.checkpoint_key == "document_base"
            for checkpoint in candidate_checkpoints
        )

        resumed_engine = FakeStructuringEngine()
        result = StructuringExecutor(
            store,
            resumed_engine,
            boundary_preflight=preflight(),
        ).run(job.job_id)

        assert result.outcome is StructuringOutcome.COMPLETED
        assert resumed_engine.polish_calls == 0
        assert resumed_engine.extract_calls == 0
        assert resumed_engine.synthesize_calls == 0
        assert resumed_engine.classify_calls == 1
        assert resumed_engine.meeting_repair_calls == 1
        progress = store.get_job_snapshot(job.job_id).progress
        assert progress is not None
        assert progress.detail is not None
        assert progress.detail.substage == "complete"


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
def test_structuring_engine_failure_blocks_artifact_publication(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=source_probe_for(95),
    ) as store:
        job = create_structuring_job(
            store,
            duration_seconds=95,
            suffix="fallback",
        )
        with pytest.raises(StructuringFailed, match="publication was blocked"):
            StructuringExecutor(
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

        assert store.get_job(job.job_id).state is JobState.STRUCTURING
        assert evidence.payload["document_available"] is False
        assert evidence.payload["finding_count"] == 0
        assert evidence.payload["unavailable_reason_code"] == "RuntimeError"
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
    ("kind", "text"),
    [
        ("action_item", "不错。"),
        ("action_item", "你写上，写备注嘛。"),
        ("question", "你。"),
        ("question", "哈哈哈哈哈！"),
        ("decision", "嗯。"),
        ("topic", "那个那个那个那个。"),
    ],
)
def test_findings_reject_conversational_debris(kind: str, text: str) -> None:
    assert _is_substantive_finding_text(FindingKind(kind), text) is False


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("action_item", "国伟负责开发数据治理演示版本。"),
        ("question", "库存数据由哪个系统提供，维护责任归谁？"),
        ("decision", "会议确认先统一数据标准，再开展系统对接。"),
    ],
)
def test_findings_keep_substantive_meeting_outcomes(kind: str, text: str) -> None:
    assert _is_substantive_finding_text(FindingKind(kind), text) is True


def test_ollama_document_synthesis_retries_truncated_json(monkeypatch) -> None:
    engine = OllamaStructuringEngine(model="qwen3:14b", editor_model="qwen3:8b")
    responses = iter(['{"title":"未闭合"', '{"title":"完整文档"}'])
    requests = []

    def generate(prompt, **kwargs):
        requests.append({"prompt": prompt, **kwargs})
        return next(responses)

    monkeypatch.setattr(engine, "_generate", generate)

    document = engine.synthesize_document(
        [],
        [{"segment_id": "seg_retry", "text": "完整原文。", "speaker_id": "speaker_1"}],
        content_type=ContentType.MEETING,
    )

    assert document == {"title": "完整文档"}
    assert len(requests) == 2
    assert requests[0]["num_predict"] == 4608
    assert requests[1]["num_predict"] == 6144
    assert requests[1]["retry_attempt"] == 1
    assert "上一次输出未形成完整 JSON" in requests[1]["prompt"]


def test_meeting_quality_editor_uses_one_unified_full_document_call(monkeypatch) -> None:
    engine = OllamaStructuringEngine(model="qwen3:14b", editor_model="qwen3:8b")
    document = FakeStructuringEngine().synthesize_document(
        [],
        [{"segment_id": "seg_1", "speaker_id": "speaker_1", "text": "项目范围。"}],
        content_type=ContentType.MEETING,
    )
    document["discussion_threads"] = []
    requests = []

    def generate(prompt, **kwargs):
        requests.append({"prompt": prompt, **kwargs})
        return json.dumps(document, ensure_ascii=False)

    monkeypatch.setattr(engine, "_generate", generate)
    result = engine.refine_meeting_document(
        document,
        [{"segment_id": "seg_1", "speaker_id": "speaker_1", "text": "项目范围。"}],
    )

    assert result["title"] == document["title"]
    assert len(requests) == 1
    assert requests[0]["num_ctx"] == 65_536
    assert requests[0]["num_predict"] == 6144
    assert "一次质量编辑" in requests[0]["prompt"]
    assert {
        "discussion_threads",
        "speaker_summaries",
        "decisions",
        "actions",
        "risks",
        "open_questions",
    }.issubset(requests[0]["format_schema"]["required"])


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
    assert "完整逐字稿的分层阅读包" in captured["prompt"]
    assert "独立于内容类型模板的顺序摘要" in captured["prompt"]


def test_meeting_profile_preserves_approved_document_contract() -> None:
    schema = _document_json_schema(ContentType.MEETING)

    assert "scene_sections" not in schema["properties"]
    assert "topics" in schema["required"]
    assert "discussion_threads" in schema["required"]
    assert "timeline_sections" in schema["required"]


def test_meeting_quality_repair_preserves_timeline_and_accepts_unified_discussion_edit() -> None:
    class MeetingEditor:
        model_id = "meeting-editor"

        def refine_meeting_document(self, document, segments):
            return {
                **{
                    key: value
                    for key, value in document.items()
                    if key not in {"discussion_threads", "timeline_sections"}
                },
                "discussion_threads": [{"title": "质量编辑后的方案选择"}],
            }

    executor = object.__new__(StructuringExecutor)
    executor.engine = MeetingEditor()
    document = {
        "title": "项目会",
        "timeline_sections": [{"title": "第一阶段"}],
        "discussion_threads": [{"title": "方案选择"}],
    }

    repaired = executor._repair_meeting_quality(document, [], aliases={})

    assert repaired["timeline_sections"] == document["timeline_sections"]
    assert repaired["discussion_threads"] == [{"title": "质量编辑后的方案选择"}]


def test_quality_repair_sanitizes_unknown_and_excess_evidence_references() -> None:
    repaired = _sanitize_quality_evidence_references(
        {
            "summary": {
                "text": "校订后的摘要",
                "evidence": ["seg_1", "unknown", "seg_2", "seg_3", "seg_4"],
            },
            "highlights": [{"text": "要点", "evidence": ["unknown"]}],
            "decisions": [{"text": "无有效证据的新增决定", "evidence": ["unknown"]}],
        },
        fallback={
            "summary": {"text": "原摘要", "evidence": ["seg_4"]},
            "highlights": [{"text": "原要点", "evidence": ["seg_2"]}],
            "decisions": [],
        },
        segment_ids={"seg_1", "seg_2", "seg_3", "seg_4"},
    )

    assert repaired["summary"]["evidence"] == ["seg_1", "seg_2", "seg_3"]
    assert repaired["highlights"][0]["evidence"] == ["seg_2"]
    assert repaired["decisions"] == []


def test_quality_repair_deduplicates_items_across_note_categories() -> None:
    repaired = _dedupe_document_categories(
        {
            "decisions": [{"text": "按两批上线。", "evidence": ["seg_1"]}],
            "actions": [
                {"task": "按两批上线", "owner": "", "deadline": "", "evidence": ["seg_1"]},
                {"task": "整理清单", "owner": "", "deadline": "", "evidence": ["seg_2"]},
            ],
            "risks": [{"text": "容量不足", "evidence": ["seg_3"]}],
            "open_questions": [
                {"text": "容量不足。", "evidence": ["seg_3"]},
                {"text": "何时复核？", "evidence": ["seg_4"]},
            ],
        }
    )

    assert [item["text"] for item in repaired["decisions"]] == ["按两批上线。"]
    assert [item["task"] for item in repaired["actions"]] == ["整理清单"]
    assert [item["text"] for item in repaired["risks"]] == ["容量不足"]
    assert [item["text"] for item in repaired["open_questions"]] == ["何时复核？"]


def test_quality_repair_merges_semantically_duplicate_actions() -> None:
    repaired = _dedupe_document_categories(
        {
            "actions": [
                {
                    "task": "提供多仓数据表",
                    "owner": "",
                    "deadline": "",
                    "evidence": ["seg_1"],
                },
                {
                    "task": "提供产线工序数据和多仓表数据",
                    "owner": "数据团队",
                    "deadline": "",
                    "evidence": ["seg_2"],
                },
            ]
        }
    )

    assert repaired["actions"] == [
        {
            "task": "提供产线工序数据和多仓表数据",
            "owner": "数据团队",
            "deadline": "",
            "evidence": ["seg_2", "seg_1"],
        }
    ]


def test_meeting_question_is_removed_when_action_already_owns_it() -> None:
    assert _meeting_question_is_covered_by_action(
        "如何确保数据与周计划时间点对齐？",
        [
            {
                "task": "确认数据导出时间与周计划时间点对齐",
                "owner": "",
                "deadline": "",
                "evidence": ["seg_1"],
            }
        ],
    )


def test_meeting_question_is_removed_when_later_transcript_answers_it() -> None:
    assert not _meeting_question_remains_open(
        ["seg_question"],
        segment_texts={
            "seg_question": "是不是不需要一直更新？",
            "seg_answer": "到后面再匹配就行了。",
        },
        segment_starts={"seg_question": 1_000, "seg_answer": 5_000},
    )


def test_unsupported_speaker_host_role_is_removed() -> None:
    assert _remove_unsupported_speaker_host_claim(
        "会议主持人，提出了数据完整性问题。",
        ["seg_1"],
        {"seg_1": "先看一下数据是不是完整。"},
    ) == "提出了数据完整性问题。"


def test_embedded_question_is_not_allowed_as_meeting_risk_or_highlight() -> None:
    assert _meeting_highlight_is_question("数据导出后是否需要人工干预")
    assert not _meeting_highlight_is_question("明确是否采用实时更新机制")


def test_summary_drops_unresolved_claim_when_no_open_question_survives() -> None:
    assert _remove_unsupported_open_question_claims(
        "会议明确了数据处理任务。部分议题仍存在分歧，如是否持续更新数据。"
    ) == "会议明确了数据处理任务。"


def test_time_bound_highlight_is_promoted_to_action_without_duplication() -> None:
    actions = _promote_meeting_actionable_highlights(
        [
            {
                "text": "今日内完成数据导出和计划跑通",
                "evidence": ["seg_1"],
            }
        ],
        actions=[],
        segment_texts={"seg_1": "今天把数据给我之后，再重新跑插件。"},
    )

    assert actions == [
        {
            "task": "完成数据导出和计划跑通",
            "owner": "",
            "deadline": "今天",
            "evidence": ["seg_1"],
        }
    ]


def test_empty_decisions_remove_generic_decision_claim_from_summary() -> None:
    assert _remove_unsupported_decision_claims("会议最终达成若干决定。") == (
        "会议未形成可由逐字稿明确确认的决定。"
    )


def test_transcript_editor_accepts_sparse_changes_and_drops_unchanged_items() -> None:
    edits = _validate_transcript_edits(
        [
            {"segment_id": "seg_1", "text": "原文一。"},
            {"segment_id": "seg_2", "text": "校订后的原文二。"},
        ],
        expected_segment_ids={"seg_1", "seg_2", "seg_3"},
        source_texts={"seg_1": "原文一。", "seg_2": "原文二。", "seg_3": "原文三。"},
    )

    assert edits == ({"segment_id": "seg_2", "text": "校订后的原文二。"},)


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
