import type {
  CompatibilityRequestSchema,
  CompatibilityResponse,
  HealthResponse,
  IssuedDeviceCredentialSchema,
  WorkerReadinessResponse
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

import type { WorkerConnectionSettings } from "./settings";

const CLIENT_PROTOCOL_VERSION = "1.0.0";
const CLIENT_ARTIFACT_SCHEMA_VERSION = "1.6.0";

export const REQUIRED_STAGE_I_FEATURES = Object.freeze([
  "resumable_uploads",
  "job_snapshots",
  "bounded_event_updates",
  "progressive_transcript",
  "recording_context",
  "correction_ledger",
  "summary_revisions",
  "evidence_linked_artifacts",
  "publication_leases",
  "atomic_vault_publication",
  "review_audio_ranges",
  "worker_readiness"
]);

export interface WorkerTransportResponse {
  readonly status: number;
  readonly json: unknown;
  readonly error?: string;
}

export interface WorkerTransport {
  request(
    worker: WorkerConnectionSettings,
    path: string,
    options?: {
      readonly method?: "GET" | "POST" | "PUT";
      readonly body?: unknown;
      readonly rawBody?: ArrayBuffer;
      readonly bearerToken?: string;
      readonly headers?: Readonly<Record<string, string>>;
      readonly onUploadProgress?: (uploadedBytes: number) => void;
      readonly timeoutMs?: number;
    }
  ): Promise<WorkerTransportResponse>;
}

export type WorkerProbeResult =
  | { readonly state: "unreachable"; readonly diagnostic: string }
  | { readonly state: "incompatible"; readonly issueCodes: readonly string[] }
  | { readonly state: "pairing_required"; readonly workerVersion: string }
  | {
      readonly state: "ready" | "warning" | "blocked";
      readonly workerVersion: string;
      readonly readiness: WorkerReadinessResponse;
    };

export type PairingConfirmationResult =
  | {
      readonly ok: true;
      readonly credential: IssuedDeviceCredentialSchema;
    }
  | {
      readonly ok: false;
      readonly reason: "invalid" | "expired" | "conflict" | "unavailable";
    };

export async function confirmPairingTicket(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  pairingTicket: string
): Promise<PairingConfirmationResult> {
  const normalized = pairingTicket.trim();
  if (!/^scpair1\.[0-9a-f]{32}\.[A-Za-z0-9_-]+$/.test(normalized)) {
    return { ok: false, reason: "invalid" };
  }
  try {
    const response = await transport.request(worker, "/v1/pairing/confirm", {
      method: "POST",
      body: { pairing_ticket: normalized }
    });
    if (response.status === 404 || response.status === 410) {
      return { ok: false, reason: "expired" };
    }
    if (response.status === 401 || response.status === 422) {
      return { ok: false, reason: "invalid" };
    }
    if (response.status === 409) {
      return { ok: false, reason: "conflict" };
    }
    const credential = parseIssuedCredential(response);
    return credential === null
      ? { ok: false, reason: "unavailable" }
      : { ok: true, credential };
  } catch {
    return { ok: false, reason: "unavailable" };
  }
}

export async function probeWorker(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string | null
): Promise<WorkerProbeResult> {
  try {
    const healthResponse = await transport.request(worker, "/v1/health");
    const health = parseHealth(healthResponse);
    if (health === null) {
      return unreachable("健康检查", healthResponse);
    }

    const negotiationRequest: CompatibilityRequestSchema = {
      protocol: {
        minimum: CLIENT_PROTOCOL_VERSION,
        maximum: CLIENT_PROTOCOL_VERSION
      },
      artifact_schema: {
        minimum: CLIENT_ARTIFACT_SCHEMA_VERSION,
        maximum: CLIENT_ARTIFACT_SCHEMA_VERSION
      },
      required_features: REQUIRED_STAGE_I_FEATURES
    };
    const negotiationResponse = await transport.request(
      worker,
      "/v1/capabilities/negotiate",
      { method: "POST", body: negotiationRequest }
    );
    const compatibility = parseCompatibility(negotiationResponse);
    if (compatibility === null) {
      return unreachable("能力协商", negotiationResponse);
    }
    if (!compatibility.compatible) {
      return {
        state: "incompatible",
        issueCodes: [
          ...compatibility.issues,
          ...compatibility.missing_features.map(
            (feature) => `missing_capability:${feature}`
          )
        ]
      };
    }
    if (!bearerToken) {
      return { state: "pairing_required", workerVersion: health.worker_version };
    }

    const readinessResponse = await transport.request(worker, "/v1/readiness", {
      bearerToken
    });
    if (readinessResponse.status === 401 || readinessResponse.status === 403) {
      return { state: "pairing_required", workerVersion: health.worker_version };
    }
    const readiness = parseReadiness(readinessResponse);
    if (readiness === null) {
      return unreachable("就绪检查", readinessResponse);
    }
    return {
      state: readiness.state,
      workerVersion: health.worker_version,
      readiness
    };
  } catch {
    return { state: "unreachable", diagnostic: "连接检查发生未预期异常" };
  }
}

function unreachable(
  stage: string,
  response: WorkerTransportResponse
): Extract<WorkerProbeResult, { readonly state: "unreachable" }> {
  if (response.error) {
    return { state: "unreachable", diagnostic: `${stage}失败：${response.error}` };
  }
  if (response.status === 0) {
    return { state: "unreachable", diagnostic: `${stage}没有收到网络响应` };
  }
  return {
    state: "unreachable",
    diagnostic: `${stage}响应无效（HTTP ${response.status}）`
  };
}

function parseHealth(response: WorkerTransportResponse): HealthResponse | null {
  if (response.status !== 200 || !isRecord(response.json)) {
    return null;
  }
  const value = response.json;
  return value.status === "ok" &&
    typeof value.worker_version === "string" &&
    typeof value.protocol_version === "string"
    ? {
        status: "ok",
        worker_version: value.worker_version,
        protocol_version: value.protocol_version
      }
    : null;
}

function parseCompatibility(
  response: WorkerTransportResponse
): CompatibilityResponse | null {
  if (response.status !== 200 || !isRecord(response.json)) {
    return null;
  }
  const value = response.json;
  if (
    typeof value.compatible !== "boolean" ||
    !isStringArray(value.issues) ||
    !isStringArray(value.missing_features) ||
    !isNullableString(value.protocol_version) ||
    !isNullableString(value.artifact_schema_version)
  ) {
    return null;
  }
  return value as unknown as CompatibilityResponse;
}

function parseReadiness(
  response: WorkerTransportResponse
): WorkerReadinessResponse | null {
  if (response.status !== 200 || !isRecord(response.json)) {
    return null;
  }
  const value = response.json;
  if (
    (value.state !== "ready" &&
      value.state !== "warning" &&
      value.state !== "blocked") ||
    typeof value.worker_version !== "string" ||
    typeof value.protocol_version !== "string" ||
    typeof value.disk_free_bytes !== "number" ||
    typeof value.memory_available_bytes !== "number" ||
    !isStringArray(value.issue_codes) ||
    !Array.isArray(value.profiles)
  ) {
    return null;
  }
  return value as unknown as WorkerReadinessResponse;
}

function parseIssuedCredential(
  response: WorkerTransportResponse
): IssuedDeviceCredentialSchema | null {
  if (response.status !== 200 || !isRecord(response.json)) {
    return null;
  }
  const value = response.json;
  if (
    typeof value.credential_id !== "string" ||
    typeof value.device_id !== "string" ||
    typeof value.bearer_token !== "string" ||
    !value.bearer_token.startsWith("scw_") ||
    !isStringArray(value.allowed_vault_ids) ||
    value.allowed_vault_ids.length !== 1 ||
    typeof value.generation !== "number" ||
    typeof value.created_at !== "string"
  ) {
    return null;
  }
  return value as unknown as IssuedDeviceCredentialSchema;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}
