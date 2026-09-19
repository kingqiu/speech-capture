import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import type {
  ArtifactListResponse,
  ArtifactSchema,
  JobSchema,
  PublicationAcknowledgementEnvelope,
  PublicationClaimEnvelope,
  PublicationStatusResponse
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

import type { ObsidianWorkerTransport } from "./obsidian-worker-transport";
import type { WorkerConnectionSettings } from "./settings";
import type { WorkerTransport, WorkerTransportResponse } from "./worker-probe";

type ArtifactName = ArtifactSchema["name"];

export const PUBLICATION_FILE_NAMES = Object.freeze([
  "transcript.raw.json",
  "transcript.md",
  "speech-record.json",
  "note.md",
  "note.evidence.md",
  "timeline.md",
  "artifact-manifest.json"
] as const satisfies readonly ArtifactName[]);

export interface DownloadedPublicationFile {
  readonly name: ArtifactName;
  readonly mediaType: string;
  readonly sha256: string;
  readonly bytes: Uint8Array;
}

export interface DownloadedPublicationPackage {
  readonly jobId: string;
  readonly speechId: string;
  readonly manifestSha256: string;
  readonly files: readonly DownloadedPublicationFile[];
}

export class PublicationClientError extends Error {
  public constructor(
    public readonly kind:
      | "authentication"
      | "conflict"
      | "lease"
      | "unavailable"
      | "invalid",
    message: string,
    public readonly workerCode: string | null = null
  ) {
    super(message);
    this.name = "PublicationClientError";
  }
}

export async function getPublicationStatus(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  jobId: string,
  outputRoot: string
): Promise<PublicationStatusResponse> {
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(jobId)}/publication?output_root=${encodeURIComponent(outputRoot)}`,
    { bearerToken }
  );
  const value = publicationSuccess(response);
  if (!isPublicationStatus(value)) {
    throw new PublicationClientError("invalid", "Worker 返回了无法识别的发布状态。");
  }
  const status = value as unknown as PublicationStatusResponse;
  if (
    (status.active_lease !== null &&
      status.active_lease.manifest_sha256 !== status.manifest_sha256)
  ) {
    throw new PublicationClientError("invalid", "Worker 发布状态与当前产物不一致。");
  }
  return status;
}

export async function claimPublication(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  request: {
    readonly job: Pick<JobSchema, "job_id" | "revision">;
    readonly targetRelativePath: string;
    readonly manifestSha256: string;
  }
): Promise<PublicationClaimEnvelope> {
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(request.job.job_id)}/publication-claims`,
    {
      method: "POST",
      bearerToken,
      body: {
        expected_revision: request.job.revision,
        target_relative_path: request.targetRelativePath,
        manifest_sha256: request.manifestSha256,
        lease_seconds: 120
      }
    }
  );
  const value = publicationSuccess(response);
  if (!isJob(value.job) || !isLease(value.lease) || typeof value.created !== "boolean") {
    throw new PublicationClientError("invalid", "Worker 返回了无法识别的发布租约。");
  }
  const envelope = value as unknown as PublicationClaimEnvelope;
  if (
    envelope.job.state !== "publishing" ||
    !envelope.lease.owned_by_caller ||
    envelope.lease.target_relative_path !== request.targetRelativePath ||
    envelope.lease.manifest_sha256 !== request.manifestSha256
  ) {
    throw new PublicationClientError("invalid", "Worker 发布租约与当前请求不一致。");
  }
  return envelope;
}

export async function releasePublication(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  jobId: string,
  leaseId: string
): Promise<void> {
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(jobId)}/publication-claims/release`,
    {
      method: "POST",
      bearerToken,
      body: { lease_id: leaseId }
    }
  );
  publicationSuccess(response);
}

export async function acknowledgePublication(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  request: {
    readonly jobId: string;
    readonly leaseId: string;
    readonly manifestSha256: string;
  }
): Promise<PublicationAcknowledgementEnvelope> {
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(request.jobId)}/publication-acknowledgements`,
    {
      method: "POST",
      bearerToken,
      body: {
        lease_id: request.leaseId,
        manifest_sha256: request.manifestSha256
      }
    }
  );
  const value = publicationSuccess(response);
  if (!isJob(value.job) || !isReceipt(value.receipt) || typeof value.created !== "boolean") {
    throw new PublicationClientError("invalid", "Worker 返回了无法识别的发布确认。");
  }
  const envelope = value as unknown as PublicationAcknowledgementEnvelope;
  if (
    envelope.job.state !== "published" ||
    envelope.receipt.manifest_sha256 !== request.manifestSha256
  ) {
    throw new PublicationClientError("invalid", "Worker 发布确认与当前写入不一致。");
  }
  return envelope;
}

export async function downloadPublicationPackage(
  transport: ObsidianWorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  jobId: string
): Promise<DownloadedPublicationPackage> {
  const listingResponse = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(jobId)}/artifacts`,
    { bearerToken }
  );
  const listingValue = publicationSuccess(listingResponse);
  if (!isArtifactListing(listingValue)) {
    throw new PublicationClientError("invalid", "Worker 返回了无法识别的发布产物。");
  }
  const listing = listingValue as unknown as ArtifactListResponse;
  const descriptors = new Map(listing.artifacts.map((item) => [item.name, item]));
  if (
    listing.artifacts.length !== PUBLICATION_FILE_NAMES.length ||
    descriptors.size !== PUBLICATION_FILE_NAMES.length ||
    PUBLICATION_FILE_NAMES.some((name) => !descriptors.has(name))
  ) {
    throw new PublicationClientError("invalid", "发布产物包不完整。");
  }
  const files: DownloadedPublicationFile[] = [];
  for (const name of PUBLICATION_FILE_NAMES) {
    const descriptor = descriptors.get(name)!;
    const response = await transport.requestBinary(worker, descriptor.download_path, {
      bearerToken,
      accept: descriptor.media_type
    });
    if (response.status === 401 || response.status === 403) {
      throw new PublicationClientError("authentication", "Worker 授权已失效，请重新连接。");
    }
    if (response.status === 0 || response.status >= 500) {
      throw new PublicationClientError("unavailable", "暂时无法读取发布产物。");
    }
    const bytes = new Uint8Array(response.arrayBuffer);
    if (
      response.status !== 200 ||
      bytes.byteLength !== descriptor.size_bytes ||
      bytesToHex(sha256(bytes)) !== descriptor.sha256
    ) {
      throw new PublicationClientError("invalid", "发布产物完整性检查未通过。");
    }
    files.push({
      name,
      mediaType: descriptor.media_type,
      sha256: descriptor.sha256,
      bytes
    });
  }
  const manifest = files.find((item) => item.name === "artifact-manifest.json");
  if (!manifest || manifest.sha256 !== listing.manifest_sha256) {
    throw new PublicationClientError("invalid", "发布清单完整性检查未通过。");
  }
  return {
    jobId: listing.job_id,
    speechId: listing.speech_id,
    manifestSha256: listing.manifest_sha256,
    files
  };
}

function publicationSuccess(response: WorkerTransportResponse): Record<string, unknown> {
  const error = isRecord(response.json) && isRecord(response.json.error)
    ? response.json.error
    : null;
  const workerCode = error && typeof error.code === "string" ? error.code : null;
  if (response.status === 401 || response.status === 403) {
    throw new PublicationClientError(
      "authentication",
      "Worker 授权已失效，请重新连接。",
      workerCode
    );
  }
  if (response.status === 409) {
    throw new PublicationClientError(
      workerCode === "PUBLICATION_LEASE_CONFLICT" ? "lease" : "conflict",
      workerCode === "PUBLICATION_LEASE_CONFLICT"
        ? "另一台已授权设备正在发布。"
        : "发布状态发生了变化。",
      workerCode
    );
  }
  if (response.status === 0 || response.status >= 500) {
    throw new PublicationClientError("unavailable", "暂时无法连接 Worker。", workerCode);
  }
  if (response.status !== 200 || !isRecord(response.json)) {
    throw new PublicationClientError("invalid", "Worker 未接受当前发布请求。", workerCode);
  }
  return response.json;
}

function isPublicationStatus(value: unknown): boolean {
  return (
    isRecord(value) &&
    isJob(value.job) &&
    typeof value.suggested_target_relative_path === "string" &&
    isSha256(value.manifest_sha256) &&
    value.artifact_count === PUBLICATION_FILE_NAMES.length &&
    (value.active_lease === null || isLease(value.active_lease)) &&
    (value.receipt === null || isReceipt(value.receipt))
  );
}

function isArtifactListing(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.job_id === "string" &&
    typeof value.speech_id === "string" &&
    isSha256(value.manifest_sha256) &&
    Array.isArray(value.artifacts) &&
    value.artifacts.every(isArtifact)
  );
}

function isArtifact(value: unknown): value is ArtifactSchema {
  return (
    isRecord(value) &&
    PUBLICATION_FILE_NAMES.includes(value.name as ArtifactName) &&
    typeof value.media_type === "string" &&
    Number.isSafeInteger(value.size_bytes) &&
    (value.size_bytes as number) >= 0 &&
    isSha256(value.sha256) &&
    typeof value.download_path === "string"
  );
}

function isJob(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.job_id === "string" &&
    typeof value.state === "string" &&
    typeof value.revision === "number"
  );
}

function isLease(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.lease_id === "string" &&
    Number.isSafeInteger(value.generation) &&
    (value.generation as number) >= 1 &&
    typeof value.target_relative_path === "string" &&
    isSha256(value.manifest_sha256) &&
    typeof value.expires_at === "string" &&
    typeof value.owned_by_caller === "boolean"
  );
}

function isReceipt(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.target_relative_path === "string" &&
    isSha256(value.manifest_sha256) &&
    typeof value.published_at === "string"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}
