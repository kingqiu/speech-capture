import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  agentConstructed: vi.fn(),
  agentDestroyed: vi.fn(),
  nodeRequest: vi.fn(),
  requestUrl: vi.fn()
}));

vi.mock("obsidian", () => ({ requestUrl: mocks.requestUrl }));

vi.mock("node:https", () => ({
  Agent: class {
    public constructor(options: unknown) {
      mocks.agentConstructed(options);
    }

    public destroy(): void {
      mocks.agentDestroyed();
    }
  },
  request: mocks.nodeRequest
}));

import {
  closeObsidianWorkerTransportPool,
  ObsidianWorkerTransport
} from "../src/obsidian-worker-transport";
import type { WorkerConnectionSettings } from "../src/settings";

const worker: WorkerConnectionSettings = {
  id: "remote-worker",
  displayName: "书房 Mac",
  endpoint: "https://worker.example.test",
  kind: "remote"
};

type RequestListener = (error: unknown) => void;
type ResponseListener = (chunk?: Uint8Array) => void;

function failedRequest(code: string) {
  let errorListener: RequestListener | null = null;
  return {
    on(event: string, listener: RequestListener) {
      if (event === "error") {
        errorListener = listener;
      }
    },
    once() {},
    write() {
      return true;
    },
    setTimeout() {},
    destroy(error: Error) {
      errorListener?.(error);
    },
    end() {
      errorListener?.(Object.assign(new Error(code), { code }));
    }
  };
}

function successfulRequest(
  responseCallback: (response: {
    readonly statusCode: number;
    readonly headers: Readonly<Record<string, string>>;
    on(event: string, listener: ResponseListener): void;
  }) => void
) {
  const responseListeners = new Map<string, ResponseListener>();
  responseCallback({
    statusCode: 200,
    headers: { "content-type": "application/json" },
    on(event, listener) {
      responseListeners.set(event, listener);
    }
  });
  return {
    on() {},
    once() {},
    write() {
      return true;
    },
    setTimeout() {},
    destroy() {},
    end() {
      responseListeners.get("data")?.(
        new TextEncoder().encode('{"status":"ok"}')
      );
      responseListeners.get("end")?.();
    }
  };
}

describe("Obsidian remote Worker transport", () => {
  it("rebuilds the HTTPS pool and retries one failed read request", async () => {
    mocks.nodeRequest.mockReset();
    mocks.agentConstructed.mockClear();
    mocks.agentDestroyed.mockClear();
    closeObsidianWorkerTransportPool();
    mocks.agentDestroyed.mockClear();

    mocks.nodeRequest
      .mockImplementationOnce(() => failedRequest("ECONNRESET"))
      .mockImplementationOnce((_url, _options, callback) =>
        successfulRequest(callback)
      );

    const response = await new ObsidianWorkerTransport().request(
      worker,
      "/v1/health"
    );

    expect(response).toEqual({ status: 200, json: { status: "ok" } });
    expect(mocks.nodeRequest).toHaveBeenCalledTimes(2);
    expect(mocks.agentConstructed).toHaveBeenCalledTimes(2);
    expect(mocks.agentDestroyed).toHaveBeenCalledTimes(1);
  });

  it("does not replay a failed write request", async () => {
    mocks.nodeRequest.mockReset();
    closeObsidianWorkerTransportPool();
    mocks.nodeRequest.mockImplementationOnce(() => failedRequest("ETIMEDOUT"));

    const response = await new ObsidianWorkerTransport().request(
      worker,
      "/v1/jobs/job-1/deletion",
      { method: "POST", body: { expected_revision: 1 } }
    );

    expect(response).toMatchObject({
      status: 0,
      error: "桌面 HTTPS 网络层返回 ETIMEDOUT"
    });
    expect(mocks.nodeRequest).toHaveBeenCalledTimes(1);
  });
});
