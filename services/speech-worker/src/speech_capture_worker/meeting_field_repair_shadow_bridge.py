"""Explicit opt-in, memory-only bridge for public-synthetic meeting repair shadows.

This bridge is deliberately not connected to the Worker runtime.  It grants no
model, task, checkpoint, candidate, revision, publication, filesystem, or Vault
authority.  A caller must inject both a bounded field caller and the trusted Worker
document invariant validator.  The bridge pins the one inactive B3.2 bundle and
returns an in-memory result only.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from speech_capture_worker.content_profiles import ProfileBundle
from speech_capture_worker.meeting_field_repair_profile import (
    run_profiled_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repair_shadow import (
    CancellationCheck,
    FieldRepairCaller,
    ProgressCallback,
)
from speech_capture_worker.meeting_field_repairs import (
    MeetingRepairIssue,
    canonical_json_sha256,
    plan_meeting_field_repairs,
)
from speech_capture_worker.meeting_invariant_validator import (
    MeetingInvariantValidatorError,
    TrustedMeetingInvariantValidator,
)
from speech_capture_worker.meeting_semantic_gate import (
    MEETING_SEMANTIC_VALIDATORS,
    MeetingSemanticGateError,
    require_meeting_semantic_edit,
)

PUBLIC_SYNTHETIC_CLASSIFICATION = "public_synthetic"
MEMORY_ONLY_RESULT_MODE = "memory_only"
MEETING_CONTENT_TYPE = "meeting"
FIELD_REPAIR_SHADOW_PROFILE_ID = "speech-capture/meeting"
FIELD_REPAIR_SHADOW_PROFILE_VERSION = "2026-08-29.2"
FIELD_REPAIR_SHADOW_BUNDLE_SHA256 = (
    "sha256:640495ce7db7aa8c624be3ad3b37f1bc82d003b8edfd7cd18cee364c8243e3c0"
)
MAX_PUBLIC_SYNTHETIC_SEGMENTS = 32
MAX_PUBLIC_SYNTHETIC_CHARACTERS = 16_000

_PUBLIC_SEGMENT_ID = re.compile(r"seg_public_[A-Za-z0-9._:-]{1,128}\Z")
_PUBLIC_SPEAKER_ID = re.compile(r"speaker_public_[A-Za-z0-9._:-]{1,128}\Z")
_SEGMENT_FIELDS = frozenset({"segment_id", "speaker_id", "text", "start_ms"})


class MeetingFieldRepairShadowBridgeError(ValueError):
    """Raised when the public-synthetic bridge refuses or fails closed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        issue_codes: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.issue_codes = tuple(issue_codes)
        super().__init__(message)


@dataclass(frozen=True)
class MeetingFieldRepairShadowOptIn:
    """Explicit capability declaration for the current public-only bridge stage."""

    enabled: bool
    content_type: str
    data_classification: str
    result_mode: str
    allow_persistence: bool


@dataclass(frozen=True)
class MeetingFieldRepairShadowBridgeResult:
    """In-memory output and content-free audit facts; never a formal candidate."""

    document: Mapping[str, Any]
    profile_id: str
    profile_version: str
    bundle_sha256: str
    baseline_sha256: str
    result_sha256: str
    plan_count: int
    call_count: int
    parser_retry_count: int
    elapsed_seconds: float


def run_public_synthetic_meeting_field_repair_shadow(
    *,
    opt_in: MeetingFieldRepairShadowOptIn,
    bundle: ProfileBundle,
    baseline: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    issues: Sequence[MeetingRepairIssue],
    caller: FieldRepairCaller,
    trusted_invariant_validator: TrustedMeetingInvariantValidator,
    progress: ProgressCallback | None = None,
    cancelled: CancellationCheck | None = None,
) -> MeetingFieldRepairShadowBridgeResult:
    """Run one explicitly public, memory-only shadow without formal side effects."""

    _validate_opt_in(opt_in)
    _validate_pinned_bundle(bundle)
    if not callable(caller):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_callable_invalid",
            "The shadow bridge requires an injected bounded field caller.",
        )
    if not isinstance(trusted_invariant_validator, TrustedMeetingInvariantValidator):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_invariant_capability_untrusted",
            "The shadow bridge requires the sealed Worker meeting invariant capability.",
        )
    if not issues:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_repairs_missing",
            "The shadow bridge requires at least one explicit repair issue.",
        )
    _raise_if_cancelled(cancelled)
    baseline_copy = copy.deepcopy(dict(baseline))
    segment_copies = _validate_and_copy_public_segments(segments)
    if not trusted_invariant_validator.matches_segments(segment_copies):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_invariant_evidence_mismatch",
            "The trusted invariant capability is bound to different evidence.",
        )
    issue_copies = copy.deepcopy(tuple(issues))
    _validate_public_references(
        baseline=baseline_copy,
        segments=segment_copies,
        issues=issue_copies,
    )
    baseline_sha256 = canonical_json_sha256(baseline_copy)
    segments_sha256 = canonical_json_sha256(segment_copies)
    plans = plan_meeting_field_repairs(
        baseline=baseline_copy,
        segments=segment_copies,
        issues=issue_copies,
    )
    if not plans:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_repairs_missing",
            "The shadow bridge did not produce a bounded repair plan.",
        )

    semantic_validators = tuple(
        validator
        for validator in bundle.validation_policy["registered_validators"]
        if validator in MEETING_SEMANTIC_VALIDATORS
    )

    def final_validator(document: Mapping[str, Any]) -> Mapping[str, Any]:
        before_validator_sha256 = canonical_json_sha256(document)
        try:
            validated = trusted_invariant_validator(copy.deepcopy(dict(document)))
        except MeetingInvariantValidatorError as error:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_invariant_validation_failed",
                "The trusted Worker meeting invariant validator rejected the shadow document.",
                issue_codes=(error.code,),
            ) from error
        if not isinstance(validated, Mapping):
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_invariant_validator_invalid",
                "The trusted invariant validator did not return a document object.",
            )
        if canonical_json_sha256(validated) != before_validator_sha256:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_invariant_validator_mutated_document",
                "The trusted invariant validator modified the shadow document.",
            )
        try:
            return require_meeting_semantic_edit(
                baseline_copy,
                validated,
                segments=segment_copies,
                validators=semantic_validators,
            )
        except MeetingSemanticGateError as error:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_semantic_gate_failed",
                "The public-synthetic shadow failed the final meeting semantic gate.",
                issue_codes=error.result.issue_codes,
            ) from error

    result = run_profiled_meeting_field_repair_shadow(
        bundle=bundle,
        baseline=baseline_copy,
        plans=plans,
        caller=caller,
        final_validator=final_validator,
        progress=progress,
        cancelled=cancelled,
    )
    _raise_if_cancelled(cancelled)
    if canonical_json_sha256(baseline) != baseline_sha256:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_baseline_mutated",
            "The public-synthetic baseline changed during the shadow run.",
        )
    if canonical_json_sha256(segments) != segments_sha256:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_segments_mutated",
            "The public-synthetic evidence changed during the shadow run.",
        )
    document = copy.deepcopy(dict(result.document))
    return MeetingFieldRepairShadowBridgeResult(
        document=document,
        profile_id=bundle.profile_id,
        profile_version=bundle.profile_version,
        bundle_sha256=bundle.bundle_sha256,
        baseline_sha256=baseline_sha256,
        result_sha256=canonical_json_sha256(document),
        plan_count=len(plans),
        call_count=result.call_count,
        parser_retry_count=result.parser_retry_count,
        elapsed_seconds=result.elapsed_seconds,
    )


def _validate_opt_in(opt_in: MeetingFieldRepairShadowOptIn) -> None:
    if not isinstance(opt_in, MeetingFieldRepairShadowOptIn):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_opt_in_invalid",
            "The shadow bridge requires its explicit opt-in capability object.",
        )
    if opt_in.enabled is not True:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_not_enabled",
            "The public-synthetic shadow bridge was not explicitly enabled.",
        )
    if opt_in.content_type != MEETING_CONTENT_TYPE:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_content_type_invalid",
            "The current shadow bridge only accepts meeting content.",
        )
    if opt_in.data_classification != PUBLIC_SYNTHETIC_CLASSIFICATION:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_data_classification_invalid",
            "The current shadow bridge only accepts public-synthetic inputs.",
        )
    if opt_in.result_mode != MEMORY_ONLY_RESULT_MODE or opt_in.allow_persistence is not False:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_result_mode_invalid",
            "The current shadow bridge only permits non-persistent memory results.",
        )


def _validate_pinned_bundle(bundle: ProfileBundle) -> None:
    if not isinstance(bundle, ProfileBundle):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_bundle_invalid",
            "The shadow bridge requires a validated ProfileBundle.",
        )
    if (
        bundle.profile_id != FIELD_REPAIR_SHADOW_PROFILE_ID
        or bundle.profile_version != FIELD_REPAIR_SHADOW_PROFILE_VERSION
        or bundle.bundle_sha256 != FIELD_REPAIR_SHADOW_BUNDLE_SHA256
        or bundle.content_type != MEETING_CONTENT_TYPE
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_bundle_not_pinned",
            "The shadow bridge requires the exact inactive B3.2 meeting bundle.",
        )
    validators = frozenset(bundle.validation_policy["registered_validators"])
    if not MEETING_SEMANTIC_VALIDATORS.issubset(validators):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_semantic_policy_incomplete",
            "The pinned shadow bundle is missing a required meeting semantic validator.",
        )


def _validate_and_copy_public_segments(
    segments: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    if (
        isinstance(segments, (str, bytes))
        or not isinstance(segments, Sequence)
        or not 1 <= len(segments) <= MAX_PUBLIC_SYNTHETIC_SEGMENTS
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_segments_invalid",
            "The public-synthetic segment set is empty or exceeds its bridge limit.",
        )
    normalized: list[dict[str, Any]] = []
    character_count = 0
    for segment in segments:
        if not isinstance(segment, Mapping) or set(segment) != _SEGMENT_FIELDS:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_segment_fields_invalid",
                "Public-synthetic segments must contain only id, speaker, text, and start time.",
            )
        segment_id = segment.get("segment_id")
        speaker_id = segment.get("speaker_id")
        text = segment.get("text")
        start_ms = segment.get("start_ms")
        if not isinstance(segment_id, str) or _PUBLIC_SEGMENT_ID.fullmatch(segment_id) is None:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_segment_id_invalid",
                "The bridge requires an explicit public-synthetic segment id.",
            )
        if not isinstance(speaker_id, str) or _PUBLIC_SPEAKER_ID.fullmatch(speaker_id) is None:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_speaker_id_invalid",
                "The bridge requires an explicit public-synthetic speaker id.",
            )
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_segment_text_invalid",
                "A public-synthetic segment requires safe non-empty text.",
            )
        if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_segment_start_invalid",
                "A public-synthetic segment requires a non-negative integer start time.",
            )
        character_count += len(text)
        normalized.append(
            {
                "segment_id": segment_id,
                "speaker_id": speaker_id,
                "text": text,
                "start_ms": start_ms,
            }
        )
    if character_count > MAX_PUBLIC_SYNTHETIC_CHARACTERS:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_segments_too_large",
            "The public-synthetic segment text exceeds its bridge limit.",
        )
    if len({segment["segment_id"] for segment in normalized}) != len(normalized):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_segment_id_duplicate",
            "Public-synthetic segment ids must be unique.",
        )
    if len({segment["start_ms"] for segment in normalized}) != len(normalized):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_segment_start_duplicate",
            "Public-synthetic segment start times must be unique.",
        )
    return tuple(normalized)


def _validate_public_references(
    *,
    baseline: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    issues: Sequence[MeetingRepairIssue],
) -> None:
    segment_ids = {str(segment["segment_id"]) for segment in segments}
    speaker_ids = {str(segment["speaker_id"]) for segment in segments}
    for reference in _document_segment_references(baseline):
        if _PUBLIC_SEGMENT_ID.fullmatch(reference) is None or reference not in segment_ids:
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_baseline_reference_invalid",
                "The baseline references evidence outside the public-synthetic input.",
            )
    for issue in issues:
        if not isinstance(issue, MeetingRepairIssue):
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_issue_invalid",
                "The shadow bridge requires registered meeting repair issues.",
            )
        if not issue.anchor_segment_ids or any(
            _PUBLIC_SEGMENT_ID.fullmatch(segment_id) is None
            or segment_id not in segment_ids
            for segment_id in issue.anchor_segment_ids
        ):
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_issue_anchor_invalid",
                "A repair issue references evidence outside the public-synthetic input.",
            )
        if issue.speaker_id is not None and (
            _PUBLIC_SPEAKER_ID.fullmatch(issue.speaker_id) is None
            or issue.speaker_id not in speaker_ids
        ):
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_bridge_issue_speaker_invalid",
                "A repair issue references a speaker outside the public-synthetic input.",
            )
        if issue.target.item_key is not None and issue.target.field == "speaker_summaries":
            if (
                _PUBLIC_SPEAKER_ID.fullmatch(issue.target.item_key) is None
                or issue.target.item_key not in speaker_ids
            ):
                raise MeetingFieldRepairShadowBridgeError(
                    "shadow_bridge_target_speaker_invalid",
                    "A speaker repair target is outside the public-synthetic input.",
                )


def _document_segment_references(value: Any) -> tuple[str, ...]:
    references: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key == "evidence" and isinstance(child, list):
                    references.extend(value for value in child if isinstance(value, str))
                elif key in {"start_segment_id", "end_segment_id"} and isinstance(child, str):
                    references.append(child)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(references)


def _raise_if_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is None:
        return
    try:
        is_cancelled = cancelled()
    except Exception as error:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_cancellation_check_failed",
            "The public-synthetic shadow cancellation check failed closed.",
        ) from error
    if is_cancelled:
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_bridge_cancelled",
            "The public-synthetic shadow bridge was cancelled before completion.",
        )
