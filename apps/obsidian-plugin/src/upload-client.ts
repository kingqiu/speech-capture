import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

import type {
  JobCreateSchema,
  JobEnvelope,
  ModelProfile,
  UploadCreateSchema,
  UploadEnvelope,
  UploadPartEnvelope
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

import type { WorkerConnectionSettings } from "./settings";
import type {
  WorkerTransport,
  WorkerTransportResponse
} from "./worker-probe";

const SOURCE_HASH_SLICE_BYTES = 4 * 1024 * 1024;

export interface UploadSource {
  readonly name: string;
  readonly size: number;
  readonly type: string;
  slice(start?: number, end?: number): Blob;
}

export type SubmissionPhase =
  | "hashing"
  | "uploading"
  | "verifying"
  | "creating_job"
  | "done";

export interface SubmissionProgress {
  readonly phase: SubmissionPhase;
  readonly processedBytes: number;
  readonly totalBytes: number;
  readonly completedParts: number;
  readonly totalParts: number;
}

export interface SubmitRecordingRequest {
  readonly worker: WorkerConnectionSettings;
  readonly bearerToken: string;
  readonly vaultId: string;
  readonly source: UploadSource;
  readonly recordingDate: string;
  readonly recordingContext: string;
  readonly modelProfile: ModelProfile;
  readonly contentTypeOverride?: string | null;
  readonly onProgress?: (progress: SubmissionProgress) => void;
}

export interface SubmitRecordingResult {
  readonly upload: UploadEnvelope["upload"];
  readonly job: JobEnvelope["job"];
}

export class SubmissionError extends Error {
  public constructor(
    public readonly code:
      | "invalid_source"
      | "authentication"
      | "conflict"
      | "worker_unavailable"
      | "unexpected_response",
    message: string
  ) {
    super(message);
    this.name = "SubmissionError";
  }
}

export async function submitRecording(
  transport: WorkerTransport,
  request: SubmitRecordingRequest
): Promise<SubmitRecordingResult> {
  validateRequest(request);
  const sourceSha256 = await hashSource(request.source, request.onProgress);
  const mediaType = inferAudioMediaType(request.source);
  const uploadCreate: UploadCreateSchema = {
    vault_id: request.vaultId,
    source_display_name: request.source.name,
    source_sha256: sourceSha256,
    source_size_bytes: request.source.size,
    media_type: mediaType
  };
  const uploadIdempotencyKey = digestText(
    `upload\n${canonicalJson(uploadCreate)}`
  );
  const uploadResponse = await transport.request(request.worker, "/v1/uploads", {
    method: "POST",
    body: uploadCreate,
    bearerToken: request.bearerToken,
    headers: { "Idempotency-Key": `obsidian-${uploadIdempotencyKey}` }
  });
  const uploadEnvelope = parseUploadEnvelope(uploadResponse);
  const missing = new Set(uploadEnvelope.missing_part_numbers);
  const totalParts = uploadEnvelope.upload.part_count;
  let completedParts = totalParts - missing.size;
  report(request, {
    phase: "uploading",
    processedBytes: Math.min(
      request.source.size,
      completedParts * uploadEnvelope.upload.chunk_size_bytes
    ),
    totalBytes: request.source.size,
    completedParts,
    totalParts
  });

  for (const partNumber of uploadEnvelope.missing_part_numbers) {
    validatePartNumber(partNumber, totalParts);
    const start = (partNumber - 1) * uploadEnvelope.upload.chunk_size_bytes;
    const end = Math.min(
      start + uploadEnvelope.upload.chunk_size_bytes,
      request.source.size
    );
    const partBytes = await request.source.slice(start, end).arrayBuffer();
    const partSha256 = bytesToHex(sha256(new Uint8Array(partBytes)));
    const partResponse = await transport.request(
      request.worker,
      `/v1/uploads/${uploadEnvelope.upload.upload_id}/parts/${partNumber}`,
      {
        method: "PUT",
        rawBody: partBytes,
        bearerToken: request.bearerToken,
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Part-SHA256": partSha256
        }
      }
    );
    parseUploadPartEnvelope(partResponse, partNumber, partSha256, end - start);
    completedParts += 1;
    report(request, {
      phase: "uploading",
      processedBytes: end,
      totalBytes: request.source.size,
      completedParts,
      totalParts
    });
  }

  report(request, {
    phase: "verifying",
    processedBytes: request.source.size,
    totalBytes: request.source.size,
    completedParts: totalParts,
    totalParts
  });
  const completeResponse = await transport.request(
    request.worker,
    `/v1/uploads/${uploadEnvelope.upload.upload_id}/complete`,
    { method: "POST", bearerToken: request.bearerToken }
  );
  const completedUpload = parseUploadEnvelope(completeResponse);
  if (completedUpload.upload.state !== "complete") {
    throw new SubmissionError(
      "unexpected_response",
      "Worker 未能确认音频上传完整。"
    );
  }

  report(request, {
    phase: "creating_job",
    processedBytes: request.source.size,
    totalBytes: request.source.size,
    completedParts: totalParts,
    totalParts
  });
  const normalizedContext = request.recordingContext.trim();
  const jobCreate: JobCreateSchema = {
    upload_id: completedUpload.upload.upload_id,
    model_profile: request.modelProfile,
    recording_date: request.recordingDate,
    ...(normalizedContext ? { recording_context: normalizedContext } : {}),
    ...(request.contentTypeOverride
      ? { content_type_override: request.contentTypeOverride }
      : {})
  };
  const jobIdempotencyKey = digestText(`job\n${canonicalJson(jobCreate)}`);
  const jobResponse = await transport.request(request.worker, "/v1/jobs", {
    method: "POST",
    body: jobCreate,
    bearerToken: request.bearerToken,
    headers: { "Idempotency-Key": `obsidian-${jobIdempotencyKey}` }
  });
  const jobEnvelope = parseJobEnvelope(jobResponse);
  report(request, {
    phase: "done",
    processedBytes: request.source.size,
    totalBytes: request.source.size,
    completedParts: totalParts,
    totalParts
  });
  return { upload: completedUpload.upload, job: jobEnvelope.job };
}

export async function hashSource(
  source: UploadSource,
  onProgress?: (progress: SubmissionProgress) => void
): Promise<string> {
  if (!Number.isSafeInteger(source.size) || source.size <= 0) {
    throw new SubmissionError("invalid_source", "音频文件为空或大小无效。");
  }
  const hash = sha256.create();
  let processedBytes = 0;
  while (processedBytes < source.size) {
    const end = Math.min(processedBytes + SOURCE_HASH_SLICE_BYTES, source.size);
    const bytes = await source.slice(processedBytes, end).arrayBuffer();
    hash.update(new Uint8Array(bytes));
    processedBytes = end;
    onProgress?.({
      phase: "hashing",
      processedBytes,
      totalBytes: source.size,
      completedParts: 0,
      totalParts: 0
    });
  }
  return bytesToHex(hash.digest());
}

export function inferAudioMediaType(source: Pick<UploadSource, "name" | "type">): string {
  if (source.type.startsWith("audio/")) {
    return source.type;
  }
  const extension = source.name.split(".").pop()?.toLowerCase();
  return (
    {
      wav: "audio/wav",
      m4a: "audio/mp4",
      mp3: "audio/mpeg",
      aac: "audio/aac",
      flac: "audio/flac",
      ogg: "audio/ogg",
      opus: "audio/ogg",
      webm: "audio/webm"
    }[extension ?? ""] ?? "application/octet-stream"
  );
}

function validateRequest(request: SubmitRecordingRequest): void {
  if (!request.bearerToken || !request.vaultId || !request.recordingDate) {
    throw new SubmissionError("authentication", "Worker 授权或任务信息不完整。");
  }
}

function parseUploadEnvelope(response: WorkerTransportResponse): UploadEnvelope {
  if (response.status === 401 || response.status === 403) {
    throw new SubmissionError("authentication", "Worker 授权已失效，请重新连接。");
  }
  if (response.status === 409) {
    throw new SubmissionError("conflict", "Worker 上存在不一致的同名上传任务。");
  }
  if (response.status !== 200 || !isRecord(response.json)) {
    throw responseError(response.status);
  }
  const value = response.json;
  if (
    !isRecord(value.upload) ||
    typeof value.upload.upload_id !== "string" ||
    typeof value.upload.chunk_size_bytes !== "number" ||
    typeof value.upload.part_count !== "number" ||
    typeof value.upload.state !== "string" ||
    !Array.isArray(value.missing_part_numbers) ||
    !value.missing_part_numbers.every(Number.isInteger)
  ) {
    throw new SubmissionError("unexpected_response", "Worker 返回了无法识别的上传状态。");
  }
  return value as unknown as UploadEnvelope;
}

function parseUploadPartEnvelope(
  response: WorkerTransportResponse,
  expectedPart: number,
  expectedSha256: string,
  expectedSize: number
): UploadPartEnvelope {
  if (response.status !== 200 || !isRecord(response.json)) {
    throw responseError(response.status);
  }
  const value = response.json;
  if (
    !isRecord(value.part) ||
    value.part.part_number !== expectedPart ||
    value.part.sha256 !== expectedSha256 ||
    value.part.size_bytes !== expectedSize
  ) {
    throw new SubmissionError("unexpected_response", "Worker 未能确认上传分段。");
  }
  return value as unknown as UploadPartEnvelope;
}

function parseJobEnvelope(response: WorkerTransportResponse): JobEnvelope {
  if (response.status === 401 || response.status === 403) {
    throw new SubmissionError("authentication", "Worker 授权已失效，请重新连接。");
  }
  if (response.status === 409) {
    throw new SubmissionError("conflict", "Worker 上存在不一致的任务请求。");
  }
  if (response.status !== 200 || !isRecord(response.json)) {
    throw responseError(response.status);
  }
  const value = response.json;
  if (
    !isRecord(value.job) ||
    typeof value.job.job_id !== "string" ||
    typeof value.job.state !== "string" ||
    typeof value.job.revision !== "number"
  ) {
    throw new SubmissionError("unexpected_response", "Worker 返回了无法识别的任务状态。");
  }
  return value as unknown as JobEnvelope;
}

function responseError(status: number): SubmissionError {
  return status === 0 || status >= 500
    ? new SubmissionError("worker_unavailable", "暂时无法连接 Worker，已上传的分段仍保留在 Worker。")
    : new SubmissionError("unexpected_response", "Worker 未接受当前请求，请重新检测后再试。");
}

function validatePartNumber(partNumber: number, totalParts: number): void {
  if (!Number.isInteger(partNumber) || partNumber < 1 || partNumber > totalParts) {
    throw new SubmissionError("unexpected_response", "Worker 返回了无效的上传分段编号。");
  }
}

function report(
  request: SubmitRecordingRequest,
  progress: SubmissionProgress
): void {
  request.onProgress?.(progress);
}

function digestText(value: string): string {
  return bytesToHex(sha256(new TextEncoder().encode(value)));
}

function canonicalJson(value: object): string {
  return JSON.stringify(
    Object.fromEntries(
      Object.entries(value).sort(([left], [right]) => left.localeCompare(right))
    )
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
