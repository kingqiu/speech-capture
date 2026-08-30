"""Read-only adapter from a validated meeting ProfileBundle to the shadow runner.

This module projects inert prompt text and already validated, lower-or-equal budgets
into an immutable shadow configuration.  It has no model transport, job store,
checkpoint, candidate, revision, publication, API, or Vault dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from speech_capture_worker.content_profiles import ProfileBundle
from speech_capture_worker.meeting_field_repair_shadow import (
    CancellationCheck,
    FieldRepairCaller,
    FinalMeetingValidator,
    MeetingFieldRepairCallPolicy,
    MeetingFieldRepairShadowConfig,
    MeetingFieldRepairShadowResult,
    ProgressCallback,
    run_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repairs import MeetingFieldRepairPlan

MAX_FIELD_REPAIR_PROMPT_CHARACTERS = 8_000


class MeetingFieldRepairProfileError(ValueError):
    """Raised when a validated Bundle cannot configure the isolated shadow."""


def build_meeting_field_repair_shadow_config(
    bundle: ProfileBundle,
) -> MeetingFieldRepairShadowConfig:
    """Project one validated meeting Bundle into an immutable shadow config."""

    if bundle.content_type != "meeting":
        raise MeetingFieldRepairProfileError("Field repair requires a meeting ProfileBundle.")
    raw_field_repairs = bundle.execution_policy.get("field_repairs")
    if not isinstance(raw_field_repairs, Mapping):
        raise MeetingFieldRepairProfileError(
            "The meeting ProfileBundle does not declare field_repairs."
        )
    raw_repairs = raw_field_repairs.get("repairs")
    if not isinstance(raw_repairs, Mapping) or not raw_repairs:
        raise MeetingFieldRepairProfileError(
            "The meeting ProfileBundle does not enable a field repair."
        )

    repairs: dict[str, MeetingFieldRepairCallPolicy] = {}
    for repair_key, raw_policy in raw_repairs.items():
        if not isinstance(repair_key, str) or not isinstance(raw_policy, Mapping):
            raise MeetingFieldRepairProfileError("The field repair policy is malformed.")
        prompt = bundle.read_prompt("named_repairs", repair_name=repair_key)
        if prompt is None:
            raise MeetingFieldRepairProfileError(
                f"The field repair prompt {repair_key!r} is unavailable."
            )
        prompt = prompt.strip()
        if (
            not prompt
            or len(prompt) > MAX_FIELD_REPAIR_PROMPT_CHARACTERS
            or "\x00" in prompt
        ):
            raise MeetingFieldRepairProfileError(
                f"The field repair prompt {repair_key!r} is empty or unsafe."
            )
        repairs[repair_key] = MeetingFieldRepairCallPolicy(
            prompt=prompt,
            model_role=raw_policy["model_role"],
            maximum_output_tokens=raw_policy["maximum_output_tokens"],
            maximum_field_characters=raw_policy["maximum_field_characters"],
            maximum_evidence_segments=raw_policy["maximum_evidence_segments"],
            maximum_evidence_characters=raw_policy["maximum_evidence_characters"],
            maximum_evidence_tokens=raw_policy["maximum_evidence_tokens"],
            call_timeout_seconds=raw_policy["call_timeout_seconds"],
            maximum_parser_retries=raw_policy["maximum_parser_retries"],
        )

    return MeetingFieldRepairShadowConfig(
        profile_id=bundle.profile_id,
        profile_version=bundle.profile_version,
        bundle_sha256=bundle.bundle_sha256,
        maximum_calls=raw_field_repairs["maximum_calls"],
        total_timeout_seconds=raw_field_repairs["total_timeout_seconds"],
        heartbeat_seconds=raw_field_repairs["heartbeat_seconds"],
        repairs=MappingProxyType(repairs),
    )


def run_profiled_meeting_field_repair_shadow(
    *,
    bundle: ProfileBundle,
    baseline: Mapping[str, Any],
    plans: Sequence[MeetingFieldRepairPlan],
    caller: FieldRepairCaller,
    final_validator: FinalMeetingValidator,
    progress: ProgressCallback | None = None,
    cancelled: CancellationCheck | None = None,
) -> MeetingFieldRepairShadowResult:
    """Run the isolated shadow using only the validated Bundle projection."""

    config = build_meeting_field_repair_shadow_config(bundle)
    return run_meeting_field_repair_shadow(
        baseline=baseline,
        plans=plans,
        caller=caller,
        final_validator=final_validator,
        progress=progress,
        total_timeout_seconds=config.total_timeout_seconds,
        heartbeat_seconds=config.heartbeat_seconds,
        profile_config=config,
        cancelled=cancelled,
    )
