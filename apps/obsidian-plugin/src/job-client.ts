import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import type {
  JobActionEnvelope,
  JobListResponse,
  JobSchema,
  JobSnapshotResponse
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

import type { WorkerConnectionSettings } from "./settings";
import type {
  WorkerTransport,
  WorkerTransportResponse
} from "./worker-probe";

export class JobClientError extends Error {
  public constructor(
    public readonly code: "authentication" | "conflict" | "unavailable" | "invalid",
    message: string
  ) {
    super(message);
    this.name = "JobClientError";
  }
}

export async function listJobs(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  vaultId: string
): Promise<readonly JobSchema[]> {
  const response = await transport.request(
    worker,
    `/v1/jobs?vault_id=${encodeURIComponent(vaultId)}&limit=100`,
    { bearerToken }
  );
  const value = parseSuccess(response);
  if (!Array.isArray(value.jobs) || !value.jobs.every(isJobSummary)) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的任务列表。");
  }
  return (value as unknown as JobListResponse).jobs;
}

export async function getJobSnapshot(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  jobId: string
): Promise<JobSnapshotResponse> {
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(jobId)}/snapshot?segment_limit=100`,
    { bearerToken }
  );
  const value = parseSuccess(response);
  if (
    !isJobSummary(value.job) ||
    !Array.isArray(value.stable_segments) ||
    !value.stable_segments.every(isTranscriptSegment) ||
    !(value.provisional === null || isProvisional(value.provisional)) ||
    !(value.progress === null || isProgress(value.progress))
  ) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的任务进度。");
  }
  return value as unknown as JobSnapshotResponse;
}

export async function applyJobAction(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  job: Pick<JobSchema, "job_id" | "revision">,
  action: "pause" | "resume" | "cancel" | "retry"
): Promise<JobSchema> {
  const idempotency = bytesToHex(
    sha256(
      new TextEncoder().encode(
        `${action}\n${job.job_id}\n${job.revision.toString()}`
      )
    )
  );
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(job.job_id)}/${action}`,
    {
      method: "POST",
      body: { expected_revision: job.revision },
      bearerToken,
      headers: { "Idempotency-Key": `obsidian-${idempotency}` }
    }
  );
  const value = parseSuccess(response);
  if (!isJobSummary(value.job) || typeof value.applied !== "boolean") {
    throw new JobClientError("invalid", "Worker 返回了无法识别的任务操作结果。");
  }
  return (value as unknown as JobActionEnvelope).job;
}

function parseSuccess(response: WorkerTransportResponse): Record<string, unknown> {
  if (response.status === 401 || response.status === 403) {
    throw new JobClientError("authentication", "Worker 授权已失效，请重新连接。");
  }
  if (response.status === 409) {
    throw new JobClientError("conflict", "任务状态已发生变化，正在重新读取。");
  }
  if (response.status === 0 || response.status >= 500) {
    throw new JobClientError("unavailable", "暂时无法连接 Worker。");
  }
  if (response.status !== 200 || !isRecord(response.json)) {
    throw new JobClientError("invalid", "Worker 未接受当前请求。");
  }
  return response.json;
}

function isJobSummary(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.job_id === "string" &&
    typeof value.source_display_name === "string" &&
    typeof value.state === "string" &&
    typeof value.revision === "number" &&
    (value.recording_date === null || typeof value.recording_date === "string")
  );
}

function isTranscriptSegment(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.segment_id === "string" &&
    typeof value.segment_sequence === "number" &&
    typeof value.start_ms === "number" &&
    typeof value.end_ms === "number" &&
    (value.text === null || typeof value.text === "string")
  );
}

function isProvisional(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.start_ms === "number" &&
    typeof value.end_ms === "number" &&
    typeof value.text === "string"
  );
}

function isProgress(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.stage === "string" &&
    typeof value.stage_progress === "number" &&
    typeof value.duration_ms === "number" &&
    typeof value.processed_ms === "number"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
