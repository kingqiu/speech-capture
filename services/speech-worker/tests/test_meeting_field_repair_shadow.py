"""Public synthetic tests for the isolated B3.2 short-call shadow runner."""

from __future__ import annotations

import ast
import copy
import json
import time
from pathlib import Path

import pytest

import speech_capture_worker.meeting_field_repair_shadow as shadow_module
from speech_capture_worker.meeting_field_repair_shadow import (
    MeetingFieldRepairShadowError,
    run_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repairs import (
    QUANTITATIVE_PROMOTION_ISSUE,
    MeetingFieldRepairPlanningError,
    MeetingFieldTarget,
    MeetingRepairIssue,
    plan_meeting_field_repairs,
)


def _baseline() -> dict:
    return {
        "summary": {"text": "团队核对规则。", "evidence": ["seg_1"]},
        "highlights": [],
        "topics": [],
        "actions": [],
        "open_questions": [],
        "speaker_summaries": [],
    }


def _segments() -> list[dict]:
    return [
        {"segment_id": "seg_1", "speaker_id": "speaker_1", "text": "先核对范围。"},
        {"segment_id": "seg_2", "speaker_id": "speaker_1", "text": "匹配达到 100%。"},
        {"segment_id": "seg_3", "speaker_id": "speaker_2", "text": "之后提交模板。"},
    ]


def _plan(field: str = "highlights"):
    return plan_meeting_field_repairs(
        baseline=_baseline(),
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field=field),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]


def _valid_result() -> dict:
    return {"items": [{"text": "匹配达到 100%。", "evidence": ["seg_2"]}]}


def test_runs_one_bounded_call_and_final_validator_without_writes() -> None:
    baseline = _baseline()
    original = copy.deepcopy(baseline)
    requests = []
    final_calls = []

    def caller(request):
        requests.append(request)
        return json.dumps(_valid_result(), ensure_ascii=False)

    def final_validator(document):
        final_calls.append(copy.deepcopy(document))
        return document

    result = run_meeting_field_repair_shadow(
        baseline=baseline,
        plans=(_plan(),),
        caller=caller,
        final_validator=final_validator,
    )

    assert result.call_count == 1
    assert result.parser_retry_count == 0
    assert result.document["highlights"] == _valid_result()["items"]
    assert requests[0].timeout_seconds <= 120
    assert requests[0].result_schema["additionalProperties"] is False
    assert len(final_calls) == 1
    assert baseline == original


def test_retries_once_only_for_unparseable_json() -> None:
    responses = iter(("not-json", json.dumps(_valid_result(), ensure_ascii=False)))
    attempts = []

    def caller(request):
        attempts.append(request.attempt)
        return next(responses)

    result = run_meeting_field_repair_shadow(
        baseline=_baseline(),
        plans=(_plan(),),
        caller=caller,
        final_validator=lambda document: document,
    )

    assert attempts == [1, 2]
    assert result.call_count == 2
    assert result.parser_retry_count == 1


def test_does_not_retry_schema_evidence_or_semantic_failures() -> None:
    calls = 0

    def caller(request):
        nonlocal calls
        calls += 1
        return {"items": [{"text": "捏造 200%。", "evidence": ["seg_2"]}]}

    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=caller,
            final_validator=lambda document: document,
        )

    assert raised.value.code == "repair_result_invents_quantitative_fact"
    assert calls == 1


def test_parser_retry_counts_against_the_three_call_global_budget() -> None:
    plans = plan_meeting_field_repairs(
        baseline={**_baseline(), "highlights": [], "actions": [], "open_questions": []},
        segments=_segments(),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="actions"),
                anchor_segment_ids=("seg_3",),
            ),
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="open_questions"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )
    calls = 0

    def caller(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "broken"
        if request.plan.target.field == "actions":
            return {
                "items": [
                    {"task": "提交模板。", "owner": "", "deadline": "", "evidence": ["seg_3"]}
                ]
            }
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowError) as raised:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=plans,
            caller=caller,
            final_validator=lambda document: document,
        )

    assert raised.value.code == "field_repair_call_budget_exceeded"
    assert calls == 3


def test_transport_timeout_and_elapsed_timeout_never_retry() -> None:
    calls = 0

    def transport_timeout(request):
        nonlocal calls
        calls += 1
        raise TimeoutError

    with pytest.raises(MeetingFieldRepairShadowError) as transport:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=transport_timeout,
            final_validator=lambda document: document,
        )
    assert transport.value.code == "field_repair_call_timeout"
    assert calls == 1

    def slow_call(request):
        time.sleep(0.02)
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowError) as elapsed:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=slow_call,
            final_validator=lambda document: document,
            call_timeout_seconds=0.005,
        )
    assert elapsed.value.code == "field_repair_call_timeout"


def test_emits_content_free_heartbeats_at_no_more_than_ten_seconds() -> None:
    progress = []

    def caller(request):
        time.sleep(0.035)
        return _valid_result()

    run_meeting_field_repair_shadow(
        baseline=_baseline(),
        plans=(_plan(),),
        caller=caller,
        final_validator=lambda document: document,
        progress=progress.append,
        heartbeat_seconds=0.01,
    )

    field_events = [event for event in progress if event.substage == "field_repair"]
    assert len(field_events) >= 3
    assert {event.target_field for event in field_events} == {"highlights"}
    assert all("text" not in event.to_dict() for event in field_events)
    assert all("segments" not in event.to_dict() for event in field_events)


def test_final_gate_failure_returns_no_partial_result_or_retry() -> None:
    baseline = _baseline()
    original = copy.deepcopy(baseline)
    calls = 0

    def caller(request):
        nonlocal calls
        calls += 1
        return _valid_result()

    def reject_final(document):
        raise MeetingFieldRepairPlanningError("synthetic_final_failure", "Rejected.")

    with pytest.raises(MeetingFieldRepairPlanningError) as raised:
        run_meeting_field_repair_shadow(
            baseline=baseline,
            plans=(_plan(),),
            caller=caller,
            final_validator=reject_final,
        )

    assert raised.value.code == "synthetic_final_failure"
    assert calls == 1
    assert baseline == original


def test_empty_plan_runs_only_final_validation_without_a_model_call() -> None:
    calls = 0

    def caller(request):
        nonlocal calls
        calls += 1
        return pytest.fail("no field call expected")

    result = run_meeting_field_repair_shadow(
        baseline=_baseline(),
        plans=(),
        caller=caller,
        final_validator=lambda document: document,
    )

    assert calls == 0
    assert result.call_count == 0
    assert result.document == _baseline()


def test_shadow_runner_imports_no_formal_state_or_publication_components() -> None:
    source_path = Path(shadow_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    forbidden = {
        "speech_capture_worker.job_store",
        "speech_capture_worker.checkpoints",
        "speech_capture_worker.summary_revisions",
        "speech_capture_worker.artifact_generation",
        "speech_capture_worker.publication",
        "speech_capture_worker.api",
    }

    assert imported_modules.isdisjoint(forbidden)
