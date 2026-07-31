"""Deterministic backend artifact generation from durable Worker evidence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from speech_capture_worker.alignment import (
    CHECKPOINT_KEY as ALIGNMENT_CHECKPOINT_KEY,
)
from speech_capture_worker.alignment import (
    CHECKPOINT_STAGE as ALIGNMENT_STAGE,
)
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.domain import JobRecord, JobState
from speech_capture_worker.errors import (
    ArtifactGenerationFailed,
    InvalidJobRequest,
    UploadStorageError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.structuring_execution import (
    STRUCTURING_CHECKPOINT_KEY,
    STRUCTURING_STAGE,
    _merge_findings,
)
from speech_capture_worker.transcript import (
    TranscriptOutcome,
    TranscriptSegment,
)

ARTIFACT_SCHEMA_VERSION = "1.0.0"
RAW_TRANSCRIPT_SCHEMA_VERSION = "1.0.0"
ARTIFACT_STAGE = "artifacts"
ARTIFACT_CHECKPOINT_KEY = "artifacts_generation"
ARTIFACT_MANIFEST = "artifact-manifest.json"
RAW_TRANSCRIPT = "transcript.raw.json"
TRANSCRIPT_MARKDOWN = "transcript.md"
SPEECH_RECORD = "speech-record.json"
NOTE_MARKDOWN = "note.md"
ARTIFACT_FILES = (
    RAW_TRANSCRIPT,
    TRANSCRIPT_MARKDOWN,
    SPEECH_RECORD,
    NOTE_MARKDOWN,
)


class ArtifactOutcome(StrEnum):
    GENERATED = "generated"
    REPLAYED = "replayed"
    SAFE_PAUSED = "safe_paused"
    ALREADY_GENERATED = "already_generated"


@dataclass(frozen=True)
class ArtifactResult:
    outcome: ArtifactOutcome
    job: JobRecord
    speech_id: str
    artifact_count: int
    manifest_sha256: str
    package_relative_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactGenerator:
    """Generate the four backend artifacts plus a checksummed manifest."""

    def __init__(
        self,
        store: JobStore,
        *,
        preprocessor: AudioPreprocessor | None = None,
    ) -> None:
        self.store = store
        self.preprocessor = preprocessor or AudioPreprocessor(store)

    def generate(self, job_id: str) -> ArtifactResult:
        job = self.store.get_job(job_id)
        if job.state is JobState.PROCESSED:
            checkpoint = _checkpoint_by_key(
                self.store.list_checkpoints(job_id, stage=ARTIFACT_STAGE),
                ARTIFACT_CHECKPOINT_KEY,
            )
            if checkpoint is None:
                raise ArtifactGenerationFailed(
                    "A processed job is missing its artifact checkpoint."
                )
            return ArtifactResult(
                outcome=ArtifactOutcome.ALREADY_GENERATED,
                job=job,
                speech_id=checkpoint.payload["speech_id"],
                artifact_count=int(checkpoint.payload["artifact_count"]),
                manifest_sha256=checkpoint.payload["manifest_sha256"],
                package_relative_path=checkpoint.payload["package_relative_path"],
            )
        if job.state is not JobState.QUALITY_CHECK:
            raise InvalidJobRequest(
                "Artifact generation requires a quality-check or processed job."
            )

        alignment_checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE),
            ALIGNMENT_CHECKPOINT_KEY,
        )
        if alignment_checkpoint is None:
            raise ArtifactGenerationFailed(
                "Artifact generation requires the current alignment report."
            )
        structuring_checkpoint = _checkpoint_by_key(
            self.store.list_checkpoints(job_id, stage=STRUCTURING_STAGE),
            STRUCTURING_CHECKPOINT_KEY,
        )
        if structuring_checkpoint is None:
            raise ArtifactGenerationFailed(
                "Artifact generation requires the structuring evidence."
            )
        if job.source_upload_id is None:
            raise ArtifactGenerationFailed(
                "Artifact generation requires a verified source upload."
            )

        speech_id = _speech_id(job.job_id)
        plan = self.preprocessor.get_plan(job_id)
        segments = self._list_all_segments(job_id)
        attempts = self.store.list_asr_attempts(job_id)
        raw_payloads = [
            self.store.get_asr_attempt_payload(
                job_id,
                chunk_index=attempt.chunk_index,
                attempt_number=attempt.attempt_number,
            )
            for attempt in attempts
        ]
        structuring_raw = self._read_structuring_evidence(
            job_id,
            checkpoint=structuring_checkpoint,
        )
        findings = _merge_findings(structuring_raw["batch_results"])
        classification = structuring_raw["classification"]
        upload = self.store.get_upload(job.source_upload_id)
        block_ids = {
            segment.segment_id: _block_id(speech_id, segment.segment_sequence)
            for segment in segments
        }
        package_dir = self.store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE)
        package_dir.mkdir(parents=True, exist_ok=True)
        if package_dir.is_symlink():
            raise UploadStorageError("The artifact package directory cannot be a symlink.")

        raw_transcript = _build_raw_transcript(
            speech_id=speech_id,
            attempts=attempts,
            raw_payloads=raw_payloads,
            normalized_sha256=plan.normalized_sha256,
        )
        transcript_markdown = _build_transcript_markdown(
            segments=segments,
            block_ids=block_ids,
        )
        speech_record = _build_speech_record(
            job=job,
            speech_id=speech_id,
            upload=upload,
            segments=segments,
            block_ids=block_ids,
            findings=findings,
            classification=classification,
            alignment_report=alignment_checkpoint.payload,
            alignment_report_generation=alignment_checkpoint.generation,
            structuring_checkpoint=structuring_checkpoint.payload,
        )
        note_markdown = _build_note_markdown(
            job=job,
            speech_id=speech_id,
            upload=upload,
            segments=segments,
            block_ids=block_ids,
            findings=findings,
            classification=classification,
            alignment_report=alignment_checkpoint.payload,
        )
        contents = {
            RAW_TRANSCRIPT: _canonical_json(raw_transcript).encode("utf-8") + b"\n",
            TRANSCRIPT_MARKDOWN: transcript_markdown.encode("utf-8"),
            SPEECH_RECORD: _canonical_json(speech_record).encode("utf-8") + b"\n",
            NOTE_MARKDOWN: note_markdown.encode("utf-8"),
        }
        hashes = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in contents.items()
        }
        for name, content in contents.items():
            _atomic_write_bytes(package_dir / name, content)
        manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "speech_id": speech_id,
            "job_id": job.job_id,
            "artifact_count": len(contents),
            "files": hashes,
            "alignment_report_generation": alignment_checkpoint.generation,
            "structuring_checkpoint_generation": structuring_checkpoint.generation,
        }
        manifest_bytes = _canonical_json(manifest).encode("utf-8") + b"\n"
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        _atomic_write_bytes(package_dir / ARTIFACT_MANIFEST, manifest_bytes)
        package_relative_path = package_dir.relative_to(self.store.data_directory).as_posix()
        checkpoint, _ = self.store.put_checkpoint(
            job_id,
            stage=ARTIFACT_STAGE,
            checkpoint_key=ARTIFACT_CHECKPOINT_KEY,
            payload={
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "speech_id": speech_id,
                "artifact_count": len(contents),
                "manifest_sha256": manifest_sha256,
                "package_relative_path": package_relative_path,
                "files": hashes,
            },
        )
        current = self.store.get_job(job_id)
        processed = self.store.transition_job(
            job_id,
            JobState.PROCESSED,
            expected_revision=current.revision,
            reason_code="artifacts_generated",
            event_type="job.processed",
        )
        return ArtifactResult(
            outcome=ArtifactOutcome.GENERATED,
            job=processed,
            speech_id=speech_id,
            artifact_count=len(contents),
            manifest_sha256=manifest_sha256,
            package_relative_path=package_relative_path,
        )

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
                raise ArtifactGenerationFailed(
                    "Transcript pagination did not advance during artifact generation."
                )
            after_sequence = snapshot.next_after_segment_sequence

    def _read_structuring_evidence(self, job_id: str, *, checkpoint: Any) -> dict[str, Any]:
        payload = checkpoint.payload
        if not isinstance(payload.get("raw_relative_path"), str) or not isinstance(
            payload.get("raw_sha256"), str
        ):
            raise ArtifactGenerationFailed("The structuring checkpoint is incomplete.")
        path = (self.store.data_directory / payload["raw_relative_path"]).resolve()
        root = self.store.get_job_stage_directory(
            job_id,
            stage="structuring_raw",
        ).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise ArtifactGenerationFailed("The structuring evidence file is unavailable.")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ArtifactGenerationFailed(
                "The structuring evidence file could not be read."
            ) from exc
        if hashlib.sha256(content).hexdigest() != payload["raw_sha256"]:
            raise ArtifactGenerationFailed(
                "The structuring evidence failed checksum verification."
            )
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactGenerationFailed(
                "The structuring evidence is not valid JSON."
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("classification"), dict):
            raise ArtifactGenerationFailed("The structuring evidence is incomplete.")
        return raw


def _build_raw_transcript(
    *,
    speech_id: str,
    attempts: list[Any],
    raw_payloads: list[dict[str, Any]],
    normalized_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RAW_TRANSCRIPT_SCHEMA_VERSION,
        "speech_id": speech_id,
        "normalized_audio_sha256": normalized_sha256,
        "attempts": [
            {
                "chunk_index": attempt.chunk_index,
                "attempt_number": attempt.attempt_number,
                "state": attempt.state.value,
                "model_id": attempt.model_id,
                "start_ms": attempt.start_ms,
                "end_ms": attempt.end_ms,
                "start_frame": attempt.start_frame,
                "end_frame": attempt.end_frame,
                "language": attempt.language,
                "finish_reason": attempt.finish_reason,
                "truncated": attempt.truncated,
                "elapsed_seconds": attempt.elapsed_seconds,
                "raw_sha256": attempt.raw_sha256,
                "error_code": attempt.error_code,
                "payload": payload,
            }
            for attempt, payload in zip(attempts, raw_payloads)
        ],
    }


def _build_transcript_markdown(
    *,
    segments: list[TranscriptSegment],
    block_ids: dict[str, str],
) -> str:
    lines: list[str] = []
    for segment in segments:
        heading = _segment_heading(segment)
        lines.append(f"## {heading}")
        lines.append("")
        if segment.outcome is TranscriptOutcome.TRANSCRIBED:
            lines.append(segment.text or "")
        elif segment.outcome is TranscriptOutcome.NON_SPEECH:
            lines.append("[非语音]")
        elif segment.outcome is TranscriptOutcome.INAUDIBLE:
            lines.append("[听不清]")
        else:
            lines.append(f"[处理失败：{segment.error_code or 'unknown'}]")
        lines.append("")
        lines.append(f"^{block_ids[segment.segment_id]}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_speech_record(
    *,
    job: JobRecord,
    speech_id: str,
    upload: Any,
    segments: list[TranscriptSegment],
    block_ids: dict[str, str],
    findings: tuple[Any, ...],
    classification: dict[str, Any],
    alignment_report: dict[str, Any],
    alignment_report_generation: int,
    structuring_checkpoint: dict[str, Any],
) -> dict[str, Any]:
    status = (
        "complete"
        if alignment_report.get("transcript_complete") is True
        and alignment_report.get("timeline_accounted") is True
        else "partial"
    )
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "speech_id": speech_id,
        "job_id": job.job_id,
        "revision": job.revision,
        "status": status,
        "content": {
            "type": classification.get("type"),
            "traits": classification.get("traits", []),
            "confidence": classification.get("confidence"),
        },
        "source": {
            "display_name": job.source_display_name,
            "sha256": job.source_sha256,
            "size_bytes": job.source_size_bytes,
            "duration_ms": round((upload.duration_seconds or 0) * 1000),
            "media_type": upload.media_type,
            "detected_format": upload.detected_format_name,
        },
        "dates": {
            "imported_at": job.created_at,
            "processed_at": job.updated_at,
        },
        "segments": [
            {
                "segment_id": segment.segment_id,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "speaker_id": segment.speaker_id,
                "text": segment.text,
                "revision": segment.revision,
                "block_id": block_ids[segment.segment_id],
                "outcome": segment.outcome.value,
                "timing_status": segment.timing_status.value,
                "language": segment.language,
                "quality": "generated",
            }
            for segment in segments
        ],
        "findings": [
            {
                "finding_id": finding.finding_id,
                "kind": finding.kind.value,
                "text": finding.text,
                "evidence": list(finding.evidence),
                "confidence": finding.confidence,
                "unsupported": finding.unsupported,
                "occurrences": finding.occurrences,
                "review_state": "generated",
            }
            for finding in findings
        ],
        "corrections": [],
        "provenance": {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "worker_package_version": _package_version(),
            "structuring_model_id": structuring_checkpoint.get("model_id"),
            "alignment_report_generation": alignment_report_generation,
        },
        "quality": {
            "transcript_complete": alignment_report.get("transcript_complete"),
            "timeline_accounted": alignment_report.get("timeline_accounted"),
            "unresolved_duration_ms": alignment_report.get("unresolved_duration_ms", 0),
            "outcome_counts": alignment_report.get("outcome_counts", {}),
            "unsupported_finding_count": sum(finding.unsupported for finding in findings),
        },
    }


def _build_note_markdown(
    *,
    job: JobRecord,
    speech_id: str,
    upload: Any,
    segments: list[TranscriptSegment],
    block_ids: dict[str, str],
    findings: tuple[Any, ...],
    classification: dict[str, Any],
    alignment_report: dict[str, Any],
) -> str:
    content_type = classification.get("type") or "generic"
    supported = [finding for finding in findings if not finding.unsupported]
    unsupported = [finding for finding in findings if finding.unsupported]
    speakers = sorted({segment.speaker_id for segment in segments if segment.speaker_id})
    duration_seconds = round(upload.duration_seconds or 0, 1)
    status = (
        "complete"
        if alignment_report.get("transcript_complete") is True
        and alignment_report.get("timeline_accounted") is True
        else "partial"
    )
    lines = [
        "---",
        f"speech_id: {speech_id}",
        f"status: {status}",
        f"content_type: {content_type}",
        f"duration_seconds: {duration_seconds}",
        "speakers:",
    ]
    lines.extend(f"  - {speaker}" for speaker in speakers)
    lines.extend(
        [
            "tags:",
            "  - speech-capture",
            "---",
            "",
            f"# {job.source_display_name}",
            "",
            "## 一分钟总览",
            "",
            f"- 内容类型：{content_type}",
            f"- 时长：{_format_duration(int(duration_seconds * 1000))}",
            f"- 说话人数：{len(speakers)}",
            "",
            "## 关键信息",
            "",
        ]
    )
    if supported:
        for finding in supported:
            evidence = "、".join(
                f"^{block_ids[segment_id]}" for segment_id in finding.evidence
            )
            lines.append(f"- [{finding.kind.value}] {finding.text}（证据：{evidence}）")
    else:
        lines.append("无。")
    lines.extend(["", "## 内容类型明细", ""])
    section_lines = _content_sections(content_type, supported, block_ids)
    lines.extend(section_lines or ["无。", ""])
    uncertain_segments = [
        segment
        for segment in segments
        if segment.outcome is not TranscriptOutcome.TRANSCRIBED
    ]
    lines.extend(["## 不确定与遗漏", ""])
    if uncertain_segments or unsupported:
        if uncertain_segments:
            lines.append(
                "以下时间范围没有稳定转写："
                + ", ".join(
                    _format_range(segment.start_ms, segment.end_ms)
                    for segment in uncertain_segments
                )
            )
        if unsupported:
            lines.append(f"{len(unsupported)} 条提炼结论证据不足，未进入关键信息。")
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 来源与处理信息",
            "",
            f"- 源文件：{job.source_display_name}",
            f"- 状态：{status}",
            f"- 完整度：{'完整' if alignment_report.get('transcript_complete') else '部分'}",
            "",
            "## 我的补充",
            "",
            "",
        ]
    )
    return "\n".join(lines)


def _content_sections(
    content_type: str,
    findings: list[Any],
    block_ids: dict[str, str],
) -> list[str]:
    headings: dict[str, str] = {
        "meeting": {
            "decision": "决定",
            "action_item": "行动项",
            "deadline": "截止日期",
            "disagreement": "分歧",
            "question": "未决问题",
        },
        "interview": {
            "topic": "主题",
            "question": "问答",
            "fact": "重要陈述",
            "next_step": "后续",
        },
        "course": {
            "topic": "概念",
            "fact": "定义与例子",
            "next_step": "学习方法",
        },
        "speech": {
            "topic": "论点",
            "fact": "论据与例子",
        },
        "voice_memo": {
            "idea": "想法",
            "next_step": "下一步",
            "question": "待确认",
        },
        "generic": {
            "topic": "主题",
            "fact": "关键陈述",
            "question": "开放问题",
        },
    }.get(content_type, {})
    by_kind: dict[str, list[Any]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind.value, []).append(finding)
    lines: list[str] = []
    for kind, heading in headings.items():
        matches = by_kind.get(kind, [])
        if not matches:
            continue
        lines.append(f"### {heading}")
        lines.append("")
        for finding in matches:
            evidence = "、".join(f"^{block_ids[segment_id]}" for segment_id in finding.evidence)
            lines.append(f"- {finding.text}（{evidence}）")
        lines.append("")
    return lines


def _segment_heading(segment: TranscriptSegment) -> str:
    speaker = (
        f"Speaker {int(segment.speaker_id.rsplit('_', 1)[-1])}"
        if segment.speaker_id and "_" in segment.speaker_id
        else "Speaker ?"
    )
    return f"{_format_range(segment.start_ms, segment.end_ms)} · {speaker}"


def _format_range(start_ms: int, end_ms: int) -> str:
    return f"{_format_duration(start_ms)}–{_format_duration(end_ms)}"


def _format_duration(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _speech_id(job_id: str) -> str:
    return "sp_" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:20]


def _block_id(speech_id: str, segment_sequence: int) -> str:
    return f"sp-{speech_id[3:]}-seg-{segment_sequence:06d}"


def _package_version() -> str:
    try:
        return importlib.metadata.version("speech-capture-worker")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


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
        raise UploadStorageError("Artifact storage must not contain symbolic links.")
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
