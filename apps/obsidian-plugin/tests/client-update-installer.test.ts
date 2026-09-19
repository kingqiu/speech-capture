import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { mkdir, readFile, rm, utimes, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const runtime = vi.hoisted(() => ({ home: "", vault: "", spawn: vi.fn() }));
const obsidian = vi.hoisted(() => {
  class SyntheticFileSystemAdapter {
    public getBasePath(): string {
      return runtime.vault;
    }
  }
  return { SyntheticFileSystemAdapter };
});

vi.mock("node:os", () => ({ homedir: () => runtime.home }));
vi.mock("node:child_process", () => ({ spawn: runtime.spawn }));
vi.mock("obsidian", () => ({ FileSystemAdapter: obsidian.SyntheticFileSystemAdapter }));
vi.mock("../scripts/client-update-helper.zsh", () => ({
  default: "#!/bin/zsh\nexit 0\n"
}));

import {
  reconcileClientUpdate,
  launchClientUpdateHelper,
  stageClientUpdate
} from "../src/client-update-installer";
import type { ConfirmedClientRelease } from "../src/client-update";

let temporaryRoot = "";

afterEach(async () => {
  if (temporaryRoot) {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
  temporaryRoot = "";
  runtime.spawn.mockReset();
});

describe("client update installer staging", () => {
  it("waits for spawn acknowledgement before claiming the helper is running", async () => {
    const setup = await makeSetup();
    const plan = await stageClientUpdate(setup.app as never, "0.1.25", candidate());
    const child = syntheticChild();
    runtime.spawn.mockReturnValue(child);
    const pending = launchClientUpdateHelper(plan);
    expect(child.unref).not.toHaveBeenCalled();
    child.emit("spawn");
    await expect(pending).resolves.toBeUndefined();
    expect(child.unref).toHaveBeenCalledOnce();
  });

  it.each(["async", "sync"])("handles %s launch failure without an unhandled error or payload leak", async (mode) => {
    const setup = await makeSetup();
    const plan = await stageClientUpdate(setup.app as never, "0.1.25", candidate());
    const child = syntheticChild();
    const failure = new Error("synthetic private path must not leak");
    if (mode === "sync") runtime.spawn.mockImplementation(() => { throw failure; });
    else runtime.spawn.mockReturnValue(child);
    const pending = launchClientUpdateHelper(plan);
    const rejected = expect(pending).rejects.toThrow("无法启动退出后安装助手");
    if (mode === "async") child.emit("error", failure);
    await rejected;
    expect(child.unref).not.toHaveBeenCalled();
    expect(await readFile(join(setup.plugin, "main.js"), "utf8")).toBe("old-main");
    await expect(readFile(plan.helperPath)).rejects.toThrow();
    await expect(readFile(plan.requestPath)).rejects.toThrow();
    const status = await readFile(join(plan.requestPath, "..", "status.json"), "utf8");
    expect(JSON.parse(status)).toMatchObject({ state: "failed", error_code: "HELPER_LAUNCH_FAILED" });
    expect(status).not.toContain("private path");
  });

  it("stages a fixed request outside the Vault without changing the active plugin", async () => {
    const setup = await makeSetup();
    const before = await readFile(join(setup.plugin, "main.js"), "utf8");

    const plan = await stageClientUpdate(setup.app as never, "0.1.25", candidate());

    expect(plan.targetVersion).toBe("0.1.26");
    expect(plan.requestPath.startsWith(setup.vault)).toBe(false);
    expect(await readFile(join(setup.plugin, "main.js"), "utf8")).toBe(before);
    const request = JSON.parse(await readFile(plan.requestPath, "utf8")) as Record<
      string,
      unknown
    >;
    expect(request).toMatchObject({
      plugin_id: "speech-capture",
      from_version: "0.1.25",
      to_version: "0.1.26",
      vault_path: setup.vault,
      config_dir_name: ".obsidian",
      reopen: true
    });
    expect(Object.keys(request).sort()).toEqual([
      "archive_path",
      "archive_sha256",
      "config_dir_name",
      "current_main_sha256",
      "from_version",
      "main_sha256",
      "min_app_version",
      "plugin_id",
      "reopen",
      "schema_version",
      "to_version",
      "transaction_id",
      "vault_path",
      "vault_scope_sha256"
    ]);
  });

  it("marks a restarted plugin loaded only after disk main.js matches", async () => {
    const setup = await makeSetup();
    const release = candidate();
    const plan = await stageClientUpdate(setup.app as never, "0.1.25", release);
    await writeFile(join(setup.plugin, "main.js"), release.archiveBytes, {
      mode: 0o600
    });
    await writeFile(
      join(setup.plugin, "manifest.json"),
      JSON.stringify({ id: "speech-capture", version: "0.1.26" }),
      { encoding: "utf8" }
    );
    await writeFile(
      join(plan.requestPath, "..", "status.json"),
      `${JSON.stringify({
        schema_version: 1,
        transaction_id: plan.transactionId,
        plugin_id: "speech-capture",
        vault_scope_sha256: bytesToHex(
          sha256(new TextEncoder().encode(`${setup.vault}\u0000.obsidian`))
        ),
        from_version: "0.1.25",
        to_version: "0.1.26",
        main_sha256: release.release.main_sha256,
        state: "restart_required",
        phase: "restart_required",
        error_code: null,
        rolled_back: false
      })}\n`,
      { encoding: "utf8", mode: 0o600 }
    );

    await expect(
      reconcileClientUpdate(setup.app as never, "0.1.26")
    ).resolves.toEqual({
      state: "loaded_verified",
      previousVersion: "0.1.25",
      targetVersion: "0.1.26"
    });
    const status = JSON.parse(
      await readFile(join(plan.requestPath, "..", "status.json"), "utf8")
    ) as { state: string };
    expect(status.state).toBe("loaded_verified");
    await expect(readFile(plan.helperPath)).rejects.toThrow();
    await expect(
      readFile(
        join(
          plan.requestPath,
          "..",
          `speech-capture-${release.release.version}-alpha.zip`
        )
      )
    ).rejects.toThrow();
  });

  it("does not accept restart_required when the active main.js differs", async () => {
    const setup = await makeSetup();
    const release = candidate();
    const plan = await stageClientUpdate(setup.app as never, "0.1.25", release);
    await writeFile(
      join(plan.requestPath, "..", "status.json"),
      `${JSON.stringify({
        schema_version: 1,
        transaction_id: plan.transactionId,
        plugin_id: "speech-capture",
        vault_scope_sha256: bytesToHex(
          sha256(new TextEncoder().encode(`${setup.vault}\u0000.obsidian`))
        ),
        from_version: "0.1.25",
        to_version: "0.1.26",
        main_sha256: release.release.main_sha256,
        state: "restart_required",
        phase: "restart_required",
        error_code: null,
        rolled_back: false
      })}\n`,
      { encoding: "utf8", mode: 0o600 }
    );

    await expect(
      reconcileClientUpdate(setup.app as never, "0.1.26")
    ).resolves.toBeNull();
  });

  it("reconciles the latest status for this Vault instead of a newer foreign Vault", async () => {
    const setup = await makeSetup();
    const release = candidate();
    const plan = await stageClientUpdate(setup.app as never, "0.1.25", release);
    await writeFile(join(setup.plugin, "main.js"), release.archiveBytes, {
      mode: 0o600
    });
    await writeFile(
      join(setup.plugin, "manifest.json"),
      JSON.stringify({ id: "speech-capture", version: "0.1.26" }),
      { encoding: "utf8" }
    );
    const matchingStatusPath = join(plan.requestPath, "..", "status.json");
    await writeFile(
      matchingStatusPath,
      statusJson({
        transactionId: plan.transactionId,
        vaultScopeSha256: scopeFor(setup.vault),
        mainSha256: release.release.main_sha256
      }),
      { encoding: "utf8", mode: 0o600 }
    );

    const foreignTransactionId = `update_${"f".repeat(32)}`;
    const foreignRoot = join(
      setup.home,
      "Library",
      "Application Support",
      "Speech Capture",
      "Client Updates",
      foreignTransactionId
    );
    await mkdir(foreignRoot, { recursive: true, mode: 0o700 });
    const foreignStatusPath = join(foreignRoot, "status.json");
    await writeFile(
      foreignStatusPath,
      statusJson({
        transactionId: foreignTransactionId,
        vaultScopeSha256: "f".repeat(64),
        mainSha256: release.release.main_sha256
      }),
      { encoding: "utf8", mode: 0o600 }
    );
    const future = new Date(Date.now() + 60_000);
    await utimes(foreignStatusPath, future, future);

    await expect(
      reconcileClientUpdate(setup.app as never, "0.1.26")
    ).resolves.toEqual({
      state: "loaded_verified",
      previousVersion: "0.1.25",
      targetVersion: "0.1.26"
    });
  });

  it("reports a pre-mutation helper failure after the existing plugin reopens", async () => {
    const setup = await makeSetup();
    const plan = await stageClientUpdate(
      setup.app as never,
      "0.1.25",
      candidate()
    );
    await writeFile(
      join(plan.requestPath, "..", "status.json"),
      statusJson({
        transactionId: plan.transactionId,
        vaultScopeSha256: scopeFor(setup.vault),
        mainSha256: candidate().release.main_sha256,
        state: "failed",
        errorCode: "INSUFFICIENT_SPACE"
      }),
      { encoding: "utf8", mode: 0o600 }
    );

    await expect(
      reconcileClientUpdate(setup.app as never, "0.1.25")
    ).resolves.toEqual({
      state: "install_failed",
      targetVersion: "0.1.26",
      errorCode: "INSUFFICIENT_SPACE"
    });
  });
});

function syntheticChild() {
  const listeners = new Map<string, (error?: Error) => void>();
  return {
    unref: vi.fn(),
    once: (event: string, listener: (error?: Error) => void) => listeners.set(event, listener),
    emit(event: string, error?: Error) {
      const listener = listeners.get(event);
      if (!listener) throw new Error(`Unhandled child event: ${event}`);
      listeners.delete(event);
      listener(error);
    }
  };
}

function statusJson({
  transactionId,
  vaultScopeSha256,
  mainSha256,
  state = "restart_required",
  errorCode = null
}: {
  readonly transactionId: string;
  readonly vaultScopeSha256: string;
  readonly mainSha256: string;
  readonly state?: "restart_required" | "failed";
  readonly errorCode?: string | null;
}): string {
  return `${JSON.stringify({
    schema_version: 1,
    transaction_id: transactionId,
    plugin_id: "speech-capture",
    vault_scope_sha256: vaultScopeSha256,
    from_version: "0.1.25",
    to_version: "0.1.26",
    main_sha256: mainSha256,
    state,
    phase: state,
    error_code: errorCode,
    rolled_back: false
  })}\n`;
}

function scopeFor(vault: string, configDir = ".obsidian"): string {
  return bytesToHex(
    sha256(new TextEncoder().encode(`${vault}\u0000${configDir}`))
  );
}

async function makeSetup() {
  temporaryRoot = `/private/tmp/speech-capture-installer-ts-${globalThis.crypto.randomUUID()}`;
  const home = join(temporaryRoot, "home");
  const vault = join(temporaryRoot, "vault");
  const plugin = join(vault, ".obsidian", "plugins", "speech-capture");
  await mkdir(home, { recursive: true, mode: 0o700 });
  await mkdir(plugin, { recursive: true, mode: 0o700 });
  await writeFile(join(plugin, "main.js"), "old-main", { encoding: "utf8" });
  await writeFile(
    join(plugin, "manifest.json"),
    JSON.stringify({ id: "speech-capture", version: "0.1.25" }),
    { encoding: "utf8" }
  );
  runtime.home = home;
  runtime.vault = vault;
  return {
    app: {
      vault: {
        adapter: new obsidian.SyntheticFileSystemAdapter(),
        configDir: ".obsidian"
      }
    },
    home,
    plugin,
    vault
  };
}

function candidate(): ConfirmedClientRelease {
  const bytes = new TextEncoder().encode("new-main");
  const mainSha256 = bytesToHex(sha256(bytes));
  return {
    release: {
      schema_version: 1,
      plugin_id: "speech-capture",
      version: "0.1.26",
      min_app_version: "1.11.4",
      desktop_only: true,
      archive: {
        filename: "speech-capture-0.1.26-alpha.zip",
        sha256: bytesToHex(sha256(bytes)),
        size_bytes: bytes.byteLength
      },
      main_sha256: mainSha256,
      release_manifest_sha256: "c".repeat(64)
    },
    verification: {
      version: "0.1.26",
      minAppVersion: "1.11.4",
      archiveFilename: "speech-capture-0.1.26-alpha.zip",
      archiveSizeBytes: bytes.byteLength,
      archiveSha256: bytesToHex(sha256(bytes)),
      releaseManifestSha256: "c".repeat(64),
      fileSha256: {
        "main.js": mainSha256,
        "manifest.json": "d".repeat(64),
        "styles.css": "e".repeat(64)
      }
    },
    archiveBytes: bytes
  };
}
