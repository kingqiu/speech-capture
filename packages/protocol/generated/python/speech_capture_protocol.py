"""Generated Worker protocol wire types. Do not edit manually."""

from typing import Final, Literal, NotRequired, TypeAlias, TypedDict

OPENAPI_SHA256: Final = "f693de0586e4bfdf685d671c30a6964e63421ea22078ba34b135566656571825"
OPENAPI_VERSION: Final = "3.1.0"
PROTOCOL_VERSION: Final = "1.0.0"

CompatibilityIssue: TypeAlias = Literal[
    'protocol_version_incompatible',
    'artifact_schema_incompatible',
    'required_capability_unavailable',
]

DiarizationStatus: TypeAlias = Literal[
    'not_started',
    'processing',
    'ready',
    'unavailable',
]

JobState: TypeAlias = Literal[
    'created',
    'uploading',
    'verifying',
    'queued',
    'preprocessing',
    'transcribing',
    'aligning',
    'diarizing',
    'structuring',
    'quality_check',
    'processed',
    'publishing',
    'published',
    'paused',
    'waiting_user',
    'partial',
    'failed',
    'cancelled',
]

ModelProfile: TypeAlias = Literal[
    'accuracy',
    'speed',
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

SpeakerLabelStatus: TypeAlias = Literal[
    'pending',
    'anonymous',
    'confirmed',
    'unavailable',
]

TranscriptOutcome: TypeAlias = Literal[
    'transcribed',
    'inaudible',
    'non_speech',
    'failed',
]

TranscriptTimingStatus: TypeAlias = Literal[
    'estimated',
    'aligned',
]

UploadState: TypeAlias = Literal[
    'uploading',
    'verifying',
    'complete',
    'failed',
]

class ApiErrorSchema(TypedDict):
    code: str
    message: str
    request_id: str

class ArtifactSchema(TypedDict):
    download_path: str
    media_type: Literal['text/markdown', 'application/json']
    name: Literal['transcript.raw.json', 'transcript.md', 'speech-record.json', 'note.md', 'note.evidence.md', 'timeline.md', 'artifact-manifest.json']
    sha256: str
    size_bytes: int

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

class IssuedDeviceCredentialSchema(TypedDict):
    allowed_vault_ids: list[str]
    bearer_token: str
    created_at: str
    credential_id: str
    device_id: str
    generation: int

class JobActionRequestSchema(TypedDict):
    expected_revision: int

class JobCreateSchema(TypedDict):
    content_type_override: NotRequired[str | None]
    language_hint: NotRequired[str | None]
    model_profile: NotRequired[ModelProfile]
    recording_context: NotRequired[str | None]
    upload_id: str

class JobProgressSchema(TypedDict):
    diarization_status: DiarizationStatus
    duration_ms: int
    elapsed_seconds: float
    estimated_remaining_seconds: float | None
    generation: int
    job_id: str
    processed_ms: int
    stage: JobState
    stage_progress: float
    updated_at: str

class JobSchema(TypedDict):
    content_type_override: str | None
    created_at: str
    job_id: str
    language_hint: str | None
    last_error_code: str | None
    last_error_message: str | None
    model_profile: ModelProfile
    recording_context: str | None
    revision: int
    source_display_name: str
    source_sha256: str
    source_size_bytes: int
    source_upload_id: str | None
    state: JobState
    updated_at: str
    vault_id: str

class JobUpdateSchema(TypedDict):
    created_at: str
    event_type: str
    job_id: str
    job_revision: int
    payload: dict[str, object]
    sequence: int

class PairingConfirmRequestSchema(TypedDict):
    pairing_code: str
    session_id: str

class ProtocolLimitsSchema(TypedDict):
    default_upload_chunk_size_bytes: int
    max_recording_context_characters: int
    max_snapshot_segments: int
    max_update_events: int
    max_upload_chunk_size_bytes: int
    max_upload_parts: int

class ProvisionalTranscriptSchema(TypedDict):
    end_ms: int
    generation: int
    job_id: str
    language: str | None
    start_ms: int
    text: str
    updated_at: str

class TranscriptSegmentSchema(TypedDict):
    confidence: float | None
    created_at: str
    end_ms: int
    error_code: str | None
    job_id: str
    language: str | None
    outcome: TranscriptOutcome
    revision: int
    segment_id: str
    segment_sequence: int
    speaker_id: str | None
    speaker_label_status: SpeakerLabelStatus
    start_ms: int
    text: str | None
    timing_status: TranscriptTimingStatus
    updated_at: str

class UploadCreateSchema(TypedDict):
    media_type: str
    source_display_name: str
    source_sha256: str
    source_size_bytes: int
    vault_id: str

class UploadPartSchema(TypedDict):
    created_at: str
    part_number: int
    sha256: str
    size_bytes: int
    updated_at: str
    upload_id: str

class UploadSchema(TypedDict):
    audio_stream_count: int | None
    chunk_size_bytes: int
    completed_at: str | None
    created_at: str
    detected_format_name: str | None
    duration_seconds: float | None
    last_error_code: str | None
    last_error_message: str | None
    media_type: str
    part_count: int
    received_bytes: int
    received_part_count: int
    source_display_name: str
    source_sha256: str
    source_size_bytes: int
    state: UploadState
    updated_at: str
    upload_id: str
    vault_id: str

class VersionRangeSchema(TypedDict):
    maximum: str
    minimum: str

class ApiErrorResponse(TypedDict):
    error: ApiErrorSchema

class ArtifactListResponse(TypedDict):
    artifacts: list[ArtifactSchema]
    job_id: str
    manifest_sha256: str
    speech_id: str

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

class JobActionEnvelope(TypedDict):
    applied: bool
    job: JobSchema

class JobEnvelope(TypedDict):
    created: NotRequired[bool | None]
    job: JobSchema

class JobListResponse(TypedDict):
    jobs: list[JobSchema]

class JobSnapshotResponse(TypedDict):
    has_more_segments: bool
    job: JobSchema
    latest_event_sequence: int
    next_after_segment_sequence: int
    progress: JobProgressSchema | None
    provisional: ProvisionalTranscriptSchema | None
    resource_report: dict[str, object] | None
    stable_segments: list[TranscriptSegmentSchema]

class JobUpdatesResponse(TypedDict):
    has_more: bool
    next_after_sequence: int
    updates: list[JobUpdateSchema]

class UploadEnvelope(TypedDict):
    created: NotRequired[bool | None]
    missing_part_numbers: list[int]
    upload: UploadSchema

class UploadPartEnvelope(TypedDict):
    created: bool
    part: UploadPartSchema

__all__ = [
    "OPENAPI_SHA256",
    "OPENAPI_VERSION",
    "PROTOCOL_VERSION",
    "ApiErrorResponse",
    "ApiErrorSchema",
    "ArtifactListResponse",
    "ArtifactSchema",
    "CapabilitiesResponse",
    "CompatibilityIssue",
    "CompatibilityRequestSchema",
    "CompatibilityResponse",
    "DiarizationStatus",
    "HealthResponse",
    "IssuedDeviceCredentialSchema",
    "JobActionEnvelope",
    "JobActionRequestSchema",
    "JobCreateSchema",
    "JobEnvelope",
    "JobListResponse",
    "JobProgressSchema",
    "JobSchema",
    "JobSnapshotResponse",
    "JobState",
    "JobUpdateSchema",
    "JobUpdatesResponse",
    "ModelProfile",
    "PairingConfirmRequestSchema",
    "ProtocolCapability",
    "ProtocolLimitsSchema",
    "ProvisionalTranscriptSchema",
    "SpeakerLabelStatus",
    "TranscriptOutcome",
    "TranscriptSegmentSchema",
    "TranscriptTimingStatus",
    "UploadCreateSchema",
    "UploadEnvelope",
    "UploadPartEnvelope",
    "UploadPartSchema",
    "UploadSchema",
    "UploadState",
    "VersionRangeSchema",
]
