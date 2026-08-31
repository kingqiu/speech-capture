"""Public synthetic tests for the explicit memory-only B3.2 shadow bridge."""

from __future__ import annotations

import ast
import copy
from dataclasses import replace
from pathlib import Path

import pytest

import speech_capture_worker.meeting_field_repair_shadow_bridge as bridge_module
from speech_capture_worker.content_profiles import load_profile_bundle
from speech_capture_worker.meeting_field_repair_shadow_bridge import (
    FIELD_REPAIR_SHADOW_BUNDLE_SHA256,
    MEMORY_ONLY_RESULT_MODE,
    PUBLIC_SYNTHETIC_CLASSIFICATION,
    MeetingFieldRepairShadowBridgeError,
    MeetingFieldRepairShadowOptIn,
    run_public_synthetic_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repairs import (
    QUANTITATIVE_PROMOTION_ISSUE,
    MeetingFieldTarget,
    MeetingRepairIssue,
    canonical_json_sha256,
)
from speech_capture_worker.structuring_execution import (
    build_trusted_meeting_invariant_validator,
)

_PROFILE_PARENT = (
    Path(__file__).parents[1]
    / "src"
    / "speech_capture_worker"
    / "profile_bundles"
    / "meeting"
)


def _bundle(version: str = "2026-08-29.2"):
    return load_profile_bundle(_PROFILE_PARENT / version)


def _opt_in() -> MeetingFieldRepairShadowOptIn:
    return MeetingFieldRepairShadowOptIn(
        enabled=True,
        content_type="meeting",
        data_classification=PUBLIC_SYNTHETIC_CLASSIFICATION,
        result_mode=MEMORY_ONLY_RESULT_MODE,
        allow_persistence=False,
    )


def _segments() -> list[dict]:
    return [
        {
            "segment_id": "seg_public_1",
            "speaker_id": "speaker_public_1",
            "text": "先核对公开合成范围。",
            "start_ms": 0,
        },
        {
            "segment_id": "seg_public_2",
            "speaker_id": "speaker_public_1",
            "text": "公开合成匹配率必须达到 100%。",
            "start_ms": 1_000,
        },
    ]


def _baseline(*, timeline_number: bool = False) -> dict:
    baseline = {
        "title": "公开合成规则会议",
        "objective": {"text": "核对公开合成范围。", "evidence": ["seg_public_1"]},
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
        "scene_sections": [],
        "discussion_threads": [],
        "decisions": [],
        "actions": [],
        "risks": [],
        "open_questions": [],
        "speaker_summaries": [],
        "chapters": [],
    }
    baseline["timeline_sections"] = [
        {
            "title": "公开合成规则",
            "summary": (
                "匹配率必须达到 100%。" if timeline_number else "团队核对公开合成规则。"
            ),
            "details": [],
            "start_segment_id": "seg_public_1",
            "end_segment_id": "seg_public_2",
        }
    ]
    return baseline


def _invariant_validator(segments=None):
    return build_trusted_meeting_invariant_validator(segments or _segments())


def _issues() -> tuple[MeetingRepairIssue, ...]:
    return (
        MeetingRepairIssue(
            code=QUANTITATIVE_PROMOTION_ISSUE,
            target=MeetingFieldTarget(field="highlights"),
            anchor_segment_ids=("seg_public_2",),
        ),
    )


def _valid_result() -> dict:
    return {
        "items": [
            {
                "text": "公开合成匹配率必须达到 100%。",
                "evidence": ["seg_public_2"],
            }
        ]
    }


def test_explicit_public_memory_shadow_returns_detached_audit_result() -> None:
    baseline = _baseline(timeline_number=True)
    segments = _segments()
    original_baseline = copy.deepcopy(baseline)
    original_segments = copy.deepcopy(segments)
    requests = []

    def caller(request):
        requests.append(request)
        return _valid_result()

    result = run_public_synthetic_meeting_field_repair_shadow(
        opt_in=_opt_in(),
        bundle=_bundle(),
        baseline=baseline,
        segments=segments,
        issues=_issues(),
        caller=caller,
        trusted_invariant_validator=_invariant_validator(segments),
    )

    assert len(requests) == 1
    assert result.profile_version == "2026-08-29.2"
    assert result.bundle_sha256 == FIELD_REPAIR_SHADOW_BUNDLE_SHA256
    assert result.plan_count == 1
    assert result.call_count == 1
    assert result.parser_retry_count == 0
    assert result.baseline_sha256 == canonical_json_sha256(original_baseline)
    assert result.result_sha256 == canonical_json_sha256(result.document)
    assert result.document["highlights"] == _valid_result()["items"]
    assert baseline == original_baseline
    assert segments == original_segments

    result.document["highlights"].append(
        {"text": "只修改返回副本。", "evidence": ["seg_public_1"]}
    )
    assert baseline == original_baseline


@pytest.mark.parametrize(
    ("opt_in", "expected_code"),
    [
        (replace(_opt_in(), enabled=False), "shadow_bridge_not_enabled"),
        (
            replace(_opt_in(), content_type="interview"),
            "shadow_bridge_content_type_invalid",
        ),
        (
            replace(_opt_in(), data_classification="private"),
            "shadow_bridge_data_classification_invalid",
        ),
        (
            replace(_opt_in(), result_mode="candidate"),
            "shadow_bridge_result_mode_invalid",
        ),
        (
            replace(_opt_in(), allow_persistence=True),
            "shadow_bridge_result_mode_invalid",
        ),
    ],
)
def test_opt_in_scope_refusals_happen_before_caller(
    opt_in: MeetingFieldRepairShadowOptIn,
    expected_code: str,
) -> None:
    called = False

    def caller(request):
        nonlocal called
        called = True
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_public_synthetic_meeting_field_repair_shadow(
            opt_in=opt_in,
            bundle=_bundle(),
            baseline=_baseline(),
            segments=_segments(),
            issues=_issues(),
            caller=caller,
            trusted_invariant_validator=_invariant_validator(),
        )

    assert raised.value.code == expected_code
    assert called is False


@pytest.mark.parametrize(
    "bundle",
    [
        pytest.param(None, id="not-a-bundle"),
        pytest.param("old", id="old-default-bundle"),
        pytest.param("forged", id="forged-hash"),
    ],
)
def test_only_exact_inactive_dot_two_bundle_is_accepted_before_caller(bundle) -> None:
    candidate = bundle
    if bundle == "old":
        candidate = _bundle("2026-08-29.1")
    elif bundle == "forged":
        candidate = replace(_bundle(), bundle_sha256="sha256:" + "0" * 64)
    called = False

    def caller(request):
        nonlocal called
        called = True
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=candidate,
            baseline=_baseline(),
            segments=_segments(),
            issues=_issues(),
            caller=caller,
            trusted_invariant_validator=_invariant_validator(),
        )

    assert raised.value.code in {"shadow_bridge_bundle_invalid", "shadow_bridge_bundle_not_pinned"}
    assert called is False


@pytest.mark.parametrize(
    ("baseline", "segments", "issues", "expected_code"),
    [
        (
            _baseline(),
            [
                {
                    "segment_id": "seg_private_1",
                    "speaker_id": "speaker_public_1",
                    "text": "不是公开合成 ID。",
                    "start_ms": 0,
                }
            ],
            _issues(),
            "shadow_bridge_segment_id_invalid",
        ),
        (
            _baseline(),
            [
                {
                    **_segments()[0],
                    "source_path": "/private/example.wav",
                }
            ],
            _issues(),
            "shadow_bridge_segment_fields_invalid",
        ),
        (
            {
                **_baseline(),
                "summary": {
                    "text": "引用包外证据。",
                    "evidence": ["seg_public_missing"],
                },
            },
            _segments(),
            _issues(),
            "shadow_bridge_baseline_reference_invalid",
        ),
        (
            _baseline(),
            _segments(),
            (),
            "shadow_bridge_repairs_missing",
        ),
    ],
)
def test_public_input_refusals_happen_before_caller(
    baseline,
    segments,
    issues,
    expected_code: str,
) -> None:
    called = False

    def caller(request):
        nonlocal called
        called = True
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=_bundle(),
            baseline=baseline,
            segments=segments,
            issues=issues,
            caller=caller,
            trusted_invariant_validator=_invariant_validator(),
        )

    assert raised.value.code == expected_code
    assert called is False


def test_arbitrary_invariant_callback_is_rejected_before_transport() -> None:
    called = False

    def caller(request):
        nonlocal called
        called = True
        return _valid_result()

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=_bundle(),
            baseline=_baseline(),
            segments=_segments(),
            issues=_issues(),
            caller=caller,
            trusted_invariant_validator=lambda document: document,
        )

    assert raised.value.code == "shadow_bridge_invariant_capability_untrusted"
    assert called is False


def test_noop_invariant_validator_cannot_bypass_bundle_semantic_gate() -> None:
    unpromoted = {
        "items": [
            {
                "text": "继续核对公开合成范围。",
                "evidence": ["seg_public_2"],
            }
        ]
    }

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=_bundle(),
            baseline=_baseline(timeline_number=True),
            segments=_segments(),
            issues=_issues(),
            caller=lambda request: unpromoted,
            trusted_invariant_validator=_invariant_validator(),
        )

    assert raised.value.code == "shadow_bridge_semantic_gate_failed"
    assert raised.value.issue_codes == ("important_quantitative_facts_not_promoted",)


def test_pre_cancelled_bridge_never_calls_transport_or_validator() -> None:
    calls: list[str] = []

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=_bundle(),
            baseline=_baseline(),
            segments=_segments(),
            issues=_issues(),
            caller=lambda request: calls.append("caller") or _valid_result(),
            trusted_invariant_validator=_invariant_validator(),
            cancelled=lambda: True,
        )

    assert raised.value.code == "shadow_bridge_cancelled"
    assert calls == []


def test_broken_cancellation_check_fails_closed_before_transport() -> None:
    calls: list[str] = []

    def broken_check() -> bool:
        raise RuntimeError("synthetic cancellation check failure")

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as raised:
        run_public_synthetic_meeting_field_repair_shadow(
            opt_in=_opt_in(),
            bundle=_bundle(),
            baseline=_baseline(),
            segments=_segments(),
            issues=_issues(),
            caller=lambda request: calls.append("caller") or _valid_result(),
            trusted_invariant_validator=_invariant_validator(),
            cancelled=broken_check,
        )

    assert raised.value.code == "shadow_bridge_cancellation_check_failed"
    assert calls == []


def test_shadow_bridge_is_not_imported_by_production_runtime_modules() -> None:
    source_root = Path(bridge_module.__file__).parent
    for path in source_root.glob("*.py"):
        if path.name in {
            Path(bridge_module.__file__).name,
            "meeting_field_repair_authorized_private_shadow.py",
            "meeting_field_repair_shadow_orchestrator.py",
        }:
            continue
        assert "meeting_field_repair_shadow_bridge" not in path.read_text(encoding="utf-8")


def test_shadow_bridge_has_no_transport_formal_state_or_filesystem_dependency() -> None:
    tree = ast.parse(Path(bridge_module.__file__).read_text(encoding="utf-8"))
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
            "pathlib",
            "http.client",
            "urllib",
            "requests",
            "speech_capture_worker.meeting_field_repair_local_transport",
            "speech_capture_worker.job_store",
            "speech_capture_worker.checkpoints",
            "speech_capture_worker.summary_revisions",
            "speech_capture_worker.artifact_generation",
            "speech_capture_worker.publication",
            "speech_capture_worker.api",
        }
    )
