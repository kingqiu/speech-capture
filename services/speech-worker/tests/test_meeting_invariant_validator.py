"""Tests for the sealed, read-only Worker meeting invariant capability."""

from __future__ import annotations

import ast
import copy
import inspect
from pathlib import Path

import pytest

import speech_capture_worker.meeting_invariant_validator as invariant_module
from speech_capture_worker.meeting_invariant_validator import (
    MeetingInvariantEvidenceSnapshot,
    MeetingInvariantValidatorError,
    TrustedMeetingInvariantValidator,
)
from speech_capture_worker.structuring_execution import (
    build_trusted_meeting_invariant_validator,
)


def _segments() -> list[dict]:
    return [
        {
            "segment_id": "seg_public_1",
            "speaker_id": "speaker_public_1",
            "text": "团队需要核对公开合成范围。",
            "start_ms": 0,
        },
        {
            "segment_id": "seg_public_2",
            "speaker_id": "speaker_public_1",
            "text": "公开合成匹配率必须达到 100%。",
            "start_ms": 1_000,
        },
    ]


def _normalized_document() -> dict:
    return {
        "title": "公开合成规则会议",
        "summary": {"text": "团队核对公开合成规则。", "evidence": ["seg_public_1"]},
        "context": [
            {
                "kind": "purpose",
                "title": "会议目的",
                "text": "核对公开合成范围。",
                "evidence": ["seg_public_1"],
            },
            {
                "kind": "constraint",
                "title": "验收标准",
                "text": "团队使用明确的验收标准完成核对。",
                "evidence": ["seg_public_2"],
            },
        ],
        "highlights": [],
        "topics": [],
        "timeline_sections": [
            {
                "title": "公开合成规则",
                "summary": "团队核对公开合成规则。",
                "details": [],
                "start_segment_id": "seg_public_1",
                "end_segment_id": "seg_public_2",
            }
        ],
        "scene_sections": [],
        "discussion_threads": [],
        "speaker_summaries": [],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
        "chapters": [],
        "objective": {"text": "核对公开合成范围。", "evidence": ["seg_public_1"]},
    }


def test_trusted_capability_accepts_only_idempotent_document_and_returns_copy() -> None:
    document = _normalized_document()
    original = copy.deepcopy(document)
    validator = build_trusted_meeting_invariant_validator(_segments())

    validated = validator(document)

    assert validated == original
    assert validated is not document
    assert document == original
    validated["highlights"].append(
        {"text": "仅修改返回副本。", "evidence": ["seg_public_1"]}
    )
    assert document == original


def test_trusted_capability_rejects_normalization_without_rewriting_source() -> None:
    document = _normalized_document()
    document.pop("objective")
    original = copy.deepcopy(document)

    with pytest.raises(MeetingInvariantValidatorError) as raised:
        build_trusted_meeting_invariant_validator(_segments())(document)

    assert raised.value.code == "meeting_invariant_document_not_idempotent"
    assert document == original


def test_trusted_capability_wraps_formal_invariant_rejection() -> None:
    document = _normalized_document()
    document["summary"]["evidence"] = ["seg_public_missing"]

    with pytest.raises(MeetingInvariantValidatorError) as raised:
        build_trusted_meeting_invariant_validator(_segments())(document)

    assert raised.value.code == "meeting_invariant_validation_failed"


def test_capability_cannot_be_constructed_with_an_arbitrary_validator() -> None:
    snapshot = MeetingInvariantEvidenceSnapshot.from_segments(_segments())

    with pytest.raises(MeetingInvariantValidatorError) as raised:
        TrustedMeetingInvariantValidator(
            token=object(),
            snapshot=snapshot,
            validator=lambda document: document,
        )

    assert raised.value.code == "meeting_invariant_capability_untrusted"


def test_evidence_snapshot_is_immutable_ordered_and_content_addressed() -> None:
    snapshot = MeetingInvariantEvidenceSnapshot.from_segments(list(reversed(_segments())))

    assert snapshot.segment_ids == ("seg_public_1", "seg_public_2")
    assert snapshot.snapshot_sha256.startswith("sha256:")
    assert snapshot.matches_segments(_segments()) is True
    with pytest.raises(TypeError):
        snapshot.segment_texts["seg_public_1"] = "不能修改"


def test_evidence_snapshot_rejects_ambiguous_order() -> None:
    segments = _segments()
    segments[1]["start_ms"] = 0

    with pytest.raises(MeetingInvariantValidatorError) as raised:
        MeetingInvariantEvidenceSnapshot.from_segments(segments)

    assert raised.value.code == "meeting_invariant_evidence_order_invalid"


def test_adapter_module_has_no_state_filesystem_transport_or_model_dependency() -> None:
    tree = ast.parse(Path(invariant_module.__file__).read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert imported_modules.isdisjoint(
        {
            "os",
            "pathlib",
            "threading",
            "urllib",
            "http.client",
            "requests",
            "speech_capture_worker.job_store",
            "speech_capture_worker.checkpoints",
            "speech_capture_worker.summary_revisions",
            "speech_capture_worker.artifact_generation",
            "speech_capture_worker.publication",
            "speech_capture_worker.api",
            "speech_capture_worker.meeting_field_repair_local_transport",
        }
    )

    factory_source = inspect.getsource(build_trusted_meeting_invariant_validator)
    assert all(
        forbidden not in factory_source
        for forbidden in (
            "JobStore",
            "checkpoint",
            "publication",
            "Path(",
            "urllib",
            "Ollama",
        )
    )
