"""Content-type classification and evidence-linked extraction over stable text."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from speech_capture_worker.audio_preprocessing import AudioPreprocessor, NormalizedAudioPlan
from speech_capture_worker.domain import JobRecord, JobState, ResourceStatus
from speech_capture_worker.errors import (
    InvalidJobRequest,
    StructuringFailed,
    UploadStorageError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import GIB, ResourceReport, check_resource_preflight
from speech_capture_worker.transcript import TranscriptSegment

STRUCTURING_SCHEMA_VERSION = "1.1.0"
STRUCTURING_RAW_SCHEMA_VERSION = "1.1.0"
STRUCTURING_STAGE = "structuring"
STRUCTURING_CHECKPOINT_KEY = "structuring_result"
STRUCTURING_HEADROOM_BYTES = GIB
DEFAULT_BATCH_MAX_CHARS = 6000
DEFAULT_EDITOR_BATCH_MAX_CHARS = 2400
MAX_FINDING_TEXT_CHARACTERS = 2000
MAX_TRAIT_COUNT = 20
MAX_DOCUMENT_TITLE_CHARACTERS = 120
MAX_DOCUMENT_TEXT_CHARACTERS = 3000
MAX_DOCUMENT_ITEMS = 20


class ContentType(StrEnum):
    MEETING = "meeting"
    INTERVIEW = "interview"
    COURSE = "course"
    SPEECH = "speech"
    VOICE_MEMO = "voice_memo"
    GENERIC = "generic"


class FindingKind(StrEnum):
    DECISION = "decision"
    ACTION_ITEM = "action_item"
    FACT = "fact"
    QUESTION = "question"
    DISAGREEMENT = "disagreement"
    UNCERTAINTY = "uncertainty"
    DEADLINE = "deadline"
    TOPIC = "topic"
    IDEA = "idea"
    NEXT_STEP = "next_step"


CLASSIFICATION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [value.value for value in ContentType]},
        "traits": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_TRAIT_COUNT,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["type", "traits", "confidence"],
    "additionalProperties": False,
}

FINDINGS_JSON_SCHEMA = {
    "type": "array",
    "maxItems": 30,
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [value.value for value in FindingKind]},
            "text": {"type": "string", "minLength": 1},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["kind", "text", "evidence", "confidence"],
        "additionalProperties": False,
    },
}

EVIDENCE_TEXT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 12,
        },
    },
    "required": ["text", "evidence"],
    "additionalProperties": False,
}

DOCUMENT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "summary": EVIDENCE_TEXT_JSON_SCHEMA,
        "highlights": {
            "type": "array",
            "items": EVIDENCE_TEXT_JSON_SCHEMA,
            "minItems": 5,
            "maxItems": 8,
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "summary": {"type": "string", "minLength": 1},
                    "details": {
                        "type": "array",
                        "items": EVIDENCE_TEXT_JSON_SCHEMA,
                        "maxItems": 8,
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 12,
                    },
                },
                "required": ["title", "summary", "details", "evidence"],
                "additionalProperties": False,
            },
            "minItems": 5,
            "maxItems": 10,
        },
        "decisions": {
            "type": "array",
            "items": EVIDENCE_TEXT_JSON_SCHEMA,
            "maxItems": 15,
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "minLength": 1},
                    "owner": {"type": "string"},
                    "deadline": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 12,
                    },
                },
                "required": ["task", "owner", "deadline", "evidence"],
                "additionalProperties": False,
            },
            "maxItems": 15,
        },
        "risks": {
            "type": "array",
            "items": EVIDENCE_TEXT_JSON_SCHEMA,
            "maxItems": 15,
        },
        "open_questions": {
            "type": "array",
            "items": EVIDENCE_TEXT_JSON_SCHEMA,
            "maxItems": 15,
        },
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "summary": {"type": "string", "minLength": 1},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 12,
                    },
                },
                "required": ["title", "summary", "evidence"],
                "additionalProperties": False,
            },
            "minItems": 6,
            "maxItems": 15,
        },
    },
    "required": [
        "title",
        "summary",
        "highlights",
        "topics",
        "decisions",
        "actions",
        "risks",
        "open_questions",
        "chapters",
    ],
    "additionalProperties": False,
}

TRANSCRIPT_EDITS_JSON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "segment_id": {"type": "string", "minLength": 1},
            "text": {"type": "string", "minLength": 1},
        },
        "required": ["segment_id", "text"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class ContentClassification:
    type: ContentType
    traits: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "traits": list(self.traits),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Finding:
    finding_id: str
    kind: FindingKind
    text: str
    evidence: tuple[str, ...]
    confidence: float
    unsupported: bool
    occurrences: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StructuringEngine(Protocol):
    model_id: str

    def classify(
        self,
        segments: list[dict[str, Any]],
        *,
        speaker_count: int,
    ) -> dict[str, Any]: ...

    def extract_batch(
        self,
        segments: list[dict[str, Any]],
        *,
        content_type: ContentType,
    ) -> list[dict[str, Any]]: ...

    def synthesize_document(
        self,
        findings: list[dict[str, Any]],
        segments: list[dict[str, Any]],
        *,
        content_type: ContentType,
    ) -> dict[str, Any]: ...

    def polish_transcript_batch(
        self,
        segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


class OllamaStructuringEngine:
    """Lazy local Ollama adapter; requires a running Ollama server."""

    def __init__(
        self,
        *,
        model: str = "qwen3:14b",
        editor_model: str = "qwen3:8b",
    ) -> None:
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise InvalidJobRequest("Ollama model name is invalid.")
        if (
            not isinstance(editor_model, str)
            or not editor_model.strip()
            or len(editor_model) > 200
        ):
            raise InvalidJobRequest("Ollama editor model name is invalid.")
        self.model = model.strip()
        self.editor_model = editor_model.strip()
        self.model_id = f"ollama/{self.model};editor={self.editor_model}"

    def classify(
        self,
        segments: list[dict[str, Any]],
        *,
        speaker_count: int,
    ) -> dict[str, Any]:
        prompt = (
            "你是语音记录内容分类器。只返回 JSON，不要解释。"
            'JSON 字段为 {"type":"...","traits":[...],"confidence":0.0-1.0}。'
            "type 只能是 meeting、interview、course、speech、voice_memo、generic。"
            f"说话人数：{speaker_count}。文字段：\n" + json.dumps(segments, ensure_ascii=False)
        )
        response = self._generate(
            prompt,
            format_schema=CLASSIFICATION_JSON_SCHEMA,
            num_predict=512,
        )
        return _parse_json_object(response)

    def extract_batch(
        self,
        segments: list[dict[str, Any]],
        *,
        content_type: ContentType,
    ) -> list[dict[str, Any]]:
        prompt = (
            "你是语音记录信息提炼器。只返回 JSON 数组，不要解释。"
            '每项字段为 {"kind":"...","text":"...","evidence":["segment_id"],'
            '"confidence":0.0-1.0}。'
            "kind 只能是 decision、action_item、fact、question、disagreement、"
            "uncertainty、deadline、topic、idea、next_step。"
            "evidence 必须来自下方给出的 segment_id，不能编造没有证据的内容。"
            "只提取本批最重要的 8-20 条信息，合并重复表达，不要把每句话都列成一项。"
            f"内容类型：{content_type}。文字段：\n" + json.dumps(segments, ensure_ascii=False)
        )
        response = self._generate(
            prompt,
            format_schema=FINDINGS_JSON_SCHEMA,
            num_predict=3072,
        )
        return _parse_json_list(response)

    def synthesize_document(
        self,
        findings: list[dict[str, Any]],
        segments: list[dict[str, Any]],
        *,
        content_type: ContentType,
    ) -> dict[str, Any]:
        prompt = (
            "你是资深中文内容编辑。请把分批提取的信息合并为一篇可直接使用的完整笔记，"
            "而不是转写片段清单。只返回符合 schema 的 JSON，不要解释。\n"
            "要求：title 要具体；summary 用一段 120-300 字的连贯文字讲清背景、核心讨论和结果；"
            "highlights 保留最重要的 5-8 点；topics 按语义合并成 5-8 个主题，每个主题写概述并列出"
            "2-6 条具体信息；decisions 只写明确达成的结论；actions 必须写清任务，只有原文明确时"
            "才填 owner 和 deadline，否则填空字符串；risks 与 open_questions 分开；chapters 按时间"
            "顺序写 6-12 个章节并覆盖主要内容。主题应在证据存在时覆盖业务背景与目标、优先场景、"
            "实施路径、组织协作、技术平台、案例成效以及风险；每个主题的概述必须与其 details 对应。"
            "合并重复待办，避免空话、重复和 Meta 信息，保留人名、数字、范围和时间。\n"
            "所有 evidence 必须使用输入中的 segment_id，且每一项至少有一个证据；不得补写原文没有"
            "的信息。可以基于同一证据做忠实归纳。\n"
            f"内容类型：{content_type.value}\n"
            "已提取信息：\n"
            + json.dumps(findings, ensure_ascii=False)
            + "\n证据原文：\n"
            + json.dumps(segments, ensure_ascii=False)
        )
        response = self._generate(
            prompt,
            format_schema=DOCUMENT_JSON_SCHEMA,
            num_predict=6144,
            timeout_seconds=1200,
        )
        return _parse_json_object(response)

    def polish_transcript_batch(
        self,
        segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        prompt = (
            "你是中文逐字稿校订员。只返回 JSON 数组，不要解释。每项只包含 segment_id 和 text，"
            "必须与输入逐项对应，不能漏项、增项或改变 segment_id。请补全标点和自然分句，清理明显"
            "的口吃式重复与无意义语气词，并仅在上下文明确时修正同音错词。不得概括、删减有效信息、"
            "改变数字、人名、专有名词或说话含义。输入：\n"
            + json.dumps(segments, ensure_ascii=False)
        )
        response = self._generate(
            prompt,
            format_schema=TRANSCRIPT_EDITS_JSON_SCHEMA,
            model=self.editor_model,
            num_predict=8192,
        )
        return _parse_json_list(response)

    def _generate(
        self,
        prompt: str,
        *,
        format_schema: dict[str, Any],
        model: str | None = None,
        num_predict: int,
        timeout_seconds: int = 600,
    ) -> str:
        payload = json.dumps(
            {
                "model": model or self.model,
                "prompt": "/no_think\n" + prompt,
                "stream": False,
                "think": False,
                "format": format_schema,
                "options": {
                    "temperature": 0.2,
                    "num_ctx": 16384,
                    "num_predict": num_predict,
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise StructuringFailed(
                "The local Ollama engine could not be reached.",
                details={"exception_type": type(exc).__name__},
            ) from exc
        value = body.get("response")
        if not isinstance(value, str) or not value.strip():
            raise StructuringFailed("The local Ollama engine returned an empty response.")
        return value.strip()


class StructuringOutcome(StrEnum):
    COMPLETED = "completed"
    REPLAYED = "replayed"
    REGENERATED = "regenerated"
    SAFE_PAUSED = "safe_paused"
    ALREADY_COMPLETED = "already_completed"


@dataclass(frozen=True)
class StructuringResult:
    outcome: StructuringOutcome
    job: JobRecord
    evidence_checkpoint_generation: int | None
    content_type: ContentType | None
    finding_count: int
    unsupported_finding_count: int
    batch_count: int
    unavailable_reason_code: str | None
    resource_report: ResourceReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict(),
            "evidence_checkpoint_generation": self.evidence_checkpoint_generation,
            "content_type": self.content_type.value if self.content_type is not None else None,
            "finding_count": self.finding_count,
            "unsupported_finding_count": self.unsupported_finding_count,
            "batch_count": self.batch_count,
            "unavailable_reason_code": self.unavailable_reason_code,
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
        }


BoundaryPreflight = Callable[..., ResourceReport]


class StructuringExecutor:
    """Classify and extract once over stable segments, durably."""

    def __init__(
        self,
        store: JobStore,
        engine: StructuringEngine,
        *,
        preprocessor: AudioPreprocessor | None = None,
        boundary_preflight: BoundaryPreflight = check_resource_preflight,
        batch_max_chars: int = DEFAULT_BATCH_MAX_CHARS,
    ) -> None:
        if (
            not isinstance(engine.model_id, str)
            or not engine.model_id
            or len(engine.model_id) > 200
            or any(not character.isprintable() for character in engine.model_id)
        ):
            raise InvalidJobRequest("Structuring engine model_id is invalid.")
        if (
            not isinstance(batch_max_chars, int)
            or isinstance(batch_max_chars, bool)
            or batch_max_chars < 1000
            or batch_max_chars > 50_000
        ):
            raise InvalidJobRequest("batch_max_chars must be between 1000 and 50000.")
        self.store = store
        self.engine = engine
        self.preprocessor = preprocessor or AudioPreprocessor(store)
        self._boundary_preflight = boundary_preflight
        self._batch_max_chars = batch_max_chars

    def run(self, job_id: str, *, force: bool = False) -> StructuringResult:
        if not isinstance(force, bool):
            raise InvalidJobRequest("force must be a boolean.")
        job = self.store.get_job(job_id)
        if job.state in {JobState.QUALITY_CHECK, JobState.PROCESSED} and not force:
            return StructuringResult(
                outcome=StructuringOutcome.ALREADY_COMPLETED,
                job=job,
                evidence_checkpoint_generation=None,
                content_type=None,
                finding_count=0,
                unsupported_finding_count=0,
                batch_count=0,
                unavailable_reason_code=None,
                resource_report=None,
            )
        if job.state not in {
            JobState.STRUCTURING,
            JobState.QUALITY_CHECK,
            JobState.PROCESSED,
        }:
            raise InvalidJobRequest(
                "Structuring requires a structuring, quality-check, or processed job."
            )

        plan = self.preprocessor.get_plan(job_id)
        segments = self._list_all_segments(job_id)
        segments_sha256 = _segments_identity_sha256(segments)
        transcribed = [segment for segment in segments if segment.text]
        evidence = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=STRUCTURING_STAGE),
            STRUCTURING_CHECKPOINT_KEY,
        )
        if force:
            evidence = None
        resource_report: ResourceReport | None = None
        replayed = evidence is not None
        if evidence is None:
            resource_report = self._boundary_preflight(
                self.store.data_directory,
                estimated_required_bytes=STRUCTURING_HEADROOM_BYTES,
                model_profile=job.model_profile,
            )
            self.store.put_checkpoint(
                job_id,
                stage=STRUCTURING_STAGE,
                checkpoint_key="structuring_resource_boundary",
                payload=resource_report.to_dict(),
            )
            if resource_report.status is ResourceStatus.BLOCKED:
                if job.state in {JobState.QUALITY_CHECK, JobState.PROCESSED}:
                    return StructuringResult(
                        outcome=StructuringOutcome.SAFE_PAUSED,
                        job=job,
                        evidence_checkpoint_generation=None,
                        content_type=None,
                        finding_count=0,
                        unsupported_finding_count=0,
                        batch_count=0,
                        unavailable_reason_code=None,
                        resource_report=resource_report,
                    )
                current = self.store.get_job(job_id)
                paused = self.store.transition_job(
                    job_id,
                    JobState.PAUSED,
                    expected_revision=current.revision,
                    reason_code="structuring_resource_blocked",
                    error_code="STRUCTURING_RESOURCE_BLOCKED",
                    error_message=("Worker resources must recover before structuring can start."),
                    event_type="resource.safe_paused",
                )
                return StructuringResult(
                    outcome=StructuringOutcome.SAFE_PAUSED,
                    job=paused,
                    evidence_checkpoint_generation=None,
                    content_type=None,
                    finding_count=0,
                    unsupported_finding_count=0,
                    batch_count=0,
                    unavailable_reason_code=None,
                    resource_report=resource_report,
                )

            speaker_count = len(
                {segment.speaker_id for segment in transcribed if segment.speaker_id}
            )
            segment_payload = _segment_payload(transcribed)
            started = time.monotonic()
            unavailable_reasons: list[str] = []
            try:
                classification = _validate_classification(
                    self.engine.classify(
                        segment_payload,
                        speaker_count=speaker_count,
                    )
                )
            except Exception as exc:
                unavailable_reasons.append(type(exc).__name__)
                classification = ContentClassification(
                    type=ContentType.GENERIC,
                    traits=(),
                    confidence=0.0,
                )
            batches = _build_batches(
                transcribed,
                max_chars=self._batch_max_chars,
            )
            batch_results: list[dict[str, Any]] = []
            valid_segment_ids = {segment.segment_id for segment in segments}
            for index, batch in enumerate(batches):
                batch_error: str | None = None
                batch_payload = _segment_payload(batch)
                try:
                    batch_findings = _validate_findings(
                        self.engine.extract_batch(
                            batch_payload,
                            content_type=classification.type,
                        ),
                        segment_ids=valid_segment_ids,
                    )
                except Exception as exc:
                    batch_error = type(exc).__name__
                    unavailable_reasons.append(batch_error)
                    batch_findings = ()
                batch_results.append(
                    {
                        "batch_index": index,
                        "segment_ids": [segment.segment_id for segment in batch],
                        "findings": [finding.to_dict() for finding in batch_findings],
                        "unavailable_reason_code": batch_error,
                    }
                )
            findings = _merge_findings(batch_results)
            document: dict[str, Any] | None = None
            document_error: str | None = None
            if findings:
                try:
                    finding_payload = [finding.to_dict() for finding in findings]
                    document = _validate_document(
                        self.engine.synthesize_document(
                            finding_payload,
                            _document_evidence_payload(transcribed, findings),
                            content_type=classification.type,
                        ),
                        segment_ids=valid_segment_ids,
                    )
                except Exception as exc:
                    document_error = type(exc).__name__
                    unavailable_reasons.append(document_error)
            transcript_edit_results: list[dict[str, Any]] = []
            editor_batches = _build_batches(
                transcribed,
                max_chars=DEFAULT_EDITOR_BATCH_MAX_CHARS,
            )
            for index, batch in enumerate(editor_batches):
                edit_error: str | None = None
                try:
                    transcript_edits = _validate_transcript_edits(
                        self.engine.polish_transcript_batch(_segment_payload(batch)),
                        expected_segment_ids={segment.segment_id for segment in batch},
                    )
                except Exception as exc:
                    edit_error = type(exc).__name__
                    unavailable_reasons.append(edit_error)
                    transcript_edits = ()
                transcript_edit_results.append(
                    {
                        "batch_index": index,
                        "segment_ids": [segment.segment_id for segment in batch],
                        "transcript_edits": list(transcript_edits),
                        "unavailable_reason_code": edit_error,
                    }
                )
            unavailable_reason = (
                ",".join(dict.fromkeys(unavailable_reasons)) if unavailable_reasons else None
            )
            elapsed_seconds = time.monotonic() - started
            raw_payload = {
                "schema_version": STRUCTURING_RAW_SCHEMA_VERSION,
                "model_id": self.engine.model_id,
                "normalized_sha256": plan.normalized_sha256,
                "segments_sha256": segments_sha256,
                "unavailable_reason_code": unavailable_reason,
                "classification": classification.to_dict(),
                "batch_results": batch_results,
                "document": document,
                "document_unavailable_reason_code": document_error,
                "transcript_edit_results": transcript_edit_results,
            }
            raw_bytes = _canonical_json(raw_payload).encode("utf-8")
            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            raw_relative_path = self._write_private_evidence(
                job_id,
                raw_sha256=raw_sha256,
                raw_bytes=raw_bytes,
            )
            evidence, _ = self.store.put_checkpoint(
                job_id,
                stage=STRUCTURING_STAGE,
                checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
                payload={
                    "schema_version": STRUCTURING_SCHEMA_VERSION,
                    "model_id": self.engine.model_id,
                    "normalized_sha256": plan.normalized_sha256,
                    "segments_sha256": segments_sha256,
                    "content_type": classification.type,
                    "content_traits": list(classification.traits),
                    "content_confidence": classification.confidence,
                    "finding_count": len(findings),
                    "unsupported_finding_count": sum(finding.unsupported for finding in findings),
                    "document_available": document is not None,
                    "batch_count": len(batches),
                    "transcript_edit_batch_count": len(editor_batches),
                    "unavailable_reason_code": unavailable_reason,
                    "raw_relative_path": raw_relative_path,
                    "raw_sha256": raw_sha256,
                    "elapsed_seconds": round(elapsed_seconds, 6),
                },
            )
        else:
            classification, findings = self._load_durable_evidence(
                job_id,
                evidence=evidence,
                plan=plan,
                segments_sha256=segments_sha256,
            )

        prior_progress = self.store.get_job_snapshot(job_id).progress
        prior_elapsed_seconds = (
            float(prior_progress.elapsed_seconds) if prior_progress is not None else 0.0
        )
        elapsed_seconds = prior_elapsed_seconds + float(
            evidence.payload.get("elapsed_seconds", 0) or 0
        )
        if job.state is JobState.STRUCTURING:
            self.store.put_job_progress(
                job_id,
                processed_ms=self.store.get_job_duration_ms(job_id),
                stage_progress=1.0,
                elapsed_seconds=elapsed_seconds,
            )
        current = self.store.get_job(job_id)
        if job.state is JobState.STRUCTURING:
            result_job = self.store.transition_job(
                job_id,
                JobState.QUALITY_CHECK,
                expected_revision=current.revision,
                reason_code="structuring_complete",
                event_type="job.structuring_completed",
            )
        else:
            result_job = current
        return StructuringResult(
            outcome=(
                StructuringOutcome.REGENERATED
                if force
                else StructuringOutcome.REPLAYED
                if replayed
                else StructuringOutcome.COMPLETED
            ),
            job=result_job,
            evidence_checkpoint_generation=evidence.generation,
            content_type=classification.type,
            finding_count=len(findings),
            unsupported_finding_count=sum(finding.unsupported for finding in findings),
            batch_count=int(evidence.payload.get("batch_count", 0)),
            unavailable_reason_code=evidence.payload.get("unavailable_reason_code"),
            resource_report=resource_report,
        )

    def resynthesize_document(self, job_id: str) -> StructuringResult:
        """Regenerate only the global document from durable extraction evidence."""
        job = self.store.get_job(job_id)
        if job.state not in {JobState.QUALITY_CHECK, JobState.PROCESSED}:
            raise InvalidJobRequest(
                "Document re-synthesis requires a quality-check or processed job."
            )
        plan = self.preprocessor.get_plan(job_id)
        segments = self._list_all_segments(job_id)
        segments_sha256 = _segments_identity_sha256(segments)
        evidence = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=STRUCTURING_STAGE),
            STRUCTURING_CHECKPOINT_KEY,
        )
        if evidence is None:
            raise StructuringFailed("Document re-synthesis requires structuring evidence.")
        payload = evidence.payload
        if (
            payload.get("schema_version") != STRUCTURING_SCHEMA_VERSION
            or payload.get("model_id") != self.engine.model_id
            or payload.get("normalized_sha256") != plan.normalized_sha256
            or payload.get("segments_sha256") != segments_sha256
            or not isinstance(payload.get("raw_relative_path"), str)
            or not isinstance(payload.get("raw_sha256"), str)
        ):
            raise StructuringFailed("Document re-synthesis evidence is incompatible.")

        resource_report = self._boundary_preflight(
            self.store.data_directory,
            estimated_required_bytes=STRUCTURING_HEADROOM_BYTES,
            model_profile=job.model_profile,
        )
        if resource_report.status is ResourceStatus.BLOCKED:
            return StructuringResult(
                outcome=StructuringOutcome.SAFE_PAUSED,
                job=job,
                evidence_checkpoint_generation=evidence.generation,
                content_type=None,
                finding_count=0,
                unsupported_finding_count=0,
                batch_count=int(payload.get("batch_count", 0)),
                unavailable_reason_code=None,
                resource_report=resource_report,
            )

        raw_payload = self._read_private_evidence(
            job_id,
            relative_path=payload["raw_relative_path"],
            expected_sha256=payload["raw_sha256"],
        )
        if not isinstance(raw_payload.get("batch_results"), list) or not isinstance(
            raw_payload.get("classification"), dict
        ):
            raise StructuringFailed("Document re-synthesis evidence is incomplete.")
        classification = _validate_classification(raw_payload["classification"])
        findings = _merge_findings(raw_payload["batch_results"])
        if not findings:
            raise StructuringFailed("Document re-synthesis requires supported findings.")

        transcribed = [segment for segment in segments if segment.text]
        started = time.monotonic()
        document = _validate_document(
            self.engine.synthesize_document(
                [finding.to_dict() for finding in findings],
                _document_evidence_payload(transcribed, findings),
                content_type=classification.type,
            ),
            segment_ids={segment.segment_id for segment in segments},
        )
        elapsed_seconds = time.monotonic() - started
        unavailable_reasons = [
            result.get("unavailable_reason_code")
            for key in ("batch_results", "transcript_edit_results")
            for result in raw_payload.get(key, [])
            if result.get("unavailable_reason_code")
        ]
        raw_payload["document"] = document
        raw_payload["document_unavailable_reason_code"] = None
        raw_payload["unavailable_reason_code"] = (
            ",".join(dict.fromkeys(unavailable_reasons)) if unavailable_reasons else None
        )
        raw_bytes = _canonical_json(raw_payload).encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        raw_relative_path = self._write_private_evidence(
            job_id,
            raw_sha256=raw_sha256,
            raw_bytes=raw_bytes,
        )
        checkpoint_payload = dict(payload)
        checkpoint_payload.update(
            {
                "document_available": True,
                "unavailable_reason_code": raw_payload["unavailable_reason_code"],
                "raw_relative_path": raw_relative_path,
                "raw_sha256": raw_sha256,
                "elapsed_seconds": round(
                    float(payload.get("elapsed_seconds", 0) or 0) + elapsed_seconds,
                    6,
                ),
            }
        )
        updated, _ = self.store.put_checkpoint(
            job_id,
            stage=STRUCTURING_STAGE,
            checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
            payload=checkpoint_payload,
        )
        return StructuringResult(
            outcome=StructuringOutcome.REGENERATED,
            job=job,
            evidence_checkpoint_generation=updated.generation,
            content_type=classification.type,
            finding_count=len(findings),
            unsupported_finding_count=sum(finding.unsupported for finding in findings),
            batch_count=int(payload.get("batch_count", 0)),
            unavailable_reason_code=raw_payload["unavailable_reason_code"],
            resource_report=resource_report,
        )

    def repair_transcript_edits(self, job_id: str) -> StructuringResult:
        """Retry only failed transcript-edit batches with smaller contexts."""
        job = self.store.get_job(job_id)
        if job.state not in {JobState.QUALITY_CHECK, JobState.PROCESSED}:
            raise InvalidJobRequest(
                "Transcript-edit repair requires a quality-check or processed job."
            )
        plan = self.preprocessor.get_plan(job_id)
        segments = self._list_all_segments(job_id)
        segments_sha256 = _segments_identity_sha256(segments)
        evidence = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=STRUCTURING_STAGE),
            STRUCTURING_CHECKPOINT_KEY,
        )
        if evidence is None:
            raise StructuringFailed("Transcript-edit repair requires structuring evidence.")
        payload = evidence.payload
        if (
            payload.get("schema_version") != STRUCTURING_SCHEMA_VERSION
            or payload.get("model_id") != self.engine.model_id
            or payload.get("normalized_sha256") != plan.normalized_sha256
            or payload.get("segments_sha256") != segments_sha256
            or not isinstance(payload.get("raw_relative_path"), str)
            or not isinstance(payload.get("raw_sha256"), str)
        ):
            raise StructuringFailed("Transcript-edit repair evidence is incompatible.")

        resource_report = self._boundary_preflight(
            self.store.data_directory,
            estimated_required_bytes=STRUCTURING_HEADROOM_BYTES,
            model_profile=job.model_profile,
        )
        if resource_report.status is ResourceStatus.BLOCKED:
            return StructuringResult(
                outcome=StructuringOutcome.SAFE_PAUSED,
                job=job,
                evidence_checkpoint_generation=evidence.generation,
                content_type=None,
                finding_count=0,
                unsupported_finding_count=0,
                batch_count=int(payload.get("batch_count", 0)),
                unavailable_reason_code=None,
                resource_report=resource_report,
            )

        raw_payload = self._read_private_evidence(
            job_id,
            relative_path=payload["raw_relative_path"],
            expected_sha256=payload["raw_sha256"],
        )
        edit_results = raw_payload.get("transcript_edit_results")
        if not isinstance(edit_results, list):
            raise StructuringFailed("Transcript-edit repair evidence is incomplete.")
        segment_map = {segment.segment_id: segment for segment in segments}
        retained = [
            result
            for result in edit_results
            if not result.get("unavailable_reason_code")
        ]
        failed_segments = [
            segment_map[segment_id]
            for result in edit_results
            if result.get("unavailable_reason_code")
            for segment_id in result.get("segment_ids", [])
            if segment_id in segment_map
        ]
        if not failed_segments:
            classification = _validate_classification(raw_payload["classification"])
            findings = _merge_findings(raw_payload["batch_results"])
            return StructuringResult(
                outcome=StructuringOutcome.ALREADY_COMPLETED,
                job=job,
                evidence_checkpoint_generation=evidence.generation,
                content_type=classification.type,
                finding_count=len(findings),
                unsupported_finding_count=sum(finding.unsupported for finding in findings),
                batch_count=int(payload.get("batch_count", 0)),
                unavailable_reason_code=raw_payload.get("unavailable_reason_code"),
                resource_report=resource_report,
            )

        started = time.monotonic()
        repaired: list[dict[str, Any]] = []
        for batch in _build_batches(failed_segments, max_chars=1200):
            edit_error: str | None = None
            try:
                edits = _validate_transcript_edits(
                    self.engine.polish_transcript_batch(_segment_payload(batch)),
                    expected_segment_ids={segment.segment_id for segment in batch},
                )
            except Exception as exc:
                edit_error = type(exc).__name__
                edits = ()
            repaired.append(
                {
                    "segment_ids": [segment.segment_id for segment in batch],
                    "transcript_edits": list(edits),
                    "unavailable_reason_code": edit_error,
                }
            )
        combined = retained + repaired
        combined.sort(
            key=lambda result: min(
                (
                    segment_map[segment_id].segment_sequence
                    for segment_id in result.get("segment_ids", [])
                    if segment_id in segment_map
                ),
                default=0,
            )
        )
        for index, result in enumerate(combined):
            result["batch_index"] = index
        raw_payload["transcript_edit_results"] = combined
        unavailable_reasons = [
            result.get("unavailable_reason_code")
            for key in ("batch_results", "transcript_edit_results")
            for result in raw_payload.get(key, [])
            if result.get("unavailable_reason_code")
        ]
        if raw_payload.get("document_unavailable_reason_code"):
            unavailable_reasons.append(raw_payload["document_unavailable_reason_code"])
        raw_payload["unavailable_reason_code"] = (
            ",".join(dict.fromkeys(unavailable_reasons)) if unavailable_reasons else None
        )
        raw_bytes = _canonical_json(raw_payload).encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        raw_relative_path = self._write_private_evidence(
            job_id,
            raw_sha256=raw_sha256,
            raw_bytes=raw_bytes,
        )
        checkpoint_payload = dict(payload)
        checkpoint_payload.update(
            {
                "transcript_edit_batch_count": len(combined),
                "unavailable_reason_code": raw_payload["unavailable_reason_code"],
                "raw_relative_path": raw_relative_path,
                "raw_sha256": raw_sha256,
                "elapsed_seconds": round(
                    float(payload.get("elapsed_seconds", 0) or 0)
                    + time.monotonic()
                    - started,
                    6,
                ),
            }
        )
        updated, _ = self.store.put_checkpoint(
            job_id,
            stage=STRUCTURING_STAGE,
            checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
            payload=checkpoint_payload,
        )
        classification = _validate_classification(raw_payload["classification"])
        findings = _merge_findings(raw_payload["batch_results"])
        return StructuringResult(
            outcome=StructuringOutcome.REGENERATED,
            job=job,
            evidence_checkpoint_generation=updated.generation,
            content_type=classification.type,
            finding_count=len(findings),
            unsupported_finding_count=sum(finding.unsupported for finding in findings),
            batch_count=int(payload.get("batch_count", 0)),
            unavailable_reason_code=raw_payload["unavailable_reason_code"],
            resource_report=resource_report,
        )

    def _load_durable_evidence(
        self,
        job_id: str,
        *,
        evidence: Any,
        plan: NormalizedAudioPlan,
        segments_sha256: str,
    ) -> tuple[ContentClassification, tuple[Finding, ...]]:
        payload = evidence.payload
        if (
            payload.get("schema_version") != STRUCTURING_SCHEMA_VERSION
            or payload.get("model_id") != self.engine.model_id
            or payload.get("normalized_sha256") != plan.normalized_sha256
            or payload.get("segments_sha256") != segments_sha256
            or not isinstance(payload.get("raw_relative_path"), str)
            or not isinstance(payload.get("raw_sha256"), str)
        ):
            raise StructuringFailed("The durable structuring evidence is invalid.")
        raw_payload = self._read_private_evidence(
            job_id,
            relative_path=payload["raw_relative_path"],
            expected_sha256=payload["raw_sha256"],
        )
        if (
            raw_payload.get("schema_version") != STRUCTURING_RAW_SCHEMA_VERSION
            or raw_payload.get("model_id") != self.engine.model_id
            or raw_payload.get("normalized_sha256") != plan.normalized_sha256
            or raw_payload.get("segments_sha256") != segments_sha256
            or not isinstance(raw_payload.get("classification"), dict)
            or not isinstance(raw_payload.get("batch_results"), list)
        ):
            raise StructuringFailed(
                "The private structuring evidence does not match its checkpoint."
            )
        classification = _validate_classification(raw_payload["classification"])
        findings = _merge_findings(raw_payload["batch_results"])
        return classification, findings

    def _list_all_segments(self, job_id: str) -> list[TranscriptSegment]:
        segments: list[TranscriptSegment] = []
        after_sequence = 0
        while True:
            snapshot = self.store.get_job_snapshot(
                job_id,
                after_segment_sequence=after_sequence,
                segment_limit=500,
            )
            segments.extend(snapshot.stable_segments)
            if not snapshot.has_more_segments:
                return segments
            if snapshot.next_after_segment_sequence <= after_sequence:
                raise StructuringFailed("Transcript pagination did not advance during structuring.")
            after_sequence = snapshot.next_after_segment_sequence

    def _write_private_evidence(
        self,
        job_id: str,
        *,
        raw_sha256: str,
        raw_bytes: bytes,
    ) -> str:
        directory = self.store.get_job_stage_directory(
            job_id,
            stage="structuring_raw",
        )
        path = directory / f"structuring-{raw_sha256[:16]}.json"
        if path.is_symlink():
            raise UploadStorageError("Private structuring evidence must not be a symbolic link.")
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise UploadStorageError(
                    "Private structuring evidence could not be verified."
                ) from exc
            if hashlib.sha256(existing).hexdigest() != raw_sha256:
                raise UploadStorageError("Private structuring evidence has conflicting content.")
        else:
            _atomic_write_bytes(path, raw_bytes)
        return path.relative_to(self.store.data_directory).as_posix()

    def _read_private_evidence(
        self,
        job_id: str,
        *,
        relative_path: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        path = (self.store.data_directory / relative_path).resolve()
        root = self.store.get_job_stage_directory(
            job_id,
            stage="structuring_raw",
        ).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise UploadStorageError("Private structuring evidence is unavailable.")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise UploadStorageError("Private structuring evidence could not be read.") from exc
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise UploadStorageError("Private structuring evidence failed checksum verification.")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadStorageError("Private structuring evidence is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise UploadStorageError("Private structuring evidence is not an object.")
        return payload


def _segment_payload(segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
    return [
        {
            "segment_id": segment.segment_id,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "speaker_id": segment.speaker_id,
            "text": segment.text,
        }
        for segment in segments
    ]


def _document_evidence_payload(
    segments: list[TranscriptSegment],
    findings: tuple[Finding, ...],
) -> list[dict[str, Any]]:
    evidence_ids = {
        segment_id
        for finding in findings
        if not finding.unsupported
        for segment_id in finding.evidence
    }
    return [
        {
            "segment_id": segment.segment_id,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "speaker_id": segment.speaker_id,
            "text": (segment.text or "")[:500],
        }
        for segment in segments
        if segment.segment_id in evidence_ids
    ]


def _build_batches(
    segments: list[TranscriptSegment],
    *,
    max_chars: int,
) -> tuple[tuple[TranscriptSegment, ...], ...]:
    batches: list[tuple[TranscriptSegment, ...]] = []
    current: list[TranscriptSegment] = []
    current_chars = 0
    for segment in segments:
        length = len(segment.text or "")
        if current and current_chars + length > max_chars:
            batches.append(tuple(current))
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += length
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _validate_classification(raw: Any) -> ContentClassification:
    if not isinstance(raw, dict):
        raise StructuringFailed("The structuring engine did not return a classification.")
    try:
        content_type = ContentType(str(raw["type"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise StructuringFailed("The structuring engine returned an invalid content type.") from exc
    traits = raw.get("traits")
    if (
        not isinstance(traits, list)
        or len(traits) > MAX_TRAIT_COUNT
        or any(not isinstance(trait, str) or not trait for trait in traits)
    ):
        raise StructuringFailed("The structuring engine returned invalid content traits.")
    confidence = raw.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise StructuringFailed("The structuring engine returned invalid confidence.")
    return ContentClassification(
        type=content_type,
        traits=tuple(traits),
        confidence=float(confidence),
    )


def _validate_findings(
    raw: Any,
    *,
    segment_ids: set[str],
) -> tuple[Finding, ...]:
    if not isinstance(raw, list):
        raise StructuringFailed("The structuring engine did not return a finding list.")
    findings: list[Finding] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise StructuringFailed(
                "The structuring engine returned an invalid finding.",
                details={"finding_index": index},
            )
        try:
            kind = FindingKind(str(item["kind"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise StructuringFailed(
                "The structuring engine returned an unknown finding kind.",
                details={"finding_index": index},
            ) from exc
        text = item.get("text")
        evidence = item.get("evidence")
        confidence = item.get("confidence")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_FINDING_TEXT_CHARACTERS:
            raise StructuringFailed(
                "The structuring engine returned invalid finding text.",
                details={"finding_index": index},
            )
        if not isinstance(evidence, list) or any(
            not isinstance(value, str) or value not in segment_ids for value in evidence
        ):
            raise StructuringFailed(
                "The structuring engine returned evidence outside the transcript.",
                details={"finding_index": index},
            )
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise StructuringFailed(
                "The structuring engine returned invalid finding confidence.",
                details={"finding_index": index},
            )
        findings.append(
            Finding(
                finding_id=f"finding_{index:04d}",
                kind=kind,
                text=text.strip(),
                evidence=tuple(dict.fromkeys(evidence)),
                confidence=float(confidence),
                unsupported=not evidence,
                occurrences=1,
            )
        )
    return tuple(findings)


def _validate_transcript_edits(
    raw: Any,
    *,
    expected_segment_ids: set[str],
) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, list) or len(raw) != len(expected_segment_ids):
        raise StructuringFailed("The transcript editor returned an incomplete segment list.")
    edits: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"segment_id", "text"}:
            raise StructuringFailed(
                "The transcript editor returned an invalid segment.",
                details={"segment_index": index},
            )
        segment_id = item.get("segment_id")
        text = item.get("text")
        if (
            not isinstance(segment_id, str)
            or segment_id not in expected_segment_ids
            or segment_id in seen
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_DOCUMENT_TEXT_CHARACTERS
        ):
            raise StructuringFailed(
                "The transcript editor returned invalid segment text.",
                details={"segment_index": index},
            )
        seen.add(segment_id)
        edits.append({"segment_id": segment_id, "text": text.strip()})
    if seen != expected_segment_ids:
        raise StructuringFailed("The transcript editor changed the segment identity set.")
    return tuple(edits)


def _validate_document(
    raw: Any,
    *,
    segment_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StructuringFailed("The structuring engine did not return a document.")
    expected_keys = {
        "title",
        "summary",
        "highlights",
        "topics",
        "decisions",
        "actions",
        "risks",
        "open_questions",
        "chapters",
    }
    if set(raw) != expected_keys:
        raise StructuringFailed("The structured document has invalid fields.")
    title = _validate_document_text(
        raw.get("title"),
        field="title",
        maximum=MAX_DOCUMENT_TITLE_CHARACTERS,
    )
    summary = _validate_evidence_text(
        raw.get("summary"),
        segment_ids=segment_ids,
        field="summary",
    )
    highlights = _validate_evidence_text_list(
        raw.get("highlights"), segment_ids=segment_ids, field="highlights", maximum=8
    )
    if len(highlights) < 5:
        raise StructuringFailed("The structured document has too few highlights.")
    decisions = _validate_evidence_text_list(
        raw.get("decisions"), segment_ids=segment_ids, field="decisions", maximum=15
    )
    risks = _validate_evidence_text_list(
        raw.get("risks"), segment_ids=segment_ids, field="risks", maximum=15
    )
    open_questions = _validate_evidence_text_list(
        raw.get("open_questions"),
        segment_ids=segment_ids,
        field="open_questions",
        maximum=15,
    )

    raw_topics = raw.get("topics")
    if not isinstance(raw_topics, list) or not 5 <= len(raw_topics) <= 10:
        raise StructuringFailed("The structured document has invalid topics.")
    topics: list[dict[str, Any]] = []
    for index, item in enumerate(raw_topics):
        if not isinstance(item, dict) or set(item) != {
            "title",
            "summary",
            "details",
            "evidence",
        }:
            raise StructuringFailed("The structured document has an invalid topic.")
        topics.append(
            {
                "title": _validate_document_text(
                    item.get("title"), field=f"topics[{index}].title", maximum=120
                ),
                "summary": _validate_document_text(
                    item.get("summary"),
                    field=f"topics[{index}].summary",
                    maximum=MAX_DOCUMENT_TEXT_CHARACTERS,
                ),
                "details": _validate_evidence_text_list(
                    item.get("details"),
                    segment_ids=segment_ids,
                    field=f"topics[{index}].details",
                    maximum=8,
                ),
                "evidence": _validate_evidence(
                    item.get("evidence"),
                    segment_ids=segment_ids,
                    field=f"topics[{index}].evidence",
                ),
            }
        )

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list) or len(raw_actions) > 15:
        raise StructuringFailed("The structured document has invalid actions.")
    actions: list[dict[str, Any]] = []
    for index, item in enumerate(raw_actions):
        if not isinstance(item, dict) or set(item) != {
            "task",
            "owner",
            "deadline",
            "evidence",
        }:
            raise StructuringFailed("The structured document has an invalid action.")
        actions.append(
            {
                "task": _validate_document_text(
                    item.get("task"), field=f"actions[{index}].task", maximum=1000
                ),
                "owner": _validate_optional_document_text(
                    item.get("owner"), field=f"actions[{index}].owner", maximum=200
                ),
                "deadline": _validate_optional_document_text(
                    item.get("deadline"),
                    field=f"actions[{index}].deadline",
                    maximum=200,
                ),
                "evidence": _validate_evidence(
                    item.get("evidence"),
                    segment_ids=segment_ids,
                    field=f"actions[{index}].evidence",
                ),
            }
        )

    raw_chapters = raw.get("chapters")
    if not isinstance(raw_chapters, list) or not 6 <= len(raw_chapters) <= 15:
        raise StructuringFailed("The structured document has invalid chapters.")
    chapters: list[dict[str, Any]] = []
    for index, item in enumerate(raw_chapters):
        if not isinstance(item, dict) or set(item) != {"title", "summary", "evidence"}:
            raise StructuringFailed("The structured document has an invalid chapter.")
        chapters.append(
            {
                "title": _validate_document_text(
                    item.get("title"), field=f"chapters[{index}].title", maximum=120
                ),
                "summary": _validate_document_text(
                    item.get("summary"),
                    field=f"chapters[{index}].summary",
                    maximum=MAX_DOCUMENT_TEXT_CHARACTERS,
                ),
                "evidence": _validate_evidence(
                    item.get("evidence"),
                    segment_ids=segment_ids,
                    field=f"chapters[{index}].evidence",
                ),
            }
        )

    return {
        "title": title,
        "summary": summary,
        "highlights": highlights,
        "topics": topics,
        "decisions": decisions,
        "actions": actions,
        "risks": risks,
        "open_questions": open_questions,
        "chapters": chapters,
    }


def _validate_document_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StructuringFailed(f"The structured document has invalid {field} text.")
    return value.strip()


def _validate_optional_document_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise StructuringFailed(f"The structured document has invalid {field} text.")
    return value.strip()


def _validate_evidence_text_list(
    value: Any,
    *,
    segment_ids: set[str],
    field: str,
    maximum: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise StructuringFailed(f"The structured document has invalid {field} items.")
    return [
        _validate_evidence_text(item, segment_ids=segment_ids, field=f"{field}[{index}]")
        for index, item in enumerate(value)
    ]


def _validate_evidence_text(
    value: Any,
    *,
    segment_ids: set[str],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"text", "evidence"}:
        raise StructuringFailed(f"The structured document has invalid {field}.")
    return {
        "text": _validate_document_text(
            value.get("text"), field=field, maximum=MAX_DOCUMENT_TEXT_CHARACTERS
        ),
        "evidence": _validate_evidence(
            value.get("evidence"), segment_ids=segment_ids, field=f"{field}.evidence"
        ),
    }


def _validate_evidence(
    value: Any,
    *,
    segment_ids: set[str],
    field: str,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_DOCUMENT_ITEMS
        or any(not isinstance(item, str) or item not in segment_ids for item in value)
    ):
        raise StructuringFailed(f"The structured document has invalid {field}.")
    return list(dict.fromkeys(value))


def _merge_findings(batch_results: list[dict[str, Any]]) -> tuple[Finding, ...]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for batch in batch_results:
        for raw_finding in batch.get("findings", []):
            key = (raw_finding["kind"], raw_finding["text"].strip().casefold())
            group = groups.setdefault(
                key,
                {
                    "kind": raw_finding["kind"],
                    "text": raw_finding["text"].strip(),
                    "evidence": set(),
                    "confidence": 0.0,
                    "occurrences": 0,
                },
            )
            group["evidence"].update(raw_finding["evidence"])
            group["confidence"] = max(group["confidence"], raw_finding["confidence"])
            group["occurrences"] += raw_finding["occurrences"]
    return tuple(
        Finding(
            finding_id=f"finding_{index:04d}",
            kind=FindingKind(group["kind"]),
            text=group["text"],
            evidence=tuple(sorted(group["evidence"])),
            confidence=round(group["confidence"], 6),
            unsupported=not group["evidence"],
            occurrences=group["occurrences"],
        )
        for index, group in enumerate(sorted(groups.values(), key=lambda item: item["text"]))
    )


def _parse_json_object(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise StructuringFailed("The Ollama engine did not return a JSON object.")
        try:
            payload = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise StructuringFailed("The Ollama engine returned unparsable JSON.") from exc
    if not isinstance(payload, dict):
        raise StructuringFailed("The Ollama engine did not return a JSON object.")
    return payload


def _parse_json_list(value: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("[")
        end = value.rfind("]")
        if start < 0 or end <= start:
            raise StructuringFailed("The Ollama engine did not return a JSON list.")
        try:
            payload = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise StructuringFailed("The Ollama engine returned unparsable JSON.") from exc
    if not isinstance(payload, list):
        raise StructuringFailed("The Ollama engine did not return a JSON list.")
    return payload


def _segments_identity_sha256(segments: list[TranscriptSegment]) -> str:
    payload = [
        {
            "segment_id": segment.segment_id,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "outcome": segment.outcome.value,
            "text_sha256": _text_sha256(segment.text or ""),
            "speaker_id": segment.speaker_id,
        }
        for segment in segments
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checkpoint_by_key(checkpoints: list[Any], checkpoint_key: str) -> Any | None:
    return next(
        (checkpoint for checkpoint in checkpoints if checkpoint.checkpoint_key == checkpoint_key),
        None,
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    if destination.parent.is_symlink() or destination.is_symlink():
        raise UploadStorageError("Private structuring storage must not contain symbolic links.")
    temporary_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        file_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    file_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
