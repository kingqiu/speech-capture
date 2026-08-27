"""Strict public schemas for the versioned Worker API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from speech_capture_worker.corrections import CorrectionField
from speech_capture_worker.domain import JobState, ModelProfile, UploadState
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.protocol_contract import (
    SEMANTIC_VERSION_PATTERN,
    CompatibilityIssue,
    ProtocolCapability,
    SemanticVersion,
)
from speech_capture_worker.recording_context import MAX_RECORDING_CONTEXT_CHARACTERS
from speech_capture_worker.recording_metadata import normalize_recording_date
from speech_capture_worker.summary_revisions import SummaryRevisionStatus
from speech_capture_worker.transcript import (
    DiarizationStatus,
    SpeakerLabelStatus,
    TranscriptOutcome,
    TranscriptTimingStatus,
)

VersionString = Annotated[str, Field(pattern=SEMANTIC_VERSION_PATTERN.pattern)]
CapabilityName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
SafeIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Sha256String = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
JobIdentifier = Annotated[str, Field(pattern=r"^job_[0-9a-f]{32}$")]
UploadIdentifier = Annotated[str, Field(pattern=r"^upl_[0-9a-f]{32}$")]
SegmentIdentifier = Annotated[str, Field(pattern=r"^seg_[0-9]{8}$")]
ArtifactName = Literal[
    "transcript.raw.json",
    "transcript.md",
    "speech-record.json",
    "note.md",
    "note.evidence.md",
    "timeline.md",
    "artifact-manifest.json",
]


class PublicSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionRangeSchema(PublicSchema):
    minimum: VersionString
    maximum: VersionString

    @model_validator(mode="after")
    def validate_order(self) -> VersionRangeSchema:
        if SemanticVersion.parse(self.minimum) > SemanticVersion.parse(self.maximum):
            raise ValueError("Version range minimum must not exceed maximum.")
        return self


class ProtocolLimitsSchema(PublicSchema):
    default_upload_chunk_size_bytes: int = Field(gt=0)
    max_upload_chunk_size_bytes: int = Field(gt=0)
    max_upload_parts: int = Field(gt=0)
    max_snapshot_segments: int = Field(gt=0)
    max_update_events: int = Field(gt=0)
    max_recording_context_characters: int = Field(gt=0)


class HealthResponse(PublicSchema):
    status: Literal["ok"]
    worker_version: str
    protocol_version: VersionString


class CapabilitiesResponse(PublicSchema):
    worker_version: str
    protocol: VersionRangeSchema
    artifact_schema: VersionRangeSchema
    features: tuple[ProtocolCapability, ...]
    content_types: tuple[str, ...]
    model_profiles: tuple[str, ...]
    limits: ProtocolLimitsSchema


class CompatibilityRequestSchema(PublicSchema):
    protocol: VersionRangeSchema
    artifact_schema: VersionRangeSchema
    required_features: tuple[CapabilityName, ...] = Field(
        default=(),
        max_length=64,
    )

    @field_validator("required_features")
    @classmethod
    def reject_duplicate_features(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("required_features must not contain duplicates.")
        return value


class CompatibilityResponse(PublicSchema):
    compatible: bool
    protocol_version: VersionString | None
    artifact_schema_version: VersionString | None
    missing_features: tuple[CapabilityName, ...]
    issues: tuple[CompatibilityIssue, ...]


class ApiErrorSchema(PublicSchema):
    code: str
    message: str
    request_id: str


class ApiErrorResponse(PublicSchema):
    error: ApiErrorSchema


class PairingConfirmRequestSchema(PublicSchema):
    pairing_ticket: str | None = Field(
        default=None,
        min_length=1,
        max_length=192,
        pattern=r"^scpair1\.[0-9a-f]{32}\.[A-Za-z0-9_-]+$",
    )
    session_id: str | None = Field(default=None, pattern=r"^pair_[0-9a-f]{32}$")
    pairing_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_pairing_input(self) -> PairingConfirmRequestSchema:
        has_ticket = self.pairing_ticket is not None
        has_legacy = self.session_id is not None or self.pairing_code is not None
        legacy_complete = self.session_id is not None and self.pairing_code is not None
        if has_ticket == has_legacy or (has_legacy and not legacy_complete):
            raise ValueError("Provide one pairing ticket or the complete legacy session and code.")
        return self


class PairingSessionCreateSchema(PublicSchema):
    device_id: SafeIdentifier
    allowed_vault_ids: tuple[SafeIdentifier, ...] = Field(min_length=1, max_length=64)
    ttl_seconds: int = Field(default=300, ge=30, le=900)


class PairingSessionSecretSchema(PublicSchema):
    session_id: str = Field(pattern=r"^pair_[0-9a-f]{32}$")
    pairing_code: str
    pairing_ticket: str = Field(
        min_length=1,
        max_length=192,
        pattern=r"^scpair1\.[0-9a-f]{32}\.[A-Za-z0-9_-]+$",
    )
    device_id: SafeIdentifier
    allowed_vault_ids: tuple[SafeIdentifier, ...]
    expires_at: str


class IssuedDeviceCredentialSchema(PublicSchema):
    credential_id: str = Field(pattern=r"^cred_[0-9a-f]{32}$")
    device_id: SafeIdentifier
    bearer_token: str = Field(min_length=32, max_length=512)
    allowed_vault_ids: tuple[SafeIdentifier, ...]
    generation: int = Field(gt=0)
    created_at: str


class PairedDeviceSchema(PublicSchema):
    credential_id: str = Field(pattern=r"^cred_[0-9a-f]{32}$")
    device_id: SafeIdentifier
    allowed_vault_ids: tuple[SafeIdentifier, ...]
    generation: int = Field(gt=0)
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class PairedDeviceListResponse(PublicSchema):
    devices: tuple[PairedDeviceSchema, ...]


class DeviceRevocationResponse(PublicSchema):
    device_id: SafeIdentifier
    revoked: bool


class CredentialRotationPrepareRequestSchema(PublicSchema):
    ttl_seconds: int = Field(default=600, ge=60, le=3600)


class PreparedCredentialRotationSchema(PublicSchema):
    rotation_id: str = Field(pattern=r"^rot_[0-9a-f]{32}$")
    device_id: SafeIdentifier
    bearer_token: str
    generation: int = Field(gt=1)
    expires_at: str


class CredentialRotationActivateRequestSchema(PublicSchema):
    device_id: SafeIdentifier


class ActivatedCredentialRotationSchema(PublicSchema):
    rotation_id: str = Field(pattern=r"^rot_[0-9a-f]{32}$")
    device_id: SafeIdentifier
    credential_id: str = Field(pattern=r"^cred_[0-9a-f]{32}$")
    generation: int = Field(gt=1)
    activated_at: str


class DiagnosticsSummaryResponse(PublicSchema):
    worker_version: str
    protocol_version: str
    worker_database_ok: bool
    security_database_ok: bool
    authorized_vault_count: int = Field(ge=0)
    visible_device_count: int = Field(ge=0)
    visible_job_count: int = Field(ge=0)
    job_state_counts: dict[str, int]


class UploadCreateSchema(PublicSchema):
    vault_id: SafeIdentifier
    source_display_name: str = Field(min_length=1, max_length=255)
    source_sha256: Sha256String
    source_size_bytes: int = Field(gt=0, le=2**63 - 1)
    media_type: str = Field(
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
            r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
        )
    )


class UploadSchema(PublicSchema):
    upload_id: UploadIdentifier
    vault_id: SafeIdentifier
    source_display_name: str
    source_sha256: Sha256String
    source_size_bytes: int
    media_type: str
    state: UploadState
    chunk_size_bytes: int
    part_count: int
    received_part_count: int
    received_bytes: int
    duration_seconds: float | None
    audio_stream_count: int | None
    detected_format_name: str | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class UploadEnvelope(PublicSchema):
    upload: UploadSchema
    created: bool | None = None
    missing_part_numbers: tuple[int, ...]


class UploadPartSchema(PublicSchema):
    upload_id: UploadIdentifier
    part_number: int
    size_bytes: int
    sha256: Sha256String
    created_at: str
    updated_at: str


class UploadPartEnvelope(PublicSchema):
    part: UploadPartSchema
    created: bool


class JobCreateSchema(PublicSchema):
    upload_id: UploadIdentifier
    model_profile: ModelProfile = ModelProfile.ACCURACY
    language_hint: str | None = Field(default=None, min_length=1, max_length=64)
    content_type_override: str | None = None
    recording_context: str | None = Field(
        default=None,
        max_length=MAX_RECORDING_CONTEXT_CHARACTERS,
    )
    recording_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("recording_date")
    @classmethod
    def validate_recording_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_recording_date(value)
        except InvalidJobRequest as exc:
            raise ValueError("recording_date must be a valid calendar date.") from exc


class JobSchema(PublicSchema):
    job_id: JobIdentifier
    vault_id: SafeIdentifier
    source_upload_id: UploadIdentifier | None
    source_display_name: str
    source_sha256: Sha256String
    source_size_bytes: int
    state: JobState
    model_profile: ModelProfile
    language_hint: str | None
    content_type_override: str | None
    recording_context: str | None
    recording_date: str | None
    revision: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str
    source_audio_status: Literal["available", "deleted"]
    source_audio_deleted_at: str | None
    source_audio_deleted_bytes: int = Field(ge=0)


class JobEnvelope(PublicSchema):
    job: JobSchema
    created: bool | None = None


class JobActionRequestSchema(PublicSchema):
    expected_revision: int = Field(ge=0)


class JobActionEnvelope(PublicSchema):
    job: JobSchema
    applied: bool


class JobSourceAudioDeletionEnvelope(PublicSchema):
    job: JobSchema
    deleted: bool
    deleted_bytes: int = Field(ge=0)
    deleted_at: str


class JobDeletionEnvelope(PublicSchema):
    job_id: JobIdentifier
    deleted_bytes: int = Field(ge=0)
    published_target_relative_path: str | None


class JobListResponse(PublicSchema):
    jobs: tuple[JobSchema, ...]


class JobProgressDetailSchema(PublicSchema):
    substage: str
    completed_units: int
    total_units: int
    cache_hits: int
    retry_attempt: int
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None


class JobProgressSchema(PublicSchema):
    job_id: JobIdentifier
    generation: int
    stage: JobState
    processed_ms: int
    duration_ms: int
    stage_progress: float
    elapsed_seconds: float
    estimated_remaining_seconds: float | None
    diarization_status: DiarizationStatus
    detail: JobProgressDetailSchema | None
    updated_at: str


class TranscriptSegmentSchema(PublicSchema):
    job_id: JobIdentifier
    segment_sequence: int
    segment_id: SegmentIdentifier
    revision: int
    start_ms: int
    end_ms: int
    outcome: TranscriptOutcome
    text: str | None
    language: str | None
    confidence: float | None
    timing_status: TranscriptTimingStatus
    speaker_id: str | None
    speaker_label_status: SpeakerLabelStatus
    error_code: str | None
    created_at: str
    updated_at: str


class ProvisionalTranscriptSchema(PublicSchema):
    job_id: JobIdentifier
    generation: int
    start_ms: int
    end_ms: int
    text: str
    language: str | None
    updated_at: str


class JobSnapshotResponse(PublicSchema):
    job: JobSchema
    progress: JobProgressSchema | None
    stable_segments: tuple[TranscriptSegmentSchema, ...]
    provisional: ProvisionalTranscriptSchema | None
    resource_report: dict[str, object] | None
    latest_event_sequence: int
    next_after_segment_sequence: int
    has_more_segments: bool


class CorrectionSchema(PublicSchema):
    sequence: int = Field(gt=0)
    correction_id: SafeIdentifier
    job_id: JobIdentifier
    job_revision: int = Field(ge=0)
    field: CorrectionField
    target_id: SafeIdentifier | None
    before: str | None
    after: str
    author: str
    idempotency_key: str
    created_at: str


class CorrectionListResponse(PublicSchema):
    corrections: tuple[CorrectionSchema, ...]


class SegmentReviewRequestSchema(PublicSchema):
    expected_revision: int = Field(ge=0)
    segment_id: SegmentIdentifier
    before_text: str = Field(min_length=1)
    after_text: str = Field(min_length=1)
    before_speaker_id: SafeIdentifier | None
    after_speaker_id: SafeIdentifier | None
    author: str = Field(min_length=1, max_length=200)


class SegmentReviewEnvelope(PublicSchema):
    job: JobSchema
    correction: CorrectionSchema
    created: bool


class SpeakerDisplayNameRequestSchema(PublicSchema):
    expected_revision: int = Field(ge=0)
    speaker_id: SafeIdentifier
    before: str = Field(min_length=1, max_length=200)
    after: str = Field(min_length=1, max_length=200)
    author: str = Field(min_length=1, max_length=200)


class SpeakerDisplayNameEnvelope(PublicSchema):
    job: JobSchema
    correction: CorrectionSchema
    created: bool


class SummaryRevisionSchema(PublicSchema):
    revision_key: SafeIdentifier
    base_version: int = Field(gt=0)
    candidate_version: int = Field(gt=0)
    status: SummaryRevisionStatus
    changed: bool
    text_correction_count: int = Field(ge=0)
    speaker_rename_count: int = Field(ge=0)
    before_document: dict[str, object] | None
    after_document: dict[str, object] | None
    diff_truncated: bool
    created_at: str
    decided_at: str | None
    artifact_manifest_sha256: Sha256String | None
    draft_markdown: str | None
    draft_version: int = Field(ge=0)
    draft_updated_at: str | None
    draft_sha256: Sha256String | None


class SummaryRevisionListResponse(PublicSchema):
    revisions: tuple[SummaryRevisionSchema, ...]
    current_version: int = Field(gt=0)
    manual_section_markdown: str
    can_regenerate: bool


class SummaryRevisionRegenerationRequestSchema(PublicSchema):
    expected_revision: int = Field(ge=0)


class SummaryRevisionRegenerationEnvelope(PublicSchema):
    job: JobSchema
    revision: SummaryRevisionSchema
    applied: bool


class SummaryRevisionDecisionRequestSchema(PublicSchema):
    expected_revision: int = Field(ge=0)
    decision: Literal["accepted", "rejected"]


class SummaryRevisionDecisionEnvelope(PublicSchema):
    job: JobSchema
    revision: SummaryRevisionSchema
    applied: bool


class SummaryRevisionDraftRequestSchema(PublicSchema):
    expected_revision: int = Field(ge=0)
    expected_draft_version: int = Field(ge=0)
    markdown: str = Field(min_length=1, max_length=2_000_000)


class SummaryRevisionDraftEnvelope(PublicSchema):
    job: JobSchema
    revision: SummaryRevisionSchema
    saved: bool


class JobUpdateSchema(PublicSchema):
    sequence: int
    job_id: JobIdentifier
    job_revision: int
    event_type: str
    payload: dict[str, object]
    created_at: str


class JobUpdatesResponse(PublicSchema):
    updates: tuple[JobUpdateSchema, ...]
    has_more: bool
    next_after_sequence: int


class ArtifactSchema(PublicSchema):
    name: ArtifactName
    media_type: Literal["text/markdown", "application/json"]
    size_bytes: int
    sha256: Sha256String
    download_path: str


class ArtifactListResponse(PublicSchema):
    job_id: JobIdentifier
    speech_id: str
    manifest_sha256: Sha256String
    artifacts: tuple[ArtifactSchema, ...]


class PublicationLeaseSchema(PublicSchema):
    lease_id: str = Field(pattern=r"^lease_[0-9a-f]{32}$")
    generation: int = Field(gt=0)
    target_relative_path: str = Field(min_length=1, max_length=1024)
    manifest_sha256: Sha256String
    expires_at: str
    owned_by_caller: bool


class PublicationReceiptSchema(PublicSchema):
    target_relative_path: str = Field(min_length=1, max_length=1024)
    manifest_sha256: Sha256String
    published_at: str


class PublicationStatusResponse(PublicSchema):
    job: JobSchema
    suggested_target_relative_path: str = Field(min_length=1, max_length=1024)
    manifest_sha256: Sha256String
    artifact_count: int = Field(gt=0)
    active_lease: PublicationLeaseSchema | None
    receipt: PublicationReceiptSchema | None


class PublicationClaimRequestSchema(PublicSchema):
    expected_revision: int = Field(ge=0)
    target_relative_path: str = Field(min_length=1, max_length=1024)
    manifest_sha256: Sha256String
    lease_seconds: int = Field(default=120, ge=30, le=900)


class PublicationClaimEnvelope(PublicSchema):
    job: JobSchema
    lease: PublicationLeaseSchema
    created: bool


class PublicationReleaseRequestSchema(PublicSchema):
    lease_id: str = Field(pattern=r"^lease_[0-9a-f]{32}$")


class PublicationReleaseEnvelope(PublicSchema):
    job: JobSchema
    released: bool


class PublicationAcknowledgementRequestSchema(PublicSchema):
    lease_id: str = Field(pattern=r"^lease_[0-9a-f]{32}$")
    manifest_sha256: Sha256String


class PublicationAcknowledgementEnvelope(PublicSchema):
    job: JobSchema
    receipt: PublicationReceiptSchema
    created: bool


class ReviewAudioResponse(PublicSchema):
    job_id: JobIdentifier
    status: Literal["available"]
    media_type: Literal["audio/wav"]
    size_bytes: int = Field(gt=0)
    sha256: Sha256String
    duration_ms: int = Field(gt=0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    bits_per_sample: int = Field(gt=0)
    accept_ranges: Literal["bytes"]
    content_path: str
    retention: Literal["job_lifetime"]


class ProfileReadinessSchema(PublicSchema):
    model_profile: ModelProfile
    state: Literal["ready", "warning", "blocked"]
    can_start: bool
    issue_codes: tuple[str, ...]


class WorkerReadinessResponse(PublicSchema):
    schema_version: VersionString
    checked_at: str
    worker_version: str
    protocol_version: VersionString
    state: Literal["ready", "warning", "blocked"]
    endpoint_mode: Literal["local_only", "private_tls"]
    tls_enabled: bool
    storage_ready: bool
    worker_database_ok: bool
    security_database_ok: bool
    ffmpeg_available: bool
    ffprobe_available: bool
    ollama_reachable: bool
    active_model_profile: Literal["accuracy", "speed", "all"] | None
    disk_total_bytes: int = Field(ge=0)
    disk_free_bytes: int = Field(ge=0)
    disk_reserve_bytes: int = Field(ge=0)
    memory_total_bytes: int = Field(ge=0)
    memory_available_bytes: int = Field(ge=0)
    memory_used_percent: float = Field(ge=0, le=100)
    swap_used_bytes: int = Field(ge=0)
    profiles: tuple[ProfileReadinessSchema, ...]
    issue_codes: tuple[str, ...]
