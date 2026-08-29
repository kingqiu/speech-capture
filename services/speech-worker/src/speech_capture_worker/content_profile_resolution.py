"""Controlled content-profile resolution and immutable per-task provenance.

This module is deliberately independent from job execution and publication.  It
establishes the B3 selection boundary without changing the current structuring
runtime: callers must explicitly activate an external meeting profile, and every
task must persist the returned :class:`ContentProfilePin` before using it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Any

from speech_capture_worker.content_profiles import (
    DOCUMENT_SCHEMA_VERSION,
    ProfileBundle,
    ProfileBundleError,
    ProfileBundleRegistry,
    ProfileReference,
)
from speech_capture_worker.domain import SUPPORTED_CONTENT_TYPES
from speech_capture_worker.note_prompt_profiles import NOTE_PROMPT_VERSION

BUILTIN_PROFILE_ID_PREFIX = "speech-capture/builtin"
BUILTIN_RENDERER_VERSION = "builtin-markdown-1.0.0"
BUILTIN_VALIDATOR_SET_VERSION = "builtin-structured-note-1.0.0"

_PIN_FIELDS = frozenset(
    {
        "content_type",
        "source",
        "reference",
        "document_schema_version",
        "validator_set_version",
        "renderer_version",
    }
)
_REFERENCE_FIELDS = frozenset({"profile_id", "profile_version", "bundle_sha256"})
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProfileSource(StrEnum):
    BUILTIN = "builtin"
    EXTERNAL = "external"


class ProfileResolutionError(RuntimeError):
    """Base error for a resolver decision that cannot be made safely."""


class ProfileActivationError(ProfileResolutionError):
    """Raised when an external profile cannot become the active meeting profile."""


class PinnedProfileUnavailable(ProfileResolutionError):
    """Raised when an existing task's exact profile is no longer available.

    The caller must pause the task.  Falling forward to another version would make
    a resumed task non-reproducible and is therefore forbidden.
    """


@dataclass(frozen=True)
class ContentProfilePin:
    """Complete, serializable profile provenance stored with a task/checkpoint."""

    content_type: str
    source: ProfileSource
    reference: ProfileReference
    document_schema_version: str
    validator_set_version: str
    renderer_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_type": self.content_type,
            "source": self.source.value,
            "reference": self.reference.to_dict(),
            "document_schema_version": self.document_schema_version,
            "validator_set_version": self.validator_set_version,
            "renderer_version": self.renderer_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> ContentProfilePin:
        if not isinstance(value, dict) or set(value) != _PIN_FIELDS:
            raise ProfileResolutionError("Content profile pin has an invalid shape.")
        content_type = _required_string(value, "content_type")
        if content_type not in SUPPORTED_CONTENT_TYPES:
            raise ProfileResolutionError("Content profile pin has an unsupported content type.")
        try:
            source = ProfileSource(_required_string(value, "source"))
        except ValueError as exc:
            raise ProfileResolutionError("Content profile pin has an invalid source.") from exc
        reference_value = value["reference"]
        if not isinstance(reference_value, dict) or set(reference_value) != _REFERENCE_FIELDS:
            raise ProfileResolutionError("Content profile pin reference has an invalid shape.")
        bundle_sha256 = _required_string(reference_value, "bundle_sha256")
        if _SHA256_PATTERN.fullmatch(bundle_sha256) is None:
            raise ProfileResolutionError("Content profile pin has an invalid bundle hash.")
        return cls(
            content_type=content_type,
            source=source,
            reference=ProfileReference(
                profile_id=_required_string(reference_value, "profile_id"),
                profile_version=_required_string(reference_value, "profile_version"),
                bundle_sha256=bundle_sha256,
            ),
            document_schema_version=_required_string(value, "document_schema_version"),
            validator_set_version=_required_string(value, "validator_set_version"),
            renderer_version=_required_string(value, "renderer_version"),
        )


@dataclass(frozen=True)
class ResolvedContentProfile:
    pin: ContentProfilePin
    bundle: ProfileBundle | None

    @property
    def is_external(self) -> bool:
        return self.pin.source is ProfileSource.EXTERNAL


@dataclass(frozen=True)
class ResolverSnapshot:
    generation: int
    active_meeting_reference: ProfileReference | None


class ContentProfileResolver:
    """Atomic meeting-profile selector with exact resume semantics.

    Only ``meeting`` may use an external profile in B3.  Other content types always
    resolve to their existing built-in profile.  Merely registering a bundle never
    activates it.
    """

    def __init__(self, registry: ProfileBundleRegistry) -> None:
        self._registry = registry
        self._lock = RLock()
        self._snapshot = ResolverSnapshot(generation=0, active_meeting_reference=None)

    def snapshot(self) -> ResolverSnapshot:
        with self._lock:
            return self._snapshot

    def activate_meeting(self, reference: ProfileReference) -> ResolverSnapshot:
        """Atomically activate an already validated, exact meeting bundle."""

        try:
            bundle = self._registry.resolve(reference)
        except ProfileBundleError as exc:
            raise ProfileActivationError("Meeting profile activation was rejected.") from exc
        if bundle.content_type != "meeting":
            raise ProfileActivationError("Only a meeting profile can enter the B3 resolver.")
        if bundle.document_schema_version != DOCUMENT_SCHEMA_VERSION:
            raise ProfileActivationError("Meeting profile document schema is incompatible.")

        # Marking last-known-good happens only after every activation guard passes.
        self._registry.mark_last_known_good(bundle.reference)
        with self._lock:
            self._snapshot = ResolverSnapshot(
                generation=self._snapshot.generation + 1,
                active_meeting_reference=bundle.reference,
            )
            return self._snapshot

    def activate_last_known_good_meeting(self) -> ResolverSnapshot:
        bundle = self._registry.last_known_good("meeting")
        if bundle is None:
            raise ProfileActivationError("No last-known-good meeting profile is available.")
        return self.activate_meeting(bundle.reference)

    def deactivate_meeting(self) -> ResolverSnapshot:
        """Return new meeting tasks to the current built-in profile."""

        with self._lock:
            self._snapshot = ResolverSnapshot(
                generation=self._snapshot.generation + 1,
                active_meeting_reference=None,
            )
            return self._snapshot

    def resolve_for_new_task(self, content_type: str) -> ResolvedContentProfile:
        """Resolve a profile once for a new task; callers persist the returned pin."""

        _validate_content_type(content_type)
        if content_type != "meeting":
            return ResolvedContentProfile(pin=builtin_profile_pin(content_type), bundle=None)

        with self._lock:
            active_reference = self._snapshot.active_meeting_reference
        if active_reference is not None:
            try:
                return _resolved_external(self._registry.resolve(active_reference))
            except ProfileBundleError:
                # New tasks may fall back; already pinned tasks may not.
                try:
                    last_known_good = self._registry.last_known_good("meeting")
                except ProfileBundleError:
                    last_known_good = None
                if last_known_good is not None:
                    return _resolved_external(last_known_good)
        return ResolvedContentProfile(pin=builtin_profile_pin("meeting"), bundle=None)

    def resolve_pinned(self, pin: ContentProfilePin) -> ResolvedContentProfile:
        """Resolve an existing task's exact profile or require a safe pause."""

        _validate_content_type(pin.content_type)
        if pin.source is ProfileSource.BUILTIN:
            expected = builtin_profile_pin(pin.content_type)
            if pin != expected:
                raise PinnedProfileUnavailable(
                    "The exact built-in profile pinned by this task is unavailable."
                )
            return ResolvedContentProfile(pin=pin, bundle=None)

        try:
            bundle = self._registry.resolve(pin.reference)
        except ProfileBundleError as exc:
            raise PinnedProfileUnavailable(
                "The exact external profile pinned by this task is unavailable."
            ) from exc
        resolved = _resolved_external(bundle)
        if resolved.pin != pin:
            raise PinnedProfileUnavailable(
                "The registered bundle does not match the task's full profile provenance."
            )
        return resolved


def builtin_profile_pin(content_type: str) -> ContentProfilePin:
    """Return deterministic provenance for the current code-bundled profile."""

    _validate_content_type(content_type)
    version = f"builtin-{NOTE_PROMPT_VERSION}"
    identity = {
        "content_type": content_type,
        "document_schema_version": DOCUMENT_SCHEMA_VERSION,
        "profile_version": version,
        "renderer_version": BUILTIN_RENDERER_VERSION,
        "validator_set_version": BUILTIN_VALIDATOR_SET_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ContentProfilePin(
        content_type=content_type,
        source=ProfileSource.BUILTIN,
        reference=ProfileReference(
            profile_id=f"{BUILTIN_PROFILE_ID_PREFIX}/{content_type}",
            profile_version=version,
            bundle_sha256=f"sha256:{digest}",
        ),
        document_schema_version=DOCUMENT_SCHEMA_VERSION,
        validator_set_version=BUILTIN_VALIDATOR_SET_VERSION,
        renderer_version=BUILTIN_RENDERER_VERSION,
    )


def _resolved_external(bundle: ProfileBundle) -> ResolvedContentProfile:
    validator_digest = hashlib.sha256(
        json.dumps(
            dict(bundle.validation_policy),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ResolvedContentProfile(
        pin=ContentProfilePin(
            content_type=bundle.content_type,
            source=ProfileSource.EXTERNAL,
            reference=bundle.reference,
            document_schema_version=bundle.document_schema_version,
            validator_set_version=f"sha256:{validator_digest}",
            renderer_version=bundle.renderer_version,
        ),
        bundle=bundle,
    )


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or item.strip() != item:
        raise ProfileResolutionError(f"Content profile pin field {key!r} is invalid.")
    return item


def _validate_content_type(content_type: str) -> None:
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise ProfileResolutionError(f"Unsupported content type: {content_type!r}.")
