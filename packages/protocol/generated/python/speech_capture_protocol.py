"""Generated Worker protocol wire types. Do not edit manually."""

from typing import Final, Literal, NotRequired, TypeAlias, TypedDict

OPENAPI_SHA256: Final = "686cc2f1e8ad8052fff0c946cda3dbb96e2ef00263261dfc1138aba7797ba58e"
OPENAPI_VERSION: Final = "3.1.0"
PROTOCOL_VERSION: Final = "1.0.0"

CompatibilityIssue: TypeAlias = Literal[
    'protocol_version_incompatible',
    'artifact_schema_incompatible',
    'required_capability_unavailable',
]

ProtocolCapability: TypeAlias = Literal[
    'resumable_uploads',
    'job_snapshots',
    'bounded_event_updates',
    'progressive_transcript',
    'recording_context',
    'content_type_override',
    'correction_ledger',
    'summary_revisions',
    'evidence_linked_artifacts',
    'publication_leases',
    'atomic_vault_publication',
]

class CompatibilityResponse(TypedDict):
    artifact_schema_version: str | None
    compatible: bool
    issues: list[CompatibilityIssue]
    missing_features: list[str]
    protocol_version: str | None

class HealthResponse(TypedDict):
    protocol_version: str
    status: Literal['ok']
    worker_version: str

class ProtocolLimitsSchema(TypedDict):
    default_upload_chunk_size_bytes: int
    max_recording_context_characters: int
    max_snapshot_segments: int
    max_update_events: int
    max_upload_chunk_size_bytes: int
    max_upload_parts: int

class ValidationError(TypedDict):
    ctx: NotRequired[dict[str, object]]
    input: NotRequired[object]
    loc: list[str | int]
    msg: str
    type: str

class VersionRangeSchema(TypedDict):
    maximum: str
    minimum: str

class CapabilitiesResponse(TypedDict):
    artifact_schema: VersionRangeSchema
    content_types: list[str]
    features: list[ProtocolCapability]
    limits: ProtocolLimitsSchema
    model_profiles: list[str]
    protocol: VersionRangeSchema
    worker_version: str

class CompatibilityRequestSchema(TypedDict):
    artifact_schema: VersionRangeSchema
    protocol: VersionRangeSchema
    required_features: NotRequired[list[str]]

class HTTPValidationError(TypedDict):
    detail: NotRequired[list[ValidationError]]

__all__ = [
    "OPENAPI_SHA256",
    "OPENAPI_VERSION",
    "PROTOCOL_VERSION",
    "CapabilitiesResponse",
    "CompatibilityIssue",
    "CompatibilityRequestSchema",
    "CompatibilityResponse",
    "HTTPValidationError",
    "HealthResponse",
    "ProtocolCapability",
    "ProtocolLimitsSchema",
    "ValidationError",
    "VersionRangeSchema",
]
