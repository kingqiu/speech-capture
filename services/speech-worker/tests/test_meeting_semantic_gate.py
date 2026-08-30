"""B3.1 deterministic meeting semantic guard tests."""

from __future__ import annotations

import copy

from speech_capture_worker.meeting_semantic_gate import (
    MEETING_SEMANTIC_VALIDATORS,
    evaluate_meeting_semantic_edit,
    repair_meeting_semantic_edit,
)


def _item(text: str, evidence: str) -> dict:
    return {"text": text, "evidence": [evidence]}


def _baseline() -> dict:
    return {
        "summary": _item("团队核对数据规则和后续交付。", "seg_1"),
        "highlights": [_item("接口成功率必须达到 95%。", "seg_2")],
        "topics": [],
        "discussion_threads": [],
        "decisions": [_item("接口成功率必须达到 95%。", "seg_2")],
        "actions": [
            {
                "task": "提交完整的接口检查清单。",
                "owner": "数据组",
                "deadline": "",
                "evidence": ["seg_3"],
            }
        ],
        "risks": [],
        "open_questions": [_item("4 个字段冲突如何处理？", "seg_4")],
        "timeline_sections": [
            {
                "title": "确认数据范围",
                "summary": "日志范围需要覆盖 6–12 个月。",
                "details": [],
                "start_segment_id": "seg_1",
                "end_segment_id": "seg_4",
            }
        ],
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


def _candidate() -> dict:
    candidate = copy.deepcopy(_baseline())
    candidate["summary"] = _item(
        "团队确认接口成功率必须达到 95%，并要求提交完整检查清单。", "seg_2"
    )
    candidate["highlights"].append(_item("日志范围覆盖 6–12 个月。", "seg_4"))
    return candidate


def _segments() -> list[dict]:
    return [
        {"segment_id": "seg_1", "speaker_id": "speaker_1", "text": "核对数据规则。"},
        {
            "segment_id": "seg_2",
            "speaker_id": "speaker_1",
            "text": "接口成功率必须达到 95%。",
        },
        {
            "segment_id": "seg_3",
            "speaker_id": "speaker_2",
            "text": "数据组提交完整的接口检查清单。",
        },
        {
            "segment_id": "seg_4",
            "speaker_id": "speaker_2",
            "text": "有 4 个字段冲突，日志范围覆盖 6–12 个月。",
        },
    ]


def test_semantic_gate_accepts_consistent_preserving_candidate() -> None:
    result = evaluate_meeting_semantic_edit(
        _baseline(),
        _candidate(),
        segments=_segments(),
        validators=tuple(MEETING_SEMANTIC_VALIDATORS),
    )

    assert result.passed is True
    assert result.issue_codes == ()


def test_semantic_gate_rejects_summary_claims_without_structured_results() -> None:
    candidate = _candidate()
    candidate["summary"]["text"] = "会议达成多项决定，并完成了任务分配和明确时间节点。"
    candidate["decisions"] = []
    candidate["actions"] = []

    result = evaluate_meeting_semantic_edit(
        _baseline(), candidate, segments=_segments(), validators=tuple(MEETING_SEMANTIC_VALIDATORS)
    )

    assert result.passed is False
    assert "summary_claims_missing_decisions" in result.issue_codes
    assert "summary_claims_missing_assignments" in result.issue_codes
    assert "summary_claims_missing_deadlines" in result.issue_codes


def test_semantic_gate_rejects_removed_evidence_backed_results() -> None:
    candidate = _candidate()
    candidate["actions"] = []
    candidate["open_questions"] = []

    result = evaluate_meeting_semantic_edit(
        _baseline(), candidate, segments=_segments(), validators=tuple(MEETING_SEMANTIC_VALIDATORS)
    )

    assert "evidence_backed_actions_removed" in result.issue_codes
    assert "evidence_backed_open_questions_removed" in result.issue_codes


def test_semantic_gate_requires_important_timeline_numbers_in_primary_sections() -> None:
    candidate = _candidate()
    candidate["highlights"] = candidate["highlights"][:1]

    result = evaluate_meeting_semantic_edit(
        _baseline(), candidate, segments=_segments(), validators=tuple(MEETING_SEMANTIC_VALIDATORS)
    )

    assert "important_quantitative_facts_not_promoted" in result.issue_codes


def test_semantic_gate_rejects_cross_speaker_evidence_and_invented_role() -> None:
    candidate = _candidate()
    candidate["speaker_summaries"][0].update(
        {
            "role": "项目负责人",
            "summary": "负责数据交付。",
            "evidence": ["seg_3"],
        }
    )

    result = evaluate_meeting_semantic_edit(
        _baseline(), candidate, segments=_segments(), validators=tuple(MEETING_SEMANTIC_VALIDATORS)
    )

    assert "speaker_summary_has_no_self_evidence" in result.issue_codes
    assert "speaker_role_not_directly_supported" in result.issue_codes
    assert "speaker_summary_sentence_not_grounded" in result.issue_codes


def test_semantic_gate_does_nothing_without_explicit_registered_validators() -> None:
    result = evaluate_meeting_semantic_edit(
        _baseline(),
        {"summary": _item("会议达成多项决定。", "seg_1")},
        segments=_segments(),
        validators=(),
    )

    assert result.passed is True
    assert result.checks == {}


def test_deterministic_repair_restores_results_and_removes_false_summary_claims() -> None:
    candidate = _candidate()
    candidate["summary"]["text"] = "会议达成多项决定。数据准备任务的分配已经完成。"
    candidate["decisions"] = []
    candidate["actions"] = []
    candidate["open_questions"] = []

    repaired = repair_meeting_semantic_edit(
        _baseline(),
        candidate,
        segments=_segments(),
        validators=tuple(MEETING_SEMANTIC_VALIDATORS),
    )

    assert repaired["decisions"] == _baseline()["decisions"]
    assert repaired["actions"] == _baseline()["actions"]
    assert repaired["open_questions"] == _baseline()["open_questions"]
    assert "达成多项决定" in repaired["summary"]["text"]
    assert "任务的分配" in repaired["summary"]["text"]

    repaired["decisions"] = []
    repaired["actions"] = []
    final_repaired = repair_meeting_semantic_edit(
        {**_baseline(), "decisions": [], "actions": []},
        repaired,
        segments=_segments(),
        validators=tuple(MEETING_SEMANTIC_VALIDATORS),
    )
    assert "达成多项决定" not in final_repaired["summary"]["text"]
    assert "任务的分配" not in final_repaired["summary"]["text"]


def test_deterministic_repair_falls_back_from_ungrounded_speaker_summary() -> None:
    candidate = _candidate()
    candidate["speaker_summaries"][0].update(
        {
            "role": "总负责人",
            "summary": "speaker_1 是会议主要组织者和长期数据负责人。",
        }
    )

    repaired = repair_meeting_semantic_edit(
        _baseline(),
        candidate,
        segments=_segments(),
        validators=tuple(MEETING_SEMANTIC_VALIDATORS),
    )

    assert repaired["speaker_summaries"][0]["role"] == ""
    assert repaired["speaker_summaries"][0]["summary"] == "提出匹配规则。"
