import { describe, expect, it } from "vitest";

import {
  canManageJobData,
  requiresPublishedFolderCleanup,
  safePublishedFolderPath
} from "../src/record-management";

describe("record management", () => {
  it("only exposes destructive actions for terminal jobs", () => {
    expect(canManageJobData("published")).toBe(true);
    expect(canManageJobData("processed")).toBe(true);
    expect(canManageJobData("failed")).toBe(true);
    expect(canManageJobData("transcribing")).toBe(false);
    expect(canManageJobData("queued")).toBe(false);
  });

  it("only resolves a Vault publication folder for published jobs", () => {
    expect(requiresPublishedFolderCleanup("published")).toBe(true);
    expect(requiresPublishedFolderCleanup("processed")).toBe(false);
    expect(requiresPublishedFolderCleanup("partial")).toBe(false);
    expect(requiresPublishedFolderCleanup("failed")).toBe(false);
    expect(requiresPublishedFolderCleanup("cancelled")).toBe(false);
  });

  it("only accepts a published folder below the configured output root", () => {
    expect(
      safePublishedFolderPath(
        "Speech Notes/2026/08/meeting--sp_123",
        "Speech Notes"
      )
    ).toBe("Speech Notes/2026/08/meeting--sp_123");
    expect(safePublishedFolderPath("Speech Notes", "Speech Notes")).toBeNull();
    expect(
      safePublishedFolderPath("Other Notes/meeting", "Speech Notes")
    ).toBeNull();
    expect(
      safePublishedFolderPath("Speech Notes/../Private", "Speech Notes")
    ).toBeNull();
  });
});
