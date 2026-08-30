"""Side-effect-free synthetic runner for bounded meeting field repairs.

The runner accepts an injected short-call adapter and final validator.  It has no
job store, checkpoint, revision, artifact, publication, API, or Vault dependency.
It is not connected to the current structuring runtime.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from speech_capture_worker.meeting_field_repairs import (
    MAX_FIELD_CALL_SECONDS,
    MAX_HEARTBEAT_SECONDS,
    MAX_PACKET_CHARACTERS,
    MAX_PACKET_ESTIMATED_TOKENS,
    MAX_PACKET_SEGMENTS,
    MAX_PARSER_RETRIES_PER_REPAIR,
    MAX_REPAIR_CALLS,
    MAX_REPAIR_FIELD_CHARACTERS,
    MAX_REPAIR_OUTPUT_TOKENS,
    MAX_TOTAL_REPAIR_SECONDS,
    MEETING_FIELD_REPAIR_KEYS,
    MeetingFieldRepairPlan,
    meeting_repair_result_json_schema,
    validate_and_merge_meeting_field_repairs,
    validate_meeting_field_repair_result,
)


class MeetingFieldRepairShadowError(ValueError):
    """Raised when an isolated short-call shadow must stop safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MeetingFieldRepairCallRequest:
    """One bounded request for an injected adapter; contains no full document."""

    plan: MeetingFieldRepairPlan
    result_schema: Mapping[str, Any]
    attempt: int
    timeout_seconds: float
    prompt: str | None = None
    model_role: str | None = None
    maximum_output_tokens: int | None = None
    profile_id: str | None = None
    profile_version: str | None = None
    bundle_sha256: str | None = None


@dataclass(frozen=True)
class MeetingFieldRepairCallPolicy:
    """One already validated, read-only Profile policy for a registered repair."""

    prompt: str
    model_role: str
    maximum_output_tokens: int
    maximum_field_characters: int
    maximum_evidence_segments: int
    maximum_evidence_characters: int
    maximum_evidence_tokens: int
    call_timeout_seconds: float
    maximum_parser_retries: int


@dataclass(frozen=True)
class MeetingFieldRepairShadowConfig:
    """A pinned Profile projection containing no runtime or publication authority."""

    profile_id: str
    profile_version: str
    bundle_sha256: str
    maximum_calls: int
    total_timeout_seconds: float
    heartbeat_seconds: float
    repairs: Mapping[str, MeetingFieldRepairCallPolicy]


@dataclass(frozen=True)
class MeetingFieldRepairProgress:
    """Content-free progress suitable for a future Worker heartbeat adapter."""

    substage: str
    completed_units: int
    total_units: int
    repair_key: str | None
    target_field: str | None
    attempt: int | None
    call_count: int
    parser_retry_count: int
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "substage": self.substage,
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "repair_key": self.repair_key,
            "target_field": self.target_field,
            "attempt": self.attempt,
            "call_count": self.call_count,
            "parser_retry_count": self.parser_retry_count,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class MeetingFieldRepairShadowResult:
    document: Mapping[str, Any]
    call_count: int
    parser_retry_count: int
    elapsed_seconds: float


FieldRepairCaller = Callable[
    [MeetingFieldRepairCallRequest],
    str | Mapping[str, Any],
]
FinalMeetingValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ProgressCallback = Callable[[MeetingFieldRepairProgress], None]
CancellationCheck = Callable[[], bool]


def run_meeting_field_repair_shadow(
    *,
    baseline: Mapping[str, Any],
    plans: Sequence[MeetingFieldRepairPlan],
    caller: FieldRepairCaller,
    final_validator: FinalMeetingValidator,
    progress: ProgressCallback | None = None,
    call_timeout_seconds: float = MAX_FIELD_CALL_SECONDS,
    total_timeout_seconds: float = MAX_TOTAL_REPAIR_SECONDS,
    heartbeat_seconds: float = MAX_HEARTBEAT_SECONDS,
    profile_config: MeetingFieldRepairShadowConfig | None = None,
    cancelled: CancellationCheck | None = None,
) -> MeetingFieldRepairShadowResult:
    """Run bounded calls and return only an in-memory, fully validated document."""

    _validate_runner_limits(
        plans=plans,
        call_timeout_seconds=call_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
        heartbeat_seconds=heartbeat_seconds,
        profile_config=profile_config,
    )
    if profile_config is not None:
        total_timeout_seconds = min(total_timeout_seconds, profile_config.total_timeout_seconds)
        heartbeat_seconds = min(heartbeat_seconds, profile_config.heartbeat_seconds)
    call_budget = min(
        MAX_REPAIR_CALLS,
        profile_config.maximum_calls if profile_config is not None else MAX_REPAIR_CALLS,
    )
    _raise_if_cancelled(cancelled)
    started = time.monotonic()
    call_count = 0
    parser_retry_count = 0
    parsed_results: list[dict[str, Any]] = []
    _emit_progress(
        progress,
        substage="repair_planning",
        completed_units=0,
        total_units=len(plans),
        call_count=0,
        parser_retry_count=0,
        started=started,
    )

    for plan_index, plan in enumerate(plans):
        call_policy = (
            profile_config.repairs[plan.repair_key] if profile_config is not None else None
        )
        parser_retry_limit = min(
            MAX_PARSER_RETRIES_PER_REPAIR,
            call_policy.maximum_parser_retries
            if call_policy is not None
            else MAX_PARSER_RETRIES_PER_REPAIR,
        )
        parsed: dict[str, Any] | None = None
        for attempt in range(1, parser_retry_limit + 2):
            _raise_if_cancelled(cancelled)
            if call_count >= call_budget:
                raise MeetingFieldRepairShadowError(
                    "field_repair_call_budget_exceeded",
                    "The parser retry would exceed the total field-call budget.",
                )
            remaining = total_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise MeetingFieldRepairShadowError(
                    "field_repair_total_timeout",
                    "The bounded meeting repair exceeded its total time budget.",
                )
            effective_timeout = min(
                call_timeout_seconds,
                remaining,
                call_policy.call_timeout_seconds
                if call_policy is not None
                else MAX_FIELD_CALL_SECONDS,
            )
            request = MeetingFieldRepairCallRequest(
                plan=plan,
                result_schema=_profiled_result_schema(
                    plan,
                    maximum_field_characters=(
                        call_policy.maximum_field_characters
                        if call_policy is not None
                        else MAX_REPAIR_FIELD_CHARACTERS
                    ),
                ),
                attempt=attempt,
                timeout_seconds=effective_timeout,
                prompt=call_policy.prompt if call_policy is not None else None,
                model_role=call_policy.model_role if call_policy is not None else None,
                maximum_output_tokens=(
                    call_policy.maximum_output_tokens if call_policy is not None else None
                ),
                profile_id=profile_config.profile_id if profile_config is not None else None,
                profile_version=(
                    profile_config.profile_version if profile_config is not None else None
                ),
                bundle_sha256=(
                    profile_config.bundle_sha256 if profile_config is not None else None
                ),
            )
            call_count += 1
            call_started = time.monotonic()
            _emit_progress(
                progress,
                substage="field_repair",
                completed_units=plan_index,
                total_units=len(plans),
                repair_key=plan.repair_key,
                target_field=plan.target.field,
                attempt=attempt,
                call_count=call_count,
                parser_retry_count=parser_retry_count,
                started=started,
            )
            try:
                raw_result = _call_with_heartbeat(
                    lambda: caller(request),
                    progress=progress,
                    progress_event=lambda: MeetingFieldRepairProgress(
                        substage="field_repair",
                        completed_units=plan_index,
                        total_units=len(plans),
                        repair_key=plan.repair_key,
                        target_field=plan.target.field,
                        attempt=attempt,
                        call_count=call_count,
                        parser_retry_count=parser_retry_count,
                        elapsed_seconds=time.monotonic() - started,
                    ),
                    heartbeat_seconds=heartbeat_seconds,
                )
            except TimeoutError as error:
                raise MeetingFieldRepairShadowError(
                    "field_repair_call_timeout",
                    "The bounded meeting field call reached its transport timeout.",
                ) from error
            _raise_if_cancelled(cancelled)
            if time.monotonic() - call_started > effective_timeout:
                raise MeetingFieldRepairShadowError(
                    "field_repair_call_timeout",
                    "The bounded meeting field call exceeded its hard time budget.",
                )
            try:
                parsed = _parse_result_object(raw_result)
            except MeetingFieldRepairShadowError as error:
                if error.code != "field_repair_json_unparseable":
                    raise
                if attempt > parser_retry_limit:
                    raise
                parser_retry_count += 1
                continue
            # Semantic/schema/evidence failures never retry the model.
            parsed = validate_meeting_field_repair_result(plan, parsed)
            if call_policy is not None:
                _validate_profile_result_limit(
                    plan=plan,
                    result=parsed,
                    maximum_field_characters=call_policy.maximum_field_characters,
                )
            break
        if parsed is None:
            raise MeetingFieldRepairShadowError(
                "field_repair_json_unparseable",
                "The bounded meeting field call returned no usable object.",
            )
        parsed_results.append(parsed)
        _emit_progress(
            progress,
            substage="field_repair",
            completed_units=plan_index + 1,
            total_units=len(plans),
            repair_key=plan.repair_key,
            target_field=plan.target.field,
            attempt=None,
            call_count=call_count,
            parser_retry_count=parser_retry_count,
            started=started,
        )

    _raise_if_cancelled(cancelled)
    _emit_progress(
        progress,
        substage="final_validation",
        completed_units=len(plans),
        total_units=len(plans),
        call_count=call_count,
        parser_retry_count=parser_retry_count,
        started=started,
    )
    document = _call_with_heartbeat(
        lambda: validate_and_merge_meeting_field_repairs(
            baseline=baseline,
            plans=plans,
            results=parsed_results,
            final_validator=final_validator,
        ),
        progress=progress,
        progress_event=lambda: MeetingFieldRepairProgress(
            substage="final_validation",
            completed_units=len(plans),
            total_units=len(plans),
            repair_key=None,
            target_field=None,
            attempt=None,
            call_count=call_count,
            parser_retry_count=parser_retry_count,
            elapsed_seconds=time.monotonic() - started,
        ),
        heartbeat_seconds=heartbeat_seconds,
    )
    _raise_if_cancelled(cancelled)
    if not isinstance(document, Mapping):
        raise MeetingFieldRepairShadowError(
            "final_meeting_validator_returned_invalid_document",
            "The final meeting validation did not return a document object.",
        )
    elapsed = time.monotonic() - started
    if elapsed > total_timeout_seconds:
        raise MeetingFieldRepairShadowError(
            "field_repair_total_timeout",
            "The bounded meeting repair exceeded its total time budget.",
        )
    return MeetingFieldRepairShadowResult(
        document=copy.deepcopy(document),
        call_count=call_count,
        parser_retry_count=parser_retry_count,
        elapsed_seconds=elapsed,
    )


def _validate_runner_limits(
    *,
    plans: Sequence[MeetingFieldRepairPlan],
    call_timeout_seconds: float,
    total_timeout_seconds: float,
    heartbeat_seconds: float,
    profile_config: MeetingFieldRepairShadowConfig | None,
) -> None:
    if len(plans) > MAX_REPAIR_CALLS:
        raise MeetingFieldRepairShadowError(
            "field_repair_call_budget_exceeded",
            f"A bounded meeting shadow accepts at most {MAX_REPAIR_CALLS} plans.",
        )
    if not 0 < call_timeout_seconds <= MAX_FIELD_CALL_SECONDS:
        raise MeetingFieldRepairShadowError(
            "field_repair_call_timeout_out_of_bounds",
            f"A field call timeout must not exceed {MAX_FIELD_CALL_SECONDS:g} seconds.",
        )
    if not 0 < total_timeout_seconds <= MAX_TOTAL_REPAIR_SECONDS:
        raise MeetingFieldRepairShadowError(
            "field_repair_total_timeout_out_of_bounds",
            f"The total repair timeout must not exceed {MAX_TOTAL_REPAIR_SECONDS:g} seconds.",
        )
    if not 0 < heartbeat_seconds <= MAX_HEARTBEAT_SECONDS:
        raise MeetingFieldRepairShadowError(
            "field_repair_heartbeat_out_of_bounds",
            f"The heartbeat interval must not exceed {MAX_HEARTBEAT_SECONDS:g} seconds.",
        )
    if profile_config is not None:
        _validate_profile_config(profile_config, plans=plans)


def _validate_profile_config(
    profile_config: MeetingFieldRepairShadowConfig,
    *,
    plans: Sequence[MeetingFieldRepairPlan],
) -> None:
    if not profile_config.profile_id or not profile_config.profile_version:
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_identity_invalid",
            "A profiled meeting shadow requires a pinned Profile identity.",
        )
    if not _is_sha256(profile_config.bundle_sha256):
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_identity_invalid",
            "A profiled meeting shadow requires a canonical bundle hash.",
        )
    _bounded_profile_integer(
        profile_config.maximum_calls,
        label="maximum_calls",
        minimum=0,
        maximum=MAX_REPAIR_CALLS,
    )
    _bounded_profile_number(
        profile_config.total_timeout_seconds,
        label="total_timeout_seconds",
        maximum=MAX_TOTAL_REPAIR_SECONDS,
    )
    _bounded_profile_number(
        profile_config.heartbeat_seconds,
        label="heartbeat_seconds",
        maximum=MAX_HEARTBEAT_SECONDS,
    )
    if len(plans) > profile_config.maximum_calls:
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_call_budget_exceeded",
            "The planned repairs exceed the pinned Profile call budget.",
        )
    unknown_repairs = set(profile_config.repairs) - MEETING_FIELD_REPAIR_KEYS
    if unknown_repairs or len(profile_config.repairs) > MAX_REPAIR_CALLS:
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_registration_invalid",
            "The pinned Profile contains an unregistered field repair.",
        )
    for policy in profile_config.repairs.values():
        _validate_profile_call_policy(policy)
    for plan in plans:
        policy = profile_config.repairs.get(plan.repair_key)
        if policy is None:
            raise MeetingFieldRepairShadowError(
                "field_repair_not_enabled_by_profile",
                "The pinned Profile does not enable the planned repair.",
            )
        packet = plan.evidence_packet
        if (
            len(packet.segments) > policy.maximum_evidence_segments
            or packet.character_count > policy.maximum_evidence_characters
            or packet.estimated_tokens > policy.maximum_evidence_tokens
        ):
            raise MeetingFieldRepairShadowError(
                "field_repair_packet_exceeds_profile_limit",
                "The evidence packet exceeds the pinned Profile budget.",
            )


def _validate_profile_call_policy(policy: MeetingFieldRepairCallPolicy) -> None:
    if (
        not isinstance(policy.prompt, str)
        or not policy.prompt.strip()
        or len(policy.prompt) > 8_000
        or "\x00" in policy.prompt
    ):
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_prompt_invalid",
            "The pinned field repair prompt is empty or unsafe.",
        )
    if policy.model_role != "editor":
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_model_role_invalid",
            "The pinned field repair role must be editor.",
        )
    for value, label, maximum in (
        (policy.maximum_output_tokens, "maximum_output_tokens", MAX_REPAIR_OUTPUT_TOKENS),
        (
            policy.maximum_field_characters,
            "maximum_field_characters",
            MAX_REPAIR_FIELD_CHARACTERS,
        ),
        (
            policy.maximum_evidence_segments,
            "maximum_evidence_segments",
            MAX_PACKET_SEGMENTS,
        ),
        (
            policy.maximum_evidence_characters,
            "maximum_evidence_characters",
            MAX_PACKET_CHARACTERS,
        ),
        (
            policy.maximum_evidence_tokens,
            "maximum_evidence_tokens",
            MAX_PACKET_ESTIMATED_TOKENS,
        ),
    ):
        _bounded_profile_integer(value, label=label, minimum=1, maximum=maximum)
    _bounded_profile_number(
        policy.call_timeout_seconds,
        label="call_timeout_seconds",
        maximum=MAX_FIELD_CALL_SECONDS,
    )
    _bounded_profile_integer(
        policy.maximum_parser_retries,
        label="maximum_parser_retries",
        minimum=0,
        maximum=MAX_PARSER_RETRIES_PER_REPAIR,
    )


def _validate_profile_result_limit(
    *,
    plan: MeetingFieldRepairPlan,
    result: Mapping[str, Any],
    maximum_field_characters: int,
) -> None:
    if plan.repair_key == "meeting_speaker_grounding":
        values = [result["summary"]]
    else:
        values = [item.get("text", item.get("task", "")) for item in result["items"]]
    if any(len(value) > maximum_field_characters for value in values) or sum(
        len(value) for value in values
    ) > maximum_field_characters:
        raise MeetingFieldRepairShadowError(
            "field_repair_result_exceeds_profile_limit",
            "The field repair result exceeds the pinned Profile field limit.",
        )


def _profiled_result_schema(
    plan: MeetingFieldRepairPlan,
    *,
    maximum_field_characters: int,
) -> dict[str, Any]:
    schema = meeting_repair_result_json_schema(plan)
    if plan.repair_key == "meeting_speaker_grounding":
        schema["properties"]["summary"]["maxLength"] = maximum_field_characters
        return schema
    item_properties = schema["properties"]["items"]["items"]["properties"]
    field_name = "task" if "task" in item_properties else "text"
    item_properties[field_name]["maxLength"] = min(
        item_properties[field_name]["maxLength"],
        maximum_field_characters,
    )
    return schema


def _bounded_profile_integer(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_limit_invalid",
            f"The pinned Profile {label} exceeds the Worker hard limit.",
        )


def _bounded_profile_number(value: Any, *, label: str, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
        raise MeetingFieldRepairShadowError(
            "field_repair_profile_limit_invalid",
            f"The pinned Profile {label} exceeds the Worker hard limit.",
        )


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _raise_if_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise MeetingFieldRepairShadowError(
            "field_repair_cancelled",
            "The bounded meeting field repair was cancelled.",
        )


def _parse_result_object(value: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if not isinstance(value, str):
        raise MeetingFieldRepairShadowError(
            "field_repair_json_unparseable",
            "The bounded meeting field call did not return a JSON object.",
        )
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise MeetingFieldRepairShadowError(
            "field_repair_json_unparseable",
            "The bounded meeting field call returned unparsable JSON.",
        ) from error
    if not isinstance(payload, dict):
        raise MeetingFieldRepairShadowError(
            "field_repair_json_unparseable",
            "The bounded meeting field call did not return a JSON object.",
        )
    return payload


def _call_with_heartbeat(
    operation: Callable[[], str | Mapping[str, Any]],
    *,
    progress: ProgressCallback | None,
    progress_event: Callable[[], MeetingFieldRepairProgress],
    heartbeat_seconds: float,
) -> str | Mapping[str, Any]:
    stop = threading.Event()

    def pulse() -> None:
        while not stop.wait(heartbeat_seconds):
            if progress is None:
                continue
            try:
                progress(progress_event())
            except Exception:
                return

    thread = threading.Thread(
        target=pulse,
        name="speech-capture-meeting-field-repair-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        return operation()
    finally:
        stop.set()
        thread.join(timeout=min(1.0, heartbeat_seconds))


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    substage: str,
    completed_units: int,
    total_units: int,
    call_count: int,
    parser_retry_count: int,
    started: float,
    repair_key: str | None = None,
    target_field: str | None = None,
    attempt: int | None = None,
) -> None:
    if callback is None:
        return
    event = MeetingFieldRepairProgress(
        substage=substage,
        completed_units=completed_units,
        total_units=total_units,
        repair_key=repair_key,
        target_field=target_field,
        attempt=attempt,
        call_count=call_count,
        parser_retry_count=parser_retry_count,
        elapsed_seconds=time.monotonic() - started,
    )
    try:
        callback(event)
    except Exception:
        return
