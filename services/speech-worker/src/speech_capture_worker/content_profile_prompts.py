"""Read-only prompt adapter for an already validated meeting ProfileBundle.

The adapter exposes inert text only.  Schema construction, evidence payloads,
recording context, model selection, timeouts, retries, validation, revisions and
publication remain owned by the Worker engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from speech_capture_worker.content_profiles import ProfileBundle, load_profile_bundle

_BUNDLED_MEETING_PROFILE = (
    Path(__file__).parent / "profile_bundles" / "meeting" / "2026-08-29.2"
)
_MAX_PROMPT_CHARACTERS = 32_000


class ContentProfilePromptError(ValueError):
    """Raised when a validated bundle cannot satisfy the meeting prompt contract."""


@dataclass(frozen=True)
class MeetingProfilePrompts:
    """The four meeting content-policy slots allowed in the first migration."""

    extraction: str
    synthesis: str
    quality_edit: str
    meeting_outcomes: str

    @classmethod
    def from_bundle(cls, bundle: ProfileBundle) -> MeetingProfilePrompts:
        if bundle.content_type != "meeting":
            raise ContentProfilePromptError("Meeting prompts require a meeting bundle.")
        return cls(
            extraction=_required_prompt(bundle, "extraction"),
            synthesis=_required_prompt(bundle, "synthesis"),
            quality_edit=_required_prompt(bundle, "quality_edit"),
            meeting_outcomes=_required_prompt(
                bundle,
                "named_repairs",
                repair_name="meeting_outcomes",
            ),
        )


def load_bundled_meeting_profile() -> ProfileBundle:
    """Load the repository-bundled meeting profile through the strict B1 loader."""

    return load_profile_bundle(_BUNDLED_MEETING_PROFILE)


def _required_prompt(
    bundle: ProfileBundle,
    slot: str,
    *,
    repair_name: str | None = None,
) -> str:
    value = bundle.read_prompt(slot, repair_name=repair_name)
    if value is None:
        raise ContentProfilePromptError(f"Required meeting prompt slot {slot!r} is empty.")
    normalized = value.strip()
    if not normalized:
        raise ContentProfilePromptError(f"Required meeting prompt slot {slot!r} is blank.")
    if len(normalized) > _MAX_PROMPT_CHARACTERS or "\x00" in normalized:
        raise ContentProfilePromptError(f"Required meeting prompt slot {slot!r} is unsafe.")
    return normalized
