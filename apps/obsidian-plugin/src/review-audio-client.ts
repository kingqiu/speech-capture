import type { ReviewAudioResponse } from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

import { JobClientError } from "./job-client";
import { ObsidianWorkerTransport } from "./obsidian-worker-transport";
import type { WorkerConnectionSettings } from "./settings";

const HEADER_PROBE_BYTES = 64 * 1024;

export async function loadReviewAudioSegment(
  transport: ObsidianWorkerTransport,
  worker: WorkerConnectionSettings,
  bearerToken: string,
  jobId: string,
  startMs: number,
  endMs: number
): Promise<{ readonly blob: Blob; readonly durationMs: number }> {
  const metadataResponse = await transport.request(
    worker,
    `/v1/jobs/${encodeURIComponent(jobId)}/review-audio`,
    { bearerToken }
  );
  const metadata = parseMetadata(metadataResponse.status, metadataResponse.json);
  const headerResponse = await transport.requestBinary(
    worker,
    metadata.content_path,
    {
      bearerToken,
      headers: { Range: `bytes=0-${(HEADER_PROBE_BYTES - 1).toString()}` }
    }
  );
  assertBinarySuccess(headerResponse.status);
  const dataOffset = findWaveDataOffset(headerResponse.arrayBuffer);
  const bytesPerFrame = metadata.channels * (metadata.bits_per_sample / 8);
  if (!Number.isInteger(bytesPerFrame) || bytesPerFrame <= 0) {
    throw new JobClientError("invalid", "Worker 返回了无法播放的音频格式。");
  }
  const boundedStart = Math.max(0, Math.min(startMs, metadata.duration_ms));
  const boundedEnd = Math.max(
    boundedStart + 1,
    Math.min(endMs, metadata.duration_ms)
  );
  const startFrame = Math.floor((boundedStart / 1_000) * metadata.sample_rate);
  const endFrame = Math.ceil((boundedEnd / 1_000) * metadata.sample_rate);
  const firstByte = dataOffset + startFrame * bytesPerFrame;
  const lastByte = Math.min(
    metadata.size_bytes - 1,
    dataOffset + endFrame * bytesPerFrame - 1
  );
  const audioResponse = await transport.requestBinary(
    worker,
    metadata.content_path,
    {
      bearerToken,
      headers: { Range: `bytes=${firstByte.toString()}-${lastByte.toString()}` }
    }
  );
  assertBinarySuccess(audioResponse.status);
  const received = new Uint8Array(audioResponse.arrayBuffer);
  const pcm =
    audioResponse.status === 200
      ? received.slice(firstByte, lastByte + 1)
      : received;
  const wave = buildPcmWave(
    pcm,
    metadata.sample_rate,
    metadata.channels,
    metadata.bits_per_sample
  );
  return {
    blob: new Blob([wave], { type: "audio/wav" }),
    durationMs: boundedEnd - boundedStart
  };
}

function parseMetadata(status: number, value: unknown): ReviewAudioResponse {
  if (status === 401 || status === 403) {
    throw new JobClientError("authentication", "Worker 授权已失效，请重新连接。");
  }
  if (status === 0 || status >= 500) {
    throw new JobClientError("unavailable", "暂时无法连接 Worker。");
  }
  if (
    status !== 200 ||
    !isRecord(value) ||
    value.media_type !== "audio/wav" ||
    typeof value.content_path !== "string" ||
    typeof value.size_bytes !== "number" ||
    typeof value.duration_ms !== "number" ||
    typeof value.sample_rate !== "number" ||
    typeof value.channels !== "number" ||
    typeof value.bits_per_sample !== "number"
  ) {
    throw new JobClientError("invalid", "Worker 返回了无法识别的复核音频。");
  }
  return value as unknown as ReviewAudioResponse;
}

function assertBinarySuccess(status: number): void {
  if (status === 401 || status === 403) {
    throw new JobClientError("authentication", "Worker 授权已失效，请重新连接。");
  }
  if (status !== 200 && status !== 206) {
    throw new JobClientError(
      status === 0 || status >= 500 ? "unavailable" : "invalid",
      status === 0 || status >= 500
        ? "暂时无法连接 Worker。"
        : "Worker 未能读取这段复核音频。"
    );
  }
}

function findWaveDataOffset(buffer: ArrayBuffer): number {
  const bytes = new Uint8Array(buffer);
  if (ascii(bytes, 0, 4) !== "RIFF" || ascii(bytes, 8, 4) !== "WAVE") {
    throw new JobClientError("invalid", "复核音频不是可识别的 WAV 文件。");
  }
  const view = new DataView(buffer);
  let offset = 12;
  while (offset + 8 <= bytes.length) {
    const chunk = ascii(bytes, offset, 4);
    const size = view.getUint32(offset + 4, true);
    if (chunk === "data") {
      return offset + 8;
    }
    offset += 8 + size + (size % 2);
  }
  throw new JobClientError("invalid", "复核音频缺少可播放的数据段。");
}

function buildPcmWave(
  pcm: Uint8Array,
  sampleRate: number,
  channels: number,
  bitsPerSample: number
): ArrayBuffer {
  const buffer = new ArrayBuffer(44 + pcm.byteLength);
  const bytes = new Uint8Array(buffer);
  const view = new DataView(buffer);
  writeAscii(bytes, 0, "RIFF");
  view.setUint32(4, 36 + pcm.byteLength, true);
  writeAscii(bytes, 8, "WAVE");
  writeAscii(bytes, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  const blockAlign = channels * (bitsPerSample / 8);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);
  writeAscii(bytes, 36, "data");
  view.setUint32(40, pcm.byteLength, true);
  bytes.set(pcm, 44);
  return buffer;
}

function ascii(bytes: Uint8Array, offset: number, length: number): string {
  return String.fromCharCode(...bytes.slice(offset, offset + length));
}

function writeAscii(bytes: Uint8Array, offset: number, value: string): void {
  for (let index = 0; index < value.length; index += 1) {
    bytes[offset + index] = value.charCodeAt(index);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
