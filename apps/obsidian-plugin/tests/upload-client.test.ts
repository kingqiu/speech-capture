import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { describe, expect, it } from "vitest";

import type { WorkerConnectionSettings } from "../src/settings";
import {
  hashSource,
  inferAudioMediaType,
  submitRecording,
  type UploadSource
} from "../src/upload-client";
import type {
  WorkerTransport,
  WorkerTransportResponse
} from "../src/worker-probe";

const WORKER: WorkerConnectionSettings = {
  id: "home",
  displayName: "书房 Mac",
  endpoint: "https://worker.example.test",
  kind: "remote"
};

describe("submitRecording", () => {
  it("resumes only missing parts and carries context and date into the job", async () => {
    const source = trackedSource("meeting.wav", "audio/wav", "abcdefghij");
    const transport = new QueueTransport([
      response(200, uploadEnvelope("uploading", [2, 3])),
      response(200, partEnvelope(2, "efgh")),
      response(200, partEnvelope(3, "ij")),
      response(200, uploadEnvelope("complete", [])),
      response(200, {
        created: true,
        job: {
          job_id: `job_${"a".repeat(32)}`,
          state: "queued",
          revision: 0
        }
      })
    ]);
    const phases: string[] = [];
    const uploadedBytes: number[] = [];

    const result = await submitRecording(transport, {
      worker: WORKER,
      bearerToken: "scw_synthetic-token",
      vaultId: "vault_one",
      source,
      recordingDate: "2026-08-03",
      recordingContext: "  正确公司名是聚衣堂。  ",
      modelProfile: "accuracy",
      onProgress: (progress) => {
        phases.push(progress.phase);
        if (progress.phase === "uploading") {
          uploadedBytes.push(progress.processedBytes);
        }
      }
    });

    expect(result.job.state).toBe("queued");
    expect(transport.requests.map((item) => item.path)).toEqual([
      "/v1/uploads",
      `/v1/uploads/upl_${"b".repeat(32)}/parts/2`,
      `/v1/uploads/upl_${"b".repeat(32)}/parts/3`,
      `/v1/uploads/upl_${"b".repeat(32)}/complete`,
      "/v1/jobs"
    ]);
    expect(transport.requests[4]?.body).toMatchObject({
      recording_context: "正确公司名是聚衣堂。",
      recording_date: "2026-08-03",
      model_profile: "accuracy"
    });
    expect(transport.requests[0]?.headers?.["Idempotency-Key"]).toMatch(
      /^obsidian-[0-9a-f]{64}$/
    );
    expect(transport.requests[4]?.headers?.["Idempotency-Key"]).toMatch(
      /^obsidian-[0-9a-f]{64}$/
    );
    expect(phases).toContain("hashing");
    expect(phases.at(-1)).toBe("done");
    expect(uploadedBytes).toEqual(expect.arrayContaining([4, 6, 8, 9, 10]));
    expect(source.slices).toEqual([
      [0, 10],
      [4, 8],
      [8, 10]
    ]);
  });

  it("hashes a large source in bounded slices", async () => {
    const size = 9 * 1024 * 1024;
    const source = new TrackingZeroSource(size);

    await expect(hashSource(source)).resolves.toMatch(/^[0-9a-f]{64}$/);

    expect(source.slices).toEqual([
      [0, 4 * 1024 * 1024],
      [4 * 1024 * 1024, 8 * 1024 * 1024],
      [8 * 1024 * 1024, 9 * 1024 * 1024]
    ]);
  });

  it("retries a disconnected part at the resumable boundary without regressing progress", async () => {
    const source = trackedSource("meeting.wav", "audio/wav", "abcdefghij");
    const transport = new QueueTransport([
      response(200, uploadEnvelope("uploading", [2, 3])),
      response(0, null),
      response(503, null),
      response(200, partEnvelope(2, "efgh")),
      response(200, partEnvelope(3, "ij")),
      response(200, uploadEnvelope("complete", [])),
      response(200, {
        created: true,
        job: {
          job_id: `job_${"c".repeat(32)}`,
          state: "queued",
          revision: 0
        }
      })
    ]);
    const retries: number[] = [];
    const progress: number[] = [];

    await submitRecording(transport, {
      worker: WORKER,
      bearerToken: "scw_synthetic-token",
      vaultId: "vault_one",
      source,
      recordingDate: "2026-08-03",
      recordingContext: "",
      modelProfile: "accuracy",
      retryDelayMs: 0,
      onProgress: (update) => {
        if (update.phase === "uploading" || update.phase === "waiting_retry") {
          progress.push(update.processedBytes);
        }
        if (update.phase === "waiting_retry") {
          retries.push(update.retryAttempt ?? -1);
        }
      }
    });

    expect(retries).toEqual([1, 2]);
    expect(
      transport.requests.filter((item) => item.path.endsWith("/parts/2"))
    ).toHaveLength(3);
    expect(progress.every((value, index) => index === 0 || value >= progress[index - 1]!))
      .toBe(true);
  });

  it("stops after three automatic retries so the existing manual retry remains available", async () => {
    const source = trackedSource("meeting.wav", "audio/wav", "abcdefghij");
    const transport = new QueueTransport([
      response(200, uploadEnvelope("uploading", [2, 3])),
      response(0, null),
      response(0, null),
      response(0, null),
      response(0, null)
    ]);

    await expect(
      submitRecording(transport, {
        worker: WORKER,
        bearerToken: "scw_synthetic-token",
        vaultId: "vault_one",
        source,
        recordingDate: "2026-08-03",
        recordingContext: "",
        modelProfile: "accuracy",
        retryDelayMs: 0
      })
    ).rejects.toMatchObject({ code: "worker_unavailable" });

    expect(
      transport.requests.filter((item) => item.path.endsWith("/parts/2"))
    ).toHaveLength(4);
  });

  it("infers a safe audio media type when the desktop file type is empty", () => {
    expect(inferAudioMediaType({ name: "recording.M4A", type: "" })).toBe(
      "audio/mp4"
    );
    expect(inferAudioMediaType({ name: "recording.unknown", type: "" })).toBe(
      "application/octet-stream"
    );
  });
});

function uploadEnvelope(state: "uploading" | "complete", missing: number[]) {
  return {
    created: state === "uploading",
    missing_part_numbers: missing,
    upload: {
      upload_id: `upl_${"b".repeat(32)}`,
      state,
      chunk_size_bytes: 4,
      part_count: 3
    }
  };
}

function partEnvelope(partNumber: number, text: string) {
  const bytes = new TextEncoder().encode(text);
  return {
    created: true,
    part: {
      part_number: partNumber,
      sha256: bytesToHex(sha256(bytes)),
      size_bytes: bytes.byteLength
    }
  };
}

function response(status: number, json: unknown): WorkerTransportResponse {
  return { status, json };
}

function trackedSource(name: string, type: string, text: string) {
  const bytes = new TextEncoder().encode(text);
  const slices: Array<[number, number]> = [];
  return {
    name,
    type,
    size: bytes.byteLength,
    slices,
    slice(start = 0, end = bytes.byteLength): Blob {
      slices.push([start, end]);
      return new Blob([bytes.slice(start, end)]);
    }
  } satisfies UploadSource & { readonly slices: Array<[number, number]> };
}

class TrackingZeroSource implements UploadSource {
  public readonly name = "large.wav";
  public readonly type = "audio/wav";
  public readonly slices: Array<[number, number]> = [];

  public constructor(public readonly size: number) {}

  public slice(start = 0, end = this.size): Blob {
    this.slices.push([start, end]);
    return new Blob([new Uint8Array(end - start)]);
  }
}

class QueueTransport implements WorkerTransport {
  public readonly requests: Array<{
    path: string;
    method?: "GET" | "POST" | "PUT";
    body?: unknown;
    rawBody?: ArrayBuffer;
    bearerToken?: string;
    headers?: Readonly<Record<string, string>>;
    onUploadProgress?: (uploadedBytes: number) => void;
  }> = [];

  public constructor(private readonly responses: WorkerTransportResponse[]) {}

  public async request(
    _worker: WorkerConnectionSettings,
    path: string,
    options: {
      readonly method?: "GET" | "POST" | "PUT";
      readonly body?: unknown;
      readonly rawBody?: ArrayBuffer;
      readonly bearerToken?: string;
      readonly headers?: Readonly<Record<string, string>>;
      readonly onUploadProgress?: (uploadedBytes: number) => void;
    } = {}
  ): Promise<WorkerTransportResponse> {
    this.requests.push({ path, ...options });
    if (options.rawBody !== undefined && options.onUploadProgress !== undefined) {
      options.onUploadProgress(Math.floor(options.rawBody.byteLength / 2));
      options.onUploadProgress(options.rawBody.byteLength);
    }
    const next = this.responses.shift();
    if (next === undefined) {
      throw new Error("Synthetic response queue exhausted.");
    }
    return next;
  }
}
