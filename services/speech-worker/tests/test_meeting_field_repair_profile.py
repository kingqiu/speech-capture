"""Public synthetic tests for the B3.2 Profile-to-shadow adapter."""

from __future__ import annotations

import ast
import copy
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

import speech_capture_worker.meeting_field_repair_profile as profile_module
from speech_capture_worker.content_profile_prompts import load_bundled_meeting_profile
from speech_capture_worker.content_profiles import load_profile_bundle
from speech_capture_worker.meeting_field_repair_profile import (
    MeetingFieldRepairProfileError,
    build_meeting_field_repair_shadow_config,
    run_profiled_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repair_shadow import (
    MeetingFieldRepairShadowError,
    run_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repairs import (
    QUANTITATIVE_PROMOTION_ISSUE,
    QUANTITATIVE_PROMOTION_REPAIR,
    MeetingFieldTarget,
    MeetingRepairIssue,
    plan_meeting_field_repairs,
)

_PROFILE_ROOT = (
    Path(__file__).parents[1]
    / "src"
    / "speech_capture_worker"
    / "profile_bundles"
    / "meeting"
)


def _bundle():
    return load_profile_bundle(_PROFILE_ROOT / "2026-08-29.2")


def _baseline() -> dict:
    return {
        "summary": {"text": "团队核对规则。", "evidence": ["seg_1"]},
        "highlights": [],
        "topics": [],
        "actions": [],
        "open_questions": [],
        "speaker_summaries": [],
    }


def _plan():
    return plan_meeting_field_repairs(
        baseline=_baseline(),
        segments=(
            {"segment_id": "seg_1", "speaker_id": "speaker_1", "text": "先核对范围。"},
            {"segment_id": "seg_2", "speaker_id": "speaker_1", "text": "匹配达到 100%。"},
        ),
        issues=(
            MeetingRepairIssue(
                code=QUANTITATIVE_PROMOTION_ISSUE,
                target=MeetingFieldTarget(field="highlights"),
                anchor_segment_ids=("seg_2",),
            ),
        ),
    )[0]


def _valid_result() -> dict:
    return {"items": [{"text": "匹配达到 100%。", "evidence": ["seg_2"]}]}


def test_builds_immutable_config_from_the_inactive_bundle() -> None:
    config = build_meeting_field_repair_shadow_config(_bundle())

    assert config.profile_version == "2026-08-29.2"
    assert config.bundle_sha256 == (
        "sha256:640495ce7db7aa8c624be3ad3b37f1bc82d003b8edfd7cd18cee364c8243e3c0"
    )
    assert config.maximum_calls == 3
    assert config.total_timeout_seconds == 180
    assert config.heartbeat_seconds == 10
    quantitative = config.repairs[QUANTITATIVE_PROMOTION_REPAIR]
    assert quantitative.model_role == "editor"
    assert quantitative.maximum_output_tokens == 1024
    assert "只修复请求中指定的一个会议字段" in quantitative.prompt
    with pytest.raises(TypeError):
        config.repairs["new_repair"] = quantitative


def test_profiled_runner_downstreams_prompt_budget_and_pinned_identity() -> None:
    baseline = _baseline()
    original = copy.deepcopy(baseline)
    requests = []

    def caller(request):
        requests.append(request)
        return _valid_result()

    result = run_profiled_meeting_field_repair_shadow(
        bundle=_bundle(),
        baseline=baseline,
        plans=(_plan(),),
        caller=caller,
        final_validator=lambda document: document,
    )

    assert result.document["highlights"] == _valid_result()["items"]
    assert baseline == original
    assert len(requests) == 1
    request = requests[0]
    assert "只修复请求中指定的一个会议字段" in request.prompt
    assert request.model_role == "editor"
    assert request.maximum_output_tokens == 1024
    assert request.timeout_seconds <= 120
    assert request.profile_version == "2026-08-29.2"
    assert request.bundle_sha256 == (
        "sha256:640495ce7db7aa8c624be3ad3b37f1bc82d003b8edfd7cd18cee364c8243e3c0"
    )
    assert not hasattr(request, "baseline")
    assert not hasattr(request, "document")


def test_last_known_good_bundle_without_field_repairs_is_rejected() -> None:
    with pytest.raises(MeetingFieldRepairProfileError, match="does not declare"):
        build_meeting_field_repair_shadow_config(
            load_profile_bundle(_PROFILE_ROOT / "2026-08-29.1")
        )


def test_active_bundle_exposes_the_validated_field_repair_contract() -> None:
    config = build_meeting_field_repair_shadow_config(load_bundled_meeting_profile())

    assert config.profile_version == "2026-08-29.2"
    assert config.maximum_calls == 3


def test_profile_call_budget_is_enforced_before_any_call() -> None:
    config = replace(build_meeting_field_repair_shadow_config(_bundle()), maximum_calls=0)
    calls = 0

    def caller(request):
        nonlocal calls
        calls += 1
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowError) as raised:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=caller,
            final_validator=lambda document: document,
            profile_config=config,
        )

    assert raised.value.code == "field_repair_profile_call_budget_exceeded"
    assert calls == 0


def test_profile_packet_limit_is_enforced_before_any_call() -> None:
    config = build_meeting_field_repair_shadow_config(_bundle())
    policy = config.repairs[QUANTITATIVE_PROMOTION_REPAIR]
    repairs = dict(config.repairs)
    repairs[QUANTITATIVE_PROMOTION_REPAIR] = replace(
        policy,
        maximum_evidence_segments=1,
    )
    config = replace(config, repairs=MappingProxyType(repairs))
    calls = 0

    def caller(request):
        nonlocal calls
        calls += 1
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowError) as raised:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=caller,
            final_validator=lambda document: document,
            profile_config=config,
        )

    assert raised.value.code == "field_repair_packet_exceeds_profile_limit"
    assert calls == 0


def test_profile_can_disable_parser_retry_and_lower_field_limit() -> None:
    config = build_meeting_field_repair_shadow_config(_bundle())
    policy = config.repairs[QUANTITATIVE_PROMOTION_REPAIR]
    repairs = dict(config.repairs)
    repairs[QUANTITATIVE_PROMOTION_REPAIR] = replace(
        policy,
        maximum_parser_retries=0,
        maximum_field_characters=4,
    )
    config = replace(config, repairs=MappingProxyType(repairs))
    calls = 0

    def invalid_json(request):
        nonlocal calls
        calls += 1
        return "not-json"

    with pytest.raises(MeetingFieldRepairShadowError) as parser:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=invalid_json,
            final_validator=lambda document: document,
            profile_config=config,
        )
    assert parser.value.code == "field_repair_json_unparseable"
    assert calls == 1

    requests = []

    def oversized_field(request):
        requests.append(request)
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowError) as field:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=oversized_field,
            final_validator=lambda document: document,
            profile_config=config,
        )
    assert field.value.code == "field_repair_result_exceeds_profile_limit"
    item_schema = requests[0].result_schema["properties"]["items"]["items"]
    assert item_schema["properties"]["text"]["maxLength"] == 4


def test_forged_profile_cannot_exceed_worker_hard_limit() -> None:
    config = build_meeting_field_repair_shadow_config(_bundle())
    policy = config.repairs[QUANTITATIVE_PROMOTION_REPAIR]
    repairs = dict(config.repairs)
    repairs[QUANTITATIVE_PROMOTION_REPAIR] = replace(
        policy,
        maximum_output_tokens=99_999,
    )
    config = replace(config, repairs=MappingProxyType(repairs))

    with pytest.raises(MeetingFieldRepairShadowError) as raised:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=lambda request: _valid_result(),
            final_validator=lambda document: document,
            profile_config=config,
        )

    assert raised.value.code == "field_repair_profile_limit_invalid"


def test_forged_profile_cannot_register_an_unknown_repair() -> None:
    config = build_meeting_field_repair_shadow_config(_bundle())
    repairs = dict(config.repairs)
    repairs["arbitrary_python_repair"] = repairs[QUANTITATIVE_PROMOTION_REPAIR]
    config = replace(config, repairs=MappingProxyType(repairs))

    with pytest.raises(MeetingFieldRepairShadowError) as raised:
        run_meeting_field_repair_shadow(
            baseline=_baseline(),
            plans=(_plan(),),
            caller=lambda request: _valid_result(),
            final_validator=lambda document: document,
            profile_config=config,
        )

    assert raised.value.code == "field_repair_profile_registration_invalid"


def test_profile_adapter_imports_no_formal_state_or_publication_components() -> None:
    tree = ast.parse(Path(profile_module.__file__).read_text(encoding="utf-8"))
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
            "speech_capture_worker.job_store",
            "speech_capture_worker.checkpoints",
            "speech_capture_worker.summary_revisions",
            "speech_capture_worker.artifact_generation",
            "speech_capture_worker.publication",
            "speech_capture_worker.api",
        }
    )
