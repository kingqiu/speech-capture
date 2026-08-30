"""Single explicit entrypoint for isolated public-synthetic meeting repair shadows.

The orchestrator composes the pinned Profile bundle, deterministic planner,
supported transport, sealed Worker invariant capability, semantic gate, progress,
and cancellation.  It is not imported by the production runtime and grants no
candidate, checkpoint, revision, publication, filesystem, or Vault authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from speech_capture_worker.content_profiles import ProfileBundle
from speech_capture_worker.meeting_field_repair_local_transport import (
    LocalOllamaMeetingFieldRepairTransport,
)
from speech_capture_worker.meeting_field_repair_shadow import (
    CancellationCheck,
    MeetingFieldRepairProgress,
    ProgressCallback,
)
from speech_capture_worker.meeting_field_repair_shadow_bridge import (
    MeetingFieldRepairShadowBridgeError,
    MeetingFieldRepairShadowBridgeResult,
    MeetingFieldRepairShadowOptIn,
    run_public_synthetic_meeting_field_repair_shadow,
)
from speech_capture_worker.meeting_field_repair_transport_shadow import (
    RecordingSyntheticFieldRepairTransport,
)
from speech_capture_worker.meeting_field_repairs import MeetingRepairIssue
from speech_capture_worker.structuring_execution import (
    build_trusted_meeting_invariant_validator,
)

SUPPORTED_PUBLIC_SHADOW_TRANSPORTS = (
    RecordingSyntheticFieldRepairTransport,
    LocalOllamaMeetingFieldRepairTransport,
)


@dataclass(frozen=True)
class MeetingFieldRepairShadowOrchestrationResult:
    """In-memory result plus content-free orchestration audit facts."""

    shadow: MeetingFieldRepairShadowBridgeResult
    transport_kind: str
    evidence_snapshot_sha256: str
    progress_events: tuple[MeetingFieldRepairProgress, ...]


def run_orchestrated_public_synthetic_meeting_field_repair_shadow(
    *,
    opt_in: MeetingFieldRepairShadowOptIn,
    bundle: ProfileBundle,
    baseline: dict[str, Any],
    segments: Sequence[dict[str, Any]],
    issues: Sequence[MeetingRepairIssue],
    transport: RecordingSyntheticFieldRepairTransport
    | LocalOllamaMeetingFieldRepairTransport,
    progress: ProgressCallback | None = None,
    cancelled: CancellationCheck | None = None,
) -> MeetingFieldRepairShadowOrchestrationResult:
    """Run the only supported public-synthetic B3.2 composition.

    Inputs and output remain in memory.  The orchestrator records only content-free
    progress and hashes; it never persists the baseline, evidence, or model result.
    """

    if not isinstance(transport, SUPPORTED_PUBLIC_SHADOW_TRANSPORTS):
        raise MeetingFieldRepairShadowBridgeError(
            "shadow_orchestrator_transport_untrusted",
            "The public shadow orchestrator requires a supported sealed transport.",
        )
    trusted_validator = build_trusted_meeting_invariant_validator(segments)
    progress_events: list[MeetingFieldRepairProgress] = []

    def record_progress(event: MeetingFieldRepairProgress) -> None:
        if not isinstance(event, MeetingFieldRepairProgress):
            raise MeetingFieldRepairShadowBridgeError(
                "shadow_orchestrator_progress_invalid",
                "The field repair runner emitted an invalid progress event.",
            )
        progress_events.append(event)
        if progress is not None:
            progress(event)

    result = run_public_synthetic_meeting_field_repair_shadow(
        opt_in=opt_in,
        bundle=bundle,
        baseline=baseline,
        segments=segments,
        issues=issues,
        caller=transport,
        trusted_invariant_validator=trusted_validator,
        progress=record_progress,
        cancelled=cancelled,
    )
    return MeetingFieldRepairShadowOrchestrationResult(
        shadow=result,
        transport_kind=(
            "recording_synthetic"
            if isinstance(transport, RecordingSyntheticFieldRepairTransport)
            else "local_ollama"
        ),
        evidence_snapshot_sha256=trusted_validator.evidence_snapshot_sha256,
        progress_events=tuple(progress_events),
    )
