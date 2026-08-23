import { requestUrl } from "obsidian";
import {
  Agent as nodeHttpsAgentConstructor,
  request as nodeHttpsRequest
} from "node:https";

import type { WorkerConnectionSettings } from "./settings";
import {
  writeNodeRequestBody,
  type NodeWritableRequest
} from "./node-upload-writer";
import type {
  WorkerTransport,
  WorkerTransportResponse
} from "./worker-probe";

type NodeHttpsAgent = { destroy(): void };
type NodeHttpsAgentConstructor = new (options: {
  readonly keepAlive: boolean;
  readonly keepAliveMsecs: number;
  readonly maxSockets: number;
  readonly maxFreeSockets: number;
}) => NodeHttpsAgent;

const remoteHttpsAgent = new (
  nodeHttpsAgentConstructor as unknown as NodeHttpsAgentConstructor
)({
  keepAlive: true,
  keepAliveMsecs: 30_000,
  maxSockets: 2,
  maxFreeSockets: 1
});

export function closeObsidianWorkerTransportPool(): void {
  remoteHttpsAgent.destroy();
}

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
      readonly onUploadProgress?: (uploadedBytes: number) => void;
    } = {}
  ): Promise<WorkerTransportResponse> {
    if (options.body !== undefined && options.rawBody !== undefined) {
      throw new Error("A Worker request cannot contain both JSON and binary bodies.");
    }
    if (worker.endpoint.startsWith("https://")) {
      return requestJsonWithNodeHttps(worker, path, options);
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
      if (options.rawBody !== undefined) {
        options.onUploadProgress?.(options.rawBody.byteLength);
      }
      return { status: response.status, json: response.json };
    } catch (error) {
      return {
        status: 0,
        json: null,
        error: describeRequestFailure(error, "Obsidian 网络层")
      };
    }
  }

  public async requestBinary(
    worker: WorkerConnectionSettings,
    path: string,
    options: {
      readonly bearerToken: string;
      readonly accept?: string;
      readonly headers?: Readonly<Record<string, string>>;
    }
  ): Promise<{
    readonly status: number;
    readonly arrayBuffer: ArrayBuffer;
    readonly headers: Readonly<Record<string, string>>;
  }> {
    if (worker.endpoint.startsWith("https://")) {
      try {
        const response = await requestWithNodeHttps(worker, path, {
          method: "GET",
          timeoutMs: 5 * 60_000,
          headers: {
            Accept: options.accept ?? "audio/wav",
            Authorization: `Bearer ${options.bearerToken}`,
            ...options.headers
          }
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
    try {
      const response = await requestUrl({
        url: `${worker.endpoint}${path}`,
        method: "GET",
        headers: {
          Accept: options.accept ?? "audio/wav",
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

type NodeHttpsResponse = {
  readonly status: number;
  readonly arrayBuffer: ArrayBuffer;
  readonly headers: Readonly<Record<string, string>>;
};

type NodeHttpsRequest = NodeWritableRequest & {
  on(event: "error", listener: (error: unknown) => void): void;
  setTimeout(milliseconds: number, callback: () => void): void;
  destroy(error: Error): void;
};

type NodeHttpsIncomingMessage = {
  readonly statusCode?: number;
  readonly headers: Readonly<Record<string, string | readonly string[] | undefined>>;
  on(event: "data", listener: (chunk: Uint8Array) => void): void;
  on(event: "end", listener: () => void): void;
  on(event: "error", listener: (error: unknown) => void): void;
};

type NodeHttpsRequestFactory = (
  url: URL,
  options: {
    readonly method: string;
    readonly headers: Readonly<Record<string, string>>;
    readonly agent: NodeHttpsAgent;
  },
  listener: (response: NodeHttpsIncomingMessage) => void
) => NodeHttpsRequest;

async function requestJsonWithNodeHttps(
  worker: WorkerConnectionSettings,
  path: string,
  options: {
    readonly method?: "GET" | "POST" | "PUT";
    readonly body?: unknown;
    readonly rawBody?: ArrayBuffer;
    readonly bearerToken?: string;
    readonly headers?: Readonly<Record<string, string>>;
    readonly onUploadProgress?: (uploadedBytes: number) => void;
  }
): Promise<WorkerTransportResponse> {
  try {
    const encodedBody =
      options.body !== undefined
        ? JSON.stringify(options.body)
        : options.rawBody !== undefined
          ? new Uint8Array(options.rawBody)
          : undefined;
    const response = await requestWithNodeHttps(worker, path, {
      method: options.method ?? "GET",
      timeoutMs: options.rawBody === undefined ? 30_000 : 15 * 60_000,
      ...(encodedBody === undefined ? {} : { body: encodedBody }),
      ...(options.onUploadProgress === undefined
        ? {}
        : { onUploadProgress: options.onUploadProgress }),
      headers: {
        Accept: "application/json",
        ...(options.body === undefined
          ? {}
          : { "Content-Type": "application/json" }),
        ...(options.bearerToken === undefined
          ? {}
          : { Authorization: `Bearer ${options.bearerToken}` }),
        ...options.headers
      }
    });
    const text = new TextDecoder().decode(response.arrayBuffer);
    let json: unknown = null;
    if (text.trim()) {
      try {
        json = JSON.parse(text) as unknown;
      } catch {
        json = null;
      }
    }
    return { status: response.status, json };
  } catch (error) {
    return {
      status: 0,
      json: null,
      error: describeRequestFailure(error, "桌面 HTTPS 网络层")
    };
  }
}

async function requestWithNodeHttps(
  worker: WorkerConnectionSettings,
  path: string,
  options: {
    readonly method: "GET" | "POST" | "PUT";
    readonly timeoutMs: number;
    readonly body?: string | Uint8Array;
    readonly headers: Readonly<Record<string, string>>;
    readonly onUploadProgress?: (uploadedBytes: number) => void;
  }
): Promise<NodeHttpsResponse> {
  const bodyLength =
    typeof options.body === "string"
      ? new TextEncoder().encode(options.body).byteLength
      : options.body?.byteLength;
  const headers = {
    ...options.headers,
    ...(bodyLength === undefined ? {} : { "Content-Length": String(bodyLength) })
  };
  return new Promise((resolve, reject) => {
    const chunks: Uint8Array[] = [];
    const request = (nodeHttpsRequest as unknown as NodeHttpsRequestFactory)(
      new URL(path, `${worker.endpoint}/`),
      { method: options.method, headers, agent: remoteHttpsAgent },
      (response) => {
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("error", reject);
        response.on("end", () => {
          const byteLength = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
          const bytes = new Uint8Array(byteLength);
          let offset = 0;
          for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.byteLength;
          }
          resolve({
            status: response.statusCode ?? 0,
            arrayBuffer: bytes.buffer,
            headers: normalizeNodeHeaders(response.headers)
          });
        });
      }
    );
    request.on("error", reject);
    request.setTimeout(options.timeoutMs, () => request.destroy(new Error("ETIMEDOUT")));
    if (options.body === undefined) {
      request.end();
    } else {
      writeNodeRequestBody(request, options.body, options.onUploadProgress);
    }
  });
}

function normalizeNodeHeaders(
  headers: Readonly<Record<string, string | readonly string[] | undefined>>
): Readonly<Record<string, string>> {
  const normalized: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined) {
      normalized[name] = typeof value === "string" ? value : value.join(", ");
    }
  }
  return normalized;
}

function describeRequestFailure(error: unknown, transport: string): string {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "未知网络错误";
  const networkCode = message.match(/(?:net::)?(ERR_[A-Z_]+)|\b(E[A-Z]{3,})\b/)?.[1] ??
    message.match(/(?:net::)?(ERR_[A-Z_]+)|\b(E[A-Z]{3,})\b/)?.[2];
  if (networkCode) {
    return `${transport}返回 ${networkCode}`;
  }
  const sanitized = message
    .replace(/https?:\/\/[^\s"']+/gi, "远程地址")
    .replace(/\/Users\/[^\s"']+/g, "本机路径")
    .replace(/\s+/g, " ")
    .trim();
  return sanitized ? `${transport}请求失败：${sanitized.slice(0, 160)}` : `${transport}请求失败`;
}
