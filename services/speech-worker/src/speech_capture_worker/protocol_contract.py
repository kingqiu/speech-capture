"""Versioned public protocol and compatibility negotiation contract."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum

from speech_capture_worker import __version__
from speech_capture_worker.artifact_generation import ARTIFACT_SCHEMA_VERSION
from speech_capture_worker.domain import SUPPORTED_CONTENT_TYPES, ModelProfile
from speech_capture_worker.job_store import (
    DEFAULT_UPLOAD_CHUNK_SIZE_BYTES,
    MAX_UPLOAD_CHUNK_SIZE_BYTES,
    MAX_UPLOAD_PARTS,
)
from speech_capture_worker.recording_context import MAX_RECORDING_CONTEXT_CHARACTERS

PROTOCOL_VERSION = "1.0.0"
MIN_PROTOCOL_VERSION = PROTOCOL_VERSION
MAX_PROTOCOL_VERSION = PROTOCOL_VERSION
MIN_ARTIFACT_SCHEMA_VERSION = ARTIFACT_SCHEMA_VERSION
MAX_ARTIFACT_SCHEMA_VERSION = ARTIFACT_SCHEMA_VERSION
MAX_SNAPSHOT_SEGMENTS = 500
MAX_UPDATE_EVENTS = 1_000
SEMANTIC_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ProtocolCapability(StrEnum):
    RESUMABLE_UPLOADS = "resumable_uploads"
    JOB_SNAPSHOTS = "job_snapshots"
    BOUNDED_EVENT_UPDATES = "bounded_event_updates"
    PROGRESSIVE_TRANSCRIPT = "progressive_transcript"
    RECORDING_CONTEXT = "recording_context"
    CONTENT_TYPE_OVERRIDE = "content_type_override"
    CORRECTION_LEDGER = "correction_ledger"
    SUMMARY_REVISIONS = "summary_revisions"
    EVIDENCE_LINKED_ARTIFACTS = "evidence_linked_artifacts"
    PUBLICATION_LEASES = "publication_leases"
    ATOMIC_VAULT_PUBLICATION = "atomic_vault_publication"
    REVIEW_AUDIO_RANGES = "review_audio_ranges"
    WORKER_READINESS = "worker_readiness"


SUPPORTED_CAPABILITIES = tuple(ProtocolCapability)


class CompatibilityIssue(StrEnum):
    PROTOCOL_VERSION_INCOMPATIBLE = "protocol_version_incompatible"
    ARTIFACT_SCHEMA_INCOMPATIBLE = "artifact_schema_incompatible"
    REQUIRED_CAPABILITY_UNAVAILABLE = "required_capability_unavailable"


@dataclass(frozen=True, order=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        if not isinstance(value, str):
            raise ValueError("Version must be a semantic-version string.")
        match = SEMANTIC_VERSION_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("Version must use canonical major.minor.patch syntax.")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class VersionRange:
    minimum: str
    maximum: str

    def __post_init__(self) -> None:
        if SemanticVersion.parse(self.minimum) > SemanticVersion.parse(self.maximum):
            raise ValueError("Version range minimum must not exceed maximum.")


@dataclass(frozen=True)
class ProtocolLimits:
    default_upload_chunk_size_bytes: int
    max_upload_chunk_size_bytes: int
    max_upload_parts: int
    max_snapshot_segments: int
    max_update_events: int
    max_recording_context_characters: int


@dataclass(frozen=True)
class CapabilitiesDocument:
    worker_version: str
    protocol: VersionRange
    artifact_schema: VersionRange
    features: tuple[ProtocolCapability, ...]
    content_types: tuple[str, ...]
    model_profiles: tuple[str, ...]
    limits: ProtocolLimits

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CompatibilityRequest:
    protocol: VersionRange
    artifact_schema: VersionRange
    required_features: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompatibilityResult:
    compatible: bool
    protocol_version: str | None
    artifact_schema_version: str | None
    missing_features: tuple[str, ...]
    issues: tuple[CompatibilityIssue, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def get_capabilities() -> CapabilitiesDocument:
    return CapabilitiesDocument(
        worker_version=__version__,
        protocol=VersionRange(MIN_PROTOCOL_VERSION, MAX_PROTOCOL_VERSION),
        artifact_schema=VersionRange(
            MIN_ARTIFACT_SCHEMA_VERSION,
            MAX_ARTIFACT_SCHEMA_VERSION,
        ),
        features=SUPPORTED_CAPABILITIES,
        content_types=tuple(sorted(SUPPORTED_CONTENT_TYPES)),
        model_profiles=tuple(profile.value for profile in ModelProfile),
        limits=ProtocolLimits(
            default_upload_chunk_size_bytes=DEFAULT_UPLOAD_CHUNK_SIZE_BYTES,
            max_upload_chunk_size_bytes=MAX_UPLOAD_CHUNK_SIZE_BYTES,
            max_upload_parts=MAX_UPLOAD_PARTS,
            max_snapshot_segments=MAX_SNAPSHOT_SEGMENTS,
            max_update_events=MAX_UPDATE_EVENTS,
            max_recording_context_characters=MAX_RECORDING_CONTEXT_CHARACTERS,
        ),
    )


def negotiate_compatibility(request: CompatibilityRequest) -> CompatibilityResult:
    capabilities = get_capabilities()
    protocol_version = _highest_shared_version(request.protocol, capabilities.protocol)
    artifact_version = _highest_shared_version(
        request.artifact_schema,
        capabilities.artifact_schema,
    )
    supported = {feature.value for feature in capabilities.features}
    missing = tuple(
        feature for feature in dict.fromkeys(request.required_features) if feature not in supported
    )
    issues: list[CompatibilityIssue] = []
    if protocol_version is None:
        issues.append(CompatibilityIssue.PROTOCOL_VERSION_INCOMPATIBLE)
    if artifact_version is None:
        issues.append(CompatibilityIssue.ARTIFACT_SCHEMA_INCOMPATIBLE)
    if missing:
        issues.append(CompatibilityIssue.REQUIRED_CAPABILITY_UNAVAILABLE)
    return CompatibilityResult(
        compatible=not issues,
        protocol_version=protocol_version,
        artifact_schema_version=artifact_version,
        missing_features=missing,
        issues=tuple(issues),
    )


def _highest_shared_version(client: VersionRange, worker: VersionRange) -> str | None:
    minimum = max(
        SemanticVersion.parse(client.minimum),
        SemanticVersion.parse(worker.minimum),
    )
    maximum = min(
        SemanticVersion.parse(client.maximum),
        SemanticVersion.parse(worker.maximum),
    )
    return str(maximum) if minimum <= maximum else None
