"""Version and capability negotiation contract tests."""

import pytest

from speech_capture_worker.artifact_generation import ARTIFACT_SCHEMA_VERSION
from speech_capture_worker.protocol_contract import (
    PROTOCOL_VERSION,
    CompatibilityIssue,
    CompatibilityRequest,
    ProtocolCapability,
    SemanticVersion,
    VersionRange,
    get_capabilities,
    negotiate_compatibility,
)


def test_capabilities_are_stable_bounded_and_current() -> None:
    capabilities = get_capabilities()

    assert capabilities.protocol == VersionRange(PROTOCOL_VERSION, PROTOCOL_VERSION)
    assert capabilities.artifact_schema == VersionRange(
        ARTIFACT_SCHEMA_VERSION,
        ARTIFACT_SCHEMA_VERSION,
    )
    assert capabilities.features == tuple(ProtocolCapability)
    assert capabilities.content_types == (
        "course",
        "generic",
        "interview",
        "meeting",
        "speech",
        "voice_memo",
    )
    assert capabilities.model_profiles == ("accuracy", "speed")
    assert capabilities.limits.max_snapshot_segments == 500
    assert capabilities.limits.max_update_events == 1_000


def test_compatible_client_negotiates_current_versions() -> None:
    result = negotiate_compatibility(
        CompatibilityRequest(
            protocol=VersionRange("0.9.0", "1.4.0"),
            artifact_schema=VersionRange("1.4.0", "1.8.0"),
            required_features=(
                ProtocolCapability.RECORDING_CONTEXT.value,
                ProtocolCapability.ATOMIC_VAULT_PUBLICATION.value,
            ),
        )
    )

    assert result.compatible is True
    assert result.protocol_version == PROTOCOL_VERSION
    assert result.artifact_schema_version == ARTIFACT_SCHEMA_VERSION
    assert result.missing_features == ()
    assert result.issues == ()


def test_incompatible_ranges_return_all_stable_reasons() -> None:
    result = negotiate_compatibility(
        CompatibilityRequest(
            protocol=VersionRange("2.0.0", "2.1.0"),
            artifact_schema=VersionRange("2.0.0", "2.1.0"),
        )
    )

    assert result.compatible is False
    assert result.protocol_version is None
    assert result.artifact_schema_version is None
    assert result.issues == (
        CompatibilityIssue.PROTOCOL_VERSION_INCOMPATIBLE,
        CompatibilityIssue.ARTIFACT_SCHEMA_INCOMPATIBLE,
    )


def test_unknown_future_capability_is_reported_instead_of_rejected() -> None:
    result = negotiate_compatibility(
        CompatibilityRequest(
            protocol=VersionRange(PROTOCOL_VERSION, PROTOCOL_VERSION),
            artifact_schema=VersionRange(
                ARTIFACT_SCHEMA_VERSION,
                ARTIFACT_SCHEMA_VERSION,
            ),
            required_features=("future_capability",),
        )
    )

    assert result.compatible is False
    assert result.missing_features == ("future_capability",)
    assert result.issues == (CompatibilityIssue.REQUIRED_CAPABILITY_UNAVAILABLE,)


@pytest.mark.parametrize("value", ["1", "1.0", "01.0.0", "1.0.0-alpha", "1.0.0 "])
def test_semantic_versions_require_canonical_three_part_syntax(value) -> None:
    with pytest.raises(ValueError):
        SemanticVersion.parse(value)


def test_version_range_rejects_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="minimum"):
        VersionRange("1.1.0", "1.0.0")
