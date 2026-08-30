"""Generated Worker protocol wire types. Do not edit manually."""

from typing import Final, Literal, NotRequired, TypeAlias, TypedDict

OPENAPI_SHA256: Final = "464e1155558ad4ca57acec210d42ea848371e0af959281126dbad6e4ebdc34f8"
OPENAPI_VERSION: Final = "3.1.0"
PROTOCOL_VERSION: Final = "1.0.0"

CompatibilityIssue: TypeAlias = Literal[
    'protocol_version_incompatible',
    'artifact_schema_incompatible',
    'required_capability_unavailable',
]

CorrectionField: TypeAlias = Literal[
    'transcript_text',
    'segment_review',
    'speaker_display_name',
    'recording_date',
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
    'background_summary_regeneration',
    'evidence_linked_artifacts',
    'publication_leases',
    'atomic_vault_publication',
    'review_audio_ranges',
    'worker_readiness',
    'job_data_deletion',
]

SpeakerLabelStatus: TypeAlias = Literal[
    'pending',
    'anonymous',
    'confirmed',
    'unavailable',
]

SummaryRevisionStatus: TypeAlias = Literal[
    'pending',
    'accepted',
    'rejected',
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

class ActivatedCredentialRotationSchema(TypedDict):
    activated_at: str
    credential_id: str
    device_id: str
    generation: int
    rotation_id: str

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

class CorrectionSchema(TypedDict):
    after: str
    author: str
    before: str | None
    correction_id: str
    created_at: str
    field: CorrectionField
    idempotency_key: str
    job_id: str
    job_revision: int
    sequence: int
    target_id: str | None

class CredentialRotationActivateRequestSchema(TypedDict):
    device_id: str

class CredentialRotationPrepareRequestSchema(TypedDict):
    ttl_seconds: NotRequired[int]

class DeviceRevocationResponse(TypedDict):
    device_id: str
    revoked: bool

class DiagnosticsSummaryResponse(TypedDict):
    authorized_vault_count: int
    job_state_counts: dict[str, int]
    protocol_version: str
    security_database_ok: bool
    visible_device_count: int
    visible_job_count: int
    worker_database_ok: bool
    worker_version: str

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
    recording_date: NotRequired[str | None]
    upload_id: str

class JobDeletionEnvelope(TypedDict):
    deleted_bytes: int
    job_id: str
    published_target_relative_path: str | None

class JobProgressDetailSchema(TypedDict):
    cache_hits: int
    completed_units: int
    input_tokens: int | None
    model_id: str | None
    output_tokens: int | None
    retry_attempt: int
    substage: str
    total_units: int

class JobSchema(TypedDict):
    content_type_override: str | None
    created_at: str
    job_id: str
    language_hint: str | None
    last_error_code: str | None
    last_error_message: str | None
    model_profile: ModelProfile
    recording_context: str | None
    recording_date: str | None
    revision: int
    source_audio_deleted_at: str | None
    source_audio_deleted_bytes: int
    source_audio_status: Literal['available', 'deleted']
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

class PairedDeviceSchema(TypedDict):
    allowed_vault_ids: list[str]
    created_at: str
    credential_id: str
    device_id: str
    generation: int
    last_used_at: str | None
    revoked_at: str | None

class PairingConfirmRequestSchema(TypedDict):
    pairing_code: NotRequired[str | None]
    pairing_ticket: NotRequired[str | None]
    session_id: NotRequired[str | None]

class PairingSessionCreateSchema(TypedDict):
    allowed_vault_ids: list[str]
    device_id: str
    ttl_seconds: NotRequired[int]

class PairingSessionSecretSchema(TypedDict):
    allowed_vault_ids: list[str]
    device_id: str
    expires_at: str
    pairing_code: str
    pairing_ticket: str
    session_id: str

class PreparedCredentialRotationSchema(TypedDict):
    bearer_token: str
    device_id: str
    expires_at: str
    generation: int
    rotation_id: str

class ProfileReadinessSchema(TypedDict):
    can_start: bool
    issue_codes: list[str]
    model_profile: ModelProfile
    state: Literal['ready', 'warning', 'blocked']

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

class PublicationAcknowledgementRequestSchema(TypedDict):
    lease_id: str
    manifest_sha256: str

class PublicationClaimRequestSchema(TypedDict):
    expected_revision: int
    lease_seconds: NotRequired[int]
    manifest_sha256: str
    target_relative_path: str

class PublicationLeaseSchema(TypedDict):
    expires_at: str
    generation: int
    lease_id: str
    manifest_sha256: str
    owned_by_caller: bool
    target_relative_path: str

class PublicationReceiptSchema(TypedDict):
    manifest_sha256: str
    published_at: str
    target_relative_path: str

class PublicationReleaseRequestSchema(TypedDict):
    lease_id: str

class ReviewAudioResponse(TypedDict):
    accept_ranges: Literal['bytes']
    bits_per_sample: int
    channels: int
    content_path: str
    duration_ms: int
    job_id: str
    media_type: Literal['audio/wav']
    retention: Literal['job_lifetime']
    sample_rate: int
    sha256: str
    size_bytes: int
    status: Literal['available']

class SegmentReviewRequestSchema(TypedDict):
    after_speaker_id: str | None
    after_text: str
    author: str
    before_speaker_id: str | None
    before_text: str
    expected_revision: int
    segment_id: str

class SpeakerDisplayNameRequestSchema(TypedDict):
    after: str
    author: str
    before: str
    expected_revision: int
    speaker_id: str

class SummaryRegenerationStatusSchema(TypedDict):
    elapsed_seconds: float
    error_code: str | None
    error_message: str | None
    finished_at: str | None
    phase: Literal['queued', 'preparing', 'synthesizing', 'quality_review', 'validating', 'completed', 'failed']
    request_id: str
    requested_at: str
    revision_key: str | None
    started_at: str | None
    state: Literal['queued', 'running', 'succeeded', 'failed']
    updated_at: str

class SummaryRevisionDecisionRequestSchema(TypedDict):
    decision: Literal['accepted', 'rejected']
    expected_revision: int

class SummaryRevisionDraftRequestSchema(TypedDict):
    expected_draft_version: int
    expected_revision: int
    markdown: str

class SummaryRevisionRegenerationRequestSchema(TypedDict):
    expected_revision: int

class SummaryRevisionSchema(TypedDict):
    after_document: dict[str, object] | None
    artifact_manifest_sha256: str | None
    base_version: int
    before_document: dict[str, object] | None
    candidate_version: int
    changed: bool
    created_at: str
    decided_at: str | None
    diff_truncated: bool
    draft_markdown: str | None
    draft_sha256: str | None
    draft_updated_at: str | None
    draft_version: int
    revision_key: str
    speaker_rename_count: int
    status: SummaryRevisionStatus
    text_correction_count: int

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

class CorrectionListResponse(TypedDict):
    corrections: list[CorrectionSchema]

class JobActionEnvelope(TypedDict):
    applied: bool
    job: JobSchema

class JobEnvelope(TypedDict):
    created: NotRequired[bool | None]
    job: JobSchema

class JobListResponse(TypedDict):
    jobs: list[JobSchema]

class JobProgressSchema(TypedDict):
    detail: JobProgressDetailSchema | None
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

class JobSourceAudioDeletionEnvelope(TypedDict):
    deleted: bool
    deleted_at: str
    deleted_bytes: int
    job: JobSchema

class JobUpdatesResponse(TypedDict):
    has_more: bool
    next_after_sequence: int
    updates: list[JobUpdateSchema]

class PairedDeviceListResponse(TypedDict):
    devices: list[PairedDeviceSchema]

class PublicationAcknowledgementEnvelope(TypedDict):
    created: bool
    job: JobSchema
    receipt: PublicationReceiptSchema

class PublicationClaimEnvelope(TypedDict):
    created: bool
    job: JobSchema
    lease: PublicationLeaseSchema

class PublicationReleaseEnvelope(TypedDict):
    job: JobSchema
    released: bool

class PublicationStatusResponse(TypedDict):
    active_lease: PublicationLeaseSchema | None
    artifact_count: int
    job: JobSchema
    manifest_sha256: str
    receipt: PublicationReceiptSchema | None
    suggested_target_relative_path: str

class SegmentReviewEnvelope(TypedDict):
    correction: CorrectionSchema
    created: bool
    job: JobSchema

class SpeakerDisplayNameEnvelope(TypedDict):
    correction: CorrectionSchema
    created: bool
    job: JobSchema

class SummaryRevisionDecisionEnvelope(TypedDict):
    applied: bool
    job: JobSchema
    revision: SummaryRevisionSchema

class SummaryRevisionDraftEnvelope(TypedDict):
    job: JobSchema
    revision: SummaryRevisionSchema
    saved: bool

class SummaryRevisionListResponse(TypedDict):
    can_regenerate: bool
    current_version: int
    manual_section_markdown: str
    regeneration: SummaryRegenerationStatusSchema | None
    revisions: list[SummaryRevisionSchema]

class SummaryRevisionRegenerationEnvelope(TypedDict):
    applied: bool
    job: JobSchema
    regeneration: SummaryRegenerationStatusSchema

class UploadEnvelope(TypedDict):
    created: NotRequired[bool | None]
    missing_part_numbers: list[int]
    upload: UploadSchema

class UploadPartEnvelope(TypedDict):
    created: bool
    part: UploadPartSchema

class WorkerReadinessResponse(TypedDict):
    active_model_profile: Literal['accuracy', 'speed', 'all'] | None
    checked_at: str
    disk_free_bytes: int
    disk_reserve_bytes: int
    disk_total_bytes: int
    endpoint_mode: Literal['local_only', 'private_tls']
    ffmpeg_available: bool
    ffprobe_available: bool
    issue_codes: list[str]
    memory_available_bytes: int
    memory_total_bytes: int
    memory_used_percent: float
    ollama_reachable: bool
    profiles: list[ProfileReadinessSchema]
    protocol_version: str
    schema_version: str
    security_database_ok: bool
    state: Literal['ready', 'warning', 'blocked']
    storage_ready: bool
    swap_used_bytes: int
    tls_enabled: bool
    worker_database_ok: bool
    worker_version: str

class JobSnapshotResponse(TypedDict):
    has_more_segments: bool
    job: JobSchema
    latest_event_sequence: int
    next_after_segment_sequence: int
    progress: JobProgressSchema | None
    provisional: ProvisionalTranscriptSchema | None
    resource_report: dict[str, object] | None
    stable_segments: list[TranscriptSegmentSchema]

__all__ = [
    "OPENAPI_SHA256",
    "OPENAPI_VERSION",
    "PROTOCOL_VERSION",
    "ActivatedCredentialRotationSchema",
    "ApiErrorResponse",
    "ApiErrorSchema",
    "ArtifactListResponse",
    "ArtifactSchema",
    "CapabilitiesResponse",
    "CompatibilityIssue",
    "CompatibilityRequestSchema",
    "CompatibilityResponse",
    "CorrectionField",
    "CorrectionListResponse",
    "CorrectionSchema",
    "CredentialRotationActivateRequestSchema",
    "CredentialRotationPrepareRequestSchema",
    "DeviceRevocationResponse",
    "DiagnosticsSummaryResponse",
    "DiarizationStatus",
    "HealthResponse",
    "IssuedDeviceCredentialSchema",
    "JobActionEnvelope",
    "JobActionRequestSchema",
    "JobCreateSchema",
    "JobDeletionEnvelope",
    "JobEnvelope",
    "JobListResponse",
    "JobProgressDetailSchema",
    "JobProgressSchema",
    "JobSchema",
    "JobSnapshotResponse",
    "JobSourceAudioDeletionEnvelope",
    "JobState",
    "JobUpdateSchema",
    "JobUpdatesResponse",
    "ModelProfile",
    "PairedDeviceListResponse",
    "PairedDeviceSchema",
    "PairingConfirmRequestSchema",
    "PairingSessionCreateSchema",
    "PairingSessionSecretSchema",
    "PreparedCredentialRotationSchema",
    "ProfileReadinessSchema",
    "ProtocolCapability",
    "ProtocolLimitsSchema",
    "ProvisionalTranscriptSchema",
    "PublicationAcknowledgementEnvelope",
    "PublicationAcknowledgementRequestSchema",
    "PublicationClaimEnvelope",
    "PublicationClaimRequestSchema",
    "PublicationLeaseSchema",
    "PublicationReceiptSchema",
    "PublicationReleaseEnvelope",
    "PublicationReleaseRequestSchema",
    "PublicationStatusResponse",
    "ReviewAudioResponse",
    "SegmentReviewEnvelope",
    "SegmentReviewRequestSchema",
    "SpeakerDisplayNameEnvelope",
    "SpeakerDisplayNameRequestSchema",
    "SpeakerLabelStatus",
    "SummaryRegenerationStatusSchema",
    "SummaryRevisionDecisionEnvelope",
    "SummaryRevisionDecisionRequestSchema",
    "SummaryRevisionDraftEnvelope",
    "SummaryRevisionDraftRequestSchema",
    "SummaryRevisionListResponse",
    "SummaryRevisionRegenerationEnvelope",
    "SummaryRevisionRegenerationRequestSchema",
    "SummaryRevisionSchema",
    "SummaryRevisionStatus",
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
    "WorkerReadinessResponse",
]
