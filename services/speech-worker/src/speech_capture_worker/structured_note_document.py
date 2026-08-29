"""Stable structured-note envelope and the Phase B1 legacy payload adapter."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from speech_capture_worker.content_profiles import (
    DOCUMENT_SCHEMA_VERSION,
    ProfileReference,
)
from speech_capture_worker.domain import SUPPORTED_CONTENT_TYPES

VALIDATOR_SET_VERSION = "1.0.0"

_HASH_PATTERN = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_LEGACY_FIELDS = frozenset(
    {
        "title",
        "objective",
        "summary",
        "context",
        "highlights",
        "topics",
        "scene_sections",
        "discussion_threads",
        "timeline_sections",
        "speaker_summaries",
        "decisions",
        "actions",
        "risks",
        "open_questions",
        "chapters",
    }
)
_LIST_FIELDS = (
    "context",
    "highlights",
    "topics",
    "scene_sections",
    "discussion_threads",
    "timeline_sections",
    "speaker_summaries",
    "decisions",
    "actions",
    "risks",
    "open_questions",
)


class StructuredNoteAdapterError(ValueError):
    """Raised when a validated legacy document cannot be adapted safely."""


@dataclass(frozen=True)
class StructuredNoteDocument:
    document_schema_version: str
    document_id: str
    content_type: str
    profile: ProfileReference
    source: dict[str, str]
    content: dict[str, Any]
    quality: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_schema_version": self.document_schema_version,
            "document_id": self.document_id,
            "content_type": self.content_type,
            "profile": self.profile.to_dict(),
            "source": copy.deepcopy(self.source),
            "content": copy.deepcopy(self.content),
            "quality": copy.deepcopy(self.quality),
        }


def adapt_current_structured_document(
    document: dict[str, Any],
    *,
    document_id: str,
    content_type: str,
    profile: ProfileReference,
    evidence_bundle_sha256: str,
    corrected_transcript_sha256: str,
    recording_context_sha256: str,
    validated_at: str,
    warnings: list[str] | None = None,
    validator_set_version: str = VALIDATOR_SET_VERSION,
) -> StructuredNoteDocument:
    """Wrap one already validated schema-1.6 document without changing its meaning.

    This adapter does not call a model, render Markdown, mutate the input payload, or
    participate in the current Worker pipeline.  ``chapters`` is the one legacy-only
    compatibility field and is intentionally omitted because it is derived from
    ``topics`` or ``scene_sections``.
    """

    if not isinstance(document, dict):
        raise StructuredNoteAdapterError("Legacy structured document must be an object.")
    unknown = set(document) - _LEGACY_FIELDS
    if unknown:
        raise StructuredNoteAdapterError(
            f"Legacy structured document has unknown fields: {sorted(unknown)}."
        )
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise StructuredNoteAdapterError("content_type is not supported.")
    if _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None:
        raise StructuredNoteAdapterError("document_id has invalid syntax.")
    if not isinstance(document.get("title"), str) or not document["title"].strip():
        raise StructuredNoteAdapterError("Legacy structured document requires a title.")
    _validate_evidence_text(document.get("summary"), field="summary")
    objective = document.get("objective")
    if content_type == "meeting" and objective is None:
        raise StructuredNoteAdapterError("Meeting documents require objective.")
    if objective is not None:
        _validate_evidence_text(objective, field="objective")
    for field in _LIST_FIELDS:
        value = document.get(field, [])
        if not isinstance(value, list):
            raise StructuredNoteAdapterError(f"Legacy field {field!r} must be an array.")

    if not isinstance(validated_at, str) or not validated_at or not validated_at.isprintable():
        raise StructuredNoteAdapterError("validated_at must be a printable timestamp.")
    if not isinstance(validator_set_version, str) or not validator_set_version:
        raise StructuredNoteAdapterError("validator_set_version must be a non-empty string.")
    if warnings is None:
        warnings = []
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise StructuredNoteAdapterError("warnings must be an array of strings.")

    content: dict[str, Any] = {
        "title": document["title"],
        "objective": objective,
        "summary": document["summary"],
        **{field: document.get(field, []) for field in _LIST_FIELDS},
    }
    content = copy.deepcopy(content)
    return StructuredNoteDocument(
        document_schema_version=DOCUMENT_SCHEMA_VERSION,
        document_id=document_id,
        content_type=content_type,
        profile=profile,
        source={
            "evidence_bundle_sha256": _normalize_hash(
                evidence_bundle_sha256,
                field="evidence_bundle_sha256",
            ),
            "corrected_transcript_sha256": _normalize_hash(
                corrected_transcript_sha256,
                field="corrected_transcript_sha256",
            ),
            "recording_context_sha256": _normalize_hash(
                recording_context_sha256,
                field="recording_context_sha256",
            ),
        },
        content=content,
        quality={
            "validator_set_version": validator_set_version,
            "validated_at": validated_at,
            "warnings": list(warnings),
        },
    )


def _validate_evidence_text(value: Any, *, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"text", "evidence"}:
        raise StructuredNoteAdapterError(f"{field} must be an evidence-linked text object.")
    if not isinstance(value["text"], str) or not value["text"].strip():
        raise StructuredNoteAdapterError(f"{field}.text must not be empty.")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or not evidence or any(
        not isinstance(item, str) or not item for item in evidence
    ):
        raise StructuredNoteAdapterError(f"{field}.evidence must contain segment ids.")


def _normalize_hash(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise StructuredNoteAdapterError(f"{field} must be a sha256 digest.")
    match = _HASH_PATTERN.fullmatch(value)
    if match is None:
        raise StructuredNoteAdapterError(f"{field} must be a lowercase sha256 digest.")
    return f"sha256:{match.group(1)}"
