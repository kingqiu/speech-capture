import { requestUrl } from "obsidian";

import type { WorkerConnectionSettings } from "./settings";
import type {
  WorkerTransport,
  WorkerTransportResponse
} from "./worker-probe";

export class ObsidianWorkerTransport implements WorkerTransport {
  public async request(
    worker: WorkerConnectionSettings,
    path: string,
    options: {
      readonly method?: "GET" | "POST" | "PUT";
      readonly body?: unknown;
      readonly rawBody?: ArrayBuffer;
      readonly bearerToken?: string;
      readonly headers?: Readonly<Record<string, string>>;
    } = {}
  ): Promise<WorkerTransportResponse> {
    if (options.body !== undefined && options.rawBody !== undefined) {
      throw new Error("A Worker request cannot contain both JSON and binary bodies.");
    }
    try {
      const response = await requestUrl({
        url: `${worker.endpoint}${path}`,
        method: options.method ?? "GET",
        headers: {
          Accept: "application/json",
          ...(options.body === undefined
            ? {}
            : { "Content-Type": "application/json" }),
          ...(options.bearerToken === undefined
            ? {}
            : { Authorization: `Bearer ${options.bearerToken}` }),
          ...options.headers
        },
        ...(options.body === undefined
          ? {}
          : { body: JSON.stringify(options.body) }),
        ...(options.rawBody === undefined ? {} : { body: options.rawBody }),
        throw: false
      });
      return { status: response.status, json: response.json };
    } catch {
      return { status: 0, json: null };
    }
  }

  public async requestBinary(
    worker: WorkerConnectionSettings,
    path: string,
    options: {
      readonly bearerToken: string;
      readonly headers?: Readonly<Record<string, string>>;
    }
  ): Promise<{
    readonly status: number;
    readonly arrayBuffer: ArrayBuffer;
    readonly headers: Readonly<Record<string, string>>;
  }> {
    try {
      const response = await requestUrl({
        url: `${worker.endpoint}${path}`,
        method: "GET",
        headers: {
          Accept: "audio/wav",
          Authorization: `Bearer ${options.bearerToken}`,
          ...options.headers
        },
        throw: false
      });
      return {
        status: response.status,
        arrayBuffer: response.arrayBuffer,
        headers: response.headers
      };
    } catch {
      return {
        status: 0,
        arrayBuffer: new ArrayBuffer(0),
        headers: {}
      };
    }
  }
}
