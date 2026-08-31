import { describe, expect, it } from "vitest";

import {
  DEFAULT_SETTINGS,
  parseSettings,
  remoteWorkerFromDraft
} from "../src/settings";

describe("parseSettings", () => {
  it("uses private-data-free defaults", () => {
    expect(parseSettings(undefined)).toEqual(DEFAULT_SETTINGS);
    expect(JSON.stringify(DEFAULT_SETTINGS)).not.toContain("token");
    expect(DEFAULT_SETTINGS.schemaVersion).toBe(2);
    expect(DEFAULT_SETTINGS.vaultIdsByWorker).toEqual({});
    expect(DEFAULT_SETTINGS.workers).toEqual([
      {
        id: "local-worker",
        displayName: "这台 Mac",
        endpoint: "http://127.0.0.1:8765",
        kind: "local"
      }
    ]);
  });

  it("accepts private TLS and loopback endpoints without credentials", () => {
    const settings = parseSettings({
      preferredWorkerId: "home",
      vaultIdsByWorker: {
        home: "vault_home",
        unknown: "vault_unknown",
        local: "not safe"
      },
      preferredProfile: "speed",
      outputFolder: "  Speech Notes  ",
      leftSidebarCollapsed: true,
      workers: [
        {
          id: "home",
          displayName: "书房 Mac",
          endpoint: "https://worker.example.test/",
          kind: "remote"
        },
        {
          id: "local",
          displayName: "这台 Mac",
          endpoint: "http://127.0.0.1:8765/",
          kind: "local"
        }
      ]
    });

    expect(settings.preferredWorkerId).toBe("home");
    expect(settings.preferredProfile).toBe("speed");
    expect(settings.outputFolder).toBe("Speech Notes");
    expect(settings.leftSidebarCollapsed).toBe(true);
    expect(settings.vaultIdsByWorker).toEqual({ home: "vault_home" });
    expect(settings.workers.map((worker) => worker.endpoint)).toEqual([
      "https://worker.example.test",
      "http://127.0.0.1:8765"
    ]);
  });

  it("migrates the legacy Vault authorization to the preferred Worker", () => {
    const settings = parseSettings({
      vaultId: "vault_legacy",
      preferredWorkerId: "home",
      workers: [
        {
          id: "home",
          displayName: "书房 Mac",
          endpoint: "https://worker.example.test",
          kind: "remote"
        }
      ]
    });

    expect(settings.vaultIdsByWorker).toEqual({ home: "vault_legacy" });
    expect("vaultId" in settings).toBe(false);
  });

  it("normalizes safe remote Worker drafts without storing credentials", () => {
    const first = remoteWorkerFromDraft(
      "  书房 Mac  ",
      "https://speech-worker.example.test/"
    );
    const second = remoteWorkerFromDraft(
      "书房 Mac",
      "https://speech-worker.example.test"
    );

    expect(first).toEqual(second);
    expect(first).toMatchObject({
      ok: true,
      worker: {
        displayName: "书房 Mac",
        endpoint: "https://speech-worker.example.test",
        kind: "remote"
      }
    });
    expect(JSON.stringify(first)).not.toContain("token");
  });

  it("rejects unsafe remote Worker drafts", () => {
    expect(remoteWorkerFromDraft("", "https://worker.example.test")).toEqual({
      ok: false,
      reason: "name_required"
    });
    expect(
      remoteWorkerFromDraft("书房 Mac", "http://worker.example.test")
    ).toEqual({ ok: false, reason: "invalid_endpoint" });
    expect(
      remoteWorkerFromDraft(
        "书房 Mac",
        "https://user:secret@worker.example.test"
      )
    ).toEqual({ ok: false, reason: "invalid_endpoint" });
  });

  it("rejects insecure remote and credential-bearing endpoints", () => {
    const settings = parseSettings({
      preferredWorkerId: "unsafe",
      workers: [
        {
          id: "unsafe",
          displayName: "不安全 Worker",
          endpoint: "http://worker.example.test",
          kind: "remote"
        },
        {
          id: "credential",
          displayName: "含凭据 Worker",
          endpoint: "https://user:password@worker.example.test",
          kind: "remote"
        }
      ]
    });

    expect(settings.workers).toEqual([
      {
        id: "local-worker",
        displayName: "这台 Mac",
        endpoint: "http://127.0.0.1:8765",
        kind: "local"
      }
    ]);
    expect(settings.preferredWorkerId).toBe("local-worker");
  });

  it("migrates a legacy Vault authorization to the effective local fallback", () => {
    const settings = parseSettings({
      schemaVersion: 1,
      vaultId: "vault_local",
      preferredWorkerId: null,
      workers: [
        {
          id: "local-worker",
          displayName: "这台 Mac",
          endpoint: "http://127.0.0.1:8765",
          kind: "local"
        }
      ]
    });

    expect(settings.preferredWorkerId).toBe("local-worker");
    expect(settings.vaultIdsByWorker).toEqual({
      "local-worker": "vault_local"
    });
  });
});
