"""Public-synthetic end-to-end matrix for all three registered B3.2 repairs."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from speech_capture_worker.content_profiles import load_profile_bundle
from speech_capture_worker.meeting_field_repair_shadow import MeetingFieldRepairShadowError
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
    TOPIC_DETAIL_ISSUE,
    MeetingFieldRepairPlanningError,
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


@dataclass(frozen=True)
class RepairCase:
    name: str
    issue: MeetingRepairIssue
    valid_result: dict[str, Any]
    invalid_result: dict[str, Any]
    expected_field: str
    expected_failure: str


def _segments() -> list[dict[str, Any]]:
    return [
        {
            "segment_id": "seg_public_1",
            "speaker_id": "speaker_public_1",
            "text": "我说明公开合成范围。",
            "start_ms": 0,
        },
        {
            "segment_id": "seg_public_2",
            "speaker_id": "speaker_public_1",
            "text": "公开合成匹配率必须达到 100%。",
            "start_ms": 1_000,
        },
    ]


def _baseline(*, quantitative_gap: bool = False) -> dict[str, Any]:
    topic = {
        "title": "公开合成规则",
        "summary": "团队确认公开合成规则。",
        "details": [],
        "evidence": ["seg_public_1", "seg_public_2"],
    }
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
        "topics": [topic],
        "timeline_sections": [
            {
                "title": "公开合成规则",
                "summary": (
                    "匹配率必须达到 100%。"
                    if quantitative_gap
                    else "团队核对公开合成规则。"
                ),
                "details": [],
                "start_segment_id": "seg_public_1",
                "end_segment_id": "seg_public_2",
            }
        ],
        "scene_sections": [],
        "discussion_threads": [],
        "speaker_summaries": [
            {
                "speaker_id": "speaker_public_1",
                "display_name": "",
                "affiliation": "",
                "role": "",
                "summary": "说明公开合成范围。",
                "evidence": ["seg_public_1"],
            }
        ],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
        "chapters": [
            {
                "title": topic["title"],
                "summary": topic["summary"],
                "evidence": topic["evidence"],
            }
        ],
        "objective": {"text": "核对公开合成范围。", "evidence": ["seg_public_1"]},
    }


def _cases() -> tuple[RepairCase, ...]:
    return (
        RepairCase(
            name="quantitative",
            issue=MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_public_2",),
            ),
            valid_result={
                "items": [
                    {
                        "text": "公开合成匹配率必须达到 100%。",
                        "evidence": ["seg_public_2"],
                    }
                ]
            },
            invalid_result={
                "items": [
                    {
                        "text": "公开合成匹配率必须达到 200%。",
                        "evidence": ["seg_public_2"],
                    }
                ]
            },
            expected_field="highlights",
            expected_failure="repair_result_invents_quantitative_fact",
        ),
        RepairCase(
            name="speaker",
            issue=MeetingRepairIssue(
                code="speaker_summary_sentence_not_grounded",
                target=MeetingFieldTarget(
                    field="speaker_summaries", item_key="speaker_public_1"
                ),
                anchor_segment_ids=("seg_public_1", "seg_public_2"),
                speaker_id="speaker_public_1",
            ),
            valid_result={
                "speaker_id": "speaker_public_1",
                "summary": "说明公开合成范围和匹配规则。",
                "evidence": ["seg_public_1", "seg_public_2"],
            },
            invalid_result={
                "speaker_id": "speaker_public_1",
                "summary": "引用包外证据。",
                "evidence": ["seg_public_missing"],
            },
            expected_field="speaker_summaries",
            expected_failure="repair_evidence_outside_packet",
        ),
        RepairCase(
            name="topic_detail",
            issue=MeetingRepairIssue(
                code=TOPIC_DETAIL_ISSUE,
                target=MeetingFieldTarget(field="topics", item_index=0),
                anchor_segment_ids=("seg_public_2",),
            ),
            valid_result={
                "items": [
                    {
                        "text": "匹配率必须达到 100%。",
                        "evidence": ["seg_public_2"],
                    }
                ]
            },
            invalid_result={
                "items": [
                    {
                        "text": "越权返回字段。",
                        "evidence": ["seg_public_2"],
                        "role": "负责人",
                    }
                ]
            },
            expected_field="topics",
            expected_failure="repair_result_has_unauthorized_fields",
        ),
    )


def _opt_in() -> MeetingFieldRepairShadowOptIn:
    return MeetingFieldRepairShadowOptIn(
        enabled=True,
        content_type="meeting",
        data_classification=PUBLIC_SYNTHETIC_CLASSIFICATION,
        result_mode=MEMORY_ONLY_RESULT_MODE,
        allow_persistence=False,
    )


def _baseline_for(case: RepairCase) -> dict[str, Any]:
    return _baseline(quantitative_gap=case.name == "quantitative")


def _run(case: RepairCase, responder, *, cancelled=None):
    transport = RecordingSyntheticFieldRepairTransport(responder)
    baseline = _baseline_for(case)
    original = copy.deepcopy(baseline)
    result = run_orchestrated_public_synthetic_meeting_field_repair_shadow(
        opt_in=_opt_in(),
        bundle=load_profile_bundle(_PROFILE_PARENT / "2026-08-29.2"),
        baseline=baseline,
        segments=_segments(),
        issues=(case.issue,),
        transport=transport,
        cancelled=cancelled,
    )
    assert baseline == original
    assert transport.active_call_count == 0
    return result, transport


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_all_registered_repairs_succeed_end_to_end_without_partial_writes(
    case: RepairCase,
) -> None:
    result, transport = _run(case, lambda envelope: case.valid_result)

    assert result.shadow.call_count == 1
    assert transport.finished_call_count == 1
    assert result.shadow.document[case.expected_field] != _baseline_for(case)[
        case.expected_field
    ]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_all_registered_repairs_reject_unauthorized_results_without_retry(
    case: RepairCase,
) -> None:
    baseline = _baseline_for(case)
    original = copy.deepcopy(baseline)
    transport = RecordingSyntheticFieldRepairTransport(
        lambda envelope: case.invalid_result
    )

    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        run_orchestrated_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=load_profile_bundle(_PROFILE_PARENT / "2026-08-29.2"),
            baseline=baseline,
            segments=_segments(),
            issues=(case.issue,),
            transport=transport,
        )

    assert raised.value.code == case.expected_failure
    assert transport.finished_call_count == 1
    assert transport.active_call_count == 0
    assert baseline == original


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_all_registered_repairs_cleanup_timeout_then_recover_on_fresh_run(
    case: RepairCase,
) -> None:
    timed_out = RecordingSyntheticFieldRepairTransport(
        lambda envelope: (_ for _ in ()).throw(TimeoutError("synthetic timeout"))
    )
    baseline = _baseline_for(case)
    original = copy.deepcopy(baseline)

    with pytest.raises(MeetingFieldRepairShadowError) as raised:
        run_orchestrated_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=load_profile_bundle(_PROFILE_PARENT / "2026-08-29.2"),
            baseline=baseline,
            segments=_segments(),
            issues=(case.issue,),
            transport=timed_out,
        )

    assert raised.value.code == "field_repair_call_timeout"
    assert timed_out.active_call_count == 0
    assert timed_out.finished_call_count == 1
    assert baseline == original

    recovered, transport = _run(case, lambda envelope: case.valid_result)
    assert recovered.shadow.call_count == 1
    assert transport.finished_call_count == 1


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_all_registered_repairs_honor_pre_cancellation_without_transport(
    case: RepairCase,
) -> None:
    transport = RecordingSyntheticFieldRepairTransport(
        lambda envelope: pytest.fail("cancelled orchestration reached transport")
    )

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_orchestrated_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=load_profile_bundle(_PROFILE_PARENT / "2026-08-29.2"),
            baseline=_baseline_for(case),
            segments=_segments(),
            issues=(case.issue,),
            transport=transport,
            cancelled=lambda: True,
        )

    assert raised.value.code == "shadow_bridge_cancelled"
    assert transport.active_call_count == 0
    assert transport.finished_call_count == 0


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_all_registered_repairs_recover_one_parser_retry_within_budget(
    case: RepairCase,
) -> None:
    responses = iter(("not-json", case.valid_result))
    result, transport = _run(case, lambda envelope: next(responses))

    assert result.shadow.call_count == 2
    assert result.shadow.parser_retry_count == 1
    assert transport.finished_call_count == 2
    assert transport.active_call_count == 0
