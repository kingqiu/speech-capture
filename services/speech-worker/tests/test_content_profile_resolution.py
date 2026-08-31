"""B3 contract tests for controlled profile selection and immutable task pins."""

from __future__ import annotations

import ast
from pathlib import Path
from types import MappingProxyType

import pytest

from speech_capture_worker.content_profile_resolution import (
    ContentProfilePin,
    ContentProfileResolver,
    PinnedProfileUnavailable,
    ProfileActivationError,
    ProfileSource,
    builtin_profile_pin,
)
from speech_capture_worker.content_profiles import (
    ProfileBundle,
    ProfileBundleRegistry,
    ProfileReference,
)
from speech_capture_worker.domain import SUPPORTED_CONTENT_TYPES


def _bundle(
    tmp_path: Path,
    *,
    version: str,
    content_type: str = "meeting",
    digest_character: str = "1",
) -> ProfileBundle:
    root = tmp_path / f"{content_type}-{version}"
    root.mkdir()
    return ProfileBundle(
        root=root,
        profile_id=f"speech-capture/{content_type}",
        profile_version=version,
        content_type=content_type,
        bundle_sha256="sha256:" + digest_character * 64,
        document_schema_version="1.0.0",
        renderer_version="1.0.0",
        manifest=MappingProxyType({}),
        prompts=MappingProxyType({}),
        document_policy=MappingProxyType({}),
        execution_policy=MappingProxyType({}),
        validation_policy=MappingProxyType(
            {
                "registered_validators": ["meeting.decision.confirmed"],
                "thresholds": {"minimum_context_facets": 2},
            }
        ),
        renderer=MappingProxyType({}),
    )


def test_default_resolver_is_builtin_and_registration_does_not_activate(
    tmp_path: Path,
) -> None:
    registry = ProfileBundleRegistry()
    external = _bundle(tmp_path, version="1.0.0")
    registry.register_validated(external)
    resolver = ContentProfileResolver(registry)

    for content_type in SUPPORTED_CONTENT_TYPES:
        resolved = resolver.resolve_for_new_task(content_type)
        assert resolved.pin == builtin_profile_pin(content_type)
        assert resolved.bundle is None


def test_explicit_meeting_activation_does_not_change_other_content_types(
    tmp_path: Path,
) -> None:
    registry = ProfileBundleRegistry()
    external = _bundle(tmp_path, version="1.0.0")
    registry.register_validated(external)
    resolver = ContentProfileResolver(registry)

    snapshot = resolver.activate_meeting(external.reference)

    assert snapshot.generation == 1
    meeting = resolver.resolve_for_new_task("meeting")
    assert meeting.is_external
    assert meeting.bundle is external
    assert meeting.pin.reference == external.reference
    for content_type in SUPPORTED_CONTENT_TYPES - {"meeting"}:
        assert resolver.resolve_for_new_task(content_type).pin.source is ProfileSource.BUILTIN


def test_pin_round_trip_is_strict_and_complete(tmp_path: Path) -> None:
    registry = ProfileBundleRegistry()
    external = _bundle(tmp_path, version="1.0.0")
    registry.register_validated(external)
    resolver = ContentProfileResolver(registry)
    resolver.activate_meeting(external.reference)
    pin = resolver.resolve_for_new_task("meeting").pin

    restored = ContentProfilePin.from_dict(pin.to_dict())

    assert restored == pin
    assert restored.document_schema_version == "1.0.0"
    assert restored.renderer_version == "1.0.0"
    assert restored.validator_set_version.startswith("sha256:")
    malformed = pin.to_dict()
    malformed["silent_fallback"] = True
    with pytest.raises(Exception, match="invalid shape"):
        ContentProfilePin.from_dict(malformed)


def test_old_task_stays_on_exact_pin_after_new_activation(tmp_path: Path) -> None:
    registry = ProfileBundleRegistry()
    first = _bundle(tmp_path, version="1.0.0", digest_character="1")
    second = _bundle(tmp_path, version="1.1.0", digest_character="2")
    registry.register_validated(first)
    registry.register_validated(second)
    resolver = ContentProfileResolver(registry)
    resolver.activate_meeting(first.reference)
    old_pin = resolver.resolve_for_new_task("meeting").pin

    resolver.activate_meeting(second.reference)

    assert resolver.resolve_for_new_task("meeting").bundle is second
    assert resolver.resolve_pinned(old_pin).bundle is first


def test_explicit_meeting_rollback_is_atomic_and_preserves_newer_task_pin(
    tmp_path: Path,
) -> None:
    registry = ProfileBundleRegistry()
    last_known_good = _bundle(tmp_path, version="2026-08-29.1", digest_character="1")
    candidate = _bundle(tmp_path, version="2026-08-29.2", digest_character="2")
    registry.register_validated(last_known_good)
    registry.register_validated(candidate)
    resolver = ContentProfileResolver(registry)

    resolver.activate_meeting(last_known_good.reference)
    resolver.activate_meeting(candidate.reference)
    candidate_pin = resolver.resolve_for_new_task("meeting").pin
    rollback = resolver.activate_meeting(last_known_good.reference)

    assert rollback.generation == 3
    assert rollback.active_meeting_reference == last_known_good.reference
    assert resolver.resolve_for_new_task("meeting").bundle is last_known_good
    assert resolver.resolve_pinned(candidate_pin).bundle is candidate
    assert registry.last_known_good("meeting") is last_known_good


def test_serialized_pin_resolves_exact_bundle_after_resolver_restart(tmp_path: Path) -> None:
    first_registry = ProfileBundleRegistry()
    meeting = _bundle(tmp_path, version="1.0.0", digest_character="7")
    first_registry.register_validated(meeting)
    first_resolver = ContentProfileResolver(first_registry)
    first_resolver.activate_meeting(meeting.reference)
    persisted = first_resolver.resolve_for_new_task("meeting").pin.to_dict()

    restarted_registry = ProfileBundleRegistry()
    restarted_registry.register_validated(meeting)
    restarted_resolver = ContentProfileResolver(restarted_registry)
    restored = restarted_resolver.resolve_pinned(ContentProfilePin.from_dict(persisted))

    assert restored.is_external is True
    assert restored.bundle is meeting
    assert restored.pin.to_dict() == persisted


def test_failed_activation_preserves_current_and_last_known_good(tmp_path: Path) -> None:
    registry = ProfileBundleRegistry()
    meeting = _bundle(tmp_path, version="1.0.0", digest_character="1")
    interview = _bundle(
        tmp_path,
        version="1.0.0",
        content_type="interview",
        digest_character="2",
    )
    registry.register_validated(meeting)
    registry.register_validated(interview)
    resolver = ContentProfileResolver(registry)
    first_snapshot = resolver.activate_meeting(meeting.reference)

    with pytest.raises(ProfileActivationError, match="Only a meeting"):
        resolver.activate_meeting(interview.reference)

    assert resolver.snapshot() == first_snapshot
    assert registry.last_known_good("meeting") is meeting
    assert resolver.resolve_for_new_task("meeting").bundle is meeting


def test_deactivation_returns_new_meeting_tasks_to_builtin(tmp_path: Path) -> None:
    registry = ProfileBundleRegistry()
    meeting = _bundle(tmp_path, version="1.0.0")
    registry.register_validated(meeting)
    resolver = ContentProfileResolver(registry)
    resolver.activate_meeting(meeting.reference)

    resolver.deactivate_meeting()

    assert resolver.resolve_for_new_task("meeting").pin == builtin_profile_pin("meeting")
    assert registry.last_known_good("meeting") is meeting


def test_missing_or_mismatched_pinned_profile_requires_safe_pause(tmp_path: Path) -> None:
    missing_reference = ProfileReference(
        profile_id="speech-capture/meeting",
        profile_version="1.0.0",
        bundle_sha256="sha256:" + "9" * 64,
    )
    missing_pin = ContentProfilePin(
        content_type="meeting",
        source=ProfileSource.EXTERNAL,
        reference=missing_reference,
        document_schema_version="1.0.0",
        validator_set_version="sha256:" + "8" * 64,
        renderer_version="1.0.0",
    )
    resolver = ContentProfileResolver(ProfileBundleRegistry())

    with pytest.raises(PinnedProfileUnavailable, match="exact external"):
        resolver.resolve_pinned(missing_pin)

    current_builtin = builtin_profile_pin("meeting")
    stale_builtin = ContentProfilePin(
        content_type=current_builtin.content_type,
        source=current_builtin.source,
        reference=ProfileReference(
            profile_id=current_builtin.reference.profile_id,
            profile_version="builtin-older",
            bundle_sha256=current_builtin.reference.bundle_sha256,
        ),
        document_schema_version=current_builtin.document_schema_version,
        validator_set_version=current_builtin.validator_set_version,
        renderer_version=current_builtin.renderer_version,
    )
    with pytest.raises(PinnedProfileUnavailable, match="exact built-in"):
        resolver.resolve_pinned(stale_builtin)


def test_resolver_has_no_job_execution_revision_or_publication_dependency() -> None:
    module_path = (
        Path(__file__).parents[1]
        / "src"
        / "speech_capture_worker"
        / "content_profile_resolution.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        forbidden in module
        for module in imported_modules
        for forbidden in (
            "job_store",
            "structuring_execution",
            "summary_revisions",
            "publication",
        )
    )
