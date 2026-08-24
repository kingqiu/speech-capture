"""Deterministic backend artifact generation from durable Worker evidence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
from dataclasses import asdict, dataclass, replace
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
from speech_capture_worker.corrections import (
    CorrectionField,
    CorrectionRecord,
    corrections_sha256,
    decode_segment_review,
)
from speech_capture_worker.domain import JobRecord, JobState
from speech_capture_worker.errors import (
    ArtifactGenerationFailed,
    InvalidJobRequest,
    UploadStorageError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.note_prompt_profiles import (
    render_headings,
    scene_section_labels,
)
from speech_capture_worker.recording_context import recording_context_from_options
from speech_capture_worker.recording_metadata import recording_date_from_options
from speech_capture_worker.structuring_execution import (
    STRUCTURING_CHECKPOINT_KEY,
    STRUCTURING_STAGE,
    _merge_findings,
)
from speech_capture_worker.transcript import (
    TranscriptOutcome,
    TranscriptSegment,
    chronological_segments,
)

ARTIFACT_SCHEMA_VERSION = "1.6.0"
RAW_TRANSCRIPT_SCHEMA_VERSION = "1.0.0"
ARTIFACT_STAGE = "artifacts"
ARTIFACT_CHECKPOINT_KEY = "artifacts_generation"
ARTIFACT_MANIFEST = "artifact-manifest.json"
RAW_TRANSCRIPT = "transcript.raw.json"
TRANSCRIPT_MARKDOWN = "transcript.md"
SPEECH_RECORD = "speech-record.json"
NOTE_MARKDOWN = "note.md"
NOTE_EVIDENCE_MARKDOWN = "note.evidence.md"
TIMELINE_MARKDOWN = "timeline.md"
ARTIFACT_FILES = (
    RAW_TRANSCRIPT,
    TRANSCRIPT_MARKDOWN,
    SPEECH_RECORD,
    NOTE_MARKDOWN,
    NOTE_EVIDENCE_MARKDOWN,
    TIMELINE_MARKDOWN,
)
MAX_EXISTING_NOTE_BYTES = 8 * 1024 * 1024
MAX_MANUAL_SECTION_BYTES = 4 * 1024 * 1024
MANUAL_SECTION_HEADING = "## 我的补充"
MANUAL_SECTION_PATTERN = re.compile(r"(?m)^##[ \t]+我的补充[ \t]*(?:\r?\n|$)")


class ArtifactOutcome(StrEnum):
    GENERATED = "generated"
    REGENERATED = "regenerated"
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


@dataclass(frozen=True)
class EffectiveCorrections:
    transcript_edits: dict[str, str]
    speaker_assignments: dict[str, str | None]
    speaker_display_names: dict[str, str]
    recording_date: str | None
    document: Any


def _artifact_files_match_checkpoint(
    package_dir: Path,
    checkpoint_payload: dict[str, Any],
) -> bool:
    """Verify the generated package before treating a processed job as idempotent."""

    expected = checkpoint_payload.get("files")
    if (
        not isinstance(expected, dict)
        or set(expected) != set(ARTIFACT_FILES)
        or package_dir.is_symlink()
        or not package_dir.is_dir()
    ):
        return False
    for name in ARTIFACT_FILES:
        expected_sha256 = expected.get(name)
        path = package_dir / name
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or path.is_symlink()
            or not path.is_file()
        ):
            return False
        try:
            digest = _file_sha256(path)
        except OSError:
            return False
        if digest != expected_sha256:
            return False
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manual_section(path: Path) -> str | None:
    """Read the terminal user-owned Note section without interpreting its Markdown."""

    if path.is_symlink():
        raise UploadStorageError("The existing note cannot be a symlink.")
    if not path.exists():
        return None
    if not path.is_file():
        raise ArtifactGenerationFailed("The existing note is not a regular file.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ArtifactGenerationFailed("The existing note could not be inspected.") from exc
    if size > MAX_EXISTING_NOTE_BYTES:
        raise ArtifactGenerationFailed("The existing note is too large to regenerate safely.")
    try:
        content = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactGenerationFailed("The existing note is not valid UTF-8.") from exc
    except OSError as exc:
        raise ArtifactGenerationFailed("The existing note could not be read.") from exc
    match = MANUAL_SECTION_PATTERN.search(content)
    if match is None:
        raise ArtifactGenerationFailed(
            "The existing note is missing the protected '我的补充' section."
        )
    manual_section = content[match.start() :]
    if len(manual_section.encode("utf-8")) > MAX_MANUAL_SECTION_BYTES:
        raise ArtifactGenerationFailed("The protected manual section is too large.")
    return manual_section


class ArtifactGenerator:
    """Generate four user Markdown files and two machine records plus a manifest."""

    def __init__(
        self,
        store: JobStore,
        *,
        preprocessor: AudioPreprocessor | None = None,
    ) -> None:
        self.store = store
        self.preprocessor = preprocessor or AudioPreprocessor(store)

    def generate(self, job_id: str, *, force: bool = False) -> ArtifactResult:
        if not isinstance(force, bool):
            raise InvalidJobRequest("force must be a boolean.")
        job = self.store.get_job(job_id)
        corrections = self.store.list_corrections(job_id)
        corrections_digest = corrections_sha256(corrections)
        regenerating_processed = False
        if job.state is JobState.PROCESSED and not force:
            checkpoint = _checkpoint_by_key(
                self.store.list_checkpoints(job_id, stage=ARTIFACT_STAGE),
                ARTIFACT_CHECKPOINT_KEY,
            )
            if checkpoint is None:
                raise ArtifactGenerationFailed(
                    "A processed job is missing its artifact checkpoint."
                )
            current_structuring = _checkpoint_by_key(
                self.store.list_checkpoints(job_id, stage=STRUCTURING_STAGE),
                STRUCTURING_CHECKPOINT_KEY,
            )
            if (
                current_structuring is not None
                and checkpoint.payload.get("schema_version") == ARTIFACT_SCHEMA_VERSION
                and checkpoint.payload.get("structuring_checkpoint_generation")
                == current_structuring.generation
                and checkpoint.payload.get("corrections_sha256") == corrections_digest
                and _artifact_files_match_checkpoint(
                    self.store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE),
                    checkpoint.payload,
                )
            ):
                return ArtifactResult(
                    outcome=ArtifactOutcome.ALREADY_GENERATED,
                    job=job,
                    speech_id=checkpoint.payload["speech_id"],
                    artifact_count=int(checkpoint.payload["artifact_count"]),
                    manifest_sha256=checkpoint.payload["manifest_sha256"],
                    package_relative_path=checkpoint.payload["package_relative_path"],
                )
            regenerating_processed = True
        if job.state not in {JobState.QUALITY_CHECK, JobState.PROCESSED}:
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
            raise ArtifactGenerationFailed("Artifact generation requires the structuring evidence.")
        if structuring_checkpoint.payload.get("document_available") is not True:
            raise ArtifactGenerationFailed(
                "Artifact generation requires a validated final structured document."
            )
        if job.source_upload_id is None:
            raise ArtifactGenerationFailed("Artifact generation requires a verified source upload.")

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
        document = _filter_scene_actions(
            structuring_raw.get("document"),
            findings=findings,
            content_type=classification.get("type"),
        )
        document = _with_legacy_timeline_sections(
            document,
            segments=segments,
            source_title=job.source_display_name,
        )
        if _usable_document(document) is None:
            raise ArtifactGenerationFailed(
                "Artifact generation requires a validated final structured document."
            )
        transcript_edits = _transcript_edits(
            structuring_raw.get("transcript_edit_results", structuring_raw["batch_results"])
        )
        effective = _apply_corrections(
            segments=segments,
            transcript_edits=transcript_edits,
            document=document,
            corrections=corrections,
            recording_date=recording_date_from_options(job.options),
        )
        transcript_edits = effective.transcript_edits
        document = effective.document
        raw_speaker_ids = {
            segment.segment_id: segment.speaker_id for segment in segments
        }
        segments = [
            replace(
                segment,
                speaker_id=effective.speaker_assignments.get(
                    segment.segment_id,
                    segment.speaker_id,
                ),
            )
            for segment in segments
        ]
        context_corrections = structuring_raw.get("context_corrections", [])
        if not isinstance(context_corrections, list):
            raise ArtifactGenerationFailed(
                "The structuring evidence has invalid context corrections."
            )
        upload = self.store.get_upload(job.source_upload_id)
        block_ids = {
            segment.segment_id: _block_id(speech_id, segment.segment_sequence)
            for segment in segments
        }
        package_dir = self.store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE)
        package_dir.mkdir(parents=True, exist_ok=True)
        if package_dir.is_symlink():
            raise UploadStorageError("The artifact package directory cannot be a symlink.")
        manual_section = _read_manual_section(package_dir / NOTE_MARKDOWN)

        raw_transcript = _build_raw_transcript(
            speech_id=speech_id,
            attempts=attempts,
            raw_payloads=raw_payloads,
            normalized_sha256=plan.normalized_sha256,
        )
        transcript_markdown = _build_transcript_markdown(
            segments=segments,
            block_ids=block_ids,
            transcript_edits=transcript_edits,
            speaker_display_names=effective.speaker_display_names,
        )
        speech_record = _build_speech_record(
            job=job,
            speech_id=speech_id,
            upload=upload,
            segments=segments,
            block_ids=block_ids,
            findings=findings,
            classification=classification,
            document=document,
            transcript_edits=transcript_edits,
            alignment_report=alignment_checkpoint.payload,
            alignment_report_generation=alignment_checkpoint.generation,
            structuring_checkpoint=structuring_checkpoint.payload,
            context_corrections=context_corrections,
            manual_corrections=corrections,
            speaker_display_names=effective.speaker_display_names,
            raw_speaker_ids=raw_speaker_ids,
            recording_date=effective.recording_date,
        )
        note_markdown = _build_note_markdown(
            job=job,
            speech_id=speech_id,
            upload=upload,
            segments=segments,
            block_ids=block_ids,
            findings=findings,
            classification=classification,
            document=document,
            alignment_report=alignment_checkpoint.payload,
            include_evidence=False,
            manual_section=manual_section,
            speaker_display_names=effective.speaker_display_names,
            recording_date=effective.recording_date,
        )
        evidence_note_markdown = _build_note_markdown(
            job=job,
            speech_id=speech_id,
            upload=upload,
            segments=segments,
            block_ids=block_ids,
            findings=findings,
            classification=classification,
            document=document,
            alignment_report=alignment_checkpoint.payload,
            include_evidence=True,
            speaker_display_names=effective.speaker_display_names,
            recording_date=effective.recording_date,
        )
        timeline_markdown = _build_timeline_markdown(
            job=job,
            segments=segments,
            transcript_edits=transcript_edits,
            document=document,
        )
        contents = {
            RAW_TRANSCRIPT: _canonical_json(raw_transcript).encode("utf-8") + b"\n",
            TRANSCRIPT_MARKDOWN: transcript_markdown.encode("utf-8"),
            SPEECH_RECORD: _canonical_json(speech_record).encode("utf-8") + b"\n",
            NOTE_MARKDOWN: note_markdown.encode("utf-8"),
            NOTE_EVIDENCE_MARKDOWN: evidence_note_markdown.encode("utf-8"),
            TIMELINE_MARKDOWN: timeline_markdown.encode("utf-8"),
        }
        hashes = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
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
            "corrections_sha256": corrections_digest,
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
                "alignment_report_generation": alignment_checkpoint.generation,
                "structuring_checkpoint_generation": structuring_checkpoint.generation,
                "corrections_sha256": corrections_digest,
            },
        )
        current = self.store.get_job(job_id)
        if current.state is JobState.QUALITY_CHECK:
            result_job = self.store.transition_job(
                job_id,
                JobState.PROCESSED,
                expected_revision=current.revision,
                reason_code="artifacts_generated",
                event_type="job.processed",
            )
        else:
            result_job = current
        return ArtifactResult(
            outcome=(
                ArtifactOutcome.REGENERATED
                if force or regenerating_processed
                else ArtifactOutcome.GENERATED
            ),
            job=result_job,
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
                return chronological_segments(segments)
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
            raise ArtifactGenerationFailed("The structuring evidence failed checksum verification.")
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactGenerationFailed("The structuring evidence is not valid JSON.") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("classification"), dict):
            raise ArtifactGenerationFailed("The structuring evidence is incomplete.")
        return raw


def _filter_scene_actions(
    document: Any,
    *,
    findings: tuple[Any, ...],
    content_type: Any,
) -> Any:
    """Keep speech tasks only when extraction independently found an action."""

    if not isinstance(document, dict) or content_type != "speech":
        return document
    action_evidence = {
        segment_id
        for finding in findings
        if not finding.unsupported and finding.kind.value in {"action_item", "next_step"}
        for segment_id in finding.evidence
    }
    filtered = dict(document)
    raw_actions = document.get("actions")
    filtered["actions"] = (
        [
            action
            for action in raw_actions
            if isinstance(action, dict)
            and isinstance(action.get("evidence"), list)
            and bool(set(action["evidence"]) & action_evidence)
        ]
        if isinstance(raw_actions, list)
        else []
    )
    return filtered


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


def _transcript_edits(batch_results: list[dict[str, Any]]) -> dict[str, str]:
    edits: dict[str, str] = {}
    for batch in batch_results:
        for item in batch.get("transcript_edits", []):
            if (
                isinstance(item, dict)
                and isinstance(item.get("segment_id"), str)
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ):
                edits[item["segment_id"]] = item["text"].strip()
    return edits


def _apply_corrections(
    *,
    segments: list[TranscriptSegment],
    transcript_edits: dict[str, str],
    document: Any,
    corrections: list[CorrectionRecord],
    recording_date: str | None,
) -> EffectiveCorrections:
    """Overlay the append-only ledger on derived values, never source evidence."""

    revised_edits = dict(transcript_edits)
    revised_document = document
    segment_map = {segment.segment_id: segment for segment in segments}
    speaker_assignments = {
        segment.segment_id: segment.speaker_id for segment in segments
    }
    speaker_ids = sorted({segment.speaker_id for segment in segments if segment.speaker_id})
    speaker_names = {speaker_id: _default_speaker_name(speaker_id) for speaker_id in speaker_ids}
    if isinstance(document, dict) and isinstance(document.get("speaker_summaries"), list):
        for summary in document["speaker_summaries"]:
            if not isinstance(summary, dict):
                continue
            speaker_id = summary.get("speaker_id")
            display_name = summary.get("display_name")
            if (
                isinstance(speaker_id, str)
                and speaker_id in speaker_names
                and isinstance(display_name, str)
                and display_name.strip()
            ):
                speaker_names[speaker_id] = display_name.strip()
    renamed_speaker_ids: set[str] = set()
    for correction in corrections:
        if correction.field is CorrectionField.TRANSCRIPT_TEXT:
            segment = segment_map.get(correction.target_id or "")
            if segment is None:
                raise ArtifactGenerationFailed(
                    f"Correction {correction.correction_id} targets a missing segment."
                )
            current = revised_edits.get(segment.segment_id, segment.text or "")
            if current != correction.before:
                raise ArtifactGenerationFailed(
                    f"Correction {correction.correction_id} no longer matches the derived text."
                )
            revised_edits[segment.segment_id] = correction.after
            revised_document = _replace_derived_text(
                revised_document,
                before=correction.before,
                after=correction.after,
            )
        elif correction.field is CorrectionField.SEGMENT_REVIEW:
            segment = segment_map.get(correction.target_id or "")
            if segment is None or correction.before is None:
                raise ArtifactGenerationFailed(
                    f"Correction {correction.correction_id} targets a missing segment."
                )
            before_text, before_speaker_id = decode_segment_review(correction.before)
            after_text, after_speaker_id = decode_segment_review(correction.after)
            current_text = revised_edits.get(segment.segment_id, segment.text or "")
            current_speaker_id = speaker_assignments.get(segment.segment_id)
            if current_text != before_text or current_speaker_id != before_speaker_id:
                raise ArtifactGenerationFailed(
                    f"Correction {correction.correction_id} no longer matches the reviewed segment."
                )
            revised_edits[segment.segment_id] = after_text
            speaker_assignments[segment.segment_id] = after_speaker_id
            revised_document = _replace_derived_text(
                revised_document,
                before=before_text,
                after=after_text,
            )
        elif correction.field is CorrectionField.SPEAKER_DISPLAY_NAME:
            speaker_id = correction.target_id or ""
            current = speaker_names.get(speaker_id)
            if speaker_id in renamed_speaker_ids and current != correction.before:
                raise ArtifactGenerationFailed(
                    f"Correction {correction.correction_id} no longer matches the speaker name."
                )
            speaker_names[speaker_id] = correction.after
            renamed_speaker_ids.add(speaker_id)
            revised_document = _replace_speaker_display_name(
                revised_document,
                speaker_id=speaker_id,
                display_name=correction.after,
            )
        else:
            if recording_date != correction.before:
                raise ArtifactGenerationFailed(
                    f"Correction {correction.correction_id} no longer matches the recording date."
                )
            recording_date = correction.after
    return EffectiveCorrections(
        transcript_edits=revised_edits,
        speaker_assignments=speaker_assignments,
        speaker_display_names=speaker_names,
        recording_date=recording_date,
        document=revised_document,
    )


def _replace_derived_text(value: Any, *, before: str, after: str) -> Any:
    if isinstance(value, str):
        return value.replace(before, after)
    if isinstance(value, list):
        return [_replace_derived_text(item, before=before, after=after) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_derived_text(item, before=before, after=after)
            for key, item in value.items()
        }
    return value


def _replace_speaker_display_name(
    document: Any,
    *,
    speaker_id: str,
    display_name: str,
) -> Any:
    if not isinstance(document, dict):
        return document
    revised = dict(document)
    raw_summaries = document.get("speaker_summaries")
    if not isinstance(raw_summaries, list):
        return revised
    summaries: list[Any] = []
    for item in raw_summaries:
        if isinstance(item, dict) and item.get("speaker_id") == speaker_id:
            updated = dict(item)
            updated["display_name"] = display_name
            summaries.append(updated)
        else:
            summaries.append(item)
    revised["speaker_summaries"] = summaries
    return revised


def _build_transcript_markdown(
    *,
    segments: list[TranscriptSegment],
    block_ids: dict[str, str],
    transcript_edits: dict[str, str],
    speaker_display_names: dict[str, str] | None = None,
) -> str:
    lines: list[str] = []
    for segment in segments:
        if not _show_transcript_segment(segment):
            continue
        heading = _segment_heading(segment, speaker_display_names=speaker_display_names)
        lines.append(f"## {heading}")
        lines.append("")
        if segment.outcome is TranscriptOutcome.TRANSCRIBED:
            lines.append(transcript_edits.get(segment.segment_id, segment.text or ""))
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


def _show_transcript_segment(segment: TranscriptSegment) -> bool:
    duration_ms = segment.end_ms - segment.start_ms
    if segment.outcome is TranscriptOutcome.NON_SPEECH:
        return False
    if segment.outcome is TranscriptOutcome.INAUDIBLE:
        return duration_ms >= 2000
    if segment.outcome is not TranscriptOutcome.TRANSCRIBED:
        return True
    # Every accepted text segment is evidence-addressable in speech-record.json.
    # Keep the complete corrected transcript equally addressable even when an
    # alignment fragment is extremely short or its language tag is noisy.
    return bool((segment.text or "").strip())


def _with_legacy_timeline_sections(
    document: Any,
    *,
    segments: list[TranscriptSegment],
    source_title: str,
) -> Any:
    """Add a deterministic full-range timeline to accepted pre-1.6 documents."""

    if not isinstance(document, dict):
        return document
    existing = document.get("timeline_sections")
    if isinstance(existing, list) and existing:
        return document
    transcribed = sorted(
        (
            segment
            for segment in segments
            if segment.outcome is TranscriptOutcome.TRANSCRIBED and (segment.text or "").strip()
        ),
        key=lambda segment: (
            segment.start_ms,
            segment.end_ms,
            segment.segment_sequence,
        ),
    )
    if not transcribed:
        return document
    title = document.get("title")
    if not isinstance(title, str) or not title.strip():
        title = source_title
    raw_summary = document.get("summary")
    summary = raw_summary.get("text") if isinstance(raw_summary, dict) else None
    if not isinstance(summary, str) or not summary.strip():
        summary = "本时间段的内容请结合完整校订逐字稿阅读。"
    upgraded = dict(document)
    upgraded["timeline_sections"] = [
        {
            "title": title.strip(),
            "summary": summary.strip(),
            "details": [],
            "start_segment_id": transcribed[0].segment_id,
            "end_segment_id": transcribed[-1].segment_id,
        }
    ]
    return upgraded


def _build_speech_record(
    *,
    job: JobRecord,
    speech_id: str,
    upload: Any,
    segments: list[TranscriptSegment],
    block_ids: dict[str, str],
    findings: tuple[Any, ...],
    classification: dict[str, Any],
    document: Any,
    transcript_edits: dict[str, str],
    alignment_report: dict[str, Any],
    alignment_report_generation: int,
    structuring_checkpoint: dict[str, Any],
    context_corrections: list[Any],
    manual_corrections: list[CorrectionRecord],
    speaker_display_names: dict[str, str],
    raw_speaker_ids: dict[str, str | None],
    recording_date: str | None,
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
            "source": structuring_checkpoint.get("content_type_source", "automatic"),
            "automatic_type": structuring_checkpoint.get(
                "automatic_content_type", classification.get("type")
            ),
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
            "recording_date": recording_date,
            "imported_at": job.created_at,
            "processed_at": job.updated_at,
        },
        "speakers": [
            {"speaker_id": speaker_id, "display_name": display_name}
            for speaker_id, display_name in sorted(speaker_display_names.items())
        ],
        "segments": [
            {
                "segment_id": segment.segment_id,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "speaker_id": segment.speaker_id,
                "raw_speaker_id": raw_speaker_ids.get(segment.segment_id),
                "text": transcript_edits.get(segment.segment_id, segment.text),
                "raw_text": segment.text,
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
        "document": document if isinstance(document, dict) else None,
        "corrections": [
            *context_corrections,
            *[
                {
                    "correction_id": item.correction_id,
                    "field": item.field.value,
                    "target_id": item.target_id,
                    "before": item.before,
                    "after": item.after,
                    "author": item.author,
                    "created_at": item.created_at,
                }
                for item in manual_corrections
            ],
        ],
        "provenance": {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "worker_package_version": _package_version(),
            "structuring_model_id": structuring_checkpoint.get("model_id"),
            "alignment_report_generation": alignment_report_generation,
            "recording_context_supplied": (recording_context_from_options(job.options) is not None),
            "recording_context_sha256": structuring_checkpoint.get("recording_context_sha256"),
            "recording_context_applied": structuring_checkpoint.get(
                "recording_context_applied", False
            ),
            "recording_context_processing_version": structuring_checkpoint.get(
                "recording_context_processing_version"
            ),
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
    document: Any,
    alignment_report: dict[str, Any],
    include_evidence: bool = True,
    manual_section: str | None = None,
    speaker_display_names: dict[str, str] | None = None,
    recording_date: str | None = None,
) -> str:
    content_type = classification.get("type") or "generic"
    supported = [finding for finding in findings if not finding.unsupported]
    unsupported = [finding for finding in findings if finding.unsupported]
    speaker_display_names = speaker_display_names or {}
    speakers = sorted(
        {
            speaker_display_names.get(segment.speaker_id, segment.speaker_id)
            for segment in segments
            if segment.speaker_id
        }
    )
    segment_map = {segment.segment_id: segment for segment in segments}
    duration_seconds = round(upload.duration_seconds or 0, 1)
    status = (
        "complete"
        if alignment_report.get("transcript_complete") is True
        and alignment_report.get("timeline_accounted") is True
        else "partial"
    )
    structured = _usable_document(document) or _fallback_document(
        source_title=job.source_display_name,
        findings=supported,
        content_type=content_type,
    )
    headings = render_headings(content_type)
    title = structured["title"]
    lines: list[str] = []
    if include_evidence:
        lines.extend(
            [
                "---",
                f"title: {json.dumps(title, ensure_ascii=False)}",
                f"status: {status}",
                f"content_type: {content_type}",
                f"speech_id: {speech_id}",
                f"duration_seconds: {duration_seconds}",
                *( [f"recording_date: {recording_date}"] if recording_date else [] ),
                "speakers:",
            ]
        )
        lines.extend(f"  - {speaker}" for speaker in speakers)
        lines.extend(
            [
                "tags:",
                "  - speech-capture",
                "  - speech-capture/note-evidence",
                "---",
                "",
            ]
        )
    lines.extend([f"# {title}", ""])
    if include_evidence:
        lines.extend(
            [
                "> [!abstract] 内容总结",
                "> " + structured["summary"]["text"],
                ">",
                "> 依据："
                + _evidence_links(
                    structured["summary"]["evidence"], block_ids, segment_map
                ),
            ]
        )
    else:
        lines.extend(["## 内容总结", "", structured["summary"]["text"]])
    lines.append("")
    if structured["context"]:
        lines.extend([f"## {headings['context']}", ""])
        for item in structured["context"]:
            lines.append(
                f"- **{item['title']}**：{item['text']}"
                + _note_evidence_suffix(
                    item["evidence"], block_ids, segment_map, include_evidence
                )
            )
        lines.append("")

    lines.extend([f"## {headings['highlights']}", ""])
    if structured["highlights"]:
        for item in _unique_evidence_items(structured["highlights"]):
            lines.append(
                f"- {item['text']}"
                + _note_evidence_suffix(
                    item["evidence"], block_ids, segment_map, include_evidence
                )
            )
    else:
        lines.append("暂无可靠的核心结论。")

    lines.extend(["", f"## {headings['body']}", ""])
    body_items = structured["topics"] if content_type == "meeting" else structured["scene_sections"]
    if body_items:
        kind_labels = scene_section_labels(content_type)
        for index, topic in enumerate(body_items, start=1):
            section_title = topic["title"]
            if content_type not in {"meeting", "voice_memo", "generic"}:
                label = kind_labels.get(topic.get("kind", ""), "内容")
                section_title = f"{label}｜{section_title}"
            lines.extend(
                [
                    f"### {index}. {section_title}",
                    "",
                    topic["summary"]
                    + _note_evidence_suffix(
                        topic["evidence"], block_ids, segment_map, include_evidence
                    ),
                    "",
                ]
            )
            summary_signature = "".join(topic["summary"].strip().rstrip("。；;").split()).casefold()
            for detail in _unique_evidence_items(topic["details"]):
                detail_signature = (
                    "".join(detail["text"].strip().rstrip("。；;").split()).casefold()
                )
                if detail_signature in summary_signature:
                    continue
                lines.append(
                    f"- {detail['text']}"
                    + _note_evidence_suffix(
                        detail["evidence"], block_ids, segment_map, include_evidence
                    )
                )
            lines.append("")
    else:
        lines.extend(["暂无可靠的正文归纳。", ""])

    if structured["discussion_threads"]:
        status_labels = {
            "confirmed": "已确认",
            "tentative": "暂定方向",
            "open": "仍待确认",
        }
        lines.extend(["## 讨论演变与当前方向", ""])
        for thread in structured["discussion_threads"]:
            lines.extend(
                [
                    f"### {thread['title']}",
                    "",
                    "- **最初建议**："
                    + thread["initial_position"]["text"]
                    + _note_evidence_suffix(
                        thread["initial_position"]["evidence"],
                        block_ids,
                        segment_map,
                        include_evidence,
                    ),
                ]
            )
            for development in thread["developments"]:
                lines.append(
                    "- **后续修正**："
                    + development["text"]
                    + _note_evidence_suffix(
                        development["evidence"], block_ids, segment_map, include_evidence
                    )
                )
            lines.extend(
                [
                    "- **当前方向**："
                    + thread["current_direction"]["text"]
                    + _note_evidence_suffix(
                        thread["current_direction"]["evidence"],
                        block_ids,
                        segment_map,
                        include_evidence,
                    ),
                    f"- **状态**：{status_labels[thread['status']]}",
                    "",
                ]
            )

    if structured["speaker_summaries"]:
        lines.extend([f"## {headings['speakers']}", ""])
        for speaker in structured["speaker_summaries"]:
            identity = speaker["display_name"] or speaker["speaker_id"]
            descriptors = [value for value in (speaker["affiliation"], speaker["role"]) if value]
            if descriptors:
                identity += f"（{' · '.join(descriptors)}）"
            lines.extend(
                [
                    f"### {identity}",
                    "",
                    speaker["summary"]
                    + _note_evidence_suffix(
                        speaker["evidence"], block_ids, segment_map, include_evidence
                    ),
                    "",
                ]
            )
            lines.append("")

    if structured["decisions"]:
        lines.extend([f"## {headings['decisions']}", ""])
        for item in _unique_evidence_items(structured["decisions"]):
            lines.append(
                f"- {item['text']}"
                + _note_evidence_suffix(
                    item["evidence"], block_ids, segment_map, include_evidence
                )
            )
        lines.append("")

    if structured["actions"]:
        lines.extend([f"## {headings['actions']}", ""])
        for action in _unique_action_items(structured["actions"]):
            attributes = []
            if action["owner"]:
                attributes.append(f"负责人：{action['owner']}")
            if action["deadline"]:
                attributes.append(f"截止：{action['deadline']}")
            suffix = f"；{'；'.join(attributes)}" if attributes else ""
            lines.append(
                f"- [ ] {action['task']}{suffix}"
                + _note_evidence_suffix(
                    action["evidence"], block_ids, segment_map, include_evidence
                )
            )
        lines.append("")
    elif content_type == "meeting":
        lines.extend([f"## {headings['actions']}", ""])
        lines.append("暂无明确待办。")
        lines.append("")

    if structured["risks"]:
        lines.extend([f"## {headings['risks']}", ""])
        for item in _unique_evidence_items(structured["risks"]):
            lines.append(
                f"- {item['text']}"
                + _note_evidence_suffix(
                    item["evidence"], block_ids, segment_map, include_evidence
                )
            )
        lines.append("")

    if structured["open_questions"]:
        risk_texts = {item["text"].strip().casefold() for item in structured["risks"]}
        open_questions = [
            item
            for item in structured["open_questions"]
            if item["text"].strip().casefold() not in risk_texts
        ]
    else:
        open_questions = []
    if open_questions:
        lines.extend([f"## {headings['questions']}", ""])
        for item in _unique_evidence_items(open_questions):
            lines.append(
                f"- {item['text']}"
                + _note_evidence_suffix(
                    item["evidence"], block_ids, segment_map, include_evidence
                )
            )
        lines.append("")

    if include_evidence and structured["chapters"]:
        lines.extend(["## 章节导航", ""])
        for chapter in _sort_chapters(structured["chapters"], segment_map):
            anchor = _evidence_links(chapter["evidence"], block_ids, segment_map).split("、", 1)[0]
            lines.append(f"- {anchor} **{chapter['title']}** — {chapter['summary']}")
        lines.append("")

    if include_evidence:
        lines.extend(
            [
                "## 会议信息" if content_type == "meeting" else "## 记录信息",
                "",
                "| 项目 | 内容 |",
                "| --- | --- |",
                f"| 录音文件 | {job.source_display_name} |",
                f"| 录音时长 | {_format_duration(round(duration_seconds * 1000))} |",
                f"| 参与者 | {', '.join(speakers) if speakers else '未识别'} |",
                f"| 完整度 | {'完整' if alignment_report.get('transcript_complete') else '部分'} |",
                "",
                "完整逐字稿：[[transcript|打开 transcript.md]]",
                "",
            ]
        )

    uncertain_segments = [
        segment
        for segment in segments
        if segment.outcome in {TranscriptOutcome.INAUDIBLE, TranscriptOutcome.FAILED}
    ]
    if uncertain_segments or unsupported:
        if include_evidence:
            lines.extend(["> [!warning]- 转写不确定与遗漏"])
        else:
            lines.extend(["## 内容完整度说明", ""])
        if uncertain_segments:
            uncertain_duration_ms = sum(
                segment.end_ms - segment.start_ms for segment in uncertain_segments
            )
            uncertainty_text = (
                f"共有 {len(uncertain_segments)} 处未稳定转写，合计"
                f"{_format_approximate_duration(uncertain_duration_ms)}。"
            )
            if include_evidence:
                uncertainty_text = uncertainty_text.rstrip("。") + (
                    "；具体位置已在 [[transcript]] 中标为「听不清」。"
                )
            lines.append(("> " if include_evidence else "") + uncertainty_text)
            significant = [
                segment
                for segment in uncertain_segments
                if segment.end_ms - segment.start_ms >= 2000
            ]
            if include_evidence and significant:
                lines.append(
                    "> 较长区间："
                    + "、".join(
                        _format_range(segment.start_ms, segment.end_ms) for segment in significant
                    )
                )
        if unsupported:
            lines.append(
                ("> " if include_evidence else "")
                + f"{len(unsupported)} 条提炼结论证据不足，未写入正文。"
            )
        lines.append("")
    rendered = "\n".join(lines)
    if include_evidence:
        return rendered
    protected = manual_section if manual_section is not None else f"{MANUAL_SECTION_HEADING}\n\n"
    return rendered.rstrip() + "\n\n" + protected


def _note_evidence_suffix(
    evidence: list[str] | tuple[str, ...],
    block_ids: dict[str, str],
    segment_map: dict[str, TranscriptSegment],
    include_evidence: bool,
) -> str:
    if not include_evidence:
        return ""
    return f" （{_evidence_links(evidence, block_ids, segment_map)}）"


def _build_timeline_markdown(
    *,
    job: JobRecord,
    segments: list[TranscriptSegment],
    transcript_edits: dict[str, str],
    document: Any,
) -> str:
    """Render a scene-neutral chronological digest of the corrected transcript."""

    transcribed = [
        segment
        for segment in segments
        if segment.outcome is TranscriptOutcome.TRANSCRIBED
        and (transcript_edits.get(segment.segment_id, segment.text) or "").strip()
    ]
    raw_sections = document.get("timeline_sections") if isinstance(document, dict) else None
    timeline_sections = raw_sections if isinstance(raw_sections, list) else []
    if not timeline_sections and transcribed:
        fallback = _usable_document(document)
        fallback_summary = (
            fallback["summary"]["text"]
            if fallback is not None
            else "本时间段的内容请结合完整校订逐字稿阅读。"
        )
        timeline_sections = [
            {
                "title": fallback["title"] if fallback is not None else job.source_display_name,
                "summary": fallback_summary,
                "details": [],
                "start_segment_id": transcribed[0].segment_id,
                "end_segment_id": transcribed[-1].segment_id,
            }
        ]

    segment_map = {segment.segment_id: segment for segment in segments}
    lines = ["# 按时间阅读", ""]
    if not timeline_sections:
        lines.extend(["没有可供提炼的有效语音内容。", ""])
        return "\n".join(lines)

    for section in timeline_sections:
        if not isinstance(section, dict):
            continue
        start = segment_map.get(section.get("start_segment_id"))
        end = segment_map.get(section.get("end_segment_id"))
        title = str(section.get("title") or "未命名时段").strip()
        summary = str(section.get("summary") or "").strip()
        if start is None or end is None or not summary:
            continue
        lines.extend(
            [
                f"## {_format_duration(start.start_ms)}–{_format_duration(end.end_ms)}｜{title}",
                "",
                summary,
                "",
            ]
        )
        details = section.get("details")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, str) and detail.strip():
                    lines.append(f"- {detail.strip()}")
            if details:
                lines.append("")
        uncertain_count = sum(
            segment.outcome in {TranscriptOutcome.INAUDIBLE, TranscriptOutcome.FAILED}
            and segment.start_ms < end.end_ms
            and segment.end_ms > start.start_ms
            for segment in segments
        )
        if uncertain_count:
            lines.extend(
                [
                    f"> 本时段有 {uncertain_count} 处内容未能稳定识别，摘要未对其作推测。",
                    "",
                ]
            )
    return "\n".join(lines)


def _usable_document(value: Any) -> dict[str, Any] | None:
    required = {
        "title",
        "summary",
        "context",
        "highlights",
        "topics",
        "discussion_threads",
        "speaker_summaries",
        "decisions",
        "actions",
        "risks",
        "open_questions",
        "chapters",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        return None
    normalized = dict(value)
    normalized.setdefault("scene_sections", [])
    normalized.setdefault("timeline_sections", [])
    return normalized


def _fallback_document(
    *, source_title: str, findings: list[Any], content_type: str
) -> dict[str, Any]:
    overview = _overview_findings(findings)
    evidence = list(
        dict.fromkeys(segment_id for finding in overview for segment_id in finding.evidence)
    )
    summary_text = "；".join(finding.text.rstrip("。") for finding in overview)
    if summary_text:
        summary_text += "。"
    else:
        summary_text = "没有生成具备可靠原文证据的内容摘要。"
    by_kind: dict[str, list[Any]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind.value, []).append(finding)
    topics = []
    for kind in ("topic", "fact", "idea", "disagreement"):
        matches = by_kind.get(kind, [])
        if not matches:
            continue
        topics.append(
            {
                "title": _finding_kind_label(kind),
                "summary": "；".join(item.text.rstrip("。") for item in matches[:4]) + "。",
                "details": [
                    {"text": item.text, "evidence": list(item.evidence)} for item in matches
                ],
                "evidence": list(
                    dict.fromkeys(segment_id for item in matches for segment_id in item.evidence)
                ),
            }
        )
    fallback_scene_kind = {
        "interview": "viewpoint",
        "course": "concept",
        "speech": "argument",
        "voice_memo": "idea",
        "generic": "theme",
    }.get(content_type)
    scene_sections = (
        [
            {
                "kind": fallback_scene_kind,
                "title": topic["title"],
                "summary": topic["summary"],
                "details": topic["details"],
                "evidence": topic["evidence"],
            }
            for topic in topics
        ]
        if fallback_scene_kind
        else []
    )
    chapters = topics if content_type == "meeting" else scene_sections
    return {
        "title": source_title,
        "summary": {"text": summary_text, "evidence": evidence},
        "context": [],
        "highlights": [
            {"text": finding.text, "evidence": list(finding.evidence)} for finding in overview
        ],
        "topics": topics,
        "scene_sections": scene_sections,
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [
            {"text": item.text, "evidence": list(item.evidence)}
            for item in by_kind.get("decision", [])
        ],
        "actions": [
            {
                "task": item.text,
                "owner": "",
                "deadline": "",
                "evidence": list(item.evidence),
            }
            for item in by_kind.get("action_item", []) + by_kind.get("next_step", [])
        ],
        "risks": [
            {"text": item.text, "evidence": list(item.evidence)}
            for item in by_kind.get("uncertainty", [])
        ],
        "open_questions": [
            {"text": item.text, "evidence": list(item.evidence)}
            for item in by_kind.get("question", [])
        ],
        "chapters": [
            {
                "title": item["title"],
                "summary": item["summary"],
                "evidence": item["evidence"],
            }
            for item in chapters
        ],
    }


def _evidence_links(
    evidence: list[str] | tuple[str, ...],
    block_ids: dict[str, str],
    segment_map: dict[str, TranscriptSegment],
) -> str:
    links = []
    ordered = sorted(
        dict.fromkeys(evidence),
        key=lambda segment_id: (
            segment_map[segment_id].start_ms if segment_id in segment_map else 2**63
        ),
    )
    for segment_id in ordered[:8]:
        segment = segment_map.get(segment_id)
        block_id = block_ids.get(segment_id)
        if segment is None or block_id is None:
            continue
        label = _format_duration(segment.start_ms)
        links.append(f"[[transcript#^{block_id}|{label}]]")
    return "、".join(links) or "原文证据不可用"


def _unique_evidence_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = item["text"].strip().rstrip("。").casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _unique_action_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = item["task"].strip().rstrip("。").casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _sort_chapters(
    chapters: list[dict[str, Any]],
    segment_map: dict[str, TranscriptSegment],
) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for chapter in chapters:
        key = (chapter["title"].strip().casefold(), chapter["summary"].strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append(chapter)
    return sorted(
        unique,
        key=lambda chapter: min(
            (
                segment_map[segment_id].start_ms
                for segment_id in chapter["evidence"]
                if segment_id in segment_map
            ),
            default=0,
        ),
    )


def _overview_findings(findings: list[Any], *, limit: int = 6) -> list[Any]:
    by_kind: dict[str, list[Any]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind.value, []).append(finding)
    for values in by_kind.values():
        values.sort(key=lambda finding: (-finding.confidence, finding.text))

    quotas = (
        ("decision", 2),
        ("topic", 2),
        ("fact", 2),
        ("idea", 2),
        ("action_item", 2),
        ("next_step", 1),
        ("question", 1),
        ("disagreement", 1),
        ("uncertainty", 1),
        ("deadline", 1),
    )
    selected: list[Any] = []
    for kind, quota in quotas:
        selected.extend(by_kind.get(kind, [])[:quota])
        if len(selected) >= limit:
            return selected[:limit]
    return selected


def _finding_kind_label(kind: str) -> str:
    return {
        "decision": "决定",
        "action_item": "行动项",
        "fact": "关键事实",
        "question": "问题",
        "disagreement": "分歧",
        "uncertainty": "不确定",
        "deadline": "截止时间",
        "topic": "主题",
        "idea": "观点",
        "next_step": "下一步",
    }.get(kind, kind)


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


def _segment_heading(
    segment: TranscriptSegment,
    *,
    speaker_display_names: dict[str, str] | None = None,
) -> str:
    speaker = None
    if segment.speaker_id and speaker_display_names:
        speaker = speaker_display_names.get(segment.speaker_id)
    if not speaker:
        speaker = _default_speaker_name(segment.speaker_id)
    return f"{_format_range(segment.start_ms, segment.end_ms)} · {speaker}"


def _default_speaker_name(speaker_id: str | None) -> str:
    if not speaker_id:
        return "Speaker ?"
    suffix = speaker_id.rsplit("_", 1)[-1]
    return f"Speaker {int(suffix)}" if suffix.isdigit() else speaker_id


def _format_range(start_ms: int, end_ms: int) -> str:
    return f"{_format_duration(start_ms)}–{_format_duration(end_ms)}"


def _format_duration(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_approximate_duration(ms: int) -> str:
    if 0 < ms < 1000:
        return "不足 1 秒"
    return f"约 {_format_duration(ms)}"


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
