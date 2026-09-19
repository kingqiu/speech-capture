import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { deflateRawSync } from "node:zlib";
import { describe, expect, it } from "vitest";

import { ClientUpdateController } from "../src/client-update";
import type { WorkerConnectionSettings } from "../src/settings";
import type { WorkerTransportResponse } from "../src/worker-probe";

const WORKER: WorkerConnectionSettings = {
  id: "home",
  displayName: "书房 Mac",
  endpoint: "https://worker.example.test",
  kind: "remote"
};
const TOKEN = "private-test-token";

describe("client update controller", () => {
  it("reports the current version without downloading an archive", async () => {
    const fixture = releaseFixture("0.1.25");
    const transport = new UpdateTransport([response(200, fixture.metadata)], fixture.archive);
    const controller = new ClientUpdateController(
      transport as never,
      "0.1.25",
      "1.13.1"
    );

    await expect(controller.check(WORKER, TOKEN)).resolves.toEqual({
      phase: "current",
      currentVersion: "0.1.25",
      latestVersion: "0.1.25"
    });
    expect(transport.binaryRequests).toHaveLength(0);
  });

  it("downloads, independently verifies, and explicitly confirms a newer package", async () => {
    const fixture = releaseFixture("0.1.26");
    const transport = new UpdateTransport([response(200, fixture.metadata)], fixture.archive);
    const controller = new ClientUpdateController(
      transport as never,
      "0.1.25",
      "1.13.1"
    );

    expect((await controller.check(WORKER, TOKEN)).phase).toBe("available");
    const verified = await controller.downloadAndVerify(WORKER, TOKEN);

    expect(verified).toMatchObject({
      phase: "awaiting_confirmation",
      currentVersion: "0.1.25",
      release: { version: "0.1.26" },
      verification: {
        archiveSha256: digest(fixture.archive),
        fileSha256: { "main.js": fixture.metadata.main_sha256 }
      }
    });
    expect(controller.confirmPreparedUpdate().phase).toBe("confirmed");
    expect(transport.requests).toEqual([
      {
        path: "/v1/client-releases/speech-capture/latest",
        bearerToken: TOKEN
      }
    ]);
    expect(transport.binaryRequests).toEqual([
      {
        path: "/v1/client-releases/speech-capture/0.1.26/archive",
        bearerToken: TOKEN,
        accept: "application/zip"
      }
    ]);
  });

  it("rejects a package with an additional ZIP entry even when its archive hash matches", async () => {
    const fixture = releaseFixture("0.1.26", {
      "speech-capture/extra.txt": new TextEncoder().encode("unexpected")
    });
    const transport = new UpdateTransport([response(200, fixture.metadata)], fixture.archive);
    const controller = new ClientUpdateController(
      transport as never,
      "0.1.25",
      "1.13.1"
    );

    await controller.check(WORKER, TOKEN);
    await expect(controller.downloadAndVerify(WORKER, TOKEN)).resolves.toMatchObject({
      phase: "failed",
      kind: "invalid"
    });
  });

  it("rejects mismatched main.js bytes without changing the active version", async () => {
    const fixture = releaseFixture("0.1.26");
    const metadata = { ...fixture.metadata, main_sha256: "a".repeat(64) };
    const transport = new UpdateTransport([response(200, metadata)], fixture.archive);
    const controller = new ClientUpdateController(
      transport as never,
      "0.1.25",
      "1.13.1"
    );

    await controller.check(WORKER, TOKEN);
    await expect(controller.downloadAndVerify(WORKER, TOKEN)).resolves.toEqual({
      phase: "failed",
      kind: "invalid",
      message: "安装包内的 main.js 校验失败。"
    });
    expect(controller.confirmPreparedUpdate()).toMatchObject({
      phase: "failed",
      kind: "invalid"
    });
  });

  it("blocks a candidate that requires a newer Obsidian version", async () => {
    const fixture = releaseFixture("0.1.26", {}, "2.0.0");
    const transport = new UpdateTransport([response(200, fixture.metadata)], fixture.archive);
    const controller = new ClientUpdateController(
      transport as never,
      "0.1.25",
      "1.13.1"
    );

    await expect(controller.check(WORKER, TOKEN)).resolves.toEqual({
      phase: "incompatible",
      currentVersion: "0.1.25",
      latestVersion: "0.1.26",
      minAppVersion: "2.0.0",
      appVersion: "1.13.1"
    });
  });

  it("keeps rejected credentials distinct from corrupt metadata", async () => {
    const controller = new ClientUpdateController(
      new UpdateTransport([response(401, { error: { code: "AUTHENTICATION_REQUIRED" } })]) as never,
      "0.1.25",
      "1.13.1"
    );

    await expect(controller.check(WORKER, TOKEN)).resolves.toEqual({
      phase: "failed",
      kind: "authentication",
      message: "Worker 授权已失效，请重新连接。"
    });
  });
});

function releaseFixture(
  version: string,
  extraEntries: Readonly<Record<string, Uint8Array>> = {},
  minAppVersion = "1.11.4"
) {
  const files: Readonly<Record<string, Uint8Array>> = {
    "speech-capture/main.js": new TextEncoder().encode("compiled-plugin"),
    "speech-capture/manifest.json": new TextEncoder().encode(
      JSON.stringify({
        id: "speech-capture",
        name: "Speech Capture",
        version,
        minAppVersion,
        isDesktopOnly: true
      })
    ),
    "speech-capture/styles.css": new TextEncoder().encode(".speech-capture {}"),
    ...extraEntries
  };
  const archive = makeStoredZip(files);
  return {
    archive,
    metadata: {
      schema_version: 1,
      plugin_id: "speech-capture",
      version,
      min_app_version: minAppVersion,
      desktop_only: true,
      archive: {
        filename: `speech-capture-${version}-alpha.zip`,
        sha256: digest(archive),
        size_bytes: archive.byteLength
      },
      main_sha256: digest(files["speech-capture/main.js"]!),
      release_manifest_sha256: "b".repeat(64)
    }
  };
}

function makeStoredZip(files: Readonly<Record<string, Uint8Array>>): Uint8Array {
  const locals: Uint8Array[] = [];
  const centrals: Uint8Array[] = [];
  let localOffset = 0;
  for (const [name, content] of Object.entries(files)) {
    const nameBytes = new TextEncoder().encode(name);
    const crc = crc32(content);
    const compressed = new Uint8Array(deflateRawSync(content));
    const local = new Uint8Array(30 + nameBytes.byteLength + compressed.byteLength);
    const localView = new DataView(local.buffer);
    writeUint32(localView, 0, 0x04034b50);
    writeUint16(localView, 4, 20);
    writeUint16(localView, 6, 0x0800);
    writeUint16(localView, 8, 8);
    writeUint32(localView, 14, crc);
    writeUint32(localView, 18, compressed.byteLength);
    writeUint32(localView, 22, content.byteLength);
    writeUint16(localView, 26, nameBytes.byteLength);
    local.set(nameBytes, 30);
    local.set(compressed, 30 + nameBytes.byteLength);
    locals.push(local);

    const central = new Uint8Array(46 + nameBytes.byteLength);
    const centralView = new DataView(central.buffer);
    writeUint32(centralView, 0, 0x02014b50);
    writeUint16(centralView, 4, 20);
    writeUint16(centralView, 6, 20);
    writeUint16(centralView, 8, 0x0800);
    writeUint16(centralView, 10, 8);
    writeUint32(centralView, 16, crc);
    writeUint32(centralView, 20, compressed.byteLength);
    writeUint32(centralView, 24, content.byteLength);
    writeUint16(centralView, 28, nameBytes.byteLength);
    writeUint32(centralView, 42, localOffset);
    central.set(nameBytes, 46);
    centrals.push(central);
    localOffset += local.byteLength;
  }
  const centralSize = centrals.reduce((total, item) => total + item.byteLength, 0);
  const eocd = new Uint8Array(22);
  const eocdView = new DataView(eocd.buffer);
  writeUint32(eocdView, 0, 0x06054b50);
  writeUint16(eocdView, 8, centrals.length);
  writeUint16(eocdView, 10, centrals.length);
  writeUint32(eocdView, 12, centralSize);
  writeUint32(eocdView, 16, localOffset);
  return concatenate([...locals, ...centrals, eocd]);
}

function concatenate(chunks: readonly Uint8Array[]): Uint8Array {
  const result = new Uint8Array(
    chunks.reduce((total, chunk) => total + chunk.byteLength, 0)
  );
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function digest(bytes: Uint8Array): string {
  return bytesToHex(sha256(bytes));
}

function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function writeUint16(view: DataView, offset: number, value: number): void {
  view.setUint16(offset, value, true);
}

function writeUint32(view: DataView, offset: number, value: number): void {
  view.setUint32(offset, value, true);
}

function response(status: number, json: unknown): WorkerTransportResponse {
  return { status, json };
}

class UpdateTransport {
  public readonly requests: Array<{ path: string; bearerToken?: string }> = [];
  public readonly binaryRequests: Array<{
    path: string;
    bearerToken: string;
    accept?: string;
  }> = [];

  public constructor(
    private readonly responses: WorkerTransportResponse[],
    private readonly archive: Uint8Array = new Uint8Array()
  ) {}

  public async request(
    _worker: WorkerConnectionSettings,
    path: string,
    options: { readonly bearerToken?: string } = {}
  ): Promise<WorkerTransportResponse> {
    this.requests.push({
      path,
      ...(options.bearerToken === undefined
        ? {}
        : { bearerToken: options.bearerToken })
    });
    const next = this.responses.shift();
    if (!next) {
      throw new Error("Synthetic response queue exhausted.");
    }
    return next;
  }

  public async requestBinary(
    _worker: WorkerConnectionSettings,
    path: string,
    options: { readonly bearerToken: string; readonly accept?: string }
  ): Promise<{
    status: number;
    arrayBuffer: ArrayBuffer;
    headers: Readonly<Record<string, string>>;
  }> {
    this.binaryRequests.push({ path, ...options });
    const checksum = digest(this.archive);
    return {
      status: 200,
      arrayBuffer: exactBuffer(this.archive),
      headers: { ETag: `"${checksum}"`, "X-Content-SHA256": checksum }
    };
  }
}

function exactBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}
