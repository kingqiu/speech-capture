import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { normalizePath, type DataAdapter } from "obsidian";

import type { DownloadedPublicationPackage } from "./publication-client";
import { PUBLICATION_FILE_NAMES } from "./publication-client";

export interface PublicationConflictDiff {
  readonly changedFiles: readonly string[];
  readonly missingFiles: readonly string[];
  readonly extraEntries: readonly string[];
  readonly currentNoteHighlights: readonly string[];
  readonly workerNoteHighlights: readonly string[];
}

export type PublicationTargetInspection =
  | { readonly kind: "available" }
  | { readonly kind: "matching" }
  | { readonly kind: "conflict"; readonly diff: PublicationConflictDiff };

export class VaultPublicationError extends Error {
  public constructor(
    public readonly kind: "conflict" | "verification" | "unsafe",
    message: string
  ) {
    super(message);
    this.name = "VaultPublicationError";
  }
}

export async function inspectPublicationTarget(
  adapter: DataAdapter,
  targetRelativePath: string,
  packageData: DownloadedPublicationPackage
): Promise<PublicationTargetInspection> {
  const target = safeRelativePath(targetRelativePath);
  const targetStat = await adapter.stat(target);
  if (targetStat === null) {
    return { kind: "available" };
  }
  if (targetStat.type !== "folder") {
    return {
      kind: "conflict",
      diff: emptyConflictDiff([], [], [leafName(target)])
    };
  }
  const listed = await adapter.list(target);
  const actualFiles = listed.files.map(leafName).sort();
  const actualFolders = listed.folders.map(leafName).sort();
  const expected = [...PUBLICATION_FILE_NAMES].sort();
  const missingFiles = expected.filter((name) => !actualFiles.includes(name));
  const extraEntries = [
    ...actualFiles.filter((name) => !expected.includes(name as typeof expected[number])),
    ...actualFolders
  ].sort();
  const changedFiles: string[] = [];
  for (const file of packageData.files) {
    if (!actualFiles.includes(file.name)) {
      continue;
    }
    const content = new Uint8Array(await adapter.readBinary(`${target}/${file.name}`));
    if (bytesToHex(sha256(content)) !== file.sha256) {
      changedFiles.push(file.name);
    }
  }
  if (!missingFiles.length && !extraEntries.length && !changedFiles.length) {
    return { kind: "matching" };
  }
  const currentNote = actualFiles.includes("note.md")
    ? new Uint8Array(await adapter.readBinary(`${target}/note.md`))
    : null;
  const workerNote = packageData.files.find((item) => item.name === "note.md")?.bytes ?? null;
  const noteHighlights = buildNoteHighlights(currentNote, workerNote);
  return {
    kind: "conflict",
    diff: {
      changedFiles: changedFiles.sort(),
      missingFiles,
      extraEntries,
      ...noteHighlights
    }
  };
}

export async function writePublicationPackage(
  adapter: DataAdapter,
  request: {
    readonly targetRelativePath: string;
    readonly leaseId: string;
    readonly packageData: DownloadedPublicationPackage;
  }
): Promise<"written" | "already_present"> {
  const target = safeRelativePath(request.targetRelativePath);
  const inspection = await inspectPublicationTarget(adapter, target, request.packageData);
  if (inspection.kind === "matching") {
    return "already_present";
  }
  if (inspection.kind === "conflict") {
    throw new VaultPublicationError("conflict", "目标位置已有不同内容。");
  }
  const parent = parentPath(target);
  await ensureFolderPath(adapter, parent);
  const temporary = `${parent ? `${parent}/` : ""}.${leafName(target)}.${request.leaseId}.tmp`;
  if (await adapter.exists(temporary)) {
    throw new VaultPublicationError("conflict", "发布临时位置已存在。");
  }
  await adapter.mkdir(temporary);
  try {
    for (const file of request.packageData.files) {
      await adapter.writeBinary(`${temporary}/${file.name}`, exactArrayBuffer(file.bytes));
    }
    const temporaryInspection = await inspectPublicationTarget(
      adapter,
      temporary,
      request.packageData
    );
    if (temporaryInspection.kind !== "matching") {
      throw new VaultPublicationError("verification", "写入后的完整性检查未通过。");
    }
    if (await adapter.exists(target)) {
      throw new VaultPublicationError("conflict", "发布过程中目标位置发生了变化。");
    }
    await adapter.rename(temporary, target);
    const finalInspection = await inspectPublicationTarget(adapter, target, request.packageData);
    if (finalInspection.kind !== "matching") {
      throw new VaultPublicationError("verification", "发布后的完整性检查未通过。");
    }
    return "written";
  } finally {
    if (await adapter.exists(temporary)) {
      await adapter.rmdir(temporary, true);
    }
  }
}

export async function chooseNewPublicationPath(
  adapter: DataAdapter,
  originalRelativePath: string
): Promise<string> {
  const original = safeRelativePath(originalRelativePath);
  const parent = parentPath(original);
  const name = leafName(original);
  for (let index = 1; index <= 999; index += 1) {
    const suffix = index === 1 ? "（新）" : `（新 ${index.toString()}）`;
    const candidate = `${parent ? `${parent}/` : ""}${name}${suffix}`;
    if (!(await adapter.exists(candidate))) {
      return candidate;
    }
  }
  throw new VaultPublicationError("conflict", "无法找到可用的新位置。");
}

function buildNoteHighlights(
  currentBytes: Uint8Array | null,
  workerBytes: Uint8Array | null
): Pick<
  PublicationConflictDiff,
  "currentNoteHighlights" | "workerNoteHighlights"
> {
  const current = currentBytes ? meaningfulLines(currentBytes) : [];
  const worker = workerBytes ? meaningfulLines(workerBytes) : [];
  const currentSet = new Set(current);
  const workerSet = new Set(worker);
  return {
    currentNoteHighlights: current.filter((line) => !workerSet.has(line)).slice(0, 3),
    workerNoteHighlights: worker.filter((line) => !currentSet.has(line)).slice(0, 3)
  };
}

function meaningfulLines(bytes: Uint8Array): string[] {
  const decoded = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  return decoded
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#"))
    .map((line) => line.replace(/^[-*]\s+/, ""));
}

async function ensureFolderPath(adapter: DataAdapter, folder: string): Promise<void> {
  if (!folder) {
    return;
  }
  let current = "";
  for (const part of folder.split("/")) {
    current = current ? `${current}/${part}` : part;
    const stat = await adapter.stat(current);
    if (stat === null) {
      await adapter.mkdir(current);
    } else if (stat.type !== "folder") {
      throw new VaultPublicationError("unsafe", "发布路径中存在同名文件。");
    }
  }
}

function safeRelativePath(value: string): string {
  if (
    !value ||
    value.startsWith("/") ||
    value.includes("\\") ||
    value.includes("\0") ||
    value.split("/").some((part) => !part || part === "." || part === "..")
  ) {
    throw new VaultPublicationError("unsafe", "发布位置不是安全的 Vault 相对路径。");
  }
  const normalized = normalizePath(value);
  if (normalized !== value) {
    throw new VaultPublicationError("unsafe", "发布位置必须是规范的 Vault 相对路径。");
  }
  return normalized;
}

function parentPath(value: string): string {
  const index = value.lastIndexOf("/");
  return index === -1 ? "" : value.slice(0, index);
}

function leafName(value: string): string {
  return value.slice(value.lastIndexOf("/") + 1);
}

function exactArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength
  ) as ArrayBuffer;
}

function emptyConflictDiff(
  changedFiles: readonly string[],
  missingFiles: readonly string[],
  extraEntries: readonly string[]
): PublicationConflictDiff {
  return {
    changedFiles,
    missingFiles,
    extraEntries,
    currentNoteHighlights: [],
    workerNoteHighlights: []
  };
}
