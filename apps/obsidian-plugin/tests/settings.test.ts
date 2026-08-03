import { describe, expect, it } from "vitest";

import { DEFAULT_SETTINGS, parseSettings } from "../src/settings";

describe("parseSettings", () => {
  it("uses private-data-free defaults", () => {
    expect(parseSettings(undefined)).toEqual(DEFAULT_SETTINGS);
    expect(JSON.stringify(DEFAULT_SETTINGS)).not.toContain("token");
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
    expect(settings.workers.map((worker) => worker.endpoint)).toEqual([
      "https://worker.example.test",
      "http://127.0.0.1:8765"
    ]);
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
    expect(settings.preferredWorkerId).toBeNull();
  });
});
