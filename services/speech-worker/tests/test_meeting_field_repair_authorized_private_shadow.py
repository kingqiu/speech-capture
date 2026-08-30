"""Safety tests for the one-shot authorized private meeting shadow capability."""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

import speech_capture_worker.meeting_field_repair_authorized_private_shadow as private_module
from speech_capture_worker.content_profiles import load_profile_bundle
from speech_capture_worker.meeting_field_repair_authorized_private_shadow import (
    AuthorizedPrivateMeetingShadowCapability,
    build_authorized_private_meeting_shadow_capability,
    run_authorized_private_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repair_local_transport import (
    LocalOllamaMeetingFieldRepairTransport,
)
from speech_capture_worker.meeting_field_repair_shadow_bridge import (
    MeetingFieldRepairShadowBridgeError,
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


def _bundle():
    return load_profile_bundle(_PROFILE_PARENT / "2026-08-29.2")


def _segments() -> list[dict]:
    return [
        {
            "segment_id": "seg_private_a",
            "speaker_id": "speaker_private_owner",
            "text": "团队先核对内部会议范围。",
            "start_ms": 0,
        },
        {
            "segment_id": "seg_private_b",
            "speaker_id": "speaker_private_owner",
            "text": "内部验收匹配率必须达到 100%。",
            "start_ms": 1_000,
        },
    ]


def _baseline() -> dict:
    return {
        "title": "内部验收会议",
        "objective": {"text": "核对内部范围。", "evidence": ["seg_private_a"]},
        "summary": {"text": "团队核对内部规则。", "evidence": ["seg_private_a"]},
        "context": [
            {
                "kind": "purpose",
                "title": "会议目的",
                "text": "核对内部范围。",
                "evidence": ["seg_private_a"],
            },
            {
                "kind": "constraint",
                "title": "验收标准",
                "text": "内部验收采用明确标准。",
                "evidence": ["seg_private_b"],
            },
        ],
        "highlights": [],
        "topics": [],
        "timeline_sections": [
            {
                "title": "内部规则",
                "summary": "内部验收匹配率必须达到 100%。",
                "details": [],
                "start_segment_id": "seg_private_a",
                "end_segment_id": "seg_private_b",
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
    }


def _issues() -> tuple[MeetingRepairIssue, ...]:
    return (
        MeetingRepairIssue(
            code=QUANTITATIVE_PROMOTION_ISSUE,
            target=MeetingFieldTarget(field="highlights"),
            anchor_segment_ids=("seg_private_b",),
        ),
    )


def _transport() -> RecordingSyntheticFieldRepairTransport:
    return RecordingSyntheticFieldRepairTransport(
        lambda envelope: {
            "items": [
                {
                    "text": "内部验收匹配率必须达到 100%。",
                    "evidence": ["seg_private_b"],
                }
            ]
        }
    )


def _capability(*, baseline=None, segments=None):
    return build_authorized_private_meeting_shadow_capability(
        explicit_authorization=True,
        authorization_reference="owner-confirmation-turn-private-shadow",
        target_job_id="job_private_01",
        baseline=baseline or _baseline(),
        segments=segments or _segments(),
    )


def test_private_shadow_is_one_shot_memory_only_and_target_bound() -> None:
    baseline = _baseline()
    segments = _segments()
    original_baseline = copy.deepcopy(baseline)
    original_segments = copy.deepcopy(segments)
    capability = _capability(baseline=baseline, segments=segments)

    result = run_authorized_private_meeting_field_repair_shadow(
        capability=capability,
        target_job_id="job_private_01",
        bundle=_bundle(),
        baseline=baseline,
        segments=segments,
        issues=_issues(),
        transport=_transport(),
    )

    assert result.shadow.profile_version == "2026-08-29.2"
    assert result.shadow.call_count == 1
    assert result.changed_fields == ("highlights",)
    assert result.persistence_permitted is False
    assert result.transport_kind == "recording_synthetic"
    assert result.authorization_reference_sha256.startswith("sha256:")
    assert result.target_job_sha256.startswith("sha256:")
    assert result.evidence_snapshot_sha256.startswith("sha256:")
    assert baseline == original_baseline
    assert segments == original_segments

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as replayed:
        run_authorized_private_meeting_field_repair_shadow(
            capability=capability,
            target_job_id="job_private_01",
            bundle=_bundle(),
            baseline=baseline,
            segments=segments,
            issues=_issues(),
            transport=_transport(),
        )
    assert replayed.value.code == "private_shadow_authorization_replayed"


def test_private_shadow_accepts_verified_noop_without_calling_transport() -> None:
    baseline = _baseline()
    baseline["highlights"].append(
        {
            "text": "内部验收匹配率必须达到 100%。",
            "evidence": ["seg_private_b"],
        }
    )
    segments = _segments()
    transport = _transport()

    result = run_authorized_private_meeting_field_repair_shadow(
        capability=_capability(baseline=baseline, segments=segments),
        target_job_id="job_private_01",
        bundle=_bundle(),
        baseline=baseline,
        segments=segments,
        issues=(),
        transport=transport,
    )

    assert result.shadow.plan_count == 0
    assert result.shadow.call_count == 0
    assert result.shadow.baseline_sha256 == result.shadow.result_sha256
    assert result.changed_fields == ()
    assert result.persistence_permitted is False
    assert transport.finished_call_count == 0
    assert tuple(event.substage for event in result.progress_events) == (
        "repair_planning",
        "final_validation",
    )


def test_private_authorization_requires_explicit_consent_and_factory_token() -> None:
    with pytest.raises(MeetingFieldRepairShadowBridgeError) as missing:
        build_authorized_private_meeting_shadow_capability(
            explicit_authorization=False,
            authorization_reference="not-authorized",
            target_job_id="job_private_01",
            baseline=_baseline(),
            segments=_segments(),
        )
    assert missing.value.code == "private_shadow_not_authorized"

    with pytest.raises(MeetingFieldRepairShadowBridgeError) as untrusted:
        AuthorizedPrivateMeetingShadowCapability(
            token=object(),
            authorization_reference_sha256="sha256:" + "a" * 64,
            target_job_sha256="sha256:" + "b" * 64,
            baseline_sha256="sha256:" + "c" * 64,
            evidence_snapshot_sha256="sha256:" + "d" * 64,
        )
    assert untrusted.value.code == "private_shadow_authorization_untrusted"


def test_scope_mismatch_refuses_before_transport_and_does_not_consume_capability() -> None:
    baseline = _baseline()
    segments = _segments()
    capability = _capability(baseline=baseline, segments=segments)
    transport = _transport()

    changed = copy.deepcopy(baseline)
    changed["title"] = "已漂移的标题"
    with pytest.raises(MeetingFieldRepairShadowBridgeError) as mismatch:
        run_authorized_private_meeting_field_repair_shadow(
            capability=capability,
            target_job_id="job_private_01",
            bundle=_bundle(),
            baseline=changed,
            segments=segments,
            issues=_issues(),
            transport=transport,
        )
    assert mismatch.value.code == "private_shadow_authorization_scope_mismatch"
    assert transport.finished_call_count == 0

    result = run_authorized_private_meeting_field_repair_shadow(
        capability=capability,
        target_job_id="job_private_01",
        bundle=_bundle(),
        baseline=baseline,
        segments=segments,
        issues=_issues(),
        transport=transport,
    )
    assert result.shadow.call_count == 1


def test_reference_outside_authorized_evidence_refuses_before_claim() -> None:
    baseline = _baseline()
    segments = _segments()
    capability = _capability(baseline=baseline, segments=segments)
    issue = MeetingRepairIssue(
        code=QUANTITATIVE_PROMOTION_ISSUE,
        target=MeetingFieldTarget(field="highlights"),
        anchor_segment_ids=("seg_not_authorized",),
    )
    with pytest.raises(MeetingFieldRepairShadowBridgeError) as outside:
        run_authorized_private_meeting_field_repair_shadow(
            capability=capability,
            target_job_id="job_private_01",
            bundle=_bundle(),
            baseline=baseline,
            segments=segments,
            issues=(issue,),
            transport=_transport(),
        )
    assert outside.value.code == "private_shadow_issue_anchor_invalid"


def test_local_transport_must_share_cancellation_source() -> None:
    def first() -> bool:
        return False

    def second() -> bool:
        return False

    transport = LocalOllamaMeetingFieldRepairTransport(
        editor_model="qwen3:8b",
        cancelled=first,
    )
    with pytest.raises(MeetingFieldRepairShadowBridgeError) as unbound:
        run_authorized_private_meeting_field_repair_shadow(
            capability=_capability(),
            target_job_id="job_private_01",
            bundle=_bundle(),
            baseline=_baseline(),
            segments=_segments(),
            issues=_issues(),
            transport=transport,
            cancelled=second,
        )
    assert unbound.value.code == "private_shadow_cancellation_unbound"


def test_private_shadow_is_not_imported_by_production_runtime() -> None:
    source_root = Path(private_module.__file__).parent
    for path in source_root.glob("*.py"):
        if path == Path(private_module.__file__):
            continue
        assert "meeting_field_repair_authorized_private_shadow" not in path.read_text(
            encoding="utf-8"
        )


def test_private_shadow_has_no_state_filesystem_or_publication_dependency() -> None:
    tree = ast.parse(Path(private_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert imported.isdisjoint(
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
