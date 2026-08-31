"""End-to-end composition tests for the explicit B3.2 shadow orchestrator."""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

import speech_capture_worker.meeting_field_repair_shadow_orchestrator as orchestrator_module
from speech_capture_worker.content_profiles import load_profile_bundle
from speech_capture_worker.meeting_field_repair_shadow_bridge import (
    MEMORY_ONLY_RESULT_MODE,
    PUBLIC_SYNTHETIC_CLASSIFICATION,
    MeetingFieldRepairShadowBridgeError,
    MeetingFieldRepairShadowOptIn,
)
from speech_capture_worker.meeting_field_repair_shadow_orchestrator import (
    run_orchestrated_public_synthetic_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repair_transport_shadow import (
    RecordingSyntheticFieldRepairTransport,
)
from speech_capture_worker.meeting_field_repairs import (
    QUANTITATIVE_PROMOTION_ISSUE,
    MeetingFieldTarget,
    MeetingRepairIssue,
)

_PROFILE_PARENT = (
    Path(__file__).parents[1]
    / "src"
    / "speech_capture_worker"
    / "profile_bundles"
    / "meeting"
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


def _baseline() -> dict:
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
                "summary": "匹配率必须达到 100%。",
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


def _opt_in() -> MeetingFieldRepairShadowOptIn:
    return MeetingFieldRepairShadowOptIn(
        enabled=True,
        content_type="meeting",
        data_classification=PUBLIC_SYNTHETIC_CLASSIFICATION,
        result_mode=MEMORY_ONLY_RESULT_MODE,
        allow_persistence=False,
    )


def _bundle():
    return load_profile_bundle(_PROFILE_PARENT / "2026-08-29.2")


def _issues() -> tuple[MeetingRepairIssue, ...]:
    return (
        MeetingRepairIssue(
            code=QUANTITATIVE_PROMOTION_ISSUE,
            target=MeetingFieldTarget(field="highlights"),
            anchor_segment_ids=("seg_public_2",),
        ),
    )


def test_orchestrator_composes_planning_profile_transport_invariant_and_progress() -> None:
    baseline = _baseline()
    segments = _segments()
    original_baseline = copy.deepcopy(baseline)
    original_segments = copy.deepcopy(segments)
    transport = RecordingSyntheticFieldRepairTransport(
        lambda envelope: {
            "items": [
                {
                    "text": "公开合成匹配率必须达到 100%。",
                    "evidence": ["seg_public_2"],
                }
            ]
        }
    )

    result = run_orchestrated_public_synthetic_meeting_field_repair_shadow(
        opt_in=_opt_in(),
        bundle=_bundle(),
        baseline=baseline,
        segments=segments,
        issues=_issues(),
        transport=transport,
    )

    assert result.transport_kind == "recording_synthetic"
    assert result.evidence_snapshot_sha256.startswith("sha256:")
    assert result.shadow.document["highlights"][0]["evidence"] == ["seg_public_2"]
    assert [event.substage for event in result.progress_events] == [
        "repair_planning",
        "field_repair",
        "field_repair",
        "final_validation",
    ]
    assert transport.active_call_count == 0
    assert transport.finished_call_count == 1
    assert baseline == original_baseline
    assert segments == original_segments


def test_orchestrator_rejects_arbitrary_transport_before_planning() -> None:
    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_orchestrated_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=_bundle(),
            baseline=_baseline(),
            segments=_segments(),
            issues=_issues(),
            transport=lambda request: {},
        )

    assert raised.value.code == "shadow_orchestrator_transport_untrusted"


def test_orchestrator_is_not_imported_by_production_runtime_modules() -> None:
    source_root = Path(orchestrator_module.__file__).parent
    for path in source_root.glob("*.py"):
        if path == Path(orchestrator_module.__file__):
            continue
        assert "meeting_field_repair_shadow_orchestrator" not in path.read_text(
            encoding="utf-8"
        )


def test_orchestrator_has_no_direct_formal_state_or_persistence_dependency() -> None:
    tree = ast.parse(Path(orchestrator_module.__file__).read_text(encoding="utf-8"))
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
            "speech_capture_worker.job_store",
            "speech_capture_worker.checkpoints",
            "speech_capture_worker.summary_revisions",
            "speech_capture_worker.artifact_generation",
            "speech_capture_worker.publication",
            "speech_capture_worker.api",
        }
    )
