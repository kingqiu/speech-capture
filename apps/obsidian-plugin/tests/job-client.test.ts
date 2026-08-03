import { describe, expect, it } from "vitest";

import { applyJobAction, getJobSnapshot, listJobs } from "../src/job-client";
import type { WorkerConnectionSettings } from "../src/settings";
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
const JOB = {
  job_id: `job_${"a".repeat(32)}`,
  source_display_name: "synthetic.wav",
  state: "transcribing",
  revision: 4,
  recording_date: "2026-08-03"
};

describe("job client", () => {
  it("lists only the authorized Vault and reads progressive snapshot content", async () => {
    const transport = new QueueTransport([
      response(200, { jobs: [JOB] }),
      response(200, {
        job: JOB,
        stable_segments: [
          {
            segment_id: "seg_00000001",
            segment_sequence: 1,
            start_ms: 0,
            end_ms: 5000,
            text: "合成稳定文字"
          }
        ],
        provisional: {
          start_ms: 5000,
          end_ms: 7000,
          text: "合成临时结果"
        },
        progress: {
          stage: "transcribing",
          stage_progress: 0.44,
          duration_ms: 10000,
          processed_ms: 4400
        }
      })
    ]);

    const jobs = await listJobs(transport, WORKER, "secret", "vault_one");
    const snapshot = await getJobSnapshot(
      transport,
      WORKER,
      "secret",
      jobs[0]!.job_id
    );

    expect(transport.requests[0]?.path).toContain("vault_id=vault_one");
    expect(snapshot.stable_segments[0]?.text).toBe("合成稳定文字");
    expect(snapshot.provisional?.text).toBe("合成临时结果");
  });

  it("sends revision-bound idempotent lifecycle actions", async () => {
    const transport = new QueueTransport([
      response(200, { applied: true, job: { ...JOB, state: "paused", revision: 5 } })
    ]);

    const paused = await applyJobAction(
      transport,
      WORKER,
      "secret",
      JOB,
      "pause"
    );

    expect(paused.state).toBe("paused");
    expect(transport.requests[0]).toMatchObject({
      path: `/v1/jobs/${JOB.job_id}/pause`,
      body: { expected_revision: 4 }
    });
    expect(transport.requests[0]?.headers?.["Idempotency-Key"]).toMatch(
      /^obsidian-[0-9a-f]{64}$/
    );
  });
});

function response(status: number, json: unknown): WorkerTransportResponse {
  return { status, json };
}

class QueueTransport implements WorkerTransport {
  public readonly requests: Array<{
    path: string;
    body?: unknown;
    headers?: Readonly<Record<string, string>>;
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
    } = {}
  ): Promise<WorkerTransportResponse> {
    this.requests.push({
      path,
      ...(options.body === undefined ? {} : { body: options.body }),
      ...(options.headers === undefined ? {} : { headers: options.headers })
    });
    const next = this.responses.shift();
    if (!next) {
      throw new Error("Synthetic response queue exhausted.");
    }
    return next;
  }
}
