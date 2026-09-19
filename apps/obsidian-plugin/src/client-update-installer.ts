import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { spawn as nodeSpawn } from "node:child_process";
import {
  chmod,
  lstat,
  mkdir,
  readFile,
  readdir,
  rename,
  rm,
  writeFile
} from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { FileSystemAdapter, type App } from "obsidian";

import helperSource from "../scripts/client-update-helper.zsh";
import type { ConfirmedClientRelease } from "./client-update";

const UPDATE_ROOT_COMPONENTS = [
  "Library",
  "Application Support",
  "Speech Capture",
  "Client Updates"
] as const;
const MAX_STATUS_BYTES = 16 * 1024;

export interface PreparedClientUpdate {
  readonly transactionId: string;
  readonly targetVersion: string;
  readonly requestPath: string;
  readonly helperPath: string;
}

export type ReconciledClientUpdate =
  | {
      readonly state: "loaded_verified";
      readonly previousVersion: string;
      readonly targetVersion: string;
    }
  | {
      readonly state: "rolled_back";
      readonly targetVersion: string;
      readonly errorCode: string;
    }
  | {
      readonly state: "install_failed";
      readonly targetVersion: string;
      readonly errorCode: string;
    }
  | null;

export class ClientUpdateInstallerError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "ClientUpdateInstallerError";
  }
}

export async function stageClientUpdate(
  app: App,
  currentVersion: string,
  candidate: ConfirmedClientRelease
): Promise<PreparedClientUpdate> {
  const vault = vaultContext(app);
  const transactionId = `update_${globalThis.crypto.randomUUID().replaceAll("-", "")}`;
  const updateRoot = clientUpdateRoot();
  const transactionRoot = join(updateRoot, transactionId);
  const archiveFilename = `speech-capture-${candidate.release.version}-alpha.zip`;
  const vaultScopeSha256 = vaultScope(vault);
  if (candidate.release.archive.filename !== archiveFilename) {
    throw new ClientUpdateInstallerError("候选安装包文件名与版本不一致。");
  }
  await ensureDirectory(updateRoot, 0o700);
  try {
    await mkdir(transactionRoot, { mode: 0o700 });
  } catch {
    throw new ClientUpdateInstallerError("无法创建插件更新暂存目录。");
  }
  const archivePath = join(transactionRoot, archiveFilename);
  const helperPath = join(transactionRoot, "helper.zsh");
  const requestPath = join(transactionRoot, "request.json");
  const statusPath = join(transactionRoot, "status.json");
  try {
    const currentMainPath = join(
      vault.basePath,
      vault.configDir,
      "plugins",
      "speech-capture",
      "main.js"
    );
    const currentMain = await readRegularFile(currentMainPath, 16 * 1024 * 1024);
    await writeFile(archivePath, candidate.archiveBytes, { flag: "wx", mode: 0o600 });
    await writeFile(helperPath, helperSource, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o700
    });
    await chmod(helperPath, 0o700);
    await writeFile(
      statusPath,
      `${JSON.stringify({
        schema_version: 1,
        transaction_id: transactionId,
        plugin_id: "speech-capture",
        vault_scope_sha256: vaultScopeSha256,
        from_version: currentVersion,
        to_version: candidate.release.version,
        main_sha256: candidate.release.main_sha256,
        state: "ready_to_apply",
        phase: "ready_to_apply",
        error_code: null,
        rolled_back: false
      })}\n`,
      { encoding: "utf8", flag: "wx", mode: 0o600 }
    );
    await writeFile(
      requestPath,
      `${JSON.stringify(
        {
          schema_version: 1,
          transaction_id: transactionId,
          plugin_id: "speech-capture",
          from_version: currentVersion,
          to_version: candidate.release.version,
          min_app_version: candidate.release.min_app_version,
          vault_path: vault.basePath,
          config_dir_name: vault.configDir,
          vault_scope_sha256: vaultScopeSha256,
          archive_path: archivePath,
          archive_sha256: candidate.release.archive.sha256,
          main_sha256: candidate.release.main_sha256,
          current_main_sha256: bytesToHex(sha256(currentMain)),
          reopen: true
        },
        null,
        2
      )}\n`,
      { encoding: "utf8", flag: "wx", mode: 0o600 }
    );
    return {
      transactionId,
      targetVersion: candidate.release.version,
      requestPath,
      helperPath
    };
  } catch (error) {
    await rm(transactionRoot, { recursive: true, force: true });
    if (error instanceof ClientUpdateInstallerError) {
      throw error;
    }
    throw new ClientUpdateInstallerError("无法安全暂存插件更新文件。");
  }
}

export function launchClientUpdateHelper(plan: PreparedClientUpdate): void {
  const spawn = nodeSpawn as unknown as (
    command: string,
    args: readonly string[],
    options: {
      readonly detached: boolean;
      readonly stdio: "ignore";
    }
  ) => { unref(): void };
  try {
    const child = spawn("/bin/zsh", [plan.helperPath, plan.requestPath], {
      detached: true,
      stdio: "ignore"
    });
    child.unref();
  } catch {
    throw new ClientUpdateInstallerError("无法启动退出后安装助手。");
  }
}

export async function reconcileClientUpdate(
  app: App,
  currentVersion: string
): Promise<ReconciledClientUpdate> {
  const vault = vaultContext(app);
  const currentVaultScope = vaultScope(vault);
  const status = await latestStatus(currentVaultScope);
  if (status === null) {
    return null;
  }
  if (status.state === "failed") {
    if (status.from_version !== currentVersion) {
      return null;
    }
    await discardTransactionPayload(status);
    return status.rolled_back
      ? {
          state: "rolled_back",
          targetVersion: status.to_version,
          errorCode: status.error_code ?? "UNKNOWN"
        }
      : {
          state: "install_failed",
          targetVersion: status.to_version,
          errorCode: status.error_code ?? "UNKNOWN"
        };
  }
  if (status.state !== "restart_required" && status.state !== "loaded_verified") {
    return null;
  }
  if (status.to_version !== currentVersion) {
    return null;
  }
  const activePlugin = join(
    vault.basePath,
    vault.configDir,
    "plugins",
    "speech-capture"
  );
  const activeMain = await readRegularFile(
    join(activePlugin, "main.js"),
    16 * 1024 * 1024
  );
  const activeManifest = parseActiveManifest(
    await readRegularFile(join(activePlugin, "manifest.json"), 64 * 1024)
  );
  if (
    activeManifest.id !== "speech-capture" ||
    activeManifest.version !== currentVersion ||
    bytesToHex(sha256(activeMain)) !== status.main_sha256
  ) {
    return null;
  }
  if (status.state !== "loaded_verified") {
    await writeLoadedVerifiedStatus(status);
  }
  await discardTransactionPayload(status);
  return {
    state: "loaded_verified",
    previousVersion: status.from_version,
    targetVersion: status.to_version
  };
}

function vaultContext(app: App): { readonly basePath: string; readonly configDir: string } {
  if (!(app.vault.adapter instanceof FileSystemAdapter)) {
    throw new ClientUpdateInstallerError("插件更新只支持本机文件系统 Vault。");
  }
  const basePath = app.vault.adapter.getBasePath();
  const configDir = app.vault.configDir;
  if (
    !basePath.startsWith("/") ||
    !/^[A-Za-z0-9._-]{1,80}$/.test(configDir) ||
    configDir === "." ||
    configDir === ".."
  ) {
    throw new ClientUpdateInstallerError("当前 Vault 路径或配置目录不安全。");
  }
  return { basePath, configDir };
}

function clientUpdateRoot(): string {
  return join(homedir(), ...UPDATE_ROOT_COMPONENTS);
}

async function ensureDirectory(path: string, mode: number): Promise<void> {
  await mkdir(path, { recursive: true, mode });
  const stats = await lstat(path);
  if (!stats.isDirectory() || stats.isSymbolicLink()) {
    throw new ClientUpdateInstallerError("插件更新暂存区不是安全目录。");
  }
  await chmod(path, mode);
}

async function readRegularFile(path: string, maximumBytes: number): Promise<Uint8Array> {
  let stats;
  try {
    stats = await lstat(path);
  } catch {
    throw new ClientUpdateInstallerError("无法读取当前插件文件。");
  }
  if (
    !stats.isFile() ||
    stats.isSymbolicLink() ||
    stats.size <= 0 ||
    stats.size > maximumBytes
  ) {
    throw new ClientUpdateInstallerError("当前插件文件不完整或不安全。");
  }
  return readFile(path);
}

interface ClientUpdateStatus {
  readonly path: string;
  readonly schema_version: 1;
  readonly transaction_id: string;
  readonly plugin_id: "speech-capture";
  readonly vault_scope_sha256: string;
  readonly from_version: string;
  readonly to_version: string;
  readonly main_sha256: string;
  readonly state:
    | "ready_to_apply"
    | "waiting_for_exit"
    | "applying"
    | "restart_required"
    | "loaded_verified"
    | "failed";
  readonly phase: string;
  readonly error_code: string | null;
  readonly rolled_back: boolean;
  readonly modifiedAt: number;
}

async function latestStatus(vaultScopeSha256: string): Promise<ClientUpdateStatus | null> {
  const root = clientUpdateRoot();
  let names: string[];
  try {
    names = await readdir(root);
  } catch {
    return null;
  }
  const statuses: ClientUpdateStatus[] = [];
  for (const name of names) {
    if (!/^update_[0-9a-f]{32}$/.test(name)) {
      continue;
    }
    const path = join(root, name, "status.json");
    try {
      const stats = await lstat(path);
      if (!stats.isFile() || stats.isSymbolicLink() || stats.size > MAX_STATUS_BYTES) {
        continue;
      }
      const parsed: unknown = JSON.parse(await readFile(path, "utf8"));
      if (
        isClientUpdateStatus(parsed, name) &&
        parsed.vault_scope_sha256 === vaultScopeSha256
      ) {
        statuses.push({ ...parsed, path, modifiedAt: stats.mtimeMs });
      }
    } catch {
      continue;
    }
  }
  return statuses.sort((left, right) => right.modifiedAt - left.modifiedAt)[0] ?? null;
}

function isClientUpdateStatus(
  value: unknown,
  transactionId: string
): value is Omit<ClientUpdateStatus, "path" | "modifiedAt"> {
  if (!isRecord(value)) {
    return false;
  }
  const expectedKeys = [
    "error_code",
    "from_version",
    "main_sha256",
    "phase",
    "plugin_id",
    "rolled_back",
    "schema_version",
    "state",
    "to_version",
    "transaction_id",
    "vault_scope_sha256"
  ];
  if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedKeys)) {
    return false;
  }
  return (
    value.schema_version === 1 &&
    value.transaction_id === transactionId &&
    value.plugin_id === "speech-capture" &&
    typeof value.vault_scope_sha256 === "string" &&
    /^[0-9a-f]{64}$/.test(value.vault_scope_sha256) &&
    isVersion(value.from_version) &&
    isVersion(value.to_version) &&
    typeof value.main_sha256 === "string" &&
    /^[0-9a-f]{64}$/.test(value.main_sha256) &&
    [
      "ready_to_apply",
      "waiting_for_exit",
      "applying",
      "restart_required",
      "loaded_verified",
      "failed"
    ].includes(String(value.state)) &&
    [
      "ready_to_apply",
      "waiting_for_exit",
      "validating",
      "replacing",
      "restart_required",
      "loaded_verified",
      "failed"
    ].includes(String(value.phase)) &&
    (value.error_code === null ||
      (typeof value.error_code === "string" && /^[A-Z0-9_]{1,80}$/.test(value.error_code))) &&
    typeof value.rolled_back === "boolean"
  );
}

async function writeLoadedVerifiedStatus(status: ClientUpdateStatus): Promise<void> {
  const temporary = `${status.path}.tmp`;
  const next = {
    schema_version: 1,
    transaction_id: status.transaction_id,
    plugin_id: "speech-capture",
    vault_scope_sha256: status.vault_scope_sha256,
    from_version: status.from_version,
    to_version: status.to_version,
    main_sha256: status.main_sha256,
    state: "loaded_verified",
    phase: "loaded_verified",
    error_code: null,
    rolled_back: false
  };
  await writeFile(temporary, `${JSON.stringify(next)}\n`, {
    encoding: "utf8",
    mode: 0o600
  });
  await chmod(temporary, 0o600);
  await rename(temporary, status.path);
}

async function discardTransactionPayload(status: ClientUpdateStatus): Promise<void> {
  const transactionRoot = dirname(status.path);
  await Promise.allSettled([
    rm(join(transactionRoot, `speech-capture-${status.to_version}-alpha.zip`), {
      force: true
    }),
    rm(join(transactionRoot, "helper.zsh"), { force: true }),
    rm(join(transactionRoot, "request.json"), { force: true })
  ]);
}

function isVersion(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(value)
  );
}

function vaultScope(vault: { readonly basePath: string; readonly configDir: string }): string {
  return bytesToHex(
    sha256(new TextEncoder().encode(`${vault.basePath}\u0000${vault.configDir}`))
  );
}

function parseActiveManifest(bytes: Uint8Array): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    return isRecord(value) ? value : {};
  } catch {
    return {};
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
