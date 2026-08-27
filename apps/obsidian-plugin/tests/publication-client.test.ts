import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { describe, expect, it } from "vitest";

import {
  acknowledgePublication,
  claimPublication,
  downloadPublicationPackage,
  getPublicationStatus,
  PUBLICATION_FILE_NAMES,
  PublicationClientError
} from "../src/publication-client";
import type { WorkerConnectionSettings } from "../src/settings";
import type { WorkerTransportResponse } from "../src/worker-probe";

const WORKER: WorkerConnectionSettings = {
  id: "home",
  displayName: "书房 Mac",
  endpoint: "https://worker.example.test",
  kind: "remote"
};
const JOB = {
  job_id: `job_${"a".repeat(32)}`,
  state: "processed",
  revision: 4
};
const MANIFEST_SHA = "d".repeat(64);

describe("publication client", () => {
  it("reads status, claims with the current revision, and acknowledges the lease", async () => {
    const status = publicationStatus();
    const lease = publicationLease();
    const transport = new QueuePublicationTransport([
      response(200, status),
      response(200, { created: true, job: { ...JOB, state: "publishing", revision: 5 }, lease }),
      response(200, {
        created: true,
        job: { ...JOB, state: "published", revision: 6 },
        receipt: {
          target_relative_path: "语音笔记/2026-08-03-合成会议",
          manifest_sha256: MANIFEST_SHA,
          published_at: "2026-08-09T10:00:00Z"
        }
      })
    ]);

    await getPublicationStatus(transport, WORKER, "secret", JOB.job_id, "语音笔记");
    const claimed = await claimPublication(transport, WORKER, "secret", {
      job: JOB,
      targetRelativePath: status.suggested_target_relative_path,
      manifestSha256: status.manifest_sha256
    });
    const acknowledged = await acknowledgePublication(transport, WORKER, "secret", {
      jobId: JOB.job_id,
      leaseId: claimed.lease.lease_id,
      manifestSha256: status.manifest_sha256
    });

    expect(transport.requests[0]?.path).toContain("output_root=%E8%AF%AD%E9%9F%B3%E7%AC%94%E8%AE%B0");
    expect(transport.requests[1]).toMatchObject({
      path: `/v1/jobs/${JOB.job_id}/publication-claims`,
      body: {
        expected_revision: 4,
        target_relative_path: "语音笔记/2026-08-03-合成会议",
        manifest_sha256: MANIFEST_SHA,
        lease_seconds: 120
      }
    });
    expect(acknowledged.job.state).toBe("published");
  });

  it("keeps a stale receipt visible so the workbench can republish the new manifest", async () => {
    const status = {
      ...publicationStatus(),
      receipt: {
        target_relative_path: "语音笔记/旧版-V1",
        manifest_sha256: "a".repeat(64),
        published_at: "2026-08-25T10:00:00Z"
      }
    };
    const transport = new QueuePublicationTransport([response(200, status)]);

    const result = await getPublicationStatus(
      transport,
      WORKER,
      "secret",
      JOB.job_id,
      "语音笔记"
    );

    expect(result.manifest_sha256).toBe(MANIFEST_SHA);
    expect(result.receipt?.manifest_sha256).toBe("a".repeat(64));
  });

  it("downloads and verifies every file in the exact publication package", async () => {
    const files = publicationFiles();
    const manifest = files.get("artifact-manifest.json")!;
    const listing = {
      job_id: JOB.job_id,
      speech_id: `sp_${"b".repeat(32)}`,
      manifest_sha256: digest(manifest),
      artifacts: PUBLICATION_FILE_NAMES.map((name) => ({
        name,
        media_type: name.endsWith(".md") ? "text/markdown" : "application/json",
        size_bytes: files.get(name)!.byteLength,
        sha256: digest(files.get(name)!),
        download_path: `/v1/jobs/${JOB.job_id}/artifacts/${name}`
      }))
    };
    const transport = new QueuePublicationTransport([response(200, listing)], files);

    const packageData = await downloadPublicationPackage(
      transport as never,
      WORKER,
      "secret",
      JOB.job_id
    );

    expect(packageData.files.map((item) => item.name)).toEqual(PUBLICATION_FILE_NAMES);
    expect(packageData.manifestSha256).toBe(digest(manifest));
    expect(transport.binaryRequests).toHaveLength(PUBLICATION_FILE_NAMES.length);
  });

  it("rejects a file whose bytes do not match the Worker descriptor", async () => {
    const files = publicationFiles();
    const manifest = files.get("artifact-manifest.json")!;
    const listing = {
      job_id: JOB.job_id,
      speech_id: `sp_${"b".repeat(32)}`,
      manifest_sha256: digest(manifest),
      artifacts: PUBLICATION_FILE_NAMES.map((name) => ({
        name,
        media_type: name.endsWith(".md") ? "text/markdown" : "application/json",
        size_bytes: files.get(name)!.byteLength,
        sha256: digest(files.get(name)!),
        download_path: `/v1/jobs/${JOB.job_id}/artifacts/${name}`
      }))
    };
    files.set("note.md", new TextEncoder().encode("tampered note"));
    const transport = new QueuePublicationTransport([response(200, listing)], files);

    await expect(
      downloadPublicationPackage(transport as never, WORKER, "secret", JOB.job_id)
    ).rejects.toMatchObject({ kind: "invalid" } satisfies Partial<PublicationClientError>);
  });
});

function publicationStatus() {
  return {
    job: JOB,
    suggested_target_relative_path: "语音笔记/2026-08-03-合成会议",
    manifest_sha256: MANIFEST_SHA,
    artifact_count: 7,
    active_lease: null,
    receipt: null
  };
}

function publicationLease() {
  return {
    lease_id: `lease_${"c".repeat(32)}`,
    target_relative_path: "语音笔记/2026-08-03-合成会议",
    manifest_sha256: MANIFEST_SHA,
    expires_at: "2026-08-09T10:02:00Z",
    generation: 1,
    owned_by_caller: true
  };
}

function publicationFiles(): Map<string, Uint8Array> {
  return new Map(
    PUBLICATION_FILE_NAMES.map((name) => [
      name,
      new TextEncoder().encode(
        name === "note.md" ? "# 合成会议\n\n- 已确认发布流程。" : `{\"file\":\"${name}\"}`
      )
    ])
  );
}

function digest(bytes: Uint8Array): string {
  return bytesToHex(sha256(bytes));
}

function response(status: number, json: unknown): WorkerTransportResponse {
  return { status, json };
}

class QueuePublicationTransport {
  public readonly requests: Array<{ path: string; body?: unknown }> = [];
  public readonly binaryRequests: string[] = [];

  public constructor(
    private readonly responses: WorkerTransportResponse[],
    private readonly files = new Map<string, Uint8Array>()
  ) {}

  public async request(
    _worker: WorkerConnectionSettings,
    path: string,
    options: { readonly body?: unknown } = {}
  ): Promise<WorkerTransportResponse> {
    this.requests.push({ path, body: options.body });
    const next = this.responses.shift();
    if (!next) {
      throw new Error("Synthetic response queue exhausted.");
    }
    return next;
  }

  public async requestBinary(
    _worker: WorkerConnectionSettings,
    path: string
  ): Promise<{ status: number; arrayBuffer: ArrayBuffer; headers: Record<string, string> }> {
    this.binaryRequests.push(path);
    const name = path.slice(path.lastIndexOf("/") + 1);
    const bytes = this.files.get(name);
    if (!bytes) {
      return { status: 404, arrayBuffer: new ArrayBuffer(0), headers: {} };
    }
    return {
      status: 200,
      arrayBuffer: exactBuffer(bytes),
      headers: {}
    };
  }
}

function exactBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}
