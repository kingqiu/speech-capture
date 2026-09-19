import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { beforeAll, describe, expect, it, vi } from "vitest";

import type { DownloadedPublicationPackage } from "../src/publication-client";

vi.mock("obsidian", () => ({
  normalizePath: (value: string) => value.replace(/\/{2,}/g, "/").replace(/^\.\//, "")
}));

let publication: typeof import("../src/vault-publication");

beforeAll(async () => {
  publication = await import("../src/vault-publication");
});

describe("Vault publication", () => {
  it("writes the complete package through a sibling temporary directory and verifies it", async () => {
    const adapter = new MemoryAdapter();
    const packageData = syntheticPackage();
    const target = "语音笔记/2026-08-03-合成会议";

    await expect(
      publication.writePublicationPackage(adapter as never, {
        targetRelativePath: target,
        leaseId: "lease_synthetic",
        packageData
      })
    ).resolves.toBe("written");

    await expect(
      publication.inspectPublicationTarget(adapter as never, target, packageData)
    ).resolves.toEqual({ kind: "matching" });
    expect(adapter.renameCalls).toEqual([
      ["语音笔记/.2026-08-03-合成会议.lease_synthetic.tmp", target]
    ]);
    expect(adapter.allPaths().some((path) => path.includes(".tmp"))).toBe(false);
  });

  it("preserves an existing target and exposes concise Note differences", async () => {
    const adapter = new MemoryAdapter();
    const packageData = syntheticPackage();
    const target = "语音笔记/2026-08-03-合成会议";
    await publication.writePublicationPackage(adapter as never, {
      targetRelativePath: target,
      leaseId: "lease_initial",
      packageData
    });
    await adapter.writeBinary(
      `${target}/note.md`,
      exactBuffer(new TextEncoder().encode("# 合成会议\n\n- 用户保留的人工补充。"))
    );

    const inspection = await publication.inspectPublicationTarget(
      adapter as never,
      target,
      packageData
    );

    expect(inspection.kind).toBe("conflict");
    if (inspection.kind === "conflict") {
      expect(inspection.diff.changedFiles).toEqual(["note.md"]);
      expect(inspection.diff.currentNoteHighlights).toEqual(["用户保留的人工补充。"]);
      expect(inspection.diff.workerNoteHighlights).toEqual(["Worker 待发布的合成结论。"]);
    }
    await expect(
      publication.writePublicationPackage(adapter as never, {
        targetRelativePath: target,
        leaseId: "lease_conflict",
        packageData
      })
    ).rejects.toMatchObject({ kind: "conflict" });
    expect(new TextDecoder().decode(new Uint8Array(await adapter.readBinary(`${target}/note.md`))))
      .toContain("用户保留的人工补充");
  });

  it("chooses a new sibling location without changing the original target", async () => {
    const adapter = new MemoryAdapter();
    const target = "语音笔记/2026-08-03-合成会议";
    await adapter.mkdir("语音笔记");
    await adapter.mkdir(target);
    await adapter.mkdir(`${target}（V2）`);

    await expect(
      publication.chooseNewPublicationPath(adapter as never, target)
    ).resolves.toBe(`${target}（V2-2）`);
    await expect(
      publication.chooseNewPublicationPath(adapter as never, target, 3)
    ).resolves.toBe(`${target}（V3）`);
  });

  it("publishes an accepted replacement to a verified new path and leaves the old Note intact", async () => {
    const adapter = new MemoryAdapter();
    const originalPackage = syntheticPackage("旧版 Note");
    const replacementPackage = syntheticPackage("人工确认后的新版 Note");
    const originalTarget = "语音笔记/2026-08-03-合成会议";
    await publication.writePublicationPackage(adapter as never, {
      targetRelativePath: originalTarget,
      leaseId: "lease_original",
      packageData: originalPackage
    });

    const inspection = await publication.inspectPublicationTarget(
      adapter as never,
      originalTarget,
      replacementPackage
    );
    expect(inspection.kind).toBe("conflict");

    const replacementTarget = await publication.chooseNewPublicationPath(
      adapter as never,
      originalTarget
    );
    await publication.writePublicationPackage(adapter as never, {
      targetRelativePath: replacementTarget,
      leaseId: "lease_replacement",
      packageData: replacementPackage
    });

    const oldNote = new TextDecoder().decode(
      new Uint8Array(await adapter.readBinary(`${originalTarget}/note.md`))
    );
    const newNote = new TextDecoder().decode(
      new Uint8Array(await adapter.readBinary(`${replacementTarget}/note.md`))
    );
    expect(oldNote).toContain("旧版 Note");
    expect(newNote).toContain("人工确认后的新版 Note");
    await expect(
      publication.inspectPublicationTarget(
        adapter as never,
        replacementTarget,
        replacementPackage
      )
    ).resolves.toEqual({ kind: "matching" });
  });

  it("removes only its own temporary directory when writing fails", async () => {
    const adapter = new MemoryAdapter("note.evidence.md");
    const target = "语音笔记/2026-08-03-合成会议";

    await expect(
      publication.writePublicationPackage(adapter as never, {
        targetRelativePath: target,
        leaseId: "lease_failure",
        packageData: syntheticPackage()
      })
    ).rejects.toThrow("synthetic write failure");

    expect(await adapter.exists(target)).toBe(false);
    expect(adapter.allPaths().some((path) => path.includes("lease_failure"))).toBe(false);
  });
});

function syntheticPackage(noteText = "Worker 待发布的合成结论。"): DownloadedPublicationPackage {
  const names = [
    "transcript.raw.json",
    "transcript.md",
    "speech-record.json",
    "note.md",
    "note.evidence.md",
    "timeline.md",
    "artifact-manifest.json"
  ] as const;
  const files = names.map((name) => {
    const bytes = new TextEncoder().encode(
      name === "note.md"
        ? `# 合成会议\n\n- ${noteText}`
        : `{\"file\":\"${name}\"}`
    );
    return {
      name,
      mediaType: name.endsWith(".md") ? "text/markdown" : "application/json",
      sha256: bytesToHex(sha256(bytes)),
      bytes
    } as const;
  });
  return {
    jobId: `job_${"a".repeat(32)}`,
    speechId: `sp_${"b".repeat(32)}`,
    manifestSha256: files.at(-1)!.sha256,
    files
  };
}

class MemoryAdapter {
  private readonly folders = new Set<string>([""]);
  private readonly files = new Map<string, Uint8Array>();
  public readonly renameCalls: Array<[string, string]> = [];

  public constructor(private readonly failOnFile: string | null = null) {}

  public async exists(path: string): Promise<boolean> {
    return this.folders.has(path) || this.files.has(path);
  }

  public async stat(path: string): Promise<{ type: "file" | "folder"; ctime: number; mtime: number; size: number } | null> {
    if (this.folders.has(path)) {
      return { type: "folder", ctime: 0, mtime: 0, size: 0 };
    }
    const file = this.files.get(path);
    return file ? { type: "file", ctime: 0, mtime: 0, size: file.byteLength } : null;
  }

  public async list(path: string): Promise<{ files: string[]; folders: string[] }> {
    const prefix = path ? `${path}/` : "";
    const files = [...this.files.keys()].filter((item) => directChild(item, prefix));
    const folders = [...this.folders].filter((item) => item && directChild(item, prefix));
    return { files, folders };
  }

  public async readBinary(path: string): Promise<ArrayBuffer> {
    const bytes = this.files.get(path);
    if (!bytes) {
      throw new Error(`Missing synthetic file: ${path}`);
    }
    return exactBuffer(bytes);
  }

  public async writeBinary(path: string, data: ArrayBuffer): Promise<void> {
    if (this.failOnFile && path.endsWith(`/${this.failOnFile}`)) {
      throw new Error("synthetic write failure");
    }
    this.files.set(path, new Uint8Array(data.slice(0)));
  }

  public async mkdir(path: string): Promise<void> {
    this.folders.add(path);
  }

  public async rename(source: string, target: string): Promise<void> {
    this.renameCalls.push([source, target]);
    const sourcePrefix = `${source}/`;
    const folderMoves = [...this.folders].filter(
      (item) => item === source || item.startsWith(sourcePrefix)
    );
    const fileMoves = [...this.files.entries()].filter(([item]) => item.startsWith(sourcePrefix));
    for (const item of folderMoves) {
      this.folders.delete(item);
      this.folders.add(`${target}${item.slice(source.length)}`);
    }
    for (const [item, bytes] of fileMoves) {
      this.files.delete(item);
      this.files.set(`${target}${item.slice(source.length)}`, bytes);
    }
  }

  public async rmdir(path: string, recursive: boolean): Promise<void> {
    const prefix = `${path}/`;
    if (recursive) {
      for (const item of [...this.files.keys()]) {
        if (item.startsWith(prefix)) this.files.delete(item);
      }
      for (const item of [...this.folders]) {
        if (item === path || item.startsWith(prefix)) this.folders.delete(item);
      }
    }
  }

  public allPaths(): string[] {
    return [...this.folders, ...this.files.keys()];
  }
}

function directChild(value: string, prefix: string): boolean {
  return value.startsWith(prefix) && !value.slice(prefix.length).includes("/");
}

function exactBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}
