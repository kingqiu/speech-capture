import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { inflateRawSync } from "node:zlib";

import type { ClientReleaseSchema } from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

import type { WorkerConnectionSettings } from "./settings";
import type { WorkerTransport, WorkerTransportResponse } from "./worker-probe";

const PLUGIN_ID = "speech-capture";
const MAX_ARCHIVE_BYTES = 16 * 1024 * 1024;
const MAX_RELEASE_FILE_BYTES = 8 * 1024 * 1024;
const RELEASE_FILE_NAMES = Object.freeze([
  "main.js",
  "manifest.json",
  "styles.css"
] as const);
const EXPECTED_ARCHIVE_ENTRIES = Object.freeze(
  RELEASE_FILE_NAMES.map((name) => `${PLUGIN_ID}/${name}`)
);

type ReleaseFileName = (typeof RELEASE_FILE_NAMES)[number];

export interface VerifiedClientRelease {
  readonly version: string;
  readonly minAppVersion: string;
  readonly archiveFilename: string;
  readonly archiveSizeBytes: number;
  readonly archiveSha256: string;
  readonly releaseManifestSha256: string;
  readonly fileSha256: Readonly<Record<ReleaseFileName, string>>;
}

export type ClientUpdateState =
  | { readonly phase: "idle" }
  | { readonly phase: "checking" }
  | {
      readonly phase: "current";
      readonly currentVersion: string;
      readonly latestVersion: string;
    }
  | {
      readonly phase: "incompatible";
      readonly currentVersion: string;
      readonly latestVersion: string;
      readonly minAppVersion: string;
      readonly appVersion: string;
    }
  | {
      readonly phase: "available" | "downloading";
      readonly currentVersion: string;
      readonly release: ClientReleaseSchema;
    }
  | {
      readonly phase: "awaiting_confirmation" | "confirmed";
      readonly currentVersion: string;
      readonly release: ClientReleaseSchema;
      readonly verification: VerifiedClientRelease;
    }
  | {
      readonly phase: "failed";
      readonly kind: "authentication" | "unavailable" | "invalid";
      readonly message: string;
    };

export class ClientUpdateError extends Error {
  public constructor(
    public readonly kind: "authentication" | "unavailable" | "invalid",
    message: string
  ) {
    super(message);
    this.name = "ClientUpdateError";
  }
}

export interface ClientReleaseTransport extends WorkerTransport {
  requestBinary(
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
  }>;
}

export class ClientUpdateController {
  public state: ClientUpdateState = { phase: "idle" };

  private verifiedArchive: Uint8Array | null = null;

  public constructor(
    private readonly transport: ClientReleaseTransport,
    private readonly currentVersion: string,
    private readonly appVersion: string
  ) {}

  public async check(
    worker: WorkerConnectionSettings,
    bearerToken: string
  ): Promise<ClientUpdateState> {
    this.verifiedArchive = null;
    this.state = { phase: "checking" };
    try {
      const response = await this.transport.request(
        worker,
        "/v1/client-releases/speech-capture/latest",
        { bearerToken }
      );
      const release = parseReleaseResponse(response);
      const comparison = compareVersions(release.version, this.currentVersion);
      if (comparison < 0) {
        throw new ClientUpdateError(
          "invalid",
          "Worker 提供的版本低于当前插件，已停止更新检查。"
        );
      }
      if (comparison === 0) {
        this.state = {
          phase: "current",
          currentVersion: this.currentVersion,
          latestVersion: release.version
        };
        return this.state;
      }
      if (compareVersions(this.appVersion, release.min_app_version) < 0) {
        this.state = {
          phase: "incompatible",
          currentVersion: this.currentVersion,
          latestVersion: release.version,
          minAppVersion: release.min_app_version,
          appVersion: this.appVersion
        };
        return this.state;
      }
      this.state = {
        phase: "available",
        currentVersion: this.currentVersion,
        release
      };
      return this.state;
    } catch (error) {
      this.state = failedState(error);
      return this.state;
    }
  }

  public async downloadAndVerify(
    worker: WorkerConnectionSettings,
    bearerToken: string
  ): Promise<ClientUpdateState> {
    if (this.state.phase !== "available") {
      return this.failInvalid("请先完成更新检查，再下载候选版本。");
    }
    const release = this.state.release;
    this.state = {
      phase: "downloading",
      currentVersion: this.currentVersion,
      release
    };
    this.verifiedArchive = null;
    try {
      const response = await this.transport.requestBinary(
        worker,
        `/v1/client-releases/speech-capture/${encodeURIComponent(release.version)}/archive`,
        { bearerToken, accept: "application/zip" }
      );
      assertDownloadResponse(response.status);
      const contentSha256 = responseHeader(response.headers, "x-content-sha256");
      const etag = responseHeader(response.headers, "etag");
      if (
        contentSha256 !== release.archive.sha256 ||
        etag !== `"${release.archive.sha256}"`
      ) {
        throw new ClientUpdateError(
          "invalid",
          "下载响应与候选版本元数据不一致。"
        );
      }
      const archive = new Uint8Array(response.arrayBuffer);
      if (
        archive.byteLength !== release.archive.size_bytes ||
        archive.byteLength > MAX_ARCHIVE_BYTES ||
        bytesToHex(sha256(archive)) !== release.archive.sha256
      ) {
        throw new ClientUpdateError("invalid", "安装包完整性检查未通过。");
      }
      const files = readVerifiedPluginArchive(archive);
      const manifest = parsePluginManifest(files["manifest.json"]);
      if (
        manifest.id !== PLUGIN_ID ||
        manifest.version !== release.version ||
        manifest.minAppVersion !== release.min_app_version ||
        manifest.isDesktopOnly !== true
      ) {
        throw new ClientUpdateError("invalid", "安装包内的插件身份或版本不匹配。");
      }
      const mainSha256 = bytesToHex(sha256(files["main.js"]));
      if (mainSha256 !== release.main_sha256) {
        throw new ClientUpdateError("invalid", "安装包内的 main.js 校验失败。");
      }
      const verification: VerifiedClientRelease = {
        version: release.version,
        minAppVersion: release.min_app_version,
        archiveFilename: release.archive.filename,
        archiveSizeBytes: archive.byteLength,
        archiveSha256: release.archive.sha256,
        releaseManifestSha256: release.release_manifest_sha256,
        fileSha256: {
          "main.js": mainSha256,
          "manifest.json": bytesToHex(sha256(files["manifest.json"])),
          "styles.css": bytesToHex(sha256(files["styles.css"]))
        }
      };
      this.verifiedArchive = archive;
      this.state = {
        phase: "awaiting_confirmation",
        currentVersion: this.currentVersion,
        release,
        verification
      };
      return this.state;
    } catch (error) {
      this.verifiedArchive = null;
      this.state = failedState(error);
      return this.state;
    }
  }

  public confirmPreparedUpdate(): ClientUpdateState {
    if (
      this.state.phase !== "awaiting_confirmation" ||
      this.verifiedArchive === null
    ) {
      return this.failInvalid("没有可确认的已验证安装包。");
    }
    this.state = { ...this.state, phase: "confirmed" };
    return this.state;
  }

  public reset(): void {
    this.verifiedArchive = null;
    this.state = { phase: "idle" };
  }

  private failInvalid(message: string): ClientUpdateState {
    this.verifiedArchive = null;
    this.state = { phase: "failed", kind: "invalid", message };
    return this.state;
  }
}

function parseReleaseResponse(response: WorkerTransportResponse): ClientReleaseSchema {
  if (response.status === 401 || response.status === 403) {
    throw new ClientUpdateError("authentication", "Worker 授权已失效，请重新连接。");
  }
  if (response.status === 404) {
    throw new ClientUpdateError("unavailable", "Worker 尚未提供可用的插件版本。");
  }
  if (response.status === 0 || response.status >= 500) {
    throw new ClientUpdateError("unavailable", "暂时无法从 Worker 检查插件更新。");
  }
  if (response.status !== 200 || !isClientRelease(response.json)) {
    throw new ClientUpdateError("invalid", "Worker 返回了无法识别的插件版本信息。");
  }
  return response.json;
}

function isClientRelease(value: unknown): value is ClientReleaseSchema {
  if (!isRecord(value) || !isRecord(value.archive)) {
    return false;
  }
  const version = canonicalVersion(value.version);
  const minAppVersion = canonicalVersion(value.min_app_version);
  return (
    value.schema_version === 1 &&
    value.plugin_id === PLUGIN_ID &&
    value.desktop_only === true &&
    version !== null &&
    minAppVersion !== null &&
    value.archive.filename === `${PLUGIN_ID}-${version}-alpha.zip` &&
    isSha256(value.archive.sha256) &&
    Number.isSafeInteger(value.archive.size_bytes) &&
    (value.archive.size_bytes as number) > 0 &&
    (value.archive.size_bytes as number) <= MAX_ARCHIVE_BYTES &&
    isSha256(value.main_sha256) &&
    isSha256(value.release_manifest_sha256)
  );
}

function assertDownloadResponse(status: number): void {
  if (status === 401 || status === 403) {
    throw new ClientUpdateError("authentication", "Worker 授权已失效，请重新连接。");
  }
  if (status === 0 || status >= 500) {
    throw new ClientUpdateError("unavailable", "暂时无法从 Worker 下载安装包。");
  }
  if (status !== 200) {
    throw new ClientUpdateError("invalid", "Worker 未返回指定版本的安装包。");
  }
}

function readVerifiedPluginArchive(
  archive: Uint8Array
): Record<ReleaseFileName, Uint8Array> {
  const view = new DataView(archive.buffer, archive.byteOffset, archive.byteLength);
  const eocdOffset = findEndOfCentralDirectory(view);
  const disk = readUint16(view, eocdOffset + 4);
  const centralDisk = readUint16(view, eocdOffset + 6);
  const diskEntries = readUint16(view, eocdOffset + 8);
  const totalEntries = readUint16(view, eocdOffset + 10);
  const centralSize = readUint32(view, eocdOffset + 12);
  const centralOffset = readUint32(view, eocdOffset + 16);
  const commentLength = readUint16(view, eocdOffset + 20);
  if (
    disk !== 0 ||
    centralDisk !== 0 ||
    diskEntries !== EXPECTED_ARCHIVE_ENTRIES.length ||
    totalEntries !== EXPECTED_ARCHIVE_ENTRIES.length ||
    commentLength !== 0 ||
    eocdOffset + 22 !== archive.byteLength ||
    centralOffset + centralSize !== eocdOffset
  ) {
    throw invalidArchive();
  }

  const result = new Map<string, Uint8Array>();
  let cursor = centralOffset;
  let expectedLocalOffset = 0;
  for (let index = 0; index < totalEntries; index += 1) {
    assertBounds(archive, cursor, 46);
    if (readUint32(view, cursor) !== 0x02014b50) {
      throw invalidArchive();
    }
    const flags = readUint16(view, cursor + 8);
    const method = readUint16(view, cursor + 10);
    const crc = readUint32(view, cursor + 16);
    const compressedSize = readUint32(view, cursor + 20);
    const uncompressedSize = readUint32(view, cursor + 24);
    const nameLength = readUint16(view, cursor + 28);
    const extraLength = readUint16(view, cursor + 30);
    const entryCommentLength = readUint16(view, cursor + 32);
    const startDisk = readUint16(view, cursor + 34);
    const externalAttributes = readUint32(view, cursor + 38);
    const localOffset = readUint32(view, cursor + 42);
    const headerSize = 46 + nameLength + extraLength + entryCommentLength;
    assertBounds(archive, cursor, headerSize);
    const name = decodeArchiveName(archive.subarray(cursor + 46, cursor + 46 + nameLength));
    if (
      name !== EXPECTED_ARCHIVE_ENTRIES[index] ||
      result.has(name) ||
      (flags & ~0x0800) !== 0 ||
      ![0, 8].includes(method) ||
      extraLength !== 0 ||
      entryCommentLength !== 0 ||
      startDisk !== 0 ||
      localOffset !== expectedLocalOffset ||
      uncompressedSize <= 0 ||
      uncompressedSize > MAX_RELEASE_FILE_BYTES ||
      ((externalAttributes >>> 16) & 0o170000) === 0o120000
    ) {
      throw invalidArchive();
    }
    const local = readLocalEntry(
      archive,
      view,
      localOffset,
      name,
      flags,
      method,
      compressedSize,
      uncompressedSize
    );
    if (crc32(local.content) !== crc) {
      throw invalidArchive();
    }
    result.set(name, local.content);
    expectedLocalOffset = local.nextOffset;
    cursor += headerSize;
  }
  if (
    cursor !== eocdOffset ||
    expectedLocalOffset !== centralOffset ||
    result.size !== EXPECTED_ARCHIVE_ENTRIES.length
  ) {
    throw invalidArchive();
  }
  return Object.fromEntries(
    RELEASE_FILE_NAMES.map((name) => [name, result.get(`${PLUGIN_ID}/${name}`)!])
  ) as unknown as Record<ReleaseFileName, Uint8Array>;
}

function readLocalEntry(
  archive: Uint8Array,
  view: DataView,
  offset: number,
  expectedName: string,
  expectedFlags: number,
  expectedMethod: number,
  compressedSize: number,
  uncompressedSize: number
): { readonly content: Uint8Array; readonly nextOffset: number } {
  assertBounds(archive, offset, 30);
  if (readUint32(view, offset) !== 0x04034b50) {
    throw invalidArchive();
  }
  const flags = readUint16(view, offset + 6);
  const method = readUint16(view, offset + 8);
  const localCompressedSize = readUint32(view, offset + 18);
  const localUncompressedSize = readUint32(view, offset + 22);
  const nameLength = readUint16(view, offset + 26);
  const extraLength = readUint16(view, offset + 28);
  const dataOffset = offset + 30 + nameLength + extraLength;
  assertBounds(archive, offset, 30 + nameLength + extraLength + compressedSize);
  const name = decodeArchiveName(archive.subarray(offset + 30, offset + 30 + nameLength));
  if (
    name !== expectedName ||
    flags !== expectedFlags ||
    method !== expectedMethod ||
    extraLength !== 0 ||
    localCompressedSize !== compressedSize ||
    localUncompressedSize !== uncompressedSize
  ) {
    throw invalidArchive();
  }
  const compressed = archive.subarray(dataOffset, dataOffset + compressedSize);
  let content: Uint8Array;
  try {
    content =
      method === 0
        ? compressed.slice()
        : new Uint8Array(
            inflateRawSync(compressed, { maxOutputLength: MAX_RELEASE_FILE_BYTES })
          );
  } catch {
    throw invalidArchive();
  }
  if (content.byteLength !== uncompressedSize) {
    throw invalidArchive();
  }
  return { content, nextOffset: dataOffset + compressedSize };
}

function findEndOfCentralDirectory(view: DataView): number {
  const minimum = Math.max(0, view.byteLength - 65_557);
  for (let offset = view.byteLength - 22; offset >= minimum; offset -= 1) {
    if (readUint32(view, offset) === 0x06054b50) {
      return offset;
    }
  }
  throw invalidArchive();
}

function parsePluginManifest(bytes: Uint8Array): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    if (!isRecord(value)) {
      throw invalidArchive();
    }
    return value;
  } catch (error) {
    if (error instanceof ClientUpdateError) {
      throw error;
    }
    throw invalidArchive();
  }
}

function responseHeader(
  headers: Readonly<Record<string, string>>,
  name: string
): string | null {
  const target = name.toLowerCase();
  for (const [headerName, value] of Object.entries(headers)) {
    if (headerName.toLowerCase() === target) {
      return value.trim();
    }
  }
  return null;
}

function compareVersions(left: string, right: string): number {
  const leftParts = parseVersion(left);
  const rightParts = parseVersion(right);
  for (let index = 0; index < 3; index += 1) {
    const difference = leftParts[index]! - rightParts[index]!;
    if (difference !== 0) {
      return Math.sign(difference);
    }
  }
  return 0;
}

function canonicalVersion(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  try {
    parseVersion(value);
    return value;
  } catch {
    return null;
  }
}

function parseVersion(value: string): readonly [number, number, number] {
  const match = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.exec(value);
  if (!match) {
    throw new ClientUpdateError("invalid", "插件版本号格式不受支持。");
  }
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function failedState(error: unknown): Extract<ClientUpdateState, { phase: "failed" }> {
  if (error instanceof ClientUpdateError) {
    return { phase: "failed", kind: error.kind, message: error.message };
  }
  return {
    phase: "failed",
    kind: "invalid",
    message: "插件更新检查发生未预期异常，活动插件未被修改。"
  };
}

function invalidArchive(): ClientUpdateError {
  return new ClientUpdateError("invalid", "安装包结构或文件校验未通过。");
}

function assertBounds(bytes: Uint8Array, offset: number, length: number): void {
  if (
    !Number.isSafeInteger(offset) ||
    !Number.isSafeInteger(length) ||
    offset < 0 ||
    length < 0 ||
    offset + length > bytes.byteLength
  ) {
    throw invalidArchive();
  }
}

function readUint16(view: DataView, offset: number): number {
  if (offset < 0 || offset + 2 > view.byteLength) {
    throw invalidArchive();
  }
  return view.getUint16(offset, true);
}

function readUint32(view: DataView, offset: number): number {
  if (offset < 0 || offset + 4 > view.byteLength) {
    throw invalidArchive();
  }
  return view.getUint32(offset, true);
}

function decodeArchiveName(bytes: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw invalidArchive();
  }
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

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
