// Generated Worker protocol wire types. Do not edit manually.
export const OPENAPI_SHA256 = "686cc2f1e8ad8052fff0c946cda3dbb96e2ef00263261dfc1138aba7797ba58e" as const;
export const OPENAPI_VERSION = "3.1.0" as const;
export const PROTOCOL_VERSION = "1.0.0" as const;

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

export interface HTTPValidationError {
  readonly detail?: ReadonlyArray<ValidationError>;
}

export interface HealthResponse {
  readonly protocol_version: string;
  readonly status: "ok";
  readonly worker_version: string;
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
  | "atomic_vault_publication";

export interface ProtocolLimitsSchema {
  readonly default_upload_chunk_size_bytes: number;
  readonly max_recording_context_characters: number;
  readonly max_snapshot_segments: number;
  readonly max_update_events: number;
  readonly max_upload_chunk_size_bytes: number;
  readonly max_upload_parts: number;
}

export interface ValidationError {
  readonly ctx?: Readonly<Record<string, unknown>>;
  readonly input?: unknown;
  readonly loc: ReadonlyArray<string | number>;
  readonly msg: string;
  readonly type: string;
}

export interface VersionRangeSchema {
  readonly maximum: string;
  readonly minimum: string;
}
