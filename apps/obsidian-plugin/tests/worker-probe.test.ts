import { describe, expect, it } from "vitest";

import type { WorkerConnectionSettings } from "../src/settings";
import {
  confirmPairingTicket,
  probeWorker,
  REQUIRED_STAGE_I_FEATURES,
  type WorkerTransport,
  type WorkerTransportResponse
} from "../src/worker-probe";

const WORKER: WorkerConnectionSettings = {
  id: "home",
  displayName: "书房 Mac",
  endpoint: "https://worker.example.test",
  kind: "remote"
};

const HEALTH = {
  status: "ok",
  worker_version: "0.1.0a0",
  protocol_version: "1.0.0"
};

const COMPATIBLE = {
  compatible: true,
  issues: [],
  missing_features: [],
  protocol_version: "1.0.0",
  artifact_schema_version: "1.6.0"
};

const READINESS = {
  schema_version: "1.0.0",
  checked_at: "2026-08-03T00:00:00Z",
  worker_version: "0.1.0a0",
  protocol_version: "1.0.0",
  state: "ready",
  endpoint_mode: "private_tls",
  tls_enabled: true,
  storage_ready: true,
  worker_database_ok: true,
  security_database_ok: true,
  ffmpeg_available: true,
  ffprobe_available: true,
  ollama_reachable: true,
  active_model_profile: "accuracy",
  disk_total_bytes: 1_000,
  disk_free_bytes: 800,
  disk_reserve_bytes: 100,
  memory_total_bytes: 1_000,
  memory_available_bytes: 800,
  memory_used_percent: 20,
  swap_used_bytes: 0,
  profiles: [],
  issue_codes: []
};

describe("probeWorker", () => {
  it("requires every Stage I blocking capability", async () => {
    const transport = new QueueTransport([
      response(200, HEALTH),
      response(200, COMPATIBLE)
    ]);

    await probeWorker(transport, WORKER, null);

    expect(transport.requests[1]?.body).toMatchObject({
      required_features: REQUIRED_STAGE_I_FEATURES
    });
  });

  it("does not call a private endpoint before pairing", async () => {
    const transport = new QueueTransport([
      response(200, HEALTH),
      response(200, COMPATIBLE)
    ]);

    await expect(probeWorker(transport, WORKER, null)).resolves.toEqual({
      state: "pairing_required",
      workerVersion: "0.1.0a0"
    });
    expect(transport.requests).toHaveLength(2);
  });

  it("returns authenticated readiness without exposing the token", async () => {
    const transport = new QueueTransport([
      response(200, HEALTH),
      response(200, COMPATIBLE),
      response(200, READINESS)
    ]);

    const result = await probeWorker(transport, WORKER, "private-test-token");

    expect(result.state).toBe("ready");
    expect(transport.requests[2]?.bearerToken).toBe("private-test-token");
    expect(JSON.stringify(result)).not.toContain("private-test-token");
  });

  it("maps rejected credentials back to pairing required", async () => {
    const transport = new QueueTransport([
      response(200, HEALTH),
      response(200, COMPATIBLE),
      response(401, { error: { code: "AUTHENTICATION_REQUIRED" } })
    ]);

    await expect(
      probeWorker(transport, WORKER, "expired-test-token")
    ).resolves.toEqual({
      state: "pairing_required",
      workerVersion: "0.1.0a0"
    });
  });

  it("keeps compatibility failures distinct from reachability", async () => {
    const transport = new QueueTransport([
      response(200, HEALTH),
      response(200, {
        compatible: false,
        issues: ["protocol_version_incompatible"],
        missing_features: ["worker_readiness"],
        protocol_version: null,
        artifact_schema_version: null
      })
    ]);

    await expect(probeWorker(transport, WORKER, null)).resolves.toEqual({
      state: "incompatible",
      issueCodes: [
        "protocol_version_incompatible",
        "missing_capability:worker_readiness"
      ]
    });
  });

  it("does not infer installation state from a connection failure", async () => {
    const transport: WorkerTransport = {
      request: async () => {
        throw new Error("synthetic connection failure");
      }
    };

    await expect(probeWorker(transport, WORKER, null)).resolves.toEqual({
      state: "unreachable"
    });
  });
});

describe("confirmPairingTicket", () => {
  const ticket = `scpair1.${"a".repeat(32)}.synthetic_code`;

  it("sends one opaque field and returns a credential without retaining the ticket", async () => {
    const transport = new QueueTransport([
      response(200, {
        credential_id: `cred_${"b".repeat(32)}`,
        device_id: "laptop_plugin",
        bearer_token: "scw_synthetic-private-token-for-test-only",
        allowed_vault_ids: ["vault_one"],
        generation: 1,
        created_at: "2026-08-03T00:00:00Z"
      })
    ]);

    const result = await confirmPairingTicket(transport, WORKER, ticket);

    expect(result.ok).toBe(true);
    expect(transport.requests[0]?.body).toEqual({ pairing_ticket: ticket });
    expect(JSON.stringify(result)).not.toContain(ticket);
  });

  it("rejects malformed input without making a request", async () => {
    const transport = new QueueTransport([]);

    await expect(confirmPairingTicket(transport, WORKER, "short-code")).resolves.toEqual(
      { ok: false, reason: "invalid" }
    );
    expect(transport.requests).toHaveLength(0);
  });

  it("maps an expired ticket to an actionable result", async () => {
    const transport = new QueueTransport([
      response(410, { error: { code: "PAIRING_SESSION_EXPIRED" } })
    ]);

    await expect(confirmPairingTicket(transport, WORKER, ticket)).resolves.toEqual({
      ok: false,
      reason: "expired"
    });
  });
});

function response(status: number, json: unknown): WorkerTransportResponse {
  return { status, json };
}

class QueueTransport implements WorkerTransport {
  public readonly requests: Array<{
    path: string;
    method?: "GET" | "POST" | "PUT";
    body?: unknown;
    rawBody?: ArrayBuffer;
    bearerToken?: string;
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
    this.requests.push({ path, ...options });
    const next = this.responses.shift();
    if (next === undefined) {
      throw new Error("Synthetic response queue exhausted.");
    }
    return next;
  }
}
