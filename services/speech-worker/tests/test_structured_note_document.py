"""Public synthetic baseline tests for the StructuredNoteDocument adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from speech_capture_worker.content_profiles import ProfileReference
from speech_capture_worker.structured_note_document import (
    StructuredNoteAdapterError,
    adapt_current_structured_document,
)

FIXTURE = Path(__file__).parent / "fixtures/content-profile-b1/current-meeting-document.json"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _adapt(document: dict):
    return adapt_current_structured_document(
        document,
        document_id="synthetic-document:meeting-baseline",
        content_type="meeting",
        profile=ProfileReference(
            profile_id="speech-capture/meeting",
            profile_version="builtin-2026-08-27.1",
            bundle_sha256=f"sha256:{HASH_A}",
        ),
        evidence_bundle_sha256=HASH_A,
        corrected_transcript_sha256=f"sha256:{HASH_B}",
        recording_context_sha256=HASH_C,
        validated_at="2026-08-28T10:00:00+08:00",
    )


def test_adapter_wraps_current_payload_without_mutating_or_reinterpreting_content() -> None:
    source = _document()
    original = json.loads(json.dumps(source, ensure_ascii=False))

    adapted = _adapt(source).to_dict()

    assert source == original
    assert adapted["document_schema_version"] == "1.0.0"
    assert adapted["profile"]["profile_version"] == "builtin-2026-08-27.1"
    assert adapted["source"] == {
        "evidence_bundle_sha256": f"sha256:{HASH_A}",
        "corrected_transcript_sha256": f"sha256:{HASH_B}",
        "recording_context_sha256": f"sha256:{HASH_C}",
    }
    assert "chapters" not in adapted["content"]
    assert adapted["content"]["title"] == source["title"]
    assert adapted["content"]["objective"] == source["objective"]
    assert adapted["content"]["timeline_sections"] == source["timeline_sections"]
    assert adapted["content"]["speaker_summaries"] == source["speaker_summaries"]


def test_adapter_output_is_detached_from_source_and_exported_values() -> None:
    source = _document()
    envelope = _adapt(source)
    exported = envelope.to_dict()

    source["summary"]["text"] = "mutated source"
    exported["content"]["summary"]["text"] = "mutated export"

    assert envelope.content["summary"]["text"] == (
        "团队确认先固定公开合成基线，再验证配置加载和文档适配。"
    )


def test_adapter_rejects_unknown_fields_and_missing_meeting_objective() -> None:
    document = _document()
    document["plugin_state"] = {"published": True}
    with pytest.raises(StructuredNoteAdapterError, match="unknown fields"):
        _adapt(document)

    document = _document()
    document.pop("objective")
    with pytest.raises(StructuredNoteAdapterError, match="require objective"):
        _adapt(document)


def test_adapter_rejects_invalid_source_hash() -> None:
    with pytest.raises(StructuredNoteAdapterError, match="evidence_bundle_sha256"):
        adapt_current_structured_document(
            _document(),
            document_id="synthetic-document:meeting-baseline",
            content_type="meeting",
            profile=ProfileReference(
                profile_id="speech-capture/meeting",
                profile_version="builtin-2026-08-27.1",
                bundle_sha256=f"sha256:{HASH_A}",
            ),
            evidence_bundle_sha256="not-a-hash",
            corrected_transcript_sha256=HASH_B,
            recording_context_sha256=HASH_C,
            validated_at="2026-08-28T10:00:00+08:00",
        )
