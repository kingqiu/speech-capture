"""Public synthetic contract tests for B3.2 bounded field repair planning."""

from __future__ import annotations

import copy

import pytest

from speech_capture_worker.meeting_field_repairs import (
    MAX_PACKET_ESTIMATED_TOKENS,
    QUANTITATIVE_PROMOTION_ISSUE,
    QUANTITATIVE_PROMOTION_REPAIR,
    SPEAKER_GROUNDING_REPAIR,
    TOPIC_DETAIL_ISSUE,
    TOPIC_DETAIL_REPAIR,
    MeetingFieldRepairPlanningError,
    MeetingFieldTarget,
    MeetingRepairIssue,
    canonical_json_sha256,
    current_target_sha256,
    meeting_repair_result_json_schema,
    plan_meeting_field_repairs,
    validate_and_merge_meeting_field_repairs,
)


def _baseline() -> dict:
    return {
        "summary": {"text": "团队核对交付规则。", "evidence": ["seg_1"]},
        "highlights": [],
        "topics": [
            {
                "title": "交付规则",
                "summary": "确认数据口径。",
                "details": [],
                "evidence": ["seg_2"],
            }
        ],
        "actions": [],
        "open_questions": [],
        "speaker_summaries": [
            {
                "speaker_id": "speaker_1",
                "display_name": "",
                "affiliation": "",
                "role": "",
                "summary": "提出匹配规则。",
                "evidence": ["seg_2"],
            }
        ],
    }


def _segments() -> list[dict]:
    return [
        {"segment_id": "seg_1", "speaker_id": "speaker_2", "text": "先核对范围。"},
        {
            "segment_id": "seg_2",
            "speaker_id": "speaker_1",
            "text": "匹配规则必须达到百分之百。",
        },
        {"segment_id": "seg_3", "speaker_id": "speaker_2", "text": "之后提交模板。"},
        {"segment_id": "seg_4", "speaker_id": "speaker_1", "text": "我补充检查冲突。"},
    ]


def _identity_validator(document: dict) -> dict:
    return document


def test_plans_quantitative_repair_with_bounded_adjacent_evidence() -> None:
    baseline = _baseline()
    original = copy.deepcopy(baseline)
    plans = plan_meeting_field_repairs(
        baseline=baseline,
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.repair_key == QUANTITATIVE_PROMOTION_REPAIR
    assert [item["segment_id"] for item in plan.evidence_packet.segments] == [
        "seg_1",
        "seg_2",
        "seg_3",
    ]
    assert plan.baseline_field_sha256 == canonical_json_sha256([])
    assert baseline == original


def test_speaker_packet_only_contains_the_target_speakers_own_evidence() -> None:
    plans = plan_meeting_field_repairs(
        baseline=_baseline(),
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code="speaker_summary_sentence_not_grounded",
                target=MeetingFieldTarget(
                    field="speaker_summaries", item_key="speaker_1"
                ),
                anchor_segment_ids=("seg_2", "seg_4"),
                speaker_id="speaker_1",
            ),
        ),
    )

    plan = plans[0]
    assert plan.repair_key == SPEAKER_GROUNDING_REPAIR
    assert [item["segment_id"] for item in plan.evidence_packet.segments] == [
        "seg_2",
        "seg_4",
    ]
    assert all(item["speaker_id"] == "speaker_1" for item in plan.evidence_packet.segments)


def test_speaker_repair_requires_a_self_spoken_anchor() -> None:
    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        plan_meeting_field_repairs(
            baseline=_baseline(),
            segments=_segments(),
            issues=(
                MeetingRepairIssue(
                    code="speaker_summary_sentence_not_grounded",
                    target=MeetingFieldTarget(
                        field="speaker_summaries", item_key="speaker_1"
                    ),
                    anchor_segment_ids=("seg_1",),
                    speaker_id="speaker_1",
                ),
            ),
        )

    assert raised.value.code == "speaker_repair_has_no_self_anchor"


def test_plans_one_existing_topic_detail_target() -> None:
    plan = plan_meeting_field_repairs(
        baseline=_baseline(),
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=TOPIC_DETAIL_ISSUE,
                target=MeetingFieldTarget(field="topics", item_index=0),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]

    assert plan.repair_key == TOPIC_DETAIL_REPAIR
    assert plan.baseline_field_sha256 == canonical_json_sha256(_baseline()["topics"][0])


@pytest.mark.parametrize(
    ("issue_code", "expected_error"),
    [
        ("summary_claims_missing_decisions", "deterministic_issue_not_model_repairable"),
        ("evidence_backed_actions_removed", "deterministic_issue_not_model_repairable"),
        ("unregistered_semantic_failure", "unknown_meeting_repair_issue"),
    ],
)
def test_rejects_deterministic_only_and_unknown_model_repairs(
    issue_code: str, expected_error: str
) -> None:
    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        plan_meeting_field_repairs(
            baseline=_baseline(),
            segments=_segments(),
            issues=(
                MeetingRepairIssue(
                    code=issue_code,
                    target=MeetingFieldTarget(field="highlights"),
                    anchor_segment_ids=("seg_2",),
                ),
            ),
        )

    assert raised.value.code == expected_error


def test_rejects_ambiguous_or_unapproved_targets() -> None:
    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        plan_meeting_field_repairs(
            baseline=_baseline(),
            segments=_segments(),
            issues=(
                MeetingRepairIssue(
                    code=QUANTITATIVE_PROMOTION_ISSUE,
                    target=MeetingFieldTarget(field="summary"),
                    anchor_segment_ids=("seg_2",),
                ),
            ),
        )

    assert raised.value.code == "invalid_quantitative_repair_target"


def test_rejects_more_than_three_calls_and_overlapping_targets() -> None:
    issues = tuple(
        MeetingRepairIssue(
            code=TOPIC_DETAIL_ISSUE,
            target=MeetingFieldTarget(field="topics", item_index=0),
            anchor_segment_ids=("seg_2",),
        )
        for _ in range(4)
    )
    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        plan_meeting_field_repairs(
            baseline=_baseline(), segments=_segments(), issues=issues
        )
    assert raised.value.code == "repair_call_budget_exceeded"

    with pytest.raises(MeetingFieldRepairPlanningError) as overlapping:
        plan_meeting_field_repairs(
            baseline=_baseline(),
            segments=_segments(),
            issues=issues[:2],
        )
    assert overlapping.value.code == "overlapping_repair_targets"


def test_refuses_unknown_anchor_and_oversized_packet_without_truncation() -> None:
    issue = MeetingRepairIssue(
        code=QUANTITATIVE_PROMOTION_ISSUE,
        target=MeetingFieldTarget(field="highlights"),
        anchor_segment_ids=("missing",),
    )
    with pytest.raises(MeetingFieldRepairPlanningError) as unknown:
        plan_meeting_field_repairs(
            baseline=_baseline(), segments=_segments(), issues=(issue,)
        )
    assert unknown.value.code == "unknown_evidence_anchor"

    oversized_segments = [
        {
            "segment_id": "seg_big",
            "speaker_id": "speaker_1",
            "text": "字" * (MAX_PACKET_ESTIMATED_TOKENS + 1),
        }
    ]
    oversized_issue = MeetingRepairIssue(
        code=QUANTITATIVE_PROMOTION_ISSUE,
        target=MeetingFieldTarget(field="highlights"),
        anchor_segment_ids=("seg_big",),
    )
    with pytest.raises(MeetingFieldRepairPlanningError) as oversized:
        plan_meeting_field_repairs(
            baseline=_baseline(), segments=oversized_segments, issues=(oversized_issue,)
        )
    assert oversized.value.code == "repair_evidence_token_limit_exceeded"


def test_field_hash_is_stable_and_detects_a_target_change() -> None:
    baseline = _baseline()
    target = MeetingFieldTarget(field="speaker_summaries", item_key="speaker_1")
    first = current_target_sha256(baseline, target)
    reordered = copy.deepcopy(baseline)
    reordered["speaker_summaries"][0] = {
        "summary": "提出匹配规则。",
        "role": "",
        "speaker_id": "speaker_1",
        "evidence": ["seg_2"],
        "affiliation": "",
        "display_name": "",
    }
    assert current_target_sha256(reordered, target) == first

    changed = copy.deepcopy(baseline)
    changed["speaker_summaries"][0]["summary"] = "修改后的摘要。"
    assert current_target_sha256(changed, target) != first


def test_result_schemas_are_strict_and_do_not_allow_speaker_role_fields() -> None:
    speaker_plan = plan_meeting_field_repairs(
        baseline=_baseline(),
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code="speaker_summary_sentence_not_grounded",
                target=MeetingFieldTarget(
                    field="speaker_summaries", item_key="speaker_1"
                ),
                anchor_segment_ids=("seg_2",),
                speaker_id="speaker_1",
            ),
        ),
    )[0]
    schema = meeting_repair_result_json_schema(speaker_plan)

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"speaker_id", "summary", "evidence"}
    assert schema["properties"]["speaker_id"] == {"const": "speaker_1"}
    assert "role" not in schema["properties"]
    assert "affiliation" not in schema["properties"]


def test_atomically_appends_quantitative_result_and_preserves_other_fields() -> None:
    baseline = _baseline()
    original = copy.deepcopy(baseline)
    plan = plan_meeting_field_repairs(
        baseline=baseline,
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]

    merged = validate_and_merge_meeting_field_repairs(
        baseline=baseline,
        plans=(plan,),
        results=(
            {
                "items": [
                    {
                        "text": "匹配规则必须达到百分之百。",
                        "evidence": ["seg_2"],
                    }
                ]
            },
        ),
        final_validator=_identity_validator,
    )

    assert merged["highlights"] == [
        {"text": "匹配规则必须达到百分之百。", "evidence": ["seg_2"]}
    ]
    assert merged["summary"] == baseline["summary"]
    assert baseline == original


def test_speaker_merge_preserves_identity_and_role_metadata() -> None:
    baseline = _baseline()
    baseline["speaker_summaries"][0]["display_name"] = "甲"
    plan = plan_meeting_field_repairs(
        baseline=baseline,
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code="speaker_summary_sentence_not_grounded",
                target=MeetingFieldTarget(
                    field="speaker_summaries", item_key="speaker_1"
                ),
                anchor_segment_ids=("seg_2", "seg_4"),
                speaker_id="speaker_1",
            ),
        ),
    )[0]

    merged = validate_and_merge_meeting_field_repairs(
        baseline=baseline,
        plans=(plan,),
        results=(
            {
                "speaker_id": "speaker_1",
                "summary": "提出匹配规则并补充检查冲突。",
                "evidence": ["seg_2", "seg_4"],
            },
        ),
        final_validator=_identity_validator,
    )

    summary = merged["speaker_summaries"][0]
    assert summary["display_name"] == "甲"
    assert summary["role"] == ""
    assert summary["affiliation"] == ""
    assert summary["summary"] == "提出匹配规则并补充检查冲突。"


def test_topic_detail_merge_only_appends_bounded_details() -> None:
    baseline = _baseline()
    plan = plan_meeting_field_repairs(
        baseline=baseline,
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=TOPIC_DETAIL_ISSUE,
                target=MeetingFieldTarget(field="topics", item_index=0),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]

    merged = validate_and_merge_meeting_field_repairs(
        baseline=baseline,
        plans=(plan,),
        results=(
            {"items": [{"text": "匹配规则必须达到百分之百。", "evidence": ["seg_2"]}]},
        ),
        final_validator=_identity_validator,
    )

    assert merged["topics"][0]["title"] == baseline["topics"][0]["title"]
    assert merged["topics"][0]["summary"] == baseline["topics"][0]["summary"]
    assert merged["topics"][0]["details"] == [
        {"text": "匹配规则必须达到百分之百。", "evidence": ["seg_2"]}
    ]


@pytest.mark.parametrize(
    ("result", "expected_error"),
    [
        (
            {"items": [{"text": "越权字段。", "evidence": ["seg_2"]}], "summary": "x"},
            "repair_result_has_unauthorized_fields",
        ),
        (
            {"items": [{"text": "包外证据。", "evidence": ["seg_4"]}]},
            "repair_evidence_outside_packet",
        ),
        (
            {"items": [{"text": "重复证据。", "evidence": ["seg_2", "seg_2"]}]},
            "invalid_repair_evidence",
        ),
    ],
)
def test_rejects_unauthorized_or_invalid_local_results(
    result: dict, expected_error: str
) -> None:
    plan = plan_meeting_field_repairs(
        baseline=_baseline(),
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]

    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        validate_and_merge_meeting_field_repairs(
            baseline=_baseline(),
            plans=(plan,),
            results=(result,),
            final_validator=_identity_validator,
        )
    assert raised.value.code == expected_error


def test_rejects_action_owner_or_deadline_creation() -> None:
    plan = plan_meeting_field_repairs(
        baseline=_baseline(),
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="actions"),
                anchor_segment_ids=("seg_3",),
            ),
        ),
    )[0]
    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        validate_and_merge_meeting_field_repairs(
            baseline=_baseline(),
            plans=(plan,),
            results=(
                {
                    "items": [
                        {
                            "task": "提交模板。",
                            "owner": "数据组",
                            "deadline": "明天",
                            "evidence": ["seg_3"],
                        }
                    ]
                },
            ),
            final_validator=_identity_validator,
        )
    assert raised.value.code == "repair_result_invents_action_metadata"


def test_rejects_a_number_absent_from_the_cited_packet_evidence() -> None:
    numeric_segments = _segments()
    numeric_segments[1]["text"] = "匹配规则必须达到 100%。"
    baseline = _baseline()
    plan = plan_meeting_field_repairs(
        baseline=baseline,
        segments=numeric_segments,
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]

    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        validate_and_merge_meeting_field_repairs(
            baseline=baseline,
            plans=(plan,),
            results=(
                {"items": [{"text": "匹配规则达到 100%，并扩大到 200%。", "evidence": ["seg_2"]}]},
            ),
            final_validator=_identity_validator,
        )

    assert raised.value.code == "repair_result_invents_quantitative_fact"


def test_rejects_stale_target_hash_without_mutating_baseline() -> None:
    baseline = _baseline()
    plan = plan_meeting_field_repairs(
        baseline=baseline,
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]
    changed = copy.deepcopy(baseline)
    changed["highlights"].append({"text": "并发写入。", "evidence": ["seg_1"]})
    original = copy.deepcopy(changed)

    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        validate_and_merge_meeting_field_repairs(
            baseline=changed,
            plans=(plan,),
            results=(
                {"items": [{"text": "局部结果。", "evidence": ["seg_2"]}]},
            ),
            final_validator=_identity_validator,
        )
    assert raised.value.code == "repair_target_hash_changed"
    assert changed == original


def test_invalid_later_result_never_partially_mutates_the_baseline() -> None:
    baseline = _baseline()
    original = copy.deepcopy(baseline)
    plans = plan_meeting_field_repairs(
        baseline=baseline,
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
            MeetingRepairIssue(
                code=TOPIC_DETAIL_ISSUE,
                target=MeetingFieldTarget(field="topics", item_index=0),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )

    with pytest.raises(MeetingFieldRepairPlanningError):
        validate_and_merge_meeting_field_repairs(
            baseline=baseline,
            plans=plans,
            results=(
                {"items": [{"text": "有效结果。", "evidence": ["seg_2"]}]},
                {"items": [{"text": "无效结果。", "evidence": ["seg_4"]}]},
            ),
            final_validator=_identity_validator,
        )

    assert baseline == original


def test_final_validator_is_mandatory_and_cannot_change_untouched_fields() -> None:
    baseline = _baseline()
    original = copy.deepcopy(baseline)
    plan = plan_meeting_field_repairs(
        baseline=baseline,
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]

    def invalid_final_validator(document: dict) -> dict:
        document["summary"] = {"text": "越权修改。", "evidence": ["seg_1"]}
        return document

    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        validate_and_merge_meeting_field_repairs(
            baseline=baseline,
            plans=(plan,),
            results=(
                {"items": [{"text": "局部结果。", "evidence": ["seg_2"]}]},
            ),
            final_validator=invalid_final_validator,
        )

    assert raised.value.code == "untouched_field_changed"
    assert baseline == original
