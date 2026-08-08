// Generated Worker protocol wire types. Do not edit manually.
export const OPENAPI_SHA256 = "c5a2bbe8b41715997dd1f99be4a60f89c09181c18c2eabaf8e69617b323d926c" as const;
export const OPENAPI_VERSION = "3.1.0" as const;
export const PROTOCOL_VERSION = "1.0.0" as const;

export interface ActivatedCredentialRotationSchema {
  readonly activated_at: string;
  readonly credential_id: string;
  readonly device_id: string;
  readonly generation: number;
  readonly rotation_id: string;
}

export interface ApiErrorResponse {
  readonly error: ApiErrorSchema;
}

export interface ApiErrorSchema {
  readonly code: string;
  readonly message: string;
  readonly request_id: string;
}

export interface ArtifactListResponse {
  readonly artifacts: ReadonlyArray<ArtifactSchema>;
  readonly job_id: string;
  readonly manifest_sha256: string;
  readonly speech_id: string;
}

export interface ArtifactSchema {
  readonly download_path: string;
  readonly media_type: "text/markdown" | "application/json";
  readonly name: "transcript.raw.json" | "transcript.md" | "speech-record.json" | "note.md" | "note.evidence.md" | "timeline.md" | "artifact-manifest.json";
  readonly sha256: string;
  readonly size_bytes: number;
}

export interface CapabilitiesResponse {
  readonly artifact_schema: VersionRangeSchema;
  readonly content_types: ReadonlyArray<string>;
  readonly features: ReadonlyArray<ProtocolCapability>;
  readonly limits: ProtocolLimitsSchema;
  readonly model_profiles: ReadonlyArray<string>;
  readonly protocol: VersionRangeSchema;
  readonly worker_version: string;
}

export type CompatibilityIssue =
  | "protocol_version_incompatible"
  | "artifact_schema_incompatible"
  | "required_capability_unavailable";

export interface CompatibilityRequestSchema {
  readonly artifact_schema: VersionRangeSchema;
  readonly protocol: VersionRangeSchema;
  readonly required_features?: ReadonlyArray<string>;
}

export interface CompatibilityResponse {
  readonly artifact_schema_version: string | null;
  readonly compatible: boolean;
  readonly issues: ReadonlyArray<CompatibilityIssue>;
  readonly missing_features: ReadonlyArray<string>;
  readonly protocol_version: string | null;
}

export type CorrectionField =
  | "transcript_text"
  | "segment_review"
  | "speaker_display_name"
  | "recording_date";

export interface CorrectionListResponse {
  readonly corrections: ReadonlyArray<CorrectionSchema>;
}

export interface CorrectionSchema {
  readonly after: string;
  readonly author: string;
  readonly before: string | null;
  readonly correction_id: string;
  readonly created_at: string;
  readonly field: CorrectionField;
  readonly idempotency_key: string;
  readonly job_id: string;
  readonly job_revision: number;
  readonly sequence: number;
  readonly target_id: string | null;
}

export interface CredentialRotationActivateRequestSchema {
  readonly device_id: string;
}

export interface CredentialRotationPrepareRequestSchema {
  readonly ttl_seconds?: number;
}

export interface DeviceRevocationResponse {
  readonly device_id: string;
  readonly revoked: boolean;
}

export interface DiagnosticsSummaryResponse {
  readonly authorized_vault_count: number;
  readonly job_state_counts: Readonly<Record<string, number>>;
  readonly protocol_version: string;
  readonly security_database_ok: boolean;
  readonly visible_device_count: number;
  readonly visible_job_count: number;
  readonly worker_database_ok: boolean;
  readonly worker_version: string;
}

export type DiarizationStatus =
  | "not_started"
  | "processing"
  | "ready"
  | "unavailable";

export interface HealthResponse {
  readonly protocol_version: string;
  readonly status: "ok";
  readonly worker_version: string;
}

export interface IssuedDeviceCredentialSchema {
  readonly allowed_vault_ids: ReadonlyArray<string>;
  readonly bearer_token: string;
  readonly created_at: string;
  readonly credential_id: string;
  readonly device_id: string;
  readonly generation: number;
}

export interface JobActionEnvelope {
  readonly applied: boolean;
  readonly job: JobSchema;
}

export interface JobActionRequestSchema {
  readonly expected_revision: number;
}

export interface JobCreateSchema {
  readonly content_type_override?: string | null;
  readonly language_hint?: string | null;
  readonly model_profile?: ModelProfile;
  readonly recording_context?: string | null;
  readonly recording_date?: string | null;
  readonly upload_id: string;
}

export interface JobEnvelope {
  readonly created?: boolean | null;
  readonly job: JobSchema;
}

export interface JobListResponse {
  readonly jobs: ReadonlyArray<JobSchema>;
}

export interface JobProgressSchema {
  readonly diarization_status: DiarizationStatus;
  readonly duration_ms: number;
  readonly elapsed_seconds: number;
  readonly estimated_remaining_seconds: number | null;
  readonly generation: number;
  readonly job_id: string;
  readonly processed_ms: number;
  readonly stage: JobState;
  readonly stage_progress: number;
  readonly updated_at: string;
}

export interface JobSchema {
  readonly content_type_override: string | null;
  readonly created_at: string;
  readonly job_id: string;
  readonly language_hint: string | null;
  readonly last_error_code: string | null;
  readonly last_error_message: string | null;
  readonly model_profile: ModelProfile;
  readonly recording_context: string | null;
  readonly recording_date: string | null;
  readonly revision: number;
  readonly source_display_name: string;
  readonly source_sha256: string;
  readonly source_size_bytes: number;
  readonly source_upload_id: string | null;
  readonly state: JobState;
  readonly updated_at: string;
  readonly vault_id: string;
}

export interface JobSnapshotResponse {
  readonly has_more_segments: boolean;
  readonly job: JobSchema;
  readonly latest_event_sequence: number;
  readonly next_after_segment_sequence: number;
  readonly progress: JobProgressSchema | null;
  readonly provisional: ProvisionalTranscriptSchema | null;
  readonly resource_report: Readonly<Record<string, unknown>> | null;
  readonly stable_segments: ReadonlyArray<TranscriptSegmentSchema>;
}

export type JobState =
  | "created"
  | "uploading"
  | "verifying"
  | "queued"
  | "preprocessing"
  | "transcribing"
  | "aligning"
  | "diarizing"
  | "structuring"
  | "quality_check"
  | "processed"
  | "publishing"
  | "published"
  | "paused"
  | "waiting_user"
  | "partial"
  | "failed"
  | "cancelled";

export interface JobUpdateSchema {
  readonly created_at: string;
  readonly event_type: string;
  readonly job_id: string;
  readonly job_revision: number;
  readonly payload: Readonly<Record<string, unknown>>;
  readonly sequence: number;
}

export interface JobUpdatesResponse {
  readonly has_more: boolean;
  readonly next_after_sequence: number;
  readonly updates: ReadonlyArray<JobUpdateSchema>;
}

export type ModelProfile =
  | "accuracy"
  | "speed";

export interface PairedDeviceListResponse {
  readonly devices: ReadonlyArray<PairedDeviceSchema>;
}

export interface PairedDeviceSchema {
  readonly allowed_vault_ids: ReadonlyArray<string>;
  readonly created_at: string;
  readonly credential_id: string;
  readonly device_id: string;
  readonly generation: number;
  readonly last_used_at: string | null;
  readonly revoked_at: string | null;
}

export interface PairingConfirmRequestSchema {
  readonly pairing_code?: string | null;
  readonly pairing_ticket?: string | null;
  readonly session_id?: string | null;
}

export interface PairingSessionCreateSchema {
  readonly allowed_vault_ids: ReadonlyArray<string>;
  readonly device_id: string;
  readonly ttl_seconds?: number;
}

export interface PairingSessionSecretSchema {
  readonly allowed_vault_ids: ReadonlyArray<string>;
  readonly device_id: string;
  readonly expires_at: string;
  readonly pairing_code: string;
  readonly pairing_ticket: string;
  readonly session_id: string;
}

export interface PreparedCredentialRotationSchema {
  readonly bearer_token: string;
  readonly device_id: string;
  readonly expires_at: string;
  readonly generation: number;
  readonly rotation_id: string;
}

export interface ProfileReadinessSchema {
  readonly can_start: boolean;
  readonly issue_codes: ReadonlyArray<string>;
  readonly model_profile: ModelProfile;
  readonly state: "ready" | "warning" | "blocked";
}

export type ProtocolCapability =
  | "resumable_uploads"
  | "job_snapshots"
  | "bounded_event_updates"
  | "progressive_transcript"
  | "recording_context"
  | "content_type_override"
  | "correction_ledger"
  | "summary_revisions"
  | "evidence_linked_artifacts"
  | "publication_leases"
  | "atomic_vault_publication"
  | "review_audio_ranges"
  | "worker_readiness";

export interface ProtocolLimitsSchema {
  readonly default_upload_chunk_size_bytes: number;
  readonly max_recording_context_characters: number;
  readonly max_snapshot_segments: number;
  readonly max_update_events: number;
  readonly max_upload_chunk_size_bytes: number;
  readonly max_upload_parts: number;
}

export interface ProvisionalTranscriptSchema {
  readonly end_ms: number;
  readonly generation: number;
  readonly job_id: string;
  readonly language: string | null;
  readonly start_ms: number;
  readonly text: string;
  readonly updated_at: string;
}

export interface ReviewAudioResponse {
  readonly accept_ranges: "bytes";
  readonly bits_per_sample: number;
  readonly channels: number;
  readonly content_path: string;
  readonly duration_ms: number;
  readonly job_id: string;
  readonly media_type: "audio/wav";
  readonly retention: "job_lifetime";
  readonly sample_rate: number;
  readonly sha256: string;
  readonly size_bytes: number;
  readonly status: "available";
}

export interface SegmentReviewEnvelope {
  readonly correction: CorrectionSchema;
  readonly created: boolean;
  readonly job: JobSchema;
}

export interface SegmentReviewRequestSchema {
  readonly after_speaker_id: string | null;
  readonly after_text: string;
  readonly author: string;
  readonly before_speaker_id: string | null;
  readonly before_text: string;
  readonly expected_revision: number;
  readonly segment_id: string;
}

export interface SpeakerDisplayNameEnvelope {
  readonly correction: CorrectionSchema;
  readonly created: boolean;
  readonly job: JobSchema;
}

export interface SpeakerDisplayNameRequestSchema {
  readonly after: string;
  readonly author: string;
  readonly before: string;
  readonly expected_revision: number;
  readonly speaker_id: string;
}

export type SpeakerLabelStatus =
  | "pending"
  | "anonymous"
  | "confirmed"
  | "unavailable";

export type TranscriptOutcome =
  | "transcribed"
  | "inaudible"
  | "non_speech"
  | "failed";

export interface TranscriptSegmentSchema {
  readonly confidence: number | null;
  readonly created_at: string;
  readonly end_ms: number;
  readonly error_code: string | null;
  readonly job_id: string;
  readonly language: string | null;
  readonly outcome: TranscriptOutcome;
  readonly revision: number;
  readonly segment_id: string;
  readonly segment_sequence: number;
  readonly speaker_id: string | null;
  readonly speaker_label_status: SpeakerLabelStatus;
  readonly start_ms: number;
  readonly text: string | null;
  readonly timing_status: TranscriptTimingStatus;
  readonly updated_at: string;
}

export type TranscriptTimingStatus =
  | "estimated"
  | "aligned";

export interface UploadCreateSchema {
  readonly media_type: string;
  readonly source_display_name: string;
  readonly source_sha256: string;
  readonly source_size_bytes: number;
  readonly vault_id: string;
}

export interface UploadEnvelope {
  readonly created?: boolean | null;
  readonly missing_part_numbers: ReadonlyArray<number>;
  readonly upload: UploadSchema;
}

export interface UploadPartEnvelope {
  readonly created: boolean;
  readonly part: UploadPartSchema;
}

export interface UploadPartSchema {
  readonly created_at: string;
  readonly part_number: number;
  readonly sha256: string;
  readonly size_bytes: number;
  readonly updated_at: string;
  readonly upload_id: string;
}

export interface UploadSchema {
  readonly audio_stream_count: number | null;
  readonly chunk_size_bytes: number;
  readonly completed_at: string | null;
  readonly created_at: string;
  readonly detected_format_name: string | null;
  readonly duration_seconds: number | null;
  readonly last_error_code: string | null;
  readonly last_error_message: string | null;
  readonly media_type: string;
  readonly part_count: number;
  readonly received_bytes: number;
  readonly received_part_count: number;
  readonly source_display_name: string;
  readonly source_sha256: string;
  readonly source_size_bytes: number;
  readonly state: UploadState;
  readonly updated_at: string;
  readonly upload_id: string;
  readonly vault_id: string;
}

export type UploadState =
  | "uploading"
  | "verifying"
  | "complete"
  | "failed";

export interface VersionRangeSchema {
  readonly maximum: string;
  readonly minimum: string;
}

export interface WorkerReadinessResponse {
  readonly active_model_profile: "accuracy" | "speed" | "all" | null;
  readonly checked_at: string;
  readonly disk_free_bytes: number;
  readonly disk_reserve_bytes: number;
  readonly disk_total_bytes: number;
  readonly endpoint_mode: "local_only" | "private_tls";
  readonly ffmpeg_available: boolean;
  readonly ffprobe_available: boolean;
  readonly issue_codes: ReadonlyArray<string>;
  readonly memory_available_bytes: number;
  readonly memory_total_bytes: number;
  readonly memory_used_percent: number;
  readonly ollama_reachable: boolean;
  readonly profiles: ReadonlyArray<ProfileReadinessSchema>;
  readonly protocol_version: string;
  readonly schema_version: string;
  readonly security_database_ok: boolean;
  readonly state: "ready" | "warning" | "blocked";
  readonly storage_ready: boolean;
  readonly swap_used_bytes: number;
  readonly tls_enabled: boolean;
  readonly worker_database_ok: boolean;
  readonly worker_version: string;
}
