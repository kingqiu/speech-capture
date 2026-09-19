"""One-shot, read-only private meeting shadow evaluation capability.

This module is intentionally not imported by the production runtime.  It does not
look up a job, read a file, persist a candidate, create a revision, publish a Note,
or touch a Vault.  A caller must first mint a one-shot capability bound to the
exact target job identity, baseline hash, and immutable evidence snapshot after
the project owner grants authorization outside this module.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from speech_capture_worker.content_profiles import ProfileBundle
from speech_capture_worker.meeting_field_repair_local_transport import (
    LocalOllamaMeetingFieldRepairTransport,
)
from speech_capture_worker.meeting_field_repair_profile import (
    run_profiled_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repair_shadow import (
    CancellationCheck,
    MeetingFieldRepairProgress,
    ProgressCallback,
)
from speech_capture_worker.meeting_field_repair_shadow_bridge import (
    MeetingFieldRepairShadowBridgeError,
    MeetingFieldRepairShadowBridgeResult,
    _document_segment_references,
    _validate_pinned_bundle,
)
from speech_capture_worker.meeting_field_repair_transport_shadow import (
    RecordingSyntheticFieldRepairTransport,
)
from speech_capture_worker.meeting_field_repairs import (
    MAX_REPAIR_CALLS,
    MAX_TOTAL_REPAIR_SECONDS,
    MeetingRepairIssue,
    canonical_json_sha256,
    plan_meeting_field_repairs,
)
from speech_capture_worker.meeting_invariant_validator import (
    MeetingInvariantEvidenceSnapshot,
    MeetingInvariantValidatorError,
)
from speech_capture_worker.meeting_semantic_gate import (
    MEETING_SEMANTIC_VALIDATORS,
    MeetingSemanticGateError,
    require_meeting_semantic_edit,
)
from speech_capture_worker.structuring_execution import (
    build_trusted_meeting_invariant_validator,
)

PRIVATE_AUTHORIZED_CLASSIFICATION = "private_authorized"
MEMORY_ONLY_RESULT_MODE = "memory_only"
MAX_PRIVATE_SHADOW_SEGMENTS = 50_000
MAX_PRIVATE_SHADOW_CHARACTERS = 25_000_000
_SEGMENT_FIELDS = frozenset({"segment_id", "speaker_id", "text", "start_ms"})
_AUTHORIZATION_FACTORY_TOKEN = object()


class AuthorizedPrivateMeetingShadowCapability:
    """Single-use scope bound to one exact private baseline and evidence set."""

    __slots__ = (
        "authorization_reference_sha256",
        "target_job_sha256",
        "baseline_sha256",
        "evidence_snapshot_sha256",
        "maximum_calls",
        "total_timeout_seconds",
        "_claimed",
        "_lock",
    )

    def __init__(
        self,
        *,
        token: object,
        authorization_reference_sha256: str,
        target_job_sha256: str,
        baseline_sha256: str,
        evidence_snapshot_sha256: str,
    ) -> None:
        if token is not _AUTHORIZATION_FACTORY_TOKEN:
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_authorization_untrusted",
                "The private shadow capability was not created by its authorization factory.",
            )
        self.authorization_reference_sha256 = authorization_reference_sha256
        self.target_job_sha256 = target_job_sha256
        self.baseline_sha256 = baseline_sha256
        self.evidence_snapshot_sha256 = evidence_snapshot_sha256
        self.maximum_calls = MAX_REPAIR_CALLS
        self.total_timeout_seconds = MAX_TOTAL_REPAIR_SECONDS
        self._claimed = False
        self._lock = threading.Lock()

    def claim(
        self,
        *,
        target_job_id: str,
        baseline: Mapping[str, Any],
        segments: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically consume the capability if every bound identity still matches."""

        target_sha256 = canonical_json_sha256(target_job_id)
        baseline_sha256 = canonical_json_sha256(baseline)
        evidence_sha256 = MeetingInvariantEvidenceSnapshot.from_segments(
            segments
        ).snapshot_sha256
        with self._lock:
            if self._claimed:
                raise MeetingFieldRepairShadowBridgeError(
                    "private_shadow_authorization_replayed",
                    "The one-shot private shadow authorization has already been used.",
                )
            if (
                target_sha256 != self.target_job_sha256
                or baseline_sha256 != self.baseline_sha256
                or evidence_sha256 != self.evidence_snapshot_sha256
            ):
                raise MeetingFieldRepairShadowBridgeError(
                    "private_shadow_authorization_scope_mismatch",
                    "The private shadow input no longer matches its authorized scope.",
                )
            self._claimed = True


@dataclass(frozen=True)
class AuthorizedPrivateMeetingShadowResult:
    """In-memory candidate plus content-free acceptance and performance evidence."""

    shadow: MeetingFieldRepairShadowBridgeResult
    authorization_reference_sha256: str
    target_job_sha256: str
    evidence_snapshot_sha256: str
    transport_kind: str
    changed_fields: tuple[str, ...]
    semantic_validators: tuple[str, ...]
    progress_events: tuple[MeetingFieldRepairProgress, ...]
    persistence_permitted: bool = False


def build_authorized_private_meeting_shadow_capability(
    *,
    explicit_authorization: bool,
    authorization_reference: str,
    target_job_id: str,
    baseline: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
) -> AuthorizedPrivateMeetingShadowCapability:
    """Mint one target-bound capability after explicit out-of-band authorization."""

    if explicit_authorization is not True:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_not_authorized",
            "A private meeting shadow requires explicit project-owner authorization.",
        )
    if (
        not isinstance(authorization_reference, str)
        or not authorization_reference.strip()
        or "\x00" in authorization_reference
        or not isinstance(target_job_id, str)
        or not target_job_id.strip()
        or "\x00" in target_job_id
        or not isinstance(baseline, Mapping)
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_authorization_invalid",
            "The private shadow authorization scope is incomplete.",
        )
    segment_copies = _validate_and_copy_private_segments(segments)
    snapshot = MeetingInvariantEvidenceSnapshot.from_segments(segment_copies)
    return AuthorizedPrivateMeetingShadowCapability(
        token=_AUTHORIZATION_FACTORY_TOKEN,
        authorization_reference_sha256=canonical_json_sha256(
            authorization_reference.strip()
        ),
        target_job_sha256=canonical_json_sha256(target_job_id),
        baseline_sha256=canonical_json_sha256(baseline),
        evidence_snapshot_sha256=snapshot.snapshot_sha256,
    )


def run_authorized_private_meeting_field_repair_shadow(
    *,
    capability: AuthorizedPrivateMeetingShadowCapability,
    target_job_id: str,
    bundle: ProfileBundle,
    baseline: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    issues: Sequence[MeetingRepairIssue],
    transport: RecordingSyntheticFieldRepairTransport
    | LocalOllamaMeetingFieldRepairTransport,
    progress: ProgressCallback | None = None,
    cancelled: CancellationCheck | None = None,
) -> AuthorizedPrivateMeetingShadowResult:
    """Run one authorized private shadow without any persistence authority."""

    if not isinstance(capability, AuthorizedPrivateMeetingShadowCapability):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_authorization_untrusted",
            "The private shadow requires its sealed one-shot capability.",
        )
    _validate_pinned_bundle(bundle)
    if not isinstance(
        transport,
        (RecordingSyntheticFieldRepairTransport, LocalOllamaMeetingFieldRepairTransport),
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_transport_untrusted",
            "The private shadow requires a supported sealed transport.",
        )
    if isinstance(transport, LocalOllamaMeetingFieldRepairTransport) and (
        cancelled is None or not transport.uses_cancellation_check(cancelled)
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_cancellation_unbound",
            "The private local transport must share the orchestrator cancellation check.",
        )
    if len(issues) > capability.maximum_calls:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_repairs_invalid",
            "The private shadow accepts zero to three explicit repair issues.",
        )
    _raise_if_cancelled(cancelled)
    baseline_copy = copy.deepcopy(dict(baseline))
    segment_copies = _validate_and_copy_private_segments(segments)
    issue_copies = copy.deepcopy(tuple(issues))
    _validate_private_references(
        baseline=baseline_copy,
        segments=segment_copies,
        issues=issue_copies,
    )
    capability.claim(
        target_job_id=target_job_id,
        baseline=baseline_copy,
        segments=segment_copies,
    )
    trusted_validator = build_trusted_meeting_invariant_validator(segment_copies)
    if (
        trusted_validator.evidence_snapshot_sha256
        != capability.evidence_snapshot_sha256
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_invariant_evidence_mismatch",
            "The trusted invariant validator is bound to different private evidence.",
        )

    baseline_sha256 = canonical_json_sha256(baseline_copy)
    segments_sha256 = canonical_json_sha256(segment_copies)
    plans = plan_meeting_field_repairs(
        baseline=baseline_copy,
        segments=segment_copies,
        issues=issue_copies,
    )
    semantic_validators = tuple(
        validator
        for validator in bundle.validation_policy["registered_validators"]
        if validator in MEETING_SEMANTIC_VALIDATORS
    )
    progress_events: list[MeetingFieldRepairProgress] = []

    def record_progress(event: MeetingFieldRepairProgress) -> None:
        if not isinstance(event, MeetingFieldRepairProgress):
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_progress_invalid",
                "The private shadow emitted an invalid progress event.",
            )
        progress_events.append(event)
        if progress is not None:
            progress(event)

    def final_validator(document: Mapping[str, Any]) -> Mapping[str, Any]:
        before_sha256 = canonical_json_sha256(document)
        try:
            validated = trusted_validator(copy.deepcopy(dict(document)))
        except MeetingInvariantValidatorError as error:
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_invariant_validation_failed",
                "The trusted Worker invariant validator rejected the private shadow.",
                issue_codes=(error.code,),
            ) from error
        if canonical_json_sha256(validated) != before_sha256:
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_invariant_mutated_document",
                "The trusted invariant validator modified the private shadow.",
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
                "private_shadow_semantic_gate_failed",
                "The authorized private shadow failed its meeting semantic gate.",
                issue_codes=error.result.issue_codes,
            ) from error

    result = run_profiled_meeting_field_repair_shadow(
        bundle=bundle,
        baseline=baseline_copy,
        plans=plans,
        caller=transport,
        final_validator=final_validator,
        progress=record_progress,
        cancelled=cancelled,
    )
    _raise_if_cancelled(cancelled)
    if result.call_count > capability.maximum_calls:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_call_budget_exceeded",
            "The private shadow exceeded its authorization call budget.",
        )
    if result.elapsed_seconds > capability.total_timeout_seconds:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_total_timeout",
            "The private shadow exceeded its authorization time budget.",
        )
    if canonical_json_sha256(baseline) != baseline_sha256:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_baseline_mutated",
            "The private baseline changed during the shadow run.",
        )
    if canonical_json_sha256(segments) != segments_sha256:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_segments_mutated",
            "The private evidence changed during the shadow run.",
        )
    document = copy.deepcopy(dict(result.document))
    changed_fields = tuple(
        sorted(
            field
            for field in set(baseline_copy) | set(document)
            if canonical_json_sha256(baseline_copy.get(field))
            != canonical_json_sha256(document.get(field))
        )
    )
    shadow = MeetingFieldRepairShadowBridgeResult(
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
    return AuthorizedPrivateMeetingShadowResult(
        shadow=shadow,
        authorization_reference_sha256=capability.authorization_reference_sha256,
        target_job_sha256=capability.target_job_sha256,
        evidence_snapshot_sha256=trusted_validator.evidence_snapshot_sha256,
        transport_kind=(
            "recording_synthetic"
            if isinstance(transport, RecordingSyntheticFieldRepairTransport)
            else "local_ollama"
        ),
        changed_fields=changed_fields,
        semantic_validators=semantic_validators,
        progress_events=tuple(progress_events),
    )


def _validate_and_copy_private_segments(
    segments: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    if (
        isinstance(segments, (str, bytes))
        or not isinstance(segments, Sequence)
        or not 1 <= len(segments) <= MAX_PRIVATE_SHADOW_SEGMENTS
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_segments_invalid",
            "The private segment set is empty or exceeds its read-only limit.",
        )
    normalized: list[dict[str, Any]] = []
    character_count = 0
    for segment in segments:
        if not isinstance(segment, Mapping) or set(segment) != _SEGMENT_FIELDS:
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_segment_fields_invalid",
                "Private segments may contain only id, speaker, text, and start time.",
            )
        segment_id = segment.get("segment_id")
        speaker_id = segment.get("speaker_id")
        text = segment.get("text")
        start_ms = segment.get("start_ms")
        if (
            not isinstance(segment_id, str)
            or not segment_id
            or len(segment_id) > 256
            or not segment_id.isprintable()
        ):
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_segment_id_invalid",
                "A private segment requires a bounded printable id.",
            )
        if speaker_id is not None and (
            not isinstance(speaker_id, str)
            or not speaker_id
            or len(speaker_id) > 256
            or not speaker_id.isprintable()
        ):
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_speaker_id_invalid",
                "A private segment speaker id must be bounded and printable when present.",
            )
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_segment_text_invalid",
                "A private segment requires safe non-empty text.",
            )
        if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_segment_start_invalid",
                "A private segment requires a non-negative integer start time.",
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
    if character_count > MAX_PRIVATE_SHADOW_CHARACTERS:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_segments_too_large",
            "The private segment text exceeds its read-only evaluation limit.",
        )
    if len({segment["segment_id"] for segment in normalized}) != len(normalized):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_segment_id_duplicate",
            "Private segment ids must be unique.",
        )
    if len({segment["start_ms"] for segment in normalized}) != len(normalized):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_segment_start_duplicate",
            "Private segment start times must be unique.",
        )
    return tuple(normalized)


def _validate_private_references(
    *,
    baseline: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    issues: Sequence[MeetingRepairIssue],
) -> None:
    segment_ids = {str(segment["segment_id"]) for segment in segments}
    speaker_ids = {
        str(segment["speaker_id"])
        for segment in segments
        if isinstance(segment.get("speaker_id"), str)
    }
    if any(
        reference not in segment_ids for reference in _document_segment_references(baseline)
    ):
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_baseline_reference_invalid",
            "The private baseline references evidence outside its authorized input.",
        )
    for issue in issues:
        if not isinstance(issue, MeetingRepairIssue):
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_issue_invalid",
                "The private shadow requires registered meeting repair issues.",
            )
        if not issue.anchor_segment_ids or any(
            segment_id not in segment_ids for segment_id in issue.anchor_segment_ids
        ):
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_issue_anchor_invalid",
                "A private repair issue references evidence outside its authorized input.",
            )
        if issue.speaker_id is not None and issue.speaker_id not in speaker_ids:
            raise MeetingFieldRepairShadowBridgeError(
                "private_shadow_issue_speaker_invalid",
                "A private repair issue references an unknown speaker.",
            )
        if issue.target.item_key is not None and issue.target.field == "speaker_summaries":
            if issue.target.item_key not in speaker_ids:
                raise MeetingFieldRepairShadowBridgeError(
                    "private_shadow_target_speaker_invalid",
                    "A private speaker repair target is outside its authorized input.",
                )


def _raise_if_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is None:
        return
    try:
        is_cancelled = cancelled()
    except Exception as error:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_cancellation_check_failed",
            "The private shadow cancellation check failed closed.",
        ) from error
    if is_cancelled:
        raise MeetingFieldRepairShadowBridgeError(
            "private_shadow_cancelled",
            "The private shadow was cancelled before completion.",
        )
