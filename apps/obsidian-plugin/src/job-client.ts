import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import type {
  CorrectionListResponse,
  CorrectionSchema,
  JobActionEnvelope,
  JobListResponse,
  JobSchema,
  JobSnapshotResponse,
  SegmentReviewEnvelope,
  SpeakerDisplayNameEnvelope,
  SummaryRevisionDecisionEnvelope,
  SummaryRevisionListResponse,
  SummaryRevisionRegenerationEnvelope,
  SummaryRevisionSchema,
  TranscriptSegmentSchema
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
  const segments: TranscriptSegmentSchema[] = [];
  let after = 0;
  let snapshot: JobSnapshotResponse | null = null;
  do {
    const response = await transport.request(
      worker,
      `/v1/jobs/${encodeURIComponent(jobId)}/snapshot?segment_limit=500&after_segment_sequence=${after.toString()}`,
      { bearerToken }
    );
    const value = parseSuccess(response);
    validateSnapshot(value);
    const page = value as unknown as JobSnapshotResponse;
    segments.push(...page.stable_segments);
    snapshot = page;
    after = page.next_after_segment_sequence;
  } while (snapshot.has_more_segments);
  return { ...snapshot, stable_segments: segments };
}

function validateSnapshot(value: Record<string, unknown>): void {
  if (
    !isJobSummary(value.job) ||
    !Array.isArray(value.stable_segments) ||
    !value.stable_segments.every(isTranscriptSegment) ||
    !(value.provisional === null || isProvisional(value.provisional)) ||
    !(value.progress === null || isProgress(value.progress))
  ) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的任务进度。");
  }
}

export async function listJobCorrections(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  jobId: string
): Promise<readonly CorrectionSchema[]> {
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(jobId)}/corrections`,
    { bearerToken }
  );
  const value = parseSuccess(response);
  if (!Array.isArray(value.corrections) || !value.corrections.every(isCorrection)) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的修订记录。");
  }
  return (value as unknown as CorrectionListResponse).corrections;
}

export async function listJobSummaryRevisions(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  jobId: string
): Promise<SummaryRevisionListResponse> {
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(jobId)}/summary-revisions`,
    { bearerToken }
  );
  const value = parseSuccess(response);
  if (
    !Array.isArray(value.revisions) ||
    !value.revisions.every(isSummaryRevision) ||
    typeof value.current_version !== "number" ||
    typeof value.manual_section_markdown !== "string" ||
    typeof value.can_regenerate !== "boolean"
  ) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的笔记版本记录。");
  }
  return value as unknown as SummaryRevisionListResponse;
}

export async function regenerateJobSummary(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  job: Pick<JobSchema, "job_id" | "revision">
): Promise<SummaryRevisionRegenerationEnvelope> {
  const body = { expected_revision: job.revision };
  const idempotency = bytesToHex(
    sha256(
      new TextEncoder().encode(
        `regenerate-summary\n${job.job_id}\n${job.revision.toString()}`
      )
    )
  );
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(job.job_id)}/summary-revisions`,
    {
      method: "POST",
      body,
      bearerToken,
      headers: { "Idempotency-Key": `obsidian-${idempotency}` }
    }
  );
  const value = parseSuccess(response);
  if (
    !isJobSummary(value.job) ||
    !isSummaryRevision(value.revision) ||
    typeof value.applied !== "boolean"
  ) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的笔记重新生成结果。");
  }
  return value as unknown as SummaryRevisionRegenerationEnvelope;
}

export async function decideJobSummaryRevision(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  request: {
    readonly job: Pick<JobSchema, "job_id" | "revision">;
    readonly revisionKey: string;
    readonly decision: "accepted" | "rejected";
  }
): Promise<SummaryRevisionDecisionEnvelope> {
  const body = {
    expected_revision: request.job.revision,
    decision: request.decision
  };
  const idempotency = bytesToHex(
    sha256(
      new TextEncoder().encode(
        JSON.stringify({
          ...body,
          revision_key: request.revisionKey
        })
      )
    )
  );
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(request.job.job_id)}/summary-revisions/${encodeURIComponent(request.revisionKey)}/decision`,
    {
      method: "POST",
      body,
      bearerToken,
      headers: { "Idempotency-Key": `obsidian-${idempotency}` }
    }
  );
  const value = parseSuccess(response);
  if (
    !isJobSummary(value.job) ||
    !isSummaryRevision(value.revision) ||
    typeof value.applied !== "boolean"
  ) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的笔记版本操作结果。");
  }
  return value as unknown as SummaryRevisionDecisionEnvelope;
}

export async function reviewTranscriptSegment(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  request: {
    readonly job: Pick<JobSchema, "job_id" | "revision">;
    readonly segmentId: string;
    readonly beforeText: string;
    readonly afterText: string;
    readonly beforeSpeakerId: string | null;
    readonly afterSpeakerId: string | null;
  }
): Promise<SegmentReviewEnvelope> {
  const body = {
    expected_revision: request.job.revision,
    segment_id: request.segmentId,
    before_text: request.beforeText,
    after_text: request.afterText,
    before_speaker_id: request.beforeSpeakerId,
    after_speaker_id: request.afterSpeakerId,
    author: "obsidian-user"
  };
  const idempotency = bytesToHex(
    sha256(new TextEncoder().encode(JSON.stringify(body)))
  );
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(request.job.job_id)}/segment-review`,
    {
      method: "POST",
      body,
      bearerToken,
      headers: { "Idempotency-Key": `obsidian-${idempotency}` }
    }
  );
  const value = parseSuccess(response);
  if (!isJobSummary(value.job) || !isCorrection(value.correction)) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的片段修订结果。");
  }
  return value as unknown as SegmentReviewEnvelope;
}

export function effectiveTranscriptSegment(
  segment: TranscriptSegmentSchema,
  corrections: readonly CorrectionSchema[]
): { readonly text: string; readonly speakerId: string | null; readonly revised: boolean } {
  let text = segment.text ?? "";
  let speakerId = segment.speaker_id;
  let revised = false;
  for (const correction of corrections) {
    if (correction.target_id !== segment.segment_id) {
      continue;
    }
    if (correction.field === "transcript_text") {
      text = correction.after;
      revised = true;
    } else if (correction.field === "segment_review") {
      const review = parseSegmentReview(correction.after);
      if (review) {
        text = review.text;
        speakerId = review.speakerId;
        revised = true;
      }
    }
  }
  return { text, speakerId, revised };
}

export function effectiveSpeakerDisplayName(
  speakerId: string,
  corrections: readonly CorrectionSchema[]
): { readonly displayName: string; readonly revised: boolean } {
  let displayName = canonicalSpeakerDisplayName(speakerId);
  let revised = false;
  for (const correction of corrections) {
    if (
      correction.field === "speaker_display_name" &&
      correction.target_id === speakerId
    ) {
      displayName = correction.after;
      revised = true;
    }
  }
  return { displayName, revised };
}

export async function renameJobSpeakerDisplayName(
  transport: WorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  request: {
    readonly job: Pick<JobSchema, "job_id" | "revision">;
    readonly speakerId: string;
    readonly before: string;
    readonly after: string;
  }
): Promise<SpeakerDisplayNameEnvelope> {
  const body = {
    expected_revision: request.job.revision,
    speaker_id: request.speakerId,
    before: request.before,
    after: request.after,
    author: "obsidian-user"
  };
  const idempotency = bytesToHex(
    sha256(new TextEncoder().encode(JSON.stringify(body)))
  );
  const response = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(request.job.job_id)}/speaker-display-name`,
    {
      method: "POST",
      body,
      bearerToken,
      headers: { "Idempotency-Key": `obsidian-${idempotency}` }
    }
  );
  const value = parseSuccess(response);
  if (!isJobSummary(value.job) || !isCorrection(value.correction)) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的说话人改名结果。");
  }
  return value as unknown as SpeakerDisplayNameEnvelope;
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

function isCorrection(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    typeof value.correction_id === "string" &&
    typeof value.field === "string" &&
    (value.target_id === null || typeof value.target_id === "string") &&
    typeof value.after === "string" &&
    typeof value.job_revision === "number"
  );
}

function isSummaryRevision(value: unknown): value is SummaryRevisionSchema {
  return (
    isRecord(value) &&
    typeof value.revision_key === "string" &&
    typeof value.base_version === "number" &&
    typeof value.candidate_version === "number" &&
    ["pending", "accepted", "rejected"].includes(String(value.status)) &&
    typeof value.changed === "boolean" &&
    typeof value.text_correction_count === "number" &&
    typeof value.speaker_rename_count === "number" &&
    (value.before_document === null || isRecord(value.before_document)) &&
    (value.after_document === null || isRecord(value.after_document)) &&
    typeof value.diff_truncated === "boolean" &&
    typeof value.created_at === "string" &&
    (value.decided_at === null || typeof value.decided_at === "string") &&
    (value.artifact_manifest_sha256 === null ||
      typeof value.artifact_manifest_sha256 === "string")
  );
}

function parseSegmentReview(
  value: string
): { readonly text: string; readonly speakerId: string | null } | null {
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      isRecord(parsed) &&
      typeof parsed.text === "string" &&
      (parsed.speaker_id === null || typeof parsed.speaker_id === "string")
    ) {
      return { text: parsed.text, speakerId: parsed.speaker_id };
    }
  } catch {
    // Invalid correction data is ignored here and remains available for diagnostics.
  }
  return null;
}

function canonicalSpeakerDisplayName(speakerId: string): string {
  const suffix = speakerId.match(/(\d+)$/)?.[1];
  return suffix ? `Speaker ${Number(suffix).toString()}` : speakerId;
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
    typeof value.processed_ms === "number" &&
    (value.detail === undefined ||
      value.detail === null ||
      isProgressDetail(value.detail))
  );
}

function isProgressDetail(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.substage === "string" &&
    typeof value.completed_units === "number" &&
    typeof value.total_units === "number" &&
    typeof value.cache_hits === "number" &&
    typeof value.retry_attempt === "number" &&
    (value.model_id === null || typeof value.model_id === "string") &&
    (value.input_tokens === null || typeof value.input_tokens === "number") &&
    (value.output_tokens === null || typeof value.output_tokens === "number")
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
